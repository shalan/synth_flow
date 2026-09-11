// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// fir8 — 8-tap direct-form FIR, 12-bit samples, constant 16-bit coefficients.
module fir8 (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               in_valid,
    input  wire signed [11:0] x,
    output reg  signed [31:0] y,
    output reg                out_valid
);
    reg signed [11:0] d [0:7];
    integer k;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (k = 0; k < 8; k = k + 1) d[k] <= 12'sd0;
        end else if (in_valid) begin
            d[0] <= x;
            for (k = 1; k < 8; k = k + 1) d[k] <= d[k-1];
        end
    end
    // Symmetric low-pass coefficients (Q15).
    wire signed [15:0] c0 = -16'sd312;
    wire signed [15:0] c1 = 16'sd  1024;
    wire signed [15:0] c2 = 16'sd  5120;
    wire signed [15:0] c3 = 16'sd 10240;
    wire signed [31:0] m0 = d[0] * c0;
    wire signed [31:0] m1 = d[1] * c1;
    wire signed [31:0] m2 = d[2] * c2;
    wire signed [31:0] m3 = d[3] * c3;
    wire signed [31:0] m4 = d[4] * c3;
    wire signed [31:0] m5 = d[5] * c2;
    wire signed [31:0] m6 = d[6] * c1;
    wire signed [31:0] m7 = d[7] * c0;
    wire signed [31:0] s  = ((m0 + m1) + (m2 + m3)) + ((m4 + m5) + (m6 + m7));
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin y <= 32'sd0; out_valid <= 1'b0; end
        else begin out_valid <= in_valid; if (in_valid) y <= s; end
    end
endmodule
