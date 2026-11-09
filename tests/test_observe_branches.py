"""Tests for the post-release workflow survey (``scripts/observe_branches.py``).

The fixtures are written by hand to mirror the shapes the README recommends and
the shapes a workflow author is likely to write instead. They are **not**
samples of real external workflows -- none have been collected yet -- so these
tests prove the classifier behaves as documented, not that the world behaves
that way. The first real corpus is what decides whether the fourth output is
needed; this file only makes sure that decision is not taken from a
miscounted survey.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "observe_branches.py"


def _load():
    spec = importlib.util.spec_from_file_location("observe_branches", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observer = _load()

#: Shape the README recommends: assign the output, then `case` on the variable.
DOCUMENTED = """
- id: audit
  uses: liao-zhizhi/harness-audit-sidecar@v1
  continue-on-error: true
  with:
    check: file src/main.py
- run: |
    reason="${{ steps.audit.outputs.reason }}"
    case "${reason}" in
      "self-audit failed"*)
        echo "::error::harness-audit itself is broken"; exit 2 ;;
      "declaration malformed"*)
        echo "::error::bad invocation"; exit 2 ;;
      *)
        echo "not judged: ${reason}" ;;
    esac
"""

#: The behaviour the README names as wrong, in the form people actually write.
VERDICT_ONLY = """
- id: audit
  uses: liao-zhizhi/harness-audit-sidecar@v1
  continue-on-error: true
- if: steps.audit.outputs.verdict != 'INVALID'
  run: echo "gate passed"
"""

EXIT_CODE_ONLY = """
- id: audit
  uses: liao-zhizhi/harness-audit-sidecar@v1
  continue-on-error: true
- if: steps.audit.outputs.exit_code == '1'
  run: echo "real failure"
"""

ECHO_ONLY = """
- id: audit
  uses: liao-zhizhi/harness-audit-sidecar@v1
- run: echo "verdict was ${{ steps.audit.outputs.verdict }} / ${{ steps.audit.outputs.reason }}"
"""

ASSUMES_FOURTH_OUTPUT = """
- id: audit
  uses: liao-zhizhi/harness-audit-sidecar@v1
  continue-on-error: true
- if: steps.audit.outputs.failure_kind == 'self_audit'
  run: echo "tool is broken"
"""

REASON_OTHER = """
- id: audit
  uses: liao-zhizhi/harness-audit-sidecar@v1
  continue-on-error: true
- if: contains(steps.audit.outputs.reason, 'nobody looked')
  run: echo "retry later"
