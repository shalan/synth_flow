#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
synth_flow.py — generic ASIC synthesis + STA + GLS orchestrator.

Sweeps multiple ABC recipes per RTL module, picks the winner per a chosen
objective (delay / area / fastest / pareto / balanced), runs multi-corner
OpenSTA (FF/TT/SS) on winners, and optionally runs gate-level simulation
with iverilog.  Supports flat and hierarchical (bottom-up) synthesis modes.

Usage:
    synth_flow.py --config synth.yaml
    synth_flow.py --rtl 'rtl/*.v' --lib lib.lib --top mycore --period-ps 8000
    synth_flow.py --config synth.yaml --list-modules
    synth_flow.py --config synth.yaml --no-gls --modules uart spi
    synth_flow.py --config synth.yaml --hierarchical --modules sub_a sub_b top

Exit codes:
    0  success (all winners meet timing, GLS passed if run)
    1  one or more modules failed synthesis
    2  setup violation at slow corner
    3  GLS failed
    4  configuration / input error
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# YAML is required for config files; YAML-less invocations work too (CLI-only).
try:
    import yaml
except ImportError:
    yaml = None

# ============================================================================
# Constants
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RECIPES_DIR = SCRIPT_DIR / 'recipes'

# Stability tiebreaker — lower index = preferred when QoR ties.
# Order: fastest-and-most-robust first, exotic/heavy recipes last.
RECIPE_PRIORITY = [
    'delay_retime',
    'delay_triple',
    'delay_choice_deep',
    'delay_iter_heavy',
    'delay_aggressive',
    'delay_choice_deep_v2',
    'delay_choice_deep_v3',
    'delay_choice_deep_v4',
    'delay_choice_deep_combined',
    'delay_choice_deep_bb',
    'balanced_resyn',
    'balanced_resyn2x',
    'balanced_struct',
    'area_safe',
    'area_classic',
    'area_lut6',
    'area_max',
    'lazy_man',
    'lms',
    'orfs_speed',
    'yosys_default',
]

EXIT_OK = 0
EXIT_SYNTH_FAIL = 1
EXIT_TIMING_FAIL = 2
EXIT_GLS_FAIL = 3
EXIT_CONFIG_ERR = 4

# ============================================================================
# Configuration
# ============================================================================

@dataclass
class Config:
    # --- required ---
    rtl_files: list[str] = field(default_factory=list)
    lib_typ: str = ''
    top: str = ''

    # --- STA corners (optional) ---
    lib_fast: Optional[str] = None
    lib_slow: Optional[str] = None

    # --- separate synthesis library (optional) ---
    # Resolution order for what Yosys / dfflibmap / ABC / stat use:
    #   1. lib_synth  (explicit override)
    #   2. lib_slow   (SS — default for robust worst-case synthesis)
    #   3. lib_typ    (TT — final fallback when no SS lib was provided)
    # Synthesising against SS by default makes ABC see worst-case cell
    # delays during mapping, so optimisation effort matches the corner
    # the design is signed off against. Set lib_synth: <path> to override
    # (e.g. lib_synth: hd_120_tt.lib for the older optimistic-synth flow).
    lib_synth: Optional[str] = None

    # --- user-supplied SDC (optional) ---
    # Path to an SDC file sourced by OpenSTA after create_clock and before
    # set_input_delay/set_output_delay. The SDC is the right place for
    # false_path / set_clock_groups / set_multicycle_path / set_max_delay
    # exceptions, and per-port input/output delay overrides.
    sdc: Optional[str] = None

    # --- hard macro liberty (optional) ---
    # Additional liberty files for hard macros (SRAM, PLL, ADC, …) loaded
    # alongside the standard cell library so Yosys recognizes the macro as
    # a blackbox cell and OpenSTA gets real timing arcs for paths through
    # the macro. Two YAML formats are accepted:
    #
    #   Format A (flat list, same file used in every STA corner):
    #     macro_libs:
    #       - path/to/sram_tt.lib
    #       - path/to/pll_tt.lib
    #
    #   Format B (per-corner, lets the slow corner use the ss model etc.):
    #     macro_libs:
    #       typ:  [path/to/sram_tt.lib]
    #       fast: [path/to/sram_ff.lib]
    #       slow: [path/to/sram_ss.lib]
    #
    # Internally always normalised to a dict with keys typ/fast/slow, each
    # mapping to a list[str]. Yosys uses `typ`; STA uses each corner.
    macro_libs: dict = field(default_factory=dict)

    # --- hierarchical synthesis (optional) ---
    cell_blackbox: Optional[str] = None

    # --- GLS (optional) ---
    tb_files: list[str] = field(default_factory=list)
    tb_top: Optional[str] = None
    primitives_dir: Optional[str] = None
    sdf_back_annotate: bool = True

    # --- design parameters ---
    period_ps: int = 10000
    clock_port: str = 'clk'
    clock_port_2: Optional[str] = None
    period_ps_2: Optional[int] = None
    objective: str = 'delay'  # delay | area | fastest | pareto | balanced
    modules: list[str] = field(default_factory=list)  # empty = auto-detect
    recipes: list[str] = field(default_factory=list)  # empty = all available
    params: dict = field(default_factory=dict)  # {module: {param: value}}
    verilog_defines: list[str] = field(default_factory=list)  # -D flags
    verilog_includes: list[str] = field(default_factory=list) # -I include dirs
    pre_read_files: list[str] = field(default_factory=list)  # DFFRAM netlists etc.
    keep_hierarchy_modules: list[str] = field(default_factory=list)  # preserve hierarchy

    # --- ABC constraints (Sky130 HD defaults) ---
    driving_cell: str = 'sky130_fd_sc_hd__inv_2'
    load_ff: float = 17.65

    # --- tool paths ---
    yosys: str = 'yosys'
    abc: str = 'abc'        # not directly invoked; yosys calls it
    opensta: str = 'sta'
    iverilog: str = 'iverilog'
    vvp: str = 'vvp'

    # --- execution ---
    parallel: int = 0  # 0 = use os.cpu_count()
    work_dir: str = 'work'
    results_dir: str = 'results'
    recipes_dir: str = str(DEFAULT_RECIPES_DIR)

    # --- behavior flags ---
    run_sta: bool = True
    run_gls: bool = True
    fail_on_timing: bool = True
    hierarchical: bool = False
    depth_only: bool = False
    depth_gate_delay_ps: float = 80.0  # per-gate delay estimate (sky130 HD ~80ps)

    # --- experimental ---
    # When True, ABC is invoked with -dff so it can perform sequential
    # optimization (retiming, sequential SAT redundancy removal). The flow
    # also reorders to: ABC -dff -> dfflibmap (so ABC sees generic flops).
    #
    # WARNING: Breaks 1:1 register correspondence with RTL. Formal LEC
    # against the original RTL will require retiming-aware comparison.
    # Can also misbehave with async resets, clock gating, and set/reset
    # semantics. Disabled by default; matches OpenLane behavior.
    abc_sequential: bool = False

    @classmethod
    def from_yaml(cls, path: Path) -> 'Config':
        if yaml is None:
            raise RuntimeError("PyYAML not installed; install with: pip install pyyaml")
        data = yaml.safe_load(path.read_text()) or {}
        # Expand ~ and env vars in path-like fields
        for key in ('lib_typ', 'lib_fast', 'lib_slow', 'lib_synth', 'sdc',
                    'primitives_dir', 'work_dir', 'results_dir', 'recipes_dir',
                    'cell_blackbox'):
            if key in data and isinstance(data[key], str):
                data[key] = os.path.expandvars(os.path.expanduser(data[key]))
        # Glob expansion for file lists
        for key in ('rtl_files', 'tb_files', 'pre_read_files'):
            if key in data:
                expanded = []
                for pat in data[key]:
                    pat = os.path.expandvars(os.path.expanduser(pat))
                    matches = sorted(glob.glob(pat))
                    expanded.extend(matches if matches else [pat])
                data[key] = expanded
        # Normalise macro_libs to {typ:[..], fast:[..], slow:[..]}
        if 'macro_libs' in data:
            data['macro_libs'] = _normalise_macro_libs(data['macro_libs'])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def merge_env(self) -> None:
        """Override tool paths from environment if set."""
        for var in ('YOSYS', 'ABC', 'OPENSTA', 'IVERILOG', 'VVP',
                    'LIB_TYP', 'LIB_FAST', 'LIB_SLOW'):
            val = os.environ.get(var)
            if val:
                setattr(self, var.lower(), val)

    def validate(self) -> list[str]:
        errs = []
        if not self.rtl_files:
            errs.append("rtl_files is empty")
        for f in self.rtl_files:
            if not Path(f).exists():
                errs.append(f"rtl file missing: {f}")
        if not self.lib_typ:
            errs.append("lib_typ is required")
        elif not Path(self.lib_typ).exists():
            errs.append(f"lib_typ missing: {self.lib_typ}")
        if not self.top:
            errs.append("top module is required")
        if self.run_sta:
            if not self.lib_fast or not Path(self.lib_fast).exists():
                errs.append(f"lib_fast missing or not set (required for STA)")
            if not self.lib_slow or not Path(self.lib_slow).exists():
                errs.append(f"lib_slow missing or not set (required for STA)")
        # macro_libs: every file must exist
        for corner in ('typ', 'fast', 'slow'):
            for f in self.macro_libs.get(corner, []):
                if not Path(f).exists():
                    errs.append(f"macro_libs[{corner}] missing: {f}")
        if self.run_gls:
            if not self.tb_files:
                errs.append("tb_files required for GLS")
            for f in self.tb_files:
                if not Path(f).exists():
                    errs.append(f"tb file missing: {f}")
        if self.objective not in ('delay', 'area', 'fastest', 'pareto', 'balanced'):
            errs.append(f"objective must be delay|area|fastest|pareto|balanced, got {self.objective}")
        if self.parallel < 0:
            errs.append("parallel must be >= 0")
        return errs

    def effective_parallel(self) -> int:
        return self.parallel if self.parallel > 0 else (os.cpu_count() or 1)


