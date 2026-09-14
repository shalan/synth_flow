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

Sizing preserves function by construction: a drive family is the set of
liberty cells with identical pin names, directions and functions
(`liberty_timing.LibCells`), so swaps never change logic and no equivalence
check is needed. Buffers, the delay cell and the default driving cell are
also taken from the liberty (by function, not by name), and hard-macro
liberties passed with --extra-lib are loaded into STA and the netlist
model, so the pass works on any Sky130 variant (hd/hs/ms/ls/lp) or other
technology, with or without SRAM macros.

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
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth_flow as sf  # noqa: E402
from liberty_timing import LibCells  # noqa: E402

# --------------------------------------------------------------------------
# Liberty: drive variants per function
# --------------------------------------------------------------------------

def drive_families(liberty, dont_use=()) -> LibCells:
    """Cell catalogue over one or more liberty files (kept name for callers)."""
    libs = [liberty] if isinstance(liberty, (str, Path)) else list(liberty)
    return LibCells(libs, dont_use=dont_use)


def prev_size(cell: str, fam: LibCells) -> Optional[str]:
    return fam.prev_size(cell)


def next_size(cell: str, fam: LibCells) -> Optional[str]:
    return fam.next_size(cell)


# --------------------------------------------------------------------------
# Netlist: instance -> type, in-place retyping
# --------------------------------------------------------------------------

_INST_RE = re.compile(r'^(\s*)(\\?[A-Za-z_$][\w$\.]*)\s+(\\?[\w$\[\]\.]+)\s*\(', re.M)


def instance_types(netlist_text: str) -> dict[str, str]:
    out = {}
    for m in _INST_RE.finditer(netlist_text):
        typ, inst = m.group(2), m.group(3)
        if typ in ('module', 'input', 'output', 'inout', 'wire', 'reg', 'assign', 'always', 'initial'):
            continue
        out[inst] = typ
    return out


def sta_name(name: str) -> str:
    """OpenSTA's spelling of a Verilog identifier: escaped names lose the
    leading backslash and trailing space (`\\u_cg.lat[1] ` -> `u_cg.lat[1]`)."""
    n = name.strip()
    return n[1:] if n.startswith('\\') else n


def name_alias(netlist_text: str) -> dict[str, str]:
    """{OpenSTA name: Verilog name} for every instance, wire and port whose
    spelling differs (escaped identifiers). Used to map STA report names back
    onto the netlist so escaped instances are found, not mistaken for ports."""
    names = {m.group(3) for m in _INST_RE.finditer(netlist_text)
             if m.group(2) not in _NOT_INSTANCES and not m.group(2).startswith('$')}
    names |= set(re.findall(r'\b(?:wire|input|output|inout)\s+(?:(?:wire|reg)\s+)?(?:\[[^\]]+\]\s*)?(\\?[\w$\.\[\]]+)\s*;', netlist_text))
    return {sta_name(n): n for n in names if sta_name(n) != n}


def output_ports(netlist_text: str) -> set[str]:
    """Verilog names of the module's output ports, vector bits included
    (`output [3:0] y` -> y, y[0..3]); an STA endpoint that is not an instance
    must be one of these to be treated as a port."""
    out: set[str] = set()
    for m in re.finditer(r'\boutput\s+(?:(?:wire|reg)\s+)?(?:\[(\d+):(\d+)\]\s*)?(\\?[\w$\.\[\]]+)\s*;', netlist_text):
        name = m.group(3)
        out.add(name)
        if m.group(1) is not None:
            hi, lo = int(m.group(1)), int(m.group(2))
            out |= {f'{name}[{i}]' for i in range(min(hi, lo), max(hi, lo) + 1)}
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

_INST_BLOCK_RE = re.compile(r'^(\s*)(\\?[A-Za-z_$][\w$\.]*)\s+(\\?[\w$\[\]\.]+)\s*\((.*?)\);', re.M | re.S)
_NOT_INSTANCES = {'module', 'input', 'output', 'inout', 'wire', 'reg', 'assign', 'always', 'initial', 'function', 'task'}
_PIN_CONN_RE = re.compile(r'\.(\w+)\s*\(\s*(.*?)\s*\)\s*(?:,|$)', re.S)
_OUT_PINS = {'X', 'Y', 'Q', 'Q_N', 'COUT', 'SUM', 'COUT_N', 'SUM_N', 'Z', 'HI', 'LO'}


def liberty_output_pins(liberty, dont_use=()) -> dict[str, set[str]]:
    """cell -> output pins, over one or more liberty files (standard cells + macros)."""
    lc = liberty if isinstance(liberty, LibCells) else drive_families(liberty, dont_use)
    return {name: set(c.outputs) for name, c in lc.cells.items()}


class Netlist:
    """Minimal editable view of a Yosys `write_verilog -noattr -noexpr` netlist.
    Instances are edited in place in the text; new wires are declared before
    the first instance and new instances appended before `endmodule`."""

    def __init__(self, text: str, out_pins: dict[str, set[str]],
                 bus_ranges: Optional[dict[str, dict[str, tuple[int, int]]]] = None):
        self.text = text
        self.out_pins = out_pins
        self.bus_ranges = bus_ranges or {}       # cell -> {bus pin: (msb, lsb)} from the liberty
        self.inst: dict[str, dict] = {}        # name -> {'type', 'pins': {pin: conn}, 'span': (a, b), 'indent'}
        self.counter = 0
        for m in _INST_BLOCK_RE.finditer(text):
            if m.group(2) in _NOT_INSTANCES or m.group(2).startswith('$'):
                continue
            pins = {pm.group(1): pm.group(2).strip() for pm in _PIN_CONN_RE.finditer(m.group(4))}
            self.inst[m.group(3)] = {'type': m.group(2), 'pins': pins, 'span': m.span(), 'indent': m.group(1), 'new': False}
        self._first_inst = min((i['span'][0] for i in self.inst.values() if i['span']), default=text.rfind('endmodule'))
        self.new_wires: list[str] = []
        self.out_ports = output_ports(text)
        self.names = set(self.inst) | set(re.findall(r'^\s*(?:wire|input|output|inout)\s+(?:\[[^\]]+\]\s*)?(\\?[\w$\.]+)', text, re.M))
        # `assign lhs = rhs;` feed-throughs (input -> output with no cell)
        self.assigns: dict[str, str] = {m.group(1).strip(): m.group(2).strip()
                                        for m in re.finditer(r'^\s*assign\s+(.+?)\s*=\s*(.+?)\s*;', text, re.M)}
        self.dropped_assigns: set[str] = set()
        self.extra_assigns: list[tuple[str, str]] = []
        # edit log for the persistent OpenSTA session (sta_session.StaSession.apply):
        # ('make_net', w) / ('make_instance', name, cell, pins) / ('reconnect', inst, pin, old, new) / ('unsupported', why)
        self.ops: list = []
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

    # ---- bus connections (hard macros): `.dout0({ \\q[31] , ... , \\q[0]  })` ----
    @staticmethod
    def _concat_items(conn: str) -> Optional[list[str]]:
        """Top-level items of a `{a, b, ...}` concatenation, or None."""
        c = conn.strip()
        if not (c.startswith('{') and c.endswith('}')):
            return None
        return [x.strip() for x in c[1:-1].split(',') if x.strip()]

    def _conn_bits(self, conn: str) -> list[str]:
        """Connection expression as a list of bit expressions, MSB first
        (constants and anything unparsable stay as single items)."""
        items = self._concat_items(conn)
        if items is None:
            items = [conn.strip()]
        out: list[str] = []
        for it in items:
            bits = self._bits(it) if not re.match(r"\d+'", it) else None
            out.extend(bits if bits else [it])
        return out

    @staticmethod
    def _pin_index(pin: str) -> tuple[str, Optional[int]]:
        m = re.fullmatch(r'(\\?[\w$\.]+)\[(\d+)\]', pin)
        return (m.group(1), int(m.group(2))) if m else (pin, None)

    def _bit_pos(self, inst: str, base: str, k: int, n_items: int) -> Optional[int]:
        """Position of bus bit `k` in the MSB-first item list of `inst/base`."""
        hi, lo = self.bus_ranges.get(self.inst[inst]['type'], {}).get(base, (n_items - 1, 0))
        pos = hi - k if hi >= lo else k - hi
        return pos if 0 <= pos < n_items else None

    def pin_net(self, inst: str, pin: str) -> Optional[str]:
        """Net connected to `inst/pin`; `pin` may be a bus bit (`din0[3]`) of a
        macro whose connection is a concatenation."""
        pins = self.inst[inst]['pins']
        if pin in pins:
            c = pins[pin]
            return None if self._concat_items(c) is not None else c
        base, k = self._pin_index(pin)
        if k is None or base not in pins:
            return None
        bits = self._conn_bits(pins[base])
        pos = self._bit_pos(inst, base, k, len(bits))
        return bits[pos] if pos is not None else None

    def set_pin_net(self, inst: str, pin: str, new: str, old: Optional[str] = None) -> bool:
        """Connect `inst/pin` to `new`. For a bus pin the connection is a
        concatenation: with a bit index in `pin` that bit is replaced, else every
        item equal to `old` is."""
        pins = self.inst[inst]['pins']
        if pin in pins and self._concat_items(pins[pin]) is None:
            self.ops.append(('reconnect', inst, pin, pins[pin], new))
            pins[pin] = new
            return True
        base, k = self._pin_index(pin)
        if base not in pins:
            return False
        bits = self._conn_bits(pins[base])
        hi, lo = self.bus_ranges.get(self.inst[inst]['type'], {}).get(base, (len(bits) - 1, 0))
        bit_index = (lambda pos: hi - pos) if hi >= lo else (lambda pos: pos + hi)     # inverse of _bit_pos
        if k is not None:
            pos = self._bit_pos(inst, base, k, len(bits))
            if pos is None:
                return False
            self.ops.append(('reconnect', inst, f'{base}[{k}]', bits[pos], new))
            bits[pos] = new
        elif old is not None and old in bits:
            for pos, b in enumerate(bits):
                if b == old:
                    self.ops.append(('reconnect', inst, f'{base}[{bit_index(pos)}]', old, new))
            bits = [new if b == old else b for b in bits]
        else:
            return False
        pins[base] = '{ ' + ' , '.join(bits) + ' }'
        return True

    def out_pin_name(self, cell: str) -> str:
        pins = self.out_pins.get(cell) or {'X'}
        return sorted(pins)[0]

    def is_output_pin(self, cell: str, pin: str) -> bool:
        pins = self.out_pins.get(cell)
        base = self._pin_index(pin)[0]
        return base in pins if pins else base in _OUT_PINS

    def _pin_has(self, conn: str, net: str) -> bool:
        return conn == net or (conn.startswith('{') and net in self._conn_bits(conn))

    def sinks(self, net: str) -> list[tuple[str, str]]:
        """(inst, pin) pairs whose input connection is `net` (a bus pin counts
        when one of its bits is `net`)."""
        return [(n, p) for n, i in self.inst.items() for p, c in i['pins'].items()
                if self._pin_has(c, net) and not self.is_output_pin(i['type'], p)]

    def driver(self, net: str) -> Optional[tuple[str, str]]:
        for n, i in self.inst.items():
            for p, c in i['pins'].items():
                if self._pin_has(c, net) and self.is_output_pin(i['type'], p):
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
        self.ops.append(('make_instance', name, cell, dict(pins)))
        return name

    def add_wire(self) -> str:
        w = self.fresh('_rdn_')
        self.new_wires.append(w)
        self.ops.append(('make_net', w))
        return w

    def buffer_tree(self, net: str, buf_cell: str, group: int, keep_ports: bool = True,
                    in_pin: str = 'A', out_pin: str = 'X') -> int:
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
            self.add_inst(buf_cell, {in_pin: net, out_pin: w})
            for inst, pin in g:
                self.set_pin_net(inst, pin, w, old=net)
            n += 1
        return n

    def delay_pin(self, inst: str, pin: str, delay_cell: str, count: int, in_pin='A', out_pin='X') -> int:
        """Feed input `inst/pin` through `count` delay cells."""
        net = self.pin_net(inst, pin)
        if net is None or re.match(r"\d+'", net):        # unknown pin or a constant
            return 0
        cur = net
        for _ in range(count):
            w = self.add_wire()
            self.add_inst(delay_cell, {in_pin: cur, out_pin: w})
            cur = w
        self.set_pin_net(inst, pin, cur, old=net)
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
            self.ops.append(('unsupported', f'assign feed-through {lhs} rewritten'))
            self.extra_assigns.extend((lb, rb) for lb, rb in pairs.items() if lb != port
                                      and not any(lb == e[0] for e in self.extra_assigns))
            cur = rhs_bit
            for k in range(count):
                w = port if k == count - 1 else self.add_wire()
                self.add_inst(delay_cell, {in_pin: cur, out_pin: w})
                cur = w
            return count
        w0 = self.add_wire()
        self.set_pin_net(drv[0], drv[1], w0, old=port)
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
            if name not in self.inst:
                continue
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
            # declare before the first real instance (Yosys puts all declarations
            # first); the regex also matches the module header, so skip non-instances
            k = None
            for m in _INST_BLOCK_RE.finditer(text):
                if m.group(3) in self.inst:
                    k = m.start()
                    break
            if k is None:
                k = text.rfind('endmodule')
            text = text[:k] + decl + text[k:]
        news = [n for n, i in self.inst.items() if i['new']]
        if news:
            k = text.rfind('endmodule')
            text = text[:k] + ''.join(block(n, self.inst[n]) + '\n' for n in news) + text[k:]
        return text


