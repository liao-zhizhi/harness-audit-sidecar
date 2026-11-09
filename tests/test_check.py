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

    def test_the_next_step_names_the_actual_path_and_not_a_placeholder(self):
        # Item 7: a next step the operator must edit is a worse answer than the
        # INVALID it came with. The literal `<path>` used to be printed.
        outcome = check_file("a.txt", cwd=str(self.root))
        self.assertIn("harness-audit baseline --file a.txt", outcome.next_step)
        self.assertNotIn("<path>", outcome.next_step)

    def test_the_next_step_says_git_head_is_a_different_claim(self):
        # A dirty file in `git status` invites "git says it changed, why
        # INVALID?". The answer is in the next step, not only in the source.
        outcome = check_file("a.txt", cwd=str(self.root))
        self.assertIn("git HEAD", outcome.next_step)
        self.assertIn("not 'changed since", outcome.next_step)

    def test_the_receipt_lists_where_the_recording_was_looked_for(self):
        # Item 5: a recording in a subdirectory is invisible from the root, so
        # the miss has to be diagnosable instead of mysterious.
        outcome = check_file("a.txt", cwd=str(self.root))
        searched = outcome.evidence["baseline_searched"]
        self.assertTrue(searched)
        self.assertTrue(all(isinstance(path, str) for path in searched))
        self.assertTrue(
            any(baseline_mod.BASELINE_FILENAME in path for path in searched)
        )

    def test_a_diff_miss_also_lists_where_it_looked(self):
        (self.root / "a.txt").write_text("dirty\n", encoding="utf-8")
        outcome = check_diff(cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.INVALID)
        self.assertTrue(outcome.evidence["baseline_searched"])

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
        self.assertIn("does not name it", outcome.reason)
        self.assertIn("harness-audit baseline --file b.txt", outcome.next_step)
        # Distinct from "no recording at all": the operator keeps the one they
        # have and adds a path, instead of re-taking a full recording.
        self.assertNotIn("no recording", outcome.reason)
        self.assertNotIn("baseline_searched", outcome.evidence)

    def test_a_subdirectory_recording_is_not_seen_from_the_root(self):
        # Documented limitation (item 5), pinned so a future "fix" that starts
        # walking the tree has to change this test on purpose.
        nested = self.root / "deep" / "inside"
        nested.mkdir(parents=True)
        baseline_mod.record(str(nested), files=["a.txt"], local=True)
        self.assertTrue((nested / baseline_mod.BASELINE_FILENAME).is_file())
        outcome = check_file("a.txt", cwd=str(self.root))
        self.assertIs(outcome.verdict, Verdict.INVALID)
        self.assertIn("no recording", outcome.reason)
        # The receipt shows where it looked, and the subdirectory is not one of
        # the places: the miss is diagnosable, not a silent "baseline missing".
        self.assertFalse(
            any("deep" in path for path in outcome.evidence["baseline_searched"])
        )

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
        "cap_topology",
        "failure_kind",
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
        # Item 1: the cap input is visible next to the receipt's topology, so
        # the semantic mapping does not have to be inferred from the source.
        self.assertIn("topology            : local", stdout)
        self.assertIn("cap_topology        : sidecar", stdout)

    def test_json_is_the_full_record_and_parses(self):
        code, stdout, _stderr = self._run(["--command", "true", "--json"])
        self.assertEqual(code, cli.EXIT_PASS)
        payload = json.loads(stdout)
        self.assertEqual(payload["verdict"], "PASS")
        for field in self.INTERNAL_FIELDS:
            self.assertIn(field, payload)
        self.assertEqual(payload["producer"], "independent")
        self.assertEqual(payload["topology"], "local")
        self.assertEqual(payload["cap_topology"], "sidecar")
        # A decided verdict has no repair kind; this is not "unknown".
        self.assertIsNone(payload["failure_kind"])


