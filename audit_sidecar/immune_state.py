"""Conditional immune response: pay for the full audit only where it can matter.

Watching every call with the full witness is correct and unaffordable. The git
change set costs a snapshot plus a diff; reading every artefact back costs the
whole file. On a long session most calls are boring, and a gate that costs the
same for a boring call as for a dangerous one is a gate operators eventually
turn off -- which is the same failure as having no gate at all.

Biology solves this the same way, in two stages:

**Innate immunity (tier 1).** Cheap, always on, local. It does not identify
what went wrong, only that something is wrong. Here it is an existence and
mtime check on the paths the call claimed to touch: two ``stat`` calls per
path, no git process, no file read.

**Adaptive immunity (tier 2).** Expensive, targeted, and *raised on evidence*.
Here it is the git change set plus a content read, which can tell a correct
edit from a correct-looking edit that also touched something else.

An inflammatory response is the switch between them. A system that has just
been contradicted is not a system whose cheap checks should be trusted, so a
run of failures escalates *every* call to tier 2 -- including calls whose own
policy says they are low risk. Healing is deliberately slower than injury:
two FAILs inflame, but five consecutive passes are needed to calm down. The
asymmetry is the point; a response that decays as fast as it rises cannot
protect anything.

Two properties this module does not have, on purpose:

* It never *loosens* a verdict. Tiers choose which witnesses to collect; the
  lattice and the rules decide what they mean. A tier-1 PASS is a PASS on
  weaker evidence, never a PASS on contradicting evidence.
* It never decides that something is fine because it is *boring*. Risk is
  declared by policy (``risk_level``) or forced by inflammation. A tool with
  no rule at all gets tier 2: absence of a policy is not evidence of low risk.
"""

from __future__ import annotations

from collections.abc import Mapping as _MappingABC
from collections.abc import Sequence as _SequenceABC
from typing import Any, Dict, Mapping, Sequence

from .verdict import Verdict

__all__ = [
    "ImmuneState",
    "select_tier",
    "STATE_INFLAMED",
    "STATE_NORMAL",
    "TIER_ADAPTIVE",
    "TIER_INNATE",
]

STATE_NORMAL = "NORMAL"
STATE_INFLAMED = "INFLAMED"

#: The cheap, always-on check: existence plus mtime of the claimed paths.
TIER_INNATE = "tier1"
#: The full check: git change set plus content read.
TIER_ADAPTIVE = "tier2"

#: Value of ``risk_level`` that permits the innate tier. ``None`` (the field
#: omitted from the YAML) means the same thing: a rule that bothers to name a
#: tool has already taken responsibility for it.
LOW_RISK = "low"


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            "{} must be an int, got {!r}".format(name, type(value).__name__)
        )
    if value < 1:
        raise ValueError("{} must be at least 1, got {}".format(name, value))
    return value


