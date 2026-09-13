#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
synth_flow.py — generic ASIC synthesis + STA + GLS orchestrator.

Sweeps multiple ABC recipes per RTL module, picks the winner per a chosen
objective (delay / area / balanced recipe sets; winner = min area meeting timing), runs multi-corner
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
import hashlib
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import sdc_parse
except ImportError:  # pragma: no cover
    sdc_parse = None
try:
    import liberty_timing
except ImportError:  # pragma: no cover
    liberty_timing = None

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
# Recipe subsets per objective. Top-5 of the 108-candidate pipeline bench
# (bench/results/pipeline2.csv, 2026-09-12): normalized gap to the best
# candidate per design, mean over 16 designs. --full-sweep runs every recipe.
RECIPE_SETS = {
    'delay':    ['delay_map_resyn', 'delay_map', 'orfs_area', 'delay_choice_deep_v3', 'delay_syn2'],
    'area':     ['delay_aggressive', 'area_lut6', 'area_max', 'yosys_default', 'area_classic'],
    'balanced': ['balanced_resyn', 'balanced_resyn2x', 'delay_triple', 'delay_iter_heavy', 'delay_map_resyn'],
}

# Tie-break order for winner selection (lower = preferred when metrics tie).
# Ordered by mean WNS rank across the 16-design STA bench (2026-09-12:
# baseline-noD.csv, newrecipes.csv); references last.
RECIPE_PRIORITY = [
    'delay_map',
    'delay_map_resyn',
    'delay_choice_deep_v3',
    'delay_triple',
    'delay_iter_heavy',
    'balanced_resyn',
    'delay_choice_deep',
    'balanced_resyn2x',
    'delay_syn2',
    'delay_choice_deep_bb',
    'delay_aggressive',
    'delay_choice_deep_v4',
    'area_classic',
    'orfs_area',
    'area_lut6',
    'area_max',
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
    # Several standard-cell libraries at once (e.g. sky130_fd_sc_hs + _ls, which
    # share a placement site): give lib_typ / lib_slow / lib_fast / lib_synth as
    # YAML lists. The first file is the primary library (wire-load model, flop
    # timing for budgets); the rest land here per corner and are loaded into
    # every Yosys (-liberty) and OpenSTA (read_liberty) step and into the
    # post-pass, which can then swap a cell for its same-named variant in a
    # faster or slower library.
    lib_extra: dict = field(default_factory=lambda: {'typ': [], 'slow': [], 'fast': []})
    lib_synth_extra: list = field(default_factory=list)
    # With several libraries: 'fastest' maps with the fastest library only
    # (critical paths come out of ABC with fast cells; slower cells enter through
    # the post-pass recovery where slack allows); 'all' offers every cell to ABC.
    mixed_map: str = 'fastest'

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
    # objective picks WHICH recipes run (RECIPE_SETS); the winner is always the
    # candidate that meets timing (WNS >= select_margin_ps) with the least
    # area, falling back to best WNS when nothing meets. 'fastest' and
    # 'pareto' are accepted as aliases (delay / balanced) for old configs.
    objective: str = 'delay'
    full_sweep: bool = False           # run every recipe regardless of objective
    select_margin_ps: int = 0          # slack a candidate needs to count as meeting timing
    # When no candidate meets timing:
    #   knee     (default) the knee of the WNS/area Pareto front among the
    #            failing candidates: closest point to (best WNS, least area)
    #            after normalizing both axes over the front, with WNS clipped
    #            to one period below the best so hopeless candidates do not
    #            stretch the scale. Parameter-free. mul32_mac: -2.07 ns at
    #            40 142 um2 instead of -0.57 ns at 60 888 (+52 %).
    #   best_wns the fastest candidate regardless of area (old behaviour).
    fallback: str = 'knee'
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
    # STA modelling (applied identically to quick STA and multi-corner STA)
    clock_uncertainty_setup_ps: int = 250
    clock_uncertainty_hold_ps: int = 100
    io_delay_frac: float = 0.2          # default input/output delay (max) as fraction of period
    # Default minimum I/O delay for hold, as a fraction of the max delay. Zero
    # (the old default) manufactures hold violations on every short
    # input-to-register path; 0.4 is the usual template value.
    io_delay_min_frac: float = 0.4
    wire_load_model: str = 'auto'       # auto = liberty default_wire_load | none | <name>
    # Path-group partitioned mapping (docs/architecture.md §3): split the
    # combinational logic into in->reg / reg->out / in->out / relaxed groups
    # and give each its own ABC delay target derived from the SDC and the
    # liberty flop timing. reg->reg logic gets T - t_cq - t_su - uncertainty.
    # ABC delay target (-D) substituted for {D} in recipes.
    #   'none'    (default) no -D: ABC maps for minimum delay in its own model.
    #             Measured best on the bench: any -D lets ABC relax/downsize
    #             against a model without wire load, and OpenSTA disagrees
    #             (period: mean WNS -0.59 ns for -3 % area; see docs/architecture.md §2.6).
    #   'period'  full clock period (ORFS convention)
    #   'reg2reg' T - t_cq - t_su - setup uncertainty from the synthesis liberty
    #   '<ps>'    explicit integer
    abc_target: str = 'none'
    # Make ABC's buffer/upsize/dnsize/stime use the liberty wire-load model
    # (`-c`) in every recipe. Measured: clear gain only for the `map`-based
    # recipes (baked into delay_map*.abc); noise for `&nf` recipes. Off by default.
    abc_wire_load: bool = False
    # OpenSTA-guided drive-strength sizing of each module's winner (resize.py).
    # Sizing only, function preserved; measured on the bench: TNS down on every
    # failing design for <1 % area on most (docs/architecture.md §2.7).
    # Yosys front-end options (Phase 5 sweep dimension). Tokens:
    #   booth                 synth -booth (Booth-encoded $mul)
    #   adder=<arch>          synth -extra-map +/choices/<arch>.v
    #                         (kogge-stone | han-carlson | sklansky; default ripple/Brent-Kung)
    #   noshare               synth -noshare
    #   hieropt               synth -hieropt
    #   opt_dff_sat           `opt_dff -sat` after synth
    #   opt_full              `opt -full` after synth
    # Either a token list (all modules) or a dict {module: [tokens], '*': [...]}.
    yosys_opts: Any = field(default_factory=list)
    # Sweep front-end variants as a second candidate dimension: each entry is a
    # yosys_opts list ([] = plain). Candidates are named <recipe>@<variant>.
    # Bench: best-of (6 variants x 16 recipes) closes 9/16 designs vs 6/16 with
    # recipes alone, mean best-WNS +0.24 ns, no design worse (docs/benchmarks.md).
    # Recommended: [[], [adder=kogge-stone], [adder=han-carlson], [adder=sklansky],
    #               [booth], [booth, adder=kogge-stone]]
    # Either a list of variants (all modules) or a dict {module: [variants], '*': [...]}.
    yosys_opts_sweep: Any = field(default_factory=list)
    # Liberty cells excluded from mapping (glob patterns), passed as
    # `-dont_use` to both `abc` and `dfflibmap`. Needed with a full PDK
    # liberty (probe, lpflow, delay cells...). The bundled hd_120 subset
    # already excludes them.
    dont_use: list[str] = field(default_factory=list)
    resize_winner: bool = False
    # Post-pass repairs on the winner (resize.py), all judged by OpenSTA:
    #   repair_design  split high-fanout nets on failing setup paths with
    #                  buffer trees (groups of <= max_fanout sinks)
    #   repair_hold    delay cells in front of failing hold endpoints at the
    #                  fast corner, kept only while slow-corner setup holds
    # Any of resize_winner / repair_design / repair_hold enables the pass.
    repair_design: bool = False
    repair_hold: bool = False
    max_fanout: int = 8                # SDC set_max_fanout overrides
    repair_buffer_cell: Optional[str] = None   # default: second-weakest buffer of the liberty
    repair_delay_cell: Optional[str] = None    # default: slowest buffer (dlygate when present)
    resize_iters: int = 25
    resize_wns_tol_ps: int = 150       # WNS regression tolerated for a TNS gain ('tns' policy)
    resize_final: str = 'tns'
    # after timing: downsize / swap to a slower library off-critical cells while
    # WNS holds (resize.py --recover-area). Also --recover-area.
    resize_recover_area: bool = False
    # post-pass on the N most promising candidates (selected, fastest, Pareto
    # front), then select again: a fast candidate that only closes after
    # sizing is not missed. 1 = the selected winner only. Also --resize-candidates.
    resize_candidates: int = 1
    # hold repair search budgets (resize.py): failing endpoints per STA and the
    # number of OpenSTA calls the hold phase may spend
    repair_hold_max_paths: Optional[int] = None
    repair_hold_sta_budget: int = 60          # tns | wns (never regress WNS)
    path_groups: bool = False
    relaxed_factor: float = 3.0         # -D multiplier for false-path cones
    min_budget_frac: float = 0.25       # never hand ABC less than this fraction of T

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
    dual_clock_synthesis: bool = False   # experimental: abc -dff per clock domain

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
            elif key in data and isinstance(data[key], list):
                data[key] = [os.path.expandvars(os.path.expanduser(str(x))) for x in data[key]]
        _split_lib_lists(data)
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
        # macro_libs / extra standard-cell libs: every file must exist
        for corner in ('typ', 'fast', 'slow'):
            for f in self.macro_libs.get(corner, []):
                if not Path(f).exists():
                    errs.append(f"macro_libs[{corner}] missing: {f}")
            for f in (self.lib_extra or {}).get(corner, []):
                if not Path(f).exists():
                    errs.append(f"lib_{corner} extra library missing: {f}")
        for f in self.lib_synth_extra:
            if not Path(f).exists():
                errs.append(f"lib_synth extra library missing: {f}")
        if self.run_gls:
            if not self.tb_files:
                errs.append("tb_files required for GLS")
            for f in self.tb_files:
                if not Path(f).exists():
                    errs.append(f"tb file missing: {f}")
        if self.mixed_map not in ('fastest', 'all'):
            errs.append(f"mixed_map must be fastest|all, got {self.mixed_map}")
        if self.objective not in ('delay', 'area', 'balanced', 'fastest', 'pareto'):
            errs.append(f"objective must be delay|area|balanced, got {self.objective}")
        if self.fallback not in ('knee', 'best_wns'):
            errs.append(f"fallback must be knee|best_wns, got {self.fallback}")
        if self.parallel < 0:
            errs.append("parallel must be >= 0")
        if self.run_sta:
            # opensta may be a path or a PATH binary (or a docker wrapper script)
            if not (shutil.which(self.opensta) or Path(self.opensta).is_file()):
                errs.append(
                    f"opensta not found: {self.opensta!r} "
                    f"(set opensta: in YAML or OPENSTA=/path/to/sta)"
                )
        return errs

    def effective_parallel(self) -> int:
        return self.parallel if self.parallel > 0 else (os.cpu_count() or 1)


def _split_lib_lists(data: dict) -> None:
    """YAML `lib_typ: [a.lib, b.lib]` -> lib_typ = a.lib, lib_extra[typ] = [b.lib]
    (same for slow/fast; lib_synth -> lib_synth_extra). Comma-separated strings
    are accepted too (CLI). No-op for plain single paths."""
    extra = dict(data.get('lib_extra') or {})
    for key, corner in (('lib_typ', 'typ'), ('lib_slow', 'slow'), ('lib_fast', 'fast')):
        v = data.get(key)
        if isinstance(v, str) and ',' in v:
            v = [x.strip() for x in v.split(',') if x.strip()]
        if isinstance(v, list):
            data[key] = v[0] if v else ''
            extra[corner] = list(extra.get(corner, [])) + [str(x) for x in v[1:]]
    for c in ('typ', 'slow', 'fast'):
        extra.setdefault(c, [])
    data['lib_extra'] = extra
    v = data.get('lib_synth')
    if isinstance(v, str) and ',' in v:
        v = [x.strip() for x in v.split(',') if x.strip()]
    if isinstance(v, list):
        data['lib_synth'] = v[0] if v else None
        data['lib_synth_extra'] = list(data.get('lib_synth_extra') or []) + [str(x) for x in v[1:]]


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
        # Match both parameterized (`leaf #(.W(1)) u0 (`) and plain
        # (`leaf u0 (`) instantiations. Previously only `#(` forms were
        # detected, which broke hierarchical bottom-up for most RTL.
        inst_re = re.compile(
            r'\b(\w+)\s+(?:#\s*\([^;]*?\)\s*)?(\w+)\s*\(',
            re.S,
        )
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
    groups: Optional[list] = None   # path-group stats when path_groups is on

# Standard flow: dfflibmap first, then ABC sees only combinational logic.
# This is what OpenLane does and what produces formally-verifiable netlists
# with 1:1 register correspondence to the RTL.
YOSYS_DRIVER_STD = """\
# generated yosys driver (standard combinational-ABC flow)
{pre_read_section}
{macro_lib_section}
{read_verilog_lines}
{param_flags}
hierarchy -top {module}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc {synth_flags}
{post_synth}
write_verilog -noattr {syn_netlist}
dfflibmap -liberty {liberty} {dont_use}
abc -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps} {dont_use}
setundef -zero
splitnets
opt_clean -purge
tee -o {stats_json} stat -liberty {liberty} -json
write_verilog -noattr -noexpr {out_netlist}
"""

# Path-group flow: same as STD, but the single abc call is replaced by one
# call per path group (tightest budget first) and a final call for the
# remaining reg->reg logic. Generated by _group_section().
YOSYS_DRIVER_GROUPS = """\
# generated yosys driver (path-group partitioned ABC)
{pre_read_section}
{macro_lib_section}
{read_verilog_lines}
{param_flags}
hierarchy -top {module}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc {synth_flags}
{post_synth}
write_verilog -noattr {syn_netlist}
dfflibmap -liberty {liberty} {dont_use}
{group_section}
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
{param_flags}
hierarchy -top {module}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc {synth_flags}
{post_synth}
write_verilog -noattr {syn_netlist}
# ABC with -dff: generic flops are part of the optimization
abc -dff -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps} {dont_use}
dfflibmap -liberty {liberty} {dont_use}
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
{param_flags}
hierarchy -top {module}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc {synth_flags}
{post_synth}
write_verilog -noattr {syn_netlist}
dfflibmap -liberty {liberty} {dont_use}
abc -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps} {dont_use}
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
{param_flags}
hierarchy -top {module}
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
{param_flags}
hierarchy -top {module}
{keep_hierarchy_section}
synth -top {module} -flatten -noabc {synth_flags}
{post_synth}
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
abc -dff -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps} {dont_use} @domain_1

# ABC on domain 2 (slow clock)
abc -dff -liberty {liberty} -constr {constr} -script {recipe} -D {period_ps_2} {dont_use} @domain_2

dfflibmap -liberty {liberty} {dont_use}
setundef -zero
splitnets
opt_clean -purge
tee -o {stats_json} stat -liberty {liberty} -json
write_verilog -noattr -noexpr {out_netlist}
"""

_ADDER_ARCHS = ('kogge-stone', 'han-carlson', 'sklansky')


def _variant_tag(variant) -> str:
    """Candidate-name suffix for a yosys_opts variant ('' for the plain front end)."""
    toks = [str(t).strip() for t in (variant or []) if str(t).strip()]
    return '+'.join(toks)


def _candidate_name(recipe: str, variant) -> str:
    tag = _variant_tag(variant)
    return f'{recipe}@{tag}' if tag else recipe


def _base_recipe(name: str) -> str:
    return name.split('@', 1)[0]


def _dont_use_flags(patterns) -> str:
    return ' '.join(f"-dont_use '{p}'" if any(ch in str(p) for ch in '*?[') else f'-dont_use {p}'
                    for p in (patterns or []) if str(p).strip())


_POST_SYNTH_TOKENS = {
    'opt_dff_sat': 'opt_dff -sat',        # SAT-based flop init/enable simplification after synth
    'opt_full':    'opt -full',           # extra opt round with full mux/expr optimisation
}


def _front_end(yosys_opts) -> tuple[str, str]:
    """(synth flags, post-synth commands) for a yosys_opts token list."""
    flags: list[str] = []
    post: list[str] = []
    for tok in yosys_opts or []:
        t = str(tok).strip().lower()
        if t == 'booth':
            flags.append('-booth')
        elif t.startswith('adder='):
            arch = t.split('=', 1)[1]
            if arch not in _ADDER_ARCHS:
                raise ValueError(f"yosys_opts: unknown adder architecture '{arch}' (choose from {_ADDER_ARCHS})")
            flags.append(f'-extra-map +/choices/{arch}.v')
        elif t in ('noshare', 'hieropt', 'nofsm', 'noalumacc'):
            flags.append(f'-{t}')
        elif t in _POST_SYNTH_TOKENS:
            post.append(_POST_SYNTH_TOKENS[t])
        elif t:
            raise ValueError(f"yosys_opts: unknown token '{tok}' (booth, adder=<arch>, noshare, hieropt, "
                             f"nofsm, noalumacc, {', '.join(_POST_SYNTH_TOKENS)})")
    if post:
        # post-synth optimisation can re-create coarse cells ($mux, ...); lower them again
        post += ['techmap', 'opt -fast']
    return ' '.join(flags), '\n'.join(post)


def _synth_flags(yosys_opts) -> str:
    """Translate cfg.yosys_opts tokens into `synth` flags (post-synth passes excluded)."""
    return _front_end(yosys_opts)[0]


def _fe_variants_for(cfg_or_dict, module: str) -> list[list[str]]:
    """Front-end variants for `module`. `yosys_opts_sweep` / `yosys_opts` may be
    global (list) or per module (dict keyed by module name, '*' as default)."""
    get = (lambda k: cfg_or_dict.get(k)) if isinstance(cfg_or_dict, dict) else (lambda k: getattr(cfg_or_dict, k))
    sweep, opts = get('yosys_opts_sweep'), get('yosys_opts')
    if isinstance(sweep, dict):
        sweep = sweep.get(module, sweep.get('*', []))
    if isinstance(opts, dict):
        opts = opts.get(module, opts.get('*', []))
    return [list(v or []) for v in (sweep or [])] or [list(opts or [])]


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


def _cfg_get(cfg, key, default=None):
    return getattr(cfg, key, default) if hasattr(cfg, 'lib_typ') else cfg.get(key, default)


def _synth_libs(cfg) -> list[str]:
    """All liberty files for synthesis: the primary (`_synth_lib`) plus the
    extra standard-cell libraries of the same corner."""
    if _cfg_get(cfg, 'lib_synth'):
        return [_cfg_get(cfg, 'lib_synth'), *(_cfg_get(cfg, 'lib_synth_extra') or [])]
    extra = _cfg_get(cfg, 'lib_extra') or {}
    if _cfg_get(cfg, 'lib_slow'):
        return [_cfg_get(cfg, 'lib_slow'), *extra.get('slow', [])]
    return [_cfg_get(cfg, 'lib_typ'), *extra.get('typ', [])]


def _liberty_arg(cfg) -> str:
    """Value for `-liberty {liberty}` in the Yosys templates; several files
    become `a.lib -liberty b.lib` (dfflibmap, abc and stat accept repeats)."""
    return ' -liberty '.join(_synth_libs(cfg))


def _postpass_libs(cfg) -> dict:
    """Liberty sets for the post-pass: `primary` (the mapping liberty), `std`
    (every other standard-cell library of the setup corner, counted in area),
    `std_fast` (same at the fast corner), `macro` / `macro_fast` (hard macros:
    timing and pins only). No duplicates."""
    primary = _synth_lib(cfg)
    corner = 'slow' if _cfg_get(cfg, 'lib_slow') else 'typ'
    extra = _cfg_get(cfg, 'lib_extra') or {}
    macro = _cfg_get(cfg, 'macro_libs') or {}
    std = [l for l in dict.fromkeys([_cfg_get(cfg, 'lib_slow') or _cfg_get(cfg, 'lib_typ')] + list(extra.get(corner, [])))
           if l and l != primary]
    std_fast = [l for l in dict.fromkeys(list(extra.get('fast', []))) if l and l != _cfg_get(cfg, 'lib_fast')]
    return {'primary': primary, 'std': std, 'std_fast': std_fast,
            'macro': list(macro.get(corner, [])), 'macro_fast': list(macro.get('fast', []))}


def _extra_libs(cfg, corner: str) -> list[str]:
    """Extra standard-cell libraries plus hard-macro libraries of a corner:
    everything OpenSTA / the post-pass must read besides the primary liberty."""
    extra = _cfg_get(cfg, 'lib_extra') or {}
    macro = _cfg_get(cfg, 'macro_libs') or {}
    return list(extra.get(corner, [])) + list(macro.get(corner, []))


def _pre_read_section(cfg: dict) -> str:
    lines = []
    if cfg.get('pre_read_files'):
        lines.extend(f'read_liberty -lib {l}' for l in _synth_libs(cfg))
        for f in cfg['pre_read_files']:
            lines.append(f'read_verilog -sv {f}')
    return '\n'.join(lines)


def _liberty_lib_section(cfg: dict) -> str:
    """Load the standard cell library (or libraries) as Yosys -lib (blackbox)
    libraries. Used by the hierarchical driver before reading pre-synthesised
    sub-module netlists, which reference std-cell names directly. Without
    this, Yosys aborts on the first std-cell reference."""
    return '\n'.join(f'read_liberty -lib {l}' for l in _synth_libs(cfg) if l)


def _macro_lib_yosys_section(cfg: dict, corner: str = 'typ') -> str:
    """Emit `read_liberty -lib <macro.lib>` lines for Yosys so each macro
    is recognised as a blackbox cell during synth/dfflibmap. Empty if no
    macro liberty is configured."""
    libs = _extra_libs(cfg, corner)
    if not libs:
        return ''
    return '\n'.join(f'read_liberty -lib {f}' for f in libs)


def _macro_lib_sta_section(cfg, corner: str) -> str:
    """Emit `read_liberty -corner <corner> <macro.lib>` lines for OpenSTA
    so timing arcs through hard macros are honoured at this corner."""
    libs = _extra_libs(cfg, corner)
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
    libs = _extra_libs(cfg, 'typ')
    if not libs:
        return ''
    return '\n'.join(f'read_liberty {f}' for f in libs)

def _keep_hierarchy_section(cfg: dict) -> str:
    lines = []
    for m in cfg.get('keep_hierarchy_modules', []):
        lines.append(f'setattr -mod -set keep_hierarchy 1 {m}')
    return '\n'.join(lines)

def _param_flags(params: dict, module: str) -> str:
    """Generate Yosys `chparam -set` commands for the given module.
    Emits one command per parameter. We use the standalone `chparam`
    command rather than `hierarchy -chparam` because the latter cannot
    decode Verilog string literals (e.g. SBOX_IMPL="LOGIC")."""
    if not params or module not in params:
        return ''
    return '\n'.join(
        f'chparam -set {k} {v} {module}'
        for k, v in params[module].items()
    )

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

_SIGNED_DECL_RE = re.compile(r'^(\s*(?:input|output|inout|wire|reg)\s+)signed\s+', re.M)


def _strip_signed_decls(netlist: Path) -> int:
    """Remove `signed` from port/wire declarations in a gate-level netlist.

    Yosys keeps the RTL signedness on ports (`input signed [11:0] x;`).
    OpenSTA's Verilog reader rejects that syntax ("syntax error"), and
    signedness carries no meaning in a mapped netlist. Returns the number of
    declarations rewritten."""
    try:
        text = netlist.read_text()
    except OSError:
        return 0
    new_text, n = _SIGNED_DECL_RE.subn(r'\1', text)
    if n:
        netlist.write_text(new_text)
    return n


_UNMAPPED_RE = re.compile(r'^\s*\\?\$[\w$]+\s', re.M)


def _count_unmapped(netlist: Path) -> int:
    """Number of Yosys internal cells ($mux, $_AND_, ...) left in a mapped netlist.
    Anything non-zero means the mapping is incomplete; stat and OpenSTA would
    silently ignore those cells and report an optimistic, wrong result."""
    try:
        return len(_UNMAPPED_RE.findall(netlist.read_text(errors='ignore')))
    except OSError:
        return 0


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


# ============================================================================
# Path groups (Phase 2): budgets from SDC + liberty, Yosys selections per group
# ============================================================================

_DEFAULT_ASYNC_RESETS = {'PRESETn', 'PRESETN', 'aresetn', 'HRESETn', 'hresetn', 'rst_n', 'resetn'}


def resolve_abc_target(cfg: Config) -> tuple[int, str]:
    """Return (D_ps, note) for ABC's -D from cfg.abc_target."""
    T = cfg.period_ps
    mode = str(cfg.abc_target).strip().lower()
    if mode in ('', 'none', 'off', 'best'):
        return 0, 'none: ABC minimum-delay mapping, {D} removed from recipes'
    if mode == 'period':
        return T, 'full period'
    if mode == 'reg2reg':
        if liberty_timing is None:
            return T, 'reg2reg requested but liberty_timing unavailable; full period'
        lt = liberty_timing.read_liberty_timing(_synth_lib(asdict(cfg)))
        if lt.t_cq_ps is None or lt.t_su_ps is None:
            return T, 'reg2reg requested but no flop timing in liberty; full period'
        d = T - lt.t_cq_ps - lt.t_su_ps - cfg.clock_uncertainty_setup_ps
        floor = int(cfg.min_budget_frac * T)
        if d < floor:
            return floor, (f'reg2reg budget {d:.0f} ps below floor; using {floor} ps '
                           f'(t_cq={lt.t_cq_ps:.0f}, t_su={lt.t_su_ps:.0f}, unc={cfg.clock_uncertainty_setup_ps})')
        return int(d), (f'T - t_cq {lt.t_cq_ps:.0f} - t_su {lt.t_su_ps:.0f} - '
                        f'unc {cfg.clock_uncertainty_setup_ps} ({Path(lt.path).name}, ref {lt.reference_flop})')
    try:
        return int(float(mode)), 'explicit'
    except ValueError:
        return T, f"unknown abc_target '{cfg.abc_target}'; full period"


def _ff_rules(flop_cells: list[str]) -> str:
    """`%co*` / `%ci*` rule suffix that refuses to traverse flop cells."""
    return ''.join(f':-{c}' for c in flop_cells)


def build_path_groups(cfg: Config, module: str, constraints, ports: dict[str, str],
                      lt, group_dir: Path) -> Optional[dict]:
    """Compute the path groups for `module`.

    Returns {'groups': [...], 'reg2reg_ps': int, 'ff_rules': str, 'notes': [...]}
    or None when nothing useful can be derived (no ports known, no flops in
    the liberty). Each group: {name, kind, budget_ps, ports, constr}.
    Groups are returned sorted by budget (tightest first); overlaps are
    resolved by that order at mapping time."""
    if not ports or lt is None or not lt.flop_cells:
        return None
    T = cfg.period_ps
    unc = cfg.clock_uncertainty_setup_ps
    tcq = lt.t_cq_ps or 0.0
    tsu = lt.t_su_ps or 0.0
    floor = int(cfg.min_budget_frac * T)
    notes: list[str] = []

    def clamp(x: float) -> int:
        return int(max(floor, min(x, cfg.relaxed_factor * T)))

    clock_ports = {cfg.clock_port}
    if cfg.clock_port_2:
        clock_ports.add(cfg.clock_port_2)
    fp_ports: set[str] = set()
    if constraints is not None:
        clock_ports |= set(constraints.clocks_on_ports())
        fp_ports |= {p for p in constraints.false_path_ports() if p in ports}
    fp_ports |= {p for p in ports if p in _DEFAULT_ASYNC_RESETS}

    default_io = cfg.io_delay_frac * T
    in_delay: dict[str, float] = {}
    out_delay: dict[str, float] = {}
    for pname, d in ports.items():
        if pname in clock_ports or pname in fp_ports:
            continue
        if d in ('input', 'inout'):
            dly = None
            if constraints is not None:
                io = constraints.input_delay_for(pname)
                if io is not None and io.max_ns is not None:
                    dly = io.max_ns * 1000.0
            in_delay[pname] = default_io if dly is None else dly
        if d in ('output', 'inout'):
            dly = None
            if constraints is not None:
                io = constraints.output_delay_for(pname)
                if io is not None and io.max_ns is not None:
                    dly = io.max_ns * 1000.0
            out_delay[pname] = default_io if dly is None else dly

    group_dir.mkdir(parents=True, exist_ok=True)
    groups: list[dict] = []

    def add(name: str, kind: str, budget: float, plist: list[str], driving: str, load_ff: float):
        if not plist:
            return
        constr = group_dir / f'{name}.constr'
        constr.write_text(f'set_driving_cell {driving}\nset_load {load_ff}\n')
        groups.append({'name': name, 'kind': kind, 'budget_ps': clamp(budget),
                       'raw_budget_ps': int(budget), 'ports': sorted(plist), 'constr': str(constr)})

    # in -> out (combinational feed-through): tightest, conservative on delays
    if in_delay and out_delay:
        add('in2out', 'in2out', T - max(in_delay.values()) - max(out_delay.values()),
            sorted(in_delay), cfg.driving_cell, cfg.load_ff)
        groups[-1]['out_ports'] = sorted(out_delay)

    # in -> reg, one group per distinct input delay
    by_val: dict[float, list[str]] = {}
    for pn, v in in_delay.items():
        by_val.setdefault(round(v, 1), []).append(pn)
    for v, plist in sorted(by_val.items(), key=lambda kv: -kv[0]):
        drv = cfg.driving_cell
        if constraints is not None:
            for pn in plist:
                dc = constraints.driving_cell_for(pn)
                if dc:
                    drv = dc
                    break
        add(f'in_{int(v)}ps', 'in2reg', T - v - tsu - unc, plist, drv, cfg.load_ff)

    # reg -> out, one group per distinct output delay
    by_val = {}
    for pn, v in out_delay.items():
        by_val.setdefault(round(v, 1), []).append(pn)
    for v, plist in sorted(by_val.items(), key=lambda kv: -kv[0]):
        load = cfg.load_ff
        if constraints is not None:
            for pn in plist:
                lf = constraints.load_for(pn)
                if lf is not None:
                    load = round(lf * 1000.0, 3)
                    break
        add(f'out_{int(v)}ps', 'reg2out', T - tcq - v - unc, plist, cfg.driving_cell, load)

    # false-path / async-reset cones: relaxed
    if fp_ports:
        add('relaxed', 'relaxed', cfg.relaxed_factor * T, sorted(fp_ports), cfg.driving_cell, cfg.load_ff)

    reg2reg = clamp(T - tcq - tsu - unc)
    if T - tcq - tsu - unc < floor:
        notes.append(f'reg2reg budget {int(T - tcq - tsu - unc)} ps below floor {floor} ps; using floor')
    groups.sort(key=lambda g: g['budget_ps'])
    notes.append(f'liberty {Path(lt.path).name}: t_cq={tcq:.0f} ps t_su={tsu:.0f} ps (ref {lt.reference_flop})')
    return {'groups': groups, 'reg2reg_ps': reg2reg, 'ff_rules': _ff_rules(lt.flop_cells),
            'notes': notes, 'period_ps': T}


def _sel(ports: list[str], prefix: str) -> str:
    return ' '.join(f'{prefix}:{p}' for p in ports)


def _group_section(spec: dict, liberty: str, constr_default: str, recipe: str,
                   groups_txt: str, dont_use: str = '') -> str:
    """Yosys script lines: one abc per group (tightest first), then reg->reg."""
    R = spec['ff_rules']
    L = [f'# path groups: {len(spec["groups"])} + reg2reg (budgets in ps)',
         f'tee -q -o {groups_txt} log PERIOD {spec["period_ps"]}']
    for g in spec['groups']:
        n = g['name']
        if g['kind'] == 'in2out':
            expr = (f'{_sel(g["ports"], "i")} %co*{R} '
                    f'{_sel(g.get("out_ports", []), "o")} %ci*{R} %i t:$_* %i')
        elif g['kind'] == 'reg2out':
            expr = f'{_sel(g["ports"], "o")} %ci*{R} t:$_* %i'
        else:  # in2reg, relaxed
            expr = f'{_sel(g["ports"], "i")} %co*{R} t:$_* %i'
        L.append(f'select -set g_{n} {expr}')
        L.append(f'tee -q -a {groups_txt} log GROUP {n} {g["kind"]} {g["budget_ps"]}')
        L.append(f'tee -q -a {groups_txt} select -count @g_{n}')
        L.append(f'abc -liberty {liberty} -constr {g["constr"]} -script {recipe} -D {g["budget_ps"]} {dont_use} @g_{n}')
    L.append(f'tee -q -a {groups_txt} log GROUP reg2reg reg2reg {spec["reg2reg_ps"]}')
    L.append(f'tee -q -a {groups_txt} select -count t:$_*')
    L.append(f'abc -liberty {liberty} -constr {constr_default} -script {recipe} -D {spec["reg2reg_ps"]} {dont_use} t:$_*')
    return '\n'.join(L)


def _parse_groups_txt(path: Path) -> list[dict]:
    """GROUP/count pairs written by the group section -> [{name, kind, budget_ps, cells}]."""
    out: list[dict] = []
    try:
        lines = path.read_text(errors='ignore').splitlines()
    except OSError:
        return out
    cur = None
    for ln in lines:
        m = re.match(r'GROUP\s+(\S+)\s+(\S+)\s+(\d+)', ln)
        if m:
            cur = {'name': m.group(1), 'kind': m.group(2), 'budget_ps': int(m.group(3)), 'cells': None}
            out.append(cur)
            continue
        m = re.match(r'\s*(\d+)\s+objects', ln)
        if m and cur is not None and cur['cells'] is None:
            cur['cells'] = int(m.group(1))
    return out


def _materialize_recipe(recipe_path: str | Path, d_ps: int, out_dir: Path, wire_load: bool = False) -> Path:
    """Write a copy of the recipe with `{D}` replaced by `-D <d_ps>`.

    Yosys only substitutes {D} in inline (`-script +...`) scripts; a script
    file is passed to ABC with `source <file>` untouched, so `&nf {D}`,
    `upsize {D}` and `dnsize {D}` reached ABC literally. d_ps <= 0 removes
    {D} (minimum-delay mapping, the measured best default). The copy lives
    next to the netlist so a run directory is self-describing."""
    src = Path(recipe_path)
    text = src.read_text()
    d_ps = int(d_ps or 0)
    text = text.replace('{D}', f'-D {d_ps}' if d_ps > 0 else '')
    if wire_load:
        text = re.sub(r'\b(buffer|upsize|dnsize|stime)\b(?! -c)', r'\1 -c', text)
    out = out_dir / (f'{src.stem}.D{d_ps}.abc' if d_ps > 0 else f'{src.stem}.noD.abc')
    if wire_load:
        out = out.with_suffix('.wl.abc')
    out.write_text(text)
    return out


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

    variant = args.get('variant')
    cfg = dict(cfg)
    cfg['yosys_opts'] = list(variant) if variant is not None else _fe_variants_for(cfg, module)[0]
    groups_spec = args.get('groups')
    groups_txt = workdir / f'{recipe}.groups.txt'
    d_ps = int(cfg.get('abc_d_ps', cfg['period_ps']))
    recipe_path = str(_materialize_recipe(recipe_path, d_ps, workdir, bool(cfg.get('abc_wire_load'))))
    if dep_netlists:
        template = YOSYS_DRIVER_HIER
    elif cfg.get('abc_sequential', False):
        template = YOSYS_DRIVER_SEQ
    elif cfg.get('clock_port_2') and cfg.get('dual_clock_synthesis', False):
        template = YOSYS_DRIVER_DUAL_CLK
    elif groups_spec:
        template = YOSYS_DRIVER_GROUPS
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
        liberty=_liberty_arg(cfg),
        constr=constr,
        recipe=recipe_path,
        period_ps=cfg.get('abc_d_ps', cfg['period_ps']),
        period_ps_2=cfg.get('period_ps_2', cfg['period_ps']),
        clock_port=cfg['clock_port'],
        clock_port_2=cfg.get('clock_port_2', ''),
        param_flags=_param_flags(cfg.get('params', {}), module),
        stats_json=stats,
        syn_netlist=syn_nl,
        out_netlist=netlist,
        group_section=(_group_section(groups_spec, _liberty_arg(cfg), constr, recipe_path, str(groups_txt),
                                      _dont_use_flags(cfg.get('dont_use')))
                       if groups_spec else ''),
        synth_flags=_front_end(cfg.get('yosys_opts'))[0],
        post_synth=_front_end(cfg.get('yosys_opts'))[1],
        dont_use=_dont_use_flags(cfg.get('dont_use')),
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
        _strip_signed_decls(netlist)
        n_unmapped = _count_unmapped(netlist)
        if n_unmapped:
            return RecipeResult(
                module=module, recipe=recipe, success=False,
                runtime_s=runtime, netlist=None, log=str(log),
                error=f"{n_unmapped} unmapped generic cell(s) left in the netlist",
            )
        cells, area = _read_stats(stats, module)
        gstats = _parse_groups_txt(groups_txt) if groups_spec else None
        if gstats is not None:
            (workdir / f'{recipe}.groups.json').write_text(json.dumps(
                {'period_ps': groups_spec['period_ps'], 'notes': groups_spec['notes'],
                 'groups': gstats}, indent=2))
        return RecipeResult(
            module=module, recipe=recipe, success=True,
            runtime_s=runtime, netlist=str(netlist),
            stats_json=str(stats), log=str(log),
            cells=cells, area=area, groups=gstats,
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

# Common async-reset port names (APB / AHB / generic). Applied via catch so
# designs that lack a given port are unaffected. PRESETn is the nc_lib
# convention and was previously missing (WNS dominated by reset recovery).
_ASYNC_RESET_FALSE_PATHS = """\
# Async-reset false paths, derived from the netlist: an input port whose
# fanout ends only at register async pins (RESET_B/SET_B...) is a reset
# distribution net; recovery/removal on it is not a synthesis objective.
# The name list is a fallback for ports the derivation misses (e.g. a reset
# that also feeds synchronous logic and is still meant to be excluded).
set _async_pins {}
foreach _rp [all_registers -async_pins] { lappend _async_pins [get_full_name $_rp] }
set _async_resets {PRESETn PRESETN aresetn HRESETn hresetn rst_n resetn}
foreach _p [all_inputs -no_clocks] {
    set _pn [get_full_name $_p]
    set _is_reset [expr {[lsearch -exact $_async_resets $_pn] >= 0}]
    if {!$_is_reset && [llength $_async_pins] > 0} {
        set _ends {}
        catch { set _ends [get_fanout -from $_p -endpoints_only -flat] }
        if {[llength $_ends] > 0} {
            set _is_reset 1
            foreach _e $_ends {
                if {[lsearch -exact $_async_pins [get_full_name $_e]] < 0} { set _is_reset 0; break }
            }
        }
    }
    if {$_is_reset} { set_false_path -from $_p }
}"""

def _default_wire_load(liberty: str) -> Optional[str]:
    """Return the liberty `default_wire_load` name, or None."""
    try:
        text = Path(liberty).read_text(errors='ignore')[:400000]
    except OSError:
        return None
    m = re.search(r'default_wire_load\s*:\s*"?([A-Za-z0-9_]+)"?', text)
    return m.group(1) if m else None


def _wire_load_section(mode: str, liberty: str) -> str:
    """`set_wire_load_mode top` + model. mode: auto | none | <model name>."""
    if not mode or mode == 'none':
        return ''
    name = _default_wire_load(liberty) if mode == 'auto' else mode
    if not name:
        return ''
    return f'set_wire_load_mode top\nset_wire_load_model -name {name}\n'


def _sta_constraints(*, clock_port: str, period_ns: float,
                     clock_port_2: Optional[str] = None,
                     period_2_ns: Optional[float] = None,
                     unc_setup_ns: float = 0.25, unc_hold_ns: float = 0.10,
                     user_sdc: Optional[str] = None,
                     driving_cell: Optional[str] = None,
                     load_pf: Optional[float] = None,
                     wire_load_section: str = '',
                     io_delay_frac: float = 0.2, io_delay_min_frac: float = 0.4) -> str:
    """Constraint preamble shared by quick STA and multi-corner STA so that
    winner ranking and the final report see the same model. Order: clocks,
    uncertainty, default driving cell / load / wire load / I/O delays,
    async-reset false paths, then the user SDC last so it overrides."""
    L = [f'create_clock -name {clock_port} -period {period_ns} [get_ports {clock_port}]']
    if clock_port_2 and period_2_ns:
        L.append(f'create_clock -name {clock_port_2} -period {period_2_ns} [get_ports {clock_port_2}]')
        L.append(f'set_clock_groups -asynchronous -group [get_clocks {clock_port}] '
                 f'-group [get_clocks {clock_port_2}]')
    L.append(f'set_clock_uncertainty -setup {unc_setup_ns} [all_clocks]')
    L.append(f'set_clock_uncertainty -hold {unc_hold_ns} [all_clocks]')
    if driving_cell:
        L.append(f'set_driving_cell -lib_cell {driving_cell} [all_inputs -no_clocks]')
    if load_pf is not None:
        L.append(f'set_load {load_pf} [all_outputs]')
    if wire_load_section:
        L.append(wire_load_section.rstrip())
    io = round(period_ns * io_delay_frac, 4)
    io_min = round(io * io_delay_min_frac, 4)
    L.append(f'set_input_delay  -clock {clock_port} -max {io} [all_inputs -no_clocks]')
    L.append(f'set_input_delay  -clock {clock_port} -min {io_min} [all_inputs -no_clocks]')
    L.append(f'set_output_delay -clock {clock_port} -max {io} [all_outputs]')
    L.append(f'set_output_delay -clock {clock_port} -min {io_min} [all_outputs]')
    L.append(_ASYNC_RESET_FALSE_PATHS.rstrip())
    if user_sdc:
        # Last, so per-port delays, driving cells, loads and exceptions in the
        # user SDC override the defaults above (later set_* replaces earlier).
        L.append('# User-supplied SDC (overrides defaults above)')
        L.append(f'source {user_sdc}')
    return '\n'.join(L) + '\n'


QSTA_TCL = """\
read_liberty {liberty}
{macro_lib_section}
read_verilog {netlist}
link_design {module}
{constraints}
# Prefer report_worst_slack: report_wns can print 0.00 even when paths have slack.
report_worst_slack -max -digits 4
report_tns -max -digits 4
exit
"""

def _quick_sta(opensta: str, liberty: str, netlist: str, module: str,
               period_ps: int, clock_port: str, log_path: Path,
               clock_port_2: str = None, period_ps_2: int = None,
               macro_libs: list = None,
               sdc: Optional[str] = None,
               driving_cell: Optional[str] = None,
               load_ff: Optional[float] = None,
               unc_setup_ps: int = 250, unc_hold_ps: int = 100,
               wire_load_model: str = 'auto',
               io_delay_frac: float = 0.2, io_delay_min_frac: float = 0.4) -> tuple[Optional[float], Optional[float]]:
    """Run a quick STA (ranking corner). Returns (wns_ns, tns_ns) or (None, None).
    Uses the same constraint preamble as the multi-corner STA."""
    period_ns = period_ps / 1000.0
    macro_lib_section = ''
    if macro_libs:
        macro_lib_section = '\n'.join(f'read_liberty {f}' for f in macro_libs)
    constraints = _sta_constraints(
        clock_port=clock_port, period_ns=period_ns,
        clock_port_2=clock_port_2,
        period_2_ns=(period_ps_2 / 1000.0) if (clock_port_2 and period_ps_2) else None,
        unc_setup_ns=unc_setup_ps / 1000.0, unc_hold_ns=unc_hold_ps / 1000.0,
        user_sdc=sdc, driving_cell=driving_cell,
        load_pf=(load_ff / 1000.0) if load_ff is not None else None,
        wire_load_section=_wire_load_section(wire_load_model, liberty),
        io_delay_frac=io_delay_frac, io_delay_min_frac=io_delay_min_frac,
    )
    with tempfile.NamedTemporaryFile('w', suffix='.tcl', delete=False) as f:
        f.write(QSTA_TCL.format(
            liberty=liberty, netlist=netlist, module=module,
            macro_lib_section=macro_lib_section,
            constraints=constraints,
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
            # report_worst_slack -max → "worst slack <n>"
            m = re.search(r'^worst slack(?:\s+(?:max|min))?\s+([-0-9.eE+]+)', line, re.I)
            if m and wns is None:
                wns = float(m.group(1))
                continue
            # Fallback: report_wns → "wns <n>"
            m = re.search(r'^wns\s+([-0-9.eE+]+)', line, re.I)
            if m and wns is None:
                wns = float(m.group(1))
                continue
            m = re.search(r'(?:^tns(?:\s+(?:max|min))?|total negative slack)\s+([-0-9.eE+]+)', line, re.I)
            if m and tns is None:
                tns = float(m.group(1))
        return wns, tns
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    finally:
        os.unlink(tcl)

def _quick_sta_job(args: dict) -> tuple[str, Optional[float], Optional[float]]:
    """Pool worker: one quick STA. Returns (recipe, wns_ns, tns_ns)."""
    cfg = args['cfg']
    corner = 'slow' if cfg.get('lib_slow') else 'typ'
    wns, tns = _quick_sta(
        cfg['opensta'], cfg.get('lib_slow') or cfg['lib_typ'], args['netlist'], args['module'],
        cfg['period_ps'], cfg['clock_port'], Path(args['log']),
        clock_port_2=cfg.get('clock_port_2'), period_ps_2=cfg.get('period_ps_2'),
        macro_libs=_extra_libs(cfg, corner),
        sdc=cfg.get('sdc'), driving_cell=cfg['driving_cell'], load_ff=cfg['load_ff'],
        unc_setup_ps=cfg['clock_uncertainty_setup_ps'], unc_hold_ps=cfg['clock_uncertainty_hold_ps'],
        wire_load_model=cfg['wire_load_model'], io_delay_frac=cfg['io_delay_frac'],
        io_delay_min_frac=cfg.get('io_delay_min_frac', 0.4))
    return args['recipe'], wns, tns


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
    base = _base_recipe(recipe)
    return RECIPE_PRIORITY.index(base) if base in RECIPE_PRIORITY else 999

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

def _pareto_knee(cands: list[Candidate], clip_ns: Optional[float] = None) -> Optional[Candidate]:
    """Knee of the WNS/area Pareto front: the front point closest to the utopia
    (best WNS, least area) after normalizing both axes over the front.
    Candidates more than `clip_ns` below the best WNS are ignored so a few
    hopeless points do not compress the WNS axis. None if the front has fewer
    than three points (nothing to trade off)."""
    valid = [c for c in cands if c.wns_ns is not None]
    if not valid:
        return None
    best_w = max(c.wns_ns for c in valid)
    if clip_ns is not None and clip_ns > 0:
        valid = [c for c in valid if c.wns_ns >= best_w - clip_ns]
    front = [c for c in valid if not any(
        (o.area <= c.area and o.wns_ns >= c.wns_ns) and (o.area < c.area or o.wns_ns > c.wns_ns)
        for o in valid if o is not c)]
    if len(front) < 3:
        return None
    w_lo, w_hi = min(c.wns_ns for c in front), max(c.wns_ns for c in front)
    a_lo, a_hi = min(c.area for c in front), max(c.area for c in front)
    if w_hi - w_lo <= 1e-9 or a_hi - a_lo <= 1e-9:
        return None
    def dist(c):
        wn = (w_hi - c.wns_ns) / (w_hi - w_lo)      # 0 = fastest
        an = (c.area - a_lo) / (a_hi - a_lo)        # 0 = smallest
        return (wn * wn + an * an) ** 0.5
    return min(front, key=lambda c: (dist(c), -c.wns_ns, _stability_idx(c.recipe)))


def select_winner(cands: list[Candidate], objective: str = 'balanced',
                  margin_ns: float = 0.0, fallback: str = 'knee',
                  period_ns: Optional[float] = None) -> Selection:
    """One rule for every objective: the candidate that meets timing
    (WNS >= margin) with the least area; if none meets, the knee of the
    WNS/area Pareto front (`fallback='knee'`) or the best WNS (`'best_wns'`).
    `objective` only labels the selection; it chose the recipe set upstream.
    Ties break on RECIPE_PRIORITY."""
    valid = [c for c in cands if c.wns_ns is not None]
    front = _pareto_front(cands) if valid else []
    if not valid:
        succeeded = [c for c in cands if c.netlist]
        if succeeded:
            winner = min(succeeded, key=lambda c: (c.area, _stability_idx(c.recipe)))
            return Selection(module='', objective=objective, winner=winner.recipe, candidates=cands,
                             rationale=f'no WNS data; fallback to min area ({winner.area:.1f} um²)')
        return Selection(module='', objective=objective, winner=None, candidates=cands,
                         rationale='no valid candidates (all failed synthesis or STA)')
    meeting = [c for c in valid if c.wns_ns >= margin_ns]
    if meeting:
        winner = min(meeting, key=lambda c: (c.area, -c.wns_ns, _stability_idx(c.recipe)))
        rationale = (f'min area among {len(meeting)}/{len(valid)} candidates meeting timing '
                     f'(WNS={winner.wns_ns:+.3f} ns, margin {margin_ns} ns): {winner.area:.1f} um²')
    else:
        best = max(valid, key=lambda c: (c.wns_ns, -c.area, -_stability_idx(c.recipe)))
        winner, how = best, 'best WNS'
        if fallback == 'knee':
            knee = _pareto_knee(valid, clip_ns=period_ns)
            if knee is not None:
                winner, how = knee, 'knee of the WNS/area front'
        rationale = (f'no candidate meets timing; {how} ({winner.wns_ns:+.3f} ns, {winner.area:.1f} um²); '
                     f'fastest was {best.wns_ns:+.3f} ns at {best.area:.1f} um²')
    return Selection(module='', objective=objective, winner=winner.recipe, candidates=cands,
                     pareto_front=front, rationale=rationale)


# ============================================================================
# Corner STA on winner
# ============================================================================

# One OpenSTA session per corner, each with a single liberty: the same script
# shape as the ranking STA. OpenSTA's multi-corner mode (`define_corners`)
# estimates wire loads slightly differently from a single-library session
# (about 30 ps on a 10 ns design, docs/architecture.md), which made the report
# disagree with the ranking. Running the corners separately removes that.
CORNER_SESSION_TCL = """\
read_liberty {liberty}
{macro_libs}
read_verilog {netlist}
link_design {module}
{constraints}
puts ">>> SETUP_BEGIN"
report_checks -path_delay max -group_path_count 5 -format full_clock
report_worst_slack -max -digits 4
report_tns -max -digits 4
puts ">>> SETUP_END"
puts ">>> HOLD_BEGIN"
report_checks -path_delay min -group_path_count 5 -format full_clock
report_worst_slack -min -digits 4
report_tns -min -digits 4
puts ">>> HOLD_END"
puts ">>> GROUPS_BEGIN"
puts "GROUPS SETUP"
report_checks -path_delay max -group_path_count 1 -format slack_only -digits 4
puts "GROUPS HOLD"
report_checks -path_delay min -group_path_count 1 -format slack_only -digits 4
puts ">>> GROUPS_END"
{sdf_line}
exit
"""


def _parse_group_slacks(section: str) -> dict:
    """`report_checks -format slack_only` prints `<group> <slack>` per path
    group (one per clock, plus async/unconstrained). -> {'setup': {g: s}, 'hold': {g: s}}"""
    out = {'setup': {}, 'hold': {}}
    mode = None
    for line in section.splitlines():
        if line.startswith('GROUPS SETUP'):
            mode = 'setup'; continue
        if line.startswith('GROUPS HOLD'):
            mode = 'hold'; continue
        # group names may contain spaces ("path delay", "**async_default**"):
        # the slack is the last column, everything before it is the name
        m = re.match(r'^(.*\S)\s+(-?[0-9]+\.[0-9]+)\s*$', line.strip())
        if mode and m and m.group(1) != 'Group' and not set(m.group(1)) <= {'-'}:
            out[mode][m.group(1)] = float(m.group(2))
    return out

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
    # worst slack per path group (one per clock) at the sign-off corners
    groups_setup_slow: dict = field(default_factory=dict)
    groups_hold_fast: dict = field(default_factory=dict)

def run_corner_sta(cfg: Config, module: str, netlist: Path,
                   results_dir: Path) -> CornerResult:
    """Multi-corner STA as three single-library sessions (slow: setup + SDF,
    typical: setup and hold, fast: hold). Numbers are parsed from the section
    markers; the slow-corner setup WNS equals the ranking STA by construction."""
    out_dir = results_dir / module
    out_dir.mkdir(parents=True, exist_ok=True)
    sdf = out_dir / 'winner.sdf'
    rpt = out_dir / 'sta.rpt'
    log = out_dir / 'sta.log'

    period_ns = cfg.period_ps / 1000.0
    corners = [('slow', cfg.lib_slow, 'slow'), ('typical', cfg.lib_typ, 'typ'), ('fast', cfg.lib_fast, 'fast')]
    res = CornerResult(module=module, success=True, report_path=str(rpt))
    report_parts, log_parts = [], []

    def grab(text, pattern):
        m = re.search(pattern, text, re.M | re.I)
        return float(m.group(1)) if m else None

    for name, lib, key in corners:
        if not lib:
            continue
        constraints = _sta_constraints(
            clock_port=cfg.clock_port, period_ns=period_ns,
            clock_port_2=cfg.clock_port_2,
            period_2_ns=(cfg.period_ps_2 / 1000.0) if (cfg.clock_port_2 and cfg.period_ps_2) else None,
            unc_setup_ns=cfg.clock_uncertainty_setup_ps / 1000.0,
            unc_hold_ns=cfg.clock_uncertainty_hold_ps / 1000.0,
            user_sdc=cfg.sdc, driving_cell=cfg.driving_cell,
            load_pf=cfg.load_ff / 1000.0,  # OpenSTA wants pF
            wire_load_section=_wire_load_section(cfg.wire_load_model, lib),
            io_delay_frac=cfg.io_delay_frac, io_delay_min_frac=cfg.io_delay_min_frac,
        )
        macro = _extra_libs(cfg, key)
        tcl = out_dir / f'sta_{name}.tcl'
        tcl.write_text(CORNER_SESSION_TCL.format(
            liberty=lib, macro_libs='\n'.join(f'read_liberty {f}' for f in macro),
            netlist=netlist, module=module, constraints=constraints,
            sdf_line=(f'write_sdf {sdf}' if name == 'slow' else ''),
        ))
        try:
            r = subprocess.run([cfg.opensta, '-no_init', '-exit', str(tcl)],
                               capture_output=True, text=True, timeout=600)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return CornerResult(module=module, success=False, error=f'{name}: {e}')
        out = r.stdout
        log_parts.append(f'##### corner {name} ({lib})\n{out}\n--- stderr ---\n{r.stderr}')
        report_parts.append(f'##### corner {name} ({lib})\n{out}')
        if r.returncode != 0:
            res.success = False
            res.error = (res.error or '') + f'opensta exit {r.returncode} at {name}; '
            continue
        setup = out[out.find('>>> SETUP_BEGIN'):out.find('>>> SETUP_END')]
        hold = out[out.find('>>> HOLD_BEGIN'):out.find('>>> HOLD_END')]
        groups = _parse_group_slacks(out[out.find('>>> GROUPS_BEGIN'):out.find('>>> GROUPS_END')])
        ws = grab(setup, r'^worst slack(?:\s+max)?\s+([-0-9.eE+]+)')
        ts = grab(setup, r'^tns(?:\s+max)?\s+([-0-9.eE+]+)')
        wh = grab(hold, r'^worst slack(?:\s+min)?\s+([-0-9.eE+]+)')
        th = grab(hold, r'^tns(?:\s+min)?\s+([-0-9.eE+]+)')
        if name == 'slow':
            res.wns_setup_slow, res.tns_setup_slow = ws, ts
            res.groups_setup_slow = groups['setup']
        elif name == 'typical':
            res.wns_setup_typ, res.tns_setup_typ, res.wns_hold_typ, res.tns_hold_typ = ws, ts, wh, th
        else:
            res.wns_hold_fast, res.tns_hold_fast = wh, th
            res.groups_hold_fast = groups['hold']
    log.write_text('\n'.join(log_parts))
    rpt.write_text('\n'.join(report_parts))
    res.sdf_path = str(sdf) if sdf.exists() else None
    return res


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
            'full_sweep': cfg.full_sweep,
            'select_margin_ps': cfg.select_margin_ps,
            'fallback': cfg.fallback,
            'recipes': cfg.recipes,
            'modules': list(selections.keys()),
            'abc_sequential': cfg.abc_sequential,
            'yosys_opts': cfg.yosys_opts,
            'yosys_opts_sweep': cfg.yosys_opts_sweep,
            'abc_target': cfg.abc_target,
            'resize_winner': cfg.resize_winner,
            'repair_design': cfg.repair_design,
            'repair_hold': cfg.repair_hold,
            'max_fanout': cfg.max_fanout,
            'dont_use': cfg.dont_use,
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
    md.append('| Module | Recipe | WNS typ (ns) | Cells | Area (um²) | Cells / area after post-pass | WNS setup slow | WNS setup typ | WNS hold fast | WNS hold typ | Status |')
    md.append('|---|---|---|---|---|---|---|---|---|---|---|')
    for m, sel in selections.items():
        if sel.winner is None:
            md.append(f'| `{m}` | FAILED | — | — | — | — | — | — | — | — | ❌ |')
            continue
        post_s = '—'
        pp = results_dir / m / 'resize.json'
        if pp.exists():
            try:
                pr = json.loads(pp.read_text())
                post_s = f"{pr.get('cells_end', '?')} / {pr['end']['area']:.1f}"
                bad = [k for k, v in (pr.get('status') or {}).items() if str(v).startswith('failed')]
                if bad:
                    post_s += ' ⚠️ ' + ','.join(bad)
            except Exception:
                post_s = '?'
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
        wns_cell = (f'{win.wns_ns:.3f}' if win and win.wns_ns is not None else '—')
        cells_s = f'{win.cells}' if win else '—'
        area_s = f'{win.area:.1f}' if win else '—'
        md.append(
            f'| `{m}` | `{sel.winner}` | '
            f'{wns_cell} | {cells_s} | {area_s} | {post_s} | '
            f'{setup_slow} | {setup_typ} | {hold_fast} | {hold_typ} | {status} |'
        )
    md.append('')
    # per path group (clock) slack at the sign-off corners
    grp_rows = []
    for m, sel in selections.items():
        corner = corners.get(m)
        if corner and (corner.groups_setup_slow or corner.groups_hold_fast):
            for g in sorted(set(corner.groups_setup_slow) | set(corner.groups_hold_fast)):
                ss_ = corner.groups_setup_slow.get(g); hf = corner.groups_hold_fast.get(g)
                grp_rows.append(f"| `{m}` | `{g}` | {ss_:+.3f} | {hf:+.3f} |" if ss_ is not None and hf is not None
                                else f"| `{m}` | `{g}` | {'—' if ss_ is None else f'{ss_:+.3f}'} | {'—' if hf is None else f'{hf:+.3f}'} |")
    if grp_rows:
        md.append('### Slack per path group')
        md.append('')
        md.append('| Module | Path group | Setup slack slow (ns) | Hold slack fast (ns) |')
        md.append('|---|---|---|---|')
        md.extend(grp_rows)
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

# ============================================================================
# SDC → synthesis constraints
# ============================================================================

def load_sdc_constraints(cfg: Config, log=None):
    """Parse cfg.sdc (if any) with the top-level ports of the RTL. Returns a
    sdc_parse.Constraints or None. Never fatal: OpenSTA still sources the SDC."""
    if not cfg.sdc:
        return None
    if sdc_parse is None:
        if log: log.warning("sdc_parse module not available; SDC used by OpenSTA only")
        return None
    ports: dict = {}
    for f in cfg.rtl_files:
        try:
            ports = sdc_parse.ports_from_verilog(f, cfg.top)
        except OSError:
            ports = {}
        if ports:
            break
    try:
        return sdc_parse.parse_sdc(cfg.sdc, ports=ports or None)
    except Exception as e:  # tclsh missing, Tcl error, ...
        if log: log.warning(f"could not read SDC for synthesis ({e}); OpenSTA will still source it")
        return None


_LIB_CELL_RE = re.compile(r'(-lib_cell\s+)(\S+)')


def _adapt_sdc_lib_cells(sdc: Path, lc, out_dir: Path, log) -> Optional[Path]:
    """Copy `sdc` with every `-lib_cell <name>` that is not in the synthesis
    liberty replaced by the liberty's default driving cell. Returns the copy's
    path, or None when nothing needed changing."""
    text = sdc.read_text()
    code = re.sub(r'#.*', '', text)                     # ignore comments (incl. our own header)
    missing = sorted({m.group(2).strip('{}"') for m in _LIB_CELL_RE.finditer(code)
                      if m.group(2).strip('{}"') not in lc})
    if not missing:
        return None
    subst = {cell: lc.default_driving_cell(cell) for cell in missing}
    for cell, rep in subst.items():
        text = re.sub(r'(-lib_cell\s+)\{?"?' + re.escape(cell) + r'"?\}?', r'\g<1>' + rep, text)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{sdc.stem}.libadapted.sdc'
    note = ', '.join(f'{c} -> {r}' for c, r in subst.items())
    out.write_text(f'# {sdc.name} with -lib_cell {note} (not in the synthesis liberty)\n' + text)
    log.warning(f"SDC {sdc.name}: -lib_cell {note}; sourcing {out}")
    return out


def apply_sdc_overrides(cfg: Config, c, log=None) -> list[str]:
    """SDC wins over YAML for clocks and boundary conditions (docs/sdc-support.md).
    Returns the list of override messages (also logged)."""
    msgs: list[str] = []
    if c is None:
        return msgs
    primary = [ck for ck in c.clocks.values() if not ck.generated and ck.ports and ck.period_ns]
    primary.sort(key=lambda ck: ck.period_ns)
    if primary:
        ck = primary[0]
        new_port, new_T = ck.ports[0], int(round(ck.period_ns * 1000))
        if new_port != cfg.clock_port or new_T != cfg.period_ps:
            msgs.append(f"clock_port/period_ps {cfg.clock_port}/{cfg.period_ps} -> "
                        f"{new_port}/{new_T} (SDC create_clock {ck.name})")
            cfg.clock_port, cfg.period_ps = new_port, new_T
        if len(primary) >= 2:
            ck2 = primary[1]
            p2, T2 = ck2.ports[0], int(round(ck2.period_ns * 1000))
            if p2 != cfg.clock_port_2 or T2 != cfg.period_ps_2:
                msgs.append(f"clock_port_2/period_ps_2 -> {p2}/{T2} (SDC create_clock {ck2.name})")
                cfg.clock_port_2, cfg.period_ps_2 = p2, T2
        if len(primary) > 2:
            msgs.append(f"SDC defines {len(primary)} clocks; synthesis targets the two fastest, "
                        f"STA sees all")
        if ck.uncertainty_setup_ns is not None:
            v = int(round(ck.uncertainty_setup_ns * 1000))
            if v != cfg.clock_uncertainty_setup_ps:
                msgs.append(f"clock_uncertainty_setup_ps {cfg.clock_uncertainty_setup_ps} -> {v} (SDC)")
                cfg.clock_uncertainty_setup_ps = v
        if ck.uncertainty_hold_ns is not None:
            v = int(round(ck.uncertainty_hold_ns * 1000))
            if v != cfg.clock_uncertainty_hold_ps:
                msgs.append(f"clock_uncertainty_hold_ps {cfg.clock_uncertainty_hold_ps} -> {v} (SDC)")
                cfg.clock_uncertainty_hold_ps = v
    # Boundary conditions: take a driving cell / load that applies to every
    # constrained port (ABC has one global value for each).
    if c.driving_cells:
        cells = {d['cell'] for d in c.driving_cells}
        cell = c.driving_cells[-1]['cell']
        if len(cells) > 1:
            msgs.append(f"SDC has {len(cells)} driving cells; ABC uses one ({cell}), STA sees all")
        if cell != cfg.driving_cell:
            msgs.append(f"driving_cell {cfg.driving_cell} -> {cell} (SDC set_driving_cell)")
            cfg.driving_cell = cell
    if c.max_fanout and int(c.max_fanout) != cfg.max_fanout:
        msgs.append(f"max_fanout {cfg.max_fanout} -> {int(c.max_fanout)} (SDC set_max_fanout)")
        cfg.max_fanout = int(c.max_fanout)
    if c.dont_use:
        added = [x for x in c.dont_use if x not in cfg.dont_use]
        if added:
            msgs.append(f"dont_use += {added} (SDC set_dont_use)")
            cfg.dont_use = list(cfg.dont_use) + added
    max_loads = [d for d in c.loads if not d.get('min')]
    if max_loads:
        pf = max_loads[-1]['pf']
        ff = round(pf * 1000.0, 3)
        if abs(ff - cfg.load_ff) > 1e-6:
            msgs.append(f"load_ff {cfg.load_ff} -> {ff} (SDC set_load {pf} pF)")
            cfg.load_ff = ff
    if log:
        for m in msgs:
            log.info(f"[sdc] override: {m}")
        for u in c.unknown:
            log.warning(f"[sdc] unknown command (OpenSTA only): {u}")
        if c.sta_only:
            log.info(f"[sdc] {len(c.sta_only)} command(s) left to OpenSTA only")
        for w in c.warnings:
            log.warning(f"[sdc] {w}")
    return msgs


def write_derived_sdc(cfg: Config, c, path: Path, overrides: list[str], groups: Optional[dict] = None) -> None:
    """results/<module>/synth.sdc — what synthesis actually acted on."""
    L = ['# Derived by synth_flow: constraints used for SYNTHESIS (ABC targets).',
         '# OpenSTA sources the original SDC verbatim; this file is for inspection.',
         f'# source SDC: {cfg.sdc or "(none: YAML defaults)"}', '']
    for m in overrides:
        L.append(f'# override: {m}')
    if overrides:
        L.append('')
    T = cfg.period_ps / 1000.0
    L.append(f'create_clock -name {cfg.clock_port} -period {T} [get_ports {cfg.clock_port}]')
    if cfg.clock_port_2 and cfg.period_ps_2:
        L.append(f'create_clock -name {cfg.clock_port_2} -period {cfg.period_ps_2 / 1000.0} '
                 f'[get_ports {cfg.clock_port_2}]')
        L.append(f'set_clock_groups -asynchronous -group {cfg.clock_port} -group {cfg.clock_port_2}')
    L.append(f'set_clock_uncertainty -setup {cfg.clock_uncertainty_setup_ps / 1000.0} [all_clocks]')
    L.append(f'set_clock_uncertainty -hold {cfg.clock_uncertainty_hold_ps / 1000.0} [all_clocks]')
    L.append(f'set_driving_cell -lib_cell {cfg.driving_cell} [all_inputs -no_clocks]')
    L.append(f'set_load {cfg.load_ff / 1000.0} [all_outputs]')
    if groups:
        L.append(f'# ABC delay targets per path group (ps): reg2reg={groups["reg2reg_ps"]}, '
                 + ', '.join(f'{g["name"]}={g["budget_ps"]}' for g in groups['groups']))
        for n in groups['notes']:
            L.append(f'# {n}')
    else:
        d, note = resolve_abc_target(cfg)
        L.append(f'# ABC delay target: -D {d} ps ({note})')
    if c is not None:
        fps = sorted(c.false_path_ports())
        if fps:
            L.append('# false-path ports from SDC (excluded from I/O delay defaults, relaxed in synthesis):')
            for fp in fps:
                L.append(f'set_false_path -from [get_ports {fp}]')
        n_in = len({p for d in c.input_delays for p in d.ports})
        n_out = len({p for d in c.output_delays for p in d.ports})
        L.append(f'# SDC I/O delays: {n_in} input port(s), {n_out} output port(s) '
                 f'(used by OpenSTA now; used for path-group budgets in Phase 2)')
        for e in c.exceptions:
            if e.kind != 'false_path' or e.to or e.through:
                L.append(f'# exception (OpenSTA now, cone budget in Phase 2): {e.kind} from={e.from_} '
                         f'to={e.to} through={e.through} value={e.value}')
        if c.sta_only:
            L.append(f'# {len(c.sta_only)} STA-only command(s) not used by synthesis')
        if c.unknown:
            L.append(f'# {len(c.unknown)} unknown command(s), passed to OpenSTA only')
    path.write_text('\n'.join(L) + '\n')


_RESIZE_KEY_FIELDS = ('period_ps', 'clock_port', 'clock_port_2', 'period_ps_2', 'driving_cell', 'load_ff',
                      'clock_uncertainty_setup_ps', 'clock_uncertainty_hold_ps', 'wire_load_model', 'io_delay_frac',
                      'io_delay_min_frac', 'resize_iters', 'resize_wns_tol_ps', 'resize_final', 'resize_recover_area',
                      'repair_design', 'max_fanout', 'repair_hold', 'repair_hold_max_paths', 'repair_hold_sta_budget',
                      'dont_use', 'repair_buffer_cell', 'repair_delay_cell', 'select_margin_ps')


def _resize_key(cfg: Config, netlist_in: Path) -> str:
    """Content key of a post-pass run: netlist text, every liberty (path, size,
    mtime), the SDC text and the settings that change the result. Same key =
    same answer, so a finished run is reused (checkpoint.json in its work dir)."""
    h = hashlib.sha1()
    h.update(netlist_in.read_bytes())
    pl = _postpass_libs(cfg)
    for lib in [pl['primary'], cfg.lib_slow or '', cfg.lib_fast or '', *pl['std'], *pl['std_fast'], *pl['macro'], *pl['macro_fast']]:
        if lib and Path(lib).exists():
            st = Path(lib).stat()
            h.update(f'{lib}:{st.st_size}:{int(st.st_mtime)}'.encode())
    if cfg.sdc and Path(cfg.sdc).exists():
        h.update(Path(cfg.sdc).read_bytes())
    h.update(json.dumps({k: getattr(cfg, k) for k in _RESIZE_KEY_FIELDS}, sort_keys=True, default=str).encode())
    return h.hexdigest()


def _run_resize(cfg: Config, module: str, netlist_in: Path, work_dir: Path, log) -> Optional[dict]:
    """Run resize.py (sizing / repairs per cfg) on one netlist. Returns the
    result dict or None on failure; never raises. A finished run with the same
    content key (netlist, libraries, SDC, settings) is reused from
    <work_dir>/checkpoint.json instead of being recomputed."""
    try:
        import resize as resize_mod
    except ImportError:
        log.warning('[resize] resize.py not found; skipping')
        return None
    key = _resize_key(cfg, netlist_in)
    ckpt = work_dir / 'checkpoint.json'
    if ckpt.exists():
        try:
            saved = json.loads(ckpt.read_text())
            if saved.get('key') == key and Path(saved['result']['output']).exists():
                res = dict(saved['result']); res['reused_checkpoint'] = True
                return res
        except Exception:
            pass
    sta_lib = cfg.lib_slow or cfg.lib_typ
    try:
        res = resize_mod.resize(
            netlist_in, module, _synth_lib(asdict(cfg)), sta_lib, cfg.period_ps, cfg.clock_port, work_dir,
            sdc=cfg.sdc, iters=cfg.resize_iters, yosys=cfg.yosys, opensta=cfg.opensta,
            driving_cell=cfg.driving_cell, load_ff=cfg.load_ff,
            unc_setup_ps=cfg.clock_uncertainty_setup_ps, unc_hold_ps=cfg.clock_uncertainty_hold_ps,
            wire_load_model=cfg.wire_load_model, io_delay_frac=cfg.io_delay_frac,
            io_delay_min_frac=cfg.io_delay_min_frac,
            clock_port_2=cfg.clock_port_2, period_ps_2=cfg.period_ps_2,
            wns_tol=cfg.resize_wns_tol_ps / 1000.0, final=cfg.resize_final,
            repair_design=cfg.repair_design, max_fanout=cfg.max_fanout,
            repair_hold=cfg.repair_hold, lib_fast=cfg.lib_fast,
            hold_max_paths=cfg.repair_hold_max_paths, hold_sta_budget=cfg.repair_hold_sta_budget,
            extra_libs=_postpass_libs(cfg)['std'], extra_libs_fast=_postpass_libs(cfg)['std_fast'],
            macro_libs=_postpass_libs(cfg)['macro'], macro_libs_fast=_postpass_libs(cfg)['macro_fast'],
            recover_area=cfg.resize_recover_area,
            dont_use=cfg.dont_use, buffer_cell=cfg.repair_buffer_cell, delay_cell=cfg.repair_delay_cell,
            log=lambda *x: log.debug('[resize] ' + ' '.join(str(v) for v in x)))
    except Exception as e:
        log.warning(f'[resize] {module}: {netlist_in.name} failed ({e})')
        return None
    try:
        ckpt.write_text(json.dumps({'key': key, 'result': res}, indent=1))
    except Exception:
        pass
    return res


def _resize_winner(cfg: Config, module: str, mod_results: Path, work_dir: Path, log) -> None:
    """Post-pass on results/<module>/winner.v; keep the pre-sizing netlist as
    winner.presize.v and write resize.json. Never fatal."""
    winner = mod_results / 'winner.v'
    presize = mod_results / 'winner.presize.v'
    shutil.copy(winner, presize)
    res = _run_resize(cfg, module, presize, work_dir, log)
    if res is None:
        log.warning(f'[resize] {module}: winner left unsized')
        return
    shutil.copy(res['output'], winner)
    (mod_results / 'resize.json').write_text(json.dumps(res, indent=2))
    _log_resize(module, res, log)


def _log_resize(module: str, res: dict, log) -> None:
    st = res.get('status', {})
    bad = {k: v for k, v in st.items() if v.startswith('failed')}
    if res.get('reused_checkpoint'):
        log.info(f'[resize] {module}: reused checkpoint (same netlist, libraries, SDC and settings)')
    log.info(f"[resize] {module}: WNS {res['start']['wns_ns']:+.3f} -> {res['end']['wns_ns']:+.3f}  "
             f"TNS {res['start']['tns_ns']:+.2f} -> {res['end']['tns_ns']:+.2f}  "
             f"area {res['start']['area']:.0f} -> {res['end']['area']:.0f}  cells {res.get('cells_start', '?')} -> {res.get('cells_end', '?')}  "
             f"({len(res['moves'])} moves, {res['buffers_inserted']} buffers, {res['delay_cells_inserted']} delay cells"
             + (f"; hold@fast {res['hold_before'][0]:+.3f} -> {res['hold_after'][0]:+.3f}" if res.get('hold_before') and res['hold_before'][0] is not None and res.get('hold_after') and res['hold_after'][0] is not None else '')
             + ')' + (f"  PHASES FAILED: {bad}" if bad else ''))


def _postpass_candidates(cands: list, sel, n: int) -> list:
    """Which candidates get the post-pass when resize_candidates > 1: the
    selected one, the fastest (best WNS), then the Pareto front in area order."""
    order = []
    def add(c):
        if c and c.netlist and c not in order:
            order.append(c)
    add(next((c for c in cands if c.recipe == sel.winner), None))
    timed = [c for c in cands if c.wns_ns is not None and c.netlist]
    if timed:
        add(max(timed, key=lambda c: c.wns_ns))
    for r in sel.pareto_front:
        add(next((c for c in cands if c.recipe == r), None))
    for c in sorted(timed, key=lambda c: (-(c.wns_ns or 0), c.area)):
        add(c)
    return order[:max(1, n)]


def _resize_candidates(cfg: Config, module: str, cands: list, sel, mod_results: Path, work_dir: Path, log):
    """Post-pass on several candidates, then select again with the same rule
    (min area among those meeting timing after repair; else fallback).
    Writes winner.v / winner.presize.v / resize.json for the final choice and
    postpass.json with every candidate's before/after."""
    chosen = _postpass_candidates(cands, sel, cfg.resize_candidates)
    log.info(f"[resize] {module}: post-pass on {len(chosen)} candidates: {', '.join(c.recipe for c in chosen)}")
    post, records = [], {}
    # independent post-passes run concurrently (each is OpenSTA/Yosys-bound)
    workers = min(len(chosen), max(1, cfg.effective_parallel()))
    results: dict[str, Optional[dict]] = {}
    if workers > 1:
        import concurrent.futures as _cf
        with _cf.ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run_resize, cfg, module, Path(c.netlist), work_dir / c.recipe, log): c.recipe for c in chosen}
            for fut in _cf.as_completed(futs):
                try:
                    results[futs[fut]] = fut.result()
                except Exception as e:
                    log.warning(f'[resize] {module}/{futs[fut]}: worker failed ({e})')
                    results[futs[fut]] = None
    else:
        for c in chosen:
            results[c.recipe] = _run_resize(cfg, module, Path(c.netlist), work_dir / c.recipe, log)
    for c in chosen:
        res = results.get(c.recipe)
        if res is None:
            records[c.recipe] = {'status': 'failed'}
            continue
        _log_resize(f'{module}/{c.recipe}', res, log)
        post.append(Candidate(recipe=c.recipe, netlist=res['output'], wns_ns=res['end']['wns_ns'], tns_ns=res['end']['tns_ns'],
                              cells=res.get('cells_end', c.cells), area=res['end']['area'], runtime_s=c.runtime_s))
        records[c.recipe] = {'before': {'wns_ns': c.wns_ns, 'tns_ns': c.tns_ns, 'area': c.area, 'cells': c.cells},
                             'after': {'wns_ns': res['end']['wns_ns'], 'tns_ns': res['end']['tns_ns'], 'area': res['end']['area'],
                                       'cells': res.get('cells_end')}, 'status': res.get('status'), 'resize_json': str(Path(res['output']).parent / 'resize.json')}
    if not post:
        log.warning(f'[resize] {module}: every candidate failed the post-pass; keeping the pre-pass winner')
        return sel
    sel2 = select_winner(post, cfg.objective, cfg.select_margin_ps / 1000.0, cfg.fallback, period_ns=cfg.period_ps / 1000.0)
    win = next(c for c in post if c.recipe == sel2.winner)
    orig = next(c for c in cands if c.recipe == sel2.winner)
    shutil.copy(orig.netlist, mod_results / 'winner.presize.v')
    shutil.copy(win.netlist, mod_results / 'winner.v')
    shutil.copy(Path(win.netlist).parent / 'resize.json', mod_results / 'resize.json')
    (mod_results / 'postpass.json').write_text(json.dumps({'winner': sel2.winner, 'rationale': sel2.rationale,
                                                            'candidates': records}, indent=2))
    if sel2.winner != sel.winner:
        log.info(f"[winner] {module}: after the post-pass {sel2.winner} replaces {sel.winner} ({sel2.rationale})")
    sel.winner = sel2.winner
    sel.rationale = f'after post-pass over {len(post)} candidates: {sel2.rationale}'
    return sel
    s0, s1 = res['start'], res['end']
    extra = ''
    if res.get('buffers_inserted'):
        extra += f", {res['buffers_inserted']} buffers"
    if res.get('hold_before') and res.get('hold_after'):
        hb, ha = res['hold_before'], res['hold_after']
        extra += f", hold@fast {hb[0]:+.3f} -> {ha[0]:+.3f} ({res.get('delay_cells_inserted', 0)} delay cells)"
    log.info(f"[resize] {module}: WNS {s0['wns_ns']:+.3f} -> {s1['wns_ns']:+.3f}  "
             f"TNS {s0['tns_ns']:+.2f} -> {s1['tns_ns']:+.2f}  area {s0['area']:.0f} -> {s1['area']:.0f}  "
             f"({len(res['moves'])} moves{extra})")


def parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    p.add_argument('--config', help='YAML config file')
    # Direct overrides
    p.add_argument('--rtl', action='append', help='RTL files/globs (repeatable)')
    p.add_argument('--lib', help='typical-corner liberty; a comma-separated list loads several standard-cell libraries at once')
    p.add_argument('--lib-fast', help='fast-corner liberty (STA hold)')
    p.add_argument('--lib-slow', help='slow-corner liberty (STA setup)')
    p.add_argument('--macro-lib', action='append', dest='macro_lib',
                   help='hard-macro liberty (e.g. SRAM .lib). Repeatable. '
                        'Applied to all STA corners (typ/fast/slow). '
                        'Use the YAML `macro_libs:` dict for per-corner files.')
    p.add_argument('--top', help='top module name')
    p.add_argument('--period-ps', type=int, help='target clock period in ps')
    p.add_argument('--clock-port', help='clock port name (default: clk)')
    p.add_argument('--sdc', help='SDC file: sourced by OpenSTA and read for synthesis clocks/budgets')
    p.add_argument('--path-groups', action='store_true', help='EXPERIMENTAL: per-path-group ABC delay targets (see docs/architecture.md §2.5)')
    p.add_argument('--abc-target', help="ABC -D: 'none' (default, min-delay mapping), 'period', 'reg2reg' (T - t_cq - t_su - uncertainty), or ps")
    p.add_argument('--resize', action='store_true', help='OpenSTA-guided drive-strength sizing of each winner (needs OpenSTA)')
    p.add_argument('--repair-design', action='store_true', help='buffer trees on high-fanout nets of failing paths (needs OpenSTA)')
    p.add_argument('--resize-candidates', type=int, help='run the post-pass on the N best candidates and select again (default 1)')
    p.add_argument('--recover-area', action='store_true', help='after the winner meets timing, downsize or swap off-critical cells to a slower library while WNS holds (implies --resize)')
    p.add_argument('--repair-hold', action='store_true', help='delay cells on failing hold endpoints at the fast corner (needs OpenSTA + lib_fast)')
    p.add_argument('--max-fanout', type=int, help='sink group size for repair_design (default 8; SDC set_max_fanout overrides)')
    p.add_argument('--yosys-opts', nargs='+', help='front-end options: booth, adder=kogge-stone|han-carlson|sklansky, noshare, hieropt')
    p.add_argument('--dont-use', nargs='+', help='liberty cell patterns excluded from abc and dfflibmap')
    p.add_argument('--abc-wire-load', action='store_true', help="ABC sizing with the liberty wire-load model (-c on buffer/upsize/dnsize/stime) in every recipe")
    p.add_argument('--objective', choices=['delay', 'area', 'balanced', 'fastest', 'pareto'],
                   help='recipe subset to run (delay|area|balanced); selection is always min-area-meeting-timing')
    p.add_argument('--full-sweep', action='store_true', help='run every recipe (ignore the objective subset)')
    p.add_argument('--select-margin-ps', type=int, help='WNS a candidate needs to count as meeting timing (default 0)')
    p.add_argument('--fallback', choices=['knee', 'best_wns'], help="when nothing meets timing: knee of the WNS/area front (default) or fastest")
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
        'lib_slow': 'lib_slow', 'top': 'top', 'period_ps': 'period_ps', 'sdc': 'sdc',
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
    if any(',' in str(getattr(args, a, '') or '') for a in ('lib', 'lib_slow', 'lib_fast')):
        d = {'lib_typ': cfg.lib_typ, 'lib_slow': cfg.lib_slow, 'lib_fast': cfg.lib_fast, 'lib_extra': cfg.lib_extra}
        _split_lib_lists(d)
        cfg.lib_typ, cfg.lib_slow, cfg.lib_fast, cfg.lib_extra = d['lib_typ'], d['lib_slow'], d['lib_fast'], d['lib_extra']
    if args.no_sta:
        cfg.run_sta = False
    if args.no_gls:
        cfg.run_gls = False
    if getattr(args, 'path_groups', False):
        cfg.path_groups = True
    if getattr(args, 'abc_target', None):
        cfg.abc_target = args.abc_target
    if getattr(args, 'resize', False):
        cfg.resize_winner = True
    if getattr(args, 'repair_design', False):
        cfg.repair_design = True
    if getattr(args, 'resize_candidates', None):
        cfg.resize_candidates = args.resize_candidates
        cfg.resize_winner = True
    if getattr(args, 'recover_area', False):
        cfg.resize_recover_area = True
        cfg.resize_winner = True
    if getattr(args, 'repair_hold', False):
        cfg.repair_hold = True
    if getattr(args, 'max_fanout', None):
        cfg.max_fanout = args.max_fanout
    if getattr(args, 'yosys_opts', None):
        cfg.yosys_opts = list(args.yosys_opts)
        cfg.yosys_opts_sweep = []            # an explicit front end on the CLI replaces any sweep
    if getattr(args, 'dont_use', None):
        cfg.dont_use = list(args.dont_use)
    if getattr(args, 'abc_wire_load', False):
        cfg.abc_wire_load = True
    if getattr(args, 'full_sweep', False):
        cfg.full_sweep = True
    if getattr(args, 'select_margin_ps', None) is not None:
        cfg.select_margin_ps = args.select_margin_ps
    if getattr(args, 'fallback', None):
        cfg.fallback = args.fallback
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

    try:
        for v in ([cfg.yosys_opts] if isinstance(cfg.yosys_opts, list) else list(cfg.yosys_opts.values())):
            _front_end(v)
        sweeps = cfg.yosys_opts_sweep if isinstance(cfg.yosys_opts_sweep, list) else \
            [v for vs in cfg.yosys_opts_sweep.values() for v in vs]
        for v in sweeps:
            _front_end(v)
    except ValueError as e:
        log.error(str(e))
        return EXIT_CONFIG_ERR

    # ----- SDC: constraints for synthesis (OpenSTA sources the file itself) -----
    sdc_constraints = load_sdc_constraints(cfg, log)
    sdc_overrides = apply_sdc_overrides(cfg, sdc_constraints, log)

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
    aliases = {'fastest': 'delay', 'pareto': 'balanced'}
    if cfg.objective in aliases:
        log.info(f"objective '{cfg.objective}' is an alias of '{aliases[cfg.objective]}'")
        cfg.objective = aliases[cfg.objective]
    requested = list(cfg.recipes)
    if not requested and not cfg.full_sweep:
        available = {p.stem for p in recipes_dir.glob('*.abc')}
        requested = [r for r in RECIPE_SETS.get(cfg.objective, []) if r in available]
        if requested:
            log.info(f"objective '{cfg.objective}': recipe set {requested} (--full-sweep runs all)")
        else:
            log.info(f"objective '{cfg.objective}': no preset matches recipes in {recipes_dir}; running all")
    try:
        recipe_pairs = discover_recipes(recipes_dir, requested)
    except FileNotFoundError as e:
        log.error(str(e))
        return EXIT_CONFIG_ERR
    cfg.recipes = [name for name, _ in recipe_pairs]
    log.info(f"recipes: {', '.join(cfg.recipes)}")
    fe_by_module = {m: _fe_variants_for(cfg, m) for m in cfg.modules}
    for m, vs in fe_by_module.items():
        if len(vs) > 1 or (vs and vs[0]):
            log.info(f"front end for {m}: {', '.join(_variant_tag(v) or 'plain' for v in vs)}"
                     + (f" -> {len(vs)} x {len(cfg.recipes)} candidates" if len(vs) > 1 else ''))

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

    cfg_dict = asdict(cfg)

    # ----- driving cell must exist in the synthesis liberty -----
    if liberty_timing is not None:
        try:
            _lc = liberty_timing.LibCells(_synth_libs(cfg_dict))
            if cfg.driving_cell not in _lc:
                fallback = _lc.default_driving_cell(cfg.driving_cell)
                log.warning(f"driving_cell '{cfg.driving_cell}' is not in {Path(_synth_lib(cfg_dict)).name}; "
                            f"using {fallback}")
                cfg.driving_cell = fallback
                cfg_dict['driving_cell'] = fallback
            # Several libraries: map with the fastest one only (mixed_map: fastest)
            if cfg.mixed_map == 'fastest' and len(_synth_libs(cfg_dict)) > 1 and _lc.fastest_lib():
                fastest = _lc.fastest_lib()
                others = [Path(l).name for l in _synth_libs(cfg_dict) if l != fastest]
                log.info(f"mixed libraries: mapping with the fastest, {Path(fastest).name}; "
                         f"{', '.join(others)} enter through the post-pass (mixed_map: all to offer every cell to ABC)")
                cfg.lib_synth, cfg.lib_synth_extra = fastest, []
                cfg_dict['lib_synth'], cfg_dict['lib_synth_extra'] = fastest, []
            # The user SDC is sourced verbatim by every STA run; a `-lib_cell`
            # from another library (an HD SDC run against HS/MS/LS/LP) would
            # abort OpenSTA, so source a copy with those cells substituted.
            if cfg.sdc and Path(cfg.sdc).exists():
                adapted = _adapt_sdc_lib_cells(Path(cfg.sdc), _lc, Path(cfg.results_dir) / cfg.top, log)
                if adapted:
                    cfg.sdc = str(adapted)
                    cfg_dict['sdc'] = str(adapted)
        except Exception as e:  # never fatal
            log.debug(f'driving cell check skipped: {e}')

    # ----- ABC delay target -----
    abc_d_ps, abc_d_note = resolve_abc_target(cfg)
    log.info(f"ABC -D = {abc_d_ps} ps ({abc_d_note})")
    cfg_dict['abc_d_ps'] = abc_d_ps

    # ----- path groups (per module; SDC applies to the top) -----
    module_groups: dict[str, Optional[dict]] = {}
    if cfg.path_groups:
        if cfg.hierarchical or cfg.abc_sequential or (cfg.clock_port_2 and cfg.dual_clock_synthesis):
            log.warning("path_groups: only supported in the flat standard flow; ignoring")
        elif liberty_timing is None or sdc_parse is None:
            log.warning("path_groups: liberty_timing/sdc_parse modules missing; ignoring")
        else:
            lt = liberty_timing.read_liberty_timing(_synth_lib(asdict(cfg)))
            for module in cfg.modules:
                ports: dict = {}
                for f in cfg.rtl_files:
                    try:
                        ports = sdc_parse.ports_from_verilog(f, module)
                    except OSError:
                        ports = {}
                    if ports:
                        break
                spec = build_path_groups(cfg, module,
                                         sdc_constraints if module == cfg.top else None,
                                         ports, lt, work / module / 'groups')
                module_groups[module] = spec
                if spec:
                    desc = ', '.join(f"{g['name']}={g['budget_ps']}" for g in spec['groups'])
                    log.info(f"[groups] {module}: {desc}, reg2reg={spec['reg2reg_ps']} ps "
                             f"(T={spec['period_ps']}; {'; '.join(spec['notes'])})")
                else:
                    log.warning(f"[groups] {module}: no ports/flop timing found; flat mapping")

    # ----- assemble jobs (module x recipe) -----
    # In hierarchical mode we process modules one at a time in dependency
    # order so that winner netlists are available for parent modules.
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
        sta_jobs: list[dict] = []
        results_by_recipe: dict[str, RecipeResult] = {}
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
            results_by_recipe[r.recipe] = r
            sta_jobs.append({'cfg': cfg_dict, 'module': module, 'recipe': r.recipe,
                             'netlist': r.netlist, 'log': str(work / module / f'{r.recipe}.qsta.log')})
        # Quick STA per candidate: independent OpenSTA processes, run in the
        # same pool as synthesis (this was the largest serial part of a run).
        timing: dict[str, tuple[Optional[float], Optional[float]]] = {}
        if sta_jobs and cfg.run_sta:
            t0 = time.time()
            if cfg.effective_parallel() == 1 or len(sta_jobs) == 1:
                for job in sta_jobs:
                    rec, wns, tns = _quick_sta_job(job)
                    timing[rec] = (wns, tns)
            else:
                with mp.Pool(min(cfg.effective_parallel(), len(sta_jobs))) as pool:
                    for rec, wns, tns in pool.imap_unordered(_quick_sta_job, sta_jobs):
                        timing[rec] = (wns, tns)
            log.info(f"  quick STA: {len(sta_jobs)} candidates in {time.time() - t0:.1f}s "
                     f"on {min(cfg.effective_parallel(), len(sta_jobs))} workers")
        for rec, r in results_by_recipe.items():
            wns, tns = timing.get(rec, (None, None))
            cands.append(Candidate(
                recipe=rec, netlist=r.netlist,
                wns_ns=wns, tns_ns=tns,
                cells=r.cells, area=r.area,
                runtime_s=r.runtime_s,
            ))

        sel = select_winner(cands, cfg.objective, cfg.select_margin_ps / 1000.0, cfg.fallback,
                            period_ns=cfg.period_ps / 1000.0)
        sel.module = module
        selections[module] = sel

        if sel.winner:
            win = next(c for c in cands if c.recipe == sel.winner)
            wns_s = (f'{win.wns_ns:.3f}ns' if win.wns_ns is not None else 'n/a')
            log.info(f"[winner] {module}: {sel.winner}  "
                     f"WNS={wns_s}  area={win.area:.1f}  ({sel.rationale})")
            mod_results = results / module
            mod_results.mkdir(parents=True, exist_ok=True)
            if win.netlist:
                shutil.copy(win.netlist, mod_results / 'winner.v')
                winner_netlists[module] = str(mod_results / 'winner.v')
                if (cfg.resize_winner or cfg.repair_design or cfg.repair_hold) and cfg.run_sta:
                    if cfg.resize_candidates > 1:
                        sel = _resize_candidates(cfg, module, cands, sel, mod_results, work / module / 'resize', log)
                        selections[module] = sel
                    else:
                        _resize_winner(cfg, module, mod_results, work / module / 'resize', log)
            write_derived_sdc(cfg, sdc_constraints, mod_results / 'synth.sdc', sdc_overrides,
                              groups=module_groups.get(module))
            win_groups = work / module / f'{sel.winner}.groups.json'
            if win_groups.exists():
                shutil.copy(win_groups, mod_results / 'groups.json')
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
            for variant in fe_by_module[module]:
                for recipe_name, recipe_path in recipe_pairs:
                    jobs.append({
                        'module': module,
                        'recipe': _candidate_name(recipe_name, variant),
                        'recipe_path': str(recipe_path),
                        'variant': list(variant),
                        'workdir': str(mod_dir),
                        'constr': str(constr),
                        'cfg': cfg_dict,
                        'dep_netlists': dep_nets,
                        'dep_modules': dep_mods,
                        'groups': module_groups.get(module),
                    })
            total = len(jobs)
            log.info(f"  running {total} jobs ({len(fe_by_module[module])} variants × "
                     f"{len(recipe_pairs)} recipes) on {cfg.effective_parallel()} workers")
            _run_jobs(jobs)
            _pick_winner(module)
        log.info(f"hierarchical sweep done in {time.time()-t0:.1f}s")
    else:
        # Flat mode: synthesize all modules in parallel
        jobs = []
        for module in cfg.modules:
            mod_dir = work / module
            for variant in fe_by_module[module]:
                for recipe_name, recipe_path in recipe_pairs:
                    jobs.append({
                        'module': module,
                        'recipe': _candidate_name(recipe_name, variant),
                        'recipe_path': str(recipe_path),
                        'variant': list(variant),
                        'workdir': str(mod_dir),
                        'constr': str(constr),
                        'cfg': cfg_dict,
                        'groups': module_groups.get(module),
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
