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
import hashlib
import json
import os
import re
import subprocess
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

#: ``uses: owner/action@ref``. The ref is kept: "which version people adopted"
#: is a different question from "did they adopt it", and a v1 example copied
#: around is not evidence about v2.
_USE = re.compile(r"uses:\s*['\"]?([^\s'\"#]+)")
#: ``verdict`` is a generic output name. A workflow that branches on
#: ``steps.x.outputs.verdict`` may not be using this Action at all, and such a
#: row is not weak evidence -- it is no evidence. Attribution is therefore a
#: separate axis from classification, never folded into it.
ACTION_MARKER = "harness-audit"


def _attribution(text: str) -> Dict[str, Any]:
    """Which action refs this file uses, and whether ours is among them."""
    stripped = _COMMENT.sub("", text)
    refs: List[str] = []
    for match in _USE.finditer(stripped):
        ref = match.group(1)
        if ref not in refs:
            refs.append(ref)
    ours = [ref for ref in refs if ACTION_MARKER in ref]
    versions = []
    for ref in ours:
        version = ref.rsplit("@", 1)[1] if "@" in ref else "(no ref)"
        if version not in versions:
            versions.append(version)
    return {
        "uses_this_action": bool(ours),
        "action_refs": ours,
        "action_versions": versions,
    }


def _signature(text: str) -> str:
    """A fingerprint of the branch skeleton, for finding copies of one template.

    Raw counts answer "how many files", not "how many independent adopters": if
    the first ten ``verdict_only`` workflows came from one organisation's one
    template, that is one decision made once and copied nine times. The
    signature keeps only the lines that decide anything and normalises the two
    things that legitimately differ between copies -- the step id and the
    pinned ref -- so a copy lands in the same bucket as its original.

    This detects *the same branch skeleton*, not semantic duplication. A
    re-typed template that reorders its steps is a new cluster; that error is in
    the safe direction (it over-counts distinct adopters, and the per-repo
    count is printed next to it as the coarser guard).
    """
    lines: List[str] = []
    for line in _COMMENT.sub("", text).splitlines():
        if not (_FIELD.search(line) or _CONDITION.search(line) or "uses:" in line):
            continue
        normalised = " ".join(line.split())
        normalised = re.sub(r"steps\.[A-Za-z0-9_-]+\.", "steps.X.", normalised)
        normalised = re.sub(r"uses:\s*([^\s@]+)@\S+", r"uses: \1@REF", normalised)
        lines.append(normalised)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


def _repo_identity(path: str, cache: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """Who owns this workflow, so copies can be told from independent adopters.

    ``unknown`` is reported as ``unknown`` rather than guessed from the
    directory name: a harvested folder of workflows is not a repo, and
    inventing an owner there would manufacture exactly the independence the
    count is supposed to measure.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory in cache:
        return cache[directory]

    identity = {"repo": "unknown", "source": "unknown"}
    try:
        top = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if top.returncode == 0 and top.stdout.strip():
            remote = subprocess.run(
                ["git", "-C", directory, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=False,
            )
            if remote.returncode == 0 and remote.stdout.strip():
                identity = {
                    "repo": _normalise_remote(remote.stdout.strip()),
                    "source": "git-remote",
                }
            else:
                identity = {
                    "repo": os.path.basename(top.stdout.strip()),
                    "source": "git-toplevel",
                }
    except OSError:
        # git not installed is not an error: the survey still works, it just
        # cannot cluster by owner.
        pass
    cache[directory] = identity
    return identity


def _normalise_remote(url: str) -> str:
    """``git@github.com:org/repo.git`` and ``https://.../org/repo`` -> org/repo."""
    cleaned = url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    for separator in (":", "/"):
        if separator in cleaned and "@" in cleaned.split(separator)[0]:
            cleaned = cleaned.split(separator, 1)[1]
    match = re.search(r"([^/:]+/[^/:]+)$", cleaned)
    return match.group(1) if match else cleaned


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


def _summarise(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-class counts on three axes, because they answer different questions.

    ``files`` is how much YAML exists. ``repos`` is how many owners decided
    this. ``skeletons`` is how many distinct templates that produced -- and a
    class whose files all share one skeleton is one decision copied, not N
    independent ones. Files that do not use this Action are counted on none of
    them: they are not evidence about this Action's interface.
    """
    attributed = [report for report in reports if report["uses_this_action"]]
    per_class: Dict[str, Dict[str, Any]] = {}
    for report in attributed:
        bucket = per_class.setdefault(
            report["class"],
            {"files": 0, "repos": set(), "skeletons": set(), "versions": set()},
        )
        bucket["files"] += 1
        bucket["repos"].add(report["repo"])
        bucket["skeletons"].add(report["signature"])
        bucket["versions"].update(report["action_versions"])

    summary = {
        "files_total": len(reports),
        "files_attributed": len(attributed),
        "files_unattributed": len(reports) - len(attributed),
        "per_class": {},
    }
    for kind, bucket in per_class.items():
        summary["per_class"][kind] = {
            "files": bucket["files"],
            "repos": len(bucket["repos"]),
            "skeletons": len(bucket["skeletons"]),
            "versions": sorted(bucket["versions"]),
        }
    return summary


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

    repo_cache: Dict[str, Dict[str, str]] = {}
    reports = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as problem:
            print("{}: {}".format(path, problem), file=sys.stderr)
            continue
        result = _classify(text)
        result.update(_attribution(text))
        result["signature"] = _signature(text)
        result["path"] = path
        result.update(_repo_identity(path, repo_cache))
        reports.append(result)

    summary = _summarise(reports)

    if options.json:
        print(json.dumps({"summary": summary, "files": reports}, indent=2, sort_keys=True))
        return 0

    width = max((len(r["class"]) for r in reports), default=10)
    for report in reports:
        print("{:<{w}}  {}".format(report["class"], report["path"], w=width))
        conditioned = report["conditioned"]
        if conditioned:
            print("    conditioned on: {}".format(", ".join(sorted(conditioned))))
        if report["prefixes"]:
            print("    prefixes: {}".format(", ".join(report["prefixes"])))
        print(
            "    repo: {} ({}){}".format(
                report["repo"],
                report["source"],
                "" if report["uses_this_action"] else "  NOT THIS ACTION",
            )
        )
        if report["action_refs"]:
            print("    uses: {}".format(", ".join(report["action_refs"])))

    print("")
    print(
        "{} file(s), {} using this Action, {} not (not evidence)".format(
            summary["files_total"],
            summary["files_attributed"],
            summary["files_unattributed"],
        )
    )
    if not summary["per_class"]:
        print("  nothing attributed to this Action -- the decision is moot here")
        return 0

    print("")
    header = "{:<{w}}  {:>5} {:>5} {:>5}  {}".format(
        "class", "files", "repos", "tmpl", "action", w=width
    )
    print(header)
    for kind in sorted(summary["per_class"]):
        bucket = summary["per_class"][kind]
        print(
            "{:<{w}}  {:>5} {:>5} {:>5}  {}".format(
                kind,
                bucket["files"],
                bucket["repos"],
                bucket["skeletons"],
                _action_for(kind),
                w=width,
            )
        )
    print("")
    print("tmpl = distinct branch skeletons. When files >> repos or files >> tmpl,")
    print("the copies are one decision, not several independent ones.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
