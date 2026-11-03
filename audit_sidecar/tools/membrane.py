"""The semi-permeable membrane: the only sanctioned way in or out.

A function that reads a file is not an interface. An interface is a boundary
with a potential difference across it -- something the outside must pay before
the inside will act -- and the payment has to be checked by the side that did
not set the terms. Otherwise the caller defines what counts as a valid witness
and the membrane is decorative.

Four properties, and what actually implements each:

**Potential difference (势差).** ``request_action_warrant`` needs a witness, and
the digest in that witness is *recomputed from the artefact itself* rather than
taken on the caller's word. But recomputation alone only proves "you showed me
some bytes" -- it is self-consistent, not authenticated. So a capability is only
warrantable for a digest that was registered **before the request arrived**, by
a different party, via ``register_artefact`` / ``register_digest``. That
expectation is the potential: if the agent could nominate its own acceptance
criterion there would be nothing to cross.

**One-way lock (单向锁定).** Every grant, denial, registration and redemption is
an entry in an :class:`~audit_sidecar.tools.ledger.AppendOnlyLedger`, hash-linked
to the one before it and sealed against a head anchor. A warrant additionally
records the chain position it was issued at, so rolling the log back invalidates
outstanding warrants: ``redeem`` fails with ``WARRANT_ROLLED_BACK``.

**Price of breach (违约代价).** A rejected request raises ``MembraneRejectError``
*and is written to the ledger*. A denial that left no record would make refusal
indistinguishable from a call that never happened, and "it never happened" is
exactly the story a failing agent wants told.

**One gate (统一入口).** ``ImmuneState`` is never returned, wrapped, or exposed;
only scalar summaries leave through :meth:`brake`. The five laws are computed
here, from this ledger, and returned as data.

**The honest limit.** This is an interface boundary, not a sandbox. Any code
running in this process can still ``import audit_sidecar.immune_state`` and read
it directly, and any process with write access can truncate the ledger file.
What the boundary buys is that every *sanctioned* path is gated and logged, so an
unlogged access is detectable by its absence -- the ledger's silence becomes the
evidence. Claiming more than that would be the same false confidence this
project exists to argue against.

Zero dependencies. Plain files. No database.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

from .eco_api import get_energy_gradient, get_evolution_history, get_recyclable_waste
from .ledger import (
    AppendOnlyLedger,
    LedgerCorruptError,
    LedgerError,
    sha256_bytes,
    sha256_file,
)

__all__ = [
    "CAPABILITIES",
    "MEMBRANE_LAWS",
    "MembraneGateway",
    "MembraneRejectError",
    "STRICT_CAPABILITIES",
    "Warrant",
]

#: What a caller may ask for. An unknown string is rejected rather than
#: granted: a typo must never be able to acquire rights nobody declared.
CAPABILITIES = ("read", "append", "write", "execute")

#: Capabilities the membrane refuses to issue while the brake is tripped. This
#: is the gateway's policy, sitting between L5 (which trips) and L3 (which denies
#: writes); the two are different layers and conflating them would misreport
#: which one refused.
STRICT_CAPABILITIES = frozenset({"write", "execute"})

#: The five ecological laws, as this gateway reports them.
MEMBRANE_LAWS = (
    "energy_gradient",
    "edge_to_mainstream",
    "recyclable_waste",
    "keystone_source",
    "carrying_capacity",
)

_REJECT_NO_WITNESS = "NO_WITNESS"
_REJECT_MALFORMED = "MALFORMED_WITNESS"
_REJECT_ARTEFACT_MISSING = "ARTEFACT_MISSING"
_REJECT_DIGEST_MISMATCH = "DIGEST_MISMATCH"
_REJECT_NOT_REGISTERED = "NOT_REGISTERED"
_REJECT_UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
_REJECT_BRAKE_TRIPPED = "BRAKE_TRIPPED"
_REJECT_PENALTY_CRITICAL = "PENALTY_CRITICAL"
_REJECT_LEDGER_BROKEN = "LEDGER_BROKEN"

#: The penalty ledger's worst grade. Spelled here rather than imported because
#: this package never imports ``agent_metabolism``; the two vocabularies are
#: joined by contract and the join is pinned by a test in the sidecar suite.
_PENALTY_CRITICAL = "CRITICAL"


class MembraneRejectError(Exception):
    """The membrane refused, and the reason is a code rather than prose.

    ``reason`` is stable and machine-readable so a caller can branch on it; the
    message is for the human reading the trace afterwards. Every instance raised
    by :class:`MembraneGateway` corresponds to a denial entry in the ledger --
    with the one exception of ``LEDGER_BROKEN``, where the ledger is precisely
    what could not be written.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message

    def as_dict(self) -> Dict[str, str]:
        return {"reason": self.reason, "message": self.message}


