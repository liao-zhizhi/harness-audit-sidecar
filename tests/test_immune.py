"""Tests for conditional immune response.

Three halves, deliberately. The state machine and the tier policy are pure
functions over verdicts and rule dicts, so they are tested exhaustively and
instantly. The routing is tested against a real temporary git repository with
real commands, because the claim being made -- "the cheap tier still catches a
lie" -- is a claim about the physical world, and a stubbed runner would only
prove that the stub was called. Persistence is tested by killing a real server
process and starting another one, because "the state survives a restart" is the
whole claim and an in-process object cannot demonstrate it.
"""

from __future__ import annotations

import atexit
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# These tests drive the CLI, and the CLI now writes an append-only usage log.
# Point it at a throwaway directory so running the suite never touches the
# developer's ~/.gradia/usage.jsonl -- a test that edits the machine it runs on
# is a test that has to be undone by hand.
_USAGE_DIR = tempfile.mkdtemp(prefix="harness-usage-")
os.environ["HARNESS_AUDIT_USAGE_DIR"] = _USAGE_DIR
atexit.register(shutil.rmtree, _USAGE_DIR, True)

from audit_sidecar.immune_state import (
    ImmuneState,
    STATE_FILENAME,
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

    def test_innate_payload_assertion_is_recorded_not_silent(self) -> None:
        # ``append_file`` asserts the content it appends, but the cheap tier
        # never reads the file. The drop must be on the result and in the
        # reason: a PASS about "the append landed" is not a PASS about the
        # bytes, and only the record can tell the two apart.
        self.append(self.proxy)
        result = self.proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertEqual(result.tier, TIER_INNATE)
        self.assertEqual(result.dropped_assertions, ("expected_content",))
        self.assertIn("content assertion dropped", result.reason)
        self.assertIn("tier=innate", result.reason)

    def test_content_assertion_the_innate_tier_cannot_read_escalates(self) -> None:
        # The exploit: a custom builder asserts EXPECTED-MARKER while its
        # command writes WRONG, under a policy that routes the tool to the
        # cheap tier. Dropping the assertion would produce a PASS about a file
        # that does not contain the marker. Escalating collects the evidence
        # instead, and the full tier sees the mismatch.
        proxy = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=self.rules_for("append_note", "low"),
        )

        def builder(_args):
            return ToolInvocation(
                command="printf 'WRONG' >> note.txt",
                intent="append EXPECTED-MARKER to note.txt",
                expected_file="note.txt",
                expected_content="EXPECTED-MARKER",
            )

        proxy.register_tool("append_note", builder)
        proxy.intercept_tool_call("append_note", {}, True)
        result = proxy.audit_log[-1]
        self.assertEqual(result.tier, TIER_ADAPTIVE)
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertEqual(result.dropped_assertions, ())

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


