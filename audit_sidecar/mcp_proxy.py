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

The proxy is *passive on the claim*. ``status`` / ``isError`` / ``content``
still match what the tool layer reported; rewriting those would make the
sidecar part of the system under test. On FAIL a sibling
``__audit_warning__`` is appended so a human client can surface the lie
without opening ``audit_trace.jsonl``. The lattice value itself is not a
response field.

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
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .git_witness import GitWitnessError
from .immune_state import ImmuneState, TIER_ADAPTIVE, TIER_INNATE, select_tier
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
    "audit_warning_text",
]


def audit_warning_text(result: ExecutionResult) -> Optional[str]:
    """Human-readable FAIL note, or None when the verdict is not FAIL.

    The sentence is Chinese because the operator-facing product is Chinese.
    Tests that dump the JSON envelope must allow this field; they must not
    treat it as a lattice leak.
    """
    if result.verdict is not Verdict.FAIL:
        return None
    reason = (result.reason or "").strip() or "（未给出原因）"
    return "审计器发现假完成：原因 {}".format(reason)


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
    #: True only when ``expected_content`` is the literal payload this command
    #: writes. The cheap (innate) tier cannot read files, so a content
    #: assertion there is dropped -- which is honest only when the command's
    #: own text already carries it, and is recorded either way. Every other
    #: content assertion escalates the call to the full witness instead.
    #:
    #: **The whitelist is currently one tool: ``append_file``.** Any other tool
    #: whose assertion is the command's own payload must set this flag in its
    #: builder; if it does not, the call is escalated to the adaptive tier,
    #: which costs a git change set but never produces a false PASS. See
    #: ``test_only_append_file_declares_a_payload_assertion``, which pins the
    #: set so adding a second one is a deliberate act rather than a drive-by.
    content_is_write_payload: bool = False


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
        # The asserted content is the very argument this command appends, so a
        # witness that proves the append landed proves something about that
        # payload. It is still weaker than a content read, which is why the
        # drop is recorded in ``dropped_assertions`` rather than hidden.
        content_is_write_payload=True,
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

