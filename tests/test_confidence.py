"""Tests for the trust energy account."""

from __future__ import annotations

import unittest

from audit_sidecar.confidence import ConfidenceAccount
from audit_sidecar.verdict import Verdict


class ConfidenceAccountTest(unittest.TestCase):
    # -- construction -------------------------------------------------

    def test_default_window_is_one_hundred(self) -> None:
        self.assertEqual(ConfidenceAccount().window_size, 100)
        self.assertEqual(ConfidenceAccount.DEFAULT_WINDOW, 100)

    def test_window_must_be_positive(self) -> None:
        for window in (0, -1):
            with self.subTest(window=window):
                with self.assertRaises(ValueError):
                    ConfidenceAccount(window=window)

    def test_window_must_be_an_int(self) -> None:
        for window in (True, 3.0, "10"):
            with self.subTest(window=window):
                with self.assertRaises(TypeError):
                    ConfidenceAccount(window=window)  # type: ignore[arg-type]

    # -- record -------------------------------------------------------

    def test_record_rejects_non_verdict(self) -> None:
        account = ConfidenceAccount()
        for value in ("PASS", True, None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    account.record(value)  # type: ignore[arg-type]

    def test_record_returns_the_verdict(self) -> None:
        account = ConfidenceAccount()
        self.assertIs(account.record(Verdict.PASS), Verdict.PASS)

    def test_clear_empties_the_window(self) -> None:
        account = ConfidenceAccount()
        account.record(Verdict.PASS)
        account.clear()
        self.assertEqual(len(account), 0)
        self.assertEqual(account.get_balance(), 0.0)

    # -- balance ------------------------------------------------------

    def test_empty_account_has_zero_balance_and_low_label(self) -> None:
        account = ConfidenceAccount()
        self.assertEqual(account.get_balance(), 0.0)
        self.assertEqual(account.get_label(), "low")

    def test_balance_is_pass_over_all_evidential(self) -> None:
        account = ConfidenceAccount()
        for verdict in (
            Verdict.PASS,
            Verdict.PASS,
            Verdict.FAIL,
            Verdict.INVALID,
        ):
            account.record(verdict)
        self.assertAlmostEqual(account.get_balance(), 2 / 4)

    def test_invalid_counts_in_the_denominator(self) -> None:
        account = ConfidenceAccount()
        account.record(Verdict.PASS)
        account.record(Verdict.INVALID)
        self.assertAlmostEqual(account.get_balance(), 0.5)

    def test_not_run_is_excluded_from_the_denominator(self) -> None:
        account = ConfidenceAccount()
        account.record(Verdict.PASS)
        account.record(Verdict.NOT_RUN)
        account.record(Verdict.NOT_RUN)
        # Two NOT RUN events must not dilute the single PASS.
        self.assertAlmostEqual(account.get_balance(), 1.0)
        self.assertEqual(account.get_label(), "high")

    def test_only_not_run_events_yield_zero_balance(self) -> None:
        account = ConfidenceAccount()
        account.record(Verdict.NOT_RUN)
        self.assertEqual(account.get_balance(), 0.0)
        self.assertEqual(account.get_label(), "low")

    def test_not_run_still_occupies_a_window_slot(self) -> None:
        account = ConfidenceAccount(window=2)
        account.record(Verdict.NOT_RUN)
        account.record(Verdict.PASS)
        self.assertEqual(len(account), 2)

    # -- labels -------------------------------------------------------

    def test_label_thresholds(self) -> None:
        cases = (
            (70, 30, "high"),    # exactly 0.7
            (71, 29, "high"),
            (69, 31, "medium"),  # just below 0.7
            (50, 50, "medium"),  # exactly 0.5
            (49, 51, "low"),     # just below 0.5
            (0, 100, "low"),
        )
        for passed, failed, expected in cases:
            with self.subTest(passed=passed, failed=failed):
                account = ConfidenceAccount()
                for _ in range(passed):
                    account.record(Verdict.PASS)
                for _ in range(failed):
                    account.record(Verdict.FAIL)
                self.assertEqual(account.get_label(), expected)

    # -- sliding window ----------------------------------------------

    def test_window_evicts_the_oldest_event(self) -> None:
        account = ConfidenceAccount(window=3)
        account.record(Verdict.FAIL)
        self.assertAlmostEqual(account.get_balance(), 0.0)
        for _ in range(3):
            account.record(Verdict.PASS)
        # The FAIL has been pushed out of the window.
        self.assertAlmostEqual(account.get_balance(), 1.0)
        self.assertEqual(len(account), 3)

    def test_window_length_never_exceeds_capacity(self) -> None:
        account = ConfidenceAccount(window=5)
        for _ in range(20):
            account.record(Verdict.PASS)
        self.assertEqual(len(account), 5)

    # -- introspection -----------------------------------------------

    def test_counts_reports_every_verdict(self) -> None:
        account = ConfidenceAccount()
        account.record(Verdict.PASS)
        account.record(Verdict.FAIL)
        account.record(Verdict.INVALID)
        account.record(Verdict.NOT_RUN)
        self.assertEqual(
            account.counts,
            {"PASS": 1, "FAIL": 1, "INVALID": 1, "NOT RUN": 1},
        )

    def test_repr_is_informative(self) -> None:
        account = ConfidenceAccount(window=10)
        account.record(Verdict.PASS)
        text = repr(account)
        self.assertIn("window=10", text)
        self.assertIn("high", text)


if __name__ == "__main__":
    unittest.main()
