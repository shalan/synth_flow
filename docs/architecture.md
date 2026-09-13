<!-- SPDX-License-Identifier: Apache-2.0 -->
# Architecture

This document describes how synth_flow works today, what was learned about the
limits of Yosys and ABC when constraints are involved, and the design of the
timing-driven flow the [roadmap](roadmap.md) builds toward. Every claim in the
"Findings" section was reproduced with the commands shown.

## 1. Current flow

```
YAML config ──► Config ──► module discovery ──► recipe sweep (Pool)
                                                   │  per (module, recipe):
                                                   │  yosys -s <driver.ys>
                                                   ▼
                                     quick STA (OpenSTA, slow corner) per netlist
                                                   ▼
                                     select_winner(objective) ──► winner.v
                                                   ▼
                                     multi-corner STA (SS/TT/FF) + SDF ──► GLS (opt.)
                                                   ▼
                                     summary.md / .json / .csv
```

### Yosys driver (standard flow)

`YOSYS_DRIVER_STD` in `synth_flow.py` is the template every recipe runs:

```
read_liberty -lib <macro libs>          # optional hard macros
read_verilog [-D ..] [-I ..] <rtl>
hierarchy -top <module>
synth -top <module> -flatten -noabc     # generic gates, no mapping yet
dfflibmap -liberty <lib>                # flops → liberty flops
abc -liberty <lib> -constr <file> -script <recipe.abc> -D <period_ps>
setundef -zero; splitnets; opt_clean -purge
stat -liberty <lib> -json
write_verilog -noattr -noexpr <out.v>
```

Points that matter for quality:

- **Everything a recipe can influence happens inside one `abc` call.** The
  Yosys front end (`synth`), the flop mapping, the delay target and the
  library are fixed for all recipes.
- **`dfflibmap` runs before `abc`**, so ABC receives a purely combinational
  network. This is the ORFS/OpenLane convention and keeps a 1:1 register
  correspondence for equivalence checking. It also means the sequential
  commands used by several recipes (`scorr`, `dretime`, `&scl`, `&lcorr`) do
  nothing; ABC prints "The network is combinational". The baseline bench
  shows `balanced_struct` producing netlists identical to `orfs_speed` on
  every design for this reason.
- **`-D` receives the full clock period.** The true combinational budget is
  the period minus clock-to-Q, setup, uncertainty and I/O delays. The
  synthesis library is the slow corner by default (`lib_synth → lib_slow →
  lib_typ`).
- The `-constr` file holds only `set_driving_cell` and `set_load`. That is
  the entire constraint vocabulary ABC accepts.

Variants: `YOSYS_DRIVER_SEQ` (`abc -dff`, experimental), `YOSYS_DRIVER_HIER`
(reads winning sub-module netlists first), `YOSYS_DRIVER_DUAL_CLK`
(selection-based `abc` per clock domain) and `YOSYS_DRIVER_DEPTH` (`ltp`
depth estimate, no mapping).

### Winner selection

Each netlist gets a quick OpenSTA run at the slow corner (`QSTA_TCL`); the
runs are independent processes and go through the same worker pool as
synthesis (108 candidates: 0.5 to 3 s on 18 workers versus 6 s serial). One
rule for every objective: the candidate that meets timing (WNS ≥
`select_margin_ps`) with the least area; if none meets, the knee of the
WNS/area Pareto front (`fallback: knee`) or the fastest (`best_wns`). The
objective only chose which recipes ran. The user SDC, if configured, is sourced into
both STA scripts after the tool's own `create_clock`.

Known inconsistencies, fixed in Phase 0 of the roadmap:

- Quick STA sets no driving cell, no output load and no wire-load model;
  corner STA sets driving cell and load but still no wire-load model.
  Ranking is therefore optimistic relative to the final report.
- Setup uncertainty is applied only in the dual-clock template.
- Async-reset false paths come from a hardcoded port-name list.

Status after Phase 0/1: both STA scripts share one constraint preamble
(driving cell, load, liberty wire-load model, uncertainty on all clocks, user
SDC last), and async-reset false paths are derived from register async pins
with the name list as fallback. The 30 ps gap between the multi-corner report and the single-library
ranking STA is gone: the report now runs one single-library session per
corner (§2.11), so `summary.json` and `selection.json` agree exactly.

## 2. Findings about ABC and constraints

All experiments used the bundled `sky130/hd_120_tt.lib`, Yosys 0.68 and
ABC 1.01 (yosys-abc). Scripts live under the session scratch area; the
essential commands are inlined here so they can be re-run.

### 2.1 ABC ignores per-input arrival times

