// SPDX-License-Identifier: Apache-2.0
// synth_flow benchmark design

// sha256_core — SHA-256 compression engine: one round per cycle, 16-word message
// schedule shift register, K constants as a case ROM. Single 512-bit block per start.
module sha256_core (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         start,       // load block, begin 64 rounds
    input  wire         chain,       // 1: continue from previous digest, 0: IV
    input  wire [511:0] block,
    output reg          busy,
    output reg          done,
    output wire [255:0] digest
);
    function [31:0] rotr; input [31:0] x; input integer n; begin rotr = (x >> n) | (x << (32-n)); end endfunction
    function [31:0] Kc; input [5:0] t; begin case (t)
        6'd0: Kc = 32'h428a2f98;
        6'd1: Kc = 32'h71374491;
        6'd2: Kc = 32'hb5c0fbcf;
        6'd3: Kc = 32'he9b5dba5;
        6'd4: Kc = 32'h3956c25b;
        6'd5: Kc = 32'h59f111f1;
        6'd6: Kc = 32'h923f82a4;
        6'd7: Kc = 32'hab1c5ed5;
        6'd8: Kc = 32'hd807aa98;
        6'd9: Kc = 32'h12835b01;
        6'd10: Kc = 32'h243185be;
        6'd11: Kc = 32'h550c7dc3;
        6'd12: Kc = 32'h72be5d74;
        6'd13: Kc = 32'h80deb1fe;
        6'd14: Kc = 32'h9bdc06a7;
        6'd15: Kc = 32'hc19bf174;
        6'd16: Kc = 32'he49b69c1;
        6'd17: Kc = 32'hefbe4786;
        6'd18: Kc = 32'h0fc19dc6;
        6'd19: Kc = 32'h240ca1cc;
        6'd20: Kc = 32'h2de92c6f;
        6'd21: Kc = 32'h4a7484aa;
        6'd22: Kc = 32'h5cb0a9dc;
        6'd23: Kc = 32'h76f988da;
        6'd24: Kc = 32'h983e5152;
        6'd25: Kc = 32'ha831c66d;
        6'd26: Kc = 32'hb00327c8;
        6'd27: Kc = 32'hbf597fc7;
        6'd28: Kc = 32'hc6e00bf3;
        6'd29: Kc = 32'hd5a79147;
        6'd30: Kc = 32'h06ca6351;
        6'd31: Kc = 32'h14292967;
        6'd32: Kc = 32'h27b70a85;
        6'd33: Kc = 32'h2e1b2138;
        6'd34: Kc = 32'h4d2c6dfc;
        6'd35: Kc = 32'h53380d13;
        6'd36: Kc = 32'h650a7354;
        6'd37: Kc = 32'h766a0abb;
        6'd38: Kc = 32'h81c2c92e;
        6'd39: Kc = 32'h92722c85;
        6'd40: Kc = 32'ha2bfe8a1;
        6'd41: Kc = 32'ha81a664b;
        6'd42: Kc = 32'hc24b8b70;
        6'd43: Kc = 32'hc76c51a3;
        6'd44: Kc = 32'hd192e819;
        6'd45: Kc = 32'hd6990624;
        6'd46: Kc = 32'hf40e3585;
        6'd47: Kc = 32'h106aa070;
        6'd48: Kc = 32'h19a4c116;
        6'd49: Kc = 32'h1e376c08;
        6'd50: Kc = 32'h2748774c;
        6'd51: Kc = 32'h34b0bcb5;
        6'd52: Kc = 32'h391c0cb3;
        6'd53: Kc = 32'h4ed8aa4a;
        6'd54: Kc = 32'h5b9cca4f;
        6'd55: Kc = 32'h682e6ff3;
        6'd56: Kc = 32'h748f82ee;
        6'd57: Kc = 32'h78a5636f;
        6'd58: Kc = 32'h84c87814;
        6'd59: Kc = 32'h8cc70208;
        6'd60: Kc = 32'h90befffa;
        6'd61: Kc = 32'ha4506ceb;
        6'd62: Kc = 32'hbef9a3f7;
        6'd63: Kc = 32'hc67178f2;
        default: Kc = 32'h0;
    endcase end endfunction
    reg [31:0] a, b, c, d, e, f, g, h;
    reg [31:0] H0, H1, H2, H3, H4, H5, H6, H7;
    reg [31:0] W [0:15];
    reg [5:0]  t;
    reg        run;
    wire [31:0] s0 = rotr(W[1], 7) ^ rotr(W[1], 18) ^ (W[1] >> 3);
    wire [31:0] s1 = rotr(W[14], 17) ^ rotr(W[14], 19) ^ (W[14] >> 10);
    wire [31:0] Wn = W[0] + s0 + W[9] + s1;   // W[t+16]
    wire [31:0] S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
    wire [31:0] ch = (e & f) ^ (~e & g);
    wire [31:0] T1 = h + S1 + ch + Kc(t) + W[0];
    wire [31:0] S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
    wire [31:0] mj = (a & b) ^ (a & c) ^ (b & c);
    wire [31:0] T2 = S0 + mj;
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            run <= 1'b0; busy <= 1'b0; done <= 1'b0; t <= 6'd0;
            a <= 0; b <= 0; c <= 0; d <= 0; e <= 0; f <= 0; g <= 0; h <= 0;
            H0 <= 32'h6a09e667; H1 <= 32'hbb67ae85; H2 <= 32'h3c6ef372; H3 <= 32'ha54ff53a; H4 <= 32'h510e527f; H5 <= 32'h9b05688c; H6 <= 32'h1f83d9ab; H7 <= 32'h5be0cd19;
            for (i = 0; i < 16; i = i + 1) W[i] <= 32'd0;
        end else begin
            done <= 1'b0;
            if (start && !run) begin
                for (i = 0; i < 16; i = i + 1) W[i] <= block[511-32*i -: 32];
                if (!chain) begin
                    H0 <= 32'h6a09e667; H1 <= 32'hbb67ae85; H2 <= 32'h3c6ef372; H3 <= 32'ha54ff53a; H4 <= 32'h510e527f; H5 <= 32'h9b05688c; H6 <= 32'h1f83d9ab; H7 <= 32'h5be0cd19;
                    a <= 32'h6a09e667; b <= 32'hbb67ae85; c <= 32'h3c6ef372; d <= 32'ha54ff53a; e <= 32'h510e527f; f <= 32'h9b05688c; g <= 32'h1f83d9ab; h <= 32'h5be0cd19;
                end else begin
                    a <= H0; b <= H1; c <= H2; d <= H3; e <= H4; f <= H5; g <= H6; h <= H7;
                end
                t <= 6'd0; run <= 1'b1; busy <= 1'b1;
            end else if (run) begin
                h <= g; g <= f; f <= e; e <= d + T1;
                d <= c; c <= b; b <= a; a <= T1 + T2;
                for (i = 0; i < 15; i = i + 1) W[i] <= W[i+1];
                W[15] <= Wn;
                if (t == 6'd63) begin
                    run <= 1'b0; busy <= 1'b0; done <= 1'b1;
                    H0 <= H0 + (T1 + T2); H1 <= H1 + a; H2 <= H2 + b; H3 <= H3 + c;
                    H4 <= H4 + (d + T1);  H5 <= H5 + e; H6 <= H6 + f; H7 <= H7 + g;
                end else t <= t + 1'b1;
            end
        end
    end
    assign digest = {H0, H1, H2, H3, H4, H5, H6, H7};
endmodule