class FailureKindTests(_Scratch):
    """One ``INVALID``, three different fixes -- and exit code 3 cannot say which.

    ``verdict`` is ``INVALID`` for all three, so a CI job that branches on it
    cannot tell "the tool is broken" from "nobody looked". The reason's first
    clause is the contract; ``failure_kind`` is the same distinction for a
    machine, derived from that clause rather than stored beside it.
    """

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run_check(argv)
        return code, out.getvalue()

    def test_a_broken_tool_is_self_audit(self):
        with mock.patch.object(cli, "run_self_audit", return_value=False):
            code, stdout = self._run(["--command", "true", "--json"])
        payload = json.loads(stdout)
        self.assertEqual(code, cli.EXIT_INVALID)
        self.assertEqual(payload["verdict"], "INVALID")
        self.assertEqual(payload["failure_kind"], "self_audit")
        self.assertTrue(payload["reason"].startswith(cli.SELF_AUDIT_REASON_PREFIX))

    def test_a_missing_witness_is_witness_missing(self):
        from audit_sidecar.check import check_file

        (self.root / "a.txt").write_text("here\n", encoding="utf-8")
        outcome = check_file("a.txt", cwd=str(self.root))
        self.assertEqual(outcome.verdict.value, "INVALID")
        self.assertEqual(outcome.failure_kind, "witness_missing")

    def test_a_bad_invocation_is_declaration_malformed(self):
        for argv in ([], ["--file", ""]):
            _code, stdout = self._run(argv + ["--json"])
            payload = json.loads(stdout)
            self.assertEqual(payload["verdict"], "INVALID", argv)
            self.assertEqual(payload["failure_kind"], "declaration_malformed", argv)

    def test_a_contradiction_has_no_repair_kind(self):
        _code, stdout = self._run(["--command", "exit 5", "--json"])
        payload = json.loads(stdout)
        self.assertEqual(payload["verdict"], "FAIL")
        self.assertIsNone(payload["failure_kind"])

    def test_the_kind_is_absent_from_the_default_output(self):
        # The default answer stays three lines: a human reads `reason`.
        _code, stdout = self._run(["--command", "exit 5"])
        self.assertNotIn("failure_kind", stdout)

    def test_the_kind_is_shown_under_verbose(self):
        with mock.patch.object(cli, "run_self_audit", return_value=False):
            _code, stdout = self._run(["--command", "true", "--verbose"])
        self.assertIn("failure_kind        : self_audit", stdout)

    def test_self_audit_sentence_is_spelled_by_the_shared_prefix(self):
        # One refusal, one spelling: the constant is the contract and the
        # sentence is built from it, so they cannot drift.
        self.assertTrue(
            cli.CHECK_SELF_AUDIT_REASON.startswith(cli.SELF_AUDIT_REASON_PREFIX)
        )

    def test_every_sentence_is_built_from_the_prefix_that_classifies_it(self):
        # The residual risk is a rewording that keeps the sentence readable but
        # moves it into another bucket: "no declaration given" reworded to
        # "nothing to judge" would silently become `witness_missing` and tell
        # the operator to record a baseline instead of fixing their arguments.
        # The sentence and the classifier therefore read the same constant.
        import audit_sidecar.check as check_mod

        pairs = (
            (cli.CHECK_SELF_AUDIT_REASON, check_mod.SELF_AUDIT_REASON_PREFIX),
            (
                cli.CHECK_NO_DECLARATION_REASON,
                check_mod.NO_DECLARATION_REASON_PREFIX,
            ),
            (
                cli.CHECK_MALFORMED_REASON_TEMPLATE,
                check_mod.MALFORMED_REASON_PREFIX,
            ),
        )
        for sentence, prefix in pairs:
            self.assertTrue(sentence.startswith(prefix), sentence)

    def test_no_declaration_is_classified_end_to_end(self):
        code, stdout = self._run(["--json"])
        payload = json.loads(stdout)
        self.assertEqual(code, cli.EXIT_INVALID)
        self.assertEqual(payload["failure_kind"], "declaration_malformed")
        self.assertTrue(
            payload["reason"].startswith(cli.NO_DECLARATION_REASON_PREFIX)
        )

    def test_a_bad_declaration_is_classified_end_to_end(self):
        code, stdout = self._run(["--file", "", "--json"])
        payload = json.loads(stdout)
        self.assertEqual(code, cli.EXIT_INVALID)
        self.assertEqual(payload["failure_kind"], "declaration_malformed")

    def test_an_unlisted_invalid_reason_falls_back_to_witness_missing(self):
        # The fallback is the point of the design: "nobody looked" is the
        # default, and only the three constants can opt out of it. A new
        # INVALID sentence is therefore never silently mislabelled as a tool
        # failure or a bad invocation.
        from audit_sidecar.check import CheckOutcome

        outcome = CheckOutcome(verdict=Verdict.INVALID, reason="nothing observed it")
        self.assertEqual(outcome.failure_kind, "witness_missing")

    def test_the_self_audit_gate_still_refuses_to_judge(self):
        with mock.patch.object(cli, "run_self_audit", return_value=False):
            code, stdout = self._run(["--command", "true"])
        self.assertEqual(code, cli.EXIT_INVALID)
        # A verdict, not prose: the first line is parseable by a CI step even
        # when the tool has refused to run at all.
        self.assertEqual(stdout.splitlines()[0], "INVALID")
        self.assertIn("self-audit failed", stdout)
        # Item 4: "the tool is broken" and "the witness is missing" are
        # different failures and must not share one INVALID sentence.
        self.assertNotIn("no witness", stdout)
        self.assertIn("no declaration was judged", stdout)


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

    def test_local_at_the_root_does_not_warn(self):
        code, _stdout, stderr = self._run(["--cwd", str(self.root), "--local"])
        self.assertEqual(code, 0)
        self.assertNotIn("will not see it", stderr)

    def test_local_in_a_subdirectory_warns_that_the_root_will_not_see_it(self):
        # Item 5: the recording is written where it was invoked, so a check at
        # the root cannot find it. The operator is told at write time instead
        # of discovering it as a mysterious INVALID later.
        nested = self.root / "deep"
        nested.mkdir()
        code, _stdout, stderr = self._run(["--cwd", str(nested), "--local"])
        self.assertEqual(code, 0)
        self.assertIn("will not see it", stderr)
        self.assertIn("repository root", stderr)

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


