<!-- SPDX-License-Identifier: Apache-2.0 -->
# Benchmarks

`bench/` is the regression and evaluation suite. Every QoR claim about a
recipe, a flow change or a library choice should be backed by a `bench.py`
run, and every before/after comparison by `bench.py --compare`.

## Quick start

```bash
cd bench
./fetch_external.sh              # once: pinned checkouts of the external IPs
./bench.py --quick --tag before  # 16 designs × 4 representative recipes
# ... change something ...
./bench.py --quick --tag after
./bench.py --compare results/before.csv results/after.csv
```

Full matrix (all recipes) is `./bench.py --tag <name>`. Subsets:
`--designs alu32 uart`, `--category datapath crypto`, `--recipes orfs_speed
area_classic`. Add `--use-sdc` to pass each design's SDC to OpenSTA.

Requirements: Yosys on `PATH` (or `YOSYS=`), PyYAML. OpenSTA (`sta` on
`PATH`, `OPENSTA=`, or `--sta-bin`) is optional; without it the WNS/TNS
columns are empty and ABC's `stime` delay is recorded as a proxy.

## Designs

| Name | Category | Clock (ns) | What it stresses |
|---|---|---|---|
| alu32 | datapath | 6 | 32-bit add/sub/shift/compare, popcount, clz; registered result |
| mul16_pipe | datapath | 6 | 16×16 signed multiplier between registers; Booth sweep target |
| mul32_mac | datapath | 10 | 32×32 MAC, 64-bit accumulator; largest single cone |
| fir8 | dsp | 8 | 8-tap FIR with constant coefficients |
| aes_round | crypto | 8 | One AES-128 round; 16 S-boxes + MixColumns; wide XOR |
| sha256_core | crypto | 8 | SHA-256 round engine; adder chains, rotates, K ROM |
| crc32_8 | logic | 4 | Parallel CRC-32, 8 bits/cycle; pure XOR network |
| rr_arbiter16 | logic | 4 | Round-robin arbiter; rotate / priority encode |
| uart | control | 4 | 8N1 TX/RX, 16× oversampling |
| spi_master | control | 4 | SPI master FSM, CPOL/CPHA, variable frame |
| apb_timer | peripheral | 5 | APB3 timer; async `PRESETn` false path |
| fifo_sync | memory | 4 | 16×32 flop FIFO; memory-to-register mapping |
| zx16_core_ahb | cpu | 10 | ZX16 16-bit CPU with AHB-Lite ports (external) |
| zxip | soc_ip | 10 / 10 | XIP flash controller with cache; dual clock; repo SDC (external) |
| ms_psram_ahb | soc_ip | 8 | PSRAM AHB controller with cache and CSRs; repo SDC (external) |
| uart_apb_sys | soc_ip | 10 | UART-driven APB master (external) |

In-house designs live in `bench/designs/<name>/` with `<name>.v` and
`constraints.sdc`. The AES S-box and the SHA-256 K/H constants were generated
programmatically (not transcribed) to avoid table errors. External designs are
fetched into `bench/external/` (gitignored) at the commits pinned in
`fetch_external.sh`:

| Repo | Commit |
|---|---|
| shalan/zxip | 3964ddb |
| shalan/zx16 | 64839e4 |
| shalan/ms_psram_ahb | 1885133 |
| shalan/uart_apb_master | 2866672 |

Every design is synthesized flat with `modules: [top]`, objective `pareto`,
bundled `sky130/hd_120_*.lib` (synthesis at the slow corner), GLS off.

## Metrics

`results/<tag>.csv` has one row per (design, recipe):

| Column | Meaning |
|---|---|
| `cells`, `area_um2` | From Yosys `stat -liberty` on the mapped netlist |
| `wns_ns`, `tns_ns` | OpenSTA quick STA at the slow corner (empty without OpenSTA) |
| `abc_delay_ps` | Last `stime` line in the synthesis log. ABC's own estimate: no wire load, synthesis liberty only, no SDC exceptions. Use for relative comparison only. |
| `runtime_s` | Yosys wall time for that recipe |
| `is_winner` | Recipe selected by the objective |
| `status`, `error` | `ok`, `recipe_failed`, `failed`, `missing_rtl` |

`results/<tag>.env.json` records the Yosys, ABC and OpenSTA versions, the
synth_flow git SHA (and whether the tree was dirty) and the date.
`results/latest.md` is a human-readable summary with a winner table and
per-recipe matrices for area, WNS, ABC delay and runtime.

## Comparing runs

`--compare A.csv B.csv` joins on (design, recipe) and prints per-row deltas
for area, WNS and ABC delay plus mean deltas. Conventions:

- Negative Δarea is better. Positive ΔWNS is better. Negative ΔabcD is better.
- Compare runs made with the same recipe set and the same tool versions
  (check the two `env.json` files). A change in Yosys or ABC version is a
  result in itself, not a flow improvement.
- For recipe pruning, look at which recipes are ever `is_winner` or on the
  Pareto front across designs, not at averages.

## Adding a design

1. Put the RTL in `bench/designs/<name>/<name>.v` (or add a `fetch` line for
   an external repo) and an SDC next to it.
2. Add an entry to `bench/manifest.yaml`: `name`, `category`, `top`, `files`
   (globs relative to `bench/`), `clock`, `period_ps`, optional `includes`,
   `defines`, `clock_2` / `period_ps_2`, `sdc`, `notes`.
3. Check it elaborates: `yosys -q -p "read_verilog <file>; hierarchy -top <top>; proc; check -assert"`.
4. Run `./bench.py --designs <name> --quick`.

Keep designs in the 300 to 20 000 cell range so the full matrix finishes in
minutes and differences are above noise.

## Caveats

- Results without OpenSTA rank by area and ABC's proxy delay only. The
  winner column then follows `select_winner`'s no-STA fallback (min area).
- `bench/work/` is wiped per design on every run unless `--keep-work`.
- Recipes that contain sequential ABC commands behave identically to their
  combinational counterparts in the standard flow; expect duplicate rows
  until Phase 0 pruning lands.
