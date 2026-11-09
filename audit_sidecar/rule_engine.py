"""Rule-driven judging: the judgment base gets the last word.

``VerdictEngine`` compares three observations (intent, effect, claim) and is
complete for that comparison. What it cannot express is a *policy*: "for this
tool, a file witness is not enough -- I want the git change set too". That kind
of requirement is not a fact about the world, it is a fact about what this
deployment is willing to accept, and it belongs in the judgment base.

``RuleEngine`` therefore wraps the lattice rather than replacing it:

    fallback verdict  ->  apply rules  ->  final verdict

The composition is **monotone**: rules may tighten a verdict and may never
loosen one. A rule base that could turn a FAIL into a PASS would be a way to
argue with the witness, which is exactly the failure the audit exists to
detect. The precedence is:

    1. NOT RUN  -- nothing executed, so no rule can have been violated.
    2. FAIL     -- the witness contradicts the record. A missing witness
                   cannot make that contradiction go away.
    3. INVALID  -- a rule demands a witness that was not collected. Absence
                   of the demanded evidence is not evidence of success.
    4. PASS     -- the fallback agreed and every rule is satisfied.

Every tightening carries the ``rule_id`` that caused it, so the trace can be
read without re-loading the policy that produced it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import AbstractSet, Any, Dict, FrozenSet, List, Optional, Tuple

from .judgment_base import JudgmentBase, Rule
from .verdict import Verdict, VerdictEngine
from .witness_provenance import cap_verdict

__all__ = ["RuleDecision", "RuleEngine"]

_LOG = logging.getLogger(__name__)

# Witness types the runner reports. Kept in sync with judgment_base's
# allowlist; a rule naming anything else is rejected at load time.
WITNESS_FILE = "file"
WITNESS_GIT_DIFF = "git_diff"
WITNESS_AGENT_REPORT = "agent_report"

_NO_WITNESSES: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class RuleDecision:
    """The verdict plus the rule provenance needed to explain it."""

    verdict: Verdict
    reason: str
    # The rule that decided the verdict, or None when the fallback decided it.
    rule_id: Optional[str] = None
    # Every rule that applied to this tool, whether or not it fired.
    matched_rule_ids: Tuple[str, ...] = ()
    violated_rule_ids: Tuple[str, ...] = ()

    @property
    def tightened(self) -> bool:
        """True when a rule, rather than the lattice, produced the verdict."""
        return self.rule_id is not None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "matched_rule_ids": list(self.matched_rule_ids),
            "violated_rule_ids": list(self.violated_rule_ids),
        }


class RuleEngine:
    """Judge a call against an external judgment base, falling back to the lattice."""

    def __init__(
        self,
        judgment_base: JudgmentBase,
        fallback: Optional[VerdictEngine] = None,
    ) -> None:
        if not isinstance(judgment_base, JudgmentBase):
            raise TypeError(
                "judgment_base must be a JudgmentBase, got {!r}".format(
                    type(judgment_base).__name__
                )
            )
        self._base = judgment_base
        self._fallback = fallback if fallback is not None else VerdictEngine()

    # -- public API ---------------------------------------------------

    @property
    def judgment_base(self) -> JudgmentBase:
        return self._base

    def rules_for_tool(self, tool_name: str) -> List[Dict[str, Any]]:
        return self._base.get_rules_for_tool(tool_name)

    def evaluate(
        self,
        tool_name: str,
        intent: Any = None,
        effect: Any = None,
        check: Any = None,
        *,
        witnesses: AbstractSet[str] = _NO_WITNESSES,
        producer: Optional[str] = None,
        topology: Optional[str] = None,
        program_fact: Optional[bool] = None,
        world_fact: Optional[bool] = None,
    ) -> Verdict:
        """Return just the verdict. Signature kept positional for callers."""
        return self.judge(
            tool_name,
            intent=intent,
            effect=effect,
            check=check,
            witnesses=witnesses,
            producer=producer,
            topology=topology,
            program_fact=program_fact,
            world_fact=world_fact,
        ).verdict

    def explain(
        self,
        *,
        intent: Any = None,
        effect: Any = None,
        check: Any = None,
        tool_name: Optional[str] = None,
        witnesses: AbstractSet[str] = _NO_WITNESSES,
        producer: Optional[str] = None,
        topology: Optional[str] = None,
        program_fact: Optional[bool] = None,
        world_fact: Optional[bool] = None,
    ) -> Tuple[Verdict, str]:
        """``VerdictEngine``-shaped entry point (verdict, reason).

        ``tool_name=None`` means "no policy context", so the call is judged by
        the plain lattice. That keeps this a drop-in replacement for
        ``VerdictEngine`` wherever a tool name is unavailable. The provenance
        caps live on the rule-aware path only: with no tool name there is no
        place to declare them, and silently capping a bare lattice call would
        make ``explain`` disagree with ``VerdictEngine.explain``.
        """
        if tool_name is None:
            return self._fallback.explain(intent=intent, effect=effect, check=check)
        decision = self.judge(
            tool_name,
            intent=intent,
            effect=effect,
            check=check,
            witnesses=witnesses,
            producer=producer,
            topology=topology,
            program_fact=program_fact,
            world_fact=world_fact,
        )
        return decision.verdict, decision.reason

    def explain_for_tool(
        self,
        tool_name: str,
        *,
        intent: Any = None,
        effect: Any = None,
        check: Any = None,
        witnesses: AbstractSet[str] = _NO_WITNESSES,
        producer: Optional[str] = None,
        topology: Optional[str] = None,
        program_fact: Optional[bool] = None,
        world_fact: Optional[bool] = None,
    ) -> RuleDecision:
        """Capability marker used by ``SafeRunner`` to route rule-aware judging.

        The runner looks for this method by name. ``VerdictEngine`` does not
        have it, so a plain engine keeps working unchanged.
        """
        return self.judge(
            tool_name,
            intent=intent,
            effect=effect,
            check=check,
            witnesses=witnesses,
            producer=producer,
            topology=topology,
            program_fact=program_fact,
            world_fact=world_fact,
        )

    def judge(
        self,
        tool_name: str,
        *,
        intent: Any = None,
        effect: Any = None,
        check: Any = None,
        witnesses: AbstractSet[str] = _NO_WITNESSES,
        producer: Optional[str] = None,
        topology: Optional[str] = None,
        program_fact: Optional[bool] = None,
        world_fact: Optional[bool] = None,
    ) -> RuleDecision:
        """Judge one call, then apply the witness-provenance caps.

        The caps are applied last and only ever turn a PASS into INVALID, so the
        monotonicity documented above still holds: a rule may tighten, and so
        may a provenance declaration, but neither can loosen.
        """
        decision = self._judge(
            tool_name,
            intent=intent,
            effect=effect,
            check=check,
            witnesses=witnesses,
        )
        capped, reason = cap_verdict(
            decision.verdict,
            decision.reason,
            producer=producer,
            topology=topology,
            program_fact=program_fact,
            world_fact=world_fact,
        )
        if capped is decision.verdict and reason == decision.reason:
            return decision
        return replace(decision, verdict=capped, reason=reason)

    def _judge(
        self,
        tool_name: str,
        *,
        intent: Any = None,
        effect: Any = None,
        check: Any = None,
        witnesses: AbstractSet[str] = _NO_WITNESSES,
    ) -> RuleDecision:
        """The uncapped judgment: fallback lattice, then policy rules."""
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name must be a non-empty string")

        base_verdict, base_reason = self._fallback.explain(
            intent=intent, effect=effect, check=check
        )

        raw_rules = self._base.get_rules_for_tool(tool_name)
        if not raw_rules:
            return RuleDecision(
                verdict=base_verdict,
                reason=base_reason,
                rule_id=None,
                matched_rule_ids=(),
            )

        rules = [self._rule_from_dict(entry) for entry in raw_rules]
        matched_ids = tuple(rule.rule_id for rule in rules)

        # Nothing ran, so nothing can be violated. Saying otherwise would let a
        # policy manufacture a failure out of a scenario that never executed.
        if base_verdict is Verdict.NOT_RUN:
            return RuleDecision(
                verdict=Verdict.NOT_RUN,
                reason="{} (judgment base not applied: nothing ran)".format(base_reason),
                rule_id=None,
                matched_rule_ids=matched_ids,
            )

        violated = tuple(rule for rule in rules if self._expectation_unmet(rule, effect))
        unsatisfied = tuple(
            rule
            for rule in rules
            if rule.require_witness and rule.witness_type not in witnesses
        )

        # 2. A violated expectation is the strongest finding: the witness is
        # present and it says the intended effect did not land.
        if violated:
            first = violated[0]
            return RuleDecision(
                verdict=Verdict.FAIL,
                reason=(
                    "rule {!r} violated: expectation {!r} not met ({})".format(
                        first.rule_id, first.expectation, base_reason
                    )
                ),
                rule_id=first.rule_id,
                matched_rule_ids=matched_ids,
                violated_rule_ids=tuple(rule.rule_id for rule in violated),
            )

        # A FAIL from the lattice is already a real contradiction. A missing
        # witness may be noted, but it cannot soften the verdict.
        if base_verdict is Verdict.FAIL:
            extra = ""
            if unsatisfied:
                extra = " (also unmet: rule {} requires a {} witness)".format(
                    unsatisfied[0].rule_id, unsatisfied[0].witness_type
                )
            return RuleDecision(
                verdict=Verdict.FAIL,
                reason=base_reason + extra,
                rule_id=None,
                matched_rule_ids=matched_ids,
                violated_rule_ids=tuple(rule.rule_id for rule in unsatisfied),
            )

        # 3. The policy asked for evidence the run did not produce.
        if unsatisfied:
            first = unsatisfied[0]
            return RuleDecision(
                verdict=Verdict.INVALID,
                reason=(
                    "rule {!r} requires a {} witness; none was collected "
                    "({})".format(first.rule_id, first.witness_type, base_reason)
                ),
                rule_id=first.rule_id,
                matched_rule_ids=matched_ids,
                violated_rule_ids=tuple(rule.rule_id for rule in unsatisfied),
            )

        # 4. Everything agrees and the policy is satisfied.
        return RuleDecision(
            verdict=base_verdict,
            reason="{} (rules satisfied: {})".format(
                base_reason, ", ".join(matched_ids)
            ),
            rule_id=None,
            matched_rule_ids=matched_ids,
        )

    # -- internals ----------------------------------------------------

    @staticmethod
    def _rule_from_dict(entry: Dict[str, Any]) -> Rule:
        """Rebuild a Rule from the dict boundary exposed by JudgmentBase."""
        return Rule(
            rule_id=entry["rule_id"],
            target_tool=entry["target_tool"],
            require_witness=bool(entry.get("require_witness", False)),
            witness_type=entry.get("witness_type"),
            expectation=entry.get("expectation"),
            # Carried through so a reconstructed Rule equals the loaded one;
            # the engine itself does not act on declared risk.
            risk_level=entry.get("risk_level"),
        )

    @staticmethod
    def _expectation_unmet(rule: Rule, effect: Any) -> bool:
        """Whether the witnessed effect contradicts the rule's expectation.

        An unobserved effect (``None``) is *not* a violation: the rule cannot
        be checked, which the missing-witness branch reports instead. Calling
        it a violation would manufacture a failure out of absent evidence.
        """
        if rule.expectation is None or effect is None:
            return False
        if rule.expectation == "file_modified":
            return effect is not True
        # JudgmentBase validates the vocabulary, so this is unreachable unless
        # a new expectation was added there without being handled here.
        raise ValueError(
            "rule {!r} uses expectation {!r}, which this engine cannot "
            "evaluate".format(rule.rule_id, rule.expectation)
        )

    def __repr__(self) -> str:
        return "RuleEngine(rules={}, dir={!r})".format(
            len(self._base), self._base.rules_dir
        )
