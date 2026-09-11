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

## Phase 2 — Path-group partitioned mapping  ☐

Deliverables
- Liberty parser for flop clock-to-Q and setup (per corner) to compute
  budgets:
  - reg→reg = T − t_cq − t_su − uncertainty
  - in→reg = T − in_delay − t_su
  - reg→out = T − t_cq − out_delay
  - in→out = T − in_delay − out_delay
  - multicycle = N·T; false path / async cross-domain = relaxed
- New Yosys driver template: select groups with `%co*` / `%ci*` stopping at
  flop types (list from liberty), assign overlaps to the tightest group, run
  `abc -D <budget> -constr <group.constr> @group` per group.
- Per-group `-constr` files from SDC driving cell / load.
- Group statistics in `summary.json` (cells per group, budget, achieved
  slack).

Acceptance: union of groups equals all `$_*_` gates (leftover 0) on every
bench design; equivalence versus flat mapping; bench shows ≥ flat QoR where
I/O delays are non-trivial.

## Phase 3 — Per-group `-D` search  ☐

Deliverables
- OpenSTA Tcl: `group_path` per synthesis group; per-group worst slack parse.
- Bisection on `-D` per group: smallest area that meets slack; capped
  iterations.
- Reuse the pre-ABC RTLIL (`write_rtlil` after `dfflibmap`) so re-mapping
  skips `synth`.

Acceptance: search converges in ≤ 6 STA calls per group on bench; area
reduction versus fixed `-D` reported in the CSV.

## Phase 4 — STA-driven refine loop  ☐

Deliverables
- OpenSTA: dump endpoints with slack below a margin
  (`find_timing_paths -slack_max`) with levels / slew / fanout of the worst
  path per endpoint.
- Yosys refine script: reload `winner.v` + functional liberty, select failing
  endpoints, full fan-in cones bounded by flops, `flatten @cone`, `abc` with
  a tight `-D` and delay recipe, clean up leftover generic gates, delete the
  imported cell modules, write the netlist.
- Accept/reject on TNS; iteration cap; classify depth-bound versus
  drive-bound endpoints (drive-bound go to sizing, Phase 6).
- Area recovery: cones with large positive slack re-mapped with an area
  recipe, accepted only if slack stays ≥ margin.
- Equivalence check after every accepted iteration; hard failure on unproven.
- Config knobs: `refine.iters`, `refine.margin_ps`, `refine.recipe`,
  `refine.area_recipe`.

Acceptance: TNS monotone non-increasing across accepted iterations; LEC
clean; bench shows WNS improvement on designs that fail timing after Phase 3.

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
- STA-driven sizing pass: parse worst paths, upsize cells and flops on them
  in the netlist, re-run STA until no gain.
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
Search and refine need consistent STA and groups (0, 2 before 3, 4).
Front-end and library sweeps are independent and can be interleaved once the
bench exists.