class Warrant:
    """A permission issued against one digest, at one position in the chain.

    Bound to ``(capability, digest)`` and to ``(seq, chain_head)``. The second
    pair is what makes a warrant non-retroactive: if the ledger is rolled back
    past ``seq``, :meth:`MembraneGateway.redeem` can tell, because the entry it
    expected to find there is no longer the entry that sits there.

    Single-use by default. ``uses`` is a countdown, not a flag, because a
    capability that is worth issuing once is worth being explicit about.
    """

    __slots__ = ("_capability", "_digest", "_seq", "_chain_head", "_uses")

    def __init__(
        self, capability: str, digest: str, seq: int, chain_head: str, uses: int = 1
    ) -> None:
        if uses < 1:
            raise ValueError("warrant uses must be at least 1, got {!r}".format(uses))
        self._capability = capability
        self._digest = digest
        self._seq = seq
        self._chain_head = chain_head
        self._uses = uses

    @property
    def capability(self) -> str:
        return self._capability

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def chain_head(self) -> str:
        return self._chain_head

    @property
    def uses_remaining(self) -> int:
        return self._uses

    def consume(self) -> int:
        """Spend one use; return the remainder. Raises when exhausted."""
        if self._uses < 1:
            raise MembraneRejectError(
                "WARRANT_EXHAUSTED",
                "warrant for {!r} has no uses left".format(self._capability),
            )
        self._uses -= 1
        return self._uses

    def __repr__(self) -> str:
        return "Warrant({!r}, digest={}, seq={}, uses_left={})".format(
            self._capability, self._digest[:12], self._seq, self._uses
        )


