"""Tests for the external judgment base.

The loader is the part of this feature most likely to fail quietly: a rule
that does not load, or loads with a field silently dropped, leaves an audit
that looks configured and enforces nothing. These tests therefore spend most
of their weight on rejection rather than acceptance.
"""

import os
import shutil
import tempfile
import unittest

from audit_sidecar.judgment_base import (
    DEFAULT_RULES_DIRNAME,
    JudgmentBase,
    RuleError,
    default_rules_dir,
    default_rules_dirs,
    packaged_rules_dir,
)

SHIPPED_RULES = packaged_rules_dir()

VALID_RULE = """\
rule_id: "no_file_drift"
target_tool: "write_file"
require_witness: true
witness_type: "git_diff"
expectation: "file_modified"
"""


class JudgmentBaseTestCase(unittest.TestCase):
    """Shared fixture: a temporary rules directory."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="harness-rules-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def write_rule(self, name, text):
        path = os.path.join(self.root, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class TestShippedRules(JudgmentBaseTestCase):
    """The rules that actually ship with the package must load and match."""

    def test_shipped_directory_loads(self):
        base = JudgmentBase(SHIPPED_RULES)
        self.assertGreaterEqual(len(base), 1)
        self.assertIn("no_file_drift", base.rule_ids)

    def test_shipped_rule_targets_write_file(self):
        base = JudgmentBase(SHIPPED_RULES)
        rules = base.get_rules_for_tool("write_file")
        self.assertTrue(any(r["rule_id"] == "no_file_drift" for r in rules))

    def test_shipped_rule_demands_git_witness(self):
        base = JudgmentBase(SHIPPED_RULES)
        rule = base.get_rules_for_tool("write_file")[0]
        self.assertTrue(rule["require_witness"])
        self.assertEqual(rule["witness_type"], "git_diff")
        self.assertEqual(rule["expectation"], "file_modified")

    def test_default_rules_dir_finds_a_directory(self):
        found = default_rules_dir()
        self.assertIsNotNone(found)
        self.assertTrue(os.path.isdir(found))


class TestLoading(JudgmentBaseTestCase):
    def test_loads_single_mapping(self):
        self.write_rule("write_file.yaml", VALID_RULE)
        base = JudgmentBase(self.root)
        self.assertEqual(base.rule_ids, ("no_file_drift",))
        self.assertEqual(len(base), 1)

    def test_accepts_yml_extension(self):
        self.write_rule("write_file.yml", VALID_RULE)
        self.assertEqual(len(JudgmentBase(self.root)), 1)

    def test_loads_list_of_rules(self):
        self.write_rule(
            "many.yaml",
            "- rule_id: one\n  target_tool: write_file\n"
            "- rule_id: two\n  target_tool: append_file\n",
        )
        base = JudgmentBase(self.root)
        self.assertEqual(set(base.rule_ids), {"one", "two"})

    def test_ignores_non_yaml_files(self):
        self.write_rule("write_file.yaml", VALID_RULE)
        self.write_rule("notes.txt", "this is not a rule")
        self.write_rule("README.md", "# not a rule")
        self.assertEqual(len(JudgmentBase(self.root)), 1)

    def test_load_order_is_deterministic(self):
        self.write_rule("b.yaml", "rule_id: bee\ntarget_tool: t\n")
        self.write_rule("a.yaml", "rule_id: ay\ntarget_tool: t\n")
        self.assertEqual(JudgmentBase(self.root).rule_ids, ("ay", "bee"))

    def test_metadata_fields_do_not_break_loading(self):
        self.write_rule(
            "annotated.yaml",
            'rule_id: r\ntarget_tool: t\ndescription: "why"\nnote: "context"\n',
        )
        self.assertEqual(len(JudgmentBase(self.root)), 1)

    def test_missing_explicit_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            JudgmentBase(os.path.join(self.root, "does-not-exist"))

    def test_empty_directory_loads_zero_rules(self):
        # A directory that exists but holds no rules is not an error at load
        # time; callers must be able to detect it via len(). The warning is
        # asserted so the "configured but empty" case stays visible.
        with self.assertLogs("audit_sidecar.judgment_base", level="WARNING"):
            base = JudgmentBase(self.root)
        self.assertEqual(len(base), 0)

    def test_rules_dir_is_exposed(self):
        self.write_rule("write_file.yaml", VALID_RULE)
        self.assertEqual(JudgmentBase(self.root).rules_dir, self.root)


class TestDefaultResolution(JudgmentBaseTestCase):
    """Resolution order decides which policy is live; it must be predictable."""

    def test_packaged_rules_ship_inside_the_package(self):
        # The whole point of v0.6.1: a wheel install has a working default.
        self.assertTrue(os.path.isdir(packaged_rules_dir()))
        self.assertTrue(
            os.path.isfile(os.path.join(packaged_rules_dir(), "write_file.yaml"))
        )
        self.assertGreaterEqual(len(JudgmentBase(packaged_rules_dir())), 1)

    def test_local_rules_directory_wins_over_packaged(self):
        self.write_rule("rules/local.yaml", "rule_id: local_rule\ntarget_tool: t\n")
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(default_rules_dir(), os.path.join(self.root, "rules"))

    def test_environment_override_wins_over_everything(self):
        env_dir = os.path.join(self.root, "from-env")
        os.makedirs(env_dir)
        previous = os.environ.get("HARNESS_AUDIT_RULES")
        os.environ["HARNESS_AUDIT_RULES"] = env_dir
        self.addCleanup(
            lambda: os.environ.pop("HARNESS_AUDIT_RULES", None)
            if previous is None
            else os.environ.__setitem__("HARNESS_AUDIT_RULES", previous)
        )
        self.assertEqual(default_rules_dir(), env_dir)

    def test_environment_override_pointing_nowhere_raises(self):
        # A typo in the override must not be papered over by the packaged
        # default; the operator would think their policy was in force.
        previous = os.environ.get("HARNESS_AUDIT_RULES")
        os.environ["HARNESS_AUDIT_RULES"] = os.path.join(self.root, "missing")
        self.addCleanup(
            lambda: os.environ.pop("HARNESS_AUDIT_RULES", None)
            if previous is None
            else os.environ.__setitem__("HARNESS_AUDIT_RULES", previous)
        )
        with self.assertRaises(FileNotFoundError):
            default_rules_dir()


class TestLayerMerging(JudgmentBaseTestCase):
    """A local ``./rules`` may add rules or tighten shipped ones -- never remove one.

    The defect this pins: ``default_rules_dir`` used to return the local
    directory *instead of* the packaged one, so a working tree could ship a
    "policy" that quietly deleted ``no_file_drift``/``append_landed`` while the
    audit still looked configured.
    """

    def _in(self, directory):
        previous = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, previous)
        previous_env = os.environ.pop("HARNESS_AUDIT_RULES", None)
        if previous_env is not None:
            self.addCleanup(
                os.environ.__setitem__, "HARNESS_AUDIT_RULES", previous_env
            )

    def test_default_rules_dirs_merges_local_and_packaged(self):
        self.write_rule("rules/local.yaml", "rule_id: local_rule\ntarget_tool: t\n")
        self._in(self.root)
        dirs = default_rules_dirs()
        self.assertEqual(len(dirs), 2)
        self.assertEqual(dirs[0], os.path.join(self.root, "rules"))
        self.assertEqual(
            os.path.realpath(dirs[1]), os.path.realpath(packaged_rules_dir())
        )

    def test_local_addition_does_not_remove_packaged_rules(self):
        self.write_rule(
            "rules/unrelated.yaml",
            "rule_id: unrelated\ntarget_tool: unrelated_tool\n",
        )
        self._in(self.root)
        base = JudgmentBase()
        self.assertIn("no_file_drift", base.rule_ids)
        self.assertIn("append_landed", base.rule_ids)
        self.assertEqual(
            [r["rule_id"] for r in base.get_rules_for_tool("write_file")],
            ["no_file_drift"],
        )
        self.assertEqual(
            [r["rule_id"] for r in base.get_rules_for_tool("append_file")],
            ["append_landed"],
        )

    def test_weaker_local_redefinition_is_rejected(self):
        # The shadow: same rule_id, no requirements at all. Accepting it would
        # erase the shipped guard while the id still looked present.
        self.write_rule(
            "rules/shadow.yaml",
            'rule_id: "no_file_drift"\ntarget_tool: "write_file"\n',
        )
        self._in(self.root)
        with self.assertRaises(RuleError) as caught:
            JudgmentBase()
        self.assertIn("no_file_drift", str(caught.exception))

    def test_equal_local_redefinition_is_rejected(self):
        # An identical copy adds nothing and makes the provenance of the live
        # rule ambiguous, so it is refused rather than silently preferred.
        self.write_rule(
            "rules/copy.yaml",
            'rule_id: "no_file_drift"\ntarget_tool: "write_file"\n'
            'require_witness: true\nwitness_type: "git_diff"\n'
            'expectation: "file_modified"\nrisk_level: "high"\n',
        )
        self._in(self.root)
        with self.assertRaises(RuleError):
            JudgmentBase()

    def test_stricter_local_redefinition_is_allowed_and_recorded(self):
        self.write_rule(
            "rules/tight.yaml",
            'rule_id: "append_landed"\ntarget_tool: "append_file"\n'
            'require_witness: true\nwitness_type: "git_diff"\n'
            'expectation: "file_modified"\nrisk_level: "high"\n',
        )
        self._in(self.root)
        base = JudgmentBase()
        self.assertIn("append_landed", base.tightened_rule_ids)
        rule = base.get_rules_for_tool("append_file")[0]
        self.assertTrue(rule["require_witness"])
        self.assertEqual(rule["witness_type"], "git_diff")
        self.assertEqual(rule["risk_level"], "high")

    def test_single_explicit_directory_is_not_merged(self):
        # An operator naming a directory means exactly that directory; merging
        # the shipped policy in would be a second, unrequested opinion.
        self.write_rule("one.yaml", "rule_id: only\ntarget_tool: t\n")
        base = JudgmentBase(self.root)
        self.assertEqual(base.rule_ids, ("only",))
        self.assertEqual(base.rules_dirs, (self.root,))
        self.assertEqual(base.tightened_rule_ids, ())


class TestRejection(JudgmentBaseTestCase):
    """Every one of these would otherwise become a silently dead guard."""

    def test_unknown_field_is_rejected(self):
        self.write_rule(
            "typo.yaml",
            "rule_id: r\ntarget_tool: t\nrequire_witnes: true\n",
        )
        with self.assertRaises(RuleError) as caught:
            JudgmentBase(self.root)
        self.assertIn("require_witnes", str(caught.exception))

    def test_missing_rule_id_is_rejected(self):
        self.write_rule("no-id.yaml", "target_tool: t\n")
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)

    def test_missing_target_tool_is_rejected(self):
        self.write_rule("no-tool.yaml", "rule_id: r\n")
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)

    def test_blank_rule_id_is_rejected(self):
        self.write_rule("blank.yaml", 'rule_id: "  "\ntarget_tool: t\n')
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)

    def test_unsupported_witness_type_is_rejected(self):
        self.write_rule(
            "bad-witness.yaml",
            'rule_id: r\ntarget_tool: t\nrequire_witness: true\n'
            'witness_type: "vibes"\n',
        )
        with self.assertRaises(RuleError) as caught:
            JudgmentBase(self.root)
        self.assertIn("vibes", str(caught.exception))

    def test_unsupported_expectation_is_rejected(self):
        self.write_rule(
            "bad-expectation.yaml",
            'rule_id: r\ntarget_tool: t\nexpectation: "file_created"\n',
        )
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)

    def test_require_witness_without_type_is_rejected(self):
        # Such a rule could never be checked, so it would read as a gate while
        # behaving as a comment.
        self.write_rule(
            "incomplete.yaml",
            "rule_id: r\ntarget_tool: t\nrequire_witness: true\n",
        )
        with self.assertRaises(RuleError) as caught:
            JudgmentBase(self.root)
        self.assertIn("witness_type", str(caught.exception))

    def test_witness_type_without_require_is_rejected(self):
        self.write_rule(
            "dead.yaml",
            'rule_id: r\ntarget_tool: t\nwitness_type: "git_diff"\n',
        )
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)

    def test_non_bool_require_witness_is_rejected(self):
        self.write_rule(
            "stringy.yaml",
            'rule_id: r\ntarget_tool: t\nrequire_witness: "true"\n',
        )
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)

    def test_duplicate_rule_id_across_files_is_rejected(self):
        self.write_rule("a.yaml", "rule_id: same\ntarget_tool: write_file\n")
        self.write_rule("b.yaml", "rule_id: same\ntarget_tool: append_file\n")
        with self.assertRaises(RuleError) as caught:
            JudgmentBase(self.root)
        self.assertIn("duplicate rule_id", str(caught.exception))

    def test_invalid_yaml_is_rejected(self):
        self.write_rule("broken.yaml", "rule_id: [unclosed\n")
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)

    def test_empty_file_is_rejected(self):
        self.write_rule("empty.yaml", "")
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)

    def test_scalar_document_is_rejected(self):
        self.write_rule("scalar.yaml", "just a string\n")
        with self.assertRaises(RuleError):
            JudgmentBase(self.root)


class TestQueries(JudgmentBaseTestCase):
    def test_returns_only_matching_tool(self):
        self.write_rule("write.yaml", "rule_id: w\ntarget_tool: write_file\n")
        base = JudgmentBase(self.root)
        self.assertEqual(
            [r["rule_id"] for r in base.get_rules_for_tool("write_file")], ["w"]
        )

    def test_unmatched_tool_returns_empty_list(self):
        self.write_rule("write.yaml", "rule_id: w\ntarget_tool: write_file\n")
        base = JudgmentBase(self.root)
        self.assertEqual(base.get_rules_for_tool("append_file"), [])

    def test_wildcard_rule_applies_to_every_tool(self):
        self.write_rule("wild.yaml", 'rule_id: everywhere\ntarget_tool: "*"\n')
        base = JudgmentBase(self.root)
        self.assertEqual(
            [r["rule_id"] for r in base.get_rules_for_tool("anything")],
            ["everywhere"],
        )

    def test_returned_dicts_are_copies(self):
        self.write_rule("write.yaml", "rule_id: w\ntarget_tool: write_file\n")
        base = JudgmentBase(self.root)
        rules = base.get_rules_for_tool("write_file")
        rules[0]["rule_id"] = "mutated"
        self.assertNotIn("mutated", base.rule_ids)

    def test_blank_tool_name_is_rejected(self):
        self.write_rule("write.yaml", "rule_id: w\ntarget_tool: write_file\n")
        base = JudgmentBase(self.root)
        with self.assertRaises(ValueError):
            base.get_rules_for_tool("")

    def test_repr_mentions_rule_count(self):
        self.write_rule("write.yaml", "rule_id: w\ntarget_tool: write_file\n")
        self.assertIn("rules=1", repr(JudgmentBase(self.root)))


if __name__ == "__main__":
    unittest.main()
