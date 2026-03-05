"""Textual TUI dashboard for zpilot."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Static

from ..config import load_config
from ..events import EventBus
from ..models import Event, PaneState, ZpilotConfig


STATE_ICONS = {
    "active": "⏳",
    "idle": "✅",
    "waiting": "🔔",
    "error": "❌",
    "exited": "🏁",
    "unknown": "❓",
}


class SessionCard(Static):
    """A card showing one session's status."""

    def __init__(
        self,
        session_name: str,
        state: str = "unknown",
        idle_secs: float = 0,
        last_line: str = "",
        **kwargs,
    ):
        self.session_name = session_name
        self.state = state
        self.idle_secs = idle_secs
        self.last_line = last_line
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        yield Static(self._render())

    def _render(self) -> str:
        icon = STATE_ICONS.get(self.state, "❓")
        idle_str = self._format_idle(self.idle_secs)
        preview = self.last_line[:40] if self.last_line else ""
        return (
            f" {icon} {self.session_name}\n"
            f"   [{self.state}]  idle: {idle_str}\n"
            f"   {preview}"
        )

    @staticmethod
    def _format_idle(secs: float) -> str:
        if secs < 60:
            return f"{secs:.0f}s"
        elif secs < 3600:
            return f"{secs / 60:.0f}m"
        else:
            return f"{secs / 3600:.1f}h"


class EventLog(Static):
    """Shows recent events."""

    events: reactive[list[dict]] = reactive(list, layout=True)

    def render(self) -> str:
        if not self.events:
            return " No events yet"
        lines = [" Events:"]
        for ev in self.events[-10:]:
            ts = datetime.fromtimestamp(ev.get("ts", 0)).strftime("%H:%M:%S")
            session = ev.get("session", "?")
            old = ev.get("old_state", "?")
            new = ev.get("new_state", "?")
            icon = STATE_ICONS.get(new, "")
            lines.append(f"  {ts}  {icon} {session}  {old} → {new}")
        return "\n".join(lines)


class ZpilotApp(App):
    """zpilot TUI dashboard."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #sessions {
        height: auto;
        max-height: 60%;
        padding: 1;
    }
    #events {
        height: 1fr;
        padding: 1;
        border-top: solid $accent;
    }
    SessionCard {
        width: 30;
        height: 5;
        border: round $primary;
        margin: 0 1;
    }
    .waiting {
        border: round $error;
    }
    .error {
        border: round $error;
    }
    .idle {
        border: round $success;
    }
    .active {
        border: round $warning;
    }
    """

    BINDINGS = [
        ("n", "new_session", "New Session"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config: ZpilotConfig | None = None):
        super().__init__()
        self.config = config or load_config()
        self.event_bus = EventBus(self.config.events_file)
        self._session_data: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="sessions"):
            yield Static(" Loading sessions...", id="session-cards")
        yield EventLog(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "zpilot"
        self.sub_title = "Mission Control"
        self.set_interval(5.0, self._poll_sessions)
        self.set_interval(2.0, self._update_events)
        # Initial load
        self.call_later(self._poll_sessions)
        self.call_later(self._update_events)

    async def _poll_sessions(self) -> None:
        """Poll Zellij for session status."""
        try:
            from .. import zellij
            from ..detector import PaneDetector

            if not await zellij.is_available():
                self.query_one("#session-cards", Static).update(
                    " ❌ Zellij not found in PATH"
                )
                return

            sessions = await zellij.list_sessions()
            detector = PaneDetector(self.config)
            data = []
            for s in sessions:
                try:
                    content = await zellij.dump_pane(session=s.name)
                    state = detector.detect(s.name, "focused", content)
                    idle = detector.get_idle_seconds(s.name, "focused")
                    last_line = ""
                    lines = content.strip().splitlines()
                    if lines:
                        last_line = lines[-1]
                    data.append({
                        "name": s.name,
                        "state": state.value,
                        "idle": idle,
                        "last": last_line,
                    })
                except Exception:
                    data.append({
                        "name": s.name,
                        "state": "unknown",
                        "idle": 0,
                        "last": "",
                    })

            self._session_data = data
            self._render_sessions()

        except Exception as e:
            self.query_one("#session-cards", Static).update(f" Error: {e}")

    def _render_sessions(self) -> None:
        """Render session cards."""
        if not self._session_data:
            self.query_one("#session-cards", Static).update(" No sessions")
            return

        lines = []
        for s in self._session_data:
            icon = STATE_ICONS.get(s["state"], "❓")
            idle_str = SessionCard._format_idle(s["idle"])
            last = s["last"][:50] if s["last"] else ""
            lines.append(
                f"  {icon} {s['name']:20s}  [{s['state']:8s}]  "
                f"idle={idle_str:6s}  {last}"
            )

        self.query_one("#session-cards", Static).update(
            " Sessions:\n" + "\n".join(lines)
        )

    async def _update_events(self) -> None:
        """Update the event log."""
        events = self.event_bus.recent(15)
        event_widget = self.query_one("#events", EventLog)
        event_widget.events = [e.to_dict() for e in events]

    def action_new_session(self) -> None:
        """Create a new session (placeholder — would open input dialog)."""
        self.notify("New session: use 'zpilot new <name>' from CLI")

    async def action_refresh(self) -> None:
        """Force refresh."""
        await self._poll_sessions()
        await self._update_events()
        self.notify("Refreshed")
