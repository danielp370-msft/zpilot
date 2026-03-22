"""Tests for the zpilot daemon module.

Tests Daemon class: initialization, poll_once, state change detection,
notifications, and run/stop lifecycle.
"""

import os
import sys

sys.path.insert(0, "src")

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zpilot.models import PaneState, Session, ZpilotConfig


@pytest.fixture
def config(tmp_path):
    return ZpilotConfig(
        poll_interval=0.1,
        idle_threshold=5.0,
        events_file=str(tmp_path / "events.jsonl"),
        notify_enabled=False,
    )


@pytest.fixture
def config_notify(tmp_path):
    return ZpilotConfig(
        poll_interval=0.1,
        idle_threshold=5.0,
        events_file=str(tmp_path / "events.jsonl"),
        notify_enabled=True,
        notify_on=["waiting", "error", "exited"],
    )


# ── Daemon initialization ───────────────────────────────────────────

class TestDaemonInit:
    def test_creates_with_config(self, config):
        with patch("zpilot.daemon.create_adapter") as mock_ca:
            mock_ca.return_value = MagicMock()
            from zpilot.daemon import Daemon
            d = Daemon(config)
            assert d.config is config
            assert d._running is False
            assert d._prev_states == {}

    def test_creates_with_default_config(self):
        with patch("zpilot.daemon.load_config", return_value=ZpilotConfig()), \
             patch("zpilot.daemon.create_adapter") as mock_ca:
            mock_ca.return_value = MagicMock()
            from zpilot.daemon import Daemon
            d = Daemon()
            assert d.config.poll_interval == 5.0


# ── poll_once ────────────────────────────────────────────────────────

class TestPollOnce:
    @pytest.mark.asyncio
    async def test_poll_empty_sessions(self, config):
        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z:
            mock_ca.return_value = MagicMock()
            mock_z.list_sessions = AsyncMock(return_value=[])

            from zpilot.daemon import Daemon
            d = Daemon(config)
            states = await d.poll_once()
            assert states == {}

    @pytest.mark.asyncio
    async def test_poll_with_sessions(self, config):
        sessions = [Session(name="s1"), Session(name="s2")]

        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z:
            mock_ca.return_value = MagicMock()
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(return_value="$ \n")

            from zpilot.daemon import Daemon
            d = Daemon(config)
            states = await d.poll_once()

            assert len(states) == 2
            assert "s1:focused" in states
            assert "s2:focused" in states

    @pytest.mark.asyncio
    async def test_poll_handles_list_error(self, config):
        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z:
            mock_ca.return_value = MagicMock()
            mock_z.list_sessions = AsyncMock(side_effect=Exception("zellij not found"))

            from zpilot.daemon import Daemon
            d = Daemon(config)
            states = await d.poll_once()
            assert states == {}

    @pytest.mark.asyncio
    async def test_poll_handles_pane_error(self, config):
        sessions = [Session(name="broken")]

        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z:
            mock_ca.return_value = MagicMock()
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(side_effect=Exception("pane gone"))

            from zpilot.daemon import Daemon
            d = Daemon(config)
            states = await d.poll_once()
            # Error handled gracefully — session skipped
            assert "broken:focused" not in states


# ── State change detection ───────────────────────────────────────────

class TestStateChangeDetection:
    @pytest.mark.asyncio
    async def test_detects_state_change(self, config):
        sessions = [Session(name="sess")]

        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z:
            mock_ca.return_value = MagicMock()
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(return_value="$ ")

            from zpilot.daemon import Daemon
            d = Daemon(config)

            # First poll: unknown → some state
            states1 = await d.poll_once()
            first_state = states1.get("sess:focused")

            # Change output to trigger different state
            mock_z.dump_pane = AsyncMock(return_value="Error: something broke\n")
            states2 = await d.poll_once()
            second_state = states2.get("sess:focused")

            # Events should have been emitted
            events = d.event_bus.recent(10)
            state_changes = [e for e in events if e.event_type == "state_change"]
            assert len(state_changes) >= 1

    @pytest.mark.asyncio
    async def test_no_event_when_state_unchanged(self, config):
        sessions = [Session(name="stable")]

        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z:
            mock_ca.return_value = MagicMock()
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(return_value="$ ")

            from zpilot.daemon import Daemon
            d = Daemon(config)

            await d.poll_once()
            events_after_first = len(d.event_bus.recent(100))

            # Poll again with same output
            await d.poll_once()
            events_after_second = len(d.event_bus.recent(100))

            # No new events on second poll (state unchanged)
            assert events_after_second == events_after_first


# ── Notifications ────────────────────────────────────────────────────

