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

Each netlist gets a quick OpenSTA run at the slow corner (`QSTA_TCL`). The
objective (`delay`, `area`, `fastest`, `balanced`, `pareto`) picks a winner
from `(wns, tns, area, cells)`. The user SDC, if configured, is sourced into
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
with the name list as fallback. One residual: the multi-corner script
(`define_corners fast typical slow`) reports slow-corner slack about 30 ps
(0.8 %) more pessimistic than the single-library quick STA on the same
netlist and constraints; ranking uses the quick STA, the report the corner
STA, so numbers in `summary.json` differ from `selection.json` by that much.

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
