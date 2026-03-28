"""Flow registry — named data streams across the zpilot mesh.

A flow is a named data channel between nodes. MIME type determines
what the data is and how it renders:

  x-zpilot/tty      — terminal session (bidirectional, persistent)
  application/*      — binary files, executables
  text/*             — text files, logs, configs
  image/*            — images (rendered inline in dashboard)
  audio/*            — audio streams

Flows are the unified abstraction over TTY sessions, file transfers,
and streaming data. The control plane handles naming/discovery, the
data plane uses the fastest available transport.

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
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

mimetypes.init()


# ── Configuration ─────────────────────────────────────────────

STAGING_DIR = Path("/tmp/zpilot/flows")
MAX_FLOW_SIZE = 5 * 1024 * 1024 * 1024  # 5 GB
DEFAULT_TTL = 3600  # 1 hour
CHUNK_SIZE = 65536  # 64 KB streaming chunks

ALLOWED_READ_DIRS = [
    Path.home(),
    Path("/tmp"),
]

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")

# zpilot custom MIME types
MIME_TTY = "x-zpilot/tty"
MIME_DEFAULT = "application/octet-stream"


# ── Types ─────────────────────────────────────────────────────

class FlowState(Enum):
    OFFERED = "offered"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


def guess_mime(path: str | None = None, name: str | None = None) -> str:
    """Guess MIME type from file path or flow name."""
    for src in (path, name):
        if src:
            mime, _ = mimetypes.guess_type(src)
            if mime:
                return mime
    return MIME_DEFAULT


def mime_category(mime: str) -> str:
    """Broad category for rendering decisions."""
    if mime == MIME_TTY:
        return "tty"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return "text"
    if mime.startswith("audio/"):
        return "audio"
    return "binary"


@dataclass
class FlowInfo:
    """Metadata about a registered flow."""
    name: str
    mime: str = MIME_DEFAULT
    state: FlowState = FlowState.OFFERED
    direction: str = "out"
    source_path: str | None = None
    size: int = 0
    transferred: int = 0
    sha256: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    ttl: float = DEFAULT_TTL
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > self.ttl

    @property
    def progress(self) -> float:
        return min(1.0, self.transferred / self.size) if self.size > 0 else 0.0

    @property
    def category(self) -> str:
        return mime_category(self.mime)

    @property
    def is_tty(self) -> bool:
        return self.mime == MIME_TTY

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mime": self.mime,
            "category": self.category,
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
    """Registry of named flows on this node."""

    def __init__(self):
        self._flows: dict[str, FlowInfo] = {}
        STAGING_DIR.mkdir(parents=True, exist_ok=True)

    def validate_name(self, name: str) -> str | None:
        if not _NAME_RE.match(name):
            return "Invalid flow name: must be alphanumeric/dash/dot/colon, 1-128 chars"
        return None

    def validate_read_path(self, path: str) -> str | None:
        resolved = Path(path).resolve()
        if ".." in resolved.parts:
            return "Path traversal not allowed"
        for allowed in ALLOWED_READ_DIRS:
            if str(resolved).startswith(str(allowed.resolve())):
                return None
        return f"Path not in allowed directories: {resolved}"

    def offer(self, name: str, mime: str | None = None,
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
            size = p.stat().st_size if p.is_file() else 0
            if size > MAX_FLOW_SIZE:
                return f"File too large: {size} bytes (max {MAX_FLOW_SIZE})"
        else:
            size = 0
        if not mime:
            mime = guess_mime(path=source_path, name=name)
        flow = FlowInfo(
            name=name, mime=mime,
            state=FlowState.OFFERED, direction="out",
            source_path=source_path, size=size, ttl=ttl,
            metadata=metadata or {},
        )
        self._flows[name] = flow
        return flow

    def receive(self, name: str, mime: str = MIME_DEFAULT,
                ttl: float = DEFAULT_TTL) -> FlowInfo | str:
        """Register an incoming flow."""
        err = self.validate_name(name)
        if err:
            return err
        flow = FlowInfo(
            name=name, mime=mime,
            state=FlowState.STREAMING, direction="in", ttl=ttl,
        )
        self._flows[name] = flow
        (STAGING_DIR / name).mkdir(parents=True, exist_ok=True)
        return flow

    def get(self, name: str) -> FlowInfo | None:
        flow = self._flows.get(name)
        if flow and flow.expired:
            flow.state = FlowState.EXPIRED
        return flow

    def list_flows(self, include_expired: bool = False) -> list[FlowInfo]:
        self._cleanup_expired()
        flows = list(self._flows.values())
        if not include_expired:
            flows = [f for f in flows if f.state != FlowState.EXPIRED]
        return flows

    def complete(self, name: str, sha256: str | None = None) -> None:
        flow = self._flows.get(name)
        if flow:
            flow.state = FlowState.COMPLETED
            if sha256:
                flow.sha256 = sha256

    def fail(self, name: str, error: str = "") -> None:
        flow = self._flows.get(name)
        if flow:
            flow.state = FlowState.FAILED
            flow.metadata["error"] = error

    def remove(self, name: str) -> bool:
        if name in self._flows:
            del self._flows[name]
            stage_dir = STAGING_DIR / name
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            return True
        return False

    def staging_path(self, name: str) -> Path:
        return STAGING_DIR / name

    def _cleanup_expired(self) -> None:
        now = time.monotonic()
        for name in [n for n, f in self._flows.items() if now - f.created_at > f.ttl]:
            self.remove(name)


# ── Rendering ─────────────────────────────────────────────────

def render_flow(flow: FlowInfo) -> tuple[bytes, str]:
    """Render a flow for display. Returns (bytes, content_type).

    TTY → terminal screenshot PNG
    image → raw image bytes
    text → UTF-8 text (truncated)
    binary → hex dump preview
    """
    cat = flow.category

    if cat == "tty" and flow.source_path:
        try:
            from .thumbnail import render_thumbnail_from_log
            session = flow.name.split(":")[0] if ":" in flow.name else flow.name
            png = render_thumbnail_from_log(session)
            if png:
                return png, "image/png"
        except Exception:
            pass
        return b"(no render)", "text/plain"

    if cat == "image" and flow.source_path and os.path.exists(flow.source_path):
        return Path(flow.source_path).read_bytes(), flow.mime

    if cat == "text" and flow.source_path and os.path.exists(flow.source_path):
        text = Path(flow.source_path).read_text(errors="replace")[:10000]
        return text.encode(), "text/plain; charset=utf-8"

    if flow.source_path and os.path.exists(flow.source_path):
        with open(flow.source_path, "rb") as f:
            head = f.read(256)
        lines = []
        for i in range(0, len(head), 16):
            chunk = head[i:i+16]
            hexs = " ".join(f"{b:02x}" for b in chunk)
            asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{i:08x}  {hexs:<48s}  {asci}")
        return "\n".join(lines).encode(), "text/plain; charset=utf-8"

    return b"(empty flow)", "text/plain"


# ── Streaming helpers ─────────────────────────────────────────

async def stream_file_chunks(path: str, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
    import asyncio
    loop = asyncio.get_event_loop()
    def _read():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    for chunk in await loop.run_in_executor(None, lambda: list(_read())):
        yield chunk


def compute_sha256(path: str) -> str:
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
    """Auto-register active TTY sessions as flows with x-zpilot/tty MIME."""
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
                name=name, mime=MIME_TTY,
                source_path=str(log_file), ttl=86400,
                metadata={"session": name},
            )
            count += 1
    return count


# ── Global registry instance ──────────────────────────────────

flow_registry = FlowRegistry()
