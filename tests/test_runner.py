"""Tests for the witness collection layer."""

from __future__ import annotations

import os
import tempfile
import unittest

from audit_sidecar.runner import SafeRunner
from audit_sidecar.verdict import Verdict


class SafeRunnerTestCase(unittest.TestCase):
    """Base class that gives each test an isolated scratch directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-test-")
        self.root = self._tmp.name
        self.runner = SafeRunner(timeout=10)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def write(self, name: str, content: str) -> str:
        target = self.path(name)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
        return target


class PipefailTest(SafeRunnerTestCase):
    """Mode 1: the pipeline must not eat the status of an earlier stage."""

    def test_masked_failure_is_captured(self) -> None:
        result = self.runner.run_shell(
            "false | true", cwd=self.root, intent_desc="step must succeed"
        )
        self.assertFalse(result.check_observed)
        self.assertNotEqual(result.exit_code, 0)

    def test_false_pipe_cat_is_not_zero(self) -> None:
        result = self.runner.run_shell(
            "false | cat", cwd=self.root, intent_desc="step must succeed"
        )
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.check_observed)

    def test_naive_bash_would_have_reported_success(self) -> None:
        # This is the bug the runner exists to avoid: the same command is
        # reported as a success by a pipeline-blind exit status.
        naive = self.runner.raw_exit_status(
            "false | cat", cwd=self.root, pipefail=False
        )
        audited = self.runner.raw_exit_status(
            "false | cat", cwd=self.root, pipefail=True
        )
        self.assertEqual(naive, 0)
        self.assertEqual(audited, 1)

    def test_pipestatus_names_the_failing_stage(self) -> None:
        result = self.runner.run_shell(
            "false | cat", cwd=self.root, intent_desc="step must succeed"
        )
        self.assertEqual(result.pipe_status, (1, 0))

    def test_clean_pipeline_records_all_zero(self) -> None:
        result = self.runner.run_shell(
            "echo hello | cat | cat", cwd=self.root, intent_desc="pipe text"
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.pipe_status, (0, 0, 0))
        self.assertTrue(result.check_observed)

    def test_single_command_status_is_recorded(self) -> None:
        result = self.runner.run_shell(
            "exit 3", cwd=self.root, intent_desc="exit nonzero"
        )
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.pipe_status, (3,))

    def test_stdout_and_stderr_are_captured(self) -> None:
        result = self.runner.run_shell(
            "echo to-stdout; echo to-stderr >&2",
            cwd=self.root,
            intent_desc="print",
        )
        self.assertIn("to-stdout", result.stdout)
        self.assertIn("to-stderr", result.stderr)


class VerdictIntegrationTest(SafeRunnerTestCase):
    """The runner's three observations must drive the engine correctly."""

    def test_successful_write_is_pass(self) -> None:
        result = self.runner.run_shell(
            "printf 'feature_x=on\\n' > config.yaml",
            cwd=self.root,
            intent_desc="write feature_x=on into config.yaml",
            expected_file="config.yaml",
            expected_content="feature_x=on",
        )
        self.assertTrue(result.check_observed)
        self.assertTrue(result.effect_observed)
        self.assertIs(result.verdict, Verdict.PASS)
        self.assertTrue(result.ok)

    def test_append_keeps_previous_content(self) -> None:
        self.write("config.yaml", "feature_x=off\n")
        result = self.runner.run_shell(
            "echo 'feature_y=on' >> config.yaml",
            cwd=self.root,
            intent_desc="append feature_y=on",
            expected_file="config.yaml",
            expected_content="feature_y=on",
        )
        self.assertIs(result.verdict, Verdict.PASS)
        with open(self.path("config.yaml"), encoding="utf-8") as handle:
            self.assertIn("feature_x=off", handle.read())

    def test_existence_alone_is_enough_without_expected_content(self) -> None:
        result = self.runner.run_shell(
            "touch marker",
            cwd=self.root,
            intent_desc="create marker",
            expected_file="marker",
        )
        self.assertTrue(result.effect_observed)
        self.assertIs(result.verdict, Verdict.PASS)

    def test_claim_without_witness_is_invalid_not_pass(self) -> None:
        # A successful exit is not a witness; it is the agent's own channel.
        result = self.runner.run_shell(
            "true", cwd=self.root, intent_desc="do something"
        )
        self.assertTrue(result.check_observed)
        self.assertIsNone(result.effect_observed)
        self.assertIs(result.verdict, Verdict.INVALID)
        self.assertIsNot(result.verdict, Verdict.PASS)

    def test_missing_file_with_success_status_is_false_completion(self) -> None:
        # Mode 3: the command "succeeded" (exit 0) but the artefact is absent.
        result = self.runner.run_shell(
            "true",
            cwd=self.root,
            intent_desc="write config.yaml",
            expected_file="config.yaml",
            expected_content="feature_x=on",
        )
        self.assertTrue(result.check_observed)
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertIn("false completion", result.reason)

    def test_content_mismatch_is_fail(self) -> None:
        self.write("config.yaml", "feature_x=off\n")
        result = self.runner.run_shell(
            "true",
            cwd=self.root,
            intent_desc="enable feature_x",
            expected_file="config.yaml",
            expected_content="feature_x=on",
        )
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)

    def test_failed_command_with_missing_artefact_is_fail(self) -> None:
        result = self.runner.run_shell(
            "false",
            cwd=self.root,
            intent_desc="write config.yaml",
            expected_file="config.yaml",
        )
        self.assertFalse(result.check_observed)
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)

    def test_false_failure_is_fail(self) -> None:
        # The write landed but the command still exited non-zero.
        result = self.runner.run_shell(
            "printf 'ok\\n' > out.txt; exit 1",
            cwd=self.root,
            intent_desc="write out.txt",
            expected_file="out.txt",
            expected_content="ok",
        )
        self.assertTrue(result.effect_observed)
        self.assertFalse(result.check_observed)
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertIn("false failure", result.reason)


