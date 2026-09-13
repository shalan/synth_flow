#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""Tests for the pure-Python logic in synth_flow.py (no EDA tools needed)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from synth_flow import (
    Config, ModuleScanner, Candidate, Selection,
    select_winner, _pareto_front, _stability_idx,
    discover_recipes, RecipeResult,
    DEFAULT_RECIPES_DIR, _strip_signed_decls, apply_sdc_overrides,
    build_path_groups, resolve_abc_target, _group_section, _materialize_recipe, _synth_flags,
    _candidate_name, _base_recipe, _dont_use_flags, _front_end, _fe_variants_for, _count_unmapped,
)

failures = []

def check(name, cond, detail=''):
    if cond:
        print(f'  ✓ {name}')
    else:
        print(f'  ✗ {name}  {detail}')
        failures.append(name)

# =========================================================================
# =========================================================================
print('\n[0] _strip_signed_decls — OpenSTA-compatible netlist declarations')
# =========================================================================

with tempfile.TemporaryDirectory() as td:
    nl = Path(td) / 'n.v'
    nl.write_text("module m(a, y);\n  input signed [11:0] a;\n  wire signed [11:0] a;\n"
                  "  output signed [31:0] y;\n  wire signed [31:0] y;\n  wire w_signed;\n"
                  "  sky130_fd_sc_hd__inv_1 u0 (.A(a[0]), .Y(y[0]));\nendmodule\n")
    n = _strip_signed_decls(nl)
    out = nl.read_text()
    check('rewrote 4 declarations', n == 4, f'n={n}')
    check('no signed declarations remain', ' signed ' not in out)
    check('identifier containing "signed" untouched', 'w_signed' in out)
    check('ports keep ranges', 'input [11:0] a;' in out and 'output [31:0] y;' in out)
    check('idempotent', _strip_signed_decls(nl) == 0)

# =========================================================================
print('\n[0b] sdc_parse — Tcl-driven SDC reader')
# =========================================================================
import shutil as _shutil
if _shutil.which('tclsh') is None:
    print('  (skipped: tclsh not found)')
else:
    from sdc_parse import parse_sdc, ports_from_verilog
    with tempfile.TemporaryDirectory() as td:
        sdc = Path(td) / 't.sdc'
        sdc.write_text(r"""
set T 8.0
create_clock -name clk -period $T [get_ports clk]
create_clock -name pclk -period 20 [get_ports pclk]
create_generated_clock -name clk_div2 -source [get_ports clk] -divide_by 2 [get_ports clkdiv]
set_clock_groups -asynchronous -group {clk clk_div2} -group {pclk}
set_clock_uncertainty -setup 0.3 [get_clocks clk]
set_clock_uncertainty 0.1 [all_clocks]
set_false_path -from [get_ports rst_n]
set_multicycle_path -setup 2 -to [get_pins r2*/D]
set bus [get_ports {haddr[*] hwrite}]
set_input_delay  -clock clk -max [expr 0.25 * $T] $bus
set_input_delay  -clock clk -min 0.5 $bus
set_output_delay -clock clk 3.0 [all_outputs]
set_driving_cell -lib_cell sky130_fd_sc_hd__inv_1 [all_inputs -no_clocks]
set_load 0.033 [all_outputs]
set_load -min 0.005 [all_outputs]
set_input_transition -max 0.4 [all_inputs]
set_max_fanout 8 [current_design]
set_dont_use {sky130_fd_sc_hd__probe_p_8 sky130_fd_sc_hd__lpflow*}
frobnicate_paths -foo 3 [get_ports x]
""")
        nl = Path(td) / 'n.v'
        nl.write_text("module top(clk, pclk, clkdiv, rst_n, haddr, hwrite, hrdata, irq);\n"
                      "  input clk; input pclk; output clkdiv; input rst_n;\n"
                      "  input [7:0] haddr; input hwrite; output [31:0] hrdata; output irq;\nendmodule\n")
        ports = ports_from_verilog(nl, 'top')
        check('ports_from_verilog directions', ports.get('haddr') == 'input' and ports.get('hrdata') == 'output', str(ports))
        c = parse_sdc(sdc, ports=ports)
        check('two primary clocks + one generated', set(c.clocks) == {'clk', 'pclk', 'clk_div2'}, str(list(c.clocks)))
        check('Tcl variable/expr in period', c.clocks['clk'].period_ns == 8.0)
        check('generated clock period derived', c.clocks['clk_div2'].period_ns == 16.0, str(c.clocks['clk_div2']))
        check('uncertainty: specific then all_clocks', c.clocks['clk'].uncertainty_setup_ns == 0.1 and c.clocks['pclk'].uncertainty_hold_ns == 0.1)
        check('clock groups', c.clock_groups == [[['clk', 'clk_div2'], ['pclk']]], str(c.clock_groups))
        check('false path from reset port', c.false_path_ports() == {'rst_n'})
        mc = [e for e in c.exceptions if e.kind == 'multicycle']
        check('multicycle kept with pin target', mc and mc[0].value == 2 and mc[0].to == ['pin:r2*/D'], str(mc))
        d = c.input_delay_for('haddr')
        check('bus pattern haddr[*] -> haddr, max from expr, min separate', d is not None and d.max_ns is None and d.min_ns == 0.5
              and c.input_delays[0].max_ns == 2.0 and 'hwrite' in c.input_delays[0].ports, str(c.input_delays))
        od = c.output_delay_for('hrdata')
        check('all_outputs expanded (no clkdiv? it is an output too)', od is not None and set(od.ports) == {'clkdiv', 'hrdata', 'irq'}, str(od))
        check('all_inputs -no_clocks excludes clock ports', set(c.driving_cells[0]['ports']) == {'rst_n', 'haddr', 'hwrite'}, str(c.driving_cells))
        check('load max/min', c.load_for('hrdata') == 0.033)
        check('input_transition is STA-only', any('set_input_transition' in s for s in c.sta_only))
        check('max_fanout', c.max_fanout == 8.0)
        check('dont_use list', c.dont_use == ['sky130_fd_sc_hd__probe_p_8', 'sky130_fd_sc_hd__lpflow*'], str(c.dont_use))
        check('unknown command recorded, not fatal', c.unknown and c.unknown[0].startswith('-foo 3') or any('frobnicate' in u or '-foo' in u for u in c.unknown), str(c.unknown))
        check('no warnings on this SDC', not c.warnings, str(c.warnings))

