"""Optional Gradia memory bus: FAIL records out, overlay rules in.

The four-valued lattice is unchanged. This module never writes a verdict.
A missing overlay, a missing Gradia install, or an unwritable home directory
must not take the sidecar down: the trace is still the source of truth.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "SCHEMA",
    "faults_path",
    "intent_requires_full_witness",
    "load_overlay",
    "record_physical_fault",
    "rules_path",
]

_LOG = logging.getLogger(__name__)

SCHEMA = "gradia.cross_layer.v1"

_overlay_mtime: Optional[float] = None
_overlay_cache: Dict[str, Any] = {}


def _gradia_home() -> Path:
    env = os.environ.get("GRADIA_HOME")
    if env:
        return Path(env)
    return Path.home() / ".gradia"


def faults_path() -> Path:
    env = os.environ.get("GRADIA_PHYSICAL_FAULTS")
    if env:
        return Path(env)
    return _gradia_home() / "memory" / "physical_faults.jsonl"


def rules_path() -> Path:
    env = os.environ.get("GRADIA_SIDECAR_RULES")
    if env:
        return Path(env)
    return _gradia_home() / "pioneer" / "sidecar_rules.yaml"


def record_physical_fault(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Append one FAIL trace record onto the memory-layer jsonl.

    Writes the shared schema directly so the sidecar never depends on Gradia
    being importable. Gradia retrieve reads the same path.
    """
    if str(record.get("verdict") or "") != "FAIL":
        return None
    row = {
        "schema": SCHEMA,
        "fault_id": str(uuid.uuid4()),
        "tool": str(record.get("tool") or "").strip(),
        "intent": str(record.get("intent") or "").strip(),
        "reason": str(record.get("reason") or "").strip(),
        "timestamp": str(record.get("timestamp") or "")
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "retrieval_count": 0,
        "last_retrieved_at": None,
    }
    extra = {
        "session_id": record.get("session_id"),
        "trace_id": record.get("id"),
        "tier": record.get("tier"),
        "witness_scope": record.get("witness_scope"),
    }
    extra = {key: value for key, value in extra.items() if value not in (None, "", [])}
    if extra:
        row["extra"] = extra
    path = faults_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError as problem:
        _LOG.warning("could not append physical fault to %s: %s", path, problem)
        return None
    return row


def load_overlay() -> Dict[str, Any]:
    """Read sidecar_rules.yaml. Empty dict when absent or unreadable."""
    global _overlay_mtime, _overlay_cache
    path = rules_path()
    try:
        stat = path.stat()
    except OSError:
        _overlay_mtime = None
        _overlay_cache = {}
        return {}
    if _overlay_mtime == stat.st_mtime and _overlay_cache:
        return _overlay_cache
    try:
        import yaml
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as problem:
        _LOG.warning("could not load sidecar overlay %s: %s", path, problem)
        return {}
    if not isinstance(loaded, dict):
        return {}
    _overlay_mtime = stat.st_mtime
    _overlay_cache = loaded
    return loaded


def intent_requires_full_witness(intent: str, tool_name: str = "") -> bool:
    """True when the memory-layer overlay names this intent as high-risk.

    Escalation is monotone: it can only raise the witness to full, never
    lower a verdict.
    """
    overlay = load_overlay()
    block = overlay.get("escalate_witness_to_full") or {}
    if not isinstance(block, dict):
        return False
    tools = block.get("tools") or []
    if tools and tool_name and tool_name not in tools:
        return False
    words = block.get("when_intent_contains") or []
    blob = intent or ""
    lower = blob.lower()
    for word in words:
        token = str(word or "").strip()
        if not token:
            continue
        if token.isascii():
            if token.lower() in lower:
                return True
        elif token in blob:
            return True
    return False
