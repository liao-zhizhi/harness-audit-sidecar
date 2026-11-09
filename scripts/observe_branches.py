#!/usr/bin/env python3
"""Survey what external workflows actually branch on.

The post-release question is not "did anyone read the README". It is "which
signal did the workflows that adopted the Action end up conditioning on". That
distinction decides whether the deferred fourth output is worth adding, and it
cannot be answered by looking at documentation -- only at real workflow files.

This is a survey tool, not a parser. It scans workflow YAML as text, so it has
no opinion about indentation, anchors or merge keys. Every classification is a
line-level heuristic, and the one that matters is deliberately conservative:
a reference counts as *conditioning* only when the same line compares it
(``if:``, ``==``, ``!=``, ``case``, ``elif``). A value that is merely echoed
into a log line is recorded as `echoed` and does not change the class.

Classes, in priority order:

``fourth_output``   references ``failure_kind`` -- someone already believes this
                    output exists. That is an incident, not a survey result.
``reason_prefix``   conditions on ``reason`` and carries one of the published
                    prefixes: the documented path.
``reason_other``    conditions on ``reason`` without a published prefix (a
                    substring match, a `contains`, something bespoke).
``verdict_only``    conditions on ``verdict`` only. The README says never to do
                    this; enough of these is the signal to add the output.
``exit_code_only``  conditions on ``exit_code`` only.
``no_branch``       never conditions on any output (echo, or no usage at all).

Usage:

    python3 scripts/observe_branches.py path/to/workflows/
    python3 scripts/observe_branches.py .github/workflows/ci.yml --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from audit_sidecar.check import (  # noqa: E402  (path set up above)
    MALFORMED_REASON_PREFIX,
    NO_DECLARATION_REASON_PREFIX,
    SELF_AUDIT_REASON_PREFIX,
)

#: Keyed on the same constants the classifier reads, so a rename cannot leave
#: this survey looking for a prefix nobody emits any more.
CONTRACT_PREFIXES: Tuple[str, ...] = (
    SELF_AUDIT_REASON_PREFIX,
    MALFORMED_REASON_PREFIX,
    NO_DECLARATION_REASON_PREFIX,
)

_FIELD = re.compile(r"outputs\.([A-Za-z_][A-Za-z0-9_]*)")
#: A line is conditioning if it compares the reference to something, or if it
#: opens a shell `case` on it. `echo "..."` is not.
_CONDITION = re.compile(r"(if:|==|!=|\bcase\b|\belif\b|\bwhen:)")
#: ``reason="${{ steps.audit.outputs.reason }}"`` names a shell variable after
#: an output. The recommended pattern then branches on the *variable*
#: (``case "${reason}" in``), a line that mentions no output at all -- so the
#: alias has to be followed or the documented usage would classify as
#: "echoed only". This is the one piece of indirection the survey follows, and
#: it is followed only one hop.
_ALIAS = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)=.*?outputs\.([A-Za-z_][A-Za-z0-9_]*)"
)

#: A trailing comment is not code. The README itself suggests an alternative in
#: one: `continue-on-error: true   # or: if: ...verdict != 'INVALID'`, which
#: would otherwise classify a workflow that branches on nothing as
#: `verdict_only` and inflate the one number the decision turns on. The `#`
#: must be preceded by whitespace, so shell `${reason#prefix}` survives.
_COMMENT = re.compile(r"(?:^|\s)#.*$", re.MULTILINE)

_YAML_SUFFIXES = (".yml", ".yaml")


def _classify(text: str) -> Dict[str, Any]:
    """Classify one workflow file. Pure, so the tests can pin each branch."""
    lines = []
    stripped = _COMMENT.sub("", text)
    for line in stripped.splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    aliases: Dict[str, str] = {}
    for line in lines:
        match = _ALIAS.match(line)
        if match:
            aliases[match.group(1)] = match.group(2)

    referenced: Dict[str, int] = {}
    conditioned: Dict[str, int] = {}
    prefixes_seen: List[str] = []

    for line in lines:
        fields = _FIELD.findall(line)
        # Follow one hop: a condition on "${alias}" is a condition on the
        # output that alias was assigned from.
        for alias, field in aliases.items():
            if field in fields:
                continue
            # `\$\{?` + alias: `$reason` and `${reason}` are both references.
            if re.search(r"\$\{?" + re.escape(alias) + r"\b", line):
                fields.append(field)
        if not fields:
            continue
        is_condition = bool(_CONDITION.search(line))
        for name in set(fields):
            referenced[name] = referenced.get(name, 0) + 1
            if is_condition:
                conditioned[name] = conditioned.get(name, 0) + 1
        if is_condition and "reason" in fields:
            for prefix in CONTRACT_PREFIXES:
                if prefix in line and prefix not in prefixes_seen:
                    prefixes_seen.append(prefix)

    # Prefix labels live on the branch lines ("self-audit failed"*) while the
    # `case` subject is the line mentioning `outputs.reason`. Both are in the
    # same file, so a file-level scan is the honest unit here.
    if conditioned.get("failure_kind") or referenced.get("failure_kind"):
        kind = "fourth_output"
    elif conditioned.get("reason"):
        for prefix in CONTRACT_PREFIXES:
            if prefix in stripped and prefix not in prefixes_seen:
                prefixes_seen.append(prefix)
        kind = "reason_prefix" if prefixes_seen else "reason_other"
    elif conditioned.get("verdict"):
        kind = "verdict_only"
    elif conditioned.get("exit_code"):
        kind = "exit_code_only"
    else:
        kind = "no_branch"

    return {
        "class": kind,
        "conditioned": dict(sorted(conditioned.items())),
        "echoed_only": sorted(name for name in referenced if name not in conditioned),
        "prefixes": prefixes_seen,
    }


def _action_for(kind: str) -> str:
    """What a class means for the deferred fourth output, in one sentence."""
    return {
        "fourth_output": (
            "adopt the output now: a workflow already assumes it exists, so the "
            "docs are not the fix -- derive it from CheckOutcome.failure_kind"
        ),
        "reason_prefix": "documented path; no action",
        "reason_other": (
            "read the match: a substring test on a published prefix is fine, "
            "anything bespoke means the prefix table is not carrying the load"
        ),
        "verdict_only": (
            "the signal that would justify the fourth output; count these before "
            "adding it, do not decide from one sample"
        ),
        "exit_code_only": (
            "expected of any gate that just wants to block; no action"
        ),
        "no_branch": (
            "the output decision is moot here; nothing conditions on it"
        ),
    }[kind]


def _iter_files(targets: Iterable[str]) -> List[str]:
    paths: List[str] = []
    for target in targets:
        if os.path.isdir(target):
            for base, _dirs, names in os.walk(target):
                for name in sorted(names):
                    if name.endswith(_YAML_SUFFIXES):
                        paths.append(os.path.join(base, name))
        else:
            paths.append(target)
    return sorted(set(paths))


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="observe_branches",
        description="Classify workflow files by which audit output they branch on.",
    )
    parser.add_argument("targets", nargs="+", help="workflow files or directories")
    parser.add_argument("--json", action="store_true", help="machine-readable")
    options = parser.parse_args(argv)

    files = _iter_files(options.targets)
    if not files:
        print("no workflow files found", file=sys.stderr)
        return 2

    reports = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as problem:
            print("{}: {}".format(path, problem), file=sys.stderr)
            continue
        result = _classify(text)
        result["path"] = path
        reports.append(result)

    if options.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
        return 0

    width = max((len(r["class"]) for r in reports), default=10)
    for report in reports:
        print("{:<{w}}  {}".format(report["class"], report["path"], w=width))
        conditioned = report["conditioned"]
        if conditioned:
            print("    conditioned on: {}".format(", ".join(sorted(conditioned))))
        if report["prefixes"]:
            print("    prefixes: {}".format(", ".join(report["prefixes"])))

    counts: Dict[str, int] = {}
    for report in reports:
        counts[report["class"]] = counts.get(report["class"], 0) + 1

    print("")
    print("{} file(s)".format(len(reports)))
    for kind in sorted(counts):
        print("  {:<{w}} {:>3}  {}".format(kind, counts[kind], _action_for(kind), w=width))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
