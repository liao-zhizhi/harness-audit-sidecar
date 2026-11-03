"""``validate_citation``: the citation tool, judged by the same lattice.

This module is the seam between the witness (:mod:`audit_sidecar.citation_witness`)
and the verdict engine (:mod:`audit_sidecar.verdict`). It exists so that a
bibliographic claim is audited by *the same* four-valued lattice as a file
write, rather than by a bespoke pass/fail that nobody can compare across the
repository.

Mapping a citation onto the lattice
-----------------------------------
``VerdictEngine`` expects three inputs -- ``intent``, ``effect`` and ``check``
-- designed for "the agent was asked to do X, the world shows Y, the agent
reported Z". A citation has the same shape once you name the parties honestly:

===========  ==========================================================
intent       ``True``: the entry claims a record exists and is this work.
effect       What the agency returned: ``True`` (record matches),
             ``False`` (record contradicts), ``None`` (no witness at all).
check        The entry's own assertion that the citation is fine. A `.bib`
             file is a set of such assertions; it does not hedge.
===========  ==========================================================

That yields exactly the verdicts the paper would demand:

* witness agrees              -> PASS
* witness contradicts          -> FAIL   (张冠李戴: right-looking id, wrong paper)
* no witness obtainable        -> INVALID (无见证不可信)
* entry never had an identifier -> INVALID (the ``@misc`` blog-post case)

Note what does *not* happen: an unreachable API never becomes FAIL. The engine
already enforces this -- ``effect is None`` short-circuits to INVALID before any
comparison -- so the distinction survives into every downstream consumer
without the tool re-implementing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..citation_witness import CitationWitness
from ..runner import ExecutionResult
from ..verdict import Verdict, VerdictEngine

__all__ = [
    "TOOL_NAME",
    "TOOL_SPEC",
    "CitationAuditProxy",
    "CitationVerdict",
    "register_citation_tool",
]

TOOL_NAME = "validate_citation"

#: Registration metadata. Kept as data (and shaped like MCP's tool schema) so a
#: server can advertise the tool without importing the witness and touching the
#: network at start-up.
TOOL_SPEC: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Verify that a bibliographic identifier resolves to the claimed work. "
        "Resolves arXiv ids against the arXiv API and DOIs against CrossRef, "
        "then compares the returned title (and optional author) with the claim. "
        "Returns PASS, FAIL (the record contradicts the claim) or INVALID (no "
        "witness could be obtained)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "doi": {
                "type": "string",
                "description": "DOI of the cited work, with or without a resolver prefix.",
            },
            "arxiv_id": {
                "type": "string",
                "description": "arXiv identifier, e.g. '2609.26751'.",
            },
            "expected_title": {
                "type": "string",
                "description": "Title as the manuscript/bibliography claims it.",
            },
            "expected_author": {
                "type": "string",
                "description": "Optional author surname or full name to cross-check.",
            },
        },
        "required": ["expected_title"],
        "anyOf": [{"required": ["doi"]}, {"required": ["arxiv_id"]}],
    },
}


@dataclass(frozen=True)
class CitationVerdict:
    """One audited citation: the verdict, why, and what the agency said."""

    verdict: Verdict
    reason: str
    detail: str = ""
    doi: str = ""
    arxiv_id: str = ""
    expected_title: str = ""
    agency: str = ""
    effect_observed: Optional[bool] = None
    #: Raw witness bookkeeping (cause, actual title, match score) so a report
    #: can explain an INVALID instead of merely counting it.
    witness: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.PASS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "detail": self.detail,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "expected_title": self.expected_title,
            "agency": self.agency,
            "witness": dict(self.witness),
        }


class CitationAuditProxy:
    """Audit citation claims with the shared verdict engine.

    Like :class:`audit_sidecar.mcp_proxy.MCPProxy`, this keeps an audit log and
    advances the immune state per verdict -- a FAIL here is a false completion
    of the same kind the sidecar already counts, so it must escalate the same
    way rather than living in a separate, un-escalated ledger.
    """

    def __init__(
        self,
        witness: Optional[CitationWitness] = None,
        *,
        engine: Optional[VerdictEngine] = None,
        immune_state: Optional[Any] = None,
    ) -> None:
        self._witness = witness if witness is not None else CitationWitness()
        self._engine = engine if engine is not None else VerdictEngine()
        self._immune = immune_state
        self._audit_log: List[CitationVerdict] = []

    # -- queries -------------------------------------------------------

    @property
    def audit_log(self) -> List[CitationVerdict]:
        return list(self._audit_log)

    def summary(self) -> Dict[str, int]:
        counts = {verdict.value: 0 for verdict in Verdict}
        for record in self._audit_log:
            counts[record.verdict.value] += 1
        return counts

    # -- the tool ------------------------------------------------------

    def validate_citation(
        self,
        *,
        doi: Optional[str] = None,
        arxiv_id: Optional[str] = None,
        expected_title: Optional[str] = None,
        expected_author: Optional[str] = None,
        reported_success: bool = True,
    ) -> CitationVerdict:
        """Audit one citation. Never raises for a bad claim or a dead network.

        ``reported_success`` is the entry's own assertion that the citation is
        sound. It defaults to ``True`` because that *is* what a `.bib` file
        asserts by containing the entry at all; an audit script has no separate
        agent report to consult.
        """
        if not isinstance(reported_success, bool):
            raise TypeError(
                "reported_success must be a bool, got {!r}".format(
                    type(reported_success).__name__
                )
            )

        ok, witness_verdict, message = self._witness.verify(
            doi=doi,
            arxiv_id=arxiv_id,
            expected_title=expected_title,
            expected_author=expected_author,
        )

        # The witness's own VERDICT string is diagnostics, not the judgement.
        # Only its boolean reaches the engine; the engine owns the lattice, so
        # there is one place where PASS/FAIL/INVALID/NOT RUN are defined.
        effect: Optional[bool] = None if witness_verdict == "INVALID" else bool(ok)

        verdict, engine_reason = self._engine.explain(
            intent=True,
            effect=effect,
            check=reported_success,
        )

        detail = self._witness.last_detail
        reason = _compose_reason(verdict, engine_reason, message, detail, effect)

        record = CitationVerdict(
            verdict=verdict,
            reason=reason,
            detail=message,
            doi=(doi or "").strip(),
            arxiv_id=(arxiv_id or "").strip(),
            expected_title=(expected_title or "").strip(),
            agency=str(detail.get("agency") or ""),
            effect_observed=effect,
            witness=dict(detail),
        )
        self._audit_log.append(record)
        if self._immune is not None:
            # Same ledger as every other tool: a broken citation is a false
            # completion, and the immune state should feel it.
            self._immune.record_verdict(verdict)
        return record

    # -- MCP surface ---------------------------------------------------

    def intercept_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        agent_reported_success: bool,
    ) -> Dict[str, Any]:
        """``MCPProxy``-shaped entry point, for callers that only want citations."""
        if tool_name != TOOL_NAME:
            raise UnknownCitationTool(
                "no citation specification for {!r}".format(tool_name)
            )
        if not isinstance(args, dict):
            raise TypeError("args must be a dict, got {!r}".format(type(args).__name__))
        record = self.validate_citation(
            doi=args.get("doi"),
            arxiv_id=args.get("arxiv_id"),
            expected_title=args.get("expected_title"),
            reported_success=agent_reported_success,
        )
        return _response(agent_reported_success, record)

    def handler(self, args: Dict[str, Any], reported_success: bool) -> ExecutionResult:
        """Adapt one call to the sidecar's :class:`ExecutionResult` record."""
        record = self.validate_citation(
            doi=args.get("doi"),
            arxiv_id=args.get("arxiv_id"),
            expected_title=args.get("expected_title"),
            expected_author=args.get("expected_author"),
            reported_success=reported_success,
        )
        identity = record.arxiv_id or record.doi or "unidentified entry"
        return ExecutionResult(
            intent="citation {} resolves to {!r}".format(
                identity, record.expected_title
            ),
            effect_observed=record.effect_observed,
            check_observed=reported_success,
            verdict=record.verdict,
            command="cite:{}".format(identity),
            cwd="",
            reason=record.reason,
            check_source="protocol",
        )


