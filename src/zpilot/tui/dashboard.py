"""Textual TUI dashboard for zpilot — Focus + Dock + Exposé layout."""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, RichLog, Static
from textual import on, work

from rich.text import Text

from ..config import load_config
from ..events import EventBus
from ..models import PaneState, ZpilotConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_ICONS = {
    "active": "⏳",
    "idle": "✅",
    "waiting": "🔔",
    "error": "❌",
    "exited": "🏁",
    "unknown": "❓",
}

_STATE_PRIORITY = {
    "error": 0,
    "waiting": 1,
    "active": 2,
    "idle": 3,
    "exited": 4,
    "unknown": 5,
}

HELP_TEXT = """\
 zpilot — Keyboard Shortcuts
 ════════════════════════════

  e          Toggle Exposé (grid overview)
  Tab        Next session
  Shift+Tab  Previous session
  1–9        Jump to session by dock position
  v          Toggle vertical split
  n          New session
  d          Delete / kill focused session
  r          Force refresh all sessions
  q          Quit
  ?          Show this help
"""


def _sort_key(s: dict) -> tuple:
    """Sort by attention priority then name."""
    return (_STATE_PRIORITY.get(s["state"], 5), s["name"])


# ---------------------------------------------------------------------------
# SessionCard — used in Exposé grid AND internally
# ---------------------------------------------------------------------------

class SessionCard(Static):
    """A card showing one session's status (Exposé grid tile)."""

    DEFAULT_CSS = """
    SessionCard {
        width: 1fr;
        height: auto;
        min-height: 5;
        border: round $primary;
        padding: 0 1;
        margin: 0 1 1 0;
    }
    SessionCard.card-selected {
        border: tall $accent;
        background: $boost;
    }
    SessionCard.card-error   { border: round $error; }
    SessionCard.card-waiting { border: round $error; }
    SessionCard.card-active  { border: round $warning; }
    SessionCard.card-idle    { border: round $success; }
    """

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

    def on_mount(self) -> None:
        self.update(self._render())
        self._apply_state_class()

    def refresh_data(self, state: str, idle_secs: float, last_line: str) -> None:
        self.state = state
        self.idle_secs = idle_secs
        self.last_line = last_line
        self.update(self._render())
        self._apply_state_class()

    def _apply_state_class(self) -> None:
        for cls in ("card-error", "card-waiting", "card-active", "card-idle"):
            self.remove_class(cls)
        tag = f"card-{self.state}"
        if tag in ("card-error", "card-waiting", "card-active", "card-idle"):
            self.add_class(tag)

    def _render(self) -> str:
        icon = STATE_ICONS.get(self.state, "❓")
        idle_str = self._format_idle(self.idle_secs)
        preview = self.last_line[:60] if self.last_line else ""
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


# ---------------------------------------------------------------------------
# SessionPill — dock bar pill
# ---------------------------------------------------------------------------

class SessionPill(Static):
    """A small pill in the dock bar representing a session."""

    def __init__(self, session_name: str, state: str = "unknown", **kwargs):
        self.session_name = session_name
        self.state = state
        super().__init__(**kwargs)

    def on_mount(self) -> None:
        self._refresh_display()

    def set_state(self, state: str) -> None:
        self.state = state
        self._refresh_display()

    def _refresh_display(self) -> None:
        icon = STATE_ICONS.get(self.state, "❓")
        label = self.session_name
        if len(label) > 14:
            label = label[:13] + "…"
        self.update(f"{icon} {label}")
        for cls in ("pill-idle", "pill-active", "pill-error",
                     "pill-waiting", "pill-unknown"):
            self.remove_class(cls)
        tag = f"pill-{self.state}"
        if tag in ("pill-idle", "pill-active", "pill-error",
                    "pill-waiting", "pill-unknown"):
            self.add_class(tag)
        else:
            self.add_class("pill-unknown")


# ---------------------------------------------------------------------------
# FocusView — main content area
# ---------------------------------------------------------------------------

class FocusView(Vertical):
    """Main content area showing one (or two) session outputs."""
    pass


# ---------------------------------------------------------------------------
# DockBar — bottom strip
# ---------------------------------------------------------------------------

class DockBar(Horizontal):
    """Bottom dock containing session pills."""
    pass


