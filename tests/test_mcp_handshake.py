"""The handshake a real MCP client performs, against the real sidecar.

Every case here starts a child process and speaks to it over its stdin/stdout
exactly as a desktop client would: ``initialize``, the ``initialized``
notification, then ``tools/list`` and ``tools/call``. Nothing is stubbed.

Why a separate file from ``test_e2e_stdio.py``: that one covers the legacy
``tool_call`` method, which predates the handshake. This one covers the MCP
surface, and the point of the whole exercise is that the two are *different*
protocols sharing a process -- so a test that mixed them would hide a
regression in either.

The assertions are deliberately about wire shape, not about our own classes:
a JSON object that our own decoder happens to accept is not evidence that a
third-party client will accept it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = "audit_sidecar.mcp_server"
GIT = shutil.which("git")
HAS_GIT = GIT is not None

STARTUP_TIMEOUT = 60

# The JSON-RPC error the MCP lifecycle assigns to a request that arrives
# before initialization.
NOT_INITIALIZED = -32002


def initialize(message_id=1, version="2025-06-18", capabilities=None):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": capabilities if capabilities is not None else {},
            "clientInfo": {"name": "harness-test-client", "version": "1.0.0"},
        },
    }


def initialized_notification():
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


def tools_list(message_id=2, cursor=None):
    message = {"jsonrpc": "2.0", "id": message_id, "method": "tools/list"}
    if cursor is not None:
        message["params"] = {"cursor": cursor}
    return message


def tools_call(name, arguments, message_id=3, audit=None):
    params = {"name": name, "arguments": arguments}
    if audit is not None:
        params["_audit"] = audit
    return {"jsonrpc": "2.0", "id": message_id, "method": "tools/call", "params": params}


@unittest.skipUnless(HAS_GIT, "git is required for the change-set witness")
class McpClientTestCase(unittest.TestCase):
    """A real git repository plus a helper that runs one client session."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-mcp-")
        self.root = self._tmp.name
        self.trace_path = os.path.join(self.root, "audit_trace.jsonl")
        self.git("init", "-q")
        self.write("target.py", "")
        self.git("add", "-A")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
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

    def session(self, messages, extra_args=()):
        """Run one client session; return (responses, stdout, stderr, code).

        ``responses`` is the parsed list of JSON lines the server emitted, in
        order. A notification produces no line, so the list length is how the
        tests assert that.
        """
        payload = "".join(json.dumps(message) + "\n" for message in messages)
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
            input=payload.encode("utf-8"),
            capture_output=True,
            cwd=PACKAGE_ROOT,
            timeout=STARTUP_TIMEOUT,
        )
        stdout = completed.stdout.decode("utf-8")
        responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        return responses, stdout, completed.stderr.decode("utf-8"), completed.returncode

    def trace_records(self):
        if not os.path.exists(self.trace_path):
            return []
        with open(self.trace_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def assert_jsonrpc_response(self, response, response_id):
        """Shape checks common to every JSON-RPC 2.0 response."""
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], response_id)
        self.assertIn("result", response)
        self.assertNotIn("error", response)

    def assert_jsonrpc_error(self, response, response_id, code):
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], response_id)
        self.assertNotIn("result", response)
        self.assertEqual(response["error"]["code"], code)
        self.assertIsInstance(response["error"]["message"], str)
        self.assertTrue(response["error"]["message"])


