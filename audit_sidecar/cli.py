"""Command line entry point.

Two things live here:

``harness-audit init``
    The install step a developer actually gets wrong. It detects the repository,
    prints the MCP client stanza, and writes the ``ref_roots.json`` map the case
    library resolves ``@alias`` refs through. It never edits a global client
    config: printing is the whole contract, because a CLI that silently rewrites
    someone's MCP settings has to be trusted more than this one is.

``harness-audit setup``
    The same install, applied: writes ``.cursor/mcp.json`` (merged, backed up),
    merges ``ref_roots.json``, and records a baseline. ``--print-only`` shows
    exactly what it would do and writes nothing.

``harness-audit mcp``
    The stdio server, so the config ``setup`` writes needs no second console
    script.

``harness-audit`` with no arguments
    The demo. It refuses to run unless the engine still passes its own
    known-answer fixtures, then walks through the four verdicts, the trust
    balance, and the witness collector so they can be inspected by eye.

``harness-audit check``
    The one-line interface: one declaration (``--file``, ``--command`` or
    ``--diff``), the witness collected here, three lines out. Internal fields
    are wrapped, not deleted: ``--verbose`` / ``--json`` expand them.

``harness-audit baseline``
    Record the current working tree so a later ``--file`` / ``--diff`` check
    can answer "changed", not merely "exists" / "non-empty".

``harness-audit report``
    Render ``audit_trace.jsonl`` as a terminal panel (``--lang zh|en``).
    FAIL rows highlight ``reason``, untouched files, and extra files.

Run with::

    python -m audit_sidecar.cli            # demo
    python -m audit_sidecar.cli init       # print MCP stanza + write ref_roots
    python -m audit_sidecar.cli setup      # write Cursor MCP config + baseline
    python -m audit_sidecar.cli baseline   # record the working tree
    python -m audit_sidecar.cli check --file src/main.py --upload
    python -m audit_sidecar.cli report --lang zh
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import baseline as baseline_mod
from . import report
from . import usage
from .check import (
    MALFORMED_REASON_PREFIX,
    NO_DECLARATION_REASON_PREFIX,
    PROVENANCE_TOPOLOGY,
    SELF_AUDIT_REASON_PREFIX,
    CheckOutcome,
    check_command,
    check_diff,
    check_file,
)
from .confidence import ConfidenceAccount
from .judgment_base import JudgmentBase, RuleError, default_rules_dir
from .mcp_proxy import MCPProxy
from .rule_engine import RuleEngine
from .runner import SafeRunner
from .self_audit import run_self_audit
from .verdict import Verdict, VerdictEngine

__all__ = ["main", "run_init", "resolve_git_root"]

SELF_AUDIT_FAILED_MESSAGE = "Self-audit failed, tool is a decoration."

#: ``check`` still refuses when the gate is closed, but it says so as a
#: verdict: the first line of its output is always one of the four values, so a
#: caller (and the Action's output parser) never has to special-case prose.
#: The sentence starts with ``SELF_AUDIT_REASON_PREFIX``, which is also what
#: ``CheckOutcome.failure_kind`` keys on -- one refusal, one spelling.
CHECK_SELF_AUDIT_REASON = (
    SELF_AUDIT_REASON_PREFIX + ", so no declaration was judged"
)

#: Neither of these is a witness problem, so neither may fall through to
#: ``witness_missing``. Built from the same prefixes ``failure_kind`` matches:
#: rewriting the sentence here without rewriting the constant would make an
#: invocation error look like "nobody looked", and send the operator off to
#: record a baseline when the real fix is to fix their arguments.
CHECK_NO_DECLARATION_REASON = (
    NO_DECLARATION_REASON_PREFIX + ", so there is nothing to judge"
)
CHECK_MALFORMED_REASON_TEMPLATE = MALFORMED_REASON_PREFIX + ": {}"

#: Where the case library looks for the alias map. ``case_store`` in the product
#: repo reads exactly this path, so writing anywhere else would produce a file
#: that looks right and is never read.
REF_ROOTS_PATH = Path.home() / ".gradia" / "case_library" / "ref_roots.json"

#: MCP stanza the developer pastes into Cursor/Claude. ``command`` is the
#: installed console script, not a ``python -m`` invocation: a client that
#: launches the server cannot be assumed to have this checkout on its path.
MCP_SERVER_NAME = "harness-audit"
MCP_COMMAND = "harness-audit-server"

# Each demo case is (label, keyword arguments for the engine).
_ENGINE_CASES = (
    (
        "clean run",
        {"intent": "hash:new", "effect": "hash:new", "check": True},
    ),
    (
        "false completion",
        {"intent": "hash:new", "effect": "hash:old", "check": True},
    ),
    (
        "false failure",
        {"intent": "hash:new", "effect": "hash:new", "check": False},
    ),
    (
        "intent not implemented",
        {"intent": "hash:new", "effect": "hash:old", "check": False},
    ),
    (
        "no witness",
        {"intent": "hash:new", "effect": None, "check": True},
    ),
    (
        "never ran",
        {"intent": None, "effect": "hash:old", "check": False},
    ),
)

# A short history that exercises every verdict and pushes one event out of
# an eight-slot window, so the eviction is visible.
_HISTORY = (
    Verdict.PASS,
    Verdict.PASS,
    Verdict.FAIL,
    Verdict.NOT_RUN,
    Verdict.PASS,
    Verdict.INVALID,
    Verdict.PASS,
    Verdict.PASS,
    Verdict.PASS,
)

_WINDOW = 8


def _print_engine_demo() -> None:
    print("[1] Verdict engine")
    print("    intent = what should happen, effect = what the witness saw,")
    print("    check = what the agent reported back")
    print()
    print("    {:<24} {:<8} {}".format("case", "verdict", "reason"))

    engine = VerdictEngine()
    rows = []
    for label, case in _ENGINE_CASES:
        verdict, reason = engine.explain(**case)
        rows.append((label, str(verdict), reason))

    reason_width = max(len(reason) for _, _, reason in rows)
    print("    {} {} {}".format("-" * 24, "-" * 8, "-" * reason_width))
    for label, verdict, reason in rows:
        print("    {:<24} {:<8} {}".format(label, verdict, reason))
    print()


def _print_account_demo() -> None:
    print("[2] Trust energy account (window={})".format(_WINDOW))
    print("    balance = PASS / (PASS + FAIL + INVALID); NOT RUN is excluded")
    print()
    print("    {:<6} {:<9} {:>9}  {:<7} {}".format(
        "step", "verdict", "balance", "label", "events"
    ))
    print("    {} {} {}  {} {}".format("-" * 6, "-" * 9, "-" * 9, "-" * 7, "-" * 6))

    account = ConfidenceAccount(window=_WINDOW)
    for step, verdict in enumerate(_HISTORY, start=1):
        account.record(verdict)
        print(
            "    {:<6} {:<9} {:>9.3f}  {:<7} {}".format(
                step, str(verdict), account.get_balance(), account.get_label(), len(account)
            )
        )

    counts = account.counts
    print()
    print(
        "    window holds {} events: PASS={} FAIL={} INVALID={} NOT RUN={}".format(
            len(account),
            counts[Verdict.PASS.value],
            counts[Verdict.FAIL.value],
            counts[Verdict.INVALID.value],
            counts[Verdict.NOT_RUN.value],
        )
    )
    print(
        "    final balance {:.3f} -> label {!r}".format(
            account.get_balance(), account.get_label()
        )
    )
    print()


def _format_bool(value: object) -> str:
    if value is None:
        return "None"
    return "True" if value else "False"


def _print_witness_rows(rows) -> None:
    print(
        "    {:<30} {:<5} {:<8} {:<7} {:<6} {}".format(
            "case", "exit", "PIPESTATUS", "effect", "check", "verdict"
        )
    )
    print(
        "    {} {} {} {} {} {}".format(
            "-" * 30, "-" * 5, "-" * 8, "-" * 7, "-" * 6, "-" * 8
        )
    )
    for label, result in rows:
        pipe_status = " ".join(str(s) for s in result.pipe_status) or "-"
        exit_code = "-" if result.exit_code is None else str(result.exit_code)
        print(
            "    {:<30} {:<5} {:<8} {:<7} {:<6} {}".format(
                label,
                exit_code,
                pipe_status,
                _format_bool(result.effect_observed),
                _format_bool(result.check_observed),
                str(result.verdict),
            )
        )


def _print_witness_demo() -> None:
    """Reproduce the paper's two classic cases against a real shell."""
    print("[3] Witness collection: real command, independently read side effect")

    runner = SafeRunner(timeout=10)

    with tempfile.TemporaryDirectory(prefix="harness-demo-") as root:
        rows = []

        # Case A: the pipeline eats the status of an earlier stage.
        print()
        print("    Case A - masked failure: `false | cat`")
        naive = runner.raw_exit_status("false | cat", cwd=root, pipefail=False)
        audited = runner.raw_exit_status("false | cat", cwd=root, pipefail=True)
        print("      bash default (last stage wins) exit = {}".format(naive))
        print("      bash pipefail                   exit = {}".format(audited))
        print("      -> the runner reads the second one, and PIPESTATUS names")
        print("         the stage that actually failed")

        rows.append(
            (
                "A1  false | cat",
                runner.run_shell(
                    "false | cat",
                    cwd=root,
                    intent_desc="run a step that must succeed",
                ),
            )
        )
        rows.append(
            (
                "A2  false | cat > result.txt",
                runner.run_shell(
                    "false | cat > result.txt",
                    cwd=root,
                    intent_desc="write done into result.txt",
                    expected_file="result.txt",
                    expected_content="done",
                ),
            )
        )

        # Case B: the action lands in the wrong coordinate.
        print()
        print("    Case B - target drift: agent runs in the wrong directory")
        project = os.path.join(root, "project")
        drift = os.path.join(root, "drift")
        os.makedirs(project)
        os.makedirs(drift)
        with open(os.path.join(project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("feature_x=off\n")
        print("      intended target : project/config.yaml  (feature_x=off)")
        print("      command cwd     : drift/  (not the project directory)")
        print("      command         : echo 'feature=on' >> config.yaml")

        rows.append(
            (
                "B1  witness in drift/",
                runner.run_shell(
                    "echo 'feature=on' >> config.yaml",
                    cwd=drift,
                    intent_desc="append feature=on to config.yaml",
                    expected_file="config.yaml",
                    expected_content="feature=on",
                    witness_root=drift,
                ),
            )
        )
        rows.append(
            (
                "B2  witness pinned to project/",
                runner.run_shell(
                    "echo 'feature=on' >> config.yaml",
                    cwd=drift,
                    intent_desc="append feature=on to config.yaml",
                    expected_file="config.yaml",
                    expected_content="feature=on",
                    witness_root=project,
                ),
            )
        )

        print()
        _print_witness_rows(rows)
        print()
        print("    A1 INVALID : the status says the step failed, but no artefact")
        print("                 was named, so no effect was witnessed. The runner")
        print("                 never turns an unavailable witness into a PASS.")
        print("    A2 FAIL    : the witness exists and is empty; the intent was")
        print("                 not implemented. pipefail is what makes the exit")
        print("                 status agree with the witness.")
        print("    B1 PASS    : the audit is blind here, not the agent correct --")
        print("                 the witness followed the action into drift/.")
        print("                 project/config.yaml was never touched.")
        print("    B2 FAIL    : same command, witness pinned to the intended")
        print("                 coordinate: false completion, caught.")
        print()


def _print_git_witness_demo() -> None:
    """Case C: the agent says it edited A, git says it actually edited B."""
    print("[4] Git witness: what changed versus what was claimed")

    if shutil.which("git") is None:
        print("    skipped: git is not installed")
        print()
        return

    runner = SafeRunner(timeout=20)

    with tempfile.TemporaryDirectory(prefix="harness-git-demo-") as root:
        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )

        try:
            git("init", "-q")
            for name, content in (("target.py", "value = 0\n"), ("other.py", "other = 0\n")):
                with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                    handle.write(content)
            git("add", "-A")
            git(
                "-c", "user.name=Test",
                "-c", "user.email=test@example.com",
                "commit", "-q", "-m", "initial",
            )
        except (subprocess.CalledProcessError, OSError) as problem:
            print("    skipped: could not build the demo repository ({})".format(problem))
            print()
            return

        print("    repo        : fresh temporary repository, one commit")
        print("    intent      : edit target.py")
        print("    agent claim : 'target.py updated' (exit status 0)")

        rows = []

        rows.append(
            (
                "C1  correct edit",
                runner.run_shell(
                    "printf 'value = 1\\n' > target.py",
                    cwd=root,
                    intent_desc="edit target.py",
                    expected_files_modified=["target.py"],
                ),
            )
        )
        rows.append(
            (
                "C2  edited wrong_dir/target.py",
                runner.run_shell(
                    "mkdir -p wrong_dir && printf 'value = 1\\n' > wrong_dir/target.py",
                    cwd=root,
                    intent_desc="edit target.py",
                    expected_files_modified=["target.py"],
                ),
            )
        )
        rows.append(
            (
                "C3  edited target.py + other.py",
                runner.run_shell(
                    "printf 'value = 2\\n' > target.py; printf 'sneaky\\n' > other.py",
                    cwd=root,
                    intent_desc="edit target.py",
                    expected_files_modified=["target.py"],
                ),
            )
        )

        print()
        print("    {:<32} {:<8} {:<34} {:<7} {}".format(
            "case", "verdict", "changed (from git)", "effect", "check"
        ))
        print("    {} {} {} {} {}".format("-" * 32, "-" * 8, "-" * 34, "-" * 7, "-" * 6))
        for label, result in rows:
            changed = ", ".join(result.changed_files) or "-"
            print("    {:<32} {:<8} {:<34} {:<7} {}".format(
                label,
                str(result.verdict),
                changed,
                _format_bool(result.effect_observed),
                _format_bool(result.check_observed),
            ))

        print()
        for label, result in rows:
            print("    {}: {}".format(label.split()[0], result.reason))

        print()
        print("    C2 is the point: the agent's exit status and its own read-back")
        print("    both say success, and the file it wrote really does contain the")
        print("    right text -- it just is not target.py. Only git knows that,")
        print("    because git is the one record the agent did not author.")
        print("    C3 shows the strict default: touching an unrelated file is")
        print("    drift too, not a bonus. allow_extra_changes=True relaxes it.")
        print()


def _print_mcp_demo() -> None:
    """Case D: the claim reaching the agent comes from the tool layer."""
    print("[5] MCP interception: the claim the agent received vs the world")

    if shutil.which("git") is None:
        print("    skipped: git is not installed")
        print()
        return

    with tempfile.TemporaryDirectory(prefix="harness-mcp-demo-") as root:
        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )

        try:
            git("init", "-q")
            with open(os.path.join(root, "target.py"), "w", encoding="utf-8") as handle:
                handle.write("")
            git("add", "-A")
            git(
                "-c", "user.name=Test",
                "-c", "user.email=test@example.com",
                "commit", "-q", "-m", "initial",
            )
        except (subprocess.CalledProcessError, OSError) as problem:
            print("    skipped: could not build the demo repository ({})".format(problem))
            print()
            return

        proxy = MCPProxy(cwd=root, runner=SafeRunner(timeout=20))
        rows = []

        # D1: the tool worked and told the agent the truth.
        proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "value = 1\n"}, True
        )
        rows.append(
            ("D1  write_file, honest tool", "write_file", True, proxy.audit_log[-1])
        )

        # D2: the tool hit a path error, swallowed it, and reported success.
        response = proxy.intercept_tool_call(
            "write_file",
            {"path": "missing_dir/target.py", "content": "value = 2\n"},
            True,
        )
        rows.append(
            ("D2  tool swallowed a path error", "write_file", True, proxy.audit_log[-1])
        )

        # D3: the write landed but the tool reported failure.
        proxy.intercept_tool_call(
            "write_file", {"path": "target.py", "content": "value = 3\n"}, False
        )
        rows.append(
            ("D3  tool under-reported success", "write_file", False, proxy.audit_log[-1])
        )

        print("    repo  : fresh temporary repository, one commit")
        print("    the proxy runs the call, reads git independently, and judges")
        print("    the claim the tool layer handed to the agent")
        print()
        print("    {:<34} {:<5} {:<12} {:<14} {}".format(
            "case", "exit", "agent check", "actual effect", "verdict"
        ))
        print("    {} {} {} {} {}".format(
            "-" * 34, "-" * 5, "-" * 12, "-" * 14, "-" * 8
        ))
        for label, _tool, _claim, result in rows:
            print("    {:<34} {:<5} {:<12} {:<14} {}".format(
                label,
                "-" if result.exit_code is None else str(result.exit_code),
                _format_bool(result.check_observed),
                _format_bool(result.effect_observed),
                str(result.verdict),
            ))

        print()
        print("    response handed back to the agent for D2:")
        print("      status={!r}  isError={}  text={!r}".format(
            response["status"], response["isError"], response["content"][0]["text"]
        ))
        print("    the proxy stays passive: the agent still believes it succeeded,")
        print("    and the audit log is where the disagreement is recorded.")
        print()

        for label, _tool, _claim, result in rows:
            print("    {}: {}".format(label.split()[0], result.reason))

        print()
        counts = proxy.summary()
        print("    audit log: {} calls -> PASS={} FAIL={} INVALID={} NOT RUN={}".format(
            len(proxy),
            counts[Verdict.PASS.value],
            counts[Verdict.FAIL.value],
            counts[Verdict.INVALID.value],
            counts[Verdict.NOT_RUN.value],
        ))
        print("    D2 is the case no exit-status audit can see: the command really")
        print("    did fail, its exit status was non-zero, and the tool layer")
        print("    replaced that with 'ok'. Judging the exit status would have")
        print("    audited the wrong party and let the wrapper off.")
        print()


