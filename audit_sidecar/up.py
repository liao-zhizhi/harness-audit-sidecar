"""``harness-audit up``: one command, three lines, no reading required.

The tool already has `setup`, `baseline` and `check`, and each one is honest
about its preconditions. That is exactly the problem for a first run: the
honest answer to "did my agent do what it said" is ``INVALID`` plus a sentence
about a recording, and the operator now has to learn three subcommands before
their first verdict. ``up`` is the orchestration, not a fourth judge.

The whole design is in the output contract:

* **three lines at most** -- verdict, ``reason:``, ``next_step:``. No progress
  chatter from the setup or the recording it may have just taken. A first run
  that prints twelve lines makes the one line that matters unfindable.
* **a first run does not judge.** If this invocation recorded the baseline, it
  prints what it configured and stops:

  ``baseline recorded (3 files)`` / ``next: change something, then run
  harness-audit up again``

  A recording taken a second ago cannot be contradicted by a change that
  predates it, so ``check`` could only answer "nothing changed since the
  recording" -- true about the lattice, and useless about the operator's world.
  Setup is idempotent, so the second run has both halves and does judge.
  ``--command`` is exempt: an exit status is not compared against a recording,
  so there is nothing unfalsifiable about running it immediately.
* **one corrective command, with this run's arguments in it.** Every failure
  path ends in a line the operator can paste. A placeholder is a command they
  have to edit before it runs, which is a worse answer than the ``INVALID`` that
  produced it.
* **no new verdict logic.** The three lines are a :class:`CheckOutcome` when a
  check ran, and a hand-built ``INVALID`` when a prerequisite could not be
  established -- the same four values, for the same reason ``check`` reports
  its own self-audit failure as ``INVALID`` rather than a crash.

Nothing here writes a ledger, prints an upload, or changes a verdict rule: the
only files it may write are the client config, a recording it was asked to
establish, and the usage log -- none of which decides anything.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import shlex
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import baseline as baseline_mod
from . import usage
from .check import CheckOutcome, check_command, check_diff, check_file
from .self_audit import run_self_audit
from .setup_cmd import (
    MCP_SERVER_NAME,
    REF_ROOTS_PATH,
    _alias_for,
    _read_json,
    choose_mcp_path,
    collect_common_files,
    run_setup,
)
from .verdict import Verdict

#: Same codes as ``check``: a verdict is data, and ``up`` returns the verdict's
#: code so a wrapper script does not have to parse the prose.
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


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="harness-audit up",
        description=(
            "Configure if needed, record a baseline if missing, then judge one "
            "declaration. Prints at most three lines."
        ),
    )
    declaration = parser.add_mutually_exclusive_group()
    declaration.add_argument("--file", metavar="PATH", help="claim: this file changed")
    declaration.add_argument("--command", metavar="CMD", help="claim: this command succeeded")
    declaration.add_argument("--diff", action="store_true", help="claim: the tree changed (default)")
    parser.add_argument("--cwd", default=os.getcwd(), help="directory to work in (default: current)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds (default: 30)")
    parser.add_argument(
        "--local",
        action="store_true",
        help="write ./.harness-baseline.json rather than the shared recording",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=200,
        help="cap on files recorded when a baseline has to be taken (default: 200)",
    )
    # Same overrides as `setup`, for a non-standard layout and for tests.
    parser.add_argument("--mcp-config", default=None, help="override the mcp.json path")
    parser.add_argument("--ref-roots", default=None, help="override the ref_roots.json path")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="append the check's trace after the three lines (diagnosis only)",
    )
    return parser.parse_args(argv)


def _kind(options: argparse.Namespace) -> str:
    if options.file is not None:
        return "file"
    if options.command is not None:
        return "command"
    return "diff"


def _is_configured(cwd: str, *, mcp_config: Optional[str], ref_roots: Optional[str]) -> bool:
    """Both halves, because either one alone produces a server that cannot run.

    An MCP entry without the ``@alias`` map starts, then fails every ``@alias``
    case; a map without the entry never starts at all. Reporting "configured"
    on half the pair would skip the setup that fixes it.
    """
    project_root = baseline_mod.resolve_repo(cwd) or os.path.abspath(cwd)
    mcp_path = choose_mcp_path(project_root)[0] if not mcp_config else _path(mcp_config)
    roots_path = _path(ref_roots) if ref_roots else REF_ROOTS_PATH
    try:
        mcp_document = _read_json(mcp_path)
        roots = _read_json(roots_path)
    except ValueError:
        # Unreadable is not configured: `setup` refuses to rewrite it and will
        # say so, which is a better answer than pretending all is well.
        return False
    servers = mcp_document.get("mcpServers") if isinstance(mcp_document, dict) else None
    has_server = isinstance(servers, dict) and MCP_SERVER_NAME in servers
    has_alias = isinstance(roots, dict) and _alias_for(project_root) in roots
    return bool(has_server and has_alias)


def _path(value: str) -> Path:
    return Path(value)


def _quiet(callable_, argv: List[str]) -> Tuple[int, str]:
    """Run a subcommand with its output captured. Returns ``(code, stderr)``.

    Captured rather than discarded: when it fails, the first line of its
    stderr is the reason the operator needs, and it is already written. The
    captured stdout is dropped because it is the twelve lines ``up`` exists to
    hide.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = callable_(argv)
        except SystemExit as exit_:  # argparse in the child rejected the call
            code = int(exit_.code or 0)
    return code, err.getvalue()