@unittest.skipUnless(shutil.which("bash"), "bash is required to run tool commands")
class EscalationOutsideGitTest(unittest.TestCase):
    """Escalation in a directory with no repository: the pairing matters.

    A content assertion promotes a low-risk call to the full witness. Whether
    that is answerable outside git depends on *what* the assertion needs: a
    change set needs a repository, a content read does not. So both directions
    are pinned here -- a claimed change set with no repository is ``INVALID``
    (we did not look), and a matching content read with no repository is
    ``PASS`` (we looked and it was right). Testing only the first invites the
    over-correction that "non-git" means "refuse", which would make the second
    a refusal to report evidence that was actually collected.

    The two halves of the underlying fixes are tested elsewhere -- H2
    escalates in a git repo and reaches FAIL, M6 answers INVALID in a non-git
    directory for a plain change set -- and neither says anything about the
    path where one causes the other.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-escalate-nogit-")
        self.root = self._tmp.name
        # Deliberately no ``git init``: this is the scratch directory a first
        # run uses, which is exactly the case M6 is about.
        self.rules_dir = tempfile.mkdtemp(prefix="harness-escalate-rules-")
        self.addCleanup(shutil.rmtree, self.rules_dir, True)
        with open(
            os.path.join(self.rules_dir, "low.yaml"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                'rule_id: "low"\ntarget_tool: "append_note"\n'
                'risk_level: "low"\nexpectation: "file_modified"\n'
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_non_payload_assertion_in_a_non_git_directory_is_invalid(self) -> None:
        proxy = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=self.rules_dir,
        )

        def builder(_args):
            return ToolInvocation(
                command="printf 'WRONG' >> note.txt",
                intent="append EXPECTED-MARKER to note.txt",
                expected_file="note.txt",
                # The change set is what makes the full witness need git. At
                # ``risk_level: low`` this alone would still be judged by the
                # innate tier (one stat, no repository); it is the content
                # assertion below that promotes the call to the full witness,
                # and that promotion is the thing under test.
                expected_files=("note.txt",),
                expected_content="EXPECTED-MARKER",
            )

        proxy.register_tool("append_note", builder)
        # Must not raise: an ordinary first run in a scratch directory is not
        # an incident.
        response = proxy.intercept_tool_call("append_note", {}, True)
        result = proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.INVALID)
        self.assertIsNone(result.effect_observed)
        # It really was promoted: the tier is derived from the witness scope
        # that was collected, so this is the policy taking effect and not a
        # coincidence of the directory.
        self.assertEqual(result.tier, TIER_ADAPTIVE)
        self.assertEqual(result.witness_scope, "full")
        # The assertion was not dropped, it was attempted and could not be
        # observed; those are different claims about the run.
        self.assertEqual(result.dropped_assertions, ())
        self.assertIn("no independent witness", result.reason)
        self.assertIn("git", result.reason)
        # The command never ran: without a baseline there is no way to tell
        # its effect from dirt that was already there.
        self.assertEqual(os.listdir(self.root), [])
        # And the proxy stays passive: the agent still sees what the tool
        # layer reported.
        self.assertEqual(response["status"], "ok")

    def test_matching_assertion_without_a_change_set_is_a_pass(self) -> None:
        # The mirror of the test above, and the reason it exists: the failure
        # being avoided is not "non-git directories are suspicious" but "we did
        # not look". Here the full witness *can* look -- the content read needs
        # no repository -- so the answer is a real PASS. Anyone who later
        # over-corrects the previous case into "non-git -> INVALID" would turn
        # this one into "we looked and saw it was right, but refuse to say so",
        # which is the same receipt defect in the other direction.
        proxy = MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=self.rules_dir,
        )

        def builder(_args):
            return ToolInvocation(
                command="printf 'EXPECTED-MARKER' >> note.txt",
                intent="append EXPECTED-MARKER to note.txt",
                expected_file="note.txt",
                # Deliberately no ``expected_files``: without a change set the
                # full witness never constructs a GitWitness, so a missing
                # repository is not a missing witness.
                expected_content="EXPECTED-MARKER",
            )

        proxy.register_tool("append_note", builder)
        proxy.intercept_tool_call("append_note", {}, True)
        result = proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.PASS)
        # Promoted by the content assertion (rather than answered on a stat),
        # and collecting no change set, because none was claimed.
        self.assertEqual(result.tier, TIER_ADAPTIVE)
        self.assertEqual(result.witness_scope, "full")
        self.assertIs(result.effect_observed, True)
        self.assertIsNone(result.git_repo)
        self.assertEqual(result.dropped_assertions, ())
        self.assertIn("witness matches intent", result.reason)


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


class PersistenceTest(unittest.TestCase):
    """The state file: written after every verdict, read at construction."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-state-")
        self.root = self._tmp.name
        self.state_path = os.path.join(self.root, STATE_FILENAME)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def read(self) -> dict:
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)

    def write_raw(self, text: str) -> None:
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_inflammation_survives_a_new_instance(self) -> None:
        # The whole point: a restart must not be a way to wash the record.
        first = ImmuneState(state_path=self.state_path)
        first.record_verdict(Verdict.FAIL)
        first.record_verdict(Verdict.FAIL)
        self.assertTrue(first.is_inflamed())

        restarted = ImmuneState(state_path=self.state_path)
        self.assertTrue(restarted.is_inflamed())
        self.assertEqual(restarted.state, STATE_INFLAMED)
        self.assertEqual(restarted.danger_count, 2)

    def test_recovery_progress_survives_too(self) -> None:
        first = ImmuneState(state_path=self.state_path)
        first.record_verdict(Verdict.INVALID)
        for _ in range(3):
            first.record_verdict(Verdict.PASS)

        restarted = ImmuneState(state_path=self.state_path)
        self.assertTrue(restarted.is_inflamed())
        self.assertEqual(restarted.recovery_progress, 3)
        # Two more passes finish the job the previous process started.
        restarted.record_verdict(Verdict.PASS)
        restarted.record_verdict(Verdict.PASS)
        self.assertFalse(restarted.is_inflamed())

    def test_healed_state_survives_as_normal(self) -> None:
        first = ImmuneState(state_path=self.state_path)
        first.record_verdict(Verdict.INVALID)
        for _ in range(5):
            first.record_verdict(Verdict.PASS)
        self.assertFalse(first.is_inflamed())

        restarted = ImmuneState(state_path=self.state_path)
        self.assertFalse(restarted.is_inflamed())
        self.assertEqual(restarted.state, STATE_NORMAL)
        self.assertEqual(restarted.danger_count, 0)

    def test_missing_file_is_an_ordinary_first_run(self) -> None:
        self.assertFalse(os.path.exists(self.state_path))
        state = ImmuneState(state_path=self.state_path)
        self.assertFalse(state.is_inflamed())
        # Construction alone must not create the file: a proxy that has not
        # judged anything has nothing to remember.
        self.assertFalse(os.path.exists(self.state_path))

    def test_file_is_written_after_the_first_verdict(self) -> None:
        state = ImmuneState(state_path=self.state_path)
        state.record_verdict(Verdict.PASS)
        self.assertTrue(os.path.exists(self.state_path))
        document = self.read()
        self.assertEqual(document["state"], STATE_NORMAL)
        self.assertEqual(document["version"], 1)

    def test_not_run_writes_nothing(self) -> None:
        # Nothing ran, so there is nothing new to remember; rewriting the file
        # on every skipped call would be work with no content behind it.
        state = ImmuneState(state_path=self.state_path)
        state.record_verdict(Verdict.PASS)
        before = os.stat(self.state_path).st_mtime_ns
        state.record_verdict(Verdict.NOT_RUN)
        self.assertEqual(os.stat(self.state_path).st_mtime_ns, before)

    def test_corrupt_json_falls_back_to_normal_and_says_so(self) -> None:
        self.write_raw("{not json at all")
        with self.assertLogs("audit_sidecar.immune_state", level="ERROR") as logs:
            state = ImmuneState(state_path=self.state_path)
        self.assertFalse(state.is_inflamed())
        # Reported as an amnesia event, not as a clean start: silence here
        # would make deliberate corruption indistinguishable from first use.
        self.assertIn("amnesia", " ".join(logs.output))

    def test_unknown_version_is_rejected_loudly(self) -> None:
        self.write_raw(json.dumps({"version": 99, "state": "INFLAMED"}))
        with self.assertLogs("audit_sidecar.immune_state", level="ERROR") as logs:
            state = ImmuneState(state_path=self.state_path)
        self.assertFalse(state.is_inflamed())
        self.assertIn("amnesia", " ".join(logs.output))

    def test_unknown_state_value_is_rejected(self) -> None:
        self.write_raw(json.dumps({"version": 1, "state": "FINE"}))
        with self.assertLogs("audit_sidecar.immune_state", level="ERROR"):
            state = ImmuneState(state_path=self.state_path)
        self.assertFalse(state.is_inflamed())

    def test_negative_counters_are_rejected(self) -> None:
        self.write_raw(
            json.dumps(
                {
                    "version": 1,
                    "state": "NORMAL",
                    "danger_count": -1,
                    "recovery_progress": 0,
                }
            )
        )
        with self.assertLogs("audit_sidecar.immune_state", level="ERROR"):
            state = ImmuneState(state_path=self.state_path)
        self.assertEqual(state.danger_count, 0)

    def test_forged_recovery_counter_cannot_skip_the_wait(self) -> None:
        # A record claiming INFLAMED with a full recovery streak would heal on
        # the very next pass. Clamped to one short, so the wait is still owed.
        self.write_raw(
            json.dumps(
                {
                    "version": 1,
                    "state": "INFLAMED",
                    "danger_count": 2,
                    "recovery_progress": 99,
                }
            )
        )
        state = ImmuneState(state_path=self.state_path)
        self.assertTrue(state.is_inflamed())
        self.assertEqual(state.recovery_progress, 4)
        state.record_verdict(Verdict.PASS)
        self.assertFalse(state.is_inflamed())

    def test_inflamed_with_zero_danger_stays_inflamed(self) -> None:
        # Inconsistent but not dangerous to trust: keep the suspicion.
        self.write_raw(
            json.dumps(
                {
                    "version": 1,
                    "state": "INFLAMED",
                    "danger_count": 0,
                    "recovery_progress": 0,
                }
            )
        )
        state = ImmuneState(state_path=self.state_path)
        self.assertTrue(state.is_inflamed())

    def test_normal_with_a_stale_streak_is_clamped(self) -> None:
        self.write_raw(
            json.dumps(
                {
                    "version": 1,
                    "state": "NORMAL",
                    "danger_count": 0,
                    "recovery_progress": 3,
                }
            )
        )
        state = ImmuneState(state_path=self.state_path)
        self.assertFalse(state.is_inflamed())
        self.assertEqual(state.recovery_progress, 0)

    def test_write_is_atomic_and_leaves_no_temporaries(self) -> None:
        state = ImmuneState(state_path=self.state_path)
        for _ in range(3):
            state.record_verdict(Verdict.PASS)
        leftovers = [n for n in os.listdir(self.root) if n.startswith(".immune-")]
        self.assertEqual(leftovers, [])

    def test_unwritable_location_does_not_raise(self) -> None:
        # Losing the ability to persist is an incident, not a reason to take
        # the agent down; it is logged instead. A parent that is a regular file
        # gives a deterministic failure without depending on file permissions
        # (which a root test runner would bypass).
        blocker = os.path.join(self.root, "not-a-directory")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("occupied\n")
        with self.assertLogs("audit_sidecar.immune_state", level="ERROR") as logs:
            state = ImmuneState(state_path=os.path.join(blocker, STATE_FILENAME))
            state.record_verdict(Verdict.INVALID)
        self.assertTrue(state.is_inflamed())
        self.assertIn("could not persist", " ".join(logs.output))
    def test_parent_directory_is_created(self) -> None:
        nested = os.path.join(self.root, "a", "b", STATE_FILENAME)
        state = ImmuneState(state_path=nested)
        state.record_verdict(Verdict.PASS)
        self.assertTrue(os.path.isfile(nested))

    def test_reset_is_persisted(self) -> None:
        first = ImmuneState(state_path=self.state_path)
        first.record_verdict(Verdict.INVALID)
        first.reset()
        self.assertFalse(ImmuneState(state_path=self.state_path).is_inflamed())

    def test_memory_only_state_writes_nothing(self) -> None:
        state = ImmuneState()
        state.record_verdict(Verdict.INVALID)
        self.assertEqual(os.listdir(self.root), [])
        self.assertIsNone(state.state_path)

    def test_state_path_is_validated(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ImmuneState(state_path=value)

    def test_state_path_accepts_a_pathlike(self) -> None:
        import pathlib

        state = ImmuneState(state_path=pathlib.Path(self.state_path))
        self.assertEqual(state.state_path, self.state_path)
        state.record_verdict(Verdict.PASS)
        self.assertTrue(os.path.isfile(self.state_path))


class PersistenceThroughProxyTest(unittest.TestCase):
    """A second proxy must inherit the first one's suspicion."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-pstate-")
        self.root = self._tmp.name
        self.state_path = os.path.join(self.root, STATE_FILENAME)
        self.git("init", "-q")
        self.write("target.py", "value = 0\n")
        self.write("log.txt", "session\n")
        self.git("add", "-A")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-q", "-m", "initial",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            [GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def write(self, name: str, content: str) -> None:
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
            handle.write(content)

    def proxy(self, **kwargs) -> MCPProxy:
        return MCPProxy(
            cwd=self.root,
            runner=SafeRunner(timeout=20),
            rules_dir=packaged_rules_dir(),
            immune_state_path=self.state_path,
            **kwargs,
        )

    def test_second_proxy_inherits_inflammation(self) -> None:
        first = self.proxy()
        for _ in range(2):
            first.intercept_tool_call(
                "write_file",
                {"path": "missing_dir/target.py", "content": "x\n"},
                True,
            )
        self.assertTrue(first.immune_state.is_inflamed())

        second = self.proxy()
        self.assertTrue(second.immune_state.is_inflamed())
        # Same low-risk tool as always, now on the expensive tier.
        second.intercept_tool_call(
            "append_file", {"path": "log.txt", "content": "more\n"}, True
        )
        self.assertEqual(second.audit_log[-1].tier, TIER_ADAPTIVE)

    def test_state_file_does_not_become_its_own_drift_witness(self) -> None:
        # The bookkeeping file lives in the audited tree, and the git witness
        # lists untracked files. If it were written while a command was being
        # observed, the audit would convict its own notebook.
        proxy = self.proxy()
        proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "value = 1\n"}, True
        )
        proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "value = 2\n"}, True
        )
        for result in proxy.audit_log:
            self.assertIs(result.verdict, Verdict.PASS)
            self.assertEqual(result.unexpected_changes, ())
        self.assertTrue(os.path.isfile(self.state_path))

    def test_state_and_object_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ValueError):
            MCPProxy(
                cwd=self.root,
                immune_state=ImmuneState(),
                immune_state_path=self.state_path,
            )


@unittest.skipUnless(HAS_GIT, "git and bash are required for restart tests")
class CrashRestartTest(unittest.TestCase):
    """Kill the process, start another one: the suspicion must still be there.

    This is the test the feature exists for. An in-process check cannot fail
    when the property is "survives a restart", so the process boundary has to be
    real: two separate interpreters, one shared state file.
    """

    MODULE = "audit_sidecar.mcp_server"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-restart-")
        self.root = self._tmp.name
        self.state_path = os.path.join(self.root, STATE_FILENAME)
        self._git("init", "-q")
        self._write("target.py", "value = 0\n")
        self._write("log.txt", "session\n")
        self._git("add", "-A")
        self._git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-q", "-m", "initial",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            [GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def _write(self, name: str, content: str) -> None:
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
            handle.write(content)

    @staticmethod
    def _request(message_id: int, tool: str, args: dict, reported: bool) -> str:
        return json.dumps(
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

    def run_server(self, lines, trace_name: str, extra_args=()):
        trace_path = os.path.join(self.root, trace_name)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                self.MODULE,
                "--cwd",
                self.root,
                "--trace",
                trace_path,
                "--quiet",
                *extra_args,
            ],
            input="".join(line + "\n" for line in lines),
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        with open(trace_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def lie(self, message_id: int) -> str:
        return self._request(
            message_id,
            "write_file",
            {"path": "missing_dir/target.py", "content": "x\n"},
            True,
        )

    def append(self, message_id: int) -> str:
        return self._request(
            message_id, "append_file", {"path": "log.txt", "content": "more\n"}, True
        )

    def test_inflammation_survives_a_restart(self) -> None:
        first = self.run_server([self.lie(1), self.lie(2)], "trace-1.jsonl")
        self.assertEqual([r["verdict"] for r in first], ["FAIL", "FAIL"])
        self.assertEqual(first[-1]["immune_state"], STATE_INFLAMED)
        self.assertTrue(os.path.isfile(self.state_path))

        # A new process, the same working tree: the state file is picked up
        # from beside the trace, so nothing has to be passed twice.
        second = self.run_server([self.append(3)], "trace-2.jsonl")
        self.assertEqual(second[0]["immune_state"], STATE_INFLAMED)
        self.assertEqual(second[0]["tier"], TIER_ADAPTIVE)
        self.assertEqual(second[0]["verdict"], "PASS")

    def test_opt_out_reproduces_the_old_amnesia(self) -> None:
        # The contrast that proves the default is what closes the hole: with
        # persistence switched off, the restart really does forget.
        self.run_server([self.lie(1), self.lie(2)], "trace-1.jsonl")
        second = self.run_server(
            [self.append(3)], "trace-2.jsonl", extra_args=("--immune-state", "none")
        )
        self.assertEqual(second[0]["immune_state"], STATE_NORMAL)
        self.assertEqual(second[0]["tier"], TIER_INNATE)

    def test_healing_survives_a_restart(self) -> None:
        self.run_server([self.lie(1), self.lie(2)], "trace-1.jsonl")
        healed = self.run_server(
            [self.append(i) for i in range(3, 8)], "trace-2.jsonl"
        )
        self.assertEqual(healed[-1]["immune_state"], STATE_NORMAL)

        third = self.run_server([self.append(8)], "trace-3.jsonl")
        self.assertEqual(third[0]["immune_state"], STATE_NORMAL)
        self.assertEqual(third[0]["tier"], TIER_INNATE)

    def test_explicit_state_path_wins(self) -> None:
        elsewhere = os.path.join(self.root, "state", "immune.json")
        self.run_server(
            [self.lie(1), self.lie(2)],
            "trace-1.jsonl",
            extra_args=("--immune-state", elsewhere),
        )
        self.assertTrue(os.path.isfile(elsewhere))
        self.assertFalse(os.path.isfile(self.state_path))

        second = self.run_server(
            [self.append(3)], "trace-2.jsonl", extra_args=("--immune-state", elsewhere)
        )
        self.assertEqual(second[0]["tier"], TIER_ADAPTIVE)


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
