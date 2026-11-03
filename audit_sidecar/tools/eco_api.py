"""The external ecology interface: what a neighbouring system may ask this one.

Internal regulation is eight laws -- L1 through L7 plus the penalty ledger -- and
it decides what may run and what may be believed. An external ecology needs a
different surface. It is not asking to schedule anything; it is asking whether
to route work here, whether to trust a claim that came from here, and whether
this system's failures are worth mining.

Three pure functions, standard library only, no web framework, no global state:

    get_energy_gradient      what a call costs, and whether it is getting dearer
    get_evolution_history    which verdicts were produced, in order, and by what
    get_recyclable_waste     how much of its own failure became rules

The five laws these expose, with provenance stated per law:

    Law 1  energy gradient       cost per call, and the sign of its slope
                                 -> get_energy_gradient          (specified)
    Law 2  edge-to-mainstream    a tool's verdicts across its own history
                                 -> get_evolution_history       (specified)
    Law 3  recyclable waste      failures converted into standing rules
                                 -> get_recyclable_waste        (specified)
    Law 4  keystone species      the source whose loss removes most evidence
                                 -> get_evolution_history       (added here)
    Law 5  carrying capacity     measured headroom before refusal
                                 -> get_energy_gradient         (added here)

Laws 1-3 are the three this module was asked for. The request named three and
closed with "etc.", and a spec that ends in "etc." has not defined a fourth and
fifth. Laws 4 and 5 are this implementation's proposal; they are marked
``"specified": False`` in the payload rather than presented as if they had been
given. A falsely attributed requirement is the same defect as a falsely
attributed citation.

**The governing rule is the lattice's own.** A quantity this system cannot
witness is reported as ``INVALID``, never estimated. A trace written before
token accounting existed carries no token counts, and the honest answer to
"what is the energy gradient" is then "not measured, here is why" -- not a zero,
which reads as a measurement, and not a plausible number, which is worse.

Verdict vocabulary. The three functions reuse the audit lattice's words so a
consumer does not have to learn a second scale:

    PASS      every requested quantity was measured from the artefact
    INVALID   the artefact does not carry what would be needed
    FAIL      reserved for a measurement that contradicts itself; not produced
              here, because these functions measure rather than judge
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "get_energy_gradient",
    "get_evolution_history",
    "get_recyclable_waste",
    "LAWS",
]

#: The five laws, in the order they are reported. ``specified`` records which
#: ones came from the interface request and which are this module's additions.
LAWS: Tuple[Dict[str, Any], ...] = (
    {
        "law": 1,
        "name": "energy_gradient",
        "question": "what does a call cost, and is that rising or falling",
        "source": "get_energy_gradient",
        "specified": True,
    },
    {
        "law": 2,
        "name": "edge_to_mainstream",
        "question": "which verdicts were produced, in what order, by what",
        "source": "get_evolution_history",
        "specified": True,
    },
    {
        "law": 3,
        "name": "recyclable_waste",
        "question": "how many failures were converted into standing rules",
        "source": "get_recyclable_waste",
        "specified": True,
    },
    {
        "law": 4,
        "name": "keystone_source",
        "question": "whose disappearance would remove most of the evidence",
        "source": "get_evolution_history",
        "specified": False,
    },
    {
        "law": 5,
        "name": "carrying_capacity",
        "question": "how much headroom remains before the system refuses work",
        "source": "get_energy_gradient",
        "specified": False,
    },
)

#: Field names that may carry a token count in a trace record. The trace format
#: predates token accounting, so this list is a compatibility surface, not a
#: contract: a record carrying none of these is reported as unmeasured.
_TOKEN_FIELDS: Tuple[str, ...] = (
    "tokens",
    "token_count",
    "tokens_used",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
)

#: Wall-clock fields, in milliseconds unless the name says otherwise.
_DURATION_MS_FIELDS: Tuple[str, ...] = (
    "elapsed_ms",
    "duration_ms",
    "latency_ms",
)

_DURATION_S_FIELDS: Tuple[str, ...] = (
    "elapsed",
    "elapsed_seconds",
    "duration",
    "duration_seconds",
)

_VERDICTS = ("PASS", "FAIL", "INVALID", "NOT RUN")

#: Record types that carry a cost worth measuring. An LLM call spends tokens
#: without producing a verdict -- nothing was witnessed about it -- so it is a
#: cost sample and *not* a trace line. Counting it as a corrupted trace would
#: make a healthy ledger look damaged; counting it as a PASS would fabricate a
#: judgement nobody made.
_COST_RECORD_TYPES = (None, "call", "llm")

#: Record types whose ``verdict`` field is a judgement.
_VERDICT_RECORD_TYPES = (None, "call")


def _is_cost_sample(record: Dict[str, Any]) -> bool:
    """True when a record's cost fields are worth including in the gradient."""
    return record.get("record_type") in _COST_RECORD_TYPES


