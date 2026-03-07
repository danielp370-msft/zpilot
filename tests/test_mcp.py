"""Tests for zpilot MCP server tool definitions and dispatch."""

import json
import pytest
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "src")

from zpilot.mcp_server import create_mcp_server, _dispatch
from zpilot.models import ZpilotConfig
from zpilot.detector import PaneDetector
from zpilot.events import EventBus


@pytest.fixture
def config():
    return ZpilotConfig()


@pytest.fixture
def detector(config):
    return PaneDetector(config)


@pytest.fixture
def event_bus(tmp_path, config):
    return EventBus(str(tmp_path / "events.jsonl"))


class TestMcpServerCreation:
    def test_create_server(self, config):
        server = create_mcp_server(config)
        assert server is not None
        assert server.name == "zpilot"


@pytest.mark.asyncio
class TestMcpDispatch:
    """Test MCP tool dispatch functions."""

    async def test_list_sessions(self, config, detector, event_bus):
        result = await _dispatch("list_sessions", {}, config, detector, event_bus)
        assert isinstance(result, str)
        # Should list sessions or say none found
        assert "Sessions:" in result or "No Zellij sessions" in result

    async def test_create_and_kill_session(self, config, detector, event_bus):
        name = "zptest-mcp-01"
        # Create
        result = await _dispatch("create_session", {"name": name}, config, detector, event_bus)
        assert name in result
        assert "Created" in result

        import asyncio
        await asyncio.sleep(3)

        # Verify exists
        result = await _dispatch("list_sessions", {}, config, detector, event_bus)
        assert name in result

        # Kill
        result = await _dispatch("kill_session", {"name": name}, config, detector, event_bus)
        assert "Killed" in result

    async def test_check_status(self, config, detector, event_bus):
        """Check status of a running session."""
        result = await _dispatch("check_status", {"session": "demo-build"}, config, detector, event_bus)
        data = json.loads(result)
        assert data["session"] == "demo-build"
        assert data["state"] in ["active", "idle", "waiting", "error", "exited", "unknown"]
        assert "idle_seconds" in data

    async def test_check_all(self, config, detector, event_bus):
        result = await _dispatch("check_all", {}, config, detector, event_bus)
        # Could be JSON array or "No sessions found."
        if "No sessions" not in result:
            data = json.loads(result)
            assert isinstance(data, list)
            for item in data:
                assert "session" in item
                assert "state" in item

    async def test_read_pane(self, config, detector, event_bus):
        result = await _dispatch("read_pane", {"session": "demo-build"}, config, detector, event_bus)
        assert isinstance(result, str)
        # Should have some content or "(empty pane)"
        assert len(result) > 0

    async def test_write_to_pane(self, config, detector, event_bus):
        result = await _dispatch(
            "write_to_pane",
            {"text": "echo MCP_TEST_123", "session": "demo-build"},
            config, detector, event_bus,
        )
        assert "chars" in result.lower() or "sent" in result.lower()

    async def test_run_in_pane(self, config, detector, event_bus):
        result = await _dispatch(
            "run_in_pane",
            {"command": "echo MCP_RUN_OK", "session": "demo-build"},
            config, detector, event_bus,
        )
        assert "Executed" in result

    async def test_recent_events_empty(self, config, detector, event_bus):
        result = await _dispatch("recent_events", {"count": 5}, config, detector, event_bus)
        assert isinstance(result, str)
        assert "No recent events" in result or "events:" in result.lower()

    async def test_recent_events_with_data(self, config, detector, event_bus):
        from zpilot.models import Event
        event_bus.emit(Event(session="test", new_state="active"))
        result = await _dispatch("recent_events", {"count": 5}, config, detector, event_bus)
        assert "test" in result

    async def test_unknown_tool(self, config, detector, event_bus):
        result = await _dispatch("nonexistent_tool", {}, config, detector, event_bus)
        assert "Unknown tool" in result

    async def test_search_pane_no_match(self, config, detector, event_bus):
        """search_pane with no results returns helpful message."""
        # This hits real Zellij — skip if no sessions
        try:
            result = await _dispatch(
                "search_pane",
                {"session": "demo-copilot", "pattern": "XYZZY_IMPOSSIBLE_STRING_999"},
                config, detector, event_bus,
            )
            assert "No matches" in result or "empty" in result.lower()
        except RuntimeError:
            pytest.skip("No Zellij session available")

    async def test_get_output_history(self, config, detector, event_bus):
        """get_output_history returns numbered lines."""
        try:
            result = await _dispatch(
                "get_output_history",
                {"session": "demo-copilot", "lines": 10},
                config, detector, event_bus,
            )
            assert "lines" in result.lower() or "empty" in result.lower()
        except RuntimeError:
            pytest.skip("No Zellij session available")

    async def test_write_resets_idle(self, config, detector, event_bus):
        """write_to_pane should call record_input on the detector."""
        # After dispatch, detector should have last_input_time set
        try:
            await _dispatch(
                "write_to_pane",
                {"text": "test", "session": "demo-copilot"},
                config, detector, event_bus,
            )
            idle = detector.get_idle_seconds("demo-copilot", "focused")
            assert idle < 2.0  # was just recorded
        except RuntimeError:
            pytest.skip("No Zellij session available")


