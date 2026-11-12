"""End-to-end tests for the stdio sidecar.

These start a real child process, write real JSON to its stdin, and read the
responses from its stdout. Nothing is mocked: the point is to prove the
shipped entry point works as an actual sidecar, including argument parsing,
the trace file, and the exit status.

The server is launched through the same callable the console script uses, via
``python -m audit_sidecar.mcp_server``, from the project root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = "audit_sidecar.mcp_server"
GIT = shutil.which("git")
HAS_GIT = GIT is not None

# A generous ceiling: these tests spawn interpreters and real commands.
STARTUP_TIMEOUT = 60


def _request(message_id, tool, args, reported_success):
    return {
        "jsonrpc": "2.0",
        "method": "tool_call",
        "params": {
            "tool": tool,
            "args": args,
            "reported_success": reported_success,
        },
        "id": message_id,
    }


@unittest.skipUnless(HAS_GIT, "git is required for the change-set witness")
class StdioServerTestCase(unittest.TestCase):
    """Base fixture: a real repository plus a helper to talk to the server."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-e2e-")
        self.root = self._tmp.name
        self.trace_path = os.path.join(self.root, "audit_trace.jsonl")
        self.git("init", "-q")
        self.write("target.py", "")
        self.git("add", "-A")
        self.git(
            "-c", "user.name=Test",
            "-c", "user.email=test@example.com",
            "commit", "-q", "-m", "initial",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            [GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        )
        return completed.stdout

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def write(self, name: str, content: str) -> None:
        with open(self.path(name), "w", encoding="utf-8") as handle:
            handle.write(content)

    def read(self, name: str) -> str:
        with open(self.path(name), encoding="utf-8") as handle:
            return handle.read()

    def run_server(self, lines, extra_args=()):
        """Send ``lines`` to a fresh server process; return (stdout, stderr, code)."""
        payload = "".join(
            (line if isinstance(line, str) else json.dumps(line)) + "\n"
            for line in lines
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                MODULE,
                "--cwd",
                self.root,
                "--trace",
                self.trace_path,
                *extra_args,
            ],
            input=payload,
            capture_output=True,
            text=True,
            cwd=PACKAGE_ROOT,
            timeout=STARTUP_TIMEOUT,
        )
        return completed.stdout, completed.stderr, completed.returncode

    def trace_records(self):
        if not os.path.exists(self.trace_path):
            return []
        with open(self.trace_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class HonestCallE2ETest(StdioServerTestCase):
    def test_response_is_valid_jsonrpc_and_passive(self) -> None:
        stdout, stderr, code = self.run_server(
            [_request(1, "write_file", {"path": "target.py", "content": "v = 1\n"}, True)]
        )
        self.assertEqual(code, 0, stderr)
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)

        response = json.loads(lines[0])
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        self.assertIn("result", response)
        self.assertEqual(response["result"]["status"], "ok")
        self.assertFalse(response["result"]["isError"])

        # A PASS call has no warning and no lattice field on the wire.
        self.assertNotIn("verdict", response["result"])
        self.assertNotIn("__audit_warning__", response["result"])
        self.assertNotIn("FAIL", json.dumps(response))

    def test_write_actually_happened(self) -> None:
        self.run_server(
            [_request(1, "write_file", {"path": "target.py", "content": "v = 1\n"}, True)]
        )
        self.assertEqual(self.read("target.py"), "v = 1\n")

    def test_trace_records_pass(self) -> None:
        self.run_server(
            [_request(1, "write_file", {"path": "target.py", "content": "v = 1\n"}, True)]
        )
        records = self.trace_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["verdict"], "PASS")
        self.assertTrue(record["effect_observed"])
        self.assertTrue(record["check_observed"])
        self.assertEqual(record["check_source"], "protocol")
        self.assertEqual(record["id"], 1)
        self.assertEqual(record["tool"], "write_file")
        self.assertIn("T", record["timestamp"])
        self.assertTrue(record["session_id"])
        self.assertIs(record["next_step_offered"], False)


