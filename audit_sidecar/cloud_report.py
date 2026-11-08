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

Two record tables live on the server, and this module is the client for exactly
one and a half of them:

* ``MutationEvent`` (D4). **Wired.** A verdict of FAIL with a success claim and
  a contradicting witness is a caught forgery, and :func:`report_execution` posts
  it to ``/api/v1/mutation`` after the verify is booked. ``detected`` is always
  ``True`` because that is the only value the sidecar can honestly produce: a
  mutation it did not catch is one it cannot report, which is precisely why the
  server's ``mu`` is a lower bound.
* ``PropagationEdge`` (D6). **Not wired, and not wireable from here.** An edge is
  "agent A adopted agent B's premise"; this package has no agent-to-agent channel
  at all. ``PROPAGATION_REPORTING`` is ``False`` and
  :func:`propagation_capability` says what is missing. A fabricated edge would
  lower ``R0`` (``rho`` is monotone in the entries), so inventing one would read
  as safety.
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

__all__ = [
    "CloudReport",
    "MutationReport",
    "PROPAGATION_REPORTING",
    "mutation_kind",
    "propagation_capability",
    "report_execution",
    "report_mutation",
    "witness_digest",
    "new_trace_id",
]

_LOG = logging.getLogger(__name__)

#: D6 propagation edges are **not** reported by the sidecar. This is a fact about
#: the code, not a roadmap item, and it is a constant rather than prose so a
#: caller can branch on it instead of trusting a README.
#:
#: Why it cannot be done from here: a ``PropagationEdge`` is the claim "agent A
#: adopted agent B's premise at cycle k". The sidecar sees one command execution
#: in one working directory -- ``runner.SafeRunner.run_shell`` -- and its only
#: identifiers are the artefact path and the digest it computes from it. There is
#: no agent-to-agent channel anywhere in the package: ``agent_id`` appears in
#: exactly one place (the payload built here), and nothing tracks which agent's
#: output was read as an input by another. A callback with no data behind it
#: would have to invent ``source_agent``/``target_agent``, and invented edges
#: lower ``R0`` by the monotonicity argument in ``cel_engine.propagation_graph``
#: -- i.e. the fabrication would read as safety.
#:
#: What would be needed: a shared context layer (a store that records reads of
#: another agent's artefacts) or a protocol field where an agent names its
#: premises. Neither exists in this package today. Until then the honest
#: statement is that the propagation table has no writer, so ``beta`` stays
#: unmeasured and ``r0_source`` stays ``"supplied-matrix"``.
PROPAGATION_REPORTING = False


def propagation_capability() -> Dict[str, Any]:
    """Machine-readable statement that the D6 callback is absent.

    Returned as a record rather than a bool so a status page or a report can
    render *why*, and so "not implemented" cannot be mistaken for "implemented
    and silently returning zero".
    """
    return {
        "supported": PROPAGATION_REPORTING,
        "table": "PropagationEdge",
        "reason": (
            "the sidecar observes one execution in one working directory and has "
            "no agent-to-agent channel; nothing records one agent reading "
            "another's artefact, so source_agent/target_agent would have to be "
            "invented"
        ),
        "consequence": (
            "beta is unmeasured, so r0_source stays 'supplied-matrix' and "
            "r0_value stays null"
        ),
        "what_would_be_needed": (
            "a shared context store that records cross-agent reads, or a protocol "
            "field where an agent names the premises it adopted"
        ),
    }


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

    # -- D4 mutation record ------------------------------------------------
    # Filled in by ``_record_mutation_if_any`` when this execution was a
    # *detected* forgery attempt. Kept on the same object rather than returned
    # separately because the two reports describe one event: the execution, and
    # the escape attempt the sidecar caught inside it. Splitting them would let a
    # caller report the execution and quietly drop the mutation.
    mutation_type: Optional[str] = None
    mutation_recorded: bool = False
    mutation_status: Optional[int] = None
    mutation_error: str = ""
    mutation_body: Dict[str, Any] = field(default_factory=dict)

    @property
    def causal_failure(self) -> bool:
        """True when the cloud refused the claim instead of booking it.

        A verdict of INVALID with ``ledger_written: false`` is the cloud saying
        "this was not evidence". That is a different event from a FAIL, which is
        the cloud having judged and rejected the claim on its merits.
        """
        return self.posted and not self.accepted and self.verdict == "INVALID"


