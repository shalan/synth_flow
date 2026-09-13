#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
abc_search — per-design search over ABC's delay target (-D) with OpenSTA feedback.

ABC's -D is a blunt global knob and its internal delay model has no wire
load, so the right value for a design is an empirical question. This module
maps a (module, recipe) at several -D values, runs the same quick STA that
ranks the recipe sweep, and picks the smallest-area netlist that meets
timing (or the best-WNS one when none does).

    python3 abc_search.py --config synth.yaml --recipe delay_choice_deep_v3 \\
        --fracs 0.5 0.7 0.85 1.0 1.2 1.5          # explicit grid, or
    python3 abc_search.py --config synth.yaml --recipe orfs_speed --bisect  # golden-section on area

Output: a table per point (D, cells, area, WNS, TNS, runtime) and the chosen
point; `--json` for machine use; netlists stay in --work-dir.

Only reads synth_flow's public pieces (Config, run_recipe, _quick_sta,
_write_constraint_file, discover_recipes, load_sdc_constraints,
apply_sdc_overrides); it never edits the flow.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth_flow as sf  # noqa: E402


@dataclass
class Point:
    d_ps: int
    frac: float
    cells: int = 0
    area: float = 0.0
    wns_ns: Optional[float] = None
    tns_ns: Optional[float] = None
    runtime_s: float = 0.0
    netlist: str = ''
    ok: bool = False
    error: str = ''


def map_at(cfg: sf.Config, module: str, recipe: str, recipe_path: Path, d_ps: int,
           work: Path, constr: Path) -> Point:
    """Synthesize `module` with `recipe` at ABC -D = d_ps and run the quick STA."""
    cfg_dict = asdict(cfg)
    cfg_dict['abc_d_ps'] = int(d_ps)
    wdir = work / module / f'D{int(d_ps)}'
    res = sf.run_recipe({
        'module': module, 'recipe': recipe, 'recipe_path': str(recipe_path),
        'workdir': str(wdir), 'constr': str(constr), 'cfg': cfg_dict,
    })
    pt = Point(d_ps=int(d_ps), frac=round(d_ps / cfg.period_ps, 3), cells=res.cells, area=res.area,
               runtime_s=res.runtime_s, netlist=res.netlist or '', ok=res.success, error=res.error or '')
    if not res.success or not cfg.run_sta:
        return pt
    qsta_lib = cfg.lib_slow or cfg.lib_typ
    corner = 'slow' if cfg.lib_slow else 'typ'
    wns, tns = sf._quick_sta(
        cfg.opensta, qsta_lib, res.netlist, module, cfg.period_ps, cfg.clock_port,
        wdir / f'{recipe}.qsta.log',
        clock_port_2=cfg.clock_port_2, period_ps_2=cfg.period_ps_2,
        macro_libs=sf._extra_libs(cfg, corner),
        sdc=cfg.sdc, driving_cell=cfg.driving_cell, load_ff=cfg.load_ff,
        unc_setup_ps=cfg.clock_uncertainty_setup_ps, unc_hold_ps=cfg.clock_uncertainty_hold_ps,
        wire_load_model=cfg.wire_load_model, io_delay_frac=cfg.io_delay_frac)
    pt.wns_ns, pt.tns_ns = wns, tns
    return pt


def choose(points: list[Point], margin_ns: float = 0.0) -> Optional[Point]:
    """Smallest area among points meeting timing (WNS >= margin); else best WNS."""
    good = [p for p in points if p.ok and p.wns_ns is not None]
    if not good:
        return None
    meeting = [p for p in good if p.wns_ns >= margin_ns]
    if meeting:
        return min(meeting, key=lambda p: (p.area, -p.wns_ns))
    return max(good, key=lambda p: (p.wns_ns, -p.area))


def grid_search(cfg, module, recipe, recipe_path, fracs, work, constr, log=print) -> list[Point]:
    pts = []
    for f in fracs:
        d = max(50, int(round(cfg.period_ps * f)))
        pt = map_at(cfg, module, recipe, recipe_path, d, work, constr)
        pts.append(pt)
        log(_fmt(pt))
    return pts


