"""``action.yml``: one input, with the old six demoted to fallbacks.

The point of the change is that a workflow should not have to learn the
auditor's vocabulary. These tests pin the shape rather than the prose: the
public input is ``check``, the legacy inputs still exist, and the run step
actually calls the subcommand the README advertises.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    import yaml

    HAVE_YAML = True
except ImportError:  # pragma: no cover - PyYAML is a package dependency
    HAVE_YAML = False


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class ActionShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "action.yml"
        cls.action = yaml.safe_load(cls.path.read_text(encoding="utf-8"))
        cls.steps = cls.action["runs"]["steps"]

    def test_the_action_is_composite(self):
        self.assertEqual(self.action["runs"]["using"], "composite")

    def test_the_public_input_is_one_check_string(self):
        inputs = self.action["inputs"]
        self.assertIn("check", inputs)
        self.assertEqual(inputs["check"]["required"], False)
        self.assertIn("file <path>", inputs["check"]["description"])
        self.assertIn("command <cmd>", inputs["check"]["description"])

    def test_the_legacy_inputs_are_kept_as_fallbacks(self):
        inputs = self.action["inputs"]
        for name in ("file", "command", "diff"):
            self.assertIn(name, inputs, name)

    def test_check_is_forwarded_to_the_step(self):
        env = self.steps[-1]["env"]
        self.assertEqual(env["INPUT_CHECK"], "${{ inputs.check }}")
        for name in ("file", "command", "diff"):
            self.assertEqual(env["INPUT_{}".format(name.upper())], "${{ inputs.%s }}" % name)

    def test_the_legacy_branch_only_runs_when_check_is_empty(self):
        script = "\n".join(step.get("run", "") for step in self.steps)
        assignment = script.index('declaration="${INPUT_CHECK}"')
        guard = script.index('if [[ -z "${declaration}" ]]')
        legacy = script.index('declaration="file ${INPUT_FILE}"')
        # check is read first, then the guard, then the legacy branch: a
        # leftover `file:` input cannot override an explicit `check:`.
        self.assertLess(assignment, guard)
        self.assertLess(guard, legacy)

    def test_the_step_invokes_the_advertised_subcommand(self):
        script = "\n".join(step.get("run", "") for step in self.steps)
        self.assertIn("harness-audit check", script)
        self.assertIn("--file", script)
        self.assertIn("--command", script)
        self.assertIn("--diff", script)

    def test_the_exit_code_is_propagated_and_published(self):
        script = self.steps[-1]["run"]
        self.assertIn('echo "verdict=${verdict}"', script)
        self.assertIn('echo "exit_code=${code}"', script)
        self.assertIn('exit "${code}"', script)

    def test_the_outputs_name_the_three_values(self):
        outputs = self.action["outputs"]
        self.assertEqual(set(outputs), {"verdict", "reason", "exit_code"})
        for name in outputs:
            self.assertEqual(
                outputs[name]["value"], "${{ steps.check.outputs.%s }}" % name
            )

    def test_the_readme_shows_the_one_input_form(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("check: file src/main.py", readme)
        self.assertIn("harness-audit check --file src/main.py", readme)

    def test_the_exit_code_output_says_invalid_blocks_ci(self):
        description = self.action["outputs"]["exit_code"]["description"]
        self.assertIn("INVALID blocks CI by default", description)
        self.assertIn("continue-on-error", description)


    def test_the_install_step_installs_the_sidecar_package(self):
        install = self.steps[0]
        self.assertEqual(install["shell"], "bash")
        self.assertIn("pip install", install["run"])
        self.assertIn("harness-audit-sidecar", install["run"])
        # Nothing to build from: `uses: owner/repo@v1` does not place a wheel on
        # the runner, so the package is the dependency rather than the checkout.
        self.assertNotIn("GITHUB_ACTION_PATH", install["run"])

    def test_the_judging_step_calls_the_check_subcommand(self):
        self.assertIn("harness-audit check", self.steps[-1]["run"])

    def test_the_removed_path_input_is_not_reintroduced(self):
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("pre-pr-audit-path", raw)
        self.assertNotIn("pre_pr_audit_path", raw)
        for step in self.steps:
            self.assertNotIn("pre-pr-audit-path", json.dumps(step))

    def test_the_readme_documents_the_v1_form_without_a_path(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("uses: liao-zhizhi/harness-audit-sidecar@v1", readme)
        self.assertIn("check: file src/main.py", readme)
        self.assertIn("harness-audit-sidecar` from PyPI", readme)


if __name__ == "__main__":
    unittest.main()
