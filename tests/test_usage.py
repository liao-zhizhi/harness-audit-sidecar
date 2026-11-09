"""The usage log: five events, no claim text, no plaintext paths, no vote.

Three claims are under test, and each fails silently in production if it fails:

* **the log is written at all** -- one row per ``check``, and a
  ``suspected_uncovered`` row only when nothing audited wrote near the FAIL;
* **nothing private is in it** -- a command carrying a token and a path with a
  client's name in it must not appear in the file, only a keyed digest;
* **it cannot change a verdict** -- a log that cannot be written still produces
  the same verdict and the same exit code.

The last one is tested from the CLI, because that is where an exception would
turn a PASS into a traceback.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import audit_sidecar.cli as cli
import audit_sidecar.usage as usage
from audit_sidecar.check import CheckOutcome
from audit_sidecar.verdict import Verdict


def _outcome(
    verdict: Verdict, reason: str = "the file is unchanged", declaration: str = "file src/main.py",
    evidence: dict = None,
) -> CheckOutcome:
    return CheckOutcome(
        verdict=verdict,
        reason=reason,
        next_step=None if verdict is Verdict.PASS else "do the thing",
        declaration=declaration,
        witness_type="file",
        evidence=evidence if evidence is not None else {"recording": "/some/recording.json"},
    )


class UsageLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.log = self.dir / "usage.jsonl"
        patch = mock.patch.dict(os.environ, {usage.USAGE_DIR_ENV: str(self.dir)})
        patch.start()
        self.addCleanup(patch.stop)

    def lines(self):
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # ------------------------------------------------------------- the schema

    def test_all_five_event_types_are_accepted(self):
        for event in usage.EVENTS:
            self.assertIsNotNone(usage.record(event))
        self.assertEqual([row["event"] for row in self.lines()], list(usage.EVENTS))

    def test_an_unknown_event_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            usage.record("chek")
        self.assertEqual(self.lines(), [])

    def test_every_row_carries_a_timestamp(self):
        usage.record("setup", mcp_scope="project")
        row = self.lines()[0]
        self.assertEqual(set(row), {"event", "at", "mcp_scope"})
        self.assertRegex(row["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

    def test_a_torn_line_is_skipped_not_fatal(self):
        self.log.write_text('{"event": "check"}\nnot json\n', encoding="utf-8")
        self.assertEqual(list(usage.rows()), [{"event": "check"}])

    def test_append_never_rewrites(self):
        usage.record("setup", mcp_scope="project")
        before = self.log.read_text(encoding="utf-8")
        usage.record("baseline", files=1, local=False, repo_hash="x")
        after = self.log.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))
        self.assertEqual(len(self.lines()), 2)

    # ----------------------------------------------------------- the privacy

    def test_a_command_never_stores_its_text(self):
        secret = "curl -H 'Authorization: Bearer sk-live-DEADBEEF' https://api"
        outcome = _outcome(Verdict.PASS, reason="the command exited 0")
        usage.record_check(outcome, declaration_kind="command", cwd=str(self.dir))

        raw = self.log.read_text(encoding="utf-8")
        self.assertNotIn("sk-live-DEADBEEF", raw)
        self.assertNotIn("Authorization", raw)
        self.assertEqual(self.lines()[0]["declaration"], "command")

    def test_a_path_is_hashed_not_stored(self):
        path = "clients/acme-secret-project/src/main.py"
        usage.record_check(
            _outcome(Verdict.FAIL),
            declaration_kind="file",
            cwd=str(self.dir),
            path=path,
        )
        raw = self.log.read_text(encoding="utf-8")
        for fragment in ("acme", "secret-project", "main.py", "clients"):
            self.assertNotIn(fragment, raw)
        row = self.lines()[0]
        self.assertRegex(row["path_hash"], r"^[0-9a-f]{32}$")

    def test_the_same_path_hashes_the_same_and_a_different_one_does_not(self):
        one = usage.hash_path("src/main.py", cwd=str(self.dir))
        two = usage.hash_path("src/main.py", cwd=str(self.dir))
        other = usage.hash_path("src/other.py", cwd=str(self.dir))
        self.assertEqual(one, two)
        self.assertNotEqual(one, other)

    def test_an_absolute_path_outside_the_directory_still_hashes(self):
        # No repo, no cwd: the raw path is hashed whole. The point of the test
        # is that it does not blow up and does not leak.
        digest = usage.hash_path("/home/someone/private/notes.md")
        self.assertRegex(digest, r"^[0-9a-f]{32}$")
        self.assertNotIn("someone", digest)

    def test_the_salt_is_created_private(self):
        usage.hash_path("src/main.py", cwd=str(self.dir))
        salt = self.dir / "usage.salt"
        self.assertTrue(salt.exists())
        mode = salt.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    # -------------------------------------------------------- baseline_present

    def test_a_missing_recording_is_recorded_as_false(self):
        usage.record_check(
            _outcome(Verdict.INVALID, evidence={"recording": None}),
            declaration_kind="file",
            cwd=str(self.dir),
            path="src/main.py",
        )
        self.assertFalse(self.lines()[0]["baseline_present"])

    def test_a_present_recording_is_recorded_as_true(self):
        usage.record_check(
            _outcome(Verdict.FAIL),
            declaration_kind="file",
            cwd=str(self.dir),
            path="src/main.py",
        )
        self.assertTrue(self.lines()[0]["baseline_present"])

    # ------------------------------------------------------------- uncovered

    def test_a_fail_with_no_write_nearby_is_suspected_uncovered(self):
        usage.observe_check(
            _outcome(Verdict.FAIL), declaration_kind="file", cwd=str(self.dir), path="src/main.py"
        )
        events = [row["event"] for row in self.lines()]
        self.assertEqual(events, ["check", "suspected_uncovered"])
        row = self.lines()[1]
        self.assertEqual(row["verdict"], "FAIL")
        self.assertIsNone(row["last_write_tool"])
        self.assertEqual(row["window_seconds"], 600)

    def test_a_recent_write_to_the_same_path_explains_the_fail(self):
        digest = usage.hash_path("src/main.py", cwd=str(self.dir))
        usage.record_tool_call(
            tool_name="write_file",
            verdict=Verdict.PASS,
            path_arg="src/main.py",
            cwd=str(self.dir),
        )
        usage.observe_check(
            _outcome(Verdict.FAIL), declaration_kind="file", cwd=str(self.dir), path="src/main.py"
        )
        self.assertEqual([row["event"] for row in self.lines()], ["mcp_tool_call", "check"])

    def test_a_write_to_another_path_does_not_explain_it(self):
        usage.record_tool_call(
            tool_name="write_file",
            verdict=Verdict.PASS,
            path_arg="src/other.py",
            cwd=str(self.dir),
        )
        usage.observe_check(
            _outcome(Verdict.FAIL), declaration_kind="file", cwd=str(self.dir), path="src/main.py"
        )
        self.assertEqual(
            [row["event"] for row in self.lines()],
            ["mcp_tool_call", "check", "suspected_uncovered"],
        )

    def test_a_write_outside_the_window_does_not_explain_it(self):
        self.record_at("write_file", seconds_ago=3600, path="src/main.py")
        usage.observe_check(
            _outcome(Verdict.FAIL), declaration_kind="file", cwd=str(self.dir), path="src/main.py"
        )
        row = self.lines()[-1]
        self.assertEqual(row["event"], "suspected_uncovered")
        self.assertEqual(row["last_write_tool"], "write_file")
        self.assertGreaterEqual(row["last_write_ago_seconds"], 3600)

    def test_a_pass_is_never_suspected(self):
        usage.observe_check(_outcome(Verdict.PASS), declaration_kind="diff", cwd=str(self.dir))
        self.assertEqual([row["event"] for row in self.lines()], ["check"])

    def test_an_invalid_is_never_suspected(self):
        usage.observe_check(_outcome(Verdict.INVALID), declaration_kind="diff", cwd=str(self.dir))
        self.assertEqual([row["event"] for row in self.lines()], ["check"])

    def test_a_diff_fail_accepts_any_recent_write(self):
        # A diff claim names no file, so "an audited write happened" is the
        # strongest question the log can answer.
        usage.record_tool_call(tool_name="write_file", verdict=Verdict.PASS, path_arg="src/x.py", cwd=str(self.dir))
        usage.observe_check(_outcome(Verdict.FAIL), declaration_kind="diff", cwd=str(self.dir))
        self.assertEqual([row["event"] for row in self.lines()], ["mcp_tool_call", "check"])

    def record_at(self, tool_name, *, seconds_ago, path):
        entry = usage.record_tool_call(
            tool_name=tool_name, verdict=Verdict.PASS, path_arg=path, cwd=str(self.dir)
        )
        # Rewrite the row's timestamp: the rule is about wall-clock distance, so
        # a test that sleeps an hour is not a test.
        stamp = time.time() - seconds_ago
        rows = self.lines()
        rows[-1] = dict(entry, at=_iso(stamp))
        self.log.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )

    # ------------------------------------------------------------- aggregate

    def test_the_aggregate_is_exactly_three_fields(self):
        usage.record_check(_outcome(Verdict.FAIL), declaration_kind="file", cwd=str(self.dir))
        aggregate = usage.aggregate()
        self.assertEqual(set(aggregate), set(usage.AGGREGATE_FIELDS))

    def test_the_aggregate_counts_verdicts_and_misses(self):
        usage.record_check(_outcome(Verdict.FAIL), declaration_kind="file", cwd=str(self.dir))
        usage.record_check(_outcome(Verdict.PASS), declaration_kind="diff", cwd=str(self.dir))
        usage.record_check(
            _outcome(Verdict.INVALID, evidence={"recording": None}),
            declaration_kind="file",
            cwd=str(self.dir),
        )
        aggregate = usage.aggregate()
        self.assertEqual(aggregate["verdicts"], {"FAIL": 1, "PASS": 1, "INVALID": 1})
        self.assertEqual(aggregate["baseline_misses"], 1)

    def test_the_aggregate_lists_tool_names_once_each(self):
        for name in ("write_file", "write_file", "run_command"):
            usage.record_tool_call(tool_name=name, verdict=Verdict.PASS)
        self.assertEqual(usage.aggregate()["tool_names"], ["run_command", "write_file"])

    def test_the_aggregate_carries_no_rows(self):
        usage.record_check(_outcome(Verdict.FAIL), declaration_kind="file", cwd=str(self.dir), path="src/main.py")
        blob = json.dumps(usage.aggregate())
        for fragment in ("path_hash", "at", "event", "reason", "declaration"):
            self.assertNotIn(fragment, blob)


def _iso(stamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")


class UsageNeverDecidesTests(unittest.TestCase):
    """A log that cannot be written must not change a verdict or an exit code."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_read_only_log_directory_does_not_change_the_exit_code(self):
        blocked = self.root / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        env = {usage.USAGE_DIR_ENV: str(blocked)}

        def run():
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.run_check(["--diff", "--cwd", str(self.root)])
            return code, out.getvalue()

        code, out = run()
        self.assertEqual(code, 3)  # INVALID: not a repository, exactly as before
        self.assertEqual(out.splitlines()[0], "INVALID")

    def test_a_raising_observe_does_not_escape(self):
        with mock.patch.object(usage, "record_check", side_effect=RuntimeError("boom")):
            # observe_check is defensive by construction; a diagnostic that can
            # raise is a diagnostic that changes the exit code it observes.
            usage.observe_check(_outcome(Verdict.FAIL), declaration_kind="diff", cwd=str(self.root))