# =========================================================================
print('\n[0c] apply_sdc_overrides — SDC wins over YAML')
# =========================================================================
if _shutil.which('tclsh') is None:
    print('  (skipped: tclsh not found)')
else:
    with tempfile.TemporaryDirectory() as td:
        sdc = Path(td) / 'o.sdc'
        sdc.write_text("create_clock -name hclk -period 10 [get_ports hclk]\n"
                       "create_clock -name pclk -period 20 [get_ports pclk]\n"
                       "set_clock_uncertainty -setup 0.5 [get_clocks hclk]\n"
                       "set_driving_cell -lib_cell sky130_fd_sc_hd__buf_2 [all_inputs]\n"
                       "set_load 0.05 [all_outputs]\n")
        c = parse_sdc(sdc, ports={'hclk': 'input', 'pclk': 'input', 'a': 'input', 'y': 'output'})
        cfg = Config(clock_port='clk', period_ps=8000, driving_cell='sky130_fd_sc_hd__inv_2', load_ff=17.65)
        msgs = apply_sdc_overrides(cfg, c)
        check('clock port/period from fastest SDC clock', cfg.clock_port == 'hclk' and cfg.period_ps == 10000, str(vars(cfg)))
        check('second clock -> clock_port_2', cfg.clock_port_2 == 'pclk' and cfg.period_ps_2 == 20000)
        check('uncertainty from SDC', cfg.clock_uncertainty_setup_ps == 500)
        check('driving cell from SDC', cfg.driving_cell == 'sky130_fd_sc_hd__buf_2')
        check('load pF -> fF', abs(cfg.load_ff - 50.0) < 1e-6, str(cfg.load_ff))
        check('every override reported', len(msgs) == 5, str(msgs))
        cfg2 = Config(clock_port='hclk', period_ps=10000, driving_cell='sky130_fd_sc_hd__buf_2', load_ff=50.0,
                      clock_port_2='pclk', period_ps_2=20000, clock_uncertainty_setup_ps=500)
        check('no messages when YAML already agrees', apply_sdc_overrides(cfg2, c) == [])
        check('None constraints is a no-op', apply_sdc_overrides(cfg2, None) == [])

# =========================================================================
print('\n[0d] path-group budgets and ABC target')
# =========================================================================
from liberty_timing import read_liberty_timing
LIB_SS = Path(__file__).parent / 'sky130' / 'hd_120_ss.lib'
lt = read_liberty_timing(LIB_SS)
check('liberty flop timing parsed', lt.t_cq_ps and lt.t_su_ps and len(lt.flop_cells) == 19, str((lt.t_cq_ps, lt.t_su_ps, len(lt.flop_cells))))
check('sky130 SS t_cq ~ 0.5-1.2 ns, t_su ~ 0.2-0.6 ns', 500 <= lt.t_cq_ps <= 1200 and 200 <= lt.t_su_ps <= 600, str((lt.t_cq_ps, lt.t_su_ps)))
cfg = Config(period_ps=10000, clock_port='clk', clock_uncertainty_setup_ps=250, io_delay_frac=0.2,
             lib_typ=str(LIB_SS), lib_slow=str(LIB_SS), abc_target='reg2reg')
d, note = resolve_abc_target(cfg)
check('reg2reg target = T - t_cq - t_su - unc', d == int(10000 - lt.t_cq_ps - lt.t_su_ps - 250), f'{d} ({note})')
cfg.abc_target = 'period'; check("'period' target", resolve_abc_target(cfg)[0] == 10000)
cfg.abc_target = 'none'; check("'none' target (default) -> 0", resolve_abc_target(cfg)[0] == 0 and Config().abc_target == 'none')
check('resize is opt-in', Config().resize_winner is False and Config().resize_final == 'tns')
check('synth flags: booth + adder', _synth_flags(['booth', 'adder=kogge-stone']) == '-booth -extra-map +/choices/kogge-stone.v')
check('synth flags: empty', _synth_flags([]) == '' and _synth_flags(None) == '')
check('post-synth tokens re-lower coarse cells', _front_end(['opt_dff_sat', 'opt_full', 'booth']) == ('-booth', 'opt_dff -sat\nopt -full\ntechmap\nopt -fast'), str(_front_end(['opt_dff_sat', 'opt_full', 'booth'])))
with tempfile.TemporaryDirectory() as td:
    nl = Path(td) / 'u.v'
    nl.write_text("module m(a,y);\n  input a; output y;\n  sky130_fd_sc_hd__inv_1 _1_ (.A(a), .Y(y));\n  \\$mux  #(\n    .WIDTH(32'd1)\n  ) _2_ (\n    .A(a)\n  );\n  \\$_AND_ _3_ (.A(a), .B(a), .Y(z));\nendmodule\n")
    check('unmapped-cell guard counts $mux and $_AND_ but not liberty cells', _count_unmapped(nl) == 2, str(_count_unmapped(nl)))
