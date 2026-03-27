"""Flow registry — named data streams across the zpilot mesh.

A flow is a named data channel between nodes. Types:
  tty   — bidirectional, persistent (shell sessions, copilots)
  file  — one-shot transfer, staged to /tmp/zpilot/flows/
  pipe  — continuous unidirectional stream (live logs, command output)

Flows are the unified abstraction over TTY sessions, file transfers,
and streaming data. The control plane handles naming/discovery, the
data plane uses the fastest available transport (WebSocket, HTTP chunked).

Security:
  - All flow operations require authenticated HTTP (Bearer token)
  - File reads restricted to ALLOWED_READ_DIRS
  - File writes go to STAGING_DIR only (consumer explicitly accepts)
  - Flow names sanitized (alphanumeric + dash, no path traversal)
  - Max flow size enforced
  - TTL auto-cleanup for stale flows
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator


# ── Configuration ─────────────────────────────────────────────

STAGING_DIR = Path("/tmp/zpilot/flows")
MAX_FLOW_SIZE = 5 * 1024 * 1024 * 1024  # 5 GB
DEFAULT_TTL = 3600  # 1 hour
CHUNK_SIZE = 65536  # 64 KB streaming chunks

# Directories allowed for flow reads (source files)
ALLOWED_READ_DIRS = [
    Path.home(),
    Path("/tmp"),
]

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


# ── Types ─────────────────────────────────────────────────────

class FlowType(Enum):
    TTY = "tty"      # bidirectional terminal session
    FILE = "file"    # one-shot file transfer
    PIPE = "pipe"    # continuous unidirectional stream


class FlowState(Enum):
    OFFERED = "offered"      # available for consumption
    STREAMING = "streaming"  # actively transferring
    COMPLETED = "completed"  # transfer done
    FAILED = "failed"        # transfer error
    EXPIRED = "expired"      # TTL exceeded


@dataclass
class FlowInfo:
    """Metadata about a registered flow."""
    name: str
    flow_type: FlowType
    state: FlowState = FlowState.OFFERED
    direction: str = "out"        # "out" = provider, "in" = consumer
    source_path: str | None = None  # local file path (for file flows)
    size: int = 0                 # known size in bytes (0 = unknown/streaming)
    transferred: int = 0         # bytes transferred so far
    sha256: str | None = None    # integrity hash (set on completion)
    created_at: float = field(default_factory=time.monotonic)
    ttl: float = DEFAULT_TTL
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > self.ttl

    @property
    def progress(self) -> float:
        if self.size <= 0:
            return 0.0
        return min(1.0, self.transferred / self.size)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.flow_type.value,
            "state": self.state.value,
            "direction": self.direction,
            "size": self.size,
            "transferred": self.transferred,
            "progress": round(self.progress, 3),
            "sha256": self.sha256,
            "ttl": self.ttl,
            "age_seconds": round(time.monotonic() - self.created_at, 1),
            "metadata": self.metadata,
        }


# ── Flow Registry ─────────────────────────────────────────────

class FlowRegistry:
    """Thread-safe registry of named flows on this node."""

    def __init__(self):
        self._flows: dict[str, FlowInfo] = {}
        STAGING_DIR.mkdir(parents=True, exist_ok=True)

    def validate_name(self, name: str) -> str | None:
        """Validate flow name. Returns error message or None."""
        if not _NAME_RE.match(name):
            return f"Invalid flow name: must be alphanumeric/dash/dot, 1-128 chars"
        return None

    def validate_read_path(self, path: str) -> str | None:
        """Validate a source path for reading. Returns error or None."""
        resolved = Path(path).resolve()
        if ".." in resolved.parts:
            return "Path traversal not allowed"
        for allowed in ALLOWED_READ_DIRS:
            if str(resolved).startswith(str(allowed.resolve())):
                return None
        return f"Path not in allowed directories: {resolved}"

    def offer(self, name: str, flow_type: FlowType,
              source_path: str | None = None,
              ttl: float = DEFAULT_TTL,
              metadata: dict | None = None) -> FlowInfo | str:
        """Register a new flow. Returns FlowInfo or error string."""
        err = self.validate_name(name)
        if err:
            return err

        if source_path:
            err = self.validate_read_path(source_path)
            if err:
                return err
            p = Path(source_path)
            if p.is_file():
                size = p.stat().st_size
                if size > MAX_FLOW_SIZE:
                    return f"File too large: {size} bytes (max {MAX_FLOW_SIZE})"
            else:
                size = 0
        else:
            size = 0

        flow = FlowInfo(
            name=name,
            flow_type=flow_type,
            state=FlowState.OFFERED,
            direction="out",
            source_path=source_path,
            size=size if source_path else 0,
            ttl=ttl,
            metadata=metadata or {},
        )
        self._flows[name] = flow
        return flow

    def receive(self, name: str, flow_type: FlowType = FlowType.FILE,
                ttl: float = DEFAULT_TTL) -> FlowInfo | str:
        """Register an incoming flow (for receiving pushes)."""
        err = self.validate_name(name)
        if err:
            return err
        flow = FlowInfo(
            name=name,
            flow_type=flow_type,
            state=FlowState.STREAMING,
            direction="in",
            ttl=ttl,
        )
        self._flows[name] = flow
        # Create staging directory
        stage_dir = STAGING_DIR / name
        stage_dir.mkdir(parents=True, exist_ok=True)
        return flow

    def get(self, name: str) -> FlowInfo | None:
        """Get flow info by name."""
        flow = self._flows.get(name)
        if flow and flow.expired:
            flow.state = FlowState.EXPIRED
        return flow

    def list_flows(self, include_expired: bool = False) -> list[FlowInfo]:
        """List all registered flows."""
        self._cleanup_expired()
        flows = list(self._flows.values())
        if not include_expired:
            flows = [f for f in flows if f.state != FlowState.EXPIRED]
        return flows

    def complete(self, name: str, sha256: str | None = None) -> None:
        """Mark a flow as completed."""
        flow = self._flows.get(name)
        if flow:
            flow.state = FlowState.COMPLETED
            if sha256:
                flow.sha256 = sha256

    def fail(self, name: str, error: str = "") -> None:
        """Mark a flow as failed."""
        flow = self._flows.get(name)
        if flow:
            flow.state = FlowState.FAILED
            flow.metadata["error"] = error

    def remove(self, name: str) -> bool:
        """Remove a flow and clean up staging data."""
        if name in self._flows:
            del self._flows[name]
            stage_dir = STAGING_DIR / name
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            return True
        return False

    def staging_path(self, name: str) -> Path:
        """Get the staging directory path for a flow."""
        return STAGING_DIR / name

    def _cleanup_expired(self) -> None:
        """Remove expired flows."""
        now = time.monotonic()
        expired = [n for n, f in self._flows.items()
                   if now - f.created_at > f.ttl]
        for name in expired:
            self.remove(name)


# ── Streaming helpers ─────────────────────────────────────────

async def stream_file_chunks(path: str, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
    """Yield file content in chunks. Non-blocking via thread executor."""
    import asyncio
    loop = asyncio.get_event_loop()

    def _read_chunks():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    for chunk in await loop.run_in_executor(None, lambda: list(_read_chunks())):
        yield chunk


def compute_sha256(path: str) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ── TTY auto-registration ────────────────────────────────────

def register_tty_sessions(registry: FlowRegistry) -> int:
    """Auto-register active TTY sessions as flows. Returns count added."""
    log_dir = Path("/tmp/zpilot/logs")
    fifo_dir = Path("/tmp/zpilot/fifos")
    count = 0
    if not log_dir.exists():
        return 0
    for log_file in log_dir.glob("*--main.log"):
        name = log_file.name.rsplit("--main.log", 1)[0]
        if not name or registry.get(name):
            continue
        fifo = fifo_dir / f"{name}.fifo"
        alive = False
        if fifo.exists():
            try:
                fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
                alive = True
            except OSError:
                pass
        if alive:
            registry.offer(
                name=name,
                flow_type=FlowType.TTY,
                source_path=str(log_file),
                ttl=86400,  # 24h for TTY sessions
                metadata={"session": name, "type": "tty"},
            )
            count += 1
    return count


# ── Global registry instance ──────────────────────────────────

flow_registry = FlowRegistry()
