// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// uart — 8N1 transmitter + receiver with 16x oversampling, programmable divisor.
module uart (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] divisor,      // clk cycles per 1/16 bit
    // TX
    input  wire        tx_start,
    input  wire [7:0]  tx_data,
    output reg         tx,
    output reg         tx_busy,
    // RX
    input  wire        rx,
    output reg  [7:0]  rx_data,
    output reg         rx_valid,
    output reg         rx_frame_err
);
    // ---------------- baud tick (16x) ----------------
    reg [15:0] bcnt;
    reg        tick16;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin bcnt <= 16'd0; tick16 <= 1'b0; end
        else if (bcnt >= divisor) begin bcnt <= 16'd0; tick16 <= 1'b1; end
        else begin bcnt <= bcnt + 1'b1; tick16 <= 1'b0; end
    end

    // ---------------- transmitter ----------------
    localparam T_IDLE = 2'd0, T_START = 2'd1, T_DATA = 2'd2, T_STOP = 2'd3;
    reg [1:0] tstate;
    reg [3:0] tsub;
    reg [2:0] tbit;
    reg [7:0] tsh;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            tstate <= T_IDLE; tx <= 1'b1; tx_busy <= 1'b0; tsub <= 4'd0; tbit <= 3'd0; tsh <= 8'd0;
        end else begin
            case (tstate)
                T_IDLE: begin
                    tx <= 1'b1;
                    if (tx_start) begin tsh <= tx_data; tstate <= T_START; tsub <= 4'd0; tx_busy <= 1'b1; end
                    else tx_busy <= 1'b0;
                end
                T_START: begin
                    tx <= 1'b0;
                    if (tick16) begin
                        if (tsub == 4'd15) begin tsub <= 4'd0; tstate <= T_DATA; tbit <= 3'd0; end
                        else tsub <= tsub + 1'b1;
                    end
                end
                T_DATA: begin
                    tx <= tsh[0];
                    if (tick16) begin
                        if (tsub == 4'd15) begin
                            tsub <= 4'd0; tsh <= {1'b0, tsh[7:1]};
                            if (tbit == 3'd7) tstate <= T_STOP; else tbit <= tbit + 1'b1;
                        end else tsub <= tsub + 1'b1;
                    end
                end
                T_STOP: begin
                    tx <= 1'b1;
                    if (tick16) begin
                        if (tsub == 4'd15) begin tsub <= 4'd0; tstate <= T_IDLE; end
                        else tsub <= tsub + 1'b1;
                    end
                end
            endcase
        end
    end

    // ---------------- receiver ----------------
    localparam R_IDLE = 2'd0, R_START = 2'd1, R_DATA = 2'd2, R_STOP = 2'd3;
    reg [1:0] rstate;
    reg [3:0] rsub;
    reg [2:0] rbit;
    reg [7:0] rsh;
    reg       rx_s1, rx_s2;      // 2-flop synchronizer
    reg [2:0] rx_maj;            // majority filter taps
    wire      rx_f = (rx_maj[0] & rx_maj[1]) | (rx_maj[1] & rx_maj[2]) | (rx_maj[0] & rx_maj[2]);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rx_s1 <= 1'b1; rx_s2 <= 1'b1; rx_maj <= 3'b111;
            rstate <= R_IDLE; rsub <= 4'd0; rbit <= 3'd0; rsh <= 8'd0;
            rx_data <= 8'd0; rx_valid <= 1'b0; rx_frame_err <= 1'b0;
        end else begin
            rx_s1 <= rx; rx_s2 <= rx_s1;
            if (tick16) rx_maj <= {rx_maj[1:0], rx_s2};
            rx_valid <= 1'b0;
            case (rstate)
                R_IDLE: if (tick16 && !rx_f) begin rstate <= R_START; rsub <= 4'd0; end
                R_START: if (tick16) begin
                    if (rsub == 4'd7) begin
                        if (rx_f) rstate <= R_IDLE;      // glitch
                        else begin rstate <= R_DATA; rsub <= 4'd0; rbit <= 3'd0; end
                    end else rsub <= rsub + 1'b1;
                end
                R_DATA: if (tick16) begin
                    if (rsub == 4'd15) begin
                        rsub <= 4'd0; rsh <= {rx_f, rsh[7:1]};
                        if (rbit == 3'd7) rstate <= R_STOP; else rbit <= rbit + 1'b1;
                    end else rsub <= rsub + 1'b1;
                end
                R_STOP: if (tick16) begin
                    if (rsub == 4'd15) begin
                        rstate <= R_IDLE; rx_data <= rsh; rx_valid <= 1'b1; rx_frame_err <= !rx_f;
                    end else rsub <= rsub + 1'b1;
                end
            endcase
        end
    end
endmodule