class UsageWiringTests(unittest.TestCase):
    """The CLI and the MCP server actually call the log. Wiring, not schema."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.patch = mock.patch.dict(os.environ, {usage.USAGE_DIR_ENV: str(self.root)})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def lines(self):
        path = self.root / "usage.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_a_cli_check_writes_one_event(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.run_check(["--diff", "--cwd", str(self.root)])

        self.assertEqual(code, 3)
        rows = self.lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "check")
        self.assertEqual(rows[0]["declaration"], "diff")
        self.assertEqual(rows[0]["verdict"], "INVALID")

    def test_a_cli_baseline_writes_one_event(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.run_baseline(["--cwd", str(self.root), "--local"])

        self.assertEqual(code, 0)
        rows = self.lines()
        self.assertEqual(rows[0]["event"], "baseline")
        self.assertTrue(rows[0]["local"])
        self.assertIn("files", rows[0])
        # The recorded paths are not in the usage row.
        self.assertNotIn("files_list", rows[0])

    def test_an_mcp_tool_call_writes_one_event(self):
        from audit_sidecar.mcp_proxy import MCPProxy
        from audit_sidecar.mcp_server import JsonRpcServer, TraceWriter
        from audit_sidecar.runner import SafeRunner

        work = self.root / "repo"
        work.mkdir()
        proxy = MCPProxy(cwd=str(work), runner=SafeRunner(timeout=10))
        server = JsonRpcServer(proxy, TraceWriter(str(self.root / "trace.jsonl")), stdout=io.StringIO())
        self._initialize(server)
        server.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "write_file",
                        "arguments": {"path": "hello.txt", "content": "hi"},
                        "audit": {"reported_success": True},
                    },
                }
            )
        )
        rows = [row for row in self.lines() if row["event"] == "mcp_tool_call"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_name"], "write_file")
        self.assertIn(rows[0]["verdict"], {"PASS", "FAIL", "INVALID"})
        # The path went in and only the digest came out.
        self.assertNotIn("hello.txt", json.dumps(rows[0]))

    @staticmethod
    def _initialize(server) -> None:
        server.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
