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

## False-path SDC for async resets

Async resets create spurious recovery violations on the reset distribution
net (often -4 ns and worse at SS), which would dominate WNS and make winner
ranking meaningless. The STA preamble (`_sta_constraints` in synth_flow.py)
derives them: any input port whose fanout ends only at register async pins
gets `set_false_path -from`. A name list (`PRESETn`, `PRESETN`, `aresetn`,
`HRESETn`, `hresetn`, `rst_n`, `resetn`) is the fallback for resets that
also feed synchronous logic. Design-specific exceptions belong in the user
SDC, which is sourced last and overrides the defaults.

## Synthesis library default

The synthesis library is resolved in this order:
`lib_synth → lib_slow → lib_typ`. SS-corner synthesis is the default
when `lib_slow` is set. To get the older TT-corner-synthesis behavior,
explicitly set `lib_synth: <path-to-tt.lib>`.