BLIF supports `.input_arrival <pi> <rise> <fall>` and `.output_required`.
An 8-input AND with one input arriving 3000 ps late was mapped with several
flows. An arrival-aware flow would place that input at the last logic level
and report ≈3200 ps.

| Flow | Levels, late input → output | Levels, other inputs | `stime` delay |
|---|---|---|---|
| `strash; balance; map` | 3 | 3 | 244 ps |
| `strash; balance -d; map` | 3 | 3 | 244 ps |
| `strash; &get -n; &dch; &nf; &put` | 3 | 3 | 200 ps |
| `strash; &get -n; &st; &b -d; &dch; &nf; &put` | 3 | 3 | 200 ps |

Nothing reacted. `read_constr -h` confirms the constraint file has no
arrival/required syntax. **Conclusion:** patching Yosys to emit BLIF timing
directives would not change results; per-port constraints must be modeled
outside ABC.

### 2.2 The Yosys `abc` pass only sees generic gates

The pass extracts `$_AND_`, `$_OR_`, `$_MUX_` … into a BLIF with `ys__n<id>`
names. Any liberty-typed cell (flops after `dfflibmap`, hard macros, or a
hypothetical "delay buffer" cell) becomes a primary input/output of ABC's
network. A dummy liberty cell wired to a port therefore cannot inject an
arrival time. In standalone ABC, `strash` reduces such a cell to its Boolean
function anyway.

### 2.3 Selection-based path groups partition the logic completely

After `dfflibmap`, the combinational gates can be split by path class using
Yosys selection rules that refuse to traverse flop cell types:

```
select -set ffs  t:*__df*
select -set in2x  i:* %co*:-sky130_fd_sc_hd__dfxtp_1:-sky130_fd_sc_hd__dfxtp_2:-sky130_fd_sc_hd__dfxtp_4 @ffs %d i:* %d
select -set x2out o:* %ci*:-sky130_fd_sc_hd__dfxtp_1:-sky130_fd_sc_hd__dfxtp_2:-sky130_fd_sc_hd__dfxtp_4 @ffs %d o:* %d
select -set io      @in2x @x2out %u
select -set reg2reg t:$_* @io %d
abc -liberty LIB -D 400  @io          # tight target for I/O paths
abc -liberty LIB -D 2000 @reg2reg     # relaxed target for reg→reg
```

On a mixed test design: 36 generic gates, `in2x` 25, `x2out` 6, `reg2reg`
18 (overlaps assigned to the I/O group), 0 gates left unmapped. The
dual-clock template already uses the same mechanism per clock domain.

### 2.4 A mapped cone can be un-mapped and re-mapped, provably

```
read_liberty -ignore_miss_func LIB     # functional cell models (not -lib)
read_verilog winner.v
select -set endpts c:_34_ c:_35_ ...   # failing endpoint flops, OpenSTA names
select -set dnets @endpts %x:+[D] @endpts %d
select -set cone  @dnets %ci*:-<flop types> t:* %i t:<flop types> %d
flatten @cone                          # cone cells → $_*_ gates, in place
abc -liberty LIB -constr C -D <tight> t:$_*
delete top %n                          # drop imported cell modules
```

Test design: 21 combinational cells, 14 in the cone, 92 generic gates after
`flatten`, 2 generic gates left after `abc` (buffers/constants from the
function expansion, need `opt_clean`), and `equiv_simple` + `equiv_induct`
proved 111/111 `$equiv` cells against the original netlist. Instance names
in `write_verilog -noattr` output are exactly what OpenSTA reports, so no
name mapping is required.

### 2.5 Partitioned mapping hurts: the boundary model is the problem

Phase 2 was implemented as designed (`path_groups: true`): in→out, in→reg,
reg→out and relaxed groups, tightest budget first, then reg→reg. Measured
with OpenSTA on the bench (SS corner, SDCs applied):

| Design / recipe | Area flat | Area grouped | WNS flat | WNS grouped |
|---|---|---|---|---|
| apb_timer / orfs_speed | 10 862 | 13 479 (+24 %) | −0.08 ns | −2.57 ns |
| apb_timer / delay_choice_deep_v3 | 10 882 | 13 916 (+28 %) | −0.07 ns | −2.37 ns |
| uart / orfs_speed | 3 675 | 4 113 (+12 %) | +0.04 ns | −0.14 ns |
| uart / delay_choice_deep_v3 | 3 605 | 3 958 (+10 %) | +0.13 ns | −0.23 ns |

Cell mix on apb_timer / orfs_speed explains it:

