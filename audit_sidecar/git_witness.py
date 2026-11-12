"""Git-backed witness: what actually changed, versus what was claimed.

An agent saying "I edited ``target.py``" is a claim, not a fact. Git holds a
second, independent record of which paths in a working tree really moved.
``GitWitness`` reads that record instead of trusting the agent.

Two things make this witness trustworthy where a plain ``git status`` would
not be:

1. **Coordinates.** ``git status`` reports paths relative to the repository
   root. A command running in ``sub/`` that correctly edits ``sub/target.py``
   shows up as ``sub/target.py``, not ``target.py``. Comparing a raw expected
   list against raw status output would manufacture drift out of a correct
   edit, so ``resolve_expected`` maps caller-supplied paths (relative to the
   command's cwd) into repository-root space first. Mismatched coordinate
   systems are how an auditor ends up accusing the innocent.

2. **Baselines.** Repositories are usually dirty before the audited command
   runs. A status snapshot taken afterwards cannot tell "this command changed
   it" from "it was already changed". ``snapshot`` records a content
   fingerprint of every dirty path, and ``changed_since`` reports only paths
   that are new or whose content actually differs. Without this a long-lived
   workspace would report every historical edit as fresh evidence.

Only the standard library is used; git is invoked through ``subprocess``.

**An unreadable file is not a changed file.** The fingerprint needs to read
content, so it can fail (EACCES, EIO, EMFILE under descriptor pressure). An
earlier version turned that failure into the token ``"<status>:unreadable"``,
which differs from the baseline digest and therefore *looks exactly like a
change*. An agent could then satisfy ``expected_files_modified`` by running
``chmod 000`` on the target and doing no work at all: the witness reported a
move, the verdict came back ``PASS``. That is the failure this module exists
to catch, committed by the module. ``changed_since`` now raises
``GitWitnessError`` when either side of a comparison is unreadable, and the
runner converts that into "no witness" (``INVALID``), never into evidence.

**Non-regular files are unreadable, and that is deliberate (fail-closed).**
``snapshot`` takes ``os.lstat`` before opening anything, and only a regular
file is hashed. A symlink, device node or FIFO is recorded as
``__unreadable__`` rather than followed: ``ln -s /dev/zero link`` would
otherwise block the snapshot forever, and a device's content is not a fact
about this tree. The consequence is blunt and intended: **one dirty
non-regular path makes every change-set verdict in that repository
``INVALID``**, exactly as an unreadable regular file does. If an audit keeps
answering ``INVALID``, look for a dirty symlink or device in
``git status`` -- that is the check working, not a false positive.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import subprocess
from typing import Dict, List, Optional, Sequence

__all__ = ["GitWitness", "GitWitnessError"]

_LOG = logging.getLogger(__name__)

# Marker appended to a fingerprint whose content could not be read. It is not
# a digest and must never be compared as one; ``changed_since`` refuses it.
_UNREADABLE = "__unreadable__"


def _is_unreadable(state: str) -> bool:
    """True when a snapshot entry means "could not look", not "did not move"."""
    return state.endswith(":" + _UNREADABLE)

DEFAULT_TIMEOUT = 15.0

# Marker used in a snapshot for a path with no working-tree content.
_ABSENT = "-"

# Filesystem entries that are never part of an intent.
_VCS_ARTEFACT = os.path.join(".git")


class GitWitnessError(RuntimeError):
    """Raised when the witness cannot observe a repository at all.

    ``SafeRunner.run_shell`` catches this during construction or snapshot
    (not a repo, missing git, timeout) and falls back to declared-file
    ``stat`` so the command still runs. An unreadable change set *after*
    the command has already executed is still "we could not look".
    """


class GitWitness:
    """Read the real change set of a git working tree."""

    def __init__(self, repo_path: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not isinstance(repo_path, str) or not repo_path:
            raise ValueError("repo_path must be a non-empty string")
        if timeout <= 0:
            raise ValueError("timeout must be positive, got {}".format(timeout))
        self._git = shutil.which("git")
        if self._git is None:
            raise GitWitnessError("git executable not found on PATH")
        self._timeout = float(timeout)
        self._root = self._discover_root(repo_path)

    # -- construction helpers -----------------------------------------

    def _discover_root(self, repo_path: str) -> str:
        if not os.path.isdir(repo_path):
            raise GitWitnessError("no such directory: {}".format(repo_path))
        try:
            top = self._git_out(
                ["rev-parse", "--show-toplevel"], cwd=repo_path
            ).strip()
        except GitWitnessError as problem:
            # Keep git's own diagnosis. Replacing it with a fixed "not a git
            # repository" sent operators looking for a missing `git init` when
            # the real cause was a uid mismatch on a bind mount, which git
            # reports as "detected dubious ownership". The two need different
            # fixes, so the message has to say which one it is.
            raise GitWitnessError(
                "cannot read a git repository at {}: {}".format(
                    os.path.abspath(repo_path), problem
                )
            ) from None
        if not top:
            raise GitWitnessError(
                "not a git repository: {}".format(os.path.abspath(repo_path))
            )
        return os.path.abspath(top)

    @staticmethod
    def is_repository(path: str) -> bool:
        """True when ``path`` sits inside a git working tree."""
        if not os.path.isdir(path):
            return False
        git = shutil.which("git")
        if git is None:
            return False
        completed = subprocess.run(
            [git, "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
        )
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    # -- observation --------------------------------------------------

    @property
    def repo_root(self) -> str:
        return self._root

    def get_changed_files(self) -> List[str]:
        """Return every path that differs from HEAD, repo-root relative.

        Includes staged, unstaged, untracked and deleted paths. Ignored paths
        are excluded because git does not report them. Paths are POSIX-style
        and sorted, so the result is stable across runs and platforms.
        """
        return sorted(self._status_map())

    def has_changes(self) -> bool:
        return bool(self._status_map())

    def snapshot(self) -> Dict[str, str]:
        """Fingerprint the current dirty set, for use with ``changed_since``."""
        fingerprint: Dict[str, str] = {}
        for path, status in self._status_map().items():
            fingerprint[path] = self._fingerprint(path, status)
        return fingerprint

    def changed_since(self, baseline: Dict[str, str]) -> List[str]:
        """Return paths whose state differs from ``baseline``.

        A path counts as changed when it is new, its git status code changed,
        or its working-tree content changed. This is what isolates the effect
        of one command from whatever dirt the workspace already carried.

        Raises
        ------
        GitWitnessError
            When a compared path could not be fingerprinted on either side.
            "I could not read it" is not "it changed": returning it as a move
            would let a chmod satisfy an expectation with no work done.
        """
        if not isinstance(baseline, dict):
            raise TypeError("baseline must be a dict from snapshot()")
        current = self.snapshot()
        moved = []
        for path, state in current.items():
            previous = baseline.get(path)
            for side, value in (("baseline", previous), ("current", state)):
                if value is not None and _is_unreadable(value):
                    raise GitWitnessError(
                        "cannot fingerprint {!r} in the {} snapshot; an "
                        "unreadable file is not evidence of a change".format(
                            path, side
                        )
                    )
            if previous != state:
                moved.append(path)
        # A path that was dirty at the baseline and is no longer dirty vanished
        # from ``current``. That is a change too: the world moved, back to the
        # committed (or untracked-and-removed) state. Iterating only the current
        # set would report "nothing changed" for a command that reverted drift.
        vanished = baseline.keys() - current.keys()
        for path in vanished:
            previous = baseline.get(path)
            if previous is not None and _is_unreadable(previous):
                raise GitWitnessError(
                    "cannot fingerprint {!r} in the baseline snapshot; an "
                    "unreadable file is not evidence of a change".format(path)
                )
        moved.extend(vanished)
        return sorted(moved)

    def resolve_expected(
        self, expected: Sequence[str], cwd: str
    ) -> List[str]:
        """Map caller paths into repository-root space.

        ``expected`` entries are interpreted relative to ``cwd`` -- the
        directory the audited command ran in -- because that is the coordinate
        system the caller was thinking in. The returned paths match what
        ``get_changed_files`` reports.
        """
        if isinstance(expected, (str, bytes)):
            raise TypeError(
                "expected must be a sequence of paths, not a single string"
            )
        resolved: List[str] = []
        cwd_abs = os.path.abspath(cwd)
        for entry in expected:
            if not isinstance(entry, str) or not entry:
                raise ValueError("expected paths must be non-empty strings")
            absolute = (
                entry if os.path.isabs(entry) else os.path.join(cwd_abs, entry)
            )
            absolute = os.path.normpath(absolute)
            relative = os.path.relpath(absolute, self._root)
            if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                raise ValueError(
                    "expected path is outside the repository: {!r}".format(entry)
                )
            resolved.append(relative.replace(os.sep, "/"))
        return resolved

    # -- internals ----------------------------------------------------

    def _fingerprint(self, path: str, status: str) -> str:
        absolute = os.path.join(self._root, path.replace("/", os.sep))
        if not os.path.lexists(absolute):
            return "{}:{}".format(status, _ABSENT)
        # lstat, not open: a symlink or device is not a regular file, and
        # opening one can block forever (``ln -s /dev/zero link``) or read an
        # unbounded amount of data. Neither is evidence about this tree.
        try:
            info = os.lstat(absolute)
        except OSError as problem:
            _LOG.error(
                "could not lstat %s: %s; treating the witness as unreadable "
                "rather than changed", absolute, problem)
            return "{}:{}".format(status, _UNREADABLE)
        if stat.S_ISDIR(info.st_mode):
            return "{}:dir".format(status)
        if not stat.S_ISREG(info.st_mode):
            _LOG.warning(
                "%s is not a regular file (mode %o); treating the witness as "
                "unreadable rather than opening it", absolute, info.st_mode)
            return "{}:{}".format(status, _UNREADABLE)
        digest = hashlib.sha256()
        try:
            with open(absolute, "rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
        except OSError as problem:
            # Never let this look like a change: see the module docstring.
            _LOG.error(
                "could not fingerprint %s: %s; treating the witness as "
                "unreadable rather than changed", absolute, problem)
            return "{}:{}".format(status, _UNREADABLE)
        return "{}:{}".format(status, digest.hexdigest())

    def _status_map(self) -> Dict[str, str]:
        """Parse ``git status --porcelain -z`` into {path: XY code}.

        ``-z`` avoids git's quoting of unusual filenames, and ``--no-renames``
        keeps every record a single (code, path) pair so a rename does not
        consume the following record. ``-uall`` forces every untracked file to
        be listed individually, which matters because a new directory is
        exactly how target drift usually looks.
        """
        raw = self._git_out(
            [
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
            ]
        )
        entries: Dict[str, str] = {}
        for record in raw.split("\0"):
            if not record:
                continue
            if len(record) < 4:
                _LOG.debug("unexpected status record: %r", record)
                continue
            code = record[:2]
            path = record[3:]
            if path == _VCS_ARTEFACT or path.startswith(_VCS_ARTEFACT + "/"):
                continue
            entries[path] = code
        return entries

    def _git_out(self, args: Sequence[str], cwd: Optional[str] = None) -> str:
        argv = [self._git, *args]
        environment = dict(os.environ)
        # Never block an audit on a credential prompt.
        environment.setdefault("GIT_TERMINAL_PROMPT", "0")
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd or self._root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self._timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            raise GitWitnessError(
                "git timed out after {:.1f}s: {}".format(self._timeout, args)
            ) from None
        except OSError as problem:
            raise GitWitnessError("git could not be executed: {}".format(problem))
        if completed.returncode != 0:
            raise GitWitnessError(
                "git {} failed ({}): {}".format(
                    " ".join(args), completed.returncode, completed.stderr.strip()
                )
            )
        return completed.stdout

    # -- dunder -------------------------------------------------------

    def __repr__(self) -> str:
        return "GitWitness(repo_root={!r})".format(self._root)
