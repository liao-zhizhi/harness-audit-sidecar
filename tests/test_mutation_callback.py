"""Is the D4 mutation callback actually wired, end to end?

This test exists because "the sidecar reports mutations" is a claim with three
separate ways to be false, and only an integration test catches all three:

1. the sidecar may decide a verdict is not a mutation (a mapping bug);
2. it may decide correctly and never POST (a wiring bug);
3. it may POST a shape the server rejects (a protocol bug).

Unit-testing ``mutation_kind`` catches the first. None of them catch 2 and 3
together, so this file runs a **real HTTP server** backed by a **real
``CelGateway``** -- the same engine the product serves -- and drives the actual
``report_execution`` against it. Nothing is mocked except the network address.

Requires ``cel_engine`` on ``sys.path``; skips without it so the sidecar keeps
installing alone. The skip is the honest outcome: a sidecar with no cloud cannot
demonstrate a callback into one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The product lives in a sibling checkout; both layouts are tried because the
# two repositories are developed side by side but deployed independently.
_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "agent-metabolism-os" / "product_mvp",
    Path(__file__).resolve().parents[1] / "product_mvp",
]
for _candidate in _CANDIDATES:
    if (_candidate / "cel_engine.py").is_file() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

try:
    import cel_engine
    from cel_engine import CelGateway
except Exception:  # pragma: no cover - exercised only where the cloud is absent
    cel_engine = None
    CelGateway = None

from audit_sidecar.cloud_report import (  # noqa: E402
    MUTATION_TYPES as SIDECAR_TYPES,
    mutation_kind,
    report_execution,
)
from audit_sidecar.runner import ExecutionResult  # noqa: E402
from audit_sidecar.verdict import Verdict  # noqa: E402

_HAVE_CLOUD = CelGateway is not None


class _CloudHandler(BaseHTTPRequestHandler):
    """The two routes the reporting path uses, over a real gateway.

    Deliberately thin: it forwards the posted body to the engine and returns what
    the engine said. Re-implementing the judgement here would let a broken
    callback pass against a stub that agrees with it.
    """

    gateway = None  # set by the fixture
    seen = None  # request log, for asserting *that* a POST happened

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        type(self).seen.append((self.path, body))

        try:
            if self.path == "/api/v1/verify":
                payload = dict(body)
                payload.setdefault("witness_hash", None)
                receipt = type(self).gateway.verify(**payload)
                self._reply(200, receipt, ledger_written=True)
            elif self.path == "/api/v1/mutation":
                event = type(self).gateway.record_mutation(
                    body.get("agent_id", ""),
                    body.get("mutation_type", ""),
                    body.get("detected"),
                    cycle=body.get("cycle"),
                    penalty=float(body.get("penalty") or 0.0),
                )
                estimate = type(self).gateway.estimate_mutation_rate(
                    body.get("agent_id")
                )
                self._reply(
                    200,
                    {
                        "recorded": True,
                        "event": event,
                        "sealed": False,
                        "mu": estimate["mu"],
                        "censoring": estimate["censoring"],
                    },
                )
            else:
                self._reply(404, {"detail": "no such route"})
        except Exception as problem:  # noqa: BLE001 - surfaced as a status
            self._reply(
                422,
                {"detail": "{}: {}".format(type(problem).__name__, problem),
                 "ledger_written": False},
            )

    def _reply(self, status: int, body: dict, **extra) -> None:
        merged = dict(body)
        merged.update(extra)
        raw = json.dumps(merged).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:  # keep the test output clean
        return


@unittest.skipUnless(_HAVE_CLOUD, "cel_engine not importable")
class MutationCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        _CloudHandler.gateway = CelGateway()
        _CloudHandler.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.cloud = "http://{}:{}".format(host, port)
        self.gateway = _CloudHandler.gateway
        # A PASS must carry a digest, and the digest must come from a real file:
        # ``report_execution`` refuses to claim success without one, which is the
        # behaviour under test elsewhere. The fixture therefore has to produce a
        # file, not a hash of a string nobody hashed.
        self._tmp = tempfile.TemporaryDirectory(prefix="sidecar-mutation-")
        self.artefact = str(Path(self._tmp.name) / "hello.txt")
        with open(self.artefact, "w", encoding="utf-8") as handle:
            handle.write("hello")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    # -- a false completion is a mutation ---------------------------------

    def test_a_false_completion_is_recorded_and_readable_as_mu(self) -> None:
        """The whole chain: verdict -> POST -> MutationEvent -> mu.

        The agent claims the work landed, the witness says nothing did. That is
        the D4 mutant, and after this call ``estimate_mutation_rate`` must see it
        with the execution in the denominator -- which is only true if the
        mutation was posted *after* the verify.
        """
        result = ExecutionResult(
            intent="create hello.txt",
            effect_observed=False,
            check_observed=True,
            verdict=Verdict.FAIL,
            command="false",
            exit_code=1,
        )

        report = report_execution(
            result,
            cloud=self.cloud,
            agent_id="liar",
            intent="create hello.txt",
        )

        self.assertTrue(report.posted)
        self.assertTrue(report.accepted)
        self.assertEqual(report.verdict, "FAIL")
        self.assertEqual(report.mutation_type, "witness_forgery")
        self.assertTrue(report.mutation_recorded, report.mutation_error)

        estimate = self.gateway.estimate_mutation_rate("liar")
        self.assertEqual(estimate["mu"], 1.0)
        self.assertEqual(estimate["intercepted"], 1)
        self.assertEqual(estimate["executions"], 1)
        self.assertEqual(estimate["censoring"], "lower-bound")
        self.assertEqual(report.mutation_body.get("mu"), 1.0)

    def test_mu_is_a_ratio_of_two_different_counts(self) -> None:
        """One forgery among three executions is 1/3, not 1/1.

        If the mutation had been posted before the verify, the denominator would
        lag by one and this would read 0.5 -- the off-by-one that makes mu drift
        upward for busy agents.
        """
        for _ in range(2):
            honest = ExecutionResult(
                intent="ok", effect_observed=True, check_observed=True,
                verdict=Verdict.PASS, exit_code=0,
            )
            report_execution(
                honest,
                cloud=self.cloud,
                agent_id="mixed",
                intent="ok",
                artefact_path=self.artefact,
            )
        liar = ExecutionResult(
            intent="bad", effect_observed=False, check_observed=True,
            verdict=Verdict.FAIL, command="false", exit_code=1,
        )
        report = report_execution(
            liar, cloud=self.cloud, agent_id="mixed", intent="bad"
        )
        self.assertTrue(report.mutation_recorded)

        estimate = self.gateway.estimate_mutation_rate("mixed")
        self.assertAlmostEqual(estimate["mu"], 1.0 / 3.0, places=6)
        self.assertEqual(estimate["executions"], 3)

    def test_an_honest_pass_writes_nothing(self) -> None:
        honest = ExecutionResult(
            intent="ok", effect_observed=True, check_observed=True,
            verdict=Verdict.PASS, exit_code=0,
        )
        report = report_execution(
            honest,
            cloud=self.cloud,
            agent_id="honest",
            intent="ok",
            artefact_path=self.artefact,
        )
        self.assertIsNone(report.mutation_type)
        self.assertFalse(report.mutation_recorded)
        # 0.0, not None: the denominator exists (one booked execution), so "no
        # mutation was caught" is a rate of zero rather than absent data. The
        # distinction was pinned in the server's tests and holds here too.
        estimate = self.gateway.estimate_mutation_rate("honest")
        self.assertEqual(estimate["mu"], 0.0)
        self.assertEqual(estimate["executions"], 1)
        self.assertEqual(
            [path for path, _ in _CloudHandler.seen], ["/api/v1/verify"]
        )

    def test_an_under_claim_is_not_a_mutation(self) -> None:
        """The effect landed and the agent said it failed -- dishonest, not evasive.

        Counting it would make mu a measure of reporting mistakes, and the D4
        number would stop being about forgery.
        """
        result = ExecutionResult(
            intent="x", effect_observed=True, check_observed=False,
            verdict=Verdict.FAIL, exit_code=0,
        )
        report = report_execution(
            result,
            cloud=self.cloud,
            agent_id="modest",
            intent="x",
            artefact_path=self.artefact,
        )
        self.assertIsNone(report.mutation_type)
        self.assertEqual(
            [path for path, _ in _CloudHandler.seen], ["/api/v1/verify"]
        )

    def test_an_unbooked_failure_records_no_mutation(self) -> None:
        """A refused submission is not an execution, so it cannot be in mu.

        Booking the mutation anyway would put one in the numerator with nothing
        in the denominator: mu would climb every time an agent was turned away at
        the door, which is the censoring inversion arriving through the front.
        """
        # fail_limit=2 (the minimum the core accepts: "the first failure is a
        # charge, the second consecutive one is a stop"). Two judged FAILs close
        # the book; the third submission is refused at the door.
        self.gateway = CelGateway(fail_limit=2)
        _CloudHandler.gateway = self.gateway
        liar = ExecutionResult(
            intent="bad", effect_observed=False, check_observed=True,
            verdict=Verdict.FAIL, command="false", exit_code=1,
        )
        first = report_execution(
            liar, cloud=self.cloud, agent_id="sealed", intent="bad"
        )
        self.assertTrue(first.mutation_recorded)
        second = report_execution(
            liar, cloud=self.cloud, agent_id="sealed", intent="bad once more"
        )
        self.assertTrue(second.accepted)
        self.assertTrue(second.mutation_recorded)

        # The book is now closed; this one never becomes an execution.
        refused = report_execution(
            liar, cloud=self.cloud, agent_id="sealed", intent="bad again"
        )
        self.assertFalse(refused.accepted)
        self.assertFalse(refused.mutation_recorded)
        self.assertIn("no execution to divide by", refused.mutation_error)

        estimate = self.gateway.estimate_mutation_rate("sealed")
        self.assertEqual(estimate["intercepted"], 2)
        self.assertEqual(estimate["executions"], 2)
        self.assertEqual(estimate["mu"], 1.0)


class VocabularyAgreementTests(unittest.TestCase):
    """The sidecar's copy of the mutation vocabulary must match the server's.

    It is a copy because the sidecar has to install without the server. Copies
    drift, so one of them is checked against the other -- when both are present,
    which is the only time the comparison is meaningful.
    """

    @unittest.skipUnless(_HAVE_CLOUD, "cel_engine not importable")
    def test_the_two_lists_are_identical(self) -> None:
        self.assertEqual(tuple(SIDECAR_TYPES), tuple(cel_engine.MUTATION_TYPES))


class PropagationIsHonestlyAbsentTests(unittest.TestCase):
    """D6's callback does not exist, and the package says so in code.

    A missing callback is easy to paper over with a stub that returns zeros --
    and zero false edges is exactly what a healthy fleet looks like. So the
    absence is a named constant and a test, not a paragraph someone skips.
    """

    def test_the_flag_is_false(self) -> None:
        from audit_sidecar.cloud_report import PROPAGATION_REPORTING

        self.assertFalse(PROPAGATION_REPORTING)

    def test_the_capability_record_explains_the_gap(self) -> None:
        from audit_sidecar.cloud_report import propagation_capability

        record = propagation_capability()
        self.assertFalse(record["supported"])
        self.assertEqual(record["table"], "PropagationEdge")
        self.assertIn("no agent-to-agent channel", record["reason"])
        self.assertIn("r0_source stays", record["consequence"])

    def test_no_propagation_request_is_ever_made(self) -> None:
        """The reporting path must not POST to a route it cannot populate.

        Checked by name rather than by inspecting the body: a future version that
        starts sending edges should fail here and be made to delete this test
        deliberately, instead of quietly gaining a caller.
        """
        import inspect

        from audit_sidecar import cloud_report

        source = inspect.getsource(cloud_report)
        self.assertNotIn("/api/v1/propagation", source)


class MutationKindTests(unittest.TestCase):
    """Pure mapping, no network. Pinned because every branch is a judgement."""

    @staticmethod
    def _result(effect, check, verdict, check_source="exit-status", exit_code=0):
        return ExecutionResult(
            intent="x",
            effect_observed=effect,
            check_observed=check,
            verdict=verdict,
            exit_code=exit_code,
            check_source=check_source,
        )

    def test_false_completion_is_witness_forgery(self) -> None:
        self.assertEqual(
            mutation_kind(self._result(False, True, Verdict.FAIL)),
            "witness_forgery",
        )

    def test_pass_invalid_and_not_run_are_never_mutations(self) -> None:
        for verdict in (Verdict.PASS, Verdict.INVALID, Verdict.NOT_RUN):
            with self.subTest(verdict=verdict):
                self.assertIsNone(mutation_kind(self._result(None, True, verdict)))

    def test_an_unknown_override_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            mutation_kind(
                self._result(False, True, Verdict.FAIL), override="made_up_kind"
            )

    def test_an_override_is_honoured_when_it_is_in_the_vocabulary(self) -> None:
        self.assertEqual(
            mutation_kind(
                self._result(False, True, Verdict.FAIL), override="code_injection"
            ),
            "code_injection",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
