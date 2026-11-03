"""The seam between the two packages, tested rather than asserted.

The metabolic gateway reports L5 by duck-typing whatever immune state it is
handed; the sidecar exposes ``ImmuneState`` with ``is_inflamed()`` and
``state``. Neither package imports the other, which is what lets them be
installed independently -- but it also means nothing *proves* the two halves fit
unless a test does.

This file is that proof. It skips when ``agent_metabolism`` is not importable,
so the sidecar's suite never acquires a hard dependency on its companion.
"""

from __future__ import annotations

import os
import sys
import unittest

from audit_sidecar.immune_state import ImmuneState
from audit_sidecar.runner import Verdict

try:  # the companion package is optional, by design
    from agent_metabolism import EnergyAccount, MetabolicGateway
except Exception:  # pragma: no cover - exercised only where it is absent
    EnergyAccount = None
    MetabolicGateway = None

_COMPANION = MetabolicGateway is not None


@unittest.skipUnless(_COMPANION, "agent_metabolism not installed")
class TestGatewayReadsTheRealImmuneState(unittest.TestCase):
    def test_brake_starts_calm_and_trips_after_the_threshold(self):
        immune = ImmuneState(fail_threshold=2, recovery_passes=3)
        gateway = MetabolicGateway(
            account=EnergyAccount(100.0), immune_state=immune
        )

        before = gateway.get_current_laws_status()["L5_Brake"]
        self.assertTrue(before["attached"])
        self.assertFalse(before["tripped"])
        self.assertEqual(before["state"], "NORMAL")

        immune.record_verdict(Verdict.FAIL)
        immune.record_verdict(Verdict.FAIL)
        self.assertTrue(immune.is_inflamed())

        after = gateway.get_current_laws_status()["L5_Brake"]
        self.assertTrue(after["tripped"])
        self.assertEqual(after["state"], "INFLAMED")

    def test_recovery_clears_the_brake(self):
        immune = ImmuneState(fail_threshold=2, recovery_passes=2)
        gateway = MetabolicGateway(
            account=EnergyAccount(100.0), immune_state=immune
        )
        immune.record_verdict(Verdict.FAIL)
        immune.record_verdict(Verdict.FAIL)
        self.assertTrue(gateway.get_current_laws_status()["L5_Brake"]["tripped"])

        # Healing is slower than injury by design: it takes the full recovery
        # count, and one clean pass is not enough.
        immune.record_verdict(Verdict.PASS)
        self.assertTrue(gateway.get_current_laws_status()["L5_Brake"]["tripped"])
        immune.record_verdict(Verdict.PASS)
        self.assertFalse(gateway.get_current_laws_status()["L5_Brake"]["tripped"])

    def test_brake_is_reported_as_unattached_without_an_immune_state(self):
        """The absence of the brake must not read as a healthy brake."""
        brake = MetabolicGateway(
            account=EnergyAccount(100.0)
        ).get_current_laws_status()["L5_Brake"]
        self.assertFalse(brake["attached"])
        self.assertIsNone(brake["tripped"])


if __name__ == "__main__":
    unittest.main()
