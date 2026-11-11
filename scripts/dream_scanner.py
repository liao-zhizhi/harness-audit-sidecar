#!/usr/bin/env python3
"""Offline dream: cluster non-PASS traces into rule-gap suggestions.

Reads ``audit_trace.jsonl`` files. Never opens a file under ``rules/`` for
writing. Candidate gaps are appended to the case-library queue
(``~/.gradia/case_library/private/rule_gaps.jsonl``), which is the slot the
architecture audit named as safe. A gap is a suggestion, not a verdict, and
does not enter PASS/FAIL/INVALID.

Usage:

    python3 scripts/dream_scanner.py /path/to/traces
    python3 scripts/dream_scanner.py /path/to/traces --min-frequency 2
    python3 scripts/dream_scanner.py /path/to/traces --gaps-file /tmp/gaps.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

TRACE_NAME = "audit_trace.jsonl"
DEFAULT_MIN_FREQUENCY = 3
GAP_PREFIX = "GAP_"
ClusterKey = Tuple[str, str, str]


def default_gaps_path() -> Path:
    """The live queue. ``private/`` is where SCHEMA already put it."""
    return (
        Path.home()
        / ".gradia"
        / "case_library"
        / "private"
        / "rule_gaps.jsonl"
    )


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def iter_trace_files(root: Path) -> Iterator[Path]:
    if root.is_file() and root.name == TRACE_NAME:
        yield root
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Do not descend into a rules/ tree looking for traces: dreaming
        # must not even appear to treat policy as an input it can rewrite.
        dirnames[:] = [name for name in dirnames if name != "rules"]
        if TRACE_NAME in filenames:
            yield Path(dirpath) / TRACE_NAME


def load_records(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as problem:
        print("dream_scanner: skip {}: {}".format(path, problem), file=sys.stderr)
        return records
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError as problem:
            print(
                "dream_scanner: skip {}:{}: {}".format(path, line_no, problem),
                file=sys.stderr,
            )
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def cluster_key(record: Dict[str, Any]) -> Optional[ClusterKey]:
    """(tool, verdict, rule_id). Missing new fields are tolerated."""
    verdict = record.get("verdict")
    if not isinstance(verdict, str) or verdict == "PASS":
        return None
    tool = record.get("tool")
    if not isinstance(tool, str) or not tool:
        return None
    rule_id = record.get("rule_id")
    if rule_id is None or rule_id == "":
        rule_norm = ""
    elif isinstance(rule_id, str):
        rule_norm = rule_id
    else:
        rule_norm = str(rule_id)
    return tool, verdict, rule_norm


def next_gap_id(existing: Iterable[Dict[str, Any]]) -> str:
    highest = 0
    for entry in existing:
        gap_id = entry.get("gap_id")
        if not isinstance(gap_id, str) or not gap_id.startswith(GAP_PREFIX):
            continue
        suffix = gap_id[len(GAP_PREFIX) :]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return "{}{:04d}".format(GAP_PREFIX, highest + 1)


def load_gaps(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    entries: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def already_queued(existing: List[Dict[str, Any]], key: ClusterKey) -> bool:
    tool, verdict, rule_norm = key
    for entry in existing:
        if entry.get("status") not in (None, "open"):
            continue
        if entry.get("source") != "dream_scanner":
            continue
        if entry.get("tool") != tool:
            continue
        if entry.get("verdict") != verdict:
            continue
        queued_rule = entry.get("rule_id")
        queued_norm = "" if queued_rule in (None, "") else str(queued_rule)
        if queued_norm == rule_norm:
            return True
    return False


def describe_gap(key: ClusterKey, frequency: int, sample: Dict[str, Any]) -> str:
    tool, verdict, rule_norm = key
    if rule_norm:
        rule_clause = "现有规则 {!r} 已命中但仍反复 {}".format(rule_norm, verdict)
    else:
        rule_clause = "该工具没有命中任何规则（rule_id 为空）"
    unexpected = sample.get("unexpected_changes") or []
    extra = ""
    if isinstance(unexpected, list) and unexpected:
        extra = "；失败含 unexpected_changes，当前 YAML 期望词汇写不出这一条"
    return (
        "dream_scanner: 高频失败 tool={!r} verdict={!r} 频次={}；{}{}".format(
            tool, verdict, frequency, rule_clause, extra
        )
    )


def build_gap(
    gap_id: str,
    key: ClusterKey,
    frequency: int,
    sample: Dict[str, Any],
    traces: List[str],
) -> Dict[str, Any]:
    tool, verdict, rule_norm = key
    offered = sample.get("next_step_offered")
    return {
        "gap_id": gap_id,
        "case_id": None,
        "source": "dream_scanner",
        "status": "open",
        "created_at": iso_now(),
        "tool": tool,
        "verdict": verdict,
        "rule_id": rule_norm or None,
        "frequency": frequency,
        "next_step_offered": offered if isinstance(offered, bool) else None,
        "missing": describe_gap(key, frequency, sample),
        "trace_files": traces,
        "sample_reason": sample.get("reason"),
        "sample_session_id": sample.get("session_id"),
    }


def scan(
    root: Path,
    min_frequency: int,
) -> Tuple[List[Tuple[ClusterKey, int, Dict[str, Any], List[str]]], int]:
    clusters: Dict[ClusterKey, int] = Counter()
    samples: Dict[ClusterKey, Dict[str, Any]] = {}
    origins: Dict[ClusterKey, List[str]] = defaultdict(list)
    record_count = 0
    for trace_path in iter_trace_files(root):
        for record in load_records(trace_path):
            record_count += 1
            key = cluster_key(record)
            if key is None:
                continue
            clusters[key] += 1
            if key not in samples:
                samples[key] = record
            location = str(trace_path)
            if location not in origins[key]:
                origins[key].append(location)
    hot: List[Tuple[ClusterKey, int, Dict[str, Any], List[str]]] = []
    for key, frequency in sorted(clusters.items(), key=lambda item: (-item[1], item[0])):
        if frequency < min_frequency:
            continue
        hot.append((key, frequency, samples[key], origins[key]))
    return hot, record_count


def append_gaps(
    gaps_path: Path,
    hot: List[Tuple[ClusterKey, int, Dict[str, Any], List[str]]],
) -> List[Dict[str, Any]]:
    existing = load_gaps(gaps_path)
    written: List[Dict[str, Any]] = []
    gaps_path.parent.mkdir(parents=True, exist_ok=True)
    with gaps_path.open("a", encoding="utf-8") as handle:
        for key, frequency, sample, traces in hot:
            if already_queued(existing + written, key):
                print(
                    "dream_scanner: skip already-open gap for {}".format(key),
                    file=sys.stderr,
                )
                continue
            gap = build_gap(
                next_gap_id(existing + written),
                key,
                frequency,
                sample,
                traces,
            )
            handle.write(json.dumps(gap, ensure_ascii=False) + "\n")
            written.append(gap)
    return written


def assert_rules_untouched(root: Path, before: Dict[Path, Tuple[int, int]]) -> None:
    """Refuse to finish if any rules/*.yaml changed while we ran."""
    after = snapshot_rules(root)
    if after != before:
        raise SystemExit(
            "dream_scanner: refused: a rules/ YAML changed during the scan. "
            "This script must never write policy."
        )


def snapshot_rules(root: Path) -> Dict[Path, Tuple[int, int]]:
    snapped: Dict[Path, Tuple[int, int]] = {}
    if not root.is_dir():
        return snapped
    for dirpath, _dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) != "rules":
            continue
        for name in filenames:
            if not name.lower().endswith((".yaml", ".yml")):
                continue
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            snapped[path] = (stat.st_mtime_ns, stat.st_size)
    return snapped


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cluster non-PASS audit traces into rule-gap suggestions. "
            "Never writes YAML under rules/."
        )
    )
    parser.add_argument(
        "root",
        help="directory (or a single audit_trace.jsonl) to scan",
    )
    parser.add_argument(
        "--min-frequency",
        type=int,
        default=DEFAULT_MIN_FREQUENCY,
        help="cluster size that becomes a gap (default: 3)",
    )
    parser.add_argument(
        "--gaps-file",
        default=None,
        help=(
            "jsonl to append (default: ~/.gradia/case_library/private/"
            "rule_gaps.jsonl)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print candidates and write nothing",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    options = parse_args(argv)
    if options.min_frequency < 1:
        print("dream_scanner: --min-frequency must be >= 1", file=sys.stderr)
        return 2
    root = Path(options.root).expanduser().resolve()
    if not root.exists():
        print("dream_scanner: path not found: {}".format(root), file=sys.stderr)
        return 2
    gaps_path = (
        Path(options.gaps_file).expanduser()
        if options.gaps_file
        else default_gaps_path()
    )

    sidecar_root = Path(__file__).resolve().parents[1]
    before_scan_rules = snapshot_rules(root)
    before_pkg_rules = snapshot_rules(sidecar_root / "audit_sidecar")

    hot, record_count = scan(root, options.min_frequency)
    print(
        "dream_scanner: scanned {} records under {}; {} cluster(s) "
        "with frequency >= {}".format(
            record_count, root, len(hot), options.min_frequency
        )
    )
    for key, frequency, _sample, _traces in hot:
        tool, verdict, rule_norm = key
        print(
            "  {}  tool={}  verdict={}  rule_id={}".format(
                frequency, tool, verdict, rule_norm or "(none)"
            )
        )

    if options.dry_run:
        print("dream_scanner: dry-run, no gaps written")
        assert_rules_untouched(root, before_scan_rules)
        assert_rules_untouched(sidecar_root / "audit_sidecar", before_pkg_rules)
        return 0

    written = append_gaps(gaps_path, hot)
    print(
        "dream_scanner: appended {} gap(s) to {}".format(len(written), gaps_path)
    )
    for gap in written:
        print("  {}".format(gap["gap_id"]))

    assert_rules_untouched(root, before_scan_rules)
    assert_rules_untouched(sidecar_root / "audit_sidecar", before_pkg_rules)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
