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
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from typing import Iterator, Tuple

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


def run_self_audit() -> bool:
    """Run the known-answer fixtures against the engine.

    Returns True only when every fixture produces exactly the expected
    verdict. Any other outcome means the tool can no longer be trusted to
    judge agent runs, so it must refuse to run.
    """
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
