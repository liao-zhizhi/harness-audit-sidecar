"""Tests for the semi-permeable membrane and its hash-chained ledger.

Three of these encode the behaviour that was asked for: an unwitnessed request is
stopped, a tampered log is caught, and a witnessed request with an intact chain
passes. The rest encode the things that make those three mean something -- most
importantly that a caller cannot nominate its own acceptance criterion, because a
gate whose threshold is set by the party being gated has no potential difference
across it.

One test exists to pin a *limitation*: truncating the tail of an unanchored
chain verifies as PASS. That is not a bug to be fixed here; it is the reason the
membrane always anchors. A test that documents a blind spot is worth more than a
docstring claiming there isn't one.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from audit_sidecar.tools.ledger import (
    AppendOnlyLedger,
    GENESIS_HASH,
    LedgerCorruptError,
    sha256_file,
)
from audit_sidecar.tools.membrane import (
    CAPABILITIES,
    MEMBRANE_LAWS,
    MembraneGateway,
    MembraneRejectError,
    Warrant,
)


class _MembraneCase(unittest.TestCase):
    """Temporary directory plus a helper for building an artefact."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.ledger_path = os.path.join(self._dir.name, "audit.jsonl")

    def make_artefact(self, name: str = "evidence.txt", body: str = "witnessed bytes\n") -> str:
        path = os.path.join(self._dir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def digest_of(self, path: str) -> str:
        return "sha256:" + sha256_file(path)

    def file_witness(self, path: str, digest: str = None) -> dict:
        return {"kind": "file", "path": path, "digest": digest or self.digest_of(path)}

    def lines(self) -> list:
        with open(self.ledger_path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()

    def rewrite(self, lines) -> None:
        with open(self.ledger_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# The three required behaviours
# ---------------------------------------------------------------------------


class TestNoWitnessIsStopped(_MembraneCase):
    def test_request_without_a_witness_is_rejected(self):
        gateway = MembraneGateway(self.ledger_path)
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant(None, "write")
        self.assertEqual(caught.exception.reason, "NO_WITNESS")

    def test_rejection_is_written_to_the_ledger(self):
        """A refusal nobody records is indistinguishable from a call that never
        happened -- and "it never happened" is the story a failing agent wants."""
        gateway = MembraneGateway(self.ledger_path)
        with self.assertRaises(MembraneRejectError):
            gateway.request_action_warrant(None, "write")
        denials = [e for e in gateway.ledger.entries() if e.get("event") == "denied"]
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0]["reason"], "NO_WITNESS")
        self.assertEqual(gateway.verify()["status"], "PASS")

    def test_malformed_witness_is_rejected(self):
        gateway = MembraneGateway(self.ledger_path)
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant({"kind": "vibes"}, "write")
        self.assertEqual(caught.exception.reason, "MALFORMED_WITNESS")

    def test_undeclared_digest_is_rejected(self):
        """An undeclared hash cannot be checked against anything, so it is not a
        commitment and not a witness."""
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact()
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant({"kind": "file", "path": path}, "write")
        self.assertEqual(caught.exception.reason, "MALFORMED_WITNESS")

    def test_unknown_capability_is_rejected_not_granted(self):
        gateway = MembraneGateway(self.ledger_path)
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant(None, "admin")
        self.assertEqual(caught.exception.reason, "UNKNOWN_CAPABILITY")

    def test_missing_artefact_is_rejected(self):
        gateway = MembraneGateway(self.ledger_path)
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant(
                {"kind": "file", "path": "/nonexistent/x", "digest": "sha256:" + "a" * 64},
                "write",
            )
        self.assertEqual(caught.exception.reason, "ARTEFACT_MISSING")


class TestTamperedLogIsCaught(_MembraneCase):
    def _ledger_with_three_entries(self) -> AppendOnlyLedger:
        ledger = AppendOnlyLedger(
            self.ledger_path, anchor_path=self.ledger_path + ".head"
        )
        for index in range(3):
            ledger.append(
                {"tool": "write_file", "verdict": "FAIL", "tokens_used": 100 * (index + 1)}
            )
        ledger.seal()
        return ledger

    def test_intact_chain_verifies(self):
        ledger = self._ledger_with_three_entries()
        result = ledger.verify_chain()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["entries"], 3)
        self.assertIsNone(result["first_bad_seq"])

    def test_editing_a_middle_entry_breaks_the_chain(self):
        ledger = self._ledger_with_three_entries()
        lines = self.lines()
        entry = json.loads(lines[1])
        entry["verdict"] = "PASS"
        lines[1] = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        self.rewrite(lines)

        result = ledger.verify_chain()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["first_bad_seq"], 2)
        self.assertIn("altered", result["detail"])

    def test_editing_the_first_entry_breaks_the_chain(self):
        ledger = self._ledger_with_three_entries()
        lines = self.lines()
        entry = json.loads(lines[0])
        entry["tokens_used"] = 1
        lines[0] = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        self.rewrite(lines)
        self.assertEqual(ledger.verify_chain()["first_bad_seq"], 1)

    def test_deleting_a_middle_entry_breaks_the_sequence(self):
        ledger = self._ledger_with_three_entries()
        lines = self.lines()
        del lines[1]
        self.rewrite(lines)
        result = ledger.verify_chain()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["first_bad_seq"], 2)

    def test_truncating_the_tail_is_caught_by_the_anchor(self):
        """The threat named in the request: delete the last line to look clean."""
        ledger = self._ledger_with_three_entries()
        self.rewrite(self.lines()[:-1])
        result = ledger.verify_chain()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("truncated", result["detail"])

    def test_unparseable_line_is_fail_not_silently_skipped(self):
        ledger = self._ledger_with_three_entries()
        lines = self.lines()
        lines[1] = "this is not json"
        self.rewrite(lines)
        self.assertEqual(ledger.verify_chain()["status"], "FAIL")

    def test_an_unreadable_tail_blocks_appending(self):
        """Extending a damaged ledger would build a chain on sand."""
        ledger = self._ledger_with_three_entries()
        lines = self.lines()
        lines[-1] = "{broken"
        self.rewrite(lines)
        with self.assertRaises(LedgerCorruptError):
            ledger.append({"tool": "x", "verdict": "PASS"})

    def test_a_missing_ledger_is_invalid_not_pass(self):
        ledger = AppendOnlyLedger(os.path.join(self._dir.name, "absent.jsonl"))
        self.assertEqual(ledger.verify_chain()["status"], "INVALID")

    def test_empty_legacy_ledger_is_invalid(self):
        ledger = AppendOnlyLedger(self.ledger_path)
        self.assertEqual(ledger.verify_chain()["status"], "INVALID")