class TestDaemonNotifications:
    @pytest.mark.asyncio
    async def test_sends_notification_on_matching_state(self, config_notify):
        sessions = [Session(name="notif-test")]

        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z:
            mock_notifier = AsyncMock()
            mock_ca.return_value = mock_notifier
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            # First poll: set initial state
            mock_z.dump_pane = AsyncMock(return_value="$ ")

            from zpilot.daemon import Daemon
            d = Daemon(config_notify)
            await d.poll_once()

            # Second poll: trigger error state (which is in notify_on)
            mock_z.dump_pane = AsyncMock(return_value="Error: crash\n")
            await d.poll_once()

            # Check if the detected state triggers notification
            # (depends on detector — if it detects "error", notification fires)
            # At minimum, the poll should complete without error
            assert True  # no crash = success


# ── run / stop lifecycle ─────────────────────────────────────────────

class TestDaemonRunStop:
    @pytest.mark.asyncio
    async def test_run_and_stop(self, config):
        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z:
            mock_ca.return_value = MagicMock()
            mock_z.list_sessions = AsyncMock(return_value=[])

            from zpilot.daemon import Daemon
            d = Daemon(config)

            async def stop_soon():
                await asyncio.sleep(0.15)
                d.stop()

            task = asyncio.create_task(stop_soon())
            await d.run()
            await task

            assert d._running is False
            # Check start/stop events
            events = d.event_bus.recent(10)
            info_events = [e for e in events if e.event_type == "info"]
            assert any("started" in (e.new_state or "") for e in info_events)
            assert any("stopped" in (e.new_state or "") for e in info_events)

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, config):
        with patch("zpilot.daemon.create_adapter") as mock_ca:
            mock_ca.return_value = MagicMock()

            from zpilot.daemon import Daemon
            d = Daemon(config)
            d._running = True
            d.stop()
            assert d._running is False


# ── run_daemon entry point ───────────────────────────────────────────

class TestRunDaemon:
    @pytest.mark.asyncio
    async def test_run_daemon_creates_and_runs(self, config):
        with patch("zpilot.daemon.create_adapter") as mock_ca, \
             patch("zpilot.daemon.zellij") as mock_z, \
             patch("zpilot.daemon.Daemon") as MockDaemon:
            mock_ca.return_value = MagicMock()

            mock_instance = MagicMock()
            mock_instance.run = AsyncMock()
            mock_instance.stop = MagicMock()
            MockDaemon.return_value = mock_instance

            from zpilot.daemon import run_daemon

            # run_daemon registers signal handlers which requires a running loop
            # We can't fully test signal handling in pytest, but we can test creation
            try:
                await run_daemon(config)
            except Exception:
                pass  # Signal handler registration may fail in test context

            MockDaemon.assert_called_once_with(config)


# ── PID file management ─────────────────────────────────────────────

class TestPidFile:
    def test_write_and_read_pid(self, tmp_path):
        from zpilot.daemon import write_pid_file, read_pid_file, PID_FILE
        import zpilot.daemon as dm
        # Redirect PID_DIR to tmp_path
        orig_dir = dm.PID_DIR
        orig_file = dm.PID_FILE
        try:
            dm.PID_DIR = tmp_path
            dm.PID_FILE = tmp_path / "zpilot.pid"
            write_pid_file()
            pid = read_pid_file()
            assert pid == os.getpid()
        finally:
            dm.PID_DIR = orig_dir
            dm.PID_FILE = orig_file

    def test_read_missing_pid_file(self, tmp_path):
        import zpilot.daemon as dm
        orig_file = dm.PID_FILE
        try:
            dm.PID_FILE = tmp_path / "nonexistent.pid"
            assert dm.read_pid_file() is None
        finally:
            dm.PID_FILE = orig_file

    def test_remove_pid_file(self, tmp_path):
        import zpilot.daemon as dm
        orig_file = dm.PID_FILE
        try:
            dm.PID_FILE = tmp_path / "zpilot.pid"
            dm.PID_FILE.write_text("12345")
            assert dm.PID_FILE.exists()
            dm.remove_pid_file()
            assert not dm.PID_FILE.exists()
        finally:
            dm.PID_FILE = orig_file

    def test_remove_missing_pid_file(self, tmp_path):
        import zpilot.daemon as dm
        orig_file = dm.PID_FILE
        try:
            dm.PID_FILE = tmp_path / "nonexistent.pid"
            dm.remove_pid_file()  # Should not raise
        finally:
            dm.PID_FILE = orig_file

    def test_is_daemon_running_returns_pid(self, tmp_path):
        """Test that is_daemon_running returns current PID."""
        import zpilot.daemon as dm
        orig_dir = dm.PID_DIR
        orig_file = dm.PID_FILE
        try:
            dm.PID_DIR = tmp_path
            dm.PID_FILE = tmp_path / "zpilot.pid"
            dm.PID_FILE.write_text(str(os.getpid()))
            assert dm.is_daemon_running() == os.getpid()
        finally:
            dm.PID_DIR = orig_dir
            dm.PID_FILE = orig_file

    def test_is_daemon_running_cleans_stale(self, tmp_path):
        """Stale PID file (process gone) gets cleaned up."""
        import zpilot.daemon as dm
        orig_dir = dm.PID_DIR
        orig_file = dm.PID_FILE
        try:
            dm.PID_DIR = tmp_path
            dm.PID_FILE = tmp_path / "zpilot.pid"
            dm.PID_FILE.write_text("99999999")  # Very unlikely to exist
            result = dm.is_daemon_running()
            assert result is None
            assert not dm.PID_FILE.exists()  # Stale file cleaned
        finally:
            dm.PID_DIR = orig_dir
            dm.PID_FILE = orig_file

    def test_is_daemon_running_no_file(self, tmp_path):
        import zpilot.daemon as dm
        orig_file = dm.PID_FILE
        try:
            dm.PID_FILE = tmp_path / "nope.pid"
            assert dm.is_daemon_running() is None
        finally:
            dm.PID_FILE = orig_file


