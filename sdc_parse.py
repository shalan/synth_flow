#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
sdc_parse — read an SDC file into a constraints model for synthesis.

The SDC is executed by a real Tcl interpreter (`tclsh`) with stub procs for
the SDC vocabulary, so variables, `expr`, `foreach`, wildcards and object
queries all behave normally. Each command is recorded and interpreted here.

    from sdc_parse import parse_sdc
    c = parse_sdc('top.sdc', ports=['clk', 'rst_n', 'a[0]', 'a[1]', 'y'])
    c.clocks, c.input_delays, c.false_paths, ...

Command line (report what synthesis would use):

    python3 sdc_parse.py top.sdc [--netlist netlist.v --top NAME] [--json]

Three tiers of support (see docs/sdc-support.md):
  * SYNTH   — used to derive path groups, budgets and ABC constraints
  * STA     — recognised, left to OpenSTA (which sources the SDC verbatim)
  * UNKNOWN — anything else; passed through to OpenSTA with a warning
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# Command tiers
# --------------------------------------------------------------------------

SYNTH_COMMANDS = {
    'create_clock', 'create_generated_clock', 'set_clock_uncertainty',
    'set_clock_groups', 'set_input_delay', 'set_output_delay',
    'set_false_path', 'set_multicycle_path', 'set_max_delay',
    'set_driving_cell', 'set_load', 'set_max_fanout',
    'set_dont_use', 'set_dont_touch',
}
STA_ONLY_COMMANDS = {
    'set_clock_latency', 'set_input_transition', 'set_max_transition',
    'set_max_capacitance', 'set_min_delay', 'set_case_analysis',
    'set_propagated_clock', 'set_ideal_network', 'set_clock_transition',
    'set_disable_timing', 'set_units', 'set_operating_conditions',
    'current_design', 'set_wire_load_mode', 'set_wire_load_model',
    'set_timing_derate', 'set_min_pulse_width', 'set_max_area',
    'set_max_time_borrow', 'set_logic_one', 'set_logic_zero', 'set_logic_dc',
    'group_path', 'set_clock_gating_check', 'set_data_check',
}
# Object queries return tagged tokens "kind:name" so the interpreter here can
# tell a port from a clock from a pin after Tcl has flattened everything.
QUERY_PROCS = {
    'get_ports': 'port', 'get_clocks': 'clock', 'get_pins': 'pin',
    'get_cells': 'cell', 'get_nets': 'net', 'get_lib_cells': 'libcell',
    'get_libs': 'lib', 'get_designs': 'design',
}

SEP_ARG = '\x1f'
SEP_REC = '\x1e'

TCL_PRELUDE = r'''
set ::_sdc_out [open $::env(SDC_PARSE_OUT) w]
fconfigure $::_sdc_out -encoding utf-8
proc _rec {cmd args} {
    set parts [list $cmd]
    foreach a $args { lappend parts $a }
    puts -nonewline $::_sdc_out [join $parts "\x1f"]
    puts -nonewline $::_sdc_out "\x1e"
}
# Object queries -> tagged tokens. Patterns are resolved in Python.
proc _query {kind args} {
    set out {}
    foreach a $args {
        if {[string index $a 0] eq "-"} { continue }   ;# -quiet, -filter <expr>, -of_objects ...
        foreach n $a { lappend out "${kind}:${n}" }
    }
    return $out
}
foreach q {get_ports get_clocks get_pins get_cells get_nets get_lib_cells get_libs get_designs} {
    set kind [dict get {get_ports port get_clocks clock get_pins pin get_cells cell get_nets net get_lib_cells libcell get_libs lib get_designs design} $q]
    proc $q args [format {return [_query %s {*}$args]} $kind]
}
proc all_inputs args {
    foreach a $args { if {$a eq "-no_clocks"} { return [list "port:*ALL_INPUTS_NO_CLOCKS*"] } }
    return [list "port:*ALL_INPUTS*"]
}
proc all_outputs args   { return [list "port:*ALL_OUTPUTS*"] }
proc all_clocks args    { return [list "clock:*ALL_CLOCKS*"] }
proc all_registers args { return [list "cell:*ALL_REGISTERS*"] }
proc current_design args { _rec current_design {*}$args; return "design:top" }
proc get_property args  { return "" }
proc get_full_name args { return [lindex $args 0] }
# Recorders for every SDC command we know about.
foreach c {%SYNTH% %STA%} {
    if {$c eq "current_design"} continue
    proc $c args [format {_rec %s {*}$args} $c]
}
# Anything else: record as unknown (with its arguments) instead of failing.
proc unknown args { _rec __unknown__ {*}$args }
'''


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Clock:
    name: str
    period_ns: Optional[float] = None
    ports: list[str] = field(default_factory=list)
    generated: bool = False
    source: Optional[str] = None
    divide_by: Optional[int] = None
    multiply_by: Optional[int] = None
    uncertainty_setup_ns: Optional[float] = None
    uncertainty_hold_ns: Optional[float] = None


