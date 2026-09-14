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
| `lib_typ` | string or list | Path to typical-corner liberty file. Final fallback for synthesis when neither `lib_synth` nor `lib_slow` is set. A **list** loads several standard-cell libraries at once (see *Several libraries at once* below). |
| `top` | string | Name of the project top module. Used as the GLS netlist filename and report title. Does not need to be in `modules` (auto-detection still scans it). |

### Required for STA

These are required only when `run_sta: true` (the default). Set
`run_sta: false` to skip STA entirely.

| Field | Type | Description |
|---|---|---|
| `lib_fast` | string or list | Fast-corner liberty (lowest delay, used for hold checks). |
| `lib_slow` | string or list | Slow-corner liberty (highest delay, used for setup checks). **Also the default synthesis library** — Yosys/ABC see SS-corner cell delays during mapping. Override with `lib_synth`. |

### Optional synthesis-library override

| Field | Type | Description |
|---|---|---|
| `mixed_map` | string | `fastest` | With several libraries: `fastest` maps with the fastest library only, `all` offers every cell to ABC (see below). |
| `lib_synth` | string or list | Explicit liberty for synthesis (Yosys / dfflibmap / ABC / stat). Bypasses the default `lib_slow → lib_typ` resolution. Use `lib_synth: <lib_typ_path>` to fall back to the older optimistic-synth-at-TT flow. |

#### Several libraries at once

```yaml
lib_typ:  [$PDK/sky130_fd_sc_hs/lib/sky130_fd_sc_hs__tt_025C_1v80.lib, $PDK/sky130_fd_sc_ls/lib/sky130_fd_sc_ls__tt_025C_1v80.lib]
lib_slow: [$PDK/sky130_fd_sc_hs/lib/sky130_fd_sc_hs__ss_100C_1v60.lib, $PDK/sky130_fd_sc_ls/lib/sky130_fd_sc_ls__ss_100C_1v60.lib]
lib_fast: [$PDK/sky130_fd_sc_hs/lib/sky130_fd_sc_hs__ff_n40C_1v95.lib, $PDK/sky130_fd_sc_ls/lib/sky130_fd_sc_ls__ff_n40C_1v95.lib]
resize_winner: true
resize_recover_area: true
```

The first file of each list is the primary library (wire-load model, flop
timing for budgets); the others are kept in `lib_extra` per corner, read into
every OpenSTA session and loaded into the post-pass. The flow ranks the
libraries by the delay of their inverters and buffers (from the liberty, so
HS < MS < LP < LS on Sky130) and applies one rule: **critical paths use fast
cells only, slow cells go where there is slack.**

- **Mapping** (`mixed_map: fastest`, default): Yosys/ABC map with the fastest
  library alone, so ABC never trades a critical-path cell for an equal-area
  slow one. `mixed_map: all` passes every library to `dfflibmap`, `abc` and
  `stat` (`-liberty` repeated) and lets ABC pick from the union.
- **Timing repair**: on a failing path the slowest stage is first replaced by
  the same cell in the *fastest* library (same pins, same function, no area
  change), then upsized.
- **Recovery** (`resize_recover_area`): cells farther than 300 ps from the
  worst slack move to the next *slower* library before being downsized, in
  batches kept only while WNS holds and TNS does not drop.

`resize.json` reports leakage and the instance count per library before and
after. Leakage is what the liberty states (`cell_leakage_power`, else the mean
of the `leakage_power` groups); the Sky130 LS/LP SS liberties report zero for
most combinational cells, so compare leakage across libraries with care. On
the CLI, `--lib a.lib,b.lib` (also `--lib-slow`, `--lib-fast`) does the same.

