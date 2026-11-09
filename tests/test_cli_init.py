"""``harness-audit init``: the install step, tested rather than trusted.

Three behaviours matter and each has a failure mode that is silent in
production:

* in a repository, the printed stanza and the ref map must name the repository
  root, not the subdirectory the developer happened to be standing in;
* outside a repository, ``init`` must still work -- it warns, because an audit
  does not need git, but the alias is then unstable and the operator is told;
* an existing ``ref_roots.json`` must never be replaced. Repointing every
  ``@alias`` case at a different checkout is the one destructive thing this
  command can do, and it would happen without an error.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import audit_sidecar.cli as cli


def _capture(argv):
    """Run ``init`` and return ``(exit code, stdout, stderr)``."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.run_init(argv)
    return code, out.getvalue(), err.getvalue()


def _git(directory, *args):
    subprocess.run(
        ["git", *args],
        cwd=directory,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


class InitInEmptyDirectoryTests(unittest.TestCase):
    """No repository, no existing map: still succeeds, and says why."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def test_it_warns_but_does_not_fail_outside_a_repository(self):
        code, stdout, stderr = _capture(["--cwd", str(self.root), "--local"])
        self.assertEqual(code, 0)
        self.assertIn("not inside a git repository", stderr)
        self.assertIn("project root", stdout)

    def test_it_writes_the_map_into_the_directory_when_local(self):
        code, _stdout, _stderr = _capture(["--cwd", str(self.root), "--local"])
        self.assertEqual(code, 0)
        written = json.loads((self.root / "ref_roots.json").read_text(encoding="utf-8"))
        self.assertEqual(written, {self.root.name.lower(): str(self.root)})

    def test_it_prints_the_mcp_stanza_without_editing_anything_global(self):
        code, stdout, _stderr = _capture(["--cwd", str(self.root), "--local", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        stanza = payload["mcp"]["mcpServers"]["harness-audit"]
        self.assertEqual(stanza["command"], "harness-audit-server")
        self.assertIn("--cwd", stanza["args"])
        # The only file created is the ref map; a client config is never touched.
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["ref_roots.json"])

    def test_the_default_target_is_the_path_the_case_library_reads(self):
        # Not ~/.gradia/ref_roots.json: case_store reads the case_library path,
        # so writing anywhere else produces a file that is never loaded.
        self.assertEqual(
            cli.REF_ROOTS_PATH,
            Path.home() / ".gradia" / "case_library" / "ref_roots.json",
        )


class InitWithExistingRefRootsTests(unittest.TestCase):
    """An existing map is reported, never overwritten."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.map_path = self.root / "ref_roots.json"
        self.prior = {"other-project": "/somewhere/else"}
        self.map_path.write_text(json.dumps(self.prior), encoding="utf-8")

    def test_it_skips_and_names_the_path(self):
        code, stdout, _stderr = _capture(["--cwd", str(self.root), "--local"])
        self.assertEqual(code, 0)
        self.assertIn("already exists, skipped", stdout)
        self.assertIn(str(self.map_path), stdout)

    def test_the_existing_contents_are_untouched(self):
        _capture(["--cwd", str(self.root), "--local"])
        self.assertEqual(
            json.loads(self.map_path.read_text(encoding="utf-8")), self.prior
        )

    def test_a_missing_alias_is_called_out(self):
        # Skipping is correct, but leaving the operator believing the new alias
        # resolves would turn the skip into a silent failure.
        _code, _stdout, stderr = _capture(["--cwd", str(self.root), "--local"])
        self.assertIn("has no alias", stderr)


class InitInsideARepositoryTests(unittest.TestCase):
    """Inside git, the root wins over the working directory."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        _git(self.root, "init", "-q")
        self.nested = self.root / "deep" / "inside"
        self.nested.mkdir(parents=True)

    def test_the_alias_maps_the_repository_root_not_the_subdirectory(self):
        if cli.resolve_git_root(str(self.nested)) is None:
            self.skipTest("git unavailable")
        code, _stdout, stderr = _capture(["--cwd", str(self.nested), "--local"])
        self.assertEqual(code, 0)
        self.assertNotIn("not inside a git repository", stderr)
        written = json.loads((self.nested / "ref_roots.json").read_text(encoding="utf-8"))
        self.assertEqual(list(written.values()), [str(self.root.resolve())])

    def test_the_stanza_points_the_server_at_the_root(self):
        if cli.resolve_git_root(str(self.nested)) is None:
            self.skipTest("git unavailable")
        _code, stdout, _stderr = _capture(["--cwd", str(self.nested), "--local", "--json"])
        stanza = json.loads(stdout)["mcp"]["mcpServers"]["harness-audit"]
        self.assertIn(str(self.root.resolve()), stanza["args"])


class DispatchTests(unittest.TestCase):
    """The demo stays the default, so existing invocations keep working."""

    def test_an_unknown_command_is_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--nonsense"]), 2)
        self.assertIn("unknown command", err.getvalue())

    def test_help_is_available_without_running_the_demo(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("harness-audit init", out.getvalue())

    def test_a_nonexistent_directory_is_an_error(self):
        code, _stdout, stderr = _capture(["--cwd", "/no/such/dir/for/init", "--local"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", stderr)


if __name__ == "__main__":
    unittest.main()
