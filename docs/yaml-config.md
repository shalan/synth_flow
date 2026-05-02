<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Author: Mohamed Shalan <mshalan@aucegypt.edu> -->
# YAML Configuration Reference

Complete reference for `synth.yaml`. Every field, with type, default,
semantics, and examples.

## Loading

```bash
synth_flow.py --config synth.yaml
```

The YAML is parsed with `yaml.safe_load` (no Python objects, no `!!`
tags). Any field can be overridden by an environment variable or CLI
flag — see [Precedence](#precedence) below.

Path-like fields support `~` (home) and `$VAR` (env) expansion.
File-list fields support glob patterns (`*.v`, `**/*.sv`).

## Quick template

Minimum viable config — synthesis only, no STA, no GLS:

```yaml
rtl_files: [rtl/*.v]
lib_typ: lib/sky130_fd_sc_hd__tt_025C_1v80.lib
top: my_design
run_sta: false
run_gls: false
```

Full config with all corners and GLS:

```yaml
rtl_files: [rtl/*.v, rtl/*.sv]
lib_typ:  $PDK/sky130_fd_sc_hd__tt_025C_1v80.lib
lib_fast: $PDK/sky130_fd_sc_hd__ff_n40C_1v95.lib
lib_slow: $PDK/sky130_fd_sc_hd__ss_100C_1v60.lib
top: my_design
period_ps: 8000
objective: delay
tb_files: [tb/tb_my_design.v]
tb_top: tb_my_design
primitives_dir: $PDK/sky130_fd_sc_hd/verilog
```

## Field reference

### Required (always)

| Field | Type | Description |
|---|---|---|
| `rtl_files` | list of strings | Verilog/SystemVerilog source files. Globs allowed. Must be non-empty and all paths must resolve. |
| `lib_typ` | string | Path to typical-corner liberty file. Used for synthesis ABC mapping and quick-STA winner selection. |
| `top` | string | Name of the project top module. Used as the GLS netlist filename and report title. Does not need to be in `modules` (auto-detection still scans it). |

### Required for STA

These are required only when `run_sta: true` (the default). Set
`run_sta: false` to skip STA entirely.

| Field | Type | Description |
|---|---|---|
| `lib_fast` | string | Fast-corner liberty (lowest delay, used for hold checks). |
| `lib_slow` | string | Slow-corner liberty (highest delay, used for setup checks). |

### Required for GLS

These are required only when `run_gls: true` (the default). Set
`run_gls: false` to skip GLS entirely.

| Field | Type | Description |
|---|---|---|
| `tb_files` | list of strings | Testbench source files (Verilog/SV). Globs allowed. |
| `tb_top` | string | Name of the testbench top module. Passed to iverilog as `-s <name>`. |
| `primitives_dir` | string | Directory containing cell models for the PDK. For Sky130 HD: `$PDK/sky130_fd_sc_hd/verilog`. The script searches this dir for an `*sc_hd.v` or `primitives.v` to include via `-v`, and adds the directory to `-y` paths. |

### Design parameters

| Field | Type | Default | Description |
|---|---|---|---|
| `period_ps` | int | `10000` | Target clock period in picoseconds. Substituted into `{D}` in every recipe. |
| `clock_port` | string | `clk` | Name of the clock port on every module being synthesized. (Currently uniform across modules.) |
| `objective` | enum | `delay` | Recipe selection criterion. One of: `delay`, `area`, `fastest`, `pareto`, `balanced`. See [Objectives](#objectives). |
| `modules` | list of strings | `[]` | Modules to synthesize separately. **Empty means auto-detect** — all modules defined in RTL but not instantiated by any other module are picked. Set this explicitly when auto-detection misses something or picks too many. |
| `recipes` | list of strings | `[]` | Subset of recipe names (without `.abc`) to sweep. **Empty means all available** in `recipes_dir`. Use this to restrict during fast iteration: `recipes: [balanced]`. |

### ABC constraints

| Field | Type | Default | Description |
|---|---|---|---|
| `driving_cell` | string | `sky130_fd_sc_hd__inv_2` | Cell used in `set_driving_cell` for ABC's input boundary model. Must exist in `lib_typ`. |
| `load_ff` | float | `17.65` | Output load in **femtofarads** for ABC's `set_load`. The OpenLane Sky130 HD default. |

### Tool paths

| Field | Type | Default | Description |
|---|---|---|---|
| `yosys` | string | `yosys` | Yosys executable. Bare command name uses `$PATH`. |
| `abc` | string | `abc` | ABC executable path. **Currently unused** — Yosys invokes its bundled ABC. Set if you need to pin a specific ABC build (would require minor script changes). |
| `opensta` | string | `sta` | OpenSTA executable. |
| `iverilog` | string | `iverilog` | Icarus Verilog compiler. |
| `vvp` | string | `vvp` | Icarus Verilog runtime. |

All five can be overridden via environment variables of the same name in
uppercase (e.g. `YOSYS=/opt/yosys/bin/yosys`).

### Execution

| Field | Type | Default | Description |
|---|---|---|---|
| `parallel` | int | `0` | Number of parallel workers for the (module × recipe) sweep. `0` means use `os.cpu_count()`. Set to `1` to disable parallelism (useful for debugging). |
| `work_dir` | string | `work` | Directory for intermediates: per-recipe netlists, logs, ys scripts, qsta logs. Safe to delete after a successful run. |
| `results_dir` | string | `results` | Directory for final deliverables: winner netlists, SDF files, summary reports. |
| `recipes_dir` | string | `<script_dir>/recipes` | Where to look for `*.abc` recipe files. Override if you maintain your own recipe collection. |

### Behavior flags

| Field | Type | Default | Description |
|---|---|---|---|
| `run_sta` | bool | `true` | Run corner STA (slow + fast) on winners. Set false to skip and avoid the `lib_fast`/`lib_slow` requirements. |
| `run_gls` | bool | `true` | Run gate-level simulation. Set false to skip and avoid the `tb_files`/`tb_top`/`primitives_dir` requirements. |
| `fail_on_timing` | bool | `true` | When `true`, exit code 2 if any winner has setup violation at the slow corner. When `false`, timing violations are reported but exit code stays 0 (useful for early characterization runs). |
| `sdf_back_annotate` | bool | `true` | When true, GLS uses SDF back-annotation. The script writes SDF during STA and passes `+sdf_<module>=<path>` plusargs to vvp. The testbench is responsible for `$sdf_annotate` calls. |

### Experimental

| Field | Type | Default | Description |
|---|---|---|---|
| `abc_sequential` | bool | `false` | Enable ABC `-dff` for sequential optimizations (retiming, scorr). Reorders the flow to `abc -dff` → `dfflibmap`. **Breaks LEC**, may misbehave with async resets and clock gating. See README's "Experimental" section. |

## Objectives

`objective:` controls how the winning recipe is picked from the sweep.

### `delay`
Maximum WNS wins. Tiebreaks: smaller area, then recipe stability priority.

### `fastest`
Among recipes with `wns >= 0` (meeting timing), the one with maximum WNS wins — this corresponds to the minimum actual critical path delay. Tiebreaks: smaller area, then stability. **Fallback:** if no recipe meets timing, falls back to max-WNS (so the netlist isn't empty).

### `area`
Among recipes with `wns >= 0` (meeting timing), the smallest area wins.
Tiebreaks: larger WNS, then stability. **Fallback:** if no recipe meets
timing, falls back to max-WNS (so the netlist isn't empty).

### `pareto`
Computes the Pareto front on (WNS↑, area↓) and reports it. The "winner"
is the max-WNS point on the front (representative; not a strict pick).
Use this when you want to see the full trade-off space; the
`pareto_front` field in `summary.json` lists all front members.

### `balanced`
50/50 weighted: WNS rank + area rank, lowest sum wins. No bias toward
either dimension. Useful when you don't want to choose.

## Recipes

The 14 recipes shipped, in stability order (preferred-first when QoR
ties):

| Recipe | Class | Strategy | Runtime |
|---|---|---|---|
| `delay_retime` | Delay | Retiming + double-pass GIA mapping | 1.3× |
| `delay_triple` | Delay | Triple-pass remap with sizing between each | 1.4× |
| `delay_choice_deep` | Delay | Choice-driven with high conflict limit | 1.0× |
| `delay_iter_heavy` | Delay | Quadruple-pass explicit unrolling | 1.7× |
| `balanced_resyn` | Balanced | Inlined resyn2 (balance/rewrite/refactor) + GIA map | 1.0× |
| `balanced_resyn2x` | Balanced | Two rewriting passes + double GIA mapping | 1.4× |
| `balanced_struct` | Structural | GIA structural cleanup (`&scl`, `&lcorr`) | 1.0× |
| `area_safe` | Area | Full AIG cleanup + retiming + GIA mapping | 1.2× |
| `area_classic` | Area | Rewriting + scorr/dc2 + GIA mapping | 1.1× |
| `area_lut6` | Area | Heavy scorr + dc2 + dretime + rewriting | 1.2× |
| `area_max` | Area | Double rewriting + double scorr/dc2 + retiming + double map | 1.3× |
| `lazy_man` | Heavy | PULP-style: 8 opt + 8 opt+map iterations (`&syn2`, `&if`) | 1.0× |
| `orfs_speed` | Reference | Direct port of ORFS/OpenLane DELAY 0 recipe | 0.8× |
| `yosys_default` | Reference | Yosys default flow baseline | 0.8× |

Runtime multipliers are vs `balanced_resyn` on a typical Sky130 HD module.
Add a recipe by dropping a `<name>.abc` into `recipes/` — the script
auto-discovers it. All recipes are verified compatible with ABC 1.01+
and use only confirmed-available commands.

## Precedence

Effective value = highest precedence source that sets the field:

1. **CLI flag** (e.g., `--period-ps 5000`)
2. **Config file** (`period_ps: 5000` in YAML)
3. **Environment variable** (only for tool paths and lib paths: `YOSYS`, `ABC`, `OPENSTA`, `IVERILOG`, `VVP`, `LIB_TYP`, `LIB_FAST`, `LIB_SLOW`)
4. **Built-in default** (the `Default` column above)

Example — config sets period to 8000, CLI overrides:
```bash
synth_flow.py --config synth.yaml --period-ps 5000
# effective: period_ps = 5000
```

Example — env overrides config tool path:
```bash
YOSYS=/opt/yosys-master/bin/yosys synth_flow.py --config synth.yaml
# effective: yosys = /opt/yosys-master/bin/yosys
```

## Variable expansion

In YAML string fields:

- `~` expands to `$HOME`
- `$VAR` and `${VAR}` expand to environment variable values
- Globs (`*`, `**`, `?`) expand against the filesystem **at config-load time**

```yaml
rtl_files:
  - $PROJECT_ROOT/rtl/*.v       # env + glob
  - ~/shared/common/*.sv        # home + glob

lib_typ: $PDK/sky130_fd_sc_hd__tt_025C_1v80.lib
work_dir: $BUILD_ROOT/synth-work
```

If a glob matches nothing, the literal pattern is kept in the list (and
will fail validation). This is intentional — silent dropping of missing
files is worse than a clear "file not found" error.

## Module auto-detection

When `modules:` is empty (or omitted), the script scans all `rtl_files`
and identifies modules that:

- Are **defined** in the file set (have a `module foo` declaration), and
- Are **not instantiated** by any other module in the file set.

This catches each standalone IP block plus the project top, which is
exactly what you want to synthesize separately.

Limitations:
- Modules instantiated through generate blocks with parameterized names
  may be missed.
- Modules instantiated only conditionally (inside generate-if) are
  always considered instantiated.
- `*ifdef` blocks aren't evaluated; all `module ...` declarations are
  considered, even those inside `*ifndef BLAH ... *endif`.

When auto-detection misses or over-picks, list modules explicitly:

```yaml
modules:
  - cpu_core
  - uart
  - spi_master
```

## Common patterns

### Fast iteration during RTL development

```yaml
recipes: [balanced_resyn, orfs_speed]
run_sta: false
run_gls: false
parallel: 4
```

### Final characterization

```yaml
# All 14 recipes, full STA, full GLS
objective: pareto    # see the trade-off space
parallel: 0          # use all cores
```

### Just one module under a tight period

```yaml
modules: [cpu_core]
recipes: [delay_triple, delay_iter_heavy, delay_retime]
period_ps: 4000
objective: delay
```

### Area-driven for a peripheral

```yaml
modules: [gpio]
objective: area
recipes: [area_safe, area_classic, area_lut6, area_max]
period_ps: 20000     # loose period; area matters more
```

### CI-friendly

```yaml
fail_on_timing: true   # exit 2 on any setup violation
parallel: 0
recipes: [balanced_resyn]    # one recipe = fast CI
```

Then in CI:

```bash
synth_flow.py --config synth.yaml
case $? in
  0) echo "OK" ;;
  1) echo "synth failed"; exit 1 ;;
  2) echo "timing failed"; exit 1 ;;
  3) echo "GLS failed";    exit 1 ;;
  4) echo "config error";  exit 1 ;;
esac
```

## Schema-ish summary (cheat sheet)

```yaml
# Required
rtl_files:        [<path>, ...]
lib_typ:          <path>
top:              <ident>

# STA (required when run_sta: true)
lib_fast:         <path>
lib_slow:         <path>

# GLS (required when run_gls: true)
tb_files:         [<path>, ...]
tb_top:           <ident>
primitives_dir:   <path>

# Design
period_ps:        <int>          # default 10000
clock_port:       <ident>        # default "clk"
objective:        delay | area | fastest | pareto | balanced   # default "delay"
modules:          [<ident>, ...] # default [] -> auto-detect
recipes:          [<name>, ...]  # default [] -> all in recipes_dir

# ABC
driving_cell:     <ident>        # default "sky130_fd_sc_hd__inv_2"
load_ff:          <float>        # default 17.65

# Tools
yosys:            <path>         # default "yosys"
abc:              <path>         # default "abc" (currently unused)
opensta:          <path>         # default "sta"
iverilog:         <path>         # default "iverilog"
vvp:              <path>         # default "vvp"

# Execution
parallel:         <int>          # default 0 -> os.cpu_count()
work_dir:         <path>         # default "work"
results_dir:      <path>         # default "results"
recipes_dir:      <path>         # default <script_dir>/recipes

# Behavior
run_sta:          <bool>         # default true
run_gls:          <bool>         # default true
fail_on_timing:   <bool>         # default true
sdf_back_annotate: <bool>        # default true

# Experimental
abc_sequential:   <bool>         # default false
```
