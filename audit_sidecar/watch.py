"""``harness-audit watch``: tell me when the tree moved and the claim broke.

A poller around ``check --diff``. It exists because ``check`` answers when you
ask, and the moment you stop asking is the moment an agent starts claiming.
The loop has three parts, and each is injectable so the loop itself can be
tested without a clock or a repository:

``signature``   what "the tree changed" means, as a string
``check``       the judgement, reusing :func:`audit_sidecar.check.check_diff`
``notify``      the operating system's notification, or a no-op

What it deliberately does not do:

* **no auto-fix.** A watcher that reverts a file is a new authority in the
  loop, and the thing being audited would then also be the thing being edited.
* **no auto-baseline.** Writing a recording after a change would make the
  change look like it happened before the claim -- the exact fabrication
  ``check`` refuses.
* **no ledger, no upload.** The verdict is for the person at the keyboard.
  ``--upload`` exists on ``check``, and a background poller that phones home on
  every save is not the same product.
* **no new verdict logic.** FAIL and INVALID are the two values worth
  interrupting a human for; PASS is the answer working, and NOT RUN is not
  produced by ``check_diff`` at all.

The change signature is ``git status --porcelain`` **plus** the size and mtime
of every path it reports as dirty or untracked. Porcelain alone does not change
when an already-dirty file is edited again, so a watcher built on it alone goes
quiet exactly during an agent's edit-edit-edit sequence. The stat pass is one
``lstat`` per dirty path; file *content* is never read, so the cost depends on
how many paths are dirty, not on how large they are.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, List, Optional

from . import usage
from .check import CheckOutcome, check_diff
from .git_witness import GitWitnessError
from .verdict import Verdict

#: Two seconds: fast enough that a notification arrives while the operator is
#: still looking at the agent, slow enough that a large checkout is not paying
#: for two git invocations constantly.
DEFAULT_INTERVAL = 2.0

#: Verdicts worth a desktop notification. PASS is the tool agreeing with the
#: claim, and an unrequested interruption for "everything is fine" is how
#: notifications get muted -- including the one that matters.
NOTIFY_VERDICTS = frozenset({Verdict.FAIL, Verdict.INVALID})

#: The tail every notification carries. Short on purpose: a notification is a
#: pointer, and a full reason in a system popup is unreadable on a phone-width
#: toast.
DETAIL_HINT = "run `harness-audit up` for details"


def first_sentence(text: str) -> str:
    """The first sentence, terminated. ``reason`` is already one sentence.

    Kept as a function because reasons *can* grow a second clause (`...;
    nothing was witnessed`), and a notification that silently keeps three
    clauses is a notification nobody reads.
    """
    body = (text or "").strip()
    if not body:
        return ""
    for index, char in enumerate(body):
        if char in ".!?" and (index + 1 == len(body) or body[index + 1] == " "):
            return body[: index + 1]
    return body


def git_signature(cwd: str, *, timeout: float = 10.0) -> str:
    """A string that changes when the working tree changes. Raises on no repo.

    Two parts, and the second is not redundant:

    * ``git status --porcelain`` -- which paths are new, deleted or dirty. This
      is the part a human would read, and it is what "the tree changed" means.
    * size and mtime of every modified or untracked path -- because porcelain
      does *not* change when an already-dirty file is saved again (`` M f``
      before and after). Without the stat pass, a watcher built on porcelain
      alone goes quiet during exactly the edit-edit-edit sequence it exists to
      watch. Sizes and mtimes are read from the filesystem, not diffed, so the
      cost is one ``lstat`` per dirty path rather than a full patch.

    Content is never read, so the cost does not depend on how large the change
    is -- only on how many paths are dirty.
    """
    _repo_root(cwd)  # raises when there is no repository to watch
    porcelain = _git(cwd, ["status", "--porcelain"], timeout=timeout)
    observed = _stat_dirty(cwd, timeout=timeout)
    return "{}\n{}\n{}".format(cwd, porcelain, observed)


def _stat_dirty(cwd: str, *, timeout: float) -> str:
    """One line per modified/untracked path: name, size, mtime. Not content."""
    listing = _git(cwd, ["ls-files", "-m", "-o", "--exclude-standard", "-z"], timeout=timeout)
    parts: List[str] = []
    for name in listing.split("\0"):
        if not name:
            continue
        try:
            info = os.lstat(os.path.join(cwd, name))
        except OSError:
            parts.append("{} missing".format(name))
            continue
        parts.append("{} {} {}".format(name, info.st_size, info.st_mtime_ns))
    return "\n".join(sorted(parts))


def _repo_root(cwd: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    if completed.returncode != 0:
        raise GitWitnessError(completed.stderr.strip() or "not a git repository")
    return completed.stdout.strip()


def _git(cwd: str, args: List[str], *, timeout: float) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise GitWitnessError(
            completed.stderr.strip() or "git {} failed".format(" ".join(args))
        )
    return completed.stdout


# ---------------------------------------------------------------------------
# notification
# ---------------------------------------------------------------------------


def notification_body(outcome: CheckOutcome) -> str:
    verdict = outcome.verdict.value
    sentence = first_sentence(outcome.reason)
    if sentence:
        return "{}: {} — {}".format(verdict, sentence, DETAIL_HINT)
    return "{} — {}".format(verdict, DETAIL_HINT)


def _osascript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(
    outcome: CheckOutcome,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
    platform: Optional[str] = None,
) -> str:
    """Send one OS notification. Returns the channel used, for tests and logs.

    ``"notify-send"``, ``"osascript"`` or ``"skipped"``. A missing notifier is
    not an error: the body is printed to stderr either way, so the loop works
    over SSH and in CI, where a desktop notifier is exactly what is absent.
    """
    body = notification_body(outcome)
    system = platform or sys.platform
    channel = "skipped"
    try:
        if system.startswith("linux") and which("notify-send"):
            completed = runner(
                ["notify-send", "--app-name", "harness-audit", outcome.verdict.value, body],
                check=False,
                capture_output=True,
            )
            # A notifier that is installed but has no session bus (headless
            # CI, ssh without a desktop) exits non-zero. Reporting it as sent
            # would be a lie in exactly the environment where the stderr line
            # is the only thing the operator sees.
            if getattr(completed, "returncode", 0) == 0:
                channel = "notify-send"
        elif system == "darwin" and which("osascript"):
            script = 'display notification "{}" with title "harness-audit: {}"'.format(
                _osascript_escape(body), _osascript_escape(outcome.verdict.value)
            )
            completed = runner(["osascript", "-e", script], check=False, capture_output=True)
            if getattr(completed, "returncode", 0) == 0:
                channel = "osascript"
        # Windows: deliberately skipped rather than half-implemented. The toast
        # API needs a registered AppUserModelID, and a PowerShell balloon that
        # may not appear is worse than the stderr line that always does.
    except (OSError, subprocess.SubprocessError):
        channel = "skipped"
    print(body, file=sys.stderr)
    return channel


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


class WatchLoop:
    """One poller, three seams. ``run`` is the only method with state in it."""

    def __init__(
        self,
        *,
        cwd: str,
        interval: float = DEFAULT_INTERVAL,
        signature: Optional[Callable[[str], str]] = None,
        checker: Optional[Callable[[str], CheckOutcome]] = None,
        notifier: Optional[Callable[[CheckOutcome], str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = None,
    ) -> None:
        self._cwd = cwd
        self._interval = max(0.0, float(interval))
        self._signature = signature or (lambda where: git_signature(where))
        self._checker = checker or (
            lambda where: check_diff(cwd=where)
        )
        self._notifier = notifier or (lambda outcome: notify(outcome))
        self._sleep = sleep
        self._log = log or (lambda message: print(message, file=sys.stderr))

    def run(self, iterations: Optional[int] = None) -> int:
        """Poll until interrupted, or ``iterations`` ticks (tests).

        The first signature is taken *before* the first sleep: whatever is
        already dirty when the watcher starts is the baseline. Notifying on it
        would report the operator's own uncommitted work as a new interception.

        Two stop paths, because a watcher that only listens for one of them is a
        watcher that has to be killed: ctrl-c raises ``KeyboardInterrupt``, and
        ``SIGTERM``/``SIGHUP`` (docker stop, closing a terminal, systemd) set a
        flag the loop checks each tick. ``SIGINT`` is also handled explicitly --
        a process started with ``SIGINT`` ignored (a background job in a
        non-interactive shell) never gets Python's default handler, and would
        otherwise be unstoppable.
        """
        stopped = {"flag": False}
        previous = self._install_stop_handlers(stopped)
        try:
            return self._loop(iterations, stopped)
        finally:
            self._restore_stop_handlers(previous)

    def _install_stop_handlers(self, stopped) -> dict:
        previous = {}
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                previous[number] = signal.signal(number, lambda *_: stopped.__setitem__("flag", True))
            except (ValueError, OSError):  # not the main thread, or unsupported
                continue
        return previous

    def _restore_stop_handlers(self, previous: dict) -> None:
        for number, handler in previous.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError):  # pragma: no cover - defensive
                continue

    def _loop(self, iterations: Optional[int], stopped) -> int:
        try:
            previous = self._signature(self._cwd)
        except (GitWitnessError, OSError) as problem:
            self._log("watch: cannot read the tree: {}".format(problem))
            return 3
        self._log(
            "watching {} every {:g}s; ctrl-c to stop".format(self._cwd, self._interval)
        )
        ticks = 0
        try:
            while iterations is None or ticks < iterations:
                if stopped["flag"]:
                    self._log("watch: stopped")
                    return 0
                ticks += 1
                self._sleep(self._interval)
                if stopped["flag"]:
                    self._log("watch: stopped")
                    return 0
                try:
                    current = self._signature(self._cwd)
                except (GitWitnessError, OSError) as problem:
                    self._log("watch: cannot read the tree: {}".format(problem))
                    return 3
                if current == previous:
                    continue
                previous = current
                outcome = self._checker(self._cwd)
                # The usage log records the verdict; `watch` still uploads
                # nothing and writes no ledger. Local row, local file.
                usage.observe_check(outcome, declaration_kind="diff", cwd=self._cwd)
                if outcome.verdict in NOTIFY_VERDICTS:
                    self._notifier(outcome)
        except KeyboardInterrupt:
            self._log("watch: stopped")
            return 0
        return 0


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="harness-audit watch",
        description=(
            "Poll the git working tree; run `check --diff` when it changes and "
            "notify on FAIL/INVALID. Never fixes, never re-baselines."
        ),
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="repository to watch (default: current)")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="seconds between polls (default: %g)" % DEFAULT_INTERVAL,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="seconds allowed for each check (default: 30)",
    )
    parser.add_argument("--json", action="store_true", help="one JSON line per notification")
    return parser.parse_args(argv)


def run_watch(argv: List[str]) -> int:
    options = _parse_args(argv)
    cwd = os.path.abspath(options.cwd)
    if not os.path.isdir(cwd):
        print("watch: directory does not exist: {}".format(cwd), file=sys.stderr)
        return 2

    try:
        _repo_root(cwd)
    except (GitWitnessError, OSError) as problem:
        # Not a repository: there is no change set to poll, and inventing one
        # is what this package refuses everywhere else.
        print("watch: not a git repository: {}".format(cwd), file=sys.stderr)
        print(
            "next_step: run it inside a repository, or pass "
            "`harness-audit watch --cwd <repo>`",
            file=sys.stderr,
        )
        return 3

    def notifier(outcome: CheckOutcome) -> str:
        if options.json:
                if options.json:
                    print(
                        json.dumps(
                            {
                                "verdict": outcome.verdict.value,
                                "reason": first_sentence(outcome.reason),
                                "next_step": DETAIL_HINT,
                            },
                            ensure_ascii=False,
                        ),
                        # Flushed per line: this is a log that a script may be
                        # following, and a redirect makes stdout block-buffered,
                        # so without this the notification appears only when the
                        # process exits -- which, for a watcher, is when it is
                        # least useful.
                        flush=True,
                    )
                    return "stdout"
        return notify(outcome)

    loop = WatchLoop(
        cwd=cwd,
        interval=options.interval,
        checker=lambda where: check_diff(cwd=where, timeout=options.timeout),
        notifier=notifier,
    )
    return loop.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_watch(sys.argv[1:]))
