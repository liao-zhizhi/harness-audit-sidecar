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
    "SENTIENCE_SCHEMA",
    "events_path",
    "faults_path",
    "intent_requires_full_witness",
    "load_overlay",
    "record_physical_fault",
    "record_scale_events",
    "rules_path",
]

_LOG = logging.getLogger(__name__)

SCHEMA = "gradia.cross_layer.v1"
SENTIENCE_SCHEMA = "agent_sentience.multi_scale.v1"

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


def events_path() -> Path:
    env = os.environ.get("GRADIA_MULTI_SCALE_EVENTS")
    if env:
        return Path(env)
    return _gradia_home() / "memory" / "multi_scale_events.jsonl"


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


def _relative_path(record: Mapping[str, Any]) -> str:
    prechecks = record.get("prechecks") or []
    if isinstance(prechecks, list) and prechecks:
        first = prechecks[0]
        if isinstance(first, dict) and first.get("path"):
            return str(first.get("path") or "")
    missing = record.get("missing_expected") or []
    if isinstance(missing, list) and missing:
        return str(missing[0])
    intent = str(record.get("intent") or "")
    for token in intent.replace("：", " ").replace(":", " ").split():
        if "/" in token or token.endswith((".py", ".txt", ".md", ".json")):
            return token.strip("，。;；")
    return ""


def _append_scale_event(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = events_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError as problem:
        _LOG.warning("could not append multi-scale event to %s: %s", path, problem)
        return None
    return row


def record_scale_events(record: Mapping[str, Any]) -> list:
    """MICRO on parent-missing precheck; TACTICAL on FAIL. Never changes the lattice.

    Sidecar does not import Gradia. Gradia retrieve reads the same jsonl.
    """
    written: list = []
    intent = str(record.get("intent") or "").strip()
    relative = _relative_path(record)
    stamp = str(record.get("timestamp") or "") or datetime.now(
        timezone.utc
    ).replace(microsecond=0).isoformat()
    project_root = str(
        record.get("project_root")
        or os.environ.get("GRADIA_PROJECT_ROOT")
        or os.getcwd()
    )
    prechecks = record.get("prechecks") or []
    if isinstance(prechecks, list):
        for item in prechecks:
            if not isinstance(item, dict):
                continue
            missing = str(item.get("missing_dir") or "").strip()
            if not missing:
                continue
            row = {
                "schema": SENTIENCE_SCHEMA,
                "event_id": str(uuid.uuid4()),
                "scale": "MICRO",
                "source": "sidecar",
                "intent": intent,
                "witness": {
                    "path": str(item.get("path") or relative),
                    "missing_dir": missing,
                    "precheck_timestamp": str(item.get("timestamp") or ""),
                },
                "verdict": "INVALID",
                "pain_level": 1,
                "timestamp": stamp,
                "project_root": project_root,
                "relative_path": str(item.get("path") or relative),
            }
            stored = _append_scale_event(row)
            if stored:
                written.append(stored)
    if str(record.get("verdict") or "") == "FAIL":
        row = {
            "schema": SENTIENCE_SCHEMA,
            "event_id": str(uuid.uuid4()),
            "scale": "TACTICAL",
            "source": "sidecar",
            "intent": intent,
            "witness": {
                "tool": str(record.get("tool") or ""),
                "reason": str(record.get("reason") or ""),
                "check_observed": record.get("check_observed"),
                "effect_observed": record.get("effect_observed"),
            },
            "verdict": "FAIL",
            "pain_level": 2,
            "timestamp": stamp,
            "project_root": project_root,
            "relative_path": relative,
        }
        stored = _append_scale_event(row)
        if stored:
            written.append(stored)
    return written


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
