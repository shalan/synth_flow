#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
liberty_timing — the few numbers synthesis budgeting needs from a liberty file.

    from liberty_timing import read_liberty_timing
    lt = read_liberty_timing('sky130/hd_120_ss.lib')
    lt.flop_cells          # ['sky130_fd_sc_hd__dfxtp_1', ...]  (cells with an ff group)
    lt.t_cq_ps             # representative clock-to-Q (ps)
    lt.t_su_ps             # representative setup (ps)
    lt.cells               # all cell names
    lt.default_wire_load   # e.g. 'Small'

"Representative" = the median over the timing tables of the smallest-area
flop, evaluated at the middle of each table. Budgets subtract these from the
clock period; a median at nominal slew/load is the right order of magnitude
without being the worst corner of the worst table (that would double-count
the margin the STA loop later measures for real).

This is a purpose-built scanner, not a full liberty parser: it tracks
`cell(...) { ... }` blocks by brace depth and pulls `area`, `ff(...)`,
`pin(...)`, `timing()` groups with `timing_type`, `related_pin` and
`cell_rise`/`cell_fall`/`rise_constraint`/`fall_constraint` value tables.
"""
from __future__ import annotations

import fnmatch
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_TIME_UNIT_RE = re.compile(r'time_unit\s*:\s*"?\s*1\s*(ps|ns)\s*"?', re.I)
_CELL_RE = re.compile(r'\bcell\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{')
_AREA_RE = re.compile(r'\barea\s*:\s*([0-9.eE+-]+)')
_FF_RE = re.compile(r'\bff\s*\(')
_PIN_RE = re.compile(r'\bpin\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{')
_BUS_RE = re.compile(r'\bbus\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{')
_TYPE_RE = re.compile(r'^\s*type\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{', re.M)


def _strip_groups(body: str, group_re) -> str:
    """Blank out every `<group>(...) { ... }` block so nested pins are not rescanned."""
    out, pos = [], 0
    for m in group_re.finditer(body):
        if m.start() < pos:
            continue
        _, end = _block(body, m.end() - 1)
        out.append(body[pos:m.start()])
        out.append(' ' * (end - m.start()))
        pos = end
    out.append(body[pos:])
    return ''.join(out)
_TIMING_RE = re.compile(r'\btiming\s*\(\s*\)\s*\{')
_TTYPE_RE = re.compile(r'timing_type\s*:\s*"?([a-z_]+)"?', re.I)
_RELPIN_RE = re.compile(r'related_pin\s*:\s*"([^"]+)"')
_TABLE_RE = re.compile(r'\b(cell_rise|cell_fall|rise_constraint|fall_constraint)\s*\([^)]*\)\s*\{(.*?)\}', re.S)
_VALUES_RE = re.compile(r'values\s*\((.*?)\)\s*;', re.S)


@dataclass
class LibertyTiming:
    path: str
    time_unit_ps: float = 1000.0          # multiplier to ps (ns -> 1000, ps -> 1)
    cells: list[str] = field(default_factory=list)
    flop_cells: list[str] = field(default_factory=list)
    flop_area: dict[str, float] = field(default_factory=dict)
    t_cq_ps: Optional[float] = None
    t_su_ps: Optional[float] = None
    t_cq_by_cell_ps: dict[str, float] = field(default_factory=dict)
    t_su_by_cell_ps: dict[str, float] = field(default_factory=dict)
    default_wire_load: Optional[str] = None
    reference_flop: Optional[str] = None


def _block(text: str, open_idx: int) -> tuple[str, int]:
    """Return (body, end_index) of the brace block whose '{' is at open_idx."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i + 1
        i += 1
    return text[open_idx + 1:], n


def _table_values(body: str) -> list[float]:
    m = _VALUES_RE.search(body)
    if not m:
        return []
    nums = re.findall(r'[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?', m.group(1))
    return [float(x) for x in nums]


