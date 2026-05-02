<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Author: Mohamed Shalan <mshalan@aucegypt.edu> -->

# synth_flow — ASIC Synthesis + STA Orchestrator

A Python tool that automates synthesis sweeps, multi-corner STA, and
gate-level simulation for ASIC designs using **Yosys + ABC**, **OpenSTA**, and
**iverilog**.

## Features

- **Recipe sweep** — runs multiple ABC optimization recipes in parallel,
  picks the best result per module using the slow-corner (SS) for WNS ranking
- **14 built-in recipes** — delay, balanced, area, and specialty strategies
  (all verified on ABC 1.01+)
- **5 optimization objectives** — `delay`, `area`, `fastest`, `pareto`,
  `balanced`
- **Multi-corner STA** — SS (setup), TT (setup+hold), FF (hold) via OpenSTA
- **Hierarchical (bottom-up) synthesis** — leaf modules first, winning
  netlists reused by parents
- **Gate-level simulation** — iverilog + vvp with optional SDF
  back-annotation
- **Verilog parameter overrides** — `params` config field maps to Yosys
  `-chparam`
- **Pareto front analysis** — identifies area-delay trade-off candidates
- **Depth-only mode** — fast Fmax estimate without full synthesis
- **Output formats** — Markdown, JSON, and CSV reports

## Quick Start

### 1. Create a config file (`synth.yaml`)

```yaml
rtl_files: [rtl/*.v]
lib_typ:   lib/sky130_fd_sc_hd__tt.lib
lib_fast:  lib/sky130_fd_sc_hd__ff.lib
lib_slow:  lib/sky130_fd_sc_hd__ss.lib
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

## CLI Reference

| Flag | Description |
|------|-------------|
| `--config FILE` | YAML config file |
| `--rtl PATTERN` | RTL files/globs (repeatable) |
| `--lib FILE` | Typical-corner liberty |
| `--lib-fast FILE` | Fast-corner liberty |
| `--lib-slow FILE` | Slow-corner liberty |
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

## Recipes

14 ABC scripts in `recipes/*.abc`, all compatible with ABC 1.01+. Each recipe
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
    hd_124_tt.lib     # Stripped TT liberty (synthesis)
    abc_constr.txt    # ABC constraints
    sky130_hd-clean.v # Behavioral Verilog (GLS)
  docs/
    yaml-config.md    # Full config reference
  examples/
    synth.yaml        # Example configuration
```

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
