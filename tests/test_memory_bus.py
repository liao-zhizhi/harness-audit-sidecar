"""Physical FAIL events reach the memory bus; overlay can raise witness tier."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audit_sidecar.immune_state import TIER_ADAPTIVE, TIER_INNATE, ImmuneState
from audit_sidecar.judgment_base import JudgmentBase
from audit_sidecar.mcp_proxy import MCPProxy, ToolInvocation
from audit_sidecar.mcp_server import TraceWriter
from audit_sidecar.memory_bus import (
    events_path,
    faults_path,
    intent_requires_full_witness,
    load_overlay,
    record_physical_fault,
    record_scale_events,
    rules_path,
)
from audit_sidecar.verdict import Verdict


class MemoryBusTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-bus-")
        self.home = self._tmp.name
        self._env = mock.patch.dict(
            os.environ,
            {
                "GRADIA_HOME": self.home,
                "GRADIA_SIDECAR_RULES": os.path.join(self.home, "pioneer", "sidecar_rules.yaml"),
                "GRADIA_PHYSICAL_FAULTS": os.path.join(
                    self.home, "memory", "physical_faults.jsonl"
                ),
            },
            clear=False,
        )
        self._env.start()
        import audit_sidecar.memory_bus as bus

        bus._overlay_mtime = None
        bus._overlay_cache = {}

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_fail_record_is_appended(self) -> None:
        row = record_physical_fault(
            {
                "verdict": "FAIL",
                "tool": "write_file",
                "intent": "modify file missing_dir/target.py",
                "reason": "false completion: report says success, witness says no effect",
                "timestamp": "2026-11-12T10:00:00+00:00",
                "id": 7,
            }
        )
        self.assertIsNotNone(row)
        path = faults_path()
        self.assertTrue(path.exists())
        stored = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(stored["tool"], "write_file")
        self.assertEqual(stored["intent"], "modify file missing_dir/target.py")
        self.assertIn("false completion", stored["reason"])

    def test_pass_record_is_ignored(self) -> None:
        self.assertIsNone(
            record_physical_fault({"verdict": "PASS", "tool": "write_file", "intent": "ok"})
        )
        self.assertFalse(faults_path().exists())

    def test_trace_writer_copies_fail_only(self) -> None:
        trace = os.path.join(self.home, "audit_trace.jsonl")
        writer = TraceWriter(trace)
        writer.write({"verdict": "PASS", "tool": "write_file", "intent": "ok", "reason": "ok"})
        writer.write(
            {
                "verdict": "FAIL",
                "tool": "run_command",
                "intent": "run command: echo fake > hello.txt",
                "reason": "false completion: report says success, witness says no effect",
                "id": 3,
            }
        )
        lines = [json.loads(line) for line in Path(trace).read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        faults = [
            json.loads(line)
            for line in faults_path().read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0]["tool"], "run_command")
        scale_rows = [
            json.loads(line)
            for line in events_path().read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual([row["scale"] for row in scale_rows], ["TACTICAL"])
        self.assertEqual(scale_rows[0]["verdict"], "FAIL")
        self.assertEqual(scale_rows[0]["pain_level"], 2)

    def test_overlay_escalates_innate_tier(self) -> None:
        path = rules_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    "version: 1",
                    "schema: gradia.cross_layer.v1",
                    "escalate_witness_to_full:",
                    "  when_intent_contains:",
                    '    - "假完成"',
                    '    - "false completion"',
                    "  tools:",
                    "    []",
                    "hard_constraints: []",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.assertTrue(intent_requires_full_witness("false completion planted here", "write_file"))
        self.assertFalse(intent_requires_full_witness("modify file target.py", "write_file"))

        rules_dir = os.path.join(self.home, "rules")
        os.makedirs(rules_dir)
        Path(os.path.join(rules_dir, "low.yaml")).write_text(
            "\n".join(
                [
                    "rule_id: low_write",
                    "target_tool: write_file",
                    "require_witness: false",
                    "risk_level: low",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        base = JudgmentBase(rules_dir)
        proxy = MCPProxy(
            cwd=self.home,
            judgment_base=base,
            immune_state=ImmuneState(),
        )
        calm = ToolInvocation(command="true", intent="modify file target.py", expected_files=("target.py",))
        hot = ToolInvocation(
            command="true",
            intent="false completion planted here",
            expected_files=("target.py",),
        )
        self.assertEqual(proxy._resolve_tier("write_file", calm), TIER_INNATE)
        self.assertEqual(proxy._resolve_tier("write_file", hot), TIER_ADAPTIVE)

    def test_missing_overlay_is_a_no_op(self) -> None:
        self.assertEqual(load_overlay(), {})
        self.assertFalse(intent_requires_full_witness("false completion", "write_file"))

    def test_precheck_writes_micro_and_fail_writes_tactical(self) -> None:
        rows = record_scale_events(
            {
                "verdict": "FAIL",
                "tool": "write_file",
                "intent": "modify file fake_dir/test.txt",
                "reason": "false completion: report says success, witness says no effect",
                "timestamp": "2026-11-13T02:00:00+00:00",
                "prechecks": [
                    {
                        "path": "fake_dir/test.txt",
                        "missing_dir": "fake_dir",
                        "timestamp": "2026-11-13T02:00:00+00:00",
                    }
                ],
            }
        )
        self.assertEqual([row["scale"] for row in rows], ["MICRO", "TACTICAL"])
        self.assertEqual(rows[0]["verdict"], "INVALID")
        self.assertEqual(rows[0]["pain_level"], 1)
        self.assertEqual(rows[0]["witness"]["missing_dir"], "fake_dir")
        self.assertEqual(rows[1]["verdict"], "FAIL")
        self.assertEqual(rows[1]["pain_level"], 2)
        stored = [
            json.loads(line)
            for line in events_path().read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len(stored), 2)


GIT = shutil.which("git")


@unittest.skipUnless(GIT, "git is required to trigger a real FAIL")
class LiveFailWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-bus-fail-")
        self.root = self._tmp.name
        self.home = os.path.join(self.root, "gradia-home")
        os.makedirs(self.home)
        self._env = mock.patch.dict(
            os.environ,
            {
                "GRADIA_HOME": self.home,
                "GRADIA_PHYSICAL_FAULTS": os.path.join(
                    self.home, "memory", "physical_faults.jsonl"
                ),
            },
            clear=False,
        )
        self._env.start()
        subprocess = __import__("subprocess")
        subprocess.run([GIT, "init", "-q", self.root], check=True)
        Path(os.path.join(self.root, "target.py")).write_text("", encoding="utf-8")
        subprocess.run([GIT, "-C", self.root, "add", "-A"], check=True)
        subprocess.run(
            [
                GIT,
                "-C",
                self.root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "init",
            ],
            check=True,
        )

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_intercepted_false_completion_lands_on_the_bus(self) -> None:
        from audit_sidecar.mcp_server import JsonRpcServer
        from audit_sidecar.runner import SafeRunner

        proxy = MCPProxy(cwd=self.root, runner=SafeRunner(timeout=20))
        writer = TraceWriter(os.path.join(self.root, "audit_trace.jsonl"))
        server = JsonRpcServer(proxy, writer)
        server._handle_tool_call(
            3,
            {
                "tool": "write_file",
                "args": {"path": "missing_dir/target.py", "content": "x\n"},
                "reported_success": True,
            },
        )
        self.assertIs(proxy.audit_log[-1].verdict, Verdict.FAIL)
        faults = [
            json.loads(line)
            for line in Path(faults_path()).read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0]["tool"], "write_file")
        self.assertIn("false completion", faults[0]["reason"])
        scale_rows = [
            json.loads(line)
            for line in events_path().read_text(encoding="utf-8").splitlines()
            if line
        ]
        scales = {row["scale"] for row in scale_rows}
        self.assertIn("MICRO", scales)
        self.assertIn("TACTICAL", scales)
        micro = next(row for row in scale_rows if row["scale"] == "MICRO")
        self.assertEqual(micro["verdict"], "INVALID")
        self.assertEqual(micro["pain_level"], 1)
        self.assertEqual(micro["witness"]["missing_dir"], "missing_dir")


if __name__ == "__main__":
    unittest.main()