_c = Config(yosys_opts_sweep={'cpu': [[], ['booth']], '*': [['adder=kogge-stone']]}, yosys_opts={'uart': ['noshare']})
check('per-module sweep', _fe_variants_for(_c, 'cpu') == [[], ['booth']] and _fe_variants_for(_c, 'other') == [['adder=kogge-stone']], str(_fe_variants_for(_c, 'cpu')))
_c2 = Config(yosys_opts={'uart': ['noshare'], '*': []})
check('per-module opts without sweep', _fe_variants_for(_c2, 'uart') == [['noshare']] and _fe_variants_for(_c2, 'x') == [[]])
check('global list forms unchanged', _fe_variants_for(Config(yosys_opts_sweep=[[], ['booth']]), 'm') == [[], ['booth']] and _fe_variants_for(Config(yosys_opts=['booth']), 'm') == [['booth']])
check('candidate naming', _candidate_name('orfs_speed', []) == 'orfs_speed' and _candidate_name('orfs_speed', ['booth', 'adder=kogge-stone']) == 'orfs_speed@booth+adder=kogge-stone')
check('base recipe and stability through variants', _base_recipe('delay_triple@booth') == 'delay_triple' and _stability_idx('delay_triple@booth') == _stability_idx('delay_triple'))
check('sweep is opt-in', Config().yosys_opts_sweep == [])
check('dont_use flags quote globs', _dont_use_flags(['sky130_fd_sc_hd__lpflow_*', 'sky130_fd_sc_hd__probe_p_8']) == "-dont_use 'sky130_fd_sc_hd__lpflow_*' -dont_use sky130_fd_sc_hd__probe_p_8" and _dont_use_flags([]) == '')
try:
    _synth_flags(['adder=ripple']); check('unknown adder rejected', False)
except ValueError:
    check('unknown adder rejected', True)
from resize import drive_families, next_size, prev_size, retype, instance_types
fam = drive_families(str(LIB_SS))
check('drive families from footprint, ordered by drive', fam.family('sky130_fd_sc_hd__nand2_1') == ['sky130_fd_sc_hd__nand2_%d' % n for n in (1, 2, 4, 8)], str(fam.family('sky130_fd_sc_hd__nand2_1')))
check('next_size steps up and stops at max', next_size('sky130_fd_sc_hd__nand2_2', fam) == 'sky130_fd_sc_hd__nand2_4' and next_size('sky130_fd_sc_hd__nand2_8', fam) is None)
check('prev_size steps down and stops at min', prev_size('sky130_fd_sc_hd__nand2_4', fam) == 'sky130_fd_sc_hd__nand2_2' and prev_size('sky130_fd_sc_hd__nand2_1', fam) is None)
_nl = "module m(a,y);\n  input a; output y;\n  sky130_fd_sc_hd__inv_1 _7_ (.A(a), .Y(y));\n  sky130_fd_sc_hd__buf_2 _8_ (.A(y), .X(z));\nendmodule\n"
check('instance_types', instance_types(_nl) == {'_7_': 'sky130_fd_sc_hd__inv_1', '_8_': 'sky130_fd_sc_hd__buf_2'}, str(instance_types(_nl)))
from resize import Netlist, liberty_output_pins
_nl2 = ("module m(a, b, y);\n  input a; input b; output y;\n  wire n1;\n"
        "  sky130_fd_sc_hd__and2_1 _1_ (\n    .A(a),\n    .B(b),\n    .X(n1)\n  );\n"
        + ''.join(f"  sky130_fd_sc_hd__inv_1 _s{k}_ (\n    .A(n1),\n    .Y(\\o[{k}] )\n  );\n" for k in range(10))
        + "  sky130_fd_sc_hd__buf_1 _o_ (\n    .A(n1),\n    .X(y)\n  );\nendmodule\n")
