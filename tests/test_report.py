"""``--upload``: three fields leave, and a display failure is not a verdict.

The privacy claim is only as good as the payload, so these tests inspect the
JSON that would be sent rather than the code that builds it. The second claim --
"a dashboard being down never changes the exit code" -- is tested through the
CLI, because that is where the exit code is actually decided.
"""

from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

# These tests drive the CLI, and the CLI now writes an append-only usage log.
# Point it at a throwaway directory so running the suite never touches the
# developer's ~/.gradia/usage.jsonl -- a test that edits the machine it runs on
# is a test that has to be undone by hand.
_USAGE_DIR = tempfile.mkdtemp(prefix="harness-usage-")
os.environ["HARNESS_AUDIT_USAGE_DIR"] = _USAGE_DIR
atexit.register(shutil.rmtree, _USAGE_DIR, True)

import audit_sidecar.cli as cli
import audit_sidecar.report as report
import audit_sidecar.usage as usage
from audit_sidecar.check import CheckOutcome
from audit_sidecar.verdict import Verdict


def _outcome(declaration: str, reason: str, verdict=Verdict.FAIL) -> CheckOutcome:
    return CheckOutcome(
        verdict=verdict,
        reason=reason,
        next_step="",
        declaration=declaration,
        witness_type="file_digest",
    )


