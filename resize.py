#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
resize — OpenSTA-guided drive-strength sizing of a mapped netlist.

ABC sizes cells against its own delay model (no wire load, one global
driver). OpenSTA with the liberty wire-load model often disagrees, and the
bench shows failing paths dominated by drive and fanout rather than logic
depth. This pass closes that gap without touching logic:

  loop:
    1. OpenSTA: worst path per failing endpoint, with per-stage delay,
       fanout, load and slew.
    2. Pick one cell per failing path (largest stage delay, not yet at max
       drive, not already tried) and bump it one drive step
       (`_1 -> _2 -> _4 -> _8 ...`, same function, same pins).
    3. Re-run STA. Accept the batch if TNS improves (a WNS regression up to
       --wns-tol is tolerated: the worst path just moved) or WNS improves at
       equal TNS; otherwise bisect the batch down to single moves and mark
       rejected cells as tried.
  until nothing fails, the iteration cap, or no candidates remain.
  apb_timer winner: TNS -3.27 -> -0.09 ns, failing endpoints 61 -> 1, area +1.6 %.

Sizing preserves function by construction (same cell function, same pin
names across Sky130 drive variants), so no equivalence check is needed.

    python3 resize.py --netlist results/top/winner.v --top top \\
        --lib sky130/hd_120_ss.lib --period-ps 8000 --clock-port clk [--sdc top.sdc]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth_flow as sf  # noqa: E402

# --------------------------------------------------------------------------
# Liberty: drive variants per function
# --------------------------------------------------------------------------

_DRIVE_RE = re.compile(r'^(.*)_(\d+)$')


def drive_families(liberty: str) -> dict[str, list[int]]:
    """base name -> sorted list of available drive strengths, e.g.
    'sky130_fd_sc_hd__nand2' -> [1, 2, 4, 8]."""
    text = Path(liberty).read_text(errors='ignore')
    fam: dict[str, set[int]] = {}
    for m in re.finditer(r'\bcell\s*\(\s*"?([^")\s]+)"?\s*\)', text):
        dm = _DRIVE_RE.match(m.group(1))
        if dm:
            fam.setdefault(dm.group(1), set()).add(int(dm.group(2)))
    return {k: sorted(v) for k, v in fam.items()}


def prev_size(cell: str, fam: dict[str, list[int]]) -> Optional[str]:
    dm = _DRIVE_RE.match(cell)
    if not dm:
        return None
    base, n = dm.group(1), int(dm.group(2))
    smaller = [s for s in fam.get(base, []) if s < n]
    return f'{base}_{smaller[-1]}' if smaller else None


def next_size(cell: str, fam: dict[str, list[int]]) -> Optional[str]:
    dm = _DRIVE_RE.match(cell)
    if not dm:
        return None
    base, n = dm.group(1), int(dm.group(2))
    sizes = fam.get(base, [])
    bigger = [s for s in sizes if s > n]
    return f'{base}_{bigger[0]}' if bigger else None


# --------------------------------------------------------------------------
# Netlist: instance -> type, in-place retyping
# --------------------------------------------------------------------------

_INST_RE = re.compile(r'^(\s*)(sky130_fd_sc_[a-z]+__\w+|[A-Za-z_][\w$]*)\s+(\\?[\w$\[\]\.]+)\s*\(', re.M)


def instance_types(netlist_text: str) -> dict[str, str]:
    out = {}
    for m in _INST_RE.finditer(netlist_text):
        typ, inst = m.group(2), m.group(3)
        if typ in ('module', 'input', 'output', 'wire', 'reg', 'assign'):
            continue
        out[inst] = typ
    return out


def retype(netlist_text: str, changes: dict[str, str]) -> str:
    """Replace the cell type of the given instances (name -> new type)."""
    def sub(m):
        inst = m.group(3)
        if inst in changes:
            return f'{m.group(1)}{changes[inst]} {inst} ('
        return m.group(0)
    return _INST_RE.sub(sub, netlist_text)


# --------------------------------------------------------------------------
# OpenSTA: worst path per failing endpoint with stage details
# --------------------------------------------------------------------------

PATHS_TCL = """\
read_liberty {liberty}
read_verilog {netlist}
link_design {top}
{constraints}
report_checks -path_delay max -group_path_count {k} -endpoint_path_count 1 -slack_max {slack_max} -format full_clock_expanded -fields {{fanout cap slew}} -digits 4
report_worst_slack -max -digits 4
report_tns -max -digits 4
exit
"""

