"""Human-readable panel for ``audit_trace.jsonl``.

``harness-audit report`` exists so an operator does not have to ``cat`` English
JSON to see what the sidecar already decided. It does not re-judge: every row
is a record that was written at call time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

TRACE_NAME = "audit_trace.jsonl"
DEFAULT_LIMIT = 20

_ANSI = {
    "PASS": "\033[32m",
    "FAIL": "\033[31m",
    "INVALID": "\033[33m",
    "NOT RUN": "\033[90m",
    "reset": "\033[0m",
    "bold": "\033[1m",
    "fail_hl": "\033[1;31m",
}

_LABEL = {
    "zh": {
        "PASS": "【PASS】",
        "FAIL": "【FAIL】",
        "INVALID": "【INVALID】",
        "NOT RUN": "【NOT_RUN】",
        "time": "时间",
        "tool": "工具",
        "intent": "意图",
        "reason": "原因",
        "missing": "没动的文件",
        "unexpected": "多动的文件",
        "none": "（无）",
        "empty": "当前目录没有 audit_trace.jsonl。先跑一次被审计的工具调用。",
        "bad": "无法读取 {path}：{error}",
        "title": "harness-audit 审计面板",
        "stats": "最近 {n} 条：PASS {pass_}  FAIL {fail}  INVALID {invalid}  NOT_RUN {not_run}",
        "no_rows": "文件是空的。",
    },
    "en": {
        "PASS": "[PASS]",
        "FAIL": "[FAIL]",
        "INVALID": "[INVALID]",
        "NOT RUN": "[NOT_RUN]",
        "time": "time",
        "tool": "tool",
        "intent": "intent",
        "reason": "reason",
        "missing": "missing_expected",
        "unexpected": "unexpected_changes",
        "none": "(none)",
        "empty": "No audit_trace.jsonl in the current directory. Run an audited tool call first.",
        "bad": "Cannot read {path}: {error}",
        "title": "harness-audit report",
        "stats": "last {n}: PASS {pass_}  FAIL {fail}  INVALID {invalid}  NOT_RUN {not_run}",
        "no_rows": "The trace file is empty.",
    },
}


def default_lang() -> str:
    """Follow the locale when it is English; otherwise Chinese."""
    blob = (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    ).lower()
    if blob.startswith("en"):
        return "en"
    return "zh"


def load_trace(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _verdict_key(row: Dict[str, Any]) -> str:
    value = str(row.get("verdict") or "")
    if value in ("NOT_RUN", "NOT RUN"):
        return "NOT RUN"
    return value


def count_verdicts(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "INVALID": 0, "NOT RUN": 0}
    for row in rows:
        key = _verdict_key(row)
        if key in counts:
            counts[key] += 1
    return counts


def _paint(text: str, color: str, *, colorize: bool) -> str:
    if not colorize:
        return text
    return color + text + _ANSI["reset"]


def _files(values: Any) -> str:
    if not values:
        return ""
    if isinstance(values, str):
        return values
    return ", ".join(str(item) for item in values if item)


def format_row(row: Dict[str, Any], *, lang: str, colorize: bool) -> List[str]:
    labels = _LABEL[lang]
    key = _verdict_key(row)
    badge = labels.get(key, "[{}]".format(key or "?"))
    color = _ANSI.get(key, "")
    marked = _paint(badge, color, colorize=colorize)
    stamp = str(row.get("timestamp") or "—")
    tool = str(row.get("tool") or "—")
    intent = str(row.get("intent") or "—")
    lines = [
        "{}  {}  {}".format(stamp, tool, marked),
        "{}: {}".format(labels["intent"], intent),
    ]
    if key == "FAIL":
        reason = str(row.get("reason") or "")
        missing = _files(row.get("missing_expected")) or labels["none"]
        unexpected = _files(row.get("unexpected_changes")) or labels["none"]
        hl = _ANSI["fail_hl"] if colorize else ""
        reset = _ANSI["reset"] if colorize else ""
        lines.append("{}{}: {}{}".format(hl, labels["reason"], reason, reset))
        lines.append("{}{}: {}{}".format(hl, labels["missing"], missing, reset))
        lines.append(
            "{}{}: {}{}".format(hl, labels["unexpected"], unexpected, reset)
        )
    return lines


def render_panel(
    rows: Sequence[Dict[str, Any]],
    *,
    lang: str = "zh",
    limit: int = DEFAULT_LIMIT,
    colorize: bool = False,
) -> str:
    labels = _LABEL[lang]
    if not rows:
        return labels["no_rows"]
    shown = list(rows[-limit:]) if limit > 0 else list(rows)
    counts = count_verdicts(shown)
    chunks = [_paint(labels["title"], _ANSI["bold"], colorize=colorize)]
    width = 72
    rule = "─" * width
    chunks.append(rule)
    for index, row in enumerate(shown):
        if index:
            chunks.append("")
        chunks.extend(format_row(row, lang=lang, colorize=colorize))
    chunks.append(rule)
    chunks.append(
        labels["stats"].format(
            n=len(shown),
            pass_=counts["PASS"],
            fail=counts["FAIL"],
            invalid=counts["INVALID"],
            not_run=counts["NOT RUN"],
        )
    )
    body = "\n".join(chunks)
    if not colorize:
        return body
    try:
        from rich.console import Console
        from rich.panel import Panel
    except ImportError:
        return body
    console = Console(no_color=False, width=80, highlight=False)
    with console.capture() as captured:
        console.print(Panel(body, title=labels["title"], expand=False))
    painted = captured.get().rstrip()
    return painted or body


def _colorize(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def run_report(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness-audit report",
        description="Render audit_trace.jsonl as a terminal panel.",
    )
    parser.add_argument(
        "--lang",
        choices=("zh", "en"),
        default=None,
        help="panel language (default: zh, or en when LANG starts with en)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="how many recent rows to show (default: 20; 0 means all)",
    )
    parser.add_argument(
        "--trace",
        default=TRACE_NAME,
        help="trace file (default: ./audit_trace.jsonl)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colours",
    )
    options = parser.parse_args(list(argv or []))
    lang = options.lang or default_lang()
    labels = _LABEL[lang]
    path = Path(options.trace)
    if not path.is_file():
        print(labels["empty"], file=sys.stderr)
        return 1
    try:
        rows = load_trace(path)
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        print(labels["bad"].format(path=path, error=error), file=sys.stderr)
        return 1
    colorize = (not options.no_color) and _colorize(sys.stdout)
    print(render_panel(rows, lang=lang, limit=options.limit, colorize=colorize))
    return 0
