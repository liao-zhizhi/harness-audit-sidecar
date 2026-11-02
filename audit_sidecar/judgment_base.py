"""External judgment base: the audit policy as data, not as code.

Before this module, every judgement the sidecar could reach was compiled
into ``VerdictEngine``. That has a specific failure mode the paper cares
about: a rule you cannot read, diff or remove is indistinguishable from a
rule you forgot you had. Tightening the gate meant shipping a new release,
so in practice nobody tightened it.

A **judgment base** moves the per-tool policy out into YAML files:

    rule_id: "no_file_drift"
    target_tool: "write_file"
    require_witness: true
    witness_type: "git_diff"
    expectation: "file_modified"

Two consequences follow, and they are the point:

* **The policy is reviewable.** Changing what counts as acceptable is a
  diff in ``rules/``, visible to anyone who is not the author of the code.
* **The policy travels with the code.** The default rules live inside the
  package, so ``pip install`` alone yields a working gate: an install that
  audits nothing until configured would be the decoration this module
  exists to prevent.
* **The policy is enforceable.** A rule can demand a witness the caller did
  not collect, which the boolean lattice alone cannot express. That is how
  ``require_witness`` becomes a real gate rather than a comment.

Loading is deliberately unforgiving. An unknown field, an unknown witness
type, a unknown expectation, or a duplicate ``rule_id`` raises
``RuleError`` at load time. Silently dropping a rule that the operator
believes is active would recreate exactly the decoration problem this
feature exists to remove.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

__all__ = [
    "DEFAULT_RULES_DIRNAME",
    "JudgmentBase",
    "RuleError",
    "default_rules_dir",
    "packaged_rules_dir",
]

_LOG = logging.getLogger(__name__)

DEFAULT_RULES_DIRNAME = "rules"

# Environment variable consulted before the built-in search, so a deployment
# can point at a shared rule directory without editing code.
RULES_DIR_ENV = "HARNESS_AUDIT_RULES"

# Witness types the runner can actually produce. A rule naming anything else
# could never be satisfied, so it is rejected at load time instead of turning
# every call for that tool into a permanent INVALID.
SUPPORTED_WITNESS_TYPES = frozenset({"git_diff", "file", "agent_report"})

# Expectations the rule engine can evaluate. Anything else would have to be
# ignored, and an ignored guard is worse than no guard.
SUPPORTED_EXPECTATIONS = frozenset({"file_modified"})

# Declared risk, which decides how much witnessing a call is worth. Omitted
# means "low": a rule that names a tool has taken responsibility for it, and
# ``immune_state`` escalates on inflammation anyway. The dangerous default is
# the opposite one -- treating silence as "cheap" -- which is why a tool no
# rule mentions is *not* low risk.
SUPPORTED_RISK_LEVELS = frozenset({"low", "high"})

# Fields that carry documentation only. Kept as an explicit allowlist so a
# misspelled guard (``require_witnes``) is an error rather than a no-op.
_METADATA_FIELDS = frozenset({"description", "note"})

_REQUIRED_FIELDS = ("rule_id", "target_tool")
_KNOWN_FIELDS = frozenset(
    {
        "rule_id",
        "target_tool",
        "require_witness",
        "witness_type",
        "expectation",
        "risk_level",
    }
) | _METADATA_FIELDS

# ``target_tool: "*"`` applies the rule to every tool.
WILDCARD_TOOL = "*"

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

# Rules that ship *inside* the installed package, so ``pip install`` is
# enough to get a working default policy.
PACKAGE_RULES_DIR = os.path.join(_PACKAGE_DIR, DEFAULT_RULES_DIRNAME)


class RuleError(ValueError):
    """A rule file is malformed, ambiguous, or asks for the impossible."""


@dataclass(frozen=True)
class Rule:
    """One normalised rule, as loaded from YAML."""

    rule_id: str
    target_tool: str
    require_witness: bool = False
    witness_type: Optional[str] = None
    expectation: Optional[str] = None
    #: ``"low"``, ``"high"``, or ``None`` when the field was omitted (which
    #: ``immune_state.select_tier`` reads as low).
    risk_level: Optional[str] = None
    source: str = ""

    @property
    def matches_every_tool(self) -> bool:
        return self.target_tool == WILDCARD_TOOL

    def as_dict(self) -> Dict[str, Any]:
        """Public shape returned by ``get_rules_for_tool``."""
        return {
            "rule_id": self.rule_id,
            "target_tool": self.target_tool,
            "require_witness": self.require_witness,
            "witness_type": self.witness_type,
            "expectation": self.expectation,
            "risk_level": self.risk_level,
        }


def packaged_rules_dir() -> str:
    """The ``rules/`` directory shipped inside the installed package.

    This is the path that makes ``pip install`` sufficient: the default policy
    travels with the code, so a fresh install has a working judgment base
    without the caller knowing a directory exists.
    """
    return PACKAGE_RULES_DIR


def default_rules_dir() -> Optional[str]:
    """Locate the judgment base, or ``None`` when there is none.

    Search order, first hit wins:

    1. ``$HARNESS_AUDIT_RULES`` -- an explicit override is obeyed exactly,
       including its absence: if it points somewhere that is not a directory
       this raises rather than falling back, because a policy the operator
       believes is active must not be silently replaced by another one.
    2. ``./rules`` -- a project-local policy, so a working directory can
       override the shipped default without touching the package.
    3. the ``rules/`` directory inside the installed package.

    (3) always exists in a normal install, so this returns ``None`` only for
    an uninstalled source tree with no rules of its own.
    """
    override = os.environ.get(RULES_DIR_ENV)
    if override:
        if not os.path.isdir(override):
            raise FileNotFoundError(
                "${} points at {!r}, which is not a directory; refusing to "
                "silently fall back to a different policy".format(
                    RULES_DIR_ENV, override
                )
            )
        return override
    local = os.path.join(os.getcwd(), DEFAULT_RULES_DIRNAME)
    if os.path.isdir(local):
        return local
    if os.path.isdir(PACKAGE_RULES_DIR):
        return PACKAGE_RULES_DIR
    return None


def _require_str(rule: Mapping[str, Any], field: str, source: str) -> str:
    value = rule.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuleError(
            "{}: field {!r} must be a non-empty string, got {!r}".format(
                source, field, value
            )
        )
    return value


def _normalise(raw: Any, source: str) -> Rule:
    """Validate one raw mapping and turn it into a :class:`Rule`."""
    if not isinstance(raw, Mapping):
        raise RuleError(
            "{}: each rule must be a mapping, got {}".format(
                source, type(raw).__name__
            )
        )

    unknown = set(raw) - _KNOWN_FIELDS
    if unknown:
        raise RuleError(
            "{}: unknown field(s) {}; the loader refuses to ignore a guard "
            "it does not understand".format(source, sorted(unknown))
        )

    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise RuleError("{}: missing required field {!r}".format(source, field))

    rule_id = _require_str(raw, "rule_id", source)
    target_tool = _require_str(raw, "target_tool", source)

    require_witness = raw.get("require_witness", False)
    if not isinstance(require_witness, bool):
        raise RuleError(
            "{}: rule {!r}: require_witness must be a bool, got {!r}".format(
                source, rule_id, require_witness
            )
        )

    witness_type = raw.get("witness_type")
    if witness_type is not None:
        if not isinstance(witness_type, str) or witness_type not in SUPPORTED_WITNESS_TYPES:
            raise RuleError(
                "{}: rule {!r}: unsupported witness_type {!r}; the runner can "
                "only produce {}".format(
                    source, rule_id, witness_type, sorted(SUPPORTED_WITNESS_TYPES)
                )
            )
    if require_witness and witness_type is None:
        raise RuleError(
            "{}: rule {!r}: require_witness is true but no witness_type was "
            "named, so the rule could never be checked".format(source, rule_id)
        )
    if witness_type is not None and not require_witness:
        raise RuleError(
            "{}: rule {!r}: witness_type is set but require_witness is false, "
            "so the witness would never be demanded".format(source, rule_id)
        )

    expectation = raw.get("expectation")
    if expectation is not None:
        if not isinstance(expectation, str) or expectation not in SUPPORTED_EXPECTATIONS:
            raise RuleError(
                "{}: rule {!r}: unsupported expectation {!r}; supported: "
                "{}".format(
                    source, rule_id, expectation, sorted(SUPPORTED_EXPECTATIONS)
                )
            )

    risk_level = raw.get("risk_level")
    if risk_level is not None:
        if not isinstance(risk_level, str) or risk_level not in SUPPORTED_RISK_LEVELS:
            raise RuleError(
                "{}: rule {!r}: unsupported risk_level {!r}; supported: {} "
                "(omit the field to mean 'low')".format(
                    source, rule_id, risk_level, sorted(SUPPORTED_RISK_LEVELS)
                )
            )

    return Rule(
        rule_id=rule_id,
        target_tool=target_tool,
        require_witness=require_witness,
        witness_type=witness_type,
        expectation=expectation,
        risk_level=risk_level,
        source=source,
    )


class JudgmentBase:
    """An immutable, in-memory set of rules loaded from a directory."""

    def __init__(self, rules_dir: Optional[str] = None) -> None:
        resolved = rules_dir if rules_dir is not None else default_rules_dir()
        if rules_dir is not None and not os.path.isdir(resolved):
            raise FileNotFoundError(
                "judgment base directory not found: {}".format(resolved)
            )

        self._rules_dir = resolved
        self._rules: Tuple[Rule, ...] = ()
        if resolved is not None and os.path.isdir(resolved):
            self._rules = self._load_directory(resolved)
        else:
            _LOG.warning(
                "no judgment base found; the audit is running on the built-in "
                "verdict lattice only (set $%s to load rules)",
                RULES_DIR_ENV,
            )

        by_tool: Dict[str, List[Rule]] = {}
        for rule in self._rules:
            by_tool.setdefault(rule.target_tool, []).append(rule)
        self._by_tool = {tool: tuple(items) for tool, items in by_tool.items()}

    # -- loading ------------------------------------------------------

    @staticmethod
    def _load_directory(directory: str) -> Tuple[Rule, ...]:
        try:
            entries = sorted(os.listdir(directory))
        except OSError as problem:
            raise RuleError(
                "could not read judgment base {}: {}".format(directory, problem)
            ) from problem

        documents = [name for name in entries if name.lower().endswith((".yaml", ".yml"))]
        if not documents:
            _LOG.warning(
                "judgment base %s contains no .yaml files; every tool will fall "
                "back to the built-in lattice",
                directory,
            )

        loaded: List[Rule] = []
        seen: Dict[str, str] = {}
        for name in documents:
            path = os.path.join(directory, name)
            for rule in JudgmentBase._load_file(path):
                previous = seen.get(rule.rule_id)
                if previous is not None:
                    # Two rules with one id make the trace ambiguous: a reader
                    # could not tell which guard fired.
                    raise RuleError(
                        "duplicate rule_id {!r} in {} and {}".format(
                            rule.rule_id, previous, path
                        )
                    )
                seen[rule.rule_id] = path
                loaded.append(rule)
        return tuple(loaded)

    @staticmethod
    def _load_file(path: str) -> Iterable[Rule]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = yaml.safe_load(handle)
        except OSError as problem:
            raise RuleError(
                "could not read rule file {}: {}".format(path, problem)
            ) from problem
        except yaml.YAMLError as problem:
            raise RuleError(
                "rule file {} is not valid YAML: {}".format(path, problem)
            ) from problem

        if document is None:
            raise RuleError("rule file {} is empty".format(path))
        if isinstance(document, Mapping):
            yield _normalise(document, path)
            return
        if isinstance(document, list):
            for index, item in enumerate(document):
                yield _normalise(item, "{}[{}]".format(path, index))
            return
        raise RuleError(
            "rule file {} must contain a mapping or a list of mappings, got "
            "{}".format(path, type(document).__name__)
        )

    # -- queries ------------------------------------------------------

    @property
    def rules_dir(self) -> Optional[str]:
        return self._rules_dir

    @property
    def rules(self) -> Tuple[Rule, ...]:
        return self._rules

    @property
    def rule_ids(self) -> Tuple[str, ...]:
        return tuple(rule.rule_id for rule in self._rules)

    def get_rules_for_tool(self, tool_name: str) -> List[Dict[str, Any]]:
        """Rules that apply to ``tool_name``, in load order.

        Returns plain dicts so callers cannot mutate the loaded rules.
        """
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name must be a non-empty string")
        exact = self._by_tool.get(tool_name, ())
        wildcard = self._by_tool.get(WILDCARD_TOOL, ())
        return [rule.as_dict() for rule in (*exact, *wildcard)]

    def __len__(self) -> int:
        return len(self._rules)

    def __repr__(self) -> str:
        return "JudgmentBase(dir={!r}, rules={})".format(
            self._rules_dir, len(self._rules)
        )
