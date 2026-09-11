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

## 3. Target architecture

```
SDC + YAML ──► sdc/parse (tclsh stubs) ──► constraints JSON
                                              │
                    liberty (t_cq, t_su) ─────┤
                                              ▼
              path groups + budgets ──► partitioned mapping (per-group abc -D)
                                              ▼
                       OpenSTA (full SDC) ──► per-group slack ──► -D bisection
                                              ▼
                     failing endpoints ──► cone un-map / re-map ──► accept on TNS
                                              │            ▲
                                              └── equiv ───┘
                                              ▼
                     area recovery on slack-rich cones ──► sizing ──► reports
```

Design rules:

- **OpenSTA is the judge.** ABC's internal model has no wire load and one
  global driver, so every ABC move is a search step validated by STA.
- **Full cones bounded by flops.** A partial cone re-creates the arrival
  problem (cut nets look like zero-arrival inputs). Full cones are
  self-consistent; a huge cone degenerates into a global re-run, which is
  still correct.
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