def _is_verdict_record(record: Dict[str, Any]) -> bool:
    """True when a record's ``verdict`` field claims to be a judgement."""
    return record.get("record_type") in _VERDICT_RECORD_TYPES


def _is_call(record: Dict[str, Any]) -> bool:
    """Back-compatible alias used by the legacy readers."""
    return _is_cost_sample(record)


# ---------------------------------------------------------------------------
# trace reading
# ---------------------------------------------------------------------------


def _read_trace(trace_path: Any) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    """Parse a JSONL trace.

    Returns ``(records, skipped_lines, problem)``. A line that does not parse is
    *counted*, not silently dropped: a reader that ignores what it cannot parse
    will report a clean history for a corrupted file, which is the failure this
    project exists to refuse. ``problem`` is a sentence when the file itself
    could not be opened, and ``None`` otherwise.
    """
    if not isinstance(trace_path, (str, bytes, os.PathLike)):
        raise TypeError(
            "trace_path must be a path, got {!r}".format(type(trace_path).__name__)
        )
    path = os.fspath(trace_path)
    if isinstance(path, bytes):
        path = path.decode()

    records: List[Dict[str, Any]] = []
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    skipped += 1
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
                else:
                    # A JSON scalar or array is not a record. Count it rather
                    # than pretending it is one.
                    skipped += 1
    except FileNotFoundError:
        return [], 0, "no trace at {!r}".format(path)
    except OSError as problem:
        return [], 0, "could not read trace {!r}: {}".format(path, problem)
    return records, skipped, None


def _as_float(value: Any) -> Optional[float]:
    """A finite float, or ``None``. Booleans are not measurements."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    return None


def _tokens_of(record: Dict[str, Any]) -> Optional[float]:
    """The record's token count, summed across a split prompt/completion pair.

    A record that carries ``prompt_tokens`` and ``completion_tokens`` reports
    their sum; one that carries neither of those but has ``tokens_used`` reports
    that. Mixing the two would double-count, so the split pair wins when both
    are present.
    """
    prompt = _as_float(record.get("prompt_tokens"))
    completion = _as_float(record.get("completion_tokens"))
    if prompt is not None or completion is not None:
        return (prompt or 0.0) + (completion or 0.0)
    for field in _TOKEN_FIELDS:
        if field in ("prompt_tokens", "completion_tokens"):
            continue
        value = _as_float(record.get(field))
        if value is not None:
            return value
    return None


def _seconds_of(record: Dict[str, Any]) -> Optional[float]:
    """The record's duration in seconds, if it reports one."""
    for field in _DURATION_MS_FIELDS:
        value = _as_float(record.get(field))
        if value is not None:
            return value / 1000.0
    for field in _DURATION_S_FIELDS:
        value = _as_float(record.get(field))
        if value is not None:
            return value
    return None


def _slope(series: Sequence[float]) -> Optional[float]:
    """Least-squares slope of ``series`` against its index.

    Used as the gradient: change in cost per call across the window. ``None``
    for fewer than two points, because one measurement has no slope -- returning
    ``0.0`` would claim a flat trend that was never observed.
    """
    n = len(series)
    if n < 2:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(series) / n
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    if denominator == 0:
        return None
    numerator = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(series))
    return numerator / denominator


def _window(records: Sequence[Dict[str, Any]], window: Optional[int]) -> List[Dict[str, Any]]:
    """The most recent ``window`` records; all of them when ``window`` is None."""
    if window is None:
        return list(records)
    if isinstance(window, bool) or not isinstance(window, int):
        raise TypeError("window must be an int or None")
    if window < 1:
        raise ValueError("window must be at least 1")
    return list(records[-window:])


# ---------------------------------------------------------------------------
# Law 1 / Law 5 -- energy gradient and carrying capacity
# ---------------------------------------------------------------------------