class ImmuneState:
    """Whether the sidecar is currently inflamed, and how it got there.

    The state is advanced one verdict at a time via :meth:`record_verdict`,
    which must be called *after* the verdict for a call has been decided. A
    verdict therefore never changes the tier of the call that produced it; it
    scopes the next one. Anything else would let a single call be judged twice
    at different costs, and would make the trace ambiguous about which check
    the recorded verdict came from.
    """

    #: Consecutive FAILs that trigger inflammation.
    DEFAULT_FAIL_THRESHOLD = 2
    #: Consecutive INVALIDs that trigger it. Lower, because INVALID means "we
    #: could not see", and an audit that cannot see is already not working.
    DEFAULT_INVALID_THRESHOLD = 1
    #: Consecutive PASSes required to return to NORMAL.
    DEFAULT_RECOVERY_PASSES = 5

    def __init__(
        self,
        *,
        fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
        invalid_threshold: int = DEFAULT_INVALID_THRESHOLD,
        recovery_passes: int = DEFAULT_RECOVERY_PASSES,
    ) -> None:
        self._fail_threshold = _positive_int(fail_threshold, "fail_threshold")
        self._invalid_threshold = _positive_int(invalid_threshold, "invalid_threshold")
        self._recovery_passes = _positive_int(recovery_passes, "recovery_passes")
        if self._recovery_passes < self._fail_threshold:
            # Healing slower than injury is the whole design; the reverse would
            # mean a single pass undoes a burst of failures.
            raise ValueError(
                "recovery_passes ({}) must be at least fail_threshold ({})".format(
                    self._recovery_passes, self._fail_threshold
                )
            )
        self._inflamed = False
        self._danger = 0
        self._recovery = 0

    # -- public API ---------------------------------------------------

    def record_verdict(self, verdict: Verdict) -> str:
        """Advance the state by one verdict; return the resulting state.

        ``NOT RUN`` is ignored rather than counted as health. It says the
        scenario never executed, which is no evidence either way -- counting
        it as a pass would let a run of skipped calls heal an inflamed system
        without a single thing having been checked.
        """
        if not isinstance(verdict, Verdict):
            raise TypeError(
                "verdict must be a Verdict, got {!r}".format(type(verdict).__name__)
            )

        if verdict is Verdict.NOT_RUN:
            return self.state

        if verdict is Verdict.PASS:
            if self._inflamed:
                self._recovery += 1
                if self._recovery >= self._recovery_passes:
                    self._inflamed = False
                    self._recovery = 0
                    self._danger = 0
            else:
                # Only *consecutive* failures inflame: an occasional failure
                # surrounded by verified work is a normal day, not an attack.
                self._danger = 0
            return self.state

        # FAIL or INVALID: damage, and the recovery streak restarts.
        self._danger += 1
        self._recovery = 0
        threshold = (
            self._invalid_threshold
            if verdict is Verdict.INVALID
            else self._fail_threshold
        )
        if self._danger >= threshold:
            self._inflamed = True
        return self.state

    def is_inflamed(self) -> bool:
        """True when every call is forced onto the full adaptive check."""
        return self._inflamed

    def reset(self) -> None:
        """Return to NORMAL, discarding the danger count. For tests and restarts."""
        self._inflamed = False
        self._danger = 0
        self._recovery = 0

    # -- observability ------------------------------------------------

    @property
    def state(self) -> str:
        return STATE_INFLAMED if self._inflamed else STATE_NORMAL

    @property
    def danger_count(self) -> int:
        """Consecutive FAIL/INVALID verdicts since the last PASS."""
        return self._danger

    @property
    def recovery_progress(self) -> int:
        """Consecutive PASSes collected while inflamed (0 when not inflamed)."""
        return self._recovery

    @property
    def fail_threshold(self) -> int:
        return self._fail_threshold

    @property
    def invalid_threshold(self) -> int:
        return self._invalid_threshold

    @property
    def recovery_passes(self) -> int:
        return self._recovery_passes

    def snapshot(self) -> Dict[str, Any]:
        """JSON-friendly state, for a trace record."""
        return {
            "state": self.state,
            "danger_count": self._danger,
            "recovery_progress": self._recovery,
            "recovery_passes": self._recovery_passes,
        }

    def __repr__(self) -> str:
        return "ImmuneState(state={}, danger={}, recovery={}/{})".format(
            self.state, self._danger, self._recovery, self._recovery_passes
        )


def select_tier(
    *,
    rules: Sequence[Mapping[str, Any]],
    inflamed: bool,
    has_observable_path: bool = True,
) -> str:
    """Choose the witness class for one call.

    The decision is deliberately conservative in one direction: every reason
    to escalate wins, and a single reason to escalate is enough.

    ``rules`` is the per-tool policy as ``JudgmentBase.get_rules_for_tool``
    returns it, i.e. plain dicts. An empty sequence means no policy names this
    tool, which escalates: the alternative would be to infer "low risk" from
    the absence of a declaration, so that adding a rules directory could make
    the audit *weaker* for every tool it forgot to mention.

    ``has_observable_path`` is False when the call names no path at all. Tier 1
    has nothing to stat in that case, and asking for it would produce an
    INVALID that says more about the tier than about the call -- an INVALID
    that would itself trigger inflammation.
    """
    if not isinstance(inflamed, bool):
        raise TypeError(
            "inflamed must be a bool, got {!r}".format(type(inflamed).__name__)
        )
    if not isinstance(has_observable_path, bool):
        raise TypeError(
            "has_observable_path must be a bool, got {!r}".format(
                type(has_observable_path).__name__
            )
        )
    if isinstance(rules, (str, bytes)) or not isinstance(rules, _SequenceABC):
        raise TypeError(
            "rules must be a sequence of mappings, got {!r}".format(
                type(rules).__name__
            )
        )

    if inflamed:
        return TIER_ADAPTIVE
    if not rules:
        return TIER_ADAPTIVE
    if not has_observable_path:
        return TIER_ADAPTIVE

    for rule in rules:
        if not isinstance(rule, _MappingABC):
            raise TypeError(
                "each rule must be a mapping, got {!r}".format(type(rule).__name__)
            )
        if rule.get("risk_level") not in (None, LOW_RISK):
            return TIER_ADAPTIVE
        if rule.get("require_witness"):
            # A rule that demands the git change set cannot be satisfied by a
            # stat, so honouring it is tier 2 regardless of declared risk.
            return TIER_ADAPTIVE
    return TIER_INNATE
