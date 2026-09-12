<!-- SPDX-License-Identifier: Apache-2.0 -->
# Benchmarks

`bench/` is the regression and evaluation suite. Every QoR claim about a
recipe, a flow change or a library choice should be backed by a `bench.py`
run, and every before/after comparison by `bench.py --compare`.

## Headline result — `obj-*.csv`

The flow as it stands, per objective, against the ORFS/OpenLane reference
(plain Yosys front end, `orfs_speed`, no sizing), SS corner, per-design SDC.
Every run: 6 front-end variants per recipe, winner = the candidate that meets
timing with the least area (fallback best WNS), then OpenSTA-guided sizing.
Objectives differ only in the recipe subset (5 each) or all 18 (`--full-sweep`).

| design | ORFS ref WNS | `delay` WNS / Δarea | `balanced` WNS / Δarea | `area` WNS / Δarea | `--full-sweep` WNS / Δarea |
|---|---|---|---|---|---|
| alu32 | -1.947 | +0.470 / +6.5 % | +0.470 / +6.5 % | -1.190 / +3.9 % | +0.470 / +6.5 % |
| mul16_pipe | -0.435 | +0.920 / -3.2 % | +0.020 / -26.0 % | -1.820 / +3.4 % | +0.020 / -26.0 % |
| mul32_mac | -2.725 | -0.560 / +54.2 % | -0.560 / +54.2 % | -5.730 / +3.2 % | -0.560 / +54.2 % |
| fir8 | +0.323 | +0.210 / +0.3 % | +0.160 / -2.1 % | -0.330 / +6.6 % | +0.160 / -2.1 % |
| aes_round | +1.265 | +1.360 / +2.5 % | +1.310 / -6.0 % | +0.960 / -11.2 % | +0.960 / -11.2 % |
| sha256_core | -0.214 | -0.000 / +3.6 % | +0.060 / -2.2 % | -2.240 / -3.2 % | +0.060 / -2.2 % |
| crc32_8 | -0.131 | +0.180 / +12.8 % | +0.270 / +23.5 % | -0.240 / +0.5 % | +0.180 / +12.8 % |
| rr_arbiter16 | -0.723 | +0.010 / +26.7 % | -0.010 / +25.1 % | -0.480 / -0.8 % | +0.010 / +26.7 % |
| uart | -0.101 | +0.010 / -1.7 % | +0.070 / -2.7 % | -0.950 / -8.5 % | +0.070 / -2.7 % |
| spi_master | -0.378 | +0.120 / -0.7 % | +0.290 / +11.1 % | +0.010 / -3.4 % | +0.010 / -3.4 % |
| apb_timer | -0.079 | +0.110 / +0.5 % | +0.040 / +1.2 % | -2.270 / -3.1 % | +0.110 / +0.5 % |
| fifo_sync | -1.302 | +0.030 / +3.2 % | +0.030 / +3.2 % | -0.710 / +11.5 % | +0.030 / +3.2 % |
| zx16_core_ahb | -0.724 | +0.110 / +1.8 % | +0.110 / +1.8 % | -0.770 / -1.8 % | +0.110 / +1.8 % |
| zxip | -2.322 | -0.470 / +0.5 % | -0.670 / -0.1 % | -0.840 / -0.9 % | -0.470 / +0.5 % |
| ms_psram_ahb | -1.134 | +0.470 / -8.1 % | +0.100 / -6.5 % | +0.500 / -6.8 % | +0.100 / -6.5 % |
| uart_apb_sys | +3.968 | +3.450 / -2.8 % | +3.530 / -4.5 % | +0.090 / -6.6 % | +0.090 / -6.6 % |
| **meeting / mean ΔWNS / mean Δarea / time** | 3 / 16 | **14 / 16, +0.82 ns, +6.0 %, 38 s** | **13 / 16, +0.74 ns, +4.8 %, 115 s** | **4 / 16, -0.58 ns, -1.1 %, 68 s** | **14 / 16, +0.50 ns, +2.8 %, 295 s** |