class TestTruncationBlindSpotIsDocumented(_MembraneCase):
    """A hash chain cannot detect its own tail being cut off.

    Deleting the last entry leaves a prefix that is still a perfectly valid
    chain. This test exists so the limitation is visible in the suite rather than
    only in prose: verification PASSes, and the anchor is what makes the
    difference. The test asserts the *current* behaviour honestly, and pairs it
    with the anchored case above.
    """

    def test_without_an_anchor_truncation_still_verifies(self):
        ledger = AppendOnlyLedger(self.ledger_path)  # deliberately unanchored
        for index in range(3):
            ledger.append({"tool": "t", "verdict": "FAIL", "tokens_used": 10})
        self.rewrite(self.lines()[:-1])

        result = ledger.verify_chain()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["entries"], 2)

    def test_the_membrane_never_builds_an_unanchored_ledger(self):
        gateway = MembraneGateway(self.ledger_path)
        self.assertIsNotNone(gateway.ledger.anchor_path)


class TestWitnessedRequestPasses(_MembraneCase):
    def test_registered_artefact_with_matching_hash_is_granted(self):
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact()
        digest = gateway.register_artefact("write", path)

        warrant = gateway.request_action_warrant(self.file_witness(path, digest), "write")
        self.assertIsInstance(warrant, Warrant)
        self.assertEqual(warrant.capability, "write")
        self.assertEqual(warrant.digest, digest)
        self.assertEqual(gateway.verify()["status"], "PASS")

    def test_grant_is_recorded_before_it_is_returned(self):
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact()
        digest = gateway.register_artefact("write", path)
        warrant = gateway.request_action_warrant(self.file_witness(path, digest), "write")

        entry = gateway.ledger.entry_at(warrant.seq)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["event"], "grant")
        self.assertEqual(entry["entry_hash"], warrant.chain_head)

    def test_inline_witness_also_passes(self):
        gateway = MembraneGateway(self.ledger_path)
        text = "the command's own output"
        digest = "sha256:" + __import__("hashlib").sha256(text.encode()).hexdigest()
        gateway.register_digest("read", digest)
        warrant = gateway.request_action_warrant(
            {"kind": "inline", "text": text, "digest": digest}, "read"
        )
        self.assertEqual(warrant.capability, "read")

    def test_warrant_is_single_use(self):
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact()
        digest = gateway.register_artefact("write", path)
        warrant = gateway.request_action_warrant(self.file_witness(path, digest), "write")

        gateway.redeem(warrant)
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.redeem(warrant)
        self.assertEqual(caught.exception.reason, "WARRANT_EXHAUSTED")

    def test_rollback_invalidates_an_outstanding_warrant(self):
        """The one-way lock: a warrant is bound to a position in the chain, so
        rewinding the log retroactively voids it."""
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact()
        digest = gateway.register_artefact("write", path)
        warrant = gateway.request_action_warrant(self.file_witness(path, digest), "write")

        # Roll the ledger back to before the grant, then re-seal, as an attacker
        # with write access would.
        lines = self.lines()
        grant_position = None
        for index, line in enumerate(lines):
            if json.loads(line).get("entry_hash") == warrant.chain_head:
                grant_position = index
                break
        self.assertIsNotNone(grant_position)
        self.rewrite(lines[:grant_position])
        gateway.ledger.seal()

        with self.assertRaises(MembraneRejectError) as caught:
            gateway.redeem(warrant)
        self.assertEqual(caught.exception.reason, "WARRANT_ROLLED_BACK")


