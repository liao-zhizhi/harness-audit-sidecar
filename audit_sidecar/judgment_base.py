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

**Layers.** A local ``./rules`` directory is merged with the packaged base
rather than substituted for it: the local layer may add rules, or redefine a
shipped rule *more strictly*, but it may never delete one. Substitution was
the hole -- a working tree could ship a one-line policy that removed
``no_file_drift`` while the audit still reported itself configured. The merge
is checked, and a weaker or merely equal redefinition is an error.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import yaml

__all__ = [
    "DEFAULT_RULES_DIRNAME",
    "JudgmentBase",
    "RuleError",
    "default_rules_dir",
    "default_rules_dirs",
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
    """The **primary** judgment base directory, or ``None``.

    This is the first entry of :func:`default_rules_dirs`. It exists for
    display and for callers that need one path; loading must use
    ``JudgmentBase()`` (or :func:`default_rules_dirs`) so the local layer and
    the packaged layer are *merged* rather than one replacing the other.
    """
    dirs = default_rules_dirs()
    return dirs[0] if dirs else None


def default_rules_dirs() -> Tuple[str, ...]:
    """Every directory that makes up the default judgment base, in order.

    * ``$HARNESS_AUDIT_RULES`` -- obeyed exactly, **and alone**. This is an
      escape hatch for a fully custom policy: the shipped floor is not merged
      and its rule ids are not required to be present. That bypass is never
      silent -- ``JudgmentBase`` logs a WARNING naming every shipped rule that
      is not in force -- and ``run_self_audit`` still exercises every rule the
      named base does contain. If the hatch's target is not a directory this
      raises, because a policy believed to be active must not be silently
      replaced.
    * ``./rules`` -- the project-local layer, if present.
    * the ``rules/`` directory inside the installed package.

    The returned tuple is the *precedence* order: earlier entries are the local
    layer, the last entry is the shipped floor (when both are present). The
    floor is loaded first and the local layer may only add to it or tighten it.
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
        return (override,)
    found: List[str] = []
    local = os.path.join(os.getcwd(), DEFAULT_RULES_DIRNAME)
    if os.path.isdir(local):
        found.append(local)
    if os.path.isdir(PACKAGE_RULES_DIR):
        # ``./rules`` can *be* the packaged directory when someone runs from
        # inside the package; loading it twice would report every rule as a
        # duplicate of itself.
        if not any(
            os.path.realpath(entry) == os.path.realpath(PACKAGE_RULES_DIR)
            for entry in found
        ):
            found.append(PACKAGE_RULES_DIR)
    return tuple(found)


#: How much evidence each witness type demands. ``git_diff`` is the hardest to
#: satisfy (it needs a baseline *and* a change set), then a content read, then
#: the agent's own report. Used only to decide whether a local redefinition of a
#: shipped rule is a tightening or a weakening.
_WITNESS_STRENGTH = {"agent_report": 0, "file": 1, "git_diff": 2}


def _strictness(rule: Rule) -> Tuple[int, int, int, int]:
    """A partial order: bigger is stricter on every axis independently."""
    return (
        int(bool(rule.require_witness)),
        _WITNESS_STRENGTH.get(rule.witness_type or "", 0),
        int(rule.expectation is not None),
        int(rule.risk_level == "high"),
    )


def _is_tightening(floor: Rule, candidate: Rule) -> bool:
    """True when ``candidate`` demands at least as much as ``floor``, and more."""
    floor_rank = _strictness(floor)
    candidate_rank = _strictness(candidate)
    return all(c >= f for f, c in zip(floor_rank, candidate_rank)) and (
        candidate_rank != floor_rank
    )


def _as_path(value: Any, name: str) -> str:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, bytes):
        value = value.decode()
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "{} must be a non-empty path, got {!r}".format(name, value)
        )
    return value


def _normalise_rules_dirs(
    rules_dir: Optional[Union[str, Sequence[str]]]
) -> Tuple[str, ...]:
    """Resolve the constructor argument into an ordered, de-duplicated tuple.

    ``None`` means "the default layers". An explicit value means exactly those
    directories: merging the shipped policy into an operator-named directory
    would audit against a policy nobody asked for.
    """
    if rules_dir is None:
        return default_rules_dirs()
    if isinstance(rules_dir, (str, bytes, os.PathLike)):
        candidates: Sequence[Any] = (rules_dir,)
    else:
        try:
            candidates = tuple(rules_dir)
        except TypeError:
            raise TypeError(
                "rules_dir must be a path or a sequence of paths, got {!r}".format(
                    type(rules_dir).__name__
                )
            ) from None
    resolved: List[str] = []
    for candidate in candidates:
        path = _as_path(candidate, "rules_dir entry")
        if not os.path.isdir(path):
            raise FileNotFoundError(
                "judgment base directory not found: {}".format(path)
            )
        if not any(os.path.realpath(path) == os.path.realpath(seen) for seen in resolved):
            resolved.append(path)
    return tuple(resolved)


def _merge_layers(
    layers: Sequence[Tuple[str, Tuple[Rule, ...]]]
) -> Tuple[Tuple[Rule, ...], Tuple[str, ...]]:
    """Load the shipped floor, then let local layers add or tighten.

    ``layers`` is in precedence order (local first, shipped last), so the last
    entry is the floor. A local rule with a new ``rule_id`` is an addition. A
    local rule that reuses a floor ``rule_id`` must be strictly stricter on
    every axis, otherwise it would be a way to delete a guard while its name
    stayed in the trace.
    """
    if not layers:
        return (), ()
    floor_dir, floor_rules = layers[-1]
    merged: Dict[str, Rule] = {}
    order: List[str] = []
    for rule in floor_rules:
        merged[rule.rule_id] = rule
        order.append(rule.rule_id)

    tightened: List[str] = []
    for directory, rules in layers[:-1]:
        for rule in rules:
            existing = merged.get(rule.rule_id)
            if existing is None:
                merged[rule.rule_id] = rule
                order.append(rule.rule_id)
                continue
            if not _is_tightening(existing, rule):
                raise RuleError(
                    "local rule {!r} in {} redefines shipped rule {!r} without "
                    "being stricter; a local layer may add rules or tighten "
                    "them, never weaken or duplicate them".format(
                        rule.rule_id, directory, rule.rule_id
                    )
                )
            merged[rule.rule_id] = rule
            tightened.append(rule.rule_id)
            _LOG.warning(
                "local rule %r in %s tightens the shipped rule of the same "
                "name; the stricter definition is live",
                rule.rule_id,
                directory,
            )

    shadowed = {
        rule.rule_id for rule in floor_rules
    } - set(merged)
    if shadowed:
        # Unreachable given the check above, kept as an explicit invariant: the
        # whole point of the layer merge is that this set is empty.
        raise RuleError(
            "shipped rule(s) {} were shadowed by the local policy in {}; "
            "refusing to run with a guard missing".format(
                ", ".join(sorted(shadowed)), floor_dir
            )
        )
    return tuple(merged[name] for name in order), tuple(tightened)


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
    """An immutable, in-memory set of rules loaded from one or more directories."""

    def __init__(
        self, rules_dir: Optional[Union[str, Sequence[str]]] = None
    ) -> None:
        resolved = _normalise_rules_dirs(rules_dir)
        self._rules_dirs: Tuple[str, ...] = resolved
        self._rules_dir = resolved[0] if resolved else None
        self._tightened_rule_ids: Tuple[str, ...] = ()

        layers: List[Tuple[str, Tuple[Rule, ...]]] = [
            (directory, self._load_directory(directory)) for directory in resolved
        ]
        if not layers:
            _LOG.warning(
                "no judgment base found; the audit is running on the built-in "
                "verdict lattice only (set $%s to load rules)",
                RULES_DIR_ENV,
            )
            self._rules: Tuple[Rule, ...] = ()
        elif len(layers) == 1:
            self._rules = layers[0][1]
        else:
            self._rules, self._tightened_rule_ids = _merge_layers(layers)

        by_tool: Dict[str, List[Rule]] = {}
        for rule in self._rules:
            by_tool.setdefault(rule.target_tool, []).append(rule)
        self._by_tool = {tool: tuple(items) for tool, items in by_tool.items()}

        self._warn_if_override_bypasses_floor()

    def _warn_if_override_bypasses_floor(self) -> None:
        """Say out loud when ``$HARNESS_AUDIT_RULES`` replaced the shipped floor.

        The override is an escape hatch: an operator pointing at their own
        policy gets *exactly* that policy, and the rule-id floor is not
        enforced, because a custom base is a legitimate thing to run and
        hard-failing every one of them would make the hatch useless. What must
        not happen is the bypass being silent -- that is the same defect as the
        local-layer shadowing, one level up. So: load it, and name what is
        missing.
        """
        override = os.environ.get(RULES_DIR_ENV)
        if not override:
            return
        if any(
            os.path.realpath(entry) == os.path.realpath(PACKAGE_RULES_DIR)
            for entry in self._rules_dirs
        ):
            return
        if not os.path.isdir(PACKAGE_RULES_DIR):
            return
        try:
            shipped = {rule.rule_id for rule in self._load_directory(PACKAGE_RULES_DIR)}
        except RuleError:
            return
        missing = shipped - {rule.rule_id for rule in self._rules}
        _LOG.warning(
            "$%s set: the judgment base is %s alone, and the shipped floor is "
            "NOT merged (escape hatch). Shipped rule(s) not in force: %s",
            RULES_DIR_ENV,
            list(self._rules_dirs),
            ", ".join(sorted(missing)) if missing else "(none)",
        )

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
    def rules_dirs(self) -> Tuple[str, ...]:
        """Every directory that was loaded, in precedence order."""
        return self._rules_dirs

    @property
    def tightened_rule_ids(self) -> Tuple[str, ...]:
        """Shipped rules a local layer redefined more strictly."""
        return self._tightened_rule_ids

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