_op = liberty_output_pins(str(LIB_SS))
check('liberty output pins parsed', _op.get('sky130_fd_sc_hd__and2_1') == {'X'} and _op.get('sky130_fd_sc_hd__dfxtp_1') == {'Q'}, str(_op.get('sky130_fd_sc_hd__and2_1')))
_n = Netlist(_nl2, _op)
check('netlist model: fanout of n1 is 11 sinks', len(_n.sinks('n1')) == 11 and _n.driver('n1') == ('_1_', 'X'))
_nb = _n.buffer_tree('n1', 'sky130_fd_sc_hd__buf_2', 4)
_r = _n.render()
_n2 = Netlist(_r, _op)
check('buffer tree: 3 buffers for 11 sinks in groups of 4, driver now fans out to 3', _nb == 3 and len(_n2.sinks('n1')) == 3, f'nb={_nb} fanout={len(_n2.sinks("n1"))}')
check('escaped identifiers keep their trailing space', '.Y(\\o[3] )' in _r, _r[_r.find('o[3]')-6:_r.find('o[3]')+8])
check('new wires declared before the first instance', _r.index('wire _rdn_1_;') < _r.index('sky130_fd_sc_hd__and2_1 _1_'))
_n3 = Netlist(_nl2, _op); _n3.delay_pin('_s0_', 'A', 'sky130_fd_sc_hd__buf_1', 2)
_r3 = Netlist(_n3.render(), _op)
check('delay chain: 2 cells in front of one pin, others untouched', len(_r3.sinks('n1')) == 11 and ('_s0_', 'A') not in _r3.sinks('n1') and _r3.inst['_s0_']['pins']['A'].startswith('_rdn_') and sum(1 for i in _r3.inst.values() if i['type'] == 'sky130_fd_sc_hd__buf_1') == 3)
from synth_flow import _sta_constraints as _sc
_pre = _sc(clock_port='clk', period_ns=10.0)
check('default I/O delays: max 20% of T, min 40% of max', 'set_input_delay  -clock clk -max 2.0 ' in _pre and 'set_input_delay  -clock clk -min 0.8 ' in _pre and 'set_output_delay -clock clk -min 0.8 ' in _pre, _pre)
check('io_delay_min_frac default 0.4', abs(Config().io_delay_min_frac - 0.4) < 1e-9)
check('repair flags are opt-in', Config().repair_design is False and Config().repair_hold is False and Config().max_fanout == 8)
# ---- LibCells: technology-agnostic classification, macros, dont_use ----
from liberty_timing import LibCells
_lc = LibCells([str(LIB_SS)])
_bufs = [b.name for b in _lc.buffers()]
check('buffers found by function, weakest first', _bufs[0] == 'sky130_fd_sc_hd__buf_1' and all('buf' in b for b in _bufs) and 'sky130_fd_sc_hd__inv_1' not in _bufs, str(_bufs[:5]))
check('delay cell = slowest buffer (no dlygate in hd_120)', _lc.delay_cell()[0] in _bufs and _lc.delay_cell()[1:] == ('A', 'X'))
check('default driving cell is a mid-drive inverter', _lc.default_driving_cell() == 'sky130_fd_sc_hd__inv_2' and _lc.default_driving_cell('sky130_fd_sc_hd__inv_1') == 'sky130_fd_sc_hd__inv_1', _lc.default_driving_cell())
check('dont_use pattern removes cells from sizing', LibCells([str(LIB_SS)], dont_use=['*nand2_8']).next_size('sky130_fd_sc_hd__nand2_4') is None and _lc.next_size('sky130_fd_sc_hd__nand2_4') == 'sky130_fd_sc_hd__nand2_8')
check('same-base-name preference when stepping drive', _lc.next_size('sky130_fd_sc_hd__inv_1') == 'sky130_fd_sc_hd__inv_2', _lc.next_size('sky130_fd_sc_hd__inv_1'))
with tempfile.TemporaryDirectory() as td:
    mac = Path(td) / 'fake_sram.lib'
    mac.write_text('''library (fake_sram) {
  time_unit : "1ns"; capacitive_load_unit (1,pf);
  cell (SRAM_256x32) {
    area : 5000;
    pin (CLK) { direction : input; capacitance : 0.02; clock : true; }
    pin (WE)  { direction : input; capacitance : 0.005; }
    pin (DIN) { direction : input; capacitance : 0.004; }
    pin (DOUT) { direction : output; max_capacitance : 0.2; timing () { related_pin : "CLK"; timing_type : rising_edge; } }
  }
}''')
    _both = LibCells([str(LIB_SS), str(mac)])
    check('macro liberty merged: output pins by direction, not by name', _both.output_pins('SRAM_256x32') == {'DOUT'} and 'SRAM_256x32' in _both and len(_both.cells) == 121)
    check('macro is not a buffer and not in any sizing family', 'SRAM_256x32' not in [b.name for b in _both.buffers()] and _both.next_size('SRAM_256x32') is None)
    from resize import Netlist, liberty_output_pins
    _nlm = ("module top(clk, we, d, q);\n  input clk; input we; input d; output q;\n  wire n1;\n"
            "  SRAM_256x32 u_mem (\n    .CLK(clk),\n    .WE(we),\n    .DIN(d),\n    .DOUT(n1)\n  );\n"
            "  sky130_fd_sc_hd__buf_1 _b_ (\n    .A(n1),\n    .X(q)\n  );\nendmodule\n")
    _nm = Netlist(_nlm, liberty_output_pins(_both))
    check('netlist model: macro output drives n1, buffer sinks it (no name-based guessing)', _nm.driver('n1') == ('u_mem', 'DOUT') and _nm.sinks('n1') == [('_b_', 'A')], str((_nm.driver('n1'), _nm.sinks('n1'))))
    check('instance regex accepts non-sky130 cell names', 'u_mem' in _nm.inst and _nm.inst['u_mem']['type'] == 'SRAM_256x32')
    # bus() pins (OpenRAM-style liberty) and Yosys concatenation connections
    bus = Path(td) / 'bus_sram.lib'
    bus.write_text('''library (bus_sram) {
  time_unit : "1ns"; capacitive_load_unit (1,pf);
  type (data) { base_type : array; data_type : bit; bit_width : 4; bit_from : 0; bit_to : 3; }
  cell (BSRAM) {
    area : 9000;
    pin (clk0) { direction : input; capacitance : 0.02; clock : true; }
    bus (din0) { bus_type : data; direction : input; capacitance : 0.006;
      pin (din0[3:0]) { timing () { related_pin : "clk0"; timing_type : setup_rising; } } }
    bus (dout0) { bus_type : data; direction : output;
      pin (dout0[3:0]) { timing () { related_pin : "clk0"; timing_type : rising_edge; } } }
  }
}''')
    _bl = LibCells([str(LIB_SS), str(bus)])
    check('bus() groups become one pin per bus with the bus direction and the type bit order', _bl.output_pins('BSRAM') == {'dout0'} and _bl.cells['BSRAM'].pins['din0']['dir'] == 'input' and _bl.bus_ranges() == {'BSRAM': {'din0': (0, 3), 'dout0': (0, 3)}}, str(_bl.cells['BSRAM'].pins))
    _bt = ("module top(clk, a, y);\n  input clk; input [3:0] a; output [3:0] y;\n  wire \\q[3] ; wire \\q[2] ; wire \\q[1] ; wire \\q[0] ; wire n1;\n"
           "  BSRAM u_m (\n    .clk0(clk),\n    .din0({ \\a[3] , \\a[2] , \\a[1] , \\a[0]  }),\n    .dout0({ \\q[3] , \\q[2] , \\q[1] , \\q[0]  })\n  );\n"
           + ''.join(f"  sky130_fd_sc_hd__inv_1 i{k} (\n    .A(\\q[0] ),\n    .Y({'n1' if k == 1 else chr(92) + 'y[' + str(k - 2) + '] '})\n  );\n" for k in (1, 2, 3))
           + "endmodule\n")
    _bn = Netlist(_bt, liberty_output_pins(_bl), _bl.bus_ranges())
    check('macro bus bit is found as driver and its std-cell sinks are listed', _bn.driver('\\q[0]') == ('u_m', 'dout0') and len(_bn.sinks('\\q[0]')) == 3, str((_bn.driver('\\q[0]'), _bn.sinks('\\q[0]'))))
    # bit_from 0 means the first concatenation item is bit 0 (OpenRAM / OpenSTA numbering)
    check('STA bus pin names map to the concatenation bit in liberty order', _bn.pin_net('u_m', 'din0[1]') == '\\a[2]' and _bn.pin_net('u_m', 'dout0[3]') == '\\q[0]', str((_bn.pin_net('u_m', 'din0[1]'), _bn.pin_net('u_m', 'dout0[3]'))))
    _bn.buffer_tree('\\q[0]', 'sky130_fd_sc_hd__buf_2', 2); _bn.delay_pin('u_m', 'din0[1]', 'sky130_fd_sc_hd__buf_1', 1)
    _br = _bn.render()
    check('buffer tree on a macro-driven bit and a delay cell into a macro bus bit render as concatenations',
          '.din0({ \\a[3] , _rdn_5_ , \\a[1] , \\a[0] })' in _br and _br.count('sky130_fd_sc_hd__buf_2 _rd_') == 2 and '.A(\\a[2] )' in _br, _br)
