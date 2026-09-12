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
# Netlist model: instances, pins, nets; buffer trees and delay chains
# --------------------------------------------------------------------------

_INST_BLOCK_RE = re.compile(r'^(\s*)(sky130_fd_sc_[a-z]+__\w+)\s+(\\?[\w$\[\]\.]+)\s*\((.*?)\);', re.M | re.S)
_PIN_CONN_RE = re.compile(r'\.(\w+)\s*\(\s*(.*?)\s*\)\s*(?:,|$)', re.S)
_OUT_PINS = {'X', 'Y', 'Q', 'Q_N', 'COUT', 'SUM', 'COUT_N', 'SUM_N', 'Z', 'HI', 'LO'}


def liberty_output_pins(liberty: str) -> dict[str, set[str]]:
    """cell -> set of output pin names, from the liberty (direction : output)."""
    text = re.sub(r'/\*.*?\*/', '', Path(liberty).read_text(errors='ignore'), flags=re.S)
    out: dict[str, set[str]] = {}
    for cm in re.finditer(r'\bcell\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{', text):
        depth, i, start = 0, cm.end() - 1, cm.end()
        for j in range(i, len(text)):
            if text[j] == '{': depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0: break
        body = text[start:j]
        pins = set()
        for pm in re.finditer(r'\bpin\s*\(\s*"?(\w+)"?\s*\)\s*\{', body):
            d2, k = 0, pm.end() - 1
            for q in range(k, len(body)):
                if body[q] == '{': d2 += 1
                elif body[q] == '}':
                    d2 -= 1
                    if d2 == 0: break
            if re.search(r'direction\s*:\s*"?output"?', body[pm.end():q]):
                pins.add(pm.group(1))
        out[cm.group(1)] = pins
    return out


