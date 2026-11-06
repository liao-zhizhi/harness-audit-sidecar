# v0.10.0 — Chronic Drift Detection

Adds one module and one idea: **a point-in-time verdict cannot see a slow death.**

## What is new

`audit_sidecar/drift.py`, exported as `analyze_chronic_drift` and `DriftReport`.

Given a per-cycle series (tokens per retry, files touched, recovery margin, remaining
budget), it decides between four outcomes using the same vocabulary as everything else:
`PASS` stationary, `FAIL` drifting, `INVALID` too few usable samples, `NOT RUN` nothing
measured.

The criterion is deliberately a **conjunction of two gates**, because either gate alone is a
known failure mode:

| gate | what it measures | what happens without it |
| --- | --- | --- |
| `r2 >= 0.98` | regularity | a metric wandering by 1e-7 gets flagged: noise alarm |
| total change `>= 1%` | magnitude | a 40% collapse with jitter is missed by the regularity gate |

Only the conjunction is quiet *and* material. That case is labelled `pattern="chronic"`;
drift that clears magnitude but not regularity is labelled `"visible"`, so the two get
different responses instead of being averaged into one alarm.

## The case that motivated it

A nine-cycle reference run. Substrate at each wake falls from 19.265 to 17.041 mM:

- total change −11.5%, per-cycle ≈1.3% of the initial level — below any per-round
  threshold (≈1.5% against each step's own level; the docs say which)
- linear fit slope −0.2788 mM/cycle, **R² = 0.9997**
- extrapolated zero crossing ≈ cycle 70; the nine "clean cycles" are the first 13% of the decay

Plotted, it is nine near-identical peaks. The visual signature of stability and the
statistical signature of decay are the same picture. `tests/test_drift.py` asserts these
numbers against the real series rather than a synthetic lookalike.

**Scope, stated plainly:** this series comes from a simulation whose kinetic constants are
placeholders. It establishes that the mechanism exists and that this detector finds it. It is
not a biological measurement, and the release does not claim one.

## Refusals, consistent with the rest of the system

- fewer than `min_points` (default 6) → `INVALID`; a trend from three points is an opinion
- any `NaN` / `inf` sample → `INVALID`; fitting across a gap invents the missing measurement
- constant series → `PASS` and `r2 = 1.0` by convention; the magnitude gate decides, not `r2`
- rising series → no `steps_to_zero`; extrapolating a rising series to zero would be the same
  invention in a different direction

## Version strings moved

```
pyproject.toml              version = "0.10.0"
audit_sidecar/__init__.py   __version__ = "0.10.0"
README.md                   status line v0.9.3 -> v0.10.0; test count 514 -> 529
```

## Tests

529 tests in total: 522 passed, 7 skipped. The 507 that predate this release all still
pass; 15 are new in `tests/test_drift.py`.

## What this release does not do

It does not collect the series for you. It does not decide which metric to watch, and it does
not know whether a 1% total change is acceptable for your system — `min_total_change` is a
policy input, and the default is an argument, not a measurement.