class SwallowedErrorE2ETest(StdioServerTestCase):
    """The scenario the whole sidecar exists for, over a real pipe."""

    def test_tool_lie_is_fail_in_trace_but_ok_on_wire(self) -> None:
        stdout, stderr, code = self.run_server(
            [
                _request(
                    7,
                    "write_file",
                    {"path": "missing_dir/target.py", "content": "v = 2\n"},
                    True,
                )
            ]
        )
        self.assertEqual(code, 0, stderr)

        response = json.loads(stdout.strip())
        # The agent is still told everything is fine...
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["result"]["status"], "ok")
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "write_file: ok")
        self.assertTrue(
            response["result"]["__audit_warning__"].startswith("审计器发现假完成：原因 ")
        )

        # ...while the audit record disagrees.
        records = self.trace_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["verdict"], "FAIL")
        self.assertTrue(record["check_observed"])
        self.assertFalse(record["effect_observed"])
        self.assertEqual(record["check_source"], "protocol")
        self.assertIn("false completion", record["reason"])
        self.assertIs(record["next_step_offered"], True)
        self.assertTrue(record["session_id"])
        self.assertIn("T", record["timestamp"])

    def test_world_is_unchanged_after_the_lie(self) -> None:
        self.run_server(
            [
                _request(
                    1, "write_file", {"path": "missing_dir/target.py", "content": "x"}, True
                )
            ]
        )
        self.assertFalse(os.path.exists(self.path("missing_dir")))

    def test_multiple_calls_in_one_session(self) -> None:
        stdout, _stderr, code = self.run_server(
            [
                _request(1, "write_file", {"path": "target.py", "content": "a\n"}, True),
                _request(2, "write_file", {"path": "missing/t.py", "content": "b\n"}, True),
                _request(3, "append_file", {"path": "target.py", "content": "c\n"}, True),
            ]
        )
        self.assertEqual(code, 0)
        responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        records = self.trace_records()
        verdicts = [record["verdict"] for record in records]
        self.assertEqual(verdicts, ["PASS", "FAIL", "PASS"])
        session_ids = {record["session_id"] for record in records}
        self.assertEqual(len(session_ids), 1)
        self.assertTrue(session_ids.pop())

    def test_ids_are_echoed_for_string_ids(self) -> None:
        stdout, _stderr, code = self.run_server(
            [_request("abc", "write_file", {"path": "target.py", "content": "x\n"}, True)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.strip())["id"], "abc")


class ProtocolErrorE2ETest(StdioServerTestCase):
    def test_malformed_json_yields_parse_error(self) -> None:
        stdout, _stderr, code = self.run_server(["{not json"])
        self.assertEqual(code, 0)
        response = json.loads(stdout.strip())
        self.assertEqual(response["error"]["code"], -32700)
        self.assertIsNone(response["id"])
        self.assertEqual(self.trace_records(), [])

    def test_unknown_tool_yields_error_and_no_trace(self) -> None:
        stdout, _stderr, code = self.run_server(
            [_request(5, "teleport", {}, True)]
        )
        self.assertEqual(code, 0)
        response = json.loads(stdout.strip())
        self.assertEqual(response["id"], 5)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32001)
        # An unaudited call must not be recorded as if it had been audited.
        self.assertEqual(self.trace_records(), [])

    def test_unknown_method_yields_method_not_found(self) -> None:
        stdout, _stderr, code = self.run_server(
            [{"jsonrpc": "2.0", "method": "resources/list", "id": 2}]
        )
        self.assertEqual(code, 0)
        response = json.loads(stdout.strip())
        self.assertEqual(response["error"]["code"], -32601)

    def test_mcp_method_before_initialize_is_refused(self) -> None:
        # MCP requires initialization to be the first interaction. Answering
        # tools/list anyway would hide the client's missing handshake until a
        # capability mismatch surfaced much later.
        stdout, _stderr, code = self.run_server(
            [{"jsonrpc": "2.0", "method": "tools/list", "id": 2}]
        )
        self.assertEqual(code, 0)
        response = json.loads(stdout.strip())
        self.assertEqual(response["error"]["code"], -32002)
        self.assertIn("initialize", response["error"]["message"])

    def test_legacy_tool_call_needs_no_handshake(self) -> None:
        # tool_call predates the handshake and is what every recorded session
        # in this project used; gating it would invalidate that history.
        stdout, _stderr, code = self.run_server(
            [_request(1, "write_file", {"path": "target.py", "content": "h\n"}, True)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.strip())["result"]["status"], "ok")

    def test_wrong_jsonrpc_version_is_rejected(self) -> None:
        stdout, _stderr, code = self.run_server(
            [
                {
                    "jsonrpc": "1.0",
                    "method": "tool_call",
                    "params": {"tool": "write_file", "args": {}, "reported_success": True},
                    "id": 3,
                }
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.strip())["error"]["code"], -32600)

    def test_missing_reported_success_is_rejected(self) -> None:
        # Defaulting this to True would fabricate the very claim under audit.
        stdout, _stderr, code = self.run_server(
            [
                {
                    "jsonrpc": "2.0",
                    "method": "tool_call",
                    "params": {"tool": "write_file", "args": {"path": "target.py"}},
                    "id": 4,
                }
            ]
        )
        self.assertEqual(code, 0)
        response = json.loads(stdout.strip())
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("reported_success", response["error"]["message"])

    def test_non_bool_reported_success_is_rejected(self) -> None:
        stdout, _stderr, code = self.run_server(
            [_request(6, "write_file", {"path": "target.py", "content": "x"}, "yes")]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.strip())["error"]["code"], -32602)

    def test_notification_gets_no_response(self) -> None:
        # JSON-RPC forbids a response to a request without an id.
        notification = _request(None, "write_file", {"path": "target.py", "content": "n\n"}, True)
        del notification["id"]
        stdout, _stderr, code = self.run_server([notification])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")
        # It was still executed and audited.
        self.assertEqual(self.read("target.py"), "n\n")
        self.assertEqual(len(self.trace_records()), 1)

    def test_blank_lines_are_ignored(self) -> None:
        stdout, _stderr, code = self.run_server(["", "   ", ""])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_bad_request_after_good_one_keeps_going(self) -> None:
        stdout, _stderr, code = self.run_server(
            [
                _request(1, "write_file", {"path": "target.py", "content": "ok\n"}, True),
                "{broken",
                _request(2, "append_file", {"path": "target.py", "content": "more\n"}, True),
            ]
        )
        self.assertEqual(code, 0)
        responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertEqual(responses[2]["id"], 2)