| | flat | grouped |
|---|---|---|
| cells | 1343 | 1395 |
| `buf` | 12 | 170 |
| drive strength ×2 / ×4 / ×6 | 99 / 12 / 0 | 200 / 110 / 91 |

Each `abc` call models its boundary with the constraint file: every
primary input is driven by `set_driving_cell` (`inv_1`) and every primary
output drives `set_load` (33 fF). At an internal group boundary both are
wrong. The later group sees a weak driver and a tight budget, so it upsizes
its first stages and buffers; the earlier group was sized for 33 fF and now
drives those enlarged inputs. OpenSTA sees the real loads and the path gets
slower, not faster. The in→out group also captured 532 of 1428 gates on
apb_timer (the read mux plus address decode), so most of the design was
mapped under the tightest budget.

Rules that follow, applied to Phases 3–4:

- **Do not cut combinational logic between ABC calls** unless the cut lies
  on flop pins, whose drive and load are known from the liberty.
- **A re-mapped cone must be bounded by flops**, and its constraint file
  must name the driving flop cell and the flop D-pin capacitance, not
  `inv_1` / 33 fF.
- **The global `-D` is the safe lever.** `abc_target: reg2reg` gives ABC
  `T − t_cq − t_su − uncertainty` (on Sky130 HD at SS that is 1.45 ns less
  than the period) without any partition. Per-design search over `-D` with
  OpenSTA feedback replaces per-group budgets.

`path_groups` stays in the code as an opt-in experiment so the result can
be reproduced (`bench.py --set path_groups=true`).

### 2.6 `{D}` never reached ABC, and that was the best setting

Yosys substitutes `{D}` only in inline scripts (`-script +...`). With
`-script <file>` the file is sourced by ABC untouched, so every recipe here
ran `&nf {D}; upsize {D}; dnsize {D}` with the literal token, which ABC
ignores: minimum-delay mapping, no target. The flow now materializes each
recipe with `{D}` substituted (or removed) so the target is explicit.

Measured on the bench (16 designs × 16 recipes, OpenSTA at SS, SDCs), each
target versus no target:

| `abc_target` | mean ΔWNS | rows worse / better (of 256) | mean Δarea | designs meeting timing |
|---|---|---|---|---|
| `period` (= T) | −0.59 ns | 190 / 1 | −3.3 % | 6 → 3 |
| `reg2reg` (T − t_cq − t_su − unc) | −0.22 ns | 72 / 2 | −1.0 % | 6 → 6 |
| `100000` (very loose) | −1.08 ns | 229 / 1 | −3.9 % | 6 → 3 |

Whole-design un-map/re-map of apb_timer with `orfs_speed` tells the same
story in one design: no `-D` gives WNS −0.038 ns; `-D 5000` (the period)
−1.07 ns; `-D 20000` −1.57 ns. ABC's `-D` lets `&nf`, `upsize` and
`dnsize` relax against ABC's own delay model, which has no wire load and a
single boundary driver; OpenSTA with the liberty wire-load model does not
see that slack. The area returned for the lost slack is small.

Consequences:

- `abc_target` defaults to `none`. `period`, `reg2reg` and explicit values
  remain available for experiments.
- The per-design `-D` search (Phase 3) is not worth building on this axis:
  the response is monotone bad. Area recovery has to come from recipe
  choice and mapper-side knobs (`&nf -R`, area recipes) validated by STA.
- Any cone or group re-map inside the flow must also run without `-D`.

### 2.7 Re-mapping cones does not pay; STA-guided sizing does

`refine.py` implements the Phase 4 loop exactly: failing endpoints from
OpenSTA, full flop-bounded fan-in cones, boundary drivers kept in place,
un-map with functional liberty models, `abc` with a delay recipe and no
`-D`, accept on TNS, equivalence with `async2sync` + `equiv_simple` +
`equiv_induct`. On apb_timer every partial-cone remap (514 of 591 cone
cells, four recipes) lands between −0.44 and −1.27 ns against the original
−0.065 ns. Only a **whole-design** remap improves it (−0.038 ns, TNS −3.27 →
−1.30, area −1.5 %, 1594/1594 equivalence cells proven), and that is just
another mapping pass over the same logic, which the recipe sweep already
provides. ABC's mapper needs the whole combinational network to make good
structural and sizing decisions; any cut inside it costs more than the
local re-optimization gains.

`resize.py` attacks the same failing paths without touching logic: one
drive-strength step per failing path per iteration (largest stage delay
first), batch accepted on TNS with a bounded WNS regression, bisected on
rejection, then a zero-tolerance WNS repair phase. Bench winners, OpenSTA at
SS with SDCs (`bench/results/postpass-resize.csv`):

