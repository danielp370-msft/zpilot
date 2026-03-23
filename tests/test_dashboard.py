"""Tests for web dashboard HTML template — layouts, DOM structure, multi-session.

These tests use httpx ASGI transport to test the rendered HTML without
requiring a separate server process or Playwright.
"""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, "src")

from httpx import AsyncClient, ASGITransport
from zpilot.web.app import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _mock_sessions(names_states):
    """Create mock session data for testing."""
    from zpilot.models import PaneState
    sessions = []
    for name, state_str in names_states:
        sessions.append({
            "name": name,
            "state": state_str,
            "last_line": f"prompt in {name}$",
            "idle_seconds": 5.0,
        })
    return sessions


@pytest.mark.asyncio
class TestDashboardHTML:
    """Test the rendered HTML structure."""

    async def test_index_has_xterm_js(self, client):
        """Index page should include xterm.js vendor scripts."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "/static/vendor/xterm.min.js" in html
            assert "/static/vendor/xterm.min.css" in html
            assert "/static/vendor/addon-fit.min.js" in html

    async def test_index_has_focus_dock_expose(self, client):
        """Should have Focus + Dock + Exposé layout elements."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "focus-area" in html
            assert "dock" in html
            assert "renderExpose" in html
            assert "focusSession" in html

    async def test_index_has_websocket_code(self, client):
        """Should include WebSocket connection code."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "ws/terminal/" in html
            assert "WebSocket" in html

    async def test_index_has_xterm_terminal_creation(self, client):
        """Should include Terminal creation with theme config."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "new Terminal(" in html
            assert "FitAddon" in html
            assert "#010409" in html  # background color

    async def test_index_has_sse_connection(self, client):
        """Should include SSE EventSource code."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "EventSource" in html
            assert "/api/stream" in html

    async def test_index_has_session_management(self, client):
        """Should include session management functions."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "function submitCreate" in html
            assert "function focusSession" in html

    async def test_index_has_create_session_function(self, client):
        """Should include createSession / submitCreate function."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "function submitCreate" in html

    async def test_index_has_dom_reconciliation(self, client):
        """renderPanels should use DOM reconciliation, not innerHTML rebuild."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            # Should have querySelector for existing panels (reconciliation)
            assert "querySelector" in html
            assert "data-session" in html

    async def test_index_has_keyboard_handling(self, client):
        """Should have keyboard event handling for inline typing."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "keydown" in html or "keyboard" in html.lower() or "sendCmd" in html

    async def test_index_has_destroy_terminal(self, client):
        """Should have destroyTerminal function for cleanup."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "function destroyTerminal" in html
            assert ".dispose()" in html  # xterm cleanup
            assert ".close()" in html    # websocket cleanup


@pytest.mark.asyncio
class TestJavaScriptLayout:
    """Test that layout-related JavaScript is correctly emitted."""

    async def test_focus_dock_expose_layout(self, client):
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "focus-area" in html
            assert "dock-pill" in html
            assert "renderExpose" in html

    async def test_focus_and_split(self, client):
        """Focus view with split toggle."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "toggleSplit" in html
            assert "focusSession" in html

    async def test_dock_renders(self, client):
        """Dock should render session pills."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "renderDock" in html
            assert "dock-pill" in html


@pytest.mark.asyncio
class TestWebSocketEndpoint:
    """Test WebSocket endpoint behavior with mocked zellij."""

    async def test_ws_endpoint_exists(self, client):
        """WebSocket route should be registered."""
        # Can't fully test WS with httpx, but verify the route is registered
        from zpilot.web.app import app as fastapi_app
        ws_routes = [r for r in fastapi_app.routes if hasattr(r, 'path') and 'ws' in r.path]
        assert len(ws_routes) > 0
        ws_paths = [r.path for r in ws_routes]
        assert "/ws/terminal/{session_name}" in ws_paths


@pytest.mark.asyncio
class TestSessionListRendering:
    """Test that sessions are rendered correctly in the sidebar."""

    async def test_sessions_in_dock(self, client):
        """Active sessions should appear in the dock."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "dock-pill" in html
            assert "renderDock" in html

    async def test_session_item_structure(self, client):
        """Each session pill should be rendered via data-session."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "data-session" in html
            assert "focusSession" in html


@pytest.mark.asyncio
class TestReconnectionLogic:
    """Test that reconnection and resilience code is present in the dashboard."""

    async def test_sse_reconnect_on_error(self, client):
        """SSE should have onerror handler that reconnects."""
        async with client:
            html = (await client.get("/")).text
            assert "src.onerror" in html
            assert "src.close()" in html
            assert "setTimeout(connectSSE" in html

    async def test_sse_exponential_backoff(self, client):
        """SSE reconnect should use exponential backoff up to 30s."""
        async with client:
            html = (await client.get("/")).text
            assert "sseRetryDelay" in html
            assert "30000" in html

    async def test_sse_status_indicator_offline(self, client):
        """SSE error should update status indicator."""
        async with client:
            html = (await client.get("/")).text
            assert "sse-dot" in html
            assert "Reconnect" in html

    async def test_sse_status_indicator_online(self, client):
        """SSE open should restore status indicator."""
        async with client:
            html = (await client.get("/")).text
            assert "src.onopen" in html
            assert "sse-dot" in html
            assert "live" in html

    async def test_sse_dot_element_exists(self, client):
        """Status bar should have sse-dot element."""
        async with client:
            html = (await client.get("/")).text
            assert 'id="sse-dot"' in html

    async def test_offline_dot_css(self, client):
        """CSS should style the sse-dot indicator."""
        async with client:
            html = (await client.get("/")).text
            assert ".sse-dot" in html
            assert 'id="sse-dot"' in html

    async def test_ws_reconnect_with_backoff(self, client):
        """WebSocket should reconnect with exponential backoff."""
        async with client:
            html = (await client.get("/")).text
            assert "wsRetryDelay" in html
            assert "wsRetryDelay[name] = Math.min" in html

    async def test_ws_backoff_resets_on_connect(self, client):
        """WebSocket backoff should reset on successful connection."""
        async with client:
            html = (await client.get("/")).text
            assert "wsRetryDelay[name] = 1000" in html

    async def test_ws_cleanup_on_destroy(self, client):
        """Destroying a terminal should clean up retry state."""
        async with client:
            html = (await client.get("/")).text
            assert "delete wsRetryDelay[name]" in html

    async def test_sse_dot_css_styles(self, client):
        """Should have CSS for sse-dot status indicator."""
        async with client:
            html = (await client.get("/")).text
            assert ".sse-dot" in html