def _normalise_macro_libs(raw) -> dict:
    """Accept either a flat list or a per-corner dict; return a dict with
    keys typ/fast/slow each mapping to a list[str] of resolved paths.

    A flat list is mirrored to all three corners. A dict may omit corners;
    missing corners fall back to `typ` if present, else stay empty.
    """
    def _expand(items):
        out = []
        for p in items:
            p = os.path.expandvars(os.path.expanduser(p))
            hits = sorted(glob.glob(p))
            out.extend(hits if hits else [p])
        return out

    if raw is None:
        return {}
    if isinstance(raw, list):
        flat = _expand(raw)
        return {'typ': list(flat), 'fast': list(flat), 'slow': list(flat)}
    if isinstance(raw, dict):
        out = {k: _expand(raw.get(k, [])) for k in ('typ', 'fast', 'slow')}
        # Fill missing corners from typ
        if out['typ']:
            if not out['fast']:
                out['fast'] = list(out['typ'])
            if not out['slow']:
                out['slow'] = list(out['typ'])
        return out
    raise ValueError(f"macro_libs must be a list or a dict, got {type(raw).__name__}")


# ============================================================================
# Module discovery
# ============================================================================

class ModuleScanner:
    """Find non-instantiated (top-level) modules in a Verilog file set."""

    MOD_RE = re.compile(r'^\s*module\s+(\w+)', re.M)
    KEYWORDS = {
        'module', 'endmodule', 'input', 'output', 'inout', 'wire', 'reg',
        'logic', 'assign', 'always', 'always_ff', 'always_comb', 'always_latch',
        'if', 'else', 'case', 'casez', 'casex', 'endcase', 'begin', 'end',
        'for', 'while', 'do', 'parameter', 'localparam', 'genvar', 'generate',
        'endgenerate', 'function', 'endfunction', 'task', 'endtask',
        'initial', 'final', 'integer', 'real', 'time', 'realtime',
        'posedge', 'negedge', 'or', 'and', 'not', 'xor', 'nor', 'nand',
        'default', 'return', 'break', 'continue', 'fork', 'join',
        'typedef', 'struct', 'union', 'enum', 'package', 'endpackage',
        'import', 'export', 'localparam', 'specify', 'endspecify',
    }

    @classmethod
    def _strip(cls, src: str) -> str:
        src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        src = re.sub(r'//[^\n]*', '', src)
        return src

    @classmethod
    def scan(cls, files: list[str]) -> list[str]:
        all_src = ''
        for f in files:
            try:
                all_src += '\n' + Path(f).read_text(errors='ignore')
            except FileNotFoundError:
                continue
        all_src = cls._strip(all_src)

        defined = set(cls.MOD_RE.findall(all_src))
        # Find instantiations: <ident> <ident>(  where first ident is defined
        instantiated = set()
        pattern = re.compile(r'\b(\w+)\s+(?:#\s*\([^)]*\)\s*)?\w+\s*\(')
        for m in pattern.finditer(all_src):
            name = m.group(1)
            if name in defined and name not in cls.KEYWORDS:
                instantiated.add(name)

        tops = defined - instantiated
        if not tops:
            tops = defined  # fallback for single-module designs
        return sorted(tops)

    @classmethod
    def dependencies(cls, files: list[str]) -> dict[str, set[str]]:
        """For each module, return the set of *other defined modules* it
        instantiates.  Keys include all modules; values may be empty."""
        all_src = ''
        for f in files:
            try:
                all_src += '\n' + Path(f).read_text(errors='ignore')
            except FileNotFoundError:
                continue
        clean = cls._strip(all_src)
        defined = set(cls.MOD_RE.findall(clean))

        deps: dict[str, set[str]] = {m: set() for m in defined}
        mod_re = re.compile(r'^\s*module\s+(\w+)', re.M)
        inst_re = re.compile(r'\b(\w+)\s*#\s*\(')
        for chunk in re.split(r'endmodule', clean):
            mm = mod_re.search(chunk)
            if not mm:
                continue
            cur = mm.group(1)
            for im in inst_re.finditer(chunk):
                name = im.group(1)
                if name in defined and name != cur and name not in cls.KEYWORDS:
                    deps.setdefault(cur, set()).add(name)
        return deps


def _topo_sort(modules: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Return *modules* sorted so that dependencies come first (leaves → root)."""
    in_set = set(modules)
    visited: set[str] = set()
    order: list[str] = []

    def visit(m: str):
        if m in visited or m not in in_set:
            return
        visited.add(m)
        for d in deps.get(m, set()):
            visit(d)
        order.append(m)

    for m in modules:
        visit(m)
    return order

# ============================================================================
# Recipe sweep — one (module, recipe) job
# ============================================================================

@dataclass
class RecipeResult:
    module: str
    recipe: str
    success: bool
    runtime_s: float
    netlist: Optional[str] = None
    stats_json: Optional[str] = None
    log: Optional[str] = None
    cells: int = 0
    area: float = 0.0
    error: Optional[str] = None

# Standard flow: dfflibmap first, then ABC sees only combinational logic.
# This is what OpenLane does and what produces formally-verifiable netlists
# with 1:1 register correspondence to the RTL.
YOSYS_DRIVER_STD = """\
# generated yosys driver (standard combinational-ABC flow)
{pre_read_section}
{macro_lib_section}
{read_verilog_lines}
hierarchy -top {module} {param_flags}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc
write_verilog -noattr {syn_netlist}
dfflibmap -liberty {liberty}
abc -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps}
setundef -zero
splitnets
opt_clean -purge
tee -o {stats_json} stat -liberty {liberty} -json
write_verilog -noattr -noexpr {out_netlist}
"""

# Sequential flow: ABC -dff sees generic flops and can perform retiming
# and sequential redundancy removal. Map to liberty flops AFTER ABC.
#
# WARNING: This breaks 1:1 register correspondence and complicates LEC.
# It can also misbehave with async resets, clock gating, and complex
# flop semantics. Use only when you understand the implications.
YOSYS_DRIVER_SEQ = """\
# generated yosys driver (experimental: ABC sequential mode)
{pre_read_section}
{macro_lib_section}
{read_verilog_lines}
hierarchy -top {module} {param_flags}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc
write_verilog -noattr {syn_netlist}
# ABC with -dff: generic flops are part of the optimization
abc -dff -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps}
dfflibmap -liberty {liberty}
setundef -zero
splitnets
opt_clean -purge
tee -o {stats_json} stat -liberty {liberty} -json
write_verilog -noattr -noexpr {out_netlist}
"""

# Hierarchical bottom-up flow: read pre-synthesized sub-module netlists
# alongside the current module's RTL, then flatten and optimize across
# module boundaries.  Sub-module netlists are already technology-mapped;
# ABC can re-optimize across the flattened boundary for better QoR.
YOSYS_DRIVER_HIER = """\
# generated yosys driver (hierarchical bottom-up)
{cell_blackbox_line}
{pre_read_section}
{liberty_lib_section}
{macro_lib_section}
# Read pre-synthesized sub-module netlists
{read_netlist_lines}
# Read top-level RTL (parameters in netlists are already resolved)
{read_verilog_lines}
hierarchy -top {module} {param_flags}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc
write_verilog -noattr {syn_netlist}
dfflibmap -liberty {liberty}
abc -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps}
setundef -zero
splitnets
opt_clean -purge
tee -o {stats_json} stat -liberty {liberty} -json
write_verilog -noattr -noexpr {out_netlist}
"""

YOSYS_DRIVER_DEPTH = """\
# generated yosys driver (depth-only analysis)
{pre_read_section}
{macro_lib_section}
{read_verilog_lines}
hierarchy -top {module} {param_flags}
{keep_hierarchy_section}
proc; flatten; opt_expr; opt_clean
opt
techmap; opt
tee -o {stats_json} stat -json
ltp
"""

YOSYS_DRIVER_DUAL_CLK = """\
# generated yosys driver (dual-clock domain-partitioned ABC)
{pre_read_section}
{macro_lib_section}
{read_verilog_lines}
hierarchy -top {module} {param_flags}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc
write_verilog -noattr {syn_netlist}

