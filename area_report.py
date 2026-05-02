#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""Generate area/cell-count report from a gate-level netlist + liberty file."""
import re, sys

def parse_liberty_areas(lib_path):
    ca = {}
    lt = open(lib_path).read()
    for m in re.finditer(r'cell\s*\("?(\S+?)"?\)\s*\{', lt):
        n = m.group(1).strip('"')
        b = lt[m.end():]
        d = 1; i = 0
        while i < len(b) and d > 0:
            d += (1 if b[i] == '{' else -1 if b[i] == '}' else 0); i += 1
        a = re.search(r'area\s*:\s*([\d.]+)', b[:i])
        if a:
            ca[n] = float(a.group(1))
    return ca

def parse_netlist_cells(nl_path):
    cc = {}
    for line in open(nl_path):
        for m in re.finditer(r'(sky130_fd_sc_hd__\w+)\s+_', line):
            cc[m.group(1)] = cc.get(m.group(1), 0) + 1
    return cc

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <netlist.v> <liberty.lib>", file=sys.stderr)
        sys.exit(1)
    nl_path, lib_path = sys.argv[1], sys.argv[2]
    ca = parse_liberty_areas(lib_path)
    cc = parse_netlist_cells(nl_path)
    tc = sum(cc.values())
    ta = sum(cc.get(c, 0) * ca.get(c, 0) for c in cc)
    df = sum(v for k, v in cc.items() if 'df' in k)
    ng = ca.get('sky130_fd_sc_hd__nand2_1', 1.0)

    print('Cell Count & Area Summary')
    print('=' * 55)
    print(f'{"Cell Type":<35} {"Count":>6} {"Area":>11}')
    print('-' * 55)
    for c in sorted(cc, key=lambda x: -cc[x]):
        a = ca.get(c, 0)
        print(f'{c:<35} {cc[c]:>6} {cc[c]*a:>11.2f}')
    print('-' * 55)
    print(f'{"TOTAL":<35} {tc:>6} {ta:>11.2f}')
    print('=' * 55)
    print(f'Total cells:     {tc}')
    print(f'Sequential:      {df}  ({100*df/tc:.1f}%)')
    print(f'Combinational:   {tc - df}  ({100*(tc-df)/tc:.1f}%)')
    print(f'Total area:      {ta:.2f} um^2')
    print(f'Eq. gate count:  {ta/ng:.0f} (NAND2=1)')
    print(f'Unique types:    {len(cc)}')

if __name__ == '__main__':
    main()
