#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
refine — STA-driven critical-cone resynthesis of a mapped netlist.

Loop (docs/architecture.md §3):
  1. OpenSTA (full SDC): endpoints with slack below --margin.
  2. Yosys: reload the netlist with functional liberty models, select the
     full fan-in cones of those endpoints bounded by flops/ports, un-map them
     (`flatten @cone`), re-run `abc` on the cone with a delay recipe and NO
     -D (architecture.md §2.6), write a new netlist.
  3. OpenSTA again: accept only if TNS improves (WNS not worse); else revert
     and escalate to the next recipe, finally a whole-design remap.
  4. Equivalence check (yosys equiv_*) on every accepted step unless --no-lec.
Stops when nothing fails timing, the iteration cap is hit, or two attempts in
a row are rejected.

    python3 refine.py --netlist results/top/winner.v --top top \\
        --lib sky130/hd_120_ss.lib --period-ps 8000 --clock-port clk \\
        [--sdc top.sdc] [--recipe recipes/delay_choice_deep_v3.abc] [--iters 5]

Standalone: works on any Yosys/OpenLane netlist that uses the given liberty.
Only reads helpers from synth_flow (STA preamble, netlist post-processing).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth_flow as sf            # noqa: E402
from liberty_timing import read_liberty_timing  # noqa: E402

# --------------------------------------------------------------------------
# OpenSTA: failing endpoints
# --------------------------------------------------------------------------

ENDPOINT_TCL = """\
read_liberty {liberty}
{extra_libs}
read_verilog {netlist}
link_design {top}
{constraints}
set _ends [find_timing_paths -path_delay max -slack_max {slack_max} -group_path_count {max_paths} -endpoint_path_count 1]
foreach _pe $_ends {{
    set _ep [get_property $_pe endpoint]
    set _sp [get_property $_pe startpoint]
    puts "ENDPOINT [get_full_name $_ep] [get_property $_pe slack] [get_full_name $_sp]"
}}
report_worst_slack -max -digits 4
report_tns -max -digits 4
exit
"""


@dataclass
class StaResult:
    wns_ns: Optional[float] = None
    tns_ns: Optional[float] = None
    endpoints: list[tuple[str, float, str]] = field(default_factory=list)  # (endpoint pin, slack, startpoint)
    log: str = ''
    ok: bool = False


def run_sta(opensta: str, liberty: str, netlist: Path, top: str, constraints: str,
            slack_max_ns: float, out_dir: Path, tag: str, max_paths: int = 2000, extra_libs=()) -> StaResult:
    tcl = out_dir / f'{tag}.sta.tcl'
    log = out_dir / f'{tag}.sta.log'
    tcl.write_text(ENDPOINT_TCL.format(liberty=liberty, netlist=netlist, top=top, constraints=constraints,
                                       slack_max=slack_max_ns, max_paths=max_paths,
                                       extra_libs='\n'.join(f'read_liberty {l}' for l in (extra_libs or []))))
    r = subprocess.run([opensta, '-no_init', '-exit', str(tcl)], capture_output=True, text=True, timeout=1800)
    out = r.stdout + r.stderr
    log.write_text(out)
    res = StaResult(log=str(log), ok=(r.returncode == 0))
    for ln in out.splitlines():
        m = re.match(r'ENDPOINT\s+(\S+)\s+([-0-9.eE+]+)\s+(\S+)', ln)
        if m:
            res.endpoints.append((m.group(1), float(m.group(2)), m.group(3)))
            continue
        m = re.match(r'worst slack(?:\s+(?:max|min))?\s+([-0-9.eE+]+)', ln, re.I)
        if m and res.wns_ns is None:
            res.wns_ns = float(m.group(1))
        m = re.match(r'tns(?:\s+(?:max|min))?\s+([-0-9.eE+]+)', ln, re.I)
        if m and res.tns_ns is None:
            res.tns_ns = float(m.group(1))
    return res


# --------------------------------------------------------------------------
# Yosys: cone un-map / re-map
# --------------------------------------------------------------------------

