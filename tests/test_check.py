"""``check`` and ``baseline``: the one-line interface, tested where it lies.

The claims in this module's docstring are not obvious from reading the code,
so each one gets a test:

* a caller who supplies no internal fields still gets a real verdict;
* the default output contains no internal field name, and ``--verbose`` /
  ``--json`` contain all of them;
* without a recording, "changed" is ``INVALID`` -- not a quiet ``PASS`` on
  "the file exists";
* the ``--local`` recording file does not count as a change in its own tree.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audit_sidecar.baseline as baseline_mod
import audit_sidecar.cli as cli
from audit_sidecar.check import (
    LOCAL_TOPOLOGY,
    PRODUCER,
    check_command,
    check_diff,
    check_file,
)
from audit_sidecar.verdict import Verdict


def _git(directory, *args):
    subprocess.run(
        ["git", *args],
        cwd=directory,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def _git_available() -> bool:
    try:
        return subprocess.run(
            ["git", "--version"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:
        return False


class _Scratch(unittest.TestCase):
    """A temporary directory plus an isolated global baseline path.

    Without the patch, ``baseline.load`` would read the developer's real
    ``~/.gradia/baseline.json`` and a test could pass or fail based on the
    machine, which is exactly the kind of witness this package refuses.
    """

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "repo"
        self.root.mkdir()
        self.global_path = Path(self.scratch.name) / "global-baseline.json"
        patcher = mock.patch.object(baseline_mod, "GLOBAL_BASELINE_PATH", self.global_path)
        patcher.start()
        self.addCleanup(patcher.stop)


class _RepoCase(_Scratch):
    """A committed repository with two files."""

    def setUp(self):
        super().setUp()
        if not _git_available():
            self.skipTest("git unavailable")
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "audit@example.invalid")
        _git(self.root, "config", "user.name", "audit")
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        (self.root / "b.txt").write_text("two\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "initial")


class CheckCommandTests(_Scratch):
    """A command claim is judged by an exit status this process observed."""

    def test_a_zero_exit_is_a_pass_and_names_the_observer(self):
        outcome = check_command("true", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.PASS)
        self.assertIn("exited 0", outcome.reason)
        self.assertEqual(outcome.producer, PRODUCER)
        self.assertEqual(outcome.topology, LOCAL_TOPOLOGY)
        self.assertIsNone(outcome.next_step)

    def test_a_nonzero_exit_contradicts_the_claim(self):
        outcome = check_command("exit 3", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.FAIL)
        self.assertIn("exited 3", outcome.reason)
        self.assertIsNotNone(outcome.next_step)

    def test_the_exit_status_is_read_through_a_pipe(self):
        # ``false | true`` exits 0 without ``pipefail``; the runner sets it, so
        # a swallowed status would show up here as a wrong PASS.
        outcome = check_command("false | true", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.FAIL)

    def test_a_timeout_is_invalid_not_a_failure(self):
        outcome = check_command("sleep 2", cwd=str(self.root), timeout=0.2)
        self.assertIs(outcome.verdict, Verdict.INVALID)
        self.assertIn("did not finish", outcome.reason)

    def test_stdout_is_kept_as_evidence(self):
        outcome = check_command("echo hello", cwd=str(self.root))
        self.assertEqual(outcome.evidence["stdout_head"], "hello\n")

    def test_an_empty_command_is_refused(self):
        with self.assertRaises(ValueError):
            check_command("   ", cwd=str(self.root))


class CheckFileTests(_RepoCase):
    """A file claim needs a recording before "changed" means anything."""

    def test_without_a_recording_an_existing_file_is_invalid(self):
        outcome = check_file("a.txt", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.INVALID)
        self.assertIn("no recording", outcome.reason)
        self.assertIn("harness-audit baseline", outcome.next_step)

    def test_without_a_recording_a_missing_file_contradicts_the_claim(self):
        # "Does not exist" is a fact that needs no before, so this one can be
        # decided; calling it INVALID would be withholding a real answer.
        outcome = check_file("missing.txt", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.FAIL)
        self.assertIn("does not exist", outcome.reason)

    def test_a_recorded_unchanged_file_fails(self):
        baseline_mod.record(str(self.root), files=["a.txt"])
        outcome = check_file("a.txt", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.FAIL)
        self.assertIn("byte-identical", outcome.reason)

    def test_a_recorded_changed_file_passes(self):
        baseline_mod.record(str(self.root), files=["a.txt"])
        (self.root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        outcome = check_file("a.txt", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.PASS)
        self.assertIn("sha256 differs", outcome.reason)

    def test_a_file_deleted_since_the_recording_counts_as_a_change(self):
        baseline_mod.record(str(self.root), files=["a.txt"])
        (self.root / "a.txt").unlink()
        outcome = check_file("a.txt", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.PASS)
        self.assertIn("removed", outcome.reason)

    def test_a_path_the_recording_never_named_is_invalid(self):
        # A recording exists, but not for this file: "changed" still needs a
        # before, and the next step is specific instead of "run baseline".
        baseline_mod.record(str(self.root), files=["a.txt"])
        outcome = check_file("b.txt", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.INVALID)
        self.assertIn("harness-audit baseline", outcome.next_step)

    def test_a_relative_path_from_a_subdirectory_resolves_to_the_same_key(self):
        nested = self.root / "deep" / "inside"
        nested.mkdir(parents=True)
        baseline_mod.record(str(self.root), files=["a.txt"])
        (self.root / "a.txt").write_text("changed\n", encoding="utf-8")
        outcome = check_file("../../a.txt", cwd=str(nested))
        self.assertIs(outcome.verdict, Verdict.PASS)
        self.assertEqual(outcome.evidence["path"], "a.txt")


class CheckDiffTests(_RepoCase):
    """A diff claim is a change-set claim, and the recording separates old dirt."""

    def test_a_clean_tree_without_a_recording_fails(self):
        outcome = check_diff(cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.FAIL)
        self.assertIn("no changes", outcome.reason)

    def test_dirty_without_a_recording_is_invalid(self):
        (self.root / "a.txt").write_text("dirty\n", encoding="utf-8")
        outcome = check_diff(cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.INVALID)
        self.assertIn("no recording", outcome.reason)
        self.assertIn("harness-audit baseline", outcome.next_step)

    def test_nothing_changed_since_the_recording_fails(self):
        baseline_mod.record(str(self.root), local=True)
        outcome = check_diff(cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.FAIL)

    def test_pre_existing_dirt_is_not_counted_as_this_claim_s_change(self):
        (self.root / "b.txt").write_text("already dirty\n", encoding="utf-8")
        baseline_mod.record(str(self.root), local=True)
        outcome = check_diff(cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.FAIL)
        self.assertEqual(outcome.evidence["changed_since_recording"], [])

    def test_a_change_after_the_recording_passes(self):
        baseline_mod.record(str(self.root), local=True)
        (self.root / "a.txt").write_text("after\n", encoding="utf-8")
        outcome = check_diff(cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.PASS)
        self.assertIn("a.txt", outcome.evidence["changed_since_recording"])

    def test_the_local_recording_file_is_not_itself_a_change(self):
        # Regression: --local writes .harness-baseline.json inside the tree it
        # records, so a naive change-set comparison makes every diff PASS.
        baseline_mod.record(str(self.root), local=True)
        outcome = check_diff(cwd=str(self.root))
        self.assertNotIn(".harness-baseline.json", outcome.evidence["changed"])
        self.assertIs(outcome.verdict, Verdict.FAIL)

    def test_outside_a_repository_the_change_set_is_invalid(self):
        plain = Path(self.scratch.name) / "plain"
        plain.mkdir()
        outcome = check_diff(cwd=str(plain))
        self.assertIs(outcome.verdict, Verdict.INVALID)
        self.assertIsNotNone(outcome.next_step)


class CheckOutputTests(_Scratch):
    """Default output hides the vocabulary; ``--verbose`` / ``--json`` show it."""

    INTERNAL_FIELDS = (
        "tier",
        "dropped_assertions",
        "cap_fired",
        "renegotiation_class",
        "producer",
        "topology",
        "witness_type",
        "witness_scope",
    )

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run_check(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_pass_is_two_lines_and_no_internal_field(self):
        code, stdout, _stderr = self._run(["--command", "true"])
        self.assertEqual(code, cli.EXIT_PASS)
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(lines[0], "PASS")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("reason: "))
        for field in self.INTERNAL_FIELDS:
            self.assertNotIn(field, stdout)

    def test_a_fail_is_three_lines_with_a_next_step(self):
        code, stdout, _stderr = self._run(["--command", "exit 4"])
        self.assertEqual(code, cli.EXIT_FAIL)
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "FAIL")
        self.assertTrue(lines[2].startswith("next_step: "))

    def test_invalid_has_its_own_exit_code(self):
        code, stdout, _stderr = self._run([])
        self.assertEqual(code, cli.EXIT_INVALID)
        self.assertEqual(stdout.splitlines()[0], "INVALID")
        self.assertIn("next_step: ", stdout)

    def test_an_empty_declaration_is_invalid_not_a_traceback(self):
        # ``--file ""`` is provided-but-empty; treating it as "not provided"
        # would silently fall through to --diff and judge a different claim.
        code, stdout, stderr = self._run(["--file", ""])
        self.assertEqual(code, cli.EXIT_INVALID)
        self.assertEqual(stdout.splitlines()[0], "INVALID")
        self.assertIn("declaration malformed", stdout)
        self.assertEqual(stderr, "")

    def test_a_non_positive_timeout_is_invalid(self):
        code, stdout, _stderr = self._run(["--command", "true", "--timeout", "0"])
        self.assertEqual(code, cli.EXIT_INVALID)
        self.assertEqual(stdout.splitlines()[0], "INVALID")

    def test_verbose_shows_every_internal_field(self):
        code, stdout, _stderr = self._run(["--command", "true", "--verbose"])
        self.assertEqual(code, cli.EXIT_PASS)
        for field in self.INTERNAL_FIELDS:
            self.assertIn(field, stdout)

    def test_json_is_the_full_record_and_parses(self):
        code, stdout, _stderr = self._run(["--command", "true", "--json"])
        self.assertEqual(code, cli.EXIT_PASS)
        payload = json.loads(stdout)
        self.assertEqual(payload["verdict"], "PASS")
        for field in self.INTERNAL_FIELDS:
            self.assertIn(field, payload)
        self.assertEqual(payload["producer"], "independent")
        self.assertEqual(payload["topology"], "local")

    def test_the_self_audit_gate_still_refuses_to_judge(self):
        with mock.patch.object(cli, "run_self_audit", return_value=False):
            code, stdout, _stderr = self._run(["--command", "true"])
        self.assertEqual(code, cli.EXIT_INVALID)
        # A verdict, not prose: the first line is parseable by a CI step even
        # when the tool has refused to run at all.
        self.assertEqual(stdout.splitlines()[0], "INVALID")
        self.assertIn("self-audit failed", stdout)


class BaselineCommandTests(_Scratch):
    """``baseline`` writes a recording, and never eats another repo's."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run_baseline(argv)
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        super().setUp()
        if not _git_available():
            self.skipTest("git unavailable")
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "audit@example.invalid")
        _git(self.root, "config", "user.name", "audit")
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "initial")

    def test_local_writes_beside_the_checkout(self):
        code, _stdout, _stderr = self._run(
            ["--cwd", str(self.root), "--local", "--file", "a.txt"]
        )
        self.assertEqual(code, 0)
        path = self.root / baseline_mod.BASELINE_FILENAME
        self.assertTrue(path.is_file())
        document = json.loads(path.read_text(encoding="utf-8"))
        entry = document["repos"][str(self.root.resolve())]
        self.assertIn("a.txt", entry["files"])
        self.assertIsNotNone(entry["files"]["a.txt"]["sha256"])

    def test_the_default_target_is_the_shared_file(self):
        code, _stdout, _stderr = self._run(["--cwd", str(self.root)])
        self.assertEqual(code, 0)
        self.assertTrue(self.global_path.is_file())

    def test_a_second_repository_does_not_delete_the_first(self):
        other = Path(self.scratch.name) / "other"
        other.mkdir()
        _git(other, "init", "-q")
        _git(other, "config", "user.email", "audit@example.invalid")
        _git(other, "config", "user.name", "audit")
        (other / "z.txt").write_text("z\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "initial")

        self._run(["--cwd", str(self.root)])
        self._run(["--cwd", str(other)])
        document = json.loads(self.global_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(document["repos"]), {str(self.root.resolve()), str(other.resolve())}
        )

    def test_json_reports_what_was_recorded(self):
        code, stdout, _stderr = self._run(
            ["--cwd", str(self.root), "--local", "--file", "a.txt", "--json"]
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["repo_root"], str(self.root.resolve()))
        self.assertIn("a.txt", payload["files"])

    def test_a_missing_directory_is_a_usage_error(self):
        code, _stdout, stderr = self._run(["--cwd", "/no/such/dir/for/baseline"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("does not exist", stderr)

    def test_outside_git_it_still_records_content_hashes(self):
        plain = Path(self.scratch.name) / "plain"
        plain.mkdir()
        (plain / "note.txt").write_text("note\n", encoding="utf-8")
        code, _stdout, stderr = self._run(
            ["--cwd", str(plain), "--local", "--file", "note.txt"]
        )
        self.assertEqual(code, 0)
        self.assertIn("not a git repository", stderr)
        document = json.loads(
            (plain / baseline_mod.BASELINE_FILENAME).read_text(encoding="utf-8")
        )
        self.assertIn("note.txt", document["repos"][str(plain.resolve())]["files"])


if __name__ == "__main__":
    unittest.main()
