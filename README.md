<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Author: Mohamed Shalan <mshalan@aucegypt.edu> -->

# synth_flow — ASIC Synthesis + STA Orchestrator

A Python tool that automates synthesis sweeps, multi-corner STA, and
gate-level simulation for ASIC designs using **Yosys + ABC**, **OpenSTA**, and
**iverilog**.

## Features

- **Recipe × front-end sweep** — runs multiple ABC recipes, optionally across
  Yosys front-end variants (Booth multipliers, Kogge-Stone / Han-Carlson /
  Sklansky adders), and picks the best result per module using the
  slow-corner (SS) for WNS ranking
- **18 built-in recipes** — delay, balanced, and area strategies, pruned and
  extended against a 16-design STA benchmark (retired ones in `recipes/retired/`)
- **5 optimization objectives** — `delay`, `area`, `fastest`, `pareto`,
  `balanced`
- **SDC in, SDC out** — one SDC drives OpenSTA verbatim and synthesis
  (clocks, boundary conditions); `results/<module>/synth.sdc` shows what
  synthesis used ([docs/sdc-support.md](docs/sdc-support.md))
- **Named scenarios, constraint hook, clock budget** — `scenarios:` ranks on
  one SDC and accepts only candidates passing every required scenario ×
  corner × check type (setup, hold, recovery, removal, fixed bounds); a Tcl
  hook runs after `link_design` in every STA with a binding audit that fails
  the run on unresolved objects; uncertainty from an explicit jitter/skew
  budget, components reported ([docs/yaml-config.md](docs/yaml-config.md))
- **STA-guided repairs** — `--resize` (drive strengths), `--repair-design`
  (buffer trees on high-fanout nets) and `--repair-hold` (delay cells on hold
  violations) with OpenSTA as the judge and function preserved by construction
- **Any liberty, any Sky130 variant** — cell choices for sizing and repairs
  (drive families, buffers, delay and driving cells) come from the liberty,
  not from name tables; `sky130_fd_sc_hd`, `hs`, `ms`, `ls`, `lp`, `hvl` and
  hard macro liberties (OpenRAM `bus()` pins) work out of the box, and an HD
  SDC is adapted to the target library
- **Several libraries at once** — `lib_typ: [hs.lib, ls.lib]` maps with the
  union of the cells; the post-pass swaps cells between libraries (faster
  library on failing paths, slower one off-critical with `--recover-area`)
  and reports leakage and the per-library mix ([docs/yaml-config.md](docs/yaml-config.md))
- **Multi-corner STA** — SS (setup), TT (setup+hold), FF (hold) via OpenSTA
- **Power report** — OpenSTA `report_power` at the nominal corner: dynamic
  (internal + switching) and static (leakage) per cell group, from a uniform
  activity assumption or a VCD/SAIF of your simulation
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

## SDC support

One SDC file does two jobs. OpenSTA sources it verbatim (after the tool's
defaults) for the ranking STA, every repair decision and the sign-off
corners, so anything OpenSTA accepts is honored where timing is judged.
Synthesis reads the same file and uses the subset below to drive Yosys/ABC;
`results/<module>/synth.sdc` records what it understood.

| SDC command | Used by synthesis for | Also in STA |
|---|---|---|
| `create_clock -period T [get_ports P]` | Period and clock port of the fastest primary clock (ranking STA, ABC budgets when `abc_target` is set). A second `create_clock` defines the second domain. | ✅ |
| `create_generated_clock` | Parsed; the domain is timed by OpenSTA. No separate mapping budget. | ✅ |
| `set_clock_uncertainty -setup` / `-hold` | Uncertainty values for STA and budgets | ✅ |
| `set_clock_groups -asynchronous` / `-exclusive` | Timed as declared by OpenSTA (no logic partitioning for mapping) | ✅ |
| `set_input_delay` / `set_output_delay` (`-max`, `-min`) | Boundary timing for ranking and repairs | ✅ |
| `set_false_path`, `set_multicycle_path`, `set_max_delay`, `set_min_delay` | Exceptions applied in STA; they decide winners and repairs, not the mapping | ✅ |
| `set_driving_cell -lib_cell X [ports]` | ABC boundary model (`-constr`). A cell from another library is mapped to the same-named cell of the synthesis library | ✅ |
| `set_load L [ports]` | ABC boundary load (`-max` value) | ✅ |
| `set_max_fanout N` | Sink group size of `--repair-design` buffer trees | — |
| `set_dont_use [cells]` | Cells removed from what `abc` / `dfflibmap` may pick (globs allowed) | — |
| `set_dont_touch [instances]` | Module instances kept through `synth -flatten` | — |
| `set_case_analysis`, `set_disable_timing`, `set_input_transition`, `set_max_transition`, `set_max_capacitance`, `set_clock_latency`, `set_propagated_clock`, `set_timing_derate`, `group_path`, … | STA only, listed in the log | ✅ |

