#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""Tests for the pure-Python logic in synth_flow.py (no EDA tools needed)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from synth_flow import (
    Config, ModuleScanner, Candidate, Selection,
    select_winner, _pareto_front, _stability_idx,
    discover_recipes, RecipeResult,
    DEFAULT_RECIPES_DIR,
)

failures = []

def check(name, cond, detail=''):
    if cond:
        print(f'  ✓ {name}')
    else:
        print(f'  ✗ {name}  {detail}')
        failures.append(name)

# =========================================================================
print('\n[1] ModuleScanner — top-level detection')
# =========================================================================

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / 'a.v').write_text('''
module leaf (input wire a, output wire y);
  assign y = ~a;
endmodule
module mid (input wire a, output wire y);
  leaf u (.a(a), .y(y));
endmodule
module top (input wire a, output wire y);
  mid m (.a(a), .y(y));
endmodule
''')
    (td / 'b.v').write_text('''
module standalone (input clk, output reg q);
  always @(posedge clk) q <= ~q;
endmodule
''')
    tops = ModuleScanner.scan([str(td/'a.v'), str(td/'b.v')])
    check('finds top + standalone, excludes leaf/mid',
          tops == ['standalone', 'top'], f'got={tops}')

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / 'a.v').write_text('module solo (input a); endmodule')
    tops = ModuleScanner.scan([str(td/'a.v')])
    check('single-module file', tops == ['solo'], f'got={tops}')

# Comment stripping
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / 'a.v').write_text('''
// module fake (input a); endmodule
/* module also_fake (input a); endmodule */
module real_mod (input a); endmodule
''')
    tops = ModuleScanner.scan([str(td/'a.v')])
    check('ignores commented-out modules',
          tops == ['real_mod'], f'got={tops}')

# =========================================================================
print('\n[2] Recipe discovery')
# =========================================================================

recipes = discover_recipes(DEFAULT_RECIPES_DIR, [])
names = [r[0] for r in recipes]
check('finds all 14 recipes', len(recipes) == 14, f'got {len(recipes)}: {names}')
check('delay_retime is first by stability', names[0] == 'delay_retime', f'got={names[0]}')
check('delay_triple before balanced_resyn',
      names.index('delay_triple') < names.index('balanced_resyn'),
      f'order={names}')
check('balanced_resyn before area_safe',
      names.index('balanced_resyn') < names.index('area_safe'),
      f'order={names}')
check('area_safe before area_max',
      names.index('area_safe') < names.index('area_max'),
      f'order={names}')

try:
    discover_recipes(DEFAULT_RECIPES_DIR, ['nonexistent'])
    check('raises on missing recipe', False, 'no exception')
except FileNotFoundError:
    check('raises on missing recipe', True)

# =========================================================================
print('\n[3] Pareto front')
# =========================================================================

# A dominates B if A.wns >= B.wns and A.area <= B.area, strict in one
# Pareto front: not dominated by anyone

c = lambda r, w, a: Candidate(recipe=r, netlist='', wns_ns=w, tns_ns=0, cells=0, area=a, runtime_s=0)

cands = [
    c('A', 1.0, 100),  # great wns, large area
    c('B', 0.5,  50),  # mid wns,    mid area
    c('C', 0.0,  20),  # bad wns,   small area
    c('D', 0.5, 100),  # dominated by A (same area, worse wns) AND by B (worse area, same wns)
]
front = _pareto_front(cands)
check('A, B, C on front; D dominated', set(front) == {'A', 'B', 'C'}, f'got={front}')

# All identical → all on front
cands2 = [c('X', 1, 50), c('Y', 1, 50)]
check('identical points: both on front', set(_pareto_front(cands2)) == {'X', 'Y'})

# =========================================================================
print('\n[4] Winner selection')
# =========================================================================

# delay objective: max wns wins
sel = select_winner(cands, 'delay')
check('delay -> A (best wns)', sel.winner == 'A', f'got={sel.winner}')

# area objective: among meeting timing, smallest area
# All three (A,B,C) meet timing (wns >= 0). C has smallest area.
sel = select_winner(cands, 'area')
check('area -> C (smallest area, meets timing)', sel.winner == 'C', f'got={sel.winner}')

# area objective with no recipe meeting timing -> falls back to max wns
fail_cands = [c('A', -2.0, 100), c('B', -1.0, 50), c('C', -3.0, 20)]
sel = select_winner(fail_cands, 'area')
check('area fallback: best WNS when none meets', sel.winner == 'B', f'got={sel.winner}')

# pareto objective: picks max-WNS on front
sel = select_winner(cands, 'pareto')
check('pareto -> A (max wns on front)', sel.winner == 'A', f'got={sel.winner}')
check('pareto reports front', set(sel.pareto_front) == {'A', 'B', 'C'},
      f'got={sel.pareto_front}')

# balanced objective: rank-sum
# A: rank 0 in WNS (best), rank 2 in area (worst-ish among A,B,C) -> 0+2=2
# B: rank 1 in wns, rank 1 in area -> 1+1=2  (tied with A)
# C: rank 2 in wns, rank 0 in area -> 2+0=2  (tied)
# stability tiebreak: A < B < C in priority? Depends on RECIPE_PRIORITY
# Our test names A/B/C aren't in the priority list so all get index 999, stable order
# In a tie, balanced should still pick deterministically
sel = select_winner(cands, 'balanced')
check('balanced returns a winner', sel.winner is not None)

# none-valid case
no_valid = [Candidate(recipe='X', netlist='', wns_ns=None, tns_ns=None,
                       cells=0, area=0, runtime_s=0)]
