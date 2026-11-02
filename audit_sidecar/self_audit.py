"""Self-audit: prove that the auditor can still say no.

The fourth failure mode in the paper's taxonomy is the auditor that has
quietly become a decoration: it always answers PASS (or always answers
FAIL) and nobody notices, because the auditor itself is never audited.

``run_self_audit`` closes that hole with fixtures whose correct answer is
known in advance. The engine must reject all three:

* known-bad     a write that never landed, reported as success -> FAIL
* known-good    a write that did land, reported as success    -> PASS
* no-witness    a success claim with no independent witness   -> INVALID

The known-good and no-witness fixtures are not decoration. Without them a
degenerate engine that returns FAIL unconditionally, or one that returns
INVALID unconditionally, would happily pass the known-bad check alone.

When a judgment base is present the gate runs a second battery against
``RuleEngine``, because moving policy out of the code moves the failure mode
too: a rule file that fails to load, or loads and is never consulted, leaves
a gate that looks open while enforcing nothing. Those fixtures therefore
check three things -- that rules are actually loaded, that a run violating the
loaded rule is rejected, and that the rejection names the offending
``rule_id``. A rule that fires without saying which rule it was is not
auditable by the person who has to change it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from typing import Iterator, Optional, Tuple

from .judgment_base import JudgmentBase, RuleError, default_rules_dir
from .rule_engine import RuleEngine
from .verdict import Verdict, VerdictEngine

__all__ = ["run_self_audit"]

_LOG = logging.getLogger(__name__)

_SEED_CONTENT = b"original content written before the run\n"
_INTENDED_CONTENT = b"content the agent claimed to write\n"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixtures(directory: str) -> Iterator[Tuple[str, dict, Verdict]]:
    """Yield ``(name, case, expected)`` triples for the self-audit run."""
    target = os.path.join(directory, "target.txt")
    intended_hash = _sha256_bytes(_INTENDED_CONTENT)

    with open(target, "wb") as handle:
        handle.write(_SEED_CONTENT)
    seed_hash = _sha256_file(target)

    # The agent claims it replaced the file, but the write is a no-op.
    # The witness (file hash) still shows the seed content.
    stale_hash = _sha256_file(target)
    if stale_hash != seed_hash or intended_hash == seed_hash:
        raise AssertionError("self-audit fixture is broken: hashes collide")

    yield (
        "known-bad/false-completion",
        {"intent": intended_hash, "effect": stale_hash, "check": True},
        Verdict.FAIL,
    )

    # The same intent, this time actually carried out.
    with open(target, "wb") as handle:
        handle.write(_INTENDED_CONTENT)
    landed_hash = _sha256_file(target)
    if landed_hash != intended_hash:
        raise AssertionError("self-audit fixture is broken: write did not land")

    yield (
        "known-good/real-completion",
        {"intent": intended_hash, "effect": landed_hash, "check": True},
        Verdict.PASS,
    )

    # No witness was collected, so nothing can be asserted either way.
    yield (
        "no-witness/missing-effect",
        {"intent": intended_hash, "effect": None, "check": True},
        Verdict.INVALID,
    )


def run_self_audit(rules_dir: Optional[str] = None) -> bool:
    """Run the known-answer fixtures against the engine.

    Returns True only when every fixture produces exactly the expected
    verdict. Any other outcome means the tool can no longer be trusted to
    judge agent runs, so it must refuse to run.

    ``rules_dir`` selects the judgment base to exercise. When omitted the
    shipped ``rules/`` directory is used if one can be found; when no base
    exists at all the rule battery is skipped with a warning, because a tool
    that refuses to start merely because no policy was configured would be
    unusable in the "plain lattice" mode. Passing a path explicitly is
    different: a missing directory there is an operator error and fails the
    gate.
    """
    if not _run_engine_audit():
        return False
    return _run_rules_audit(rules_dir)


def _run_engine_audit() -> bool:
    """The original battery: the lattice must still reject known-bad cases."""
    engine = VerdictEngine()
    passed = True

    with tempfile.TemporaryDirectory(prefix="harness-self-audit-") as directory:
        try:
            fixtures = list(_fixtures(directory))
        except (AssertionError, OSError) as error:
            _LOG.error("self-audit could not build its fixtures: %s", error)
            return False

        for name, case, expected in fixtures:
            actual, reason = engine.explain(**case)
            if actual is expected:
                _LOG.debug("self-audit %s -> %s", name, actual)
                continue
            passed = False
            _LOG.error(
                "self-audit %s returned %s, expected %s (%s)",
                name,
                actual,
                expected,
                reason,
            )

    if not passed:
        _LOG.error(
            "self-audit failed: the verdict engine no longer rejects known-bad "
            "cases, so every verdict it produces is decoration"
        )
    return passed


# Rule fixtures: (name, tool, case kwargs, expected verdict, expected rule_id).
#
# ``witnesses`` is the set of witness types the runner would have reported.
# The middle fixture is the one that only a rule engine can catch: the file
# witness alone proves the write landed, so the lattice says PASS, and the
# policy says "not without the git change set".
def _rule_fixtures() -> Tuple[Tuple[str, str, dict, Verdict, Optional[str]], ...]:
    return (
        (
            "rules/violation-no-git-witness",
            "write_file",
            {
                "intent": True,
                "effect": True,
                "check": True,
                "witnesses": frozenset({"file", "agent_report"}),
            },
            Verdict.INVALID,
            "no_file_drift",
        ),
        (
            "rules/violation-effect-missing",
            "write_file",
            {
                "intent": True,
                "effect": False,
                "check": True,
                "witnesses": frozenset({"file", "git_diff", "agent_report"}),
            },
            Verdict.FAIL,
            "no_file_drift",
        ),
        (
            "rules/satisfied-with-git-witness",
            "write_file",
            {
                "intent": True,
                "effect": True,
                "check": True,
                "witnesses": frozenset({"file", "git_diff", "agent_report"}),
            },
            Verdict.PASS,
            None,
        ),
        (
            "rules/fallback-no-rule-for-tool",
            "run_command",
            {
                "intent": True,
                "effect": True,
                "check": True,
                "witnesses": frozenset({"git_diff"}),
            },
            Verdict.PASS,
            None,
        ),
    )


def _run_rules_audit(rules_dir: Optional[str] = None) -> bool:
    """Prove the judgment base loads, applies, and names its rule."""
    explicit = rules_dir is not None
    target = rules_dir if explicit else default_rules_dir()
    if target is None:
        _LOG.warning(
            "self-audit: no judgment base found, skipping the rule battery "
            "(the audit is running on the plain lattice)"
        )
        return True

    try:
        base = JudgmentBase(target)
    except (RuleError, FileNotFoundError) as problem:
        _LOG.error("self-audit: judgment base %s could not be loaded: %s", target, problem)
        return False

    if not len(base):
        if explicit:
            _LOG.error(
                "self-audit: judgment base %s contains no rules, so the rule "
                "gate would be open while appearing configured",
                target,
            )
            return False
        _LOG.warning("self-audit: judgment base %s is empty, skipping", target)
        return True

    engine = RuleEngine(base)
    passed = True
    for name, tool, case, expected_verdict, expected_rule in _rule_fixtures():
        witnesses = case["witnesses"]
        observations = {k: v for k, v in case.items() if k != "witnesses"}
        decision = engine.judge(tool, witnesses=witnesses, **observations)
        if decision.verdict is not expected_verdict or decision.rule_id != expected_rule:
            passed = False
            _LOG.error(
                "self-audit %s returned %s (rule=%r), expected %s (rule=%r)",
                name,
                decision.verdict,
                decision.rule_id,
                expected_verdict,
                expected_rule,
            )
            continue
        _LOG.debug("self-audit %s -> %s (rule=%s)", name, decision.verdict, decision.rule_id)

    if not passed:
        _LOG.error(
            "self-audit failed: the judgment base no longer rejects a run that "
            "violates its own rules, so the policy is decoration"
        )
        return False

    _LOG.debug(
        "self-audit: judgment base %s loaded %d rule(s): %s",
        base.rules_dir,
        len(base),
        ", ".join(base.rule_ids),
    )
    return True
