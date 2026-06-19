<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Author: Mohamed Shalan <mshalan@aucegypt.edu> -->

# synth_flow — ASIC Synthesis + STA Orchestrator

A Python tool that automates synthesis sweeps, multi-corner STA, and
gate-level simulation for ASIC designs using **Yosys + ABC**, **OpenSTA**, and
**iverilog**.

## Features

- **Recipe sweep** — runs multiple ABC optimization recipes in parallel,
  picks the best result per module using the slow-corner (SS) for WNS ranking
- **15 built-in recipes** — delay, balanced, area, and specialty strategies
  (all verified on ABC 1.01+)
- **5 optimization objectives** — `delay`, `area`, `fastest`, `pareto`,
  `balanced`
- **Multi-corner STA** — SS (setup), TT (setup+hold), FF (hold) via OpenSTA
- **Hierarchical (bottom-up) synthesis** — leaf modules first, winning
  netlists reused by parents
- **Gate-level simulation** — iverilog + vvp with optional SDF
  back-annotation
- **Multi-clock STA** — defines two asynchronous clock groups with
  independent periods for setup/hold analysis
- **Verilog defines** — `-D` flags passed to `read_verilog`
- **Pre-read netlists** — pre-mapped IP (e.g. DFFRAM) loaded with liberty
  before RTL, preserved via `keep_hierarchy_modules`
- **Verilog parameter overrides** — `params` config field maps to Yosys
  `-chparam`
- **Pareto front analysis** — identifies area-delay trade-off candidates
- **Depth-only mode** — fast Fmax estimate without full synthesis
- **Output formats** — Markdown, JSON, and CSV reports

## Quick Start

### 1. Create a config file (`synth.yaml`)

```yaml
rtl_files: [rtl/*.v]
lib_typ:   sky130/hd_120_tt.lib
lib_slow:  sky130/hd_120_ss.lib
top: my_design
period_ps: 8000
clock_port: clk
objective: pareto
run_gls: false
```

Full config reference: [docs/yaml-config.md](docs/yaml-config.md).

### 2. Run synthesis

```bash
# Flat sweep (default)
python3 synth_flow.py --config synth.yaml

# Hierarchical bottom-up
python3 synth_flow.py --config synth.yaml --hierarchical

# Specific recipes only
python3 synth_flow.py --config synth.yaml --recipes area_safe balanced_resyn

# Fast depth-only Fmax estimate (no ABC)
python3 synth_flow.py --config synth.yaml --depth-only
```

### Dual-clock design (e.g. AttoIO)

```yaml
period_ps: 8000
clock_port: sysclk
clock_port_2: clk_iop
period_ps_2: 33333
verilog_defines: [NRV_SINGLE_PORT_REGF, NRV_SHARED_ADDER, NRV_SERIAL_SHIFT]
pre_read_files: [models/dffram_gen/dffram_combined.nl.v, models/dffram_gen/dffram_wrapper.v]
keep_hierarchy_modules: [DFFRAM, RAM128, RAM32]
```

When `clock_port_2` is set, STA creates two asynchronous clock groups.
Synthesis still targets `clock_port` / `period_ps` for ABC (conservative
for the second domain). Set `dual_clock_synthesis: true` to enable
experimental clock-domain-partitioned ABC (`abc -dff` per domain).

## CLI Reference

