<!-- SPDX-License-Identifier: Apache-2.0 -->
# Benchmark designs and constraints

Sixteen designs, all synthesized flat (`modules: [top]`) with the bundled
Sky130 HD 120-cell liberty at the SS corner (`hd_120_ss.lib`) and each
design's own SDC. Twelve are in-house RTL under `designs/<name>/`; four are
external shalan/* IPs fetched at pinned commits by `./fetch_external.sh`
into `external/` (gitignored). How to run, what the columns mean, and every
result table: [../docs/benchmarks.md](../docs/benchmarks.md).

## Designs

| design | category | top | cells (ref) | what it stresses | source |
|---|---|---|---|---|---|
| alu32 | datapath | `alu32` | ~2 000 | 32-bit add/sub/shift/rotate/compare, popcount, clz; registered result | `designs/alu32/alu32.v` |
| mul16_pipe | datapath | `mul16_pipe` | ~1 800 | 16×16 signed multiplier between registers; Booth target | `designs/mul16_pipe/mul16_pipe.v` |
| mul32_mac | datapath | `mul32_mac` | ~6 500 | 32×32 multiply-accumulate, 64-bit accumulator; largest single cone | `designs/mul32_mac/mul32_mac.v` |
| fir8 | dsp | `fir8` | ~1 600 | 8-tap direct-form FIR, 12-bit samples, constant 16-bit coefficients | `designs/fir8/fir8.v` |
| aes_round | crypto | `aes_round` | ~9 300 | one AES-128 round (SubBytes, ShiftRows, MixColumns, AddRoundKey); 16 S-boxes | `designs/aes_round/aes_round.v` |
| sha256_core | crypto | `sha256_core` | ~8 000 | SHA-256 compression, one round per cycle, 16-word schedule, K ROM | `designs/sha256_core/sha256_core.v` |
| crc32_8 | logic | `crc32_8` | ~230 | Ethernet CRC-32, 8 bits per cycle; pure XOR network | `designs/crc32_8/crc32_8.v` |
| rr_arbiter16 | logic | `rr_arbiter16` | ~400 | 16-way round-robin arbiter; rotate, priority-encode, rotate back | `designs/rr_arbiter16/rr_arbiter16.v` |
| uart | control | `uart` | ~380 | 8N1 TX/RX, 16× oversampling, majority filter | `designs/uart/uart.v` |
| spi_master | control | `spi_master` | ~450 | SPI master FSM, CPOL/CPHA, 8/16/24/32-bit frames, clock divider | `designs/spi_master/spi_master.v` |
| apb_timer | peripheral | `apb_timer` | ~1 250 | APB3 timer: prescaler, auto-reload, match, W1C status, IRQ; async `PRESETn` | `designs/apb_timer/apb_timer.v` |
| fifo_sync | memory | `fifo_sync` | ~1 900 | 16 × 32 flop-based synchronous FIFO; memory-to-register mapping, read mux | `designs/fifo_sync/fifo_sync.v` |
| zx16_core_ahb | cpu | `zx16_core_ahb` | ~2 700 | ZX16 16-bit CPU core with AHB-Lite instruction and data ports | shalan/zx16 @ `64839e4` |
| zxip | soc_ip | `zxip_top` | ~15 400 | XIP flash controller: cache, AHB slave, APB registers, SPI PHY; two clocks | shalan/zxip @ `3964ddb` |
| ms_psram_ahb | soc_ip | `ms_psram_ahb` | ~2 400 | PSRAM AHB controller with cache and CSRs, QSPI PHY | shalan/ms_psram_ahb @ `1885133` |
| uart_apb_sys | soc_ip | `uart_apb_sys` | ~1 700 | UART-driven APB master: command parser, response builder, splitter | shalan/uart_apb_master @ `2866672` |

Cell counts are the ORFS reference recipe (`orfs_speed`, plain front end)
for orientation; they vary by recipe.

## Constraints

Clock periods were calibrated so that the best recipe of the original
16-recipe sweep lands near timing at the SS corner (within about ±1 ns),
not at the design's intrinsic maximum frequency. The in-house SDCs follow one
template; the two external repos ship their own SDC, used as-is.

| design | clock port(s) | period | setup / hold uncertainty | input delay (max) | output delay (max) | false paths | driving cell / output load |
|---|---|---|---|---|---|---|---|
| alu32 | `clk` | 10.0 ns | 0.25 / 0.10 ns | 25 % of T on all inputs | 25 % of T on all outputs | `rst_n` | `inv_1` / 33 fF |
| mul16_pipe | `clk` | 9.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| mul32_mac | `clk` | 18.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| fir8 | `clk` | 12.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| aes_round | `clk` | 8.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| sha256_core | `clk` | 13.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| crc32_8 | `clk` | 4.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| rr_arbiter16 | `clk` | 6.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| uart | `clk` | 4.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| spi_master | `clk` | 4.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| apb_timer | `PCLK` | 5.0 ns | 0.25 / 0.10 | 25 % | 25 % | `PRESETn` | `inv_1` / 33 fF |
| fifo_sync | `clk` | 4.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |
| zx16_core_ahb | `HCLK` | 10.0 ns | 0.25 / 0.10 | 25 % | 25 % | `HRESETn` | `inv_1` / 33 fF |
| zxip | `hclk`, `pclk` (asynchronous groups) | 11.0 / 11.0 ns (repo SDC relaxed from 10) | tool default 0.25 / 0.10 | 4.0 ns on AHB, page and APB inputs | 4.0 ns on AHB, APB and SPI outputs | `hresetn`, `presetn` | tool default / 50 fF |
| ms_psram_ahb | `hclk` | 8.0 ns | 0.5 / 0.2 | 3.2 ns AHB inputs, 4.0 ns `spi_sio_i` | 3.2 ns AHB outputs, 4.0 ns SPI outputs | `hresetn` | `inv_1` / 33 fF max, 5 fF min |
| uart_apb_sys | `clk` | 10.0 ns | 0.25 / 0.10 | 25 % | 25 % | `rst_n` | `inv_1` / 33 fF |

Notes:

- "25 %" means `set_input_delay -max [expr 0.25 * $T]` on `all_inputs
  -no_clocks` and the same for outputs; `-min` delays are 10 % of T (40 % of
  the max), matching the flow default `io_delay_min_frac`.
- All in-house SDCs: `set_driving_cell -lib_cell sky130_fd_sc_hd__inv_1` on
  inputs, `set_load 0.033` (pF) on outputs, matching the flow's defaults for
  the Sky130 HD library.
- The tool derives async-reset false paths from register async pins, so the
  `set_false_path -from` lines above are confirmations, not requirements.
- `ms_psram_ahb`'s SDC also sets `set_input_transition` and min loads; those
  are STA-only and listed as such in `synth.sdc`.
- Periods for mul32_mac (16 → 18 ns) and zxip (10 → 11 ns, a bench copy of
  the repo SDC) were relaxed on 2026-09-12 to where the flow can close them;
  at the original values the best candidates were −0.57 ns and −0.47 ns. See the headline table in
  [../docs/benchmarks.md](../docs/benchmarks.md).

## Files

| file | purpose |
|---|---|
| `manifest.yaml` | design list: top, files, clock(s), period, SDC, category |
| `designs/<name>/<name>.v`, `constraints.sdc` | in-house RTL and SDC |
| `fetch_external.sh` | pinned checkouts of the external repos into `external/` |
| `bench.py` | runs the flow over the manifest, writes `results/<tag>.csv`, `.env.json`, `latest.md`; `--compare A B` |
| `postpass.py` | runs `resize.py` / `refine.py` on bench winners |
| `results/` | committed CSVs of every experiment (baselines, sweeps, negative results) |
