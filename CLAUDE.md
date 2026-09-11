# Agent notes for synth_flow

## OpenSTA discovery

`synth_flow` calls OpenSTA via the binary name in `cfg.opensta` (default
`sta`). On this machine OpenSTA 3.1.0 is built from source (parallaxsw/OpenSTA,
CUDD from the mht208/formal tap, Homebrew tcl-tk@8) and installed at
`~/.local/opt/opensta/bin/sta`, symlinked to `~/.local/bin/sta`, which is on
PATH. There is no Nix store here. If `sta` is missing, check that path first;
rebuild with the recipe in docs/benchmarks.md → Requirements.

Don't rely on ABC's `stime` output as a delay proxy when real OpenSTA is
available — `stime` lacks wire RC, ignores SDC exceptions, and reports against
the synthesis liberty only.

## False-path SDC for APB peripherals

Async resets (`PRESETn`, `hresetn`, `rst_n`) create spurious recovery
violations on the reset distribution buffer (often -100+ ns at SS).
`synth_flow` auto-applies `set_false_path` (via `catch`) on a common set:

`PRESETn`, `PRESETN`, `aresetn`, `HRESETn`, `hresetn`, `rst_n`, `resetn`.

Still prefer an IP SDC for design-specific async inputs (UART RX, GPIO,
etc.). Default I/O delay no longer applies to clock ports or those
async-reset names.

Without this, every recipe's WNS is dominated by the reset path and
winner ranking becomes meaningless.

## Synthesis library default

The synthesis library is resolved in this order:
`lib_synth → lib_slow → lib_typ`. SS-corner synthesis is the default
when `lib_slow` is set. To get the older TT-corner-synthesis behavior,
explicitly set `lib_synth: <path-to-tt.lib>`.