def get_energy_gradient(
    trace_path: Any,
    window: Optional[int] = 20,
    capacity: Optional[float] = None,
) -> Dict[str, Any]:
    """Cost per call over the last ``window`` calls, and the sign of its slope.

    ``capacity`` is an optional per-call budget. Law 5 (carrying capacity) is
    the headroom that remains: how much dearer a call may get before it exceeds
    ``capacity``. Supplying it is what turns a measurement into a forecast;
    without it the forecast is reported as unavailable rather than guessed.

    Returns a dict always containing ``law``, ``status``, ``samples``,
    ``unmeasured``, ``reason``, plus ``tokens``, ``seconds`` and ``capacity``
    sections. ``status`` is ``PASS`` when at least one cost dimension was
    measured and ``INVALID`` when none was -- a trace written before token
    accounting existed genuinely cannot answer this, and says so.
    """
    records, skipped, problem = _read_trace(trace_path)
    if problem is not None:
        return _invalid_gradient(problem, skipped=skipped)

    recent = _window([record for record in records if _is_call(record)], window)
    if not recent:
        return _invalid_gradient(
            "the trace holds no records; nothing has been measured yet",
            skipped=skipped,
        )

    token_series = [t for t in (_tokens_of(r) for r in recent) if t is not None]
    second_series = [s for s in (_seconds_of(r) for r in recent) if s is not None]

    tokens = _cost_block(token_series, unit="tokens")
    seconds = _cost_block(second_series, unit="seconds")

    unmeasured: List[str] = []
    if not token_series:
        unmeasured.append("tokens")
    if not second_series:
        unmeasured.append("seconds")

    capacity_block = _capacity_block(token_series, capacity)

    if not token_series and not second_series:
        return {
            "law": "energy_gradient",
            "status": "INVALID",
            "samples": len(recent),
            "window": window,
            "tokens": tokens,
            "seconds": seconds,
            "capacity": capacity_block,
            "unmeasured": unmeasured,
            "skipped_lines": skipped,
            "reason": (
                "no record in the window carries a token count or a duration. The "
                "trace schema stores the verdict, not the cost of reaching it, so "
                "this is a missing witness rather than a zero. Record "
                "'tokens_used' or 'elapsed_ms' to make this answerable."
            ),
        }

    reason_parts = []
    if "tokens" in unmeasured:
        reason_parts.append("no token counts in the window")
    if "seconds" in unmeasured:
        reason_parts.append("no durations in the window")
    if skipped:
        reason_parts.append("{} unparseable line(s) skipped".format(skipped))

    return {
        "law": "energy_gradient",
        "status": "PASS",
        "samples": len(recent),
        "window": window,
        "tokens": tokens,
        "seconds": seconds,
        "capacity": capacity_block,
        "unmeasured": unmeasured,
        "skipped_lines": skipped,
        "reason": "; ".join(reason_parts) if reason_parts else "measured in full",
    }


def _cost_block(series: Sequence[float], unit: str) -> Dict[str, Any]:
    """Aggregate one cost dimension: total, per-call mean, and slope."""
    if not series:
        return {
            "unit": unit,
            "measured_calls": 0,
            "total": None,
            "per_call": None,
            "gradient_per_call": None,
            "trend": None,
        }
    total = sum(series)
    per_call = total / len(series)
    slope = _slope(series)
    trend = None
    # The dead band matters: a slope of 0.0001 tokens per call is not a trend,
    # it is rounding. Claiming "rising" there would be a manufactured signal.
    if slope is not None:
        if abs(slope) <= 1e-9:
            trend = "flat"
        else:
            trend = "rising" if slope > 0 else "falling"
    return {
        "unit": unit,
        "measured_calls": len(series),
        "total": total,
        "per_call": per_call,
        "gradient_per_call": slope,
        "trend": trend,
    }