@dataclass
class IoDelay:
    ports: list[str]
    clock: Optional[str]
    max_ns: Optional[float] = None
    min_ns: Optional[float] = None
    add: bool = False


@dataclass
class PathException:
    kind: str                       # false_path | multicycle | max_delay | min_delay
    from_: list[str] = field(default_factory=list)   # tagged tokens
    to: list[str] = field(default_factory=list)
    through: list[str] = field(default_factory=list)
    value: Optional[float] = None   # multicycle N or delay ns
    setup: bool = True
    hold: bool = False


@dataclass
class Constraints:
    clocks: dict[str, Clock] = field(default_factory=dict)
    clock_groups: list[list[list[str]]] = field(default_factory=list)  # per statement: list of groups
    input_delays: list[IoDelay] = field(default_factory=list)
    output_delays: list[IoDelay] = field(default_factory=list)
    exceptions: list[PathException] = field(default_factory=list)
    driving_cells: list[dict] = field(default_factory=list)   # {ports, cell, pin}
    loads: list[dict] = field(default_factory=list)           # {ports, pf, min, max}
    max_fanout: Optional[float] = None
    dont_use: list[str] = field(default_factory=list)
    dont_touch: list[str] = field(default_factory=list)
    sta_only: list[str] = field(default_factory=list)         # command lines left to OpenSTA
    unknown: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ports: dict[str, str] = field(default_factory=dict)       # port -> direction, universe for expansion

    # ---- convenience -----------------------------------------------------
    def primary_clock(self) -> Optional[Clock]:
        real = [c for c in self.clocks.values() if not c.generated and c.ports]
        if not real:
            return None
        return min(real, key=lambda c: c.period_ns or float('inf'))

    def clocks_on_ports(self) -> dict[str, str]:
        """port -> clock name for every clock defined on a port."""
        return {p: c.name for c in self.clocks.values() for p in c.ports}

    def input_delay_for(self, port: str) -> Optional[IoDelay]:
        hit = None
        for d in self.input_delays:
            if port in d.ports:
                hit = d          # later definitions override earlier ones
        return hit

    def output_delay_for(self, port: str) -> Optional[IoDelay]:
        hit = None
        for d in self.output_delays:
            if port in d.ports:
                hit = d
        return hit

    def false_path_ports(self) -> set[str]:
        """Ports that start a false path (e.g. async resets)."""
        out = set()
        for e in self.exceptions:
            if e.kind == 'false_path' and not e.to and not e.through:
                for t in e.from_:
                    if t.startswith('port:'):
                        out.add(t[5:])
        return out

    def driving_cell_for(self, port: str) -> Optional[str]:
        hit = None
        for d in self.driving_cells:
            if port in d['ports']:
                hit = d['cell']
        return hit

    def load_for(self, port: str) -> Optional[float]:
        hit = None
        for d in self.loads:
            if port in d['ports'] and d.get('pf') is not None and not d.get('min'):
                hit = d['pf']
        return hit

    def to_json(self) -> str:
        d = asdict(self)
        d['clocks'] = {k: asdict(v) for k, v in self.clocks.items()}
        return json.dumps(d, indent=2)


# --------------------------------------------------------------------------
# Argument parsing helpers
# --------------------------------------------------------------------------

