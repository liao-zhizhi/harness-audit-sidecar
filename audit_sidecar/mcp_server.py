"""Stdio JSON-RPC sidecar: the wire between an agent and its tool layer.

The proxy in ``mcp_proxy`` models the call boundary. This module puts it on a
pipe so a real agent client can talk to it, speaking MCP.

Wire format
-----------
Newline-delimited JSON-RPC 2.0 over UTF-8, one object per line. That is the
MCP stdio transport as specified (2025-06-18, "Transports"): *"Messages are
delimited by newlines, and MUST NOT contain embedded newlines."* There is no
``Content-Length`` header in MCP -- that framing belongs to the Language
Server Protocol, and an earlier version of this file's notes repeated the
confusion. ``json.dumps`` escapes control characters, so a serialized message
can never contain a literal newline and the framing is unambiguous.

Methods
-------
``initialize``             protocol version and capability negotiation
``notifications/initialized``  state transition, never answered
``ping``                   liveness, returns ``{}``
``tools/list``             the audited tools, with JSON Schemas
``tools/call``             execute + audit one tool call (MCP standard)
``tool_call``              legacy pre-handshake alias, kept for compatibility

Two properties matter more than the protocol details:

* **The claim stays passive.** ``content`` / ``isError`` still match what the
  tool layer claimed. The lattice value is not a field on the result, and
  ``initialize`` still omits MCP's optional ``instructions`` field so the
  server does not announce "your calls are being judged" as a deterrent.
  On FAIL a sibling ``__audit_warning__`` is passed through so a human
  client can surface a false completion without opening the trace file.
* **The verdict still goes to a trace file.** A JSONL record per call,
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
import threading
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, TextIO, Tuple, Union

from . import __version__
from . import usage
from .immune_state import STATE_FILENAME
from .judgment_base import RuleError, default_rules_dir, default_rules_dirs
from .mcp_proxy import MCPProxy, UnknownToolError
from .runner import SafeRunner
from .self_audit import run_self_audit
from .tools.citation_audit import register_citation_tool

__all__ = [
    "JsonRpcServer",
    "TraceWriter",
    "main",
    "TRACE_FILENAME",
    "PROTOCOL_VERSIONS",
    "LATEST_PROTOCOL_VERSION",
]

TRACE_FILENAME = "audit_trace.jsonl"

# Protocol revisions this server can serve. They differ in transports and in
# optional features, none of which this server uses: the tool surface
# (initialize / tools/list / tools/call) is unchanged across all three, so
# advertising them is a claim the code can keep. Ordered newest first.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

SERVER_NAME = "harness-audit-sidecar"

# JSON-RPC 2.0 reserved error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP application code: a request arrived before the handshake.
SERVER_NOT_INITIALIZED = -32002

# Application-level code for a tool name the proxy has no spec for, on the
# legacy ``tool_call`` method. ``tools/call`` answers -32602 instead, following
# the MCP example for an unknown tool; both are kept because the legacy code is
# pinned by an existing end-to-end test and by the trace of every past session.
UNKNOWN_TOOL = -32001

# How the claim under audit was obtained. Both are recorded on every trace
# record: an implicit claim is a weaker statement than a supplied one, and a
# trace that cannot tell them apart overstates the weaker one.
CLAIM_EXPLICIT = "explicit"      # params._audit.reported_success, supplied by the client
CLAIM_TOOL_RETURN = "tool-return"  # what a non-error MCP tool result means by definition

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


def _result(message_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


class CloudSync:
    """Optional paid-tier upload. Never on the verdict path.

    Local JSONL is the source of truth. A missing network, a rejected key, or a
    hung socket must not delay or fail the audit. Posts run on daemon threads
    with a short timeout; ``drain`` is only called when the process is already
    leaving, so a one-shot test can still observe the request.

    Failures are appended to ``error_log_path`` (beside the local trace) so a
    paid user can see that sync did not land. The log must never contain the
    key or the POST body.
    """

    TIMEOUT_SECONDS = 1.5
    ERROR_LOG_NAME = "cloud_sync_errors.log"
    # Product UA, not Python-urllib/3.x: Cloudflare's bot rule 1010 rejects the
    # default urllib signature. Do not impersonate a browser.
    USER_AGENT = "{}/{}".format(SERVER_NAME, __version__)
    CONTENT_TYPE = "application/json; charset=utf-8"

    def __init__(
        self,
        endpoint: str,
        key: str,
        error_log_path: Optional[str] = None,
    ) -> None:
        self._endpoint = endpoint
        self._key = key
        self._error_log_path = error_log_path
        self._lock = threading.Lock()
        self._inflight: List[threading.Thread] = []

    def schedule(self, record: Dict[str, Any]) -> None:
        try:
            payload = json.dumps(
                {"cloud_key": self._key, "trace": record},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return
        try:
            thread = threading.Thread(
                target=self._post, args=(payload,), daemon=True
            )
            with self._lock:
                self._inflight.append(thread)
            thread.start()
        except Exception:
            return

    def drain(self, timeout: float = 2.0) -> None:
        """Wait briefly for in-flight posts. Never raises."""
        try:
            with self._lock:
                threads = list(self._inflight)
            deadline = time.monotonic() + timeout
            for thread in threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(remaining)
        except Exception:
            return

    def _post(self, payload: bytes) -> None:
        try:
            request = urllib.request.Request(
                self._endpoint,
                data=payload,
                headers={
                    "Content-Type": self.CONTENT_TYPE,
                    "Authorization": "Bearer {}".format(self._key),
                    "User-Agent": self.USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(
                request, timeout=self.TIMEOUT_SECONDS
            ) as response:
                status = getattr(response, "status", 200)
                if status >= 400:
                    self._note_failure("HTTP {}".format(status))
        except urllib.error.HTTPError as exc:
            snippet = ""
            try:
                snippet = exc.read(240).decode("utf-8", "replace").replace("\n", " ")
            except Exception:
                snippet = ""
            header_bits = []
            try:
                headers = exc.headers
                if headers is not None:
                    for name in ("cf-ray", "server", "content-type"):
                        value = headers.get(name)
                        if value:
                            header_bits.append("{}={}".format(name, value))
            except Exception:
                header_bits = []
            extra = (" " + " ".join(header_bits)) if header_bits else ""
            self._note_failure(
                "HTTP {} {}{}".format(exc.code, snippet, extra).strip()
            )
        except Exception as exc:
            # Broken network, DNS, timeout: local audit already landed.
            self._note_failure("{}: {}".format(type(exc).__name__, exc))

    def _note_failure(self, reason: str) -> None:
        """Append one redacted line. Never raises, never writes the key."""
        path = self._error_log_path
        if not path:
            return
        try:
            stamp = datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            )
            line = "{} endpoint={} {}\n".format(stamp, self._endpoint, reason)
            if self._key:
                line = line.replace(self._key, "<redacted>")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            return


def _optional_cloud_sync(
    endpoint: Optional[str],
    key: Optional[str],
    error_log_path: Optional[str] = None,
) -> Optional[CloudSync]:
    """Both flags required; one without the other is still local-only."""
    if not endpoint or not key:
        return None
    endpoint = endpoint.strip()
    key = key.strip()
    if not endpoint or not key:
        return None
    return CloudSync(endpoint, key, error_log_path=error_log_path)


class TraceWriter:
    """Append-only JSONL audit trace.

    Each record is written with a single ``write`` call and flushed, so a
    concurrent reader sees whole lines rather than a torn record.
    """

    def __init__(
        self, path: str, cloud_sync: Optional[CloudSync] = None
    ) -> None:
        self._path = path
        self._cloud_sync = cloud_sync
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
            return
        self._offer_cloud(record)

    def _offer_cloud(self, record: Dict[str, Any]) -> None:
        sync = self._cloud_sync
        if sync is None:
            return
        try:
            sync.schedule(record)
        except Exception:
            return


def _iso_timestamp() -> str:
    """Timezone-aware ISO 8601 second precision, local offset included."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _next_step_offered(result: Any) -> bool:
    """Whether this outcome offered a next_step to a human reader.

    The MCP JSON-RPC result stays passive: the agent never sees a next_step.
    ``check.py`` still offers one on every non-PASS verdict, and an explicit
    ``result.next_step`` (if a future caller sets it) wins. This bit is for
    the operator trace and the dream scanner, not for the wire.
    """
    explicit = getattr(result, "next_step", None)
    if isinstance(explicit, str) and explicit.strip():
        return True
    if explicit is True:
        return True
    verdict = getattr(result, "verdict", None)
    value = getattr(verdict, "value", verdict)
    return value not in (None, "PASS")


