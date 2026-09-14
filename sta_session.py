#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Author: Mohamed Shalan <mshalan@aucegypt.edu>
"""
sta_session — a persistent OpenSTA process for the post-pass.

Every sizing / buffering / hold trial used to start `sta`, read every liberty
and link the netlist again: minutes per call on a 350k-cell design. A
StaSession keeps one `sta` process per corner alive for the whole post-pass.
The liberties are read once; a trial is either

  * re-linked: `read_verilog new.v; link_design top` plus the constraints in
    the same process (no liberty re-read), or
  * applied incrementally: the netlist edits of the trial are mirrored with
    `replace_cell`, `make_instance`, `make_net`, `connect_pin` /
    `disconnect_pin`, and undone on rejection, so OpenSTA re-times only the
    affected paths.

Commands are sent over stdin; each request ends with a marker `puts` so the
reply can be read back deterministically. Enabled by `sta_session: true`.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional


class StaSessionError(RuntimeError):
    pass


def sta_name(name: str) -> str:
    """OpenSTA's spelling of a Verilog identifier (escaped names lose the backslash)."""
    n = name.strip()
    return n[1:] if n.startswith('\\') else n


class StaSession:
    """One interactive OpenSTA process bound to one liberty set."""

    def __init__(self, opensta: str, libs: list, log=print, timeout: float = 1800.0):
        self.opensta = opensta
        self.libs = [str(l) for l in libs]
        self.log = log
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self.seq = 0
        self.current: Optional[Path] = None          # netlist the linked design corresponds to
        self.top: Optional[str] = None
        self.constraints: str = ''
        self._undo: list[str] = []                    # Tcl to revert the pending trial, in reverse order
        self.stats = {'relinks': 0, 'incremental': 0, 'commands': 0, 'seconds': 0.0}

    # ---- process -------------------------------------------------------
    def start(self) -> None:
        self.proc = subprocess.Popen([self.opensta, '-no_init', '-no_splash'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for lib in self.libs:
            self.cmd(f'read_liberty {lib}')

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write('exit\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        self.proc = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def cmd(self, tcl: str, check: bool = True) -> str:
        """Run Tcl and return its output. Raises StaSessionError on an OpenSTA
        Error line when `check` (Warnings pass through)."""
        if not self.alive():
            raise StaSessionError('OpenSTA session is not running')
        self.seq += 1
        marker = f'<<<synth_flow {self.seq}>>>'
        t0 = time.time()
        self.proc.stdin.write(tcl.rstrip('\n') + '\n' + f'puts "{marker}"\n')
        self.proc.stdin.flush()
        out = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise StaSessionError('OpenSTA session ended unexpectedly: ' + ''.join(out)[-500:])
            if line.strip() == marker:
                break
            out.append(line)
            if time.time() - t0 > self.timeout:
                self.close()
                raise StaSessionError('OpenSTA session timed out')
        self.stats['commands'] += 1
        self.stats['seconds'] += time.time() - t0
        text = ''.join(out)
        if check:
            err = next((l for l in out if l.startswith('Error')), None)
            if err:
                raise StaSessionError(err.strip())
        return text

    # ---- design --------------------------------------------------------
    def link(self, netlist: Path, top: str, constraints: str) -> None:
        """(Re)link `netlist` and apply `constraints` (Tcl text). Any pending
        trial edits are dropped: the linked design is the file again."""
        if not self.alive():
            self.start()
        self.cmd(f'read_verilog {netlist}')
        self.cmd(f'link_design {top}')
        self.cmd(constraints, check=False)
        self.current, self.top, self.constraints = Path(netlist), top, constraints
        self._undo = []
        self.stats['relinks'] += 1

    # ---- incremental trial ---------------------------------------------
    def apply(self, ops: list) -> None:
        """Mirror netlist edits into the linked design. Ops (Verilog spellings):
          ('replace_cell', inst, new_cell, old_cell)
          ('make_net', net)
          ('make_instance', inst, cell, {pin: net})
          ('reconnect', inst, pin, old_net, new_net)      (net None = unconnected)
        Recorded for `undo`; raises StaSessionError on any OpenSTA error."""
        for op in ops:
            kind = op[0]
            if kind == 'replace_cell':
                _, inst, new, old = op
                self.cmd(f'replace_cell {{{sta_name(inst)}}} {new}')
                self._undo.append(f'replace_cell {{{sta_name(inst)}}} {old}')
            elif kind == 'make_net':
                self.cmd(f'make_net {{{sta_name(op[1])}}}')
                self._undo.append(f'delete_net {{{sta_name(op[1])}}}')
            elif kind == 'make_instance':
                _, inst, cell, pins = op
                self.cmd(f'make_instance {{{sta_name(inst)}}} {cell}')
                for pin, net in pins.items():
                    if net is not None and not _is_const(net):
                        self.cmd(f'connect_pin {{{sta_name(net)}}} {{{sta_name(inst)}/{pin}}}')
                self._undo.append(f'delete_instance {{{sta_name(inst)}}}')
            elif kind == 'reconnect':
                _, inst, pin, old, new = op
                pin_path = f'{sta_name(inst)}/{pin}'
                if old is not None and not _is_const(old):
                    self.cmd(f'disconnect_pin {{{sta_name(old)}}} {{{pin_path}}}')
                if new is not None and not _is_const(new):
                    self.cmd(f'connect_pin {{{sta_name(new)}}} {{{pin_path}}}')
                back = ''
                if new is not None and not _is_const(new):
                    back += f'disconnect_pin {{{sta_name(new)}}} {{{pin_path}}}\n'
                if old is not None and not _is_const(old):
                    back += f'connect_pin {{{sta_name(old)}}} {{{pin_path}}}'
                if back:
                    self._undo.append(back)
            else:
                raise StaSessionError(f'unknown edit op {kind}')
        self.stats['incremental'] += 1

    def commit(self, netlist: Path) -> None:
        """The pending trial is accepted: the linked design now matches `netlist`."""
        self.current = Path(netlist)
        self._undo = []

    def undo(self) -> None:
        """Revert the pending trial's edits (the design matches `current` again)."""
        for tcl in reversed(self._undo):
            self.cmd(tcl)
        self._undo = []

    # ---- reports -------------------------------------------------------
    def report(self, tcl: str) -> str:
        return self.cmd(tcl, check=False)


def _is_const(net: str) -> bool:
    n = net.strip()
    return "'" in n or n in ("1'b0", "1'b1")
