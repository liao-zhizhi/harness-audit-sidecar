"""Rule Interface: a rule base that can be installed, verified and mounted.

The judgment base moved policy out of the auditor and into YAML. That was
necessary but not sufficient for an *ecosystem*. A YAML file on this machine is
a local opinion; a rule package published by another organisation is an
interface, and an interface has to answer three questions the local file never
had to:

    10|
1. **Who is this?** A rule id that is only unique inside one directory cannot
   be distributed. ``rule_id`` is namespaced (``org.example.safe-web3-transfer``)
   and carries ``version`` and ``author`` so a trace can name the policy that
   decided, not just the file it happened to live in.
2. **What does it need?** ``interface_spec`` declares the physical witnesses the
   rule demands (``git_diff``, ``compiler_exit_code``, ...). A rule that cannot
   state its evidence requirement cannot be checked by anyone but its author.
3. **What does breaking it cost?** ``penalty_weight`` makes the consequence
   explicit. A rule whose violation costs nothing is an advertisement, not a
   gate.

The packaged ``rules/`` directory remains the *innate* policy and is still
loaded by :class:`~audit_sidecar.judgment_base.JudgmentBase`. This module is the
distribution layer above it: a package carries its own identity and integrity
proof before any of its rules are allowed to act.

**Integrity.** Every package is installed against an expected SHA256 digest that
is supplied out of band (a publisher page, a lock file, a manifest signed by a
different key). The digest is checked *before* the YAML is parsed, so a tampered
byte never reaches the loader, and a failed install leaves the registry exactly
as it was. The honest limit, stated here rather than discovered later: if the
package and its digest are fetched over the same channel, SHA256 detects
corruption but not an attacker who can rewrite both. It is a transport seal, not
a signature by an author key. The module says "signature" in the same sense
``sha256sum -c`` does.

**Mounting.** :class:`RuleRegistry.mount` injects the installed rules into an
existing :class:`~audit_sidecar.tools.membrane.MembraneGateway` through its
public API only. The package itself has to cross the membrane: its digest is
registered, an inline witness is offered, and the resulting read warrant is
redeemed and logged. Enforcement then turns a missing witness into a denial
record carrying the rule id and ``penalty_weight``. ``membrane.py`` is not
modified, and it does not need to know that rules exist.

Zero dependencies beyond PyYAML, which the judgment base already requires.
"""

from __future__ import annotations

import logging
import math
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

from .ledger import sha256_bytes
from .membrane import CAPABILITIES

__all__ = [
    "EnforcementDecision",
    "InterfaceRule",
    "InterfaceSpec",
    "PACKAGE_SCHEMA",
    "RuleIntegrityError",
    "RuleInterface",
    "RuleInterfaceError",
    "RulePackage",
    "RuleRegistry",
    "RuleSchemaError",
    "RuleViolation",
    "SUPPORTED_INTERFACE_WITNESSES",
    "normalise_digest",
    "sha256_digest",
    "verify_signature",
]

_LOG = logging.getLogger(__name__)

#: Version of the package envelope this loader understands. A different schema
#: is refused rather than interpreted optimistically: a package that means
#: something else to its author must not be read as if it meant this.
PACKAGE_SCHEMA = "harness-audit/rule-package/1.0"

#: Witness vocabulary an interface rule may name. This is deliberately a closed
#: set: a rule demanding evidence no runner can produce would be a guard that
#: always fires, which is indistinguishable from one that never loads. Adding a
#: type is a code change precisely because somebody must agree to collect it.
SUPPORTED_INTERFACE_WITNESSES = frozenset(
    {
        "git_diff",
        "file",
        "agent_report",
        "compiler_exit_code",
        "test_exit_code",
        "lint_exit_code",
        "static_analysis_report",
        "coverage_report",
        "transaction_receipt",
        "simulation_receipt",
    }
)

#: Expectations this layer can describe. The judgment base evaluates them; the
#: interface only guarantees that a package cannot name one nobody evaluates.
SUPPORTED_EXPECTATIONS = frozenset({"file_modified"})

SUPPORTED_RISK_LEVELS = frozenset({"low", "high"})

_RULE_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)*$"
)
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_RULE_FIELDS = frozenset(
    {
        "rule_id",
        "version",
        "author",
        "target_tool",
        "interface_spec",
        "penalty_weight",
        "risk_level",
        "description",
        "note",
    }
)

_INTERFACE_SPEC_FIELDS = frozenset(
    {"required_witnesses", "witnesses", "witness_type", "expectation", "capability"}
)

_PACKAGE_FIELDS = frozenset(
    {"schema", "package", "rules", "description", "note"}
)

_PACKAGE_HEADER_FIELDS = frozenset(
    {"name", "version", "author", "description", "note"}
)

#: Pseudo-tool recorded when a package is admitted through the membrane. It is
#: not an executable tool; it is an audited action, and the prefix keeps it from
#: colliding with a real one in a trace.
MOUNT_TOOL = "rule_interface.mount"

_REJECT_MISSING_WITNESS = "RULE_INTERFACE_VIOLATION"

Transport = Callable[[str], Any]


