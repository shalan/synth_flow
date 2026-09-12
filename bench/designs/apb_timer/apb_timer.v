// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design
// apb_timer — APB3 slave: 32-bit down-counter with prescaler, IRQ, 6 registers.
// Register map (byte offsets): 0x00 CTRL, 0x04 LOAD, 0x08 VALUE (RO),
// 0x0C PRESCALE, 0x10 STATUS (W1C), 0x14 MATCH.
module apb_timer (
    input  wire        PCLK,
    input  wire        PRESETn,
    input  wire        PSEL,
    input  wire        PENABLE,
    input  wire        PWRITE,
    input  wire [7:0]  PADDR,
    input  wire [31:0] PWDATA,
    output reg  [31:0] PRDATA,
    output wire        PREADY,
    output wire        PSLVERR,
    output reg         IRQ
);
    reg [31:0] ctrl, load, value, prescale, match;
    reg [31:0] pcnt;
    reg        f_zero, f_match;
    wire en        = ctrl[0];
    wire auto_rld  = ctrl[1];
    wire ie_zero   = ctrl[2];
    wire ie_match  = ctrl[3];
    wire one_shot  = ctrl[4];

    wire setup  = PSEL && !PENABLE;
    wire access = PSEL &&  PENABLE;
    wire wr     = access && PWRITE;
    assign PREADY  = 1'b1;
    assign PSLVERR = access && (PADDR[7:5] != 3'b000);

    wire tick = (pcnt == prescale);
    always @(posedge PCLK or negedge PRESETn) begin
        if (!PRESETn) begin
            ctrl <= 32'd0; load <= 32'hFFFF_FFFF; value <= 32'hFFFF_FFFF;
            prescale <= 32'd0; match <= 32'd0; pcnt <= 32'd0;
            f_zero <= 1'b0; f_match <= 1'b0; IRQ <= 1'b0;
        end else begin
            // Register writes
            if (wr) begin
                case (PADDR[4:2])
                    3'd0: ctrl     <= PWDATA;
                    3'd1: begin load <= PWDATA; value <= PWDATA; pcnt <= 32'd0; end
                    3'd3: prescale <= PWDATA;
                    3'd4: begin
                        if (PWDATA[0]) f_zero  <= 1'b0;
                        if (PWDATA[1]) f_match <= 1'b0;
                    end
                    3'd5: match    <= PWDATA;
                    default: ;
                endcase
            end
            // Counter
            if (en) begin
                if (tick) begin
                    pcnt <= 32'd0;
                    if (value == 32'd0) begin
                        f_zero <= 1'b1;
                        if (auto_rld) value <= load;
                        if (one_shot) ctrl[0] <= 1'b0;
                    end else begin
                        value <= value - 1'b1;
                        if (value - 1'b1 == match) f_match <= 1'b1;
                    end
                end else pcnt <= pcnt + 1'b1;
            end
            IRQ <= (f_zero & ie_zero) | (f_match & ie_match);
        end
    end

    always @(*) begin
        case (PADDR[4:2])
            3'd0: PRDATA = ctrl;
            3'd1: PRDATA = load;
            3'd2: PRDATA = value;
            3'd3: PRDATA = prescale;
            3'd4: PRDATA = {30'd0, f_match, f_zero};
            3'd5: PRDATA = match;
            default: PRDATA = 32'd0;
        endcase
    end
endmodule
