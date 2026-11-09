"""``harness-audit watch``: one check per change, one notification per failure.

The loop is tested with three fakes -- a signature, a checker, a notifier -- so
the assertions are about *when* each is called, not about timing. A test that
sleeps two seconds to prove a poller polls is a test that fails on a loaded CI
machine and teaches nothing.

The two integration tests use a real repository, because the claim "a second
edit to an already-dirty file still notifies" is a claim about git's output,
and faking that output would only prove the fake.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audit_sidecar.usage as usage
from audit_sidecar import watch as watch_mod
from audit_sidecar.check import CheckOutcome
from audit_sidecar.git_witness import GitWitnessError
from audit_sidecar.verdict import Verdict

GIT = shutil.which("git")
HAS_GIT = GIT is not None and shutil.which("bash") is not None


def _outcome(verdict: Verdict, reason: str = "2 path(s) changed since the recording") -> CheckOutcome:
    return CheckOutcome(
        verdict=verdict,
        reason=reason,
        next_step="look at it" if verdict is not Verdict.PASS else None,
        declaration="diff",
        witness_type="git_diff",
    )


class LoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patch = mock.patch.dict(os.environ, {usage.USAGE_DIR_ENV: str(self.root / "usage")})
        patch.start()
        self.addCleanup(patch.stop)

    def _loop(self, signatures, outcome=None, interval=0.0):
        signatures = list(signatures)
        calls = {"check": 0, "notified": []}

        def signature(_cwd):
            # Last value repeats, so a loop of N ticks does not run out.
            return signatures.pop(0) if len(signatures) > 1 else signatures[0]

        def checker(_cwd):
            calls["check"] += 1
            return outcome if outcome is not None else _outcome(Verdict.FAIL)

        def notifier(result):
            calls["notified"].append(result)
            return "fake"

        loop = watch_mod.WatchLoop(
            cwd=str(self.root),
            interval=interval,
            signature=signature,
            checker=checker,
            notifier=notifier,
            sleep=lambda _seconds: None,
            log=lambda _message: None,
        )
        return loop, calls

    def test_one_change_runs_one_check(self):
        loop, calls = self._loop(["a", "b", "b"])
        loop.run(iterations=2)
        self.assertEqual(calls["check"], 1)

    def test_no_change_runs_no_check(self):
        loop, calls = self._loop(["a", "a", "a"])
        loop.run(iterations=2)
        self.assertEqual(calls["check"], 0)

    def test_two_changes_run_two_checks(self):
        loop, calls = self._loop(["a", "b", "c"])
        loop.run(iterations=3)
        self.assertEqual(calls["check"], 2)

    def test_a_fail_notifies(self):
        loop, calls = self._loop(["a", "b", "b"], outcome=_outcome(Verdict.FAIL))
        loop.run(iterations=2)
        self.assertEqual(len(calls["notified"]), 1)
        self.assertIs(calls["notified"][0].verdict, Verdict.FAIL)

    def test_an_invalid_notifies(self):
        loop, calls = self._loop(["a", "b", "b"], outcome=_outcome(Verdict.INVALID))
        loop.run(iterations=2)
        self.assertEqual(len(calls["notified"]), 1)

    def test_a_pass_is_silent(self):
        loop, calls = self._loop(["a", "b", "b"], outcome=_outcome(Verdict.PASS))
        loop.run(iterations=2)
        self.assertEqual(calls["notified"], [])

    def test_the_check_is_recorded_locally(self):
        loop, _calls = self._loop(["a", "b", "b"], outcome=_outcome(Verdict.PASS))
        loop.run(iterations=2)
        log = self.root / "usage" / "usage.jsonl"
        events = [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["check"])

    def test_a_fail_with_no_write_also_logs_the_suspicion(self):
        # The log has no `mcp_tool_call` row, so nothing audited wrote near this
        # FAIL: exactly the case `suspected_uncovered` exists to count.
        loop, _calls = self._loop(["a", "b", "b"], outcome=_outcome(Verdict.FAIL))
        loop.run(iterations=2)
        log = self.root / "usage" / "usage.jsonl"
        events = [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["check", "suspected_uncovered"])

    def test_a_stop_flag_ends_the_loop_before_another_poll(self):
        # SIGTERM/SIGHUP (and an ignored SIGINT) set this flag; the loop must
        # notice without waiting for the next change.
        loop, calls = self._loop(["a", "a", "a"])
        self.assertEqual(loop._loop(iterations=None, stopped={"flag": True}), 0)
        self.assertEqual(calls["check"], 0)

    def test_signal_handlers_are_restored_afterwards(self):
        before = signal.getsignal(signal.SIGTERM)
        loop, _calls = self._loop(["a", "a"])
        loop.run(iterations=1)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_a_tree_that_cannot_be_read_stops_with_invalid(self):
        def boom(_cwd):
            raise GitWitnessError("not a repository")

        loop = watch_mod.WatchLoop(
            cwd=str(self.root),
            signature=boom,
            sleep=lambda _seconds: None,
            log=lambda _message: None,
        )
        self.assertEqual(loop.run(iterations=2), 3)


class NotificationTests(unittest.TestCase):
    def test_the_body_is_verdict_reason_and_pointer(self):
        body = watch_mod.notification_body(_outcome(Verdict.FAIL, "the tree is unchanged. Nothing moved."))
        self.assertTrue(body.startswith("FAIL: the tree is unchanged."))
        self.assertIn("run `harness-audit up` for details", body)
        # The second sentence is not in the toast: a notification is a pointer.
        self.assertNotIn("Nothing moved", body)

    def test_linux_uses_notify_send(self):
        captured = {}

        def runner(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        with contextlib.redirect_stderr(io.StringIO()):
            channel = watch_mod.notify(
                _outcome(Verdict.FAIL), runner=runner, which=lambda _n: "/usr/bin/notify-send", platform="linux"
            )
        self.assertEqual(channel, "notify-send")
        self.assertEqual(captured["argv"][0], "notify-send")
        self.assertIn("FAIL", captured["argv"])
        self.assertTrue(any("harness-audit up" in part for part in captured["argv"]))

    def test_macos_uses_osascript(self):
        captured = {}

        def runner(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        with contextlib.redirect_stderr(io.StringIO()):
            channel = watch_mod.notify(
                _outcome(Verdict.INVALID), runner=runner, which=lambda _n: "/usr/bin/osascript", platform="darwin"
            )
        self.assertEqual(channel, "osascript")
        self.assertEqual(captured["argv"][:2], ["osascript", "-e"])

    def test_windows_is_skipped_and_still_prints(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            channel = watch_mod.notify(
                _outcome(Verdict.FAIL), which=lambda _n: None, platform="win32"
            )
        self.assertEqual(channel, "skipped")
        self.assertIn("FAIL", err.getvalue())

    def test_a_missing_notifier_is_not_an_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            channel = watch_mod.notify(_outcome(Verdict.FAIL), which=lambda _n: None, platform="linux")
        self.assertEqual(channel, "skipped")
        self.assertIn("harness-audit up", err.getvalue())

    def test_the_body_never_carries_a_path_from_evidence(self):
        outcome = CheckOutcome(
            verdict=Verdict.FAIL,
            reason="2 path(s) changed since the recording",
            declaration="diff",
            witness_type="git_diff",
            evidence={"changed": ["clients/acme/secret.py"], "repo_root": "/home/me/acme"},
        )
        body = watch_mod.notification_body(outcome)
        self.assertNotIn("acme", body)
        self.assertNotIn("/home/me", body)

    def test_first_sentence_keeps_a_single_sentence(self):
        self.assertEqual(watch_mod.first_sentence("One. Two."), "One.")
        self.assertEqual(watch_mod.first_sentence("No period here"), "No period here")
        self.assertEqual(watch_mod.first_sentence(""), "")


@unittest.skipUnless(HAS_GIT, "git is required for the signature test")
class GitSignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "-C", str(self.repo), "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "t@example.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "t"], check=True, capture_output=True
        )
        self.file = self.repo / "notes.md"
        self.file.write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "one"], check=True, capture_output=True
        )

    def test_a_modification_changes_the_signature(self):
        before = watch_mod.git_signature(str(self.repo))
        self.file.write_text("two\n", encoding="utf-8")
        after = watch_mod.git_signature(str(self.repo))
        self.assertNotEqual(before, after)

    def test_a_second_edit_to_an_already_dirty_file_also_changes_it(self):
        # The porcelain line is identical after both edits (" M notes.md"). If
        # the signature were porcelain alone, the watcher would sleep through
        # an agent's whole edit sequence.
        self.file.write_text("two\n", encoding="utf-8")
        first = watch_mod.git_signature(str(self.repo))
        self.file.write_text("three\n", encoding="utf-8")
        second = watch_mod.git_signature(str(self.repo))
        self.assertNotEqual(first, second)

    def test_a_new_untracked_file_changes_the_signature(self):
        before = watch_mod.git_signature(str(self.repo))
        (self.repo / "new.py").write_text("x\n", encoding="utf-8")
        self.assertNotEqual(before, watch_mod.git_signature(str(self.repo)))

    def test_outside_a_repository_the_signature_refuses(self):
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(GitWitnessError):
                watch_mod.git_signature(outside)


class RunWatchTests(unittest.TestCase):
    def test_a_missing_directory_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = watch_mod.run_watch(["--cwd", "/nonexistent/for-sure-" + os.urandom(4).hex()])
        self.assertEqual(code, 2)

    def test_outside_a_repository_it_refuses_before_polling(self):
        with tempfile.TemporaryDirectory() as outside:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = watch_mod.run_watch(["--cwd", outside])
        self.assertEqual(code, 3)
        self.assertIn("not a git repository", err.getvalue())
        self.assertIn("harness-audit watch --cwd", err.getvalue())

    def test_the_interval_flag_reaches_the_loop(self):
        captured = {}

        class RecordingLoop:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                return 0

        # The repository check is the only thing between the flag and the loop;
        # stub it so this test is about the number, not about git.
        with mock.patch.object(watch_mod, "_repo_root", return_value="/repo"), \
             mock.patch.object(watch_mod, "WatchLoop", RecordingLoop):
            code = watch_mod.run_watch(["--cwd", os.getcwd(), "--interval", "5"])

        self.assertEqual(code, 0)
        self.assertEqual(captured["interval"], 5.0)
        # And the value is actually the pause, not a number stored beside one.
        loop = watch_mod.WatchLoop(cwd="/repo", interval=5)
        self.assertEqual(loop._interval, 5.0)

    def test_the_interval_defaults_to_two_seconds(self):
        captured = {}

        class RecordingLoop:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                return 0

        with mock.patch.object(watch_mod, "_repo_root", return_value="/repo"), \
             mock.patch.object(watch_mod, "WatchLoop", RecordingLoop):
            watch_mod.run_watch(["--cwd", os.getcwd()])

        self.assertEqual(captured["interval"], 2.0)


if __name__ == "__main__":
    unittest.main()
