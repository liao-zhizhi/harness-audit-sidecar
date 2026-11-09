"""A recording of the workspace, taken *before* a claim needs judging.

``check --file`` is asked "did this file change?", and "changed" is a
comparison: it needs a before. Without one the honest answers are only "it
exists" and "it does not", which is why the check reports ``INVALID`` with a
``next_step`` instead of guessing. This module is where the "before" lives.

Two things are recorded, because the two checks need different witnesses:

* ``dirty`` -- ``GitWitness.snapshot()`` of the working tree. ``--diff`` uses
  it to isolate changes made *after* the recording from dirt that was already
  there.
* ``files`` -- a SHA-256 per path the operator named with ``--file``. ``--file``
  needs content, not git status: a file can be dirty relative to ``HEAD`` while
  being byte-identical to the recording, and that is not a change.

The file is JSON and deliberately not a ledger: it is local scratch state, not
evidence. A caller who deletes it gets ``INVALID``, which is the correct
outcome -- the audit's claim is only ever "since the recording I was given",
never "since the beginning of time".

Storage is keyed by repository root so the shared ``~/.gradia`` file can serve
several projects without one silently answering for another. ``--local``
writes beside the code instead, which is what a CI job wants: the recording
travels with the checkout rather than with the machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .git_witness import GitWitness, GitWitnessError

__all__ = [
    "BASELINE_FILENAME",
    "GLOBAL_BASELINE_PATH",
    "BASELINE_VERSION",
    "RepoBaseline",
    "baseline_path",
    "load",
    "record",
    "resolve_repo",
]

#: Version of the on-disk shape. A different number is refused rather than
#: coerced: a baseline read with the wrong rules would answer a question the
#: operator did not ask.
BASELINE_VERSION = 1

#: Written beside the checkout by ``--local``.
BASELINE_FILENAME = ".harness-baseline.json"

#: Shared across projects on this machine, and keyed by repository root.
GLOBAL_BASELINE_PATH = Path.home() / ".gradia" / "baseline.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_repo(cwd: str) -> Optional[str]:
    """The repository root containing ``cwd``, or ``None`` outside git."""
    try:
        return GitWitness(cwd).repo_root
    except (GitWitnessError, OSError):
        return None


def baseline_path(cwd: str, *, local: bool = False) -> Path:
    """Where a recording is written. ``--local`` beside the checkout."""
    if local:
        return Path(os.path.abspath(cwd)) / BASELINE_FILENAME
    return GLOBAL_BASELINE_PATH


def _key_for(path: str, cwd: str, root: Optional[str]) -> str:
    """A stable key: repo-root-relative inside git, else cwd-relative."""
    absolute = path if os.path.isabs(path) else os.path.join(os.path.abspath(cwd), path)
    absolute = os.path.normpath(absolute)
    base = root or os.path.abspath(cwd)
    try:
        relative = os.path.relpath(absolute, base)
    except ValueError:  # different drive on Windows
        return absolute
    return relative.replace(os.sep, "/")


def sha256_file(path: str) -> Optional[str]:
    """The digest of a regular file, or ``None`` when it cannot be read.

    ``None`` is not "unchanged": callers must report ``INVALID``. A symlink is
    not followed -- the same rule ``GitWitness._fingerprint`` applies, so a
    link to ``/dev/zero`` cannot hang the recording either.
    """
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


@dataclass(frozen=True)
class RepoBaseline:
    """One repository's recording, as read back from disk."""

    repo_root: str
    created_at: str = ""
    git_head: Optional[str] = None
    dirty: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Which file this recording came from, for the reason line.
    source: str = ""

    def file_digest(self, key: str) -> Optional[str]:
        entry = self.files.get(key)
        if not isinstance(entry, Mapping):
            return None
        digest = entry.get("sha256")
        return digest if isinstance(digest, str) and digest else None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "created_at": self.created_at,
            "git_head": self.git_head,
            "dirty": dict(self.dirty),
            "files": {key: dict(value) for key, value in self.files.items()},
        }


def _read_document(path: Path) -> Optional[Dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("version") != BASELINE_VERSION:
        return None
    repos = document.get("repos")
    return repos if isinstance(repos, dict) else None


def load(
    cwd: str, *, repo_root: Optional[str] = None, local: Optional[bool] = None
) -> Optional[RepoBaseline]:
    """Read the recording for this repository, or ``None``.

    ``local=None`` means "prefer ``./.harness-baseline.json``, else the shared
    file". The local file wins because it is the one committed beside the code,
    and mixing the two would let a stale machine-wide entry answer for a fresh
    checkout.
    """
    root = repo_root if repo_root is not None else resolve_repo(cwd)
    key = root or os.path.abspath(cwd)
    candidates: List[Path] = []
    if local is not False:
        candidates.append(Path(os.path.abspath(cwd)) / BASELINE_FILENAME)
        if root and os.path.abspath(cwd) != root:
            candidates.append(Path(root) / BASELINE_FILENAME)
    if local is not True:
        candidates.append(GLOBAL_BASELINE_PATH)

    for path in candidates:
        repos = _read_document(path)
        if not repos:
            continue
        entry = repos.get(key)
        if not isinstance(entry, dict):
            continue
        dirty = entry.get("dirty")
        files = entry.get("files")
        return RepoBaseline(
            repo_root=key,
            created_at=str(entry.get("created_at") or ""),
            git_head=entry.get("git_head"),
            dirty=dirty if isinstance(dirty, dict) else {},
            files=files if isinstance(files, dict) else {},
            source=str(path),
        )
    return None


def _git_head(root: Optional[str]) -> Optional[str]:
    if root is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def record(
    cwd: str, *, files: Iterable[str] = (), local: bool = False
) -> Tuple[RepoBaseline, Path]:
    """Take a recording and write it. Returns ``(baseline, path_written)``.

    A path named in ``files`` but unreadable is recorded with ``sha256: null``
    rather than omitted. Omitting it would make the next ``--file`` check say
    "no recording for this path" and send the operator to re-run ``baseline``
    forever; the null says "recorded, unreadable", which is the fact.
    """
    root = resolve_repo(cwd)
    dirty: Dict[str, str] = {}
    if root is not None:
        try:
            dirty = dict(GitWitness(root).snapshot())
        except (GitWitnessError, OSError):
            dirty = {}

    recorded_files: Dict[str, Dict[str, Any]] = {}
    for name in files:
        key = _key_for(name, cwd, root)
        absolute = os.path.join(root or os.path.abspath(cwd), key.replace("/", os.sep))
        recorded_files[key] = {
            "sha256": sha256_file(absolute),
            "size": _size(absolute),
        }

    baseline = RepoBaseline(
        repo_root=root or os.path.abspath(cwd),
        created_at=_now(),
        git_head=_git_head(root),
        dirty=dirty,
        files=recorded_files,
    )

    path = baseline_path(cwd, local=local)
    path.parent.mkdir(parents=True, exist_ok=True)
    repos: Dict[str, Any] = {}
    existing = _read_document(path)
    if existing:
        # Other repositories sharing this file keep their recordings. Replacing
        # the document would make one project's baseline delete another's,
        # which is the same class of silent loss as overwriting ref_roots.json.
        repos.update(
            {key: value for key, value in existing.items() if key != baseline.repo_root}
        )
    repos[baseline.repo_root] = baseline.as_dict()
    document = {"version": BASELINE_VERSION, "repos": repos}
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return RepoBaseline(**{**baseline.__dict__, "source": str(path)}), path