class Netlist:
    """Minimal editable view of a Yosys `write_verilog -noattr -noexpr` netlist.
    Instances are edited in place in the text; new wires are declared before
    the first instance and new instances appended before `endmodule`."""

    def __init__(self, text: str, out_pins: dict[str, set[str]]):
        self.text = text
        self.out_pins = out_pins
        self.inst: dict[str, dict] = {}        # name -> {'type', 'pins': {pin: conn}, 'span': (a, b), 'indent'}
        self.counter = 0
        for m in _INST_BLOCK_RE.finditer(text):
            pins = {pm.group(1): pm.group(2).strip() for pm in _PIN_CONN_RE.finditer(m.group(4))}
            self.inst[m.group(3)] = {'type': m.group(2), 'pins': pins, 'span': m.span(), 'indent': m.group(1), 'new': False}
        self._first_inst = min((i['span'][0] for i in self.inst.values()), default=text.rfind('endmodule'))
        self.new_wires: list[str] = []
        self.names = set(self.inst) | set(re.findall(r'^\s*(?:wire|input|output|inout)\s+(?:\[[^\]]+\]\s*)?(\\?[\w$\.]+)', text, re.M))
        # `assign lhs = rhs;` feed-throughs (input -> output with no cell)
        self.assigns: dict[str, str] = {m.group(1).strip(): m.group(2).strip()
                                        for m in re.finditer(r'^\s*assign\s+(.+?)\s*=\s*(.+?)\s*;', text, re.M)}
        self.dropped_assigns: set[str] = set()
        self.extra_assigns: list[tuple[str, str]] = []
        self.widths: dict[str, tuple[int, int]] = {m.group(3): (int(m.group(1)), int(m.group(2)))
            for m in re.finditer(r'^\s*(?:output|input|inout|wire)\s+\[(\d+):(\d+)\]\s*(\\?[\w$\.]+)\s*;', text, re.M)}

    def _bits(self, expr: str) -> Optional[list[str]]:
        """Expand `name`, `name[k]`, `name[hi:lo]` into bit names (MSB first); None for anything else."""
        m = re.fullmatch(r'(\\?[\w$\.]+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]', expr)
        if m:
            hi, lo = int(m.group(2)), int(m.group(3))
            step = -1 if hi >= lo else 1
            return [f'{m.group(1)}[{i}]' for i in range(hi, lo + step, step)]
        m = re.fullmatch(r'(\\?[\w$\.]+)\s*\[\s*(\d+)\s*\]', expr)
        if m:
            return [expr]
        m = re.fullmatch(r'\\?[\w$\.]+', expr)
        if m:
            if expr in self.widths:
                hi, lo = self.widths[expr]
                step = -1 if hi >= lo else 1
                return [f'{expr}[{i}]' for i in range(hi, lo + step, step)]
            return [expr]
        return None

    def assign_source(self, bit: str) -> Optional[tuple[str, str, dict[str, str]]]:
        """For a port/net bit driven by an assign: (assign lhs, rhs bit expr, {lhs bit: rhs bit}) or None."""
        for lhs, rhs in self.assigns.items():
            if lhs in self.dropped_assigns:
                continue
            lb, rb = self._bits(lhs), self._bits(rhs)
            if lb is None or rb is None or len(lb) != len(rb):
                continue
            pairs = dict(zip(lb, rb))
            if bit in pairs:
                return lhs, pairs[bit], pairs
        return None

    def out_pin_name(self, cell: str) -> str:
        pins = self.out_pins.get(cell) or {'X'}
        return sorted(pins)[0]

    def is_output_pin(self, cell: str, pin: str) -> bool:
        pins = self.out_pins.get(cell)
        return pin in pins if pins else pin in _OUT_PINS

    def sinks(self, net: str) -> list[tuple[str, str]]:
        return [(n, p) for n, i in self.inst.items() for p, c in i['pins'].items()
                if c == net and not self.is_output_pin(i['type'], p)]

    def driver(self, net: str) -> Optional[tuple[str, str]]:
        for n, i in self.inst.items():
            for p, c in i['pins'].items():
                if c == net and self.is_output_pin(i['type'], p):
                    return n, p
        return None

    def fresh(self, prefix: str) -> str:
        while True:
            self.counter += 1
            name = f'{prefix}{self.counter}_'
            if name not in self.names:
                self.names.add(name)
                return name

    def add_inst(self, cell: str, pins: dict[str, str]) -> str:
        name = self.fresh('_rd_')
        self.inst[name] = {'type': cell, 'pins': dict(pins), 'span': None, 'indent': '  ', 'new': True}
        return name

    def add_wire(self) -> str:
        w = self.fresh('_rdn_')
        self.new_wires.append(w)
        return w

    def buffer_tree(self, net: str, buf_cell: str, group: int, keep_ports: bool = True) -> int:
        """Split `net`'s sinks into groups of <= `group`, each fed by a new
        buffer; output ports (bare names not driven by an instance input) stay
        on the original net. Returns the number of buffers inserted."""
        sinks = self.sinks(net)
        if len(sinks) <= group:
            return 0
        sinks.sort(key=lambda s: (s[0], s[1]))
        groups = [sinks[i:i + group] for i in range(0, len(sinks), group)]
        n = 0
        for g in groups:
            w = self.add_wire()
            self.add_inst(buf_cell, {'A': net, 'X': w})
            for inst, pin in g:
                self.inst[inst]['pins'][pin] = w
            n += 1
        return n

    def delay_pin(self, inst: str, pin: str, delay_cell: str, count: int, in_pin='A', out_pin='X') -> int:
        """Feed input `inst/pin` through `count` delay cells."""
        net = self.inst[inst]['pins'][pin]
        cur = net
        for _ in range(count):
            w = self.add_wire()
            self.add_inst(delay_cell, {in_pin: cur, out_pin: w})
            cur = w
        self.inst[inst]['pins'][pin] = cur
        return count

    def delay_port(self, port: str, delay_cell: str, count: int, in_pin='A', out_pin='X') -> int:
        """Delay an output port: the driver now drives a new net, the chain
        drives the port (other sinks of the port net are delayed too)."""
        drv = self.driver(port)
        if drv is None:
            src = self.assign_source(port)        # feed-through: chain replaces this bit of the assign
            if src is None:
                return 0
            lhs, rhs_bit, pairs = src
            self.dropped_assigns.add(lhs)
            self.extra_assigns.extend((lb, rb) for lb, rb in pairs.items() if lb != port
                                      and not any(lb == e[0] for e in self.extra_assigns))
            cur = rhs_bit
            for k in range(count):
                w = port if k == count - 1 else self.add_wire()
                self.add_inst(delay_cell, {in_pin: cur, out_pin: w})
                cur = w
            return count
        w0 = self.add_wire()
        self.inst[drv[0]]['pins'][drv[1]] = w0
        cur = w0
        for k in range(count):
            w = port if k == count - 1 else self.add_wire()
            self.add_inst(delay_cell, {in_pin: cur, out_pin: w})
            cur = w
        return count

    def render(self) -> str:
        def conn(c: str) -> str:
            return c + ' ' if c.startswith('\\') else c     # escaped identifiers need a trailing space

        def block(name, i):
            body = ',\n'.join(f"{i['indent']}  .{p}({conn(c)})" for p, c in i['pins'].items())
            return f"{i['indent']}{i['type']} {name} (\n{body}\n{i['indent']});"
        out = []
        pos = 0
        for m in _INST_BLOCK_RE.finditer(self.text):
            name = m.group(3)
            out.append(self.text[pos:m.start()])
            out.append(block(name, self.inst[name]))
            pos = m.end()
        out.append(self.text[pos:])
        text = ''.join(out)
        for lhs in self.dropped_assigns:
            text = re.sub(r'^\s*assign\s+' + re.escape(lhs) + r'\s*=\s*.+?;\s*\n', '', text, count=1, flags=re.M)
        # bits of a split vector assign that are not delayed keep a per-bit assign
        # (bits delayed later in the same edit session are excluded at chain time)
        delayed = {n for i in self.inst.values() if i['new'] for n in [i['pins'].get(self.out_pin_name(i['type']))] if n}
        keep = [(lb, rb) for lb, rb in self.extra_assigns if lb not in delayed]
        if keep:
            k = text.rfind('endmodule')
            text = text[:k] + ''.join(f'  assign {lb} = {rb};\n' for lb, rb in keep) + text[k:]
        if self.new_wires:
            decl = ''.join(f'  wire {w};\n' for w in self.new_wires)
            # declare before the first instance (Yosys puts all declarations first)
            m = _INST_BLOCK_RE.search(text)
            k = m.start() if m else text.rfind('endmodule')
            text = text[:k] + decl + text[k:]
        news = [n for n, i in self.inst.items() if i['new']]
        if news:
            k = text.rfind('endmodule')
            text = text[:k] + ''.join(block(n, self.inst[n]) + '\n' for n in news) + text[k:]
        return text