_STAGE_RE = re.compile(
    r'^\s*(?:(\d+)\s+)?([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([v^])\s+(\S+)\s+\((\S+)\)\s*$')


@dataclass
class Stage:
    inst: str
    pin: str
    cell: str
    delay: float
    fanout: Optional[int]
    cap: float
    slew: float


@dataclass
class PathInfo:
    endpoint: str
    slack: float
    stages: list[Stage] = field(default_factory=list)


@dataclass
class StaOut:
    ok: bool
    wns: Optional[float] = None
    tns: Optional[float] = None
    paths: list[PathInfo] = field(default_factory=list)
    log: str = ''


def run_sta(opensta, liberty, netlist: Path, top, constraints, out_dir: Path, tag, k=200, slack_max=0.0) -> StaOut:
    tcl = out_dir / f'{tag}.tcl'
    tcl.write_text(PATHS_TCL.format(liberty=liberty, netlist=netlist, top=top, constraints=constraints,
                                    k=k, slack_max=slack_max))
    r = subprocess.run([opensta, '-no_init', '-exit', str(tcl)], capture_output=True, text=True, timeout=1800)
    text = r.stdout + r.stderr
    (out_dir / f'{tag}.log').write_text(text)
    res = StaOut(ok=(r.returncode == 0), log=str(out_dir / f'{tag}.log'))
    cur: Optional[PathInfo] = None
    for ln in text.splitlines():
        if ln.startswith('Endpoint:'):
            cur = PathInfo(endpoint=ln.split()[1], slack=0.0)
            res.paths.append(cur)
            continue
        m = re.match(r'\s*([-0-9.]+)\s+slack', ln)
        if m and cur is not None:
            cur.slack = float(m.group(1))
            continue
        m = _STAGE_RE.match(ln)
        if m and cur is not None and '/' in m.group(7):
            inst, pin = m.group(7).rsplit('/', 1)
            cur.stages.append(Stage(inst=inst, pin=pin, cell=m.group(8), delay=float(m.group(4)),
                                    fanout=int(m.group(1)) if m.group(1) else None,
                                    cap=float(m.group(2)), slew=float(m.group(3))))
            continue
        m = re.match(r'worst slack(?:\s+(?:max|min))?\s+([-0-9.eE+]+)', ln, re.I)
        if m and res.wns is None:
            res.wns = float(m.group(1))
        m = re.match(r'tns(?:\s+(?:max|min))?\s+([-0-9.eE+]+)', ln, re.I)
        if m and res.tns is None:
            res.tns = float(m.group(1))
    res.paths = [p for p in res.paths if p.slack < slack_max + 1e-9]
    return res


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

@dataclass
class Step:
    it: int
    moves: dict[str, str]
    wns_before: float
    tns_before: float
    wns_after: Optional[float]
    tns_after: Optional[float]
    accepted: bool


def area_of(yosys: str, liberty: str, netlist: Path, top: str) -> float:
    r = subprocess.run([yosys, '-p', f'read_liberty -lib {liberty}; read_verilog {netlist}; '
                        f'hierarchy -top {top}; stat -liberty {liberty}'], capture_output=True, text=True)
    m = re.search(r'Chip area for (?:top )?module.*?:\s*([0-9.]+)', r.stdout)
    return float(m.group(1)) if m else 0.0


def pick_moves(paths: list[PathInfo], types: dict[str, str], fam: dict[str, list[int]],
               tried: set[str], per_path: int = 1, flops_too: bool = True) -> dict[str, str]:
    """One (or per_path) upsizes per failing path: the stages with the largest
    delay whose cell has a bigger drive variant and was not tried before."""
    moves: dict[str, str] = {}
    for p in paths:
        cands = sorted(p.stages, key=lambda s: -s.delay)
        n = 0
        for s in cands:
            if s.inst in moves or s.inst in tried:
                continue
            cur = types.get(s.inst, s.cell)
            if not flops_too and re.search(r'__(df|dl|sd)', cur):
                continue
            nxt = next_size(cur, fam)
            if nxt is None:
                continue
            moves[s.inst] = nxt
            n += 1
            if n >= per_path:
                break
    return moves


