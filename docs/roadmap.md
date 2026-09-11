<!-- SPDX-License-Identifier: Apache-2.0 -->
# Roadmap: timing-driven synthesis around Yosys/ABC

**Goal.** Turn synth_flow from a recipe-sweep harness into a synthesis tool
that honors SDC constraints and measurably beats the stock ORFS/OpenLane
Yosys+ABC flow on area and slack, with equivalence proven for every netlist
it emits.

Status legend: ☐ not started · ◐ in progress · ☑ done

## Findings that shape the plan

Verified 2026-09-12 with Yosys 0.68 and its bundled ABC 1.01. Details and
reproduction commands are in [architecture.md](architecture.md).

- ABC ignores BLIF `.input_arrival` / `.output_required` in `balance`,
  `&dch`, `&nf`, `map` and `stime`; `-constr` carries only a global driving
  cell and load. Per-port timing cannot be expressed inside ABC. It must be
  expressed as Yosys selections plus a per-group `abc -D`.
- Yosys `abc` extracts only `$_*_` gates; liberty cells are opaque
  boundaries. A mapped cone can be un-mapped with
  `read_liberty -ignore_miss_func` + `flatten @cone`, re-run through `abc`,
  and proven equivalent with `equiv_simple` / `equiv_induct`.
- Selection-based path groups (in→reg, reg→out, reg→reg) partition the
  combinational logic completely; each can take its own `-D` and `-constr`.
- In the standard flow ABC sees a combinational network, so `scorr`,
  `dretime`, `&scl`, `&lcorr` are no-ops. The baseline bench confirms
  `balanced_struct` is identical to `orfs_speed` on all 16 designs; other
  recipes with dead commands still differ through their remaining steps.
- The quick STA used for ranking and the multi-corner STA use different
  driving cell / load settings and neither sets a wire-load model, so ranking
  is optimistic and inconsistent.

## Phase 0 — Measure first  ☑

