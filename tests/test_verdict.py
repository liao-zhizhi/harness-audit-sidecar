"""Tests for the four-valued verdict engine."""

from __future__ import annotations

import unittest

from audit_sidecar.verdict import Verdict, VerdictEngine


class _Report:
    """Minimal stand-in for an agent status object."""

    def __init__(self, success: bool) -> None:
        self.success = success


class VerdictEnumTest(unittest.TestCase):
    def test_exactly_four_values(self) -> None:
        self.assertEqual(
            {member.value for member in Verdict},
            {"PASS", "FAIL", "INVALID", "NOT RUN"},
        )

    def test_str_keeps_paper_spelling(self) -> None:
        self.assertEqual(str(Verdict.NOT_RUN), "NOT RUN")
        self.assertEqual("{}".format(Verdict.INVALID), "INVALID")

    def test_not_run_is_not_evidence(self) -> None:
        self.assertFalse(Verdict.NOT_RUN.is_evidence)
        for verdict in (Verdict.PASS, Verdict.FAIL, Verdict.INVALID):
            self.assertTrue(verdict.is_evidence)


class VerdictEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = VerdictEngine()

    # -- PASS ---------------------------------------------------------

    def test_pass_when_witness_and_report_agree(self) -> None:
        verdict = self.engine.evaluate(
            intent="hash:new", effect="hash:new", check=True
        )
        self.assertIs(verdict, Verdict.PASS)

    def test_pass_with_hash_style_effect(self) -> None:
        verdict = self.engine.evaluate(
            intent="a" * 64, effect="a" * 64, check="success"
        )
        self.assertIs(verdict, Verdict.PASS)

    # -- INVALID ------------------------------------------------------

    def test_invalid_when_effect_is_missing(self) -> None:
        verdict = self.engine.evaluate(intent="hash:new", effect=None, check=True)
        self.assertIs(verdict, Verdict.INVALID)

    def test_missing_effect_never_yields_pass(self) -> None:
        # The whole point of the lattice: a confident report cannot
        # substitute for a missing witness.
        for report in (True, "success", "ok", _Report(True)):
            with self.subTest(report=report):
                verdict = self.engine.evaluate(
                    intent="hash:new", effect=None, check=report
                )
                self.assertIs(verdict, Verdict.INVALID)
                self.assertIsNot(verdict, Verdict.PASS)

    def test_missing_effect_wins_over_failed_report(self) -> None:
        verdict = self.engine.evaluate(intent="hash:new", effect=None, check=False)
        self.assertIs(verdict, Verdict.INVALID)

    # -- FAIL: intent not implemented --------------------------------

    def test_fail_when_intent_not_implemented(self) -> None:
        verdict = self.engine.evaluate(
            intent="hash:new", effect="hash:old", check=False
        )
        self.assertIs(verdict, Verdict.FAIL)

    # -- FAIL: false completion --------------------------------------

    def test_fail_on_false_completion(self) -> None:
        verdict = self.engine.evaluate(
            intent="hash:new", effect="hash:old", check=True
        )
        self.assertIs(verdict, Verdict.FAIL)

    def test_false_completion_is_not_invalid(self) -> None:
        # A present-but-contradicting witness is a definite FAIL, not an
        # unknown. INVALID is reserved for "we never looked".
        verdict = self.engine.evaluate(
            intent="hash:new", effect="hash:old", check="done"
        )
        self.assertIs(verdict, Verdict.FAIL)

    # -- FAIL: false failure -----------------------------------------

    def test_fail_on_false_failure(self) -> None:
        verdict = self.engine.evaluate(
            intent="hash:new", effect="hash:new", check=False
        )
        self.assertIs(verdict, Verdict.FAIL)

    def test_false_failure_with_error_string(self) -> None:
        verdict = self.engine.evaluate(
            intent="hash:new", effect="hash:new", check="error"
        )
        self.assertIs(verdict, Verdict.FAIL)

    # -- NOT RUN -----------------------------------------------------

    def test_not_run_when_no_intent(self) -> None:
        verdict = self.engine.evaluate(intent=None, effect="hash:old", check=False)
        self.assertIs(verdict, Verdict.NOT_RUN)

    def test_missing_witness_beats_missing_intent(self) -> None:
        # Rule order: no witness is INVALID even if nothing was intended,
        # because the absence of an effect cannot be asserted either.
        verdict = self.engine.evaluate(intent=None, effect=None, check=False)
        self.assertIs(verdict, Verdict.INVALID)

    # -- report normalisation ----------------------------------------

    def test_report_accepts_bool_string_and_object(self) -> None:
        cases = (
            (True, Verdict.PASS),
            ("PASSED", Verdict.PASS),
            (" ok ", Verdict.PASS),
            (False, Verdict.FAIL),
            ("failed", Verdict.FAIL),
            (_Report(True), Verdict.PASS),
            (_Report(False), Verdict.FAIL),
        )
        for report, expected in cases:
            with self.subTest(report=report):
                verdict = self.engine.evaluate(
                    intent=1, effect=1, check=report
                )
                self.assertIs(verdict, expected)

    def test_unrecognised_report_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.engine.evaluate(intent=1, effect=1, check="probably fine")

    def test_report_of_wrong_type_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.engine.evaluate(intent=1, effect=1, check=42)

    # -- call contract -----------------------------------------------

    def test_inputs_must_be_keyword_arguments(self) -> None:
        with self.assertRaises(TypeError):
            self.engine.evaluate("hash:new", "hash:new", True)  # type: ignore[misc]

    def test_missing_input_raises(self) -> None:
        with self.assertRaises(TypeError):
            self.engine.evaluate(intent="hash:new", effect="hash:new")

    def test_engine_can_be_bound_at_construction(self) -> None:
        bound = VerdictEngine(intent="hash:new", effect="hash:old", check=True)
        self.assertIs(bound.evaluate(), Verdict.FAIL)

    def test_bound_values_can_be_overridden(self) -> None:
        bound = VerdictEngine(intent="hash:new", effect="hash:old", check=True)
        self.assertIs(
            bound.evaluate(effect="hash:new"), Verdict.PASS
        )

    # -- explain -----------------------------------------------------

    def test_explain_agrees_with_evaluate(self) -> None:
        cases = (
            {"intent": "n", "effect": "n", "check": True},
            {"intent": "n", "effect": "o", "check": True},
            {"intent": "n", "effect": "n", "check": False},
            {"intent": "n", "effect": None, "check": True},
            {"intent": None, "effect": "o", "check": False},
        )
        for case in cases:
            with self.subTest(case=case):
                verdict, reason = self.engine.explain(**case)
                self.assertIs(verdict, self.engine.evaluate(**case))
                self.assertIsInstance(reason, str)
                self.assertTrue(reason)

    def test_false_completion_reason_mentions_witness(self) -> None:
        _, reason = self.engine.explain(
            intent="hash:new", effect="hash:old", check=True
        )
        self.assertIn("false completion", reason)


if __name__ == "__main__":
    unittest.main()
