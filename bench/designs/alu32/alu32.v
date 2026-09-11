// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// alu32 — 32-bit ALU with registered result. Datapath + shifter + compare.
module alu32 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire [3:0]  op,
    input  wire        valid,
    output reg  [31:0] y,
    output reg         zero,
    output reg         ovf,
    output reg         ready
);
    reg  [31:0] r;
    reg         v;
    wire [32:0] sum  = {1'b0, a} + {1'b0, b};
    wire [32:0] diff = {1'b0, a} - {1'b0, b};
    wire [4:0]  sh   = b[4:0];

    integer i;
    reg [5:0] pop;
    reg [5:0] clz;
    reg       found;
    always @(*) begin
        pop = 0;
        for (i = 0; i < 32; i = i + 1) pop = pop + a[i];
        clz = 0; found = 0;
        for (i = 31; i >= 0; i = i - 1) begin
            if (!found) begin
                if (a[i]) found = 1;
                else      clz = clz + 1;
            end
        end
    end

    always @(*) begin
        v = 1'b0;
        case (op)
            4'd0:  begin r = sum[31:0];  v = (a[31] == b[31]) && (sum[31] != a[31]); end
            4'd1:  begin r = diff[31:0]; v = (a[31] != b[31]) && (diff[31] != a[31]); end
            4'd2:  r = a & b;
            4'd3:  r = a | b;
            4'd4:  r = a ^ b;
            4'd5:  r = a << sh;
            4'd6:  r = a >> sh;
            4'd7:  r = $signed(a) >>> sh;
            4'd8:  r = {31'b0, $signed(a) < $signed(b)};
            4'd9:  r = {31'b0, a < b};
            4'd10: r = {31'b0, a == b};
            4'd11: r = (a << sh) | (a >> (6'd32 - sh));
            4'd12: r = (a >> sh) | (a << (6'd32 - sh));
            4'd13: r = ~(a | b);
            4'd14: r = {26'b0, pop};
            default: r = {26'b0, clz};
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            y <= 32'b0; zero <= 1'b0; ovf <= 1'b0; ready <= 1'b0;
        end else begin
            ready <= valid;
            if (valid) begin
                y    <= r;
                zero <= (r == 32'b0);
                ovf  <= v;
            end
        end
    end
endmodule
