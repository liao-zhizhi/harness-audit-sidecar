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
    load_recent_pain_events,
    load_recent_plans,
    procedural_path,
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
        recent = load_recent_pain_events(limit=5, min_pain_level=1)
        self.assertEqual({row["scale"] for row in recent}, {"MICRO", "TACTICAL"})
        by_scale = {row["scale"]: row for row in recent}
        self.assertEqual(by_scale["TACTICAL"]["pain_level"], 2)
        self.assertIn("missing_dir=fake_dir", by_scale["MICRO"]["witness_summary"])

    def test_pain_lookup_is_not_traced_or_judged(self) -> None:
        record_scale_events(
            {
                "verdict": "FAIL",
                "tool": "write_file",
                "intent": "modify file fake_dir/test.txt",
                "reason": "false completion",
                "timestamp": "2026-11-13T03:00:00+00:00",
                "prechecks": [
                    {
                        "path": "fake_dir/test.txt",
                        "missing_dir": "fake_dir",
                        "timestamp": "2026-11-13T03:00:00+00:00",
                    }
                ],
            }
        )
        from audit_sidecar.mcp_server import JsonRpcServer
        from audit_sidecar.runner import SafeRunner

        trace = os.path.join(self.home, "pain_lookup_trace.jsonl")
        proxy = MCPProxy(cwd=self.home, runner=SafeRunner(timeout=5))
        server = JsonRpcServer(proxy, TraceWriter(trace))
        payload = server._handle_tools_call(
            9,
            {"name": "get_recent_pain_events", "arguments": {"limit": 5}},
        )
        self.assertFalse(payload.get("isError"))
        body = json.loads(payload["content"][0]["text"])
        self.assertGreaterEqual(len(body["events"]), 2)
        self.assertNotIn("verdict", payload)
        lines = [
            line
            for line in Path(trace).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(lines, [])

    def test_human_correction_lookup_is_not_traced_or_judged(self) -> None:
        from audit_sidecar.mcp_server import JsonRpcServer
        from audit_sidecar.runner import SafeRunner

        trace = os.path.join(self.home, "correction_trace.jsonl")
        proxy = MCPProxy(cwd=self.home, runner=SafeRunner(timeout=5))
        server = JsonRpcServer(proxy, TraceWriter(trace))
        payload = server._handle_tools_call(
            10,
            {
                "name": "record_human_correction",
                "arguments": {
                    "narrative_ref": "evt-x",
                    "human_action": "mkdir then write",
                    "reported_alignment": "aligned",
                    "evidence": "not captured",
                },
            },
        )
        self.assertFalse(payload.get("isError"))
        self.assertNotIn("verdict", payload)
        body = json.loads(payload["content"][0]["text"])
        self.assertIn("ok", body)
        lines = [
            line
            for line in Path(trace).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(lines, [])

    def test_recent_corrections_lookup_is_not_traced_or_judged(self) -> None:
        from audit_sidecar.mcp_server import JsonRpcServer
        from audit_sidecar.memory_bus import load_recent_corrections
        from audit_sidecar.runner import SafeRunner

        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "agent_sentience.multi_scale.v1",
                    "event_id": "hc-1",
                    "scale": "TACTICAL",
                    "source": "human_correction",
                    "intent": "先 mkdir -p 再写",
                    "human_action": "先 mkdir -p 再写",
                    "reported_alignment": "aligned",
                    "reported_by": "agent",
                    "evidence": "fake_dir/test.txt exists",
                    "narrative_ref": "evt-n",
                    "verdict": "PASS",
                    "pain_level": 0,
                    "timestamp": "2026-11-13T06:00:00+00:00",
                    "project_root": self.home,
                    "relative_path": "fake_dir/test.txt",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        listed = load_recent_corrections(limit=5, project_root=self.home)
        self.assertEqual(listed[0]["human_action"], "先 mkdir -p 再写")
        self.assertEqual(listed[0]["reported_alignment"], "aligned")
        self.assertEqual(listed[0]["evidence"], "fake_dir/test.txt exists")
        self.assertEqual(listed[0]["narrative_ref"], "evt-n")
        trace = os.path.join(self.home, "corrections_lookup_trace.jsonl")
        proxy = MCPProxy(cwd=self.home, runner=SafeRunner(timeout=5))
        server = JsonRpcServer(proxy, TraceWriter(trace))
        payload = server._handle_tools_call(
            11,
            {"name": "get_recent_corrections", "arguments": {"limit": 5}},
        )
        self.assertFalse(payload.get("isError"))
        body = json.loads(payload["content"][0]["text"])
        self.assertEqual(body["events"][0]["reported_alignment"], "aligned")
        self.assertNotIn("verdict", payload)
        lines = [
            line
            for line in Path(trace).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(lines, [])

    def test_recent_plans_lookup_is_not_traced_or_judged(self) -> None:
        from audit_sidecar.mcp_server import JsonRpcServer
        from audit_sidecar.runner import SafeRunner

        path = procedural_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "trigger": "写文件到新路径",
                    "action": "先检查父目录是否存在，不存在则 mkdir -p 再写",
                    "source_evidence": ["evt-a", "evt-b"],
                    "source": "human_correction",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        listed = load_recent_plans(limit=3)
        self.assertEqual(listed[0]["trigger"], "写文件到新路径")
        self.assertIn("mkdir -p", listed[0]["action"])
        self.assertEqual(listed[0]["source_evidence"], ["evt-a", "evt-b"])
        trace = os.path.join(self.home, "plans_lookup_trace.jsonl")
        proxy = MCPProxy(cwd=self.home, runner=SafeRunner(timeout=5))
        server = JsonRpcServer(proxy, TraceWriter(trace))
        payload = server._handle_tools_call(
            12,
            {"name": "get_recent_plans", "arguments": {"limit": 3}},
        )
        self.assertFalse(payload.get("isError"))
        body = json.loads(payload["content"][0]["text"])
        self.assertEqual(body["plans"][0]["action"], listed[0]["action"])
        self.assertNotIn("verdict", payload)
        lines = [
            line
            for line in Path(trace).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(lines, [])

    def test_probe_target_is_not_traced_or_judged(self) -> None:
        from audit_sidecar.mcp_server import JsonRpcServer
        from audit_sidecar.runner import SafeRunner

        present = os.path.join(self.home, "probe_present.txt")
        Path(present).write_text("abc", encoding="utf-8")
        link = os.path.join(self.home, "probe_link")
        os.symlink(present, link)
        dangling = os.path.join(self.home, "probe_dangling")
        os.symlink(os.path.join(self.home, "no_such_target"), dangling)
        missing = os.path.join(self.home, "probe_missing", "test.txt")

        trace = os.path.join(self.home, "probe_trace.jsonl")
        proxy = MCPProxy(cwd=self.home, runner=SafeRunner(timeout=5))
        server = JsonRpcServer(proxy, TraceWriter(trace))
        names = [tool["name"] for tool in server._handle_tools_list({})["tools"]]
        self.assertIn("probe_target", names)

        missing_payload = server._handle_tools_call(
            21,
            {"name": "probe_target", "arguments": {"path": missing}},
        )
        self.assertFalse(missing_payload.get("isError"))
        self.assertNotIn("verdict", missing_payload)
        missing_body = json.loads(missing_payload["content"][0]["text"])
        self.assertEqual(missing_body["exists"], False)
        self.assertEqual(missing_body["parent_exists"], False)
        self.assertEqual(missing_body["parent_writable"], False)
        self.assertEqual(missing_body["is_symlink"], False)
        self.assertIsNone(missing_body["symlink_target"])
        self.assertIsNone(missing_body["mtime"])
        self.assertIsNone(missing_body["size"])
        self.assertFalse(os.path.isdir(os.path.dirname(missing)))

        present_body = json.loads(
            server._handle_tools_call(
                22,
                {
                    "name": "probe_target",
                    "arguments": {"path": "probe_present.txt"},
                },
            )["content"][0]["text"]
        )
        self.assertTrue(present_body["exists"])
        self.assertTrue(present_body["parent_exists"])
        self.assertTrue(present_body["parent_writable"])
        self.assertFalse(present_body["is_symlink"])
        self.assertIsNone(present_body["symlink_target"])
        self.assertEqual(present_body["size"], 3)
        self.assertIsInstance(present_body["mtime"], float)

        link_body = json.loads(
            server._handle_tools_call(
                23,
                {"name": "probe_target", "arguments": {"path": "probe_link"}},
            )["content"][0]["text"]
        )
        self.assertTrue(link_body["is_symlink"])
        self.assertEqual(link_body["symlink_target"], present)
        self.assertTrue(link_body["exists"])
        self.assertEqual(link_body["size"], len(os.readlink(link).encode()))

        dangling_body = json.loads(
            server._handle_tools_call(
                24,
                {"name": "probe_target", "arguments": {"path": "probe_dangling"}},
            )["content"][0]["text"]
        )
        self.assertFalse(dangling_body["exists"])
        self.assertTrue(dangling_body["is_symlink"])
        self.assertTrue(str(dangling_body["symlink_target"]).endswith("no_such_target"))

        empty_body = json.loads(
            server._handle_tools_call(
                25,
                {"name": "probe_target", "arguments": {"path": "  "}},
            )["content"][0]["text"]
        )
        self.assertEqual(empty_body, {"error": "path is required"})

        legacy = server._handle_tool_call(
            26,
            {"tool": "probe_target", "args": {"path": "probe_present.txt"}},
        )
        self.assertNotIn("verdict", legacy["result"])
        self.assertEqual(Path(present).read_text(encoding="utf-8"), "abc")
        lines = [
            line
            for line in Path(trace).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(lines, [])

    def test_same_trace_id_writes_one_tactical(self) -> None:
        payload = {
            "verdict": "FAIL",
            "tool": "write_file",
            "intent": "modify file missing_dir/target.py",
            "reason": "false completion",
            "timestamp": "2026-11-13T08:00:00+00:00",
            "id": 42,
        }
        first = record_scale_events(payload)
        second = record_scale_events(payload)
        self.assertEqual([row["scale"] for row in first], ["TACTICAL"])
        stored = [
            json.loads(line)
            for line in events_path().read_text(encoding="utf-8").splitlines()
            if line
        ]
        tactical = [row for row in stored if row["scale"] == "TACTICAL"]
        self.assertEqual(len(tactical), 1)
        self.assertEqual(str(tactical[0]["extra"]["trace_id"]), "42")
        self.assertEqual(tactical[0]["source"], "sidecar")
        self.assertEqual(second[0]["event_id"], tactical[0]["event_id"])

    def test_read_by_empty_is_skipped_by_plans(self) -> None:
        path = procedural_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "trigger": "无人读取",
                    "action": "仍有 action",
                    "read_by": [],
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "trigger": "写文件到新路径",
                    "action": "先检查父目录",
                    "read_by": ["get_recent_plans", "distill_inject"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        listed = load_recent_plans(limit=3)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["trigger"], "写文件到新路径")


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
        self.assertEqual(sum(1 for row in scale_rows if row["scale"] == "TACTICAL"), 1)
        micro = next(row for row in scale_rows if row["scale"] == "MICRO")
        self.assertEqual(micro["verdict"], "INVALID")
        self.assertEqual(micro["pain_level"], 1)
        self.assertEqual(micro["witness"]["missing_dir"], "missing_dir")


if __name__ == "__main__":
    unittest.main()
