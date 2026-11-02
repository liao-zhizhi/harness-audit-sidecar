"""Four-valued verdict engine.

The paper argues that a boolean "did the agent finish?" answer cannot
express the case that matters most: the agent says it finished, the
checker agrees, and nothing on disk actually changed. This module
implements the proposed lattice instead.

    PASS     The witnessed effect matches the intent and the agent's own
             report agrees with the witness.
    FAIL     The witness contradicts the intent, or the agent's report
             contradicts the witness (false completion / false failure).
    INVALID  No witness was collected. The engine refuses to answer PASS
             here: absence of evidence is not evidence of success.
    NOT RUN  No intent was recorded, i.e. the scenario never executed.

Precedence is fixed and documented on ``VerdictEngine.explain``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Tuple

__all__ = ["Verdict", "VerdictEngine"]


class Verdict(Enum):
    """The four values of the verdict lattice."""

    PASS = "PASS"
    FAIL = "FAIL"
    INVALID = "INVALID"
    # The paper spells this value "NOT RUN". Python identifiers cannot
    # contain a space, so the member is NOT_RUN while the value keeps the
    # original spelling.
    NOT_RUN = "NOT RUN"

    def __str__(self) -> str:
        return self.value

    @property
    def is_evidence(self) -> bool:
        """True when the verdict says something about the harness.

        NOT RUN is an absence of execution, not an observation, so it is
        kept out of the trust balance denominator.
        """
        return self is not Verdict.NOT_RUN


# Sentinel that distinguishes "caller omitted this argument" from an
# explicit None. The distinction matters: effect=None means "no witness
# was collected" and must be judged as such, not treated as a default.
_UNSET: Any = object()

# Vocabulary accepted in the agent's free-text report.
_SUCCESS_WORDS = frozenset(
    {"success", "succeeded", "ok", "pass", "passed", "done", "true", "yes"}
)
_FAILURE_WORDS = frozenset(
    {"failure", "failed", "fail", "error", "errored", "false", "no", "none"}
)


def _claims_success(check: Any) -> bool:
    """Normalise the agent's self-report into a success/failure claim.

    Accepts a bool, a short status string, or any object exposing a
    ``success`` or ``ok`` attribute. Unrecognised reports raise instead of
    being silently coerced, because a guessing auditor is exactly the
    failure mode this tool exists to detect.
    """
    if isinstance(check, bool):
        return check
    if isinstance(check, str):
        word = check.strip().lower()
        if word in _SUCCESS_WORDS:
            return True
        if word in _FAILURE_WORDS:
            return False
        raise ValueError("unrecognised self-report string: {!r}".format(check))
    for attribute in ("success", "ok"):
        if hasattr(check, attribute):
            return bool(getattr(check, attribute))
    raise TypeError(
        "self-report must be a bool, a status string, or expose "
        ".success/.ok, got {!r}".format(type(check).__name__)
    )


class VerdictEngine:
    """Judge one audit case.

    The engine may be used statelessly::

        VerdictEngine().evaluate(intent=..., effect=..., check=...)

    or bound to a case at construction time::

        VerdictEngine(intent=..., effect=..., check=...).evaluate()

    All three inputs are keyword-only on purpose. ``effect`` and ``check``
    have the same shape, so positional arguments would let a caller swap
    them and get a plausible but wrong verdict without any error.
    """

    def __init__(
        self,
        *,
        intent: Any = _UNSET,
        effect: Any = _UNSET,
        check: Any = _UNSET,
    ) -> None:
        self._intent = intent
        self._effect = effect
        self._check = check

    def evaluate(
        self,
        *,
        intent: Any = _UNSET,
        effect: Any = _UNSET,
        check: Any = _UNSET,
    ) -> Verdict:
        """Return the verdict for one case."""
        return self.explain(intent=intent, effect=effect, check=check)[0]

    def explain(
        self,
        *,
        intent: Any = _UNSET,
        effect: Any = _UNSET,
        check: Any = _UNSET,
    ) -> Tuple[Verdict, str]:
        """Return the verdict plus a short machine-auditable reason.

        Rules are applied in this order, first match wins:

        1. ``effect is None``      -> INVALID (no independent witness).
        2. ``intent is None``      -> NOT RUN (scenario never executed).
        3. ``effect != intent``    -> FAIL (intent not implemented; if the
           report claimed success this is a false completion).
        4. ``check`` claims failure -> FAIL (false failure: the effect
           landed but the agent reported an error).
        5. otherwise               -> PASS (witness and report agree).
        """
        intent = self._intent if intent is _UNSET else intent
        effect = self._effect if effect is _UNSET else effect
        check = self._check if check is _UNSET else check

        if intent is _UNSET or effect is _UNSET or check is _UNSET:
            raise TypeError("intent, effect and check are all required")

        # Rule 1: no witness, no claim. Checked before anything else so
        # that a confident self-report can never buy a PASS.
        if effect is None:
            return (
                Verdict.INVALID,
                "no witness collected: refusing to claim PASS",
            )

        # Rule 2: nothing was intended, so nothing ran.
        if intent is None:
            return Verdict.NOT_RUN, "no intent recorded: scenario never ran"

        claimed_success = _claims_success(check)
        witnessed_ok = effect == intent

        # Rule 3: the side effect does not match the intent.
        if not witnessed_ok:
            if claimed_success:
                return (
                    Verdict.FAIL,
                    "false completion: report says success, witness says "
                    "no effect",
                )
            return (
                Verdict.FAIL,
                "intent not implemented: witness does not match intent",
            )

        # Rule 4: the effect landed but the agent reported an error.
        if not claimed_success:
            return (
                Verdict.FAIL,
                "false failure: witness says effect landed, report says "
                "error",
            )

        # Rule 5: everything agrees.
        return Verdict.PASS, "witness matches intent, report agrees"
