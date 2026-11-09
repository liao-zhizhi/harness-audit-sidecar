"""Tests for the witness-provenance caps.

Two properties carry the weight:

1. **A cap can only tighten.** PASS may become INVALID; FAIL, INVALID and
   NOT RUN must come out unchanged. A cap that softened a contradiction would
   be a way to argue with the witness.
2. **A cap is off unless declared.** Callers that pass no provenance keep the
   pre-existing behaviour, so adding the fields cannot silently change a
   verdict anywhere in the existing corpus.

The cases are built on a real ``RuleEngine`` with an empty judgment base, so
"no rule fired" is produced by the shipped composition rather than asserted.
"""

from __future__ import annotations

import tempfile
import unittest

from audit_sidecar.judgment_base import JudgmentBase
from audit_sidecar.rule_engine import RuleEngine
from audit_sidecar.verdict import Verdict, VerdictEngine
from audit_sidecar.witness_provenance import CAP_REASON_PREFIX, cap_verdict

# Everything agrees: a witness landed and the report says success. Without a
# cap this is a PASS, which is exactly the verdict the caps must be able to
# withhold.
AGREES = dict(intent=True, effect=True, check=True)


class CapTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-provenance-")
        # Empty directory => no rules for any tool => the fallback lattice
        # decides, then the cap applies on top of it.
        self.engine = RuleEngine(JudgmentBase(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    # -- case 3: self-report over a direct API path ---------------------

    def test_self_reported_direct_api_is_not_pass(self):
        decision = self.engine.judge(
            "write_file",
            producer="self_reported",
            topology="direct_api",
            **AGREES,
        )
        self.assertIsNot(decision.verdict, Verdict.PASS)
        self.assertIs(decision.verdict, Verdict.INVALID)
        self.assertIn(CAP_REASON_PREFIX, decision.reason)

    def test_self_reported_through_sidecar_still_passes(self):
        # A sidecar in the path means another observer exists, so the report is
        # not the only account. The cap must not fire here.
        decision = self.engine.judge(
            "write_file",
            producer="self_reported",
            topology="sidecar",
            **AGREES,
        )
        self.assertIs(decision.verdict, Verdict.PASS)

    def test_independent_witness_over_direct_api_still_passes(self):
        # The topology alone is not the problem; self-report alone is not the
        # problem. It is the conjunction.
        decision = self.engine.judge(
            "write_file",
            producer="independent",
            topology="direct_api",
            **AGREES,
        )
        self.assertIs(decision.verdict, Verdict.PASS)

    # -- program fact vs world fact -------------------------------------

    def test_program_fact_without_world_fact_is_not_pass(self):
        decision = self.engine.judge(
            "run_shell",
            program_fact=True,
            world_fact=False,
            **AGREES,
        )
        self.assertIsNot(decision.verdict, Verdict.PASS)
        self.assertIs(decision.verdict, Verdict.INVALID)
        self.assertIn(CAP_REASON_PREFIX, decision.reason)

    def test_program_fact_without_world_fact_declared_is_not_pass(self):
        # world_fact omitted is "not established", not "established".
        decision = self.engine.judge("run_shell", program_fact=True, **AGREES)
        self.assertIs(decision.verdict, Verdict.INVALID)

    def test_program_fact_with_independent_world_fact_passes(self):
        decision = self.engine.judge(
            "run_shell",
            program_fact=True,
            world_fact=True,
            **AGREES,
        )
        self.assertIs(decision.verdict, Verdict.PASS)

    # -- the cap may tighten, never loosen ------------------------------

    def test_cap_does_not_touch_fail(self):
        # Witness contradicts the intent. Softening FAIL to INVALID would let a
        # provenance declaration erase a contradiction.
        for kwargs in (
            dict(producer="self_reported", topology="direct_api"),
            dict(program_fact=True, world_fact=False),
            dict(
                producer="self_reported",
                topology="direct_api",
                program_fact=True,
                world_fact=False,
            ),
        ):
            decision = self.engine.judge(
                "write_file", intent=True, effect=False, check=True, **kwargs
            )
            self.assertIs(decision.verdict, Verdict.FAIL, kwargs)

    def test_cap_does_not_touch_not_run(self):
        decision = self.engine.judge(
            "write_file",
            intent=None,
            effect=False,
            check=True,
            producer="self_reported",
            topology="direct_api",
        )
        self.assertIs(decision.verdict, Verdict.NOT_RUN)

    def test_cap_does_not_touch_invalid(self):
        decision = self.engine.judge(
            "write_file",
            intent=True,
            effect=None,
            check=True,
            producer="self_reported",
            topology="direct_api",
        )
        self.assertIs(decision.verdict, Verdict.INVALID)

    # -- undeclared provenance is a no-op -------------------------------

    def test_no_provenance_keeps_lattice_behaviour(self):
        decision = self.engine.judge("write_file", **AGREES)
        self.assertIs(decision.verdict, Verdict.PASS)
        self.assertEqual(
            decision.verdict, VerdictEngine().evaluate(**AGREES)
        )

    def test_both_caps_name_themselves(self):
        verdict, reason = cap_verdict(
            Verdict.PASS,
            "witness matches intent, report agrees",
            producer="self_reported",
            topology="direct_api",
            program_fact=True,
            world_fact=False,
        )
        self.assertIs(verdict, Verdict.INVALID)
        self.assertIn("producer=self_reported", reason)
        self.assertIn("program_fact", reason)
        self.assertIn("witness matches intent", reason)

    # -- a typo must not silently disable the cap -----------------------

    def test_unknown_enum_raises(self):
        for kwargs in (
            dict(producer="self-reported"),
            dict(topology="direct-api"),
            dict(producer="independent", topology="proxy"),
        ):
            with self.assertRaises(ValueError, msg=kwargs):
                cap_verdict(Verdict.PASS, "", **kwargs)

    def test_unknown_producer_does_not_cap(self):
        # "unknown" is a declared absence of knowledge, not a self-report.
        verdict, _ = cap_verdict(
            Verdict.PASS, "", producer="unknown", topology="direct_api"
        )
        self.assertIs(verdict, Verdict.PASS)


class EngineIntegrationTestCase(unittest.TestCase):
    """The caps must be reachable from the entry points the runner uses."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-provenance-e2e-")
        self.engine = RuleEngine(JudgmentBase(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_explain_for_tool_applies_cap(self):
        # SafeRunner calls explain_for_tool; if the cap were only on judge(),
        # the runner path would silently stay green.
        decision = self.engine.explain_for_tool(
            "write_file",
            producer="self_reported",
            topology="direct_api",
            **AGREES,
        )
        self.assertIs(decision.verdict, Verdict.INVALID)

    def test_explain_applies_cap(self):
        verdict, _ = self.engine.explain(
            tool_name="write_file",
            producer="self_reported",
            topology="direct_api",
            **AGREES,
        )
        self.assertIs(verdict, Verdict.INVALID)

    def test_evaluate_applies_cap(self):
        verdict = self.engine.evaluate(
            "write_file",
            producer="self_reported",
            topology="direct_api",
            **AGREES,
        )
        self.assertIs(verdict, Verdict.INVALID)


if __name__ == "__main__":
    unittest.main()
