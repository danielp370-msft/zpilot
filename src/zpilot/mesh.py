"""zpilot mesh join/invite system.

Handles secure enrollment of new nodes into a zpilot mesh.
Any zpilot node can invite others; any node can be the entry point.

Flow:
  1. Inviter runs: zpilot invite [--name suggested-name] [--url my-url]
     → generates token, stores pending invite, prints join command

  2. Joiner runs: zpilot join --token <token> [--name my-name] [--url my-url]
     → decodes token, POSTs to inviter's /api/mesh/join
     → inviter validates, adds joiner to its nodes.toml
     → joiner adds inviter to its nodes.toml
     → both can now communicate
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(
    os.environ.get("ZPILOT_CONFIG_DIR", "~/.config/zpilot")
).expanduser()

PENDING_INVITES_FILE = CONFIG_DIR / "pending-invites.json"

NODES_FILE = Path(
    os.environ.get("ZPILOT_NODES_FILE", str(CONFIG_DIR / "nodes.toml"))
).expanduser()


# ---------------------------------------------------------------------------
# Invite data
# ---------------------------------------------------------------------------

@dataclass
class Invite:
    """A pending mesh invite."""
    secret: str
    inviter_name: str
    inviter_url: str
    created_at: float
    expires_at: float
    suggested_name: str = ""
    used: bool = False
    used_by: str = ""


# ---------------------------------------------------------------------------
# Token encode / decode
# ---------------------------------------------------------------------------

def generate_invite(
    inviter_url: str,
    inviter_name: str = "",
    expires_minutes: int = 60,
    suggested_name: str = "",
) -> tuple[str, Invite]:
    """Generate an invite token for a new node to join the mesh.

    Returns (token_string, invite_record).
    """
    if not inviter_name:
        inviter_name = socket.gethostname()

    invite_secret = secrets.token_urlsafe(32)
    now = time.time()

    invite = Invite(
        secret=invite_secret,
        inviter_name=inviter_name,
        inviter_url=inviter_url,
        created_at=now,
        expires_at=now + (expires_minutes * 60),
        suggested_name=suggested_name,
    )

    # Persist to disk so the server can validate later
    _save_invite(invite)

    # Build compact token (base64url JSON)
    payload: dict[str, Any] = {
        "u": inviter_url,
        "n": inviter_name,
        "s": invite_secret,
        "e": invite.expires_at,
    }
    if suggested_name:
        payload["sn"] = suggested_name

    token = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")

    return token, invite


def decode_invite(token: str) -> dict[str, Any]:
    """Decode an invite token string back to its payload.

    Returns dict with keys: url, inviter, secret, expires, suggested_name.
    """
    # Re-pad base64
    padding = 4 - (len(token) % 4)
    if padding != 4:
        token += "=" * padding

    raw = base64.urlsafe_b64decode(token)
    data = json.loads(raw)

    # Normalize short keys to readable names
    return {
        "url": data["u"],
        "inviter": data["n"],
        "secret": data["s"],
        "expires": data["e"],
        "suggested_name": data.get("sn", ""),
    }


# ---------------------------------------------------------------------------
# Pending invites persistence
# ---------------------------------------------------------------------------

def _load_invites() -> list[dict[str, Any]]:
    """Load pending invites from disk."""
    if not PENDING_INVITES_FILE.exists():
        return []
    try:
        return json.loads(PENDING_INVITES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_invite(invite: Invite) -> None:
    """Append an invite to the pending invites file."""
    invites = _load_invites()
    invites.append({
        "secret": invite.secret,
        "inviter_name": invite.inviter_name,
        "inviter_url": invite.inviter_url,
        "created_at": invite.created_at,
        "expires_at": invite.expires_at,
        "suggested_name": invite.suggested_name,
        "used": False,
        "used_by": "",
    })
    PENDING_INVITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_INVITES_FILE.write_text(json.dumps(invites, indent=2))
    PENDING_INVITES_FILE.chmod(0o600)


def validate_invite(secret: str) -> dict[str, Any] | None:
    """Validate an invite secret.

    Returns invite data if valid, None if invalid/expired/used.
    Uses constant-time comparison for the secret.
    """
    invites = _load_invites()
    now = time.time()

    for inv in invites:
        if secrets.compare_digest(inv["secret"], secret):
            if inv.get("used"):
                return None
            if now > inv.get("expires_at", 0):
                return None
            return inv

    return None


def mark_invite_used(secret: str, used_by: str) -> bool:
    """Mark an invite as used (one-time)."""
    invites = _load_invites()

    for inv in invites:
        if secrets.compare_digest(inv["secret"], secret):
            inv["used"] = True
            inv["used_by"] = used_by
            PENDING_INVITES_FILE.write_text(json.dumps(invites, indent=2))
            return True

    return False


def cleanup_expired_invites() -> int:
    """Remove expired invites. Returns count removed."""
    invites = _load_invites()
    now = time.time()
    before = len(invites)
    invites = [i for i in invites if i.get("expires_at", 0) > now or i.get("used")]
    after = len(invites)
    if before != after:
        PENDING_INVITES_FILE.write_text(json.dumps(invites, indent=2))
    return before - after


# ---------------------------------------------------------------------------
# nodes.toml management
# ---------------------------------------------------------------------------

def _serialize_node_toml(name: str, config: dict[str, Any]) -> str:
    """Serialize a single node entry to TOML text."""
    lines = [f"\n[nodes.{name}]"]

    labels = config.pop("labels", None)

    for k, v in config.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {str(v).lower()}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        else:
            lines.append(f'{k} = "{v}"')

    if labels:
        lines.append(f"\n[nodes.{name}.labels]")
        for lk, lv in labels.items():
            lines.append(f'{lk} = "{lv}"')

    return "\n".join(lines) + "\n"


def node_exists(name: str) -> bool:
    """Check if a node already exists in nodes.toml."""
    if not NODES_FILE.exists():
        return False
    content = NODES_FILE.read_text()
    return f"[nodes.{name}]" in content


def add_node_to_config(
    name: str,
    url: str,
    token: str,
    labels: dict[str, str] | None = None,
    verify_ssl: bool = False,
) -> None:
    """Add a node to nodes.toml (append). Raises if already exists."""
    if node_exists(name):
        raise ValueError(f"Node '{name}' already exists in {NODES_FILE}")

    node_config: dict[str, Any] = {
        "transport": "mcp",
        "url": url,
        "token": token,
        "verify_ssl": verify_ssl,
    }
    if labels:
        node_config["labels"] = dict(labels)

    entry_text = _serialize_node_toml(name, node_config)

    # Ensure file exists with header
    if not NODES_FILE.exists():
        NODES_FILE.parent.mkdir(parents=True, exist_ok=True)
        NODES_FILE.write_text(
            "# zpilot fleet nodes configuration\n"
            "# Local node is always implicit (no config needed).\n"
        )
        NODES_FILE.chmod(0o600)

    # Append
    with NODES_FILE.open("a") as f:
        f.write(entry_text)


def update_node_in_config(
    name: str,
    url: str,
    token: str,
    labels: dict[str, str] | None = None,
    verify_ssl: bool = False,
) -> None:
    """Update an existing node or add if not exists."""
    if node_exists(name):
        remove_node_from_config(name)
    add_node_to_config(name, url, token, labels=labels, verify_ssl=verify_ssl)


def remove_node_from_config(name: str) -> bool:
    """Remove a node from nodes.toml. Returns True if found and removed."""
    if not NODES_FILE.exists():
        return False

    content = NODES_FILE.read_text()
    section = f"[nodes.{name}]"
    label_section = f"[nodes.{name}.labels]"

    if section not in content:
        return False

    # Parse line by line, skip the node's sections
    lines = content.split("\n")
    result: list[str] = []
    skipping = False

    for line in lines:
        stripped = line.strip()
        if stripped == section or stripped == label_section:
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            # New section — stop skipping
            skipping = False
        if skipping and (stripped == "" or "=" in stripped):
            continue
        if skipping and stripped == "":
            continue
        result.append(line)

    NODES_FILE.write_text("\n".join(result))
    return True


# ---------------------------------------------------------------------------
# Join handshake helpers
# ---------------------------------------------------------------------------

def build_join_request(
    invite_secret: str,
    node_name: str,
    node_url: str,
    node_token: str,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the JSON payload for POST /api/mesh/join."""
    return {
        "secret": invite_secret,
        "name": node_name,
        "url": node_url,
        "token": node_token,
        "labels": labels or {},
    }


def build_join_response(
    inviter_name: str,
    inviter_url: str,
    inviter_token: str,
    peers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the JSON response from POST /api/mesh/join."""
    return {
        "ok": True,
        "inviter": {
            "name": inviter_name,
            "url": inviter_url,
            "token": inviter_token,
        },
        "peers": peers or [],
        "message": f"Welcome to the mesh! You are now connected to {inviter_name}.",
    }
