#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
bench.py — synth_flow benchmark runner.

Runs synth_flow.py over every design in bench/manifest.yaml (or a subset),
collects per-recipe QoR into a CSV, writes a Markdown summary, and can diff
two result files to evaluate a change.

Usage
-----
  ./bench.py                              # all designs, all recipes
  ./bench.py --quick                      # 4 representative recipes
  ./bench.py --designs alu32 uart --recipes orfs_speed area_classic
  ./bench.py --tag before                 # results/before.csv (+ latest.csv/.md)
  ./bench.py --compare results/before.csv results/latest.csv

Columns
-------
  design, category, top, recipe, is_winner, cells, area_um2, wns_ns, tns_ns,
  abc_delay_ps, runtime_s, status, error

  wns_ns / tns_ns come from OpenSTA at the slow corner (empty when OpenSTA is
  not available). abc_delay_ps is ABC's own `stime` estimate parsed from the
  synthesis log: no wire load, synthesis liberty only. It is a proxy, useful
  for relative comparisons when STA is missing, not a timing sign-off number.

Environment (yosys/abc/sta versions, synth_flow git SHA, date) is written to
results/<tag>.env.json next to the CSV so results are reproducible.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML required: pip install pyyaml")

BENCH_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_DIR.parent
SYNTH_FLOW = REPO_DIR / 'synth_flow.py'
LIB_TT = REPO_DIR / 'sky130' / 'hd_120_tt.lib'
LIB_SS = REPO_DIR / 'sky130' / 'hd_120_ss.lib'
LIB_FF = REPO_DIR / 'sky130' / 'hd_120_ff.lib'

QUICK_RECIPES = ['orfs_speed', 'balanced_resyn', 'area_classic', 'delay_choice_deep']

COLUMNS = ['design', 'category', 'top', 'recipe', 'is_winner', 'cells', 'area_um2',
           'wns_ns', 'tns_ns', 'abc_delay_ps', 'runtime_s', 'status', 'error']


# --------------------------------------------------------------------------
# Environment capture
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ''


def capture_env(yosys: str, sta: str | None) -> dict:
    yosys_v = _run([yosys, '-V']).splitlines()[:1]
    abc_bin = shutil.which('yosys-abc') or ''
    abc_v = ''
    if abc_bin:
        out = _run([abc_bin, '-q', 'version'])
        m = re.search(r'UC Berkeley, ABC[^\n]*', out)
        abc_v = m.group(0) if m else out.splitlines()[:1]
    sta_v = _run([sta, '-version']).splitlines()[:1] if sta else ''
    sha = _run(['git', '-C', str(REPO_DIR), 'rev-parse', 'HEAD'])
    dirty = bool(_run(['git', '-C', str(REPO_DIR), 'status', '--porcelain', '--', ':!bench/results']))
    return {
        'date': dt.datetime.now().isoformat(timespec='seconds'),
        'yosys': yosys_v[0] if yosys_v else '',
        'abc': abc_v if isinstance(abc_v, str) else (abc_v[0] if abc_v else ''),
        'opensta': sta_v[0] if sta_v else '(not available)',
        'synth_flow_sha': sha,
        'synth_flow_dirty': dirty,
        'lib_synth': LIB_SS.name,
    }


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text()) or {}
    designs = data.get('designs', [])
    for d in designs:
        for k in ('name', 'top', 'files', 'clock', 'period_ps'):
            if k not in d:
                sys.exit(f"manifest: design entry missing '{k}': {d}")
    return designs


def resolve_files(d: dict) -> list[str]:
    out = []
    for pat in d['files']:
        hits = sorted((BENCH_DIR).glob(pat))
        if not hits:
            return []
        out.extend(str(h) for h in hits)
    return out


# --------------------------------------------------------------------------
# One design
# --------------------------------------------------------------------------

