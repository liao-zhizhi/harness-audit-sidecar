"""Chronic drift detection: a "perfect loop" can be a slow death.

Why this module exists
----------------------
The four-valued verdict catches a single lie: the agent says done, the
witness says nothing landed. It cannot catch the other failure, because
that one has no bad record to point at.

A repeating agent loop -- retry, partial success, retry -- produces a
series of numbers that look periodic. If each round loses a little margin,
the series is not periodic: it is a **decaying transient**. In the reference
run behind this module, the substrate level at each wake was 19.265, 18.982,
... 17.041 mM over nine cycles. Plotted, that is nine near-identical peaks.
Fitted, it is a line with ``R^2 = 0.9997`` and a slope of -0.2788 mM/cycle.

Per-round loss was 1.3% of the initial level (1.5% if each step is measured
against its own level), below any per-round alarm threshold. The
extrapolated zero crossing was round 70. Nine "clean cycles" were the first
13% of the decay.

Why the naive criterion fails
-----------------------------
``R^2`` measures **regularity**, not **magnitude**. Two different things get
confused here, and both directions matter:

* A metric wobbling by 1e-7 per cycle around 0.9998 gives ``R^2 ~ 1.0`` and is
  harmless. Flagging on ``R^2`` alone produces noise alarms.
* A metric falling 40% with jitter lands around ``R^2 = 0.86`` and is genuinely
  dying, but every variance-based alarm would have seen it.

So the criterion is a conjunction: ``R^2 >= r2_threshold`` **and** a
material total change. When both hold, the drift is quiet *and* large --
which is the dangerous quadrant, and the one no threshold on a single round
can reach. When only the magnitude holds, the drift is real but visible, and
is reported as a distinct pattern rather than silently merged into the first.

Deliberate refusals
-------------------
* Fewer than ``min_points`` finite samples -> ``INVALID``. A trend fitted to
  three points is an opinion, not a measurement. This is the same rule as
  ``PASS`` not being permitted without a witness.
* Any non-finite sample -> ``INVALID``. Interpolating across a gap would be
  inventing the very measurement that is missing.
* Empty input -> ``NOT RUN``. Nothing was measured, so nothing is claimed.

The verdict vocabulary is reused rather than extended: a drift result is a
judgement about evidence, so it answers with the same four words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .verdict import Verdict

__all__ = [
    "DriftReport",
    "PATTERN_CHRONIC",
    "PATTERN_VISIBLE",
    "analyze_chronic_drift",
]


# Pattern labels. Only meaningful when the verdict is FAIL.
PATTERN_CHRONIC = "chronic"   # quiet and material: the R6 case
PATTERN_VISIBLE = "visible"   # material but noisy: ordinary, catchable trend
PATTERN_RECOVERED = "recovered-drawdown"  # dipped hard, came back: nearly died


@dataclass(frozen=True)
class DriftReport:
    """Result of fitting one measurement series.

    Attributes
    ----------
    verdict:
        ``PASS`` stationary, ``FAIL`` drifting, ``INVALID`` not enough usable
        samples, ``NOT RUN`` nothing to measure.
    pattern:
        ``"chronic"`` or ``"visible"`` when the verdict is ``FAIL``, else None.
    n:
        Number of finite samples that entered the fit.
    slope, intercept:
        Ordinary least squares against the sample index (0-based), so the
        slope is "change per cycle" in the caller's own unit.
    r2:
        Coefficient of determination. Reported as 1.0 for a constant series by
        convention; a constant series is ``PASS`` because its total change is
        zero, not because of ``r2``.
    total_change:
        ``last - first``.
    relative_total_change:
        ``total_change / |first|`` when ``first`` is non-zero, else None.
    steps_to_zero:
        Extrapolated **0-based index** at which the fitted line reaches zero,
        when the slope is negative and the intercept is positive. None
        otherwise -- a rising series does not "reach zero", and pretending it
        does would be the kind of extrapolation this module refuses elsewhere.
        If the caller's first sample is cycle 1, the cycle number is this
        value plus one (the reference run reports 69.05 here, i.e. cycle 70).
    max_drawdown:
        Largest peak-to-trough fall, as a signed fraction of the running peak
        (``-0.11`` means an 11% drawdown). ``None`` when the running peak is
        zero or the series is empty.
    reason:
        One line explaining the verdict, suitable for a log or a receipt.
    """

    verdict: Verdict
    pattern: Optional[str]
    n: int
    slope: float
    intercept: float
    r2: float
    total_change: float
    relative_total_change: Optional[float]
    steps_to_zero: Optional[float]
    max_drawdown: Optional[float]
    reason: str

    @property
    def is_chronic(self) -> bool:
        """True only for the quiet-and-material case."""
        return self.pattern == PATTERN_CHRONIC


def _ols(values: Sequence[float]) -> tuple:
    """Least-squares fit against the sample index. No SciPy, no numpy."""
    n = len(values)
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    sxx = sum((i - mean_x) ** 2 for i in range(n))
    sxy = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(values))
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in values)
    if ss_tot == 0.0:
        # A constant series: the fit is exact. r2 is conventionally 1.0 here;
        # do not read that as "drifting" -- the magnitude gate decides.
        return slope, intercept, 1.0
    ss_res = sum((y - (intercept + slope * i)) ** 2 for i, y in enumerate(values))
    return slope, intercept, 1.0 - ss_res / ss_tot


def _max_drawdown(values: Sequence[float]) -> Optional[float]:
    """Largest peak-to-trough fall, as a signed fraction of the running peak.

    Net change cannot see a drawdown that recovered. A series that falls 40%
    and climbs back has ``total_change == 0`` and used to be reported as
    ``PASS`` -- a system that nearly died scoring as perfectly healthy. This
    is the endpoint-relative blind spot of a cumulative sum, and the reason
    ``phi`` alone is not enough.
    """
    peak = values[0]
    worst = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak == 0:
            # A relative drawdown from zero is undefined; do not invent one.
            continue
        fall = (value - peak) / abs(peak)
        if fall < worst:
            worst = fall
    if peak == 0 and worst == 0.0:
        return None
    return worst


def analyze_chronic_drift(
    values: Sequence[float],
    *,
    min_points: int = 6,
    r2_threshold: float = 0.98,
    min_total_change: float = 0.01,
    min_drawdown: float = 0.10,
) -> DriftReport:
    """Fit ``values`` and decide whether it is drifting.

    Parameters
    ----------
    values:
        One measurement per cycle, in order. **Sample at the same phase of
        every cycle.** Feeding the raw time series (intra-cycle peaks and
        troughs included) makes ``r2`` describe the oscillation rather than
        the trend, and the drift gate then fails on a genuine decay. The
        reference series is the wake value of each cycle, not the trace.
    min_points:
        Fewest samples that may be fitted. Default 6 -- see the module
        docstring for why a short series is refused rather than extrapolated.
    r2_threshold:
        Regularity gate. Default 0.98, i.e. a drift this quiet will not be
        seen by watching any single round.
    min_total_change:
        Magnitude gate, as a fraction (0.01 = 1% over the whole window).
        Applied to the relative change when ``values[0]`` is non-zero,
        otherwise to the absolute change.
    min_drawdown:
        Peak-to-trough gate, as a fraction (0.10 = 10%). Only consulted when
        the net change is *not* material, i.e. exactly the case where a dip
        recovered and the endpoint sum cancelled it out. This is a policy
        input, not a measurement: a system that fell 10% and came back did
        survive, and whether that counts as an incident is the operator's
        call, not this function's.
    """
    n_total = len(values)
    if n_total == 0:
        return DriftReport(
            verdict=Verdict.NOT_RUN, pattern=None, n=0, slope=0.0, intercept=0.0,
            r2=0.0, total_change=0.0, relative_total_change=None,
            steps_to_zero=None, max_drawdown=None,
            reason="no samples: nothing was measured",
        )

    # Non-finite values are not skipped: a gap is an absence of measurement,
    # and fitting across it would invent the missing point.
    bad = [i for i, v in enumerate(values) if v != v or v in (float("inf"), float("-inf"))]
    if bad:
        return DriftReport(
            verdict=Verdict.INVALID, pattern=None, n=n_total, slope=0.0,
            intercept=0.0, r2=0.0, total_change=0.0,
            relative_total_change=None, steps_to_zero=None, max_drawdown=None,
            reason="non-finite sample at index {}: cannot fit across a gap".format(bad[0]),
        )

    if n_total < min_points:
        return DriftReport(
            verdict=Verdict.INVALID, pattern=None, n=n_total, slope=0.0,
            intercept=0.0, r2=0.0, total_change=0.0,
            relative_total_change=None, steps_to_zero=None, max_drawdown=None,
            reason="{} samples < min_points {}: a trend from this is an opinion".format(
                n_total, min_points),
        )

    slope, intercept, r2 = _ols(values)
    total_change = values[-1] - values[0]
    first = values[0]
    relative = (total_change / abs(first)) if first != 0 else None

    # Magnitude, relative when possible so the threshold is unit-free.
    magnitude = abs(relative) if relative is not None else abs(total_change)
    material = magnitude >= min_total_change

    steps_to_zero = None
    if slope < 0 and intercept > 0:
        steps_to_zero = -intercept / slope

    drawdown = _max_drawdown(values)

    if not material:
        # Net change says "fine". A recovered dip says "it nearly died". These
        # are different questions and the endpoint sum cannot answer the second.
        if drawdown is not None and abs(drawdown) >= min_drawdown:
            return DriftReport(
                verdict=Verdict.FAIL, pattern=PATTERN_RECOVERED, n=n_total,
                slope=slope, intercept=intercept, r2=r2,
                total_change=total_change, relative_total_change=relative,
                steps_to_zero=steps_to_zero, max_drawdown=drawdown,
                reason=("recovered drawdown: fell {:.2%} peak-to-trough and "
                        "returned, so the net change is {:.2%} -- the endpoint "
                        "sum hides that it almost reached zero").format(
                            abs(drawdown), magnitude),
            )
        return DriftReport(
            verdict=Verdict.PASS, pattern=None, n=n_total, slope=slope,
            intercept=intercept, r2=r2, total_change=total_change,
            relative_total_change=relative, steps_to_zero=None,
            max_drawdown=drawdown,
            reason="no material drift: total change {:.3%} < {:.3%}".format(
                magnitude, min_total_change),
        )

    if r2 >= r2_threshold:
        return DriftReport(
            verdict=Verdict.FAIL, pattern=PATTERN_CHRONIC, n=n_total, slope=slope,
            intercept=intercept, r2=r2, total_change=total_change,
            relative_total_change=relative, steps_to_zero=steps_to_zero,
            max_drawdown=drawdown,
            reason=("chronic drift: {:.2%} over {} cycles at r2={:.4f} -- "
                    "systematic and below any per-cycle threshold").format(
                        magnitude, n_total, r2),
        )

    return DriftReport(
        verdict=Verdict.FAIL, pattern=PATTERN_VISIBLE, n=n_total, slope=slope,
        intercept=intercept, r2=r2, total_change=total_change,
        relative_total_change=relative, steps_to_zero=steps_to_zero,
        max_drawdown=drawdown,
        reason="visible drift: {:.2%} over {} cycles but r2={:.4f} < {:.2f}".format(
            magnitude, n_total, r2, r2_threshold),
    )
