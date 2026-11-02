"""Stdio JSON-RPC sidecar: the wire between an agent and its tool layer.

The proxy in ``mcp_proxy`` models the call boundary. This module puts it on a
pipe so a real agent client can talk to it.

Wire format is newline-delimited JSON-RPC 2.0, one object per line. That is the
framing used by most stdio MCP transports in their line mode: no Content-Length
headers, no batching, and no framing ambiguity to get wrong.

Two properties matter more than the protocol details:

* **The response is passive.** The agent receives exactly what the tool layer
  claimed, and nothing about the verdict. Leaking the verdict would let the
  agent detect the audit and adapt, which destroys the audit's value.
* **The verdict goes to a trace file, not the wire.** A JSONL record per call,
  flushed immediately, so the audit survives the process being killed. If the
  trace cannot be written the call is still answered -- an unwritable log must
  not become an outage -- but the failure is reported on stderr.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, TextIO

from .mcp_proxy import MCPProxy, UnknownToolError
from .runner import SafeRunner

__all__ = ["JsonRpcServer", "TraceWriter", "main", "TRACE_FILENAME"]

TRACE_FILENAME = "audit_trace.jsonl"

# JSON-RPC 2.0 reserved error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application-level code for a tool name the proxy has no spec for.
UNKNOWN_TOOL = -32001

_LOG = logging.getLogger("audit_sidecar.mcp_server")


class _RequestError(Exception):
    """Internal signal that a request must become a JSON-RPC error object."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(message_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