#: Mutation vocabulary, mirrored from ``cel_engine.MUTATION_TYPES``. Duplicated
#: rather than imported because the sidecar must install without the server; a
#: hard dependency on the cloud package would make the client unusable alone.
#: ``tests/test_mutation_callback.py`` asserts the two lists agree, so the copy
#: cannot drift in silence.
MUTATION_TYPES = (
    "prompt_tamper",
    "code_injection",
    "witness_forgery",
    "tool_substitution",
    "other",
)


def mutation_kind(result: Any, *, override: Optional[str] = None) -> Optional[str]:
    """Which mutation, if any, this execution's verdict represents.

    Returns ``None`` when the run was not an attempted *evasion*, and that
    exclusion is the substance of the function:

    * ``FAIL`` with ``check_observed=True`` and ``effect_observed=False`` is a
      **false completion** -- the agent claimed the work landed and the witness
      contradicts it. That is the D4 mutant: it tried to pass without doing.
      Reported as ``witness_forgery``.
    * ``FAIL`` with ``check_observed=False`` is an **under-claim**: the effect
      landed and the agent said it failed. Dishonest, but not evasion -- it
      understates its own work, which cannot get an unfounded premise adopted.
      Counting it would make ``mu`` a measure of reporting mistakes, not of
      mutations.
    * A tool layer reporting success over a non-zero exit is a defect in the
      *harness*, not an act by the agent. Attributing it to the agent would
      charge the tooling's fault to the agent's mutation rate, so it is not
      counted here. ``override`` exists for a caller that has evidence this one
      really was the agent (e.g. a swapped tool), and naming it is the point.
    * Anything not ``FAIL`` (``PASS``/``INVALID``/``NOT RUN``) returns ``None``.
      ``INVALID`` especially: nobody looked, so nobody saw a mutation.

    ``override`` is validated against :data:`MUTATION_TYPES` -- an unlisted
    string would be recorded by the cloud and then parsed as ``other`` by every
    reader, which is a quieter lie than raising.
    """
    if override is not None:
        if override not in MUTATION_TYPES:
            raise ValueError(
                "mutation override {!r} is not in MUTATION_TYPES {}; a label the "
                "vocabulary cannot parse is recorded as 'other' downstream, so "
                "refusing here is the less lossy failure".format(
                    override, MUTATION_TYPES
                )
            )
        return override

    verdict = getattr(result, "verdict", None)
    if str(verdict) != "FAIL":
        return None
    if result.check_observed is True and result.effect_observed is False:
        return "witness_forgery"
    return None


@dataclass
class MutationReport:
    """What the cloud said about one mutation record.

    ``recorded`` means the cloud stored the event, not that it charged anyone.
    The charge happened on the ``/verify`` call that caught it -- see
    ``MutationEvent`` on the server, which says the same thing from the other
    side: record is not charge, and a record that also booked the charge would
    double-charge on replay.
    """

    recorded: bool = False
    status: Optional[int] = None
    mu: Optional[float] = None
    censoring: str = ""
    reason: str = ""
    error: str = ""
    body: Dict[str, Any] = field(default_factory=dict)


