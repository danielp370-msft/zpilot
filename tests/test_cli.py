"""Tests for zpilot CLI commands."""

import subprocess
import sys
import pytest

sys.path.insert(0, "src")

PYTHON = sys.executable


class TestCliHelp:
    """Test that CLI commands have proper help text."""

    def test_main_help(self):
        r = subprocess.run([PYTHON, "-m", "zpilot.cli", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "zpilot" in r.stdout.lower()
        assert "Mission control" in r.stdout or "mission control" in r.stdout.lower()

    def test_serve_help(self):
        r = subprocess.run([PYTHON, "-m", "zpilot.cli", "serve", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "MCP" in r.stdout or "mcp" in r.stdout.lower()

    def test_daemon_help(self):
        r = subprocess.run([PYTHON, "-m", "zpilot.cli", "daemon", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "daemon" in r.stdout.lower() or "watcher" in r.stdout.lower()

    def test_status_help(self):
        r = subprocess.run([PYTHON, "-m", "zpilot.cli", "status", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "status" in r.stdout.lower()

    def test_new_help(self):
        r = subprocess.run([PYTHON, "-m", "zpilot.cli", "new", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "session" in r.stdout.lower() or "NAME" in r.stdout

    def test_web_help(self):
        r = subprocess.run([PYTHON, "-m", "zpilot.cli", "web", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "web" in r.stdout.lower()

    def test_config_help(self):
        r = subprocess.run([PYTHON, "-m", "zpilot.cli", "config", "--help"], capture_output=True, text=True)
        assert r.returncode == 0


class TestCliStatus:
    """Test `zpilot status` against live sessions."""

    def test_status_runs(self):
        r = subprocess.run(
            [PYTHON, "-m", "zpilot.cli", "status"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0
        # Should either show sessions or say none found
        output = r.stdout + r.stderr
        assert "demo-build" in output or "No Zellij sessions" in output or "❌" in output

    def test_status_shows_state(self):
        r = subprocess.run(
            [PYTHON, "-m", "zpilot.cli", "status"],
            capture_output=True, text=True, timeout=30,
        )
        if "demo-build" in r.stdout:
            # Should have state indicator
            assert any(icon in r.stdout for icon in ["⏳", "✅", "🔔", "❌", "🏁", "❓"])


class TestCliConfig:
    """Test `zpilot config` output."""

    def test_config_shows_values(self):
        r = subprocess.run(
            [PYTHON, "-m", "zpilot.cli", "config"],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0
        assert "poll_interval" in r.stdout
        assert "idle_threshold" in r.stdout