class TraceWriter:
    """Append-only JSONL audit trace.

    Each record is written with a single ``write`` call and flushed, so a
    concurrent reader sees whole lines rather than a torn record.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        # Create eagerly so the file exists even if no call is ever audited;
        # an E2E test then cannot pass by finding a stale file.
        with open(self._path, "a", encoding="utf-8"):
            pass

    @property
    def path(self) -> str:
        return self._path

    def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as problem:
            # An audit log that cannot be written is an incident, but refusing
            # to answer would take the agent down with it. Report, keep serving.
            _LOG.error("could not append to trace %s: %s", self._path, problem)


def _trace_record(
    message_id: Any,
    tool: str,
    args: Dict[str, Any],
    reported_success: bool,
    result: Any,
) -> Dict[str, Any]:
    return {
        "id": message_id,
        "tool": tool,
        "args_keys": sorted(args.keys()),
        "intent": result.intent,
        "agent_reported_success": reported_success,
        "check_observed": result.check_observed,
        "check_source": result.check_source,
        "effect_observed": result.effect_observed,
        "verdict": result.verdict.value,
        "reason": result.reason,
        "exit_code": result.exit_code,
        "pipe_status": list(result.pipe_status),
        "changed_files": list(result.changed_files),
        "missing_expected": list(result.missing_expected),
        "unexpected_changes": list(result.unexpected_changes),
    }


class JsonRpcServer:
    """Line-delimited JSON-RPC 2.0 front end for ``MCPProxy``."""

    def __init__(
        self,
        proxy: MCPProxy,
        trace: TraceWriter,
        stdout: Optional[TextIO] = None,
    ) -> None:
        self._proxy = proxy
        self._trace = trace
        self._stdout = stdout if stdout is not None else sys.stdout

    # -- public API ---------------------------------------------------

    def handle_line(self, line: str) -> List[Dict[str, Any]]:
        """Handle one input line; return the responses to emit.

        Returns a list because the shape is uniform for callers; the transport
        is one request per line, so it is empty or holds one element.
        """
        stripped = line.strip()
        if not stripped:
            # Blank lines are transport noise, not protocol violations.
            return []

        try:
            message = json.loads(stripped)
        except ValueError as problem:
            return [_error(None, PARSE_ERROR, "parse error: {}".format(problem))]

        if not isinstance(message, dict):
            return [_error(None, INVALID_REQUEST, "request must be a JSON object")]

        message_id = message.get("id")
        # A request without an id is a notification: JSON-RPC forbids a
        # response, and replying anyway would desynchronise a client that
        # simply fired a call and moved on.
        is_notification = "id" not in message

        if message.get("jsonrpc") != "2.0":
            if is_notification:
                return []
            return [_error(message_id, INVALID_REQUEST, "jsonrpc must be '2.0'")]

        method = message.get("method")
        if method != "tool_call":
            if is_notification:
                return []
            return [
                _error(
                    message_id,
                    METHOD_NOT_FOUND,
                    "unknown method: {!r}".format(method),
                )
            ]

        try:
            response = self._handle_tool_call(message_id, message.get("params"))
        except _RequestError as failure:
            if is_notification:
                return []
            return [_error(message_id, failure.code, failure.message)]

        if is_notification:
            return []
        return [response]

    def serve(self, stream: Optional[Iterable[str]] = None) -> int:
        """Read lines until EOF, writing one response per request."""
        source = stream if stream is not None else sys.stdin
        for line in source:
            for response in self.handle_line(line):
                self._write(response)
        return 0

    # -- internals ----------------------------------------------------

    def _handle_tool_call(self, message_id: Any, params: Any) -> Dict[str, Any]:
        if not isinstance(params, dict):
            raise _RequestError(INVALID_PARAMS, "params must be an object")

        tool = params.get("tool")
        if not isinstance(tool, str) or not tool:
            raise _RequestError(
                INVALID_PARAMS, "params.tool must be a non-empty string"
            )

        args = params.get("args", {})
        if not isinstance(args, dict):
            raise _RequestError(INVALID_PARAMS, "params.args must be an object")

        reported_success = params.get("reported_success")
        if not isinstance(reported_success, bool):
            # Refuse rather than default to True: a default would fabricate the
            # very claim under audit, and every such call would score PASS.
            raise _RequestError(
                INVALID_PARAMS,
                "params.reported_success must be a boolean; the claim cannot be "
                "inferred because the audit exists to judge it",
            )

        try:
            response = self._proxy.intercept_tool_call(tool, args, reported_success)
        except UnknownToolError as problem:
            raise _RequestError(UNKNOWN_TOOL, str(problem)) from None
        except (ValueError, TypeError) as problem:
            raise _RequestError(INVALID_PARAMS, str(problem)) from None
        except OSError as problem:
            raise _RequestError(
                INTERNAL_ERROR, "execution failed: {}".format(problem)
            ) from None

        result = self._proxy.audit_log[-1]
        self._trace.write(
            _trace_record(message_id, tool, args, reported_success, result)
        )
        return {"jsonrpc": "2.0", "id": message_id, "result": response}

    def _write(self, payload: Dict[str, Any]) -> None:
        self._stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._stdout.flush()


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="harness-audit-server",
        description=(
            "Stdio JSON-RPC sidecar that audits agent tool calls against "
            "independently witnessed effects."
        ),
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="working directory the audited commands run in (default: .)",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="git repository used for the change-set witness (default: --cwd)",
    )
    parser.add_argument(
        "--trace",
        default=None,
        help="JSONL audit trace path (default: <cwd>/audit_trace.jsonl)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="per-command timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress diagnostic logging on stderr",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the console script and ``python -m``."""
    options = _parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=logging.ERROR if options.quiet else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cwd = os.path.abspath(options.cwd)
    if not os.path.isdir(cwd):
        _LOG.error("working directory does not exist: %s", cwd)
        return 2

    trace_path = options.trace or os.path.join(cwd, TRACE_FILENAME)
    try:
        trace = TraceWriter(os.path.abspath(trace_path))
    except OSError as problem:
        _LOG.error("could not open trace %s: %s", trace_path, problem)
        return 2

    try:
        proxy = MCPProxy(
            cwd=cwd,
            runner=SafeRunner(timeout=options.timeout),
            repo_path=options.repo,
        )
    except (ValueError, FileNotFoundError) as problem:
        _LOG.error("could not start the proxy: %s", problem)
        return 2

    server = JsonRpcServer(proxy, trace)
    try:
        return server.serve()
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        # The agent hung up. Nothing to report, and stdout is gone anyway.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
