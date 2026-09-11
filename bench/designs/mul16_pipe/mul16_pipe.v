// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// mul16_pipe — 16x16 signed multiplier, registered inputs and output.
module mul16_pipe (
    input  wire               clk,
    input  wire               rst_n,
    input  wire signed [15:0] a,
    input  wire signed [15:0] b,
    input  wire               en,
    output reg  signed [31:0] p,
    output reg                p_valid
);
    reg signed [15:0] ra, rb;
    reg               rv;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ra <= 16'sd0; rb <= 16'sd0; rv <= 1'b0;
            p  <= 32'sd0; p_valid <= 1'b0;
        end else begin
            ra <= a; rb <= b; rv <= en;
            p_valid <= rv;
            if (rv) p <= ra * rb;
        end
    end
endmodule