@pytest.mark.asyncio
class TestMcpToolsMocked:
    """MCP tool tests with mocked Zellij (no live sessions needed)."""

    @patch("zpilot.mcp_server.zellij")
    async def test_search_pane_finds_matches(self, mock_zellij, config, detector, event_bus):
        mock_zellij.dump_pane = AsyncMock(return_value=(
            "line 1: hello world\n"
            "line 2: foo bar\n"
            "line 3: hello again\n"
            "line 4: baz qux\n"
            "line 5: hello final\n"
        ))
        result = await _dispatch(
            "search_pane",
            {"session": "test", "pattern": "hello", "context": 1},
            config, detector, event_bus,
        )
        assert "3 match" in result
        assert "hello world" in result
        assert "hello again" in result
        assert "hello final" in result

    @patch("zpilot.mcp_server.zellij")
    async def test_search_pane_no_matches(self, mock_zellij, config, detector, event_bus):
        mock_zellij.dump_pane = AsyncMock(return_value="just some content\n")
        result = await _dispatch(
            "search_pane",
            {"session": "test", "pattern": "ZZZZZ"},
            config, detector, event_bus,
        )
        assert "No matches" in result

    @patch("zpilot.mcp_server.zellij")
    async def test_search_pane_regex(self, mock_zellij, config, detector, event_bus):
        mock_zellij.dump_pane = AsyncMock(return_value=(
            "error: file not found\n"
            "warning: deprecated\n"
            "error: timeout\n"
        ))
        result = await _dispatch(
            "search_pane",
            {"session": "test", "pattern": "^error:"},
            config, detector, event_bus,
        )
        assert "2 match" in result

    @patch("zpilot.mcp_server.zellij")
    async def test_search_pane_empty(self, mock_zellij, config, detector, event_bus):
        mock_zellij.dump_pane = AsyncMock(return_value="")
        result = await _dispatch(
            "search_pane",
            {"session": "test", "pattern": "anything"},
            config, detector, event_bus,
        )
        assert "empty" in result.lower()

    @patch("zpilot.mcp_server.zellij")
    async def test_get_output_history_returns_tail(self, mock_zellij, config, detector, event_bus):
        lines = "\n".join(f"line {i}" for i in range(100))
        mock_zellij.dump_pane = AsyncMock(return_value=lines)
        result = await _dispatch(
            "get_output_history",
            {"session": "test", "lines": 5},
            config, detector, event_bus,
        )
        assert "5 of 100" in result
        assert "line 95" in result
        assert "line 99" in result

    @patch("zpilot.mcp_server.zellij")
    async def test_get_output_history_empty(self, mock_zellij, config, detector, event_bus):
        mock_zellij.dump_pane = AsyncMock(return_value="")
        result = await _dispatch(
            "get_output_history",
            {"session": "test", "lines": 10},
            config, detector, event_bus,
        )
        assert "empty" in result.lower()

    @patch("zpilot.mcp_server.zellij")
    async def test_write_to_pane_resets_idle(self, mock_zellij, config, detector, event_bus):
        mock_zellij.write_to_pane = AsyncMock()
        # Pre-set detector to show old activity
        import time
        detector._last_change_time["test:focused"] = time.time() - 60
        assert detector.get_idle_seconds("test", "focused") > 50

        await _dispatch(
            "write_to_pane",
            {"text": "ls -la", "session": "test"},
            config, detector, event_bus,
        )
        assert detector.get_idle_seconds("test", "focused") < 2.0

    @patch("zpilot.mcp_server.zellij")
    async def test_run_in_pane_resets_idle(self, mock_zellij, config, detector, event_bus):
        mock_zellij.run_command_in_pane = AsyncMock()
        import time
        detector._last_change_time["test:focused"] = time.time() - 60

        await _dispatch(
            "run_in_pane",
            {"command": "make build", "session": "test"},
            config, detector, event_bus,
        )
        assert detector.get_idle_seconds("test", "focused") < 2.0
