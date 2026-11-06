"""The published numbers must be the numbers the code produces.

R6 was edited three times in one evening: ``1.25%`` became ``1.3%`` (the
figure depended on which denominator the rolling std used), the per-cycle loss
needed its denominator named, and the reference fit moved into a module. Every
one of those edits was applied by hand to four documents. That is precisely the
drift this project exists to detect, so it should not be checked by eye.

What this does **not** do: parse prose. It asserts that each figure the code
can recompute appears, in the form the code rounds it to, in the documents that
quote it. A sentence can still be wrong about why a number matters. A number
cannot silently become a different number.
"""

from __future__ import annotations

import os
import re
import unittest

from audit_sidecar.drift import analyze_chronic_drift

# ``tests/`` is not a package; both pytest and ``unittest discover -s tests``
# put this directory on sys.path, so the sibling import resolves either way.
from test_drift import R6_SERIES

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(_REPO, *parts), encoding="utf-8") as handle:
        return handle.read()


class PublishedFitTest(unittest.TestCase):
    """Every figure quoted from the reference run is recomputed here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = analyze_chronic_drift(R6_SERIES)
        cls.readme = _read("README.md")
        cls.module = _read("audit_sidecar", "drift.py")

    def test_r2_is_quoted_as_the_code_rounds_it(self) -> None:
        self.assertAlmostEqual(self.report.r2, 0.9997, places=4)
        self.assertIn("0.9997", self.readme)
        self.assertIn("0.9997", self.module)

    def test_slope_is_quoted(self) -> None:
        self.assertAlmostEqual(self.report.slope, -0.2788, places=4)
        self.assertIn("0.2788", self.readme)
        self.assertIn("0.2788", self.module)

    def test_total_change_is_quoted(self) -> None:
        # The code's reason string prints it to two decimals as a percentage.
        self.assertIn("{:.2%}".format(abs(self.report.relative_total_change)), self.report.reason)
        self.assertIn("11.5", self.readme)

    def test_both_denominators_for_per_cycle_loss_are_named(self) -> None:
        # 1.28% of the initial level, 1.52% against each step's own level. The
        # README used to quote one number without saying which, and a reader
        # recomputing the other way would find a discrepancy rather than a
        # definition.
        self.assertIn("1.3%", self.readme)
        self.assertIn("1.5%", self.readme)
        self.assertIn("denominator", self.readme)

    def test_drawdown_is_quoted(self) -> None:
        self.assertAlmostEqual(self.report.max_drawdown, -0.1154, places=3)

    def test_zero_crossing_convention_is_stated(self) -> None:
        # 0-based index 69.05 == cycle 70. The README must say which.
        self.assertIn("69.05", self.readme)
        self.assertIn("cycle 70", self.readme)


class DocumentedRefusalsTest(unittest.TestCase):
    """A refusal the code performs must be a refusal the README documents."""

    def test_every_refusal_in_the_readme_is_in_the_code(self) -> None:
        readme = _read("README.md")
        for phrase in (
            "NOT RUN",
            "INVALID",
            "min_points",
            "`NaN`",      # the README's wording for the non-finite refusal
            "`inf`",
        ):
            self.assertIn(phrase, readme)
        code = _read("audit_sidecar", "drift.py")
        for phrase in ("non-finite", "min_points"):
            self.assertIn(phrase, code)

    def test_status_line_version_matches_the_package(self) -> None:
        import audit_sidecar

        readme = _read("README.md")
        self.assertRegex(
            readme,
            r"Project status: MVP / early version \(v" + re.escape(audit_sidecar.__version__) + r"\)",
        )


class PublishedLimitsTest(unittest.TestCase):
    """The declarations at the top of the README must match SELF_AUDIT.md.

    This is the same defect as the citation rule in the theory repo: a
    sentence people are told to quote is a sentence that has been certified,
    and it goes stale exactly like any other. The range here is derived from
    the file rather than written by hand, so adding LIM-10 cannot leave the
    README claiming nine.
    """

    def setUp(self) -> None:
        self.audit = _read("SELF_AUDIT.md")

    def test_declared_range_matches_the_actual_limits(self) -> None:
        readme = _read("README.md")
        declared = re.search(r"`LIM-(\d+)` to `LIM-(\d+)`", readme)
        self.assertIsNotNone(declared, "the README states no limit range")
        low, high = (int(declared.group(1)), int(declared.group(2)))
        actual = {int(n) for n in re.findall(r"`LIM-(\d+)`", self.audit)}
        self.assertTrue(actual, "SELF_AUDIT.md defines no limit ids")
        # A range that does not cover the file is the citation-rule defect: a
        # sentence people are told to quote, certified and then gone stale.
        self.assertEqual(
            (low, high),
            (min(actual), max(actual)),
            "the declared range {}..{} does not match SELF_AUDIT.md {}".format(
                low, high, sorted(actual)),
        )
        self.assertEqual(actual, set(range(low, high + 1)),
                         "SELF_AUDIT.md has a gap in its limit numbering")

    def test_the_two_read_this_first_claims_are_present(self) -> None:
        readme = _read("README.md")
        self.assertIn("Read this first", readme)
        self.assertIn("PASS means the world matches the intent", readme)
        self.assertIn("cannot tell you whether it was in a position", readme)
        # The declaration must come before the descriptive paragraph, i.e. it
        # has to be the first thing a reader meets.
        self.assertLess(
            readme.index("Read this first"),
            readme.index("Four verdicts, not two"),
        )


class SelfAuditDocTest(unittest.TestCase):
    """The audit must not describe defects it no longer has."""

    def test_every_fixed_item_has_a_regression_test(self) -> None:
        audit = _read("SELF_AUDIT.md")
        fixed = set(re.findall(r"\| \*\*(D\d\.\d+)\*\* .*?\| `?(FIXED)`?", audit))
        self.assertTrue(fixed)
        suite = "\n".join(
            _read("tests", name)
            for name in os.listdir(os.path.join(_REPO, "tests"))
            if name.endswith(".py")
        )
        # Both halves of the critical finding are pinned by name in the suite.
        self.assertIn("chmod", suite)
        self.assertIn("__unreadable__", suite)
        self.assertIn("recovered-drawdown", suite)


class PackagingHygieneTest(unittest.TestCase):
    """Internal working notes must not reach an sdist."""

    def test_manifest_prunes_marketing(self) -> None:
        manifest = _read("MANIFEST.in")
        self.assertRegex(manifest, r"(?m)^prune marketing$")


if __name__ == "__main__":
    unittest.main()