Object queries run in a real Tcl interpreter: variables, `expr`, `foreach`,
wildcards, `get_ports`, `get_clocks`, `all_inputs -no_clocks`,
`all_outputs`, `all_registers`, and `get_pins` / `get_cells` on registers
(register names survive synthesis; internal combinational names do not).
Async-reset false paths are derived automatically from register async pins;
your own `set_false_path` lines are applied as well.

### Examples

A single-clock block with boundary conditions (the bench designs use this form):

```tcl
set T 5.0
create_clock -name PCLK -period $T [get_ports PCLK]
set_clock_uncertainty -setup 0.25 [get_clocks PCLK]
set_clock_uncertainty -hold  0.10 [get_clocks PCLK]
set_false_path -from [get_ports PRESETn]
set_input_delay  -clock PCLK -max [expr 0.25 * $T] [all_inputs -no_clocks]
set_input_delay  -clock PCLK -min [expr 0.10 * $T] [all_inputs -no_clocks]
set_output_delay -clock PCLK -max [expr 0.25 * $T] [all_outputs]
set_output_delay -clock PCLK -min [expr 0.10 * $T] [all_outputs]
set_driving_cell -lib_cell sky130_fd_sc_hd__inv_1 [all_inputs -no_clocks]
set_load 0.033 [all_outputs]
```

Two asynchronous clocks, a divided clock and a CDC exception:

```tcl
create_clock -name sysclk -period 10.0 [get_ports sysclk]
create_clock -name spiclk -period 40.0 [get_ports sck]
create_generated_clock -name clk_div2 -source [get_ports sysclk] -divide_by 2 [get_pins u_div/q_reg/Q]
set_clock_groups -asynchronous -group {sysclk clk_div2} -group {spiclk}
set_max_delay 8.0 -from [get_cells sync_*/meta_reg] -to [get_cells sync_*/sync_reg]
set_multicycle_path -setup 2 -to [get_cells acc_reg*]
set_multicycle_path -hold  1 -to [get_cells acc_reg*]
set_input_delay  -clock spiclk -max 5.0 [get_ports {mosi cs_n}]
set_output_delay -clock spiclk -max 5.0 [get_ports miso]
```

Steering the mapper and the repairs:

```tcl
set_dont_use [get_lib_cells sky130_fd_sc_hd__lpflow_*]
set_dont_use [get_lib_cells {sky130_fd_sc_hd__probe* sky130_fd_sc_hd__sdlclkp*}]
set_max_fanout 6 [current_design]
set_dont_touch [get_cells u_dffram]
set_driving_cell -lib_cell sky130_fd_sc_hd__buf_4 [get_ports clk]
set_case_analysis 0 [get_ports scan_en]
```

Precedence and the full list: [docs/sdc-support.md](docs/sdc-support.md).

## CLI Reference