class TargetDriftTest(SafeRunnerTestCase):
    """Mode 3 variant: action and check land in the same wrong place."""

    def setUp(self) -> None:
        super().setUp()
        self.project = os.path.join(self.root, "project")
        self.drift = os.path.join(self.root, "drift")
        os.makedirs(self.project)
        os.makedirs(self.drift)
        with open(os.path.join(self.project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("feature_x=off\n")

    def test_pinned_witness_exposes_drift(self) -> None:
        result = self.runner.run_shell(
            "echo 'feature=on' >> config.yaml",
            cwd=self.drift,
            intent_desc="append feature=on to config.yaml",
            expected_file="config.yaml",
            expected_content="feature=on",
            witness_root=self.project,
        )
        # The command exited 0 and created a file, but not the intended one.
        self.assertTrue(result.check_observed)
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)

    def test_colocated_witness_is_blind_to_drift(self) -> None:
        # Documents the failure mode: if the witness follows the action into
        # the wrong directory, the audit cannot tell drift from success.
        result = self.runner.run_shell(
            "echo 'feature=on' >> config.yaml",
            cwd=self.drift,
            intent_desc="append feature=on to config.yaml",
            expected_file="config.yaml",
            expected_content="feature=on",
            witness_root=self.drift,
        )
        self.assertTrue(result.effect_observed)
        self.assertIs(result.verdict, Verdict.PASS)
        # The intended file was untouched, which is the whole problem.
        with open(os.path.join(self.project, "config.yaml"), encoding="utf-8") as f:
            self.assertNotIn("feature=on", f.read())

    def test_absolute_witness_path_ignores_witness_root(self) -> None:
        target = os.path.join(self.project, "config.yaml")
        result = self.runner.run_shell(
            "echo 'feature=on' >> config.yaml",
            cwd=self.drift,
            intent_desc="append feature=on",
            expected_file=target,
            expected_content="feature=on",
        )
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)


class FailureHandlingTest(SafeRunnerTestCase):
    def test_nonexistent_cwd_is_not_run(self) -> None:
        result = self.runner.run_shell(
            "true",
            cwd=os.path.join(self.root, "does-not-exist"),
            intent_desc="do something",
        )
        self.assertIsNone(result.exit_code)
        self.assertIsNotNone(result.error)
        self.assertIs(result.verdict, Verdict.NOT_RUN)

    def test_timeout_is_invalid(self) -> None:
        result = self.runner.run_shell(
            "sleep 5", cwd=self.root, intent_desc="sleep", timeout=0.2
        )
        self.assertIsNone(result.exit_code)
        self.assertIn("timed out", result.error or "")
        self.assertIs(result.verdict, Verdict.INVALID)

    def test_empty_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.runner.run_shell("   ", cwd=self.root, intent_desc="nothing")

    def test_content_without_file_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.runner.run_shell(
                "true",
                cwd=self.root,
                intent_desc="ambiguous witness",
                expected_content="something",
            )

    def test_missing_bash_interpreter_is_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            SafeRunner(bash="/nonexistent/bash")

    def test_non_positive_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SafeRunner(timeout=0)


class ExecutionResultTest(SafeRunnerTestCase):
    def test_result_is_frozen(self) -> None:
        result = self.runner.run_shell("true", cwd=self.root, intent_desc="x")
        with self.assertRaises(Exception):
            result.verdict = Verdict.PASS  # type: ignore[misc]

    def test_ok_is_true_only_for_pass(self) -> None:
        passed = self.runner.run_shell(
            "touch f", cwd=self.root, intent_desc="create f", expected_file="f"
        )
        failed = self.runner.run_shell(
            "true", cwd=self.root, intent_desc="create g", expected_file="g"
        )
        invalid = self.runner.run_shell(
            "true", cwd=self.root, intent_desc="anything"
        )
        self.assertTrue(passed.ok)
        self.assertFalse(failed.ok)
        self.assertFalse(invalid.ok)

    def test_result_records_cwd_absolute(self) -> None:
        result = self.runner.run_shell(
            "true", cwd=".", intent_desc="x"
        )
        self.assertTrue(os.path.isabs(result.cwd))
        self.assertEqual(result.cwd, os.path.abspath("."))

    def test_result_echoes_intent_description(self) -> None:
        text = "append feature_x=on to config.yaml"
        result = self.runner.run_shell("true", cwd=self.root, intent_desc=text)
        self.assertEqual(result.intent, text)

    def test_env_is_clean_of_scratch_files(self) -> None:
        # The wrapper and status file live in a temp dir that must not leak
        # into the working directory.
        self.runner.run_shell("true", cwd=self.root, intent_desc="x")
        self.assertEqual(os.listdir(self.root), [])


if __name__ == "__main__":
    unittest.main()
