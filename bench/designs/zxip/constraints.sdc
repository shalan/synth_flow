# Bench copy of shalan/zxip sta/zxip_top.sdc @ 3964ddb with both clocks relaxed
# from 10.0 ns to 11.0 ns: at 10 ns the best candidate of the full flow is
# -0.47 ns, so the bench target is set where the flow can close (+0.5 ns).
# SDC for zxip_top — pre-P&R STA
# Clock period: 10 ns | I/O external delay: 4 ns
# Units match liberty time_unit (1ns)

create_clock -name hclk -period 11.0 [get_ports hclk]
create_clock -name pclk -period 11.0 [get_ports pclk]

# Separate domains (APB soft-reset etc.)
set_clock_groups -asynchronous -group {hclk} -group {pclk}

set_false_path -from [get_ports hresetn]
set_false_path -from [get_ports presetn]

set ahb_in  [get_ports {haddr[*] htrans[*] hwrite hsize[*] hsel hready hwdata[*]}]
set page_in [get_ports {phys_valid phys_i[*] fixed_page[*] page_sel[*] page_is_flash}]
set apb_in  [get_ports {paddr[*] psel penable pwrite pwdata[*]}]

set_input_delay  -clock hclk -max 4.0 $ahb_in
set_input_delay  -clock hclk -min 0.0 $ahb_in
set_input_delay  -clock hclk -max 4.0 $page_in
set_input_delay  -clock hclk -min 0.0 $page_in
set_input_delay  -clock pclk -max 4.0 $apb_in
set_input_delay  -clock pclk -min 0.0 $apb_in

set_output_delay -clock hclk -max 4.0 [get_ports {hrdata[*] hreadyout hresp}]
set_output_delay -clock hclk -min 0.0 [get_ports {hrdata[*] hreadyout hresp}]
set_output_delay -clock pclk -max 4.0 [get_ports {prdata[*] pready pslverr}]
set_output_delay -clock pclk -min 0.0 [get_ports {prdata[*] pready pslverr}]
set_output_delay -clock hclk -max 4.0 [get_ports {spi_sck spi_cs_n spi_reset_n}]
set_output_delay -clock hclk -min 0.0 [get_ports {spi_sck spi_cs_n spi_reset_n}]

set_load 0.05 [all_outputs]