def pick_delay_cell(liberty, fam: LibCells) -> tuple[str, str, str]:
    """(cell, in_pin, out_pin): the slowest usable buffer in the catalogue."""
    return fam.delay_cell()


def structural_problems(text: str, fam: LibCells, limit: int = 5) -> list[str]:
    """Cheap sanity check of an edited netlist before it is timed: exactly one
    module, every instance a known cell, every pin a liberty pin of that cell,
    no net driven by more than one instance output. Returns problem strings
    (empty = fine)."""
    probs: list[str] = []
    if len(re.findall(r'^\s*module\s', text, re.M)) != 1:
        probs.append('netlist must contain exactly one module')
    nl = Netlist(text, liberty_output_pins(fam), fam.bus_ranges())
    drivers: dict[str, int] = {}
    for name, i in nl.inst.items():
        c = fam.cells.get(i['type'])
        if c is None:
            probs.append(f'{name}: unknown cell {i["type"]}')
            continue
        for p, conn in i['pins'].items():
            if p not in c.pins:
                probs.append(f'{name}/{p}: not a pin of {i["type"]}')
            elif c.pins[p]['dir'] == 'output':
                for b in nl._conn_bits(conn):
                    if not re.match(r"\d+'", b):
                        drivers[b] = drivers.get(b, 0) + 1
        if len(probs) >= limit:
            break
    for net, n in drivers.items():
        if n > 1:
            probs.append(f'net {net} has {n} drivers')
            if len(probs) >= limit:
                break
    return probs[:limit]


# --------------------------------------------------------------------------
# OpenSTA: worst path per failing endpoint with stage details
# --------------------------------------------------------------------------

REPORT_TCL = """\
report_checks -path_delay {mode} -group_path_count {k} -endpoint_path_count 1 -slack_max {slack_max} -format full_clock_expanded -fields {{fanout cap slew}} -digits 4
report_worst_slack -{mode} -digits 4
report_tns -{mode} -digits 4
"""

PATHS_TCL = """\
read_liberty {liberty}
{extra_libs}
read_verilog {netlist}
link_design {top}
{constraints}
""" + REPORT_TCL + 'exit\n'

# --- persistent OpenSTA sessions (sta_session: true) -----------------------
# One interactive `sta` per liberty set, alive for the whole post-pass: the
# liberties are read once and each trial re-links the netlist (or, with a
# pending incremental edit, re-times only what changed). Off by default.
USE_SESSION = False
SESSIONS: dict = {}
SESSION_LOG = print


VERIFY_SESSION = False
PENDING = {'base': None, 'ops': None}     # the trial about to be timed: edits relative to `base`


def _trial_begin(base, ops) -> None:
    """Declare the next run_sta calls as a trial derived from `base` by `ops`
    (None or an 'unsupported' op = re-link from the file instead)."""
    if ops is not None and any(o[0] == 'unsupported' for o in ops):
        ops = None
    PENDING['base'], PENDING['ops'] = (Path(base) if base is not None else None), (list(ops) if ops is not None else None)


def _trial_end(accepted: bool, new_path) -> None:
    """Close the pending trial in every session: commit the edits (the linked
    design is now `new_path`) or undo them (it is `base` again)."""
    for s in SESSIONS.values():
        if s._undo:
            try:
                if accepted:
                    s.commit(Path(new_path))
                else:
                    s.undo()
            except Exception as e:      # the next run_sta re-links from the file
                SESSION_LOG(f'sta session: {e}; re-linking on the next trial')
                s.current = None
                s._undo = []
    PENDING['base'], PENDING['ops'] = None, None


def _session_for(opensta, liberty, extra_libs):
    from sta_session import StaSession
    key = (str(liberty), tuple(str(l) for l in (extra_libs or [])))
    s = SESSIONS.get(key)
    if s is None or not s.alive():
        s = StaSession(opensta, [liberty, *(extra_libs or [])], log=SESSION_LOG)
        s.start()
        SESSIONS[key] = s
    return s


def close_sessions() -> dict:
    """Close every session; returns their combined statistics."""
    tot = {'relinks': 0, 'incremental': 0, 'commands': 0, 'seconds': 0.0, 'sessions': len(SESSIONS)}
    for s in SESSIONS.values():
        for k in ('relinks', 'incremental', 'commands', 'seconds'):
            tot[k] += s.stats[k]
        s.close()
    SESSIONS.clear()
    return tot

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
    power: Optional[dict] = None      # report_power groups when the run asked for power (see run_sta(power=True))


STA_STATS = {'calls': 0, 'seconds': 0.0}     # every OpenSTA run of this process (resize() reports per phase)


POWER_REPORT_TCL = 'puts ">>> POWER_BEGIN"\nreport_power -digits 6\nputs ">>> POWER_END"\n'