| Flag | Description |
|------|-------------|
| `--config FILE` | YAML config file |
| `--rtl PATTERN` | RTL files/globs (repeatable) |
| `--lib FILE` | Typical-corner liberty |
| `--lib-fast FILE` | Fast-corner liberty |
| `--lib-slow FILE` | Slow-corner liberty |
| `--macro-lib FILE` | Hard-macro liberty (SRAM, PLL, …). Repeatable. Applied to all STA corners. Use the YAML `macro_libs:` dict for per-corner files. |
| `--top NAME` | Top module name |
| `--period-ps N` | Clock period in picoseconds |
| `--clock-port NAME` | Clock port name (default: `clk`) |
| `--objective OBJ` | `delay`, `area`, `fastest`, `pareto`, `balanced` |
| `--modules M1 M2` | Modules to synthesize (default: auto-detect) |
| `--recipes R1 R2` | Recipes to sweep (default: all) |
| `--driving-cell CELL` | ABC driving cell |
| `--load-ff LOAD` | ABC load in fF |
| `--parallel N` | Worker count (0 = auto) |
| `--no-sta` | Skip multi-corner STA |
| `--no-gls` | Skip gate-level simulation |
| `--abc-sequential` | Enable ABC `-dff` (experimental) |
| `--hierarchical` | Bottom-up synthesis |
| `--depth-only` | Fast depth-based Fmax estimate |
| `--list-modules` | Print auto-detected modules and exit |
| `--work-dir DIR` | Working directory (default: `work`) |
| `--results-dir DIR` | Results directory (default: `results`) |

## Hard macros (SRAM, PLL, …)

Designs with hard macros need their `.lib` files loaded alongside the
standard cell library so that:

1. Yosys recognises the macro name as a real cell instead of an unknown
   blackbox (no orphan instances after `dfflibmap`).
2. OpenSTA gets timing arcs for paths that touch the macro — without
   this the SRAM input setup, clock-to-Q, and output transition are all
   silently zero, producing optimistic WNS and missed setup violations.

Configure via the `macro_libs` YAML field. Two formats:

```yaml
# Format A — flat list, used in every STA corner.
macro_libs:
  - $PDK_ROOT/sram_macro/lib/sram_tt.lib
  - $PDK_ROOT/pll/lib/pll_tt.lib
```

```yaml
# Format B — per-corner. Each corner reads its matching PVT model.
# Missing corners fall back to `typ`.
macro_libs:
  typ:  [path/to/sram_tt_180V_25C.lib]
  fast: [path/to/sram_ff_195V_n40C.lib]
  slow: [path/to/sram_ss_160V_100C.lib]
```

What happens under the hood:

- **Yosys synth (all driver flavors):** emits `read_liberty -lib <file>`
  for each `typ` macro lib before `read_verilog`. The `-lib` flag tells
  Yosys these are blackbox cells; their internals are not synthesised.
- **OpenSTA winner-selection STA (`_quick_sta`):** emits
  `read_liberty <file>` for each `slow`-corner macro lib (or `typ` if no
  slow lib is set) so WNS ranking is timing-accurate.
- **OpenSTA multi-corner STA:** emits `read_liberty -corner fast|typical|slow <file>`
  for each macro lib in each corner.

CLI:

```bash
python3 synth_flow.py --config synth.yaml \
        --macro-lib path/sram_tt.lib --macro-lib path/pll_tt.lib
```

CLI form is flat-list only; use the YAML dict for per-corner control.

## Recipes

15 ABC scripts in `recipes/*.abc`, all compatible with ABC 1.01+. Each recipe
uses only commands confirmed available: `strash`, `ifraig`, `scorr`, `dc2`,
`dretime`, `balance`, `rewrite`, `refactor`, `dch`, `map`, `mfs`, and the GIA
subset (`&get`, `&st`, `&dch`, `&nf`, `&put`, `&scl`, `&lcorr`, `&if`,
`&syn2`, `&b`, `&mfs`).

| Recipe | Class | Strategy | Runtime |
|--------|-------|----------|---------|
| `delay_retime` | Delay | Retiming + double-pass GIA mapping | 1.3× |
| `delay_triple` | Delay | Triple-pass remap with sizing | 1.4× |
| `delay_choice_deep` | Delay | Choice-driven, high conflict limit | 1.0× |
| `delay_iter_heavy` | Delay | Quadruple-pass explicit unrolling | 1.7× |
| `balanced_resyn` | Balanced | Inlined resyn2 + single GIA map | 1.0× |
| `balanced_resyn2x` | Balanced | Two rewriting passes + double map | 1.4× |
| `balanced_struct` | Structural | GIA structural cleanup (`&scl`, `&lcorr`) | 1.0× |
| `area_safe` | Area | Full AIG cleanup + retiming + GIA mapping | 1.2× |
| `area_classic` | Area | Rewriting + scorr/dc2 + GIA mapping | 1.1× |
| `area_lut6` | Area | Heavy scorr + dc2 + dretime + rewriting | 1.2× |
| `area_max` | Area | Double everything + retiming + double map | 1.3× |
| `lazy_man` | Heavy | PULP-style: 8 opt + 8 opt+map iterations | 1.0× |
| `lms` | Heavy | PULP LMS port: opt iters + placement-aware buffering | 1.1× |
| `orfs_speed` | Reference | ORFS/OpenLane DELAY 0 port | 0.8× |
| `yosys_default` | Reference | Yosys default flow baseline | 0.8× |