class RuleInterfaceError(ValueError):
    """A rule package or an enforcement request is not usable."""


class RuleIntegrityError(RuleInterfaceError):
    """The bytes do not match the digest that was supposed to vouch for them."""


class RuleSchemaError(RuleInterfaceError):
    """The package is well sealed but does not say something this loader knows."""


class RuleViolation(RuleInterfaceError):
    """An enforcement request was denied; the decision is attached."""

    def __init__(self, decision: "EnforcementDecision") -> None:
        super().__init__(decision.reason)
        self.decision = decision


def sha256_digest(content: Any) -> str:
    """The ``sha256:<hex>`` digest of a package's exact bytes."""
    return "sha256:" + sha256_bytes(_as_bytes(content))


def normalise_digest(value: Any) -> str:
    """Accept ``<hex>`` or ``sha256:<hex>`` and return the canonical form."""
    if not isinstance(value, str):
        raise RuleIntegrityError(
            "digest must be a string, got {!r}".format(type(value).__name__)
        )
    candidate = value.strip().lower()
    if candidate.startswith("sha256:"):
        candidate = candidate[len("sha256:"):]
    if not _HEX64_RE.match(candidate):
        raise RuleIntegrityError(
            "digest must be 64 hex characters, got {!r}".format(value)
        )
    return "sha256:" + candidate


def verify_signature(
    content: Any, expected_digest: Any, *, source: str = "<memory>"
) -> str:
    """Return the package's digest, or raise if it is not the expected one.

    The check happens on the exact bytes and before any YAML parsing. Callers
    can use this directly to verify a download without installing it.
    """
    raw = _as_bytes(content)
    expected = normalise_digest(expected_digest)
    actual = sha256_digest(raw)
    if actual != expected:
        raise RuleIntegrityError(
            "{}: SHA256 mismatch: expected {}, package hashes to {}. The file "
            "was altered in transit or the digest is stale.".format(
                source, expected, actual
            )
        )
    return actual


def _as_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    raise TypeError(
        "rule package content must be bytes or str, got {!r}".format(
            type(content).__name__
        )
    )


def _require_str(
    container: Mapping[str, Any], field_name: str, source: str, *, allow_blank: bool = False
) -> str:
    value = container.get(field_name)
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        raise RuleSchemaError(
            "{}: field {!r} must be a non-empty string, got {!r}".format(
                source, field_name, value
            )
        )
    return value


def _validate_rule_id(value: Any, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleSchemaError(
            "{}: rule_id must be a non-empty string, got {!r}".format(source, value)
        )
    rule_id = value.strip()
    if not _RULE_ID_RE.match(rule_id):
        raise RuleSchemaError(
            "{}: rule_id {!r} is not a namespaced identifier; expected something "
            "like 'org.example.safe-web3-transfer'".format(source, value)
        )
    return rule_id


def _validate_version(value: Any, source: str, rule_id: str) -> str:
    if not isinstance(value, str) or not _VERSION_RE.match(value.strip()):
        raise RuleSchemaError(
            "{}: rule {!r}: version must be semver (e.g. '1.0.0'), got {!r}".format(
                source, rule_id, value
            )
        )
    return value.strip()


def _validate_penalty(value: Any, source: str, rule_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleSchemaError(
            "{}: rule {!r}: penalty_weight must be a number, got {!r}".format(
                source, rule_id, value
            )
        )
    weight = float(value)
    if not math.isfinite(weight):
        raise RuleSchemaError(
            "{}: rule {!r}: penalty_weight must be finite, got {!r}".format(
                source, rule_id, value
            )
        )
    if weight < 0:
        raise RuleSchemaError(
            "{}: rule {!r}: penalty_weight must be >= 0, got {!r}".format(
                source, rule_id, value
            )
        )
    return weight


def _validate_witnesses(values: Any, source: str, rule_id: str) -> Tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise RuleSchemaError(
            "{}: rule {!r}: interface witnesses must be a list of names, got "
            "{!r}".format(source, rule_id, values)
        )
    normalised: List[str] = []
    for witness in values:
        if not isinstance(witness, str) or not witness.strip():
            raise RuleSchemaError(
                "{}: rule {!r}: witness names must be non-empty strings, got "
                "{!r}".format(source, rule_id, witness)
            )
        name = witness.strip()
        if name not in SUPPORTED_INTERFACE_WITNESSES:
            raise RuleSchemaError(
                "{}: rule {!r}: unsupported interface witness {!r}; the runner "
                "can only produce {}".format(
                    source, rule_id, name, sorted(SUPPORTED_INTERFACE_WITNESSES)
                )
            )
        if name not in normalised:
            normalised.append(name)
    if not normalised:
        raise RuleSchemaError(
            "{}: rule {!r}: interface_spec declares no witnesses, so the rule "
            "could never be checked".format(source, rule_id)
        )
    return tuple(normalised)


@dataclass(frozen=True)
class InterfaceSpec:
    """What physical evidence a rule demands before it will let an action run."""

    required_witnesses: Tuple[str, ...]
    expectation: str = "file_modified"
    capability: str = "write"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "required_witnesses": list(self.required_witnesses),
            "expectation": self.expectation,
            "capability": self.capability,
        }


@dataclass(frozen=True)
class InterfaceRule:
    """One normalised rule from an installed package."""

    rule_id: str
    version: str
    author: str
    target_tool: str
    interface_spec: InterfaceSpec
    penalty_weight: float
    risk_level: str = "high"
    description: str = ""
    source: str = ""
    package_name: str = ""
    package_digest: str = ""

    @property
    def namespace(self) -> str:
        """The dotted prefix, or ``""`` for a single-segment id."""
        return self.rule_id.rsplit(".", 1)[0] if "." in self.rule_id else ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "author": self.author,
            "target_tool": self.target_tool,
            "interface_spec": self.interface_spec.as_dict(),
            "penalty_weight": self.penalty_weight,
            "risk_level": self.risk_level,
            "description": self.description,
            "source": self.source,
            "package_name": self.package_name,
            "package_digest": self.package_digest,
        }


