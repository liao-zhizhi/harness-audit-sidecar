#!/usr/bin/env python3
"""End-to-end test: local physical state -> patched ledger -> refusal or pass.

Three scenarios, each a single HTTP request against a **local** instance of the
patched ``product_mvp`` server:

  A  liar        ``witness=true`` and ``witness_hash=null``
                 expected: 422, ``ledger_written: false``, ledger unchanged
  B  honest      a real file is created by a real command, the sidecar hashes it,
                 and the digest is submitted
                 expected: 200, ``PASS``, ledger +1 with that digest on the chain
  C  careful liar an invented digest, no file at all
                 expected: 200, ``PASS`` -- the residual hole, asserted on
                 purpose so nobody mistakes A-and-B-clearing for grounding

Scenario C is not a bug to fix here. It is the boundary that ``WitnessHashLimits``
documents: the server cannot see the client's disk, so a well-formed digest is a
commitment, not a proof. A test suite that only ran A and B would report "the
hole is closed" while leaving the interesting half unmeasured.

Usage:
    python3 examples/real_causal_e2e.py \
        --server-python /path/to/python-with-fastapi \
        [--json OUT] [--api-key KEY]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from audit_sidecar import SafeRunner, new_trace_id, report_execution, witness_digest  # noqa: E402

PRODUCT_MVP = "/home/liaozhizhi/Gradia/agent-metabolism-os/product_mvp"
SANDBOX = "/tmp/audit_test"
API_KEY = "e2e-local-key-not-a-secret"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(method: str, url: str, payload: Optional[Dict[str, Any]] = None,
         key: Optional[str] = None, timeout: float = 20.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "User-Agent": "causal-e2e/0.1"},
    )
    if key:
        request.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"status": response.status, "body": json.loads(response.read() or b"{}")}
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"detail": raw}
        return {"status": error.code, "body": parsed}
    except Exception as problem:  # noqa: BLE001
        return {"status": None, "body": {"detail": "{}: {}".format(type(problem).__name__, problem)}}


def wait_healthy(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - still starting
            time.sleep(0.3)
    return False


def ledger(base: str) -> Dict[str, Any]:
    probe = http("GET", base + "/api/v1/ledger?limit=200")
    return probe["body"] if probe["status"] == 200 else {"error": probe["body"]}


def sha256_of_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def scenario_a(base: str, key: str) -> Dict[str, Any]:
    """Liar: claims success, offers no digest."""
    before = ledger(base)
    trace = new_trace_id()
    response = http("POST", base + "/api/v1/verify", {
        "agent_id": "liar_no_hash",
        "intent": "create hello.txt containing hello",
        "check": True,
        "witness": True,
        "witness_hash": None,
        "trace_id": trace,
    }, key=key)
    after = ledger(base)
    return {
        "scenario": "A",
        "name": "liar: witness=true, witness_hash=null",
        "request": {"witness": True, "witness_hash": None, "trace_id": trace},
        "http_status": response["status"],
        "response": response["body"],
        "ledger_before": before.get("total_entries"),
        "ledger_after": after.get("total_entries"),
        "ledger_delta": (after.get("total_entries") or 0) - (before.get("total_entries") or 0),
        "rejected": response["status"] in (400, 409, 422),
        "ledger_written": bool(response["body"].get("ledger_written", True))
        if isinstance(response["body"], dict) else True,
    }


def scenario_b(base: str, key: str) -> Dict[str, Any]:
    """Honest: a real command creates a real file; the sidecar hashes it."""
    os.makedirs(SANDBOX, exist_ok=True)
    target = os.path.join(SANDBOX, "hello.txt")
    if os.path.exists(target):
        os.remove(target)

    runner = SafeRunner()
    result = runner.run_shell(
        command='printf hello > hello.txt',
        cwd=SANDBOX,
        intent_desc="create hello.txt containing hello",
        expected_file="hello.txt",
        expected_content="hello",
    )
    digest = witness_digest(target)

    before = ledger(base)
    trace = new_trace_id()
    report = report_execution(
        result,
        cloud=base,
        agent_id="honest_agent",
        intent="create hello.txt containing hello",
        artefact_path=target,
        api_key=key,
        trace_id=trace,
    )
    after = ledger(base)

    entry = next(
        (e for e in after.get("entries", []) if e.get("trace_id") == trace), None
    )
    return {
        "scenario": "B",
        "name": "honest: real file, real digest",
        "sidecar_verdict": str(result.verdict),
        "witness_effect_observed": result.effect_observed,
        "file_exists": os.path.exists(target),
        "file_content": open(target).read() if os.path.exists(target) else None,
        "local_digest": digest,
        "http_status": report.status,
        "accepted": report.accepted,
        "response_verdict": report.verdict,
        "response_grounded": report.body.get("grounded") if isinstance(report.body, dict) else None,
        "ledger_before": before.get("total_entries"),
        "ledger_after": after.get("total_entries"),
        "ledger_delta": (after.get("total_entries") or 0) - (before.get("total_entries") or 0),
        "chain_entry": {
            "seq": entry.get("seq") if entry else None,
            "witness_hash": entry.get("witness_hash") if entry else None,
            "trace_id": entry.get("trace_id") if entry else None,
            "verdict": entry.get("verdict") if entry else None,
        },
        "digest_matches_chain": bool(entry) and (
            entry.get("witness_hash") == (digest or "").replace("sha256:", "")
        ),
    }


def scenario_c(base: str, key: str) -> Dict[str, Any]:
    """Careful liar: no file anywhere, a well-formed digest of a made-up string."""
    before = ledger(base)
    fabricated = sha256_of_text("hello")
    trace = new_trace_id()
    response = http("POST", base + "/api/v1/verify", {
        "agent_id": "liar_fake_hash",
        "intent": "create hello.txt containing hello",
        "check": True,
        "witness": True,
        "witness_hash": fabricated,
        "trace_id": trace,
    }, key=key)
    after = ledger(base)
    entry = next(
        (e for e in after.get("entries", []) if e.get("trace_id") == trace), None
    )
    return {
        "scenario": "C",
        "name": "careful liar: invented digest, no file",
        "requested_digest": fabricated,
        "file_created": False,
        "http_status": response["status"],
        "response": response["body"],
        "ledger_before": before.get("total_entries"),
        "ledger_after": after.get("total_entries"),
        "ledger_delta": (after.get("total_entries") or 0) - (before.get("total_entries") or 0),
        "booked_as": entry.get("verdict") if entry else None,
        "passed": response["status"] == 200
        and isinstance(response["body"], dict)
        and response["body"].get("verdict") == "PASS",
        "note": (
            "PASS here is the honest result of this test, not a defect in the "
            "fix: the cloud validates the digest's form, and cannot see a "
            "remote disk. Closing this needs a witness the client does not "
            "control (instrument audit trail, hardware attestation, notary)."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", default=sys.executable,
                        help="interpreter that can import fastapi/uvicorn")
    parser.add_argument("--json", default="")
    parser.add_argument("--api-key", default=API_KEY)
    args = parser.parse_args()

    port = free_port()
    base = "http://127.0.0.1:{}".format(port)
    env = dict(os.environ)
    env["CEL_API_KEY"] = args.api_key
    env["PYTHONUNBUFFERED"] = "1"

    print("[1/5] starting patched server on {}".format(base))
    server = subprocess.Popen(
        [args.server_python, "-m", "uvicorn", "api_server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=PRODUCT_MVP, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    report: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "server": {"base": base, "cwd": PRODUCT_MVP, "auth": True},
    }
    try:
        if not wait_healthy(base):
            output = ""
            if server.stdout:
                server.stdout.close()
            print("      server did not become healthy")
            return 1
        print("      healthy")

        report["baseline"] = {
            "total_entries": ledger(base).get("total_entries"),
        }
        print("[2/5] baseline ledger entries = {}".format(
            report["baseline"]["total_entries"]))

        print("[3/5] scenario A: liar, no digest")
        report["scenario_a"] = scenario_a(base, args.api_key)
        a = report["scenario_a"]
        print("      HTTP {} verdict={} ledger {} -> {} (delta {})".format(
            a["http_status"],
            a["response"].get("verdict") if isinstance(a["response"], dict) else "?",
            a["ledger_before"], a["ledger_after"], a["ledger_delta"]))

        print("[4/5] scenario B: real file, real digest via sidecar")
        report["scenario_b"] = scenario_b(base, args.api_key)
        b = report["scenario_b"]
        print("      local digest = {}".format(b["local_digest"]))
        print("      HTTP {} verdict={} ledger {} -> {} (delta {})".format(
            b["http_status"], b["response_verdict"],
            b["ledger_before"], b["ledger_after"], b["ledger_delta"]))
        print("      digest on chain = {} (match={})".format(
            b["chain_entry"]["witness_hash"], b["digest_matches_chain"]))

        print("[4b/5] scenario C: careful liar, invented digest")
        report["scenario_c"] = scenario_c(base, args.api_key)
        c = report["scenario_c"]
        print("      HTTP {} verdict={} ledger delta {}".format(
            c["http_status"],
            c["response"].get("verdict") if isinstance(c["response"], dict) else "?",
            c["ledger_delta"]))

        print("[5/5] chain integrity")
        verify = http("GET", base + "/api/v1/ledger/verify")
        stats = http("GET", base + "/api/v1/stats")
        report["final"] = {
            "chain_ok": verify["body"].get("chain_ok"),
            "total_entries": verify["body"].get("total_entries"),
            "rejected_unwitnessed": stats["body"].get("rejected_unwitnessed"),
            "verified_passes": stats["body"].get("verified_passes"),
        }
        print("      chain_ok={} entries={} rejected={} passes={}".format(
            report["final"]["chain_ok"], report["final"]["total_entries"],
            report["final"]["rejected_unwitnessed"], report["final"]["verified_passes"]))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()

    print()
    print(json.dumps(
        {k: report.get(k) for k in ("scenario_a", "scenario_b", "scenario_c")},
        ensure_ascii=False, indent=2)[:4000])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("\nwritten: {}".format(args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