| design | WNS before | WNS after | TNS before | TNS after | Δarea |
|---|---|---|---|---|---|
| alu32 | -0.922 | -1.032 | -2.07 | -1.65 | +0.33 % |
| mul16_pipe | -0.411 | -0.272 | -0.67 | -0.28 | +0.38 % |
| mul32_mac | -2.584 | -2.371 | -2.58 | -2.37 | +0.21 % |
| fir8 | +0.323 | +0.323 | +0.00 | +0.00 | +0.00 % |
| aes_round | +1.422 | +1.422 | +0.00 | +0.00 | +0.00 % |
| sha256_core | -0.074 | +0.010 | -0.07 | +0.00 | +0.03 % |
| crc32_8 | -0.030 | -0.009 | -0.06 | -0.01 | +0.70 % |
| rr_arbiter16 | -0.430 | -0.384 | -4.20 | -3.88 | +0.69 % |
| uart | +0.054 | +0.054 | +0.00 | +0.00 | +0.00 % |
| spi_master | +0.203 | +0.203 | +0.00 | +0.00 | +0.00 % |
| apb_timer | -0.065 | -0.085 | -3.27 | -0.09 | +1.64 % |
| fifo_sync | -0.602 | -0.694 | -229.47 | -151.69 | +5.34 % |
| zx16_core_ahb | -0.173 | -0.141 | -2.06 | -0.54 | +0.74 % |
| zxip | -1.238 | -0.346 | -14.82 | -2.29 | +0.32 % |
| ms_psram_ahb | +0.117 | +0.117 | +0.00 | +0.00 | +0.00 % |
| uart_apb_sys | +3.971 | +3.971 | +0.00 | +0.00 | +0.00 % |

TNS improves on every failing design; sha256_core closes; zxip recovers
0.9 ns of WNS for 0.3 % area. Three designs trade a few tens of ps of WNS
for a large TNS gain under the default `tns` policy (`resize_final: wns`
forbids that and rolls back to the input). Sizing is function-preserving
by construction, so no equivalence check is needed. It is available in the
flow as `resize_winner: true` / `--resize`, applied to each module's winner
before the multi-corner STA; `winner.presize.v` keeps the input.

### 2.8 `abc_new` (Yosys 0.68, experimental) is not ready for standard cells

`abc_new` drives ABC through `abc9_exe` with an XAIGER interface and box
files, the path built for FPGA LUT mapping. Measured with a pure GIA script
(`&st; &dch -f; &nf`), the same constraint file, and OpenSTA at SS:

| design | `abc` WNS / area | `abc_new` WNS / area | equivalent (3000-cycle random sim) |
|---|---|---|---|
| uart | −0.018 / 3880 | −0.303 / 4006 | yes |
| alu32 | −1.599 / 11001 | −4.517 / 10772 | yes |
| apb_timer | −0.597 / 11295 | −4.119 / 13345 | yes |

Pitfalls found on the way: it must run before `dfflibmap` (after it, liberty
flops with `RESET_B` fail with "Bad connection"); `dfflibmap` then emits
`$_MUX_` cells for enable flops that a second plain `abc` has to map; and
until that pass is added `stat` and OpenSTA silently ignore the unmapped
cells, which made the first numbers (alu32 "+6.8 ns", −23 % area) look
spectacular and were wrong. The mapped GIA comes back without the
`buffer`/`upsize`/`dnsize` steps of the SCL flow and with flops as plain
inputs (`box = 0`), so every cell is minimum drive. Revisit when Yosys
derives boxes for liberty flops and the SCL sizing is reachable from the
GIA path.

### 2.9 ABC's own wire-load model: on for the `map` recipes

ABC's SCL commands `buffer`, `upsize`, `dnsize` and `stime` accept `-c`,
"use wire-loads if specified", and read the liberty's `default_wire_load`
(`Small` here). No recipe used it, so ABC sized against pure pin
capacitance while OpenSTA charged the wire-load table, one of the mismatches
behind the sizing gaps of §2.5 and §2.7. Paired bench, each recipe with and
without `-c` on all 16 designs (`bench/results/wireload.csv`):

| recipe | mean ΔWNS | better / worse designs | mean Δarea |
|---|---|---|---|
| `delay_map_resyn` | +0.035 ns | 10 / 0 | +0.6 % |
| `delay_map` | +0.086 ns | 10 / 3 | +1.7 % |
| `balanced_resyn2x` | −0.003 ns | 12 / 2 | +0.7 % |
| `orfs_speed` | −0.007 ns | 4 / 6 | +0.6 % |
| `delay_choice_deep_v3` | −0.019 ns | 7 / 6 | +0.4 % |

