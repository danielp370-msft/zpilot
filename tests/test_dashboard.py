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

    async def test_index_has_layout_buttons(self, client):
        """Should have 4 layout buttons (1, 2h, 2v, 4)."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "setLayout('1')" in html
            assert "setLayout('2h')" in html
            assert "setLayout('2v')" in html
            assert "setLayout('4')" in html

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

    async def test_index_has_send_cmd_function(self, client):
        """Should include sendCmd function for input bar."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "function sendCmd" in html

    async def test_index_has_create_session_function(self, client):
        """Should include createSession function."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "function createSession" in html

    async def test_index_has_dom_reconciliation(self, client):
        """renderPanels should use DOM reconciliation, not innerHTML rebuild."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            # Should have querySelector for existing panels (reconciliation)
            assert "querySelector" in html
            assert "data-session" in html

    async def test_index_has_keyboard_toggle(self, client):
        """Should have toggleInputBar function."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "toggleInputBar" in html
            assert "hideInputBar" in html

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

    async def test_layout_max_panels_defined(self, client):
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "'1': 1" in html
            assert "'2h': 2" in html
            assert "'2v': 2" in html
            assert "'4': 4" in html

    async def test_layout_class_applied(self, client):
        """Default layout class should be on the panels container."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "layout-2h" in html  # default layout

    async def test_empty_state_message(self, client):
        """When no sessions docked, show hint message."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "Click a session" in html


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

    async def test_sessions_in_sidebar(self, client):
        """Active sessions should appear in the sidebar HTML."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            # The sidebar should have session-list div
            assert 'id="session-list"' in html
            # Should have event panel
            assert "EVENTS" in html

    async def test_session_item_structure(self, client):
        """Each session item should have name, state, preview, idle."""
        async with client:
            resp = await client.get("/")
            html = resp.text
            assert "si-name" in html
            assert "si-state" in html
            assert "si-preview" in html
            assert "si-meta" in html
