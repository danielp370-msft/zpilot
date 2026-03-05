"""Tests for zpilot MCP server tool definitions and dispatch."""

import json
import pytest
import sys

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