def _print_judgment_base_demo() -> None:
    """Section 6: the same case, judged with and without the rule base."""
    print("[6] Judgment base: policy as data, and it can only tighten")

    rules_dir = default_rules_dir()
    if rules_dir is None:
        print("    skipped: no rules/ directory found")
        print()
        return

    try:
        # ``JudgmentBase()`` merges the local ``./rules`` layer with the
        # shipped base; naming one directory would re-open the shadowing hole.
        base = JudgmentBase()
    except (RuleError, FileNotFoundError) as problem:
        print("    skipped: judgment base could not be loaded ({})".format(problem))
        print()
        return

    print("    rules dir : {}".format(", ".join(base.rules_dirs) or rules_dir))
    print("    loaded    : {}".format(", ".join(base.rule_ids) or "(none)"))
    print()
    print("    {:<30} {:<9} {:<12} {}".format("case", "lattice", "with rules", "rule"))
    print("    {} {} {} {}".format("-" * 30, "-" * 9, "-" * 12, "-" * 16))

    engine = RuleEngine(base)
    cases = (
        (
            "file witness only",
            "write_file",
            {"intent": True, "effect": True, "check": True},
            frozenset({"file", "agent_report"}),
        ),
        (
            "file + git witness",
            "write_file",
            {"intent": True, "effect": True, "check": True},
            frozenset({"file", "git_diff", "agent_report"}),
        ),
        (
            "write did not land",
            "write_file",
            {"intent": True, "effect": False, "check": True},
            frozenset({"file", "git_diff", "agent_report"}),
        ),
        (
            "tool with no rule",
            "run_command",
            {"intent": True, "effect": True, "check": True},
            frozenset({"git_diff"}),
        ),
    )

    for label, tool, case, witnesses in cases:
        lattice = VerdictEngine().explain(**case)[0]
        decision = engine.judge(tool, witnesses=witnesses, **case)
        print(
            "    {:<30} {:<9} {:<12} {}".format(
                label,
                str(lattice),
                str(decision.verdict),
                decision.rule_id or "-",
            )
        )

    print()
    print("    Row 1 is the point. The file witness alone proves the write landed,")
    print("    so the lattice says PASS -- and it is right about the world. The")
    print("    rule says a git change set is also required, so the same run is")
    print("    INVALID: not 'the write failed', but 'this success is not evidence'.")
    print("    That sentence is impossible to write in code that has no rules.")
    print()
    print("    The rule is monotone: row 3 stays FAIL, and row 4 is untouched")
    print("    because no rule targets run_command. A policy that could turn a")
    print("    FAIL into a PASS would be a way to argue with the witness.")
    print()