class TraceFileTest(StdioServerTestCase):
    def test_trace_is_created_even_with_no_calls(self) -> None:
        self.run_server([])
        self.assertTrue(os.path.exists(self.trace_path))
        self.assertEqual(self.trace_records(), [])

    def test_trace_lines_are_valid_json_one_per_call(self) -> None:
        self.run_server(
            [
                _request(index, "write_file", {"path": "target.py", "content": str(index)}, True)
                for index in range(4)
            ]
        )
        with open(self.trace_path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 4)
        for line in lines:
            record = json.loads(line)
            self.assertIn("verdict", record)
            self.assertIn("reason", record)

    def test_trace_is_append_only_across_sessions(self) -> None:
        self.run_server(
            [_request(1, "write_file", {"path": "target.py", "content": "a"}, True)]
        )
        self.run_server(
            [_request(2, "write_file", {"path": "target.py", "content": "b"}, True)]
        )
        self.assertEqual(len(self.trace_records()), 2)

    def test_default_trace_location_is_the_working_directory(self) -> None:
        payload = json.dumps(
            _request(1, "write_file", {"path": "target.py", "content": "x"}, True)
        )
        completed = subprocess.run(
            [sys.executable, "-m", MODULE, "--cwd", self.root],
            input=payload + "\n",
            capture_output=True,
            text=True,
            cwd=PACKAGE_ROOT,
            timeout=STARTUP_TIMEOUT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.root, "audit_trace.jsonl")))


class StartupTest(unittest.TestCase):
    def test_missing_working_directory_exits_nonzero(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", MODULE, "--cwd", "/nonexistent-directory-xyz"],
            input="",
            capture_output=True,
            text=True,
            cwd=PACKAGE_ROOT,
            timeout=STARTUP_TIMEOUT,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("does not exist", completed.stderr)

    def test_help_exits_zero(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", MODULE, "--help"],
            capture_output=True,
            text=True,
            cwd=PACKAGE_ROOT,
            timeout=STARTUP_TIMEOUT,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("harness-audit-server", completed.stdout)
        self.assertIn("--cloud-endpoint", completed.stdout)
        self.assertIn("report", completed.stdout.lower())

    def test_report_subcommand_renders_the_trace(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="harness-report-")
        self.addCleanup(scratch.cleanup)
        trace = os.path.join(scratch.name, "audit_trace.jsonl")
        with open(trace, "w", encoding="utf-8") as handle:
            handle.write(
                '{"timestamp":"2026-11-12T06:00:00+00:00","tool":"write_file",'
                '"intent":"modify file target.py","verdict":"FAIL",'
                '"reason":"false completion","missing_expected":["target.py"],'
                '"unexpected_changes":[]}\n'
            )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                MODULE,
                "report",
                "--lang",
                "zh",
                "--no-color",
                "--trace",
                trace,
            ],
            capture_output=True,
            text=True,
            cwd=PACKAGE_ROOT,
            timeout=STARTUP_TIMEOUT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("【FAIL】", completed.stdout)
        self.assertIn("没动的文件", completed.stdout)
        self.assertNotIn("unrecognized arguments", completed.stderr)


class CloudSyncHeaderTest(unittest.TestCase):
    """The paid POST must not look like the default Python urllib client.

    Cloudflare rule 1010 rejected ``User-Agent: Python-urllib/3.14`` while the
    same URL accepted curl. Content-Type is set explicitly because urllib
    would otherwise send form-urlencoded for some payloads.
    """

    def test_post_sets_product_user_agent_and_json_content_type(self) -> None:
        from audit_sidecar import __version__
        from audit_sidecar.mcp_server import CloudSync, SERVER_NAME

        captured = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            captured["ua"] = req.get_header("User-agent")
            captured["ct"] = req.get_header("Content-type")
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            return _Resp()

        with mock.patch(
            "audit_sidecar.mcp_server.urllib.request.urlopen", fake_urlopen
        ):
            sync = CloudSync("https://example.invalid/api/v1/assets/sync", "k")
            sync.schedule({"tool": "write_file"})
            sync.drain(2.0)

        self.assertEqual(captured.get("method"), "POST")
        self.assertEqual(
            captured.get("ua"), "{}/{}".format(SERVER_NAME, __version__)
        )
        self.assertTrue(
            (captured.get("ct") or "").startswith("application/json"),
            captured.get("ct"),
        )
        self.assertNotIn("Python-urllib", captured.get("ua") or "")


if __name__ == "__main__":
    unittest.main()
