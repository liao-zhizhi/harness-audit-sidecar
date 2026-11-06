"""Tests for chronic drift detection.

The first test uses the actual nine-cycle series from the reference run, so
the module is pinned to the case that motivated it rather than to a synthetic
lookalike.
"""

from __future__ import annotations

import unittest

from audit_sidecar.drift import (
    PATTERN_CHRONIC,
    PATTERN_VISIBLE,
    analyze_chronic_drift,
)
from audit_sidecar.verdict import Verdict


# results/fed_batch_cycles.csv: substrate at each wake, cycles 1..9.
R6_SERIES = [
    19.265347845615832,
    18.981644367978493,
    18.687637194265356,
    18.400092994099662,
    18.118078247468766,
    17.841377747079730,
    17.569799852561950,
    17.303164601044287,
    17.041297149218096,
]


class ReferenceSeriesTest(unittest.TestCase):
    """The R6 case: a near-perfect fit that no per-cycle alarm would catch."""

    def setUp(self) -> None:
        self.report = analyze_chronic_drift(R6_SERIES)

    def test_is_flagged_as_chronic(self) -> None:
        self.assertIs(self.report.verdict, Verdict.FAIL)
        self.assertEqual(self.report.pattern, PATTERN_CHRONIC)
        self.assertTrue(self.report.is_chronic)

    def test_r2_matches_the_published_fit(self) -> None:
        # Published: r2 = 0.9997, slope = -0.2788 mM per cycle.
        self.assertAlmostEqual(self.report.r2, 0.9997, places=4)
        self.assertAlmostEqual(self.report.slope, -0.2788, places=3)

    def test_total_change_is_reported_as_a_fraction(self) -> None:
        self.assertAlmostEqual(self.report.relative_total_change, -0.1154, places=3)

    def test_zero_crossing_is_extrapolated(self) -> None:
        # The fit is against a 0-based index, so cycle 70 is index 69.
        # The reference run reports "about cycle 70"; +1 converts index to
        # cycle number for a series that starts at cycle 1.
        self.assertIsNotNone(self.report.steps_to_zero)
        self.assertAlmostEqual(self.report.steps_to_zero + 1, 70.1, delta=0.5)

    def test_tiny_per_cycle_loss_is_explained(self) -> None:
        # The whole point: per-cycle loss is ~1.3%, invisible in one record.
        self.assertLess(abs(self.report.relative_total_change) / self.report.n, 0.02)