class PayloadTests(unittest.TestCase):
    def test_the_payload_has_exactly_three_fields(self):
        payload = report.build_payload(
            _outcome("file /tmp/app/main.py", "/tmp/app/main.py exists")
        )

        # Not ``to_dict()`` minus fields: a new trace field must not become a
        # new upload field by default.
        self.assertEqual(set(payload), {"verdict", "reason", "check"})

    def test_a_file_declaration_does_not_send_the_path(self):
        payload = report.build_payload(
            _outcome("file /home/me/secret-project/src/main.py", "no recording")
        )

        self.assertEqual(payload["check"], "file <redacted>")
        self.assertNotIn("secret-project", json.dumps(payload))
        self.assertNotIn("/home/me", json.dumps(payload))

    def test_a_command_declaration_does_not_send_the_command(self):
        payload = report.build_payload(
            _outcome('command curl -H "Authorization: token xyz" https://api', "exited 0")
        )

        self.assertEqual(payload["check"], "command <redacted>")
        self.assertNotIn("token", json.dumps(payload))

    def test_absolute_paths_inside_the_reason_are_redacted(self):
        payload = report.build_payload(
            _outcome(
                "file /tmp/abc/main.py",
                "/tmp/abc/main.py exists, but /tmp/abc has no recording",
                verdict=Verdict.INVALID,
            )
        )

        self.assertNotIn("/tmp/abc", json.dumps(payload))
        self.assertIn("<path>", payload["reason"])

    def test_a_relative_declaration_keeps_its_shape(self):
        payload = report.build_payload(_outcome("file src/main.py", "no recording"))

        self.assertEqual(payload["check"], "file <redacted>")
        self.assertNotIn("src/main.py", json.dumps(payload))

    def test_diff_has_no_argument_to_redact(self):
        payload = report.build_payload(_outcome("diff", "working tree changed"))
        self.assertEqual(payload["check"], "diff")

    def test_no_declaration_is_reported_as_such(self):
        payload = report.build_payload(
            _outcome("(none)", "no declaration given", verdict=Verdict.INVALID)
        )
        self.assertEqual(payload["check"], "(none)")


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UploadTransportTests(unittest.TestCase):
    def _capture_request(self, status=200, body=b'{"recorded": true, "verdict": "FAIL"}'):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["timeout"] = timeout
            return FakeResponse(status, body)

        return captured, mock.patch("urllib.request.urlopen", fake_urlopen)

    def test_the_post_targets_the_report_path_with_exactly_three_fields(self):
        captured, patch = self._capture_request()
        with patch:
            status, body = report.upload(
                _outcome("file /tmp/secret/main.py", "/tmp/secret/main.py exists"),
                "http://localhost:8000",
            )

        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])
        self.assertEqual(captured["url"], "http://localhost:8000/api/v1/check/report")
        sent = json.loads(captured["body"])
        self.assertEqual(set(sent), {"verdict", "reason", "check"})
        self.assertNotIn("/tmp/secret", captured["body"].decode("utf-8"))

    def test_an_explicit_report_url_is_not_doubled(self):
        self.assertEqual(
            report.resolve_url("https://dash.example/api/v1/check/report"),
            "https://dash.example/api/v1/check/report",
        )
        self.assertEqual(
            report.resolve_url("https://dash.example/"),
            "https://dash.example/api/v1/check/report",
        )

    def test_the_environment_supplies_the_base_url(self):
        with mock.patch.dict(os.environ, {report.UPLOAD_URL_ENV: "https://dash.example"}):
            self.assertEqual(
                report.resolve_url(), "https://dash.example/api/v1/check/report"
            )

    def test_a_failed_post_is_reported_and_does_not_raise(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            status, body = report.upload(_outcome("diff", "x"), "http://127.0.0.1:1")

        self.assertIsNone(status)
        self.assertIn("no route", body["error"])

    def test_the_default_is_the_public_dashboard(self):
        # A localhost default only works on the author's machine; the deployed
        # host is the one an install actually has a chance of reaching.
        self.assertEqual(
            report.DEFAULT_BASE_URL, "https://audit.silicon-probe.com"
        )
        with mock.patch.dict(os.environ, {report.UPLOAD_URL_ENV: ""}):
            self.assertEqual(
                report.resolve_url(),
                "https://audit.silicon-probe.com/api/v1/check/report",
            )

    def test_the_environment_overrides_the_public_default(self):
        with mock.patch.dict(os.environ, {report.UPLOAD_URL_ENV: "https://mine.example"}):
            self.assertEqual(
                report.resolve_url(), "https://mine.example/api/v1/check/report"
            )

    def test_an_api_key_is_sent_only_when_given(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["key"] = request.get_header("X-api-key")
            return FakeResponse(200, b'{"recorded": true}')

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            report.upload(_outcome("diff", "x"), "http://dash")
            self.assertIsNone(captured["key"])
            report.upload(_outcome("diff", "x"), "http://dash", api_key="k-123")
            self.assertEqual(captured["key"], "k-123")

    def test_the_summary_line_distinguishes_recorded_from_ignored(self):
        self.assertIn(
            "recorded", report.summary_line(200, {"recorded": True, "verdict": "FAIL"})
        )
        self.assertIn(
            "not recorded",
            report.summary_line(200, {"recorded": False, "reason": "a PASS is not an interception"}),
        )
        self.assertIn("rejected", report.summary_line(429, {"detail": "slow down"}))
        self.assertIn("not sent", report.summary_line(None, {"error": "down"}))


class ProbeTests(unittest.TestCase):
    """The probe reads a status the way a router means it, not as a success flag.

    This is the part that would silently be wrong: a healthy FastAPI deployment
    answers ``OPTIONS`` on a POST-only route with **405**, so "non-2xx means not
    deployed" would refuse every correct host. Both directions are pinned here.
    """

    def setUp(self) -> None:
        report.reset_probe_cache()
        self.addCleanup(report.reset_probe_cache)

    def _probe(self, outcome):
        """``outcome`` is a response, an exception, or an (status, allow) pair."""
        import email.message
        import urllib.error

        def fake_urlopen(request, timeout=None):
            self.method = request.get_method()
            self.url = request.full_url
            if isinstance(outcome, BaseException):
                raise outcome
            status, allow = outcome
            if status >= 400:
                headers = email.message.Message()
                if allow:
                    headers["Allow"] = allow
                raise urllib.error.HTTPError(
                    request.full_url, status, "no", headers, None
                )
            return FakeResponse(status, b"{}")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            return report.probe("https://dash.example", cache=False)

    def test_a_post_only_route_is_probed_as_present(self):
        # Exactly what product_mvp answers: 405 + `Allow: POST`.
        result = self._probe((405, "POST"))
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(self.method, "OPTIONS")
        self.assertEqual(self.url, "https://dash.example/api/v1/check/report")

    def test_a_missing_route_is_probed_as_absent(self):
        result = self._probe((404, ""))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 404)

    def test_an_ungated_answer_is_present(self):
        self.assertTrue(self._probe((200, "")).ok)
        self.assertTrue(self._probe((204, "")).ok)

    def test_a_gated_route_is_present_because_the_post_will_explain_it(self):
        self.assertTrue(self._probe((401, "")).ok)
        self.assertTrue(self._probe((403, "")).ok)

    def test_a_server_error_is_not_reported_as_not_deployed(self):
        result = self._probe((503, ""))
        self.assertFalse(result.ok)
        self.assertIn("503", report.failure_message("https://dash.example", result))

    def test_no_answer_names_the_host_and_the_flag(self):
        result = self._probe(OSError("connection refused"))
        self.assertFalse(result.ok)
        message = report.failure_message("https://dash.example", result)
        self.assertIn("cannot reach https://dash.example", message)
        self.assertIn("--upload-url <your-instance>", message)

    def test_the_missing_route_message_is_the_two_line_remediation(self):
        message = report.failure_message(
            "https://audit.silicon-probe.com", self._probe((404, ""))
        )
        self.assertEqual(
            message,
            "upload failed: https://audit.silicon-probe.com does not expose "
            "/api/v1/check/report.\n"
            "Either deploy the latest product_mvp to that host, or pass "
            "--upload-url <your-instance>.",
        )

    def test_the_two_upload_paths_agree_on_the_default_host(self):
        # The verdict POST and the usage aggregate are two requests to one
        # deployment. Two defaults that drift would send half the traffic to a
        # host nobody deployed -- silently, because both calls swallow errors.
        self.assertEqual(report.DEFAULT_BASE_URL, usage.DEFAULT_BASE_URL)
        self.assertEqual(
            report.PUBLIC_DASHBOARD, "https://audit.silicon-probe.com"
        )
        self.assertEqual(
            usage.resolve_url(),
            "https://audit.silicon-probe.com/api/v1/usage/report",
        )

    def test_the_probe_is_cached_per_host(self):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            return FakeResponse(200, b"{}")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            report.probe("https://dash.example")
            report.probe("https://dash.example")

        self.assertEqual(calls, ["https://dash.example/api/v1/check/report"])

    def test_the_announcement_names_the_public_dashboard_once(self):
        first = report.announcement("https://audit.silicon-probe.com")
        self.assertEqual(
            first,
            "upload: posting to public dashboard https://audit.silicon-probe.com "
            "(redacted payload)",
        )
        # Once per process: the second POST is not a second surprise.
        self.assertIsNone(report.announcement("https://audit.silicon-probe.com"))

    def test_a_private_host_is_not_called_public(self):
        line = report.announcement("https://mine.example")
        self.assertIn("https://mine.example", line)
        self.assertNotIn("public dashboard", line)


class CliUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = self._tmp.name

        # This class is about the CLI's orchestration, not the socket. A test
        # that reaches the internet is a test that fails on a train, so the
        # probe is stubbed to "the route exists" unless a test says otherwise.
        report.reset_probe_cache()
        self.addCleanup(report.reset_probe_cache)
        self.probe = report.Probe(True, 405, "route exists")
        patcher = mock.patch.object(report, "probe", side_effect=lambda *a, **k: self.probe)
        self.probe_call = patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run_check(argv)
        return code, out.getvalue(), err.getvalue()

    def test_upload_is_off_by_default(self):
        with mock.patch.object(report, "upload") as upload:
            code, _, _ = self._run(["--diff", "--cwd", self.cwd])

        self.assertEqual(code, 3)  # no baseline: INVALID, unchanged behaviour
        upload.assert_not_called()

    def test_a_reachable_dashboard_is_named_in_the_stderr_line(self):
        with mock.patch.object(
            report, "upload", return_value=(200, {"recorded": True, "verdict": "INVALID"})
        ) as upload, mock.patch.object(usage, "upload_aggregate", return_value=(200, {})):
            code, out, err = self._run(
                ["--diff", "--cwd", self.cwd, "--upload", "--upload-url", "http://dash"]
            )

        self.assertEqual(code, 3)
        upload.assert_called_once()
        self.assertIn("upload: recorded", err)
        # The verdict stays the first line of stdout: a CI step parses it, and
        # the upload line must not get in front of it.
        self.assertEqual(out.splitlines()[0], "INVALID")

    def test_an_unreachable_dashboard_does_not_change_the_exit_code(self):
        with mock.patch.object(report, "upload", return_value=(None, {"error": "down"})):
            code, out, err = self._run(["--diff", "--cwd", self.cwd, "--upload"])

        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[0], "INVALID")
        self.assertIn("upload: not sent", err)

    def test_the_first_post_announces_the_public_dashboard(self):
        with mock.patch.object(
            report, "upload", return_value=(200, {"recorded": True, "verdict": "INVALID"})
        ), mock.patch.object(usage, "upload_aggregate", return_value=(200, {})):
            _code, _out, err = self._run(["--diff", "--cwd", self.cwd, "--upload"])

        self.assertIn(
            "upload: posting to public dashboard https://audit.silicon-probe.com "
            "(redacted payload)",
            err,
        )

    def test_a_missing_endpoint_posts_nothing_and_says_so(self):
        self.probe = report.Probe(False, 404, "no route")
        with mock.patch.object(report, "upload") as upload, mock.patch.object(
            usage, "upload_aggregate"
        ) as aggregate:
            code, out, err = self._run(
                ["--diff", "--cwd", self.cwd, "--upload", "--upload-url", "https://audit.silicon-probe.com"]
            )

        # The verdict is local, so it is unchanged; the *absence* of an upload
        # is reported rather than dressed up as one.
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[0], "INVALID")
        self.assertIn(
            "upload failed: https://audit.silicon-probe.com does not expose "
            "/api/v1/check/report.",
            err,
        )
        self.assertIn(
            "Either deploy the latest product_mvp to that host, or pass "
            "--upload-url <your-instance>.",
            err,
        )
        self.assertNotIn("upload: recorded", err)
        self.assertNotIn("upload: not sent", err)
        upload.assert_not_called()
        aggregate.assert_not_called()

    def test_the_probe_is_asked_before_the_first_post(self):
        self.probe = report.Probe(True, 405, "route exists")
        with mock.patch.object(report, "upload", return_value=(200, {"recorded": True})), \
             mock.patch.object(usage, "upload_aggregate", return_value=(200, {})):
            self._run(["--diff", "--cwd", self.cwd, "--upload"])

        self.assertEqual(self.probe_call.call_count, 1)

    def test_verbose_and_json_still_work_with_upload(self):
        with mock.patch.object(report, "upload", return_value=(200, {"recorded": True})), \
             mock.patch.object(usage, "upload_aggregate", return_value=(200, {})):
            code, out, _ = self._run(
                ["--diff", "--cwd", self.cwd, "--upload", "--json"]
            )

        self.assertEqual(code, 3)
        parsed = json.loads(out)
        self.assertEqual(parsed["verdict"], "INVALID")
        # The upload result is not folded into the trace: --json stays the
        # verdict document it was before the flag existed.
        self.assertNotIn("upload", parsed)


if __name__ == "__main__":
    unittest.main()