Runtime multipliers are relative to `balanced_resyn` on a typical Sky130 HD
module. Add custom recipes by dropping `<name>.abc` into `recipes/`.

## Objectives

| Objective | Selection Strategy |
|-----------|-------------------|
| `delay` | Maximize WNS (SS corner), then minimize area |
| `area` | Minimize area among timing-meeting recipes, fallback to max WNS |
| `fastest` | Max WNS among timing-meeting |
| `balanced` | Equal-weight rank score on WNS and area |
| `pareto` | Reports Pareto front, picks max-WNS representative |

**Note:** WNS ranking uses `lib_slow` (SS corner) by default so the winner
meets timing at worst-case conditions. Set `objective: pareto` to see the
full trade-off space.

## Hierarchical Mode

When `--hierarchical` is enabled:

1. Auto-detects module dependencies from RTL instantiation patterns
2. Topological sort ensures leaf modules are synthesized first
3. Each module gets its own recipe sweep
4. Parent modules read sub-module winning netlists (gate-level)
5. `cell_blackbox` resolves library cell references

## Output

```
results/
  summary.md          # Human-readable report
  summary.json        # Machine-readable data
  summary.csv         # Per-recipe CSV
  <module>/
    winner.v          # Winning netlist
    winner.sdf        # SDF (from multi-corner STA)
    selection.json    # Winner selection details
```

## Project Structure

```
synth_flow/
  synth_flow.py       # Main orchestrator
  area_report.py      # Cell count + area report utility
  test_synth_flow.py  # Unit tests (no EDA tools needed)
  recipes/            # 14 ABC recipe scripts
  sky130/             # Curated Sky130 HD PDK subset
    hd_120_tt.lib     # Stripped TT liberty (synthesis)
    hd_120e_*.lib     # ss/tt/ff + edfxtp/edfxbp enable flops restored
    abc_constr.txt    # ABC constraints
    sky130_hd-clean.v # Behavioral Verilog (GLS)
  docs/
    yaml-config.md    # Full config reference
  examples/
    synth.yaml        # Example configuration
```

## Standard-cell libraries

`sky130/hd_120_{ss,tt,ff}.lib` are stripped corner subsets. The companion
`sky130/hd_120e_{ss,tt,ff}.lib` add the `edfxtp`/`edfxbp` **enable flip-flops**
restored from the full Sky130 HD PDK at the matching corners (`ss_100C_1v60` /
`tt_025C_1v80` / `ff_n40C_1v95`). Without an enable flop, a clock-enable register
(`if (en) q <= d`) maps to a plain DFF **plus a feedback mux in the data path**;
with `hd_120e_*` it maps to a single `edfxtp` (enable on the CE pin) — smaller and
faster for enabled / register-heavy datapaths. Point `lib_typ` / `lib_slow` /
`lib_fast` at the `hd_120e_*` files to use them.

## Requirements

- Python 3.10+
- [Yosys](https://github.com/YosysHQ/yosys) (synthesis)
- ABC (bundled with Yosys)
- [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) (STA)
- [iverilog](https://github.com/steveicarus/iverilog) (GLS, optional)
- PyYAML (`pip install pyyaml`)

## Testing

```bash
python3 test_synth_flow.py   # Pure-Python unit tests, no tools needed
```

## License

Apache-2.0 — see [LICENSE](../LICENSE).
