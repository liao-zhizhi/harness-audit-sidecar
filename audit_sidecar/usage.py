"""Usage log: what the tool was asked to do, never what it was shown.

``~/.gradia/usage.jsonl``, append-only, one JSON object per line. Five event
types:

``check``               a declaration was judged, and with what verdict
``baseline``            a recording was taken
``setup``               client configuration was written
``mcp_tool_call``       an audited MCP tool call reached a verdict
``suspected_uncovered`` a FAIL with no audited write anywhere near it

The schema is deliberately narrow, because a usage log is the easiest place to
leak the thing the tool is supposed to keep private:

* **no claim text.** ``command <cmd>`` stores ``declaration: "command"`` and
  nothing else. The command line is the user's business, and it routinely
  contains tokens.
* **no plaintext paths.** A file claim stores ``path_hash``: an HMAC-SHA256 of
  the repository-relative path under a machine-local key
  (``~/.gradia/usage.salt``, created ``0600``). The key exists because a bare
  SHA-256 over ``src/main.py`` is reversible against a dictionary of plausible
  filenames in about no time; a keyed digest is not reversible without the key
  file. This is a privacy boundary, not a cryptographic guarantee: anyone who
  can read the salt *and* the log can confirm a guessed path. Deleting the salt
  makes existing hashes unlinkable -- and makes it impossible to tell whether
  two rows are the same path. That trade is the user's to make.
* **no code.** Nothing here reads a file's contents.

Two properties the rest of the package depends on:

1. **Nothing in this module can change a verdict.** ``record`` never raises:
   a full disk, a read-only home, a malformed existing line -- each is swallowed
   and reported at debug level. An auditor that fails because its telemetry
   failed has inverted its own priorities.
2. **Nothing here is uploaded by default.** ``--upload`` sends
   :func:`aggregate` -- three fields -- and never the rows. The export is a
   separate function rather than "read the file and JSON it" so that adding a
   field to a row cannot silently widen what leaves the machine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

_LOG = logging.getLogger("audit_sidecar.usage")

#: Event names. A tuple, not a free string, so a typo is a programming error
#: caught by the callers' tests rather than a new event type nobody queries.
EVENTS: Tuple[str, ...] = (
    "check",
    "baseline",
    "setup",
    "mcp_tool_call",
    "suspected_uncovered",
)

#: Resolved at call time through :func:`_log_path`, so a test (or a user with a
#: different layout) can point the log somewhere else without monkeypatching
#: module state. ``HARNESS_AUDIT_USAGE_DIR`` holds both files.
USAGE_PATH = Path.home() / ".gradia" / "usage.jsonl"
SALT_PATH = Path.home() / ".gradia" / "usage.salt"
USAGE_DIR_ENV = "HARNESS_AUDIT_USAGE_DIR"

_SALT_CACHE: Dict[str, bytes] = {}

#: Tool names that write. Used only to answer "was there an audited write near
#: this FAIL", so an incomplete list produces a *false suspicion*, never a false
#: reassurance -- which is the safe direction for a diagnostic.
WRITE_TOOLS = frozenset({"write_file", "append_file"})

#: "Recently" for the uncovered rule, in seconds. Ten minutes is long enough to
#: cover an edit-then-claim turn and short enough that yesterday's write does
#: not excuse today's unsupported claim.
UNCOVERED_WINDOW_SECONDS = 600.0

#: How many trailing lines ``recent_write_tool_call`` will look at. A cap keeps
#: the check path O(1) on a machine that has been logging for a year; the
#: window (above) is what decides, and this only bounds the read.
_TAIL_LINES = 400

#: Where ``--upload`` posts the aggregate. Same base URL as the verdict report,
#: different path -- one is a decision, the other is a counter. The default is
#: the public dashboard for the same reason it is in ``report.py``: a local
#: default would fail for every install that is not the author's.
AGGREGATE_PATH = "/api/v1/usage/report"
DEFAULT_BASE_URL = "https://audit.silicon-probe.com"
UPLOAD_URL_ENV = "HARNESS_AUDIT_UPLOAD_URL"
API_KEY_ENV = "HARNESS_AUDIT_API_KEY"
API_KEY_HEADER = "X-API-Key"

#: The only three things an aggregate upload carries. A test asserts the key
#: set, so widening it is a visible decision rather than a convenience.
AGGREGATE_FIELDS: Tuple[str, ...] = ("verdicts", "baseline_misses", "tool_names")


def _log_path() -> Path:
    override = os.environ.get(USAGE_DIR_ENV)
    return Path(override) / "usage.jsonl" if override else USAGE_PATH


def _salt_path() -> Path:
    override = os.environ.get(USAGE_DIR_ENV)
    return Path(override) / "usage.salt" if override else SALT_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _salt() -> bytes:
    """Read the machine-local key, creating it ``0600`` on first use.

    A world-readable salt would defeat the point: the hash is only as private
    as the key. ``O_EXCL`` on creation so two concurrent first-runs agree on
    one key instead of the second overwriting the first.

    Cached per path rather than globally: the check path hashes at most a
    handful of values, and a per-process cache keyed by the *path* is what lets
    a test switch directories mid-run without inheriting the previous key.
    """
    target = _salt_path()
    key = str(target)
    cached = _SALT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        value = target.read_bytes().strip()
        _SALT_CACHE[key] = value
        return value
    except OSError:
        pass
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32).encode("ascii")
        handle = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(handle, value)
        finally:
            os.close(handle)
        _SALT_CACHE[key] = value
        return value
    except FileExistsError:
        # Lost the race: the winner's key is the one to use.
        try:
            value = target.read_bytes().strip()
        except OSError:
            return b""
        _SALT_CACHE[key] = value
        return value
    except OSError:
        # No writable home. Fall back to a per-process key rather than to a
        # world-guessable digest: unlinkable rows are better than reversible
        # ones, even though they cannot be correlated across runs.
        return b""


def hash_path(path: str, *, cwd: Optional[str] = None, repo_root: Optional[str] = None) -> str:
    """``path_hash`` for a file claim. Never returns the path.

    Repository-relative when possible, so a checkout at a different absolute
    path on the same machine produces the same hash. That is what lets
    ``suspected_uncovered`` match "the write tool touched this file" against
    "the check was about this file" without either side storing the name.
    """
    raw = path or ""
    base = repo_root or cwd
    if base and raw and not os.path.isabs(raw):
        absolute = os.path.normpath(os.path.join(os.path.abspath(base), raw))
    else:
        absolute = os.path.normpath(raw) if raw else ""
    if base and absolute:
        try:
            absolute = os.path.relpath(absolute, os.path.abspath(base))
        except ValueError:  # different drive on Windows
            pass
    digest = hmac.new(_salt(), absolute.encode("utf-8", "surrogateescape"), hashlib.sha256)
    return digest.hexdigest()[:32]


def record(event: str, *, path: Optional[Path] = None, **fields: Any) -> Optional[Dict[str, Any]]:
    """Append one event. Returns it, or ``None`` if the write failed.

    Never raises on I/O: see the module docstring. ``event`` is validated
    because a new event type is a schema decision, and a typo would create one
    silently.
    """
    if event not in EVENTS:
        raise ValueError(
            "unknown usage event {!r}; expected one of {}".format(event, ", ".join(EVENTS))
        )
    entry: Dict[str, Any] = {"event": event, "at": _now()}
    entry.update(fields)
    target = Path(path) if path is not None else _log_path()
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # One O_APPEND write: two processes (a check and a running `watch`)
        # interleave whole lines rather than half of each other's.
        handle = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(handle, line.encode("utf-8"))
        finally:
            os.close(handle)
    except OSError as problem:
        _LOG.debug("usage log not written (%s): %s", target, problem)
        return None
    return entry


def rows(path: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
    """Parsed events, oldest first. A torn line is skipped, not fatal."""
    target = Path(path) if path is not None else _log_path()
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("event") in EVENTS:
                    yield parsed
    except OSError:
        return


def _tail(path: Optional[Path], limit: int) -> List[Dict[str, Any]]:
    """The last ``limit`` parsed events. Bounds the uncovered-rule read."""
    kept: List[Dict[str, Any]] = []
    target = Path(path) if path is not None else _log_path()
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(parsed, dict) or parsed.get("event") not in EVENTS:
                    continue
                kept.append(parsed)
                if len(kept) > limit:
                    kept.pop(0)
    except OSError:
        return []
    return kept


# ---------------------------------------------------------------------------
# the five emitters
# ---------------------------------------------------------------------------


def record_check(
    outcome: Any,
    *,
    declaration_kind: str,
    cwd: str,
    path: Optional[str] = None,
    path_file: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Log one judged declaration. ``declaration_kind`` is a word, not the text.

    ``baseline_present`` is read from the outcome's own evidence rather than by
    loading the recording a second time: the check already looked, and asking
    twice is how the log and the receipt start disagreeing.
    """
    evidence = dict(getattr(outcome, "evidence", {}) or {})
    recording = evidence.get("recording")
    if "recording" in evidence:
        baseline_present = recording is not None
    else:
        # `--command` has no recording in its evidence: an exit status does not
        # compare against one. Whether *a* recording exists is still a fact
        # worth logging, and the cheap way to answer it is on disk.
        from . import baseline as baseline_mod

        baseline_present = baseline_mod.load(cwd) is not None

    fields: Dict[str, Any] = {
        "declaration": declaration_kind,
        "verdict": outcome.verdict.value,
        "failure_kind": outcome.failure_kind,
        "baseline_present": bool(baseline_present),
    }
    if path is not None:
        fields["path_hash"] = hash_path(path, cwd=cwd, repo_root=_repo_root(cwd))
    return record("check", path=path_file, **fields)


def record_baseline(
    *, files: int, local: bool, cwd: str, path: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """A recording is a count and a flag; the paths themselves stay out."""
    return record(
        "baseline",
        path=path,
        files=int(files),
        local=bool(local),
        repo_hash=hash_path(_repo_root(cwd) or os.path.abspath(cwd)),
    )


def record_setup(
    *,
    mcp_scope: str,
    mcp_changed: bool,
    ref_roots_changed: bool,
    baseline_files: int,
    print_only: bool,
    baseline_written: bool = True,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Which file class was touched, not which file."""
    return record(
        "setup",
        path=path,
        mcp_scope=mcp_scope,
        mcp_changed=bool(mcp_changed),
        ref_roots_changed=bool(ref_roots_changed),
        baseline_files=int(baseline_files),
        baseline_written=bool(baseline_written),
        print_only=bool(print_only),
    )


def record_tool_call(
    *,
    tool_name: str,
    verdict: Any,
    tier: Optional[str] = None,
    path_arg: Optional[str] = None,
    cwd: Optional[str] = None,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """One audited MCP call. ``path_arg`` is hashed, never stored."""
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("tool_name must be a non-empty string")
    value = getattr(verdict, "value", verdict)
    fields: Dict[str, Any] = {
        "tool_name": tool_name,
        "verdict": str(value),
    }
    if tier is not None:
        fields["tier"] = str(tier)
    if path_arg:
        fields["path_hash"] = hash_path(
            path_arg, cwd=cwd, repo_root=_repo_root(cwd) if cwd else None
        )
    return record("mcp_tool_call", path=path, **fields)


def recent_write_tool_call(
    *,
    window_seconds: float = UNCOVERED_WINDOW_SECONDS,
    path_hash: Optional[str] = None,
    path: Optional[Path] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """The newest audited write **inside the window**, or ``None``.

    ``path_hash`` given means "a write to *this* file"; ``None`` means "any
    write at all", which is the right question for a ``diff`` claim (it names
    no file) and the lenient one for a ``command`` claim.

    The scan stops at the first matching write: entries are walked newest
    first, so if that one is already outside the window, every older one is
    too. No second pass, no "was there a write at some point" answer that would
    silently excuse today's FAIL with yesterday's work.
    """
    cutoff = (time.time() if now is None else now) - max(0.0, float(window_seconds))
    for entry in reversed(_tail(path, _TAIL_LINES)):
        if entry.get("event") != "mcp_tool_call":
            continue
        if entry.get("tool_name") not in WRITE_TOOLS:
            continue
        if path_hash is not None and entry.get("path_hash") != path_hash:
            continue
        stamp = _parse_at(entry.get("at"))
        if stamp is None or stamp < cutoff:
            return None
        return entry
    return None


def last_write_tool_call(
    *, path_hash: Optional[str] = None, path: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """The newest audited write regardless of age, for the diagnostic field.

    Kept separate from :func:`recent_write_tool_call` on purpose: one function
    answering both "did a write happen recently" and "when did one last happen"
    would eventually be called with the window read as an option rather than as
    the rule.
    """
    for entry in reversed(_tail(path, _TAIL_LINES)):
        if entry.get("event") != "mcp_tool_call":
            continue
        if entry.get("tool_name") not in WRITE_TOOLS:
            continue
        if path_hash is not None and entry.get("path_hash") != path_hash:
            continue
        return entry
    return None


def record_suspected_uncovered(
    outcome: Any,
    *,
    declaration_kind: str,
    cwd: str,
    path: Optional[str] = None,
    window_seconds: float = UNCOVERED_WINDOW_SECONDS,
    path_file: Optional[Path] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """A FAIL that no audited write can explain.

    The claim says the world changed and the world disagrees. Either the change
    was never made, or it was made through a path this tool cannot see -- the
    editor's own file API, a shell the harness did not launch. The second case
    is the interesting one: it is exactly the "uncovered" surface the tool
    exists to name, and it is reported as a suspicion because the log cannot
    distinguish it from a plain lie.
    """
    digest = (
        hash_path(path, cwd=cwd, repo_root=_repo_root(cwd)) if path is not None else None
    )
    moment = time.time() if now is None else now
    earlier = last_write_tool_call(path_hash=digest, path=path_file)
    age: Optional[int] = None
    if earlier is not None:
        stamp = _parse_at(earlier.get("at"))
        if stamp is not None:
            age = max(0, int(moment - stamp))
    return record(
        "suspected_uncovered",
        path=path_file,
        verdict=outcome.verdict.value,
        declaration=declaration_kind,
        path_hash=digest,
        window_seconds=int(window_seconds),
        last_write_tool=earlier.get("tool_name") if earlier else None,
        last_write_ago_seconds=age,
    )


def observe_check(
    outcome: Any,
    *,
    declaration_kind: str,
    cwd: str,
    path: Optional[str] = None,
    window_seconds: float = UNCOVERED_WINDOW_SECONDS,
    path_file: Optional[Path] = None,
) -> None:
    """Log a judged declaration, and the uncovered suspicion when it applies.

    One function so there is one place where "FAIL with no audited write" is
    decided. It returns nothing and swallows everything: this is called from
    ``check``, and a diagnostic that can raise would be a diagnostic that
    changes the exit code it is supposed to observe.
    """
    try:
        record_check(
            outcome,
            declaration_kind=declaration_kind,
            cwd=cwd,
            path=path,
            path_file=path_file,
        )
        verdict = getattr(outcome, "verdict", None)
        if getattr(verdict, "value", verdict) != "FAIL":
            return
        digest = (
            hash_path(path, cwd=cwd, repo_root=_repo_root(cwd))
            if path is not None
            else None
        )
        if recent_write_tool_call(path_hash=digest, path=path_file) is not None:
            return
        record_suspected_uncovered(
            outcome,
            declaration_kind=declaration_kind,
            cwd=cwd,
            path=path,
            window_seconds=window_seconds,
            path_file=path_file,
        )
    except Exception as problem:  # pragma: no cover - defensive by design
        _LOG.debug("usage observation skipped: %s", problem)


def _repo_root(cwd: str) -> Optional[str]:
    from . import baseline as baseline_mod

    try:
        return baseline_mod.resolve_repo(cwd)
    except (OSError, ValueError):
        return None


def _parse_at(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# export: aggregates only, never rows
# ---------------------------------------------------------------------------


def aggregate(path: Optional[Path] = None) -> Dict[str, Any]:
    """Exactly the three documented fields. Nothing else leaves via ``--upload``.

    ``verdicts`` counts ``check`` events only. Counting ``mcp_tool_call``
    verdicts too would make the number depend on how many tool calls a session
    happened to make, which is a property of the agent, not of the claims.
    """
    verdicts: Dict[str, int] = {}
    baseline_misses = 0
    tool_names: List[str] = []
    for entry in rows(path):
        event = entry.get("event")
        if event == "check":
            verdict = entry.get("verdict")
            if isinstance(verdict, str):
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
            if entry.get("baseline_present") is False:
                baseline_misses += 1
        elif event == "mcp_tool_call":
            name = entry.get("tool_name")
            if isinstance(name, str) and name and name not in tool_names:
                tool_names.append(name)
    return {
        "verdicts": verdicts,
        "baseline_misses": baseline_misses,
        "tool_names": sorted(tool_names),
    }


def resolve_url(explicit: Optional[str] = None) -> str:
    base = explicit or os.environ.get(UPLOAD_URL_ENV) or DEFAULT_BASE_URL
    base = (base or "").strip().rstrip("/")
    # A caller may hand the same flag to both uploads; if it already names the
    # verdict report, take the base out of it rather than appending a second
    # path. ``report.resolve_url`` accepts the full URL, so this must too.
    for suffix in (AGGREGATE_PATH, "/api/v1/check/report"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base + AGGREGATE_PATH


def upload_aggregate(
    base_url: Optional[str] = None,
    *,
    path: Optional[Path] = None,
    timeout: float = 5.0,
    api_key: Optional[str] = None,
) -> Tuple[Optional[int], Dict[str, Any]]:
    """``(status, body)``; ``status`` is ``None`` when the request never landed.

    Never raises, for the same reason ``record`` does not: a dashboard being
    down must not turn a PASS into a usage error.
    """
    payload = aggregate(path)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = (api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")) or ""
    if key.strip():
        headers[API_KEY_HEADER] = key.strip()
    request = urllib.request.Request(
        resolve_url(base_url),
        data=data,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as error:
        try:
            return error.code, _decode(error.read())
        finally:
            error.close()
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, {"error": str(getattr(error, "reason", error))}


def _decode(raw: bytes) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"raw": raw.decode("utf-8", "replace")[:200]}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def summary_line(status: Optional[int], body: Dict[str, Any]) -> str:
    if status is None:
        return "usage: not sent ({})".format(body.get("error", "unreachable"))
    if status != 200:
        detail = body.get("detail") or body.get("error") or ""
        return "usage: rejected HTTP {} {}".format(status, detail).rstrip()
    total = body.get("runs") or body.get("checks")
    return "usage: aggregate sent{}".format(" ({} checks)".format(total) if total else "")