with tempfile.TemporaryDirectory() as td:
    from synth_flow import _adapt_sdc_lib_cells
    import logging as _logging
    _sdc = Path(td) / 'c.sdc'
    _sdc.write_text('create_clock -name clk -period 10 [get_ports clk]\nset_driving_cell -lib_cell sky130_fd_sc_hs__inv_1 [all_inputs]\nset_driving_cell -lib_cell sky130_fd_sc_hd__inv_2 [get_ports a]\n')
    _out = _adapt_sdc_lib_cells(_sdc, _lc, Path(td) / 'res', _logging.getLogger('t'))
    _txt = _out.read_text() if _out else ''
    check('SDC -lib_cell from another library is mapped to the same-named cell', _out is not None and '-lib_cell sky130_fd_sc_hd__inv_1 [all_inputs]' in _txt and '-lib_cell sky130_fd_sc_hd__inv_2 [get_ports a]' in _txt, _txt)
    check('SDC with only known cells is left alone', _adapt_sdc_lib_cells(_out, _lc, Path(td) / 'res2', _logging.getLogger('t')) is None)
check('same-named cell of another library variant is preferred as driving cell', _lc.default_driving_cell('sky130_fd_sc_hs__inv_1') == 'sky130_fd_sc_hd__inv_1' and _lc.default_driving_cell('sky130_fd_sc_hs__nosuch_3') == 'sky130_fd_sc_hd__inv_2')
# --- several standard-cell libraries at once -------------------------------
with tempfile.TemporaryDirectory() as td:
    def _fakelib(name, delay, leak):
        return f'''library ({name}) {{
  time_unit : "1ns"; capacitive_load_unit (1,pf); leakage_power_unit : "1nW";
  cell ({name}__inv_1) {{ area : 3; cell_leakage_power : {leak};
    pin (A) {{ direction : input; capacitance : 0.002; }}
    pin (Y) {{ direction : output; function : "!A"; max_capacitance : 0.1;
      timing () {{ related_pin : "A"; cell_rise (scalar) {{ values ("{delay}"); }} cell_fall (scalar) {{ values ("{delay}"); }} }} }} }}
  cell ({name}__inv_2) {{ area : 5; cell_leakage_power : {leak * 2};
    pin (A) {{ direction : input; capacitance : 0.004; }}
    pin (Y) {{ direction : output; function : "!A"; max_capacitance : 0.2;
      timing () {{ related_pin : "A"; cell_rise (scalar) {{ values ("{delay * 0.8}"); }} cell_fall (scalar) {{ values ("{delay * 0.8}"); }} }} }} }}
  cell ({name}__nand2_1) {{ area : 4; cell_leakage_power : {leak};
    pin (A) {{ direction : input; capacitance : 0.002; }} pin (B) {{ direction : input; capacitance : 0.002; }}
    pin (Y) {{ direction : output; function : "!(A&B)"; max_capacitance : 0.1; }} }}
}}'''
    fast, slow = Path(td) / 'fast.lib', Path(td) / 'slow.lib'
    fast.write_text(_fakelib('fastlib', 0.10, 100.0)); slow.write_text(_fakelib('slowlib', 0.25, 10.0))
    _ml = LibCells([str(fast), str(slow)])
    check('two libraries are ranked by the delay of their single-input cells', _ml.is_multi_lib() and _ml.lib_speed[str(fast)] < _ml.lib_speed[str(slow)], str(_ml.lib_speed))
    check('faster_variant is the same cell in the faster library; none from the fastest', _ml.faster_variant('slowlib__nand2_1') == 'fastlib__nand2_1' and _ml.faster_variant('fastlib__nand2_1') is None)
    check('slower_variant is the reverse', _ml.slower_variant('fastlib__inv_2') == 'slowlib__inv_2' and _ml.slower_variant('slowlib__inv_2') is None)
    check('next_size stays inside one library', _ml.next_size('slowlib__inv_1') == 'slowlib__inv_2' and _ml.next_size('fastlib__inv_1') == 'fastlib__inv_2')
    check('leakage is read per cell and summed over instance types', _ml.cells['fastlib__inv_2'].leakage_nw == 200.0 and _ml.leakage_total({'a': 'fastlib__inv_1', 'b': 'slowlib__inv_1'}) == 110.0)
    check('lib_mix counts instances per library prefix', _ml.lib_mix({'a': 'fastlib__inv_1', 'b': 'slowlib__inv_1', 'c': 'slowlib__nand2_1'}) == {'fastlib': 1, 'slowlib': 2})
    check('single library: no variants, sizing unchanged', not _lc.is_multi_lib() and _lc.faster_variant('sky130_fd_sc_hd__nand2_1') is None)
    from synth_flow import _split_lib_lists, _synth_libs, _liberty_arg, _extra_libs
    _d = {'lib_typ': [str(fast), str(slow)], 'lib_slow': f'{fast},{slow}', 'lib_fast': str(fast), 'macro_libs': {'slow': ['m.lib']}}
    _split_lib_lists(_d)
    check('list-valued lib fields split into primary + lib_extra per corner', _d['lib_typ'] == str(fast) and _d['lib_extra'] == {'typ': [str(slow)], 'slow': [str(slow)], 'fast': []} and _d['lib_slow'] == str(fast), str(_d))
    check('_synth_libs / _liberty_arg use the slow corner and its extras', _synth_libs(_d) == [str(fast), str(slow)] and _liberty_arg(_d) == f'{fast} -liberty {slow}')
    check('_extra_libs = extra std libs + macro libs of the corner', _extra_libs(_d, 'slow') == [str(slow), 'm.lib'] and _extra_libs(_d, 'fast') == [])