def run_sta(opensta, liberty, netlist: Path, top, constraints, out_dir: Path, tag, k=200, slack_max=0.0,
            mode='max', extra_libs=(), alias=None, power=False) -> StaOut:
    report = REPORT_TCL.format(k=k, slack_max=slack_max, mode=mode) + (POWER_REPORT_TCL if power else '')
    t0 = time.time()
    if USE_SESSION:
        # persistent process: liberties already read; re-link only when the
        # linked design is not this netlist (incremental trials keep it in step)
        from sta_session import StaSessionError
        try:
            ses = _session_for(opensta, liberty, extra_libs)
            incremental = (PENDING['ops'] is not None and ses.current is not None and PENDING['base'] is not None
                           and Path(ses.current) == PENDING['base'] and ses.top == top and not ses._undo
                           and Path(netlist) != PENDING['base'])
            if incremental:
                try:
                    ses.apply(PENDING['ops'])            # re-times only what the edits touch
                except StaSessionError as e:
                    SESSION_LOG(f'sta session: incremental edit failed ({e}); re-linking {Path(netlist).name}')
                    ses._undo = []
                    ses.link(Path(netlist), top, constraints)
            elif ses.current is None or Path(ses.current) != Path(netlist) or ses.top != top:
                ses.link(Path(netlist), top, constraints)
            text = ses.report(report)
            ok_run = True
            if VERIFY_SESSION:
                vt = out_dir / f'{tag}.verify.tcl'
                vt.write_text(PATHS_TCL.format(liberty=liberty, netlist=netlist, top=top, constraints=constraints,
                                               k=k, slack_max=slack_max, mode=mode,
                                               extra_libs='\n'.join(f'read_liberty {l}' for l in (extra_libs or []))))
                vr = subprocess.run([opensta, '-no_init', '-exit', str(vt)], capture_output=True, text=True, timeout=1800)
                a, b = parse_sta_report(text), parse_sta_report(vr.stdout + vr.stderr)
                same = (a.wns is not None and b.wns is not None and abs(a.wns - b.wns) < 5e-4
                        and a.tns is not None and b.tns is not None and abs(a.tns - b.tns) < 5e-3)
                STA_STATS['verify'] = STA_STATS.get('verify', 0) + 1
                if not same:
                    STA_STATS['verify_mismatch'] = STA_STATS.get('verify_mismatch', 0) + 1
                    SESSION_LOG(f'sta session VERIFY MISMATCH {tag} ({"incremental" if incremental else "relink"}): '
                                f'session WNS/TNS {a.wns}/{a.tns} vs fresh {b.wns}/{b.tns}')
        except StaSessionError as e:
            SESSION_LOG(f'sta session: {e}; falling back to a fresh OpenSTA process for {tag}')
            text, ok_run = None, False
        if text is None:
            ok_run = None
    else:
        text, ok_run = None, None
    if text is None:
        tcl = out_dir / f'{tag}.tcl'
        tcl.write_text(PATHS_TCL.format(liberty=liberty, netlist=netlist, top=top, constraints=constraints,
                                        k=k, slack_max=slack_max, mode=mode,
                                        extra_libs='\n'.join(f'read_liberty {l}' for l in (extra_libs or [])))
                       .replace('\nexit\n', '\n' + (POWER_REPORT_TCL if power else '') + 'exit\n'))
        r = subprocess.run([opensta, '-no_init', '-exit', str(tcl)], capture_output=True, text=True, timeout=1800)
        text, ok_run = r.stdout + r.stderr, (r.returncode == 0)
    STA_STATS['calls'] += 1
    STA_STATS['seconds'] += time.time() - t0
    (out_dir / f'{tag}.log').write_text(text)
    if alias is None:
        alias = name_alias(Path(netlist).read_text())
    res = parse_sta_report(text, alias, ok=bool(ok_run), slack_max=slack_max)
    res.log = str(out_dir / f'{tag}.log')
    return res


def parse_sta_report(text: str, alias: Optional[dict] = None, ok: bool = True, slack_max: float = 0.0) -> StaOut:
    """Paths, stages, worst slack and TNS from a report_checks log. Instance
    and endpoint names are mapped back to their Verilog spelling with `alias`."""
    alias = alias or {}
    res = StaOut(ok=ok)
    cur: Optional[PathInfo] = None
    for ln in text.splitlines():
        if ln.startswith('Endpoint:'):
            ep = ln.split()[1]
            cur = PathInfo(endpoint=alias.get(ep, ep), slack=0.0)
            res.paths.append(cur)
            continue
        m = re.match(r'\s*([-0-9.]+)\s+slack', ln)
        if m and cur is not None:
            cur.slack = float(m.group(1))
            continue
        m = _STAGE_RE.match(ln)
        if m and cur is not None and '/' in m.group(7):
            inst, pin = m.group(7).rsplit('/', 1)
            inst = alias.get(inst, inst)
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
    if '>>> POWER_BEGIN' in text:
        res.power = sf._parse_power(text[text.find('>>> POWER_BEGIN'):text.find('>>> POWER_END')])
    return res


def _power_w(r: StaOut) -> Optional[float]:
    return r.power['total']['total_w'] if r and r.power and 'total' in r.power else None


def _report_text(opensta, liberty, netlist: Path, top, constraints, out_dir: Path, tag, report: str, extra_libs=()) -> str:
    """Output of an arbitrary report Tcl on the linked netlist: through the
    persistent session when enabled (linking only if needed), else a fresh process."""
    if USE_SESSION:
        from sta_session import StaSessionError
        try:
            ses = _session_for(opensta, liberty, extra_libs)
            if ses.current is None or Path(ses.current) != Path(netlist) or ses.top != top:
                ses.link(Path(netlist), top, constraints)
            return ses.report(report)
        except StaSessionError as e:
            SESSION_LOG(f'sta session: {e}; report from a fresh process')
    tcl = out_dir / f'{tag}.tcl'
    tcl.write_text(f"read_liberty {liberty}\n" + '\n'.join(f'read_liberty {l}' for l in (extra_libs or []))
                   + f"\nread_verilog {netlist}\nlink_design {top}\n{constraints}\n{report}exit\n")
    r = subprocess.run([opensta, '-no_init', '-exit', str(tcl)], capture_output=True, text=True, timeout=1800)
    STA_STATS['calls'] += 1
    return r.stdout + r.stderr


DRC_TCL = 'puts ">>> DRC_BEGIN"\nreport_check_types -max_slew -max_capacitance -max_fanout -violators -digits 4\nputs ">>> DRC_END"\n'
DRC_KINDS = ('max_slew', 'max_capacitance', 'max_fanout')


def parse_drc(text: str, alias: Optional[dict] = None) -> dict:
    """`report_check_types -violators` -> {kind: [(pin, limit, actual, slack)]} with
    instance names in their Verilog spelling. Pins are `inst/pin` or a port."""
    alias = alias or {}
    out = {k: [] for k in DRC_KINDS}
    kind = None
    sec = text[text.find('>>> DRC_BEGIN'):text.find('>>> DRC_END')] if '>>> DRC_BEGIN' in text else text
    for line in sec.splitlines():
        s = line.strip()
        if s in ('max slew', 'max capacitance', 'max fanout'):
            kind = s.replace(' ', '_')
            continue
        parts = s.split()
        if kind and len(parts) >= 4 and parts[-1].startswith('(') and parts[0] not in ('Pin', 'Group'):
            try:
                limit, actual, slack = float(parts[-4]), float(parts[-3]), float(parts[-2])
            except ValueError:
                continue
            pin = parts[0]
            if '/' in pin:
                inst, p = pin.rsplit('/', 1)
                pin = f'{alias.get(inst, inst)}/{p}'
            else:
                pin = alias.get(pin, pin)
            out[kind].append((pin, limit, actual, slack))
    return out


def drc_summary(v: dict) -> dict:
    """{kind: count}, plus 'total_slack' (sum of negative slacks; fanout in sinks, slew in ns, cap in pF)."""
    s = {k: len(v.get(k, [])) for k in DRC_KINDS}
    s['total_slack'] = round(sum(x[3] for k in DRC_KINDS for x in v.get(k, []) if x[3] < 0), 4)
    return s


def run_drc(opensta, liberty, netlist: Path, top, constraints, out_dir: Path, tag, extra_libs=()) -> dict:
    text = _report_text(opensta, liberty, netlist, top, constraints, out_dir, tag, DRC_TCL, extra_libs)
    (out_dir / f'{tag}.drc.log').write_text(text)
    return parse_drc(text, name_alias(Path(netlist).read_text()))


def instance_power(opensta, liberty, netlist: Path, top, constraints, out_dir: Path, tag, insts, extra_libs=()) -> dict:
    """Total power per instance (W) from `report_power -instances`, in chunks;
    uses the persistent session when enabled. Names are Verilog spellings."""
    out: dict = {}
    insts = list(insts)
    for c0 in range(0, len(insts), 1000):
        chunk = insts[c0:c0 + 1000]
        names = ' '.join('{' + sta_name(n) + '}' for n in chunk)
        report = f'puts ">>> IPOWER_BEGIN"\nreport_power -instances [get_cells [list {names}]] -digits 6\nputs ">>> IPOWER_END"\n'
        text = None
        if USE_SESSION:
            from sta_session import StaSessionError
            try:
                ses = _session_for(opensta, liberty, extra_libs)
                if ses.current is None or Path(ses.current) != Path(netlist) or ses.top != top:
                    ses.link(Path(netlist), top, constraints)
                text = ses.report(report)
            except StaSessionError as e:
                SESSION_LOG(f'sta session: {e}; instance power from a fresh process')
        if text is None:
            tcl = out_dir / f'{tag}_{c0}.tcl'
            tcl.write_text(PATHS_TCL.split('\n' + REPORT_TCL.splitlines()[0])[0] if False else
                           f"read_liberty {liberty}\n" + '\n'.join(f'read_liberty {l}' for l in (extra_libs or []))
                           + f"\nread_verilog {netlist}\nlink_design {top}\n{constraints}\n{report}exit\n")
            r = subprocess.run([opensta, '-no_init', '-exit', str(tcl)], capture_output=True, text=True, timeout=1800)
            text = r.stdout + r.stderr
        alias = name_alias(Path(netlist).read_text())
        sec = text[text.find('>>> IPOWER_BEGIN'):text.find('>>> IPOWER_END')]
        for line in sec.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                try:
                    vals = [float(x) for x in parts[-4:]]
                except ValueError:
                    continue
                name = ' '.join(parts[:-4]) if len(parts) > 5 else parts[0]
                out[alias.get(name, name)] = vals[3]
    return out


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