# ---------------------------------------------------------------------------
# The potential difference: the gate's threshold is not set by the caller
# ---------------------------------------------------------------------------


class TestSelfDeclaredEvidenceIsNotEnough(_MembraneCase):
    def test_agent_cannot_register_its_own_artefact_by_offering_it(self):
        """The core of the potential difference.

        Recomputing a hash proves the bytes are the bytes. It does not prove the
        membrane expected them. Until someone *else* registered the digest, the
        agent is nominating its own acceptance criterion and there is nothing to
        cross.
        """
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact("forged.txt", "whatever I felt like producing\n")
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant(self.file_witness(path), "write")
        self.assertEqual(caught.exception.reason, "NOT_REGISTERED")

    def test_declared_hash_disagreeing_with_the_artefact_is_rejected(self):
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact()
        digest = gateway.register_artefact("write", path)
        # Right capability, wrong declared digest: the membrane recomputes and
        # refuses to quietly substitute the correct value.
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant(
                {"kind": "file", "path": path, "digest": "sha256:" + "b" * 64}, "write"
            )
        self.assertEqual(caught.exception.reason, "DIGEST_MISMATCH")
        self.assertTrue(digest.startswith("sha256:"))

    def test_registration_for_one_capability_does_not_grant_another(self):
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact()
        digest = gateway.register_artefact("read", path)
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant(self.file_witness(path, digest), "write")
        self.assertEqual(caught.exception.reason, "NOT_REGISTERED")

    def test_inline_text_that_differs_from_the_registered_digest_is_rejected(self):
        gateway = MembraneGateway(self.ledger_path)
        import hashlib

        good = "the real evidence"
        gateway.register_digest("read", hashlib.sha256(good.encode()).hexdigest())
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant(
                {"kind": "inline", "text": "something else", "digest": hashlib.sha256(good.encode()).hexdigest()},
                "read",
            )
        self.assertEqual(caught.exception.reason, "DIGEST_MISMATCH")


# ---------------------------------------------------------------------------
# The brake, and the ban on reaching past the membrane
# ---------------------------------------------------------------------------


