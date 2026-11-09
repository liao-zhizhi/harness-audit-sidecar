"""``action.yml``: one input, with the old six demoted to fallbacks.

The point of the change is that a workflow should not have to learn the
auditor's vocabulary. These tests pin the shape rather than the prose: the
public input is ``check``, the legacy inputs still exist, and the run step
actually calls the subcommand the README advertises.
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
