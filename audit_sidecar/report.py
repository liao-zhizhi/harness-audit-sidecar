"""Opt-in upload of a verdict to a dashboard's display ledger.

This is a report, not a witness. The sidecar already decided; the upload moves
the *decision* to a screen someone else is watching. Nothing about the verdict
changes if the upload fails, which is why a failed POST never alters the exit
code -- a display that is down must not turn a FAIL into a pass, or a PASS into
a usage error.

The default destination is the public dashboard. That is a deliberate change
from ``127.0.0.1``: a default that only works on the author's machine is a
default that fails for everyone who installs the package. Because it is somebody
else's host, two things follow, and both live in this module:

* **the endpoint is probed before anything is posted.** A 404 today would
  otherwise look like a successful upload (the response body is displayed, not
  parsed), and "sent" would be printed for a host that never received it.
* **the first POST announces the host** on stderr, so "it uploaded somewhere" is
  never something the operator has to discover from the source.

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

#: Where ``--upload`` posts by default: the deployed product dashboard. Local
#: development is the *explicit* case (``--upload-url http://127.0.0.1:8000``)
#: rather than the default, because a default that only works on the author's
#: machine is a default that fails for everyone who installs the package.
#:
#: This is the product host, not the demo host, and the distinction is load
#: bearing: the demo's tunnel points at a build that predates
#: /api/v1/check/report, so a default aimed there would answer every first
#: upload with "does not expose the route". See DEPLOY_PRODUCT.md in the
#: product repository for the deployment that makes this name true.
PUBLIC_DASHBOARD = "https://audit.silicon-probe.com"
DEFAULT_BASE_URL = PUBLIC_DASHBOARD
REPORT_PATH = "/api/v1/check/report"

#: Overrides the base URL. Opting in is the flag; the address is environment.
UPLOAD_URL_ENV = "HARNESS_AUDIT_UPLOAD_URL"

#: The dashboard's write gate, same header ``cloud_report`` uses. Optional
#: everywhere: the anonymous tier is the default, and a key is what lifts it.
API_KEY_ENV = "HARNESS_AUDIT_API_KEY"
API_KEY_HEADER = "X-API-Key"

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


def resolve_base(explicit: Optional[str] = None) -> str:
    """The base URL this run will talk to, with no path on the end.

    Normalised once, here, so the announcement, the probe and the POST cannot
    disagree about which host they are addressing.
    """
    base = explicit or os.environ.get(UPLOAD_URL_ENV) or DEFAULT_BASE_URL
    base = (base or "").strip().rstrip("/")
    if base.endswith(REPORT_PATH):
        base = base[: -len(REPORT_PATH)].rstrip("/")
    return base


def resolve_url(explicit: Optional[str] = None) -> str:
    """Full report URL from a flag, the environment, or the public default."""
    return resolve_base(explicit) + REPORT_PATH


def resolve_api_key(explicit: Optional[str] = None) -> Optional[str]:
    """The dashboard key, if one was given. Empty means "anonymous tier"."""
    key = (explicit if explicit is not None else os.environ.get(API_KEY_ENV, "")) or ""
    key = key.strip()
    return key or None


# ---------------------------------------------------------------------------
# the probe: is there anything listening on the other end?
# ---------------------------------------------------------------------------

#: One probe and one announcement per process. Two POSTs go out per ``--upload``
#: (verdict + usage aggregate); probing twice would double the latency to learn
#: something that cannot have changed in the same second.
_PROBED: Dict[str, "Probe"] = {}
_ANNOUNCED: set = set()


class Probe:
    """What ``OPTIONS`` said. ``ok`` means "a request to this path can land".

    ``status`` is ``None`` when the request never got an answer. ``detail`` is
    prose for the operator, not for a machine.
    """

    __slots__ = ("ok", "status", "detail")

    def __init__(self, ok: bool, status: Optional[int], detail: str) -> None:
        self.ok = ok
        self.status = status
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Probe(ok={}, status={}, detail={!r})".format(
            self.ok, self.status, self.detail
        )


def probe(
    base_url: Optional[str] = None,
    *,
    timeout: float = 5.0,
    cache: bool = True,
) -> Probe:
    """Ask the endpoint whether it exists before pretending to post to it.

    ``OPTIONS`` on the report path, and the status is read *as a router reads
    it*, not as a success flag:

    * ``200``/``204`` -- the path answered; fine.
    * ``405`` -- **this is the healthy answer**, not a failure. The route exists
      and only accepts POST; a deployment that returned 200 to OPTIONS would be
      the unusual one. Rejecting 405 here would refuse every correct FastAPI
      instance -- verified against ``product_mvp`` itself, which answers
      ``405`` with ``Allow: POST``.
    * ``401``/``403`` -- the route exists and is gated; the POST will say so
      properly.
    * ``404``/``410`` -- nothing is deployed at that path, which is the case
      worth stopping for.
    * no answer -- DNS, refused connection, timeout, TLS. Also worth stopping
      for, and a different sentence.

    ``cache=False`` bypasses the per-process memo, for callers that need a
    fresh reading (tests, and a retry loop if one is ever added).
    """
    base = resolve_base(base_url)
    if cache and base in _PROBED:
        return _PROBED[base]

    request = urllib.request.Request(
        base + REPORT_PATH,
        method="OPTIONS",
        headers={"User-Agent": "harness-audit-sidecar"},
    )
    result = _ask(request, base, timeout)
    if cache:
        _PROBED[base] = result
    return result


def _ask(request: urllib.request.Request, base: str, timeout: float) -> Probe:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Probe(True, response.status, "answered")
    except urllib.error.HTTPError as error:
        status = error.code
        allow = ""
        try:
            allow = (error.headers or {}).get("Allow", "") or ""
        finally:
            # An HTTPError is a response object; leaving it open leaks the
            # socket and surfaces later as a ResourceWarning that looks like
            # flakiness rather than the descriptor leak it is.
            error.close()
        if status in (404, 410):
            return Probe(False, status, "no route")
        if status == 405:
            # See the docstring: 405 is what a POST-only route answers, and
            # `Allow` is recorded only so a *wrong* route would be visible.
            return Probe(True, status, "route exists (Allow: {})".format(allow or "POST"))
        if status in (401, 403):
            return Probe(True, status, "route exists, gated")
        if status >= 500:
            # The host is there and the app is broken. Saying "not deployed"
            # would send the operator to the wrong fix.
            return Probe(False, status, "server error")
        return Probe(False, status, "unexpected status")
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        return Probe(False, None, "unreachable: {}".format(reason))


def reset_probe_cache() -> None:
    """Forget what this process learned. Tests, and nothing else."""
    _PROBED.clear()
    _ANNOUNCED.clear()


def announcement(base_url: Optional[str] = None) -> Optional[str]:
    """The one line printed before the first POST, or ``None`` if already said.

    Naming the host is the point: ``--upload`` now defaults to a machine someone
    else runs, and "the tool sent something somewhere" is exactly the kind of
    thing that should not be discovered by reading the source.
    """
    base = resolve_base(base_url)
    if base in _ANNOUNCED:
        return None
    _ANNOUNCED.add(base)
    if is_public(base):
        return "upload: posting to public dashboard {} (redacted payload)".format(base)
    return "upload: posting to {} (redacted payload)".format(base)


def is_public(base_url: Optional[str] = None) -> bool:
    base = resolve_base(base_url)
    return base == PUBLIC_DASHBOARD


def failure_message(base_url: Optional[str], result: Probe) -> str:
    """Two lines: what is wrong, and the two ways out. No docs link.

    Written as a paste-able next step rather than "see the README": the operator
    is at a terminal at the moment this prints, and the fix is one flag.
    """
    base = resolve_base(base_url)
    if result.status is None:
        first = "upload failed: cannot reach {}. ({})".format(base, result.detail)
        second = (
            "Either start a dashboard there, or pass --upload-url <your-instance>."
        )
    elif result.status in (404, 410):
        first = "upload failed: {} does not expose {}.".format(base, REPORT_PATH)
        second = (
            "Either deploy the latest product_mvp to that host, or pass "
            "--upload-url <your-instance>."
        )
    else:
        first = "upload failed: {} answered HTTP {}. ({})".format(
            base, result.status, result.detail
        )
        second = "Pass --upload-url <your-instance> once that host is healthy."
    return "{}\n{}".format(first, second)


def upload(
    outcome: CheckOutcome,
    url: Optional[str] = None,
    *,
    timeout: float = 5.0,
    api_key: Optional[str] = None,
) -> Tuple[Optional[int], Dict[str, Any]]:
    """``(status, body)``. ``status`` is ``None`` when the request never landed.

    Never raises: a dashboard being unreachable is not a verdict, and an
    auditor that crashes because a display is down is an auditor that cannot be
    used offline. The caller prints the outcome and keeps its exit code.

    ``api_key`` is optional and only lifts the anonymous tier; without it the
    request is anonymous and the response's ``X-Trial-Remaining`` is the honest
    account of the budget it spent.
    """
    payload = build_payload(outcome)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "harness-audit-sidecar"}
    key = resolve_api_key(api_key)
    if key:
        headers[API_KEY_HEADER] = key
    request = urllib.request.Request(
        resolve_url(url),
        data=data,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _decode(response.read())
            return response.status, body
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
