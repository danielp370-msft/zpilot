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

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp,
            "type": self.event_type,
            "session": self.session,
            "pane": self.pane,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "details": self.details,
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
        )


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
