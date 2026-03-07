"""Tests for web API endpoints — keys, raw pane, layouts, multi-session."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "src")

from httpx import AsyncClient, ASGITransport
from zpilot.web.app import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
class TestKeysEndpoint:
    """Test /api/session/{name}/keys endpoint."""

    async def test_send_valid_keys(self, client):
        """Valid keys return results array."""
        with patch("zpilot.web.app.zellij.send_special_key", new_callable=AsyncMock, return_value=True):
            async with client:
                resp = await client.post(
                    "/api/session/test-sess/keys",
                    json=["enter", "tab"],
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "sent"
                assert data["session"] == "test-sess"
                assert len(data["results"]) == 2
                assert data["results"][0] == {"key": "enter", "sent": True}
                assert data["results"][1] == {"key": "tab", "sent": True}

    async def test_send_invalid_key(self, client):
        """Invalid key returns sent=False in results."""
        with patch("zpilot.web.app.zellij.send_special_key", new_callable=AsyncMock, return_value=False):
            async with client:
                resp = await client.post(
                    "/api/session/test-sess/keys",
                    json=["nonexistent_key"],
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["results"][0] == {"key": "nonexistent_key", "sent": False}

    async def test_send_mixed_keys(self, client):
        """Mix of valid and invalid keys."""
        async def mock_send(key, session=None):
            return key in ("enter", "arrow_up")

        with patch("zpilot.web.app.zellij.send_special_key", side_effect=mock_send):
            async with client:
                resp = await client.post(
                    "/api/session/test-sess/keys",
                    json=["enter", "bad_key", "arrow_up"],
                )
                data = resp.json()
                assert data["results"][0]["sent"] is True
                assert data["results"][1]["sent"] is False
                assert data["results"][2]["sent"] is True

    async def test_send_empty_keys_list(self, client):
        """Empty keys list should return empty results."""
        async with client:
            resp = await client.post(
                "/api/session/test-sess/keys",
                json=[],
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["results"] == []

    async def test_key_order_preserved(self, client):
        """Keys should be sent and reported in order."""
        call_order = []

        async def mock_send(key, session=None):
            call_order.append(key)
            return True

        with patch("zpilot.web.app.zellij.send_special_key", side_effect=mock_send):
            async with client:
                keys = ["arrow_up", "arrow_down", "enter", "escape", "tab"]
                resp = await client.post("/api/session/test-sess/keys", json=keys)
                data = resp.json()
                assert call_order == keys
                assert [r["key"] for r in data["results"]] == keys


@pytest.mark.asyncio
class TestRawPaneEndpoint:
    """Test /api/pane/{session_name}/raw endpoint."""

    async def test_raw_returns_content(self, client):
        """Raw endpoint returns session and content fields."""
        with patch("zpilot.web.app.zellij.dump_pane", new_callable=AsyncMock, return_value="\x1b[32mgreen\x1b[0m"):
            async with client:
                resp = await client.get("/api/pane/test-sess/raw")
                assert resp.status_code == 200
                data = resp.json()
                assert data["session"] == "test-sess"
                assert "\x1b[32m" in data["content"]  # ANSI preserved

    async def test_raw_preserves_ansi(self, client):
        """ANSI codes should NOT be stripped in raw endpoint."""
        raw_content = "\x1b[1;31mbold red\x1b[0m\x1b[34mblue\x1b[0m"
        with patch("zpilot.web.app.zellij.dump_pane", new_callable=AsyncMock, return_value=raw_content):
            async with client:
                resp = await client.get("/api/pane/test-sess/raw")
                data = resp.json()
                assert data["content"] == raw_content

    async def test_raw_respects_lines_param(self, client):
        """Lines parameter should be passed to dump_pane."""
        with patch("zpilot.web.app.zellij.dump_pane", new_callable=AsyncMock, return_value="content") as mock:
            async with client:
                await client.get("/api/pane/test-sess/raw?lines=42")
                mock.assert_called_once_with(session="test-sess", pane_name="main", tail_lines=42)

    async def test_raw_respects_pane_name(self, client):
        """pane_name parameter should be passed to dump_pane."""
        with patch("zpilot.web.app.zellij.dump_pane", new_callable=AsyncMock, return_value="content") as mock:
            async with client:
                await client.get("/api/pane/test-sess/raw?pane_name=secondary")
                mock.assert_called_once_with(session="test-sess", pane_name="secondary", tail_lines=80)

    async def test_raw_empty_pane(self, client):
        """Empty pane should return valid JSON with empty content."""
        with patch("zpilot.web.app.zellij.dump_pane", new_callable=AsyncMock, return_value=""):
            async with client:
                resp = await client.get("/api/pane/test-sess/raw")
                data = resp.json()
                assert data["content"] == ""


@pytest.mark.asyncio
class TestSendEndpoint:
    """Test /api/session/{name}/send with mocked zellij."""

    async def test_send_records_idle_reset(self, client):
        """Sending text should call record_input on detector."""
        with patch("zpilot.web.app.zellij.write_to_pane", new_callable=AsyncMock), \
             patch("zpilot.web.app.zellij.send_enter", new_callable=AsyncMock), \
             patch("zpilot.web.app.detector.record_input") as mock_record:
            async with client:
                resp = await client.post(
                    "/api/session/test-sess/send",
                    data={"text": "echo hello"},
                )
                assert resp.status_code == 200
                mock_record.assert_called_once_with("test-sess", "main")

    async def test_send_returns_text(self, client):
        """Send endpoint should echo back the text sent."""
        with patch("zpilot.web.app.zellij.write_to_pane", new_callable=AsyncMock), \
             patch("zpilot.web.app.zellij.send_enter", new_callable=AsyncMock):
            async with client:
                resp = await client.post(
                    "/api/session/my-sess/send",
                    data={"text": "my command"},
                )
                data = resp.json()
                assert data["text"] == "my command"
                assert data["session"] == "my-sess"
                assert data["status"] == "sent"


@pytest.mark.asyncio
class TestCreateDeleteSession:
    """Test session lifecycle endpoints with mocked zellij."""

    async def test_create_session(self, client):
        with patch("zpilot.web.app.zellij.new_session", new_callable=AsyncMock):
            async with client:
                resp = await client.post("/api/session/test-new")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "created"
                assert data["session"] == "test-new"

    async def test_delete_session(self, client):
        with patch("zpilot.web.app.zellij._run", new_callable=AsyncMock):
            async with client:
                resp = await client.delete("/api/session/test-del")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "deleted"
                assert data["session"] == "test-del"


@pytest.mark.asyncio
class TestStaticFiles:
    """Test static file serving for xterm.js vendor assets."""

    async def test_xterm_js_served(self, client):
        async with client:
            resp = await client.get("/static/vendor/xterm.min.js")
            assert resp.status_code == 200
            assert "Terminal" in resp.text  # xterm.js exports Terminal

    async def test_xterm_css_served(self, client):
        async with client:
            resp = await client.get("/static/vendor/xterm.min.css")
            assert resp.status_code == 200
            assert "xterm" in resp.text

    async def test_fit_addon_served(self, client):
        async with client:
            resp = await client.get("/static/vendor/addon-fit.min.js")
            assert resp.status_code == 200
            assert "FitAddon" in resp.text