def report_mutation(
    *,
    cloud: str,
    agent_id: str,
    mutation_type: str,
    detected: bool = True,
    penalty: float = 0.0,
    cycle: Optional[int] = None,
    api_key: Optional[str] = None,
    timeout: float = 20.0,
) -> MutationReport:
    """Record one mutation event with the ledger, for D4's ``mu``.

    Called by :func:`report_execution` after a caught forgery, and available on
    its own for detection paths that do not go through an execution (a prompt
    diff, a rule hit).

    ``detected`` is ``True`` for everything the sidecar writes, and that is not
    a default -- it is the only value the sidecar can honestly produce. A
    mutation it did not catch is, by definition, one it cannot report, and the
    server's ``mu`` is defined as intercepted/executions for exactly this
    reason. Passing ``False`` here would require forensic evidence that the
    attempt happened and was missed; the sidecar has no such witness, so the
    argument exists for a caller that does.
    """
    import urllib.error
    import urllib.request

    report = MutationReport()
    if mutation_type not in MUTATION_TYPES:
        report.reason = (
            "mutation_type {!r} is not in MUTATION_TYPES; refused at the side "
            "rather than recorded as 'other'".format(mutation_type)
        )
        _LOG.warning("变异事件未上报：%s", report.reason)
        return report

    payload: Dict[str, Any] = {
        "agent_id": agent_id,
        "mutation_type": mutation_type,
        "detected": bool(detected),
        "penalty": float(penalty),
    }
    if cycle is not None:
        # Omitted rather than sent as null: the server's default is the agent's
        # own booked-execution count, which is the correct cycle for an event
        # reported right after that execution. Sending null would defeat it.
        payload["cycle"] = int(cycle)

    request = urllib.request.Request(
        cloud.rstrip("/") + "/api/v1/mutation",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "audit-sidecar-cloud-report/0.10.1",
        },
        method="POST",
    )
    if api_key:
        request.add_header("X-API-Key", api_key)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read() or b"{}")
            report.recorded = bool(body.get("recorded", False))
            report.status = response.status
            report.mu = body.get("mu")
            report.censoring = str(body.get("censoring", ""))
            report.body = body
    except urllib.error.HTTPError as error:
        # ``HTTPError`` is a response object, not just an exception: leaving it
        # open leaks the socket and prints a ResourceWarning at GC time, which
        # looks like flakiness in a test run and is a descriptor leak in a daemon.
        try:
            raw = error.read().decode("utf-8", "replace")
        finally:
            error.close()
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"detail": raw}
        report.status = error.code
        report.reason = str(body.get("detail", raw))[:400]
        report.body = body
        _LOG.error("变异事件上报被拒 status=%s: %s", report.status, report.reason)
    except Exception as problem:  # noqa: BLE001 - reported, never swallowed
        report.error = "{}: {}".format(type(problem).__name__, problem)
        _LOG.error("变异事件上报失败（传输层）: %s", report.error)

    return report


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
        # ``HTTPError`` is a response object, not just an exception: leaving it
        # open leaks the socket and prints a ResourceWarning at GC time, which
        # looks like flakiness in a test run and is a descriptor leak in a daemon.
        try:
            raw = error.read().decode("utf-8", "replace")
        finally:
            error.close()
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

    _record_mutation_if_any(
        report,
        result,
        cloud=cloud,
        agent_id=agent_id,
        api_key=api_key,
        timeout=timeout,
    )
    return report


def _record_mutation_if_any(
    report: CloudReport,
    result: Any,
    *,
    cloud: str,
    agent_id: str,
    api_key: Optional[str],
    timeout: float,
) -> None:
    """Attach the D4 mutation record to ``report``, if this run was a mutation.

    Two conditions gate the write, and the second is the one that is easy to get
    wrong:

    1. ``mutation_kind(result)`` says the verdict was an attempted evasion.
    2. **The verify call was actually booked** (``report.accepted``). If the
       claim was refused at the door -- a 423, a 409 replay, a 422 -- then no
       execution exists in the ledger. Writing a mutation against it would put
       one in ``mu``'s numerator with nothing in the denominator, so ``mu`` would
       rise every time an agent was turned away. That is the inversion
       ``CensoringLimits`` warns about, arriving through the front door.

    Order matters for the same arithmetic: this runs *after* the verify, so the
    server's execution count already includes the run being reported and the
    event's default cycle is the one that just happened.
    """
    kind = mutation_kind(result)
    if kind is None:
        return
    report.mutation_type = kind
    if not report.accepted:
        report.mutation_error = (
            "verify was not booked (status={}, verdict={}); no execution to "
            "divide by, so no mutation recorded".format(report.status, report.verdict)
        )
        return

    charged = 0.0
    if isinstance(report.body, dict):
        try:
            charged = float(report.body.get("penalty") or 0.0)
        except (TypeError, ValueError):
            charged = 0.0

    recorded = report_mutation(
        cloud=cloud,
        agent_id=agent_id,
        mutation_type=kind,
        detected=True,
        penalty=charged,
        api_key=api_key,
        timeout=timeout,
    )
    report.mutation_recorded = recorded.recorded
    report.mutation_status = recorded.status
    report.mutation_error = recorded.error or recorded.reason
    report.mutation_body = recorded.body
