"""Tests for the MCP interception layer.

Every test drives a real command inside a real temporary git repository. The
point of the layer is to catch a tool wrapper that reports success while the
world did not change, so a stubbed runner would test the stub instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from audit_sidecar.mcp_proxy import MCPProxy, ToolInvocation, UnknownToolError
from audit_sidecar.runner import SafeRunner
from audit_sidecar.verdict import Verdict

GIT = shutil.which("git")
HAS_GIT = GIT is not None and shutil.which("bash") is not None


@unittest.skipUnless(HAS_GIT, "git and bash are required for interceptor tests")
class ProxyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-mcp-")
        self.root = self._tmp.name
        self.git("init", "-q")
        self.write("target.py", "")
        self.git("add", "-A")
        self.commit("initial")
        self.proxy = MCPProxy(cwd=self.root, runner=SafeRunner(timeout=20))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            [GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        )
        return completed.stdout

    def commit(self, message: str) -> None:
        self.git(
            "-c", "user.name=Test",
            "-c", "user.email=test@example.com",
            "commit", "-q", "-m", message,
        )

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def write(self, name: str, content: str) -> None:
        with open(self.path(name), "w", encoding="utf-8") as handle:
            handle.write(content)

    def read(self, name: str) -> str:
        with open(self.path(name), encoding="utf-8") as handle:
            return handle.read()

    def changed(self) -> list:
        return self.git("status", "--porcelain", "-z", "-uall").split("\0")[:-1]


class HonestCallTest(ProxyTestCase):
    def test_successful_write_is_pass(self) -> None:
        response = self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "value = 1\n"}, True
        )
        self.assertEqual(response["status"], "ok")
        self.assertFalse(response["isError"])
        result = self.proxy.audit_log[-1]
        self.assertEqual(result.check_source, "protocol")
        self.assertTrue(result.check_observed)
        self.assertTrue(result.effect_observed)
        self.assertIs(result.verdict, Verdict.PASS)

    def test_append_file_is_pass(self) -> None:
        self.proxy.intercept_tool_call(
            "append_file", {"path": "target.py", "content": "appended\n"}, True
        )
        self.assertIn("appended", self.read("target.py"))
        self.assertIs(self.proxy.audit_log[-1].verdict, Verdict.PASS)

    def test_intent_is_derived_from_the_call(self) -> None:
        self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "x\n"}, True
        )
        self.assertEqual(self.proxy.audit_log[-1].intent, "modify file target.py")

    def test_run_command_tool(self) -> None:
        self.proxy.intercept_tool_call(
            "run_command",
            {
                "command": "printf 'value = 2\\n' > target.py",
                "expected_files_modified": ["target.py"],
            },
            True,
        )
        self.assertIs(self.proxy.audit_log[-1].verdict, Verdict.PASS)


class SwallowedErrorTest(ProxyTestCase):
    """The failure mode the interceptor exists for: a tool that lies."""

    def test_reported_success_without_effect_is_fail(self) -> None:
        # The tool layer hits a path error and reports success anyway.
        self.proxy.intercept_tool_call(
            "write_file",
            {"path": "missing_dir/target.py", "content": "x\n"},
            True,
        )
        result = self.proxy.audit_log[-1]
        self.assertTrue(result.check_observed)
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)

    def test_check_is_not_rederived_from_exit_status(self) -> None:
        # This is the regression guard for the whole module. If check were
        # computed as (reported_success and exit_code == 0), the lie would
        # become invisible and this assertion would fail.
        self.proxy.intercept_tool_call(
            "write_file",
            {"path": "missing_dir/target.py", "content": "x\n"},
            True,
        )
        result = self.proxy.audit_log[-1]
        self.assertEqual(result.check_source, "protocol")
        self.assertTrue(result.check_observed)
        self.assertNotEqual(result.exit_code, 0)

    def test_reason_names_the_tool_layer_contradiction(self) -> None:
        self.proxy.intercept_tool_call(
            "write_file",
            {"path": "missing_dir/target.py", "content": "x\n"},
            True,
        )
        reason = self.proxy.audit_log[-1].reason
        self.assertIn("false completion", reason)
        self.assertIn("tool layer reported success", reason)

    def test_world_is_actually_unchanged(self) -> None:
        self.proxy.intercept_tool_call(
            "write_file",
            {"path": "missing_dir/target.py", "content": "x\n"},
            True,
        )
        self.assertEqual(self.changed(), [])

    def test_response_stays_passive(self) -> None:
        # An observer must not correct the agent's belief: leaking the witness
        # through the response would let the agent route around the audit.
        response = self.proxy.intercept_tool_call(
            "write_file",
            {"path": "missing_dir/target.py", "content": "x\n"},
            True,
        )
        self.assertEqual(response["status"], "ok")
        self.assertFalse(response["isError"])
        self.assertNotIn("FAIL", str(response))
        self.assertNotIn("witness", str(response))

    def test_silent_tool_is_fail_even_with_no_error(self) -> None:
        # A tool implementation that does nothing at all, and still reports
        # success. The audit must not accept "no error" as evidence.
        def do_nothing(args):
            return ToolInvocation(
                command="true",
                intent="modify file target.py",
                expected_files=("target.py",),
            )

        self.proxy.register_tool("lazy_write", do_nothing)
        self.proxy.intercept_tool_call(
            "lazy_write", {"path": "target.py"}, True
        )
        result = self.proxy.audit_log[-1]
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.check_observed)
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)


class FalseFailureTest(ProxyTestCase):
    def test_reported_failure_with_real_effect_is_fail(self) -> None:
        # The write landed, but the tool layer reported an error.
        self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "value = 1\n"}, False
        )
        result = self.proxy.audit_log[-1]
        self.assertTrue(result.effect_observed)
        self.assertFalse(result.check_observed)
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertIn("false failure", result.reason)

    def test_response_reflects_the_claim(self) -> None:
        response = self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "x\n"}, False
        )
        self.assertEqual(response["status"], "error")
        self.assertTrue(response["isError"])


class ArgumentSafetyTest(ProxyTestCase):
    def test_content_cannot_inject_shell_syntax(self) -> None:
        payload = "evil; touch PWNED"
        self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": payload}, True
        )
        self.assertEqual(self.read("target.py"), payload)
        self.assertFalse(os.path.exists(self.path("PWNED")))

    def test_command_substitution_is_not_expanded(self) -> None:
        payload = "$(touch PWNED2)"
        self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": payload}, True
        )
        self.assertEqual(self.read("target.py"), payload)
        self.assertFalse(os.path.exists(self.path("PWNED2")))

    def test_backticks_are_not_expanded(self) -> None:
        payload = "`touch PWNED3`"
        self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": payload}, True
        )
        self.assertEqual(self.read("target.py"), payload)
        self.assertFalse(os.path.exists(self.path("PWNED3")))

    def test_path_with_spaces_is_handled(self) -> None:
        self.proxy.intercept_tool_call(
            "write_file", {"path": "a file.py", "content": "x\n"}, True
        )
        self.assertEqual(self.read("a file.py"), "x\n")
        self.assertIs(self.proxy.audit_log[-1].verdict, Verdict.PASS)

    def test_missing_argument_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.proxy.intercept_tool_call(
                "write_file", {"path": "target.py"}, True
            )

    def test_non_string_argument_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.proxy.intercept_tool_call(
                "write_file", {"path": "target.py", "content": 42}, True
            )

    def test_expected_files_as_bare_string_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.proxy.intercept_tool_call(
                "run_command",
                {"command": "true", "expected_files_modified": "target.py"},
                True,
            )


class ContractTest(ProxyTestCase):
    def test_unknown_tool_is_refused(self) -> None:
        with self.assertRaises(UnknownToolError):
            self.proxy.intercept_tool_call("teleport", {}, True)
        # A refused call must not be silently recorded as audited.
        self.assertEqual(len(self.proxy.audit_log), 0)

    def test_unknown_tool_does_not_execute_anything(self) -> None:
        with self.assertRaises(UnknownToolError):
            self.proxy.intercept_tool_call("teleport", {}, True)
        self.assertEqual(self.changed(), [])

    def test_non_bool_reported_success_is_rejected(self) -> None:
        for value in (None, 1, "yes"):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    self.proxy.intercept_tool_call(
                        "write_file",
                        {"path": "target.py", "content": "x\n"},
                        value,  # type: ignore[arg-type]
                    )

    def test_non_dict_args_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.proxy.intercept_tool_call(
                "write_file", ["target.py"], True  # type: ignore[arg-type]
            )

    def test_empty_tool_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.proxy.intercept_tool_call("", {}, True)

    def test_audit_log_is_read_only(self) -> None:
        self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "x\n"}, True
        )
        log = self.proxy.audit_log
        self.assertIsInstance(log, tuple)
        with self.assertRaises(AttributeError):
            log.append(None)  # type: ignore[attr-defined]

    def test_responses_align_with_audit_log(self) -> None:
        for index in range(3):
            self.proxy.intercept_tool_call(
                "write_file", {"path": "target.py", "content": str(index)}, True
            )
        self.assertEqual(len(self.proxy.responses), len(self.proxy.audit_log))
        self.assertEqual(len(self.proxy), 3)

    def test_summary_counts_verdicts(self) -> None:
        self.proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "x\n"}, True
        )
        self.proxy.intercept_tool_call(
            "write_file", {"path": "missing_dir/t.py", "content": "x\n"}, True
        )
        counts = self.proxy.summary()
        self.assertEqual(counts[Verdict.PASS.value], 1)
        self.assertEqual(counts[Verdict.FAIL.value], 1)
        self.assertEqual(counts[Verdict.INVALID.value], 0)

    def test_registering_a_tool_makes_it_callable(self) -> None:
        def marker(args):
            return ToolInvocation(
                command="printf 'ok\\n' > marker.txt",
                intent="create marker.txt",
                expected_files=("marker.txt",),
            )

        self.proxy.register_tool("make_marker", marker)
        self.proxy.intercept_tool_call("make_marker", {}, True)
        self.assertIs(self.proxy.audit_log[-1].verdict, Verdict.PASS)


if __name__ == "__main__":
    unittest.main()