# ---------------------------------------------------------------------------
# HelpScreen
# ---------------------------------------------------------------------------

class HelpScreen(ModalScreen[None]):
    """Shows keybinding help."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-box {
        width: 50;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("question_mark", "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help-box")


# ---------------------------------------------------------------------------
# NewSessionScreen
# ---------------------------------------------------------------------------

class NewSessionScreen(ModalScreen[str | None]):
    """Prompt for a new session name."""

    DEFAULT_CSS = """
    NewSessionScreen {
        align: center middle;
    }
    #new-session-box {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="new-session-box"):
            yield Label("New session name:")
            yield Input(id="new-session-input", placeholder="my-session")

    @on(Input.Submitted, "#new-session-input")
    def _submit(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# ConfirmDeleteScreen
# ---------------------------------------------------------------------------

class ConfirmDeleteScreen(ModalScreen[bool]):
    """Confirm session deletion."""

    DEFAULT_CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm-box {
        width: 50;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "Cancel", show=False),
    ]

    def __init__(self, session_name: str):
        super().__init__()
        self._session_name = session_name

    def compose(self) -> ComposeResult:
        yield Static(
            f"  Kill session [bold]{self._session_name}[/bold]?\n\n"
            f"  Press [bold]y[/bold] to confirm, [bold]n[/bold] to cancel.",
            id="confirm-box",
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# ExposeScreen — grid overlay
# ---------------------------------------------------------------------------

class ExposeScreen(ModalScreen[int | None]):
    """Grid overlay showing all sessions."""

    DEFAULT_CSS = """
    ExposeScreen {
        align: center middle;
    }
    #expose-outer {
        width: 90%;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1;
    }
    #expose-title {
        height: 1;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    #expose-grid {
        height: 1fr;
        layout: grid;
        grid-size: 3;
        grid-gutter: 1;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_expose", "Close", show=False),
        Binding("e", "dismiss_expose", "Close", show=False),
        Binding("enter", "select", "Select"),
    ]

    def __init__(self, session_data: list[dict], focused_idx: int = 0):
        super().__init__()
        self._session_data = session_data
        self._selected = min(focused_idx, max(0, len(session_data) - 1))

    def compose(self) -> ComposeResult:
        with Vertical(id="expose-outer"):
            yield Static(
                f"  Exposé — {len(self._session_data)} sessions  "
                "(↑↓←→ navigate, Enter select, Esc close)",
                id="expose-title",
            )
            with Grid(id="expose-grid"):
                for i, s in enumerate(self._session_data):
                    last_lines = ""
                    if s.get("content"):
                        lines = s["content"].strip().splitlines()
                        last_lines = "\n".join(lines[-3:]) if lines else ""
                    card = SessionCard(
                        session_name=s["name"],
                        state=s["state"],
                        idle_secs=s["idle"],
                        last_line=last_lines,
                        id=f"expose-card-{i}",
                    )
                    yield card

    def on_mount(self) -> None:
        self._highlight()

    def _highlight(self) -> None:
        for i in range(len(self._session_data)):
            try:
                card = self.query_one(f"#expose-card-{i}", SessionCard)
                if i == self._selected:
                    card.add_class("card-selected")
                else:
                    card.remove_class("card-selected")
            except NoMatches:
                pass

    def key_up(self) -> None:
        cols = 3
        if self._selected >= cols:
            self._selected -= cols
            self._highlight()

    def key_down(self) -> None:
        cols = 3
        if self._selected + cols < len(self._session_data):
            self._selected += cols
            self._highlight()

    def key_left(self) -> None:
        if self._selected > 0:
            self._selected -= 1
            self._highlight()

    def key_right(self) -> None:
        if self._selected < len(self._session_data) - 1:
            self._selected += 1
            self._highlight()

    def action_select(self) -> None:
        if self._session_data:
            self.dismiss(self._selected)

    def action_dismiss_expose(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# ZpilotApp — main application
# ---------------------------------------------------------------------------

class ZpilotApp(App):
    """zpilot TUI dashboard — Focus + Dock + Exposé."""

    CSS = """
    Screen {
        layout: vertical;
    }

    /* Focus area */
    #focus-area {
        height: 1fr;
    }
    #focus-header {
        height: 1;
        dock: top;
        background: $surface;
        color: $text;
        text-style: bold;
        padding: 0 1;
    }
    #focus-content {
        height: 1fr;
    }
    #focus-left {
        width: 1fr;
        height: 1fr;
    }
    #focus-right {
        width: 1fr;
        height: 1fr;
        border-left: tall $accent;
    }
    #split-header {
        height: 1;
        dock: top;
        background: $surface;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    .focus-log {
        height: 1fr;
    }
    #welcome-msg {
        width: 100%;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }

    /* Dock */
    #dock {
        height: 3;
        dock: bottom;
        background: $panel;
        layout: horizontal;
        overflow-x: auto;
    }
    #dock-count {
        dock: right;
        width: auto;
        min-width: 14;
        height: 1;
        margin: 1 1 0 0;
        color: $text-muted;
        text-align: right;
    }
    .pill {
        width: auto;
        min-width: 12;
        max-width: 22;
        height: 1;
        margin: 1 0 0 1;
        padding: 0 1;
        text-style: bold;
    }
    .pill-idle    { background: $success; color: $text; }
    .pill-active  { background: $warning; color: $text; }
    .pill-error   { background: $error;   color: $text; }
    .pill-waiting { background: $error;   color: $text; }
    .pill-selected { border: tall $accent; }
    .pill-unknown { background: $surface; color: $text-muted; }
    """

    BINDINGS = [
        Binding("e", "toggle_expose", "Exposé", show=True),
        Binding("tab", "next_session", "Next", show=False),
        Binding("shift+tab", "prev_session", "Prev", show=False),
        Binding("v", "toggle_split", "Split", show=True),
        Binding("n", "new_session", "New", show=True),
        Binding("d", "delete_session", "Delete", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("question_mark", "show_help", "Help", show=True),
        Binding("1", "jump_1", show=False),
        Binding("2", "jump_2", show=False),
        Binding("3", "jump_3", show=False),
        Binding("4", "jump_4", show=False),
        Binding("5", "jump_5", show=False),
        Binding("6", "jump_6", show=False),
        Binding("7", "jump_7", show=False),
        Binding("8", "jump_8", show=False),
        Binding("9", "jump_9", show=False),
    ]

    focused_session_index: reactive[int] = reactive(0)
    split_mode: reactive[bool] = reactive(False)
    split_session_index: reactive[int] = reactive(1)

    def __init__(self, config: ZpilotConfig | None = None):
        super().__init__()
        self.config = config or load_config()
        self.event_bus = EventBus(self.config.events_file)
        self._session_data: list[dict] = []
        self._zellij_available: bool | None = None

    # -- Compose -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with FocusView(id="focus-area"):
            yield Static("", id="focus-header")
            with Horizontal(id="focus-content"):
                yield RichLog(id="focus-left", classes="focus-log", wrap=True,
                              markup=True, highlight=True)
        with DockBar(id="dock"):
            yield Static("", id="dock-count")
        yield Footer()

    # -- Lifecycle -----------------------------------------------------------

    def on_mount(self) -> None:
        self.title = "zpilot"
        self.sub_title = "Focus + Dock + Exposé"
        self.set_interval(3.0, self._poll_sessions)
        self.call_later(self._poll_sessions)

    # -- Data polling --------------------------------------------------------

    @work(exclusive=True, group="poll")
    async def _poll_sessions(self) -> None:
        """Poll Zellij for session status."""
        try:
            from .. import zellij
            from ..detector import PaneDetector

            if self._zellij_available is None:
                self._zellij_available = await zellij.is_available()

            if not self._zellij_available:
                self._show_welcome("❌ Zellij not found in PATH.\n"
                                   "Install Zellij and try again.")
                return

            sessions = await zellij.list_sessions()
            detector = PaneDetector(self.config)
            data: list[dict] = []
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
                        "content": content,
                    })
                except Exception:
                    data.append({
                        "name": s.name,
                        "state": "unknown",
                        "idle": 0,
                        "last": "",
                        "content": "",
                    })

            data.sort(key=_sort_key)
            prev_states = {s["name"]: s["state"] for s in self._session_data}
            self._session_data = data

            # Clamp indices
            if data:
                if self.focused_session_index >= len(data):
                    self.focused_session_index = len(data) - 1
                if self.split_session_index >= len(data):
                    self.split_session_index = min(1, len(data) - 1)
            else:
                self.focused_session_index = 0
                self.split_session_index = 0

            self._rebuild_dock(prev_states)
            self._update_focus()

        except Exception as e:
            self._show_welcome(f"Error polling sessions: {e}")

    # -- UI updates ----------------------------------------------------------

    def _show_welcome(self, message: str = "") -> None:
        """Show a welcome/status message in the focus area."""
        text = message or (
            "No sessions found.\n\n"
            "Press [bold]n[/bold] to create one, or "
            "press [bold]?[/bold] for help."
        )
        try:
            header = self.query_one("#focus-header", Static)
            header.update("  zpilot — no sessions")
            log = self.query_one("#focus-left", RichLog)
            log.clear()
            log.write(text)
        except NoMatches:
            pass

    def _rebuild_dock(self, prev_states: dict[str, str] | None = None) -> None:
        """Rebuild dock pills from session data."""
        try:
            dock = self.query_one("#dock", DockBar)
        except NoMatches:
            return

        # Remove old pills (keep dock-count)
        for pill in self.query(".pill"):
            pill.remove()

        for i, s in enumerate(self._session_data):
            pill = SessionPill(
                s["name"], s["state"],
                id=f"pill-{i}",
                classes="pill",
            )
            dock.mount(pill, before=self.query_one("#dock-count", Static))

            # Flash if state changed
            if prev_states and s["name"] in prev_states:
                old = prev_states[s["name"]]
                if old != s["state"]:
                    pill.styles.animate("opacity", 0.3, duration=0.15,
                                        final_value=1.0)

        self._highlight_dock()

        # Update count
        total = len(self._session_data)
        focused = self.focused_session_index + 1 if total else 0
        try:
            self.query_one("#dock-count", Static).update(
                f" {focused}/{total} sessions"
            )
        except NoMatches:
            pass

    def _highlight_dock(self) -> None:
        """Highlight the selected pill in the dock."""
        for i in range(len(self._session_data)):
            try:
                pill = self.query_one(f"#pill-{i}", SessionPill)
                if i == self.focused_session_index:
                    pill.add_class("pill-selected")
                else:
                    pill.remove_class("pill-selected")
            except NoMatches:
                pass

    def _update_focus(self) -> None:
        """Update the focus area content for the current session(s)."""
        if not self._session_data:
            self._show_welcome()
            return

        idx = self.focused_session_index
        s = self._session_data[idx]
        icon = STATE_ICONS.get(s["state"], "❓")
        idle_str = SessionCard._format_idle(s["idle"])

        try:
            header = self.query_one("#focus-header", Static)
            header.update(
                f"  {icon} {s['name']}  [{s['state']}]  idle: {idle_str}"
            )
        except NoMatches:
            pass

        self._write_content("#focus-left", s.get("content", ""))

        # Handle split mode
        if self.split_mode and len(self._session_data) > 1:
            self._ensure_split_panel()
            sidx = self.split_session_index
            if sidx >= len(self._session_data):
                sidx = 0
            s2 = self._session_data[sidx]
            icon2 = STATE_ICONS.get(s2["state"], "❓")
            idle2 = SessionCard._format_idle(s2["idle"])
            try:
                self.query_one("#split-header", Static).update(
                    f"  {icon2} {s2['name']}  [{s2['state']}]  idle: {idle2}"
                )
            except NoMatches:
                pass
            self._write_content("#focus-right", s2.get("content", ""))
        else:
            self._remove_split_panel()

        self._highlight_dock()

        # Update dock counter
        total = len(self._session_data)
        focused = idx + 1
        try:
            self.query_one("#dock-count", Static).update(
                f" {focused}/{total} sessions"
            )
        except NoMatches:
            pass

    def _write_content(self, log_id: str, content: str) -> None:
        """Write content to a RichLog widget."""
        try:
            log = self.query_one(log_id, RichLog)
            log.clear()
            if content:
                for line in content.splitlines():
                    log.write(line)
            else:
                log.write("[dim]No output yet[/dim]")
        except NoMatches:
            pass

    def _ensure_split_panel(self) -> None:
        """Add the right split panel if not already present."""
        try:
            self.query_one("#focus-right", RichLog)
        except NoMatches:
            container = self.query_one("#focus-content", Horizontal)
            right = Vertical(id="focus-right-wrapper")
            header = Static("", id="split-header")
            log = RichLog(id="focus-right", classes="focus-log", wrap=True,
                          markup=True, highlight=True)
            container.mount(right)
            right.mount(header)
            right.mount(log)

    def _remove_split_panel(self) -> None:
        """Remove the right split panel if present."""
        try:
            wrapper = self.query_one("#focus-right-wrapper")
            wrapper.remove()
        except NoMatches:
            pass

    # -- Actions (keybindings) -----------------------------------------------

    def action_next_session(self) -> None:
        if self._session_data:
            self.focused_session_index = (
                (self.focused_session_index + 1) % len(self._session_data)
            )
            self._update_focus()

    def action_prev_session(self) -> None:
        if self._session_data:
            self.focused_session_index = (
                (self.focused_session_index - 1) % len(self._session_data)
            )
            self._update_focus()

    def _jump_to(self, n: int) -> None:
        if 0 <= n < len(self._session_data):
            self.focused_session_index = n
            self._update_focus()

    def action_jump_1(self) -> None: self._jump_to(0)
    def action_jump_2(self) -> None: self._jump_to(1)
    def action_jump_3(self) -> None: self._jump_to(2)
    def action_jump_4(self) -> None: self._jump_to(3)
    def action_jump_5(self) -> None: self._jump_to(4)
    def action_jump_6(self) -> None: self._jump_to(5)
    def action_jump_7(self) -> None: self._jump_to(6)
    def action_jump_8(self) -> None: self._jump_to(7)
    def action_jump_9(self) -> None: self._jump_to(8)

    def action_toggle_split(self) -> None:
        if len(self._session_data) < 2:
            self.notify("Need at least 2 sessions to split")
            return
        self.split_mode = not self.split_mode
        if self.split_mode:
            # Pick a different session for the right panel
            self.split_session_index = (
                (self.focused_session_index + 1) % len(self._session_data)
            )
        self._update_focus()
        self.notify("Split ON" if self.split_mode else "Split OFF")

    def action_toggle_expose(self) -> None:
        if not self._session_data:
            self.notify("No sessions to show")
            return
        self.push_screen(
            ExposeScreen(self._session_data, self.focused_session_index),
            callback=self._on_expose_result,
        )

    def _on_expose_result(self, result: int | None) -> None:
        if result is not None and 0 <= result < len(self._session_data):
            self.focused_session_index = result
            self._update_focus()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_new_session(self) -> None:
        self.push_screen(NewSessionScreen(), callback=self._on_new_session)

    @work(exclusive=True, group="session-mgmt")
    async def _on_new_session(self, name: str | None) -> None:
        if not name:
            return
        try:
            from .. import zellij
            await zellij.new_session(name)
            self.notify(f"Created session: {name}")
            await self._do_refresh()
        except Exception as e:
            self.notify(f"Error creating session: {e}", severity="error")

    def action_delete_session(self) -> None:
        if not self._session_data:
            self.notify("No session to delete")
            return
        s = self._session_data[self.focused_session_index]
        self.push_screen(
            ConfirmDeleteScreen(s["name"]),
            callback=self._on_delete_confirm,
        )

    @work(exclusive=True, group="session-mgmt")
    async def _on_delete_confirm(self, confirmed: bool) -> None:
        if not confirmed or not self._session_data:
            return
        s = self._session_data[self.focused_session_index]
        try:
            from .. import zellij
            await zellij.kill_session(s["name"])
            self.notify(f"Killed session: {s['name']}")
            await self._do_refresh()
        except Exception as e:
            self.notify(f"Error killing session: {e}", severity="error")

    async def action_refresh(self) -> None:
        await self._do_refresh()
        self.notify("Refreshed")

    async def _do_refresh(self) -> None:
        self._zellij_available = None  # re-check availability
        await self._poll_sessions()

    # -- Pill click handler --------------------------------------------------

    def on_click(self, event) -> None:
        """Handle click on a dock pill to switch focus."""
        widget = event.widget if hasattr(event, "widget") else None
        if isinstance(widget, SessionPill):
            for i, s in enumerate(self._session_data):
                if s["name"] == widget.session_name:
                    self.focused_session_index = i
                    self._update_focus()
                    break
