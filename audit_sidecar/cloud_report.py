"""Report a witnessed execution to the CEL ledger, with a real digest.

The sidecar is the only party that can see the client's disk, so it is the only
party that can compute a digest of what actually landed. Everything in this
module exists to make that sentence true in code:

* the digest is computed **here**, from the artefact on disk, after the command
  ran -- never from the model's description of it;
* a ``PASS`` carries a digest and a ``trace_id``; an honest failure carries
  neither, because a negative result has nothing to hash and demanding one would
  only teach agents to stop reporting failures;
* a refusal from the cloud (``ledger_written: false``) is logged as
  ``云端因果校验失败`` rather than retried or swallowed.

What a digest proves is bounded, and ``cel_engine.WitnessHashLimits`` on the
server states the same boundary: it fixes a commitment at submission time, so a
lie is falsifiable later. It does not prove the file was produced by the action
that claims it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

__all__ = ["CloudReport", "witness_digest", "report_execution", "new_trace_id"]

_LOG = logging.getLogger(__name__)


def witness_digest(path: Optional[str]) -> Optional[str]:
    """``sha256:<hex>`` of the artefact, or ``None`` if it cannot be read.

    ``None`` is a real answer, not a placeholder: an absent or unreadable file
    means the sidecar has no digest to offer, and inventing one would defeat the
    entire protocol. The caller must therefore handle ``None`` by not claiming a
    pass, which is what :func:`report_execution` does.
    """
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError as problem:
        _LOG.warning("witness %s is unreadable, so no digest can be offered: %s", path, problem)
        return None
    return "sha256:" + digest.hexdigest()


def new_trace_id() -> str:
    """A fresh id per action. Reuse is refused by the cloud as a replay."""
    return "trace-" + uuid.uuid4().hex


@dataclass
class CloudReport:
    """What the ledger said, verbatim enough to argue with.

    ``accepted`` is the only derived field, and it means exactly "the cloud
    booked this". A refused submission is not accepted, not retried, and not
    rounded up.
    """

    posted: bool = False
    status: Optional[int] = None
    verdict: str = ""
    accepted: bool = False
    reason: str = ""
    witness: Optional[bool] = None
    witness_hash: str = ""
    trace_id: str = ""
    body: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def causal_failure(self) -> bool:
        """True when the cloud refused the claim instead of booking it.

        A verdict of INVALID with ``ledger_written: false`` is the cloud saying
        "this was not evidence". That is a different event from a FAIL, which is
        the cloud having judged and rejected the claim on its merits.
        """
        return self.posted and not self.accepted and self.verdict == "INVALID"


def report_execution(
    result: Any,
    *,
    cloud: str,
    agent_id: str,
    intent: str,
    artefact_path: Optional[str] = None,
    api_key: Optional[str] = None,
    trace_id: Optional[str] = None,
    timeout: float = 20.0,
) -> CloudReport:
    """Send one :class:`~audit_sidecar.runner.ExecutionResult` to the ledger.

    ``result`` is the sidecar's own verdict carrier, not the model's claim. The
    digest comes from ``artefact_path`` on disk and is computed only when the
    witness actually observed the effect -- so a submission cannot acquire a
    digest it did not earn.
    """
    import urllib.error
    import urllib.request

    witnessed = result.effect_observed is True
    digest = witness_digest(artefact_path) if witnessed else None
    trace = trace_id or new_trace_id()

    report = CloudReport(
        witness=result.effect_observed,
        witness_hash=digest or "",
        trace_id=trace,
    )

    # A pass with no digest would be refused by the cloud anyway; saying so here
    # names the cause at the side that lost the evidence, rather than reporting a
    # remote rejection nobody can act on.
    if witnessed and digest is None:
        report.reason = (
            "witness saw the effect but no digest could be computed from "
            "{!r}; not claiming a pass".format(artefact_path)
        )
        _LOG.warning("云端因果校验失败：%s", report.reason)
        return report

    payload: Dict[str, Any] = {
        "agent_id": agent_id,
        "intent": intent,
        "check": bool(result.check_observed),
        "witness": result.effect_observed,
        "artefact": os.path.basename(artefact_path) if artefact_path else "",
        "trace_id": trace,
        "cost": 0.0,
    }
    if digest is not None:
        payload["witness_hash"] = digest
    report.body = payload

    request = urllib.request.Request(
        cloud.rstrip("/") + "/api/v1/verify",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # python-urllib's default agent is on Cloudflare's ban list and
            # returns "error code: 1010", which is indistinguishable from the
            # app's own 403 unless the client names itself.
            "User-Agent": "audit-sidecar-cloud-report/0.10.1",
        },
        method="POST",
    )
    if api_key:
        request.add_header("X-API-Key", api_key)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read() or b"{}")
            report.posted = True
            report.status = response.status
            report.verdict = str(body.get("verdict", ""))
            report.accepted = True
            report.body = body
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"detail": raw}
        report.posted = True
        report.status = error.code
        report.verdict = str(body.get("verdict", "INVALID"))
        report.accepted = False
        report.reason = str(body.get("detail", ""))[:400]
        report.body = body
        if report.causal_failure:
            _LOG.error(
                "云端因果校验失败 status=%s verdict=%s: %s",
                report.status,
                report.verdict,
                report.reason,
            )
    except Exception as problem:  # noqa: BLE001 - reported, never swallowed
        report.posted = False
        report.error = "{}: {}".format(type(problem).__name__, problem)
        _LOG.error("云端因果校验失败（传输层）: %s", report.error)

    return report
