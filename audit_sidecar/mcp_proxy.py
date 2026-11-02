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
Only the standard library is used.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .runner import ExecutionResult, SafeRunner
from .verdict import Verdict

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
    """Minimal interception layer that judges what a tool told the agent."""

    def __init__(
        self,
        cwd: str,
        runner: Optional[SafeRunner] = None,
        repo_path: Optional[str] = None,
        registry: Optional[Dict[str, Callable[[Dict[str, Any]], ToolInvocation]]] = None,
    ) -> None:
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("cwd must be a non-empty string")
        self._cwd = cwd
        self._repo_path = repo_path if repo_path is not None else cwd
        self._runner = runner if runner is not None else SafeRunner()
        self._registry = dict(_DEFAULT_REGISTRY)
        if registry:
            self._registry.update(registry)
        self._audit_log: List[ExecutionResult] = []
        self._responses: List[Dict[str, Any]] = []

    # -- public API ---------------------------------------------------

    @property
    def audit_log(self) -> Tuple[ExecutionResult, ...]:
        """Every audited call, oldest first. Read-only view."""
        return tuple(self._audit_log)

    @property
    def responses(self) -> Tuple[Dict[str, Any], ...]:
        """Every response handed back, in call order."""
        return tuple(self._responses)

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

        result = self._runner.run_shell(
            invocation.command,
            cwd=self._cwd,
            intent_desc=invocation.intent,
            expected_file=invocation.expected_file,
            expected_content=invocation.expected_content,
            expected_files_modified=invocation.expected_files or None,
            repo_path=self._repo_path,
            # The claim reaches the engine exactly as the agent received it.
            reported_success=agent_reported_success,
        )
        self._audit_log.append(result)

        response = self._tool_response(tool_name, agent_reported_success, result)
        self._responses.append(response)
        return response

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
