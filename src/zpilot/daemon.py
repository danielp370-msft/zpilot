"""Background daemon that monitors Zellij sessions and emits events."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from . import zellij
from .config import load_config
from .detector import PaneDetector
from .events import EventBus
from .models import Event, PaneState, ZpilotConfig
from .notifications import NotificationAdapter, create_adapter

log = logging.getLogger("zpilot.daemon")

# PID file location
PID_DIR = Path("/tmp/zpilot")
PID_FILE = PID_DIR / "zpilot.pid"


def write_pid_file() -> None:
    """Write current process PID to the PID file."""
    PID_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def read_pid_file() -> int | None:
    """Read PID from the PID file. Returns None if not found."""
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def remove_pid_file() -> None:
    """Remove the PID file if it exists."""
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def is_daemon_running() -> int | None:
    """Check if a daemon is running. Returns PID if alive, None otherwise.

    Also cleans up stale PID files (process no longer exists).
    """
    pid = read_pid_file()
    if pid is None:
        return None
    try:
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return pid
    except (ProcessLookupError, PermissionError):
        # Stale PID file — process is gone
        remove_pid_file()
        return None


def generate_systemd_unit(python_path: str | None = None) -> str:
    """Generate a systemd user unit file for zpilot daemon."""
    import shutil
    import sys

    if python_path is None:
        python_path = shutil.which("zpilot") or sys.executable

    # If zpilot CLI is on PATH, use it directly; otherwise use python -m
    zpilot_bin = shutil.which("zpilot")
    if zpilot_bin:
        exec_start = f"{zpilot_bin} daemon"
    else:
        exec_start = f"{python_path} -m zpilot.cli daemon"

    return f"""\
[Unit]
Description=zpilot terminal orchestration daemon
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def install_systemd_unit() -> Path:
    """Install the systemd user unit file. Returns the path."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "zpilot.service"
    unit_path.write_text(generate_systemd_unit())
    return unit_path


def uninstall_systemd_unit() -> bool:
    """Remove the systemd user unit file. Returns True if removed."""
    unit_path = Path.home() / ".config" / "systemd" / "user" / "zpilot.service"
    if unit_path.exists():
        unit_path.unlink()
        return True
    return False


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
        # Check for already-running daemon
        existing_pid = is_daemon_running()
        if existing_pid and existing_pid != os.getpid():
            log.error(f"Daemon already running (PID {existing_pid})")
            return

        write_pid_file()
        self._running = True
        log.info(
            f"zpilot daemon started (pid={os.getpid()}, poll={self.config.poll_interval}s, "
            f"idle_threshold={self.config.idle_threshold}s)"
        )

        self.event_bus.emit(Event(
            event_type="info",
            new_state="started",
            details="zpilot daemon started",
        ))

        try:
            while self._running:
                await self.poll_once()
                await asyncio.sleep(self.config.poll_interval)
        finally:
            remove_pid_file()

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
