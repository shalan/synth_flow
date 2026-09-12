// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// mul32_mac — 32x32 unsigned multiply-accumulate, 64-bit accumulator.
module mul32_mac (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        clr,
    input  wire        en,
    input  wire [31:0] a,
    input  wire [31:0] b,
    output reg  [63:0] acc,
    output wire        sat
);
    wire [63:0] prod = a * b;
    wire [64:0] nxt  = {1'b0, acc} + {1'b0, prod};
    assign sat = nxt[64];
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)      acc <= 64'd0;
        else if (clr)    acc <= 64'd0;
        else if (en)     acc <= nxt[63:0];
    end
endmodule