def _split_opts(args: list[str], value_opts: set[str], flag_opts: set[str]):
    """Return (opts: dict, positionals: list). Unknown '-x' tokens become flags."""
    opts, pos = {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith('-') and not _is_number(a):
            key = a.lstrip('-')
            if key in value_opts and i + 1 < len(args):
                opts[key] = args[i + 1]
                i += 2
                continue
            opts[key] = True
            i += 1
            continue
        pos.append(a)
        i += 1
    return opts, pos


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _tokens(s: str) -> list[str]:
    """Split a Tcl list-ish string into tokens; strip braces."""
    return [t for t in re.split(r'[\s{}]+', s.strip()) if t]


def _base(name: str) -> str:
    """haddr[3] / haddr[*] -> haddr. Yosys selections and path groups work on
    whole wires; OpenSTA sources the SDC itself, so bit granularity is kept there."""
    return re.sub(r'\[[^\]]*\]$', '', name)


def _expand_ports(tokens: list[str], ports: dict[str, str], clock_ports: set[str],
                  warnings: list[str]) -> list[str]:
    """Resolve tagged/bare tokens to concrete top-level port (wire) names.

    `ports` maps port name -> direction ('input' | 'output' | 'inout'); empty
    when unknown, in which case patterns are kept verbatim. Bare tokens are
    treated as port names (SDC allows `set_load 0.1 {a b}`)."""
    out: list[str] = []
    for t in tokens:
        kind, _, name = t.partition(':') if ':' in t else ('port', '', t)
        if kind != 'port':
            warnings.append(f'expected a port, got {t}')
            continue
        if name in ('*ALL_INPUTS*', '*ALL_INPUTS_NO_CLOCKS*'):
            if ports:
                out.extend(p for p, d in ports.items()
                           if d in ('input', 'inout')
                           and not (name.endswith('NO_CLOCKS*') and p in clock_ports))
            else:
                out.append(name)
            continue
        if name == '*ALL_OUTPUTS*':
            out.extend(p for p, d in ports.items() if d in ('output', 'inout')) if ports else out.append(name)
            continue
        if not ports:
            out.append(_base(name))
            continue
        pat = _base(name)
        if pat in ports:
            out.append(pat)
            continue
        hits = [p for p in ports if fnmatch.fnmatchcase(p, pat)]
        if hits:
            out.extend(hits)
        else:
            warnings.append(f"port pattern '{name}' matched nothing")
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def _clock_name(tokens: list[str], clocks: dict[str, Clock]) -> Optional[str]:
    for t in tokens:
        kind, _, name = t.partition(':') if ':' in t else ('clock', '', t)
        if kind == 'clock' and name != '*ALL_CLOCKS*':
            return name
        if kind == 'port':      # -clock [get_ports clk] also appears in the wild
            for c in clocks.values():
                if name in c.ports:
                    return c.name
    return None


# --------------------------------------------------------------------------
# Interpretation of recorded calls
# --------------------------------------------------------------------------

def _interpret(records: list[list[str]], ports: dict[str, str]) -> Constraints:
    c = Constraints(ports=dict(ports))
    W = c.warnings
    clock_ports: set[str] = set()

    for rec in records:
        cmd, args = rec[0], rec[1:]
        line = ' '.join([cmd] + args)

        if cmd == '__unknown__':
            c.unknown.append(' '.join(args))
            continue
        if cmd in STA_ONLY_COMMANDS:
            c.sta_only.append(line)
            continue

        # ---------------- clocks ----------------
        if cmd == 'create_clock':
            o, pos = _split_opts(args, {'period', 'name', 'waveform', 'comment'}, {'add'})
            src = _expand_ports(_tokens(pos[0]), ports, clock_ports, W) if pos else []
            name = o.get('name') or (src[0] if src else None)
            if not name:
                W.append(f'create_clock without name or port: {line}')
                continue
            period = float(o['period']) if 'period' in o else None
            c.clocks[name] = Clock(name=name, period_ns=period, ports=src)
            clock_ports.update(src)
            continue

        if cmd == 'create_generated_clock':
            o, pos = _split_opts(args, {'name', 'source', 'divide_by', 'multiply_by',
                                        'duty_cycle', 'edges', 'edge_shift', 'master_clock'},
                                 {'add', 'invert', 'combinational'})
            tgt = _expand_ports(_tokens(pos[0]), ports, clock_ports, W) if pos else []
            name = o.get('name') or (tgt[0] if tgt else None)
            if not name:
                W.append(f'create_generated_clock without name: {line}')
                continue
            src_tokens = _tokens(o.get('source', ''))
            src_clock = o.get('master_clock') or _clock_name(src_tokens, c.clocks)
            if src_clock is None and src_tokens:
                # -source given as a port: find the clock on it
                for sp in _expand_ports(src_tokens, ports, clock_ports, W):
                    for ck in c.clocks.values():
                        if sp in ck.ports:
                            src_clock = ck.name
            period = None
            master = c.clocks.get(src_clock) if src_clock else None
            div = int(o['divide_by']) if 'divide_by' in o else None
            mul = int(o['multiply_by']) if 'multiply_by' in o else None
            if master and master.period_ns:
                period = master.period_ns
                if div: period *= div
                if mul: period /= mul
            c.clocks[name] = Clock(name=name, period_ns=period, ports=tgt, generated=True,
                                   source=src_clock, divide_by=div, multiply_by=mul)
            clock_ports.update(tgt)
            continue

        if cmd == 'set_clock_uncertainty':
            o, pos = _split_opts(args, {'from', 'to', 'rise_from', 'fall_from', 'rise_to', 'fall_to'},
                                 {'setup', 'hold', 'rise', 'fall'})
            if not pos:
                W.append(f'set_clock_uncertainty without value: {line}')
                continue
            val = float(pos[0])
            targets = _tokens(pos[1]) if len(pos) > 1 else ['clock:*ALL_CLOCKS*']
            if 'from' in o or 'to' in o:
                c.sta_only.append(line)      # inter-clock uncertainty: STA only
                continue
            names = [n for n in (_clock_name([t], c.clocks) for t in targets) if n] \
                if 'clock:*ALL_CLOCKS*' not in targets else list(c.clocks)
            both = not o.get('setup') and not o.get('hold')
            for n in names:
                ck = c.clocks.get(n)
                if not ck:
                    W.append(f"set_clock_uncertainty on unknown clock '{n}'")
                    continue
                if o.get('setup') or both:
                    ck.uncertainty_setup_ns = val
                if o.get('hold') or both:
                    ck.uncertainty_hold_ns = val
            continue

        if cmd == 'set_clock_groups':
            groups: list[list[str]] = []
            i = 0
            while i < len(args):
                if args[i] == '-group' and i + 1 < len(args):
                    groups.append([n for n in (_clock_name([t], c.clocks) for t in _tokens(args[i + 1])) if n])
                    i += 2
                elif args[i] == '-name' and i + 1 < len(args):
                    i += 2
                else:
                    i += 1
            if groups:
                c.clock_groups.append(groups)
            continue

        # ---------------- I/O delays ----------------
        if cmd in ('set_input_delay', 'set_output_delay'):
            o, pos = _split_opts(args, {'clock', 'reference_pin'},
                                 {'max', 'min', 'rise', 'fall', 'add_delay', 'clock_fall',
                                  'level_sensitive', 'network_latency_included',
                                  'source_latency_included'})
            if len(pos) < 2:
                W.append(f'{cmd} needs a value and a port list: {line}')
                continue
            val = float(pos[0])
            plist = _expand_ports(_tokens(pos[1]), ports, clock_ports, W)
            clk = _clock_name(_tokens(o.get('clock', '')), c.clocks) if 'clock' in o else None
            both = not o.get('max') and not o.get('min')
            d = IoDelay(ports=plist, clock=clk,
                        max_ns=val if (o.get('max') or both) else None,
                        min_ns=val if (o.get('min') or both) else None,
                        add=bool(o.get('add_delay')))
            (c.input_delays if cmd == 'set_input_delay' else c.output_delays).append(d)
            continue

        # ---------------- exceptions ----------------
        if cmd in ('set_false_path', 'set_multicycle_path', 'set_max_delay', 'set_min_delay'):
            o, pos = _split_opts(args, {'from', 'to', 'through', 'rise_from', 'fall_from',
                                        'rise_to', 'fall_to', 'rise_through', 'fall_through',
                                        'start', 'end', 'comment'},
                                 {'setup', 'hold', 'rise', 'fall', 'reset_path',
                                  'ignore_clock_latency'})
            frm = _tokens(o.get('from', '') or o.get('rise_from', '') or o.get('fall_from', ''))
            to = _tokens(o.get('to', '') or o.get('rise_to', '') or o.get('fall_to', ''))
            thr = _tokens(o.get('through', '') or o.get('rise_through', '') or o.get('fall_through', ''))
            frm = _resolve_tagged(frm, ports, clock_ports, W)
            to = _resolve_tagged(to, ports, clock_ports, W)
            kind = {'set_false_path': 'false_path', 'set_multicycle_path': 'multicycle',
                    'set_max_delay': 'max_delay', 'set_min_delay': 'min_delay'}[cmd]
            val = float(pos[0]) if pos else None
            if kind == 'min_delay':
                c.sta_only.append(line)
                continue
            e = PathException(kind=kind, from_=frm, to=to, through=thr, value=val,
                              setup=not o.get('hold'), hold=bool(o.get('hold')))
            if kind == 'multicycle' and o.get('hold') and not o.get('setup'):
                c.sta_only.append(line)          # hold multicycle: STA only
                continue
            c.exceptions.append(e)
            continue

        # ---------------- boundary conditions ----------------
        if cmd == 'set_driving_cell':
            o, pos = _split_opts(args, {'lib_cell', 'pin', 'from_pin', 'library',
                                        'input_transition_rise', 'input_transition_fall'},
                                 {'rise', 'fall', 'min', 'max', 'dont_scale', 'no_design_rule'})
            plist = _expand_ports(_tokens(pos[0]), ports, clock_ports, W) if pos else []
            if 'lib_cell' not in o:
                W.append(f'set_driving_cell without -lib_cell: {line}')
                continue
            c.driving_cells.append({'ports': plist, 'cell': o['lib_cell'], 'pin': o.get('pin')})
            continue

        if cmd == 'set_load':
            o, pos = _split_opts(args, {}, {'min', 'max', 'pin_load', 'wire_load', 'subtract_pin_load'})
            if len(pos) < 2:
                W.append(f'set_load needs a value and a port list: {line}')
                continue
            plist = _expand_ports(_tokens(pos[1]), ports, clock_ports, W)
            c.loads.append({'ports': plist, 'pf': float(pos[0]),
                            'min': bool(o.get('min')), 'max': bool(o.get('max'))})
            continue

        if cmd == 'set_max_fanout':
            o, pos = _split_opts(args, {}, set())
            if pos:
                c.max_fanout = float(pos[0])
            continue

        if cmd == 'set_dont_use':
            for t in _tokens(' '.join(args)):
                c.dont_use.append(t.split(':', 1)[-1])
            continue

        if cmd == 'set_dont_touch':
            o, pos = _split_opts(args, {}, set())
            for t in _tokens(' '.join(pos)):
                c.dont_touch.append(t.split(':', 1)[-1])
            continue

        c.unknown.append(line)

    return c


def _resolve_tagged(tokens: list[str], ports: dict[str, str], clock_ports: set[str],
                    warnings: list[str]) -> list[str]:
    """Expand port patterns inside tagged tokens; keep other kinds as-is."""
    out = []
    for t in tokens:
        if t.startswith('port:'):
            out.extend(f'port:{p}' for p in _expand_ports([t], ports, clock_ports, warnings))
        elif ':' in t:
            out.append(t)
        else:
            out.append(f'port:{t}' if (not ports or t in ports) else f'clock:{t}')
    return out


# --------------------------------------------------------------------------
# Running tclsh
# --------------------------------------------------------------------------

def _prelude() -> str:
    return TCL_PRELUDE.replace('%SYNTH%', ' '.join(sorted(SYNTH_COMMANDS))) \
                      .replace('%STA%', ' '.join(sorted(STA_ONLY_COMMANDS)))


def parse_sdc(sdc_path: str | Path, ports: Optional[dict[str, str] | list[str]] = None,
              tclsh: str = 'tclsh') -> Constraints:
    """`ports`: {name: direction} (preferred) or a list of names (direction unknown)."""
    sdc_path = Path(sdc_path)
    if not sdc_path.exists():
        raise FileNotFoundError(sdc_path)
    exe = shutil.which(tclsh)
    if exe is None:
        raise RuntimeError(f"'{tclsh}' not found; a Tcl interpreter is required to read SDC files")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'calls.txt'
        script = Path(td) / 'run.tcl'
        script.write_text(_prelude() + f'\nsource {{{sdc_path.resolve()}}}\nclose $::_sdc_out\n')
        import os
        env = dict(os.environ, SDC_PARSE_OUT=str(out))
        r = subprocess.run([exe, str(script)], capture_output=True, text=True, env=env, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f'tclsh failed on {sdc_path}:\n{r.stderr.strip()}')
        raw = out.read_text(encoding='utf-8') if out.exists() else ''
    records = [rec.split(SEP_ARG) for rec in raw.split(SEP_REC) if rec]
    if isinstance(ports, list):
        ports = {p: 'input' for p in ports}   # unknown direction; all_inputs/all_outputs stay symbolic
    return _interpret(records, ports or {})


# --------------------------------------------------------------------------
# Port discovery from a Verilog netlist / RTL (top module only)
# --------------------------------------------------------------------------

_PORT_DECL_RE = re.compile(
    r'\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?:signed)?\s*'
    r'(?:\[\s*([^:\]]+)\s*:\s*([^\]]+)\s*\])?\s*([A-Za-z_][\w$]*(?:\s*,\s*[A-Za-z_][\w$]*)*)',
    re.S)


def ports_from_verilog(path: str | Path, top: str) -> dict[str, str]:
    """Port name -> direction for module `top` (base names, no bit indices),
    matching the wires Yosys selections and path groups operate on."""
    text = Path(path).read_text(errors='ignore')
    text = re.sub(r'//.*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    m = re.search(rf'\bmodule\s+{re.escape(top)}\b(.*?)\bendmodule\b', text, re.S)
    if not m:
        return {}
    ports: dict[str, str] = {}
    for mm in _PORT_DECL_RE.finditer(m.group(1)):
        for name in [n.strip() for n in mm.group(4).split(',')]:
            ports.setdefault(name, mm.group(1))
    return ports


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def report(c: Constraints) -> str:
    L = []
    L.append('Clocks:')
    for ck in c.clocks.values():
        kind = f'generated from {ck.source}' if ck.generated else 'primary'
        unc = f', uncertainty setup={ck.uncertainty_setup_ns} hold={ck.uncertainty_hold_ns}' \
            if ck.uncertainty_setup_ns is not None or ck.uncertainty_hold_ns is not None else ''
        L.append(f'  {ck.name:12} period={ck.period_ns} ns  ports={ck.ports}  ({kind}{unc})')
    if c.clock_groups:
        L.append(f'Clock groups (asynchronous/exclusive): {c.clock_groups}')
    L.append(f'Input delays:  {len(c.input_delays)} statements covering '
             f'{len({p for d in c.input_delays for p in d.ports})} ports')
    L.append(f'Output delays: {len(c.output_delays)} statements covering '
             f'{len({p for d in c.output_delays for p in d.ports})} ports')
    for e in c.exceptions:
        L.append(f'  {e.kind:11} from={e.from_} to={e.to} through={e.through} value={e.value}')
    if c.driving_cells:
        L.append(f'Driving cells: {[(d["cell"], len(d["ports"])) for d in c.driving_cells]}')
    if c.loads:
        L.append(f'Loads (pF): {[(d["pf"], len(d["ports"]), "min" if d["min"] else "max" if d["max"] else "") for d in c.loads]}')
    if c.max_fanout is not None:
        L.append(f'Max fanout: {c.max_fanout}')
    if c.dont_use:
        L.append(f'dont_use: {c.dont_use}')
    if c.dont_touch:
        L.append(f'dont_touch: {c.dont_touch}')
    if c.sta_only:
        L.append(f'STA-only ({len(c.sta_only)}):')
        L.extend(f'  {s}' for s in c.sta_only)
    if c.unknown:
        L.append(f'UNKNOWN, passed to OpenSTA only ({len(c.unknown)}):')
        L.extend(f'  {s}' for s in c.unknown)
    if c.warnings:
        L.append(f'Warnings ({len(c.warnings)}):')
        L.extend(f'  {w}' for w in c.warnings)
    return '\n'.join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('sdc')
    ap.add_argument('--netlist', help='Verilog file to take the port list from')
    ap.add_argument('--top', help='module name in --netlist')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    ports = ports_from_verilog(a.netlist, a.top) if a.netlist and a.top else None
    if a.netlist and a.top and not ports:
        print(f'warning: no ports found for module {a.top} in {a.netlist}', file=sys.stderr)
    c = parse_sdc(a.sdc, ports=ports)
    print(c.to_json() if a.json else report(c))
    return 0


if __name__ == '__main__':
    sys.exit(main())