@dataclass(frozen=True)
class RulePackage:
    """An installed, digest-verified collection of interface rules."""

    name: str
    version: str
    author: str
    digest: str
    source: str
    rules: Tuple[InterfaceRule, ...]
    raw: bytes = field(repr=False, compare=False)
    schema: str = PACKAGE_SCHEMA

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "digest": self.digest,
            "source": self.source,
            "schema": self.schema,
            "rules": [rule.as_dict() for rule in self.rules],
        }


@dataclass(frozen=True)
class EnforcementDecision:
    """The interface's answer, plus the provenance needed to explain it."""

    allowed: bool
    tool: str
    reason: str
    matched_rule_ids: Tuple[str, ...] = ()
    violated_rule_ids: Tuple[str, ...] = ()
    #: ``((rule_id, (missing, ...)), ...)``; a tuple keeps the decision hashable.
    missing_witnesses: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    penalty_weight: float = 0.0
    warrant: Optional[Any] = None
    ledger_seq: Optional[int] = None

    @property
    def missing_by_rule(self) -> Dict[str, Tuple[str, ...]]:
        return dict(self.missing_witnesses)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "tool": self.tool,
            "reason": self.reason,
            "matched_rule_ids": list(self.matched_rule_ids),
            "violated_rule_ids": list(self.violated_rule_ids),
            "missing_witnesses": {
                rule_id: list(missing)
                for rule_id, missing in self.missing_witnesses
            },
            "penalty_weight": self.penalty_weight,
            "ledger_seq": self.ledger_seq,
        }


def _parse_interface_spec(
    raw: Any, source: str, rule_id: str
) -> InterfaceSpec:
    """Accept the documented mapping and two harmless shorthands.

    The shorthands exist because ``interface_spec: ["git_diff"]`` is the shape
    most authors reach for first. Both are normalised to the same immutable
    object, so nothing downstream has to branch on how the YAML was written.
    """
    expectation = "file_modified"
    capability = "write"

    if isinstance(raw, str):
        witnesses_raw: Any = [raw]
    elif isinstance(raw, (list, tuple)):
        witnesses_raw = raw
    elif isinstance(raw, Mapping):
        unknown = set(raw) - _INTERFACE_SPEC_FIELDS
        if unknown:
            raise RuleSchemaError(
                "{}: rule {!r}: unknown interface_spec field(s) {}; the loader "
                "refuses to ignore a requirement it does not understand".format(
                    source, rule_id, sorted(unknown)
                )
            )
        witnesses_raw = raw.get("required_witnesses", raw.get("witnesses"))
        if witnesses_raw is None and "witness_type" in raw:
            witnesses_raw = [raw["witness_type"]]
        if witnesses_raw is None:
            raise RuleSchemaError(
                "{}: rule {!r}: interface_spec must name required_witnesses".format(
                    source, rule_id
                )
            )
        expectation = raw.get("expectation", expectation)
        capability = raw.get("capability", capability)
    else:
        raise RuleSchemaError(
            "{}: rule {!r}: interface_spec must be a mapping or a list of "
            "witness names, got {!r}".format(source, rule_id, raw)
        )

    witnesses = _validate_witnesses(witnesses_raw, source, rule_id)

    if not isinstance(expectation, str) or expectation not in SUPPORTED_EXPECTATIONS:
        raise RuleSchemaError(
            "{}: rule {!r}: unsupported expectation {!r}; supported: {}".format(
                source, rule_id, expectation, sorted(SUPPORTED_EXPECTATIONS)
            )
        )
    if not isinstance(capability, str) or capability not in CAPABILITIES:
        raise RuleSchemaError(
            "{}: rule {!r}: capability {!r} is not one of {}".format(
                source, rule_id, capability, ", ".join(CAPABILITIES)
            )
        )

    return InterfaceSpec(
        required_witnesses=witnesses,
        expectation=expectation,
        capability=capability,
    )


