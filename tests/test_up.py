"""``harness-audit up``: three lines, and the preconditions handled quietly.

The tests are about the contract, not the prose:

* an unconfigured directory ends with a configuration, a recording and a
  verdict -- and the twelve lines those three steps would have printed do not
  appear;
* a configured directory runs only the check. Asserted by making ``setup`` and
  ``baseline`` fail loudly if they are called at all;
* ``--file`` judges that file and nothing else;
* every failure ends in one runnable command that carries this invocation's
  arguments, because a placeholder is a command the operator must edit before
  it can answer them.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audit_sidecar.baseline as baseline_mod
import audit_sidecar.usage as usage
from audit_sidecar import up as up_mod
from audit_sidecar.setup_cmd import MCP_SERVER_NAME, _alias_for

GIT = shutil.which("git")
HAS_GIT = GIT is not None and shutil.which("bash") is not None


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


class UpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.mcp = self.root / "mcp.json"
        self.roots = self.root / "ref_roots.json"
        # The usage log is a side effect of every check; keep it out of the
        # developer's home.
        patch = mock.patch.dict(os.environ, {usage.USAGE_DIR_ENV: str(self.root / "usage")})
        patch.start()
        self.addCleanup(patch.stop)

    def run_up(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = up_mod.run_up(list(args))
        return code, out.getvalue(), err.getvalue()

    def up_args(self, *extra: str):
        return (
            "--cwd",
            str(self.project),
            "--mcp-config",
            str(self.mcp),
            "--ref-roots",
            str(self.roots),
            *extra,
        )

    def configure(self) -> None:
        """Write the two halves `up` checks for, without running setup."""
        self.mcp.write_text(
            json.dumps({"mcpServers": {MCP_SERVER_NAME: {"command": "harness-audit", "args": ["mcp"]}}}),
            encoding="utf-8",
        )
        alias = _alias_for(baseline_mod.resolve_repo(str(self.project)) or str(self.project))
        self.roots.write_text(json.dumps({alias: str(self.project)}), encoding="utf-8")

    def assert_three_lines_at_most(self, out: str, expected: int) -> None:
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), expected, out)
        self.assertIn(lines[0], {"PASS", "FAIL", "INVALID", "NOT RUN"})


class FirstRunTests(UpTestCase):
    def test_an_empty_directory_gets_setup_baseline_and_a_check(self):
        code, out, _err = self.run_up(*self.up_args("--diff", "--local"))

        # Not a repository, so the diff cannot be read: INVALID is the honest
        # answer, and the point of the test is that it is *reached*, not that it
        # is PASS.
        self.assertEqual(code, 3)
        self.assert_three_lines_at_most(out, 3)

        config = json.loads(self.mcp.read_text(encoding="utf-8"))
        self.assertIn(MCP_SERVER_NAME, config["mcpServers"])
        self.assertTrue(self.roots.exists())
        self.assertTrue((self.project / ".harness-baseline.json").exists())

    def test_the_setup_chatter_stays_out_of_the_three_lines(self):
        _code, out, _err = self.run_up(*self.up_args("--diff", "--local"))
        for noise in ("mcp config", "baseline written", "repo             :"):
            self.assertNotIn(noise, out)

    def test_the_run_is_recorded_in_the_usage_log(self):
        self.run_up(*self.up_args("--diff", "--local"))
        log = self.root / "usage" / "usage.jsonl"
        events = [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertIn("check", events)
        self.assertIn("baseline", events)
        self.assertIn("setup", events)


class AlreadyConfiguredTests(UpTestCase):
    def test_a_configured_directory_runs_only_the_check(self):
        self.configure()
        baseline_mod.record(str(self.project), files=[], local=True)

        with mock.patch.object(up_mod, "run_setup", side_effect=AssertionError("setup was called")), \
             mock.patch.object(up_mod.baseline_mod, "record", side_effect=AssertionError("baseline was called")):
            code, out, _err = self.run_up(*self.up_args("--diff"))

        self.assertEqual(code, 3)  # INVALID: no git repo, but the check ran
        self.assert_three_lines_at_most(out, 3)

    def test_a_half_configuration_is_not_configured(self):
        # mcp entry but no ref_roots: the server would start and fail every
        # `@alias`, so `up` must finish the job rather than skip it.
        self.mcp.write_text(
            json.dumps({"mcpServers": {MCP_SERVER_NAME: {"command": "harness-audit"}}}),
            encoding="utf-8",
        )
        code, _out, _err = self.run_up(*self.up_args("--diff", "--local"))
        self.assertEqual(code, 3)
        self.assertTrue(self.roots.exists())
        alias = _alias_for(baseline_mod.resolve_repo(str(self.project)) or str(self.project))
        self.assertIn(alias, json.loads(self.roots.read_text(encoding="utf-8")))


@unittest.skipUnless(HAS_GIT, "git and bash are required for a real check")
class SetupMustNotEraseTheRecordingTests(UpTestCase):
    """`setup` records a baseline; `up` must not let it erase the one it judges.

    Found by running the two commands in order by hand: a directory with a
    recording but no MCP config goes through `setup`, whose third step is a
    fresh recording -- taken *after* the change. The next check then compares
    the change against itself and reports FAIL, which is the opposite of the
    truth. `up` passes `--no-baseline` when a recording already exists.
    """

    def setUp(self) -> None:
        super().setUp()
        _git(self.project, "init")
        _git(self.project, "config", "user.email", "t@example.com")
        _git(self.project, "config", "user.name", "t")
        (self.project / "notes.md").write_text("one\n", encoding="utf-8")
        _git(self.project, "add", ".")
        _git(self.project, "commit", "-m", "one")
        baseline_mod.record(str(self.project), files=["notes.md"], local=True)
        (self.project / "notes.md").write_text("two\n", encoding="utf-8")

    def test_the_change_is_still_visible_after_setup_runs(self):
        code, out, _err = self.run_up(*self.up_args("--file", "notes.md", "--local"))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "PASS")

    def test_the_recording_still_holds_the_original_digest(self):
        before = baseline_mod.load(str(self.project)).file_digest("notes.md")
        self.run_up(*self.up_args("--file", "notes.md", "--local"))
        after = baseline_mod.load(str(self.project)).file_digest("notes.md")
        self.assertEqual(before, after)


@unittest.skipUnless(HAS_GIT, "git and bash are required for a real check")
class FileDeclarationTests(UpTestCase):
    def setUp(self) -> None:
        super().setUp()
        _git(self.project, "init")
        (_git(self.project, "config", "user.email", "t@example.com"),)
        _git(self.project, "config", "user.name", "t")
        self.configure()

    def test_file_judges_that_file(self):
        target = self.project / "notes.md"
        target.write_text("one\n", encoding="utf-8")
        _git(self.project, "add", ".")
        _git(self.project, "commit", "-m", "one")
        baseline_mod.record(str(self.project), files=["notes.md"], local=True)

        target.write_text("two\n", encoding="utf-8")
        code, out, _err = self.run_up(*self.up_args("--file", "notes.md", "--local"))

        self.assertEqual(code, 0, out)
        self.assert_three_lines_at_most(out, 2)  # PASS carries no next_step
        self.assertNotIn("next_step", out)

    def test_an_unrecorded_file_is_invalid_with_a_baseline_command(self):
        # A recording exists, but not for this path. The corrective line has to
        # name the path the operator typed -- that is the whole difference
        # between a fix and a riddle.
        other = self.project / "other.md"
        other.write_text("x\n", encoding="utf-8")
        _git(self.project, "add", ".")
        _git(self.project, "commit", "-m", "one")
        baseline_mod.record(str(self.project), files=["other.md"], local=True)

        (self.project / "notes.md").write_text("y\n", encoding="utf-8")
        code, out, err = self.run_up(*self.up_args("--file", "notes.md", "--local"))

        self.assertEqual(code, 3, out)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        self.assertIn("harness-audit baseline", lines[2])
        self.assertIn("notes.md", lines[2])

    def test_a_missing_diff_recording_points_at_the_diff_baseline(self):
        (self.project / "notes.md").write_text("one\n", encoding="utf-8")
        _git(self.project, "add", ".")
        _git(self.project, "commit", "-m", "one")
        # No recording at all, and a configured project: `up` would normally
        # take one. Simulate a recording that vanished by patching load.
        with mock.patch.object(up_mod.baseline_mod, "load", return_value=None), \
             mock.patch.object(up_mod.baseline_mod, "record", side_effect=OSError("read-only")):
            code, out, _err = self.run_up(*self.up_args("--diff"))

        self.assertEqual(code, 3)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertIn("harness-audit baseline", lines[-1])


class FailureTests(UpTestCase):
    def test_a_setup_failure_prints_a_parameterised_command(self):
        with mock.patch.object(up_mod, "run_setup", return_value=2):
            code, out, _err = self.run_up(*self.up_args("--diff"))

        self.assertEqual(code, 3)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(lines[0], "INVALID")
        self.assertIn("harness-audit setup", lines[-1])
        self.assertIn(str(self.mcp), lines[-1])
        self.assertIn(str(self.roots), lines[-1])

    def test_the_corrective_command_is_runnable_and_quoted(self):
        spaced = self.root / "a dir with spaces"
        spaced.mkdir()
        args = (
            "--cwd", str(spaced),
            "--mcp-config", str(self.mcp),
            "--ref-roots", str(self.roots),
            "--diff",
        )
        with mock.patch.object(up_mod, "run_setup", return_value=2):
            _code, out, _err = self.run_up(*args)
        line = out.splitlines()[-1]
        self.assertIn("'" + str(spaced) + "'", line)

    def test_a_baseline_failure_prints_a_baseline_command(self):
        self.configure()
        with mock.patch.object(up_mod.baseline_mod, "load", return_value=None), \
             mock.patch.object(up_mod.baseline_mod, "record", side_effect=OSError("read-only")):
            code, out, _err = self.run_up(*self.up_args("--diff"))
        self.assertEqual(code, 3)
        self.assertIn("harness-audit baseline", out.splitlines()[-1])

    def test_a_missing_directory_is_a_usage_error_not_a_verdict(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = up_mod.run_up(["--cwd", str(self.root / "nope")])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
