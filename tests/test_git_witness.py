"""Tests for the git-backed witness.

Every test builds a real temporary repository and drives real ``git``. A test
that faked the change set would only prove that the mock works.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from audit_sidecar.git_witness import GitWitness, GitWitnessError
from audit_sidecar.runner import SafeRunner
from audit_sidecar.verdict import Verdict

GIT = shutil.which("git")
HAS_GIT = GIT is not None and shutil.which("bash") is not None


@unittest.skipUnless(HAS_GIT, "git and bash are required for witness tests")
class GitRepositoryTestCase(unittest.TestCase):
    """Base fixture: a real repository with one commit."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-git-")
        self.root = self._tmp.name
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.write("target.py", "value = 0\n")
        self.write("other.py", "other = 0\n")
        self.git("add", "-A")
        self.commit("initial")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            [GIT, *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    def commit(self, message: str) -> None:
        # A synthetic identity keeps the test independent of the user's
        # global git configuration and of any signing setup.
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            message,
        )

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def write(self, name: str, content: str) -> None:
        target = self.path(name)
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)


class GetChangedFilesTest(GitRepositoryTestCase):
    def test_clean_repo_is_empty(self) -> None:
        witness = GitWitness(self.root)
        self.assertEqual(witness.get_changed_files(), [])
        self.assertFalse(witness.has_changes())

    def test_detects_modified_file(self) -> None:
        self.write("target.py", "value = 1\n")
        self.assertEqual(GitWitness(self.root).get_changed_files(), ["target.py"])

    def test_detects_untracked_file(self) -> None:
        self.write("brand_new.py", "x = 1\n")
        self.assertEqual(
            GitWitness(self.root).get_changed_files(), ["brand_new.py"]
        )

    def test_detects_untracked_nested_file(self) -> None:
        # Target drift usually looks like a new directory, and only -uall
        # reports the file inside it rather than the directory itself.
        self.write("wrong_dir/target.py", "x = 1\n")
        self.assertEqual(
            GitWitness(self.root).get_changed_files(), ["wrong_dir/target.py"]
        )

    def test_detects_deleted_file(self) -> None:
        os.remove(self.path("other.py"))
        self.assertEqual(GitWitness(self.root).get_changed_files(), ["other.py"])

    def test_detects_staged_change(self) -> None:
        self.write("target.py", "value = 2\n")
        self.git("add", "target.py")
        self.assertEqual(GitWitness(self.root).get_changed_files(), ["target.py"])

    def test_result_is_sorted_and_posix(self) -> None:
        self.write("b.py", "b\n")
        self.write("a.py", "a\n")
        changed = GitWitness(self.root).get_changed_files()
        self.assertEqual(changed, sorted(changed))
        self.assertTrue(all("\\" not in p for p in changed))

    def test_ignored_files_stay_invisible(self) -> None:
        self.write(".gitignore", "*.log\n")
        self.git("add", "-A")
        self.commit("ignore logs")
        self.write("noise.log", "x\n")
        # .gitignore itself is committed, so only ignored content is pending.
        self.assertEqual(GitWitness(self.root).get_changed_files(), [])

    def test_non_repository_raises(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(GitWitnessError):
                GitWitness(plain)

    def test_missing_directory_raises(self) -> None:
        with self.assertRaises(GitWitnessError):
            GitWitness(os.path.join(self.root, "nope"))

    def test_is_repository_helper(self) -> None:
        self.assertTrue(GitWitness.is_repository(self.root))
        with tempfile.TemporaryDirectory() as plain:
            self.assertFalse(GitWitness.is_repository(plain))

    def test_subdirectory_resolves_to_repo_root(self) -> None:
        os.makedirs(self.path("sub"))
        witness = GitWitness(self.path("sub"))
        self.assertEqual(
            os.path.realpath(witness.repo_root), os.path.realpath(self.root)
        )


class SnapshotTest(GitRepositoryTestCase):
    def test_changed_since_ignores_pre_existing_dirt(self) -> None:
        self.write("other.py", "other = 1\n")
        witness = GitWitness(self.root)
        baseline = witness.snapshot()
        # Nothing happened between the two snapshots.
        self.assertEqual(witness.changed_since(baseline), [])

    def test_changed_since_reports_only_new_edits(self) -> None:
        self.write("other.py", "other = 1\n")
        witness = GitWitness(self.root)
        baseline = witness.snapshot()
        self.write("target.py", "value = 9\n")
        self.assertEqual(witness.changed_since(baseline), ["target.py"])

    def test_changed_since_detects_second_edit_to_same_file(self) -> None:
        # A path that was already dirty still counts when its content moves.
        self.write("target.py", "value = 1\n")
        witness = GitWitness(self.root)
        baseline = witness.snapshot()
        self.write("target.py", "value = 2\n")
        self.assertEqual(witness.changed_since(baseline), ["target.py"])

    def test_changed_since_detects_identical_file_replaced_by_git(self) -> None:
        witness = GitWitness(self.root)
        baseline = witness.snapshot()
        self.write("new.py", "")
        self.assertEqual(witness.changed_since(baseline), ["new.py"])

    def test_snapshot_rejects_non_dict(self) -> None:
        witness = GitWitness(self.root)
        with self.assertRaises(TypeError):
            witness.changed_since([])  # type: ignore[arg-type]


class ResolveExpectedTest(GitRepositoryTestCase):
    def test_relative_path_uses_cwd_as_coordinate(self) -> None:
        os.makedirs(self.path("sub"))
        witness = GitWitness(self.root)
        resolved = witness.resolve_expected(["target.py"], self.path("sub"))
        # The caller meant sub/target.py, not the root one.
        self.assertEqual(resolved, ["sub/target.py"])

    def test_absolute_path_is_accepted(self) -> None:
        witness = GitWitness(self.root)
        resolved = witness.resolve_expected([self.path("other.py")], self.root)
        self.assertEqual(resolved, ["other.py"])

    def test_path_outside_repository_is_rejected(self) -> None:
        witness = GitWitness(self.root)
        with self.assertRaises(ValueError):
            witness.resolve_expected(["../escape.py"], self.root)

    def test_bare_string_is_rejected(self) -> None:
        witness = GitWitness(self.root)
        with self.assertRaises(TypeError):
            witness.resolve_expected("target.py", self.root)  # type: ignore[arg-type]


class RunnerGitWitnessTest(GitRepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = SafeRunner(timeout=20)

    def test_correct_edit_is_pass(self) -> None:
        result = self.runner.run_shell(
            "printf 'value = 1\\n' > target.py",
            cwd=self.root,
            intent_desc="edit target.py",
            expected_files_modified=["target.py"],
        )
        self.assertEqual(result.changed_files, ("target.py",))
        self.assertTrue(result.effect_observed)
        self.assertIs(result.verdict, Verdict.PASS)

    def test_wrong_file_is_fail(self) -> None:
        result = self.runner.run_shell(
            "mkdir -p wrong_dir && printf 'value = 1\\n' > wrong_dir/target.py",
            cwd=self.root,
            intent_desc="edit target.py",
            expected_files_modified=["target.py"],
        )
        self.assertTrue(result.check_observed)
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertEqual(result.missing_expected, ("target.py",))
        self.assertEqual(result.unexpected_changes, ("wrong_dir/target.py",))

    def test_expected_file_left_untouched_is_fail(self) -> None:
        result = self.runner.run_shell(
            "true",
            cwd=self.root,
            intent_desc="edit target.py",
            expected_files_modified=["target.py"],
        )
        self.assertTrue(result.check_observed)
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)

    def test_pollution_is_fail_by_default(self) -> None:
        result = self.runner.run_shell(
            "printf 'value = 1\\n' > target.py; printf 'sneaky\\n' > other.py",
            cwd=self.root,
            intent_desc="edit target.py",
            expected_files_modified=["target.py"],
        )
        self.assertIn("target.py", result.changed_files)
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)
        self.assertEqual(result.unexpected_changes, ("other.py",))
        self.assertEqual(result.missing_expected, ())

    def test_pollution_is_accepted_in_lenient_mode(self) -> None:
        result = self.runner.run_shell(
            "printf 'value = 1\\n' > target.py; printf 'sneaky\\n' > other.py",
            cwd=self.root,
            intent_desc="edit target.py",
            expected_files_modified=["target.py"],
            allow_extra_changes=True,
        )
        self.assertTrue(result.effect_observed)
        self.assertIs(result.verdict, Verdict.PASS)

    def test_multiple_expected_files(self) -> None:
        result = self.runner.run_shell(
            "printf 'a\\n' > target.py; printf 'b\\n' > other.py",
            cwd=self.root,
            intent_desc="edit both files",
            expected_files_modified=["target.py", "other.py"],
        )
        self.assertTrue(result.effect_observed)
        self.assertIs(result.verdict, Verdict.PASS)

    def test_pre_existing_dirt_is_not_attributed_to_the_command(self) -> None:
        self.write("other.py", "other = 1\n")
        result = self.runner.run_shell(
            "printf 'value = 1\\n' > target.py",
            cwd=self.root,
            intent_desc="edit target.py",
            expected_files_modified=["target.py"],
        )
        # other.py was already dirty before the run, so it must not be
        # reported as unexpected drift caused by this command.
        self.assertEqual(result.changed_files, ("target.py",))
        self.assertEqual(result.unexpected_changes, ())
        self.assertIs(result.verdict, Verdict.PASS)

    def test_subdirectory_expectation_maps_to_repo_root(self) -> None:
        os.makedirs(self.path("pkg"))
        self.write("pkg/mod.py", "x = 0\n")
        self.git("add", "-A")
        self.commit("add pkg")
        result = self.runner.run_shell(
            "printf 'x = 1\\n' > mod.py",
            cwd=self.path("pkg"),
            intent_desc="edit pkg/mod.py",
            expected_files_modified=["mod.py"],
        )
        # A correct edit inside a subdirectory must not be mistaken for drift.
        self.assertEqual(result.changed_files, ("pkg/mod.py",))
        self.assertEqual(result.unexpected_changes, ())
        self.assertIs(result.verdict, Verdict.PASS)

    def test_repo_path_selects_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.runner.run_shell(
                "true",
                cwd=elsewhere,
                intent_desc="edit target.py",
                expected_files_modified=["target.py"],
                repo_path=self.root,
            )
            self.assertEqual(
                os.path.realpath(result.git_repo or ""), os.path.realpath(self.root)
            )
            self.assertIs(result.verdict, Verdict.FAIL)

    def test_failed_command_with_correct_edit_is_false_failure(self) -> None:
        result = self.runner.run_shell(
            "printf 'value = 1\\n' > target.py; exit 1",
            cwd=self.root,
            intent_desc="edit target.py",
            expected_files_modified=["target.py"],
        )
        self.assertTrue(result.effect_observed)
        self.assertFalse(result.check_observed)
        self.assertIs(result.verdict, Verdict.FAIL)

    def test_not_a_repo_raises(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(GitWitnessError):
                self.runner.run_shell(
                    "true",
                    cwd=plain,
                    intent_desc="edit something",
                    expected_files_modified=["x.py"],
                )
            # The argument error must surface before any command runs.
            self.assertEqual(os.listdir(plain), [])

    def test_bare_string_expected_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.runner.run_shell(
                "true",
                cwd=self.root,
                intent_desc="edit target.py",
                expected_files_modified="target.py",  # type: ignore[arg-type]
            )

    def test_empty_expected_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.runner.run_shell(
                "true",
                cwd=self.root,
                intent_desc="edit nothing",
                expected_files_modified=[],
            )

    def test_file_and_git_witnesses_are_a_conjunction(self) -> None:
        # Both witnesses agree here: the file exists and git sees it.
        result = self.runner.run_shell(
            "printf 'ok\\n' > out.txt",
            cwd=self.root,
            intent_desc="write out.txt",
            expected_file="out.txt",
            expected_content="ok",
            expected_files_modified=["out.txt"],
        )
        self.assertTrue(result.effect_observed)
        self.assertEqual(result.unexpected_changes, ())
        self.assertIs(result.verdict, Verdict.PASS)

    def test_disagreeing_witnesses_cannot_pass(self) -> None:
        # Git says the intended path moved; the file witness points elsewhere
        # and fails. One false is enough to withhold a PASS.
        result = self.runner.run_shell(
            "printf 'value = 1\\n' > target.py",
            cwd=self.root,
            intent_desc="edit target.py and write marker",
            expected_file="marker",
            expected_files_modified=["target.py"],
        )
        self.assertFalse(result.effect_observed)
        self.assertIs(result.verdict, Verdict.FAIL)


if __name__ == "__main__":
    unittest.main()