class InterfaceDocumentationTests(unittest.TestCase):
    """The seven boundaries must be written where a caller reads them.

    Each of these was a real ambiguity in review: an interface that is minimal
    only in its input is easy to read as "it does everything". These assertions
    pin the sentences that stop that reading, so a later edit cannot quietly
    drop them.
    """

    @classmethod
    def setUpClass(cls):
        raw = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        # Newlines are layout, not content: prose that wraps must still match.
        cls.readme = " ".join(raw.split())

    def test_invalid_is_documented_as_non_zero_and_blocking(self):
        # Item 2: check exits 3 for INVALID and the Action propagates it.
        self.assertIn("INVALID` is **not** zero", self.readme)
        self.assertIn("both fail the job by default", self.readme)

    def test_the_command_scope_is_documented(self):
        # Item 3: --command judges the operator's claim, not a tool layer.
        self.assertIn("does **not** observe an agent's tool layer", self.readme)
        self.assertIn("reported_success", self.readme)

    def test_the_local_baseline_limit_is_documented(self):
        # Item 5: a recording in a subdirectory is invisible from the root.
        self.assertIn("writes where it was invoked", self.readme)
        self.assertIn("baseline_searched", self.readme)

    def test_git_head_is_documented_as_a_different_claim(self):
        self.assertIn("is **not** an implicit baseline", self.readme)
        self.assertIn("a different claim", self.readme)

    def test_the_recording_is_documented_as_per_path(self):
        # Item 7: re-run `baseline --file <this path>`, not a full recording.
        self.assertIn("A recording is per path", self.readme)
        self.assertNotIn("<path>", self.readme)

    def test_the_invalid_case_is_a_pass_fail_verdict_and_prefixed_reason_list(self):
        # Item 1 of the follow-up: exit 3 and `verdict == INVALID` are shared by
        # "tool broken", "bad call" and "nobody looked", so the README has to
        # say the branching key is the reason's first clause.
        self.assertIn("Three different repairs hide behind one `INVALID`", self.readme)
        self.assertIn("the **reason prefix**", self.readme)
        self.assertIn("never branch on `verdict`", self.readme)
        self.assertIn("`self_audit` / `declaration_malformed` / `witness_missing`", self.readme)
        # The prefixes are a contract, and the README has to say so: a
        # reworded sentence is how this classification rots in the field.
        self.assertIn("**Rewording a `reason` is a breaking change", self.readme)
        self.assertIn("Treat the first clause as an API version", self.readme)

    def test_the_topology_mapping_is_documented_as_semantic(self):
        # Item 1: the cap is told `sidecar`, the receipt says `local`.
        import audit_sidecar.check as check_mod

        self.assertEqual(check_mod.LOCAL_TOPOLOGY, "local")
        self.assertEqual(check_mod.PROVENANCE_TOPOLOGY, "sidecar")
        doc = " ".join((check_mod.__doc__ or "").split())
        self.assertIn("semantic mapping for the cap call", doc)
        self.assertIn("not a claim that this check ran through a sidecar", doc)