class _Inflamed:
    state = "INFLAMED"

    def __init__(self) -> None:
        self.danger_count = 3

    def is_inflamed(self) -> bool:
        return True


class _Calm:
    state = "NORMAL"
    danger_count = 0

    def is_inflamed(self) -> bool:
        return False


class TestBrakeClosesStrictCapabilities(_MembraneCase):
    def test_inflamed_state_closes_write(self):
        gateway = MembraneGateway(self.ledger_path, immune_state=_Inflamed())
        path = self.make_artefact()
        digest = gateway.register_artefact("write", path)
        with self.assertRaises(MembraneRejectError) as caught:
            gateway.request_action_warrant(self.file_witness(path, digest), "write")
        self.assertEqual(caught.exception.reason, "BRAKE_TRIPPED")

    def test_inflamed_state_leaves_read_open(self):
        gateway = MembraneGateway(self.ledger_path, immune_state=_Inflamed())
        path = self.make_artefact()
        digest = gateway.register_artefact("read", path)
        warrant = gateway.request_action_warrant(self.file_witness(path, digest), "read")
        self.assertEqual(warrant.capability, "read")

    def test_calm_state_does_not_close_write(self):
        gateway = MembraneGateway(self.ledger_path, immune_state=_Calm())
        path = self.make_artefact()
        digest = gateway.register_artefact("write", path)
        warrant = gateway.request_action_warrant(self.file_witness(path, digest), "write")
        self.assertIsInstance(warrant, Warrant)

    def test_brake_is_reported_as_scalars_and_never_as_the_object(self):
        immune = _Inflamed()
        gateway = MembraneGateway(self.ledger_path, immune_state=immune)
        brake = gateway.brake()
        self.assertEqual(brake["state"], "INFLAMED")
        self.assertTrue(brake["tripped"])
        self.assertEqual(brake["danger_count"], 3)
        for value in brake.values():
            self.assertNotIsInstance(value, _Inflamed)

    def test_unattached_brake_is_reported_as_unattached(self):
        brake = MembraneGateway(self.ledger_path).brake()
        self.assertFalse(brake["attached"])
        self.assertIsNone(brake["tripped"])

    def test_gateway_does_not_expose_the_immune_object(self):
        gateway = MembraneGateway(self.ledger_path, immune_state=_Inflamed())
        self.assertFalse(hasattr(gateway, "immune_state"))
        # The only route out is a dict of scalars.
        self.assertIsInstance(gateway.brake(), dict)


# ---------------------------------------------------------------------------
# Unified entry: calls in, laws out
# ---------------------------------------------------------------------------


class TestUnifiedEntryPoint(_MembraneCase):
    def test_append_call_writes_a_trace_shaped_record(self):
        gateway = MembraneGateway(self.ledger_path)
        entry = gateway.append_call(
            "write_file", "PASS", tokens_used=120, elapsed_ms=45.0
        )
        self.assertEqual(entry["tool"], "write_file")
        self.assertEqual(entry["verdict"], "PASS")
        self.assertEqual(entry["tokens_used"], 120)
        self.assertEqual(gateway.verify()["status"], "PASS")

    def test_ecology_reports_all_five_laws(self):
        gateway = MembraneGateway(self.ledger_path)
        ecology = gateway.ecology()
        self.assertEqual(set(ecology), set(MEMBRANE_LAWS))

    def test_energy_gradient_becomes_measurable_once_costs_are_recorded(self):
        """The old trace could not answer this law at all. Recording the cost at
        the gate is what makes it answerable."""
        gateway = MembraneGateway(self.ledger_path)
        for tokens in (100, 200, 300):
            gateway.append_call("write_file", "PASS", tokens_used=tokens, elapsed_ms=10.0)
        gradient = gateway.ecology()["energy_gradient"]
        self.assertEqual(gradient["status"], "PASS")
        self.assertEqual(gradient["tokens"]["per_call"], 200)
        self.assertEqual(gradient["tokens"]["trend"], "rising")

    def test_bookkeeping_entries_are_not_counted_as_corrupt_trace_lines(self):
        gateway = MembraneGateway(self.ledger_path)
        path = self.make_artefact()
        digest = gateway.register_artefact("write", path)
        gateway.append_call("write_file", "PASS", tokens_used=50)
        with self.assertRaises(MembraneRejectError):
            gateway.request_action_warrant(None, "write")

        history = gateway.ecology()["edge_to_mainstream"]
        self.assertEqual(history["status"], "PASS")
        self.assertEqual(history["skipped_lines"], 0)
        self.assertGreaterEqual(history["bookkeeping"], 2)
        self.assertTrue(digest.startswith("sha256:"))

    def test_laws_report_reports_the_chain_and_each_law(self):
        gateway = MembraneGateway(self.ledger_path)
        gateway.append_call("write_file", "PASS", tokens_used=10)
        report = gateway.laws_report()
        self.assertEqual(report["chain"]["status"], "PASS")
        self.assertEqual([row["law"] for row in report["laws"]], list(MEMBRANE_LAWS))
        self.assertFalse(report["all_measured"])

    def test_verdict_object_is_accepted(self):
        from audit_sidecar.verdict import Verdict

        gateway = MembraneGateway(self.ledger_path)
        entry = gateway.append_call("shell", Verdict.FAIL)
        self.assertEqual(entry["verdict"], "FAIL")


