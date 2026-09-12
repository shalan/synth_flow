# Retired recipes

Not loaded by `synth_flow` (`discover_recipes` only reads `recipes/*.abc`).
Kept for reference. Retired on 2026-09-12 from the 16-design STA baseline
(`bench/results/baseline-sta.csv`, Yosys 0.68 / ABC 1.01, SS corner):

| Recipe | Reason |
|---|---|
| `balanced_struct` | Byte-identical netlists to `orfs_speed` on all 16 designs (`&scl`/`&lcorr` are no-ops on a combinational network). |
| `lazy_man` | Never on the area/WNS Pareto front of any design; mean WNS rank 12/21. |
| `lms` | Never on the Pareto front; 10x the runtime of `orfs_speed`. |
| `delay_retime` | Pareto front on 2/16 designs only; `dretime` is a no-op in the combinational flow, so it duplicates `delay_triple` minus one pass. |
| `delay_choice_deep_combined` | Pareto front on 1/16 designs; dominated by `delay_choice_deep_v3`. |


Retired on 2026-09-12 (second round) from the 120-candidate pipeline bench
(`bench/results/pipeline2.csv`, `pipeline2-area.csv`): never the winner
under either objective, fewest Pareto points of the set.

| Recipe | Reason |
|---|---|
| `area_safe` | 4 Pareto points of ~250, no wins; dominated by `area_classic` (same script minus `ifraig`). |
| `delay_choice_deep_v2` | 6 Pareto points, no wins; one sizing round more than `delay_choice_deep`, which it never beats meaningfully. |

To re-enable one, move it back to `recipes/`.