class InitializeTest(McpClientTestCase):
    def test_initialize_returns_version_capabilities_and_server_info(self) -> None:
        responses, stdout, stderr, code = self.session([initialize(1)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(responses), 1)

        response = responses[0]
        self.assert_jsonrpc_response(response, 1)
        result = response["result"]

        # The client asked for 2025-06-18, so the server must answer with the
        # same version -- not with "latest".
        self.assertEqual(result["protocolVersion"], "2025-06-18")

        # Only capabilities that are actually implemented may be advertised.
        self.assertIn("tools", result["capabilities"])
        self.assertIs(result["capabilities"]["tools"]["listChanged"], False)
        self.assertNotIn("resources", result["capabilities"])
        self.assertNotIn("prompts", result["capabilities"])

        info = result["serverInfo"]
        self.assertEqual(info["name"], "harness-audit-sidecar")
        self.assertRegex(info["version"], r"^\d+\.\d+\.\d+")

        # The one field that reaches the model. Announcing the audit would
        # teach an agent to hide from it.
        self.assertNotIn("instructions", result)

    def test_supported_older_version_is_echoed(self) -> None:
        responses, _stdout, _stderr, _code = self.session([initialize(1, "2024-11-05")])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")

    def test_unsupported_version_falls_back_to_a_supported_one(self) -> None:
        responses, _stdout, _stderr, _code = self.session([initialize(1, "1999-01-01")])
        negotiated = responses[0]["result"]["protocolVersion"]
        self.assertNotEqual(negotiated, "1999-01-01")
        # Must be a version the server actually implements, so the client can
        # recognise it rather than disconnect on an unknown string.
        from audit_sidecar.mcp_server import PROTOCOL_VERSIONS

        self.assertIn(negotiated, PROTOCOL_VERSIONS)

    def test_missing_protocol_version_is_rejected(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [{"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}}]
        )
        self.assert_jsonrpc_error(responses[0], 9, -32602)

    def test_initialize_can_be_repeated(self) -> None:
        # A client reconnecting inside one process is unusual but harmless;
        # refusing the second handshake would be a gratuitous way to break it.
        responses, _stdout, _stderr, _code = self.session([initialize(1), initialize(2)])
        self.assertEqual([r["id"] for r in responses], [1, 2])


class NotificationTest(McpClientTestCase):
    def test_initialized_notification_gets_no_response(self) -> None:
        responses, stdout, stderr, code = self.session(
            [initialize(1), initialized_notification()]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 1)

    def test_unknown_notification_is_ignored_not_answered(self) -> None:
        # JSON-RPC forbids answering a notification; a server that does
        # desynchronises any client that pipelines.
        responses, _stdout, _stderr, _code = self.session(
            [
                initialize(1),
                {"jsonrpc": "2.0", "method": "notifications/cancelled",
                 "params": {"requestId": 99}},
            ]
        )
        self.assertEqual(len(responses), 1)

    def test_notification_between_requests_does_not_shift_ids(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [
                initialize(1),
                initialized_notification(),
                tools_list(2),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                tools_list(3),
            ]
        )
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])


class ToolsListTest(McpClientTestCase):
    def test_tools_list_advertises_callable_tools(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [initialize(1), initialized_notification(), tools_list(2)]
        )
        self.assertEqual(len(responses), 2)

        listing = responses[1]
        self.assert_jsonrpc_response(listing, 2)
        tools = listing["result"]["tools"]
        self.assertIsInstance(tools, list)
        self.assertTrue(tools)

        names = [tool["name"] for tool in tools]
        # Sorted and unique: a client's menu order must not depend on dict
        # insertion order.
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))

        for expected in ("write_file", "append_file", "run_command", "validate_citation"):
            self.assertIn(expected, names)

        for tool in tools:
            self.assertIn("description", tool)
            self.assertTrue(tool["description"])
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)

        # The whole list fits one page, so the pagination cursor is absent.
        self.assertNotIn("nextCursor", listing["result"])

        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(
            by_name["write_file"]["inputSchema"]["required"], ["path", "content"]
        )
        # A model that never learns about this argument cannot get a PASS.
        self.assertIn(
            "expected_files_modified",
            by_name["run_command"]["inputSchema"]["properties"],
        )

    def test_tools_list_before_initialize_is_refused(self) -> None:
        responses, _stdout, _stderr, _code = self.session([tools_list(2)])
        self.assert_jsonrpc_error(responses[0], 2, NOT_INITIALIZED)

    def test_listing_is_stable_across_calls(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [initialize(1), tools_list(2), tools_list(3)]
        )
        self.assertEqual(responses[1]["result"], responses[2]["result"])

    def test_unknown_cursor_still_returns_the_tools(self) -> None:
        # A client replaying a stale cursor should get the list, not an error
        # it has no way to act on.
        responses, _stdout, _stderr, _code = self.session(
            [initialize(1), tools_list(2, cursor="stale-cursor-value")]
        )
        self.assert_jsonrpc_response(responses[1], 2)
        self.assertTrue(responses[1]["result"]["tools"])


