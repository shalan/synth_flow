// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// fifo_sync — 16-deep x 32-bit synchronous FIFO built from flops.
module fifo_sync #(parameter W = 32, parameter D = 16, parameter AW = 4) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          wr_en,
    input  wire [W-1:0]  wr_data,
    input  wire          rd_en,
    output reg  [W-1:0]  rd_data,
    output wire          full,
    output wire          empty,
    output reg  [AW:0]   count
);
    reg [W-1:0] mem [0:D-1];
    reg [AW-1:0] wp, rp;
    assign full  = (count == D);
    assign empty = (count == 0);
    wire do_wr = wr_en && !full;
    wire do_rd = rd_en && !empty;
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wp <= 0; rp <= 0; count <= 0; rd_data <= 0;
            for (i = 0; i < D; i = i + 1) mem[i] <= 0;
        end else begin
            if (do_wr) begin mem[wp] <= wr_data; wp <= wp + 1'b1; end
            if (do_rd) begin rd_data <= mem[rp]; rp <= rp + 1'b1; end
            case ({do_wr, do_rd})
                2'b10: count <= count + 1'b1;
                2'b01: count <= count - 1'b1;
                default: ;
            endcase
        end
    end
endmodule