Only libraries that share a placement site and rail geometry can be mixed
in one design: on Sky130 that is `hs`, `ms`, `ls` and `lp` (0.48 × 3.33 µm
`unit` site); `hd`/`hdll` (2.72 µm) and `hvl` (4.07 µm, 3.3 V) stand alone.
The Vt implants differ between HS/MS (low-Vt NMOS) and LS/LP (high-Vt PMOS),
so a mixed layout needs its own DRC/LVS at cell boundaries; the PDK vendor
verified each library on its own. `sky130_fd_sc_hvl` works as a single
library (57 cells, 3.3 V nominal: `tt_025C_3v30` / `ss_100C_3v00` /
`ff_n40C_4v40`).

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
| `clock_port` | string | `clk` | Name of the primary clock port. Used for synthesis and STA. |
| `clock_port_2` | string or null | `null` | Name of a second (asynchronous) clock port. When set, STA creates two clock groups with `set_clock_groups -asynchronous`. |
| `period_ps_2` | int or null | `null` | Period in picoseconds for `clock_port_2`. Required when `clock_port_2` is set. |
| `objective` | enum | `delay` | Recipe selection criterion. One of: `delay`, `area`, `fastest`, `pareto`, `balanced`. See [Objectives](#objectives). |
| `modules` | list of strings | `[]` | Modules to synthesize separately. **Empty means auto-detect** — all modules defined in RTL but not instantiated by any other module are picked. Set this explicitly when auto-detection misses something or picks too many. |
| `recipes` | list of strings | `[]` | Subset of recipe names (without `.abc`) to sweep. **Empty means all available** in `recipes_dir`. Use this to restrict during fast iteration: `recipes: [balanced]`. |
| `verilog_defines` | list of strings | `[]` | Verilog `-D` flags passed to every `read_verilog` invocation. Example: `[NRV_SINGLE_PORT_REGF, NRV_SHARED_ADDER]`. |
| `pre_read_files` | list of strings | `[]` | Files read *before* RTL (after loading liberty). Used for pre-mapped IP netlists (e.g. DFFRAM) that reference library cells. Globs allowed. |
| `keep_hierarchy_modules` | list of strings | `[]` | Module names to mark with `keep_hierarchy` before `synth -flatten`. Preserves hand-crafted structures (e.g. DFFRAM) from being re-optimized by ABC. |

### ABC constraints

| Field | Type | Default | Description |
|---|---|---|---|
| `driving_cell` | string | `sky130_fd_sc_hd__inv_2` | Cell used in `set_driving_cell` for ABC's input boundary model and the STA preamble. If it is not in the synthesis liberty (another library, e.g. `sky130_fd_sc_hs`), the flow substitutes the same-named cell of that library (`sky130_fd_sc_hs__inv_2`), else its second-weakest plain inverter, and logs a warning, so the default works unchanged with any library. `-lib_cell` names in the user SDC are adapted the same way (docs/sdc-support.md → Precedence). |
| `load_ff` | float | `17.65` | Output load in **femtofarads** for ABC's `set_load`. The OpenLane Sky130 HD default. |

### ABC delay target

| Field | Type | Default | Notes |
|---|---|---|---|
| `abc_target` | string | `none` | ABC `-D` substituted for `{D}` in recipes. `none` removes it (minimum-delay mapping; measured best, docs/architecture.md §2.6); `period` = full clock period; `reg2reg` = `period − t_cq − t_su − clock_uncertainty_setup_ps`; or an integer in ps. Also `--abc-target`. |
| `min_budget_frac` | float | `0.25` | Floor for any derived target, as a fraction of the period. |
| `abc_wire_load` | bool | `false` | Add `-c` (liberty wire-load model) to ABC's `buffer`/`upsize`/`dnsize`/`stime` in every recipe. Already baked into `delay_map*`; neutral for `&nf` recipes (docs/architecture.md §2.9). Also `--abc-wire-load`. |
| `path_groups` | bool | `false` | EXPERIMENTAL: one `abc` call per path group with its own budget. Measured worse than flat mapping (docs/architecture.md §2.5); off by default. |
| `relaxed_factor` | float | `3.0` | `-D` multiplier for false-path cones when `path_groups` is on. |

### Yosys front end

| Field | Type | Default | Notes |
|---|---|---|---|
| `yosys_opts` | list or dict | `[]` | Front-end options passed to `synth` (also `--yosys-opts`, which replaces any sweep). Tokens: `booth` (`synth -booth`), `adder=kogge-stone` / `adder=han-carlson` / `adder=sklansky` (`synth -extra-map +/choices/<arch>.v`), `noshare`, `hieropt`, `nofsm`, `noalumacc`, and post-synth passes `opt_dff_sat` (`opt_dff -sat`), `opt_full` (`opt -full`). A dict `{module: [tokens], '*': [tokens]}` sets options per module (`'*'` is the default). |
| `yosys_opts_sweep` | list or dict | `[]` | Sweep several front ends: each entry is a `yosys_opts` list (`[]` = plain). Candidates are named `<recipe>@<variant>` and compete in the same winner selection. A dict `{module: [variants], '*': [variants]}` sweeps different variants per module (a datapath core can try Booth and adder architectures while a control block stays plain). Recommended global set: `[[], [adder=kogge-stone], [adder=han-carlson], [adder=sklansky], [booth], [booth, adder=kogge-stone]]`. |

### Library cell exclusion

| Field | Type | Default | Notes |
|---|---|---|---|
| `dont_use` | list | `[]` | Liberty cell names or glob patterns passed as `-dont_use` to `abc` and `dfflibmap` (also `--dont-use`; SDC `set_dont_use` entries are merged in). Required with a full PDK liberty, e.g. `['sky130_fd_sc_hd__lpflow_*', 'sky130_fd_sc_hd__probe*', 'sky130_fd_sc_hd__dly*', 'sky130_fd_sc_hd__clkdly*', 'sky130_fd_sc_hd__sdlclkp*']`. The bundled `hd_120` subset already excludes them. |

### Constraint scenarios, hook and clock budget

```yaml
scenarios:
  func:  {sdc: sdc/func.sdc, rank: true}          # ranking uses this one
  scan:  {sdc: sdc/scan.sdc, checks: [hold]}       # required, hold checks only
  sleep: {sdc: sdc/sleep.sdc, corners: [slow]}     # checked at the slow corner only
constraint_hook: sdc/post_map.tcl
clock_budget:
  clk: {jitter_ps: 50, skew_ps: 150, setup_margin_ps: 0, hold_margin_ps: 20, skew_post_cts_ps: 40}
cts_stage: pre_cts
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `scenarios` | map | `{}` | Named constraint scenarios: `sdc` (file), `rank` (exactly one `true`: ranking and synthesis use it), `required` (default `true`: governs acceptance), `corners` (subset of `slow`/`typ`/`fast`; omitted = every configured corner), `checks` (subset of `setup`, `hold`, `recovery`, `removal`; default all). A bare string is the SDC path. `sdc:` alone is the single scenario `default`. |
| `scenario_check_candidates` | int | `3` | How many rank-meeting candidates (smallest area first, the selected one first) are tried against the required checks before falling back. |
| `constraint_hook` | path | — | Tcl sourced by OpenSTA in **every** STA (ranking, post-pass, sign-off) after `link_design` and the scenario SDC. It sees the linked design and applies constraints directly. Variables: `synth_scenario`, `synth_corner`, `synth_module`. Procs: `require_binding NAME OBJECTS [-count N] [-min N] [-max N]` (records what resolved; nothing or a wrong count **fails the run**, exit code 7) and `optional_binding NAME OBJECTS`. The hook copy and every resolved binding are written to `results/<module>/constraint_hook.tcl` and `bindings.json`. |
| `clock_budget` | map | `{}` | Per clock (`'*'` = all): `jitter_ps`, `skew_ps`, `setup_margin_ps`, `hold_margin_ps`, `skew_post_cts_ps`. Uncertainty: **setup = jitter + skew + setup_margin**, **hold = skew + hold_margin**; with `cts_stage: post_cts` the skew term is `skew_post_cts_ps`. Applied per clock after the flat `clock_uncertainty_*_ps`; the components are written to `synth.sdc` and the summary. An SDC `set_clock_uncertainty` still wins (sourced later). |
| `cts_stage` | string | `pre_cts` | `pre_cts` or `post_cts`; selects the skew term above. |

**Acceptance rule.** With `scenarios:` given, the winner is the smallest-area
candidate that meets the ranking scenario **and** passes every required
scenario at each of its corners for each of its check types and path groups
(fixed `set_max_delay` / `set_min_delay` bounds included). If none passes,
`fallback` applies and the module is marked **NOT CLOSED** with every failing
`scenario@corner: check slack` (log, `selection.json` → `acceptance`,
summary). The same rule guards the post-pass: a sizing, buffering or hold
move is rejected when a required check that passed on the input netlist
would fail. After the post-pass the required checks run again on the
repaired netlists: with `resize_candidates > 1` only candidates passing them
are eligible for re-selection, and `postpass.json` carries `closed`,
`failing` and per-candidate `closed` / `failing`; with a single winner the
result lands in `selection.json` → `acceptance.after_postpass`. A module
that fails is reported NOT CLOSED at every stage, never as "meeting timing". Sign-off reports scenario × corner × check type (setup, hold,
recovery, removal, worst path group) for every scenario, required or not.
Ranking never uses the worst slack across scenarios: a fixed CDC bound and a
functional clock-period violation are reported as what they are.

### Post-pass: winner sizing

| Field | Type | Default | Notes |
|---|---|---|---|
| `resize_winner` | bool | `false` | Run `resize.py` on each module's winner before multi-corner STA (needs OpenSTA). Also `--resize`. Input kept as `winner.presize.v`; log in `resize.json`. |
| `resize_iters` | int | `25` | Sizing iterations (STA calls) in the TNS phase. |
| `resize_wns_tol_ps` | int | `150` | WNS regression tolerated for a TNS gain under the `tns` policy. |
| `resize_candidates` | int | `1` | Run the post-pass on the N most promising candidates (the selected one, the fastest, then the Pareto front) and select again with the same rule on the post-pass numbers. Catches a fast candidate that only closes after sizing. Per-candidate before/after in `results/<module>/postpass.json`. Also `--resize-candidates N` (implies `--resize`). |
| `repair_hold_max_paths` | int | `max_paths` (200) | Failing hold endpoints listed per STA in the hold phase. |
| `repair_hold_sta_budget` | int | `60` | OpenSTA calls the hold phase may spend (two per trial batch); the phase stops with status `ok (budget)` when it is used up. |
| `sta_session` | bool | `false` | One persistent OpenSTA process per corner for the whole post-pass. The liberties are read once; a trial is mirrored into the linked design as incremental edits (`replace_cell` for sizing and library swaps, `make_net` / `make_instance` / `connect_pin` for buffer trees and delay cells) and undone when rejected, so OpenSTA re-times only what changed; a trial the edit log cannot express (an `assign` feed-through rewritten) re-links the netlist in the same process. Same moves, WNS and area as fresh processes (`rr_arbiter16` HS+LS: 37.5 s → 2.7 s for 57 STA calls). A session error, including an error inside the constraints, falls back to a fresh process for that trial. Also `--sta-session`. |
| `sta_session_verify` | bool | `false` | Debug: with `sta_session`, also time every trial in a fresh process and log `VERIFY MISMATCH` lines; the session summary line reports `N compared, M mismatches`. |
| `resize_time_budget_s` | int | — | Wall-clock budget for one candidate's post-pass. When spent, the remaining phases stop and their status reads `ok (time budget)`; the delivered netlist is the last accepted one. `resize.json` → `runtime` reports total seconds, OpenSTA calls and seconds per phase (buffering, sizing, recovery, hold); the flow log prints the same after each candidate. |
| `resize_recover_area` | bool | `false` | After timing is met: downsize off-critical cells, or with several libraries swap them to the slower one first, in batches accepted only while WNS stays at its floor and TNS does not drop. Also `--recover-area` (implies `--resize`). |
| `resize_final` | string | `tns` | `tns`: best TNS within the tolerance; `wns`: never return a netlist with worse WNS than the input. Timing-clean states are always eligible. |

### Post-pass: repairs

| Field | Type | Default | Notes |
|---|---|---|---|
| `repair_design` | bool | `false` | Buffer trees on high-fanout nets of failing setup paths (`resize.py`): sinks split into groups of ≤ `max_fanout`, one net per failing path per round, batch accepted on TNS like the upsizes, bisected on rejection. Also `--repair-design`. ABC's own `buffer` runs before any measurement and without the real boundary loads; this is the repair step after OpenSTA has measured. |
| `max_fanout` | int | `8` | Sink group size for `repair_design`. SDC `set_max_fanout` overrides. |
| `repair_hold` | bool | `false` | Min-delay STA at the fast corner (`lib_fast`) lists failing hold endpoints. Each endpoint pin is checked against the liberty first: delay elements (`repair_delay_cell`) go only on data/enable inputs (setup/hold-checked pins, macro data pins, output ports); clock pins, async controls (recovery/removal-checked) and outputs are skipped and logged. Endpoints are deduplicated, delayed as one batch, and the batch is bisected on rejection so feasible subsets still land. A batch is kept only if hold TNS improves and slow-corner setup WNS stays at its floor. Also `--repair-hold`. Function-preserving by construction. |
| `repair_buffer_cell` | string | liberty-chosen | Buffer used for `repair_design` trees. Default: the second-weakest cell of the liberty's largest plain buffer family (`buf_2` on Sky130 HD/HS/MS/LS, `buf_1` on LP). Buffers are recognised by function (output = input), not by name. |
| `repair_delay_cell` | string | liberty-chosen | Delay element for `repair_hold`. Default: the slowest explicit delay cell (`dly*` name) among the weak-drive buffers, else the weakest plain buffer (`dlygate4sd3_1` on the full Sky130 libraries, `buf_1` on the bundled `hd_120` subset). |

Any of `resize_winner`, `repair_design`, `repair_hold` enables the post-pass on
each module's winner; the input netlist is kept as `winner.presize.v`.
The hold phase orders endpoints worst-first, keeps those on setup paths
within 300 ps of the floor (synchronizers, CDC bounds) apart and tries them
one at a time after the others, and on a rejected batch retries **both**
halves (worklist bisection), so every feasible endpoint gets its chance.
With `resize_candidates > 1` the candidates' post-passes run concurrently
(`parallel` workers) and each finished run is checkpointed in
`work/<module>/resize/<recipe>/checkpoint.json`, keyed by netlist text,
liberty files, SDC text and settings; a rerun with the same inputs reuses it.
`resize.json` → `timing` says whether setup and hold actually closed on the
delivered netlist and whether the final state was rolled back; cell count,
library mix and hold numbers are recomputed from that netlist.

**Runtime.** Every trial is a fresh OpenSTA process that re-reads the
liberties and re-links the netlist, so on a 350k-cell design one call is
minutes and a candidate's post-pass is dozens of calls (hold search up to
`repair_hold_sta_budget`, plus one scenario check per required
scenario × corner at each accepted batch). The required scenario checks now
run side by side, `resize_time_budget_s` bounds a candidate, and the
`runtime` block shows where the time went. The structural fix, a persistent
OpenSTA session with incremental edits (`replace_cell`, `insert_buffer`,
`make_net` / `connect_pin`) instead of a process per trial, is on the
roadmap.

Every edited netlist passes a structural check before it is timed (one module,
known cells, liberty pins, single driver per net) and is rejected otherwise.
Each phase (buffering, sizing, recovery, hold) is a transaction: a failure in
one phase keeps the last accepted netlist of the earlier phases and is
reported in `resize.json` → `status` (`ok` / `skipped` / `failed: …`) and in
the summary's post-pass column. `summary.md` also lists cells and area after
the post-pass and the worst setup/hold slack per path group (per clock).

For a frequency-pushing run where no candidate meets timing initially, use
`fallback: best_wns` with `resize_final: wns` (and `resize_candidates: 3`):
the fastest candidate is sized instead of the smaller, slower knee.
The post-pass reads the same `macro_libs` as the corner STA (slow corner for
setup, fast corner for hold), so SRAM/PLL pins have real arcs during sizing
and their instances are recognised as drivers and sinks by the repairs.
Drive families, buffers, delay and driving cells all come from the liberty
(`liberty_timing.LibCells`), so every Sky130 variant (`hd`, `hs`, `ms`, `ls`,
`lp`) and other technologies work without name tables; `dont_use` patterns
are honoured by the sizing candidates too.

### STA modelling

Applied identically to the quick STA used for winner ranking and to the
multi-corner STA on the winner (three single-library sessions), so both see
the same model and agree exactly on the slow-corner numbers. The user `sdc`
is sourced **last** and overrides any of these defaults per port.

| Field | Type | Default | Notes |
|---|---|---|---|
| `clock_uncertainty_setup_ps` | int | `250` | `set_clock_uncertainty -setup` on all clocks. |
| `clock_uncertainty_hold_ps` | int | `100` | `set_clock_uncertainty -hold` on all clocks. |
| `io_delay_frac` | float | `0.2` | Default `set_input_delay -max` / `set_output_delay -max` as a fraction of the period, on ports the SDC does not constrain. |
| `io_delay_min_frac` | float | `0.4` | Default `-min` I/O delay as a fraction of the max (hold). Zero manufactures hold violations on every short input path. |
| `wire_load_model` | string | `auto` | `auto` uses the liberty `default_wire_load` (Sky130 HD: `Small`) with `set_wire_load_mode top`; `none` disables wire load; any other value is passed to `set_wire_load_model -name`. |
| `sdc` | path | — | User SDC (also `--sdc`). Sourced into every STA script after the defaults, and read for synthesis: its clocks, uncertainty, driving cell and load override the YAML fields above. See [sdc-support.md](sdc-support.md). |

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
| `strict` | bool | `false` | Exit code 6 when any module is NOT CLOSED under its required scenarios or misses setup at sign-off. A post-pass phase failure always exits 5, strict or not. Also `--strict`. |
| `report_power` | bool | `true` | OpenSTA `report_power` in the nominal-corner sign-off session (typical, else slow): internal, switching and leakage power per group (sequential, combinational, clock, macro, pad, total) in `summary.md`, `summary.json` → `corner.power`, and the bench CSV (`power_dynamic_uw`, `power_static_uw`, `power_total_uw`). |
| `power_activity` | float | `0.1` | Without an activity file: toggles per clock cycle assumed on every input (`set_power_activity -input`); propagated through the netlist. |
| `power_duty` | float | `0.5` | Probability of an input being high, same command. |
| `power_activity_file` | path | — | `.vcd` or `.saif` from a simulation, read with `read_power_activities`; replaces the uniform assumption. |
| `power_scope` | string | — | Hierarchical scope of the DUT inside that file (`tb/dut`). |
| `fail_on_timing` | bool | `true` | When `true`, exit code 2 if any winner has setup violation at the slow corner. When `false`, timing violations are reported but exit code stays 0 (useful for early characterization runs). |
| `sdf_back_annotate` | bool | `true` | When true, GLS uses SDF back-annotation. The script writes SDF during STA and passes `+sdf_<module>=<path>` plusargs to vvp. The testbench is responsible for `$sdf_annotate` calls. |

### Experimental

| Field | Type | Default | Description |
|---|---|---|---|
| `abc_sequential` | bool | `false` | Enable ABC `-dff` for sequential optimizations (retiming, scorr). Reorders the flow to `abc -dff` → `dfflibmap`. **Breaks LEC**, may misbehave with async resets and clock gating. See README's "Experimental" section. |
| `dual_clock_synthesis` | bool | `false` | When `true` *and* `clock_port_2` is set, partitions the design into clock domains using Yosys `select` expressions and runs `abc -dff` on each domain with its own period. Experimental; requires all FFs to be driven by exactly one of the two clock ports. |

## Objectives

`objective` selects the recipe subset (see README → Objectives); it no longer
changes how the winner is picked. Selection rule for every run: the candidate
that meets timing (slow-corner WNS ≥ `select_margin_ps`) with the least area;
if none meets, the `fallback` rule (knee of the WNS/area front by default);
ties by `RECIPE_PRIORITY`.

| Field | Type | Default | Notes |
|---|---|---|---|
| `objective` | string | `delay` | `delay` \| `area` \| `balanced`. `fastest` → `delay`, `pareto` → `balanced` (aliases). |
| `full_sweep` | bool | `false` | Run every recipe in `recipes/` (also `--full-sweep`). |
| `select_margin_ps` | int | `0` | Slack a candidate needs to count as meeting timing. The multi-corner report is ~30 ps more pessimistic than the ranking STA, so 50 is a reasonable safety margin for marginal designs. |
| `fallback` | string | `knee` | When no candidate meets timing: `knee` picks the knee of the WNS/area Pareto front among the failing candidates (closest to best-WNS-and-least-area after normalizing both axes over the front, WNS clipped to one period below the best); `best_wns` picks the fastest regardless of area. mul32_mac: knee −2.10 ns at 39 873 µm² vs fastest −0.57 ns at 60 888 µm². Also `--fallback`. |
| `recipes` | list | — | Explicit recipes; overrides the objective subset. |

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
# All recipes, full STA, full GLS
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

### Dual-clock design with pre-mapped IP

```yaml
period_ps: 8000
clock_port: sysclk
clock_port_2: clk_iop
period_ps_2: 33333
verilog_defines: [NRV_SINGLE_PORT_REGF, NRV_SHARED_ADDER, NRV_SERIAL_SHIFT]
pre_read_files:
  - models/dffram_gen/dffram_combined.nl.v
  - models/dffram_gen/dffram_wrapper.v
keep_hierarchy_modules: [DFFRAM, RAM128, RAM32]
```

STA creates two asynchronous clock groups. Synthesis targets the primary
clock period (conservative for the second domain). Set
`dual_clock_synthesis: true` to enable per-domain ABC optimization.

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
clock_port_2:     <ident>        # default null
period_ps_2:      <int>          # default null
objective:        delay | area | fastest | pareto | balanced   # default "delay"
modules:          [<ident>, ...] # default [] -> auto-detect
recipes:          [<name>, ...]  # default [] -> all in recipes_dir
verilog_defines:  [<define>, ...] # default []
pre_read_files:   [<path>, ...]  # default [], globs allowed
keep_hierarchy_modules: [<ident>, ...] # default []

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
dual_clock_synthesis: <bool>     # default false
```