REFINE_YS = """\
# refine iteration {it}: un-map {n_cells} endpoint cones and re-map with -D {d_ps}
read_liberty -ignore_miss_func {liberty}
read_verilog {netlist}
hierarchy -top {top}
select -set ffs {ff_types}
{endpoint_select}
select -set cone {cone_expr}
tee -q -o {stats} log CONE_RAW
tee -q -a {stats} select -count @cone
# Boundary rule (architecture.md §2.5): a cone cell whose output also feeds
# logic OUTSIDE the cone keeps its current cell; ABC never sees a boundary
# load it cannot model. %co2 = cells reading the nets driven by the selection.
{boundary_rule}
tee -q -a {stats} log CONE_CELLS
tee -q -a {stats} select -count @cone
flatten @cone
opt_clean
tee -q -a {stats} log GENERIC_AFTER_FLATTEN
tee -q -a {stats} select -count t:$_*
abc -liberty {liberty} -constr {constr} -script {recipe} t:$_*
opt_clean
tee -q -a {stats} log GENERIC_LEFT
tee -q -a {stats} select -count t:$_*
# leftover generic gates (buffers/constants from function expansion): map plainly
abc -liberty {liberty} -constr {constr} t:$_*
opt_clean
delete {top} %n
setundef -zero
splitnets
opt_clean -purge
write_verilog -noattr -noexpr {out}
"""

LEC_YS = """\
read_liberty -ignore_miss_func {liberty}
read_verilog {gold}
rename {top} gold
read_verilog {gate}
rename {top} gate
flatten gold gate
async2sync
equiv_make gold gate eq
hierarchy -top eq
equiv_simple -seq 2
equiv_induct
equiv_status -assert
"""


def _ff_types(lt) -> str:
    return ' '.join(f't:{c}' for c in lt.flop_cells)


def _ff_rules(lt) -> str:
    return ''.join(f':-{c}' for c in lt.flop_cells)


def _dpin_cap_ff(liberty: str, flop: str, pin: str = 'D') -> Optional[float]:
    """Capacitance (fF) of input `pin` of `flop` from the liberty, if found."""
    try:
        text = Path(liberty).read_text(errors='ignore')
    except OSError:
        return None
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    um = re.search(r'capacitive_load_unit\s*\(\s*([0-9.]+)\s*,\s*"?(pf|ff)"?\s*\)', text, re.I)
    unit = (1000.0 if um.group(2).lower() == 'pf' else 1.0) * (float(um.group(1)) if um else 1.0) if um else 1.0

    def block(t: str, start_brace: int) -> str:
        depth = 0
        for i in range(start_brace, len(t)):
            if t[i] == '{':
                depth += 1
            elif t[i] == '}':
                depth -= 1
                if depth == 0:
                    return t[start_brace + 1:i]
        return t[start_brace + 1:]

    m = re.search(r'\bcell\s*\(\s*"?' + re.escape(flop) + r'"?\s*\)\s*\{', text)
    if not m:
        return None
    body = block(text, m.end() - 1)
    pm = re.search(r'\bpin\s*\(\s*"?' + re.escape(pin) + r'"?\s*\)\s*\{', body)
    if not pm:
        return None
    pbody = block(body, pm.end() - 1)
    cm = re.search(r'\bcapacitance\s*:\s*([0-9.eE+-]+)', pbody)
    return float(cm.group(1)) * unit if cm else None


def build_cone_expr(endpoints: list[str], ports: set[str], lt, dpin: str = 'D') -> tuple[str, str]:
    """Return (select -set lines, cone expression) for the given endpoint pins.

    Flop endpoints look like `_231_/D`; output-port endpoints are bare port
    names (`y[3]`, `ready`). Cones are the fan-in of the endpoint nets, not
    traversing flops, intersected with cells, minus the flops themselves."""
    R = _ff_rules(lt)
    cells = sorted({e.rsplit('/', 1)[0] for e in endpoints if '/' in e})
    port_ends = sorted({re.sub(r'\[\d+\]$', '', e) for e in endpoints if '/' not in e})
    lines = []
    parts = []
    if cells:
        lines.append('select -set endpts ' + ' '.join(f'c:{c}' for c in cells))
        lines.append(f'select -set dnets @endpts %x:+[{dpin}] @endpts %d')
        parts.append(f'@dnets %ci*{R}')
    if port_ends:
        parts.append(' '.join(f'o:{p}' for p in port_ends) + f' %ci*{R}')
    expr = ' '.join(parts) + (' %u' if len(parts) == 2 else '') + ' t:* %i @ffs %d'
    return '\n'.join(lines), expr


