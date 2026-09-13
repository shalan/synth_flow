<!-- SPDX-License-Identifier: Apache-2.0 -->
# Interface specification

This is the target interface for synth_flow as the [roadmap](roadmap.md)
lands. Sections marked **(today)** describe current behavior; everything else
is the specification the phases implement. Backward compatibility rule: the
existing `python3 synth_flow.py --config synth.yaml` invocation keeps working
as an alias for `synth_flow run`.

## Command line

```
synth_flow run      synth.yaml [--sdc top.sdc] [--stage map|search|refine|all]
                               [--objective delay|area|balanced] [--full-sweep] [--select-margin-ps N]
                               [--recipes R ...] [--modules M ...] [--hierarchical]
                               [--parallel N] [--work-dir D] [--results-dir D] [--json]
synth_flow sdc      check top.sdc --top NAME [--rtl ...]   # what synthesis uses / STA-only / ignored
synth_flow resize   netlist.v --sdc top.sdc --lib LIB --period-ps N --clock-port C   # today: python3 resize.py
synth_flow refine   netlist.v --sdc top.sdc --lib LIB [--lib-slow ..] [--iters N] [--margin-ps N]  # today: python3 refine.py
synth_flow sta      netlist.v --sdc top.sdc --lib-slow .. --lib-typ .. --lib-fast .. [--sdf out.sdf]
synth_flow lec      golden.v revised.v --lib LIB
synth_flow bench    [--designs ...] [--recipes ...] [--quick] [--tag T] [--compare A.csv B.csv]
synth_flow modules  synth.yaml                              # (today: --list-modules)
synth_flow recipes                                          # list recipes, class, notes
```

`refine`, `sta` and `lec` accept a bare netlist so they work on netlists from
other flows. `--json` prints machine-readable results on stdout; the Markdown
report is still written to the results directory.

### Stages of `run`

| Stage | What runs | Phase |
|---|---|---|
| `map` | Recipe sweep, quick STA, winner selection, corner STA, with the configured `abc_target`. | today |
| `search` | Per-group `-D` bisection with OpenSTA feedback. | 3 |
| `resize` | OpenSTA-guided drive-strength sizing of the winner (today: `--resize`). | today |
| `refine` | Whole-design remap passes with equivalence (`refine.py --whole-only`); cone remaps measured negative. | 4 |
| `all` | All of the above. Default once `search` exists. | 3 |

Until Phase 3, `run` behaves as `--stage map`.

### Current flags (today)

Documented in the README CLI table: `--config`, `--rtl`, `--lib`,
`--lib-fast`, `--lib-slow`, `--macro-lib`, `--top`, `--period-ps`,
`--clock-port`, `--objective`, `--modules`, `--recipes`, `--driving-cell`,
`--load-ff`, `--parallel`, `--no-sta`, `--no-gls`, `--abc-sequential`,
`--hierarchical`, `--depth-only`, `--list-modules`, `--work-dir`,
`--results-dir`. These map 1:1 onto `run` options.

## Configuration

Two inputs: the YAML describes the design and the flow, the SDC describes
timing. Existing keys are documented in [yaml-config.md](yaml-config.md).
New keys:

```yaml
sdc: constraints/top.sdc          # today: STA + synthesis clocks/boundary conditions (see sdc-support.md)
abc_target: none                  # today: none (default, best) | period | reg2reg | <ps>
resize_winner: true               # today: OpenSTA-guided sizing of each winner (--resize)
dont_use:                         # cells hidden from abc and dfflibmap
  - sky130_fd_sc_hd__probe*
  - sky130_fd_sc_hd__lpflow*
yosys_opts: [booth, adder=kogge-stone]   # today; or {cpu: [booth], '*': []} per module; tokens incl. opt_dff_sat, opt_full
yosys_opts_sweep: {cpu: [[], [booth], [adder=kogge-stone]], '*': [[]]}   # per-module sweep
search:
  enable: true
  max_sta_calls: 6
refine:
  iters: 5
  margin_ps: 50
  recipe: delay_choice_deep
  area_recipe: area_classic
  lec: true                       # fail hard if equivalence is not proven
```

Precedence between SDC and YAML is defined in [sdc-support.md](sdc-support.md).

## Outputs

```
results/
  summary.md / summary.json / summary.csv     # (today)
  <module>/
    winner.v, winner.sdf, sta.rpt, selection.json   # (today)
    synth.sdc        # constraints synthesis acted on             (Phase 1)
    groups.json      # per path group: budget, cells, achieved slack (Phase 2/3)
    refine.json      # per iteration: endpoints, cone size, TNS before/after, accepted, lec (Phase 4)
    lec.log          # equivalence proof log                       (Phase 4)
```

Exit codes: `0` success, `1` no winner for a module, `2` a winner misses setup at
the slow corner (`fail_on_timing`), `3` gate-level simulation failed, `4`
configuration error, `5` a post-pass phase (sizing, buffering, recovery,
hold) aborted on some module (the delivered netlists are the last accepted
ones; see `resize.json` → `status`), `6` `--strict`: a module is NOT CLOSED
under its required scenarios or misses setup, `7` the constraint hook's
`require_binding` did not resolve.

`summary.json` gains `env` (yosys, abc, opensta versions; recipe hashes;
synth_flow git SHA) in Phase 6.

## Python API

Mirrors the CLI so `bench.py`, tests and agents can call the flow without a
subprocess.

```python
from synth_flow import Flow

flow = Flow.from_yaml("synth.yaml", sdc="constraints/top.sdc")
res  = flow.run(stage="all")

res.modules["cpu"].winner            # recipe name
res.modules["cpu"].wns_setup_slow    # ns
res.modules["cpu"].groups            # list of PathGroup(budget_ps, cells, slack_ns)
res.modules["cpu"].refine.iterations # list of RefineStep(...)
res.write_reports("results/")
```

Sub-steps are exposed for standalone use:

```python
from synth_flow.sdc import parse_sdc
from synth_flow.sta import run_sta, failing_endpoints
from synth_flow.refine import refine_netlist
from synth_flow.lec import prove_equivalent
```

## Package layout (Phase 6)

```
synth_flow/
  __init__.py      Flow, public API
  config.py        YAML loading, validation, precedence
  sdc/parse.py     Tcl stub interpreter → constraints JSON
  liberty.py       t_cq / t_su / cell list extraction
  drivers.py       Yosys script templates (std, hier, groups, refine)
  select.py        path-group and cone selection builders
  sta.py           OpenSTA scripts and parsers
  search.py        per-group -D bisection
  refine.py        cone resynthesis loop
  lec.py           equivalence checking
  report.py        summary writers
  cli.py           argparse subcommands
```

`pyproject.toml` provides the `synth_flow` console script and lists PyYAML
as the only Python dependency. Yosys, ABC, OpenSTA and iverilog stay external.
