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

<details>
<summary>Building OpenSTA on macOS (Homebrew)</summary>

```bash
brew install bison cmake eigen flex swig tcl-tk@8 mht208/formal/cudd
git clone https://github.com/parallaxsw/OpenSTA.git && cd OpenSTA
export PATH="$(brew --prefix bison)/bin:$(brew --prefix flex)/bin:$PATH"
export CMAKE_INCLUDE_PATH="$(brew --prefix flex)/include"
export CMAKE_LIBRARY_PATH="$(brew --prefix flex)/lib;$(brew --prefix bison)/lib"
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DUSE_TCL_READLINE=OFF \
  -DCUDD_DIR="$(brew --prefix cudd)" \
  -DTCL_LIBRARY="$(brew --prefix tcl-tk@8)/lib/libtcl8.6.dylib" \
  -DTCL_INCLUDE_PATH="$(brew --prefix tcl-tk@8)/include/tcl-tk" \
  -DCMAKE_INSTALL_PREFIX="$HOME/.local/opt/opensta"
make -j"$(sysctl -n hw.ncpu)" && make install
ln -sf "$HOME/.local/opt/opensta/bin/sta" "$HOME/.local/bin/sta"
```

Verified with OpenSTA 3.1.0 (2026-09-11) on Apple Silicon, ~5 min build.
</details>

## Designs

| Name | Category | Clock (ns) | What it stresses |
|---|---|---|---|
| alu32 | datapath | 10 | 32-bit add/sub/shift/compare, popcount, clz; registered result |
| mul16_pipe | datapath | 9 | 16×16 signed multiplier between registers; Booth sweep target |
| mul32_mac | datapath | 16 | 32×32 MAC, 64-bit accumulator; largest single cone |
| fir8 | dsp | 12 | 8-tap FIR with constant coefficients |
| aes_round | crypto | 8 | One AES-128 round; 16 S-boxes + MixColumns; wide XOR |
| sha256_core | crypto | 13 | SHA-256 round engine; adder chains, rotates, K ROM |
| crc32_8 | logic | 4 | Parallel CRC-32, 8 bits/cycle; pure XOR network |
| rr_arbiter16 | logic | 6 | Round-robin arbiter; rotate / priority encode |
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

## Baselines (2026-09-12)

Two committed baselines, same 16 designs, Yosys 0.68 / ABC 1.01, slow-corner
synthesis liberty.

### A. `baseline-full.csv` — 21 recipes, area only (before OpenSTA was available)

- **Recipe choice matters for area.** Best-to-worst spread per design 9 %
  (spi_master) to 62 % (fir8) at the original, over-tight periods.
- **Best-of-sweep beats `orfs_speed` on every design**, mean −4.6 % area.
- **`balanced_struct` is byte-identical to `orfs_speed`** on all designs
  (`&scl`/`&lcorr` are no-ops on a combinational network).
- Seven recipes were never min-area or min-ABC-delay anywhere.

### B. `baseline-sta.csv` — 16 recipes, OpenSTA at SS, SDCs applied, recalibrated periods

Produced after the STA-consistency fixes (shared constraint preamble,
wire-load model, uncertainty), the `signed`-declaration fix, recipe
retirement and period recalibration. 256 rows, every row has a WNS.

| Metric | Value |
|---|---|
| Designs where ≥ 1 recipe meets timing | 7 / 16 |
| Mean WNS gain, best recipe vs `orfs_speed` | +0.42 ns |
| Largest WNS gains | ms_psram_ahb +1.25, zxip +1.08, alu32 +1.06 ns |
| Area spread best-to-worst recipe | 3 % to 15 % |
| Recipe with best mean WNS rank | `delay_choice_deep_v3` (4.2 / 16) |
| Recipe most often on the area/WNS Pareto front | `delay_aggressive` (11 / 16 designs) |
| Recipes that are best-WNS on ≥ 1 design | 7 different recipes |

Observations that drive Phases 2–4:

- **No recipe wins everywhere**, so a sweep is still needed, but the best
  recipe on a design is within ~0.2 ns of the next two on most designs. The
  remaining gap to timing (alu32 −0.73, mul32_mac −2.6, zxip −1.24 ns) is
  not a recipe-selection problem; it needs a different delay target per path
  group and endpoint-driven resynthesis.
- **I/O-constrained designs benefit most from real STA.** On zxip and
  ms_psram_ahb, whose SDCs carry 4 ns and 3.2 ns I/O delays, the ABC `stime`
  proxy anti-correlates with OpenSTA (Spearman −0.33 and −0.38 on the
  21-recipe run) while it correlates at +0.9 or better on register-bound
  designs. Never rank by the proxy when an SDC has I/O delays.
- **WNS spread across recipes is large on control logic**: apb_timer ranges
  from −0.07 to −4.94 ns at the same period, and uart from +0.20 to −1.55.
  The failing recipes there produce high-fanout nets that the wire-load model
  punishes; this is the sizing/buffering problem the Phase 6 sizing pass
  targets, and the cone classifier in Phase 4 must route those endpoints to
  it rather than to remapping.
- **Area follows WNS rank only loosely.** `area_max` / `area_lut6` are
  smallest on a few designs but sit at the bottom of the WNS ranking; the
  smallest netlist that still meets timing is chosen by 6 different recipes
  across the 7 designs that close. This is exactly the selection the
  per-group `-D` search (Phase 3) should make deterministic.

Reproduce: `./bench.py --use-sdc --tag <name>` with OpenSTA on `PATH`, then
`./bench.py --compare results/baseline-sta.csv results/<name>.csv`.

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
- Sequential ABC commands (`scorr`, `dretime`, `&scl`, `&lcorr`) are no-ops
  in the standard flow. Only `balanced_struct` collapses onto another recipe
  (`orfs_speed`); the rest differ through their remaining steps.