The `map` mapper's netlists, with their duplication and higher fanout,
respond to the wire-load-aware sizing; the `&nf` netlists do not. `-c` is
baked into `delay_map` and `delay_map_resyn`; `abc_wire_load: true`
(`--abc-wire-load`) adds it to every recipe for experiments.

### 2.10 Repairs after measurement: buffering and hold

ABC buffers (`buffer` in every recipe) against its own model, before any
OpenSTA measurement and without the real loads at the network boundary
(§2.5). `resize.py` therefore gained two repair moves that operate on the
mapped netlist with OpenSTA as the judge, both function-preserving by
construction (verified by 3000-cycle random simulation on every test):

- **`repair_design`**: for each failing setup path, the highest-delay stage
  whose fanout is ≥ `max_fanout` gets its net split into buffer trees of
  ≤ `max_fanout` sinks (the liberty's plain buffer, `buf_2` on Sky130, see
  §2.12); one net per path per round, the batch
  accepted on TNS and bisected on rejection. On the apb_timer reference
  netlist (`orfs_speed`): 15 nets, 30 buffers, WNS −0.079 → −0.009 ns and
  TNS −3.85 → −0.13 in one round; with upsizing it closes at +0.013 ns for
  +1.7 % area. On uart the same move made the path 0.8 ns slower and was
  rejected, which is the point of judging every move.
- **`repair_hold`**: min-delay STA at the fast corner; each failing endpoint
  (a register data pin or an output port) gets one delay element per round,
  the liberty's slowest delay cell when it has one, else its weakest buffer
  (§2.12). A round is kept only if
  hold TNS improves and slow-corner setup WNS does not drop below its floor.
  uart: hold −0.083 → +0.024 ns in two rounds, setup unchanged, +0.2 % area.

Both are opt-in (`repair_design`, `repair_hold`); bench results per design
are in [benchmarks.md](benchmarks.md).

### 2.11 OpenSTA's multi-corner wire-load estimate differs from single-library

Same netlist, same constraints, same `Small` wire-load table in every corner
library: a `define_corners fast typical slow` session reported the slow
corner 11 to 33 ps worse than a session that read only the slow liberty.
Bisected on uart_apb_sys:

| session | slow-corner WNS |
|---|---|
| single library (slow) | 3.9566 ns |
| three corners, slow library read first | 3.9566 ns |
| three corners, fast library read first | 3.9456 ns |
| any of the above without a wire-load model | identical |
| `set_wire_load_model -library <slow lib>`, `-max`/`-min` split | no change |

Only the wire-load estimate moves, every net's capacitance by a few
femtofarads, and only when a different library was read first; the tables
themselves are byte-identical across the three libraries. Binding the model
to a library does not help. Rather than depend on read order, the
multi-corner report now runs three single-library sessions (slow: setup and
SDF; typical: setup and hold; fast: hold), the same script shape the ranking
STA uses. Ranking and report agree to four decimals by construction.

Related default change: minimum I/O delays were 0, which manufactures a hold
violation on every short input-to-register path at the fast corner. The
default is now 40 % of the maximum delay (`io_delay_min_frac`), the usual
template value, and the in-house bench SDCs carry the same.

### 2.12 Nothing in the repairs is HD-specific: the liberty is the catalogue

Sizing, buffering and hold repair used to know Sky130 HD by name
(`_1/_2/_4` suffixes, `buf_2`, `dlygate4sd3_1`, `inv_2`) and the netlist model
only followed scalar connections, so a separate SRAM liberty was invisible to
the post-pass and another library would have produced wrong or no moves.
`liberty_timing.LibCells` now reads every liberty the flow has (standard
cells plus `macro_libs`) and derives what the repairs need:

- **drive families** = cells with identical pin names, directions and
  functions (the footprint), ordered by area; flops included, since
  `dfxtp_1/2/4` share one footprint as well;
- **buffers** = single-input cells whose output function equals the input;
  the tree buffer is the second-weakest member of the largest plain buffer
  family (`buf_2` on HD/HS/MS/LS, `buf_1` on LP), the delay cell the slowest
  weak-drive `dly*` cell when the liberty has one (`dlygate4sd3_1` on the full
  Sky130 libraries, `dlygate4s50_1` on LP, `buf_1` on the bundled `hd_120`);
