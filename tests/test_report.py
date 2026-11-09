"""``--upload``: three fields leave, and a display failure is not a verdict.

The privacy claim is only as good as the payload, so these tests inspect the
JSON that would be sent rather than the code that builds it. The second claim --
"a dashboard being down never changes the exit code" -- is tested through the
CLI, because that is where the exit code is actually decided.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

import audit_sidecar.cli as cli
import audit_sidecar.report as report
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


class CliUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = self._tmp.name

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
        ) as upload:
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

    def test_verbose_and_json_still_work_with_upload(self):
        with mock.patch.object(report, "upload", return_value=(200, {"recorded": True})):
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