def resolve_git_root(cwd: str) -> Optional[str]:
    """The repository root containing ``cwd``, or ``None``.

    ``None`` is not an error: an audit can be pointed at a directory that is not
    under version control yet. It only means the alias has to fall back to the
    directory name, and the ref map will not be stable across machines.
    """
    if shutil.which("git") is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    root = completed.stdout.strip()
    return root or None


def mcp_stanza(project_root: str) -> Dict[str, Any]:
    """The exact JSON a client needs. One shape, so the print cannot drift."""
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": MCP_COMMAND,
                "args": ["--cwd", project_root, "--quiet"],
            }
        }
    }


def default_alias(project_root: str) -> str:
    """``/home/x/Gradia`` -> ``Gradia``. Lower-cased, path-safe."""
    name = os.path.basename(os.path.abspath(project_root.rstrip(os.sep))) or "repo"
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name).strip("-")
    return (cleaned or "repo").lower()


def prepare_ref_roots(
    path: Path, alias: str, project_root: str
) -> Tuple[bool, Dict[str, str]]:
    """``(written, existing)``. Never overwrites.

    Overwriting is the one destructive act available here, and it would silently
    repoint every ``@alias`` case in the library at a different checkout. If the
    file is there, the operator gets the path and decides.
    """
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        return False, existing if isinstance(existing, dict) else {}
    payload = {alias: os.path.abspath(project_root)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True, payload


def _parse_init_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="harness-audit init",
        description=(
            "Print the MCP client stanza and write ref_roots.json. Prints, never "
            "edits a global config."
        ),
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="project directory (default: current)")
    parser.add_argument(
        "--alias",
        default=None,
        help="ref_roots alias (default: the repository directory name)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="write ./ref_roots.json instead of ~/.gradia/case_library/ref_roots.json",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def run_init(argv: List[str]) -> int:
    """Detect, print, write. Returns a process exit code."""
    options = _parse_init_args(argv)
    cwd = os.path.abspath(options.cwd)
    if not os.path.isdir(cwd):
        print("init: directory does not exist: {}".format(cwd), file=sys.stderr)
        return 2

    git_root = resolve_git_root(cwd)
    if git_root is None:
        # A warning, not a failure. The audit works on any directory; what it
        # cannot do is give the case library a stable alias for one that has no
        # repository identity.
        print(
            "warning: {} is not inside a git repository; using the directory "
            "name as the alias, so the same project elsewhere will get a "
            "different one".format(cwd),
            file=sys.stderr,
        )
        project_root = cwd
        in_repo = False
    else:
        project_root = git_root
        in_repo = True

    alias = options.alias or default_alias(project_root)
    target = Path(cwd, "ref_roots.json") if options.local else REF_ROOTS_PATH
    written, mapping = prepare_ref_roots(target, alias, project_root)
    stanza = mcp_stanza(project_root)

    if options.json:
        print(
            json.dumps(
                {
                    "project_root": project_root,
                    "in_git_repository": in_repo,
                    "alias": alias,
                    "ref_roots_path": str(target),
                    "ref_roots_written": written,
                    "ref_roots": mapping,
                    "mcp": stanza,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("project root : {}".format(project_root))
    print("alias        : {}".format(alias))
    if written:
        print("ref_roots    : written {} -> {}".format(target, mapping))
    else:
        # The path is printed in both branches: on a skip it is the only thing
        # the operator needs, and on a write it is the receipt.
        print("ref_roots    : already exists, skipped -> {}".format(target))
        if alias not in mapping:
            print(
                "               note: this file has no alias {!r}; the case "
                "library will resolve @{} refs to nothing until one is "
                "added".format(alias, alias),
                file=sys.stderr,
            )
    print("")
    # Said unconditionally, in both branches: the file is shared by every
    # project on this machine, so "written" does not mean "replaced", and the
    # operator is the only one who knows which other aliases are needed.
    print(
        "note         : existing @aliases in ref_roots.json are preserved; add "
        "other repo aliases manually if needed"
    )
    print("MCP configuration (paste into your Cursor/Claude client):")
    print("")
    print(json.dumps(stanza, indent=2, ensure_ascii=False))
    print("")
    print("next: paste the block above into your MCP client config, then restart")
    print("      the client. Verify with: harness-audit-server --cwd {}".format(project_root))
    return 0


#: Process exit codes. A verdict is data, so the code has to distinguish
#: "judged and failed" from "could not judge"; otherwise CI treats a missing
#: witness as a bug in the code under test. ``2`` is argparse's usage error.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INVALID = 3
EXIT_NOT_RUN = 4

_VERDICT_EXIT = {
    Verdict.PASS: EXIT_PASS,
    Verdict.FAIL: EXIT_FAIL,
    Verdict.INVALID: EXIT_INVALID,
    Verdict.NOT_RUN: EXIT_NOT_RUN,
}


def _parse_check_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="harness-audit check",
        description=(
            "Judge one declaration. The witness is collected here, so the "
            "caller supplies no witness_type, producer or topology."
        ),
    )
    declaration = parser.add_mutually_exclusive_group()
    declaration.add_argument(
        "--file", metavar="PATH", help="claim: this file changed"
    )
    declaration.add_argument(
        "--command", metavar="CMD", help="claim: this command succeeded"
    )
    declaration.add_argument(
        "--diff", action="store_true", help="claim: the working tree changed"
    )
    parser.add_argument(
        "--cwd", default=os.getcwd(), help="directory to judge (default: current)"
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="seconds (default: 30)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show tier, dropped_assertions, cap_fired, renegotiation_class",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help=(
            "also POST the verdict to a dashboard's display ledger. Off by "
            "default; sends verdict, reason and the claim shape only. "
            "Uploading consumes the anonymous trial quota on the dashboard "
            "(same budget as /api/v1/verify). Dense uploads can exhaust the "
            "trial quota; pass --upload-api-key if you need more"
        ),
    )
    parser.add_argument(
        "--upload-url",
        default=None,
        metavar="URL",
        help=(
            "dashboard base URL for --upload (default: {} or ${})".format(
                report.DEFAULT_BASE_URL, report.UPLOAD_URL_ENV
            )
        ),
    )
    parser.add_argument(
        "--upload-api-key",
        default=None,
        metavar="KEY",
        help=(
            "X-API-Key for --upload, lifting the anonymous trial quota "
            "(default: ${}). Sending a key is opt-in; without it the request "
            "is anonymous".format(report.API_KEY_ENV)
        ),
    )
    parser.add_argument(
        "--upload-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="budget for --upload; a timeout never changes the verdict (default: 5)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable trace")
    return parser.parse_args(argv)


def _format_check(outcome: CheckOutcome, *, verbose: bool) -> List[str]:
    """The three-line answer, plus the trace only when asked for.

    Default output deliberately contains no field name from the internal
    vocabulary. If a reader needs ``tier`` to understand the answer, the answer
    is not the product.
    """
    lines = [outcome.verdict.value, "reason: {}".format(outcome.reason)]
    if outcome.verdict is not Verdict.PASS and outcome.next_step:
        lines.append("next_step: {}".format(outcome.next_step))
    if not verbose:
        return lines

    def show(name: str, value: Any) -> str:
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(str(item) for item in value) or "(none)"
        elif isinstance(value, dict):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        elif value is None:
            rendered = "(none)"
        else:
            rendered = str(value)
        return "{:<20}: {}".format(name, rendered)

    lines.extend(
        [
            "",
            show("declaration", outcome.declaration),
            show("witness_type", outcome.witness_type),
            show("witness_scope", outcome.witness_scope),
            show("producer", outcome.producer),
            show("topology", outcome.topology),
            # Both words on purpose: `topology` is what the receipt claims,
            # `cap_topology` is the value the cap was given. They differ by a
            # documented semantic mapping, and the trace shows it.
            show("cap_topology", PROVENANCE_TOPOLOGY),
            show("failure_kind", outcome.failure_kind),
            show("tier", outcome.tier),
            show("dropped_assertions", outcome.dropped_assertions),
            show("cap_fired", outcome.cap_fired),
            show("renegotiation_class", outcome.renegotiation_class),
            show("evidence", dict(outcome.evidence)),
        ]
    )
    return lines


def _emit_check(outcome: CheckOutcome, *, verbose: bool, as_json: bool) -> None:
    if as_json:
        print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False))
        return
    for line in _format_check(outcome, verbose=verbose):
        print(line)


def _finish_check(outcome: CheckOutcome, options: argparse.Namespace) -> int:
    """Emit, observe, optionally upload, return the verdict's exit code.

    Order matters. The verdict is printed first (a CI step parses line 1), the
    usage log is written next (it observes and cannot fail), and the network
    happens last -- so a slow or absent dashboard cannot delay the answer.

    A failed upload never changes the exit code: the verdict was decided
    locally, and a display being down is not evidence about the claim.
    """
    _emit_check(outcome, verbose=options.verbose, as_json=options.json)

    kind = "file" if options.file is not None else "command" if options.command is not None else "diff"
    usage.observe_check(
        outcome,
        declaration_kind=kind,
        cwd=os.path.abspath(options.cwd),
        path=options.file if kind == "file" else None,
    )

    if options.upload:
        # Flush before the network call: stdout is block-buffered when piped, so
        # without this the verdict can appear after the upload line, and a CI
        # log would read as if the dashboard decided the result.
        sys.stdout.flush()
        note = report.announcement(options.upload_url)
        if note:
            print(note, file=sys.stderr)
        # Probe before posting. "Sent" for a host that has no such route is the
        # one failure mode this flag must not have: the operator would believe a
        # remote display holds their verdict when nothing received it.
        readiness = report.probe(options.upload_url, timeout=options.upload_timeout)
        if not readiness.ok:
            print(
                report.failure_message(options.upload_url, readiness), file=sys.stderr
            )
        else:
            status, body = report.upload(
                outcome,
                options.upload_url,
                timeout=options.upload_timeout,
                api_key=options.upload_api_key,
            )
            print(report.summary_line(status, body), file=sys.stderr)
            # The aggregate, not the rows: counts of verdicts, baseline misses
            # and tool names. Sending the log itself would ship every path hash
            # and every timestamp, which is a different product.
            usage_status, usage_body = usage.upload_aggregate(
                options.upload_url,
                timeout=options.upload_timeout,
                api_key=options.upload_api_key,
            )
            print(usage.summary_line(usage_status, usage_body), file=sys.stderr)
    return _VERDICT_EXIT[outcome.verdict]
    return _VERDICT_EXIT[outcome.verdict]


def run_check(argv: List[str]) -> int:
    """Judge one declaration. Returns a process exit code.

    A declaration the tool cannot ground is ``INVALID`` with a ``next_step``,
    never a guess. ``--file`` and ``--diff`` need a recording because "changed"
    is a comparison; without one they can only answer "exists" or "non-empty",
    which is not the question that was asked.
    """
    options = _parse_check_args(argv)

    if options.file is None and options.command is None and not options.diff:
        outcome = CheckOutcome(
            verdict=Verdict.INVALID,
            reason=CHECK_NO_DECLARATION_REASON,
            next_step=(
                'pass exactly one of --file <path>, --command "<cmd>" or --diff'
            ),
            declaration="(none)",
            witness_type="none",
        )
        return _finish_check(outcome, options)

    cwd = os.path.abspath(options.cwd)
    if not os.path.isdir(cwd):
        print("check: directory does not exist: {}".format(cwd), file=sys.stderr)
        return EXIT_USAGE
    if not run_self_audit():
        # The gate is closed, so nothing is judged. Reported as INVALID rather
        # than a bare message: the first line of `check` is always a verdict,
        # which is what lets a CI step parse it without knowing the prose.
        outcome = CheckOutcome(
            verdict=Verdict.INVALID,
            reason=CHECK_SELF_AUDIT_REASON,
            next_step=(
                "run `harness-audit` (the demo) to see which fixture failed, "
                "then re-run this check"
            ),
            declaration="(gate closed)",
            witness_type="none",
        )
        return _finish_check(outcome, options)

    if options.file is not None:
        try:
            outcome = check_file(options.file, cwd=cwd, timeout=options.timeout)
        except ValueError as problem:
            outcome = _malformed_declaration(options.file, problem)
    elif options.command is not None:
        try:
            outcome = check_command(options.command, cwd=cwd, timeout=options.timeout)
        except ValueError as problem:
            outcome = _malformed_declaration(options.command, problem)
    else:
        try:
            outcome = check_diff(cwd=cwd, timeout=options.timeout)
        except ValueError as problem:
            outcome = _malformed_declaration("diff", problem)

    return _finish_check(outcome, options)


def _malformed_declaration(declaration: str, problem: Exception) -> CheckOutcome:
    """A declaration the tool cannot read is INVALID, not a traceback.

    argparse accepts ``--file ""``; the check helper rejects it. Letting the
    ValueError escape would turn a usage mistake into a stack trace, which
    reads like a bug in the auditor rather than in the invocation.
    """
    return CheckOutcome(
        verdict=Verdict.INVALID,
        reason=CHECK_MALFORMED_REASON_TEMPLATE.format(problem),
        next_step=(
            'pass a non-empty declaration: --file <path>, --command "<cmd>" or --diff'
        ),
        declaration=declaration,
        witness_type="none",
    )


def _parse_baseline_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="harness-audit baseline",
        description=(
            "Record the working tree so a later --file / --diff check can say "
            "'changed' instead of 'exists'."
        ),
    )
    parser.add_argument(
        "--cwd", default=os.getcwd(), help="directory to record (default: current)"
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="also record a content digest for PATH (repeatable)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="write ./.harness-baseline.json instead of ~/.gradia/baseline.json",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def run_baseline(argv: List[str]) -> int:
    """Write the recording. Returns a process exit code.

    Outside a repository this still succeeds for ``--file`` (content hashes do
    not need git) but warns that ``--diff`` will stay ``INVALID``: a change set
    is a git fact, and inventing one would be the fabrication this package
    refuses everywhere else.
    """
    options = _parse_baseline_args(argv)
    cwd = os.path.abspath(options.cwd)
    if not os.path.isdir(cwd):
        print("baseline: directory does not exist: {}".format(cwd), file=sys.stderr)
        return EXIT_USAGE

    try:
        recorded, path = baseline_mod.record(cwd, files=options.file, local=options.local)
    except OSError as problem:
        print("baseline: could not write {}: {}".format(cwd, problem), file=sys.stderr)
        return EXIT_USAGE

    # A count and a flag; the recorded paths stay in the recording, which is
    # already on disk and is the file the operator asked for.
    usage.record_baseline(
        files=len(recorded.files), local=bool(options.local), cwd=cwd
    )

    in_repo = baseline_mod.resolve_repo(cwd) is not None
    if not in_repo:
        print(
            "warning: {} is not a git repository, so --diff has no change set "
            "to compare; record paths with --file for content checks".format(cwd),
            file=sys.stderr,
        )
    elif options.local and os.path.abspath(cwd) != recorded.repo_root:
        # ``--local`` writes where it was invoked, and a check looks in its own
        # working directory and then the repository root. A recording one level
        # down is therefore invisible from the root, which otherwise reads as
        # "baseline did not work".
        print(
            "warning: this recording is at {}; a check run from {} will not "
            "see it (run `harness-audit baseline --local` at the repository "
            "root, or pass the same --cwd)".format(cwd, recorded.repo_root),
            file=sys.stderr,
        )

    if options.json:
        print(
            json.dumps(
                {
                    "written": str(path),
                    "repo_root": recorded.repo_root,
                    "created_at": recorded.created_at,
                    "git_head": recorded.git_head,
                    "dirty": sorted(recorded.dirty),
                    "files": sorted(recorded.files),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print("baseline written : {}".format(path))
    print("repo             : {}".format(recorded.repo_root))
    print("recording time    : {}".format(recorded.created_at))
    print("dirty paths      : {}".format(len(recorded.dirty)))
    print("file digests     : {}".format(len(recorded.files)))
    for key in sorted(recorded.files):
        digest = recorded.file_digest(key)
        print("  {}  {}".format(key, digest or "(unreadable)"))
    print("")
    print("next: run `harness-audit check --file <path>` or `--diff`")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch to ``init``, ``setup``, ``mcp``, ``baseline``, ``check``, ``report`` or the demo."""
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("init", "setup", "up", "watch", "mcp", "demo", "baseline", "check", "report"):
        command, rest = args[0], args[1:]
        if command == "init":
            return run_init(rest)
        if command == "setup":
            # Imported here: setup_cmd imports baseline and self_audit, and
            # keeping the import local means the demo path does not pay for it.
            from .setup_cmd import run_setup

            return run_setup(rest)
        if command == "up":
            # The one-command entry. It orchestrates the three subcommands
            # below rather than re-implementing them; see `up.py`.
            from .up import run_up

            return run_up(rest)
        if command == "watch":
            from .watch import run_watch

            return run_watch(rest)
        if command == "mcp":
            # The stdio server behind the config `setup` writes. Aliasing it
            # here is what makes ``{"command": "harness-audit", "args": ["mcp"]}``
            # a working entry rather than a second console script the user has
            # to have installed.
            from .mcp_server import main as mcp_main

            return mcp_main(rest)
        if command == "baseline":
            return run_baseline(rest)
        if command == "check":
            return run_check(rest)
        if command == "report":
            from .trace_panel import run_report

            return run_report(rest)
        args = []
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if args:
        print(
            "unknown command {!r}; expected 'init', 'setup', 'up', 'watch', "
            "'mcp', 'baseline', 'check', 'report' or no command for the demo".format(args[0]),
            file=sys.stderr,
        )
        return EXIT_USAGE

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if not run_self_audit():
        print(SELF_AUDIT_FAILED_MESSAGE)
        return 1

    print("agent-harness-audit :: MVP demo")
    print("=" * 31)
    print("Self-audit passed: known-bad cases are still rejected, and")
    print("known-good cases are still accepted.")
    print()

    _print_engine_demo()
    _print_account_demo()
    _print_witness_demo()
    _print_git_witness_demo()
    _print_mcp_demo()
    _print_judgment_base_demo()

    print("Self-audit gate: OPEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