- **default driving cell** = second-weakest plain inverter (`inv_2`), and a
  cell named in the YAML or an SDC `-lib_cell` that is not in the synthesis
  liberty is mapped to the same-named cell of that library
  (`sky130_fd_sc_hd__inv_1` → `sky130_fd_sc_hs__inv_1`). Without this every
  quick STA of the HS/MS/LS/LP benches aborted on the HD driving cell and
  reported nothing: the first library run showed 1/16 closing for that reason.
- **hard macros**: OpenRAM liberties declare `bus()` pins whose `type()` has
  `bit_from : 0`, and OpenSTA numbers a Verilog concatenation in that order
  (`dout0[31]` is the *last* item). The netlist model follows concatenation
  connections with that mapping, so an SRAM output bit can get a buffer tree
  and an SRAM data pin a hold delay cell. Verified on a wrapper around
  `sram_1rw1r_32_256_8_sky130`: the STA fanout of every bus bit matches the
  netlist sinks of the mapped net, trees on `dout0` bits were built, timed
  and (rightly) rejected on TNS, and the final netlist is
  simulation-equivalent to the input over 3000 random cycles.

The five Sky130 standard-cell libraries through the default `delay` objective
with `resize_winner`, per-design SDCs unchanged from the HD bench
(`bench/results/lib-<v>.csv`; `--lib-dir` on the ciel PDK):

| | HD (`hd_120`) | HS | MS | LS | LP |
|---|---|---|---|---|---|
| designs meeting timing | 16 / 16 | 16 / 16 | 14 / 16 | 8 / 16 | 5 / 16 |
| mean WNS (ns) | +0.487 | +0.917 | +0.546 | -0.418 | -0.650 |
| mean area vs HD | — | +31.1 % | +38.5 % | +45.5 % | +36.4 % |
| STA-failed designs | 0 | 0 | 0 | 0 | 0 |

HS closes everything and is faster than HD at +31 % area; MS is close.
LS and LP are slower libraries running against periods tuned for HD, so
half or more of the designs miss, but the post-pass behaves the same way on
all of them: it sized 12 of the 16 LS designs (closing `mul16_pipe`, `zxip`,
`sha256_core`, `apb_timer`) and 12 of the 16 LP designs, using each
library's own drive families (`a2111oi_4`, `dfrtp_4`, `mux4_4`, LP's `_0`
sizes, …). Per design:

| design | HD WNS | HS WNS | MS WNS | LS WNS | LP WNS | HD area | HS Δarea | MS Δarea | LS Δarea | LP Δarea |
|---|---|---|---|---|---|---|---|---|---|---|
| alu32 | +0.487 | +1.054 | +0.152 | -3.398 | -3.105 | 11596 | +49.3 % | +54.8 % | +32.3 % | +24.5 % |
| mul16_pipe | +0.952 | +1.548 | +0.391 | +0.010 | -1.500 | 11994 | +43.6 % | +44.3 % | +101.6 % | +45.5 % |
| mul32_mac | +0.037 | +0.161 | -1.001 | -2.680 | -2.339 | 56921 | -4.7 % | +30.1 % | +30.4 % | +18.1 % |
| fir8 | +0.237 | +2.451 | +1.198 | +0.105 | +0.792 | 13332 | +33.1 % | +34.0 % | +63.5 % | +52.4 % |
| aes_round | +1.397 | +0.362 | +0.007 | -0.869 | -1.289 | 57296 | +58.0 % | +55.4 % | +57.5 % | +52.2 % |
| sha256_core | +0.025 | +0.163 | +1.674 | +0.125 | -0.898 | 70110 | +34.2 % | +42.9 % | +44.8 % | +37.5 % |
| crc32_8 | +0.145 | +0.021 | +0.239 | -0.399 | +0.182 | 2540 | +12.0 % | +20.1 % | +19.7 % | +52.4 % |
| rr_arbiter16 | +0.026 | +0.029 | -0.753 | -1.964 | -2.280 | 3867 | +5.0 % | +32.0 % | +28.7 % | +1.3 % |
| uart | +0.020 | +0.573 | +0.262 | +0.041 | -0.212 | 3719 | +33.0 % | +33.0 % | +51.5 % | +43.5 % |
| spi_master | +0.143 | +0.134 | +0.058 | -0.622 | -0.452 | 4682 | +39.6 % | +44.1 % | +50.8 % | +47.9 % |
| apb_timer | +0.129 | +0.570 | +0.058 | +0.035 | -0.051 | 11353 | +29.1 % | +29.4 % | +40.6 % | +39.1 % |
| fifo_sync | +0.049 | +0.314 | +0.465 | -0.051 | -0.725 | 27264 | +20.7 % | +34.9 % | +46.6 % | +27.4 % |
| zx16_core_ahb | +0.136 | +1.155 | +0.254 | -1.296 | -1.594 | 20573 | +33.8 % | +35.5 % | +47.1 % | +36.1 % |
| zxip | +0.078 | +0.008 | +0.362 | +0.009 | +0.018 | 160331 | +36.2 % | +51.5 % | +37.8 % | +30.3 % |
| ms_psram_ahb | +0.467 | +1.047 | +0.672 | +0.117 | +0.295 | 24714 | +43.9 % | +43.7 % | +43.9 % | +40.4 % |
| uart_apb_sys | +3.457 | +5.082 | +4.691 | +4.150 | +2.756 | 16241 | +31.2 % | +30.9 % | +31.1 % | +33.3 % |