class ReleaseChecklistTests(unittest.TestCase):
    """The prefix table in the checklist is coupled to the constants.

    A rename in ``check.py`` goes green in every other test (the sentence and
    the classifier read the same constant), so nothing else in this repo can
    detect it. This class is the forcing function: it fails until the author
    has updated ``RELEASE_CHECKLIST.md``, which is where the "renaming a prefix
    is a breaking change" rule lives.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent
        raw = (cls.root / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        cls.checklist = " ".join(raw.split())

    def test_every_contract_prefix_is_listed_verbatim(self):
        import audit_sidecar.check as check_mod

        for prefix in (
            check_mod.SELF_AUDIT_REASON_PREFIX,
            check_mod.MALFORMED_REASON_PREFIX,
            check_mod.NO_DECLARATION_REASON_PREFIX,
        ):
            self.assertIn(
                "`{}`".format(prefix),
                self.checklist,
                "prefix {!r} is a wire contract but is not in "
                "RELEASE_CHECKLIST.md".format(prefix),
            )

    def test_the_rename_rule_is_stated_as_breaking(self):
        self.assertIn("is a breaking change, not a copy edit", self.checklist)
        self.assertIn("never ship a prefix rename in a patch or minor", self.checklist)

    def test_the_deferred_fourth_output_is_recorded_not_added(self):
        # The Action deliberately publishes three outputs. Recording the
        # decision keeps a future release from adding one as a "small win"
        # without noticing it is a second contract.
        self.assertIn(
            "Action does not expose `failure_kind` as a fourth output",
            self.checklist,
        )
        self.assertIn("derive it from `CheckOutcome.failure_kind`", self.checklist)
        action = (self.root / "action.yml").read_text(encoding="utf-8")
        for name in ("verdict:", "reason:", "exit_code:"):
            self.assertIn(name, action)
        self.assertNotIn("failure_kind:", action)

    def test_the_adoption_signal_is_surveyed_not_guessed(self):
        # "Someone renamed a prefix" is not the signal; what external
        # workflows condition on is. The checklist has to name the survey and
        # the reading for `verdict_only`, or the revisit condition degrades
        # back into an opinion.
        self.assertIn("scripts/observe_branches.py", self.checklist)
        self.assertIn("not** \"someone renamed a prefix\"", self.checklist)
        self.assertIn("count before acting", self.checklist)
        self.assertIn("this is an incident, not a survey row", self.checklist)


if __name__ == "__main__":
    unittest.main()
