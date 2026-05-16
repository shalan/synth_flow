# Agent notes for synth_flow

## OpenSTA discovery

`synth_flow` calls OpenSTA via the binary name in `cfg.opensta` (default
`sta`). On many setups (macOS in particular) `sta` is not on PATH but
**is** installed via Nix. Before assuming OpenSTA is missing, search the
Nix store:

```bash
# Quick discovery — pick the newest version found.
find /nix/store -maxdepth 3 -name sta -type f 2>/dev/null | head
# Then verify:
/nix/store/<hash>-devshell-dir/bin/sta -version
/nix/store/<hash>-openroad/bin/sta -version
```

If found, wire it into the YAML config:

```yaml
opensta: /nix/store/<hash>-devshell-dir/bin/sta
```

…and re-run **without** `--no-sta`. Don't rely on ABC's `stime` output
as a delay proxy when real OpenSTA is available — `stime` lacks wire RC,
ignores SDC false-paths, and reports against the synthesis liberty only.

The user keeps OpenSTA in Nix on this machine; check there first before
falling back to ABC-internal estimates or asking the user to install it.

## False-path SDC for APB peripherals

Async resets (`PRESETn`, `hresetn`, `rst_n`) create spurious recovery
violations on the reset distribution buffer (often -100+ ns at SS).
`synth_flow` auto-applies `set_false_path` on `hresetn` and `rst_n`, but
**not** on `PRESETn`. For APB peripherals, point `cfg.sdc` at an SDC
file containing at minimum:

```tcl
set_false_path -from [get_ports PRESETn]
```

Without this, every recipe's WNS is dominated by the reset path and
winner ranking becomes meaningless.

## Synthesis library default

The synthesis library is resolved in this order:
`lib_synth → lib_slow → lib_typ`. SS-corner synthesis is the default
when `lib_slow` is set. To get the older TT-corner-synthesis behavior,
explicitly set `lib_synth: <path-to-tt.lib>`.