# Partition into clock domains
select -set ffs_1 n:{clock_port} %co
select -set ffs_2 n:{clock_port_2} %co
select -set ffs @ffs_1 @ffs_2

select -set domain_1 @ffs_1 %x:+@ffs %d %xe*:+@ffs_1 @ffs_1
select -set domain_2 @ffs_2 %x:+@ffs %d %xe*:+@ffs_2 @ffs_2

# Inter-domain cells -> faster domain
select -set domain_2 @domain_2 @domain_1 %d

# ABC on domain 1 (fast clock)
abc -dff -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps} @domain_1

# ABC on domain 2 (slow clock)
abc -dff -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps_2} @domain_2

dfflibmap -liberty {liberty}
setundef -zero
splitnets
opt_clean -purge
tee -o {stats_json} stat -liberty {liberty} -json
write_verilog -noattr -noexpr {out_netlist}
"""

def _read_verilog_lines(rtl_files: list[str], verilog_defines: list[str] = None,
                        verilog_includes: list[str] = None) -> str:
    lines = []
    for f in rtl_files:
        cmd = "read_verilog -sv"
        if verilog_defines:
            for d in verilog_defines:
                cmd += f" -D{d}"
        if verilog_includes:
            for inc in verilog_includes:
                cmd += f" -I{inc}"
        cmd += f" {f}"
        lines.append(cmd)
    return '\n'.join(lines)

def _synth_lib(cfg) -> str:
    """Pick the liberty used for synthesis (Yosys / dfflibmap / ABC / stat).
    Order: explicit lib_synth > lib_slow (SS, robust default) > lib_typ.
    Accepts both Config dataclasses (attribute access) and plain dicts."""
    if hasattr(cfg, 'lib_typ'):
        return (cfg.lib_synth or cfg.lib_slow or cfg.lib_typ)
    return cfg.get('lib_synth') or cfg.get('lib_slow') or cfg.get('lib_typ')


def _pre_read_section(cfg: dict) -> str:
    lines = []
    if cfg.get('pre_read_files'):
        lines.append(f'read_liberty -lib {_synth_lib(cfg)}')
        for f in cfg['pre_read_files']:
            lines.append(f'read_verilog -sv {f}')
    return '\n'.join(lines)


def _liberty_lib_section(cfg: dict) -> str:
    """Load the standard cell library as a Yosys -lib (blackbox) library.
    Used by the hierarchical driver before reading pre-synthesised
    sub-module netlists, which reference std-cell names directly. Without
    this, Yosys aborts on the first std-cell reference."""
    lib = _synth_lib(cfg)
    return f'read_liberty -lib {lib}' if lib else ''


def _macro_lib_yosys_section(cfg: dict, corner: str = 'typ') -> str:
    """Emit `read_liberty -lib <macro.lib>` lines for Yosys so each macro
    is recognised as a blackbox cell during synth/dfflibmap. Empty if no
    macro liberty is configured."""
    libs = (cfg.get('macro_libs') or {}).get(corner, [])
    if not libs:
        return ''
    return '\n'.join(f'read_liberty -lib {f}' for f in libs)


def _macro_lib_sta_section(cfg, corner: str) -> str:
    """Emit `read_liberty -corner <corner> <macro.lib>` lines for OpenSTA
    so timing arcs through hard macros are honoured at this corner."""
    macro_libs = getattr(cfg, 'macro_libs', None) or (
        cfg.get('macro_libs') if isinstance(cfg, dict) else {}
    ) or {}
    libs = macro_libs.get(corner, [])
    if not libs:
        return ''
    return '\n'.join(f'read_liberty -corner {corner} {f}' for f in libs)


def _user_sdc_section(cfg) -> str:
    """Source a user-provided SDC file after create_clock has run. The SDC
    is where false_path / clock_groups / multicycle / per-port I/O delay
    exceptions belong. Empty if no `sdc:` is configured."""
    sdc = getattr(cfg, 'sdc', None) or (
        cfg.get('sdc') if isinstance(cfg, dict) else None
    )
    return f'source {sdc}' if sdc else ''


def _macro_lib_quick_sta_section(cfg) -> str:
    """Emit `read_liberty <macro.lib>` lines for the single-corner quick STA
    used in winner selection. Uses the typ corner."""
    macro_libs = getattr(cfg, 'macro_libs', None) or (
        cfg.get('macro_libs') if isinstance(cfg, dict) else {}
    ) or {}
    libs = macro_libs.get('typ', [])
    if not libs:
        return ''
    return '\n'.join(f'read_liberty {f}' for f in libs)

def _keep_hierarchy_section(cfg: dict) -> str:
    lines = []
    for m in cfg.get('keep_hierarchy_modules', []):
        lines.append(f'setattr -mod -set keep_hierarchy 1 {m}')
    return '\n'.join(lines)

def _param_flags(params: dict, module: str) -> str:
    """Generate Yosys -chparam flags for the given module."""
    if not params or module not in params:
        return ''
    flags = []
    for k, v in params[module].items():
        flags.append(f'-chparam {k} {v}')
    return ' '.join(flags)

def _read_netlist_lines(netlists: dict[str, str]) -> str:
    """Generate Yosys read_verilog -overwrite for pre-synthesized netlists."""
    lines = []
    for mod, path in netlists.items():
        lines.append(f"# pre-synthesized netlist: {mod}")
        lines.append(f"read_verilog -overwrite {path}")
    return '\n'.join(lines)

def _write_constraint_file(work_dir: Path, driving_cell: str, load_ff: float) -> Path:
    p = work_dir / 'abc.constr'
    p.write_text(f"set_driving_cell {driving_cell}\nset_load {load_ff}\n")
    return p

def _read_stats(stats_json: Path, module: str) -> tuple[int, float]:
    """Read cell count + area from yosys stats JSON. Robust to module
    name variations (\\name vs name)."""
    try:
        d = json.loads(stats_json.read_text())
        modules = d.get('modules', {})
        m = (modules.get('\\' + module) or
             modules.get(module) or
             (next(iter(modules.values())) if modules else None))
        if m is None:
            return 0, 0.0
        cells = sum(m.get('num_cells_by_type', {}).values())
        area = float(m.get('area', 0.0))
        return cells, area
    except Exception:
        return 0, 0.0

@dataclass
class DepthResult:
    module: str
    success: bool
    depth: int = 0
    cells: int = 0
    est_delay_ns: float = 0.0
    est_fmax_mhz: float = 0.0
    error: Optional[str] = None

def run_depth(args: dict) -> DepthResult:
    module = args['module']
    cfg = args['cfg']
    workdir = Path(args['workdir'])
    workdir.mkdir(parents=True, exist_ok=True)
    stats_json = workdir / 'depth.json'
    log_path = workdir / 'depth.log'
    yscript = workdir / 'depth.ys'

    yscript.write_text(YOSYS_DRIVER_DEPTH.format(
        pre_read_section=_pre_read_section(cfg),
        macro_lib_section=_macro_lib_yosys_section(cfg, 'typ'),
        keep_hierarchy_section=_keep_hierarchy_section(cfg),
        read_verilog_lines=_read_verilog_lines(cfg['rtl_files'], cfg.get('verilog_defines'), cfg.get('verilog_includes')),
        module=module,
        param_flags=_param_flags(cfg.get('params', {}), module),
        stats_json=stats_json,
    ))

    try:
        with open(log_path, 'w') as logf:
            r = subprocess.run(
                [cfg['yosys'], '-s', str(yscript)],
                stdout=logf, stderr=subprocess.STDOUT,
                timeout=120,
            )
        if r.returncode != 0:
            return DepthResult(module=module, success=False,
                               error=f'yosys exit {r.returncode}')

        depth = 0
        log_text = log_path.read_text(errors='ignore')
        for m in re.finditer(r'length\s*=\s*(\d+)', log_text):
            depth = max(depth, int(m.group(1)))

        cells, _ = _read_stats(stats_json, module)

        gate_ps = cfg.get('depth_gate_delay_ps', 80.0)
        est_ns = depth * gate_ps / 1000.0
        est_mhz = (1000.0 / est_ns) if est_ns > 0 else 0.0

        return DepthResult(
            module=module, success=True,
            depth=depth, cells=cells,
            est_delay_ns=est_ns, est_fmax_mhz=est_mhz,
        )
    except Exception as e:
        return DepthResult(module=module, success=False, error=str(e))

def _strip_param_overrides(rtl_path: str, dep_modules: set[str],
                           workdir: Path) -> str:
    """Create a copy of the RTL file with parameter overrides stripped from
    instantiations of the given dependency modules."""
    src = Path(rtl_path).read_text(errors='ignore')
    for mod in dep_modules:
        # Match: mod_name #( ... )  where ... may span multiple lines.
        # Use a non-greedy match that correctly handles nested parens at
        # one level (parameter lists don't nest deeper).
        src = re.sub(
            rf'\b{re.escape(mod)}\s*#\s*\((?:[^()]|\([^()]*\))*\)\s*',
            f'{mod} ',
            src, flags=re.S,
        )
    out = workdir / f'_hier_{Path(rtl_path).name}'
    out.write_text(src)
    return str(out)


def run_recipe(args: dict) -> RecipeResult:
    """Worker function — runs Yosys for one (module, recipe) pair.
    Must be top-level for multiprocessing pickling."""
    module = args['module']
    recipe = args['recipe']
    cfg = args['cfg']  # this is a dict (not Config) — Pool pickling
    workdir = Path(args['workdir'])
    constr = args['constr']
    recipe_path = args['recipe_path']
    dep_netlists = args.get('dep_netlists', {})
    dep_modules = args.get('dep_modules', set())

    workdir.mkdir(parents=True, exist_ok=True)
    netlist = workdir / f'{recipe}.v'
    syn_nl  = workdir / f'{recipe}.syn.v'
    stats   = workdir / f'{recipe}.json'
    log     = workdir / f'{recipe}.synth.log'
    yscript = workdir / f'{recipe}.ys'

    rtl_files = cfg['rtl_files']
    if dep_modules:
        dep_lower = {m.lower() for m in dep_modules}
        filtered = []
        for f in rtl_files:
            stem = Path(f).stem
            if stem.lower() in dep_lower:
                continue
            if stem.lower() == module.lower():
                filtered.append(_strip_param_overrides(f, dep_modules, workdir))
            else:
                filtered.append(f)
        rtl_files = filtered if filtered else cfg['rtl_files']
    if not rtl_files:
        rtl_files = cfg['rtl_files']

    if dep_netlists:
        template = YOSYS_DRIVER_HIER
    elif cfg.get('abc_sequential', False):
        template = YOSYS_DRIVER_SEQ
    elif cfg.get('clock_port_2') and cfg.get('dual_clock_synthesis', False):
        template = YOSYS_DRIVER_DUAL_CLK
    else:
        template = YOSYS_DRIVER_STD
    bb = cfg.get('cell_blackbox', '') or ''
    bb_line = f'# Read cell blackbox stubs\nread_verilog {bb}' if bb else ''
    yscript.write_text(template.format(
        pre_read_section=_pre_read_section(cfg),
        liberty_lib_section=_liberty_lib_section(cfg),
        macro_lib_section=_macro_lib_yosys_section(cfg, 'typ'),
        keep_hierarchy_section=_keep_hierarchy_section(cfg),
        read_verilog_lines=_read_verilog_lines(rtl_files, cfg.get('verilog_defines'), cfg.get('verilog_includes')),
        read_netlist_lines=_read_netlist_lines(dep_netlists),
        cell_blackbox=bb,
        cell_blackbox_line=bb_line,
        module=module,
        liberty=_synth_lib(cfg),
        constr=constr,
        recipe=recipe_path,
        period_ps=cfg['period_ps'],
        period_ps_2=cfg.get('period_ps_2', cfg['period_ps']),
        clock_port=cfg['clock_port'],
        clock_port_2=cfg.get('clock_port_2', ''),
        param_flags=_param_flags(cfg.get('params', {}), module),
        stats_json=stats,
        syn_netlist=syn_nl,
        out_netlist=netlist,
    ))

    start = time.time()
    try:
        with open(log, 'w') as logf:
            r = subprocess.run(
                [cfg['yosys'], '-s', str(yscript)],
                stdout=logf, stderr=subprocess.STDOUT,
                timeout=3600,
            )
        runtime = time.time() - start
        if r.returncode != 0:
            return RecipeResult(
                module=module, recipe=recipe, success=False,
                runtime_s=runtime, log=str(log),
                error=f"yosys exit {r.returncode}",
            )
        if not netlist.exists():
            return RecipeResult(
                module=module, recipe=recipe, success=False,
                runtime_s=runtime, log=str(log),
                error="netlist not produced",
            )
        cells, area = _read_stats(stats, module)
        return RecipeResult(
            module=module, recipe=recipe, success=True,
            runtime_s=runtime, netlist=str(netlist),
            stats_json=str(stats), log=str(log),
            cells=cells, area=area,
        )
    except subprocess.TimeoutExpired:
        return RecipeResult(
            module=module, recipe=recipe, success=False,
            runtime_s=time.time() - start, log=str(log),
            error="timeout",
        )
    except Exception as e:
        return RecipeResult(
            module=module, recipe=recipe, success=False,
            runtime_s=time.time() - start, log=str(log),
            error=str(e),
        )

# ============================================================================
# Quick STA for winner selection
# ============================================================================

QSTA_TCL = """\
read_liberty {liberty}
{macro_lib_section}
read_verilog {netlist}
link_design {module}
create_clock -name clk -period {period_ns} [get_ports {clock_port}]
{clock_2_section}
catch {{ set_false_path -from [get_ports hresetn] }}
catch {{ set_false_path -from [get_ports rst_n] }}
{user_sdc_section}
set_input_delay  -clock clk [expr {{{period_ns} * 0.2}}] [all_inputs]
set_output_delay -clock clk [expr {{{period_ns} * 0.2}}] [all_outputs]
report_wns
report_tns
exit
"""

QSTA_CLK2 = """\
create_clock -name clk2 -period {period_2_ns} [get_ports {clock_port_2}]
set_clock_groups -asynchronous -group clk -group clk2
"""

def _quick_sta(opensta: str, liberty: str, netlist: str, module: str,
               period_ps: int, clock_port: str, log_path: Path,
               clock_port_2: str = None, period_ps_2: int = None,
               macro_libs: list = None,
               sdc: Optional[str] = None) -> tuple[Optional[float], Optional[float]]:
    """Run a quick STA at typical corner. Returns (wns_ns, tns_ns) or (None, None)."""
    period_ns = period_ps / 1000.0
    if clock_port_2 and period_ps_2:
        clk2 = QSTA_CLK2.format(
            period_2_ns=period_ps_2 / 1000.0,
            clock_port_2=clock_port_2,
        )
    else:
        clk2 = ''
    macro_lib_section = ''
    if macro_libs:
        macro_lib_section = '\n'.join(f'read_liberty {f}' for f in macro_libs)
    with tempfile.NamedTemporaryFile('w', suffix='.tcl', delete=False) as f:
        f.write(QSTA_TCL.format(
            liberty=liberty, netlist=netlist, module=module,
            period_ns=period_ns, clock_port=clock_port,
            clock_2_section=clk2,
            macro_lib_section=macro_lib_section,
            user_sdc_section=(f'source {sdc}' if sdc else ''),
        ))
        tcl = f.name
    try:
        r = subprocess.run(
            [opensta, '-no_init', '-exit', tcl],
            capture_output=True, text=True, timeout=300,
        )
        out = r.stdout + r.stderr
        Path(log_path).write_text(out)
        wns = tns = None
        for line in out.splitlines():
            m = re.search(r'wns\s+([-0-9.eE+]+)', line, re.I)
            if m and wns is None:
                wns = float(m.group(1))
            m = re.search(r'tns\s+([-0-9.eE+]+)', line, re.I)
            if m and tns is None:
                tns = float(m.group(1))
        return wns, tns
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    finally:
        os.unlink(tcl)

# ============================================================================
# Winner selection
# ============================================================================

@dataclass
class Candidate:
    recipe: str
    netlist: str
    wns_ns: Optional[float]
    tns_ns: Optional[float]
    cells: int
    area: float
    runtime_s: float

@dataclass
class Selection:
    module: str
    objective: str
    winner: Optional[str]
    candidates: list[Candidate]
    pareto_front: list[str] = field(default_factory=list)
    rationale: str = ''

def _stability_idx(recipe: str) -> int:
    return RECIPE_PRIORITY.index(recipe) if recipe in RECIPE_PRIORITY else 999

def _pareto_front(cands: list[Candidate]) -> list[str]:
    """Compute Pareto front on (max wns, min area). Returns recipe names."""
    valid = [c for c in cands if c.wns_ns is not None]
    front = []
    for c in valid:
        dominated = False
        for d in valid:
            if d is c:
                continue
            # d dominates c iff d.wns >= c.wns AND d.area <= c.area, with strict in at least one
            if (d.wns_ns >= c.wns_ns and d.area <= c.area and
                (d.wns_ns > c.wns_ns or d.area < c.area)):
                dominated = True
                break
        if not dominated:
            front.append(c.recipe)
    return front

def select_winner(cands: list[Candidate], objective: str) -> Selection:
    valid = [c for c in cands if c.wns_ns is not None]
    if not valid:
        succeeded = [c for c in cands if c.netlist]
        if succeeded:
            winner = min(succeeded, key=lambda c: (c.area, _stability_idx(c.recipe)))
            return Selection(
                module='',
                objective=objective,
                winner=winner.recipe,
                candidates=cands,
                rationale=f'no WNS data; fallback to min area ({winner.area:.1f} um²)',
            )
        return Selection(
            module='',  # filled by caller
            objective=objective,
            winner=None,
            candidates=cands,
            rationale='no valid candidates (all failed synthesis or STA)',
        )

    front = _pareto_front(cands)

    if objective == 'delay':
        # max wns -> min area -> stability
        winner = max(valid, key=lambda c: (c.wns_ns, -c.area, -_stability_idx(c.recipe)))
        rationale = f'max WNS ({winner.wns_ns:.3f} ns)'

    elif objective == 'area':
        meeting = [c for c in valid if c.wns_ns >= 0]
        if meeting:
            winner = min(meeting, key=lambda c: (c.area, -c.wns_ns, _stability_idx(c.recipe)))
            rationale = f'min area among timing-meeting ({winner.area:.1f} um²)'
        else:
            # fallback: nothing meets timing, pick best WNS
            winner = max(valid, key=lambda c: (c.wns_ns, -c.area))
            rationale = f'no recipe meets timing; fallback to max WNS ({winner.wns_ns:.3f} ns)'

    elif objective == 'fastest':
        meeting = [c for c in valid if c.wns_ns >= 0]
        if meeting:
            winner = max(meeting, key=lambda c: (c.wns_ns, -c.area, -_stability_idx(c.recipe)))
            rationale = f'min delay among timing-meeting (WNS={winner.wns_ns:.3f} ns)'
        else:
            winner = max(valid, key=lambda c: (c.wns_ns, -c.area))
            rationale = f'no recipe meets timing; fallback to max WNS ({winner.wns_ns:.3f} ns)'

    elif objective == 'balanced':
        # 50/50 normalized score: WNS rank + (1/area) rank, both descending
        wns_sorted = sorted(valid, key=lambda c: c.wns_ns, reverse=True)
        area_sorted = sorted(valid, key=lambda c: c.area)
        score = {}
        for i, c in enumerate(wns_sorted):
            score.setdefault(c.recipe, 0)
            score[c.recipe] += i  # lower = better
        for i, c in enumerate(area_sorted):
            score[c.recipe] += i
        winner = min(valid, key=lambda c: (score[c.recipe], _stability_idx(c.recipe)))
        rationale = f'balanced rank: score={score[winner.recipe]}'

    elif objective == 'pareto':
        # report front, but pick a representative: highest-WNS on the front
        front_cands = [c for c in valid if c.recipe in front]
        winner = max(front_cands or valid, key=lambda c: c.wns_ns)
        rationale = f'pareto front: {", ".join(front)} (representative: max-WNS)'

    else:
        raise ValueError(f"unknown objective {objective}")

    return Selection(
        module='',
        objective=objective,
        winner=winner.recipe,
        candidates=cands,
        pareto_front=front,
        rationale=rationale,
    )

# ============================================================================
# Corner STA on winner
# ============================================================================

CORNER_STA_TCL = """\
define_corners fast typical slow
read_liberty -corner fast {lib_fast}
read_liberty -corner typical {lib_typ}
read_liberty -corner slow {lib_slow}
{macro_libs_fast}
{macro_libs_typ}
{macro_libs_slow}
read_verilog {netlist}
link_design {module}
create_clock -name clk -period {period_ns} [get_ports {clock_port}]
{clock_2_section}
set_driving_cell -lib_cell {driving_cell} [all_inputs]
set_load {load_pf} [all_outputs]
set_input_delay  -clock clk [expr {{{period_ns} * 0.2}}] [all_inputs]
set_output_delay -clock clk [expr {{{period_ns} * 0.2}}] [all_outputs]
catch {{ set_false_path -from [get_ports hresetn] }}
catch {{ set_false_path -from [get_ports rst_n] }}
# User-supplied exceptions (false_path, multicycle, set_clock_groups, ...)
{user_sdc_section}