def area_of(yosys: str, liberty: str, netlist: Path, top: str, extra_libs=(), read_only=()) -> float:
    """Chip area from Yosys stat over the standard-cell liberties; `read_only`
    liberties (hard macros) are loaded so the netlist links but not counted,
    matching the candidates' area from synthesis."""
    libs = ' '.join(f'read_liberty -lib {l};' for l in [liberty, *extra_libs, *read_only])
    stat_libs = ' '.join(f'-liberty {l}' for l in [liberty, *extra_libs])
    r = subprocess.run([yosys, '-p', f'{libs} read_verilog {netlist}; hierarchy -top {top}; stat {stat_libs}'],
                       capture_output=True, text=True)
    m = re.search(r'Chip area for (?:top )?module.*?:\s*([0-9.]+)', r.stdout)
    return float(m.group(1)) if m else 0.0


def pick_moves(paths: list[PathInfo], types: dict[str, str], fam: LibCells,
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
            if not flops_too and cur in fam and fam.cells[cur].is_ff:
                continue
            # with several standard-cell libraries loaded, critical paths use fast
            # cells only: the same cell in the fastest library (a Vt swap, no area
            # change) comes before a bigger drive
            nxt = fam.fastest_variant(cur) or next_size(cur, fam)
            if nxt is None:
                continue
            moves[s.inst] = nxt
            n += 1
            if n >= per_path:
                break
    return moves


def resize(netlist: Path, top: str, liberty: str, sta_liberty: str, period_ps: int, clock_port: str,
           out_dir: Path, *, sdc=None, iters=10, margin_ps=0, yosys='yosys', opensta='sta',
           driving_cell=None, load_ff=17.65, unc_setup_ps=250, unc_hold_ps=100,
           wire_load_model='auto', io_delay_frac=0.2, io_delay_min_frac=0.4, clock_port_2=None, period_ps_2=None,
           per_path=1, max_paths=200, wns_tol=0.15, wns_repair_iters=8, final='tns',
           recover_area=False, recover_rounds=6,
           repair_design=False, max_fanout=8, buffer_iters=6,
           repair_hold=False, lib_fast=None, hold_iters=10, hold_max_paths=None, hold_sta_budget=60,
           extra_libs=(), extra_libs_fast=None, macro_libs=(), macro_libs_fast=None,
           dont_use=(), buffer_cell=None, delay_cell=None,
           budget_section='', hook_section='', guard=None, time_budget_s=None, sta_session=False,
           sta_session_verify=False, recover_objective='area', power_activity_tcl='',
           repair_drc=False, drc_iters=4, log=print) -> dict:
    global USE_SESSION, SESSION_LOG, VERIFY_SESSION
    USE_SESSION, SESSION_LOG, VERIFY_SESSION = bool(sta_session), log, bool(sta_session_verify)
    PENDING['base'], PENDING['ops'] = None, None
    try:
        return _resize(netlist, top, liberty, sta_liberty, period_ps, clock_port, out_dir, sdc=sdc, iters=iters,
                       margin_ps=margin_ps, yosys=yosys, opensta=opensta, driving_cell=driving_cell, load_ff=load_ff,
                       unc_setup_ps=unc_setup_ps, unc_hold_ps=unc_hold_ps, wire_load_model=wire_load_model,
                       io_delay_frac=io_delay_frac, io_delay_min_frac=io_delay_min_frac, clock_port_2=clock_port_2,
                       period_ps_2=period_ps_2, per_path=per_path, max_paths=max_paths, wns_tol=wns_tol,
                       wns_repair_iters=wns_repair_iters, final=final, recover_area=recover_area,
                       recover_rounds=recover_rounds, repair_design=repair_design, max_fanout=max_fanout,
                       buffer_iters=buffer_iters, repair_hold=repair_hold, lib_fast=lib_fast, hold_iters=hold_iters,
                       hold_max_paths=hold_max_paths, hold_sta_budget=hold_sta_budget, extra_libs=extra_libs,
                       extra_libs_fast=extra_libs_fast, macro_libs=macro_libs, macro_libs_fast=macro_libs_fast,
                       dont_use=dont_use, buffer_cell=buffer_cell, delay_cell=delay_cell, budget_section=budget_section,
                       hook_section=hook_section, guard=guard, time_budget_s=time_budget_s,
                       recover_objective=recover_objective, power_activity_tcl=power_activity_tcl,
                       repair_drc=repair_drc, drc_iters=drc_iters, log=log)
    finally:
        if SESSIONS:
            st = close_sessions()
            log(f"sta sessions: {st['sessions']} process(es), {st['relinks']} relinks, {st['incremental']} incremental trials, "
                f"{st['commands']} commands, {st['seconds']:.1f}s"
                + (f"; verify: {STA_STATS.get('verify', 0)} compared, {STA_STATS.get('verify_mismatch', 0)} mismatches" if VERIFY_SESSION else ''))
        USE_SESSION = VERIFY_SESSION = False


def _resize(netlist: Path, top: str, liberty: str, sta_liberty: str, period_ps: int, clock_port: str,
            out_dir: Path, *, sdc=None, iters=10, margin_ps=0, yosys='yosys', opensta='sta',
            driving_cell=None, load_ff=17.65, unc_setup_ps=250, unc_hold_ps=100,
            wire_load_model='auto', io_delay_frac=0.2, io_delay_min_frac=0.4, clock_port_2=None, period_ps_2=None,
            per_path=1, max_paths=200, wns_tol=0.15, wns_repair_iters=8, final='tns',
            recover_area=False, recover_rounds=6,
            repair_design=False, max_fanout=8, buffer_iters=6,
            repair_hold=False, lib_fast=None, hold_iters=10, hold_max_paths=None, hold_sta_budget=60,
            extra_libs=(), extra_libs_fast=None, macro_libs=(), macro_libs_fast=None,
            dont_use=(), buffer_cell=None, delay_cell=None,
            budget_section='', hook_section='', guard=None, time_budget_s=None,
            recover_objective='area', power_activity_tcl='', repair_drc=False, drc_iters=4, log=print) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    want_power = recover_objective == 'power'
    constraints = sf._sta_constraints(
        clock_port=clock_port, period_ns=period_ps / 1000.0, clock_port_2=clock_port_2,
        period_2_ns=(period_ps_2 / 1000.0) if (clock_port_2 and period_ps_2) else None,
        unc_setup_ns=unc_setup_ps / 1000.0, unc_hold_ns=unc_hold_ps / 1000.0, user_sdc=sdc,
        driving_cell=driving_cell, load_pf=load_ff / 1000.0,
        wire_load_section=sf._wire_load_section(wire_load_model, sta_liberty), io_delay_frac=io_delay_frac,
        io_delay_min_frac=io_delay_min_frac, budget_section=budget_section, hook_section=hook_section)
    if want_power and power_activity_tcl:
        constraints += '\n# switching activity for report_power (recover_objective: power)\n' + power_activity_tcl.strip() + '\n'

    def _guarded(ok: bool, path) -> bool:
        """Acceptance rule across scenarios: a move that passes the ranking
        scenario must not break a required scenario/corner check that passed
        on the input netlist (`guard` runs those checks)."""
        if not ok or guard is None:
            return ok
        g_ok, detail = guard(path)
        if not g_ok:
            log(f'{Path(path).name}: rejected by the scenario guard: {detail}')
        return g_ok
    # Catalogue over the synthesis liberty plus hard-macro libraries, so macro
    # pins get directions (netlist model) and STA gets their timing arcs.
    # extra_libs: further standard-cell libraries (counted in area); macro_libs:
    # hard-macro liberties (timing arcs + pin directions only, like the candidates' stat)
    std_extra = list(extra_libs or [])
    macro_libs = list(macro_libs or [])
    macro_libs_fast = list(macro_libs_fast if macro_libs_fast is not None else macro_libs)
    fam = drive_families([liberty, *std_extra, *macro_libs], dont_use)
    driving_cell = driving_cell or fam.default_driving_cell()
    extra_libs = std_extra + macro_libs
    extra_libs_fast = list(extra_libs_fast if extra_libs_fast is not None else std_extra) + macro_libs_fast
    margin = margin_ps / 1000.0

    cur = out_dir / 'it0.v'
    shutil.copy(netlist, cur)
    sf._strip_signed_decls(cur)
    text = cur.read_text()
    types = instance_types(text)
    sta = run_sta(opensta, sta_liberty, cur, top, constraints, out_dir, 'it0', k=max_paths, slack_max=margin, extra_libs=extra_libs, power=want_power)
    power0 = _power_w(sta)
    if want_power:
        log(f'power objective: start {power0 * 1e6:.1f} uW (total, report_power)' if power0 is not None else 'power objective: report_power gave no total; falling back to area')
        if power0 is None:
            want_power = False
    if not sta.ok:
        raise RuntimeError(f'OpenSTA failed, see {sta.log}')
    area0 = area_of(yosys, liberty, cur, top, std_extra, read_only=macro_libs)
    # runtime accounting: OpenSTA calls / seconds per phase, and an optional
    # wall-clock budget after which the remaining phases stop (status 'ok (time budget)')
    t_start = time.time()
    marks: list[tuple[str, int, float, float]] = [('start', STA_STATS['calls'], STA_STATS['seconds'], t_start)]

    def _mark(name):
        marks.append((name, STA_STATS['calls'], STA_STATS['seconds'], time.time()))

    budget_stop = {'hit': False}

    def _over_budget() -> bool:
        if time_budget_s is not None and time.time() - t_start > time_budget_s:
            if not budget_stop['hit']:
                log(f'time budget of {time_budget_s}s spent after {time.time() - t_start:.0f}s; stopping the remaining phases')
            budget_stop['hit'] = True
            return True
        return False
    leak0, mix0 = fam.leakage_total(types), fam.lib_mix(types)
    if fam.is_multi_lib():
        log(f'libraries by speed: ' + ', '.join(Path(l).stem for l, _ in sorted(fam.lib_speed.items(), key=lambda x: x[1]))
            + f'; start mix {mix0}')
    log(f'start: WNS={sta.wns:+.3f} TNS={sta.tns:+.2f} area={area0:.1f} failing paths={len(sta.paths)} '
        f'(margin {margin} ns)')
    steps: list[Step] = []
    tried: set[str] = set()
    total_moves: dict[str, str] = {}
    start_wns, start_tns = sta.wns, sta.tns
    delay_cells = 0
    states: list[tuple] = [(cur, sta.wns, sta.tns, {}, {'buffers': 0, 'delay': 0})]   # accepted (netlist, wns, tns, moves, counters)
    out_pins = liberty_output_pins(fam)
    bus_ranges = fam.bus_ranges()
    buffers_inserted = 0
    it = 0

    def evaluate_text(new_text: str, tag: str, ops=None):
        new = out_dir / f'{tag}.v'
        new.write_text(new_text)
        probs = structural_problems(new_text, fam)
        if probs:
            log(f'{tag}: rejected before STA, netlist check failed: ' + '; '.join(probs))
            return new, StaOut(ok=False, log='structural: ' + '; '.join(probs))
        _trial_begin(cur, ops)
        r = run_sta(opensta, sta_liberty, new, top, constraints, out_dir, tag, k=max_paths, slack_max=margin, extra_libs=extra_libs)
        return new, r

    phase_status: dict[str, str] = {}

    def _f(x, fmt='+.3f'):
        return format(x, fmt) if isinstance(x, (int, float)) else 'n/a'

    def accept_tns(old: StaOut, new: StaOut) -> bool:
        if not new.ok or new.tns is None or old.tns is None:
            return False
        if new.tns > old.tns + 1e-6 and new.wns >= old.wns - wns_tol:
            return True
        return abs(new.tns - old.tns) < 1e-6 and new.wns > old.wns + 1e-6

    # Phase 0 (optional): repair_design. Split high-fanout nets on failing paths
    # with buffer trees (groups of <= max_fanout sinks), one net per failing
    # path per round, batch accepted on TNS, bisected on rejection.
    try:
        if repair_design and sta.paths:
            if buffer_cell and buffer_cell in fam:
                c = fam.cells[buffer_cell]
                buf_cell, buf_in, buf_out = buffer_cell, c.inputs[0], c.outputs[0]
            else:
                buf_cell, buf_in, buf_out = fam.buffer_cell()
            buffered: set[str] = set()
            for rnd in range(buffer_iters):
                if not sta.paths or _over_budget():
                    break
                nl = Netlist(text, out_pins, bus_ranges)
                cands: list[tuple[float, str]] = []
                for pth in sta.paths:
                    best = None
                    for st in pth.stages:
                        if st.fanout is None or st.fanout <= max_fanout or st.inst not in nl.inst:   # split only nets with > max_fanout sinks
                            continue
                        net = nl.pin_net(st.inst, st.pin)
                        if not net or re.match(r"\d+'", net) or net in buffered or net in [c[1] for c in cands]:
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
                    nl = Netlist(text, out_pins, bus_ranges)
                    nb = sum(nl.buffer_tree(net, buf_cell, max_fanout, in_pin=buf_in, out_pin=buf_out) for net in batch)
                    if nb == 0:                       # STA fanout counted pins we do not split (ports etc.)
                        buffered |= set(batch)
                        break
                    it += 1
                    new, sta_new = evaluate_text(nl.render(), f'it{it}', ops=nl.ops)
                    ok = accept_tns(sta, sta_new)
                    ok = _guarded(ok, new)
                    _trial_end(ok, new)
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
                        states.append((cur, sta.wns, sta.tns, dict(total_moves), {'buffers': buffers_inserted, 'delay': delay_cells}))
                        accepted = True
                        break
                    if len(batch) == 1:
                        buffered.add(batch[0])
                        break
                    batch = batch[:len(batch) // 2]
                if not accepted and len(cands) <= 1:
                    break
        phase_status['repair_design'] = phase_status.get('repair_design', 'ok' if repair_design else 'skipped')
    except Exception as e:      # keep the last accepted netlist; never lose earlier gains
        phase_status['repair_design'] = f'failed: {e}'
        log(f'repair_design: phase aborted ({e}); continuing with the last accepted netlist')
    _mark('repair_design')
    def evaluate(moves: dict[str, str], tag: str, power: bool = False):
        new_text = retype(text, moves)
        new = out_dir / f'{tag}.v'
        new.write_text(new_text)
        _trial_begin(cur, [('replace_cell', i, c, types.get(i, '')) for i, c in moves.items()])
        r = run_sta(opensta, sta_liberty, new, top, constraints, out_dir, tag, k=max_paths, slack_max=margin, extra_libs=extra_libs, power=power)
        return new, new_text, r

    def _delta_area(moves: dict) -> float:
        return sum((fam.cells[c].area if c in fam else 0.0) - (fam.cells[types.get(i, '')].area if types.get(i, '') in fam else 0.0)
                   for i, c in moves.items())

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
    while it < iters and not _over_budget():
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
            ok = _guarded(ok, new)
            _trial_end(ok, new)
            steps.append(Step(it=it, moves=sub, wns_before=sta.wns, tns_before=sta.tns,
                              wns_after=sta_new.wns, tns_after=sta_new.tns, accepted=ok))
            log(f'it{it}: {len(sub)} upsizes -> WNS {sta.wns:+.3f}->{sta_new.wns:+.3f} '
                f'TNS {sta.tns:+.2f}->{sta_new.tns:+.2f} {"ACCEPT" if ok else "reject"}')
            if ok:
                cur, text, sta = new, new_text, sta_new
                types.update(sub)
                total_moves.update(sub)
                states.append((cur, sta.wns, sta.tns, dict(total_moves), {'buffers': buffers_inserted, 'delay': delay_cells}))
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
        if _over_budget():
            break
        if not sta.paths or (start_wns is not None and sta.wns >= start_wns - 1e-6 and sta.wns >= 0):
            break
        worst = sorted(sta.paths, key=lambda pth: pth.slack)[:3]
        moves = pick_moves(worst, types, fam, tried | tried2, per_path=2)
        if not moves:
            break
        it += 1
        new, new_text, sta_new = evaluate(moves, f'it{it}')
        ok = sta_new.ok and sta_new.wns > sta.wns + 1e-6 and sta_new.tns >= sta.tns - 1e-6
        ok = _guarded(ok, new)
        _trial_end(ok, new)
        steps.append(Step(it=it, moves=moves, wns_before=sta.wns, tns_before=sta.tns,
                          wns_after=sta_new.wns, tns_after=sta_new.tns, accepted=ok))
        log(f'it{it} (wns repair): {len(moves)} upsizes -> WNS {sta.wns:+.3f}->{sta_new.wns:+.3f} '
            f'TNS {sta.tns:+.2f}->{sta_new.tns:+.2f} {"ACCEPT" if ok else "reject"}')
        tried2 |= set(moves)
        if ok:
            cur, text, sta = new, new_text, sta_new
            types.update(moves)
            total_moves.update(moves)
            states.append((cur, sta.wns, sta.tns, dict(total_moves), {'buffers': buffers_inserted, 'delay': delay_cells}))
    _mark('sizing')
    # Phase 3 (optional): area recovery. Downsize cells that are not on any
    # path within `guard` of the margin, in batches bisected on rejection.
    # Accepted only while WNS stays >= min(start WNS, margin) and TNS does not
    # drop, so timing never pays for area.
    try:
        if recover_area:
            sens_band = 0.3          # ns; not `guard`: that name is the scenario-guard callable
            floor_wns = min(sta.wns, margin)
            tried3: set[str] = set()
            power_cur = power0
            for rnd in range(recover_rounds):
                if _over_budget():
                    break
                near = run_sta(opensta, sta_liberty, cur, top, constraints, out_dir, f'near{rnd}', k=2000,
                               slack_max=margin + sens_band, extra_libs=extra_libs)
                protected = {st.inst for pth in near.paths for st in pth.stages}
                # off-critical cells: same cell in a slower (lower-leakage) library first, else one drive down
                cands = {i: (fam.slower_variant(t) or prev_size(t, fam)) for i, t in types.items()
                         if i not in protected and i not in tried3 and (fam.slower_variant(t) or prev_size(t, fam))}
                if not cands:
                    log(f'{recover_objective} recovery: no downsizing/swap candidates; done')
                    break
                if want_power:
                    # biggest consumers first: the batch is bisected from the front on rejection
                    ipw = instance_power(opensta, sta_liberty, cur, top, constraints, out_dir, f'ipower{rnd}', cands, extra_libs)
                    batch = sorted(cands, key=lambda i: -ipw.get(i, 0.0))
                else:
                    batch = sorted(cands)
                done_round = False
                while batch and it < iters + recover_rounds * 8:
                    it += 1
                    sub = {i: cands[i] for i in batch}
                    new, new_text, sta_new = evaluate(sub, f'it{it}', power=want_power)
                    ok = (sta_new.ok and sta_new.wns >= floor_wns - 1e-6 and sta_new.tns >= sta.tns - 1e-6)
                    pw_new = _power_w(sta_new)
                    if want_power:
                        # the objective is power: it must drop, and area must not grow
                        ok = ok and pw_new is not None and power_cur is not None and pw_new < power_cur * (1 - 1e-6) and _delta_area(sub) <= 1e-9
                    ok = _guarded(ok, new)
                    _trial_end(ok, new)
                    steps.append(Step(it=it, moves=sub, wns_before=sta.wns, tns_before=sta.tns,
                                      wns_after=sta_new.wns, tns_after=sta_new.tns, accepted=ok))
                    log(f'it{it} ({recover_objective}): {len(sub)} downsizes/swaps -> WNS {sta.wns:+.3f}->{sta_new.wns:+.3f} '
                        f'TNS {sta.tns:+.2f}->{sta_new.tns:+.2f}'
                        + (f' power {power_cur * 1e6:.1f}->{pw_new * 1e6:.1f} uW' if want_power and pw_new is not None and power_cur is not None else '')
                        + f' {"ACCEPT" if ok else "reject"}')
                    if ok:
                        cur, text, sta = new, new_text, sta_new
                        if want_power:
                            power_cur = pw_new
                        types.update(sub)
                        total_moves.update(sub)
                        states.append((cur, sta.wns, sta.tns, dict(total_moves), {'buffers': buffers_inserted, 'delay': delay_cells}))
                        done_round = True
                        break
                    if len(batch) == 1:
                        tried3.add(batch[0])
                        break
                    batch = batch[:len(batch) // 2]
                if not done_round and len(cands) <= 1:
                    break
        phase_status['recover_area'] = phase_status.get('recover_area', 'ok' if recover_area else 'skipped')
    except Exception as e:      # keep the last accepted netlist; never lose earlier gains
        phase_status['recover_area'] = f'failed: {e}'
        log(f'recover_area: phase aborted ({e}); continuing with the last accepted netlist')

    _mark('recover_area')
    # Phase 3b (optional): repair_drc. Electrical checks from the liberty / SDC
    # limits (max transition, max capacitance, max fanout): the violating net's
    # driver is upsized when it can be, else the net is split into buffer trees
    # sized from the limit/actual ratio. A batch is kept when the summed DRC
    # slack improves and setup WNS/TNS stay within the hold-phase tolerance.
    drc_before = drc_after = None
    drc_buffers = 0
    if repair_drc:
      try:
        drc = run_drc(opensta, sta_liberty, cur, top, constraints, out_dir, 'drc0', extra_libs)
        drc_before = drc_summary(drc)
        log(f"drc: slew {drc_before['max_slew']}, cap {drc_before['max_capacitance']}, fanout {drc_before['max_fanout']} violations (sum slack {drc_before['total_slack']})")
        if buffer_cell and buffer_cell in fam:
            c = fam.cells[buffer_cell]
            dbuf, dbuf_in, dbuf_out = buffer_cell, c.inputs[0], c.outputs[0]
        else:
            dbuf, dbuf_in, dbuf_out = fam.buffer_cell()
        floor_d = min(sta.wns, margin)
        tried_drc: set[str] = set()
        for rnd in range(drc_iters):
            if drc_before is None or sum(drc_summary(drc)[k] for k in DRC_KINDS) == 0 or _over_budget():
                break
            nl0 = Netlist(text, out_pins, bus_ranges)
            in_ports = set(re.findall(r'\binput\s+(?:(?:wire|reg)\s+)?(?:\[[^\]]+\]\s*)?(\\?[\w$\.\[\]]+)\s*;', text))
            # violations -> nets: {net: {'kinds', 'worst', 'ratio'}}
            nets: dict = {}
            for kind in DRC_KINDS:
                for pin, limit, actual, slack in drc[kind]:
                    if '/' in pin:
                        inst, p = pin.rsplit('/', 1)
                        if inst not in nl0.inst:
                            continue
                        net = nl0.pin_net(inst, p)
                    else:
                        net = pin if pin in in_ports else None
                    if not net or re.match(r"\d+'", net) or net in tried_drc:
                        continue
                    d = nets.setdefault(net, {'kinds': set(), 'worst': 0.0, 'ratio': 1.0, 'fanout_limit': None})
                    d['kinds'].add(kind)
                    d['worst'] = min(d['worst'], slack)
                    if limit > 0 and actual > limit:
                        d['ratio'] = min(d['ratio'], limit / actual)
                    if kind == 'max_fanout':
                        d['fanout_limit'] = int(limit) if d['fanout_limit'] is None else min(d['fanout_limit'], int(limit))
            if not nets:
                log('drc: no repairable net left; done')
                break
            order = sorted(nets, key=lambda n: nets[n]['worst'])
            batch = list(order)
            progressed = False
            while batch:
                nl = Netlist(text, out_pins, bus_ranges)
                moves: dict = {}
                nb = 0
                for net in batch:
                    d = nets[net]
                    drv = nl.driver(net)
                    up = fam.next_size(nl.inst[drv[0]]['type']) if drv else None
                    if drv and up and 'max_fanout' not in d['kinds'] and drv[0] not in moves:
                        moves[drv[0]] = up                               # a stronger driver fixes slew / cap
                    else:
                        n_sinks = len(nl.sinks(net))
                        if d['fanout_limit']:
                            group = max(2, d['fanout_limit'])                       # the SDC / liberty fanout limit
                        else:
                            group = max(2, min(max_fanout, int(n_sinks * d['ratio'])))   # load per buffer within the limit
                        nb += nl.buffer_tree(net, dbuf, group, in_pin=dbuf_in, out_pin=dbuf_out)
                if not moves and nb == 0:
                    tried_drc.update(batch)
                    break
                new_text = retype(nl.render(), moves)
                ops = list(nl.ops) + [('replace_cell', i, c, nl.inst[i]['type']) for i, c in moves.items()]
                it += 1
                new, sta_new = evaluate_text(new_text, f'it{it}', ops=ops)
                drc_new = run_drc(opensta, sta_liberty, new, top, constraints, out_dir, f'drc{it}', extra_libs) if sta_new.ok else drc
                s_old, s_new = drc_summary(drc), drc_summary(drc_new)
                ok = (sta_new.ok and s_new['total_slack'] > s_old['total_slack'] + 1e-6
                      and sta_new.wns >= floor_d - 1e-6 and sta_new.tns >= sta.tns - 0.05)
                ok = _guarded(ok, new)
                _trial_end(ok, new)
                steps.append(Step(it=it, moves={**{f'drc:{n}': 'buffer_tree' for n in batch if n not in moves}, **moves}, wns_before=sta.wns, tns_before=sta.tns,
                                  wns_after=sta_new.wns, tns_after=sta_new.tns, accepted=ok))
                log(f'it{it} (drc): {len(batch)} nets, {len(moves)} driver upsizes, {nb} buffers -> violations '
                    f"{sum(s_old[k] for k in DRC_KINDS)}->{sum(s_new[k] for k in DRC_KINDS)} (sum slack {s_old['total_slack']}->{s_new['total_slack']}); "
                    f'setup WNS {_f(sta.wns)}->{_f(sta_new.wns)} {"ACCEPT" if ok else "reject"}')
                if ok:
                    cur, text, sta, drc = new, new_text, sta_new, drc_new
                    types = instance_types(text)
                    total_moves.update(moves)
                    for n in batch:
                        if n not in moves:
                            total_moves[f'drc_buffer_tree:{n}'] = dbuf
                    drc_buffers += nb
                    buffers_inserted += nb
                    states.append((cur, sta.wns, sta.tns, dict(total_moves), {'buffers': buffers_inserted, 'delay': delay_cells}))
                    progressed = True
                    break
                if len(batch) == 1:
                    tried_drc.add(batch[0])
                    break
                batch = batch[:len(batch) // 2]
            if not progressed and all(n in tried_drc for n in nets):
                break
        drc_after = drc_summary(drc)
        log(f"drc: {sum(drc_before[k] for k in DRC_KINDS)} -> {sum(drc_after[k] for k in DRC_KINDS)} violations, {drc_buffers} buffers")
        phase_status['repair_drc'] = 'ok'
      except Exception as e:
        phase_status['repair_drc'] = f'failed: {e}'
        log(f'repair_drc: phase aborted ({e}); continuing with the last accepted netlist')
        log(traceback.format_exc())
    _mark('repair_drc')
    # Phase 4 (optional): repair_hold. Min-delay STA at the fast corner lists
    # failing hold endpoints; each gets one delay element in front of its
    # data pin (dlygate if the library has one, else buf_1). A round is kept
    # only if hold TNS improves and setup WNS at the slow corner stays where
    # it was (or above the margin). Repeats until hold is clean or the cap.
    hold_before = hold_after = None
    if repair_hold:
      try:
        hold_lib = lib_fast or sta_liberty
        if delay_cell and delay_cell in fam:
            c = fam.cells[delay_cell]
            dcell, dpin_in, dpin_out = delay_cell, c.inputs[0], c.outputs[0]
        else:
            dcell, dpin_in, dpin_out = fam.delay_cell()
        hk = hold_max_paths or max_paths
        sta_calls = 0
        hold = run_sta(opensta, hold_lib, cur, top, constraints, out_dir, 'hold0', k=hk, slack_max=0.0, mode='min', extra_libs=extra_libs_fast)
        sta_calls += 1
        hold_before = (hold.wns, hold.tns)
        if not hold.ok:
            raise RuntimeError('hold STA at the fast corner failed (see hold0.log)')
        log(f'hold @fast: WNS={_f(hold.wns)} TNS={_f(hold.tns, "+.2f")} failing={len(hold.paths)}; delay cell {dcell}; '
            f'budget {hold_sta_budget} STA calls, {hk} paths')
        setup_floor = min(sta.wns, margin)
        # Endpoints on setup paths within `guard` of the floor are setup-sensitive
        # (synchronizers, CDC bounds): a delay cell there is likely to be rejected,
        # so they are tried apart, after the feasible ones, one at a time.
        sens_band = 0.3          # ns; not `guard`: that name is the scenario-guard callable
        near = run_sta(opensta, sta_liberty, cur, top, constraints, out_dir, 'hold_near', k=2000,
                       slack_max=margin + sens_band, extra_libs=extra_libs)
        sta_calls += 1
        sensitive = {p.endpoint for p in near.paths} | {st.inst for p in near.paths for st in p.stages[-1:]}
        tried_hold: set[tuple] = set()

        def targets_of(paths, nl0):
            """(worst-first) delay targets of failing hold paths: ('pin', inst, pin) or ('port', name)."""
            out, skipped = [], {}
            for pth in sorted(paths, key=lambda p: p.slack):
                ep = pth.endpoint
                last = pth.stages[-1] if pth.stages else None
                if ep in nl0.inst:
                    inst, pin = ep, (last.pin if (last and last.inst == ep) else 'D')
                elif '/' in ep and ep.rsplit('/', 1)[0] in nl0.inst:
                    inst, pin = ep.rsplit('/', 1)
                elif ep in nl0.out_ports:
                    key = ('port', ep)
                    if key not in tried_hold and key not in out:
                        out.append(key)
                    continue
                else:
                    skipped[f'unknown endpoint {ep}'] = skipped.get(f'unknown endpoint {ep}', 0) + 1
                    continue
                kind = fam.pin_kind(nl0.inst[inst]['type'], pin)
                if kind not in ('data', 'input'):
                    k = f'{kind}:{nl0.inst[inst]["type"].split("__")[-1]}/{pin}'
                    skipped[k] = skipped.get(k, 0) + 1
                    continue
                key = ('pin', inst, pin)
                if key not in tried_hold and key not in out:
                    out.append(key)
            return out, skipped

        def try_batch(batch, rnd):
            """Insert one delay cell per target; accept on hold TNS gain with setup intact.
            Returns True/False (None when the budget is spent)."""
            nonlocal cur, text, hold, sta, types, delay_cells, it, sta_calls
            if sta_calls + 2 > hold_sta_budget or _over_budget():
                return None
            nl = Netlist(text, out_pins, bus_ranges)
            n = 0
            for t in batch:
                n += nl.delay_pin(t[1], t[2], dcell, 1, dpin_in, dpin_out) if t[0] == 'pin' else nl.delay_port(t[1], dcell, 1, dpin_in, dpin_out)
            if n == 0:
                tried_hold.update(batch)
                return False
            it += 1
            new = out_dir / f'it{it}.v'
            new_text = nl.render()
            probs = structural_problems(new_text, fam)
            if probs:
                log(f'it{it} (hold): rejected before STA, netlist check failed: ' + '; '.join(probs))
                return False
            new.write_text(new_text)
            _trial_begin(cur, nl.ops)
            hold_new = run_sta(opensta, hold_lib, new, top, constraints, out_dir, f'hold{it}', k=hk, slack_max=0.0, mode='min', extra_libs=extra_libs_fast)
            setup_new = run_sta(opensta, sta_liberty, new, top, constraints, out_dir, f'it{it}', k=max_paths, slack_max=margin, extra_libs=extra_libs)
            sta_calls += 2
            ok = (hold_new.ok and setup_new.ok and hold_new.tns is not None and hold_new.tns > hold.tns + 1e-6
                  and setup_new.wns >= setup_floor - 1e-6 and setup_new.tns >= sta.tns - 0.05)
            ok = _guarded(ok, new)
            _trial_end(ok, new)
            steps.append(Step(it=it, moves={('hold:' + (t[1] if t[0] == 'port' else f'{t[1]}/{t[2]}')): dcell for t in batch},
                              wns_before=hold.wns, tns_before=hold.tns, wns_after=hold_new.wns, tns_after=hold_new.tns, accepted=ok))
            log(f'it{it} (hold): {n} delay cells on {len(batch)} endpoints -> hold WNS {_f(hold.wns)}->{_f(hold_new.wns)} '
                f'TNS {_f(hold.tns, "+.2f")}->{_f(hold_new.tns, "+.2f")}; setup WNS {_f(sta.wns)}->{_f(setup_new.wns)} {"ACCEPT" if ok else "reject"}')
            if ok:
                cur, text, hold, sta = new, new_text, hold_new, setup_new
                types = instance_types(text)
                delay_cells += n
                total_moves[f'hold_round_{rnd}_{it}'] = f'{n} x {dcell} on {len(batch)} endpoints'
                states.append((cur, sta.wns, sta.tns, dict(total_moves), {'buffers': buffers_inserted, 'delay': delay_cells}))
            return ok

        budget_hit = False
        for rnd in range(hold_iters):
            if not hold.paths or not hold.ok or budget_hit:
                break
            nl0 = Netlist(text, out_pins, bus_ranges)
            targets, skipped = targets_of(hold.paths, nl0)
            if skipped:
                log('hold: no delay cell on ' + ', '.join(f'{k} x{v}' for k, v in sorted(skipped.items())[:6])
                    + ' (only data/enable pins are delayed)')
            if not targets:
                log('hold: no delayable endpoints left; done')
                break
            feasible = [t for t in targets if (t[1] if t[0] == 'pin' else t[1]) not in sensitive]
            blocked = [t for t in targets if t not in feasible]
            log(f'hold round {rnd + 1}: {len(targets)} endpoints ({len(feasible)} feasible, {len(blocked)} setup-sensitive)')
            # Worklist bisection: a rejected batch is split and BOTH halves are
            # retried, so every feasible endpoint gets its chance; the
            # setup-sensitive ones are tried individually at the end.
            work = ([feasible] if feasible else []) + [[t] for t in blocked]
            progressed = False
            while work:
                batch = work.pop(0)
                if not batch or all(t in tried_hold for t in batch):
                    continue
                batch = [t for t in batch if t not in tried_hold]
                res_b = try_batch(batch, rnd + 1)
                if res_b is None:
                    if not budget_stop['hit']:
                        log(f'hold: STA budget of {hold_sta_budget} calls spent; stopping')
                    budget_hit = True
                    break
                if res_b:
                    progressed = True
                    continue
                if len(batch) == 1:
                    tried_hold.add(batch[0])
                else:
                    h = len(batch) // 2
                    work[0:0] = [batch[:h], batch[h:]]
            if not progressed:
                log('hold: no batch accepted this round; done')
                break
        hold_after = (hold.wns, hold.tns)
        phase_status['repair_hold'] = 'ok' if not budget_hit else 'ok (budget)'
      except Exception as e:          # setup gains stay; report the hold failure explicitly
        phase_status['repair_hold'] = f'failed: {e}'
        log(f'repair_hold: phase aborted ({e}); continuing with the last accepted netlist')
        log(traceback.format_exc())

    _mark('repair_hold')
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
        w, t = pool[i][1], pool[i][2]
        met = w >= margin - 1e-6
        return (round(t, 4), 1 if met else 0, i if met else round(w, 4))
    best = pool[max(range(len(pool)), key=rank)]
    wns_regressed = best[1] < start_wns - 1e-6
    rolled_back = best[0] != cur
    if rolled_back:
        cur = best[0]
        total_moves = best[3]
        buffers_inserted, delay_cells = best[4]['buffers'], best[4]['delay']   # counters describe the delivered netlist
        text = cur.read_text()
        types = instance_types(text)
        sta = run_sta(opensta, sta_liberty, cur, top, constraints, out_dir, 'final', k=max_paths, slack_max=margin, extra_libs=extra_libs, power=want_power)
        log(f'final state rolled back to {cur.name}: WNS={_f(sta.wns)} TNS={_f(sta.tns, "+.2f")}')
        if repair_hold and hold_before is not None:
            # hold numbers must describe the delivered netlist, not the last trial
            hf = run_sta(opensta, lib_fast or sta_liberty, cur, top, constraints, out_dir, 'final_hold', k=max_paths,
                         slack_max=0.0, mode='min', extra_libs=extra_libs_fast)
            hold_after = (hf.wns, hf.tns) if hf.ok else None
    if wns_regressed:
        log(f'note: WNS regressed {start_wns:+.3f} -> {sta.wns:+.3f} for a TNS gain; --final wns forbids this')
    final = out_dir / 'resized.v'
    shutil.copy(cur, final)
    area = area_of(yosys, liberty, final, top, std_extra, read_only=macro_libs)
    log(f'end:   WNS={sta.wns:+.3f} TNS={sta.tns:+.2f} area={area:.1f} ({(area / area0 - 1) * 100:+.2f}%) '
        f'failing={len(sta.paths)} moves={len(total_moves)} -> {final}')
    leak1, mix1 = fam.leakage_total(types), fam.lib_mix(types)
    if leak0 is not None and leak1 is not None and (fam.is_multi_lib() or abs(leak1 - leak0) > 1e-9):
        log(f'leakage {leak0 / 1000:.2f} -> {leak1 / 1000:.2f} uW ({(leak1 / leak0 - 1) * 100 if leak0 else 0:+.1f}%); mix {mix1}')
    phase_status.setdefault('sizing', 'ok')
    phase_status.setdefault('repair_hold', 'skipped')
    phase_status.setdefault('repair_drc', 'skipped')
    power1 = _power_w(sta) if want_power else None
    if want_power and power1 is None:       # the delivered state was timed without power (e.g. a hold step): measure it
        power1 = _power_w(run_sta(opensta, sta_liberty, cur, top, constraints, out_dir, 'final_power', k=1, slack_max=margin, extra_libs=extra_libs, power=True))
    if want_power and power0 is not None and power1 is not None:
        log(f'power: {power0 * 1e6:.1f} -> {power1 * 1e6:.1f} uW ({(power1 / power0 - 1) * 100:+.1f}%) at the recovery activity')
    _mark('final')
    phases = {}
    for (n0, c0, s0_, t0_), (n1, c1, s1_, t1_) in zip(marks, marks[1:]):
        phases[n1] = {'sta_calls': c1 - c0, 'sta_seconds': round(s1_ - s0_, 1), 'seconds': round(t1_ - t0_, 1)}
    runtime = {'total_seconds': round(time.time() - t_start, 1), 'sta_calls': marks[-1][1] - marks[0][1],
               'sta_seconds': round(marks[-1][2] - marks[0][2], 1), 'time_budget_s': time_budget_s,
               'budget_hit': budget_stop['hit'], 'phases': phases}
    if budget_stop['hit']:
        for k in ('sizing', 'recover_area', 'repair_hold', 'repair_design'):
            if phase_status.get(k) == 'ok':
                phase_status[k] = 'ok (time budget)'
    log('runtime: ' + f"{runtime['total_seconds']}s, {runtime['sta_calls']} OpenSTA calls ({runtime['sta_seconds']}s); "
        + ', '.join(f"{k} {v['sta_calls']} calls/{v['seconds']}s" for k, v in phases.items() if v['sta_calls'] or v['seconds'] > 1))
    res = {'input': str(netlist), 'output': str(final), 'top': top, 'status': phase_status,
           'cells_start': len(instance_types(netlist.read_text())), 'cells_end': len(types),
           'start': {'wns_ns': start_wns, 'tns_ns': start_tns, 'area': area0,
                     'leakage_nw': leak0, 'lib_mix': mix0, 'power_w': power0},
           'end': {'wns_ns': sta.wns, 'tns_ns': sta.tns, 'area': area, 'failing': len(sta.paths), 'leakage_nw': leak1, 'lib_mix': mix1, 'power_w': power1},
           'recover_objective': recover_objective if recover_area else None,
           'drc_before': drc_before, 'drc_after': drc_after, 'drc_buffers': drc_buffers,
           'moves': total_moves, 'steps': [asdict(s) for s in steps], 'wns_regressed': wns_regressed,
           'buffers_inserted': buffers_inserted, 'delay_cells_inserted': delay_cells,
           'hold_before': hold_before, 'hold_after': hold_after, 'runtime': runtime,
           # phase status says whether a phase ran to completion; this says whether timing closed
           'timing': {'setup_met': bool(sta.wns is not None and sta.wns >= margin - 1e-6),
                      'hold_met': (None if hold_after is None or hold_after[0] is None else bool(hold_after[0] >= -1e-6)),
                      'rolled_back': rolled_back}}
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
    ap.add_argument('--repair-drc', action='store_true', help='fix max transition / capacitance / fanout violations (driver upsize, else buffer trees)')
    ap.add_argument('--drc-iters', type=int, default=4)
    ap.add_argument('--recover-power', action='store_true', help='recovery with total power (report_power, uniform activity) as the objective instead of area')
    ap.add_argument('--power-activity', type=float, default=0.1); ap.add_argument('--power-duty', type=float, default=0.5)
    ap.add_argument('--recover-area', action='store_true', help='after timing, downsize off-critical cells while WNS holds')
    ap.add_argument('--repair-design', action='store_true', help='split high-fanout nets on failing paths with buffer trees')
    ap.add_argument('--max-fanout', type=int, default=8)
    ap.add_argument('--repair-hold', action='store_true', help='insert delay cells on failing hold endpoints (fast corner) while setup holds')
    ap.add_argument('--lib-fast', help='fast-corner liberty for hold analysis (default: --lib-sta)')
    ap.add_argument('--sta-session-verify', action='store_true', help='with --sta-session: also time every trial in a fresh process and report mismatches')
    ap.add_argument('--sta-session', action='store_true', help='one persistent OpenSTA process per corner for the whole pass (liberties read once)')
    ap.add_argument('--time-budget-s', type=int, help='wall-clock budget for the whole post-pass; remaining phases stop when spent')
    ap.add_argument('--hold-max-paths', type=int, help='failing hold endpoints per STA (default: --max-paths)')
    ap.add_argument('--hold-sta-budget', type=int, default=60, help='max OpenSTA calls in the hold phase (default 60)')
    ap.add_argument('--macro-lib', action='append', default=[], help='hard-macro liberty: timing + pins, not counted in area (repeatable)')
    ap.add_argument('--extra-lib', action='append', default=[], help='hard-macro liberty (SRAM, PLL...), repeatable; read by STA and the netlist model')
    ap.add_argument('--dont-use', nargs='+', default=[], help='cell patterns never used by repairs')
    ap.add_argument('--buffer-cell', help='buffer cell for repair_design (default: second-weakest buffer in the liberty)')
    ap.add_argument('--delay-cell', help='delay cell for repair_hold (default: slowest buffer in the liberty)')
    ap.add_argument('--recover-rounds', type=int, default=6)
    ap.add_argument('--final', choices=['tns', 'wns'], default='tns',
                    help="final state: best TNS within --wns-tol of the start WNS (tns), or never regress WNS (wns)")
    ap.add_argument('--driving-cell', default=None, help='default: a mid-drive inverter from the liberty'); ap.add_argument('--load-ff', type=float, default=17.65)
    ap.add_argument('--wire-load-model', default='auto')
    ap.add_argument('--yosys', default='yosys'); ap.add_argument('--opensta', default='sta')
    ap.add_argument('--work-dir', default='work_resize'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    log = (lambda *x: None) if a.json else print
    drv = a.driving_cell or LibCells([a.lib, *a.extra_lib], dont_use=a.dont_use).default_driving_cell()
    res = resize(Path(a.netlist), a.top, a.lib, a.lib_sta or a.lib, a.period_ps, a.clock_port, Path(a.work_dir),
                 sdc=a.sdc, iters=a.iters, margin_ps=a.margin_ps, yosys=a.yosys, opensta=a.opensta,
                 driving_cell=drv, load_ff=a.load_ff, wire_load_model=a.wire_load_model,
                 clock_port_2=a.clock_port_2, period_ps_2=a.period_ps_2, per_path=a.per_path,
                 max_paths=a.max_paths, wns_tol=a.wns_tol, final=a.final,
                 repair_drc=a.repair_drc, drc_iters=a.drc_iters, recover_area=a.recover_area or a.recover_power, recover_rounds=a.recover_rounds, recover_objective='power' if a.recover_power else 'area', power_activity_tcl=f'set_power_activity -input -activity {a.power_activity} -duty {a.power_duty}',
                 repair_design=a.repair_design, max_fanout=a.max_fanout,
                 repair_hold=a.repair_hold, lib_fast=a.lib_fast,
                 hold_max_paths=a.hold_max_paths, hold_sta_budget=a.hold_sta_budget, time_budget_s=a.time_budget_s, sta_session=a.sta_session, sta_session_verify=a.sta_session_verify, extra_libs=a.extra_lib, macro_libs=a.macro_lib, dont_use=a.dont_use, buffer_cell=a.buffer_cell, delay_cell=a.delay_cell, log=log)
    if a.json:
        print(json.dumps(res, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