class CriteriaTest(unittest.TestCase):
    """R2 alone is not a drift test; magnitude alone is not the quiet case."""

    def test_stationary_series_passes(self) -> None:
        report = analyze_chronic_drift([1.0, 1.0001, 0.9999, 1.0, 1.0002, 0.9998])
        self.assertIs(report.verdict, Verdict.PASS)

    def test_constant_series_passes_despite_r2_of_one(self) -> None:
        # Documents the convention: r2 = 1.0 on a flat line, and the magnitude
        # gate is what stops that from being read as a drift.
        report = analyze_chronic_drift([5.0] * 8)
        self.assertIs(report.verdict, Verdict.PASS)
        self.assertEqual(report.r2, 1.0)

    def test_high_r2_with_negligible_magnitude_is_not_a_failure(self) -> None:
        # A 1e-7 slope gives a great fit and means nothing. Flagging it would
        # be the noise-alarm failure mode.
        series = [0.9998 + 1e-7 * i for i in range(10)]
        report = analyze_chronic_drift(series)
        self.assertIs(report.verdict, Verdict.PASS)

    def test_material_but_noisy_drift_is_visible_not_chronic(self) -> None:
        series = [10.0, 9.4, 9.9, 8.7, 9.1, 8.2, 8.6, 7.4, 7.9, 7.0]
        report = analyze_chronic_drift(series)
        self.assertIs(report.verdict, Verdict.FAIL)
        self.assertEqual(report.pattern, PATTERN_VISIBLE)
        self.assertFalse(report.is_chronic)
        # Pins the figure quoted in the module docstring and the README: a
        # material fall with this much jitter sits well under the 0.98 gate.
        self.assertAlmostEqual(report.r2, 0.864, places=2)

    def test_rising_drift_has_no_zero_crossing(self) -> None:
        report = analyze_chronic_drift([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertIs(report.verdict, Verdict.FAIL)
        self.assertIsNone(report.steps_to_zero)


class RecoveredDrawdownTest(unittest.TestCase):
    """The endpoint blind spot: net change cannot see a dip that recovered.

    ``total_change`` is what a cumulative sum measures, and a V-shaped series
    has ``total_change == 0``. Before this gate existed, a system that fell 40%
    and climbed back scored ``PASS`` -- the exact failure mode of watching an
    average instead of the path. Reported as its own pattern because the
    response differs: nothing is still decaying, but something almost died.
    """

    V_SHAPE = [100, 92, 84, 76, 68, 60, 62, 70, 78, 86, 94, 100]

    def setUp(self) -> None:
        self.report = analyze_chronic_drift(self.V_SHAPE)

    def test_v_shape_is_not_pass(self) -> None:
        self.assertIs(self.report.verdict, Verdict.FAIL)
        self.assertEqual(self.report.pattern, "recovered-drawdown")

    def test_net_change_really_is_zero(self) -> None:
        # Why the old criterion missed it, asserted rather than described.
        self.assertAlmostEqual(self.report.relative_total_change, 0.0)

    def test_drawdown_is_measured(self) -> None:
        self.assertAlmostEqual(self.report.max_drawdown, -0.40, places=2)

    def test_small_dip_is_still_pass(self) -> None:
        # The gate must not become a noise alarm: a 2% dip and recovery is
        # within ordinary variation.
        report = analyze_chronic_drift([1.0, 0.98, 1.0, 1.01, 1.0, 0.99, 1.0])
        self.assertIs(report.verdict, Verdict.PASS)

    def test_drawdown_threshold_is_a_policy_input(self) -> None:
        series = [100, 90, 100, 100, 100, 100]
        self.assertIs(
            analyze_chronic_drift(series, min_drawdown=0.05).verdict, Verdict.FAIL)
        self.assertIs(
            analyze_chronic_drift(series, min_drawdown=0.50).verdict, Verdict.PASS)

    def test_reference_series_reports_its_drawdown(self) -> None:
        # A monotone decay has drawdown equal to its total fall; the two gates
        # agree here, and the net-change branch decides the label.
        self.assertAlmostEqual(
            analyze_chronic_drift(R6_SERIES).max_drawdown, -0.1154, places=3)


class SamplingContractTest(unittest.TestCase):
    """r2 describes the trend only if the input is one sample per cycle.

    Feeding the raw trace (peaks and troughs) makes r2 measure the oscillation
    instead. Pinned as a test so the contract is visible rather than implied.
    """

    def test_intra_cycle_oscillation_destroys_the_regularity_signal(self) -> None:
        trend = [10.0 - 0.3 * i for i in range(9)]
        with_oscillation = []
        for level in trend:
            with_oscillation.extend([level + 0.4, level - 0.4])
        per_cycle = analyze_chronic_drift(trend)
        raw_trace = analyze_chronic_drift(with_oscillation)
        self.assertGreaterEqual(per_cycle.r2, 0.98)
        self.assertEqual(per_cycle.pattern, "chronic")
        # 0.86, measured -- under the gate. The decay is still reported (FAIL),
        # but as "visible", i.e. the quiet-drift signal is lost. That is the
        # cost of feeding the wrong series, and it is why the contract is
        # stated in the docstring instead of assumed.
        self.assertLess(raw_trace.r2, 0.98)
        self.assertEqual(raw_trace.pattern, "visible")


class RefusalTest(unittest.TestCase):
    """Same rule as the verdict lattice: do not answer what was not measured."""

    def test_empty_input_is_not_run(self) -> None:
        report = analyze_chronic_drift([])
        self.assertIs(report.verdict, Verdict.NOT_RUN)
        self.assertEqual(report.n, 0)

    def test_too_few_points_is_invalid(self) -> None:
        report = analyze_chronic_drift([3.0, 2.0, 1.0])
        self.assertIs(report.verdict, Verdict.INVALID)

    def test_gap_is_invalid_not_interpolated(self) -> None:
        report = analyze_chronic_drift([1.0, 0.9, float("nan"), 0.7, 0.6, 0.5])
        self.assertIs(report.verdict, Verdict.INVALID)
        self.assertIn("non-finite", report.reason)

    def test_infinity_is_invalid(self) -> None:
        report = analyze_chronic_drift([1.0, 1.0, 1.0, float("inf"), 1.0, 1.0])
        self.assertIs(report.verdict, Verdict.INVALID)

    def test_min_points_is_respected(self) -> None:
        # Five points refused by default, accepted when the caller lowers it.
        tight = [10.0, 8.0, 6.0, 4.0, 2.0]
        self.assertIs(analyze_chronic_drift(tight).verdict, Verdict.INVALID)
        self.assertIs(
            analyze_chronic_drift(tight, min_points=5).verdict, Verdict.FAIL)


if __name__ == "__main__":
    unittest.main()