Deliverables
- ☑ `bench/` with 12 in-house designs (datapath, DSP, crypto, logic, control,
  peripheral, memory) and 4 external shalan/* IPs (zxip, zx16, ms_psram_ahb,
  uart_apb_master) at pinned commits. See [benchmarks.md](benchmarks.md).
- ☑ `bench/bench.py`: recipes × designs → `results/<tag>.csv`, env JSON,
  `latest.md`; `--compare A B` for before/after evaluation.
- ☑ Baseline rows: `orfs_speed` and `yosys_default` recipes.
- ☑ Baseline committed: `bench/results/baseline-full.csv` (16 × 21, see
  [benchmarks.md](benchmarks.md#baseline-2026-09-12)).
- ☑ Recipe pruning: 5 recipes retired to `recipes/retired/` from the STA
  baseline; tie-break order now follows mean WNS rank.
- ☑ STA consistency: shared constraint preamble (`_sta_constraints`),
  wire-load model from liberty, uncertainty on all clocks, OpenSTA 3.x
  report parsing, `signed` stripped from netlists.
- ☑ GitHub Actions: unit tests + smoke bench (OSS CAD Suite).

Acceptance: one command reproduces the full matrix; CSV committed; recipe set
reduced to distinct behaviors; quick-STA WNS within a few percent of
corner-STA slow-corner WNS.

## Phase 1 — SDC front end  ◐

Deliverables
- ☑ `sdc_parse.py`: run the user SDC through `tclsh` with stub procs; build a
  constraints model (clocks, generated clocks, uncertainty, per-port I/O
  delays, false paths, multicycle, max_delay, clock groups, driving cell,
  load, max_fanout, dont_use, dont_touch). Unknown commands → warning,
  STA-only.
- ☑ Precedence: SDC overrides `period_ps`, `clock_port`, `clock_port_2`,
  `driving_cell`, `load_ff`, uncertainty; every override is logged.
- ☑ `results/<module>/synth.sdc`: the derived constraints synthesis acted on.
- ☐ Async-reset false paths derived from the netlist (trace flop async pins to
  ports) instead of the hardcoded name list.
- ☑ Tests: sample SDCs with variables, `expr`, wildcards, `get_ports`,
  `all_inputs` (25 checks).

Acceptance: OpenSTA still sources the SDC verbatim; JSON matches for all
sample SDCs; hardcoded reset list removed. Command coverage is specified in
[sdc-support.md](sdc-support.md).

## Phase 2 — Path-group partitioned mapping  ☑ (evaluated: negative)

Implemented as specified (`path_groups: true`, `liberty_timing.py`,
`build_path_groups`, per-group `-constr`, `groups.json`, budgets in
`synth.sdc`; 20 unit tests). Measured on the bench with OpenSTA:
**area +10 to +28 %, WNS worse by 0.2 to 2.5 ns** on apb_timer and uart.
Cause: every internal group boundary is modelled as `inv_1` driving 33 fF,
so later groups oversize and earlier groups drive loads they were not sized
for ([architecture.md §2.5](architecture.md#25-partitioned-mapping-hurts-the-boundary-model-is-the-problem)).
Kept as an opt-in experiment; default off. The SDC-derived budgets and the
liberty flop timing are reused by Phase 3.

## Phase 3 — ABC delay target  ☑ (evaluated: no target is best)

- ☑ Found that `{D}` in recipe files was never substituted by Yosys, so ABC
  always mapped for minimum delay. Recipes are now materialized with the
  target explicit ([architecture.md §2.6](architecture.md#26-d-never-reached-abc-and-that-was-the-best-setting)).
- ☑ Benched `period`, `reg2reg` and a loose target against no target: every
  target loses WNS (−0.2 to −1.1 ns mean) for ≤ 4 % area. `abc_target`
  defaults to `none`.
- ☐ Area recovery on slack-rich designs via mapper knobs (`&nf -R`, area
  recipes) validated by STA, replacing the `-D` search idea. `abc_search.py`
  stays as the grid/bisection harness for any per-design knob.

## Phase 4 — STA-driven refine loop  ☑ (evaluated: cone remap negative; sizing positive)

- ☑ `refine.py`: endpoints from OpenSTA, flop-bounded cones with boundary
  drivers kept, un-map/re-map with no `-D`, TNS acceptance, equivalence
  (`async2sync`, `equiv_simple`, `equiv_induct`), recipe escalation and a
  whole-design fallback (`--whole-only`). Result: partial-cone remaps are
  always worse; whole-design remap is a modest extra mapping pass
  ([architecture.md §2.7](architecture.md#27-re-mapping-cones-does-not-pay-sta-guided-sizing-does)).
  Kept as a standalone tool.
- ☑ `resize.py` (pulled forward from Phase 6): OpenSTA-guided drive-strength
  sizing; TNS down on every failing bench design, sha256_core closes,
  zxip +0.9 ns WNS, ≤ 0.7 % area on most designs. In the flow as
  `resize_winner: true` / `--resize`; `bench/postpass.py` evaluates passes
  on bench winners.
- ☐ Downsizing for area recovery on slack-rich paths (same machinery,
  reverse direction, accepted only while WNS stays ≥ margin).
- ☐ Buffering of high-fanout nets (the apb_timer/uart WNS spread across
  recipes comes from these) as a sizing-pass move.

## Phase 5 — Front-end and library sweeps  ☐

Deliverables
- `yosys_opts` sweep dimension: `synth -booth`, `opt -full`, `opt_dff -sat`,
  `share` on/off, adder architecture via `techmap -map +/choices/kogge-stone.v`
  and `han-carlson.v`.
- Library experiment: `hd_120` versus full `sky130_fd_sc_hd` with an
  ORFS-style dont-use list; choose the default from bench data.
- Replace the 80 ps/gate depth-only constant with a value calibrated from
  bench.

Acceptance: bench CSV rows for each option; defaults chosen by data.

## Phase 6 — Sizing, hierarchy, packaging  ☐

Deliverables
- ☑ STA-driven sizing pass (`resize.py`, see Phase 4).
- Hierarchical time budgeting: derive leaf-module I/O delays from a flat
  depth / STA pass instead of the default 20 %.
- Split `synth_flow.py` into a package (config, sdc, drivers, sta, select,
  refine, report); `pyproject.toml` with a console entry point. Target
  interface in [cli.md](cli.md).
- Tool versions and recipe hashes in `summary.json`.

Acceptance: pip-installable; CI green; hierarchical bench within a few
percent of flat on WNS.

## Ordering rationale

Measure before optimizing (0). Budgets need parsed constraints (1 before 2).
Search and refine need consistent STA (0 before 3, 4); Phase 2's budgets feed Phase 3's starting point.
Front-end and library sweeps are independent and can be interleaved once the
bench exists.
