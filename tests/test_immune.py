"""Tests for conditional immune response.

Two halves, deliberately. The state machine and the tier policy are pure
functions over verdicts and rule dicts, so they are tested exhaustively and
instantly. The routing itself is tested against a real temporary git repository
with real commands, because the claim being made -- "the cheap tier still
catches a lie" -- is a claim about the physical world, and a stubbed runner
would only prove that the stub was called.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from audit_sidecar.immune_state import (
    ImmuneState,
    STATE_INFLAMED,
    STATE_NORMAL,
    TIER_ADAPTIVE,
    TIER_INNATE,
    select_tier,
)
from audit_sidecar.judgment_base import JudgmentBase, RuleError, packaged_rules_dir
from audit_sidecar.mcp_proxy import MCPProxy, ToolInvocation
from audit_sidecar.mcp_server import JsonRpcServer, TraceWriter
from audit_sidecar.runner import SafeRunner, WITNESS_SCOPE_STAT
from audit_sidecar.verdict import Verdict

GIT = shutil.which("git")
HAS_GIT = GIT is not None and shutil.which("bash") is not None


class ImmuneStateTest(unittest.TestCase):
    """The state machine, without any I/O."""

    def setUp(self) -> None:
        self.immune = ImmuneState()

    def test_starts_normal(self) -> None:
        self.assertEqual(self.immune.state, STATE_NORMAL)
        self.assertFalse(self.immune.is_inflamed())
        self.assertEqual(self.immune.danger_count, 0)

    def test_one_failure_is_not_inflammation(self) -> None:
        # An occasional failure surrounded by verified work is a normal day.
        self.assertEqual(self.immune.record_verdict(Verdict.FAIL), STATE_NORMAL)
        self.assertEqual(self.immune.danger_count, 1)

    def test_two_consecutive_failures_inflame(self) -> None:
        self.immune.record_verdict(Verdict.FAIL)
        self.assertEqual(self.immune.record_verdict(Verdict.FAIL), STATE_INFLAMED)
        self.assertTrue(self.immune.is_inflamed())

    def test_a_pass_resets_the_failure_streak(self) -> None:
        self.immune.record_verdict(Verdict.FAIL)
        self.immune.record_verdict(Verdict.PASS)
        self.assertEqual(self.immune.danger_count, 0)
        self.immune.record_verdict(Verdict.FAIL)
        self.assertFalse(self.immune.is_inflamed())

    def test_one_invalid_inflames_immediately(self) -> None:
        # INVALID means "we could not see". A gate that cannot see is already
        # not doing its job, so it gets a lower threshold than a contradiction.
        self.assertEqual(self.immune.record_verdict(Verdict.INVALID), STATE_INFLAMED)

    def test_not_run_is_not_evidence_of_health(self) -> None:
        self.immune.record_verdict(Verdict.INVALID)
        for _ in range(10):
            self.immune.record_verdict(Verdict.NOT_RUN)
        self.assertTrue(self.immune.is_inflamed())
        self.assertEqual(self.immune.recovery_progress, 0)

    def test_four_passes_do_not_heal(self) -> None:
        self.immune.record_verdict(Verdict.INVALID)
        for _ in range(4):
            self.immune.record_verdict(Verdict.PASS)
        self.assertTrue(self.immune.is_inflamed())
        self.assertEqual(self.immune.recovery_progress, 4)

    def test_five_passes_heal(self) -> None:
        self.immune.record_verdict(Verdict.INVALID)
        for _ in range(5):
            self.immune.record_verdict(Verdict.PASS)
        self.assertFalse(self.immune.is_inflamed())
        self.assertEqual(self.immune.state, STATE_NORMAL)
        self.assertEqual(self.immune.danger_count, 0)

    def test_a_failure_during_recovery_restarts_the_count(self) -> None:
        # Healing is deliberately slower than injury: one contradiction inside
        # the recovery window throws away the progress.
        self.immune.record_verdict(Verdict.INVALID)
        for _ in range(4):
            self.immune.record_verdict(Verdict.PASS)
        self.immune.record_verdict(Verdict.FAIL)
        self.assertEqual(self.immune.recovery_progress, 0)
        self.assertTrue(self.immune.is_inflamed())
        for _ in range(4):
            self.immune.record_verdict(Verdict.PASS)
        self.assertTrue(self.immune.is_inflamed())

    def test_custom_thresholds_are_honoured(self) -> None:
        immune = ImmuneState(fail_threshold=3, recovery_passes=6)
        immune.record_verdict(Verdict.FAIL)
        immune.record_verdict(Verdict.FAIL)
        self.assertFalse(immune.is_inflamed())
        immune.record_verdict(Verdict.FAIL)
        self.assertTrue(immune.is_inflamed())
        for _ in range(5):
            immune.record_verdict(Verdict.PASS)
        self.assertTrue(immune.is_inflamed())
        immune.record_verdict(Verdict.PASS)
        self.assertFalse(immune.is_inflamed())

    def test_healing_may_not_be_faster_than_injury(self) -> None:
        with self.assertRaises(ValueError):
            ImmuneState(fail_threshold=4, recovery_passes=2)

    def test_thresholds_must_be_positive_integers(self) -> None:
        for keyword in ("fail_threshold", "invalid_threshold", "recovery_passes"):
            for value in (0, -1):
                with self.subTest(keyword=keyword, value=value):
                    with self.assertRaises(ValueError):
                        ImmuneState(**{keyword: value})
            for value in (True, 2.0, "2"):
                with self.subTest(keyword=keyword, value=value):
                    with self.assertRaises(TypeError):
                        ImmuneState(**{keyword: value})

    def test_record_verdict_rejects_non_verdicts(self) -> None:
        for value in ("FAIL", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    self.immune.record_verdict(value)  # type: ignore[arg-type]

    def test_reset_returns_to_normal(self) -> None:
        self.immune.record_verdict(Verdict.INVALID)
        self.immune.reset()
        self.assertEqual(self.immune.state, STATE_NORMAL)
        self.assertEqual(self.immune.danger_count, 0)

    def test_snapshot_is_json_friendly(self) -> None:
        self.immune.record_verdict(Verdict.FAIL)
        payload = json.loads(json.dumps(self.immune.snapshot()))
        self.assertEqual(payload["state"], STATE_NORMAL)
        self.assertEqual(payload["danger_count"], 1)
        self.assertEqual(payload["recovery_passes"], 5)

    def test_repr_names_the_state(self) -> None:
        self.assertIn(STATE_NORMAL, repr(self.immune))


class SelectTierTest(unittest.TestCase):
    """The routing policy, as a pure table."""

    def test_no_policy_means_the_expensive_tier(self) -> None:
        # The dangerous default would be the other way round: adding a rules
        # directory would then weaken the audit for every tool it forgot.
        self.assertEqual(
            select_tier(rules=[], inflamed=False), TIER_ADAPTIVE
        )

    def test_declared_low_risk_earns_the_cheap_tier(self) -> None:
        rules = [{"rule_id": "r", "risk_level": "low", "require_witness": False}]
        self.assertEqual(select_tier(rules=rules, inflamed=False), TIER_INNATE)

    def test_omitted_risk_level_means_low(self) -> None:
        rules = [{"rule_id": "r", "risk_level": None, "require_witness": False}]
        self.assertEqual(select_tier(rules=rules, inflamed=False), TIER_INNATE)

    def test_high_risk_forces_the_expensive_tier(self) -> None:
        rules = [{"rule_id": "r", "risk_level": "high", "require_witness": False}]
        self.assertEqual(select_tier(rules=rules, inflamed=False), TIER_ADAPTIVE)

    def test_one_high_risk_rule_escalates_everything(self) -> None:
        rules = [
            {"rule_id": "a", "risk_level": "low"},
            {"rule_id": "b", "risk_level": "high"},
        ]
        self.assertEqual(select_tier(rules=rules, inflamed=False), TIER_ADAPTIVE)

    def test_a_required_witness_escalates_even_when_low(self) -> None:
        # A stat cannot satisfy a rule that demands the git change set, so
        # routing it to the cheap tier would turn it into a permanent INVALID.
        rules = [{"rule_id": "r", "risk_level": "low", "require_witness": True}]
        self.assertEqual(select_tier(rules=rules, inflamed=False), TIER_ADAPTIVE)

    def test_inflammation_overrides_declared_low_risk(self) -> None:
        rules = [{"rule_id": "r", "risk_level": "low", "require_witness": False}]
        self.assertEqual(select_tier(rules=rules, inflamed=True), TIER_ADAPTIVE)

    def test_nothing_to_stat_falls_back_to_the_full_tier(self) -> None:
        # A cheap tier with no path to stat could only produce INVALID, which
        # would then inflame the system on its own absence of evidence.
        rules = [{"rule_id": "r", "risk_level": "low"}]
        self.assertEqual(
            select_tier(rules=rules, inflamed=False, has_observable_path=False),
            TIER_ADAPTIVE,
        )

    def test_arguments_are_type_checked(self) -> None:
        with self.assertRaises(TypeError):
            select_tier(rules="low", inflamed=False)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            select_tier(rules=[], inflamed="no")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            select_tier(rules=["rule"], inflamed=False)  # type: ignore[list-item]
        with self.assertRaises(TypeError):
            select_tier(
                rules=[], inflamed=False, has_observable_path=1  # type: ignore[arg-type]
            )


class StatWitnessTest(unittest.TestCase):
    """The innate witness itself, at the runner boundary."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-stat-")
        self.root = self._tmp.name
        self.runner = SafeRunner(timeout=20)
        with open(os.path.join(self.root, "old.txt"), "w", encoding="utf-8") as handle:
            handle.write("before\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_content_assertion_is_refused(self) -> None:
        # Dropping the check silently would turn the caller's assertion into a
        # weaker one without telling them.
        with self.assertRaises(ValueError):
            self.runner.run_shell(
                "printf 'x' > old.txt",
                cwd=self.root,
                intent_desc="rewrite old.txt",
                expected_file="old.txt",
                expected_content="x",
                witness_scope=WITNESS_SCOPE_STAT,
            )

    def test_unknown_scope_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.runner.run_shell(
                "true",
                cwd=self.root,
                intent_desc="nothing",
                witness_scope="cheap",
            )

    def test_modification_is_observed(self) -> None:
        result = self.runner.run_shell(
            "printf 'after\\n' > old.txt",
            cwd=self.root,
            intent_desc="rewrite old.txt",
            expected_file="old.txt",
            witness_scope=WITNESS_SCOPE_STAT,
        )
        self.assertTrue(result.effect_observed)
        self.assertEqual(result.witness_scope, WITNESS_SCOPE_STAT)
        self.assertEqual(result.tier, TIER_INNATE)
        self.assertIsNotNone(result.witness_mtime)
        self.assertIsNone(result.git_repo)

    def test_untouched_file_is_not_observed_as_modified(self) -> None:
        result = self.runner.run_shell(
            "true",
            cwd=self.root,
            intent_desc="pretend to rewrite old.txt",
            expected_file="old.txt",
            witness_scope=WITNESS_SCOPE_STAT,
        )
        self.assertFalse(result.effect_observed)
        self.assertEqual(result.missing_expected, ("old.txt",))

    def test_creation_counts_as_modification(self) -> None:
        result = self.runner.run_shell(
            "printf 'new\\n' > new.txt",
            cwd=self.root,
            intent_desc="create new.txt",
            expected_file="new.txt",
            witness_scope=WITNESS_SCOPE_STAT,
        )
        self.assertTrue(result.effect_observed)
        self.assertEqual(result.changed_files, ("new.txt",))

    def test_unexpected_changes_are_not_claimed(self) -> None:
        # Nothing surveys the tree in this tier, so reporting an empty set is
        # the only honest answer; the empty set means "not looked", not "clean".
        result = self.runner.run_shell(
            "printf 'x' > old.txt && printf 'y' > drift.txt",
            cwd=self.root,
            intent_desc="rewrite old.txt",
            expected_file="old.txt",
            witness_scope=WITNESS_SCOPE_STAT,
        )
        self.assertTrue(result.effect_observed)
        self.assertEqual(result.unexpected_changes, ())


@unittest.skipUnless(HAS_GIT, "git and bash are required for routing tests")
class ProxyTierTest(unittest.TestCase):
    """Routing through the real proxy, against a real repository."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-immune-")
        self.root = self._tmp.name
        self.git("init", "-q")
        self.write("target.py", "value = 0\n")
        self.write("log.txt", "session\n")
        self.git("add", "-A")
        self.commit("initial")
        self.rules_dir = packaged_rules_dir()
        self.proxy = MCPProxy(
            cwd=self.root, runner=SafeRunner(timeout=20), rules_dir=self.rules_dir
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------

    def git(self, *args: str) -> str:
        return subprocess.run(
            [GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def commit(self, message: str) -> None:
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-q", "-m", message,
        )

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def write(self, name: str, content: str) -> None:
        with open(self.path(name), "w", encoding="utf-8") as handle:
            handle.write(content)

    def append(self, proxy: MCPProxy, name: str = "log.txt") -> dict:
        return proxy.intercept_tool_call(
            "append_file", {"path": name, "content": "more\n"}, True
        )

    def lie(self, proxy: MCPProxy) -> None:
        """A false completion: report success writing into a missing directory."""
        proxy.intercept_tool_call(
            "write_file", {"path": "missing_dir/target.py", "content": "x\n"}, True
        )

    def rule_dir(self, body: str) -> str:
        """A judgment base with one rule, written before the proxy loads it."""
        directory = tempfile.mkdtemp(prefix="harness-risk-")
        self.addCleanup(shutil.rmtree, directory, True)
        with open(os.path.join(directory, "r.yaml"), "w", encoding="utf-8") as handle:
            handle.write(body)
        return directory

    def rules_for(self, target_tool: str, risk: str) -> str:
        """A one-rule judgment base, to exercise both tiers on the same tool."""
        return self.rule_dir(
            'rule_id: "only"\ntarget_tool: "{}"\n'
            'risk_level: "{}"\nexpectation: "file_modified"\n'.format(
                target_tool, risk
            )
        )

    def rewrite_tool(self, proxy: MCPProxy) -> None:
        """A tool that names a *change set*, so tier 2 must run git.

        The payload differs on every call: an identical rewrite produces no git
        diff, and "nothing changed" would be reported as a missing expectation
        rather than as the pass it looks like.
        """

        counter = {"n": 0}

        def builder(_args):
            counter["n"] += 1
            return ToolInvocation(
                command="printf 'rewrite {}' > log.txt".format(counter["n"]),
                intent="rewrite log.txt",
                expected_files=("log.txt",),
            )

        proxy.register_tool("rewrite_log", builder)

    # -- tier selection -----------------------------------------------

    def test_low_risk_append_uses_the_innate_tier(self) -> None:
        self.append(self.proxy)
        result = self.proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertEqual(result.tier, TIER_INNATE)
        self.assertEqual(result.witness_scope, WITNESS_SCOPE_STAT)
        self.assertIsNone(result.git_repo)
        self.assertIn("innate tier", result.reason)

    def test_innate_tier_never_constructs_a_git_witness(self) -> None:
        # The saving is precisely this: no git process, no snapshot, no diff.
        with mock.patch(
            "audit_sidecar.runner.GitWitness", side_effect=AssertionError("git ran")
        ):
            self.append(self.proxy)
        self.assertIs(self.proxy.audit_log[-1].verdict, Verdict.PASS)

    def test_innate_tier_still_catches_false_completion(self) -> None:
        self.proxy.intercept_tool_call(
            "append_file", {"path": "missing_dir/log.txt", "content": "x\n"}, True
        )
        result = self.proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertIn("false completion", result.reason)

    def test_high_risk_write_uses_the_adaptive_tier(self) -> None:
        self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "value = 1\n"}, True
        )
        result = self.proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertEqual(result.tier, TIER_ADAPTIVE)
        self.assertEqual(result.witness_scope, "full")
        self.assertIsNotNone(result.git_repo)
        self.assertEqual(result.changed_files, ("target.py",))

    def test_no_rules_at_all_keeps_the_full_witness(self) -> None:
        proxy = MCPProxy(cwd=self.root, runner=SafeRunner(timeout=20))
        proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "value = 2\n"}, True
        )
        result = proxy.audit_log[-1]
        self.assertEqual(result.tier, TIER_ADAPTIVE)
        self.assertIsNotNone(result.git_repo)

    def test_adaptive_tier_sees_drift_the_innate_tier_cannot(self) -> None:
        # The documented blind spot, stated as a test rather than a caveat. This
        # is why overwriting is declared high risk in the shipped policy.
        def writes_two_files(_args):
            return ToolInvocation(
                command="printf 'a' > target.py && printf 'b' > extra.py",
                intent="write target.py",
                expected_files=("target.py",),
            )

        low = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=self.rules_for("write_two", "low"),
        )
        low.register_tool("write_two", writes_two_files)
        low.intercept_tool_call("write_two", {}, True)
        self.assertIs(low.audit_log[-1].verdict, Verdict.PASS)
        self.assertEqual(low.audit_log[-1].tier, TIER_INNATE)
        self.git("checkout", "--", ".")
        self.git("clean", "-qfd")

        high = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=self.rules_for("write_two", "high"),
        )
        high.register_tool("write_two", writes_two_files)
        high.intercept_tool_call("write_two", {}, True)
        result = high.audit_log[-1]
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertEqual(result.unexpected_changes, ("extra.py",))

    # -- inflammation -------------------------------------------------

    def test_inflammation_escalates_a_low_risk_call(self) -> None:
        self.lie(self.proxy)
        self.lie(self.proxy)
        self.assertTrue(self.proxy.immune_state.is_inflamed())

        self.append(self.proxy)
        result = self.proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.PASS)
        # Same tool, same policy, different tier: the escalation is the ledger's
        # doing, not the rule's.
        self.assertEqual(result.tier, TIER_ADAPTIVE)
        self.assertEqual(result.witness_scope, "full")
        self.assertNotIn("innate tier", result.reason)

    def test_escalation_actually_buys_the_git_change_set(self) -> None:
        proxy = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=self.rules_for("rewrite_log", "low"),
        )
        self.rewrite_tool(proxy)
        proxy.intercept_tool_call("rewrite_log", {}, True)
        calm = proxy.audit_log[-1]
        self.assertEqual(calm.tier, TIER_INNATE)
        self.assertIsNone(calm.git_repo)

        proxy.immune_state.record_verdict(Verdict.FAIL)
        proxy.immune_state.record_verdict(Verdict.FAIL)
        proxy.intercept_tool_call("rewrite_log", {}, True)
        hot = proxy.audit_log[-1]
        self.assertEqual(hot.tier, TIER_ADAPTIVE)
        self.assertIsNotNone(hot.git_repo)
        self.assertEqual(hot.changed_files, ("log.txt",))

    def test_first_failure_alone_does_not_escalate(self) -> None:
        self.lie(self.proxy)
        self.append(self.proxy)
        self.assertEqual(self.proxy.audit_log[-1].tier, TIER_INNATE)

    def test_five_clean_passes_restore_the_cheap_tier(self) -> None:
        self.lie(self.proxy)
        self.lie(self.proxy)
        for _ in range(4):
            self.append(self.proxy)
        self.assertTrue(self.proxy.immune_state.is_inflamed())
        self.assertEqual(self.proxy.audit_log[-1].tier, TIER_ADAPTIVE)

        self.append(self.proxy)  # the fifth
        self.assertFalse(self.proxy.immune_state.is_inflamed())
        self.append(self.proxy)
        self.assertEqual(self.proxy.audit_log[-1].tier, TIER_INNATE)

    def test_an_unwitnessable_call_inflames_on_the_first_attempt(self) -> None:
        # A rule demanding a witness the runner cannot collect yields INVALID,
        # and INVALID is exactly the state a gate must not sit in quietly: one
        # occurrence is enough to widen the aperture.
        strict = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=self.rule_dir(
                'rule_id: "needs_git"\ntarget_tool: "write_note"\n'
                'risk_level: "high"\nrequire_witness: true\n'
                'witness_type: "git_diff"\n'
            ),
        )

        def file_only(_args):
            # Names an artefact but no change set, so the demanded git witness
            # can never be collected.
            return ToolInvocation(
                command="printf 'note' > note.txt",
                intent="write note.txt",
                expected_file="note.txt",
            )

        strict.register_tool("write_note", file_only)
        strict.intercept_tool_call("write_note", {}, True)
        self.assertIs(strict.audit_log[-1].verdict, Verdict.INVALID)
        self.assertTrue(strict.immune_state.is_inflamed())

    def test_injected_state_is_shared_with_the_caller(self) -> None:
        immune = ImmuneState()
        proxy = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=self.rules_dir,
            immune_state=immune,
        )
        self.assertIs(proxy.immune_state, immune)
        immune.record_verdict(Verdict.INVALID)
        self.append(proxy)
        self.assertEqual(proxy.audit_log[-1].tier, TIER_ADAPTIVE)

    def test_immune_state_type_is_checked(self) -> None:
        with self.assertRaises(TypeError):
            MCPProxy(cwd=self.root, immune_state=object())  # type: ignore[arg-type]

    def test_response_does_not_leak_the_tier(self) -> None:
        self.lie(self.proxy)
        self.lie(self.proxy)
        payload = json.dumps(self.append(self.proxy))
        self.assertNotIn("tier", payload)
        self.assertNotIn("INFLAMED", payload)


@unittest.skipUnless(HAS_GIT, "git and bash are required for trace tests")
class TraceTest(unittest.TestCase):
    """The record has to say which check produced the verdict."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-trace-")
        self.root = self._tmp.name
        self.trace_path = os.path.join(self.root, "audit_trace.jsonl")
        self.git("init", "-q")
        with open(os.path.join(self.root, "log.txt"), "w", encoding="utf-8") as handle:
            handle.write("session\n")
        self.git("add", "-A")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-q", "-m", "initial",
        )
        self.proxy = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=packaged_rules_dir(),
        )
        self.server = JsonRpcServer(
            self.proxy, TraceWriter(self.trace_path), stdout=io.StringIO()
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            [GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def call(self, message_id: int, tool: str, args: dict, reported: bool) -> None:
        self.server.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "tool_call",
                    "params": {
                        "tool": tool,
                        "args": args,
                        "reported_success": reported,
                    },
                    "id": message_id,
                }
            )
        )

    def records(self) -> list:
        with open(self.trace_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_record_carries_tier_and_state(self) -> None:
        self.call(1, "append_file", {"path": "log.txt", "content": "one\n"}, True)
        record = self.records()[-1]
        self.assertEqual(record["tier"], TIER_INNATE)
        self.assertEqual(record["witness_scope"], WITNESS_SCOPE_STAT)
        self.assertIsNotNone(record["witness_mtime"])
        self.assertEqual(record["immune_state"], STATE_NORMAL)

    def test_escalation_is_visible_in_the_trace(self) -> None:
        for message_id in (1, 2):
            self.call(
                message_id,
                "append_file",
                {"path": "no_such_dir/log.txt", "content": "x\n"},
                True,
            )
        self.call(3, "append_file", {"path": "log.txt", "content": "two\n"}, True)

        first, second, third = self.records()
        self.assertEqual(first["verdict"], "FAIL")
        self.assertEqual(first["immune_state"], STATE_NORMAL)
        self.assertEqual(second["verdict"], "FAIL")
        self.assertEqual(second["immune_state"], STATE_INFLAMED)
        # The third call is the same tool and the same policy as the first, and
        # lands on the expensive tier because of the two above it.
        self.assertEqual(third["tier"], TIER_ADAPTIVE)
        self.assertEqual(third["witness_scope"], "full")
        self.assertEqual(third["verdict"], "PASS")


class ShippedPolicyTest(unittest.TestCase):
    """The default policy is where the tiers actually take effect."""

    def setUp(self) -> None:
        self.base = JudgmentBase(packaged_rules_dir())

    def test_overwriting_is_declared_high_risk(self) -> None:
        rules = self.base.get_rules_for_tool("write_file")
        self.assertEqual(rules[0]["risk_level"], "high")

    def test_appending_is_declared_low_risk(self) -> None:
        rules = self.base.get_rules_for_tool("append_file")
        self.assertEqual(rules[0]["risk_level"], "low")
        self.assertFalse(rules[0]["require_witness"])

    def test_unsupported_risk_level_is_rejected(self) -> None:
        directory = tempfile.mkdtemp(prefix="harness-risk-")
        self.addCleanup(shutil.rmtree, directory, True)
        with open(os.path.join(directory, "r.yaml"), "w", encoding="utf-8") as handle:
            handle.write('rule_id: "r"\ntarget_tool: "t"\nrisk_level: "medium"\n')
        with self.assertRaises(RuleError) as caught:
            JudgmentBase(directory)
        self.assertIn("risk_level", str(caught.exception))

    def test_risk_level_is_carried_into_the_public_rule_shape(self) -> None:
        rules = self.base.get_rules_for_tool("write_file")
        self.assertIn("risk_level", rules[0])


if __name__ == "__main__":
    unittest.main()
