"""``harness-audit report``: a panel, not a second JSON dump."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audit_sidecar.cli as cli
from audit_sidecar.trace_panel import (
    count_verdicts,
    default_lang,
    format_row,
    load_trace,
    render_panel,
    run_report,
)


def _row(**fields):
    base = {
        "timestamp": "2026-11-12T06:00:00+00:00",
        "tool": "write_file",
        "intent": "modify file target.py",
        "verdict": "PASS",
        "reason": "witness matches intent, report agrees",
        "missing_expected": [],
        "unexpected_changes": [],
    }
    base.update(fields)
    return base


class DefaultLangTests(unittest.TestCase):
    def test_english_locale_selects_en(self):
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
            self.assertEqual(default_lang(), "en")

    def test_everything_else_selects_zh(self):
        with mock.patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}, clear=True):
            self.assertEqual(default_lang(), "zh")


class PanelRenderTests(unittest.TestCase):
    def test_pass_row_is_labelled_in_chinese(self):
        lines = format_row(_row(), lang="zh", colorize=False)
        joined = "\n".join(lines)
        self.assertIn("write_file", joined)
        self.assertIn("【PASS】", joined)
        self.assertIn("意图", joined)
        self.assertNotIn("原因", joined)

    def test_fail_row_highlights_the_lie(self):
        lines = format_row(
            _row(
                verdict="FAIL",
                reason="false completion: report says success, witness says no effect",
                missing_expected=["missing_dir/target.py"],
                unexpected_changes=["wrong.py"],
            ),
            lang="zh",
            colorize=False,
        )
        joined = "\n".join(lines)
        self.assertIn("【FAIL】", joined)
        self.assertIn("原因", joined)
        self.assertIn("false completion", joined)
        self.assertIn("没动的文件", joined)
        self.assertIn("missing_dir/target.py", joined)
        self.assertIn("多动的文件", joined)
        self.assertIn("wrong.py", joined)

    def test_english_labels_and_not_run(self):
        lines = format_row(
            _row(verdict="NOT RUN", tool="run_command"),
            lang="en",
            colorize=False,
        )
        joined = "\n".join(lines)
        self.assertIn("[NOT_RUN]", joined)
        self.assertIn("intent", joined)
        self.assertIn("run_command", joined)

    def test_invalid_uses_the_yellow_label(self):
        lines = format_row(_row(verdict="INVALID"), lang="zh", colorize=False)
        self.assertIn("【INVALID】", "\n".join(lines))

    def test_footer_counts_the_window(self):
        rows = [
            _row(verdict="PASS"),
            _row(verdict="FAIL"),
            _row(verdict="FAIL"),
            _row(verdict="INVALID"),
            _row(verdict="NOT RUN"),
        ]
        text = render_panel(rows, lang="zh", limit=5, colorize=False)
        self.assertIn("最近 5 条：PASS 1  FAIL 2  INVALID 1  NOT_RUN 1", text)
        self.assertIn("harness-audit 审计面板", text)

    def test_limit_uses_the_most_recent_rows(self):
        rows = [_row(verdict="PASS", tool="old")] + [
            _row(verdict="FAIL", tool="new") for _ in range(3)
        ]
        text = render_panel(rows, lang="en", limit=3, colorize=False)
        self.assertNotIn("old", text)
        self.assertIn("new", text)
        self.assertIn("last 3: PASS 0  FAIL 3", text)

    def test_ansi_colours_when_requested(self):
        lines = format_row(_row(verdict="FAIL"), lang="zh", colorize=True)
        joined = "\n".join(lines)
        self.assertIn("\033[31m", joined)
        self.assertIn("\033[1;31m", joined)


class LoadAndCliTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.trace = self.root / "audit_trace.jsonl"
        self.trace.write_text(
            json.dumps(_row(verdict="PASS"))
            + "\n"
            + json.dumps(
                _row(
                    verdict="FAIL",
                    tool="run_command",
                    intent="run command: printf x > wrong.py",
                    reason="false completion: expected target.py",
                    missing_expected=["target.py"],
                    unexpected_changes=["wrong.py"],
                )
            )
            + "\n",
            encoding="utf-8",
        )

    def test_load_trace_reads_jsonl(self):
        rows = load_trace(self.trace)
        self.assertEqual(len(rows), 2)
        self.assertEqual(count_verdicts(rows)["FAIL"], 1)

    def test_run_report_zh(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_report(
                ["--lang", "zh", "--no-color", "--trace", str(self.trace)]
            )
        self.assertEqual(code, 0, err.getvalue())
        body = out.getvalue()
        self.assertIn("【PASS】", body)
        self.assertIn("【FAIL】", body)
        self.assertIn("没动的文件", body)
        self.assertIn("target.py", body)

    def test_run_report_en(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_report(
                ["--lang", "en", "--no-color", "--trace", str(self.trace)]
            )
        self.assertEqual(code, 0)
        body = out.getvalue()
        self.assertIn("[PASS]", body)
        self.assertIn("[FAIL]", body)
        self.assertIn("missing_expected", body)
        self.assertIn("unexpected_changes", body)

    def test_missing_trace_is_a_nonzero_exit(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run_report(["--lang", "zh", "--trace", str(self.root / "nope.jsonl")])
        self.assertEqual(code, 1)
        self.assertIn("audit_trace.jsonl", err.getvalue())

    def test_cli_dispatch_reaches_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(
                ["report", "--lang", "en", "--no-color", "--trace", str(self.trace)]
            )
        self.assertEqual(code, 0)
        self.assertIn("[FAIL]", out.getvalue())

    def test_server_entry_dispatches_report(self):
        from audit_sidecar.mcp_server import main as server_main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = server_main(
                ["report", "--lang", "zh", "--no-color", "--trace", str(self.trace)]
            )
        self.assertEqual(code, 0)
        self.assertIn("【FAIL】", out.getvalue())


if __name__ == "__main__":
    unittest.main()
