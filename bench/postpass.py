#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
postpass.py — evaluate netlist post-passes (resize, refine) on bench winners.

Reads each design's winner from bench/work/<design>/results/<top>/winner.v
(produced by bench.py), takes the clock, period, driving cell and load from
the derived results/<top>/synth.sdc, runs the requested passes in order and
records WNS / TNS / area before and after.

    ./postpass.py --passes resize                 # all designs with a winner
    ./postpass.py --passes refine,resize --designs apb_timer alu32
    ./postpass.py --compare results/postpass-<tag>.csv   # print the table again

Output: results/postpass-<tag>.csv (+ .md), one row per design.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
sys.path.insert(0, str(REPO))
import refine as refine_mod   # noqa: E402
import resize as resize_mod   # noqa: E402

LIB_SS = REPO / 'sky130' / 'hd_120_ss.lib'


def derived_constraints(sdc_path: Path) -> dict:
    """Pull clock/period/driving cell/load (and a 2nd clock) from synth.sdc."""
    d = {'clock_port': None, 'period_ps': None, 'clock_port_2': None, 'period_ps_2': None,
         'driving_cell': 'sky130_fd_sc_hd__inv_2', 'load_ff': 17.65, 'unc_setup_ps': 250, 'unc_hold_ps': 100}
    clocks = re.findall(r'create_clock -name (\S+) -period ([0-9.]+)', sdc_path.read_text())
    if clocks:
        d['clock_port'], d['period_ps'] = clocks[0][0], int(round(float(clocks[0][1]) * 1000))
    if len(clocks) > 1:
        d['clock_port_2'], d['period_ps_2'] = clocks[1][0], int(round(float(clocks[1][1]) * 1000))
    m = re.search(r'set_driving_cell -lib_cell (\S+)', sdc_path.read_text())
    if m:
        d['driving_cell'] = m.group(1)
    m = re.search(r'set_load ([0-9.]+)', sdc_path.read_text())
    if m:
        d['load_ff'] = float(m.group(1)) * 1000.0
    m = re.search(r'set_clock_uncertainty -setup ([0-9.]+)', sdc_path.read_text())
    if m:
        d['unc_setup_ps'] = int(round(float(m.group(1)) * 1000))
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--passes', default='resize', help='comma list in order: resize, refine')
    ap.add_argument('--designs', nargs='+')
    ap.add_argument('--tag', default=dt.datetime.now().strftime('%Y%m%d-%H%M%S'))
    ap.add_argument('--iters', type=int, default=25)
    ap.add_argument('--refine-iters', type=int, default=2)
    ap.add_argument('--use-sdc', action='store_true', default=True)
    ap.add_argument('--no-sdc', dest='use_sdc', action='store_false')
    a = ap.parse_args()
    passes = [p.strip() for p in a.passes.split(',') if p.strip()]

    manifest = yaml.safe_load((BENCH / 'manifest.yaml').read_text())['designs']
    if a.designs:
        manifest = [d for d in manifest if d['name'] in a.designs]
    rows = []
    for d in manifest:
        name, top = d['name'], d['top']
        res_dir = BENCH / 'work' / name / 'results' / top
        winner = res_dir / 'winner.v'
        sdc_derived = res_dir / 'synth.sdc'
        if not winner.exists() or not sdc_derived.exists():
            print(f'[{name}] no winner (run bench.py first)')
            continue
        c = derived_constraints(sdc_derived)
        user_sdc = str(BENCH / d['sdc']) if (a.use_sdc and d.get('sdc')) else None
        work = BENCH / 'work' / name / 'postpass'
        cur = winner
        row = {'design': name, 'top': top, 'period_ps': c['period_ps'], 'passes': '+'.join(passes)}
        t0 = time.time()
        print(f'[{name}] {top} T={c["period_ps"]} clk={c["clock_port"]} passes={passes}')
        first = True
        for pss in passes:
            wd = work / pss
            common = dict(sdc=user_sdc, yosys='yosys', opensta='sta', driving_cell=c['driving_cell'],
                          load_ff=c['load_ff'], unc_setup_ps=c['unc_setup_ps'], unc_hold_ps=c['unc_hold_ps'],
                          clock_port_2=c['clock_port_2'], period_ps_2=c['period_ps_2'],
                          log=lambda *x: print('   ', *x))
            try:
                if pss == 'resize':
                    r = resize_mod.resize(cur, top, str(LIB_SS), str(LIB_SS), c['period_ps'], c['clock_port'], wd,
                                          iters=a.iters, **common)
                elif pss == 'refine':
                    r = refine_mod.refine(cur, top, str(LIB_SS), str(LIB_SS), c['period_ps'], c['clock_port'], wd,
                                          recipe=REPO / 'recipes' / 'orfs_speed.abc', iters=a.refine_iters,
                                          whole_only=True, **common)
                else:
                    print('   unknown pass', pss)
                    continue
            except Exception as e:  # keep going with the other designs
                print(f'   {pss} FAILED: {e}')
                row[f'{pss}_error'] = str(e)
                continue
            if first:
                row.update(wns_before=r['start']['wns_ns'], tns_before=r['start']['tns_ns'], area_before=r['start']['area'])
                first = False
            row.update(wns_after=r['end']['wns_ns'], tns_after=r['end']['tns_ns'], area_after=r['end']['area'])
            cur = Path(r['output'])
        row['runtime_s'] = round(time.time() - t0, 1)
        rows.append(row)

    cols = ['design', 'top', 'period_ps', 'passes', 'wns_before', 'wns_after', 'tns_before', 'tns_after',
            'area_before', 'area_after', 'runtime_s']
    out = BENCH / 'results' / f'postpass-{a.tag}.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f'\n{"design":14} {"WNS before":>10} {"WNS after":>10} {"TNS before":>10} {"TNS after":>10} {"Δarea%":>7}')
    for r in rows:
        if 'wns_after' not in r:
            continue
        da = (r['area_after'] / r['area_before'] - 1) * 100 if r.get('area_before') else 0
        print(f"{r['design']:14} {r['wns_before']:+10.3f} {r['wns_after']:+10.3f} {r['tns_before']:+10.2f} {r['tns_after']:+10.2f} {da:+7.2f}")
    print(f'wrote {out.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