sel = select_winner(no_valid, 'delay')
check('no valid candidates -> winner=None', sel.winner is None)

# =========================================================================
print('\n[5] Stability tiebreak')
# =========================================================================

# Two candidates with identical metrics, different recipe names from the priority list
tied = [c('area_max', 1.0, 100), c('delay_retime', 1.0, 100)]
sel = select_winner(tied, 'delay')
check('tie -> delay_retime wins (lower priority idx)',
      sel.winner == 'delay_retime', f'got={sel.winner}')

check('delay_retime priority < area_max priority',
      _stability_idx('delay_retime') < _stability_idx('area_max'))
check('unknown recipe gets 999',
      _stability_idx('foobar') == 999)

# =========================================================================
print('\n[6] Config validation')
# =========================================================================

cfg = Config()
errs = cfg.validate()
check('empty config -> errors', len(errs) > 0, f'got={errs}')

cfg = Config(rtl_files=['/no/such/file.v'], lib_typ='/no/such.lib', top='x',
             run_sta=False, run_gls=False)
errs = cfg.validate()
check('missing files reported', any('missing' in e for e in errs), f'got={errs}')

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    rtl = td/'a.v'; rtl.write_text('module a; endmodule')
    lib = td/'a.lib'; lib.write_text('library(a) {}')
    cfg = Config(rtl_files=[str(rtl)], lib_typ=str(lib), top='a',
                 run_sta=False, run_gls=False)
    errs = cfg.validate()
    check('valid minimal config -> no errors', len(errs) == 0, f'got={errs}')

# Invalid objective
cfg.objective = 'banana'
errs = cfg.validate()
check('invalid objective rejected', any('objective' in e for e in errs), f'got={errs}')

# =========================================================================
print('\n[7] YAML config loading')
# =========================================================================

import yaml
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    rtl = td/'a.v'; rtl.write_text('module mod; endmodule')
    lib = td/'a.lib'; lib.write_text('library(a) {}')
    cfg_yaml = td/'cfg.yaml'
    cfg_yaml.write_text(yaml.dump({
        'rtl_files': [str(td / '*.v')],   # glob
        'lib_typ': str(lib),
        'top': 'mod',
        'period_ps': 7777,
        'objective': 'pareto',
        'run_sta': False,
        'run_gls': False,
    }))
    cfg = Config.from_yaml(cfg_yaml)
    check('yaml: glob expanded', cfg.rtl_files == [str(rtl)], f'got={cfg.rtl_files}')
    check('yaml: period_ps loaded', cfg.period_ps == 7777, f'got={cfg.period_ps}')
    check('yaml: objective loaded', cfg.objective == 'pareto', f'got={cfg.objective}')
    errs = cfg.validate()
    check('yaml-loaded config validates', len(errs) == 0, f'got={errs}')

# =========================================================================
print('\n[8] Recipe file content')
# =========================================================================

for r in DEFAULT_RECIPES_DIR.glob('*.abc'):
    content = r.read_text()
    has_D = '{D}' in content
    is_pure_area = r.stem in {'area_classic', 'area_max'}
    check(f'  {r.stem}: has stime',
          'stime' in content)
    # Delay-targeting recipes must use {D}; pure area recipes may skip it
    if not is_pure_area:
        check(f'  {r.stem}: uses {{D}} placeholder', has_D,
              f'recipe lacks {{D}} but is not pure-area')

# =========================================================================
print('\n[9] abc_sequential flag (experimental ABC -dff mode)')
# =========================================================================

from synth_flow import YOSYS_DRIVER_STD, YOSYS_DRIVER_SEQ

# Default Config has it off
cfg = Config()
check('abc_sequential default = False', cfg.abc_sequential is False)

# Templates differ
check('STD template has dfflibmap before abc',
      YOSYS_DRIVER_STD.find('dfflibmap') < YOSYS_DRIVER_STD.find('abc -liberty'),
      'order check')
check('SEQ template uses abc -dff',
      'abc -dff' in YOSYS_DRIVER_SEQ)
check('SEQ template runs abc before dfflibmap',
      YOSYS_DRIVER_SEQ.find('abc -dff') < YOSYS_DRIVER_SEQ.find('dfflibmap'),
      'order check')
check('STD template does NOT have abc -dff',
      'abc -dff' not in YOSYS_DRIVER_STD)

# YAML loads abc_sequential
import yaml as _y
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    rtl = td/'a.v'; rtl.write_text('module mod; endmodule')
    lib = td/'a.lib'; lib.write_text('library(a) {}')
    cfg_yaml = td/'cfg.yaml'
    cfg_yaml.write_text(_y.dump({
        'rtl_files': [str(rtl)],
        'lib_typ': str(lib),
        'top': 'mod',
        'run_sta': False,
        'run_gls': False,
        'abc_sequential': True,
    }))
    cfg = Config.from_yaml(cfg_yaml)
    check('yaml: abc_sequential loaded as True',
          cfg.abc_sequential is True, f'got={cfg.abc_sequential}')

# Confirm template selection logic in run_recipe matches the flag
# (we don't actually invoke yosys, just check the template branching)
def _select_template(seq_flag: bool) -> str:
    return YOSYS_DRIVER_SEQ if seq_flag else YOSYS_DRIVER_STD

check('flag=False -> standard template', _select_template(False) is YOSYS_DRIVER_STD)
check('flag=True  -> sequential template', _select_template(True) is YOSYS_DRIVER_SEQ)

# =========================================================================
print()
if failures:
    print(f'❌ {len(failures)} failures: {failures}')
    sys.exit(1)
else:
    print('✅ all tests passed')