def _parse_rule(
    raw: Any,
    source: str,
    defaults: Mapping[str, Any],
    package_name: str,
    package_digest: str,
) -> InterfaceRule:
    if not isinstance(raw, Mapping):
        raise RuleSchemaError(
            "{}: each rule must be a mapping, got {}".format(
                source, type(raw).__name__
            )
        )

    unknown = set(raw) - _RULE_FIELDS
    if unknown:
        raise RuleSchemaError(
            "{}: unknown rule field(s) {}; the loader refuses to ignore a guard "
            "it does not understand".format(source, sorted(unknown))
        )

    for required in ("rule_id", "target_tool", "interface_spec", "penalty_weight"):
        if required not in raw:
            raise RuleSchemaError(
                "{}: missing required field {!r}".format(source, required)
            )

    rule_id = _validate_rule_id(raw.get("rule_id"), source)
    target_tool = _require_str(raw, "target_tool", source)

    version_raw = raw.get("version", defaults.get("version"))
    if version_raw is None:
        raise RuleSchemaError(
            "{}: rule {!r}: missing required field 'version'".format(source, rule_id)
        )
    version = _validate_version(version_raw, source, rule_id)

    author_raw = raw.get("author", defaults.get("author"))
    if not isinstance(author_raw, str) or not author_raw.strip():
        raise RuleSchemaError(
            "{}: rule {!r}: author must be a non-empty string, got {!r}".format(
                source, rule_id, author_raw
            )
        )

    interface_spec = _parse_interface_spec(raw.get("interface_spec"), source, rule_id)
    penalty_weight = _validate_penalty(raw.get("penalty_weight"), source, rule_id)

    risk_level = raw.get("risk_level", "high")
    if risk_level not in SUPPORTED_RISK_LEVELS:
        raise RuleSchemaError(
            "{}: rule {!r}: unsupported risk_level {!r}; supported: {}".format(
                source, rule_id, risk_level, sorted(SUPPORTED_RISK_LEVELS)
            )
        )

    description = raw.get("description", raw.get("note", ""))
    if not isinstance(description, str):
        raise RuleSchemaError(
            "{}: rule {!r}: description must be a string".format(source, rule_id)
        )

    return InterfaceRule(
        rule_id=rule_id,
        version=version,
        author=author_raw.strip(),
        target_tool=target_tool,
        interface_spec=interface_spec,
        penalty_weight=penalty_weight,
        risk_level=risk_level,
        description=description,
        source=source,
        package_name=package_name,
        package_digest=package_digest,
    )


def _source_stem(source: str) -> str:
    parsed = urllib.parse.urlparse(source)
    base = os.path.basename(parsed.path or source)
    stem, _ = os.path.splitext(base)
    return stem or "rule-package"


def _parse_package(content: bytes, source: str, digest: str) -> RulePackage:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as problem:
        raise RuleSchemaError(
            "{}: rule package is not UTF-8: {}".format(source, problem)
        ) from problem

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as problem:
        raise RuleSchemaError(
            "{}: rule package is not valid YAML: {}".format(source, problem)
        ) from problem

    if document is None:
        raise RuleSchemaError("{}: rule package is empty".format(source))

    rules: Tuple[InterfaceRule, ...]

    if isinstance(document, Mapping) and "rules" in document:
        unknown = set(document) - _PACKAGE_FIELDS
        if unknown:
            raise RuleSchemaError(
                "{}: unknown package field(s) {}".format(source, sorted(unknown))
            )

        schema = document.get("schema", PACKAGE_SCHEMA)
        if schema != PACKAGE_SCHEMA:
            raise RuleSchemaError(
                "{}: unsupported package schema {!r}; this loader speaks "
                "{!r}".format(source, schema, PACKAGE_SCHEMA)
            )

        header_raw = document.get("package", {})
        if not isinstance(header_raw, Mapping):
            raise RuleSchemaError(
                "{}: 'package' must be a mapping, got {}".format(
                    source, type(header_raw).__name__
                )
            )
        unknown_header = set(header_raw) - _PACKAGE_HEADER_FIELDS
        if unknown_header:
            raise RuleSchemaError(
                "{}: unknown package header field(s) {}".format(
                    source, sorted(unknown_header)
                )
            )

        name_raw = header_raw.get("name", _source_stem(source))
        if not isinstance(name_raw, str) or not name_raw.strip():
            raise RuleSchemaError(
                "{}: package name must be a non-empty string".format(source)
            )
        name = name_raw.strip()

        defaults: Dict[str, Any] = {}
        if "version" in header_raw:
            defaults["version"] = header_raw["version"]
        if "author" in header_raw:
            defaults["author"] = header_raw["author"]

        raw_rules = document.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise RuleSchemaError(
                "{}: 'rules' must be a non-empty list".format(source)
            )

        rules = tuple(
            _parse_rule(
                item,
                "{}[{}]".format(source, index),
                defaults,
                name,
                digest,
            )
            for index, item in enumerate(raw_rules)
        )

        version_raw = header_raw.get("version", rules[0].version)
        version = _validate_version(version_raw, source, "<package>")
        author_raw = header_raw.get("author", rules[0].author)
        if not isinstance(author_raw, str) or not author_raw.strip():
            raise RuleSchemaError(
                "{}: package author must be a non-empty string".format(source)
            )
        author = author_raw.strip()
        schema_name = PACKAGE_SCHEMA

    elif isinstance(document, list):
        name = _source_stem(source)
        rules = tuple(
            _parse_rule(
                item,
                "{}[{}]".format(source, index),
                {},
                name,
                digest,
            )
            for index, item in enumerate(document)
        )
        if not rules:
            raise RuleSchemaError(
                "{}: rule package contains no rules".format(source)
            )
        version = rules[0].version
        author = rules[0].author
        schema_name = PACKAGE_SCHEMA

    elif isinstance(document, Mapping):
        name = _source_stem(source)
        rule = _parse_rule(document, source, {}, name, digest)
        rules = (rule,)
        version = rule.version
        author = rule.author
        schema_name = PACKAGE_SCHEMA

    else:
        raise RuleSchemaError(
            "{}: rule package must be a mapping or a list, got {}".format(
                source, type(document).__name__
            )
        )

    seen: Dict[str, str] = {}
    for rule in rules:
        if rule.rule_id in seen:
            raise RuleSchemaError(
                "{}: duplicate rule_id {!r} in one package".format(
                    source, rule.rule_id
                )
            )
        seen[rule.rule_id] = rule.rule_id

    # ``package_name`` is already correct for packages; for the bare-document
    # shorthands it was filled from the source stem, which is what callers see.
    return RulePackage(
        name=name,
        version=version,
        author=author,
        digest=digest,
        source=source,
        rules=rules,
        raw=content,
        schema=schema_name,
    )