def yosys_refine(yosys: str, liberty: str, netlist: Path, top: str, endpoints: list[str], lt,
                 recipe: Path, d_ps: int, constr: Path, out_dir: Path, it: int,
                 whole_design: bool = False) -> tuple[Optional[Path], dict]:
    sel_lines, cone_expr = build_cone_expr(endpoints, set(), lt)
    if whole_design:
        sel_lines, cone_expr = '', 't:* @ffs %d'
    recipe = sf._materialize_recipe(recipe, d_ps, out_dir)
    stats = out_dir / f'it{it}.stats.txt'
    out = out_dir / f'it{it}.v'
    ys = out_dir / f'it{it}.ys'
    boundary = ('' if whole_design else
                'select -set outside @cone %co2 @cone %d @ffs %d t:* %i\n'
                'select -set keep @outside %ci2 @cone %i\n'
                'select -set cone @cone @keep %d')
    ys.write_text(REFINE_YS.format(it=it, n_cells=len(endpoints), d_ps=d_ps, liberty=liberty, netlist=netlist,
                                   top=top, ff_types=_ff_types(lt), endpoint_select=sel_lines,
                                   cone_expr=cone_expr, stats=stats, constr=constr, recipe=recipe, out=out,
                                   boundary_rule=boundary))
    log = out_dir / f'it{it}.yosys.log'
    with open(log, 'w') as lf:
        r = subprocess.run([yosys, '-q', '-s', str(ys)], stdout=lf, stderr=subprocess.STDOUT, timeout=3600)
    info = {'yosys_log': str(log), 'cone_raw': None, 'cone_cells': None, 'generic_after_flatten': None, 'generic_left': None}
    try:
        cur = None
        for ln in stats.read_text().splitlines():
            if ln.strip() in ('CONE_RAW', 'CONE_CELLS', 'GENERIC_AFTER_FLATTEN', 'GENERIC_LEFT'):
                cur = ln.strip().lower()
            else:
                m = re.match(r'\s*(\d+)\s+objects', ln)
                if m and cur:
                    info[cur] = int(m.group(1))
                    cur = None
    except OSError:
        pass
    if r.returncode != 0 or not out.exists():
        info['error'] = f'yosys exit {r.returncode}'
        return None, info
    sf._strip_signed_decls(out)
    return out, info


def lec(yosys: str, liberty: str, gold: Path, gate: Path, top: str, out_dir: Path, it: int) -> tuple[bool, str]:
    ys = out_dir / f'it{it}.lec.ys'
    ys.write_text(LEC_YS.format(liberty=liberty, gold=gold, gate=gate, top=top))
    log = out_dir / f'it{it}.lec.log'
    with open(log, 'w') as lf:
        r = subprocess.run([yosys, '-s', str(ys)], stdout=lf, stderr=subprocess.STDOUT, timeout=3600)
    # equiv_status -assert makes yosys exit non-zero on any unproven cell.
    return r.returncode == 0, str(log)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

_RECIPES = Path(__file__).resolve().parent / 'recipes'
DEFAULT_ESCALATION = [_RECIPES / 'delay_choice_deep_v3.abc', _RECIPES / 'delay_iter_heavy.abc',
                      _RECIPES / 'delay_triple.abc', _RECIPES / 'orfs_speed.abc']

@dataclass
class Step:
    it: int
    d_ps: int
    n_endpoints: int
    cone_cells: Optional[int]
    wns_before: Optional[float]
    tns_before: Optional[float]
    wns_after: Optional[float]
    tns_after: Optional[float]
    accepted: bool
    lec: Optional[bool]
    area_before: float
    area_after: float
    note: str = ''
    recipe: str = ''
    whole_design: bool = False


def area_of(yosys: str, liberty: str, netlist: Path, top: str) -> float:
    r = subprocess.run([yosys, '-p', f'read_liberty -lib {liberty}; read_verilog {netlist}; '
                        f'hierarchy -top {top}; stat -liberty {liberty}'], capture_output=True, text=True)
    m = re.search(r'Chip area for (?:top )?module.*?:\s*([0-9.]+)', r.stdout)
    return float(m.group(1)) if m else 0.0