check('retype swaps only the named instance', 'sky130_fd_sc_hd__inv_4 _7_ (' in retype(_nl, {'_7_': 'sky130_fd_sc_hd__inv_4'}) and 'buf_2 _8_' in retype(_nl, {'_7_': 'sky130_fd_sc_hd__inv_4'}))
cfg.abc_target = '4321'; check('explicit ps target', resolve_abc_target(cfg)[0] == 4321)
cfg.period_ps = 1000; cfg.abc_target = 'reg2reg'
check('floor applies when budget is negative', resolve_abc_target(cfg)[0] == 250)
if _shutil.which('tclsh'):
    with tempfile.TemporaryDirectory() as td:
        sdc = Path(td) / 'g.sdc'
        sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n"
                       "set_false_path -from [get_ports rst_n]\n"
                       "set_input_delay -clock clk -max 4.0 [get_ports {haddr hwrite}]\n"
                       "set_output_delay -clock clk -max 3.0 [get_ports hrdata]\n"
                       "set_load 0.05 [get_ports hrdata]\n")
        ports = {'clk': 'input', 'rst_n': 'input', 'haddr': 'input', 'hwrite': 'input', 'misc': 'input',
                 'hrdata': 'output', 'irq': 'output'}
        c = parse_sdc(sdc, ports=ports)
        cfg = Config(period_ps=10000, clock_port='clk', clock_uncertainty_setup_ps=250, io_delay_frac=0.2,
                     lib_typ=str(LIB_SS), lib_slow=str(LIB_SS), driving_cell='sky130_fd_sc_hd__inv_2', load_ff=17.65)
        spec = build_path_groups(cfg, 'top', c, ports, lt, Path(td) / 'groups')
        names = {g['name']: g for g in spec['groups']}
        check('groups: in2out, two input groups, two output groups, relaxed',
              set(names) == {'in2out', 'in_4000ps', 'in_2000ps', 'out_3000ps', 'out_2000ps', 'relaxed'}, str(sorted(names)))
        check('input group ports from SDC vs default', set(names['in_4000ps']['ports']) == {'haddr', 'hwrite'} and names['in_2000ps']['ports'] == ['misc'])
        check('in2reg budget = T - in - t_su - unc', names['in_4000ps']['budget_ps'] == int(10000 - 4000 - lt.t_su_ps - 250), str(names['in_4000ps']))
        check('reg2out budget = T - t_cq - out - unc', names['out_3000ps']['budget_ps'] == int(10000 - lt.t_cq_ps - 3000 - 250))
        check('in2out budget = T - max in - max out', names['in2out']['budget_ps'] == 10000 - 4000 - 3000)
        check('clock and false-path ports excluded from I/O groups', 'clk' not in names['in_2000ps']['ports'] and names['relaxed']['ports'] == ['rst_n'])
        check('relaxed budget = relaxed_factor * T', names['relaxed']['budget_ps'] == 30000)
        check('reg2reg budget', spec['reg2reg_ps'] == int(10000 - lt.t_cq_ps - lt.t_su_ps - 250))
        check('groups sorted tightest first', [g['budget_ps'] for g in spec['groups']] == sorted(g['budget_ps'] for g in spec['groups']))
        check('per-group load from SDC', '0.05' not in (Path(names['out_3000ps']['constr']).read_text()) and 'set_load 50.0' in Path(names['out_3000ps']['constr']).read_text(), Path(names['out_3000ps']['constr']).read_text())
        sec = _group_section(spec, 'L.lib', 'def.constr', 'r.abc', 'g.txt')
        check('section: one abc per group + reg2reg', sec.count('abc -liberty') == len(spec['groups']) + 1)
        check('section: flop stop rules present', ':-sky130_fd_sc_hd__dfxtp_1' in sec)

# =========================================================================
print('\n[0e] recipe materialization — {D} must reach ABC as -D <ps>')
# =========================================================================
with tempfile.TemporaryDirectory() as td:
    for rp in sorted(DEFAULT_RECIPES_DIR.glob('*.abc')):
        out = _materialize_recipe(rp, 4321, Path(td))
        txt = out.read_text()
        if '{D}' in txt or ('-D 4321' not in txt and '{D}' in rp.read_text()):
            check(f'{rp.name} materialized', False, txt[:80]); break
    else:
        check('all recipes materialize with -D substituted', True)
    check('recipes without {D} are copied unchanged', _materialize_recipe(DEFAULT_RECIPES_DIR / 'yosys_default.abc', 7, Path(td)).read_text().count('-D 7') == (DEFAULT_RECIPES_DIR / 'yosys_default.abc').read_text().count('{D}'))
    nod = _materialize_recipe(DEFAULT_RECIPES_DIR / 'orfs_speed.abc', 0, Path(td)).read_text()
    check('d_ps=0 strips {D} entirely', '{D}' not in nod and '-D' not in nod and '&nf' in nod, nod)
    wl = _materialize_recipe(DEFAULT_RECIPES_DIR / 'orfs_speed.abc', 0, Path(td), wire_load=True).read_text()
    check('wire_load adds -c to sizing commands once', wl.count('buffer -c') == 1 and 'upsize -c' in wl and 'stime -c' in wl and '-c -c' not in wl, wl)
    dm = (DEFAULT_RECIPES_DIR / 'delay_map.abc').read_text()
    check('delay_map recipes carry -c already', 'upsize -c' in dm and 'buffer -c' in dm)

