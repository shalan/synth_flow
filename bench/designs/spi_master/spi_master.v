// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// spi_master — FSM-based SPI master, 8/16/24/32-bit frames, CPOL/CPHA, clock divider.
module spi_master (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    input  wire [1:0]  frame_len,   // 0:8 1:16 2:24 3:32 bits
    input  wire        cpol,
    input  wire        cpha,
    input  wire [7:0]  clk_div,     // half-period in clk cycles
    input  wire [31:0] tx_data,
    output reg  [31:0] rx_data,
    output reg         busy,
    output reg         done,
    output reg         sclk,
    output reg         mosi,
    input  wire        miso,
    output reg         cs_n
);
    localparam S_IDLE = 3'd0, S_LEAD = 3'd1, S_XFER = 3'd2, S_TRAIL = 3'd3, S_DONE = 3'd4;
    reg [2:0]  state;
    reg [7:0]  dcnt;
    reg [5:0]  bits_left;
    reg [31:0] sh;
    reg        phase;              // 0 = first edge, 1 = second edge
    wire [5:0] nbits = {frame_len, 3'b000} + 6'd8;
    wire       div_hit = (dcnt == clk_div);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE; dcnt <= 8'd0; bits_left <= 6'd0; sh <= 32'd0; phase <= 1'b0;
            rx_data <= 32'd0; busy <= 1'b0; done <= 1'b0; sclk <= 1'b0; mosi <= 1'b0; cs_n <= 1'b1;
        end else begin
            done <= 1'b0;
            case (state)
                S_IDLE: begin
                    sclk <= cpol; cs_n <= 1'b1; busy <= 1'b0;
                    if (start) begin
                        sh <= tx_data << (6'd32 - nbits);
                        bits_left <= nbits; busy <= 1'b1; cs_n <= 1'b0;
                        dcnt <= 8'd0; phase <= 1'b0; state <= S_LEAD;
                    end
                end
                S_LEAD: begin
                    mosi <= cpha ? mosi : sh[31];
                    if (div_hit) begin dcnt <= 8'd0; state <= S_XFER; end
                    else dcnt <= dcnt + 1'b1;
                end
                S_XFER: begin
                    if (div_hit) begin
                        dcnt <= 8'd0;
                        sclk <= ~sclk;
                        phase <= ~phase;
                        if (phase == cpha) begin
                            // sample edge
                            sh <= {sh[30:0], miso};
                        end else begin
                            // shift edge
                            mosi <= sh[31];
                            bits_left <= bits_left - 1'b1;
                        end
                        if (phase == 1'b1 && bits_left == 6'd1 && cpha == 1'b0) state <= S_TRAIL;
                        if (phase == 1'b1 && bits_left == 6'd0 && cpha == 1'b1) state <= S_TRAIL;
                    end else dcnt <= dcnt + 1'b1;
                end
                S_TRAIL: begin
                    sclk <= cpol;
                    if (div_hit) begin dcnt <= 8'd0; state <= S_DONE; end
                    else dcnt <= dcnt + 1'b1;
                end
                S_DONE: begin
                    cs_n <= 1'b1; rx_data <= sh; done <= 1'b1; busy <= 1'b0; state <= S_IDLE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule
