"""Opt-in upload of a verdict to a dashboard's display ledger.

This is a report, not a witness. The sidecar already decided; the upload moves
the *decision* to a screen someone else is watching. Nothing about the verdict
changes if the upload fails, which is why a failed POST never alters the exit
code -- a display that is down must not turn a FAIL into a pass, or a PASS into
a usage error.

What leaves the machine, exactly: ``verdict``, ``reason``, ``check``. No path,
no command text, no digest, no agent id. The reason and the declaration are
redacted first, because both can contain the thing being withheld: a file
reason reads ``/home/me/app/src/main.py exists, but no recording``, and the
declaration carries the same path. Uploading either verbatim would make "no
paths leave the machine" false in the first request.

The redaction is deliberately blunt -- a positive list of three fields and a
replacement for anything that looks like a path. A cleverer scheme (hashing
paths, keeping extensions) would preserve more display value and would also be
a claim that the hash is not reversible over the small set of plausible
filenames. Blunt is the honest choice here.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .check import CheckOutcome

#: Where ``--upload`` posts by default: the local dashboard started from
#: ``product_mvp/``. A deployed instance is a different base and therefore an
#: explicit choice, not something this default silently guesses.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
REPORT_PATH = "/api/v1/check/report"

#: Overrides the base URL. Opting in is the flag; the address is environment.
UPLOAD_URL_ENV = "HARNESS_AUDIT_UPLOAD_URL"

#: Absolute POSIX paths, and Windows drive paths. Deliberately not matching
#: relative paths: ``src/main.py`` is the shape of the declaration, not the
#: location of the checkout, and it is what makes the dashboard row legible.
_ABS_PATH = re.compile(r"(?:^|(?<=[\s(\[\"']))(?:/[^\s,;:)\]\"']+|[A-Za-z]:\\[^\s,;)\]\"']+)")

#: The three fields the display ledger stores. A test asserts the payload has
#: exactly these keys, so adding a field later is a visible decision (and a
#: privacy review) rather than a convenience.
REPORT_FIELDS: Tuple[str, ...] = ("verdict", "reason", "check")


def _redact_absolute_paths(text: str) -> str:
    return _ABS_PATH.sub("<path>", text or "")


def redact_declaration(declaration: str) -> str:
    """``file /home/me/src/main.py`` -> ``file <redacted>``.

    The declaration *kind* is kept: "a file claim was blocked" is the whole
    useful content of a dashboard row. The argument is dropped because for
    ``file`` it is a path and for ``command`` it can contain anything.
    """
    text = (declaration or "").strip()
    if not text:
        return "(none)"
    kind, separator, argument = text.partition(" ")
    if kind in ("file", "command") and separator and argument.strip():
        return "{} <redacted>".format(kind)
    return _redact_absolute_paths(text)


def redact_reason(reason: str, declaration: str) -> str:
    """Drop the declared argument, then any remaining absolute path.

    Two passes because the reason repeats the argument verbatim (``{path}
    exists, but no recording``), so the exact string is available and cheaper
    to trust than a pattern. The pattern then catches paths that appear for
    other reasons, such as the ``--cwd`` directory.
    """
    text = reason or ""
    _, _, argument = (declaration or "").strip().partition(" ")
    argument = argument.strip()
    if argument and kind_is_private((declaration or "").strip()):
        text = text.replace(argument, "<redacted>")
    return _redact_absolute_paths(text)


def kind_is_private(declaration: str) -> bool:
    """Whether the declaration's argument is private (file path or command)."""
    kind, _, argument = (declaration or "").strip().partition(" ")
    return kind in ("file", "command") and bool(argument.strip())


def build_payload(outcome: CheckOutcome) -> Dict[str, str]:
    """The exact three fields, redacted. No ``**outcome.to_dict()`` on purpose.

    The trace carries ``evidence`` with a path and a digest, ``declaration``
    with the path itself, and a witness type. Spreading it would upload all of
    that, and a test that only checked ``verdict`` would still pass.
    """
    return {
        "verdict": outcome.verdict.value,
        "reason": redact_reason(outcome.reason, outcome.declaration),
        "check": redact_declaration(outcome.declaration),
    }


def resolve_url(explicit: Optional[str] = None) -> str:
    """Full report URL from a flag, the environment, or the local default."""
    base = explicit or os.environ.get(UPLOAD_URL_ENV) or DEFAULT_BASE_URL
    base = base.strip()
    if base.endswith(REPORT_PATH):
        return base
    return base.rstrip("/") + REPORT_PATH


def upload(
    outcome: CheckOutcome,
    url: Optional[str] = None,
    *,
    timeout: float = 5.0,
) -> Tuple[Optional[int], Dict[str, Any]]:
    """``(status, body)``. ``status`` is ``None`` when the request never landed.

    Never raises: a dashboard being unreachable is not a verdict, and an
    auditor that crashes because a display is down is an auditor that cannot be
    used offline. The caller prints the outcome and keeps its exit code.
    """
    payload = build_payload(outcome)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        resolve_url(url),
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _decode(response.read())
            return response.status, body
    except urllib.error.HTTPError as error:
        return error.code, _decode(error.read())
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, {"error": str(getattr(error, "reason", error))}


def _decode(raw: bytes) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"raw": raw.decode("utf-8", "replace")[:500]}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def summary_line(status: Optional[int], body: Dict[str, Any]) -> str:
    """One line for stderr. Always says whether anything was recorded."""
    if status is None:
        return "upload: not sent ({})".format(body.get("error", "unreachable"))
    if status != 200:
        detail = body.get("detail") or body.get("error") or ""
        return "upload: rejected HTTP {} {}".format(status, detail).rstrip()
    if body.get("recorded"):
        return "upload: recorded ({})".format(body.get("verdict", "?"))
    return "upload: accepted but not recorded -- {}".format(body.get("reason", "not an interception"))
