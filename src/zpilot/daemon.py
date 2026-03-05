"""Background daemon that monitors Zellij sessions and emits events."""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from . import zellij
from .config import load_config
from .detector import PaneDetector
from .events import EventBus
from .models import Event, PaneState, ZpilotConfig
from .notifications import NotificationAdapter, create_adapter

log = logging.getLogger("zpilot.daemon")


class Daemon:
    """Watches Zellij sessions, detects state changes, emits events."""

    def __init__(self, config: ZpilotConfig | None = None):
        self.config = config or load_config()
        self.detector = PaneDetector(self.config)
        self.event_bus = EventBus(self.config.events_file)
        self.notifier: NotificationAdapter = create_adapter(self.config)
        self._running = False
        # Track previous states to detect changes
        self._prev_states: dict[str, PaneState] = {}

    async def poll_once(self) -> dict[str, PaneState]:
        """Poll all sessions once. Returns current states."""
        states: dict[str, PaneState] = {}

        try:
            sessions = await zellij.list_sessions()
        except Exception as e:
            log.error(f"Failed to list sessions: {e}")
            return states

        for session in sessions:
            try:
                content = await zellij.dump_pane(session=session.name)
                pane_name = "focused"  # TODO: iterate panes individually
                key = f"{session.name}:{pane_name}"

                state = self.detector.detect(
                    session=session.name,
                    pane=pane_name,
                    content=content,
                )
                states[key] = state

                # Check for state change
                prev = self._prev_states.get(key, PaneState.UNKNOWN)
                if state != prev:
                    event = Event(
                        event_type="state_change",
                        session=session.name,
                        pane=pane_name,
                        old_state=prev.value,
                        new_state=state.value,
                        details=f"idle={self.detector.get_idle_seconds(session.name, pane_name):.0f}s",
                    )
                    self.event_bus.emit(event)
                    log.info(
                        f"{session.name}:{pane_name} {prev.value} → {state.value}"
                    )

                    # Send notification if needed
                    if (
                        self.config.notify_enabled
                        and state.value in self.config.notify_on
                    ):
                        await self._notify(session.name, pane_name, state)

            except Exception as e:
                log.warning(f"Error polling {session.name}: {e}")

        self._prev_states = states
        return states

    async def _notify(
        self, session: str, pane: str, state: PaneState
    ) -> None:
        """Send a notification for a state change."""
        titles = {
            PaneState.WAITING: "⏳ Needs Input",
            PaneState.ERROR: "❌ Error Detected",
            PaneState.EXITED: "🏁 Session Exited",
        }
        title = titles.get(state, f"State: {state.value}")
        body = f"Session '{session}' pane '{pane}' is now {state.value}"
        try:
            await self.notifier.send(f"zpilot: {title}", body, priority="high")
        except Exception as e:
            log.warning(f"Notification failed: {e}")

    async def run(self) -> None:
        """Main daemon loop."""
        self._running = True
        log.info(
            f"zpilot daemon started (poll={self.config.poll_interval}s, "
            f"idle_threshold={self.config.idle_threshold}s)"
        )

        self.event_bus.emit(Event(
            event_type="info",
            new_state="started",
            details="zpilot daemon started",
        ))

        while self._running:
            await self.poll_once()
            await asyncio.sleep(self.config.poll_interval)

    def stop(self) -> None:
        """Stop the daemon loop."""
        self._running = False
        self.event_bus.emit(Event(
            event_type="info",
            new_state="stopped",
            details="zpilot daemon stopped",
        ))


async def run_daemon(config: ZpilotConfig | None = None) -> None:
    """Entry point for running the daemon."""
    daemon = Daemon(config)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.stop)

    await daemon.run()