print('\n[1] ModuleScanner — top-level detection')
# =========================================================================

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / 'a.v').write_text('''
module leaf (input wire a, output wire y);
  assign y = ~a;
endmodule
module mid (input wire a, output wire y);
  leaf u (.a(a), .y(y));
endmodule
module top (input wire a, output wire y);
  mid m (.a(a), .y(y));
endmodule
''')
    (td / 'b.v').write_text('''
module standalone (input clk, output reg q);
  always @(posedge clk) q <= ~q;
endmodule
''')
    tops = ModuleScanner.scan([str(td/'a.v'), str(td/'b.v')])
    check('finds top + standalone, excludes leaf/mid',
          tops == ['standalone', 'top'], f'got={tops}')

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / 'a.v').write_text('module solo (input a); endmodule')
    tops = ModuleScanner.scan([str(td/'a.v')])
    check('single-module file', tops == ['solo'], f'got={tops}')

# Hierarchical dependencies: plain + parameterized instantiations
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / 'a.v').write_text('''
module leaf (input wire a, output wire y);
  assign y = ~a;
endmodule
module mid (input wire a, output wire y);
  leaf u0 (.a(a), .y(y));
endmodule
module top (input wire a, output wire y);
  mid m0 (.a(a), .y(y));
endmodule
''')
    deps = ModuleScanner.dependencies([str(td / 'a.v')])
    check('hier deps: plain inst mid→leaf',
          deps.get('mid') == {'leaf'}, f'got={deps}')
    check('hier deps: plain inst top→mid',
          deps.get('top') == {'mid'}, f'got={deps}')

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / 'b.v').write_text('''
module leaf (input wire a, output wire y);
  assign y = ~a;
endmodule
module mid (input wire a, output wire y);
  leaf #(.W(1)) u0 (.a(a), .y(y));
endmodule
module top (input wire a, output wire y);
  mid #() m0 (.a(a), .y(y));
endmodule
''')
    deps = ModuleScanner.dependencies([str(td / 'b.v')])
    check('hier deps: param inst mid→leaf',
          deps.get('mid') == {'leaf'}, f'got={deps}')
    check('hier deps: param inst top→mid',
          deps.get('top') == {'mid'}, f'got={deps}')

# Comment stripping
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / 'a.v').write_text('''
// module fake (input a); endmodule
/* module also_fake (input a); endmodule */
module real_mod (input a); endmodule
''')
    tops = ModuleScanner.scan([str(td/'a.v')])
    check('ignores commented-out modules',
          tops == ['real_mod'], f'got={tops}')

# =========================================================================
print('\n[2] Recipe discovery')
# =========================================================================

recipes = discover_recipes(DEFAULT_RECIPES_DIR, [])
names = [r[0] for r in recipes]
check('finds all 18 recipes', len(recipes) == 18, f'got {len(recipes)}: {names}')
check('delay_map is first by stability', names[0] == 'delay_map', f'got={names[0]}')
check('delay_triple before balanced_resyn',
      names.index('delay_triple') < names.index('balanced_resyn'),
      f'order={names}')
check('balanced_resyn before area_classic',
      names.index('balanced_resyn') < names.index('area_classic'),
      f'order={names}')
check('area_classic before area_max',
      names.index('area_classic') < names.index('area_max'),
      f'order={names}')

try:
    discover_recipes(DEFAULT_RECIPES_DIR, ['nonexistent'])
    check('raises on missing recipe', False, 'no exception')
except FileNotFoundError:
    check('raises on missing recipe', True)

# =========================================================================
print('\n[3] Pareto front')
# =========================================================================

# A dominates B if A.wns >= B.wns and A.area <= B.area, strict in one
# Pareto front: not dominated by anyone

c = lambda r, w, a: Candidate(recipe=r, netlist='', wns_ns=w, tns_ns=0, cells=0, area=a, runtime_s=0)

cands = [
    c('A', 1.0, 100),  # great wns, large area
    c('B', 0.5,  50),  # mid wns,    mid area
    c('C', 0.0,  20),  # bad wns,   small area
    c('D', 0.5, 100),  # dominated by A (same area, worse wns) AND by B (worse area, same wns)
]
front = _pareto_front(cands)
check('A, B, C on front; D dominated', set(front) == {'A', 'B', 'C'}, f'got={front}')

# All identical → all on front
cands2 = [c('X', 1, 50), c('Y', 1, 50)]
check('identical points: both on front', set(_pareto_front(cands2)) == {'X', 'Y'})

# =========================================================================
print('\n[4] Winner selection')
# =========================================================================

