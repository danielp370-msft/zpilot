"""In-process CLI tests using Click's CliRunner.

Tests zpilot CLI commands by mocking async dependencies
so no real Zellij sessions are needed.
"""

import sys

sys.path.insert(0, "src")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from zpilot.cli import main
from zpilot.models import PaneState, Session, ZpilotConfig


@pytest.fixture
def runner():
    return CliRunner(mix_stderr=False)


@pytest.fixture
def mock_config():
    cfg = ZpilotConfig(poll_interval=2.0, idle_threshold=10.0)
    return cfg


# ── main (no subcommand → dashboard) ────────────────────────────────

class TestMainCommand:
    def test_main_help(self, runner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "zpilot" in result.output

    def test_main_no_args_invokes_dashboard(self, runner):
        with patch("zpilot.cli.ensure_config"), \
             patch("zpilot.cli.dashboard") as mock_dash:
            mock_dash.invoke = MagicMock()
            # dashboard is a click command; patch the underlying function
            with patch("zpilot.tui.dashboard.ZpilotApp") as MockApp:
                MockApp.return_value.run = MagicMock()
                result = runner.invoke(main, [])
                # Either dashboard was invoked or the TUI app was created
                # Accept both since main() -> ctx.invoke(dashboard)
                assert result.exit_code == 0 or MockApp.called


# ── serve ────────────────────────────────────────────────────────────

class TestServeCommand:
    def test_serve_help(self, runner):
        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0
        assert "MCP" in result.output

    def test_serve_invokes_mcp(self, runner):
        with patch("zpilot.mcp_server.serve", new_callable=AsyncMock) as mock_serve:
            result = runner.invoke(main, ["serve"])
            assert result.exit_code == 0
            mock_serve.assert_awaited_once()


# ── daemon ───────────────────────────────────────────────────────────

class TestDaemonCommand:
    def test_daemon_help(self, runner):
        result = runner.invoke(main, ["daemon", "--help"])
        assert result.exit_code == 0

    def test_daemon_with_options(self, runner):
        with patch("zpilot.cli.load_config") as mock_lc, \
             patch("zpilot.daemon.is_daemon_running", return_value=None), \
             patch("zpilot.daemon.run_daemon", new_callable=AsyncMock) as mock_rd:
            mock_lc.return_value = ZpilotConfig()
            result = runner.invoke(main, [
                "daemon", "start", "--poll-interval", "1.5", "--idle-threshold", "20"
            ])
            assert result.exit_code == 0
            mock_rd.assert_awaited_once()
            cfg = mock_rd.call_args[0][0]
            assert cfg.poll_interval == 1.5
            assert cfg.idle_threshold == 20.0


# ── status ───────────────────────────────────────────────────────────

class TestStatusCommand:
    def test_status_no_sessions(self, runner):
        with patch("zpilot.cli.load_config", return_value=ZpilotConfig()), \
             patch("zpilot.zellij.is_available", new_callable=AsyncMock, return_value=True), \
             patch("zpilot.zellij.list_sessions", new_callable=AsyncMock, return_value=[]):
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "No Zellij sessions" in result.output

    def test_status_with_sessions(self, runner):
        sessions = [
            Session(name="test-session", is_current=True),
        ]
        with patch("zpilot.cli.load_config", return_value=ZpilotConfig()), \
             patch("zpilot.zellij.is_available", new_callable=AsyncMock, return_value=True), \
             patch("zpilot.zellij.list_sessions", new_callable=AsyncMock, return_value=sessions), \
             patch("zpilot.zellij.dump_pane", new_callable=AsyncMock, return_value="$ "):
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "test-session" in result.output
            assert "(current)" in result.output

    def test_status_zellij_unavailable(self, runner):
        with patch("zpilot.cli.load_config", return_value=ZpilotConfig()), \
             patch("zpilot.zellij.is_available", new_callable=AsyncMock, return_value=False):
            result = runner.invoke(main, ["status"])
            assert result.exit_code != 0 or "not installed" in result.stderr


# ── new ──────────────────────────────────────────────────────────────

class TestNewCommand:
    def test_new_creates_session(self, runner):
        with patch("zpilot.zellij.is_available", new_callable=AsyncMock, return_value=True), \
             patch("zpilot.zellij.new_session", new_callable=AsyncMock) as mock_ns:
            result = runner.invoke(main, ["new", "my-session"])
            assert result.exit_code == 0
            assert "my-session" in result.output
            mock_ns.assert_awaited_once_with("my-session")

    def test_new_with_command(self, runner):
        with patch("zpilot.zellij.is_available", new_callable=AsyncMock, return_value=True), \
             patch("zpilot.zellij.new_session", new_callable=AsyncMock), \
             patch("zpilot.zellij.run_command_in_pane", new_callable=AsyncMock) as mock_run, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = runner.invoke(main, ["new", "my-session", "make build"])
            assert result.exit_code == 0
            mock_run.assert_awaited_once()


# ── config ───────────────────────────────────────────────────────────

class TestConfigCommand:
    def test_config_shows_values(self, runner):
        with patch("zpilot.cli.ensure_config"), \
             patch("zpilot.cli.load_config", return_value=ZpilotConfig()):
            result = runner.invoke(main, ["config"])
            assert result.exit_code == 0
            assert "poll_interval" in result.output
            assert "idle_threshold" in result.output


# ── nodes ────────────────────────────────────────────────────────────

class TestNodesCommand:
    def test_nodes_list(self, runner):
        from zpilot.nodes import Node
        mock_nodes = [
            Node(name="local", transport_type="local"),
            Node(name="remote1", transport_type="ssh", host="10.0.0.1"),
        ]
        with patch("zpilot.nodes.load_nodes", return_value=mock_nodes):
            result = runner.invoke(main, ["nodes"])
            assert result.exit_code == 0
            assert "local" in result.output
            assert "remote1" in result.output
            assert "2" in result.output  # "Nodes (2):"


# ── serve-http ───────────────────────────────────────────────────────

class TestServeHttpCommand:
    def test_serve_http_help(self, runner):
        result = runner.invoke(main, ["serve-http", "--help"])
        assert result.exit_code == 0
        assert "HTTP" in result.output

    def test_serve_http_with_options(self, runner):
        with patch("zpilot.cli.load_config", return_value=ZpilotConfig()), \
             patch("zpilot.mcp_http.serve_http", new_callable=AsyncMock) as mock_sh:
            result = runner.invoke(main, [
                "serve-http", "--host", "0.0.0.0", "--port", "9999", "--token", "abc"
            ])
            assert result.exit_code == 0
            mock_sh.assert_awaited_once()
            cfg = mock_sh.call_args[0][0]
            assert cfg.http_host == "0.0.0.0"
            assert cfg.http_port == 9999
            assert cfg.http_token == "abc"


# ── token-gen ────────────────────────────────────────────────────────

class TestTokenGenCommand:
    def test_token_gen_outputs_token(self, runner):
        result = runner.invoke(main, ["token-gen"])
        assert result.exit_code == 0
        token = result.output.strip()
        assert len(token) >= 20  # urlsafe base64 of 32 bytes


# ── up / down ────────────────────────────────────────────────────────

class TestUpDownCommands:
    def test_up_starts_background(self, runner, tmp_path):
        pid_dir = tmp_path / "zpilot"
        pid_dir.mkdir()
        mock_proc = MagicMock()
        mock_proc.pid = 12345

        with patch("zpilot.cli.PID_DIR", pid_dir), \
             patch("zpilot.cli.ensure_config"), \
             patch("zpilot.daemon.is_daemon_running", return_value=None), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("builtins.open", MagicMock()):
            result = runner.invoke(main, ["up", "--no-ssl"])
            assert result.exit_code == 0
            assert "zpilot is up" in result.output
            # Should have called Popen twice (daemon + web)
            assert mock_popen.call_count == 2

    def test_up_already_running(self, runner, tmp_path):
        import os
        pid_dir = tmp_path / "zpilot"
        pid_dir.mkdir()
        pid_file = pid_dir / "web.pid"
        # Use current process PID so os.kill(pid, 0) succeeds
        pid_file.write_text(str(os.getpid()))

        with patch("zpilot.cli.PID_DIR", pid_dir):
            result = runner.invoke(main, ["up"])
            assert result.exit_code == 0
            assert "already running" in result.output

    def test_down_no_pidfile(self, runner, tmp_path):
        pid_dir = tmp_path / "zpilot"
        pid_dir.mkdir()
        with patch("zpilot.cli.PID_DIR", pid_dir):
            result = runner.invoke(main, ["down"])
            assert result.exit_code == 0
            assert "not running" in result.output

    def test_down_stops_process(self, runner, tmp_path):
        pid_dir = tmp_path / "zpilot"
        pid_dir.mkdir()
        # Write both pid files
        (pid_dir / "web.pid").write_text("99999")
        (pid_dir / "zpilot.pid").write_text("99998")

        with patch("zpilot.cli.PID_DIR", pid_dir), \
             patch("os.kill") as mock_kill:
            result = runner.invoke(main, ["down"])
            assert result.exit_code == 0
            assert "stopped" in result.output

    def test_down_stale_pid(self, runner, tmp_path):
        pid_dir = tmp_path / "zpilot"
        pid_dir.mkdir()
        pid_file = pid_dir / "web.pid"
        pid_file.write_text("99999")

        import os
        with patch("zpilot.cli.PID_DIR", pid_dir), \
             patch("os.kill", side_effect=ProcessLookupError):
            result = runner.invoke(main, ["down"])
            assert result.exit_code == 0
            # No stopped message (already gone) but cleaned up
            assert not pid_file.exists()


# ── fleet ────────────────────────────────────────────────────────────

class TestFleetCommand:
    def test_fleet_displays_status(self, runner):
        from zpilot.models import FleetStatus, NodeHealth, NodeState
        fleet = FleetStatus(nodes=[
            NodeHealth(name="local", state=NodeState.ONLINE),
        ])
        with patch("zpilot.cli.load_config", return_value=ZpilotConfig()), \
             patch("zpilot.nodes.load_nodes", return_value=[]), \
             patch("zpilot.monitor.Monitor.poll_all", new_callable=AsyncMock, return_value=fleet), \
             patch("zpilot.monitor.Monitor.stuck_sessions", return_value=[]):
            result = runner.invoke(main, ["fleet"])
            assert result.exit_code == 0
            assert "local" in result.output


# ── ping ─────────────────────────────────────────────────────────────

class TestPingCommand:
    def test_ping_reachable(self, runner):
        from zpilot.nodes import Node
        mock_node = Node(name="local", transport_type="local")
        mock_transport = AsyncMock()
        mock_transport.is_alive = AsyncMock(return_value=True)
        mock_node._transport = mock_transport

        with patch("zpilot.nodes.load_nodes", return_value=[mock_node]):
            result = runner.invoke(main, ["ping", "local"])
            assert result.exit_code == 0
            assert "reachable" in result.output

    def test_ping_unreachable(self, runner):
        from zpilot.nodes import Node
        mock_node = Node(name="remote1", transport_type="ssh", host="10.0.0.1")
        mock_transport = AsyncMock()
        mock_transport.is_alive = AsyncMock(return_value=False)
        mock_node._transport = mock_transport

        with patch("zpilot.nodes.load_nodes", return_value=[mock_node]):
            result = runner.invoke(main, ["ping", "remote1"])
            assert result.exit_code == 0
            assert "unreachable" in result.output


# ── web ──────────────────────────────────────────────────────────────

class TestWebCommand:
    def test_web_help(self, runner):
        result = runner.invoke(main, ["web", "--help"])
        assert result.exit_code == 0
        assert "web" in result.output.lower()

    def test_web_invokes_run_web(self, runner):
        with patch("zpilot.cli.ensure_config"), \
             patch("zpilot.web.app.run_web") as mock_rw:
            result = runner.invoke(main, ["web", "--no-ssl", "--port", "9000"])
            assert result.exit_code == 0
            mock_rw.assert_called_once_with(host="0.0.0.0", port=9000, ssl=False)