def refine(netlist: Path, top: str, liberty: str, sta_liberty: str, period_ps: int, clock_port: str,
           out_dir: Path, *, sdc: Optional[str] = None, recipe: Path, iters: int = 5, margin_ps: int = 0,
           yosys: str = 'yosys', opensta: str = 'sta', driving_cell: str = 'sky130_fd_sc_hd__inv_2',
           load_ff: float = 17.65, unc_setup_ps: int = 250, unc_hold_ps: int = 100,
           wire_load_model: str = 'auto', io_delay_frac: float = 0.2, clock_port_2: Optional[str] = None,
           period_ps_2: Optional[int] = None, do_lec: bool = True, max_endpoints: int = 400,
           extra_recipes: Optional[list] = None, boundary: str = 'flat', whole_only: bool = False,
           log=print) -> dict:
    extra_recipes = extra_recipes if extra_recipes is not None else DEFAULT_ESCALATION
    out_dir.mkdir(parents=True, exist_ok=True)
    lt = read_liberty_timing(liberty)
    if not lt.flop_cells:
        raise RuntimeError(f'no flop cells found in {liberty}')
    constraints = sf._sta_constraints(
        clock_port=clock_port, period_ns=period_ps / 1000.0, clock_port_2=clock_port_2,
        period_2_ns=(period_ps_2 / 1000.0) if (clock_port_2 and period_ps_2) else None,
        unc_setup_ns=unc_setup_ps / 1000.0, unc_hold_ns=unc_hold_ps / 1000.0, user_sdc=sdc,
        driving_cell=driving_cell, load_pf=load_ff / 1000.0,
        wire_load_section=sf._wire_load_section(wire_load_model, sta_liberty), io_delay_frac=io_delay_frac,
        io_delay_min_frac=io_delay_min_frac)
    # Cone boundary model. 'flat' (default) reuses the flow's driving cell and
    # load: with a wire-load model in STA the conventional ~33 fF stands in
    # for pin cap + wire, and it measured better than the physically exact
    # D-pin capacitance (1.6 fF), which makes ABC undersize every output.
    dcap = _dpin_cap_ff(liberty, lt.reference_flop or '') or load_ff
    if boundary == 'flop':
        cone_driver = 'sky130_fd_sc_hd__buf_1' if 'sky130_fd_sc_hd__buf_1' in lt.cells else driving_cell
        cone_load = dcap
    else:
        cone_driver, cone_load = driving_cell, load_ff
    constr = out_dir / 'cone.constr'
    constr.write_text(f'set_driving_cell {cone_driver}\nset_load {cone_load:.3f}\n')

    cur = out_dir / 'it0.v'
    shutil.copy(netlist, cur)
    sf._strip_signed_decls(cur)
    margin_ns = margin_ps / 1000.0
    sta0 = run_sta(opensta, sta_liberty, cur, top, constraints, margin_ns, out_dir, 'it0')
    if not sta0.ok:
        raise RuntimeError(f'OpenSTA failed on the input netlist, see {sta0.log}')
    area0 = area_of(yosys, liberty, cur, top)
    log(f'start: WNS={sta0.wns_ns:+.3f} TNS={sta0.tns_ns:+.2f} area={area0:.1f} '
        f'failing endpoints={len(sta0.endpoints)} (margin {margin_ns} ns)')
    log(f'cone boundary ({boundary}): driver {cone_driver}, load {cone_load:.2f} fF (D-pin cap {dcap:.2f} fF); '
        f'flop t_cq={lt.t_cq_ps} t_su={lt.t_su_ps} ps')

    # No ABC -D: any target lets ABC relax against a model OpenSTA disagrees
    # with (architecture.md §2.6). Escalate through delay recipes instead, and
    # finish with a whole-design remap (all endpoints) as the last attempt.
    d_ps = 0
    recipes = [] if whole_only else [Path(recipe)] + [r for r in extra_recipes if Path(r) != Path(recipe)]
    if whole_only:
        recipes = []          # attempt 0 is already the whole-design remap

    steps: list[Step] = []
    sta = sta0
    area = area0
    rejects = 0
    attempt = 0
    for it in range(1, iters + 1):
        if not sta.endpoints:
            log('no endpoints below margin; done')
            break
        whole = attempt >= len(recipes)          # recipes exhausted (or whole_only): remap everything
        rec = recipes[min(attempt, len(recipes) - 1)] if recipes else Path(recipe)
        eps = [e for e, _, _ in sorted(sta.endpoints, key=lambda t: t[1])[:max_endpoints]]
        new, info = yosys_refine(yosys, liberty, cur, top, eps, lt, rec, d_ps, constr, out_dir, it,
                                 whole_design=whole)
        if new is None:
            log(f'it{it}: yosys failed ({info.get("error")}), see {info["yosys_log"]}')
            break
        sta_new = run_sta(opensta, sta_liberty, new, top, constraints, margin_ns, out_dir, f'it{it}')
        area_new = area_of(yosys, liberty, new, top)
        better = (sta_new.ok and sta_new.tns_ns is not None and sta.tns_ns is not None and
                  (sta_new.tns_ns > sta.tns_ns + 1e-6 or
                   (abs(sta_new.tns_ns - sta.tns_ns) < 1e-6 and sta_new.wns_ns > sta.wns_ns + 1e-6)) and
                  sta_new.wns_ns >= sta.wns_ns - 1e-6)
        lec_ok: Optional[bool] = None
        note = ''
        if better and do_lec:
            lec_ok, lec_log = lec(yosys, liberty, cur, new, top, out_dir, it)
            if not lec_ok:
                better = False
                note = f'REJECTED: equivalence not proven ({lec_log})'
        step = Step(it=it, d_ps=d_ps, n_endpoints=len(eps), cone_cells=info.get('cone_cells'), recipe=rec.name,
                    whole_design=whole,
                    wns_before=sta.wns_ns, tns_before=sta.tns_ns, wns_after=sta_new.wns_ns,
                    tns_after=sta_new.tns_ns, accepted=better, lec=lec_ok, area_before=area,
                    area_after=area_new, note=note)
        steps.append(step)
        log(f'it{it}: {rec.stem}{" WHOLE" if whole else ""} endpoints={len(eps)} cone={info.get("cone_cells")}/{info.get("cone_raw")} cells -> '
            f'WNS {sta.wns_ns:+.3f}->{sta_new.wns_ns:+.3f} TNS {sta.tns_ns:+.2f}->{sta_new.tns_ns:+.2f} '
            f'area {area:.0f}->{area_new:.0f} {"ACCEPT" if better else "reject"}'
            + (f' lec={"ok" if lec_ok else "FAIL"}' if lec_ok is not None else '') + (' ' + note if note else ''))
        if better:
            cur, sta, area = new, sta_new, area_new
            rejects = 0
        else:
            rejects += 1
            attempt += 1
            if attempt > len(recipes):           # every recipe and the whole-design remap tried
                log('all cone recipes and a whole-design remap rejected; stopping')
                break
    final = out_dir / 'refined.v'
    shutil.copy(cur, final)
    result = {'input': str(netlist), 'output': str(final), 'top': top, 'period_ps': period_ps,
              'start': {'wns_ns': sta0.wns_ns, 'tns_ns': sta0.tns_ns, 'area': area0, 'failing': len(sta0.endpoints)},
              'end': {'wns_ns': sta.wns_ns, 'tns_ns': sta.tns_ns, 'area': area, 'failing': len(sta.endpoints)},
              'steps': [asdict(s) for s in steps], 'cone_constr': constr.read_text().strip().replace('\n', '; ')}
    (out_dir / 'refine.json').write_text(json.dumps(result, indent=2))
    log(f'end:   WNS={sta.wns_ns:+.3f} TNS={sta.tns_ns:+.2f} area={area:.1f} failing={len(sta.endpoints)} -> {final}')
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--netlist', required=True)
    ap.add_argument('--top', required=True)
    ap.add_argument('--lib', required=True, help='synthesis liberty (functional models + ABC)')
    ap.add_argument('--lib-sta', help='STA liberty (default: --lib)')
    ap.add_argument('--period-ps', type=int, required=True)
    ap.add_argument('--clock-port', default='clk')
    ap.add_argument('--clock-port-2'); ap.add_argument('--period-ps-2', type=int)
    ap.add_argument('--sdc')
    ap.add_argument('--recipe', default=str(Path(__file__).parent / 'recipes' / 'delay_choice_deep_v3.abc'),
                    help='first cone recipe; then escalates through delay_iter_heavy, delay_triple, orfs_speed, then a whole-design remap')
    ap.add_argument('--iters', type=int, default=5)
    ap.add_argument('--margin-ps', type=int, default=0)
    ap.add_argument('--max-endpoints', type=int, default=400)
    ap.add_argument('--driving-cell', default='sky130_fd_sc_hd__inv_2')
    ap.add_argument('--load-ff', type=float, default=17.65)
    ap.add_argument('--wire-load-model', default='auto')
    ap.add_argument('--no-lec', action='store_true')
    ap.add_argument('--whole-only', action='store_true', help='skip cone attempts; only whole-design remap passes')
    ap.add_argument('--boundary', choices=['flat', 'flop'], default='flat',
                    help="cone constraint file: 'flat' = flow driving cell/load (default), 'flop' = buf_1 / D-pin cap")
    ap.add_argument('--yosys', default='yosys'); ap.add_argument('--opensta', default='sta')
    ap.add_argument('--work-dir', default='work_refine')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    log = (lambda *x: None) if a.json else print
    res = refine(Path(a.netlist), a.top, a.lib, a.lib_sta or a.lib, a.period_ps, a.clock_port, Path(a.work_dir),
                 sdc=a.sdc, recipe=Path(a.recipe), iters=a.iters, margin_ps=a.margin_ps, yosys=a.yosys,
                 opensta=a.opensta, driving_cell=a.driving_cell, load_ff=a.load_ff,
                 wire_load_model=a.wire_load_model, clock_port_2=a.clock_port_2, period_ps_2=a.period_ps_2,
                 do_lec=not a.no_lec, max_endpoints=a.max_endpoints, boundary=a.boundary,
                 whole_only=a.whole_only, log=log)
    if a.json:
        print(json.dumps(res, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