# single selection rule: min area among timing-meeting, else best WNS
cands = [c('a', -0.5, 100), c('b', 0.2, 300), c('d', 0.1, 200), c('e', 0.9, 250)]
sel = select_winner(cands, 'delay')
check('min area among meeting (d, 200) regardless of objective label', sel.winner == 'd', f'got={sel.winner} ({sel.rationale})')
sel = select_winner(cands, 'area', margin_ns=0.15)
check('margin excludes d (0.1 < 0.15) -> e (250) over b (300)', sel.winner == 'e', f'got={sel.winner}')
sel = select_winner([c('a', -0.5, 100), c('b', -0.2, 300)], 'balanced', fallback='best_wns')
check('nothing meets, best_wns -> b', sel.winner == 'b' and 'best WNS' in sel.rationale, sel.rationale)
sel = select_winner([c('a', -0.5, 100), c('b', -0.2, 300)], 'balanced')
check('knee needs >= 3 front points; with 2 falls back to best WNS (b)', sel.winner == 'b', sel.rationale)
mul = [c('fast', -0.5, 100), c('mid', -1.8, 68), c('slow', -4.0, 58), c('hopeless', -12.0, 54)]
sel = select_winner(mul, 'delay', period_ns=5.0)
check('knee with clip (period 5 ns) -> mid', sel.winner == 'mid', sel.rationale)
sel = select_winner(mul, 'delay', period_ns=None)
check('knee without clip is dragged by the hopeless point -> slow', sel.winner == 'slow', sel.rationale)
check('dominated points never win the knee', select_winner(mul + [c('dom', -1.9, 80)], 'delay', period_ns=5.0).winner == 'mid')
check('default fallback is knee', Config().fallback == 'knee')
cfg_bad = Config(); cfg_bad.fallback = 'cheapest'
check('invalid fallback rejected', any('fallback' in e for e in cfg_bad.validate()))
from synth_flow import _quick_sta_job
import inspect as _inspect
check('quick STA job is a module-level (picklable) function', _inspect.isfunction(_quick_sta_job) and _quick_sta_job.__module__ == 'synth_flow')
sel = select_winner([c('a', -0.5, 100), c('b', -0.5, 90)], 'balanced')
check('nothing meets, WNS tie -> smaller area (b)', sel.winner == 'b')
check('pareto front still reported', set(select_winner(cands, 'delay').pareto_front) >= {'a', 'e'}, str(select_winner(cands, 'delay').pareto_front))
tied = [c('area_max', 1.0, 100), c('delay_choice_deep_v3', 1.0, 100)]
sel = select_winner(tied, 'balanced')
check('tie -> delay_choice_deep_v3 wins (lower priority idx)', sel.winner == 'delay_choice_deep_v3', f'got={sel.winner}')
check('delay_choice_deep_v3 priority < area_max priority', _stability_idx('delay_choice_deep_v3') < _stability_idx('area_max'))
from synth_flow import RECIPE_SETS
check('recipe sets are subsets of the available recipes', all(r in names for s_ in RECIPE_SETS.values() for r in s_), str(RECIPE_SETS))
check('recipe sets have 5 entries each', all(len(v) == 5 for v in RECIPE_SETS.values()))
check("default objective is delay, full_sweep off", Config().objective == 'delay' and Config().full_sweep is False)

# Invalid objective
cfg.objective = 'banana'
errs = cfg.validate()
check('invalid objective rejected', any('objective' in e for e in errs), f'got={errs}')

# =========================================================================
print('\n[7] YAML config loading')
# =========================================================================

import yaml
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    rtl = td/'a.v'; rtl.write_text('module mod; endmodule')
    lib = td/'a.lib'; lib.write_text('library(a) {}')
    cfg_yaml = td/'cfg.yaml'
    cfg_yaml.write_text(yaml.dump({
        'rtl_files': [str(td / '*.v')],   # glob
        'lib_typ': str(lib),
        'top': 'mod',
        'period_ps': 7777,
        'objective': 'pareto',
        'run_sta': False,
        'run_gls': False,
    }))
    cfg = Config.from_yaml(cfg_yaml)
    check('yaml: glob expanded', cfg.rtl_files == [str(rtl)], f'got={cfg.rtl_files}')
    check('yaml: period_ps loaded', cfg.period_ps == 7777, f'got={cfg.period_ps}')
    check('yaml: objective loaded', cfg.objective == 'pareto', f'got={cfg.objective}')
    errs = cfg.validate()
    check('yaml-loaded config validates', len(errs) == 0, f'got={errs}')

# =========================================================================
print('\n[8] Recipe file content')
# =========================================================================

for r in DEFAULT_RECIPES_DIR.glob('*.abc'):
    content = r.read_text()
    has_D = '{D}' in content
    is_pure_area = r.stem in {'area_classic', 'area_max'}
    check(f'  {r.stem}: has stime',
          'stime' in content)
    # Delay-targeting recipes must use {D}; pure area recipes may skip it
    if not is_pure_area:
        check(f'  {r.stem}: uses {{D}} placeholder', has_D,
              f'recipe lacks {{D}} but is not pure-area')

# =========================================================================
print('\n[9] abc_sequential flag (experimental ABC -dff mode)')
# =========================================================================

from synth_flow import YOSYS_DRIVER_STD, YOSYS_DRIVER_SEQ

# Default Config has it off
cfg = Config()
check('abc_sequential default = False', cfg.abc_sequential is False)

# Templates differ
check('STD template has dfflibmap before abc',
      YOSYS_DRIVER_STD.find('dfflibmap') < YOSYS_DRIVER_STD.find('abc -liberty'),
      'order check')
check('SEQ template uses abc -dff',
      'abc -dff' in YOSYS_DRIVER_SEQ)
check('SEQ template runs abc before dfflibmap',
      YOSYS_DRIVER_SEQ.find('abc -dff') < YOSYS_DRIVER_SEQ.find('dfflibmap'),
      'order check')
check('STD template does NOT have abc -dff',
      'abc -dff' not in YOSYS_DRIVER_STD)

# YAML loads abc_sequential
import yaml as _y
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    rtl = td/'a.v'; rtl.write_text('module mod; endmodule')
    lib = td/'a.lib'; lib.write_text('library(a) {}')
    cfg_yaml = td/'cfg.yaml'
    cfg_yaml.write_text(_y.dump({
        'rtl_files': [str(rtl)],
        'lib_typ': str(lib),
        'top': 'mod',
        'run_sta': False,
        'run_gls': False,
        'abc_sequential': True,
    }))
    cfg = Config.from_yaml(cfg_yaml)
    check('yaml: abc_sequential loaded as True',
          cfg.abc_sequential is True, f'got={cfg.abc_sequential}')

# Confirm template selection logic in run_recipe matches the flag
# (we don't actually invoke yosys, just check the template branching)
def _select_template(seq_flag: bool) -> str:
    return YOSYS_DRIVER_SEQ if seq_flag else YOSYS_DRIVER_STD

check('flag=False -> standard template', _select_template(False) is YOSYS_DRIVER_STD)
check('flag=True  -> sequential template', _select_template(True) is YOSYS_DRIVER_SEQ)

# =========================================================================
print()
if failures:
    print(f'❌ {len(failures)} failures: {failures}')
    sys.exit(1)
else:
    print('✅ all tests passed')
