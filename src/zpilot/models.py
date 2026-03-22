"""Data models for zpilot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class PaneState(str, Enum):
    """State of a monitored pane."""
    ACTIVE = "active"      # producing output, busy
    IDLE = "idle"          # no output change for idle_threshold
    WAITING = "waiting"    # BEL detected or prompt matched — needs human
    ERROR = "error"        # error pattern detected
    EXITED = "exited"      # shell/process has exited
    UNKNOWN = "unknown"    # not yet categorized


class NodeState(str, Enum):
    """Connectivity state of a node."""
    ONLINE = "online"
    OFFLINE = "offline"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass
class Pane:
    """A Zellij pane."""
    pane_id: int
    name: str | None = None
    command: str | None = None
    is_focused: bool = False
    is_floating: bool = False
    # Tracking fields (set by daemon)
    state: PaneState = PaneState.UNKNOWN
    last_output_time: float = 0.0
    last_content_hash: str = ""
    last_lines: list[str] = field(default_factory=list)


@dataclass
class Session:
    """A Zellij session."""
    name: str
    is_current: bool = False
    panes: list[Pane] = field(default_factory=list)
    tabs: list[str] = field(default_factory=list)
    managed: bool = False  # True if zpilot shell_wrapper is active (FIFO + log)


@dataclass
class Event:
    """An event emitted by the daemon."""
    timestamp: float = field(default_factory=time.time)
    event_type: str = "state_change"  # state_change, notification, error, info
    session: str = ""
    pane: str | None = None
    old_state: str | None = None
    new_state: str = ""
    details: str | None = None
    node: str = "local"

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp,
            "type": self.event_type,
            "session": self.session,
            "pane": self.pane,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "details": self.details,
            "node": self.node,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Event:
        return cls(
            timestamp=d.get("ts", 0.0),
            event_type=d.get("type", ""),
            session=d.get("session", ""),
            pane=d.get("pane"),
            old_state=d.get("old_state"),
            new_state=d.get("new_state"),
            details=d.get("details"),
            node=d.get("node", "local"),
        )


@dataclass
class SessionHealth:
    """Health snapshot of a single session on a node."""
    node: str
    session: str
    state: PaneState
    idle_seconds: float = 0.0
    last_line: str = ""
    error: str | None = None


@dataclass
class NodeHealth:
    """Health snapshot of a node."""
    name: str
    state: NodeState = NodeState.UNKNOWN
    sessions: list[SessionHealth] = field(default_factory=list)
    last_ping: float = 0.0
    error: str | None = None

    @property
    def busy_count(self) -> int:
        return sum(1 for s in self.sessions if s.state == PaneState.ACTIVE)

    @property
    def idle_count(self) -> int:
        return sum(1 for s in self.sessions if s.state in (PaneState.IDLE, PaneState.WAITING))

    @property
    def total_sessions(self) -> int:
        return len(self.sessions)


@dataclass
class FleetStatus:
    """Aggregated health across all nodes."""
    nodes: list[NodeHealth] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def online_count(self) -> int:
        return sum(1 for n in self.nodes if n.state == NodeState.ONLINE)

    @property
    def total_nodes(self) -> int:
        return len(self.nodes)

    @property
    def total_sessions(self) -> int:
        return sum(n.total_sessions for n in self.nodes)

    @property
    def total_busy(self) -> int:
        return sum(n.busy_count for n in self.nodes)

    def summary(self) -> str:
        parts = [
            f"{self.online_count}/{self.total_nodes} nodes online",
            f"{self.total_sessions} sessions",
            f"{self.total_busy} busy",
        ]
        return " │ ".join(parts)


@dataclass
class ZpilotConfig:
    """Runtime configuration."""
    poll_interval: float = 5.0
    idle_threshold: float = 30.0
    events_file: str = "/tmp/zpilot/events.jsonl"
    bel_detection: bool = True
    prompt_patterns: list[str] = field(default_factory=lambda: [
        r"^\$ $",
        r"^> $",
        r"^❯ ",
        r"\$\s*$",              # any line ending with $ (common prompts)
        r"#\s*$",               # root prompt ending with #
        r">>>\s*$",             # Python REPL
        r"\.\.\.\s*$",          # Python continuation
    ])
    error_patterns: list[str] = field(default_factory=lambda: [
        r"^Error:",
        r"^FATAL:",
        r"^panic:",
    ])
    notify_enabled: bool = True
    notify_adapter: str = "log"
    notify_on: list[str] = field(default_factory=lambda: [
        "waiting", "error", "exited",
    ])
    ntfy_topic: str = "zpilot"
    ntfy_server: str = "https://ntfy.sh"
    # HTTP MCP server settings (for distributed zpilot)
    http_host: str = "127.0.0.1"
    http_port: int = 8222
    http_token: str = ""  # empty = auto-generate on first serve-http
    # TLS settings for HTTP server
    http_tls: bool = True  # default ON — always encrypt network traffic
    http_cert_file: str = ""  # path to cert PEM; auto-generated if empty
    http_key_file: str = ""  # path to key PEM; auto-generated if empty
