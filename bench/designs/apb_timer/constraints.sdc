# SPDX-License-Identifier: Apache-2.0
# synth_flow benchmark constraints — apb_timer
set T 5.0
create_clock -name PCLK -period $T [get_ports PCLK]
set_clock_uncertainty -setup 0.25 [get_clocks PCLK]
set_clock_uncertainty -hold  0.10 [get_clocks PCLK]
set_false_path -from [get_ports PRESETn]
set_input_delay  -clock PCLK -max [expr 0.25 * $T] [all_inputs -no_clocks]
set_input_delay  -clock PCLK -min 0.0 [all_inputs -no_clocks]
set_output_delay -clock PCLK -max [expr 0.25 * $T] [all_outputs]
set_output_delay -clock PCLK -min 0.0 [all_outputs]
set_driving_cell -lib_cell sky130_fd_sc_hd__inv_1 [all_inputs -no_clocks]
set_load 0.033 [all_outputs]