### 2.13 Several libraries at once: fast cells on critical paths, slow cells where there is slack

Sky130's `hs`, `ms`, `ls` and `lp` libraries share one placement site
(0.48 × 3.33 µm) and rail geometry, so a design can mix them; `hd` (2.72 µm)
and `hvl` (4.07 µm, 3.3 V) stand alone. `lib_typ/lib_slow/lib_fast` accept
lists, the extra libraries reach every Yosys and OpenSTA step, and the
catalogue ranks libraries by the median delay of their inverters and buffers
(HS 171 ps, MS 173, LP 279, LS 294 for `inv_1` at SS). Two designs were tried:

- **Union mapping** (`mixed_map: all`): every cell offered to ABC. HS and LS
  variants have identical area, so ABC's ties land arbitrarily and critical
  paths come out with slow cells (`rr_arbiter16` −0.97 ns after mapping,
  `mul32_mac` +40 % area after repair).
- **Fastest-library mapping** (`mixed_map: fastest`, default): ABC maps with
  the fastest library only; the post-pass then moves off-critical cells to
  the slower library (`resize_recover_area`) and, on failing paths, swaps a
  cell straight to its fastest variant before upsizing. Swaps keep pins and
  function, so no equivalence check is needed (both mixed netlists of the
  smoke designs were also simulated against their inputs: no mismatch).

HS alone (with recovery) against HS+LS, 16 designs, HD periods
(`bench/results/lib-hs-recover.csv`, `mix-hs-ls.csv`, `mix-hs-ls-allmap.csv`):

| | HS alone + recovery | HS+LS, fastest mapping | HS+LS, union mapping | HS+MS+LS+LP |
|---|---|---|---|---|
| designs meeting timing | 16 / 16 | 16 / 16 | 16 / 16 | 16 / 16 |
| mean WNS (ns) | +0.702 | +0.299 | +0.280 | +0.225 |
| mean area vs HS alone | — | +2.1 % | +8.3 % | +1.5 % |
| cells left in HS | 100 % | 62 % | 45 % | 32 % (MS 27 %, LP 24 %, LS 17 %) |

Designs with slack end up almost entirely in LS (`uart` 305 of 323 cells,
`ms_psram_ahb` and `uart_apb_sys` 100 %), tight ones stay in HS
(`rr_arbiter16` 2 cells, `mul32_mac` 64 of 4990). Union mapping puts more
cells in LS but pays 8 % area to repair what ABC mapped slow; the fastest
mapping is the default. With all four tall libraries the recovery walks each
cell down the speed ladder (HS → MS → LP → LS) as far as its slack allows:
`alu32` ends with 1629 MS, 273 LS, 269 LP and no HS cells, `zxip` with 7166
LP, `mul32_mac` keeps 4913 of 4990 in HS (`mix-all.csv`).

Leakage is reported from the liberty (`cell_leakage_power`, else the mean of
the `leakage_power` groups). Only the HS liberties are populated; MS, LS and
LP state zero for most combinational cells at every corner, so the leakage
column is a lower bound for mixed netlists and the library mix is the
honest metric. `sky130_fd_sc_hvl` runs as a single library (57 cells,
`tt_025C_3v30` / `ss_100C_3v00` / `ff_n40C_4v40`); at the HD periods it
closes 1 of 16 designs at 2.4× the area, as expected for a 3.3 V
thick-oxide library (`lib-hvl.csv`).

### 2.14 Repairs as validated transactions; several candidates through the post-pass

A user running an MCU with SRAM macros on the HS library reported (on an
earlier version) a delay cell inserted at a register's Q output, a hold batch
rejected as a whole and the pass stopping, and a hold-stage exception that
discarded the setup gains. The post-pass now:

- classifies every hold endpoint pin from the liberty (`LibCells.pin_kind`:
  clock, data/enable, async control, output; clock pins are also recognised
  as the `related_pin` of setup/hold arcs when a stripped liberty lacks
  `clock : true`) and only delays data/enable pins;
