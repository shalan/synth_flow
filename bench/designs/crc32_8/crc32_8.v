// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// crc32_8 — Ethernet CRC-32, 8 bits per cycle, bit-serial loop unrolled by synthesis.
module crc32_8 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        init,
    input  wire        valid,
    input  wire [7:0]  data,
    output wire [31:0] crc_out
);
    reg  [31:0] crc;
    reg  [31:0] c;
    integer i;
    always @(*) begin
        c = crc;
        for (i = 0; i < 8; i = i + 1) begin
            if (c[31] ^ data[i]) c = {c[30:0], 1'b0} ^ 32'h04C11DB7;
            else                 c = {c[30:0], 1'b0};
        end
    end
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)      crc <= 32'hFFFFFFFF;
        else if (init)   crc <= 32'hFFFFFFFF;
        else if (valid)  crc <= c;
    end
    assign crc_out = ~crc;
endmodule
