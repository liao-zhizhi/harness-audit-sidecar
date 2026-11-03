"""Tests for the Rule Interface: signed packages, parsing and mounting.

The weight is on the two failure modes that would make an ecosystem rule base
decorative:

1. **A tampered package is installed anyway.** Every integrity test asserts that
   the registry is left untouched, not merely that an exception was raised.
2. **A rule is loaded but cannot be enforced.** The mounting tests use a real
   ``MembraneGateway`` and a real ledger, so "mounted" means the package crossed
   the membrane and its denials are in the chain.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

import yaml

from audit_sidecar.tools.membrane import MembraneGateway, MembraneRejectError
from audit_sidecar.tools.rule_interface import (
    PACKAGE_SCHEMA,
    RuleIntegrityError,
    RuleInterface,
    RuleRegistry,
    RuleSchemaError,
    RuleViolation,
    normalise_digest,
    sha256_digest,
)


def rule_document(**overrides):
    rule = {
        "rule_id": "org.example.safe-web3-transfer",
        "version": "1.0.0",
        "author": "Example Web3 Safety Guild",
        "target_tool": "web3_transfer",
        "interface_spec": {
            "required_witnesses": ["git_diff", "compiler_exit_code"],
            "expectation": "file_modified",
            "capability": "write",
        },
        "penalty_weight": 40.0,
        "risk_level": "high",
        "description": "A transfer must carry a reviewed diff and a compiler result.",
    }
    rule.update(overrides)
    return rule


def package_document(rules=None, **header):
    if rules is None:
        rules = [rule_document()]
    metadata = {
        "name": "web3-safety",
        "version": "1.0.0",
        "author": "Example Web3 Safety Guild",
    }
    metadata.update(header)
    return yaml.safe_dump(
        {
            "schema": PACKAGE_SCHEMA,
            "package": metadata,
            "rules": rules,
        },
        sort_keys=False,
    )


def digest_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = RuleRegistry()

    def install(self, text=None, *, expected=None, source="<test>"):
        content = package_document() if text is None else text
        return self.registry.install(
            content,
            expected_digest=expected or digest_of(content),
            source=source,
        )


# ---------------------------------------------------------------------------
# Parsing: the schema is the contract an external author writes against
# ---------------------------------------------------------------------------


class TestParsing(RegistryTestCase):
    def test_package_exposes_every_interface_field(self):
        package = self.install()
        self.assertEqual(package.name, "web3-safety")
        self.assertEqual(package.version, "1.0.0")
        self.assertEqual(package.author, "Example Web3 Safety Guild")
        self.assertEqual(package.digest, sha256_digest(package.raw))
        self.assertEqual(len(package.rules), 1)

        rule = package.rules[0]
        self.assertEqual(rule.rule_id, "org.example.safe-web3-transfer")
        self.assertEqual(rule.namespace, "org.example")
        self.assertEqual(rule.version, "1.0.0")
        self.assertEqual(rule.author, "Example Web3 Safety Guild")
        self.assertEqual(
            rule.interface_spec.required_witnesses,
            ("git_diff", "compiler_exit_code"),
        )
        self.assertEqual(rule.interface_spec.expectation, "file_modified")
        self.assertEqual(rule.interface_spec.capability, "write")
        self.assertEqual(rule.penalty_weight, 40.0)
        self.assertEqual(rule.package_name, "web3-safety")
        self.assertEqual(rule.package_digest, package.digest)

    def test_interface_spec_accepts_a_list_shorthand(self):
        text = package_document(
            [rule_document(interface_spec=["git_diff", "test_exit_code"])]
        )
        rule = self.install(text).rules[0]
        self.assertEqual(
            rule.interface_spec.required_witnesses, ("git_diff", "test_exit_code")
        )
        self.assertEqual(rule.interface_spec.capability, "write")
        self.assertEqual(rule.interface_spec.expectation, "file_modified")

    def test_interface_spec_accepts_a_single_witness_string(self):
        text = package_document([rule_document(interface_spec="git_diff")])
        rule = self.install(text).rules[0]
        self.assertEqual(rule.interface_spec.required_witnesses, ("git_diff",))

    def test_rule_can_inherit_version_and_author_from_the_package(self):
        rule = rule_document()
        del rule["version"]
        del rule["author"]
        text = package_document([rule])
        parsed = self.install(text).rules[0]
        self.assertEqual(parsed.version, "1.0.0")
        self.assertEqual(parsed.author, "Example Web3 Safety Guild")

    def test_bare_rule_document_is_accepted_for_local_files(self):
        text = yaml.safe_dump(rule_document(), sort_keys=False)
        package = self.install(text, source="/tmp/safe-web3-transfer.yaml")
        self.assertEqual(package.name, "safe-web3-transfer")
        self.assertEqual(package.rules[0].rule_id, "org.example.safe-web3-transfer")

    def test_bare_list_document_is_accepted(self):
        other = rule_document(
            rule_id="org.example.no-unlimited-approval",
            target_tool="web3_approve",
            interface_spec={"required_witnesses": ["static_analysis_report"]},
            penalty_weight=60,
        )
        text = yaml.safe_dump([rule_document(), other], sort_keys=False)
        package = self.install(text, source="bundle.yaml")
        self.assertEqual(len(package.rules), 2)
        self.assertEqual(package.name, "bundle")

    def test_wildcard_target_is_kept_verbatim(self):
        text = package_document([rule_document(target_tool="*")])
        rule = self.install(text).rules[0]
        self.assertEqual(rule.target_tool, "*")

    def test_duplicate_witness_names_are_deduplicated(self):
        text = package_document(
            [rule_document(interface_spec=["git_diff", "git_diff"])]
        )
        rule = self.install(text).rules[0]
        self.assertEqual(rule.interface_spec.required_witnesses, ("git_diff",))

    def test_as_dict_is_json_friendly(self):
        rule = self.install().rules[0]
        record = rule.as_dict()
        self.assertEqual(record["rule_id"], "org.example.safe-web3-transfer")
        self.assertEqual(
            record["interface_spec"]["required_witnesses"],
            ["git_diff", "compiler_exit_code"],
        )
        self.assertEqual(record["penalty_weight"], 40.0)


# ---------------------------------------------------------------------------
# Schema rejection: a field silently dropped is a guard silently disabled
# ---------------------------------------------------------------------------


class TestSchemaRejection(RegistryTestCase):
    def assert_rejected(self, text, contains=None):
        with self.assertRaises(RuleSchemaError) as caught:
            self.install(text)
        if contains is not None:
            self.assertIn(contains, str(caught.exception))
        self.assertEqual(len(self.registry), 0)

    def test_missing_penalty_weight_is_rejected(self):
        rule = rule_document()
        del rule["penalty_weight"]
        self.assert_rejected(package_document([rule]), "penalty_weight")

    def test_negative_penalty_weight_is_rejected(self):
        self.assert_rejected(
            package_document([rule_document(penalty_weight=-1)]), ">= 0"
        )

    def test_boolean_penalty_weight_is_rejected(self):
        self.assert_rejected(
            package_document([rule_document(penalty_weight=True)]), "number"
        )

    def test_nan_penalty_weight_is_rejected(self):
        text = package_document().replace("penalty_weight: 40.0", "penalty_weight: .nan")
        self.assert_rejected(text, "finite")

    def test_bad_version_is_rejected(self):
        self.assert_rejected(
            package_document([rule_document(version="1.0")]), "semver"
        )

    def test_blank_author_is_rejected(self):
        self.assert_rejected(package_document([rule_document(author="   ")]), "author")

    def test_blank_rule_id_is_rejected(self):
        self.assert_rejected(package_document([rule_document(rule_id="")]), "rule_id")

    def test_rule_id_with_whitespace_is_rejected(self):
        self.assert_rejected(
            package_document([rule_document(rule_id="org example rule")]),
            "namespaced",
        )

    def test_missing_target_tool_is_rejected(self):
        rule = rule_document()
        del rule["target_tool"]
        self.assert_rejected(package_document([rule]), "target_tool")

    def test_unknown_rule_field_is_rejected(self):
        self.assert_rejected(
            package_document([rule_document(penalty=5)]), "unknown rule field"
        )

    def test_missing_interface_spec_is_rejected(self):
        rule = rule_document()
        del rule["interface_spec"]
        self.assert_rejected(package_document([rule]), "interface_spec")

    def test_unsupported_interface_witness_is_rejected(self):
        self.assert_rejected(
            package_document([rule_document(interface_spec=["vibes"])]),
            "unsupported interface witness",
        )

    def test_empty_interface_witnesses_are_rejected(self):
        self.assert_rejected(
            package_document([rule_document(interface_spec=[])]),
            "no witnesses",
        )

    def test_unknown_interface_spec_field_is_rejected(self):
        self.assert_rejected(
            package_document(
                [
                    rule_document(
                        interface_spec={
                            "required_witnesses": ["git_diff"],
                            "require_witnes": True,
                        }
                    )
                ]
            ),
            "unknown interface_spec field",
        )

    def test_unknown_capability_is_rejected(self):
        self.assert_rejected(
            package_document(
                [
                    rule_document(
                        interface_spec={
                            "required_witnesses": ["git_diff"],
                            "capability": "admin",
                        }
                    )
                ]
            ),
            "capability",
        )

    def test_unknown_expectation_is_rejected(self):
        self.assert_rejected(
            package_document(
                [
                    rule_document(
                        interface_spec={
                            "required_witnesses": ["git_diff"],
                            "expectation": "file_created",
                        }
                    )
                ]
            ),
            "expectation",
        )

    def test_unknown_risk_level_is_rejected(self):
        self.assert_rejected(
            package_document([rule_document(risk_level="critical")]), "risk_level"
        )

    def test_unknown_package_field_is_rejected(self):
        document = yaml.safe_load(package_document())
        document["typo"] = True
        self.assert_rejected(yaml.safe_dump(document, sort_keys=False), "unknown package field")

    def test_unknown_package_header_field_is_rejected(self):
        document = yaml.safe_load(package_document())
        document["package"]["license"] = "MIT"
        self.assert_rejected(
            yaml.safe_dump(document, sort_keys=False), "unknown package header"
        )

    def test_wrong_schema_version_is_rejected(self):
        document = yaml.safe_load(package_document())
        document["schema"] = "harness-audit/rule-package/9.9"
        self.assert_rejected(
            yaml.safe_dump(document, sort_keys=False), "unsupported package schema"
        )

    def test_duplicate_rule_id_within_one_package_is_rejected(self):
        self.assert_rejected(
            package_document([rule_document(), rule_document()]),
            "duplicate rule_id",
        )

    def test_empty_package_is_rejected(self):
        document = yaml.safe_load(package_document())
        document["rules"] = []
        self.assert_rejected(yaml.safe_dump(document, sort_keys=False), "non-empty list")

    def test_invalid_yaml_is_rejected(self):
        self.assert_rejected("rules: [unclosed\n", "not valid YAML")


# ---------------------------------------------------------------------------
# Integrity: the digest is checked before the parser sees a single byte
# ---------------------------------------------------------------------------


class TestIntegrity(RegistryTestCase):
    def test_matching_digest_installs(self):
        text = package_document()
        package = self.install(text, expected=digest_of(text))
        self.assertEqual(package.digest, "sha256:" + digest_of(text))

    def test_digest_without_prefix_is_accepted(self):
        text = package_document()
        package = self.install(text, expected=digest_of(text))
        self.assertTrue(package.digest.startswith("sha256:"))

    def test_tampered_package_is_rejected(self):
        text = package_document()
        original = digest_of(text)
        tampered = text.replace("penalty_weight: 40.0", "penalty_weight: 0.0")
        with self.assertRaises(RuleIntegrityError) as caught:
            self.registry.install(tampered, expected_digest=original, source="tampered")
        self.assertIn("SHA256 mismatch", str(caught.exception))
        self.assertEqual(len(self.registry), 0)
        self.assertEqual(self.registry.rule_ids, ())

    def test_malformed_digest_is_rejected(self):
        with self.assertRaises(RuleIntegrityError):
            normalise_digest("not-a-digest")

    def test_sha256_digest_normalises_bytes_and_text_the_same(self):
        text = package_document()
        self.assertEqual(sha256_digest(text), sha256_digest(text.encode("utf-8")))

    def test_signature_can_be_checked_without_installing(self):
        text = package_document()
        self.assertEqual(
            RuleRegistry.verify_signature(text, digest_of(text)),
            sha256_digest(text),
        )
        with self.assertRaises(RuleIntegrityError):
            RuleRegistry.verify_signature(text, "0" * 64)
        self.assertEqual(len(self.registry), 0)

    def test_reinstalling_the_same_bytes_is_idempotent(self):
        text = package_document()
        first = self.install(text)
        second = self.install(text)
        self.assertIs(first, second)
        self.assertEqual(len(self.registry), 1)

    def test_same_package_name_with_different_bytes_is_rejected(self):
        self.install(package_document())
        other = package_document([rule_document(penalty_weight=99.0)])
        with self.assertRaises(Exception) as caught:
            self.install(other)
        self.assertIn("already installed", str(caught.exception))

    def test_same_rule_id_from_two_packages_is_rejected(self):
        self.install(package_document())
        other = package_document([rule_document()], name="another-package")
        with self.assertRaises(Exception) as caught:
            self.install(other)
        self.assertIn("already installed", str(caught.exception))


# ---------------------------------------------------------------------------
# Local directory and remote transport
# ---------------------------------------------------------------------------


class LocalDirectoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-rule-interface-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def write_signed(self, name, text=None, digest=None):
        text = package_document() if text is None else text
        path = self.write(name, text)
        with open(path + ".sha256", "w", encoding="utf-8") as handle:
            handle.write((digest or digest_of(text)) + "  " + name + "\n")
        return path

    def test_directory_loads_signed_packages(self):
        self.write_signed("web3.yaml")
        registry = RuleRegistry(self.root)
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry.rule_ids, ("org.example.safe-web3-transfer",))

    def test_directory_ignores_non_yaml_files(self):
        self.write_signed("web3.yaml")
        self.write("README.md", "not a rule")
        self.write("notes.txt", "not a rule")
        self.assertEqual(len(RuleRegistry(self.root)), 1)

    def test_missing_sidecar_is_refused(self):
        self.write("web3.yaml", package_document())
        with self.assertRaises(RuleIntegrityError) as caught:
            RuleRegistry(self.root)
        self.assertIn("sidecar", str(caught.exception))

    def test_tampered_local_file_is_refused(self):
        path = self.write_signed("web3.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(package_document([rule_document(penalty_weight=0.0)]))
        with self.assertRaises(RuleIntegrityError):
            RuleRegistry(self.root)

    def test_explicit_signatures_mapping_is_accepted(self):
        text = package_document()
        self.write("web3.yaml", text)
        registry = RuleRegistry()
        registry.load_directory(self.root, signatures={"web3.yaml": digest_of(text)})
        self.assertEqual(len(registry), 1)

    def test_load_local_accepts_a_single_file(self):
        text = package_document()
        path = self.write("web3.yaml", text)
        registry = RuleRegistry()
        package = registry.load_local(path, expected_digest=digest_of(text))
        self.assertEqual(package.name, "web3-safety")
        self.assertEqual(len(registry), 1)

    def test_missing_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            RuleRegistry(os.path.join(self.root, "missing"))

    def test_duplicate_rule_across_local_packages_is_refused(self):
        first = package_document()
        second = package_document([rule_document()], name="other-package")
        self.write("a.yaml", first)
        self.write("b.yaml", second)
        registry = RuleRegistry()
        with self.assertRaises(Exception) as caught:
            registry.load_directory(
                self.root,
                signatures={
                    "a.yaml": digest_of(first),
                    "b.yaml": digest_of(second),
                },
            )
        self.assertIn("already installed", str(caught.exception))


class RemotePullTest(unittest.TestCase):
    URL = "https://rules.example.org/web3_safety_rules.yaml"

    def setUp(self):
        self.registry = RuleRegistry()
        self.text = package_document()
        self.raw = self.text.encode("utf-8")
        self.digest = digest_of(self.text)

    def transport(self, files):
        def fetch(url):
            if url not in files:
                raise FileNotFoundError(url)
            return files[url]

        return fetch

    def test_pull_installs_a_verified_package(self):
        files = {self.URL: self.raw}
        package = self.registry.pull(
            self.URL,
            expected_digest=self.digest,
            transport=self.transport(files),
        )
        self.assertEqual(package.source, self.URL)
        self.assertEqual(package.name, "web3-safety")
        self.assertEqual(len(self.registry), 1)

    def test_tampered_remote_package_is_refused(self):
        tampered = self.text.replace(
            "penalty_weight: 40.0", "penalty_weight: 0.0"
        ).encode("utf-8")
        files = {self.URL: tampered}
        with self.assertRaises(RuleIntegrityError):
            self.registry.pull(
                self.URL,
                expected_digest=self.digest,
                transport=self.transport(files),
            )
        self.assertEqual(len(self.registry), 0)
        self.assertEqual(self.registry.rule_ids, ())

    def test_missing_digest_and_missing_sidecar_is_refused(self):
        files = {self.URL: self.raw}
        with self.assertRaises(RuleIntegrityError) as caught:
            self.registry.pull(self.URL, transport=self.transport(files))
        self.assertIn("without a SHA256 digest", str(caught.exception))
        self.assertEqual(len(self.registry), 0)

    def test_sidecar_digest_is_used_when_no_explicit_digest_is_given(self):
        files = {
            self.URL: self.raw,
            self.URL + ".sha256": (self.digest + "  web3_safety_rules.yaml\n").encode(),
        }
        with self.assertLogs(
            "audit_sidecar.tools.rule_interface", level="WARNING"
        ) as logs:
            package = self.registry.pull(self.URL, transport=self.transport(files))
        self.assertEqual(package.digest, "sha256:" + self.digest)
        self.assertTrue(any("same channel" in line for line in logs.output))

    def test_fetch_error_is_reported(self):
        with self.assertRaises(RuleIntegrityError):
            self.registry.pull(
                "https://rules.example.org/missing.yaml",
                expected_digest=self.digest,
                transport=self.transport({}),
            )


# ---------------------------------------------------------------------------
# Mounting on the membrane
# ---------------------------------------------------------------------------


class MountTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-rule-mount-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ledger_path = os.path.join(self.root, "audit.jsonl")

        self.text = package_document()
        self.registry = RuleRegistry()
        self.registry.install(
            self.text,
            expected_digest=digest_of(self.text),
            source="https://rules.example.org/web3_safety_rules.yaml",
        )
        self.gateway = MembraneGateway(self.ledger_path)
        self.interface = self.registry.mount(self.gateway)

    def entries(self):
        return list(self.gateway.ledger.entries())

    def make_artefact(self, name="transfer.diff", body="diff --git a/x b/x\n"):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def file_witness(self, path):
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        return {"kind": "file", "path": path, "digest": "sha256:" + digest}

    def test_mount_is_a_membrane_crossing_not_just_a_dictionary_update(self):
        entries = self.entries()
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0]["event"], "registration")
        self.assertEqual(entries[1]["event"], "grant")
        self.assertEqual(entries[2]["event"], "redeem")
        self.assertEqual(entries[3]["tool"], "rule_interface.mount")
        self.assertEqual(entries[3]["record_type"], "call")
        self.assertEqual(self.gateway.verify()["status"], "PASS")

    def test_mount_registers_the_package_digest(self):
        package = self.registry.packages[0]
        registration = self.entries()[0]
        self.assertEqual(registration["digest"], package.digest)
        self.assertEqual(registration["capability"], "read")

    def test_mounted_packages_are_reported_as_data(self):
        mounted = self.interface.mounted_packages
        self.assertIn("web3-safety", mounted)
        record = mounted["web3-safety"]
        self.assertEqual(record["rules"], ("org.example.safe-web3-transfer",))
        self.assertEqual(record["digest"], self.registry.packages[0].digest)
        self.assertTrue(all(isinstance(value, (str, int, tuple)) for value in record.values()))

    def test_empty_registry_mounts_nothing(self):
        empty = RuleRegistry().mount(MembraneGateway(os.path.join(self.root, "empty.jsonl")))
        self.assertEqual(empty.mounted_packages, {})
        self.assertEqual(len(list(empty.gateway.ledger.entries())), 0)

    def test_bad_gateway_is_refused(self):
        with self.assertRaises(TypeError):
            RuleInterface(self.registry, object())

    def test_missing_witness_is_denied_and_recorded(self):
        before = len(self.entries())
        decision = self.interface.enforce(
            "web3_transfer", witnesses={"git_diff"}, agent="demo-agent"
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.violated_rule_ids, ("org.example.safe-web3-transfer",))
        self.assertEqual(
            decision.missing_by_rule["org.example.safe-web3-transfer"],
            ("compiler_exit_code",),
        )
        self.assertEqual(decision.penalty_weight, 40.0)
        self.assertEqual(decision.ledger_seq, before + 1)

        entry = self.entries()[-1]
        self.assertEqual(entry["record_type"], "deny")
        self.assertEqual(entry["event"], "denied")
        self.assertEqual(entry["reason"], "RULE_INTERFACE_VIOLATION")
        self.assertEqual(entry["rule_ids"], ["org.example.safe-web3-transfer"])
        self.assertEqual(entry["penalty_weight"], 40.0)
        self.assertEqual(entry["agent"], "demo-agent")
        self.assertEqual(self.gateway.verify()["status"], "PASS")

    def test_penalty_is_accumulated(self):
        self.assertEqual(self.interface.trust_score, 100.0)
        self.interface.enforce("web3_transfer", witnesses={"git_diff"})
        self.assertEqual(self.interface.penalty_assessed, 40.0)
        self.assertEqual(self.interface.trust_score, 60.0)

    def test_satisfied_rules_allow_without_offering_bytes(self):
        before = len(self.entries())
        decision = self.interface.enforce(
            "web3_transfer",
            witnesses={"git_diff", "compiler_exit_code"},
        )
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.warrant)
        self.assertEqual(len(self.entries()), before)
        self.assertIn("only collection was proven", decision.reason)

    def test_satisfied_rules_with_a_witness_cross_the_membrane(self):
        path = self.make_artefact()
        self.gateway.register_artefact("write", path)

        decision = self.interface.enforce(
            "web3_transfer",
            witnesses={
                "git_diff",
                "compiler_exit_code",
                "test_exit_code",
            },
            witness_payload=self.file_witness(path),
        )
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.warrant)
        self.assertEqual(decision.warrant.uses_remaining, 0)
        self.assertEqual(self.gateway.verify()["status"], "PASS")
        with self.assertRaises(MembraneRejectError):
            self.gateway.redeem(decision.warrant)

    def test_witness_payload_that_is_not_registered_is_still_stopped(self):
        path = self.make_artefact("unregistered.diff", "something else\n")
        with self.assertRaises(MembraneRejectError) as caught:
            self.interface.enforce(
                "web3_transfer",
                witnesses={"git_diff", "compiler_exit_code"},
                witness_payload=self.file_witness(path),
            )
        self.assertEqual(caught.exception.reason, "NOT_REGISTERED")

    def test_no_rule_means_no_interface_opinion(self):
        decision = self.interface.enforce("unrelated_tool", witnesses=())
        self.assertTrue(decision.allowed)
        self.assertIn("no interface rule", decision.reason)
        self.assertEqual(decision.matched_rule_ids, ())

    def test_unknown_witness_name_is_refused(self):
        with self.assertRaises(ValueError):
            self.interface.enforce("web3_transfer", witnesses={"vibes"})

    def test_string_instead_of_witness_collection_is_refused(self):
        with self.assertRaises(TypeError):
            self.interface.enforce("web3_transfer", witnesses="git_diff")

    def test_enforce_or_raise_attaches_the_decision(self):
        with self.assertRaises(RuleViolation) as caught:
            self.interface.enforce_or_raise("web3_transfer", witnesses={"git_diff"})
        self.assertFalse(caught.exception.decision.allowed)
        self.assertEqual(
            caught.exception.decision.violated_rule_ids,
            ("org.example.safe-web3-transfer",),
        )

    def test_wildcard_rule_applies_to_any_tool(self):
        text = package_document(
            [
                rule_document(
                    rule_id="org.example.every-write-needs-a-diff",
                    target_tool="*",
                    interface_spec={"required_witnesses": ["git_diff"]},
                    penalty_weight=10.0,
                )
            ],
            name="universal-guard",
        )
        registry = RuleRegistry()
        registry.install(text, expected_digest=digest_of(text), source="guard.yaml")
        gateway = MembraneGateway(os.path.join(self.root, "wild.jsonl"))
        interface = registry.mount(gateway)
        decision = interface.enforce("anything_at_all", witnesses=())
        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.violated_rule_ids, ("org.example.every-write-needs-a-diff",)
        )


if __name__ == "__main__":
    unittest.main()
