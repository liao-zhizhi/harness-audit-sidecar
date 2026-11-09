"""Regression tests for the self-audit defects found in SELF_AUDIT.md.

Each test here corresponds to a way the sidecar could have reported health it
did not have. They exist because the earlier suite passed while every one of
these defects was live: no test had ever fed the auditor an *unreadable*
witness, which is how ``chmod 000`` ended up producing ``PASS``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from audit_sidecar.git_witness import GitWitness, GitWitnessError
from audit_sidecar.judgment_base import JudgmentBase, packaged_rules_dir
from audit_sidecar.mcp_server import main as server_main
from audit_sidecar.runner import SafeRunner
from audit_sidecar.self_audit import _rule_fixtures, run_self_audit
from audit_sidecar.verdict import Verdict

HAS_GIT = shutil.which("git") is not None


def _init_repo(path: str) -> None:
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "t"], check=True)


def _commit_all(path: str) -> None:
    subprocess.run(["git", "-C", path, "add", "."], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "x"], check=True)


@unittest.skipUnless(shutil.which("chmod"), "chmod is required")
class UnreadableWitnessTest(unittest.TestCase):
    """An unreadable witness is INVALID, never PASS and never FAIL.

    The three outcomes are genuinely different statements:
        PASS     the witness was read and supports the claim
        FAIL     the witness was read and contradicts the claim
        INVALID  the witness was not read at all
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="self-audit-")
        self.root = self._tmp.name
        self.runner = SafeRunner(timeout=10)
        self._wrote = []

    def tearDown(self) -> None:
        for path in self._wrote:
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass
        self._tmp.cleanup()

    def _locked_file(self, name: str = "target.txt") -> str:
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("ORIGINAL\n")
        self._wrote.append(path)
        return path

    def test_file_witness_that_cannot_be_read_is_invalid(self) -> None:
        self._locked_file()
        result = self.runner.run_shell(
            "chmod 000 target.txt",
            cwd=self.root,
            intent_desc="rewrite target.txt",
            expected_file="target.txt",
            expected_content="ORIGINAL",
            reported_success=True,
        )
        self.assertIs(result.verdict, Verdict.INVALID)
        self.assertIsNone(result.effect_observed)
        # The receipt must not claim the witness said "no effect"; it never looked.
        self.assertNotIn("witness says no effect", result.reason)
        self.assertIn("could not be read", result.reason)

    def test_missing_artefact_is_still_fail_not_invalid(self) -> None:
        # The mirror case, pinned so the fix does not over-correct: a file that
        # is genuinely absent IS an observation.
        result = self.runner.run_shell(
            "true",
            cwd=self.root,
            intent_desc="write never-written.txt",
            expected_file="never-written.txt",
            expected_content="anything",
            reported_success=True,
        )
        self.assertIs(result.verdict, Verdict.FAIL)

    def test_no_witness_requested_keeps_its_own_reason(self) -> None:
        result = self.runner.run_shell(
            "true", cwd=self.root, intent_desc="x", reported_success=True
        )
        self.assertIs(result.verdict, Verdict.INVALID)
        self.assertIn("no independent witness requested", result.reason)