class UnknownCitationTool(LookupError):
    """Raised when a call names a tool this module does not define."""


def _compose_reason(
    verdict: Verdict,
    engine_reason: str,
    witness_message: str,
    detail: Dict[str, Any],
    effect: Optional[bool],
) -> str:
    """One line an operator can act on, without losing the engine's wording."""
    if verdict is Verdict.PASS:
        return "{} ({})".format(witness_message, detail.get("agency") or "witness")
    if verdict is Verdict.FAIL:
        if effect is False:
            # A contradicting record is the finding; the engine's generic
            # "false completion" would bury the actual disagreement.
            return witness_message
        # The witness agreed and the *report* denied it: that is a different
        # failure, and the engine's wording is the informative one.
        return "{}: {}".format(engine_reason, witness_message)
    cause = detail.get("cause") or ""
    if cause == "no_identifier":
        return "no witness found: the entry carries no DOI or arXiv id"
    if cause == "no_expected_title":
        return "no witness found: the entry carries no title, so nothing was compared"
    if cause == "unavailable":
        return "{}: agency unreachable (retry later)".format(witness_message)
    if cause == "not_found":
        return "{}: the agency has no record under this identifier".format(
            witness_message
        )
    if cause:
        return "{} ({})".format(witness_message, cause)
    return "{}: {}".format(engine_reason, witness_message)


def _response(reported_success: bool, record: CitationVerdict) -> Dict[str, Any]:
    """The response the caller sees -- the claim, not the witness.

    Deliberately identical in shape to ``MCPProxy._tool_response``: leaking the
    verdict here would let a caller branch on it and route around the gate.
    """
    if reported_success:
        text = "{}: ok".format(TOOL_NAME)
    else:
        text = "{}: failed".format(TOOL_NAME)
    return {
        "status": "ok" if reported_success else "error",
        "isError": not reported_success,
        "content": [{"type": "text", "text": text}],
    }


def register_citation_tool(
    proxy: Any,
    *,
    witness: Optional[CitationWitness] = None,
) -> CitationAuditProxy:
    """Install ``validate_citation`` into an :class:`MCPProxy`.

    The proxy's ordinary tools are shell commands whose witness is the file
    system. A citation has no shell command: its witness is an HTTP response.
    Registering it as a *witness tool* keeps the verdict, the audit log and the
    immune-state accounting on one path instead of opening a second, weaker one
    beside it.
    """
    citations = CitationAuditProxy(
        witness=witness, immune_state=proxy.immune_state
    )
    proxy.register_witness_tool(TOOL_NAME, citations.handler)
    return citations
