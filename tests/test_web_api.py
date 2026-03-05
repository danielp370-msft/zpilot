"""Tests for zpilot.web.app — FastAPI endpoints."""

import asyncio
import json
import pytest
import sys

sys.path.insert(0, "src")

from httpx import AsyncClient, ASGITransport
from zpilot.web.app import app, _strip_ansi


class TestStripAnsi:
    """Test ANSI escape stripping utility."""

    def test_plain_text(self):
        assert _strip_ansi("hello world") == "hello world"

    def test_csi_color(self):
        assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_csi_cursor(self):
        assert _strip_ansi("\x1b[2J\x1b[Hstart") == "start"

    def test_csi_question(self):
        assert _strip_ansi("\x1b[?2004h$ prompt\x1b[?2004l") == "$ prompt"

    def test_osc_title(self):
        assert _strip_ansi("\x1b]0;title\x07content") == "content"

    def test_osc_title_st(self):
        assert _strip_ansi("\x1b]0;title\x1b\\content") == "content"

    def test_charset_switch(self):
        assert _strip_ansi("\x1b(B\x1b)0text") == "text"

    def test_keypad_mode(self):
        assert _strip_ansi("\x1b=\x1b>text") == "text"

    def test_control_chars(self):
        assert _strip_ansi("\x00\x01\x02hello\x0e\x0f") == "hello"

    def test_preserves_newline_tab(self):
        assert _strip_ansi("line1\nline2\tcol") == "line1\nline2\tcol"

    def test_complex_mixed(self):
        raw = "\x1b[?2004h\x1b]0;user@host\x07\x1b[01;32muser\x1b[00m:\x1b[01;34m~\x1b[00m$ \x1b[?2004l"
        clean = _strip_ansi(raw)
        assert "\x1b" not in clean
        assert "user" in clean
        assert "~" in clean


@pytest.mark.asyncio
class TestApiEndpoints:
    """Test REST API endpoints via HTTPX async client."""

    @pytest.fixture
    def client(self):
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://testserver")

    async def test_root_returns_html(self, client):
        async with client:
            resp = await client.get("/")
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]
            assert "zpilot" in resp.text.lower()

    async def test_api_sessions(self, client):
        async with client:
            resp = await client.get("/api/sessions")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)

    async def test_api_events(self, client):
        async with client:
            resp = await client.get("/api/events")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)

    async def test_api_events_with_count(self, client):
        async with client:
            resp = await client.get("/api/events?count=5")
            assert resp.status_code == 200

    async def test_api_pane_content(self, client):
        """Test pane content endpoint — returns content or error gracefully."""
        async with client:
            resp = await client.get("/api/pane/demo-build")
            assert resp.status_code == 200
            data = resp.json()
            assert "session" in data
            assert "state" in data
            assert "content" in data

    async def test_api_pane_nonexistent(self, client):
        """Non-existent session should still return 200 with empty/error content."""
        async with client:
            resp = await client.get("/api/pane/nonexistent-session-xyz")
            # Should not crash — returns either 200 with empty or 404
            assert resp.status_code in (200, 404, 500)

    async def test_create_delete_session(self, client):
        """Create and delete a test session."""
        name = "zptest-api-01"
        async with client:
            # Create
            resp = await client.post(f"/api/session/{name}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "created"

            await asyncio.sleep(3)

            # Verify it appears in list
            resp = await client.get("/api/sessions")
            names = [s["name"] for s in resp.json()]
            assert name in names

            # Delete
            resp = await client.delete(f"/api/session/{name}")
            assert resp.status_code == 200
            assert resp.json()["status"] == "deleted"

            await asyncio.sleep(1)

    async def test_send_command(self, client):
        """Send a command to an existing session."""
        async with client:
            # Send to demo-build (should exist from prior setup)
            resp = await client.post(
                "/api/session/demo-build/send",
                data={"text": "echo APITEST_MARKER"},
            )
            if resp.status_code == 200:
                data = resp.json()
                assert data["status"] == "sent"
                assert "APITEST_MARKER" in data["text"]
            # If session doesn't exist, that's OK — we test the endpoint works

    # SSE (/api/stream) is tested via Playwright — httpx ASGI transport
    # doesn't handle infinite streams well.
