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
- Unknown commands are logged as warnings and left to OpenSTA. Recognised
  STA-only commands are counted in the log.
- `python3 sdc_parse.py top.sdc --netlist rtl.v --top NAME` prints what
  synthesis understood, what is STA-only and what is unknown.
- Async-reset false paths still come from a fixed name list; the SDC's
  `set_false_path -from` ports are reported in `synth.sdc` and become
  relaxed path groups in Phase 2.

## Target (Phase 1 and later)

### Commands used by synthesis

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

`get_pins` and `get_cells` on **registers** work: register names survive
synthesis. Internal combinational net and cell names do not, so a `-through`
on one of those is honored by STA only, and the tool lists those lines.

### Precedence

- If the SDC defines a clock on a port, it wins over `period_ps` /
  `clock_port` / `clock_port_2` / `period_ps_2`; the override is logged.
- If the SDC sets a driving cell or load, it wins over `driving_cell` /
  `load_ff`.
- YAML values remain as defaults for anything the SDC does not define.

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