def pick_delay_cell(liberty: str, fam: dict[str, list[int]]) -> tuple[str, str, str]:
    """(cell, in_pin, out_pin): a dlygate if the library has one, else buf_1."""
    for base in ('sky130_fd_sc_hd__dlygate4sd3', 'sky130_fd_sc_hd__dlygate4sd2', 'sky130_fd_sc_hd__dlygate4sd1'):
        if base in fam:
            return f'{base}_{fam[base][0]}', 'A', 'X'
    if 'sky130_fd_sc_hd__buf' in fam:
        return f'sky130_fd_sc_hd__buf_{fam["sky130_fd_sc_hd__buf"][0]}', 'A', 'X'
    raise RuntimeError('no delay/buffer cell found in liberty')


# --------------------------------------------------------------------------
# OpenSTA: worst path per failing endpoint with stage details
# --------------------------------------------------------------------------

PATHS_TCL = """\
read_liberty {liberty}
read_verilog {netlist}
link_design {top}
{constraints}
report_checks -path_delay {mode} -group_path_count {k} -endpoint_path_count 1 -slack_max {slack_max} -format full_clock_expanded -fields {{fanout cap slew}} -digits 4
report_worst_slack -{mode} -digits 4
report_tns -{mode} -digits 4
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


def run_sta(opensta, liberty, netlist: Path, top, constraints, out_dir: Path, tag, k=200, slack_max=0.0,
            mode='max') -> StaOut:
    tcl = out_dir / f'{tag}.tcl'
    tcl.write_text(PATHS_TCL.format(liberty=liberty, netlist=netlist, top=top, constraints=constraints,
                                    k=k, slack_max=slack_max, mode=mode))
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
           wire_load_model='auto', io_delay_frac=0.2, io_delay_min_frac=0.4, clock_port_2=None, period_ps_2=None,
           per_path=1, max_paths=200, wns_tol=0.15, wns_repair_iters=8, final='tns',
           recover_area=False, recover_rounds=6,
           repair_design=False, max_fanout=8, buffer_iters=6,
           repair_hold=False, lib_fast=None, hold_iters=10, log=print) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    constraints = sf._sta_constraints(
        clock_port=clock_port, period_ns=period_ps / 1000.0, clock_port_2=clock_port_2,
        period_2_ns=(period_ps_2 / 1000.0) if (clock_port_2 and period_ps_2) else None,
        unc_setup_ns=unc_setup_ps / 1000.0, unc_hold_ns=unc_hold_ps / 1000.0, user_sdc=sdc,
        driving_cell=driving_cell, load_pf=load_ff / 1000.0,
        wire_load_section=sf._wire_load_section(wire_load_model, sta_liberty), io_delay_frac=io_delay_frac,
        io_delay_min_frac=io_delay_min_frac)
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
    out_pins = liberty_output_pins(liberty)
    buffers_inserted = 0
    it = 0

    def evaluate_text(new_text: str, tag: str):
        new = out_dir / f'{tag}.v'
        new.write_text(new_text)
        r = run_sta(opensta, sta_liberty, new, top, constraints, out_dir, tag, k=max_paths, slack_max=margin)
        return new, r

    def accept_tns(old: StaOut, new: StaOut) -> bool:
        if not new.ok or new.tns is None or old.tns is None:
            return False
        if new.tns > old.tns + 1e-6 and new.wns >= old.wns - wns_tol:
            return True
        return abs(new.tns - old.tns) < 1e-6 and new.wns > old.wns + 1e-6

    # Phase 0 (optional): repair_design. Split high-fanout nets on failing paths
    # with buffer trees (groups of <= max_fanout sinks), one net per failing
    # path per round, batch accepted on TNS, bisected on rejection.
    if repair_design and sta.paths:
        buf_sizes = fam.get('sky130_fd_sc_hd__buf', [])
        buf_cell = f"sky130_fd_sc_hd__buf_{2 if 2 in buf_sizes else (buf_sizes[0] if buf_sizes else 1)}"
        buffered: set[str] = set()
        for rnd in range(buffer_iters):
            if not sta.paths:
                break
            nl = Netlist(text, out_pins)
            cands: list[tuple[float, str]] = []
            for pth in sta.paths:
                best = None
                for st in pth.stages:
                    if st.fanout is None or st.fanout < max_fanout or st.inst not in nl.inst:
                        continue
                    net = nl.inst[st.inst]['pins'].get(st.pin)
                    if not net or net in buffered or net in [c[1] for c in cands]:
                        continue
                    if best is None or st.delay > best[0]:
                        best = (st.delay, net)
                if best:
                    cands.append(best)
            if not cands:
                log('repair_design: no high-fanout stage left on failing paths; done')
                break
            cands.sort(reverse=True)
            batch = [net for _, net in cands]
            accepted = False
            while batch:
                nl = Netlist(text, out_pins)
                nb = sum(nl.buffer_tree(net, buf_cell, max_fanout) for net in batch)
                if nb == 0:                       # STA fanout counted pins we do not split (ports etc.)
                    buffered |= set(batch)
                    break
                it += 1
                new, sta_new = evaluate_text(nl.render(), f'it{it}')
                ok = accept_tns(sta, sta_new)
                steps.append(Step(it=it, moves={net: f'buffer_tree({buf_cell})' for net in batch}, wns_before=sta.wns,
                                  tns_before=sta.tns, wns_after=sta_new.wns, tns_after=sta_new.tns, accepted=ok))
                log(f'it{it} (buffer): {len(batch)} nets, {nb} buffers -> WNS {sta.wns:+.3f}->{sta_new.wns:+.3f} '
                    f'TNS {sta.tns:+.2f}->{sta_new.tns:+.2f} {"ACCEPT" if ok else "reject"}')
                if ok:
                    cur, sta = new, sta_new
                    text = new.read_text()
                    types = instance_types(text)
                    buffered |= set(batch)
                    buffers_inserted += nb
                    for net in batch:
                        total_moves[net] = f'buffer_tree({buf_cell}) x{max_fanout}'
                    states.append((cur, sta.wns, sta.tns, dict(total_moves)))
                    accepted = True
                    break
                if len(batch) == 1:
                    buffered.add(batch[0])
                    break
                batch = batch[:len(batch) // 2]
            if not accepted and len(cands) <= 1:
                break
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

    stall = 0
    iters = iters + it          # buffering iterations do not eat the upsizing budget
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

    # Phase 4 (optional): repair_hold. Min-delay STA at the fast corner lists
    # failing hold endpoints; each gets one delay element in front of its
    # data pin (dlygate if the library has one, else buf_1). A round is kept
    # only if hold TNS improves and setup WNS at the slow corner stays where
    # it was (or above the margin). Repeats until hold is clean or the cap.
    hold_before = hold_after = None
    delay_cells = 0
    if repair_hold:
        hold_lib = lib_fast or sta_liberty
        dcell, dpin_in, dpin_out = pick_delay_cell(liberty, fam)
        hold = run_sta(opensta, hold_lib, cur, top, constraints, out_dir, 'hold0', k=max_paths, slack_max=0.0, mode='min')
        hold_before = (hold.wns, hold.tns)
        log(f'hold @fast: WNS={hold.wns:+.3f} TNS={hold.tns:+.2f} failing={len(hold.paths)}; delay cell {dcell}')
        setup_floor = min(sta.wns, margin)
        for rnd in range(hold_iters):
            if not hold.paths or not hold.ok:
                break
            nl = Netlist(text, out_pins)
            n = 0
            for pth in hold.paths:
                # report_checks names a register endpoint by instance; the data
                # pin is the last stage of the path (e.g. _665_/D).
                ep = pth.endpoint
                last = pth.stages[-1] if pth.stages else None
                if ep in nl.inst:
                    pin = last.pin if (last and last.inst == ep) else 'D'
                    n += nl.delay_pin(ep, pin, dcell, 1, dpin_in, dpin_out)
                elif '/' in ep and ep.rsplit('/', 1)[0] in nl.inst:
                    inst, pin = ep.rsplit('/', 1)
                    n += nl.delay_pin(inst, pin, dcell, 1, dpin_in, dpin_out)
                else:
                    n += nl.delay_port(ep, dcell, 1, dpin_in, dpin_out)
            if n == 0:
                break
            it += 1
            new = out_dir / f'it{it}.v'
            new.write_text(nl.render())
            hold_new = run_sta(opensta, hold_lib, new, top, constraints, out_dir, f'hold{rnd + 1}', k=max_paths, slack_max=0.0, mode='min')
            setup_new = run_sta(opensta, sta_liberty, new, top, constraints, out_dir, f'it{it}', k=max_paths, slack_max=margin)
            ok = (hold_new.ok and setup_new.ok and hold_new.tns > hold.tns + 1e-6
                  and setup_new.wns >= setup_floor - 1e-6 and setup_new.tns >= sta.tns - 0.05)
            steps.append(Step(it=it, moves={f'hold:{p.endpoint}': dcell for p in hold.paths}, wns_before=hold.wns,
                              tns_before=hold.tns, wns_after=hold_new.wns, tns_after=hold_new.tns, accepted=ok))
            log(f'it{it} (hold): {n} delay cells on {len(hold.paths)} endpoints -> hold WNS {hold.wns:+.3f}->{hold_new.wns:+.3f} '
                f'TNS {hold.tns:+.2f}->{hold_new.tns:+.2f}; setup WNS {sta.wns:+.3f}->{setup_new.wns:+.3f} {"ACCEPT" if ok else "reject"}')
            if not ok:
                break
            cur, text, hold, sta = new, new.read_text(), hold_new, setup_new
            types = instance_types(text)
            delay_cells += n
            total_moves[f'hold_round_{rnd + 1}'] = f'{n} x {dcell}'
            states.append((cur, sta.wns, sta.tns, dict(total_moves)))
        hold_after = (hold.wns, hold.tns)

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
           'moves': total_moves, 'steps': [asdict(s) for s in steps], 'wns_regressed': wns_regressed,
           'buffers_inserted': buffers_inserted, 'delay_cells_inserted': delay_cells,
           'hold_before': hold_before, 'hold_after': hold_after}
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
    ap.add_argument('--repair-design', action='store_true', help='split high-fanout nets on failing paths with buffer trees')
    ap.add_argument('--max-fanout', type=int, default=8)
    ap.add_argument('--repair-hold', action='store_true', help='insert delay cells on failing hold endpoints (fast corner) while setup holds')
    ap.add_argument('--lib-fast', help='fast-corner liberty for hold analysis (default: --lib-sta)')
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
                 recover_area=a.recover_area, recover_rounds=a.recover_rounds,
                 repair_design=a.repair_design, max_fanout=a.max_fanout,
                 repair_hold=a.repair_hold, lib_fast=a.lib_fast, log=log)
    if a.json:
        print(json.dumps(res, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