def _mid(values: list[float]) -> Optional[float]:
    """Middle entry of a flattened table (nominal slew / load)."""
    if not values:
        return None
    return values[len(values) // 2]


def read_liberty_timing(path: str | Path) -> LibertyTiming:
    text = Path(path).read_text(errors='ignore')
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    lt = LibertyTiming(path=str(path))
    m = _TIME_UNIT_RE.search(text)
    if m:
        lt.time_unit_ps = 1.0 if m.group(1).lower() == 'ps' else 1000.0
    m = re.search(r'default_wire_load\s*:\s*"?([A-Za-z0-9_]+)"?', text)
    if m:
        lt.default_wire_load = m.group(1)

    for cm in _CELL_RE.finditer(text):
        name = cm.group(1)
        body, _ = _block(text, cm.end() - 1)
        lt.cells.append(name)
        if not _FF_RE.search(body):
            continue
        lt.flop_cells.append(name)
        am = _AREA_RE.search(body)
        lt.flop_area[name] = float(am.group(1)) if am else float('inf')
        cq: list[float] = []
        su: list[float] = []
        for pm in _PIN_RE.finditer(body):
            pbody, _ = _block(body, pm.end() - 1)
            for tm in _TIMING_RE.finditer(pbody):
                tbody, _ = _block(pbody, tm.end() - 1)
                tt = _TTYPE_RE.search(tbody)
                ttype = tt.group(1).lower() if tt else ''
                for tab in _TABLE_RE.finditer(tbody):
                    kind, tbl = tab.group(1), tab.group(2)
                    v = _mid(_table_values(tbl))
                    if v is None:
                        continue
                    if ttype in ('rising_edge', 'falling_edge') and kind in ('cell_rise', 'cell_fall'):
                        cq.append(v)
                    elif ttype in ('setup_rising', 'setup_falling') and kind in ('rise_constraint', 'fall_constraint'):
                        su.append(v)
        if cq:
            lt.t_cq_by_cell_ps[name] = statistics.median(cq) * lt.time_unit_ps
        if su:
            lt.t_su_by_cell_ps[name] = statistics.median(su) * lt.time_unit_ps

    # Representative flop: smallest-area flop that has both numbers (the one
    # dfflibmap picks for a plain DFF), else any flop with numbers.
    candidates = [c for c in lt.flop_cells if c in lt.t_cq_by_cell_ps and c in lt.t_su_by_cell_ps]
    if candidates:
        ref = min(candidates, key=lambda c: lt.flop_area.get(c, float('inf')))
        lt.reference_flop = ref
        lt.t_cq_ps = round(lt.t_cq_by_cell_ps[ref], 1)
        lt.t_su_ps = round(lt.t_su_by_cell_ps[ref], 1)
    return lt


if __name__ == '__main__':
    import sys
    for p in sys.argv[1:]:
        lt = read_liberty_timing(p)
        print(f'{p}: {len(lt.cells)} cells, {len(lt.flop_cells)} flops, unit={lt.time_unit_ps}ps')
        print(f'  reference flop {lt.reference_flop}: t_cq={lt.t_cq_ps} ps  t_su={lt.t_su_ps} ps  '
              f'default_wire_load={lt.default_wire_load}')
        for c in lt.flop_cells:
            print(f'  {c:32} area={lt.flop_area[c]:6.2f} t_cq={lt.t_cq_by_cell_ps.get(c, float("nan")):7.1f} '
                  f't_su={lt.t_su_by_cell_ps.get(c, float("nan")):7.1f}')


# ---------------------------------------------------------------------------
# LibCells: a technology-agnostic cell catalogue over one or more liberty files
# ---------------------------------------------------------------------------

@dataclass
class CellInfo:
    name: str
    area: float = 0.0
    dont_use: bool = False
    is_ff: bool = False
    pins: dict = field(default_factory=dict)      # pin -> {'dir', 'cap', 'function', 'max_cap'}
    # representative delay of the (single) output arc, ps, when there is exactly one input
    delay_ps: Optional[float] = None

    @property
    def inputs(self) -> list[str]:
        return sorted(p for p, i in self.pins.items() if i['dir'] == 'input')

    @property
    def outputs(self) -> list[str]:
        return sorted(p for p, i in self.pins.items() if i['dir'] == 'output')

    def footprint(self) -> tuple:
        """Same footprint = same pin names, directions and functions: safe to
        swap one cell for another (drive-strength variants)."""
        return tuple(sorted((p, i['dir'], _norm_fn(i.get('function'))) for p, i in self.pins.items()))

    def drive(self) -> float:
        """Drive proxy: max_capacitance of the output, else area."""
        caps = [i.get('max_cap') for p, i in self.pins.items() if i['dir'] == 'output' and i.get('max_cap')]
        return max(caps) if caps else self.area


def _norm_fn(fn: Optional[str]) -> str:
    return re.sub(r'[\s()"]', '', fn or '')


class LibCells:
    """Cells of one or more liberty files (standard cells plus hard macros),
    classified by function rather than by name so repairs work on any library.

        lc = LibCells(['sky130/hd_120_ss.lib', 'sram.lib'], dont_use=['*probe*'])
        lc.output_pins('sky130_fd_sc_hd__nand2_1')   -> {'Y'}
        lc.buffers()                                  -> buffer cells, weakest first
        lc.next_size(cell) / lc.prev_size(cell)       -> same footprint, next drive
        lc.delay_cell()                               -> (cell, in_pin, out_pin), slowest buffer
    """

    def __init__(self, libs, dont_use=()):
        self.cells: dict[str, CellInfo] = {}
        self.libs = [str(l) for l in libs]
        self.dont_use_patterns = list(dont_use or [])
        for lib in self.libs:
            self._read(lib)
        for c in self.cells.values():
            if any(fnmatch.fnmatchcase(c.name, pat) for pat in self.dont_use_patterns):
                c.dont_use = True
        self._families: dict[tuple, list[str]] = {}
        for c in self.cells.values():
            if c.outputs:      # flops included: dfxtp_1/2/4 share a footprint too
                self._families.setdefault(c.footprint(), []).append(c.name)
        for fp, names in self._families.items():
            # area is the reliable drive order within one footprint (max_capacitance
            # is not monotonic with size on every library, e.g. sky130 HS nand2)
            names.sort(key=lambda n: (self.cells[n].area, self.cells[n].drive(), n))

    def _read(self, lib: str) -> None:
        text = re.sub(r'/\*.*?\*/', '', Path(lib).read_text(errors='ignore'), flags=re.S)
        m = _TIME_UNIT_RE.search(text)
        tu = 1.0 if (m and m.group(1).lower() == 'ps') else 1000.0
        um = re.search(r'capacitive_load_unit\s*\(\s*([0-9.]+)\s*,\s*"?(pf|ff)"?\s*\)', text, re.I)
        cu = (1000.0 if um and um.group(2).lower() == 'pf' else 1.0) * (float(um.group(1)) if um else 1.0)
        # library-level bus types: `type (data) { bit_from : 0; bit_to : 31; }`.
        # bit_from is the first (leftmost) bit of a Verilog connection, which is
        # how OpenSTA numbers the bits; OpenRAM liberties are [0:N-1].
        bus_types: dict[str, tuple[int, int]] = {}
        for tm in _TYPE_RE.finditer(text):
            tbody, _ = _block(text, tm.end() - 1)
            bf = re.search(r'\bbit_from\s*:\s*(\d+)', tbody)
            bt = re.search(r'\bbit_to\s*:\s*(\d+)', tbody)
            if bf and bt:
                bus_types[tm.group(1)] = (int(bf.group(1)), int(bt.group(1)))
        for cm in _CELL_RE.finditer(text):
            body, _ = _block(text, cm.end() - 1)
            ci = CellInfo(name=cm.group(1))
            am = _AREA_RE.search(body)
            ci.area = float(am.group(1)) if am else 0.0
            ci.is_ff = bool(_FF_RE.search(body)) or bool(re.search(r'\blatch\s*\(', body))
            ci.dont_use = bool(re.search(r'\bdont_use\s*:\s*true', body, re.I))
            delays: list[float] = []
            # bus(...) groups (hard macros): direction/capacitance live on the bus,
            # the netlist connects the bus name, so record one pin per bus and
            # drop the group from the scalar-pin scan below.
            for bm in _BUS_RE.finditer(body):
                bbody, bend = _block(body, bm.end() - 1)
                d = re.search(r'\bdirection\s*:\s*"?(\w+)"?', bbody)
                cap = re.search(r'\bcapacitance\s*:\s*([0-9.eE+-]+)', bbody)
                mc = re.search(r'\bmax_capacitance\s*:\s*([0-9.eE+-]+)', bbody)
                bt = re.search(r'\bbus_type\s*:\s*"?(\w+)"?', bbody)
                rng = re.search(r'\bpin\s*\(\s*"?[^"()\[\s]+\[(\d+):(\d+)\]"?\s*\)', bbody)
                brange = bus_types.get(bt.group(1)) if bt else None
                if brange is None and rng:
                    brange = (int(rng.group(1)), int(rng.group(2)))
                ci.pins[bm.group(1)] = {
                    'dir': d.group(1).lower() if d else 'input',
                    'cap': float(cap.group(1)) * cu if cap else None,
                    'max_cap': float(mc.group(1)) * cu if mc else None,
                    'function': None, 'bus': True,
                    'range': brange,          # (first bit, last bit) of a Verilog connection
                }
            body = _strip_groups(body, _BUS_RE)
            for pm in _PIN_RE.finditer(body):
                pbody, _ = _block(body, pm.end() - 1)
                d = re.search(r'\bdirection\s*:\s*"?(\w+)"?', pbody)
                cap = re.search(r'\bcapacitance\s*:\s*([0-9.eE+-]+)', pbody)
                mc = re.search(r'\bmax_capacitance\s*:\s*([0-9.eE+-]+)', pbody)
                fn = re.search(r'\bfunction\s*:\s*"([^"]*)"', pbody)
                ci.pins[pm.group(1)] = {
                    'dir': d.group(1).lower() if d else 'input',
                    'cap': float(cap.group(1)) * cu if cap else None,
                    'max_cap': float(mc.group(1)) * cu if mc else None,
                    'function': fn.group(1) if fn else None,
                }
                if d and d.group(1).lower() == 'output':
                    for tm in _TIMING_RE.finditer(pbody):
                        tbody, _ = _block(pbody, tm.end() - 1)
                        for tab in _TABLE_RE.finditer(tbody):
                            if tab.group(1) in ('cell_rise', 'cell_fall'):
                                v = _mid(_table_values(tab.group(2)))
                                if v is not None:
                                    delays.append(v * tu)
            if delays:
                ci.delay_ps = statistics.median(delays)
            self.cells[ci.name] = ci

    # ---- queries -----------------------------------------------------------
    def __contains__(self, name: str) -> bool:
        return name in self.cells

    def output_pins(self, cell: str) -> Optional[set]:
        c = self.cells.get(cell)
        return set(c.outputs) if c else None

    def bus_ranges(self) -> dict[str, dict[str, tuple[int, int]]]:
        """{cell: {bus pin: (msb, lsb)}} for cells with bus() pins (hard macros)."""
        out: dict[str, dict[str, tuple[int, int]]] = {}
        for c in self.cells.values():
            r = {p: i['range'] for p, i in c.pins.items() if i.get('bus') and i.get('range')}
            if r:
                out[c.name] = r
        return out

    def input_cap_ff(self, cell: str, pin: str) -> Optional[float]:
        c = self.cells.get(cell)
        return c.pins.get(pin, {}).get('cap') if c else None

    def usable(self, name: str) -> bool:
        c = self.cells.get(name)
        return bool(c) and not c.dont_use

    def buffers(self) -> list[CellInfo]:
        """Non-inverting single-input single-output cells (function == input pin),
        usable, weakest drive first."""
        out = []
        for c in self.cells.values():
            if c.is_ff or c.dont_use or len(c.inputs) != 1 or len(c.outputs) != 1:
                continue
            fn = _norm_fn(c.pins[c.outputs[0]].get('function'))
            if fn == c.inputs[0]:
                out.append(c)
        return sorted(out, key=lambda c: (c.area, c.drive(), c.name))

    def _buffer_family(self) -> list:
        """The main buffer family: the base name with the most drive variants
        (buf_1/2/4/... rather than clkbuf/bufbuf/dlygate), weakest first."""
        bufs = self.buffers()
        by_base: dict[str, list] = {}
        for b in bufs:
            by_base.setdefault(self._base(b.name), []).append(b)
        if not by_base:
            return []
        fam = max(by_base.values(), key=lambda v: (self._plain(v[0].name), len(v), -min(c.area for c in v)))
        return sorted(fam, key=lambda c: (c.area, c.drive()))

    @staticmethod
    def _plain(name: str) -> int:
        """1 for ordinary cells, 0 for clock-tree / low-power flavours (clkbuf, clkinv, lp...)."""
        n = name.split('__')[-1].lower()
        return 0 if (n.startswith('clk') or n.startswith('lp') or '_lp' in n or 'kapwr' in n) else 1

    def buffer_cell(self, rank: int = 1) -> tuple[str, str, str]:
        """(cell, in_pin, out_pin): the `rank`-th weakest member of the main
        buffer family (default: second weakest, a mid-drive tree buffer)."""
        fam = self._buffer_family() or self.buffers()
        if not fam:
            raise RuntimeError('no buffer cell (single input, function == input) found in the liberty')
        b = fam[min(rank, len(fam) - 1)]
        return b.name, b.inputs[0], b.outputs[0]

    def delay_cell(self) -> tuple[str, str, str]:
        """Slowest usable buffer among the weak-drive half of the buffers, by its
        own timing table: a dlygate when the library has one, else the smallest
        plain buffer. Strong two-stage buffers (bufbuf_16) are slow on the table
        only because of their size and are excluded by the drive filter."""
        bufs = [b for b in self.buffers() if b.delay_ps is not None]
        if not bufs:
            return self.buffer_cell(0)
        areas = sorted(b.area for b in bufs)
        median = areas[len(areas) // 2]
        weak = [b for b in bufs if b.area <= median] or bufs
        dly = [b for b in weak if 'dly' in b.name.lower() or 'delay' in b.name.lower()]
        if dly:                                   # explicit delay cells: slowest of them
            b = max(dly, key=lambda c: (c.delay_ps, -c.area))
        else:                                     # no delay cells: weakest plain buffer
            fam = self._buffer_family()
            b = fam[0] if fam else max(weak, key=lambda c: (c.delay_ps, -c.area))
        return b.name, b.inputs[0], b.outputs[0]

    def family(self, cell: str) -> list[str]:
        c = self.cells.get(cell)
        return self._families.get(c.footprint(), [cell]) if c else [cell]

    @staticmethod
    def _base(name: str) -> str:
        return re.sub(r'_\d+$', '', name)

    def _ordered_family(self, cell: str) -> list[str]:
        """Usable cells with the same footprint, weakest first; cells sharing the
        base name (nand2_1/nand2_2/...) are preferred over other members
        (clkinv/bufinv...) when both exist at a given drive."""
        fam = [n for n in self.family(cell) if self.usable(n) or n == cell]
        same = [n for n in fam if self._base(n) == self._base(cell)]
        return same if len(same) > 1 else fam

    def next_size(self, cell: str) -> Optional[str]:
        fam = self._ordered_family(cell)
        if cell not in fam:
            return None
        i = fam.index(cell)
        return fam[i + 1] if i + 1 < len(fam) else None

    def prev_size(self, cell: str) -> Optional[str]:
        fam = self._ordered_family(cell)
        if cell not in fam:
            return None
        i = fam.index(cell)
        return fam[i - 1] if i > 0 else None

    def default_driving_cell(self, preferred: Optional[str] = None) -> Optional[str]:
        """`preferred` if the library has it; else a mid-drive inverter (single
        input, function == !input), else a mid-drive buffer."""
        if preferred and preferred in self.cells:
            return preferred
        invs = []
        for c in self.cells.values():
            if c.is_ff or c.dont_use or len(c.inputs) != 1 or len(c.outputs) != 1:
                continue
            fn = _norm_fn(c.pins[c.outputs[0]].get('function'))
            if fn in ('!' + c.inputs[0], c.inputs[0] + "'"):
                invs.append(c)
        if invs:
            # prefer the largest same-base-name family (inv_1/2/4/... over clkinv),
            # then take its second-weakest member as a mid-drive default
            by_base: dict[str, list] = {}
            for c in invs:
                by_base.setdefault(self._base(c.name), []).append(c)
            fam = max(by_base.values(), key=lambda v: (self._plain(v[0].name), len(v), -min(c.area for c in v)))
            fam.sort(key=lambda c: (c.area, c.drive()))
            return fam[min(1, len(fam) - 1)].name
        bufs = self.buffers()
        return bufs[min(1, len(bufs) - 1)].name if bufs else None
