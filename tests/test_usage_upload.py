"""``--upload``: the verdict report and the aggregate, and what must not leave.

Two requests on ``--upload``, and they carry different things:

* ``/api/v1/check/report`` -- this one verdict (already covered in
  ``test_report``);
* ``/api/v1/usage/report`` -- three aggregate fields: ``verdicts``,
  ``baseline_misses``, ``tool_names``.

The test that matters is the negative one: the usage **rows** -- with their
timestamps and path hashes -- must never be in either body. That is asserted on
the bytes that would go on the wire, not on the builder function, because the
builder is what a future edit would change.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audit_sidecar.cli as cli
import audit_sidecar.report as report
import audit_sidecar.usage as usage


class _Recorder:
    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(
            {
                "url": request.full_url,
                "body": json.loads(request.data.decode("utf-8")),
            }
        )
        return _Response()

    def by_path(self, suffix):
        return [item for item in self.requests if item["url"].endswith(suffix)]


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b'{"verdict": "INVALID", "runs": 1}'


class AggregateUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patch = mock.patch.dict(os.environ, {usage.USAGE_DIR_ENV: str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        # The probe is about the socket, not about the aggregate. Stubbed, and
        # the per-process memo cleared, so one test's verdict about a host
        # cannot decide the next test's.
        report.reset_probe_cache()
        self.addCleanup(report.reset_probe_cache)
        probe = mock.patch.object(
            report, "probe", return_value=report.Probe(True, 405, "route exists")
        )
        probe.start()
        self.addCleanup(probe.stop)
        # Two check events and one tool call, so the aggregate has something in
        # every field and the "rows" key is easy to look for.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.run_check(["--diff", "--cwd", str(self.root)])
            cli.run_check(["--diff", "--cwd", str(self.root)])
        usage.record_tool_call(tool_name="write_file", verdict="PASS", path_arg="src/main.py", cwd=str(self.root))

    def _run(self, url):
        recorder = _Recorder()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("urllib.request.urlopen", recorder), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run_check(["--diff", "--cwd", str(self.root), "--upload", "--upload-url", url])
        return code, out.getvalue(), err.getvalue(), recorder

    def test_the_aggregate_goes_to_its_own_path(self):
        _code, _out, _err, recorder = self._run("http://dash.example")
        self.assertEqual(
            [item["url"] for item in recorder.requests],
            ["http://dash.example/api/v1/check/report", "http://dash.example/api/v1/usage/report"],
        )

    def test_the_aggregate_body_is_exactly_the_three_fields(self):
        _code, _out, _err, recorder = self._run("http://dash.example")
        body = recorder.by_path("/api/v1/usage/report")[0]["body"]
        self.assertEqual(set(body), {"verdicts", "baseline_misses", "tool_names"})
        self.assertEqual(body["verdicts"], {"INVALID": 3})
        self.assertEqual(body["baseline_misses"], 3)
        self.assertEqual(body["tool_names"], ["write_file"])

    def test_no_usage_row_leaves_the_machine(self):
        _code, _out, _err, recorder = self._run("http://dash.example")
        blob = json.dumps(recorder.requests)
        for fragment in ('"event"', '"at"', "path_hash", "src/main.py"):
            self.assertNotIn(fragment, blob)

    def test_the_aggregate_lines_are_reported_on_stderr(self):
        _code, _out, err, _recorder = self._run("http://dash.example")
        self.assertIn("usage: aggregate sent", err)

    def test_an_unreachable_dashboard_does_not_change_the_exit_code(self):
        recorder = _Recorder()

        def boom(request, timeout=None):
            import urllib.error

            raise urllib.error.URLError("refused")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("urllib.request.urlopen", boom), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run_check(["--diff", "--cwd", str(self.root), "--upload", "--upload-url", "http://dash.example"])
        self.assertEqual(code, 3)
        self.assertIn("usage: not sent", err.getvalue())

    def test_no_upload_flag_means_no_request(self):
        recorder = _Recorder()
        with mock.patch("urllib.request.urlopen", recorder), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.run_check(["--diff", "--cwd", str(self.root)])
        self.assertEqual(recorder.requests, [])


if __name__ == "__main__":
    unittest.main()
