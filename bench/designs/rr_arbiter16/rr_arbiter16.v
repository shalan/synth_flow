// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// rr_arbiter16 — 16-way round-robin arbiter with registered one-hot grant.
module rr_arbiter16 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] req,
    input  wire        ack,
    output reg  [15:0] grant,
    output reg  [3:0]  grant_id,
    output reg         grant_valid
);
    reg [3:0] ptr;
    // Rotate requests so that ptr is bit 0, priority-encode, rotate back.
    wire [31:0] dbl  = {req, req};
    wire [15:0] rot  = dbl[ptr +: 16];
    reg  [15:0] pri;
    reg  [3:0]  idx;
    integer i;
    always @(*) begin
        pri = 16'b0; idx = 4'd0;
        for (i = 15; i >= 0; i = i - 1)
            if (rot[i]) begin pri = 16'b1 << i; idx = i[3:0]; end
    end
    wire [31:0] undbl = {pri, pri} << ptr;
    wire [15:0] g     = undbl[31:16];
    wire [3:0]  gid   = idx + ptr;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            grant <= 16'b0; grant_id <= 4'd0; grant_valid <= 1'b0; ptr <= 4'd0;
        end else begin
            if (!grant_valid || ack) begin
                grant       <= g;
                grant_id    <= gid;
                grant_valid <= |req;
                if (|req) ptr <= gid + 4'd1;
            end
        end
    end
endmodule