def write_config(d: dict, files: list[str], args, run_sta: bool, work: Path) -> Path:
    cfg = {
        'rtl_files': files,
        'top': d['top'],
        'modules': [d['top']],
        'lib_typ': str(LIB_TT),
        'lib_slow': str(LIB_SS),
        'lib_fast': str(LIB_FF),
        'period_ps': int(d['period_ps']),
        'clock_port': d['clock'],
        'objective': args.objective,
        'run_sta': run_sta,
        'run_gls': False,
        'fail_on_timing': False,
        'parallel': args.parallel,
        'work_dir': str(work / 'work'),
        'results_dir': str(work / 'results'),
    }
    if d.get('includes'):
        cfg['verilog_includes'] = [str(BENCH_DIR / p) for p in d['includes']]
    if d.get('defines'):
        cfg['verilog_defines'] = list(d['defines'])
    if d.get('clock_2'):
        cfg['clock_port_2'] = d['clock_2']
        cfg['period_ps_2'] = int(d.get('period_ps_2', d['period_ps']))
    if args.use_sdc and d.get('sdc'):
        cfg['sdc'] = str(BENCH_DIR / d['sdc'])
    if args.sta_bin:
        cfg['opensta'] = args.sta_bin
    p = work / 'synth.yaml'
    work.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return p


ABC_DELAY_RE = re.compile(r'Delay\s*=\s*([0-9.]+)\s*ps')


def abc_delay_from_log(log_path: Path) -> str:
    """Last `stime` delay printed by ABC in a synth log (proxy metric)."""
    try:
        text = log_path.read_text(errors='ignore')
    except OSError:
        return ''
    hits = ABC_DELAY_RE.findall(text)
    return hits[-1] if hits else ''


def run_design(d: dict, args, run_sta: bool) -> list[dict]:
    name = d['name']
    work = BENCH_DIR / 'work' / name
    files = resolve_files(d)
    base = {'design': name, 'category': d.get('category', ''), 'top': d['top']}
    if not files:
        hint = ' (run bench/fetch_external.sh)' if d.get('external') else ''
        return [dict(base, recipe='', status='missing_rtl', error=f'no RTL matched{hint}')]

    if work.exists() and not args.keep_work:
        shutil.rmtree(work)
    cfg_path = write_config(d, files, args, run_sta, work)

    cmd = [sys.executable, str(SYNTH_FLOW), '--config', str(cfg_path), '--no-gls', '-q']
    if not run_sta:
        cmd.append('--no-sta')
    if args.recipes:
        cmd += ['--recipes', *args.recipes]
    t0 = time.time()
    log = work / 'bench.log'
    with open(log, 'w') as lf:
        try:
            r = subprocess.run(cmd, cwd=REPO_DIR, stdout=lf, stderr=subprocess.STDOUT,
                               timeout=args.timeout)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = -1
    elapsed = time.time() - t0

    summary = work / 'results' / 'summary.json'
    if not summary.exists():
        err = 'timeout' if rc == -1 else f'synth_flow exit {rc} (see {log.relative_to(BENCH_DIR)})'
        return [dict(base, recipe='', status='failed', error=err, runtime_s=f'{elapsed:.1f}')]

    data = json.loads(summary.read_text())
    mod = data['modules'].get(d['top'])
    if not mod:
        return [dict(base, recipe='', status='failed', error='top module missing from summary')]

    rows = []
    for c in mod['candidates']:
        recipe = c['recipe']
        synth_log = next((work / 'work').rglob(f'{recipe}.synth.log'), None)
        ok = bool(c.get('netlist'))
        rows.append(dict(
            base,
            recipe=recipe,
            is_winner=int(recipe == mod.get('winner')),
            cells=c.get('cells', ''),
            area_um2=f"{c['area']:.2f}" if c.get('area') is not None else '',
            wns_ns='' if c.get('wns_ns') is None else f"{c['wns_ns']:.3f}",
            tns_ns='' if c.get('tns_ns') is None else f"{c['tns_ns']:.3f}",
            abc_delay_ps=abc_delay_from_log(synth_log) if synth_log else '',
            runtime_s=f"{c.get('runtime_s', 0):.1f}",
            status='ok' if ok else 'recipe_failed',
            error='' if ok else 'no netlist',
        ))
    return rows


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in COLUMNS})


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def write_md(path: Path, rows: list[dict], env: dict, recipes: list[str]) -> None:
    md = ['# synth_flow benchmark results', '']
    md.append(f"- date: {env['date']}")
    md.append(f"- yosys: `{env['yosys']}`")
    md.append(f"- abc: `{env['abc']}`")
    md.append(f"- opensta: `{env['opensta']}`")
    md.append(f"- synth_flow: `{env['synth_flow_sha'][:12]}`{' (dirty)' if env['synth_flow_dirty'] else ''}")
    md.append(f"- synthesis liberty: `{env['lib_synth']}`")
    md.append('')
    have_sta = any(r.get('wns_ns') for r in rows)
    designs = sorted({r['design'] for r in rows}, key=lambda n: [r['design'] for r in rows].index(n))

    md.append('## Winner per design')
    md.append('')
    hdr = '| design | category | winner | cells | area (um²) | ' + ('WNS@SS (ns) | ' if have_sta else '') + 'ABC delay (ps) |'
    md.append(hdr)
    md.append('|' + '---|' * (hdr.count('|') - 1))
    for dname in designs:
        drows = [r for r in rows if r['design'] == dname]
        win = next((r for r in drows if str(r.get('is_winner')) == '1'), None)
        if win is None:
            err = drows[0].get('error', '')
            md.append(f"| {dname} | {drows[0].get('category','')} | *{drows[0].get('status','')}* | | | " + ('| ' if have_sta else '') + f"{err} |")
            continue
        md.append(f"| {dname} | {win['category']} | {win['recipe']} | {win['cells']} | {win['area_um2']} | "
                  + (f"{win['wns_ns']} | " if have_sta else '') + f"{win['abc_delay_ps']} |")
    md.append('')

    def matrix(title, key, fmt=lambda v: v):
        md.append(f'## {title}')
        md.append('')
        md.append('| design | ' + ' | '.join(recipes) + ' |')
        md.append('|' + '---|' * (len(recipes) + 1))
        for dname in designs:
            cells = []
            for rec in recipes:
                r = next((x for x in rows if x['design'] == dname and x['recipe'] == rec), None)
                cells.append(fmt(r.get(key, '')) if r and r.get('status') == 'ok' else '—')
            md.append(f'| {dname} | ' + ' | '.join(cells) + ' |')
        md.append('')

    matrix('Area (um²) per recipe', 'area_um2')
    if have_sta:
        matrix('WNS at slow corner (ns) per recipe', 'wns_ns')
    matrix('ABC stime delay (ps) per recipe — proxy, no wire load', 'abc_delay_ps')
    matrix('Runtime (s) per recipe', 'runtime_s')
    path.write_text('\n'.join(md))