- deduplicates endpoints, delays them as one batch and bisects on rejection,
  so a feasible subset lands instead of nothing;
- checks every edited netlist structurally before STA (one module, known
  cells, liberty pins, one driver per net) and rejects it on failure;
- runs each phase as a transaction with an explicit status; a failure keeps
  the last accepted netlist of the earlier phases (`resize.json` → `status`);
- with `resize_candidates: N` sizes the selected candidate, the fastest and
  the Pareto front, then applies the selection rule to the post-pass numbers
  (`postpass.json`). On `rr_arbiter16` at 3.3 ns none of three candidates
  closes, and the re-selection picks the knee again; on a design where only
  the fastest candidate closes after sizing, it now wins.
- reports cells/area after the post-pass and the worst slack per path group
  (per clock) at the sign-off corners in `summary.md`.

Second review round (same user, HS MCU): path-group names with spaces
(`path delay`) are kept whole in the per-group table; the hold search orders
endpoints worst-first, separates setup-sensitive endpoints (within 300 ps of
the setup floor) and retries both halves of a rejected batch, with explicit
STA-call and path-count budgets; the report describes the delivered netlist
(cells, mix, hold re-timed after a rollback; `timing.setup_met` /
`hold_met` apart from the phase status); candidate post-passes run in
parallel with content-keyed checkpoints (a rerun with unchanged inputs takes
seconds).

### 2.15 Named scenarios: rank on one, accept on all

One SDC cannot describe a functional mode, a scan mode and a sleep mode at
once, and one worst-slack number cannot tell a fixed CDC bound from a clock
period violation. Scenarios fix both: the `rank: true` scenario drives
synthesis and ranking exactly as `sdc:` did; every `required` scenario is
then checked at each of its corners for each check type (setup, hold,
recovery, removal, via `report_check_types -verbose`) and each path group
(fixed bounds included). The smallest-area candidate passing everything is
the winner; otherwise the fallback choice is delivered and marked NOT CLOSED
with the failing `scenario@corner: check slack` list. The post-pass applies
the same rule: a move is rejected when a required check that passed on the
input netlist would fail. A Tcl constraint hook runs after `link_design` in
every session, with `synth_scenario` / `synth_corner` / `synth_module` set
and a binding audit (`require_binding`) that aborts the run (exit 3) when a
required object is missing. Clock uncertainty can come from an explicit
budget (setup = jitter + skew + margin, hold = skew + margin, pre/post-CTS
skew) whose components are reported.

On the SRAM wrapper with `func` (rank) and `sleep` (slow corner only):
both candidates pass 4 required checks and the module is closed; with a
0.5 ns `set_max_delay` in `sleep`, both fail by −4.3 ns, the winner is
delivered NOT CLOSED with `sleep@slow: setup -4.256` listed, and a hook
requiring a non-existent cell aborts the run before ranking.

## 3. Target architecture (revised after §2.5)

```
SDC + YAML ──► sdc_parse (tclsh stubs) ──► constraints (clocks, I/O delays, exceptions)
                                              │
                    liberty (t_cq, t_su) ─────┤
                                              ▼
                 global ABC target  ──► single abc call per recipe (-D = search variable)
                                              ▼
                       OpenSTA (full SDC) ──► WNS / TNS per netlist ──► -D bisection per design
                                              ▼
                     failing endpoints ──► full flop-bounded cone un-map / re-map
                                              │   (constr = driving flop, D-pin load)
                                              └── accept on TNS ── equiv ──┘
                                              ▼
                     area recovery on slack-rich cones ──► sizing ──► reports
```

Design rules:

- **OpenSTA is the judge.** ABC's internal model has no wire load and one
  global driver, so every ABC move is a search step validated by STA.
- **Never cut inside combinational logic** (§2.5). Cones handed to ABC are
  bounded by flops or ports, and their constraint file describes the real
  boundary: the driving flop cell and the D-pin capacitance.
- **Take all failing endpoints per iteration** and converge on TNS, not
  WNS, to avoid whack-a-mole.
- **Depth-bound versus drive-bound.** Few levels with bad slews means
  sizing, not remapping. The refine loop routes those cones to the sizing
  pass.
- **Names must survive.** No renaming or purge passes between STA and the
  reload.
- **Equivalence after every accepted step.** Cheap with `equiv_simple`, and
  mandatory for a tool that rewrites parts of a mapped netlist.

Interface and configuration for this flow are specified in [cli.md](cli.md);
SDC command coverage in [sdc-support.md](sdc-support.md).
