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
