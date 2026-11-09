"""``harness-audit setup``: writes are backed up, merged, and never silent.

The failure modes this file is about are all quiet ones:

* an existing ``mcp.json`` with other servers, replaced instead of merged;
* a first run that overwrites ``.bak`` on the second, leaving a backup of
  nothing;
* ``--print-only`` that writes "just the baseline", which is still a file;
* ``ref_roots.json`` replaced, repointing every other project's ``@alias``.

Each one produces a command that looks like it worked. So the tests assert the
bytes on disk, not the prose on stdout.
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
from unittest import mock

import audit_sidecar.baseline as baseline_mod
import audit_sidecar.cli as cli
import audit_sidecar.setup_cmd as setup_cmd


def _capture(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = setup_cmd.run_setup(argv)
    return code, out.getvalue(), err.getvalue()


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.mcp_path = self.project / ".cursor" / "mcp.json"
        self.ref_path = self.root / "case_library" / "ref_roots.json"
        self.baseline_store = self.root / "baseline.json"

        # Nothing here may touch the real ~/.cursor or ~/.gradia: a test that
        # writes the developer's client config is a test that breaks the
        # developer's client. The explicit overrides below are the same ones a
        # machine with a non-standard layout would use.
        patches = [
            mock.patch.object(setup_cmd, "GLOBAL_MCP_PATH", self.root / "global-mcp.json"),
            mock.patch.object(baseline_mod, "GLOBAL_BASELINE_PATH", self.baseline_store),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def argv(self, *extra):
        return [
            "--cwd",
            str(self.project),
            "--mcp-config",
            str(self.mcp_path),
            "--ref-roots",
            str(self.ref_path),
            *extra,
        ]

    def read(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------- empty project

    def test_empty_directory_gets_a_project_config_and_a_baseline(self):
        (self.project / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (self.project / "notes.md").write_text("# notes\n", encoding="utf-8")
        (self.project / "blob.bin").write_bytes(b"\x00\x01")

        code, out, err = _capture(self.argv())

        self.assertEqual(code, 0, err)
        document = self.read(self.mcp_path)
        self.assertEqual(
            document["mcpServers"]["harness-audit"],
            {"command": "harness-audit", "args": ["mcp"]},
        )
        # Only the common extensions: a digest for a binary is a recording that
        # changes for reasons no claim mentions.
        baseline = self.read(self.baseline_store)
        recorded = baseline["repos"][os.path.abspath(str(self.project))]["files"]
        self.assertIn("app.py", recorded)
        self.assertIn("notes.md", recorded)
        self.assertNotIn("blob.bin", recorded)
        self.assertIn("restart Cursor", out)
        self.assertIn("2 file(s)", out)

    def test_the_alias_points_at_the_repository_root_not_the_subdirectory(self):
        subprocess.run(
            ["git", "init", "--quiet", str(self.project)],
            check=True,
            capture_output=True,
        )
        (self.project / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.project), "add", "app.py"],
            check=True,
            capture_output=True,
        )
        sub = self.project / "sub"
        sub.mkdir()

        code, out, err = _capture(
            ["--cwd", str(sub), "--mcp-config", str(self.mcp_path), "--ref-roots", str(self.ref_path)]
        )

        self.assertEqual(code, 0, err)
        mapping = self.read(self.ref_path)
        self.assertEqual(mapping, {"project": os.path.abspath(str(self.project))})

    def test_the_project_config_lands_at_the_repository_root(self):
        subprocess.run(
            ["git", "init", "--quiet", str(self.project)],
            check=True,
            capture_output=True,
        )
        (self.project / "app.py").write_text("x = 1\n", encoding="utf-8")
        sub = self.project / "sub"
        sub.mkdir()

        code, _, err = _capture(["--cwd", str(sub), "--ref-roots", str(self.ref_path)])

        self.assertEqual(code, 0, err)
        # Cursor reads .cursor/mcp.json from the workspace it opened; a file in
        # sub/ would be a config the client never loads.
        self.assertTrue((self.project / ".cursor" / "mcp.json").exists())
        self.assertFalse((sub / ".cursor").exists())

    def test_ref_roots_merge_keeps_other_aliases(self):
        self.ref_path.parent.mkdir(parents=True, exist_ok=True)
        self.ref_path.write_text(json.dumps({"other": "/elsewhere/other"}), encoding="utf-8")

        code, _, err = _capture(self.argv())

        self.assertEqual(code, 0, err)
        mapping = self.read(self.ref_path)
        self.assertEqual(mapping["other"], "/elsewhere/other")
        self.assertEqual(mapping["project"], os.path.abspath(str(self.project)))

    # ------------------------------------------------------- existing configs

    def test_existing_config_is_merged_and_backed_up(self):
        self.mcp_path.parent.mkdir(parents=True)
        original = {
            "mcpServers": {"someone-else": {"command": "other-server"}},
            "telemetry": {"enabled": False},
        }
        self.mcp_path.write_text(json.dumps(original, indent=2), encoding="utf-8")

        code, out, err = _capture(self.argv())

        self.assertEqual(code, 0, err)
        merged = self.read(self.mcp_path)
        self.assertEqual(merged["mcpServers"]["someone-else"], {"command": "other-server"})
        self.assertEqual(merged["telemetry"], {"enabled": False})
        self.assertEqual(
            merged["mcpServers"]["harness-audit"],
            {"command": "harness-audit", "args": ["mcp"]},
        )

        backup = Path(str(self.mcp_path) + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(self.read(backup), original)
        self.assertIn("backup", out)

    def test_second_run_does_not_replace_the_original_backup(self):
        self.mcp_path.parent.mkdir(parents=True)
        original = {"mcpServers": {"someone-else": {"command": "other-server"}}}
        self.mcp_path.write_text(json.dumps(original), encoding="utf-8")

        self.assertEqual(_capture(self.argv())[0], 0)
        # Change the merged file, then run again: a second write must keep the
        # *pre-merge* bytes as the first backup, not the merged ones.
        document = self.read(self.mcp_path)
        document["mcpServers"]["harness-audit"]["args"] = ["mcp", "--stale"]
        self.mcp_path.write_text(json.dumps(document), encoding="utf-8")

        self.assertEqual(_capture(self.argv())[0], 0)
        self.assertEqual(self.read(Path(str(self.mcp_path) + ".bak")), original)

    def test_an_idempotent_second_run_writes_nothing(self):
        self.assertEqual(_capture(self.argv())[0], 0)
        mcp_before = self.mcp_path.read_text(encoding="utf-8")
        roots_before = self.ref_path.read_text(encoding="utf-8")

        code, out, err = _capture(self.argv())

        self.assertEqual(code, 0, err)
        self.assertEqual(self.mcp_path.read_text(encoding="utf-8"), mcp_before)
        self.assertEqual(self.ref_path.read_text(encoding="utf-8"), roots_before)
        self.assertIn("unchanged", out)
        self.assertFalse(Path(str(self.mcp_path) + ".bak.1").exists())

    def test_a_broken_config_is_refused_rather_than_replaced(self):
        self.mcp_path.parent.mkdir(parents=True)
        self.mcp_path.write_text('{"mcpServers": {', encoding="utf-8")

        code, _, err = _capture(self.argv())

        self.assertEqual(code, 2)
        self.assertIn("refusing to write", err)
        # Untouched, byte for byte, and no backup: nothing was written at all.
        self.assertEqual(self.mcp_path.read_text(encoding="utf-8"), '{"mcpServers": {')
        self.assertFalse(Path(str(self.mcp_path) + ".bak").exists())

    def test_a_non_object_mcp_servers_key_is_refused(self):
        self.mcp_path.parent.mkdir(parents=True)
        self.mcp_path.write_text(json.dumps({"mcpServers": ["nope"]}), encoding="utf-8")

        code, _, err = _capture(self.argv())

        self.assertEqual(code, 2)
        self.assertIn("mcpServers is not a JSON object", err)

    def test_an_existing_global_config_is_reused_when_project_has_none(self):
        global_path = self.root / "global-mcp.json"
        global_path.write_text(
            json.dumps({"mcpServers": {"existing": {"command": "other"}}}), encoding="utf-8"
        )
        with mock.patch.object(setup_cmd, "GLOBAL_MCP_PATH", global_path):
            argv = ["--cwd", str(self.project), "--ref-roots", str(self.ref_path)]
            code, out, err = _capture(argv)

        self.assertEqual(code, 0, err)
        # No project file was created to shadow it.
        self.assertFalse(self.mcp_path.exists())
        document = self.read(global_path)
        self.assertIn("existing", document["mcpServers"])
        self.assertIn("harness-audit", document["mcpServers"])
        self.assertIn("(global)", out)

    # ----------------------------------------------------------- print-only

    def test_print_only_writes_nothing_at_all(self):
        (self.project / "app.py").write_text("x = 1\n", encoding="utf-8")

        code, out, err = _capture(self.argv("--print-only"))

        self.assertEqual(code, 0, err)
        self.assertFalse(self.mcp_path.exists())
        self.assertFalse(self.ref_path.exists())
        self.assertFalse(self.baseline_store.exists())
        self.assertIn("would write", out)
        self.assertIn("nothing was written", out)

    def test_print_only_reports_the_merge_it_would_perform(self):
        self.mcp_path.parent.mkdir(parents=True)
        self.mcp_path.write_text(json.dumps({"mcpServers": {"keep": {"command": "x"}}}), encoding="utf-8")

        code, out, err = _capture(self.argv("--print-only"))

        self.assertEqual(code, 0, err)
        # The preview is the merged document, not "a new file": the whole point
        # is that the operator can see the other server survives.
        self.assertIn("harness-audit", out)
        self.assertEqual(
            self.read(self.mcp_path), {"mcpServers": {"keep": {"command": "x"}}}
        )

    # ----------------------------------------------------- failure handling

    def test_a_closed_self_audit_writes_nothing(self):
        with mock.patch.object(setup_cmd, "run_self_audit", return_value=False):
            code, _, err = _capture(self.argv())

        self.assertEqual(code, 3)
        self.assertIn("self-audit failed", err)
        self.assertFalse(self.mcp_path.exists())
        self.assertFalse(self.ref_path.exists())

    def test_a_missing_directory_is_a_usage_error(self):
        code, _, err = _capture(["--cwd", str(self.root / "missing")])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)

    def test_the_json_report_names_every_path_and_count(self):
        (self.project / "app.py").write_text("x = 1\n", encoding="utf-8")

        code, out, err = _capture(self.argv("--json"))

        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["mcp"]["path"], str(self.mcp_path))
        self.assertTrue(report["mcp"]["written"])
        self.assertEqual(report["ref_roots"]["alias"], "project")
        self.assertEqual(report["baseline"]["files"], 1)
        self.assertTrue(report["baseline"]["written"])

    def test_the_file_cap_is_reported_rather_than_hidden(self):
        for name in ("a.py", "b.py", "c.py"):
            (self.project / name).write_text("x = 1\n", encoding="utf-8")

        code, out, err = _capture(self.argv("--max-files", "1", "--json"))

        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["baseline"]["files"], 1)
        self.assertEqual(report["baseline"]["matched"], 3)
        self.assertTrue(report["baseline"]["truncated"])

        # And in the human output: a cap nobody is told about reads as "all".
        code, out, _ = _capture(self.argv("--max-files", "1"))
        self.assertIn("capped", out)


class CliDispatchTests(unittest.TestCase):
    def test_setup_is_reachable_through_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = [
                "setup",
                "--print-only",
                "--cwd",
                tmp,
                "--mcp-config",
                str(root / "mcp.json"),
                "--ref-roots",
                str(root / "ref_roots.json"),
            ]
            out = io.StringIO()
            with mock.patch.object(setup_cmd, "GLOBAL_MCP_PATH", root / "global.json"), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(argv)

            self.assertEqual(code, 0)
            self.assertIn("would write", out.getvalue())

    def test_mcp_delegates_to_the_stdio_server(self):
        with mock.patch("audit_sidecar.mcp_server.main", return_value=7) as server:
            code = cli.main(["mcp", "--quiet"])

        self.assertEqual(code, 7)
        server.assert_called_once_with(["--quiet"])


if __name__ == "__main__":
    unittest.main()