def bisect_search(cfg, module, recipe, recipe_path, work, constr, lo=0.4, hi=2.0,
                  max_points=7, margin_ns=0.0, log=print) -> list[Point]:
    """Find the largest -D (least effort, usually least area) that still meets
    timing. Monotonicity is only approximate, so this is a guided grid:
    evaluate lo, hi, then bisect on 'meets timing' with a cap on STA calls."""
    pts: list[Point] = []

    def ev(frac):
        d = max(50, int(round(cfg.period_ps * frac)))
        for p in pts:
            if p.d_ps == d:
                return p
        p = map_at(cfg, module, recipe, recipe_path, d, work, constr)
        pts.append(p)
        log(_fmt(p))
        return p

    a, b = ev(lo), ev(hi)
    meets = lambda p: p.ok and p.wns_ns is not None and p.wns_ns >= margin_ns
    if meets(b):                 # loosest already meets: try even looser once
        ev(min(hi * 1.5, 4.0))
        return pts
    if not meets(a):             # tightest fails: report the grid, nothing more to bisect
        ev((lo + hi) / 2)
        return pts
    lo_f, hi_f = lo, hi          # invariant: lo meets, hi fails
    while len(pts) < max_points and (hi_f - lo_f) > 0.05:
        mid = (lo_f + hi_f) / 2
        p = ev(mid)
        if meets(p):
            lo_f = mid
        else:
            hi_f = mid
    return pts


def _fmt(p: Point) -> str:
    if not p.ok:
        return f'  D={p.d_ps:6d} ({p.frac:.2f}T)  FAILED {p.error}'
    w = f'{p.wns_ns:+.3f}' if p.wns_ns is not None else '  n/a'
    t = f'{p.tns_ns:+.2f}' if p.tns_ns is not None else 'n/a'
    return (f'  D={p.d_ps:6d} ({p.frac:.2f}T)  cells={p.cells:6d}  area={p.area:10.1f}  '
            f'WNS={w}  TNS={t}  {p.runtime_s:.1f}s')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', required=True)
    ap.add_argument('--module', help='default: top')
    ap.add_argument('--recipe', default='orfs_speed')
    ap.add_argument('--fracs', nargs='+', type=float, help='-D as fractions of the period')
    ap.add_argument('--bisect', action='store_true', help='guided bisection on "meets timing"')
    ap.add_argument('--lo', type=float, default=0.4)
    ap.add_argument('--hi', type=float, default=2.0)
    ap.add_argument('--max-points', type=int, default=7)
    ap.add_argument('--margin-ns', type=float, default=0.0)
    ap.add_argument('--work-dir', default='work_dsearch')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--sdc')
    a = ap.parse_args()

    cfg = sf.Config.from_yaml(Path(a.config))
    cfg.merge_env()
    if a.sdc:
        cfg.sdc = a.sdc
    errs = cfg.validate()
    if errs:
        for e in errs:
            print('config error:', e, file=sys.stderr)
        return 1
    c = sf.load_sdc_constraints(cfg)
    sf.apply_sdc_overrides(cfg, c)
    module = a.module or cfg.top
    pairs = sf.discover_recipes(Path(cfg.recipes_dir), [a.recipe])
    if not pairs:
        print(f'recipe {a.recipe} not found', file=sys.stderr)
        return 1
    recipe_path = Path(pairs[0][1])
    work = Path(a.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    constr = sf._write_constraint_file(work, cfg.driving_cell, cfg.load_ff)

    log = (lambda *x: None) if a.json else print
    log(f'{module} / {a.recipe}: period {cfg.period_ps} ps, clock {cfg.clock_port}, '
        f'STA lib {cfg.lib_slow or cfg.lib_typ}')
    t0 = time.time()
    if a.bisect or not a.fracs:
        pts = bisect_search(cfg, module, a.recipe, recipe_path, work, constr,
                            lo=a.lo, hi=a.hi, max_points=a.max_points, margin_ns=a.margin_ns, log=log)
    else:
        pts = grid_search(cfg, module, a.recipe, recipe_path, a.fracs, work, constr, log=log)
    best = choose(pts, a.margin_ns)
    out = {'module': module, 'recipe': a.recipe, 'period_ps': cfg.period_ps,
           'points': [asdict(p) for p in sorted(pts, key=lambda p: p.d_ps)],
           'chosen': asdict(best) if best else None, 'elapsed_s': round(time.time() - t0, 1)}
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        base = next((p for p in pts if abs(p.frac - 1.0) < 1e-6), None)
        if best:
            print(f'chosen: D={best.d_ps} ({best.frac:.2f}T) area={best.area:.1f} WNS={best.wns_ns:+.3f}'
                  + (f'   vs D=T: area {(best.area / base.area - 1) * 100:+.1f}%, WNS {best.wns_ns - base.wns_ns:+.3f} ns'
                     if base and base.ok and base.wns_ns is not None else ''))
        print(f'{len(pts)} points in {out["elapsed_s"]}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