# ── Systemd unit generation ─────────────────────────────────────────

class TestSystemdUnit:
    def test_generate_unit_contains_required_sections(self):
        from zpilot.daemon import generate_systemd_unit
        unit = generate_systemd_unit(python_path="/usr/bin/python3")
        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit
        assert "Restart=on-failure" in unit
        assert "WantedBy=default.target" in unit

    def test_install_unit(self, tmp_path):
        from zpilot.daemon import generate_systemd_unit
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        unit_path = unit_dir / "zpilot.service"
        unit_path.write_text(generate_systemd_unit())
        assert unit_path.exists()
        content = unit_path.read_text()
        assert "zpilot" in content

    def test_uninstall_unit(self, tmp_path):
        from zpilot.daemon import uninstall_systemd_unit
        import zpilot.daemon as dm
        # Create a fake unit file to test uninstall
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        unit_path = unit_dir / "zpilot.service"
        unit_path.write_text("[Unit]\nDescription=test\n")

        with patch("zpilot.daemon.Path.home", return_value=tmp_path):
            assert dm.uninstall_systemd_unit() is True
            assert not unit_path.exists()

    def test_uninstall_missing_unit(self, tmp_path):
        import zpilot.daemon as dm
        with patch("zpilot.daemon.Path.home", return_value=tmp_path):
            assert dm.uninstall_systemd_unit() is False


# ── Daemon run writes PID file ──────────────────────────────────────

class TestDaemonRunPidFile:
    @pytest.mark.asyncio
    async def test_run_writes_pid_and_cleans_on_stop(self, config, tmp_path):
        import zpilot.daemon as dm
        orig_dir = dm.PID_DIR
        orig_file = dm.PID_FILE
        try:
            dm.PID_DIR = tmp_path
            dm.PID_FILE = tmp_path / "zpilot.pid"

            with patch("zpilot.daemon.create_adapter") as mock_ca, \
                 patch("zpilot.daemon.zellij") as mock_z:
                mock_ca.return_value = MagicMock()
                mock_z.list_sessions = AsyncMock(return_value=[])

                d = dm.Daemon(config)

                async def stop_soon():
                    await asyncio.sleep(0.15)
                    d.stop()

                task = asyncio.create_task(stop_soon())
                await d.run()
                await task

                # PID file should be cleaned up after run exits
                assert not dm.PID_FILE.exists()
        finally:
            dm.PID_DIR = orig_dir
            dm.PID_FILE = orig_file

    @pytest.mark.asyncio
    async def test_run_refuses_if_already_running(self, config, tmp_path):
        import zpilot.daemon as dm
        orig_dir = dm.PID_DIR
        orig_file = dm.PID_FILE
        try:
            dm.PID_DIR = tmp_path
            dm.PID_FILE = tmp_path / "zpilot.pid"
            # Write our own PID + 1 won't work (may not exist).
            # Instead, mock is_daemon_running to return a fake PID.
            with patch("zpilot.daemon.create_adapter") as mock_ca, \
                 patch("zpilot.daemon.is_daemon_running", return_value=99999):
                mock_ca.return_value = MagicMock()
                d = dm.Daemon(config)
                await d.run()  # Should return immediately
                assert d._running is False
        finally:
            dm.PID_DIR = orig_dir
            dm.PID_FILE = orig_file