# The argument contract of each built-in tool, in MCP tool-definition shape.
#
# It lives beside the builders rather than in the transport module because the
# builders are what actually enforce it: a schema in ``mcp_server`` could drift
# from ``_require`` and advertise a parameter the builder rejects, which is
# precisely the kind of documentation that lies. ``run_command``'s
# ``expected_files_modified`` is the load-bearing one -- it is the only way a
# caller tells the git witness what the command was supposed to touch, and a
# model that never learns about it gets an INVALID for an honest call.
_DEFAULT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "write_file": {
        "description": (
            "Write a file, replacing its contents. The write is witnessed "
            "against the filesystem on disk."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File to write. Relative paths resolve against the "
                        "audited working directory."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Exact bytes to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    "append_file": {
        "description": "Append to a file, creating it if absent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to append to."},
                "content": {"type": "string", "description": "Text to append."},
            },
            "required": ["path", "content"],
        },
    },
    "run_command": {
        "description": (
            "Run a shell command in the audited working directory. Declare "
            "expected_files_modified so the git change set can be used as a "
            "witness; without a declared expectation the call cannot be "
            "judged PASS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command line to execute.",
                },
                "expected_files_modified": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Paths this command is supposed to modify, relative "
                        "to the working directory. The change set must equal "
                        "this set exactly."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

# Schema advertised for a tool registered without one. Permissive on purpose:
# inventing required fields for someone else's handler would make the server
# reject calls the handler accepts.
_OPAQUE_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
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
        rules_dir: Optional[Union[str, Tuple[str, ...]]] = None,
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
        # Tool definitions advertised over MCP. Kept separate from the
        # registry because a tool's *description* is a protocol concern that
        # may be added, changed or withheld without touching how the call is
        # built -- and because a witness tool has no builder at all.
        self._schemas: Dict[str, Dict[str, Any]] = {
            name: dict(spec) for name, spec in _DEFAULT_SCHEMAS.items()
        }
        # Tools whose witness is collected in-process rather than by running a
        # shell command (see ``audit_sidecar/tools/citation_audit.py``). Kept
        # separate from ``_registry`` because their value is a witness, not a
        # command line, and conflating the two would let a caller smuggle an
        # unexecuted "command" past the runner.
        self._witness_handlers: Dict[
            str, Callable[[Dict[str, Any], bool], ExecutionResult]
        ] = {}
        self._audit_log: List[ExecutionResult] = []
        self._responses: List[Dict[str, Any]] = []

    @staticmethod
    def _resolve_engine(
        rules_dir: Optional[Union[str, Tuple[str, ...]]],
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
    def cwd(self) -> str:
        """The directory this proxy audits, absolute.

        Read-only accessor added for the usage log, which needs to turn a tool
        argument into the same repository-relative ``path_hash`` a ``check``
        uses. Exposing the setter would let a caller change the audited project
        mid-session while the trace kept one name for it.
        """
        return os.path.abspath(self._cwd)

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
        tier = select_tier(
            rules=rules,
            inflamed=self._immune.is_inflamed(),
            has_observable_path=bool(
                invocation.expected_file or invocation.expected_files
            ),
        )
        if (
            tier == TIER_INNATE
            and invocation.expected_content is not None
            and not invocation.content_is_write_payload
        ):
            # The innate tier does not read files, so it cannot evaluate a
            # content assertion. Dropping it would silently audit a weaker
            # claim than the caller made, and ``run_shell`` refuses to do that
            # with ``witness_scope='stat'``. Escalating is the monotone
            # direction: more evidence than the policy asked for, never less.
            _LOG.info(
                "escalating %s to the adaptive tier: it carries a content "
                "assertion the innate tier cannot read",
                tool_name,
            )
            return TIER_ADAPTIVE
        if tier == TIER_INNATE:
            try:
                from .memory_bus import intent_requires_full_witness

                if intent_requires_full_witness(invocation.intent, tool_name):
                    _LOG.info(
                        "escalating %s to the adaptive tier: memory-layer "
                        "sidecar_rules.yaml matched a high-risk intent",
                        tool_name,
                    )
                    return TIER_ADAPTIVE
            except Exception:
                _LOG.debug("memory-layer overlay skipped", exc_info=True)
        return tier

    def register_tool(
        self,
        name: str,
        builder: Callable[[Dict[str, Any]], ToolInvocation],
        *,
        description: Optional[str] = None,
        input_schema: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be a non-empty string")
        if not callable(builder):
            raise TypeError("builder must be callable")
        self._registry[name] = builder
        self._describe(name, description, input_schema)

    def _describe(
        self,
        name: str,
        description: Optional[str],
        input_schema: Optional[Dict[str, Any]],
    ) -> None:
        """Record the advertised definition of ``name``.

        Called by both registration paths. A partial definition is merged into
        whatever is already known (built-ins start from their schema), so a
        caller may override just the description without restating the schema.
        """
        spec = dict(self._schemas.get(name, {}))
        if description is not None:
            if not isinstance(description, str):
                raise TypeError("description must be a string")
            spec["description"] = description
        if input_schema is not None:
            if not isinstance(input_schema, dict):
                raise TypeError("input_schema must be an object")
            spec["inputSchema"] = input_schema
        self._schemas[name] = spec

    def register_witness_tool(
        self,
        name: str,
        handler: Callable[[Dict[str, Any], bool], ExecutionResult],
        *,
        description: Optional[str] = None,
        input_schema: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a tool that collects its own witness in-process.

        ``handler(args, agent_reported_success)`` must return an
        :class:`ExecutionResult`; the verdict inside it is recorded verbatim, so
        the handler is responsible for having consulted an independent source.
        Unlike a shell tool there is no command to sandbox -- which is exactly
        why such a tool must not be registered through :meth:`register_tool`,
        where the absence of a real command would otherwise look audited.

        ``description`` and ``input_schema`` are optional and only affect what
        ``tools/list`` advertises. Omitting them is allowed, but the tool then
        appears with an opaque schema, which a model cannot call correctly.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")
        if name in _DEFAULT_REGISTRY:
            raise ValueError(
                "tool {!r} is a built-in command tool; refusing to shadow it "
                "with an in-process witness".format(name)
            )
        self._witness_handlers[name] = handler
        self._describe(name, description, input_schema)

    @property
    def tool_names(self) -> Tuple[str, ...]:
        """Every tool this proxy can audit, sorted for a stable ``tools/list``.

        Both registration paths are included: a client that cannot see a tool
        in ``tools/list`` will never call it, so an absent name here is an
        unreachable witness rather than a private one.
        """
        return tuple(sorted(set(self._registry) | set(self._witness_handlers)))

    def describe_tools(self) -> List[Dict[str, Any]]:
        """The MCP ``tools/list`` payload: name, description, inputSchema.

        A tool registered without a definition is still listed, with a
        permissive schema. Hiding it would be worse: the handler would remain
        callable while ``tools/list`` claimed it did not exist, and a client
        that trusts discovery would quietly stop auditing that tool.
        """
        definitions: List[Dict[str, Any]] = []
        for name in self.tool_names:
            spec = self._schemas.get(name, {})
            definitions.append(
                {
                    "name": name,
                    "description": spec.get("description")
                    or "{} (no description registered)".format(name),
                    "inputSchema": spec.get("inputSchema") or dict(_OPAQUE_INPUT_SCHEMA),
                }
            )
        return definitions

    @property
    def immune_state(self) -> ImmuneState:
        """The state every verdict advances. Exposed so a witness tool can join
        the same ledger instead of keeping a private one."""
        return self._immune

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

        handler = self._witness_handlers.get(tool_name)
        if handler is not None:
            result = handler(args, agent_reported_success)
            if not isinstance(result, ExecutionResult):
                # A handler that returns the wrong shape would crash the trace
                # writer on the next line. Refuse here, where the cause is
                # obvious, rather than three frames deeper.
                raise TypeError(
                    "witness handler for {!r} returned {!r}, expected "
                    "ExecutionResult".format(tool_name, type(result).__name__)
                )
            return self._record(tool_name, agent_reported_success, result)

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
        # The innate tier never reads the file, so a content assertion cannot be
        # evaluated there. The only case that reaches this branch is a payload
        # assertion the builder declared is the command's own argument; every
        # other content assertion escalated the tier above. The drop is
        # recorded, on the result and in the reason, rather than silent.
        dropped: Tuple[str, ...] = ()
        expected_content = invocation.expected_content
        if innate and expected_content is not None:
            dropped = ("expected_content",)
            expected_content = None

        try:
            result = self._runner.run_shell(
                invocation.command,
                cwd=self._cwd,
                intent_desc=invocation.intent,
                expected_file=invocation.expected_file,
                expected_content=expected_content,
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
            # ``run_shell`` now falls back to declared-file stat instead of
            # raising on a missing repository. This branch remains as a
            # safety net for a GitWitnessError that escaped the runner.
            result = self._unwitnessed(invocation, agent_reported_success, problem)
        if dropped:
            # ``replace`` rather than mutation: ExecutionResult is frozen, and
            # the reason must not read as a full-witness PASS when one of the
            # caller's assertions went unread.
            result = replace(
                result,
                dropped_assertions=dropped,
                reason="{} [content assertion dropped; tier=innate cannot read "
                "files, and the asserted content is the command's own payload]".format(
                    result.reason
                ),
            )
        return self._record(tool_name, agent_reported_success, result)

    def _record(
        self,
        tool_name: str,
        agent_reported_success: bool,
        result: ExecutionResult,
    ) -> Dict[str, Any]:
        """Log one verdict, advance the immune state, and answer the caller.

        Both the shell path and the in-process witness path end here, so a
        citation PASS is counted by the same ledger as a file-write PASS and
        cannot drift into a friendlier rule of its own.
        """
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

        ``status`` / ``isError`` / ``content`` still reflect the tool-layer
        claim, not the witness: correcting the agent's belief is out of
        scope. On FAIL a sibling ``__audit_warning__`` is appended so a
        human (or a client that renders extra fields) can see the lie
        without parsing ``audit_trace.jsonl``. The lattice value itself
        is not a field on this object.
        """
        if reported_success:
            text = "{}: ok".format(tool_name)
        else:
            text = "{}: failed (underlying exit status {})".format(
                tool_name, result.exit_code
            )
        payload: Dict[str, Any] = {
            "status": "ok" if reported_success else "error",
            "isError": not reported_success,
            "content": [{"type": "text", "text": text}],
        }
        warning = audit_warning_text(result)
        if warning:
            payload["__audit_warning__"] = warning
        return payload

    # -- dunder -------------------------------------------------------

    def __len__(self) -> int:
        return len(self._audit_log)

    def __repr__(self) -> str:
        return "MCPProxy(cwd={!r}, calls={})".format(self._cwd, len(self._audit_log))
