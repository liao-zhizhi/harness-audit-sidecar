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

**Inflammation survives the process.** A state that lived only in memory would
be a get-out-of-jail card: crash the sidecar, restart it, and the suspicion is
gone -- and crashing the sidecar is something the audited agent can arrange.
So with a ``state_path`` the counts are written to a small JSON file after every
verdict and read back at construction. Two rules govern the recovery:

* **Nothing is lost silently.** A missing file means "first run" and is normal;
  an unreadable, malformed or unknown-version file is an *amnesia event* and is
  logged at ERROR, because a system that quietly forgets is a system that can be
  made to forget on purpose.
* **Every ambiguity resolves toward suspicion.** A file whose counters are
  inconsistent with its state (``INFLAMED`` with zero danger, a recovery
  progress past its own threshold) is clamped so that it stays inflamed, never
  so that it calms down. Guessing "probably fine" from a damaged record is the
  one direction this module must never round in.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping as _MappingABC
from collections.abc import Sequence as _SequenceABC
from typing import Any, Dict, Mapping, Optional, Sequence

from .verdict import Verdict

__all__ = [
    "ImmuneState",
    "select_tier",
    "STATE_FILENAME",
    "STATE_INFLAMED",
    "STATE_NORMAL",
    "TIER_ADAPTIVE",
    "TIER_INNATE",
]

_LOG = logging.getLogger(__name__)

STATE_NORMAL = "NORMAL"
STATE_INFLAMED = "INFLAMED"

#: Default filename for the persisted state, placed beside the audit trace.
STATE_FILENAME = ".immune_state.json"

#: Schema version. A future reader that finds a version it does not know must
#: not guess: the file is rejected and the system stays suspicious.
STATE_VERSION = 1

#: The cheap, always-on check: existence plus mtime of the claimed paths.
TIER_INNATE = "tier1"
#: The full check: git change set plus content read.
TIER_ADAPTIVE = "tier2"

#: Value of ``risk_level`` that permits the innate tier. ``None`` (the field
#: omitted from the YAML) means the same thing: a rule that bothers to name a
#: tool has already taken responsibility for it.
LOW_RISK = "low"


def _normalise_state_path(state_path: Any) -> Optional[str]:
    """Validate the persistence path, or ``None`` for a memory-only state."""
    if state_path is None:
        return None
    if isinstance(state_path, os.PathLike):
        state_path = os.fspath(state_path)
    if not isinstance(state_path, str) or not state_path.strip():
        raise ValueError(
            "state_path must be a non-empty string or None, got {!r}".format(
                state_path
            )
        )
    return os.path.abspath(state_path)


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
        state_path: Optional[str] = None,
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
        self._state_path = _normalise_state_path(state_path)
        self._inflamed = False
        self._danger = 0
        self._recovery = 0
        if self._state_path is not None:
            self._restore()

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
            self._persist()
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
        self._persist()
        return self.state

    def is_inflamed(self) -> bool:
        """True when every call is forced onto the full adaptive check."""
        return self._inflamed

    def reset(self) -> None:
        """Return to NORMAL, discarding the danger count.

        An explicit reset is durable: it is written through to the state file,
        so "I cleared it" and "I restarted the sidecar" do not accidentally
        look the same afterwards.
        """
        self._inflamed = False
        self._danger = 0
        self._recovery = 0
        self._persist()

    # -- persistence --------------------------------------------------

    @property
    def state_path(self) -> Optional[str]:
        """Where this state is written, or ``None`` when it is memory-only."""
        return self._state_path

    def _persist(self) -> None:
        """Write the counters to ``state_path`` atomically, if configured.

        Written to a sibling temporary file and moved into place, because the
        interesting failure is a crash *during* the write: a half-written file
        would restore as "unparseable" and lose exactly the inflammation that
        was being recorded. An I/O failure is logged and swallowed -- taking
        the agent down because bookkeeping could not be flushed would turn a
        lost audit trail into an outage -- but never silently: this is the one
        path by which suspicion can be dropped without a verdict.
        """
        if self._state_path is None:
            return
        payload = self.as_document()
        directory = os.path.dirname(self._state_path) or "."
        handle_name: Optional[str] = None
        try:
            os.makedirs(directory, exist_ok=True)
            descriptor, handle_name = tempfile.mkstemp(
                prefix=".immune-", suffix=".tmp", dir=directory
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle_name, self._state_path)
            handle_name = None
        except OSError as problem:
            _LOG.error(
                "could not persist immune state to %s: %s; inflammation is "
                "memory-only until this is fixed",
                self._state_path,
                problem,
            )
        finally:
            if handle_name is not None:
                try:
                    os.unlink(handle_name)
                except OSError:
                    # A stray temp file is not worth raising over; the real
                    # failure has already been logged above.
                    pass

    def _restore(self) -> None:
        """Read ``state_path`` if it exists, resolving doubt toward suspicion."""
        assert self._state_path is not None
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            # A missing file is an ordinary first run, not an incident: there
            # is nothing to remember yet.
            return
        except (OSError, ValueError) as problem:
            _LOG.error(
                "could not read immune state from %s: %s; starting NORMAL. "
                "Treat this as a possible amnesia event, not a clean slate",
                self._state_path,
                problem,
            )
            return

        restored = self._from_document(document)
        if restored is None:
            _LOG.error(
                "immune state in %s is not a record this version understands; "
                "starting NORMAL. Treat this as a possible amnesia event",
                self._state_path,
            )
            return

        inflamed, danger, recovery = restored
        self._inflamed = inflamed
        self._danger = danger
        self._recovery = recovery

    def _from_document(self, document: Any) -> Optional[Any]:
        """Validate a loaded document; ``None`` when it cannot be trusted.

        Anything inconsistent is clamped in the *suspicious* direction: an
        ``INFLAMED`` record with zero danger stays inflamed, and a recovery
        progress at or past its threshold is held one short of healing. A
        damaged record must never be able to calm the system down.
        """
        if not isinstance(document, Mapping):
            return None
        if document.get("version") != STATE_VERSION:
            return None
        state = document.get("state")
        if state not in (STATE_NORMAL, STATE_INFLAMED):
            return None
        danger = document.get("danger_count")
        recovery = document.get("recovery_progress")
        for value in (danger, recovery):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
        inflamed = state == STATE_INFLAMED
        if not inflamed:
            # A calm record with a recovery streak in progress is incoherent;
            # dropping the streak loses nothing, since healing is complete.
            return False, danger, 0
        # Still inflamed: keep the lowest recovery count that is consistent
        # with not having healed, so a forged or damaged counter cannot be
        # used to skip straight to calm.
        return True, danger, min(recovery, self._recovery_passes - 1)

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

    def as_document(self) -> Dict[str, Any]:
        """The persisted form, including the schema version.

        Separate from ``snapshot`` because they answer different questions: a
        trace wants "what did the system believe at this call", a state file
        wants "what should the next process believe".
        """
        return {
            "version": STATE_VERSION,
            "state": self.state,
            "danger_count": self._danger,
            "recovery_progress": self._recovery,
            "recovery_passes": self._recovery_passes,
            "fail_threshold": self._fail_threshold,
            "invalid_threshold": self._invalid_threshold,
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
