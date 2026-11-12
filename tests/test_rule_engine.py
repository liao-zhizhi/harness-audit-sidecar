"""Tests for rule-driven judging.

Two properties carry most of the weight:

1. **A rule can tighten but never loosen.** An engine that could turn a FAIL
   into a PASS would be a way to argue with the witness.
2. **A violation names its rule.** A verdict that fires silently is one the
   operator cannot act on.

The integration tests drive real commands so the witness set is produced by
the runner rather than asserted by the test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from audit_sidecar.judgment_base import JudgmentBase, packaged_rules_dir
from audit_sidecar.mcp_proxy import MCPProxy
from audit_sidecar.rule_engine import RuleEngine
from audit_sidecar.runner import SafeRunner
from audit_sidecar.verdict import Verdict, VerdictEngine

GIT = shutil.which("git")
HAS_GIT = GIT is not None and shutil.which("bash") is not None

SHIPPED_RULES = packaged_rules_dir()

RULE_YAML = """\
rule_id: "no_file_drift"
target_tool: "write_file"
require_witness: true
witness_type: "git_diff"
expectation: "file_modified"
"""

ALL_WITNESSES = frozenset({"file", "git_diff", "agent_report"})
NO_GIT = frozenset({"file", "agent_report"})


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-rule-engine-")
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def base_with(self, text=RULE_YAML, name="rules.yaml"):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
            handle.write(text)
        return JudgmentBase(self.root)

    def engine_with(self, text=RULE_YAML, name="rules.yaml"):
        return RuleEngine(self.base_with(text, name))


class TestFallback(EngineTestCase):
    """With no matching rule the lattice must decide, unchanged."""

    def test_tool_without_rules_uses_lattice(self):
        engine = self.engine_with()
        decision = engine.judge(
            "run_command", intent=True, effect=True, check=True, witnesses=ALL_WITNESSES
        )
        self.assertIs(decision.verdict, Verdict.PASS)
        self.assertIsNone(decision.rule_id)
        self.assertEqual(decision.matched_rule_ids, ())

    def test_fallback_still_reports_false_completion(self):
        engine = self.engine_with()
        decision = engine.judge(
            "run_command", intent=True, effect=False, check=True, witnesses=ALL_WITNESSES
        )
        self.assertIs(decision.verdict, Verdict.FAIL)
        self.assertIn("false completion", decision.reason)

    def test_empty_judgment_base_is_pure_fallback(self):
        with self.assertLogs("audit_sidecar.judgment_base", level="WARNING"):
            engine = RuleEngine(JudgmentBase(self.root))
        decision = engine.judge(
            "write_file", intent=True, effect=True, check=True, witnesses=NO_GIT
        )
        self.assertIs(decision.verdict, Verdict.PASS)
        self.assertIsNone(decision.rule_id)

    def test_explain_without_tool_name_is_pure_lattice(self):
        engine = self.engine_with()
        verdict, reason = engine.explain(intent=True, effect=False, check=True)
        self.assertIs(verdict, Verdict.FAIL)
        self.assertIn("false completion", reason)

    def test_evaluate_matches_explain(self):
        engine = self.engine_with()
        self.assertIs(
            engine.evaluate("write_file", True, True, True, witnesses=NO_GIT),
            Verdict.INVALID,
        )


class TestTightening(EngineTestCase):
    def test_missing_required_witness_downgrades_pass_to_invalid(self):
        # The file witness alone proves the write landed, so the lattice says
        # PASS. The policy says a git change set is also required.
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=True, effect=True, check=True, witnesses=NO_GIT
        )
        self.assertIs(decision.verdict, Verdict.INVALID)
        self.assertEqual(decision.rule_id, "no_file_drift")
        self.assertIn("no_file_drift", decision.reason)
        self.assertIn("git_diff", decision.reason)

    def test_satisfied_rule_leaves_pass_alone(self):
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=True, effect=True, check=True, witnesses=ALL_WITNESSES
        )
        self.assertIs(decision.verdict, Verdict.PASS)
        self.assertIsNone(decision.rule_id)
        self.assertEqual(decision.matched_rule_ids, ("no_file_drift",))

    def test_unmet_expectation_returns_fail_naming_the_rule(self):
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=True, effect=False, check=True, witnesses=ALL_WITNESSES
        )
        self.assertIs(decision.verdict, Verdict.FAIL)
        self.assertEqual(decision.rule_id, "no_file_drift")
        self.assertIn("no_file_drift", decision.reason)
        self.assertEqual(decision.violated_rule_ids, ("no_file_drift",))

    def test_violated_rule_id_is_in_the_reason(self):
        engine = self.engine_with()
        _, reason = engine.explain(
            tool_name="write_file",
            intent=True,
            effect=False,
            check=True,
            witnesses=ALL_WITNESSES,
        )
        self.assertIn("no_file_drift", reason)


class TestMonotonicity(EngineTestCase):
    """Rules may only tighten. This is the property that keeps the audit honest."""

    def test_rule_never_upgrades_fail_to_pass(self):
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=True, effect=False, check=True, witnesses=ALL_WITNESSES
        )
        self.assertIs(decision.verdict, Verdict.FAIL)

    def test_rule_never_softens_fail_even_with_missing_witness(self):
        # Both a contradiction and a missing witness are present. The
        # contradiction wins: a missing witness cannot explain it away.
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=True, effect=False, check=True, witnesses=NO_GIT
        )
        self.assertIs(decision.verdict, Verdict.FAIL)
        self.assertIn("false completion", decision.reason)

    def test_false_failure_survives_a_satisfied_rule(self):
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=True, effect=True, check=False, witnesses=ALL_WITNESSES
        )
        self.assertIs(decision.verdict, Verdict.FAIL)
        self.assertIn("false failure", decision.reason)
        self.assertIsNone(decision.rule_id)

    def test_not_run_is_never_tightened(self):
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=None, effect=False, check=False, witnesses=NO_GIT
        )
        self.assertIs(decision.verdict, Verdict.NOT_RUN)
        self.assertIsNone(decision.rule_id)

    def test_absent_witness_is_not_called_a_violation(self):
        # effect=None means "not observed", not "not done". Reporting FAIL
        # here would manufacture a failure out of missing evidence.
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=True, effect=None, check=True, witnesses=NO_GIT
        )
        self.assertIs(decision.verdict, Verdict.INVALID)
        self.assertNotIn("violated", decision.reason)


class TestDecisionShape(EngineTestCase):
    def test_decision_is_frozen(self):
        engine = self.engine_with()
        decision = engine.judge(
            "write_file", intent=True, effect=True, check=True, witnesses=NO_GIT
        )
        with self.assertRaises(Exception):
            decision.verdict = Verdict.PASS

    def test_as_dict_is_json_friendly(self):
        engine = self.engine_with()
        record = engine.judge(
            "write_file", intent=True, effect=False, check=True, witnesses=ALL_WITNESSES
        ).as_dict()
        self.assertEqual(record["verdict"], "FAIL")
        self.assertEqual(record["rule_id"], "no_file_drift")
        self.assertEqual(record["violated_rule_ids"], ["no_file_drift"])

    def test_tightened_flag_tracks_rule_attribution(self):
        engine = self.engine_with()
        tightened = engine.judge(
            "write_file", intent=True, effect=True, check=True, witnesses=NO_GIT
        )
        fallback = engine.judge(
            "run_command", intent=True, effect=True, check=True, witnesses=ALL_WITNESSES
        )
        self.assertTrue(tightened.tightened)
        self.assertFalse(fallback.tightened)

    def test_blank_tool_name_is_rejected(self):
        engine = self.engine_with()
        with self.assertRaises(ValueError):
            engine.judge("", intent=True, effect=True, check=True)

    def test_judgment_base_must_be_a_judgment_base(self):
        with self.assertRaises(TypeError):
            RuleEngine({"not": "a base"})

    def test_repr_mentions_rule_count(self):
        self.assertIn("rules=1", repr(self.engine_with()))


@unittest.skipUnless(HAS_GIT, "git and bash are required")
class TestRunnerIntegration(unittest.TestCase):
    """The runner must route ``tool_name`` to the rule engine."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-rule-runner-")
        self.root = self._tmp.name
        rules_dir = os.path.join(self.root, "rules")
        os.makedirs(rules_dir)
        with open(os.path.join(rules_dir, "write_file.yaml"), "w", encoding="utf-8") as handle:
            handle.write(RULE_YAML)
        self.engine = RuleEngine(JudgmentBase(rules_dir))
        self.runner = SafeRunner(engine=self.engine, timeout=20)

        self.git("init", "-q")
        self.write_file("target.py", "")
        self.git("add", "-A")
        self.commit("initial")

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------

    def git(self, *args):
        return subprocess.run(
            [GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def commit(self, message):
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-q", "-m", message,
        )

    def write_file(self, name, content):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    # -- tests --------------------------------------------------------

    def test_file_witness_only_is_downgraded_by_rule(self):
        result = self.runner.run_shell(
            "printf 'x' > target.py",
            cwd=self.root,
            intent_desc="modify target.py",
            expected_file="target.py",
            expected_content="x",
            tool_name="write_file",
        )
        self.assertIs(result.verdict, Verdict.INVALID)
        self.assertEqual(result.rule_id, "no_file_drift")
        self.assertIn("no_file_drift", result.reason)

    def test_git_witness_satisfies_the_rule(self):
        result = self.runner.run_shell(
            "printf 'x' > target.py",
            cwd=self.root,
            intent_desc="modify target.py",
            expected_file="target.py",
            expected_content="x",
            expected_files_modified=["target.py"],
            tool_name="write_file",
        )
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertIsNone(result.rule_id)
        self.assertEqual(result.rules_matched, ("no_file_drift",))

    def test_tool_name_without_rule_engine_changes_nothing(self):
        plain = SafeRunner(timeout=20)
        result = plain.run_shell(
            "printf 'x' > target.py",
            cwd=self.root,
            intent_desc="modify target.py",
            expected_file="target.py",
            expected_content="x",
            tool_name="write_file",
        )
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertIsNone(result.rule_id)

    def test_with_engine_returns_a_new_runner(self):
        clone = self.runner.with_engine(VerdictEngine())
        self.assertIsNot(clone, self.runner)
        self.assertIsInstance(clone.engine, VerdictEngine)
        self.assertIsInstance(self.runner.engine, RuleEngine)


@unittest.skipUnless(HAS_GIT, "git and bash are required")
class TestProxyIntegration(unittest.TestCase):
    """The proxy must select rules for the tool it is auditing.

    ``write_file`` already collects both a file witness and a git change set,
    so the shipped rule is satisfied on its normal path. To observe a rule
    *tightening* a verdict through the proxy, these tests register a tool that
    produces a file witness only.
    """

    NOTE_RULE = """\
rule_id: "note_needs_git"
target_tool: "write_note"
require_witness: true
witness_type: "git_diff"
expectation: "file_modified"
"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-rule-proxy-")
        self.root = self._tmp.name
        self.rules_dir = os.path.join(self.root, "rules")
        os.makedirs(self.rules_dir)
        for name, text in (
            ("write_file.yaml", RULE_YAML),
            ("write_note.yaml", self.NOTE_RULE),
        ):
            with open(os.path.join(self.rules_dir, name), "w", encoding="utf-8") as handle:
                handle.write(text)

        self.git("init", "-q")
        self.write_file("target.py", "")
        self.git("add", "-A")
        self.commit("initial")

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------

    def git(self, *args):
        return subprocess.run(
            [GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def commit(self, message):
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-q", "-m", message,
        )

    def write_file(self, name, content):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
            handle.write(content)

    def proxy_with_file_witness_tool(self, **kwargs):
        """A proxy whose custom tool yields a file witness but no git change set."""
        from audit_sidecar.mcp_proxy import ToolInvocation

        proxy = MCPProxy(cwd=self.root, runner=SafeRunner(timeout=20), **kwargs)

        def builder(_args):
            return ToolInvocation(
                command="printf 'note' > note.txt",
                intent="write note.txt",
                expected_file="note.txt",
                expected_content="note",
            )

        proxy.register_tool("write_note", builder)
        return proxy

    # -- tests --------------------------------------------------------

    def test_proxy_without_rules_keeps_legacy_behaviour(self):
        proxy = MCPProxy(cwd=self.root, runner=SafeRunner(timeout=20))
        self.assertIsNone(proxy.rule_engine)
        proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "v"}, True
        )
        self.assertIs(proxy.audit_log[-1].verdict, Verdict.PASS)

    def test_proxy_applies_the_rule_for_the_selected_tool(self):
        proxy = self.proxy_with_file_witness_tool(rules_dir=self.rules_dir)
        proxy.intercept_tool_call("write_note", {}, True)
        result = proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.INVALID)
        self.assertEqual(result.rule_id, "note_needs_git")
        self.assertIn("note_needs_git", result.reason)

    def test_proxy_still_hands_the_agent_its_original_response(self):
        # Rule-tightened judging must not leak into the agent's view; otherwise
        # the agent learns to route around the audit.
        proxy = self.proxy_with_file_witness_tool(rules_dir=self.rules_dir)
        response = proxy.intercept_tool_call("write_note", {}, True)
        self.assertEqual(response["status"], "ok")
        self.assertFalse(response["isError"])
        self.assertIs(proxy.audit_log[-1].verdict, Verdict.INVALID)

    def test_shipped_rule_is_satisfied_by_the_write_file_path(self):
        proxy = MCPProxy(
            cwd=self.root, runner=SafeRunner(timeout=20), rules_dir=self.rules_dir
        )
        proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "v"}, True
        )
        result = proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertIsNone(result.rule_id)
        self.assertIn("no_file_drift", result.rules_matched)

    def test_judgment_base_is_exposed(self):
        proxy = MCPProxy(cwd=self.root, rules_dir=self.rules_dir)
        self.assertIn("no_file_drift", proxy.judgment_base.rule_ids)

    def test_rules_dir_and_judgment_base_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            MCPProxy(
                cwd=self.root,
                rules_dir=self.rules_dir,
                judgment_base=JudgmentBase(self.rules_dir),
            )

    def test_shipped_rules_load_through_the_proxy(self):
        proxy = MCPProxy(cwd=self.root, rules_dir=SHIPPED_RULES)
        self.assertIn("no_file_drift", proxy.judgment_base.rule_ids)

    def test_missing_git_falls_back_to_stat_and_the_write_still_runs(self):
        # A working directory that is not a git repository is an ordinary
        # first run. The command must still execute; declared-file mtime/size
        # is the witness, recorded as witness_scope=stat.
        plain = tempfile.mkdtemp(prefix="harness-nogit-")
        self.addCleanup(shutil.rmtree, plain, True)
        proxy = MCPProxy(cwd=plain, runner=SafeRunner(timeout=20))
        with self.assertLogs("audit_sidecar.runner", level="WARNING"):
            response = proxy.intercept_tool_call(
                "write_file", {"path": "t.py", "content": "v"}, True
            )
        result = proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertEqual(result.witness_scope, "stat")
        self.assertTrue(os.path.isfile(os.path.join(plain, "t.py")))
        self.assertEqual(response["status"], "ok")


if __name__ == "__main__":
    unittest.main()
