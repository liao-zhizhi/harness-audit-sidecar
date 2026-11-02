"""Command line entry point.

The CLI is deliberately boring: it refuses to start unless the engine
still passes its own known-answer fixtures, then walks through a small
demo so the four verdicts, the trust balance, and the witness collector
can be inspected by eye.

Run with::

    python -m audit_sidecar.cli
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

from .confidence import ConfidenceAccount
from .judgment_base import JudgmentBase, RuleError, default_rules_dir
from .mcp_proxy import MCPProxy
from .rule_engine import RuleEngine
from .runner import SafeRunner
from .self_audit import run_self_audit
from .verdict import Verdict, VerdictEngine

__all__ = ["main"]

SELF_AUDIT_FAILED_MESSAGE = "Self-audit failed, tool is a decoration."

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
        base = JudgmentBase(rules_dir)
    except (RuleError, FileNotFoundError) as problem:
        print("    skipped: judgment base could not be loaded ({})".format(problem))
        print()
        return

    print("    rules dir : {}".format(rules_dir))
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


def main() -> int:
    """Run the self-audit, then the demo. Returns a process exit code."""
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