@unittest.skipUnless(HAS_GIT, "git is required for the change-set witness")
class UnreadableChangeSetTest(unittest.TestCase):
    """The chmod exploit: no work done, target unreadable, verdict was PASS.

    ``_fingerprint`` returned ``"<status>:unreadable"`` on EACCES, which
    differs from the baseline digest and therefore looked exactly like a
    modification. With ``expected_files_modified`` the change set satisfied
    the expectation, ``missing`` was empty, and the run passed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="self-audit-git-")
        self.root = self._tmp.name
        _init_repo(self.root)
        self.target = os.path.join(self.root, "target.txt")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("ORIGINAL\n")
        _commit_all(self.root)
        self.runner = SafeRunner(timeout=20)

    def tearDown(self) -> None:
        try:
            os.chmod(self.target, 0o644)
        except OSError:
            pass
        self._tmp.cleanup()

    def test_chmod_is_not_a_change(self) -> None:
        result = self.runner.run_shell(
            "chmod 000 target.txt",
            cwd=self.root,
            intent_desc="update target.txt",
            expected_files_modified=["target.txt"],
            repo_path=self.root,
            reported_success=True,
        )
        self.assertIs(result.verdict, Verdict.INVALID)
        self.assertIsNone(result.effect_observed)

    def test_snapshot_reports_unreadable_rather_than_a_digest(self) -> None:
        witness = GitWitness(self.root)
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("CHANGED\n")
        os.chmod(self.target, 0o000)
        fingerprint = witness.snapshot().get("target.txt", "")
        self.assertTrue(fingerprint.endswith("__unreadable__"), fingerprint)

        with self.assertRaises(GitWitnessError):
            witness.changed_since({"target.txt": "M:deadbeef"})

    def test_touch_is_invisible_to_the_change_set(self) -> None:
        # The forgery this witness is supposed to survive: same content, new
        # mtime. A content fingerprint cannot be fooled by it.
        witness = GitWitness(self.root)
        os.utime(self.target, None)
        self.assertEqual(witness.get_changed_files(), [])


class SelfAuditGateTest(unittest.TestCase):
    """The gate that proves the gate works had no tests at all."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="self-audit-gate-")
        self.root = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shipped_engine_passes(self) -> None:
        # ``None`` = auto-detect the shipped base. A path of "none" would be an
        # operator error, which the next test covers indirectly.
        self.assertTrue(run_self_audit())

    def test_broken_judgment_base_fails_the_gate(self) -> None:
        bad = os.path.join(self.root, "rules")
        os.makedirs(bad)
        with open(os.path.join(bad, "broken.yaml"), "w", encoding="utf-8") as handle:
            handle.write("not: [valid\n")
        self.assertFalse(run_self_audit(bad))

    def test_empty_explicit_base_fails_the_gate(self) -> None:
        # An explicitly configured base that contains nothing would leave the
        # rule gate open while appearing configured.
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        self.assertFalse(run_self_audit(empty))

    def test_missing_explicit_base_fails_the_gate(self) -> None:
        self.assertFalse(run_self_audit(os.path.join(self.root, "nope")))

    def test_shadowing_local_policy_fails_the_gate(self) -> None:
        # A working tree that ships ./rules must not be able to delete a
        # shipped guard while the self-audit still reports health.
        rules = os.path.join(self.root, "rules")
        os.makedirs(rules)
        with open(os.path.join(rules, "shadow.yaml"), "w", encoding="utf-8") as handle:
            handle.write('rule_id: "no_file_drift"\ntarget_tool: "write_file"\n')
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        previous_env = os.environ.pop("HARNESS_AUDIT_RULES", None)
        if previous_env is not None:
            self.addCleanup(
                os.environ.__setitem__, "HARNESS_AUDIT_RULES", previous_env
            )
        self.assertFalse(run_self_audit())

    def test_rule_battery_covers_every_shipped_rule_id(self) -> None:
        # The gate used to exercise only ``no_file_drift``; a rule added to the
        # shipped base could therefore be loaded and never checked.
        base = JudgmentBase(packaged_rules_dir())
        covered = {
            exercised
            for _name, _tool, _case, _verdict, _expected, exercised in _rule_fixtures(
                base.rules
            )
            if exercised is not None
        }
        self.assertEqual(covered, set(base.rule_ids))


class ServerRefusesToStartTest(unittest.TestCase):
    """The production path must self-audit; only the demo CLI did."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="self-audit-srv-")
        self.root = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_broken_policy_blocks_startup(self) -> None:
        bad = os.path.join(self.root, "rules")
        os.makedirs(bad)
        with open(os.path.join(bad, "broken.yaml"), "w", encoding="utf-8") as handle:
            handle.write("not: [valid\n")
        code = server_main([
            "--cwd", self.root,
            "--trace", os.path.join(self.root, "t.jsonl"),
            "--rules", bad,
            "--quiet",
        ])
        # 3 = refused after a failed self-audit, distinct from 2 (bad config).
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
