#!/usr/bin/env python3
"""Probe: is control-audit parallelism already in the sidecar, or only analogical?

Does not patch mcp_server / mcp_proxy / memory_bus. Three *simulated* timings
plus one *production* call through JsonRpcServer._execute. Simulation results
are not a production claim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET = "fake_dir/test.txt"
CONTENT = "probe\n"


def _now() -> float:
    return time.perf_counter()


def _ms(start: float, end: float) -> float:
    return (end - start) * 1000.0


def _write_faults(path: Path, reason: str, t_label: str, events: List[Tuple[str, float]]) -> None:
    events.append((t_label, _now()))
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": "gradia.cross_layer.v1",
        "tool": "write_file",
        "intent": "modify file {}".format(TARGET),
        "reason": reason,
        "verdict": "FAIL",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    events.append(("physical_faults_fsync", _now()))


def _reply(events: List[Tuple[str, float]], extra: Dict[str, Any]) -> Dict[str, Any]:
    events.append(("reply_built", _now()))
    payload = {
        "content": [{"type": "text", "text": "write_file: ok"}],
        "isError": False,
        "verdict": "FAIL",
    }
    payload.update(extra)
    events.append(("reply_ready", _now()))
    return payload


def _run_write(cwd: Path) -> int:
    return subprocess.call(
        "printf '%s' {} > {}".format(json.dumps(CONTENT), TARGET),
        shell=True,
        cwd=str(cwd),
    )


def mode_posthoc(cwd: Path, faults: Path) -> Dict[str, Any]:
    """Naive after-the-fact: execute, then look, then write faults, then reply."""
    events: List[Tuple[str, float]] = [("call_start", _now())]
    code = _run_write(cwd)
    events.append(("execute_done", _now()))
    existed = (cwd / TARGET).exists()
    reason = "posthoc: file {} after exit {}".format(
        "present" if existed else "absent", code
    )
    verdict = "FAIL" if not existed else "PASS"
    events.append(("verdict", _now()))
    if verdict == "FAIL":
        _write_faults(faults, reason, "physical_faults_write", events)
    reply = _reply(events, {})
    events.append(("call_end", _now()))
    return {"mode": "posthoc", "verdict": verdict, "reply": reply, "events": events}


def mode_parallel(cwd: Path, faults: Path) -> Dict[str, Any]:
    """Pre-snapshot, execute, compare, one record holds verdict + fault write, then reply."""
    events: List[Tuple[str, float]] = [("call_start", _now())]
    path = cwd / TARGET
    parent = path.parent
    snap = {
        "parent_exists": parent.is_dir(),
        "mtime_size": None
        if not path.exists()
        else (path.stat().st_mtime_ns, path.stat().st_size),
    }
    events.append(("pre_snapshot", _now()))
    code = _run_write(cwd)
    events.append(("execute_done", _now()))
    after_exists = path.exists()
    after = (
        None
        if not after_exists
        else (path.stat().st_mtime_ns, path.stat().st_size)
    )
    unchanged = after == snap["mtime_size"] and not (
        snap["mtime_size"] is None and after_exists
    )
    fail = (not snap["parent_exists"]) or unchanged or not after_exists
    events.append(("verdict", _now()))
    reason = "parallel: parent_exists={} unchanged={} exit={}".format(
        snap["parent_exists"], unchanged, code
    )
    if fail:
        _write_faults(faults, reason, "physical_faults_write", events)
    reply = _reply(events, {"snapshot": snap})
    events.append(("call_end", _now()))
    return {
        "mode": "parallel",
        "verdict": "FAIL" if fail else "PASS",
        "reply": reply,
        "events": events,
        "snapshot": snap,
    }


def mode_precheck(cwd: Path, faults: Path) -> Dict[str, Any]:
    """Warn if parent is missing, still execute, still judge afterwards."""
    events: List[Tuple[str, float]] = [("call_start", _now())]
    parent = (cwd / TARGET).parent
    warning = None
    if not parent.is_dir():
        warning = "parent directory {!r} does not exist".format(str(parent))
        events.append(("precheck_warning", _now()))
    else:
        events.append(("precheck_ok", _now()))
    code = _run_write(cwd)
    events.append(("execute_done", _now()))
    existed = (cwd / TARGET).exists()
    events.append(("verdict", _now()))
    extra: Dict[str, Any] = {}
    if warning:
        extra["__precheck_warning__"] = warning
    if not existed:
        _write_faults(faults, "precheck then FAIL", "physical_faults_write", events)
    reply = _reply(events, extra)
    events.append(("call_end", _now()))
    return {
        "mode": "precheck",
        "verdict": "FAIL" if not existed else "PASS",
        "reply": reply,
        "events": events,
        "precheck_warning": warning,
        "exit_code": code,
    }


def mode_production(cwd: Path, home: Path) -> Dict[str, Any]:
    """Current code path: JsonRpcServer._execute. Not a simulated timing."""
    os.environ["GRADIA_HOME"] = str(home)
    from audit_sidecar.mcp_proxy import MCPProxy
    from audit_sidecar.mcp_server import JsonRpcServer, TraceWriter
    from audit_sidecar.memory_bus import faults_path

    events: List[Tuple[str, float]] = [("call_start", _now())]
    proxy = MCPProxy(cwd=str(cwd))

    class ClockedTrace(TraceWriter):
        def write(self, record):  # type: ignore[no-untyped-def]
            events.append(("trace_write_enter", _now()))
            super().write(record)
            events.append(("trace_write_return", _now()))

    clocked = ClockedTrace(str(cwd / "audit_trace.jsonl"))
    server = JsonRpcServer(proxy, clocked)
    parent_before = (cwd / "fake_dir").is_dir()
    events.append(("pre_parent_stat", _now()))
    response = server._execute(
        1,
        "write_file",
        {"path": TARGET, "content": CONTENT},
        True,
        "tools/call",
        "tool-return",
    )
    events.append(("execute_returned", _now()))
    faults = faults_path()
    fault_lines = faults.read_text(encoding="utf-8").splitlines() if faults.exists() else []
    events.append(("call_end", _now()))
    log = proxy.audit_log[-1]
    return {
        "mode": "production",
        "verdict": log.verdict.value,
        "reason": log.reason,
        "reply": response,
        "events": events,
        "parent_existed_before_call": parent_before,
        "physical_faults_rows": len(fault_lines),
        "faults_path": str(faults),
        "effect_observed": log.effect_observed,
        "witness_scope": log.witness_scope,
        "missing_expected": list(log.missing_expected),
        "has_audit_warning": "__audit_warning__" in response,
        "has_precheck_warning": "__precheck_warning__" in response,
    }


def _summarise(run: Dict[str, Any]) -> Dict[str, Any]:
    named = {name: ts for name, ts in run["events"]}
    start = named["call_start"]
    ordered = [name for name, _ in run["events"]]
    fail_at = named.get("verdict") or named.get("execute_returned")
    return {
        "mode": run["mode"],
        "verdict": run["verdict"],
        "ms_to_fail_readable": round(_ms(start, fail_at), 3) if fail_at else None,
        "ms_call": round(_ms(start, named["call_end"]), 3),
        "event_order": ordered,
        "faults_before_reply": _before(
            ordered, "physical_faults_write", "reply_ready"
        )
        or _before(ordered, "trace_write_return", "execute_returned"),
        "precheck_before_execute": "precheck_warning" in ordered
        and ordered.index("precheck_warning") < ordered.index("execute_done"),
        "precheck_warning": run.get("precheck_warning")
        or run.get("has_precheck_warning"),
    }


def _before(order: List[str], left: str, right: str) -> bool:
    if left not in order or right not in order:
        return False
    return order.index(left) < order.index(right)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="parallel-audit-probe-"))
    rows = []
    for index, fn in enumerate((mode_posthoc, mode_parallel, mode_precheck), start=1):
        cwd = tmp / "sim{}".format(index)
        cwd.mkdir()
        faults = cwd / "physical_faults.jsonl"
        rows.append(fn(cwd, faults))

    prod_cwd = tmp / "prod"
    prod_home = tmp / "gradia-home"
    prod_cwd.mkdir()
    prod_home.mkdir()
    production = mode_production(prod_cwd, prod_home)
    rows.append(production)

    summaries = [_summarise(run) for run in rows]
    report = {
        "scenario": "write_file {} (parent missing)".format(TARGET),
        "tmp": str(tmp),
        "summaries": summaries,
        "production_detail": {
            k: production[k]
            for k in (
                "verdict",
                "reason",
                "parent_existed_before_call",
                "physical_faults_rows",
                "effect_observed",
                "witness_scope",
                "missing_expected",
                "has_audit_warning",
                "has_precheck_warning",
            )
        },
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    out = Path(__file__).with_name("parallel_audit_probe_last.json")
    out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