def _one_line(text: str) -> str:
    """Collapse to one line: a reason is a line of output, not a paragraph.

    Git's stderr arrives with newlines in it (one per diagnostic), and a
    verdict whose reason spans four lines is no longer the three-line contract
    -- the fourth line reads like a new field.
    """
    return " ".join((text or "").split())


def _humanise(next_step: Optional[str], options: argparse.Namespace) -> Optional[str]:
    """`check`'s advice, addressed to someone who typed ``up``.

    ``check``'s next steps end with "then re-run this check", which is correct
    where it is written and confusing in this command's output: the operator
    does not have a ``check`` invocation to re-run. Only the wording changes;
    the advice and the verdict behind it are the check's own.
    """
    if not next_step:
        return next_step
    return next_step.replace(
        "re-run this check", "re-run `harness-audit up` with the same arguments"
    )


def _report_config(*, configured: bool, files: int) -> None:
    """What a first run prints: what was set up, and nothing else.

    No verdict line, because nothing was judged -- printing ``INVALID`` here
    would be a verdict about a claim this run never examined. The exit code is
    ``0``: the setup succeeded, and that is all that was attempted.
    """
    if configured:
        print("configured (mcp + ref_roots)")
    print("baseline recorded ({} {})".format(files, "file" if files == 1 else "files"))
    print("next: change something, then run harness-audit up again")


def _report(verdict: Verdict, reason: str, next_step: Optional[str]) -> None:
    print(verdict.value)
    print("reason: {}".format(_one_line(reason)))
    if verdict is not Verdict.PASS and next_step:
        print("next_step: {}".format(_one_line(next_step)))


def _error(reason: str, next_step: str) -> int:
    """A prerequisite failed: ``INVALID``, not a crash and not a FAIL.

    ``FAIL`` would say the claim was contradicted, which is a fact about the
    claim; this is a fact about the tool's footing. ``INVALID`` is the value
    that already means "nothing judged this", so the vocabulary is unchanged.
    """
    _report(Verdict.INVALID, reason, next_step)
    return EXIT_INVALID


def _quote(value: str) -> str:
    return shlex.quote(value)


def _check_command_for(kind: str, options: argparse.Namespace) -> str:
    parts = ["harness-audit", "up"]
    if kind == "file":
        parts += ["--file", options.file or "<path>"]
    elif kind == "command":
        parts += ["--command", options.command or "<cmd>"]
    else:
        parts += ["--diff"]
    parts += ["--cwd", options.cwd]
    if options.local:
        parts.append("--local")
    # Overrides are carried, not dropped: a corrective command that silently
    # falls back to detection on a machine with a non-standard layout would not
    # run as written.
    if options.mcp_config:
        parts += ["--mcp-config", options.mcp_config]
    if options.ref_roots:
        parts += ["--ref-roots", options.ref_roots]
    return " ".join(_quote(part) for part in parts)


def _baseline_command_for(kind: str, options: argparse.Namespace) -> str:
    parts = ["harness-audit", "baseline", "--cwd", options.cwd]
    if options.local:
        parts.append("--local")
    if kind == "file" and options.file:
        parts += ["--file", options.file]
    return " ".join(_quote(part) for part in parts)


def _corrective(kind: str, options: argparse.Namespace, outcome: CheckOutcome) -> str:
    """One runnable line for this failure. Prefers the tool's own next step.

    The outcome's ``next_step`` is kept when it already names a parameterised
    command: it was written where the failure was detected, so it knows more
    than this function can reconstruct. Only when it is prose does the
    orchestrator supply the paste-ready form.
    """
    reason_kind = outcome.failure_kind
    if reason_kind == "self_audit":
        return "harness-audit"
    if reason_kind == "declaration_malformed":
        return _check_command_for(kind, options)

    existing = (outcome.next_step or "").strip()
    if existing.startswith("harness-audit "):
        # `check`'s own advice ends with "then re-run this check"; the operator
        # is running `up`, so point at the command they typed.
        return existing

    if kind in ("file", "diff"):
        # Missing recording is the common INVALID on a first run: two commands,
        # one line, both with this invocation's arguments in them.
        return "{} && {}".format(
            _baseline_command_for(kind, options), _check_command_for(kind, options)
        )
    if kind == "command":
        return "{} --timeout {}".format(_check_command_for(kind, options), int(options.timeout))
    return _check_command_for(kind, options)