def _trace_record(
    message_id: Any,
    tool: str,
    args: Dict[str, Any],
    reported_success: bool,
    result: Any,
    immune_state: Optional[str] = None,
    *,
    mcp_method: str = "tool_call",
    claim_basis: str = CLAIM_EXPLICIT,
    session_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": message_id,
        "tool": tool,
        "args_keys": sorted(args.keys()),
        "timestamp": timestamp if timestamp is not None else _iso_timestamp(),
        "session_id": session_id,
        "next_step_offered": _next_step_offered(result),
        "intent": result.intent,
        "agent_reported_success": reported_success,
        # Which wire method produced this record, and how the claim behind it
        # was obtained. Without these two, a call made over ``tools/call``
        # (where MCP has no claim field at all) is indistinguishable from one
        # where the client explicitly supplied the claim under audit.
        "mcp_method": mcp_method,
        "claim_basis": claim_basis,
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
        # Judgment-base provenance. ``rule_id`` names the rule that decided
        # the verdict, so a trace stays readable after the policy changes:
        # without it, an INVALID caused by a rule is indistinguishable from
        # one caused by a missing witness.
        "rule_id": result.rule_id,
        "rules_matched": list(result.rules_matched),
        "rules_violated": list(result.rules_violated),
        # Which defence tier judged this call, and the inflammation state
        # *after* it. Without the tier, a cheap PASS reads exactly like a
        # witnessed one; without the state, a later escalation looks arbitrary
        # instead of being attributable to the failures above it.
        "tier": result.tier,
        "witness_scope": result.witness_scope,
        "witness_mtime": result.witness_mtime,
        # A weaker verdict than the caller asked for must say so in the trace.
        "dropped_assertions": list(result.dropped_assertions),
        "immune_state": immune_state,
    }