def _parse_digest_text(text: str, source: str) -> str:
    """Pull the first SHA256-looking token out of a ``sha256sum`` style file."""
    for token in re.split(r"\s+", text.strip()):
        candidate = token.lower()
        if candidate.startswith("sha256:"):
            candidate = candidate[len("sha256:"):]
        if _HEX64_RE.match(candidate):
            return "sha256:" + candidate
    raise RuleIntegrityError(
        "{}: no SHA256 digest found in {!r}".format(source, text[:80])
    )


def _default_transport(url: str) -> bytes:
    """Fetch a URL without hiding which scheme is being trusted."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https"):
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read()
    if parsed.scheme == "file":
        path = urllib.request.url2pathname(parsed.path)
        with open(path, "rb") as handle:
            return handle.read()
    if parsed.scheme == "":
        with open(url, "rb") as handle:
            return handle.read()
    raise RuleIntegrityError(
        "{}: unsupported URL scheme {!r}".format(url, parsed.scheme)
    )


class RuleRegistry:
    """A miniature package manager for interface rules.

    Installed packages are immutable and keyed by package name. Reinstalling the
    exact same bytes is idempotent; a different package under the same name, or
    the same ``rule_id`` from two packages, is refused. Two guards with one name
    make a trace ambiguous, and an ambiguous trace is what this project exists
    to avoid.
    """

    def __init__(self, rules_dir: Optional[str] = None) -> None:
        self._packages: Dict[str, RulePackage] = {}
        self._rules: Dict[str, InterfaceRule] = {}
        self._by_tool: Dict[str, List[InterfaceRule]] = {}
        if rules_dir is not None:
            self.load_directory(rules_dir)

    # -- inspection ---------------------------------------------------

    @property
    def packages(self) -> Tuple[RulePackage, ...]:
        return tuple(self._packages.values())

    @property
    def rules(self) -> Tuple[InterfaceRule, ...]:
        return tuple(self._rules.values())

    @property
    def rule_ids(self) -> Tuple[str, ...]:
        return tuple(self._rules)

    def get_rule(self, rule_id: str) -> Optional[InterfaceRule]:
        return self._rules.get(rule_id)

    def get_package(self, name: str) -> Optional[RulePackage]:
        return self._packages.get(name)

    def rules_for_tool(self, tool_name: str) -> List[InterfaceRule]:
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name must be a non-empty string")
        exact = self._by_tool.get(tool_name, ())
        wildcard = self._by_tool.get("*", ())
        return [*exact, *wildcard]

    # -- installation -------------------------------------------------

    def install(
        self,
        content: Any,
        *,
        expected_digest: str,
        source: str = "<memory>",
    ) -> RulePackage:
        """Verify ``content`` against ``expected_digest`` and install it.

        The digest is checked before YAML is touched. A mismatch raises and the
        registry is left unchanged: a half-installed policy is worse than none,
        because its gaps are invisible.
        """
        raw = _as_bytes(content)
        actual = verify_signature(raw, expected_digest, source=source)

        package = _parse_package(raw, source, actual)

        existing = self._packages.get(package.name)
        if existing is not None:
            if existing.digest == package.digest:
                return existing
            raise RuleInterfaceError(
                "package {!r} is already installed with digest {}; refusing to "
                "replace it with {}".format(
                    package.name, existing.digest, package.digest
                )
            )

        duplicates = [
            rule.rule_id for rule in package.rules if rule.rule_id in self._rules
        ]
        if duplicates:
            raise RuleInterfaceError(
                "rule_id(s) {} are already installed by another package; two "
                "guards with one name make the trace ambiguous".format(
                    sorted(set(duplicates))
                )
            )

        self._packages[package.name] = package
        for rule in package.rules:
            self._rules[rule.rule_id] = rule
            self._by_tool.setdefault(rule.target_tool, []).append(rule)
        return package

    # ``install_package`` is the spelling a caller reaches for first; both do
    # the same verification, so neither can become the unverified one.
    install_package = install

    @staticmethod
    def verify_signature(
        content: Any, expected_digest: Any, *, source: str = "<memory>"
    ) -> str:
        """Check a download without installing it. Returns the actual digest."""
        return verify_signature(content, expected_digest, source=source)

    def load_rule_file(
        self,
        path: str,
        *,
        expected_digest: Optional[str] = None,
    ) -> RulePackage:
        """Load one file, taking the digest from its ``.sha256`` sidecar."""
        if expected_digest is None:
            expected_digest = self._read_sidecar(path)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as problem:
            raise RuleIntegrityError(
                "could not read rule package {}: {}".format(path, problem)
            ) from problem
        return self.install(raw, expected_digest=expected_digest, source=path)

    def load_directory(
        self,
        directory: str,
        *,
        signatures: Optional[Mapping[str, str]] = None,
    ) -> Tuple[RulePackage, ...]:
        """Install every YAML package in ``directory``.

        Each file needs a digest: either supplied in ``signatures`` (keyed by
        file name or full path) or stored in ``<file>.sha256``. There is no
        "unsigned local" mode. A local directory is exactly the place a rule
        file is most likely to be edited by hand, and an edit is precisely what
        the digest is there to make visible.
        """
        if not os.path.isdir(directory):
            raise FileNotFoundError(
                "rule package directory not found: {}".format(directory)
            )

        supplied = dict(signatures or {})
        installed: List[RulePackage] = []
        for name in sorted(os.listdir(directory)):
            if not name.lower().endswith((".yaml", ".yml")):
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            expected = supplied.get(name, supplied.get(path))
            if expected is None:
                expected = self._read_sidecar(path)
            installed.append(self.load_rule_file(path, expected_digest=expected))

        if not installed:
            _LOG.warning(
                "rule package directory %s contains no .yaml/.yml files; the "
                "registry is empty",
                directory,
            )
        return tuple(installed)

    def load_local(
        self,
        path: str,
        *,
        expected_digest: Optional[str] = None,
        signatures: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """Load a local file or directory, always through digest verification.

        A directory returns a tuple of packages; a file returns one package.
        The asymmetry is deliberate: one file is one package, while a directory
        is however many packages its owner published.
        """
        if os.path.isdir(path):
            return self.load_directory(path, signatures=signatures)
        return self.load_rule_file(path, expected_digest=expected_digest)

    # ``load`` mirrors the local/remote symmetry: load_local vs pull.
    load = load_local

    def pull(
        self,
        url: str,
        *,
        expected_digest: Optional[str] = None,
        transport: Optional[Transport] = None,
    ) -> RulePackage:
        """Fetch a package over ``transport`` and install it after verification.

        ``expected_digest`` is the trust anchor and should come from a channel
        other than the package itself. When omitted, the registry tries
        ``<url>.sha256`` and logs that this only detects corruption: an attacker
        able to replace the package can usually replace the sidecar beside it.
        """
        fetch = transport if transport is not None else _default_transport

        if expected_digest is None:
            sidecar_url = url + ".sha256"
            try:
                sidecar = _as_bytes(fetch(sidecar_url))
            except Exception as problem:  # noqa: BLE001 - reported, not swallowed
                raise RuleIntegrityError(
                    "refusing to install {!r} without a SHA256 digest: no "
                    "expected_digest was supplied and {} could not be "
                    "fetched ({})".format(url, sidecar_url, problem)
                ) from problem
            try:
                sidecar_text = sidecar.decode("utf-8")
            except UnicodeDecodeError as problem:
                raise RuleIntegrityError(
                    "{}: digest sidecar is not UTF-8: {}".format(
                        sidecar_url, problem
                    )
                ) from problem
            expected_digest = _parse_digest_text(sidecar_text, sidecar_url)
            _LOG.warning(
                "digest for %s came from the same channel as the package; "
                "SHA256 then detects corruption, not tampering",
                url,
            )

        try:
            content = _as_bytes(fetch(url))
        except Exception as problem:  # noqa: BLE001 - reported, not swallowed
            raise RuleIntegrityError(
                "could not fetch rule package {}: {}".format(url, problem)
            ) from problem

        return self.install(content, expected_digest=expected_digest, source=url)

    # ``fetch`` mirrors the network verb; it is the same verified path.
    fetch = pull

    # -- mounting -----------------------------------------------------

    def mount(self, gateway: Any) -> "RuleInterface":
        """Inject every installed package into a membrane gateway.

        The return value is the live interface; construction is when admission
        happens. Mounting an empty registry is legal and produces an interface
        that governs nothing -- which is reported, not hidden.
        """
        return RuleInterface(self, gateway)

    # -- internals ----------------------------------------------------

    @staticmethod
    def _read_sidecar(path: str) -> str:
        sidecar = path + ".sha256"
        if not os.path.isfile(sidecar):
            raise RuleIntegrityError(
                "{}: no {} sidecar and no expected digest was supplied; an "
                "unsigned rule package is refused".format(path, os.path.basename(sidecar))
            )
        try:
            with open(sidecar, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as problem:
            raise RuleIntegrityError(
                "could not read digest sidecar {}: {}".format(sidecar, problem)
            ) from problem
        return _parse_digest_text(text, sidecar)

    def __len__(self) -> int:
        return len(self._packages)

    def __repr__(self) -> str:
        return "RuleRegistry(packages={}, rules={})".format(
            len(self._packages), len(self._rules)
        )


class RuleInterface:
    """Installed rules, mounted on a :class:`MembraneGateway`.

    Two layers, two jobs, deliberately not merged:

    * **Rules** decide whether the evidence an action needs was collected. A
      missing ``compiler_exit_code`` is a policy denial even if the shell
      command exited zero.
    * **The membrane** decides whether the exact bytes offered as evidence were
      expected. A rule that is satisfied by a witness name still has to cross
      the gate with a digest someone else registered.

    ``enforce`` reports the first; passing ``witness_payload`` also asks the
    second for a warrant, so a caller that wants both gets both in one step.
    """

    def __init__(
        self,
        registry: RuleRegistry,
        gateway: Any,
        *,
        starting_trust: float = 100.0,
    ) -> None:
        if not isinstance(registry, RuleRegistry):
            raise TypeError(
                "registry must be a RuleRegistry, got {!r}".format(
                    type(registry).__name__
                )
            )
        required = ("register_digest", "request_action_warrant", "redeem", "append_denial", "append_call")
        missing = [name for name in required if not callable(getattr(gateway, name, None))]
        if missing or not hasattr(gateway, "ledger"):
            raise TypeError(
                "gateway must be a MembraneGateway (missing {}); rules are "
                "injected only through the membrane's public API".format(
                    ", ".join(missing) or "ledger"
                )
            )

        if isinstance(starting_trust, bool) or not isinstance(
            starting_trust, (int, float)
        ):
            raise TypeError("starting_trust must be a number")
        if not math.isfinite(float(starting_trust)) or float(starting_trust) < 0:
            raise ValueError("starting_trust must be finite and >= 0")

        self._registry = registry
        self._gateway = gateway
        self._starting_trust = float(starting_trust)
        self._penalty_assessed = 0.0
        self._mounted: Dict[str, Dict[str, Any]] = {}

        for package in registry.packages:
            self._admit(package)

    # -- inspection ---------------------------------------------------

    @property
    def registry(self) -> RuleRegistry:
        return self._registry

    @property
    def gateway(self) -> Any:
        return self._gateway

    @property
    def rules(self) -> Tuple[InterfaceRule, ...]:
        """Every rule this interface can enforce, in installation order."""
        return self._registry.rules

    @property
    def rule_ids(self) -> Tuple[str, ...]:
        return self._registry.rule_ids

    @property
    def mounted_packages(self) -> Dict[str, Dict[str, Any]]:
        """Per-package admission records, as plain data."""
        return {name: dict(record) for name, record in self._mounted.items()}

    @property
    def penalty_assessed(self) -> float:
        """Trust points this interface has recorded as owed in this session."""
        return self._penalty_assessed

    @property
    def trust_score(self) -> float:
        """A local projection only; the authoritative trust ledger lives elsewhere."""
        return max(0.0, self._starting_trust - self._penalty_assessed)

    # -- admission ----------------------------------------------------

    def _admit(self, package: RulePackage) -> None:
        """Make the package itself cross the membrane before its rules count."""
        digest = self._gateway.register_digest("read", package.digest)
        warrant = self._gateway.request_action_warrant(
            {
                "kind": "inline",
                "text": package.text,
                "digest": digest,
            },
            "read",
        )
        redemption = self._gateway.redeem(warrant)
        mount_record = self._gateway.append_call(
            MOUNT_TOOL,
            "PASS",
            capability="read",
            digest=digest,
            package=package.name,
            package_version=package.version,
            package_author=package.author,
            rules=len(package.rules),
        )
        self._mounted[package.name] = {
            "digest": digest,
            "rules": tuple(rule.rule_id for rule in package.rules),
            "registration_seq": warrant.seq - 1,
            "warrant_seq": warrant.seq,
            "redeem_seq": redemption["seq"],
            "mount_seq": mount_record["seq"],
        }

    # -- enforcement --------------------------------------------------

    def enforce(
        self,
        tool_name: str,
        witnesses: Iterable[str] = (),
        *,
        capability: Optional[str] = None,
        witness_payload: Optional[Mapping[str, Any]] = None,
        agent: Optional[str] = None,
    ) -> EnforcementDecision:
        """Check ``tool_name`` against the mounted rules.

        ``witnesses`` is the set of evidence kinds the runner actually
        collected. When a required kind is absent the denial is written to the
        membrane's hash-chained ledger with the rule id and ``penalty_weight``;
        the amount is also accumulated in :attr:`penalty_assessed`.

        ``witness_payload`` is optional. Supplying it makes a satisfied rule set
        also request and redeem a membrane warrant, which is the only way the
        exact bytes get checked. Leaving it out means "the evidence exists" is
        all that was proven -- useful for a dry run, not for a real write.
        """
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if capability is not None and capability not in CAPABILITIES:
            raise ValueError(
                "capability {!r} is not one of {}".format(
                    capability, ", ".join(CAPABILITIES)
                )
            )
        witness_set = self._normalise_witnesses(witnesses)

        matched = self._registry.rules_for_tool(tool_name.strip())
        matched_ids = tuple(rule.rule_id for rule in matched)
        if not matched:
            return EnforcementDecision(
                allowed=True,
                tool=tool_name,
                reason="no interface rule governs {!r}".format(tool_name),
            )

        missing: List[Tuple[str, Tuple[str, ...]]] = []
        violations: List[InterfaceRule] = []
        for rule in matched:
            absent = tuple(
                witness
                for witness in rule.interface_spec.required_witnesses
                if witness not in witness_set
            )
            if absent:
                missing.append((rule.rule_id, absent))
                violations.append(rule)

        if violations:
            violated_ids = tuple(rule.rule_id for rule in violations)
            penalty = sum(rule.penalty_weight for rule in violations)
            self._penalty_assessed += penalty
            resolved = capability or violations[0].interface_spec.capability
            entry = self._gateway.append_denial(
                tool_name,
                _REJECT_MISSING_WITNESS,
                agent=agent,
                capability=resolved,
                rule_ids=list(violated_ids),
                missing_witnesses={
                    rule_id: list(absent) for rule_id, absent in missing
                },
                penalty_weight=penalty,
                interface_packages=sorted(
                    {rule.package_name for rule in violations}
                ),
            )
            first_rule, first_missing = missing[0]
            return EnforcementDecision(
                allowed=False,
                tool=tool_name,
                reason=(
                    "rule {!r} requires witness(es) {}; not collected".format(
                        first_rule, ", ".join(first_missing)
                    )
                ),
                matched_rule_ids=matched_ids,
                violated_rule_ids=violated_ids,
                missing_witnesses=tuple(missing),
                penalty_weight=penalty,
                ledger_seq=entry["seq"],
            )

        capabilities = {rule.interface_spec.capability for rule in matched}
        if capability is not None:
            resolved = capability
        elif len(capabilities) == 1:
            resolved = next(iter(capabilities))
        else:
            raise ValueError(
                "rules for {!r} disagree on capability ({}); pass capability= "
                "explicitly".format(tool_name, ", ".join(sorted(capabilities)))
            )

        if witness_payload is None:
            return EnforcementDecision(
                allowed=True,
                tool=tool_name,
                reason=(
                    "all required witnesses present ({}); no bytes were offered "
                    "to the membrane, so only collection was proven".format(
                        ", ".join(sorted(witness_set))
                    )
                ),
                matched_rule_ids=matched_ids,
            )

        warrant = self._gateway.request_action_warrant(witness_payload, resolved)
        redemption = self._gateway.redeem(warrant)
        return EnforcementDecision(
            allowed=True,
            tool=tool_name,
            reason=(
                "all required witnesses present; membrane warrant issued at "
                "seq {} and redeemed at seq {}".format(
                    warrant.seq, redemption["seq"]
                )
            ),
            matched_rule_ids=matched_ids,
            warrant=warrant,
            ledger_seq=redemption["seq"],
        )

    def enforce_or_raise(
        self,
        tool_name: str,
        witnesses: Iterable[str] = (),
        *,
        capability: Optional[str] = None,
        witness_payload: Optional[Mapping[str, Any]] = None,
        agent: Optional[str] = None,
    ) -> EnforcementDecision:
        decision = self.enforce(
            tool_name,
            witnesses,
            capability=capability,
            witness_payload=witness_payload,
            agent=agent,
        )
        if not decision.allowed:
            raise RuleViolation(decision)
        return decision

    # -- internals ----------------------------------------------------

    @staticmethod
    def _normalise_witnesses(witnesses: Iterable[str]) -> frozenset:
        if isinstance(witnesses, str):
            raise TypeError(
                "witnesses must be an iterable of witness names, not a single "
                "string; a string would be read one character at a time"
            )
        collected = set()
        for witness in witnesses:
            if not isinstance(witness, str) or not witness.strip():
                raise ValueError(
                    "witness names must be non-empty strings, got {!r}".format(
                        witness
                    )
                )
            name = witness.strip()
            if name not in SUPPORTED_INTERFACE_WITNESSES:
                raise ValueError(
                    "unsupported witness {!r}; known: {}".format(
                        name, sorted(SUPPORTED_INTERFACE_WITNESSES)
                    )
                )
            collected.add(name)
        return frozenset(collected)

    def __repr__(self) -> str:
        return "RuleInterface(packages={}, rules={}, penalty={})".format(
            len(self._mounted),
            len(self._registry),
            self._penalty_assessed,
        )


# The mounted object is the interface. Keeping a second name makes the concept
# discoverable without making two different implementations.
MountedRuleInterface = RuleInterface