class MembraneGateway:
    """The membrane. Everything external goes through here or not at all.

    Args:
        ledger: an :class:`AppendOnlyLedger`, or a path to build one. A path is
            wrapped with a head anchor at ``<path>.head`` and ``fsync`` on,
            because an unanchored ledger cannot detect tail truncation and that
            is the failure this gateway exists to close.
        immune_state: any object answering ``is_inflamed()`` or carrying a
            ``state`` string. Duck-typed: this package never imports the
            auditor, and the auditor never imports this. The object is retained
            privately and never handed back out. L5: transient, recoverable.
        penalty_ledger: any object with a ``penalty_state`` string, in practice
            an ``agent_metabolism.penalty.PenaltyLedger``. L3's write gate: a
            ``CRITICAL`` ledger closes ``strict_capabilities`` even when the
            immune state is calm. Damage does not heal on its own the way
            inflammation does, so it is a separate input rather than a stricter
            reading of the same one.
        errata_path: optional ``ERRATA.md`` for the waste-recycling law.
        capacity: optional per-call token budget for the carrying-capacity law.
        strict_capabilities: which capabilities the brake closes.
    """

    def __init__(
        self,
        ledger: Any,
        *,
        immune_state: Any = None,
        penalty_ledger: Any = None,
        errata_path: Any = None,
        capacity: Optional[float] = None,
        strict_capabilities: Any = None,
    ) -> None:
        if isinstance(ledger, AppendOnlyLedger):
            self._ledger = ledger
        else:
            if isinstance(ledger, (str, bytes, os.PathLike)):
                path = os.fspath(ledger)
                if isinstance(path, bytes):
                    path = path.decode()
                self._ledger = AppendOnlyLedger(
                    path, anchor_path=path + ".head", fsync=True
                )
            else:
                raise TypeError(
                    "ledger must be an AppendOnlyLedger or a path, got {!r}".format(
                        type(ledger).__name__
                    )
                )

        # Two safety layers, two vocabularies, and they must not be swapped.
        # ``ImmuneState`` says NORMAL/INFLAMED; ``PenaltyLedger`` says
        # HEALTHY/WARNING/CRITICAL. Both used to carry an attribute literally
        # named ``state``, so handing a penalty ledger to ``immune_state=`` read
        # "CRITICAL" as "not INFLAMED" and reported a healthy brake at the worst
        # possible moment. The names are now distinct and the roles are checked
        # here, so the mistake is a TypeError at wiring time instead of a calm
        # answer at the wrong time.
        if penalty_ledger is not None and callable(getattr(penalty_ledger, "is_inflamed", None)):
            raise TypeError(
                "penalty_ledger looks like an immune state; pass it as "
                "immune_state= -- the two answer different questions and their "
                "state vocabularies do not overlap"
            )
        if immune_state is not None and hasattr(immune_state, "penalty_state"):
            raise TypeError(
                "immune_state looks like a PenaltyLedger (it has penalty_state); "
                "pass it as penalty_ledger= so a CRITICAL ledger is read as "
                "damage rather than as a calm brake"
            )

        self._immune = immune_state
        self._penalty = penalty_ledger
        self._errata_path = errata_path
        self._capacity = capacity
        #: capability -> set of digests registered before the request arrived.
        self._expectations: Dict[str, set] = {}
        if strict_capabilities is None:
            self._strict = set(STRICT_CAPABILITIES)
        else:
            unknown = set(strict_capabilities) - set(CAPABILITIES)
            if unknown:
                raise ValueError(
                    "unknown strict capability(ies): {}".format(", ".join(sorted(unknown)))
                )
            self._strict = set(strict_capabilities)

    # -- properties -------------------------------------------------------

    @property
    def ledger(self) -> AppendOnlyLedger:
        """The ledger, for verification only.

        Exposed because an auditor must be able to check the chain without
        going through the thing being audited. It is deliberately *not* a route
        to the immune state.
        """
        return self._ledger

    @property
    def strict_capabilities(self) -> frozenset:
        return frozenset(self._strict)

    # -- registration: where the potential difference is created ----------

    def register_artefact(self, capability: str, path: Any, *, uses: int = 1) -> str:
        """Register the digest of a physical file as acceptable evidence.

        This is the out-of-band step that gives the membrane something to
        expect. It is recorded in the ledger, so a registration cannot be
        slipped in afterwards without breaking the chain -- and its position
        tells a reader exactly when that capability became warrantable.

        Returns the digest, in ``sha256:<hex>`` form.
        """
        self._require_capability(capability)
        digest = self._digest_of_file(path)
        self._remember(capability, digest)
        self._ledger.append(
            {
                "event": "registration",
                "capability": capability,
                "digest": digest,
                "uses": uses,
            },
            record_type="registration",
        )
        self._ledger.seal()
        return digest

    def register_digest(self, capability: str, digest: str, *, uses: int = 1) -> str:
        """Register a digest directly, for artefacts that are not local files."""
        self._require_capability(capability)
        normalised = self._normalise_digest(digest)
        self._remember(capability, normalised)
        self._ledger.append(
            {
                "event": "registration",
                "capability": capability,
                "digest": normalised,
                "uses": uses,
            },
            record_type="registration",
        )
        self._ledger.seal()
        return normalised

    def _remember(self, capability: str, digest: str) -> None:
        self._expectations.setdefault(capability, set()).add(digest)

    # -- the gate ---------------------------------------------------------

    def request_action_warrant(
        self, witness_payload: Any, required_capability: str
    ) -> Warrant:
        """Ask for permission. Returns a :class:`Warrant` or raises.

        The witness must name a physical artefact and declare its digest. The
        digest is *recomputed here* from the artefact; a declared value that
        disagrees with reality is rejected as ``DIGEST_MISMATCH`` rather than
        quietly corrected, because a witness whose stated hash is wrong is not a
        witness that was checked.

        ``witness_payload`` shape::

            {"kind": "file",   "path": "...",   "digest": "sha256:<hex>"}
            {"kind": "inline", "text": "...",   "digest": "sha256:<hex>"}

        Raises:
            MembraneRejectError: always with a ``reason`` code, and always after
                writing a denial to the ledger (except when the ledger itself is
                what refused).
        """
        capability = required_capability

        # Order matters. The brake is checked before the witness is read, so a
        # closed gate does not do the expensive hashing first; the capability
        # name is checked before either, so an unknown string cannot be used to
        # probe the gate's behaviour.
        if capability not in CAPABILITIES:
            return self._reject(
                _REJECT_UNKNOWN_CAPABILITY,
                "capability {!r} is not one of {}".format(
                    capability, ", ".join(CAPABILITIES)
                ),
                capability=capability if isinstance(capability, str) else None,
            )

        if self._penalty_tripped() and capability in self._strict:
            return self._reject(
                _REJECT_PENALTY_CRITICAL,
                "the penalty ledger is CRITICAL ({!r} is closed); trust "
                "bankruptcy stops new writes even with charge to spare".format(
                    capability
                ),
                capability=capability,
            )

        if self._brake_tripped() and capability in self._strict:
            return self._reject(
                _REJECT_BRAKE_TRIPPED,
                "the saturation brake is tripped; {!r} is closed until the immune "
                "state recovers".format(capability),
                capability=capability,
            )

        if witness_payload is None:
            return self._reject(
                _REJECT_NO_WITNESS,
                "no witness submitted; {!r} requires physical evidence".format(capability),
                capability=capability,
            )

        if not isinstance(witness_payload, Mapping):
            return self._reject(
                _REJECT_MALFORMED,
                "witness must be a mapping, got {!r}".format(
                    type(witness_payload).__name__
                ),
                capability=capability,
            )

        digest, failure = self._resolve_witness(witness_payload)
        if failure is not None:
            return self._reject(failure[0], failure[1], capability=capability)

        assert digest is not None
        registered = self._expectations.get(capability, set())
        if digest not in registered:
            return self._reject(
                _REJECT_NOT_REGISTERED,
                "digest {} is not registered for {!r}; {}/{} artefact(s) are "
                "known to this membrane for that capability".format(
                    digest[:19], capability, len(registered), len(registered)
                ),
                capability=capability,
                digest=digest,
            )

        # A warrant is only as good as the chain it is bound to. Issuing one over
        # a broken ledger would hand out a permission whose proof of issue has
        # already been shown to be unreliable.
        verification = self._ledger.verify_chain()
        if verification["status"] != "PASS":
            raise MembraneRejectError(
                _REJECT_LEDGER_BROKEN,
                "refusing to issue a warrant on a ledger that does not verify "
                "({}): {}".format(verification["status"], verification["detail"]),
            )

        entry = self._ledger.append(
            {
                "event": "grant",
                "capability": capability,
                "digest": digest,
            },
            record_type="warrant",
        )
        self._ledger.seal()
        return Warrant(capability, digest, entry["seq"], entry["entry_hash"], uses=1)

    def redeem(self, warrant: Any) -> Dict[str, Any]:
        """Spend a warrant. Verifies it is still the warrant that was issued.

        Raises:
            MembraneRejectError: ``WARRANT_ROLLED_BACK`` when the ledger no
                longer holds the entry the warrant was issued against, or
                ``WARRANT_EXHAUSTED`` when its uses are gone.
        """
        if not isinstance(warrant, Warrant):
            raise TypeError(
                "warrant must be a Warrant, got {!r}".format(type(warrant).__name__)
            )
        entry = self._ledger.entry_at(warrant.seq)
        if entry is None:
            self._reject(
                "WARRANT_ROLLED_BACK",
                "warrant was issued at seq {}, which the ledger no longer "
                "holds; the log was rolled back".format(warrant.seq),
                capability=warrant.capability,
                digest=warrant.digest,
            )
        if entry.get("entry_hash") != warrant.chain_head:
            self._reject(
                "WARRANT_ROLLED_BACK",
                "the entry at seq {} is not the one the warrant was issued "
                "against; history was rewritten".format(warrant.seq),
                capability=warrant.capability,
                digest=warrant.digest,
            )

        warrant.consume()
        record = self._ledger.append(
            {
                "event": "redeem",
                "capability": warrant.capability,
                "digest": warrant.digest,
                "issued_at": warrant.seq,
            },
            record_type="warrant",
        )
        self._ledger.seal()
        return record

    # -- witness resolution ------------------------------------------------

    def _resolve_witness(self, payload: Mapping[str, Any]):
        """Recompute the digest from the artefact. Returns ``(digest, failure)``."""
        kind = payload.get("kind")
        declared = payload.get("digest")

        if kind not in ("file", "inline"):
            return None, (
                _REJECT_MALFORMED,
                "witness kind must be 'file' or 'inline', got {!r}".format(kind),
            )
        if declared is None:
            return None, (
                _REJECT_MALFORMED,
                "witness must declare the digest it is offering; an undeclared "
                "hash cannot be checked against anything",
            )

        if kind == "file":
            raw_path = payload.get("path")
            if not isinstance(raw_path, (str, bytes, os.PathLike)):
                return None, (
                    _REJECT_MALFORMED,
                    "a file witness must carry a 'path', got {!r}".format(
                        type(raw_path).__name__
                    ),
                )
            try:
                actual = self._digest_of_file(raw_path)
            except OSError as problem:
                return None, (
                    _REJECT_ARTEFACT_MISSING,
                    "the witnessed artefact cannot be read: {}".format(problem),
                )
        else:
            text = payload.get("text")
            if not isinstance(text, str):
                return None, (
                    _REJECT_MALFORMED,
                    "an inline witness must carry 'text' as a str, got {!r}".format(
                        type(text).__name__
                    ),
                )
            actual = "sha256:" + sha256_bytes(text.encode("utf-8"))

        try:
            expected = self._normalise_digest(declared)
        except ValueError as problem:
            return None, (_REJECT_MALFORMED, str(problem))

        if actual != expected:
            return None, (
                _REJECT_DIGEST_MISMATCH,
                "declared digest {} does not match the artefact, which hashes to "
                "{}".format(expected[:19], actual[:19]),
            )
        return actual, None

    @staticmethod
    def _normalise_digest(digest: Any) -> str:
        if not isinstance(digest, str):
            raise ValueError(
                "digest must be a string, got {!r}".format(type(digest).__name__)
            )
        value = digest.strip().lower()
        for prefix in ("sha256:",):
            if value.startswith(prefix):
                value = value[len(prefix):]
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("digest must be 64 hex characters, got {!r}".format(digest))
        return "sha256:" + value

    def _digest_of_file(self, path: Any) -> str:
        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if isinstance(path, bytes):
            path = path.decode()
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        return "sha256:" + sha256_file(path)

    # -- the brake --------------------------------------------------------

    def _brake_tripped(self) -> bool:
        """Read the brake without ever exposing the object behind it."""
        immune = self._immune
        if immune is None:
            return False
        probe = getattr(immune, "is_inflamed", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:  # pragma: no cover - fail closed, not open
                return True
        state = getattr(immune, "state", None)
        if isinstance(state, str):
            return state == "INFLAMED"
        return True

    def _penalty_state(self) -> Optional[str]:
        """The attached ledger's grade, read by its own name only."""
        ledger = self._penalty
        if ledger is None:
            return None
        state = getattr(ledger, "penalty_state", None)
        return state if isinstance(state, str) else None

    def _penalty_tripped(self) -> bool:
        """True when accumulated damage has closed the write gate.

        Separate from :meth:`_brake_tripped` because the two inputs are
        different questions with different lifetimes: inflammation is transient
        and heals after enough passes, whereas a ``CRITICAL`` ledger is
        long-lived damage. An unattached ledger reads as *not tripped* and is
        reported as ``attached: False``, the same treatment the brake gets --
        an absent safety layer must be visible, not inferred.
        """
        return self._penalty_state() == _PENALTY_CRITICAL

    def brake(self) -> Dict[str, Any]:
        """L5 and the penalty gate as data: scalars, never the objects."""
        attached = self._immune is not None
        state = getattr(self._immune, "state", None) if attached else None
        danger = getattr(self._immune, "danger_count", None) if attached else None
        penalty_attached = self._penalty is not None
        return {
            "attached": attached,
            "tripped": self._brake_tripped() if attached else None,
            "state": state if isinstance(state, str) else None,
            "danger_count": danger if isinstance(danger, int) else None,
            "penalty_attached": penalty_attached,
            "penalty_state": self._penalty_state(),
            "penalty_tripped": self._penalty_tripped() if penalty_attached else None,
        }

    # -- unified entry points ---------------------------------------------

    def append_denial(
        self,
        tool: str,
        reason: str,
        *,
        agent: Optional[str] = None,
        capability: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Record a refusal that happened *before* anything ran.

        Written as ``record_type="deny"``, not as a call with a ``FAIL``
        verdict, because the two are opposite evidence. "The agent was stopped"
        means the boundary held; "the agent produced something wrong" means it
        did not. Readers that count verdicts skip this record type, so a blocked
        request cannot inflate a damage counter -- while the record itself stays
        in the hash chain, so "I refused" is still provable afterwards.

        No ``verdict`` field is written at all. The lattice's four values all
        describe a judgement about work that was witnessed; a refusal is not one
        of those, and inventing a value for it would be the same false precision
        this record type exists to avoid.
        """
        payload: Dict[str, Any] = {
            "event": "denied",
            "tool": tool,
            "reason": reason,
            "outcome": "DENIED",
        }
        if agent is not None:
            payload["agent"] = agent
        if capability is not None:
            payload["capability"] = capability
        payload.update(extra)
        entry = self._ledger.append(payload, record_type="deny")
        self._ledger.seal()
        return entry

    def append_call(
        self,
        tool: str,
        verdict: Any,
        *,
        tokens_used: Optional[int] = None,
        elapsed_ms: Optional[float] = None,
        capability: Optional[str] = None,
        digest: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Write one audited call. The only sanctioned trace writer.

        Cost fields are optional but recorded when supplied, because the first
        ecological law -- the energy gradient -- is exactly the question the old
        trace could not answer. A call that reports what it cost makes the
        gradient measurable; one that does not leaves that law INVALID, which is
        the correct answer rather than a zero.
        """
        payload: Dict[str, Any] = {
            "tool": tool,
            "verdict": self._verdict_of(verdict),
        }
        if tokens_used is not None:
            payload["tokens_used"] = tokens_used
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        if capability is not None:
            payload["capability"] = capability
        if digest is not None:
            payload["digest"] = digest
        payload.update(extra)

        entry = self._ledger.append(payload, record_type="call")
        self._ledger.seal()
        return entry

    @staticmethod
    def _verdict_of(verdict: Any) -> str:
        value = getattr(verdict, "value", verdict)
        if not isinstance(value, str):
            raise TypeError(
                "verdict must be a Verdict or a str, got {!r}".format(
                    type(verdict).__name__
                )
            )
        return value

    def verify(self) -> Dict[str, Any]:
        """The chain's own verdict."""
        return self._ledger.verify_chain()

    def ecology(self) -> Dict[str, Any]:
        """The five laws, computed from this gateway's ledger.

        Every figure is derived from an artefact on disk -- the ledger, the
        errata file -- never from a cached counter. That is the point of routing
        the read-outs through the membrane: a metric nobody can recompute is a
        metric nobody can challenge.
        """
        gradient = get_energy_gradient(self._ledger.path, capacity=self._capacity)
        history = get_evolution_history(self._ledger.path)
        if self._errata_path is None:
            waste = {
                "law": "recyclable_waste",
                "status": "INVALID",
                "rules_extracted": 0,
                "recycle_rate": 0.0,
                "reason": "no errata path configured on this gateway",
            }
        else:
            waste = get_recyclable_waste(self._errata_path)

        keystone = history.get("keystone")
        capacity_block = gradient.get("capacity", {})
        returning = (
            gradient.get("capacity", {}).get("headroom")
            if isinstance(capacity_block, Mapping)
            else None
        )

        return {
            "energy_gradient": gradient,
            "edge_to_mainstream": history,
            "recyclable_waste": waste,
            "keystone_source": {
                "law": "keystone_source",
                "status": "PASS" if keystone else "INVALID",
                "value": keystone,
                "reason": (
                    "identified from witnessed PASSes in this ledger"
                    if keystone
                    else "no tool carries a witnessed PASS yet"
                ),
            },
            "carrying_capacity": {
                "law": "carrying_capacity",
                "status": "PASS" if capacity_block.get("measured") else "INVALID",
                "headroom": returning,
                "limit": capacity_block.get("limit"),
                "calls_before_refusal": capacity_block.get("calls_before_refusal"),
                "reason": capacity_block.get("reason", "not measured"),
            },
        }

    def laws_report(self) -> Dict[str, Any]:
        """A one-line-per-law summary, plus the chain's own verdict."""
        ecology = self.ecology()
        chain = self.verify()
        return {
            "chain": chain,
            "brake": self.brake(),
            "laws": [
                {
                    "law": name,
                    "status": ecology[name].get("status", "INVALID"),
                }
                for name in MEMBRANE_LAWS
            ],
            "all_measured": all(
                ecology[name].get("status") == "PASS" for name in MEMBRANE_LAWS
            ),
        }

    # -- rejection --------------------------------------------------------

    def _require_capability(self, capability: Any) -> None:
        if capability not in CAPABILITIES:
            raise ValueError(
                "capability {!r} is not one of {}".format(
                    capability, ", ".join(CAPABILITIES)
                )
            )

    def _reject(
        self,
        reason: str,
        message: str,
        *,
        capability: Optional[str] = None,
        digest: Optional[str] = None,
    ):
        """Record the denial, then raise. Never raises anything but the membrane's own."""
        payload: Dict[str, Any] = {"event": "denied", "reason": reason}
        if capability is not None:
            payload["capability"] = capability
        if digest is not None:
            payload["digest"] = digest
        try:
            self._ledger.append(payload, record_type="warrant")
            self._ledger.seal()
        except (LedgerError, OSError, ValueError) as problem:
            # The refusal still happens; it is the *record* that could not be
            # written. Raising the ledger error instead would report a storage
            # fault where a denied request was the actual event.
            message = "{} (denial could not be recorded: {})".format(message, problem)
        raise MembraneRejectError(reason, message)

    def __repr__(self) -> str:
        return (
            "MembraneGateway(ledger={!r}, brake_attached={}, "
            "penalty_attached={}, anchored={})".format(
                self._ledger.path,
                self._immune is not None,
                self._penalty is not None,
                self._ledger.anchor_path is not None,
            )
        )