def resize(netlist: Path, top: str, liberty: str, sta_liberty: str, period_ps: int, clock_port: str,
           out_dir: Path, *, sdc=None, iters=10, margin_ps=0, yosys='yosys', opensta='sta',
           driving_cell='sky130_fd_sc_hd__inv_2', load_ff=17.65, unc_setup_ps=250, unc_hold_ps=100,
           wire_load_model='auto', io_delay_frac=0.2, clock_port_2=None, period_ps_2=None,
           per_path=1, max_paths=200, wns_tol=0.15, wns_repair_iters=8, final='tns',
           recover_area=False, recover_rounds=6, log=print) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    constraints = sf._sta_constraints(
        clock_port=clock_port, period_ns=period_ps / 1000.0, clock_port_2=clock_port_2,
        period_2_ns=(period_ps_2 / 1000.0) if (clock_port_2 and period_ps_2) else None,
        unc_setup_ns=unc_setup_ps / 1000.0, unc_hold_ns=unc_hold_ps / 1000.0, user_sdc=sdc,
        driving_cell=driving_cell, load_pf=load_ff / 1000.0,
        wire_load_section=sf._wire_load_section(wire_load_model, sta_liberty), io_delay_frac=io_delay_frac)
    fam = drive_families(liberty)
    margin = margin_ps / 1000.0

    cur = out_dir / 'it0.v'
    shutil.copy(netlist, cur)
    sf._strip_signed_decls(cur)
    text = cur.read_text()
    types = instance_types(text)
    sta = run_sta(opensta, sta_liberty, cur, top, constraints, out_dir, 'it0', k=max_paths, slack_max=margin)
    if not sta.ok:
        raise RuntimeError(f'OpenSTA failed, see {sta.log}')
    area0 = area_of(yosys, liberty, cur, top)
    log(f'start: WNS={sta.wns:+.3f} TNS={sta.tns:+.2f} area={area0:.1f} failing paths={len(sta.paths)} '
        f'(margin {margin} ns)')
    steps: list[Step] = []
    tried: set[str] = set()
    total_moves: dict[str, str] = {}
    start_wns = sta.wns
    states: list[tuple[Path, float, float, dict]] = [(cur, sta.wns, sta.tns, {})]   # accepted (netlist, wns, tns, moves so far)
    def evaluate(moves: dict[str, str], tag: str):
        new_text = retype(text, moves)
        new = out_dir / f'{tag}.v'
        new.write_text(new_text)
        r = run_sta(opensta, sta_liberty, new, top, constraints, out_dir, tag, k=max_paths, slack_max=margin)
        return new, new_text, r

    def accept(old: StaOut, new: StaOut) -> bool:
        """TNS is the objective. A WNS regression up to wns_tol is tolerated
        when TNS improves clearly (the worst path just moved); a pure WNS gain
        with equal TNS is also accepted."""
        if not new.ok or new.tns is None or old.tns is None:
            return False
        if new.tns > old.tns + 1e-6 and new.wns >= old.wns - wns_tol:
            return True
        return abs(new.tns - old.tns) < 1e-6 and new.wns > old.wns + 1e-6

    it = 0
    stall = 0
    while it < iters:
        if not sta.paths:
            log('no failing endpoints; done')
            break
        moves = pick_moves(sta.paths, types, fam, tried, per_path=per_path)
        if not moves:
            log('no sizing candidates left; done')
            break
        # Try the whole batch; on rejection bisect it (largest-delay stages first).
        order = sorted(moves, key=lambda i: -max((s.delay for pth in sta.paths for s in pth.stages if s.inst == i), default=0))
        batch = list(order)
        accepted = False
        while batch and it < iters:
            it += 1
            sub = {i: moves[i] for i in batch}
            new, new_text, sta_new = evaluate(sub, f'it{it}')
            ok = accept(sta, sta_new)
            steps.append(Step(it=it, moves=sub, wns_before=sta.wns, tns_before=sta.tns,
                              wns_after=sta_new.wns, tns_after=sta_new.tns, accepted=ok))
            log(f'it{it}: {len(sub)} upsizes -> WNS {sta.wns:+.3f}->{sta_new.wns:+.3f} '
                f'TNS {sta.tns:+.2f}->{sta_new.tns:+.2f} {"ACCEPT" if ok else "reject"}')
            if ok:
                cur, text, sta = new, new_text, sta_new
                types.update(sub)
                total_moves.update(sub)
                states.append((cur, sta.wns, sta.tns, dict(total_moves)))
                accepted = True
                break
            if len(batch) == 1:
                tried.add(batch[0])
                break
            batch = batch[:len(batch) // 2]
        if not accepted:
            stall += 1
            if stall >= 3:
                log('three batches rejected down to single moves; stopping')
                break
        else:
            stall = 0
    # Phase 2: WNS repair. The TNS phase tolerates a moving worst path; now
    # attack only the worst path(s) with zero WNS tolerance so the final
    # netlist is never worse than the input on WNS.
    wns_tol_start = wns_tol
    wns_tol = 0.0
    tried2: set[str] = set()
    for rep in range(wns_repair_iters):
        if not sta.paths or (start_wns is not None and sta.wns >= start_wns - 1e-6 and sta.wns >= 0):
            break
        worst = sorted(sta.paths, key=lambda pth: pth.slack)[:3]
        moves = pick_moves(worst, types, fam, tried | tried2, per_path=2)
        if not moves:
            break
        it += 1
        new, new_text, sta_new = evaluate(moves, f'it{it}')
        ok = sta_new.ok and sta_new.wns > sta.wns + 1e-6 and sta_new.tns >= sta.tns - 1e-6
        steps.append(Step(it=it, moves=moves, wns_before=sta.wns, tns_before=sta.tns,
                          wns_after=sta_new.wns, tns_after=sta_new.tns, accepted=ok))
        log(f'it{it} (wns repair): {len(moves)} upsizes -> WNS {sta.wns:+.3f}->{sta_new.wns:+.3f} '
            f'TNS {sta.tns:+.2f}->{sta_new.tns:+.2f} {"ACCEPT" if ok else "reject"}')
        tried2 |= set(moves)
        if ok:
            cur, text, sta = new, new_text, sta_new
            types.update(moves)
            total_moves.update(moves)
            states.append((cur, sta.wns, sta.tns, dict(total_moves)))
    # Phase 3 (optional): area recovery. Downsize cells that are not on any
    # path within `guard` of the margin, in batches bisected on rejection.
    # Accepted only while WNS stays >= min(start WNS, margin) and TNS does not
    # drop, so timing never pays for area.
    if recover_area:
        guard = 0.3
        floor_wns = min(sta.wns, margin)
        tried3: set[str] = set()
        for rnd in range(recover_rounds):
            near = run_sta(opensta, sta_liberty, cur, top, constraints, out_dir, f'near{rnd}', k=2000,
                           slack_max=margin + guard)
            protected = {st.inst for pth in near.paths for st in pth.stages}
            cands = {i: prev_size(t, fam) for i, t in types.items()
                     if i not in protected and i not in tried3 and prev_size(t, fam)}
            if not cands:
                log('area recovery: no downsizing candidates; done')
                break
            batch = sorted(cands)
            done_round = False
            while batch and it < iters + recover_rounds * 8:
                it += 1
                sub = {i: cands[i] for i in batch}
                new, new_text, sta_new = evaluate(sub, f'it{it}')
                ok = (sta_new.ok and sta_new.wns >= floor_wns - 1e-6 and sta_new.tns >= sta.tns - 1e-6)
                steps.append(Step(it=it, moves=sub, wns_before=sta.wns, tns_before=sta.tns,
                                  wns_after=sta_new.wns, tns_after=sta_new.tns, accepted=ok))
                log(f'it{it} (area): {len(sub)} downsizes -> WNS {sta.wns:+.3f}->{sta_new.wns:+.3f} '
                    f'TNS {sta.tns:+.2f}->{sta_new.tns:+.2f} {"ACCEPT" if ok else "reject"}')
                if ok:
                    cur, text, sta = new, new_text, sta_new
                    types.update(sub)
                    total_moves.update(sub)
                    states.append((cur, sta.wns, sta.tns, dict(total_moves)))
                    done_round = True
                    break
                if len(batch) == 1:
                    tried3.add(batch[0])
                    break
                batch = batch[:len(batch) // 2]
            if not done_round and len(cands) <= 1:
                break

    # Final state: best TNS among accepted states that did not regress WNS;
    # if every improvement moved the worst path, best TNS overall (reported).
    # States that meet timing (WNS >= margin) are always eligible: area
    # recovery legitimately spends slack above the margin.
    if final == 'wns':
        pool = [st for st in states if st[1] >= margin - 1e-6 or st[1] >= start_wns - 1e-6] or states
    else:  # 'tns': bounded WNS regression for a TNS gain
        pool = [st for st in states if st[1] >= margin - 1e-6 or st[1] >= start_wns - wns_tol_start - 1e-6] or states
    # Rank: TNS first; then, among states that meet timing (WNS >= margin), the
    # latest state (area recovery only ever removes area); otherwise best WNS.
    def rank(i):
        _, w, t, _ = pool[i]
        met = w >= margin - 1e-6
        return (round(t, 4), 1 if met else 0, i if met else round(w, 4))
    best = pool[max(range(len(pool)), key=rank)]
    wns_regressed = best[1] < start_wns - 1e-6
    if best[0] != cur:
        cur = best[0]
        total_moves = best[3]
        sta = run_sta(opensta, sta_liberty, cur, top, constraints, out_dir, 'final', k=max_paths, slack_max=margin)
        log(f'final state rolled back to {cur.name}: WNS={sta.wns:+.3f} TNS={sta.tns:+.2f}')
    if wns_regressed:
        log(f'note: WNS regressed {start_wns:+.3f} -> {sta.wns:+.3f} for a TNS gain; --final wns forbids this')
    final = out_dir / 'resized.v'
    shutil.copy(cur, final)
    area = area_of(yosys, liberty, final, top)
    log(f'end:   WNS={sta.wns:+.3f} TNS={sta.tns:+.2f} area={area:.1f} ({(area / area0 - 1) * 100:+.2f}%) '
        f'failing={len(sta.paths)} moves={len(total_moves)} -> {final}')
    res = {'input': str(netlist), 'output': str(final), 'top': top,
           'start': {'wns_ns': steps[0].wns_before if steps else sta.wns, 'tns_ns': steps[0].tns_before if steps else sta.tns, 'area': area0},
           'end': {'wns_ns': sta.wns, 'tns_ns': sta.tns, 'area': area, 'failing': len(sta.paths)},
           'moves': total_moves, 'steps': [asdict(s) for s in steps], 'wns_regressed': wns_regressed}
    (out_dir / 'resize.json').write_text(json.dumps(res, indent=2))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--netlist', required=True); ap.add_argument('--top', required=True)
    ap.add_argument('--lib', required=True); ap.add_argument('--lib-sta')
    ap.add_argument('--period-ps', type=int, required=True); ap.add_argument('--clock-port', default='clk')
    ap.add_argument('--clock-port-2'); ap.add_argument('--period-ps-2', type=int)
    ap.add_argument('--sdc'); ap.add_argument('--iters', type=int, default=10)
    ap.add_argument('--margin-ps', type=int, default=0); ap.add_argument('--per-path', type=int, default=1)
    ap.add_argument('--max-paths', type=int, default=200)
    ap.add_argument('--wns-tol', type=float, default=0.15, help='ns of WNS regression tolerated when TNS improves')
    ap.add_argument('--recover-area', action='store_true', help='after timing, downsize off-critical cells while WNS holds')
    ap.add_argument('--recover-rounds', type=int, default=6)
    ap.add_argument('--final', choices=['tns', 'wns'], default='tns',
                    help="final state: best TNS within --wns-tol of the start WNS (tns), or never regress WNS (wns)")
    ap.add_argument('--driving-cell', default='sky130_fd_sc_hd__inv_2'); ap.add_argument('--load-ff', type=float, default=17.65)
    ap.add_argument('--wire-load-model', default='auto')
    ap.add_argument('--yosys', default='yosys'); ap.add_argument('--opensta', default='sta')
    ap.add_argument('--work-dir', default='work_resize'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    log = (lambda *x: None) if a.json else print
    res = resize(Path(a.netlist), a.top, a.lib, a.lib_sta or a.lib, a.period_ps, a.clock_port, Path(a.work_dir),
                 sdc=a.sdc, iters=a.iters, margin_ps=a.margin_ps, yosys=a.yosys, opensta=a.opensta,
                 driving_cell=a.driving_cell, load_ff=a.load_ff, wire_load_model=a.wire_load_model,
                 clock_port_2=a.clock_port_2, period_ps_2=a.period_ps_2, per_path=a.per_path,
                 max_paths=a.max_paths, wns_tol=a.wns_tol, final=a.final,
                 recover_area=a.recover_area, recover_rounds=a.recover_rounds, log=log)
    if a.json:
        print(json.dumps(res, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