def _judge(options: argparse.Namespace, cwd: str) -> CheckOutcome:
    """The existing check helpers, unchanged. Raises ``ValueError`` when malformed."""
    kind = _kind(options)
    if kind == "file":
        return check_file(options.file, cwd=cwd, timeout=options.timeout)
    if kind == "command":
        return check_command(options.command, cwd=cwd, timeout=options.timeout)
    return check_diff(cwd=cwd, timeout=options.timeout)


def run_up(argv: List[str]) -> int:
    options = _parse_args(argv)
    cwd = os.path.abspath(options.cwd)
    if not os.path.isdir(cwd):
        # A usage error, not a verdict: there is no directory to judge, and
        # printing "INVALID" for a typo'd --cwd would blame the claim.
        print("up: directory does not exist: {}".format(cwd), file=sys.stderr)
        return EXIT_USAGE

    if not run_self_audit():
        return _error(
            "self-audit failed, so nothing was judged",
            "harness-audit",
        )

    kind = _kind(options)

    # Asked before setup, because setup records a baseline by default: doing
    # that here would overwrite the very memory this run is about to judge, and
    # a fresh recording made after the change can only produce a false FAIL.
    has_recording = baseline_mod.load(cwd) is not None
    did_setup = False

    if not _is_configured(cwd, mcp_config=options.mcp_config, ref_roots=options.ref_roots):
        setup_argv = ["--cwd", cwd]
        if options.local:
            # Same recording location the caller asked for; otherwise `up` would
            # take a machine-wide recording and then report a local file missing.
            setup_argv.append("--local")
        if has_recording:
            setup_argv.append("--no-baseline")
        if options.mcp_config:
            setup_argv += ["--mcp-config", options.mcp_config]
        if options.ref_roots:
            setup_argv += ["--ref-roots", options.ref_roots]
        code, stderr = _quiet(run_setup, setup_argv)
        if code != 0:
            return _error(
                "setup could not write the client configuration",
                "harness-audit setup {}".format(" ".join(_quote(a) for a in setup_argv)),
            )
        did_setup = True

    recorded_files = 0
    did_record = False
    if not has_recording:
        files, _matched, _truncated = collect_common_files(cwd, limit=options.max_files)
        if kind == "file" and options.file:
            # The one file this run is about, recorded first. Otherwise the
            # capped common-file walk can miss it, and the very next `up --file`
            # would answer INVALID "the recording does not name it" -- a
            # correction the operator already made impossible to fix.
            files = list(dict.fromkeys([options.file, *files]))
        try:
            recorded, _written = baseline_mod.record(
                cwd, files=files, local=options.local
            )
        except OSError:
            return _error(
                "a baseline could not be written, so nothing can be compared",
                " ".join(
                    _quote(part)
                    for part in (
                        ["harness-audit", "baseline", "--cwd", cwd]
                        + (["--local"] if options.local else [])
                    )
                ),
            )
        recorded_files = len(recorded.files)
        did_record = True
        # `up` took the recording itself, so it has to log it itself: the
        # `baseline` subcommand is not on the path here, and a log that only
        # sees the subcommand would claim this project was never recorded.
        usage.record_baseline(
            files=recorded_files, local=bool(options.local), cwd=cwd
        )

    # A recording taken seconds ago cannot be contradicted by a change made
    # before it exists, so judging now can only produce "nothing changed since
    # the recording" -- a true statement about the lattice and a false one about
    # the operator's world. Stop after configuring and say what to do next.
    #
    # ``--command`` is exempt: an exit status does not compare against a
    # recording, so there is nothing unfalsifiable about running it now, and
    # skipping it would silently not run the command the caller asked about.
    if did_record and kind in ("file", "diff"):
        _report_config(configured=did_setup, files=recorded_files)
        return EXIT_PASS

    try:
        outcome = _judge(options, cwd)
    except ValueError as problem:
        return _error(
            "declaration malformed: {}".format(problem),
            _check_command_for(kind, options),
        )

    # The usage log observes; it never decides. `observe_check` swallows its own
    # errors, but the verdict is already fixed by here, so this cannot move it.
    usage.observe_check(
        outcome,
        declaration_kind=kind,
        cwd=cwd,
        path=options.file if kind == "file" else None,
    )

    next_step = outcome.next_step
    if outcome.verdict is Verdict.INVALID:
        next_step = _corrective(kind, options, outcome)
    else:
        next_step = _humanise(next_step, options)
    _report(outcome.verdict, outcome.reason, next_step)

    if options.verbose:
        # After the three lines, and only when asked: the contract is the three
        # lines, and a trace is a diagnosis of them, not part of the answer.
        _print_trace(outcome)

    return _VERDICT_EXIT[outcome.verdict]


def _print_trace(outcome: CheckOutcome) -> None:
    print("")
    print("declaration      : {}".format(outcome.declaration))
    print("witness_type     : {}".format(outcome.witness_type))
    print("failure_kind     : {}".format(outcome.failure_kind))
    print("evidence         : {}".format(sorted(outcome.evidence)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_up(sys.argv[1:]))
