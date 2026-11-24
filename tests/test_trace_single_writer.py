"""One trace, one writer.

The guard exists because a client can leave a previous sidecar alive. Cursor
restarts an MCP server when ``~/.cursor/mcp.json`` changes and does not always
reap the child it started, so two processes can append to the same
``audit_trace.jsonl``. Two interleaved verdict streams cannot be trusted
afterwards, so the writer that loses the race must stop appending.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from audit_sidecar.mcp_server import TraceWriter


class SingleWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = scratch.name
        self.path = os.path.join(self.root, "audit_trace.jsonl")

    def writer(self, path: str) -> TraceWriter:
        made = TraceWriter(path)
        self.addCleanup(made.close)
        return made

    def records(self, path: str | None = None) -> list:
        with open(path or self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_first_writer_appends(self) -> None:
        first = self.writer(self.path)
        self.assertTrue(first._exclusive)
        first.write({"verdict": "PASS", "tool": "write_file"})
        self.assertEqual([row["verdict"] for row in self.records()], ["PASS"])

    def test_second_writer_answers_but_does_not_append(self) -> None:
        first = self.writer(self.path)
        second = self.writer(self.path)
        self.assertTrue(first._exclusive)
        self.assertFalse(second._exclusive)
        first.write({"verdict": "PASS", "source": "first"})
        second.write({"verdict": "FAIL", "source": "second"})
        second.write({"verdict": "FAIL", "source": "second"})
        self.assertEqual([row.get("source") for row in self.records()], ["first"])
        self.assertEqual(second._refused_writes, 2)

    def test_a_released_trace_can_be_claimed_again(self) -> None:
        first = self.writer(self.path)
        self.assertTrue(first._exclusive)
        first.close()
        second = self.writer(self.path)
        self.assertTrue(second._exclusive)
        second.write({"verdict": "PASS", "source": "second"})
        self.assertEqual([row.get("source") for row in self.records()], ["second"])

    def test_distinct_traces_do_not_contend(self) -> None:
        other = os.path.join(self.root, "other_trace.jsonl")
        first = self.writer(self.path)
        second = self.writer(other)
        self.assertTrue(first._exclusive)
        self.assertTrue(second._exclusive)
        first.write({"verdict": "PASS", "trace": "a"})
        second.write({"verdict": "PASS", "trace": "b"})
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.records(other)), 1)


if __name__ == "__main__":
    unittest.main()