def _capacity_block(token_series: Sequence[float], capacity: Optional[float]) -> Dict[str, Any]:
    """Headroom against an optional per-call budget (Law 5)."""
    limit = _as_float(capacity)
    if limit is None:
        return {
            "measured": False,
            "limit": None,
            "current_per_call": None,
            "headroom": None,
            "calls_before_refusal": None,
            "reason": "no capacity supplied; headroom cannot be computed",
        }
    if limit <= 0:
        raise ValueError("capacity must be positive, got {!r}".format(capacity))
    if not token_series:
        return {
            "measured": False,
            "limit": limit,
            "current_per_call": None,
            "headroom": None,
            "calls_before_refusal": None,
            "reason": "no token measurements to compare against the limit",
        }
    current = sum(token_series) / len(token_series)
    headroom = limit - current
    # A negative headroom is the informative case: the system is already over
    # its per-call budget, and consuming "calls before refusal" would be
    # meaningless. Report zero rather than a negative count.
    calls = None
    if current > 0:
        calls = max(0, int(headroom // current)) if headroom >= 0 else 0
    return {
        "measured": True,
        "limit": limit,
        "current_per_call": current,
        "headroom": headroom,
        "calls_before_refusal": calls,
        "reason": "measured against the supplied per-call limit",
    }


def _invalid_gradient(reason: str, skipped: int = 0) -> Dict[str, Any]:
    return {
        "law": "energy_gradient",
        "status": "INVALID",
        "samples": 0,
        "window": None,
        "tokens": _cost_block([], unit="tokens"),
        "seconds": _cost_block([], unit="seconds"),
        "capacity": _capacity_block([], None),
        "unmeasured": ["tokens", "seconds"],
        "skipped_lines": skipped,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Law 2 / Law 4 -- succession and the keystone source
# ---------------------------------------------------------------------------


def get_evolution_history(trace_path: Any) -> Dict[str, Any]:
    """The verdict sequence, and what each tool's own history looks like.

    Law 2 is succession: a capability that starts on the edge -- producing
    ``FAIL`` or ``INVALID`` when first used -- and ends producing ``PASS`` has
    graduated into the mainstream of this system. The reverse, a tool that
    passed first and fails now, is a regression and is reported separately,
    because the two have different causes and only one of them is progress.

    Law 4 is the keystone source: the tool carrying the largest share of
    witnessed ``PASS``es. It is the one whose removal would take most of the
    evidence with it, which is why it is worth naming rather than leaving a
    consumer to infer it from a table.

    Returns ``sequence`` (every verdict, in order), ``counts``, ``total``,
    ``succession`` (per-tool first and latest verdict), ``regressions``,
    ``keystone`` and ``skipped_lines``.
    """
    records, skipped, problem = _read_trace(trace_path)
    if problem is not None:
        return {
            "law": "edge_to_mainstream",
            "status": "INVALID",
            "sequence": [],
            "counts": {v: 0 for v in _VERDICTS},
            "total": 0,
            "succession": [],
            "graduated": [],
            "regressions": [],
            "keystone": None,
            "skipped_lines": skipped,
            "bookkeeping": 0,
            "reason": problem,
        }

    sequence: List[str] = []
    per_tool: Dict[str, List[str]] = {}
    bookkeeping = 0
    for record in records:
        verdict = record.get("verdict")
        if verdict not in _VERDICTS:
            if not _is_verdict_record(record):
                # A grant, a denial or an LLM cost sample is a ledger entry, not
                # a corrupted verdict.
                bookkeeping += 1
                continue
            # An unrecognised verdict is not silently bucketed as INVALID: that
            # would turn "this reader is out of date" into "the system failed".
            skipped += 1
            continue
        sequence.append(verdict)
        tool = record.get("tool")
        key = tool if isinstance(tool, str) and tool else "unknown"
        per_tool.setdefault(key, []).append(verdict)

    counts = {verdict: sequence.count(verdict) for verdict in _VERDICTS}

    succession: List[Dict[str, Any]] = []
    graduated: List[Dict[str, Any]] = []
    regressions: List[Dict[str, Any]] = []
    for tool in sorted(per_tool):
        history = per_tool[tool]
        first, latest = history[0], history[-1]
        entry = {
            "tool": tool,
            "calls": len(history),
            "first_verdict": first,
            "latest_verdict": latest,
            "passes": history.count("PASS"),
        }
        # Graduation requires having started off the mainstream: a tool that
        # passed on its first call did not succeed at anything, it was simply
        # never observed in trouble.
        entry["graduated"] = first != "PASS" and latest == "PASS"
        entry["regressed"] = first == "PASS" and latest != "PASS"
        succession.append(entry)
        if entry["graduated"]:
            graduated.append(entry)
        if entry["regressed"]:
            regressions.append(entry)

    total_passes = counts["PASS"]
    keystone = None
    if total_passes:
        best = max(succession, key=lambda item: (item["passes"], item["calls"]))
        if best["passes"]:
            keystone = {
                "tool": best["tool"],
                "passes": best["passes"],
                "share_of_passes": best["passes"] / total_passes,
                "calls": best["calls"],
                "reason": (
                    "carries {} of {} witnessed PASSes; removing it would take "
                    "that share of the evidence with it".format(
                        best["passes"], total_passes
                    )
                ),
            }

    reason_parts = []
    if not sequence:
        reason_parts.append("no recognised verdict in the trace")
    if skipped:
        reason_parts.append("{} record(s) skipped".format(skipped))

    return {
        "law": "edge_to_mainstream",
        "status": "PASS" if sequence else "INVALID",
        "sequence": sequence,
        "counts": counts,
        "total": len(sequence),
        "succession": succession,
        "graduated": graduated,
        "regressions": regressions,
        "keystone": keystone,
        "skipped_lines": skipped,
        "bookkeeping": bookkeeping,
        "reason": "; ".join(reason_parts) if reason_parts else "measured in full",
    }


# ---------------------------------------------------------------------------
# Law 3 -- recyclable waste
# ---------------------------------------------------------------------------


_MISTAKE = "**The Mistake.**"
_FIX = "**The Fix.**"
_EVIDENCE = "**Evidence.**"
_NO_ARTEFACT = "no surviving artefact"


def get_recyclable_waste(errata_path: Any) -> Dict[str, Any]:
    """How many recorded failures became standing rules.

    An errata entry that states a mistake and a fix has converted waste into a
    rule: the failure is now something the system is built to avoid. One that
    states a mistake and no fix is compost, not fertiliser -- it is still worth
    recording, but nothing was extracted from it, and the ratio should show that
    rather than counting every heading as a lesson.

    An entry whose evidence has disappeared is counted as ``unwitnessed``. It is
    still a rule if it names a fix, but a reader must be able to see that its
    provenance is memory rather than an artefact.

    Returns ``entries``, ``rules_extracted``, ``unwitnessed``, ``recycle_rate``,
    the per-entry detail, and ``skipped_sections`` (headings that are prose
    rather than errata, such as an addendum or a closing note).
    """
    if not isinstance(errata_path, (str, bytes, os.PathLike)):
        raise TypeError(
            "errata_path must be a path, got {!r}".format(type(errata_path).__name__)
        )
    path = os.fspath(errata_path)
    if isinstance(path, bytes):
        path = path.decode()

    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return _invalid_waste("no errata file at {!r}".format(path))
    except OSError as problem:
        return _invalid_waste("could not read errata {!r}: {}".format(path, problem))

    sections = re.split(r"(?m)^##\s+", text)
    entries: List[Dict[str, Any]] = []
    skipped_sections = 0
    unrecycled = 0
    for section in sections:
        if not section.strip():
            continue
        heading = section.splitlines()[0].strip() if section.splitlines() else ""
        has_mistake = _MISTAKE in section
        has_fix = _FIX in section
        if not has_mistake:
            # Prose sections (the preamble, an addendum, a closing note) are not
            # errata. Counting them would inflate the ratio with text that never
            # recorded a failure.
            skipped_sections += 1
            continue
        if not has_fix:
            # A recorded failure nothing was extracted from. It is compost, not
            # fertiliser, and the ratio has to show it rather than round it up.
            unrecycled += 1
            entries.append(
                {
                    "heading": heading,
                    "has_fix": False,
                    "unwitnessed": _NO_ARTEFACT in section.lower(),
                    "has_evidence": _EVIDENCE in section,
                }
            )
            continue
        entries.append(
            {
                "heading": heading,
                "has_fix": True,
                "unwitnessed": _NO_ARTEFACT in section.lower(),
                "has_evidence": _EVIDENCE in section,
            }
        )

    rules = sum(1 for entry in entries if entry["has_fix"])
    unwitnessed_count = sum(1 for entry in entries if entry["unwitnessed"])
    # The denominator is failures, not headings: a ratio that moves when someone
    # adds a paragraph of prose is measuring the document, not the recycling.
    failures = rules + unrecycled

    return {
        "law": "recyclable_waste",
        "status": "PASS" if entries else "INVALID",
        "entries": len(entries),
        "rules_extracted": rules,
        "unrecycled": unrecycled,
        "unwitnessed": unwitnessed_count,
        "recycle_rate": (rules / failures) if failures else 0.0,
        "detail": entries,
        "skipped_sections": skipped_sections,
        "reason": (
            "{} failure(s) recorded, {} converted into standing rules, {} left "
            "with no fix, {} resting on memory rather than a surviving "
            "artefact".format(rules + unrecycled, rules, unrecycled, unwitnessed_count)
            if entries
            else "no errata entry records a mistake"
        ),
    }


def _invalid_waste(reason: str) -> Dict[str, Any]:
    return {
        "law": "recyclable_waste",
        "status": "INVALID",
        "entries": 0,
        "rules_extracted": 0,
        "unrecycled": 0,
        "unwitnessed": 0,
        "recycle_rate": 0.0,
        "detail": [],
        "skipped_sections": 0,
        "reason": reason,
    }