"""


class ClassificationTests(unittest.TestCase):
    def test_the_documented_pattern_classifies_as_reason_prefix(self):
        result = observer._classify(DOCUMENTED)
        self.assertEqual(result["class"], "reason_prefix")
        self.assertIn("self-audit failed", result["prefixes"])
        self.assertIn("declaration malformed", result["prefixes"])

    def test_a_condition_on_the_variable_is_followed(self):
        # The `case` line mentions no output at all; without following the
        # alias this would score as "no_branch" and undercount the only
        # pattern the docs recommend.
        result = observer._classify(DOCUMENTED)
        self.assertEqual(result["conditioned"], {"reason": 1})

    def test_branching_on_verdict_is_the_signal_for_the_fourth_output(self):
        self.assertEqual(observer._classify(VERDICT_ONLY)["class"], "verdict_only")

    def test_branching_on_exit_code_is_not_the_signal(self):
        # A blocking gate is not evidence that the reason contract failed.
        self.assertEqual(observer._classify(EXIT_CODE_ONLY)["class"], "exit_code_only")

    def test_echoing_an_output_is_not_branching_on_it(self):
        # This is the false positive that would make the survey useless: most
        # workflows mention `verdict` somewhere in a log line.
        result = observer._classify(ECHO_ONLY)
        self.assertEqual(result["class"], "no_branch")
        self.assertEqual(result["echoed_only"], ["reason", "verdict"])

    def test_referencing_failure_kind_is_an_incident_not_a_survey_row(self):
        self.assertEqual(
            observer._classify(ASSUMES_FOURTH_OUTPUT)["class"], "fourth_output"
        )

    def test_a_bespoke_reason_match_is_separated_from_a_prefix_match(self):
        result = observer._classify(REASON_OTHER)
        self.assertEqual(result["class"], "reason_other")
        self.assertEqual(result["prefixes"], [])

    def test_comments_are_ignored(self):
        text = "# if: steps.audit.outputs.failure_kind == 'self_audit'\n"
        self.assertEqual(observer._classify(text)["class"], "no_branch")

    def test_a_trailing_comment_is_not_a_branch(self):
        # Verbatim from the README's own example, where the alternative is
        # suggested *in a comment*. Counting it would inflate verdict_only,
        # which is the only number the fourth-output decision rests on.
        text = (
            "  continue-on-error: true          "
            "# or: if: steps.audit.outputs.verdict != 'INVALID'\n"
            '- run: echo "verdict was ${{ steps.audit.outputs.verdict }}"\n'
        )
        result = observer._classify(text)
        self.assertEqual(result["class"], "no_branch")
        self.assertEqual(result["conditioned"], {})

    def test_a_shell_parameter_expansion_is_not_a_comment(self):
        # `${reason#prefix}` contains a `#` that is not preceded by whitespace.
        # Stripping it would silently lose the case branch it lives in.
        text = (
            '    case "${reason#self-audit failed}" in\n'
            '      "") exit 0 ;;\n'
            "    esac\n"
        )
        # No output reference at all, so this is only a guard that the stripper
        # does not mangle lines; the file stays unclassified rather than
        # producing a false branch.
        self.assertEqual(observer._classify(text)["class"], "no_branch")


class ContractCouplingTests(unittest.TestCase):
    def test_the_survey_looks_for_the_live_prefixes(self):
        # Keyed on check.py's constants rather than a second copy: a rename
        # must not leave the survey hunting for a string nobody emits.
        import audit_sidecar.check as check_mod

        self.assertEqual(
            observer.CONTRACT_PREFIXES,
            (
                check_mod.SELF_AUDIT_REASON_PREFIX,
                check_mod.MALFORMED_REASON_PREFIX,
                check_mod.NO_DECLARATION_REASON_PREFIX,
            ),
        )


class ReadmeExampleTests(unittest.TestCase):
    """The documented example must be a shape the survey recognises.

    If the README's `case` snippet drifted into something this scan classifies
    as `no_branch`, then following the docs would look like not using the
    output at all -- and the survey that decides the fourth output would be
    reading our own documentation as non-adoption.
    """

    def test_the_recommended_example_classifies_as_the_documented_path(self):
        text = (_ROOT / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```yaml\n(.*?)```", text, re.S)
        snippets = [block for block in blocks if "case " in block]
        self.assertTrue(snippets, "README no longer contains a `case` example")
        result = observer._classify(snippets[0])
        self.assertEqual(result["class"], "reason_prefix", snippets[0])


class CommandLineTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_directory_is_scanned_and_only_yaml_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.yml").write_text(DOCUMENTED, encoding="utf-8")
            (root / "b.yaml").write_text(VERDICT_ONLY, encoding="utf-8")
            (root / "README.md").write_text("outputs.verdict", encoding="utf-8")
            completed = self._run(str(root))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("reason_prefix", completed.stdout)
            self.assertIn("verdict_only", completed.stdout)
            self.assertIn("2 file(s)", completed.stdout)

    def test_json_is_machine_readable_and_carries_the_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ci.yml"
            path.write_text(VERDICT_ONLY, encoding="utf-8")
            completed = self._run(str(path), "--json")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["class"], "verdict_only")
            self.assertEqual(payload[0]["path"], str(path))

    def test_an_empty_target_is_a_usage_error_not_a_clean_survey(self):
        # "no files" must not look like "no problems": exit 2, per the repo's
        # own convention (0 PASS / 1 FAIL / 2 usage / 3 INVALID).
        with tempfile.TemporaryDirectory() as tmp:
            completed = self._run(tmp)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("no workflow files found", completed.stderr)


if __name__ == "__main__":
    unittest.main()
