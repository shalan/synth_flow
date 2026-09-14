<!-- SPDX-License-Identifier: Apache-2.0 -->
# SDC support

One SDC file is the single source of timing constraints. It has two readers:

1. **OpenSTA** sources the file verbatim (quick STA for ranking, multi-corner
   STA on the winner, and the refine loop). Every command OpenSTA supports
   therefore shapes winner selection and every accept/reject decision, even
   when synthesis cannot act on it directly.
2. **Synthesis** reads a subset through a small Tcl stub interpreter and turns
   it into path groups, delay budgets and per-group ABC constraint files.

## Today

- `sdc:` in the YAML or `--sdc FILE` on the command line. OpenSTA sources the
  file verbatim, **after** the tool's defaults, so per-port delays, driving
  cells, loads and exceptions in the SDC override them.
- Synthesis reads the same file through `sdc_parse.py` (a `tclsh` stub
  interpreter) and applies these overrides, each one logged as
  `[sdc] override: ...` and recorded in `results/<module>/synth.sdc`:
  - fastest primary clock → `clock_port` / `period_ps` (ABC `-D`)
  - second primary clock → `clock_port_2` / `period_ps_2`
  - `set_clock_uncertainty` on that clock → `clock_uncertainty_*_ps`
  - `set_driving_cell` → `driving_cell`; `set_load` (max) → `load_ff`
  - `set_dont_use` → merged into `dont_use` (passed to `abc` / `dfflibmap`)
  - `set_max_fanout` → `max_fanout` (sink group size of `repair_design`)
- Unknown commands are logged as warnings and left to OpenSTA. Recognised
  STA-only commands are counted in the log.
- `python3 sdc_parse.py top.sdc --netlist rtl.v --top NAME` prints what
  synthesis understood, what is STA-only and what is unknown.
- Async-reset false paths are derived in the STA script: an input port whose
  fanout ends only at register async pins (`all_registers -async_pins`) gets
  `set_false_path -from`. The fixed name list (`PRESETn`, `hresetn`,
  `rst_n`, ...) remains as a fallback. Your SDC's own `set_false_path`
  lines are applied as well (it is sourced last).

## Command reference

The README's *SDC support* section lists what the default (flat) flow does
with each command. The table below adds the per-cone budgeting effects that
apply only with `path_groups: true`, the experimental partitioned mapping
(off by default; measured worse than flat mapping, architecture.md §2.5).
With `path_groups` off, the exception commands are honored by OpenSTA for
ranking, repairs and sign-off, but do not change the mapping.

### Commands used by synthesis (flat flow, and with `path_groups`)

| Command | Synthesis effect |
|---|---|
| `create_clock -period T [get_ports P]` | Period and domain for the clock port. Replaces `period_ps` / `clock_port`. |
| `create_generated_clock -divide_by N` / `-multiply_by N` | Derived period for that domain. |
| `set_clock_uncertainty -setup U` | Subtracted from every budget in the domain. |
| `set_clock_groups -asynchronous` / `-exclusive` | Domain partition; cross-domain cones get a relaxed target. |
| `set_input_delay -max D -clock C [ports]` | in→reg budget = T − D − t_su. Distinct values create distinct groups. |
| `set_output_delay -max D -clock C [ports]` | reg→out budget = T − t_cq − D. |
| `set_false_path -from` / `-to` (ports, clocks, registers) | Cone moved to a relaxed group with a very large target. |
| `set_multicycle_path -setup N -to [regs]` | Endpoint cone budget = N·T. |
| `set_max_delay D -from -to` | Used as the budget for that cone. |
| `set_driving_cell -lib_cell X [ports]` | Written to the `-constr` file of the groups it touches. |
| `set_load L [ports]` | Same, as the group load. |
| `set_max_fanout N` | ABC `buffer -N` limit in the recipe. |
| `set_dont_use [cells]` | Cells removed from the liberty seen by `abc` and `dfflibmap`. |
| `set_dont_touch [module instances]` | Added to `keep_hierarchy_modules`. |

Budgets use flop clock-to-Q (`t_cq`) and setup (`t_su`) parsed from the
synthesis liberty. See [architecture.md](architecture.md) §3.

### Recognized, STA-only

These are parsed without error and left to OpenSTA. The parser prints one
informational line per command so the log shows what synthesis skipped.

`set_clock_latency`, `set_input_transition`, `set_max_transition`,
`set_max_capacitance`, `set_min_delay`, `set_input_delay -min`,
`set_output_delay -min`, `set_clock_uncertainty -hold`, `set_case_analysis`,
`set_propagated_clock`, `set_ideal_network`, `set_load -min/-max` (the
`-max` value is used as the group load).

### Anything else

Passed through to OpenSTA untouched with a warning naming the line.

