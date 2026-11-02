"""Protocol-layer interception: audit the claim the agent actually received.

A sidecar that only watches exit statuses audits the wrong observer. In a real
harness the command's exit status is seen by the *tool layer*, and the agent
only ever sees what that layer chose to report. A tool wrapper that swallows a
failure and returns ``success`` produces an agent that is wrong about the world
while being perfectly consistent with everything it was told -- the hardest
form of false completion to detect, because there is no lie in the transcript.

``MCPProxy`` sits between the agent and the tool layer. It executes the call,
reads the physical witness independently (``SafeRunner`` plus ``GitWitness``),
and judges the tool layer's claim against that witness. The claim is passed in
as the agent saw it; it is deliberately *not* re-derived from the exit status,
because re-deriving it would launder exactly the lie under audit.

The proxy is *passive*. It returns to the caller the response the agent would
have received, unchanged, and keeps its judgement in an internal log. A sidecar
that rewrites the agent's view stops being an observer and becomes part of the
system under test.

The real protocol is JSON-RPC over stdio or SSE. This module models the call
boundary as a Python method so the audit logic can be exercised without a
transport; the shape of the response follows MCP's tool-result envelope.

Judging is delegated to an engine. By default that is the plain
``VerdictEngine`` lattice. Supplying ``rules_dir`` (or a ``JudgmentBase`` /
``RuleEngine``) switches to rule-aware judging, which can only tighten a
verdict -- see ``rule_engine`` for why that direction is fixed.
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .git_witness import GitWitnessError
from .immune_state import ImmuneState, TIER_INNATE, select_tier
from .judgment_base import JudgmentBase
from .rule_engine import RuleEngine
from .runner import (
    ExecutionResult,
    SafeRunner,
    WITNESS_SCOPE_FULL,
    WITNESS_SCOPE_STAT,
)
from .verdict import Verdict, VerdictEngine

__all__ = [
    "MCPProxy",
    "ToolInvocation",
    "UnknownToolError",
]
_LOG = logging.getLogger(__name__)


class UnknownToolError(LookupError):
    """Raised when a tool call names a tool the proxy has no spec for.

    Refusing is deliberate. The alternative -- returning a generic error while
    recording nothing -- would let unaudited calls accumulate silently, and an
    interceptor with blind spots is worse than none, because callers believe
    it is watching. A production sidecar should log and pass the call through
    instead of raising, so as not to break the agent; the MVP prefers the loud
    failure.
    """


@dataclass(frozen=True)
class ToolInvocation:
    """A resolved tool call: what to run, and what counts as success."""

    command: str
    intent: str
    expected_files: Tuple[str, ...] = ()
    expected_file: Optional[str] = None
    expected_content: Optional[str] = None


def _require(args: Dict[str, Any], key: str) -> str:
    if not isinstance(args, dict):
        raise TypeError("args must be a dict, got {!r}".format(type(args).__name__))
    if key not in args:
        raise ValueError("missing required argument: {!r}".format(key))
    value = args[key]
    if not isinstance(value, str):
        raise TypeError(
            "argument {!r} must be a string, got {!r}".format(
                key, type(value).__name__
            )
        )
    return value


def _build_write_file(args: Dict[str, Any]) -> ToolInvocation:
    path = _require(args, "path")
    content = _require(args, "content")
    if not path.strip():
        raise ValueError("argument 'path' must not be empty")
    # shlex.quote is the only thing standing between audited content and code
    # execution: without it a tool argument could inject shell syntax into the
    # command the runner executes.
    command = "printf '%s' {} > {}".format(shlex.quote(content), shlex.quote(path))
    return ToolInvocation(
        command=command,
        intent="modify file {}".format(path),
        expected_files=(path,),
        expected_file=path,
        # An empty needle matches anything, so it would assert nothing.
        expected_content=content if content else None,
    )


def _build_append_file(args: Dict[str, Any]) -> ToolInvocation:
    path = _require(args, "path")
    content = _require(args, "content")
    if not path.strip():
        raise ValueError("argument 'path' must not be empty")
    command = "printf '%s' {} >> {}".format(shlex.quote(content), shlex.quote(path))
    return ToolInvocation(
        command=command,
        intent="append to file {}".format(path),
        expected_files=(path,),
        expected_file=path,
        expected_content=content if content else None,
    )


def _build_run_command(args: Dict[str, Any]) -> ToolInvocation:
    command = _require(args, "command")
    expected = args.get("expected_files_modified", ())
    if isinstance(expected, str):
        raise TypeError(
            "expected_files_modified must be a sequence of paths, not a string"
        )
    paths: Tuple[str, ...] = tuple(expected)
    for entry in paths:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("expected_files_modified entries must be non-empty")
    return ToolInvocation(
        command=command,
        intent="run command: {}".format(command),
        expected_files=paths,
    )


_DEFAULT_REGISTRY: Dict[str, Callable[[Dict[str, Any]], ToolInvocation]] = {
    "write_file": _build_write_file,
    "append_file": _build_append_file,
    "run_command": _build_run_command,
}


class MCPProxy:
    """Minimal interception layer that judges what a tool told the agent.

    How much evidence each call is worth is decided per call by
    ``immune_state.select_tier``: a declared low-risk tool on a system that has
    not been contradicted gets the cheap witness (existence and mtime), while
    high-risk tools and every call made while the system is inflamed get the
    full change set. The tier is recorded on each audit record rather than
    inferred later, because a PASS on an mtime is a weaker claim than a PASS on
    a git diff and the trace must not blur them.
    """

    def __init__(
        self,
        cwd: str,
        runner: Optional[SafeRunner] = None,
        repo_path: Optional[str] = None,
        registry: Optional[Dict[str, Callable[[Dict[str, Any]], ToolInvocation]]] = None,
        rules_dir: Optional[str] = None,
        judgment_base: Optional[JudgmentBase] = None,
        rule_engine: Optional[RuleEngine] = None,
        immune_state: Optional[ImmuneState] = None,
        immune_state_path: Optional[str] = None,
    ) -> None:
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("cwd must be a non-empty string")
        if immune_state is not None and immune_state_path is not None:
            raise ValueError(
                "pass immune_state or immune_state_path, but not both: a state "
                "object that also has a path would be saved behind the "
                "caller's back"
            )
        if immune_state is not None and not isinstance(immune_state, ImmuneState):
            raise TypeError(
                "immune_state must be an ImmuneState, got {!r}".format(
                    type(immune_state).__name__
                )
            )
        self._cwd = cwd
        self._repo_path = repo_path if repo_path is not None else cwd
        # Always present, even when nothing escalates: the state is what makes
        # escalation possible later, and a proxy without one could not react to
        # the failures it just recorded.
        self._immune = (
            immune_state
            if immune_state is not None
            else ImmuneState(state_path=immune_state_path)
        )
        self._rule_engine = self._resolve_engine(rules_dir, judgment_base, rule_engine)
        if runner is not None:
            # Rebind a caller-supplied runner onto this proxy's engine instead
            # of mutating it, so the caller's runner keeps working.
            self._runner = (
                runner
                if self._rule_engine is None
                else runner.with_engine(self._rule_engine)
            )
        else:
            self._runner = SafeRunner(
                engine=self._rule_engine
                if self._rule_engine is not None
                else VerdictEngine()
            )
        self._registry = dict(_DEFAULT_REGISTRY)
        if registry:
            self._registry.update(registry)
        self._audit_log: List[ExecutionResult] = []
        self._responses: List[Dict[str, Any]] = []

    @staticmethod
    def _resolve_engine(
        rules_dir: Optional[str],
        judgment_base: Optional[JudgmentBase],
        rule_engine: Optional[RuleEngine],
    ) -> Optional[RuleEngine]:
        """Pick the judging engine from the several ways it can be supplied.

        Rules are opt-in: with no argument the proxy judges with the plain
        lattice, exactly as before. Loading a policy implicitly would change
        the verdict for every existing caller the moment a ``rules/`` directory
        appeared in their working tree.
        """
        if rule_engine is not None:
            if rules_dir is not None or judgment_base is not None:
                raise ValueError(
                    "pass rule_engine, or rules_dir/judgment_base, but not both"
                )
            return rule_engine
        if judgment_base is not None and rules_dir is not None:
            raise ValueError("pass judgment_base or rules_dir, but not both")
        base = judgment_base
        if base is None and rules_dir is not None:
            base = JudgmentBase(rules_dir)
        if base is None:
            return None
        return RuleEngine(base)

    # -- public API ---------------------------------------------------

    @property
    def audit_log(self) -> Tuple[ExecutionResult, ...]:
        """Every audited call, oldest first. Read-only view."""
        return tuple(self._audit_log)

    @property
    def responses(self) -> Tuple[Dict[str, Any], ...]:
        """Every response handed back, in call order."""
        return tuple(self._responses)

    @property
    def rule_engine(self) -> Optional[RuleEngine]:
        """The judgment-base engine in use, or ``None`` for the plain lattice."""
        return self._rule_engine

    @property
    def judgment_base(self) -> Optional[JudgmentBase]:
        """The loaded rule set, or ``None`` when rules are not in play."""
        return self._rule_engine.judgment_base if self._rule_engine is not None else None

    @property
    def immune_state(self) -> ImmuneState:
        """The inflammation state that scopes how much each call is worth."""
        return self._immune

    def _resolve_tier(self, tool_name: str, invocation: "ToolInvocation") -> str:
        """Ask the immune policy how much this call is worth spending on.

        With no rules loaded every tool lands on the adaptive tier, which is
        exactly the pre-existing behaviour: the plain lattice always collected
        the full witness, and silently downgrading it would be a change nobody
        asked for, justified by a policy nobody wrote.
        """
        rules = (
            ()
            if self._rule_engine is None
            else self._rule_engine.rules_for_tool(tool_name)
        )
        return select_tier(
            rules=rules,
            inflamed=self._immune.is_inflamed(),
            has_observable_path=bool(
                invocation.expected_file or invocation.expected_files
            ),
        )

    def register_tool(
        self, name: str, builder: Callable[[Dict[str, Any]], ToolInvocation]
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be a non-empty string")
        if not callable(builder):
            raise TypeError("builder must be callable")
        self._registry[name] = builder

    def intercept_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        agent_reported_success: bool,
    ) -> Dict[str, Any]:
        """Execute one tool call, audit it, and return what the agent saw.

        ``agent_reported_success`` is the status the agent received from the
        tool layer. It is the *claim under audit*. Do not compute it from
        ``ExecutionResult.exit_code`` -- that would make the tool layer's
        misreport indistinguishable from the truth, which is the single
        failure this class exists to catch.

        The returned dict is the response the agent would have seen. It
        reflects the claim, not the witness: correcting the agent is out of
        scope for an observer.
        """
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(agent_reported_success, bool):
            raise TypeError(
                "agent_reported_success must be a bool, got {!r}; use False for "
                "'no status reached the agent' rather than None".format(
                    type(agent_reported_success).__name__
                )
            )
        if not isinstance(args, dict):
            raise TypeError("args must be a dict, got {!r}".format(type(args).__name__))

        builder = self._registry.get(tool_name)
        if builder is None:
            raise UnknownToolError(
                "no tool specification for {!r}; refusing to run an unaudited call".format(
                    tool_name
                )
            )
        invocation = builder(args)
        tier = self._resolve_tier(tool_name, invocation)
        innate = tier == TIER_INNATE

        try:
            result = self._runner.run_shell(
                invocation.command,
                cwd=self._cwd,
                intent_desc=invocation.intent,
                expected_file=invocation.expected_file,
                # The innate tier never reads the file, so a content assertion
                # cannot be honoured there; it is dropped only because the
                # policy routed this call to a tier that does not look, which
                # the verdict reason and the trace both record.
                expected_content=None if innate else invocation.expected_content,
                # The claimed paths are passed in both tiers: the tier decides
                # *how* they are observed (stat versus git change set), not
                # which paths the call said it would touch.
                expected_files_modified=invocation.expected_files or None,
                repo_path=self._repo_path,
                # The claim reaches the engine exactly as the agent received it.
                reported_success=agent_reported_success,
                # Lets the judgment base select rules for this specific tool.
                tool_name=tool_name,
                witness_scope=WITNESS_SCOPE_STAT if innate else WITNESS_SCOPE_FULL,
            )
        except GitWitnessError as problem:
            # The requested change-set witness cannot be collected -- usually
            # because the working directory is not a git repository. That is
            # "we did not look", not "we looked and it passed", so the verdict
            # is INVALID and the run is still recorded. Raising here instead
            # would take the whole sidecar down on an ordinary first run in a
            # scratch directory, and leave no trace to explain why.
            result = self._unwitnessed(invocation, agent_reported_success, problem)
        self._audit_log.append(result)
        # Every verdict advances the immune state, and the state is read at the
        # *start* of the next call. Escalating on the verdict one has just
        # produced would make a single call judged twice at two different
        # costs, and leave the trace unable to say which check produced it.
        self._immune.record_verdict(result.verdict)

        response = self._tool_response(tool_name, agent_reported_success, result)
        self._responses.append(response)
        return response

    def _unwitnessed(
        self,
        invocation: "ToolInvocation",
        reported_success: bool,
        problem: Exception,
    ) -> ExecutionResult:
        """Build the INVALID record for a call whose witness is unobtainable."""
        warning = (
            "no independent witness: {}; refusing to claim PASS. Point --repo "
            "at a git repository, or pass --rules none to accept the plain "
            "lattice".format(problem)
        )
        _LOG.warning("%s", warning)
        return ExecutionResult(
            intent=invocation.intent,
            effect_observed=None,
            check_observed=reported_success,
            verdict=Verdict.INVALID,
            command=invocation.command,
            cwd=os.path.abspath(self._cwd),
            reason=warning,
            check_source="protocol",
        )

    def summary(self) -> Dict[str, int]:
        """Count verdicts across the audit log."""
        counts = {verdict.value: 0 for verdict in Verdict}
        for result in self._audit_log:
            counts[result.verdict.value] += 1
        return counts

    # -- internals ----------------------------------------------------

    @staticmethod
    def _tool_response(
        tool_name: str, reported_success: bool, result: ExecutionResult
    ) -> Dict[str, Any]:
        """Build the MCP-shaped response the agent receives.

        The body deliberately reflects ``reported_success`` rather than the
        witness. Leaking the witness through the response would let the agent
        route around the audit.
        """
        if reported_success:
            text = "{}: ok".format(tool_name)
        else:
            text = "{}: failed (underlying exit status {})".format(
                tool_name, result.exit_code
            )
        return {
            "status": "ok" if reported_success else "error",
            "isError": not reported_success,
            "content": [{"type": "text", "text": text}],
        }

    # -- dunder -------------------------------------------------------

    def __len__(self) -> int:
        return len(self._audit_log)

    def __repr__(self) -> str:
        return "MCPProxy(cwd={!r}, calls={})".format(self._cwd, len(self._audit_log))