| Flag | Description |
|------|-------------|
| `--config FILE` | YAML config file |
| `--rtl PATTERN` | RTL files/globs (repeatable) |
| `--lib FILE[,FILE…]` | Typical-corner liberty (a comma list loads several libraries) |
| `--lib-fast FILE` | Fast-corner liberty |
| `--lib-slow FILE` | Slow-corner liberty |
| `--macro-lib FILE` | Hard-macro liberty (SRAM, PLL, …). Repeatable. Applied to all STA corners. Use the YAML `macro_libs:` dict for per-corner files. |
| `--top NAME` | Top module name |
| `--period-ps N` | Clock period in picoseconds |
| `--clock-port NAME` | Clock port name (default: `clk`) |
| `--sdc FILE` | SDC file: sourced by OpenSTA and read for synthesis clocks and boundary conditions (overrides `--period-ps`/`--clock-port`) |
| `--abc-target T` | ABC `-D`: `none` (default), `period`, `reg2reg`, or ps. Measured: `none` is best (docs/architecture.md §2.6) |
| `--resize` | OpenSTA-guided drive-strength sizing of each winner (`winner.presize.v` keeps the input) |
| `--repair-design` | Buffer trees on high-fanout nets of failing paths, judged by OpenSTA (`--max-fanout N`, default 8; SDC `set_max_fanout` overrides) |
| `--repair-hold` | Delay cells on failing hold endpoints at the fast corner, kept only while slow-corner setup holds |
| `--recover-area` | After the winner meets timing, downsize or swap off-critical cells to a slower library while WNS holds (implies `--resize`) |
| `--resize-candidates N` | Post-pass on the N best candidates (selected, fastest, Pareto front), then select again (implies `--resize`) |
| `--strict` | Exit 6 when a module is NOT CLOSED under its required scenarios or misses setup (a post-pass phase failure always exits 5) |
| `--yosys-opts T...` | Front-end options: `booth`, `adder=kogge-stone\|han-carlson\|sklansky`, `noshare`, `hieropt`, `opt_dff_sat`, `opt_full`. Sweep several, globally or per module, with `yosys_opts_sweep` in YAML |
| `--objective OBJ` | Recipe subset to run: `delay`, `area`, `balanced` (default). Selection is always min-area-meeting-timing |
| `--full-sweep` | Run every recipe instead of the objective subset |
| `--select-margin-ps N` | WNS a candidate needs to count as meeting timing (default 0) |
| `--fallback knee\|best_wns` | Pick when nothing meets timing: knee of the WNS/area front (default) or the fastest netlist |
| `--parallel N` | Workers for synthesis and quick STA (0 = all cores) |
| `--modules M1 M2` | Modules to synthesize (default: auto-detect) |
| `--recipes R1 R2` | Recipes to sweep (default: the objective's subset) |
| `--driving-cell CELL` | ABC driving cell |
| `--load-ff LOAD` | ABC load in fF |
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
3. The post-pass (`resize_winner`, `repair_design`, `repair_hold`) sees the
   same arcs and treats macro instances as drivers and sinks, so a
   high-fanout SRAM output can be buffered and a path into an SRAM data
   pin sized like any other.

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

18 ABC scripts in `recipes/*.abc`, all compatible with ABC 1.01+. Each recipe
uses only commands confirmed available: `strash`, `ifraig`, `scorr`, `dc2`,
`dretime`, `balance`, `rewrite`, `refactor`, `dch`, `map`, `mfs`, and the GIA
subset (`&get`, `&st`, `&dch`, `&nf`, `&put`, `&scl`, `&lcorr`, `&if`,
`&syn2`, `&b`, `&mfs`).

| Recipe | Class | Strategy | Runtime |
|--------|-------|----------|---------|
| `delay_map` | Delay | `dch -f` choices + classic supergate `map`; best WNS on 12/16 bench designs, +8..60 % area | 1.0× |
| `delay_map_resyn` | Delay | resyn2 cleanup, then `dch -f; map` | 1.3× |
| `delay_syn2` | Delay | `&syn2` restructuring before choices + `&nf` | 1.0× |
| `delay_triple` | Delay | Triple-pass remap with sizing | 1.4× |
| `delay_choice_deep` | Delay | Choice-driven (`&dch; &nf`), 2 sizing rounds | 1.0× |
| `delay_choice_deep_v3` | Delay | `&dch -f` (more choices); best mean WNS rank in the baseline | 1.0× |
| `delay_choice_deep_v4` | Delay | `&b` before `&dch` | 1.0× |
| `delay_choice_deep_bb` | Delay | Double `&b` before `&dch` | 1.0× |
| `delay_aggressive` | Delay | Full cleanup + 4-pass mapping + aggressive sizing | 1.7× |
| `delay_iter_heavy` | Delay | Quadruple-pass explicit unrolling | 1.7× |
| `balanced_resyn` | Balanced | Inlined resyn2 + single GIA map | 1.0× |
| `balanced_resyn2x` | Balanced | Two rewriting passes + double map | 1.4× |
| `orfs_area` | Area | ORFS AREA-style: `&syn2; &if -g; &synch2; &nf` | 1.0× |
| `area_classic` | Area | Rewriting + scorr/dc2 + GIA mapping | 1.1× |
| `area_lut6` | Area | Heavy scorr + dc2 + dretime + rewriting | 1.2× |
| `area_max` | Area | Double everything + retiming + double map | 1.3× |
| `orfs_speed` | Reference | ORFS/OpenLane DELAY 0 port | 0.8× |
| `yosys_default` | Reference | Yosys default flow baseline | 0.8× |

Runtime multipliers are relative to `balanced_resyn` on a typical Sky130 HD
module. Add custom recipes by dropping `<name>.abc` into `recipes/`. Recipes
retired after benchmarking live in `recipes/retired/` with the reasons.

## Objectives

`objective` chooses **which recipes run**; the winner is always chosen the
same way: the candidate that meets timing (slow-corner WNS ≥
`select_margin_ps`) with the least area. When nothing meets, the knee of the
WNS/area front is returned (`fallback: knee`; `best_wns` returns the fastest
netlist regardless of area). The subsets are the top-5 of each leaderboard on the
16-design bench (`docs/benchmarks.md`).

| Objective | Recipes run | When to use |
|-----------|-------------|-------------|
| `delay` (default) | `delay_map_resyn`, `delay_map`, `orfs_area`, `delay_choice_deep_v3`, `delay_syn2` | closing a clock period; closes as many bench designs as the full sweep in 1/8 of the time |
| `area` | `delay_aggressive`, `area_lut6`, `area_max`, `yosys_default`, `area_classic` | relaxed period, smallest netlist |
| `balanced` | `balanced_resyn`, `balanced_resyn2x`, `delay_triple`, `delay_iter_heavy`, `delay_map_resyn` | general use |
| `--full-sweep` | all 18 | final characterization |

`--recipes R1 R2` overrides the subset. `fastest` and `pareto` are accepted
as aliases of `delay` and `balanced` for old configs. With `yosys_opts_sweep`
each recipe also runs once per front-end variant.

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
  liberty_timing.py   # Liberty reader: flop timing + LibCells cell catalogue
  resize.py           # OpenSTA-guided sizing, buffer trees, hold repair
  area_report.py      # Cell count + area report utility
  test_synth_flow.py  # Unit tests (no EDA tools needed)
  recipes/            # 18 ABC recipe scripts (+ retired/)
  sky130/             # Curated Sky130 HD PDK subset
    hd_120_tt.lib     # Stripped TT liberty (synthesis)
    abc_constr.txt    # ABC constraints
    sky130_hd-clean.v # Behavioral Verilog (GLS)
  bench/              # Benchmark suite (designs, manifest, runner)
  docs/               # Architecture, SDC support, CLI spec, benchmarks, roadmap
  examples/
    synth.yaml        # Example configuration
```

## Results

On the 16-design benchmark (Sky130 HD, SS corner, per-design SDC), the full
flow (front-end sweep × recipe sweep × OpenSTA-guided sizing) against the
ORFS/OpenLane reference (plain Yosys, `orfs_speed`, no sizing):

| | ORFS reference | `objective: delay` (default, 5 recipes) | `--full-sweep` (18 recipes) |
|---|---|---|---|
| designs meeting timing | 3 / 16 | 16 / 16 | 16 / 16 |
| mean ΔWNS | — | +0.76 ns | +0.44 ns |
| mean Δarea | — | +5.3 % | +2.1 % |
| wall time per design (18 cores, incl. STA and sizing) | — | 6 s (2 to 30 s) | ~40 s |

Winner rule in both: the candidate that meets timing with the least area. Both
sweep 6 front-end variants per recipe and finish with OpenSTA-guided sizing.

Per-design numbers and every intermediate experiment (including the negative
ones) are in [docs/benchmarks.md](docs/benchmarks.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/yaml-config.md](docs/yaml-config.md) | Full YAML config reference |
| [docs/architecture.md](docs/architecture.md) | How the flow works, verified findings about Yosys/ABC and constraints, target timing-driven architecture |
| [docs/sdc-support.md](docs/sdc-support.md) | Which SDC commands synthesis uses, which are STA-only, precedence over YAML |
| [docs/cli.md](docs/cli.md) | Target CLI, configuration keys, outputs, exit codes, Python API |
| [docs/benchmarks.md](docs/benchmarks.md) | Benchmark suite: designs, metrics, running and comparing |
| [docs/roadmap.md](docs/roadmap.md) | Phased plan with deliverables and acceptance criteria |

## Benchmarks

`bench/` holds 16 designs (12 in-house, 4 external shalan/* IPs at pinned
commits) and a runner that sweeps recipes and writes CSV/Markdown reports.

```bash
cd bench
./fetch_external.sh                 # once
./bench.py --quick --tag before     # 4 representative recipes
./bench.py --compare results/before.csv results/after.csv
```

See [docs/benchmarks.md](docs/benchmarks.md).

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