class ToolsCallTest(McpClientTestCase):
    def test_tools_call_executes_and_returns_mcp_envelope(self) -> None:
        responses, _stdout, stderr, code = self.session(
            [
                initialize(1),
                initialized_notification(),
                tools_call("write_file", {"path": "target.py", "content": "v = 1\n"}, 3),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual([r["id"] for r in responses], [1, 3])

        result = responses[1]["result"]
        # Exactly the fields the specification defines, and nothing else.
        self.assertEqual(set(result), {"content", "isError"})
        self.assertIs(result["isError"], False)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["content"][0]["text"], "write_file: ok")

        # The side effect really happened, and the audit really recorded it.
        self.assertEqual(self.read("target.py"), "v = 1\n")
        records = self.trace_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["verdict"], "PASS")
        self.assertEqual(records[0]["mcp_method"], "tools/call")

    def test_tools_call_before_initialize_is_refused_and_does_not_run(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [tools_call("write_file", {"path": "target.py", "content": "x\n"}, 3)]
        )
        self.assert_jsonrpc_error(responses[0], 3, NOT_INITIALIZED)
        # The guard must be before execution, not after: a refused call that
        # still wrote the file would be the worst of both behaviours.
        self.assertEqual(self.read("target.py"), "")
        self.assertEqual(self.trace_records(), [])

    def test_false_completion_is_recorded_while_the_agent_is_told_ok(self) -> None:
        # The scenario the product exists for, over the MCP surface.
        responses, _stdout, stderr, code = self.session(
            [
                initialize(1),
                tools_call(
                    "write_file",
                    {"path": "missing_dir/target.py", "content": "v = 2\n"},
                    3,
                ),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIs(responses[1]["result"]["isError"], False)
        self.assertFalse(os.path.exists(self.path("missing_dir")))

        record = self.trace_records()[0]
        self.assertEqual(record["verdict"], "FAIL")
        self.assertIs(record["effect_observed"], False)
        self.assertEqual(record["check_source"], "protocol")
        self.assertIn("false completion", record["reason"])
        # MCP has no claim field, so the claim is the meaning of a normal tool
        # result -- and the trace must say so rather than imply the client
        # supplied it.
        self.assertEqual(record["claim_basis"], "tool-return")

    def test_explicit_claim_is_recorded_as_explicit(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [
                initialize(1),
                tools_call(
                    "append_file",
                    {"path": "target.py", "content": "x"},
                    3,
                    audit={"reported_success": False},
                ),
            ]
        )
        self.assertEqual(responses[1]["result"]["isError"], True)
        record = self.trace_records()[0]
        self.assertEqual(record["claim_basis"], "explicit")
        self.assertIs(record["agent_reported_success"], False)
        # The effect landed but the caller claimed failure: false failure.
        self.assertEqual(record["verdict"], "FAIL")
        self.assertIn("false failure", record["reason"])

    def test_malformed_audit_block_is_rejected(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [
                initialize(1),
                tools_call(
                    "write_file", {"path": "target.py", "content": "x"}, 3,
                    audit={"reported_success": "yes"},
                ),
            ]
        )
        self.assert_jsonrpc_error(responses[1], 3, -32602)
        self.assertEqual(self.trace_records(), [])

    def test_unknown_tool_uses_the_spec_code(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [initialize(1), tools_call("teleport", {}, 3)]
        )
        self.assert_jsonrpc_error(responses[1], 3, -32602)
        self.assertIn("teleport", responses[1]["error"]["message"])
        # An unaudited call must not be recorded as if it had been audited.
        self.assertEqual(self.trace_records(), [])

    def test_unknown_argument_is_a_protocol_error(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [initialize(1), tools_call("write_file", {"path": "t.py"}, 3)]
        )
        self.assert_jsonrpc_error(responses[1], 3, -32602)

    def test_run_command_with_declared_change_set_passes(self) -> None:
        responses, _stdout, stderr, code = self.session(
            [
                initialize(1),
                tools_call(
                    "run_command",
                    {
                        "command": "printf 'landed\\n' > target.py",
                        "expected_files_modified": ["target.py"],
                    },
                    3,
                ),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIs(responses[1]["result"]["isError"], False)
        record = self.trace_records()[0]
        self.assertEqual(record["verdict"], "PASS")
        self.assertEqual(list(record["changed_files"]), ["target.py"])

    def test_utf8_round_trips_through_the_pipe(self) -> None:
        # Framing is newline-delimited UTF-8; a CJK argument must survive both
        # directions without \u escaping or a locale-dependent encoding error.
        content = "变量 = '因果接地'\n"
        responses, stdout, stderr, code = self.session(
            [
                initialize(1),
                tools_call("write_file", {"path": "target.py", "content": content}, 3),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIs(responses[1]["result"]["isError"], False)
        self.assertEqual(self.read("target.py"), content)
        # No escape sequences on the wire: this is UTF-8 text, not ASCII JSON.
        self.assertNotIn("\\u", stdout)

    def test_response_never_contains_the_verdict(self) -> None:
        responses, stdout, _stderr, _code = self.session(
            [
                initialize(1),
                tools_call(
                    "write_file",
                    {"path": "missing/t.py", "content": "x"},
                    3,
                ),
            ]
        )
        wire = json.dumps(responses)
        for leak in ("FAIL", "verdict", "witness", "false completion"):
            self.assertNotIn(leak, wire)
        # And the verdict is still on disk, where the operator can see it.
        self.assertEqual(self.trace_records()[0]["verdict"], "FAIL")


class HandshakeInteropTest(McpClientTestCase):
    """A full session in the order a desktop client actually performs it."""

    def test_full_session(self) -> None:
        responses, stdout, stderr, code = self.session(
            [
                initialize(1),
                initialized_notification(),
                tools_list(2),
                tools_call("write_file", {"path": "a.py", "content": "1\n"}, 3),
                tools_call("append_file", {"path": "a.py", "content": "2\n"}, 4),
                tools_call("run_command", {"command": "true"}, 5),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4, 5])
        for response in responses:
            self.assertEqual(response["jsonrpc"], "2.0")
            self.assertNotIn("error", response)

        self.assertEqual(self.read("a.py"), "1\n2\n")

        verdicts = [record["verdict"] for record in self.trace_records()]
        # `run_command` with no declared expectation cannot be judged PASS:
        # there is nothing to witness against.
        self.assertEqual(verdicts, ["PASS", "PASS", "INVALID"])

    def test_every_output_line_is_a_complete_json_object(self) -> None:
        responses, stdout, stderr, code = self.session(
            [
                initialize(1),
                initialized_notification(),
                tools_list(2),
                tools_call("write_file", {"path": "t.py", "content": "x"}, 3),
            ]
        )
        self.assertEqual(code, 0, stderr)
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            # One line == one message. A message containing an embedded newline
            # would split here, which is exactly the framing bug to catch.
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")

    def test_ping_is_answered_with_an_empty_result(self) -> None:
        responses, _stdout, _stderr, _code = self.session(
            [initialize(1), {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
        )
        self.assert_jsonrpc_response(responses[1], 2)
        self.assertEqual(responses[1]["result"], {})

    def test_stdout_carries_only_protocol_messages(self) -> None:
        # Diagnostics must go to stderr. A stray log line on stdout breaks
        # every client, and we would not notice because our own parser skips
        # blank lines and fails loudly on nothing else.
        #
        # An unsupported protocol version is used as the probe because it is a
        # condition that definitely logs: if the warning ever moved to stdout,
        # this test would see a non-JSON line there.
        _responses, stdout, stderr, code = self.session(
            [initialize(1, "1999-01-01"), tools_list(2)]
        )
        self.assertEqual(code, 0)
        for line in stdout.splitlines():
            if line.strip():
                self.assertIn("jsonrpc", json.loads(line))
        self.assertNotIn("unsupported protocol version", stdout)
        self.assertIn("unsupported protocol version", stderr)


if __name__ == "__main__":
    unittest.main()
