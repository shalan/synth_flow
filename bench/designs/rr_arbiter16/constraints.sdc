# SPDX-License-Identifier: Apache-2.0
# synth_flow benchmark constraints — rr_arbiter16
set T 6.0
create_clock -name clk -period $T [get_ports clk]
set_clock_uncertainty -setup 0.25 [get_clocks clk]
set_clock_uncertainty -hold  0.10 [get_clocks clk]
set_false_path -from [get_ports rst_n]
set_input_delay  -clock clk -max [expr 0.25 * $T] [all_inputs -no_clocks]
set_input_delay  -clock clk -min [expr 0.10 * $T] [all_inputs -no_clocks]
set_output_delay -clock clk -max [expr 0.25 * $T] [all_outputs]
set_output_delay -clock clk -min [expr 0.10 * $T] [all_outputs]
set_driving_cell -lib_cell sky130_fd_sc_hd__inv_1 [all_inputs -no_clocks]
set_load 0.033 [all_outputs]
