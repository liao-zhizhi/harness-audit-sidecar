"""Non-git and git-timeout runs fall back to declared-file stat.

The command must still execute. Only the named paths are statted. The trace
records ``witness_scope="stat"``. Lattice: declared path moved → PASS;
claimed success but the path did not move → FAIL.
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

from audit_sidecar.git_witness import GitWitness, GitWitnessError
from audit_sidecar.mcp_proxy import MCPProxy
from audit_sidecar.runner import SafeRunner, WITNESS_SCOPE_STAT
from audit_sidecar.verdict import Verdict

BASH = shutil.which("bash")
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(BASH, "bash is required")
class DeclaredFileStatFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-stat-fb-")
        self.root = self._tmp.name
        self.runner = SafeRunner(timeout=20)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_declared_file_change_is_pass_on_stat(self) -> None:
        result = self.runner.run_shell(
            "printf 'test\\n' > a.txt",
            cwd=self.root,
            intent_desc="write a.txt",
            expected_files_modified=["a.txt"],
        )
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertEqual(result.witness_scope, WITNESS_SCOPE_STAT)
        self.assertEqual(list(result.changed_files), ["a.txt"])
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "test\n")

    def test_claimed_success_without_a_stat_change_is_fail(self) -> None:
        result = self.runner.run_shell(
            "true",
            cwd=self.root,
            intent_desc="write b.txt",
            expected_files_modified=["b.txt"],
            reported_success=True,
        )
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertEqual(result.witness_scope, WITNESS_SCOPE_STAT)
        self.assertIn("b.txt", result.missing_expected)

    def test_declared_b_but_wrote_a_is_fail(self) -> None:
        result = self.runner.run_shell(
            "printf 'test\\n' > a.txt",
            cwd=self.root,
            intent_desc="write b.txt",
            expected_files_modified=["b.txt"],
            reported_success=True,
        )
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertEqual(result.witness_scope, WITNESS_SCOPE_STAT)
        self.assertIn("b.txt", result.missing_expected)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a.txt")))

    def test_git_timeout_falls_back_to_stat(self) -> None:
        def boom(self, args, cwd=None):
            raise GitWitnessError(
                "git timed out after 1.0s: {}".format(list(args))
            )

        with mock.patch.object(GitWitness, "_git_out", boom):
            result = self.runner.run_shell(
                "printf 'test\\n' > a.txt",
                cwd=self.root,
                intent_desc="write a.txt",
                expected_files_modified=["a.txt"],
            )
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertEqual(result.witness_scope, WITNESS_SCOPE_STAT)

    def test_does_not_walk_the_tree(self) -> None:
        # A sibling directory with many files must not be visited. The stat
        # fallback only looks at declared paths.
        junk = os.path.join(self.root, "junk")
        os.makedirs(junk)
        for index in range(20):
            with open(os.path.join(junk, "f{}.txt".format(index)), "w") as handle:
                handle.write("x")
        walked = []
        real_stat = os.stat

        def tracking_stat(path, *args, **kwargs):
            walked.append(os.path.abspath(path))
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", tracking_stat):
            result = self.runner.run_shell(
                "printf 'test\\n' > a.txt",
                cwd=self.root,
                intent_desc="write a.txt",
                expected_files_modified=["a.txt"],
            )
        self.assertIs(result.verdict, Verdict.PASS)
        declared = os.path.abspath(os.path.join(self.root, "a.txt"))
        self.assertTrue(any(path == declared for path in walked))
        self.assertFalse(any(os.path.basename(path).startswith("f") for path in walked))


@unittest.skipUnless(BASH, "bash is required")
class ProxyAndServerStatFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-stat-mcp-")
        self.root = self._tmp.name
        self.trace = os.path.join(self.root, "audit_trace.jsonl")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_command_pass_without_git(self) -> None:
        proxy = MCPProxy(cwd=self.root, runner=SafeRunner(timeout=20))
        response = proxy.intercept_tool_call(
            "run_command",
            {
                "command": "printf 'test\\n' > a.txt",
                "expected_files_modified": ["a.txt"],
            },
            True,
        )
        result = proxy.audit_log[-1]
        self.assertEqual(response["status"], "ok")
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertEqual(result.witness_scope, "stat")

    def test_run_command_false_completion_without_git(self) -> None:
        proxy = MCPProxy(cwd=self.root, runner=SafeRunner(timeout=20))
        proxy.intercept_tool_call(
            "run_command",
            {
                "command": "printf 'test\\n' > a.txt",
                "expected_files_modified": ["b.txt"],
            },
            True,
        )
        result = proxy.audit_log[-1]
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertEqual(result.witness_scope, "stat")
        self.assertIn("b.txt", result.missing_expected)

    def test_stdio_server_records_stat_pass(self) -> None:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tool_call",
                "params": {
                    "tool": "run_command",
                    "args": {
                        "command": "printf 'test\\n' > a.txt",
                        "expected_files_modified": ["a.txt"],
                    },
                    "reported_success": True,
                },
            }
        ) + "\n"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "audit_sidecar.mcp_server",
                "--cwd",
                self.root,
                "--trace",
                self.trace,
            ],
            input=payload,
            capture_output=True,
            text=True,
            cwd=PACKAGE_ROOT,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        response = json.loads(completed.stdout.strip().splitlines()[0])
        self.assertEqual(response["result"]["status"], "ok")
        with open(self.trace, encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["verdict"], "PASS")
        self.assertEqual(records[0]["witness_scope"], "stat")
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "test\n")


if __name__ == "__main__":
    unittest.main()