def compare(a_path: Path, b_path: Path) -> int:
    def load(p):
        with open(p) as f:
            return {(r['design'], r['recipe']): r for r in csv.DictReader(f) if r['status'] == 'ok'}
    A, B = load(a_path), load(b_path)
    keys = sorted(set(A) & set(B))
    if not keys:
        print('no overlapping (design, recipe) rows'); return 1
    print(f'{"design":16} {"recipe":22} {"area A":>10} {"area B":>10} {"Δarea%":>8} '
          f'{"wns A":>8} {"wns B":>8} {"Δwns":>8} {"abcD A":>8} {"abcD B":>8} {"ΔabcD%":>8}')
    tot_area = []; tot_abc = []
    for k in keys:
        a, b = A[k], B[k]
        aa, ab = _f(a['area_um2']), _f(b['area_um2'])
        wa, wb = _f(a['wns_ns']), _f(b['wns_ns'])
        da, db = _f(a['abc_delay_ps']), _f(b['abc_delay_ps'])
        d_area = (ab - aa) / aa * 100 if aa and ab is not None else None
        d_abc = (db - da) / da * 100 if da and db is not None else None
        d_wns = (wb - wa) if wa is not None and wb is not None else None
        if d_area is not None: tot_area.append(d_area)
        if d_abc is not None: tot_abc.append(d_abc)
        fmt = lambda v, s: (s % v) if v is not None else '—'
        print(f'{k[0]:16} {k[1]:22} {fmt(aa,"%10.1f")} {fmt(ab,"%10.1f")} {fmt(d_area,"%+8.2f")} '
              f'{fmt(wa,"%8.3f")} {fmt(wb,"%8.3f")} {fmt(d_wns,"%+8.3f")} {fmt(da,"%8.0f")} {fmt(db,"%8.0f")} {fmt(d_abc,"%+8.2f")}')
    if tot_area:
        print(f'\nmean Δarea  = {sum(tot_area)/len(tot_area):+.2f}%  over {len(tot_area)} rows')
    if tot_abc:
        print(f'mean ΔabcD  = {sum(tot_abc)/len(tot_abc):+.2f}%  over {len(tot_abc)} rows')
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--manifest', default=str(BENCH_DIR / 'manifest.yaml'))
    p.add_argument('--designs', nargs='+', help='subset of design names')
    p.add_argument('--category', nargs='+', help='subset by category')
    p.add_argument('--recipes', nargs='+', help='recipes to sweep (default: all)')
    p.add_argument('--quick', action='store_true', help=f'use {QUICK_RECIPES}')
    p.add_argument('--objective', default='pareto')
    p.add_argument('--use-sdc', action='store_true', help='pass each design SDC to synth_flow (STA)')
    p.add_argument('--parallel', type=int, default=0, help='synth_flow workers per design (0=auto)')
    p.add_argument('--timeout', type=int, default=7200, help='seconds per design')
    p.add_argument('--sta-bin', help='OpenSTA binary (default: `sta` on PATH)')
    p.add_argument('--no-sta', action='store_true')
    p.add_argument('--keep-work', action='store_true', help='do not wipe bench/work/<design>')
    p.add_argument('--tag', help='results file stem (default: timestamp)')
    p.add_argument('--compare', nargs=2, metavar=('A.csv', 'B.csv'), help='diff two result files and exit')
    args = p.parse_args()

    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))

    if args.quick and not args.recipes:
        args.recipes = QUICK_RECIPES
    designs = load_manifest(Path(args.manifest))
    if args.designs:
        unknown = set(args.designs) - {d['name'] for d in designs}
        if unknown:
            sys.exit(f'unknown designs: {sorted(unknown)}')
        designs = [d for d in designs if d['name'] in args.designs]
    if args.category:
        designs = [d for d in designs if d.get('category') in args.category]

    yosys = os.environ.get('YOSYS', 'yosys')
    sta = None if args.no_sta else (args.sta_bin or os.environ.get('OPENSTA') or shutil.which('sta'))
    run_sta = bool(sta)
    if not run_sta:
        print('note: OpenSTA not found; WNS/TNS columns will be empty (ABC stime used as proxy)')
    env = capture_env(yosys, sta)

    recipes = args.recipes or sorted(p.stem for p in (REPO_DIR / 'recipes').glob('*.abc'))
    rows: list[dict] = []
    failures = 0
    for i, d in enumerate(designs, 1):
        print(f'[{i}/{len(designs)}] {d["name"]} ... ', end='', flush=True)
        t0 = time.time()
        drows = run_design(d, args, run_sta)
        rows.extend(drows)
        ok = [r for r in drows if r.get('status') == 'ok']
        if not ok:
            failures += 1
            print(f'FAILED ({drows[0].get("error","")})')
        else:
            win = next((r for r in ok if str(r.get('is_winner')) == '1'), ok[0])
            print(f'{len(ok)}/{len(drows)} recipes ok, winner {win["recipe"]} '
                  f'area={win["area_um2"]} cells={win["cells"]}'
                  + (f' wns={win["wns_ns"]}' if win.get('wns_ns') else f' abcD={win["abc_delay_ps"]}ps')
                  + f'  [{time.time()-t0:.0f}s]')

    out_dir = BENCH_DIR / 'results'
    out_dir.mkdir(exist_ok=True)
    tag = args.tag or dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    csv_path = out_dir / f'{tag}.csv'
    write_csv(csv_path, rows)
    (out_dir / f'{tag}.env.json').write_text(json.dumps(env, indent=2))
    write_csv(out_dir / 'latest.csv', rows)
    write_md(out_dir / 'latest.md', rows, env, recipes)
    print(f'\nwrote {csv_path.relative_to(REPO_DIR)}, results/latest.csv, results/latest.md')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