Reading: the `delay` subset closes as many designs as the full sweep in an
eighth of the time and is the default. `obj-delay2.csv` is the same run after
parallel quick STA and the knee fallback: still 14/16, +0.73 ns, +2.7 % area,
95 s wall time for all 16 designs (mul32_mac now −1.84 ns at +2.0 % instead
of −0.56 ns at +54 %; zxip −0.67 ns at −0.1 %); the full sweep buys area back where
several candidates close; the `area` subset is for relaxed periods (only 4
designs close at these periods, area −1.1 %). The two designs no objective
closes are mul32_mac (−0.56 ns at 16 ns; the fallback picks the fastest
netlist, hence +54 % area) and zxip (−0.47 ns at 10 ns, the repo's own SDC).
`pipeline2*.csv` are the earlier runs with the previous per-objective
selection rules.

Reproduce: `./bench.py --use-sdc --objective delay --set 'yosys_opts_sweep=[[],[adder=kogge-stone],[adder=han-carlson],[adder=sklansky],[booth],[booth,adder=kogge-stone]]' --set resize_winner=true --tag <name>` (add `--set full_sweep=true` for all recipes).

## Quick start

```bash
cd bench
./fetch_external.sh              # once: pinned checkouts of the external IPs
./bench.py --quick --tag before  # 16 designs × 4 representative recipes
# ... change something ...
./bench.py --quick --tag after
./bench.py --compare results/before.csv results/after.csv
```

Full matrix (all recipes) is `./bench.py --tag <name>`. `--lib-dir DIR` runs
against the full `sky130_fd_sc_hd` liberty (tt/ss/ff files in DIR, e.g. a
ciel/volare `libs.ref/sky130_fd_sc_hd/lib`) instead of the bundled 120-cell
subset. Subsets:
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

### B. `baseline-noD.csv` — 16 recipes, OpenSTA at SS, SDCs applied, recalibrated periods (no ABC -D; the default)

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
`./bench.py --compare results/baseline-noD.csv results/<name>.csv`.

### C. ABC delay-target sweep — `abc-period.csv`, `abc-reg2reg.csv`, `abc-loose.csv`

Same matrix with `--set abc_target=...`. Every target is worse than none on
WNS (mean −0.59 / −0.22 / −1.08 ns) for at most 3.9 % area; see
[architecture.md §2.6](architecture.md#26-d-never-reached-abc-and-that-was-the-best-setting).
`abc_target` therefore defaults to `none`.

## Front-end sweep — `fe-*.csv`

Same 16 × 16 matrix with one `yosys_opts` variant each
(`--set 'yosys_opts=[...]'`), compared with `baseline-noD.csv`:

| variant | mean Δ best-WNS | mean Δ area (best-WNS recipe) | designs meeting timing |
|---|---|---|---|
| plain (baseline) | — | — | 6 / 16 |
| `booth` | -0.110 ns | -2.18 % | 7 / 16 |
| `adder=kogge-stone` | +0.174 ns | +2.69 % | 8 / 16 |
| `adder=han-carlson` | +0.073 ns | +1.47 % | 7 / 16 |
| `adder=sklansky` | +0.075 ns | +0.69 % | 6 / 16 |
| `booth + kogge-stone` | +0.052 ns | +0.55 % | 8 / 16 |

Best over all variants and recipes per design (what `yosys_opts_sweep`
selects automatically):

| design | baseline best WNS | best over all variants | variant / recipe |
|---|---|---|---|
| alu32 | -0.922 | -0.331 | booth + kogge-stone / delay_choice_deep_v4 |
| mul16_pipe | -0.411 | +0.272 | booth + kogge-stone / balanced_resyn2x |
| mul32_mac | -2.584 | -1.784 | adder=kogge-stone / delay_iter_heavy |
| fir8 | +0.323 | +0.669 | booth + kogge-stone / balanced_resyn2x |
| aes_round | +1.422 | +1.422 | plain / balanced_resyn2x |
| sha256_core | -0.074 | +0.760 | booth + kogge-stone / balanced_resyn |
| crc32_8 | -0.030 | -0.030 | plain / delay_triple |
| rr_arbiter16 | -0.430 | -0.430 | plain / delay_choice_deep_v3 |
| uart | +0.054 | +0.312 | adder=sklansky / delay_choice_deep_v2 |
| spi_master | +0.203 | +0.203 | plain / delay_aggressive |
| apb_timer | -0.065 | +0.046 | booth + kogge-stone / orfs_speed |
| fifo_sync | -0.602 | -0.602 | plain / area_lut6 |
| zx16_core_ahb | -0.173 | -0.020 | adder=han-carlson / delay_choice_deep_v4 |
| zxip | -1.238 | -1.237 | adder=sklansky / delay_choice_deep_v3 |
| ms_psram_ahb | +0.117 | +0.117 | plain / balanced_resyn2x |
| uart_apb_sys | +3.971 | +3.971 | plain / delay_iter_heavy |

Mean best-WNS gain +0.24 ns; 9 of 16 designs close timing versus 6; no
design gets worse because the plain front end stays in the candidate set.
Effects are strongly design-specific: Booth is −26 % area / +0.45 ns on
mul16_pipe but −2.3 ns WNS on mul32_mac; Kogge-Stone buys 0.6 to 0.8 ns on
alu32, mul32_mac and sha256_core for 3 to 8 % area. Hence a sweep, not a
default. `sweep6.csv` is the flow's own run with all six variants
(`yosys_opts_sweep`, 96 candidates per design): winners meeting timing
6 → 9 of 16, mean best-WNS +0.236 ns, identical to the best-of analysis.
Runtime per design 21 s (crc32_8) to 986 s (zxip, 16k cells).

## New-recipe candidates — `newrecipes.csv`

Eight candidate recipes run with the plain front end and scored against the
16 existing recipes on every design (`--set recipes_dir=<dir>`):

| candidate | best-WNS designs | Pareto points | smallest timing-meeting | verdict |
|---|---|---|---|---|
| `delay_map` (`strash; dch -f; map; buffer; topo; upsize; dnsize`) | 12 / 16 | 12 | 6 | **added**; closes alu32, mul16_pipe, sha256_core, crc32_8, rr_arbiter16, zx16_core_ahb that nothing else closed, at +8 to +60 % area |
| `orfs_area` (`&syn2; &if -g; &synch2; &nf`) | 1 (aes_round) | 3 | 0 | added |
| `delay_syn2` | 0 | 3 | 1 (spi_master) | added |
| `delay_resyn3` (resub ladder) | 0 | 4 | 0 | not adopted |
| `area_amap` | 0 | 9 | 0 | not adopted (Pareto points are area-only, all fail timing) |
| `area_deepsyn` (`&deepsyn -T 4`) | 0 | 9 | 0 | not adopted; 7× runtime |
| `area_relax50` / `area_relax200` (`&nf -R`) | 0 | 0 / 5 | 0 | not adopted |

`delay_map` is the classic supergate mapper instead of `&nf`. It maps for
delay with duplication (alu32: 2453 cells vs 2055), which is exactly the
trade the area-flow mapper refuses; verified fully mapped and equivalent by
4000-cycle random simulation on four designs. `delay_map_resyn` (resyn2 then
`dch -f; map`) was added as its natural companion and became the most
frequent winner of the pipeline bench (8/16 under `pareto`, 4/16 under
`area`). Second-round retirements from the same data: `area_safe`,
`delay_choice_deep_v2` (never winners, fewest Pareto points).

## Library experiment — `fulllib.csv`

`./bench.py --use-sdc --lib-dir <sky130A/libs.ref/sky130_fd_sc_hd/lib> --set "dont_use=[lpflow_*, probe*, dly*, clkdly*, sdlclkp*]"`:
the full 428-cell `sky130_fd_sc_hd` liberty (ORFS-style exclusions) against
the bundled 120-cell subset, 16 recipes, plain front end.

| | bundled `hd_120` | full liberty |
|---|---|---|
| designs whose best recipe meets timing | 6 / 16 | 4 / 16 |
| mean Δ best-WNS | — | −0.70 ns (worse on 15 of 16 designs) |
| mean Δ area of the best-WNS recipe | — | +0.1 % |
| mean Δ min-area recipe | — | −0.5 % |

The same netlist times identically under both libraries, so the difference
is in what ABC chooses: with 3- and 4-input gates and high-stack complex
cells available it builds slower structures at the SS corner
(sha256_core −2.6 ns, rr_arbiter16 −1.7 ns, mul32_mac −1.0 ns) and recovers
almost no area. The curated subset stays the default; the full liberty
remains available through `--lib-dir` / `dont_use` for designs that need
cells the subset lacks.

## Post-passes on winners — `postpass.py`

`./postpass.py --passes resize [--designs ...]` runs `resize.py` (and/or
`refine.py --whole-only`) on every `bench/work/<design>/results/<top>/winner.v`
with the clock, period, driving cell and load taken from the derived
`synth.sdc`, and writes `results/postpass-<tag>.csv`. Current result
(`postpass-resize.csv`):

| design | WNS before | WNS after | TNS before | TNS after | Δarea |
|---|---|---|---|---|---|
| alu32 | -0.922 | -1.032 | -2.07 | -1.65 | +0.33 % |
| mul16_pipe | -0.411 | -0.272 | -0.67 | -0.28 | +0.38 % |
| mul32_mac | -2.584 | -2.371 | -2.58 | -2.37 | +0.21 % |
| fir8 | +0.323 | +0.323 | +0.00 | +0.00 | +0.00 % |
| aes_round | +1.422 | +1.422 | +0.00 | +0.00 | +0.00 % |
| sha256_core | -0.074 | +0.010 | -0.07 | +0.00 | +0.03 % |
| crc32_8 | -0.030 | -0.009 | -0.06 | -0.01 | +0.70 % |
| rr_arbiter16 | -0.430 | -0.384 | -4.20 | -3.88 | +0.69 % |
| uart | +0.054 | +0.054 | +0.00 | +0.00 | +0.00 % |
| spi_master | +0.203 | +0.203 | +0.00 | +0.00 | +0.00 % |
| apb_timer | -0.065 | -0.085 | -3.27 | -0.09 | +1.64 % |
| fifo_sync | -0.602 | -0.694 | -229.47 | -151.69 | +5.34 % |
| zx16_core_ahb | -0.173 | -0.141 | -2.06 | -0.54 | +0.74 % |
| zxip | -1.238 | -0.346 | -14.82 | -2.29 | +0.32 % |
| ms_psram_ahb | +0.117 | +0.117 | +0.00 | +0.00 | +0.00 % |
| uart_apb_sys | +3.971 | +3.971 | +0.00 | +0.00 | +0.00 % |

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