# ---------------------------------------------------------------------------
# ledger primitives
# ---------------------------------------------------------------------------


class TestLedgerPrimitives(_MembraneCase):
    def test_first_entry_links_to_the_genesis_hash(self):
        ledger = AppendOnlyLedger(self.ledger_path)
        entry = ledger.append({"tool": "t", "verdict": "PASS"})
        self.assertEqual(entry["prev_hash"], GENESIS_HASH)
        self.assertEqual(entry["seq"], 1)

    def test_reserved_fields_cannot_be_set_by_a_payload(self):
        ledger = AppendOnlyLedger(self.ledger_path)
        for field in ("seq", "prev_hash", "entry_hash"):
            with self.assertRaises(ValueError):
                ledger.append({field: "x"})

    def test_payload_must_be_a_mapping(self):
        ledger = AppendOnlyLedger(self.ledger_path)
        with self.assertRaises(TypeError):
            ledger.append(["not", "a", "mapping"])

    def test_nan_is_refused_because_it_would_not_reproduce(self):
        ledger = AppendOnlyLedger(self.ledger_path)
        with self.assertRaises(ValueError):
            ledger.append({"score": float("nan")})

    def test_seal_requires_an_anchor_path(self):
        ledger = AppendOnlyLedger(self.ledger_path)
        with self.assertRaises(Exception):
            ledger.seal()

    def test_head_is_read_from_disk_so_a_second_writer_is_noticed(self):
        first = AppendOnlyLedger(self.ledger_path)
        first.append({"tool": "a", "verdict": "PASS"})
        second = AppendOnlyLedger(self.ledger_path)
        second.append({"tool": "b", "verdict": "PASS"})
        # The first object must not append a conflicting seq 2 from a stale head.
        third = first.append({"tool": "c", "verdict": "PASS"})
        self.assertEqual(third["seq"], 3)
        self.assertEqual(first.verify_chain()["status"], "PASS")

    def test_entries_are_readable_in_order(self):
        ledger = AppendOnlyLedger(self.ledger_path)
        for index in range(3):
            ledger.append({"tool": "t{}".format(index), "verdict": "PASS"})
        self.assertEqual(
            [entry["tool"] for entry in ledger.entries()], ["t0", "t1", "t2"]
        )

    def test_len_counts_entries(self):
        ledger = AppendOnlyLedger(self.ledger_path)
        self.assertEqual(len(ledger), 0)
        ledger.append({"tool": "t", "verdict": "PASS"})
        self.assertEqual(len(ledger), 1)


class TestCapabilityVocabulary(unittest.TestCase):
    def test_strict_capabilities_are_a_subset_of_the_vocabulary(self):
        from audit_sidecar.tools.membrane import STRICT_CAPABILITIES

        self.assertTrue(STRICT_CAPABILITIES.issubset(set(CAPABILITIES)))


if __name__ == "__main__":
    unittest.main()
