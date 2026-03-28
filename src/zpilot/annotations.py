"""Annotations — operational context for nodes, sessions, and the fleet.

Stores key/value metadata that copilots and humans can read/write:
  - Node annotations: capabilities, runbooks, wake scripts
  - Session annotations: purpose, owner, warnings
  - Fleet annotations: shared config, conventions

Storage: ~/.config/zpilot/annotations/{scope}.json
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("zpilot.annotations")

ANNOTATIONS_DIR = Path("~/.config/zpilot/annotations").expanduser()


def _ensure_dir() -> None:
    ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _scope_file(scope: str) -> Path:
    """Get the JSON file for a scope (node name, session name, or 'fleet')."""
    # Sanitize scope name
    safe = "".join(c for c in scope if c.isalnum() or c in "-_.")
    if not safe:
        safe = "_default"
    return ANNOTATIONS_DIR / f"{safe}.json"


def _load(scope: str) -> dict[str, Any]:
    """Load annotations for a scope."""
    path = _scope_file(scope)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load annotations for %s: %s", scope, e)
        return {}


def _save(scope: str, data: dict[str, Any]) -> None:
    """Save annotations for a scope."""
    _ensure_dir()
    path = _scope_file(scope)
    path.write_text(json.dumps(data, indent=2, default=str))


# ── Public API ────────────────────────────────────────────────

def get(scope: str, key: str | None = None) -> Any:
    """Get annotation(s) for a scope.

    If key is None, returns all annotations as a dict.
    If key is given, returns that value (or None).
    """
    data = _load(scope)
    if key is None:
        return data
    return data.get(key)


def set_annotation(scope: str, key: str, value: Any) -> None:
    """Set an annotation. Value can be string, list, dict, etc."""
    data = _load(scope)
    data[key] = value
    data["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save(scope, data)


def delete(scope: str, key: str) -> bool:
    """Delete an annotation. Returns True if it existed."""
    data = _load(scope)
    if key in data:
        del data[key]
        data["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save(scope, data)
        return True
    return False


def list_scopes() -> list[str]:
    """List all scopes that have annotations."""
    _ensure_dir()
    return [p.stem for p in ANNOTATIONS_DIR.glob("*.json")]


def get_all(scope: str) -> dict[str, Any]:
    """Get all annotations for a scope (excluding internal keys)."""
    data = _load(scope)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def get_for_display(scope: str) -> list[dict]:
    """Get annotations formatted for display."""
    data = _load(scope)
    result = []
    for k, v in sorted(data.items()):
        if k.startswith("_"):
            continue
        result.append({
            "key": k,
            "value": v if isinstance(v, str) else json.dumps(v),
            "type": type(v).__name__,
        })
    return result


# ── Convenience helpers ───────────────────────────────────────

def set_node_runbook(node: str, steps: list[str]) -> None:
    """Store a runbook (recovery procedure) for a node."""
    set_annotation(node, "runbook", steps)


def get_node_runbook(node: str) -> list[str] | None:
    """Get a node's runbook."""
    return get(node, "runbook")


def set_session_purpose(session: str, purpose: str) -> None:
    """Annotate what a session is for."""
    set_annotation(session, "purpose", purpose)


def set_session_owner(session: str, owner: str) -> None:
    """Annotate who/what owns a session."""
    set_annotation(session, "owner", owner)