### Object queries and Tcl

The SDC runs in a real Tcl interpreter (`tclsh`) with stub procs, so
variables, `expr`, `foreach`, wildcards, `get_ports`, `get_clocks`,
`all_inputs [-no_clocks]`, `all_outputs` and `all_registers` behave normally.

`get_pins` and `get_cells` on **registers** work when `keep_names: true`
names the flops after their RTL wires (`acc[3]_reg`); without it, flattened
flops come out as `_889_` and only ports, macro instances and kept-hierarchy
instances have stable names. Internal combinational net and cell names never
survive, so a `-through` on one of those is honored by STA only, and the
tool lists those lines.

### Precedence

- If the SDC defines a clock on a port, it wins over `period_ps` /
  `clock_port` / `clock_port_2` / `period_ps_2`; the override is logged.
- If the SDC sets a driving cell or load, it wins over `driving_cell` /
  `load_ff`.
- YAML values remain as defaults for anything the SDC does not define.
- A `-lib_cell` that is not in the synthesis liberty (an HD SDC run against
  `sky130_fd_sc_hs/ms/ls/lp`) is replaced by the same-named cell of that
  library (`sky130_fd_sc_hd__inv_1` → `sky130_fd_sc_hs__inv_1`), else by the
  library's default inverter. STA sources the adapted copy,
  `results/<top>/<name>.libadapted.sdc`, and the substitution is logged.
  The same rule applies to the YAML `driving_cell`.

### Scenarios and the constraint hook

Several SDCs can be named scenarios (`scenarios:` in the YAML, see
yaml-config.md). Synthesis and ranking read the `rank: true` scenario; every
`required` scenario governs acceptance and the post-pass; all are reported
at sign-off per corner and check type.

`constraint_hook: file.tcl` is sourced by OpenSTA in this order, in every
STA session:

```
read_liberty …  →  read_verilog netlist  →  link_design  →  defaults
→ clock_budget uncertainty  →  scenario SDC  →  constraint hook
→ binding validation (fails the run on a missing/miscounted required binding)
→ timing checks
```

The hook applies constraints directly on the mapped design:

```tcl
puts "hook: $synth_scenario @ $synth_corner ($synth_module)"
require_binding sram      [get_cells u_sram] -count 1
require_binding din_pins  [get_pins u_sram/din0*] -count 32
require_binding acc_regs  [get_cells acc*] -min 32          ;# needs keep_names: true
optional_binding debug    [get_cells dbg*]
set_false_path -from [get_ports rst_n]
set_multicycle_path -setup 2 -from [get_cells key_r*] -to [get_cells acc*]
set_multicycle_path -hold  1 -from [get_cells key_r*] -to [get_cells acc*]
if {$synth_scenario eq "scan"} { set_case_analysis 1 [get_ports scan_en] }
```

`bindings.json` next to the results records each binding's name, status and
resolved objects; `constraint_hook.tcl` is the copy that was used.

### Derived constraints file

For every module, synthesis writes `results/<module>/synth.sdc`: the clocks,
budgets, groups and per-group driving cell / load it actually used. Diffing
it against the input SDC is the fastest way to see what was understood.

## Hierarchical mode

The SDC describes the top. Leaf modules get default I/O delays (20 % of the
period) until the time-budgeting step in Phase 6 propagates constraints
across module boundaries.

## Examples

Minimal:

```tcl
create_clock -name clk -period 8.0 [get_ports clk]
set_clock_uncertainty -setup 0.25 [get_clocks clk]
set_false_path -from [get_ports rst_n]
set_input_delay  -clock clk -max 2.0 [all_inputs -no_clocks]
set_output_delay -clock clk -max 2.0 [all_outputs]
set_driving_cell -lib_cell sky130_fd_sc_hd__inv_1 [all_inputs -no_clocks]
set_load 0.033 [all_outputs]
```

Two asynchronous clocks with per-bus I/O delays (from `bench/external/zxip`):

```tcl
create_clock -name hclk -period 10.0 [get_ports hclk]
create_clock -name pclk -period 10.0 [get_ports pclk]
set_clock_groups -asynchronous -group {hclk} -group {pclk}
set ahb_in [get_ports {haddr[*] htrans[*] hwrite hsize[*] hsel hready hwdata[*]}]
set_input_delay  -clock hclk -max 4.0 $ahb_in
set_output_delay -clock hclk -max 4.0 [get_ports {hrdata[*] hreadyout hresp}]
```

Synthesis derives: two domains, an in→reg group for the AHB inputs with
budget 10 − 4 − t_su, a reg→out group for the AHB outputs with budget
10 − t_cq − 4, a relaxed group for hclk↔pclk crossings, and reg→reg groups
per domain.