class JsonRpcServer:
    """Line-delimited JSON-RPC 2.0 front end for ``MCPProxy``.

    Implements the MCP handshake (``initialize`` plus the ``initialized``
    notification) and the tool surface (``tools/list``, ``tools/call``).

    The handshake is *enforced* for the MCP methods: a ``tools/call`` before
    ``initialize`` is answered with :data:`SERVER_NOT_INITIALIZED` instead of
    being executed. The specification requires initialization to be the first
    interaction, and a server that silently tolerates the omission hides a
    client bug that will surface later as a confusing capability mismatch.
    The legacy ``tool_call`` method is exempt, so sessions recorded by earlier
    versions keep working; it is a pre-handshake API, not an MCP one.
    """

    def __init__(
        self,
        proxy: MCPProxy,
        trace: TraceWriter,
        stdout: Optional[TextIO] = None,
        *,
        server_name: str = SERVER_NAME,
        server_version: Optional[str] = None,
    ) -> None:
        self._proxy = proxy
        self._trace = trace
        self._stdout = stdout if stdout is not None else sys.stdout
        self._server_name = server_name
        self._server_version = (
            server_version if server_version is not None else __version__
        )
        self._initialized = False
        #: Set by ``notifications/initialized``. Not used to gate anything --
        #: the server sends no requests, so there is nothing for it to unblock
        #: -- but kept so a trace reader can tell a full handshake from an
        #: ``initialize`` that the client never confirmed.
        self._client_ready = False
        self._protocol_version: Optional[str] = None
        self._client_info: Optional[Dict[str, Any]] = None
        # One id for the life of this process. Dreaming needs to tell a
        # session's FAIL cluster from two processes that both started at id=1.
        self.session_id = str(uuid.uuid4())

    # -- negotiated state ---------------------------------------------

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def protocol_version(self) -> Optional[str]:
        """The version agreed in ``initialize``, or ``None`` before it."""
        return self._protocol_version

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
        # A UTF-8 BOM is not JSON. Some Windows clients emit one anyway, and
        # rejecting the first message of a session for it turns an interop
        # quirk into a failed handshake.
        stripped = stripped.lstrip("\ufeff").strip()
        if not stripped:
            return []

        try:
            message = json.loads(stripped)
        except ValueError as problem:
            return [_error(None, PARSE_ERROR, "parse error: {}".format(problem))]

        if not isinstance(message, dict):
            # JSON-RPC batches are arrays; MCP removed batching in 2025-06-18,
            # so an array is a protocol error here rather than a list of calls.
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
        if not isinstance(method, str) or not method:
            if is_notification:
                return []
            return [
                _error(message_id, INVALID_REQUEST, "method must be a non-empty string")
            ]

        params = message.get("params")

        if method == "initialize" and is_notification:
            # An initialize with no id cannot be answered, so the client would
            # never learn the negotiated version. Refusing to enter the state
            # is better than half-performing a handshake.
            _LOG.warning("ignoring initialize sent as a notification (no id)")
            return []

        try:
            response = self._dispatch(message_id, method, params)
        except _RequestError as failure:
            if is_notification:
                return []
            return [_error(message_id, failure.code, failure.message)]

        # A notification is executed (side effects and audit record included)
        # but never answered -- that is the whole point of the id-less form.
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

    # -- dispatch -----------------------------------------------------

    def _dispatch(
        self, message_id: Any, method: str, params: Any
    ) -> Dict[str, Any]:
        if method == "initialize":
            return self._handle_initialize(message_id, params)

        if method.startswith("notifications/"):
            self._handle_notification(method, params)
            # Unreachable for a notification (handle_line drops the response)
            # but a client that puts an id on a notification is a request for
            # an empty result, which is the least surprising answer.
            return _result(message_id, {})

        if method == "ping":
            return _result(message_id, {})

        if method == "tools/list":
            self._require_initialized(method)
            return _result(message_id, self._handle_tools_list(params))

        if method == "tools/call":
            self._require_initialized(method)
            return _result(message_id, self._handle_tools_call(message_id, params))

        if method == "tool_call":
            # Legacy alias from before the handshake existed. Deliberately not
            # gated: it is what every recorded session in this project used.
            return self._handle_tool_call(message_id, params)

        raise _RequestError(
            METHOD_NOT_FOUND, "unknown method: {!r}".format(method)
        )

    def _require_initialized(self, method: str) -> None:
        if not self._initialized:
            raise _RequestError(
                SERVER_NOT_INITIALIZED,
                "server not initialized: send 'initialize' before {!r}".format(method),
            )

    def _handle_notification(self, method: str, params: Any) -> None:
        if method == "notifications/initialized":
            self._client_ready = True
            return
        # notifications/cancelled, notifications/roots/list_changed and anything
        # a newer revision adds: acknowledging is not permitted, ignoring is
        # required. Log at debug level so a typo'd notification name is
        # diagnosable without polluting the wire.
        _LOG.debug("ignoring notification %s", method)

    # -- initialize ---------------------------------------------------

    def _handle_initialize(self, message_id: Any, params: Any) -> Dict[str, Any]:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise _RequestError(INVALID_PARAMS, "params must be an object")

        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not requested:
            raise _RequestError(
                INVALID_PARAMS,
                "params.protocolVersion must be a non-empty string; version "
                "negotiation cannot start without the client's version",
            )

        # Spec: echo the requested version when it is supported, otherwise
        # answer with the newest version this server supports and let the
        # client decide whether to continue.
        if requested in PROTOCOL_VERSIONS:
            negotiated = requested
        else:
            negotiated = LATEST_PROTOCOL_VERSION
            _LOG.warning(
                "client requested unsupported protocol version %r; answering %r",
                requested,
                negotiated,
            )

        self._initialized = True
        self._protocol_version = negotiated
        client_info = params.get("clientInfo")
        self._client_info = client_info if isinstance(client_info, dict) else None

        return _result(
            message_id,
            {
                "protocolVersion": negotiated,
                # Only the capability this server actually implements. Claiming
                # `resources` or `prompts` here would make a conforming client
                # call methods that answer -32601.
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": self._server_name,
                    "version": self._server_version,
                },
                # `instructions` is omitted on purpose: it is the one field
                # that reaches the model, and telling the model it is under
                # audit removes the false completions we exist to measure.
            },
        )

    # -- tools --------------------------------------------------------

    def _handle_tools_list(self, params: Any) -> Dict[str, Any]:
        if params is not None and not isinstance(params, dict):
            raise _RequestError(INVALID_PARAMS, "params must be an object")
        # Pagination: the tool set is fixed at startup and small, so the whole
        # list is one page and `nextCursor` is omitted (the spec's signal for
        # "no more pages"). A supplied cursor is ignored rather than rejected,
        # because a client replaying an old cursor should get the list, not an
        # error it cannot act on.
        return {"tools": self._proxy.describe_tools()}

    def _handle_tools_call(self, message_id: Any, params: Any) -> Dict[str, Any]:
        if not isinstance(params, dict):
            raise _RequestError(INVALID_PARAMS, "params must be an object")

        tool = params.get("name")
        if not isinstance(tool, str) or not tool:
            raise _RequestError(
                INVALID_PARAMS, "params.name must be a non-empty string"
            )

        args = params.get("arguments", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise _RequestError(INVALID_PARAMS, "params.arguments must be an object")

        claim, basis = self._resolve_claim(params, tool)

        try:
            response = self._execute(
                message_id, tool, args, claim, "tools/call", basis
            )
        except UnknownToolError:
            # MCP's example for an unknown tool is -32602, not a custom code.
            # Answering in the reserved range keeps generic JSON-RPC clients
            # able to classify the failure.
            raise _RequestError(
                INVALID_PARAMS, "Unknown tool: {}".format(tool)
            ) from None

        # MCP's defined tool-result fields stay as the agent saw them.
        # ``status`` is dropped (not in the spec). ``__audit_warning__`` is
        # passed through on FAIL so a client can render the lie without
        # flipping ``isError``.
        payload = {
            "content": response.get("content", []),
            "isError": bool(response.get("isError")),
        }
        warning = response.get("__audit_warning__")
        if warning:
            payload["__audit_warning__"] = warning
        return payload

    def _resolve_claim(self, params: Dict[str, Any], tool: str) -> tuple:
        """Work out what the claim under audit is for a ``tools/call``.

        MCP has no field for "what the agent was told": the response *is* the
        report. So the claim has to come from one of two places.

        ``params._audit.reported_success`` -- an out-of-band field, not part of
        ``arguments``, so it never reaches the model and cannot be set by it.
        This is the only way to audit the *caller's* claim over MCP.

        Otherwise the claim is the meaning of a non-error MCP tool result: a
        tool that fails is required to set ``isError``, so returning a normal
        result asserts success. That is a real claim about the world, and it is
        the one this server must not break -- which is why the audit records
        ``claim_basis: "tool-return"`` instead of pretending a client supplied
        it.
        """
        audit = params.get("_audit")
        if audit is None:
            return True, CLAIM_TOOL_RETURN
        if not isinstance(audit, dict):
            raise _RequestError(INVALID_PARAMS, "params._audit must be an object")
        reported = audit.get("reported_success")
        if not isinstance(reported, bool):
            raise _RequestError(
                INVALID_PARAMS,
                "params._audit.reported_success must be a boolean; the claim "
                "cannot be inferred because the audit exists to judge it",
            )
        return reported, CLAIM_EXPLICIT

    # -- shared execution ---------------------------------------------

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
            response = self._execute(
                message_id, tool, args, reported_success, "tool_call", CLAIM_EXPLICIT
            )
        except UnknownToolError as problem:
            # The legacy code, kept because sessions recorded before the
            # handshake existed are keyed on it. ``tools/call`` answers -32602
            # instead, per the MCP example for an unknown tool.
            raise _RequestError(UNKNOWN_TOOL, str(problem)) from None
        return _result(message_id, response)

    def _execute(
        self,
        message_id: Any,
        tool: str,
        args: Dict[str, Any],
        reported_success: bool,
        mcp_method: str,
        claim_basis: str,
    ) -> Dict[str, Any]:
        """Run one audited call, append its trace record, return the response."""
        try:
            response = self._proxy.intercept_tool_call(tool, args, reported_success)
        except UnknownToolError:
            raise
        except (ValueError, TypeError) as problem:
            raise _RequestError(INVALID_PARAMS, str(problem)) from None
        except OSError as problem:
            raise _RequestError(
                INTERNAL_ERROR, "execution failed: {}".format(problem)
            ) from None

        result = self._proxy.audit_log[-1]
        # Local usage row: the tool name and the verdict, plus a keyed hash of
        # the path it touched. Not the path, not the arguments, not the output.
        # `observe_check` is for declarations; this is the MCP surface, where
        # the "declaration" is the call itself.
        usage.record_tool_call(
            tool_name=tool,
            verdict=result.verdict,
            tier=result.tier,
            path_arg=args.get("path") if isinstance(args.get("path"), str) else None,
            cwd=self._proxy.cwd,
        )
        self._trace.write(
            _trace_record(
                message_id,
                tool,
                args,
                reported_success,
                result,
                self._proxy.immune_state.state,
                mcp_method=mcp_method,
                claim_basis=claim_basis,
                session_id=self.session_id,
            )
        )
        return response

    def _write(self, payload: Dict[str, Any]) -> None:
        # No separators trickery needed for framing: json.dumps escapes every
        # control character, so the serialized object cannot contain a literal
        # newline. ensure_ascii=False keeps non-ASCII text as UTF-8 rather than
        # \u escapes, which is what the transport specifies.
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
        "--immune-state",
        default=None,
        help=(
            "JSON file the inflammation counters survive in (default: "
            ".immune_state.json beside --trace). Pass 'none' for a memory-only "
            "state, which means a restart forgets every failure so far"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="per-command timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--rules",
        default=None,
        help=(
            "judgment-base directory of .yaml rules (default: auto-detect "
            "./rules, then the rules/ shipped inside the package, which a "
            "pip install always has; pass 'none' to force the plain lattice)"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress diagnostic logging on stderr",
    )
    parser.add_argument(
        "--cloud-endpoint",
        default=None,
        help=(
            "optional URL to POST each local trace record after it is written. "
            "Does nothing unless --cloud-key is also set. Failures are ignored"
        ),
    )
    parser.add_argument(
        "--cloud-key",
        default=None,
        help="bearer key for --cloud-endpoint; local-only when omitted",
    )
    return parser.parse_args(argv)


def _resolve_rules_dir(
    option: Optional[str],
) -> Optional[Union[str, Tuple[str, ...]]]:
    """Turn the ``--rules`` value into the layer(s) to load, or ``None``.

    Without ``--rules`` the *default layers* are returned: a local ``./rules``
    merged over the shipped base, never one instead of the other. ``"none"``
    (case-insensitive) disables the judgment base explicitly, which is
    different from auto-detection finding nothing: the operator asked for the
    plain lattice rather than getting it by accident.
    """
    if option is None:
        return default_rules_dirs() or None
    if option.strip().lower() in ("none", ""):
        return None
    return option


def _resolve_immune_state(
    option: Optional[str], trace_path: str
) -> Optional[str]:
    """Where the inflammation counters live, defaulting beside the trace.

    Beside the trace is deliberate: both files answer "what did this session
    believe", and splitting them across directories makes it easy to keep one
    and lose the other. ``"none"`` disables persistence explicitly, which is a
    different thing from the default and is the only way to ask for amnesia.
    """
    if option is not None and option.strip().lower() == "none":
        return None
    if option is not None:
        return os.path.abspath(option)
    return os.path.join(
        os.path.dirname(os.path.abspath(trace_path)), STATE_FILENAME
    )


def _configure_stdio() -> None:
    """Pin both stdio directions to UTF-8 with newline framing.

    The transport specifies UTF-8, but Python picks its stream encoding from
    the locale, and a container started with no ``LANG`` can end up on ASCII
    for stdout -- which turns any non-ASCII tool argument into a
    ``UnicodeEncodeError`` at the moment of writing a response, i.e. a crash
    inside the protocol layer over a data value.

    Decoding is lenient on the way in: one stray byte should produce a
    ``-32700`` parse error, not take the session down. Encoding is strict on
    the way out, because everything written there was produced by this process.
    """
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "strict")):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors, newline="\n")
        except (ValueError, OSError):  # detached or already-consumed stream
            _LOG.debug("could not reconfigure %r to UTF-8", stream)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the console script and ``python -m``."""
    options = _parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=logging.ERROR if options.quiet else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    _configure_stdio()

    cwd = os.path.abspath(options.cwd)
    if not os.path.isdir(cwd):
        _LOG.error("working directory does not exist: %s", cwd)
        return 2

    # Prove this server can still say no before it is allowed to say anything.
    # The demo CLI has always done this; the server -- the path an agent
    # actually connects to -- did not, which made the gate a decoration on the
    # only path that matters. An auditor that cannot reject its own known-bad
    # fixtures produces decoration verdicts, so it refuses to start.
    rules_dir = _resolve_rules_dir(options.rules)
    if not run_self_audit(rules_dir):
        _LOG.error(
            "self-audit failed: refusing to serve verdicts. The verdict engine "
            "or the judgment base no longer rejects its own known-bad cases, so "
            "every PASS this server produced would be decoration.")
        return 3

    trace_path = options.trace or os.path.join(cwd, TRACE_FILENAME)
    abs_trace = os.path.abspath(trace_path)
    error_log = os.path.join(
        os.path.dirname(abs_trace), CloudSync.ERROR_LOG_NAME
    )
    cloud_sync = _optional_cloud_sync(
        getattr(options, "cloud_endpoint", None),
        getattr(options, "cloud_key", None),
        error_log_path=error_log,
    )
    try:
        trace = TraceWriter(abs_trace, cloud_sync=cloud_sync)
    except OSError as problem:
        _LOG.error("could not open trace %s: %s", trace_path, problem)
        if isinstance(problem, PermissionError):
            # The commonest cause is a uid mismatch on a container bind mount:
            # the process runs as 10001 while the mount belongs to the host
            # user. Saying so saves an hour of looking at permissions tables.
            _LOG.error(
                "the trace directory is not writable by uid %s. In a container, "
                "run with `--user \"$(id -u):$(id -g)\"` and point --trace at a "
                "host directory that user owns",
                os.getuid(),
            )
        return 2

    try:
        proxy = MCPProxy(
            cwd=cwd,
            runner=SafeRunner(timeout=options.timeout),
            repo_path=options.repo,
            rules_dir=_resolve_rules_dir(options.rules),
            immune_state_path=_resolve_immune_state(
                options.immune_state, trace_path
            ),
        )
    except (ValueError, FileNotFoundError, RuleError) as problem:
        _LOG.error("could not start the proxy: %s", problem)
        return 2

    # A citation's witness is a registration agency, not a filesystem effect,
    # so it cannot come from the shell-command registry. Registering it here
    # makes `validate_citation` answerable over the same JSON-RPC surface --
    # and judged by the same lattice -- as the file tools.
    register_citation_tool(proxy)

    server = JsonRpcServer(proxy, trace)
    try:
        return server.serve()
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        # The agent hung up. Nothing to report, and stdout is gone anyway.
        return 0
    finally:
        if cloud_sync is not None:
            cloud_sync.drain()


if __name__ == "__main__":
    raise SystemExit(main())