# --- setup at slow ---
puts ">>> SETUP_SLOW_BEGIN"
report_checks -path_delay max -corner slow -group_count 5 -format full_clock
report_worst_slack -max
report_tns
puts ">>> SETUP_SLOW_END"

# --- setup at typical ---
puts ">>> SETUP_TYP_BEGIN"
report_checks -path_delay max -corner typical -group_count 5 -format full_clock
report_worst_slack -max
report_tns
puts ">>> SETUP_TYP_END"

# --- hold at fast ---
puts ">>> HOLD_FAST_BEGIN"
report_checks -path_delay min -corner fast -group_count 5 -format full_clock
report_worst_slack -min
report_tns
puts ">>> HOLD_FAST_END"

# --- hold at typical ---
puts ">>> HOLD_TYP_BEGIN"
report_checks -path_delay min -corner typical -group_count 5 -format full_clock
report_worst_slack -min
report_tns
puts ">>> HOLD_TYP_END"

write_sdf -corner slow {sdf_out}
exit
"""

CORNER_STA_CLK2 = """\
create_clock -name clk2 -period {period_2_ns} [get_ports {clock_port_2}]
set_clock_uncertainty -setup 0.25 [get_clocks clk]
set_clock_uncertainty -setup 0.25 [get_clocks clk2]
set_clock_uncertainty -hold 0.10 [get_clocks clk]
set_clock_uncertainty -hold 0.10 [get_clocks clk2]
set_clock_groups -asynchronous -group [get_clocks clk] -group [get_clocks clk2]
"""

@dataclass
class CornerResult:
    module: str
    success: bool
    wns_setup_slow: Optional[float] = None
    tns_setup_slow: Optional[float] = None
    wns_setup_typ: Optional[float] = None
    tns_setup_typ: Optional[float] = None
    wns_hold_fast: Optional[float] = None
    tns_hold_fast: Optional[float] = None
    wns_hold_typ: Optional[float] = None
    tns_hold_typ: Optional[float] = None
    sdf_path: Optional[str] = None
    report_path: Optional[str] = None
    error: Optional[str] = None

def run_corner_sta(cfg: Config, module: str, netlist: Path,
                   results_dir: Path) -> CornerResult:
    out_dir = results_dir / module
    out_dir.mkdir(parents=True, exist_ok=True)
    sdf  = out_dir / 'winner.sdf'
    rpt  = out_dir / 'sta.rpt'
    log  = out_dir / 'sta.log'
    tcl  = out_dir / 'sta.tcl'

    period_ns = cfg.period_ps / 1000.0
    if cfg.clock_port_2 and cfg.period_ps_2:
        clk2 = CORNER_STA_CLK2.format(
            period_2_ns=cfg.period_ps_2 / 1000.0,
            clock_port_2=cfg.clock_port_2,
        )
    else:
        clk2 = ''
    # Per-corner macro liberty lines. OpenSTA corner is named "typical" in
    # the Tcl while our internal key is "typ"; map at emission.
    def _ml_lines(internal_key: str, sta_corner: str) -> str:
        libs = cfg.macro_libs.get(internal_key, []) if cfg.macro_libs else []
        if not libs:
            return ''
        return '\n'.join(f'read_liberty -corner {sta_corner} {f}' for f in libs)

    tcl.write_text(CORNER_STA_TCL.format(
        lib_fast=cfg.lib_fast, lib_typ=cfg.lib_typ, lib_slow=cfg.lib_slow,
        macro_libs_fast=_ml_lines('fast', 'fast'),
        macro_libs_typ =_ml_lines('typ',  'typical'),
        macro_libs_slow=_ml_lines('slow', 'slow'),
        user_sdc_section=_user_sdc_section(cfg),
        netlist=netlist, module=module,
        period_ns=period_ns, clock_port=cfg.clock_port,
        clock_2_section=clk2,
        driving_cell=cfg.driving_cell,
        load_pf=cfg.load_ff / 1000.0,  # OpenSTA wants pF
        sdf_out=sdf,
    ))

    try:
        r = subprocess.run(
            [cfg.opensta, '-no_init', '-exit', str(tcl)],
            capture_output=True, text=True, timeout=600,
        )
        log.write_text(r.stdout + '\n--- stderr ---\n' + r.stderr)
        out = r.stdout
        rpt.write_text(out)

        def section(tag_begin, tag_end):
            m = re.search(rf'{tag_begin}(.+?){tag_end}', out, re.S)
            return m.group(1) if m else out

        setup_sec = section('>>> SETUP_SLOW_BEGIN', '>>> SETUP_SLOW_END')
        setup_typ_sec = section('>>> SETUP_TYP_BEGIN', '>>> SETUP_TYP_END')
        hold_sec  = section('>>> HOLD_FAST_BEGIN', '>>> HOLD_FAST_END')
        hold_typ_sec = section('>>> HOLD_TYP_BEGIN', '>>> HOLD_TYP_END')

        def grab_wns(text):
            m = re.search(r'^worst slack\s+([-0-9.eE+]+)', text, re.M)
            return float(m.group(1)) if m else None

        def grab_tns(text):
            m = re.search(r'^total negative slack\s+([-0-9.eE+]+)', text, re.M)
            return float(m.group(1)) if m else None

        def grab_wns_from_paths(text):
            m_all = re.findall(r'^\s+([-0-9.eE+]+)\s+slack\s', text, re.M)
            return float(min(m_all, key=float)) if m_all else None

        return CornerResult(
            module=module,
            success=(r.returncode == 0),
            wns_setup_slow=grab_wns(setup_sec),
            tns_setup_slow=grab_tns(setup_sec),
            wns_setup_typ=grab_wns_from_paths(setup_typ_sec) or grab_wns(setup_typ_sec),
            tns_setup_typ=grab_tns(setup_typ_sec),
            wns_hold_fast=grab_wns(hold_sec),
            tns_hold_fast=grab_tns(hold_sec),
            wns_hold_typ=grab_wns_from_paths(hold_typ_sec) or grab_wns(hold_typ_sec),
            tns_hold_typ=grab_tns(hold_typ_sec),
            sdf_path=str(sdf) if sdf.exists() else None,
            report_path=str(rpt),
            error=None if r.returncode == 0 else f'opensta exit {r.returncode}',
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return CornerResult(module=module, success=False, error=str(e))

# ============================================================================
# Gate-level simulation
# ============================================================================

@dataclass
class GLSResult:
    success: bool
    log_path: Optional[str] = None
    compile_log_path: Optional[str] = None
    error: Optional[str] = None

def run_gls(cfg: Config, assembled_netlist: Path, sdf_paths: dict[str, str],
            results_dir: Path) -> GLSResult:
    vvp_out = results_dir / 'gls.vvp'
    log     = results_dir / 'gls.log'
    clog    = results_dir / 'gls.compile.log'

    cmd = [cfg.iverilog, '-g2012', '-o', str(vvp_out)]
    if cfg.tb_top:
        cmd += ['-s', cfg.tb_top]
    cmd += list(cfg.tb_files)
    cmd.append(str(assembled_netlist))
    if cfg.primitives_dir:
        prim = Path(cfg.primitives_dir)
        if prim.is_dir():
            cmd += ['-y', str(prim)]
            # try to add the canonical primitives include file if present
            for pat in ('*sc_hd.v', 'primitives.v', '*.v'):
                hits = sorted(prim.glob(pat))
                if hits:
                    cmd += ['-v', str(hits[0])]
                    break

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        clog.write_text(r.stdout + '\n--- stderr ---\n' + r.stderr)
        if r.returncode != 0:
            return GLSResult(success=False, compile_log_path=str(clog),
                             error=f'iverilog exit {r.returncode}')
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return GLSResult(success=False, error=f'iverilog launch: {e}')

    # Run simulation
    vvp_cmd = [cfg.vvp, '-N', str(vvp_out)]
    # SDF back-annotation is typically via $sdf_annotate inside the TB.
    # We pass the SDF path as a plusarg the TB can use: +sdf_<module>=<path>
    if cfg.sdf_back_annotate:
        for module, path in sdf_paths.items():
            vvp_cmd.append(f'+sdf_{module}={path}')

    try:
        r = subprocess.run(vvp_cmd, capture_output=True, text=True, timeout=3600)
        log.write_text(r.stdout + '\n--- stderr ---\n' + r.stderr)
        # Failure detection: explicit FAIL/ERROR in output, or non-zero exit
        out_l = r.stdout.lower() + r.stderr.lower()
        failed = (r.returncode != 0 or
                  re.search(r'\b(fail|error)\b', out_l) is not None)
        return GLSResult(
            success=not failed,
            log_path=str(log),
            compile_log_path=str(clog),
            error=None if not failed else 'simulation reported failure',
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return GLSResult(success=False, log_path=str(log), error=str(e))

# ============================================================================
# Reporting
# ============================================================================

def write_reports(cfg: Config, selections: dict[str, Selection],
                  corners: dict[str, CornerResult],
                  gls: Optional[GLSResult],
                  results_dir: Path) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)

    # ----- summary.json (full data) -----
    full = {
        'config': {
            'top': cfg.top,
            'period_ps': cfg.period_ps,
            'objective': cfg.objective,
            'recipes': cfg.recipes,
            'modules': list(selections.keys()),
            'abc_sequential': cfg.abc_sequential,
        },
        'modules': {
            m: {
                'winner': sel.winner,
                'rationale': sel.rationale,
                'pareto_front': sel.pareto_front,
                'candidates': [asdict(c) for c in sel.candidates],
                'corner': asdict(corners[m]) if m in corners else None,
            }
            for m, sel in selections.items()
        },
        'gls': asdict(gls) if gls else None,
    }
    (results_dir / 'summary.json').write_text(json.dumps(full, indent=2))

    # ----- summary.csv (one row per module-recipe) -----
    csv_path = results_dir / 'summary.csv'
    with open(csv_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['module', 'recipe', 'is_winner', 'wns_ns', 'tns_ns',
                    'cells', 'area_um2', 'runtime_s',
                    'wns_setup_slow', 'wns_setup_typ', 'wns_hold_fast', 'wns_hold_typ'])
        for m, sel in selections.items():
            corner = corners.get(m)
            for c in sel.candidates:
                is_winner = (c.recipe == sel.winner)
                w.writerow([
                    m, c.recipe, is_winner,
                    c.wns_ns if c.wns_ns is not None else '',
                    c.tns_ns if c.tns_ns is not None else '',
                    c.cells, f'{c.area:.2f}', f'{c.runtime_s:.1f}',
                    (corner.wns_setup_slow if corner and is_winner else ''),
                    (corner.wns_setup_typ  if corner and is_winner else ''),
                    (corner.wns_hold_fast  if corner and is_winner else ''),
                    (corner.wns_hold_typ   if corner and is_winner else ''),
                ])

    # ----- summary.md (human-readable) -----
    md = []
    md.append(f'# Synthesis Summary — `{cfg.top}`')
    md.append('')
    md.append(f'- **Objective:** `{cfg.objective}`')
    md.append(f'- **Target period:** {cfg.period_ps} ps ({cfg.period_ps/1000:.2f} ns, {1e6/cfg.period_ps:.1f} MHz)')
    md.append(f'- **Modules:** {len(selections)}')
    md.append(f'- **Recipes swept:** {", ".join(cfg.recipes)}')
    if cfg.abc_sequential:
        md.append(f'- **ABC sequential mode:** ⚠️ enabled (retiming/scorr active)')
    md.append('')
    md.append('## Winners')
    md.append('')
    md.append('| Module | Recipe | WNS typ (ns) | Cells | Area (um²) | WNS setup slow | WNS setup typ | WNS hold fast | WNS hold typ | Status |')
    md.append('|---|---|---|---|---|---|---|---|---|---|')
    for m, sel in selections.items():
        if sel.winner is None:
            md.append(f'| `{m}` | FAILED | — | — | — | — | — | — | — | ❌ |')
            continue
        win = next((c for c in sel.candidates if c.recipe == sel.winner), None)
        corner = corners.get(m)
        setup_slow = (f'{corner.wns_setup_slow:.3f}'
                 if corner and corner.wns_setup_slow is not None else '—')
        setup_typ = (f'{corner.wns_setup_typ:.3f}'
                 if corner and corner.wns_setup_typ is not None else '—')
        hold_fast  = (f'{corner.wns_hold_fast:.3f}'
                 if corner and corner.wns_hold_fast is not None else '—')
        hold_typ  = (f'{corner.wns_hold_typ:.3f}'
                 if corner and corner.wns_hold_typ is not None else '—')
        status = '✅'
        if corner and corner.wns_setup_slow is not None and corner.wns_setup_slow < 0:
            status = '⚠️ setup'
        if corner and corner.wns_hold_fast is not None and corner.wns_hold_fast < 0:
            status = '⚠️ hold' if status == '✅' else '⚠️ both'
        md.append(
            f'| `{m}` | `{sel.winner}` | '
            f'{win.wns_ns:.3f} | {win.cells} | {win.area:.1f} | '
            f'{setup_slow} | {setup_typ} | {hold_fast} | {hold_typ} | {status} |'
        )
    md.append('')

    # Per-module recipe comparison
    md.append('## Recipe sweep results')
    md.append('')
    for m, sel in selections.items():
        md.append(f'### `{m}`')
        md.append('')
        md.append(f'**Winner:** `{sel.winner}` — {sel.rationale}')
        if sel.pareto_front:
            md.append(f'**Pareto front:** {", ".join(sel.pareto_front)}')
        md.append('')
        md.append('| Recipe | WNS (ns) | Cells | Area (um²) | Runtime (s) | Notes |')
        md.append('|---|---|---|---|---|---|')
        for c in sorted(sel.candidates, key=lambda x: _stability_idx(x.recipe)):
            wns = f'{c.wns_ns:.3f}' if c.wns_ns is not None else 'FAIL'
            mark = ' ★' if c.recipe == sel.winner else ''
            note = ''
            if c.recipe in sel.pareto_front and c.recipe != sel.winner:
                note = 'on pareto front'
            md.append(
                f'| `{c.recipe}`{mark} | {wns} | {c.cells} | '
                f'{c.area:.1f} | {c.runtime_s:.1f} | {note} |'
            )
        md.append('')

    # GLS
    if gls is not None:
        md.append('## GLS')
        md.append('')
        if gls.success:
            md.append('✅ **PASSED**')
        else:
            md.append(f'❌ **FAILED** — {gls.error}')
            if gls.log_path:
                md.append(f'See `{gls.log_path}` and `{gls.compile_log_path}`.')
        md.append('')

    (results_dir / 'summary.md').write_text('\n'.join(md))

# ============================================================================
# CLI / main
# ============================================================================

def parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    p.add_argument('--config', help='YAML config file')
    # Direct overrides
    p.add_argument('--rtl', action='append', help='RTL files/globs (repeatable)')
    p.add_argument('--lib', help='typical-corner liberty (synthesis)')
    p.add_argument('--lib-fast', help='fast-corner liberty (STA hold)')
    p.add_argument('--lib-slow', help='slow-corner liberty (STA setup)')
    p.add_argument('--macro-lib', action='append', dest='macro_lib',
                   help='hard-macro liberty (e.g. SRAM .lib). Repeatable. '
                        'Applied to all STA corners (typ/fast/slow). '
                        'Use the YAML `macro_libs:` dict for per-corner files.')
    p.add_argument('--top', help='top module name')
    p.add_argument('--period-ps', type=int, help='target clock period in ps')
    p.add_argument('--clock-port', help='clock port name (default: clk)')
    p.add_argument('--objective', choices=['delay', 'area', 'fastest', 'pareto', 'balanced'])
    p.add_argument('--modules', nargs='+', help='modules to synthesize')
    p.add_argument('--recipes', nargs='+', help='recipes to sweep')
    p.add_argument('--driving-cell')
    p.add_argument('--load-ff', type=float)
    p.add_argument('--parallel', type=int, help='worker count (0=auto)')
    p.add_argument('--no-sta',  action='store_true')
    p.add_argument('--no-gls',  action='store_true')
    p.add_argument('--abc-sequential', action='store_true',
                   help='EXPERIMENTAL: enable ABC -dff for sequential opt '
                        '(retiming, scorr). Breaks LEC; use with care.')
    p.add_argument('--hierarchical', action='store_true',
                   help='Hierarchical bottom-up synthesis: synthesize leaf '
                        'modules first, then use their winning netlists as '
                        'inputs when synthesizing parent modules.')
    p.add_argument('--depth-only', action='store_true',
                   help='Fast depth-only analysis: skip recipe sweep, STA, GLS. '
                        'Estimates Fmax from longest topological path length.')
    p.add_argument('--list-modules', action='store_true',
                   help='only print discovered modules and exit')
    p.add_argument('--work-dir')
    p.add_argument('--results-dir')
    p.add_argument('-v', '--verbose', action='count', default=0)
    p.add_argument('-q', '--quiet', action='store_true')
    return p.parse_args()

def apply_cli_overrides(cfg: Config, args: argparse.Namespace) -> None:
    map_ = {
        'rtl': 'rtl_files', 'lib': 'lib_typ', 'lib_fast': 'lib_fast',
        'lib_slow': 'lib_slow', 'top': 'top', 'period_ps': 'period_ps',
        'clock_port': 'clock_port', 'objective': 'objective',
        'modules': 'modules', 'recipes': 'recipes',
        'driving_cell': 'driving_cell', 'load_ff': 'load_ff',
        'parallel': 'parallel', 'work_dir': 'work_dir',
        'results_dir': 'results_dir',
    }
    for arg, attr in map_.items():
        v = getattr(args, arg, None)
        if v is not None:
            setattr(cfg, attr, v)
    if args.no_sta:
        cfg.run_sta = False
    if args.no_gls:
        cfg.run_gls = False
    if args.abc_sequential:
        cfg.abc_sequential = True
    if args.hierarchical:
        cfg.hierarchical = True
    if args.depth_only:
        cfg.depth_only = True
    # rtl globs need expansion
    if args.rtl:
        expanded = []
        for pat in args.rtl:
            hits = sorted(glob.glob(os.path.expanduser(pat)))
            expanded.extend(hits if hits else [pat])
        cfg.rtl_files = expanded
    # macro libs from CLI: flat list, mirrored to every corner
    if getattr(args, 'macro_lib', None):
        cfg.macro_libs = _normalise_macro_libs(args.macro_lib)

def discover_recipes(recipes_dir: Path, requested: list[str]) -> list[tuple[str, Path]]:
    available = {p.stem: p for p in recipes_dir.glob('*.abc')}
    if not requested:
        return [(name, available[name]) for name in
                sorted(available, key=_stability_idx)]
    out = []
    for r in requested:
        if r not in available:
            raise FileNotFoundError(f'recipe not found: {r} (available: {list(available)})')
        out.append((r, available[r]))
    return out

def main() -> int:
    args = parse_cli()
    log_level = logging.DEBUG if args.verbose >= 2 else \
                logging.INFO if (args.verbose == 1 or not args.quiet) else \
                logging.WARNING
    logging.basicConfig(level=log_level, format='%(message)s')
    log = logging.getLogger('synth_flow')

    # ----- load config -----
    cfg = Config()
    if args.config:
        if not Path(args.config).exists():
            log.error(f"config not found: {args.config}")
            return EXIT_CONFIG_ERR
        cfg = Config.from_yaml(Path(args.config))
    cfg.merge_env()
    apply_cli_overrides(cfg, args)

    errs = cfg.validate()
    if errs:
        for e in errs:
            log.error(f"config error: {e}")
        return EXIT_CONFIG_ERR

    if cfg.abc_sequential:
        log.warning("=" * 70)
        log.warning("EXPERIMENTAL: abc_sequential is ENABLED")
        log.warning("  ABC will be invoked with -dff and may retime/restructure")
        log.warning("  flops. This breaks 1:1 register correspondence with RTL.")
        log.warning("  Implications:")
        log.warning("    - Formal LEC against the RTL will require retiming-aware")
        log.warning("      tools/options")
        log.warning("    - Async resets, clock gating, and complex flop semantics")
        log.warning("      may not survive intact")
        log.warning("    - Hold fixing in P&R may behave unexpectedly")
        log.warning("  Verify the resulting netlist carefully before tape-out.")
        log.warning("=" * 70)

    work = Path(cfg.work_dir)
    results = Path(cfg.results_dir)
    work.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)

    # ----- discover modules -----
    if not cfg.modules:
        cfg.modules = ModuleScanner.scan(cfg.rtl_files)
        log.info(f"auto-detected modules: {', '.join(cfg.modules)}")
    if not cfg.modules:
        log.error("no modules found in RTL")
        return EXIT_CONFIG_ERR

    if args.list_modules:
        for m in cfg.modules:
            print(m)
        return EXIT_OK

    # ----- depth-only fast analysis -----
    if cfg.depth_only:
        cfg_dict = asdict(cfg)
        period_ns = cfg.period_ps / 1000.0
        gate_ps = cfg.depth_gate_delay_ps
        log.info(f"depth-only analysis: {len(cfg.modules)} modules, "
                 f"gate_delay={gate_ps:.0f}ps, target={period_ns:.2f}ns")
        for module in cfg.modules:
            mod_dir = work / module
            res = run_depth({
                'module': module,
                'cfg': cfg_dict,
                'workdir': str(mod_dir),
            })
            if not res.success:
                log.error(f"[depth] {module:<20s}: FAILED — {res.error}")
                continue
            flag = ''
            if res.est_delay_ns > period_ns:
                flag = '  ⚠ exceeds target'
            log.info(f"[depth] {res.module:<20s}: {res.depth:>3d} levels  "
                     f"→ ~{res.est_delay_ns:.2f} ns  "
                     f"→ ~{res.est_fmax_mhz:.0f} MHz  "
                     f"({res.cells} cells){flag}")
        return EXIT_OK

    # ----- discover recipes -----
    recipes_dir = Path(cfg.recipes_dir)
    try:
        recipe_pairs = discover_recipes(recipes_dir, cfg.recipes)
    except FileNotFoundError as e:
        log.error(str(e))
        return EXIT_CONFIG_ERR
    cfg.recipes = [name for name, _ in recipe_pairs]
    log.info(f"recipes: {', '.join(cfg.recipes)}")

    # ----- generate constraint file -----
    constr = _write_constraint_file(work, cfg.driving_cell, cfg.load_ff)

    # ----- detect hierarchy (if hierarchical mode) -----
    all_deps: dict[str, set[str]] = {}
    if cfg.hierarchical:
        all_deps = ModuleScanner.dependencies(cfg.rtl_files)
        cfg.modules = _topo_sort(cfg.modules, all_deps)
        log.info(f"hierarchical order: {' → '.join(cfg.modules)}")
        for m in cfg.modules:
            if all_deps.get(m):
                log.info(f"  {m} depends on: {', '.join(sorted(all_deps[m]))}")

    # ----- assemble jobs (module x recipe) -----
    # In hierarchical mode we process modules one at a time in dependency
    # order so that winner netlists are available for parent modules.
    cfg_dict = asdict(cfg)
    by_module: dict[str, list[RecipeResult]] = {m: [] for m in cfg.modules}
    winner_netlists: dict[str, str] = {}  # module → winner.v path
    any_synth_failed = False
    selections: dict[str, Selection] = {}

    def _run_jobs(job_list: list[dict]):
        nonlocal any_synth_failed
        if cfg.effective_parallel() == 1:
            for job in job_list:
                res = run_recipe(job)
                by_module[res.module].append(res)
                _log_recipe(res)
        else:
            with mp.Pool(cfg.effective_parallel()) as pool:
                for res in pool.imap_unordered(run_recipe, job_list):
                    by_module[res.module].append(res)
                    _log_recipe(res)

    def _log_recipe(res: RecipeResult):
        nonlocal any_synth_failed
        status = '✓' if res.success else '✗'
        log.info(f"  [{status}] {res.module}/{res.recipe}  "
                 f"runtime={res.runtime_s:.1f}s  "
                 f"cells={res.cells}  area={res.area:.1f}"
                 + (f"  err={res.error}" if not res.success else ""))

    def _pick_winner(module: str):
        nonlocal any_synth_failed
        cands: list[Candidate] = []
        for r in by_module[module]:
            if not r.success:
                any_synth_failed = True
                cands.append(Candidate(
                    recipe=r.recipe, netlist=r.netlist or '',
                    wns_ns=None, tns_ns=None,
                    cells=r.cells, area=r.area,
                    runtime_s=r.runtime_s,
                ))
                continue
            qsta_log = work / module / f'{r.recipe}.qsta.log'
            qsta_lib = cfg.lib_slow if cfg.lib_slow else cfg.lib_typ
            qsta_corner = 'slow' if cfg.lib_slow else 'typ'
            wns, tns = _quick_sta(
                cfg.opensta, qsta_lib, r.netlist, module,
                cfg.period_ps, cfg.clock_port, qsta_log,
                clock_port_2=cfg.clock_port_2,
                period_ps_2=cfg.period_ps_2,
                macro_libs=cfg.macro_libs.get(qsta_corner, []) if cfg.macro_libs else [],
                sdc=cfg.sdc,
            )
            cands.append(Candidate(
                recipe=r.recipe, netlist=r.netlist,
                wns_ns=wns, tns_ns=tns,
                cells=r.cells, area=r.area,
                runtime_s=r.runtime_s,
            ))

        sel = select_winner(cands, cfg.objective)
        sel.module = module
        selections[module] = sel

        if sel.winner:
            win = next(c for c in cands if c.recipe == sel.winner)
            log.info(f"[winner] {module}: {sel.winner}  "
                     f"WNS={win.wns_ns:.3f}ns  area={win.area:.1f}  ({sel.rationale})")
            mod_results = results / module
            mod_results.mkdir(parents=True, exist_ok=True)
            shutil.copy(win.netlist, mod_results / 'winner.v')
            winner_netlists[module] = str(mod_results / 'winner.v')
            (mod_results / 'selection.json').write_text(
                json.dumps({
                    'winner': sel.winner,
                    'rationale': sel.rationale,
                    'pareto_front': sel.pareto_front,
                    'candidates': [asdict(c) for c in cands],
                }, indent=2)
            )
        else:
            log.error(f"[winner] {module}: NONE — {sel.rationale}")

    if cfg.hierarchical:
        # Bottom-up: synthesize modules in dependency order, one module
        # at a time, so winner netlists are available for parents.
        t0 = time.time()
        for module in cfg.modules:
            deps = all_deps.get(module, set())
            dep_nets = {d: winner_netlists[d] for d in deps
                        if d in winner_netlists}
            dep_mods = set(dep_nets.keys())
            log.info(f"[hier] synthesizing {module}"
                     + (f" (using netlists: {', '.join(dep_mods)})" if dep_nets else ""))

            jobs = []
            mod_dir = work / module
            for recipe_name, recipe_path in recipe_pairs:
                jobs.append({
                    'module': module,
                    'recipe': recipe_name,
                    'recipe_path': str(recipe_path),
                    'workdir': str(mod_dir),
                    'constr': str(constr),
                    'cfg': cfg_dict,
                    'dep_netlists': dep_nets,
                    'dep_modules': dep_mods,
                })
            total = len(jobs)
            log.info(f"  running {total} jobs ({total // len(recipe_pairs)} modules × "
                     f"{len(recipe_pairs)} recipes) on {cfg.effective_parallel()} workers")
            _run_jobs(jobs)
            _pick_winner(module)
        log.info(f"hierarchical sweep done in {time.time()-t0:.1f}s")
    else:
        # Flat mode: synthesize all modules in parallel
        jobs = []
        for module in cfg.modules:
            mod_dir = work / module
            for recipe_name, recipe_path in recipe_pairs:
                jobs.append({
                    'module': module,
                    'recipe': recipe_name,
                    'recipe_path': str(recipe_path),
                    'workdir': str(mod_dir),
                    'constr': str(constr),
                    'cfg': cfg_dict,
                })

        log.info(f"running {len(jobs)} jobs ({len(cfg.modules)} modules × {len(cfg.recipes)} recipes) "
                 f"on {cfg.effective_parallel()} workers")

        t0 = time.time()
        _run_jobs(jobs)
        log.info(f"synthesis sweep done in {time.time()-t0:.1f}s")

        # ----- per-module: quick STA + winner pick -----
        for module in cfg.modules:
            _pick_winner(module)

    # ----- corner STA on winners -----
    corners: dict[str, CornerResult] = {}
    if cfg.run_sta:
        for module, sel in selections.items():
            if sel.winner is None:
                continue
            netlist = results / module / 'winner.v'
            log.info(f"[sta] {module}  fast={Path(cfg.lib_fast).name}  typ={Path(cfg.lib_typ).name}  slow={Path(cfg.lib_slow).name}")
            cr = run_corner_sta(cfg, module, netlist, results)
            corners[module] = cr
            if cr.success:
                log.info(f"  setup_slow={cr.wns_setup_slow}  setup_typ={cr.wns_setup_typ}  hold_fast={cr.wns_hold_fast}  hold_typ={cr.wns_hold_typ}")
            else:
                log.error(f"  STA failed: {cr.error}")

    # ----- assemble + GLS -----
    gls_result: Optional[GLSResult] = None
    if cfg.run_gls and all(s.winner for s in selections.values()):
        assembled = results / f'{cfg.top}.netlist.v'
        with open(assembled, 'w') as out:
            for module in cfg.modules:
                out.write(f'// ---- {module} ----\n')
                out.write((results / module / 'winner.v').read_text())
                out.write('\n')
        sdf_paths = {m: c.sdf_path for m, c in corners.items() if c.sdf_path}
        log.info(f"[gls] iverilog + vvp on {assembled.name}")
        gls_result = run_gls(cfg, assembled, sdf_paths, results)
        if gls_result.success:
            log.info("[gls] PASSED")
        else:
            log.error(f"[gls] FAILED — {gls_result.error}")

    # ----- reports -----
    write_reports(cfg, selections, corners, gls_result, results)
    log.info(f"reports written to {results}/summary.{{md,json,csv}}")

    # ----- exit code -----
    if any(sel.winner is None for sel in selections.values()):
        return EXIT_SYNTH_FAIL
    if cfg.fail_on_timing:
        for cr in corners.values():
            if cr.wns_setup_slow is not None and cr.wns_setup_slow < 0:
                return EXIT_TIMING_FAIL
    if gls_result is not None and not gls_result.success:
        return EXIT_GLS_FAIL
    return EXIT_OK

if __name__ == '__main__':
    sys.exit(main())
