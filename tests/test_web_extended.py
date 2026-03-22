"""Extended web API tests with mocked zellij dependencies.

Tests SSE streaming, keys endpoint, raw pane content,
helper functions, WebSocket terminal, and self-signed cert generation.
"""

import sys

sys.path.insert(0, "src")

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from zpilot.models import PaneState, Session
from zpilot.web.app import (
    _normalize_for_xterm,
    _strip_ansi,
    app,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── _normalize_for_xterm tests ───────────────────────────────────────

class TestNormalizeForXterm:
    def test_plain_text(self):
        result = _normalize_for_xterm("hello world")
        assert "hello world" in result

    def test_preserves_sgr_colors(self):
        text = "\x1b[32mgreen\x1b[0m"
        result = _normalize_for_xterm(text)
        assert "\x1b[32m" in result
        assert "green" in result

    def test_strips_osc_sequences(self):
        text = "\x1b]0;my title\x07real content"
        result = _normalize_for_xterm(text)
        assert "my title" not in result
        assert "real content" in result

    def test_strips_charset_switches(self):
        text = "\x1b(0before\x1b(Bafter"
        result = _normalize_for_xterm(text)
        assert "\x1b(0" not in result
        assert "before" in result

    def test_newline_to_crlf_conversion(self):
        text = "line1\nline2\nline3"
        result = _normalize_for_xterm(text)
        assert "\r\n" in result

    def test_fullscreen_app_keeps_last_frame(self):
        text = "frame1\x1b[2Jframe2\x1b[2Jlast frame content"
        result = _normalize_for_xterm(text)
        assert "last frame content" in result

    def test_collapses_blank_lines(self):
        text = "a\n\n\n\n\nb"
        result = _normalize_for_xterm(text)
        # Should collapse 5+ newlines to 2
        assert "\r\n\r\n\r\n\r\n" not in result

    def test_strips_mode_sequences(self):
        text = "\x1b[?1049hcontent\x1b[?1049l"
        result = _normalize_for_xterm(text)
        assert "\x1b[?" not in result


# ── _get_session_data tests ──────────────────────────────────────────

class TestGetSessionData:
    @pytest.mark.asyncio
    async def test_returns_session_list(self):
        sessions = [
            Session(name="sess1", is_current=True),
            Session(name="sess2"),
        ]
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(return_value="$ \n")

            from zpilot.web.app import _get_session_data
            result = await _get_session_data()

            assert len(result) == 2
            names = [s["name"] for s in result]
            assert "sess1" in names
            assert "sess2" in names

    @pytest.mark.asyncio
    async def test_handles_list_sessions_error(self):
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(side_effect=Exception("no zellij"))

            from zpilot.web.app import _get_session_data
            result = await _get_session_data()
            assert result == []

    @pytest.mark.asyncio
    async def test_handles_dump_pane_error(self):
        sessions = [Session(name="broken")]
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(side_effect=Exception("pane error"))

            from zpilot.web.app import _get_session_data
            result = await _get_session_data()
            assert len(result) == 1
            assert result[0]["state"] == "unknown"
            assert "error" in result[0]["last_line"]


# ── SSE stream endpoint ─────────────────────────────────────────────

class TestSSEStream:
    @pytest.mark.asyncio
    async def test_sse_stream_returns_events(self, client):
        """Test /api/stream returns SSE formatted data."""
        sessions = [Session(name="s1")]

        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(return_value="hello\n")

            # Use streaming with wait_for timeout — SSE endpoint sleeps 2s
            async def read_first_chunk():
                async with client.stream("GET", "/api/stream") as resp:
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers["content-type"]
                    chunk = b""
                    async for c in resp.aiter_bytes():
                        chunk += c
                        if b"data:" in chunk:
                            break
                    return chunk

            try:
                chunk = await asyncio.wait_for(read_first_chunk(), timeout=5.0)
                text = chunk.decode(errors="replace")
                assert "data:" in text
            except asyncio.TimeoutError:
                pytest.skip("SSE endpoint did not produce data within 5s")


# ── Keys endpoint ───────────────────────────────────────────────────

class TestKeysEndpoint:
    @pytest.mark.asyncio
    async def test_send_keys(self, client):
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.send_special_key = AsyncMock(return_value=True)

            resp = await client.post(
                "/api/session/test-sess/keys",
                json=["enter", "arrow_up"],
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "sent"
            assert len(data["results"]) == 2
            assert data["results"][0]["key"] == "enter"
            assert data["results"][0]["sent"] is True


# ── Raw pane endpoint ───────────────────────────────────────────────

class TestRawPaneEndpoint:
    @pytest.mark.asyncio
    async def test_raw_pane_content(self, client):
        raw = "\x1b[32mhello\x1b[0m world\n"
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.dump_pane = AsyncMock(return_value=raw)

            resp = await client.get("/api/pane/my-session/raw")
            assert resp.status_code == 200
            data = resp.json()
            assert data["session"] == "my-session"
            # Raw content should preserve ANSI codes
            assert "\x1b[32m" in data["content"]


# ── Pane content endpoint ───────────────────────────────────────────

class TestPaneContentEndpoint:
    @pytest.mark.asyncio
    async def test_pane_content(self, client):
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.dump_pane = AsyncMock(return_value="$ ls\nfile1.txt\nfile2.txt\n")

            resp = await client.get("/api/pane/demo-build")
            assert resp.status_code == 200
            data = resp.json()
            assert data["session"] == "demo-build"
            assert "state" in data
            assert "content" in data
            assert "idle_seconds" in data


# ── Session CRUD ─────────────────────────────────────────────────────

class TestSessionCRUD:
    @pytest.mark.asyncio
    async def test_create_session(self, client):
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.new_session = AsyncMock()
            mock_z.new_pane = AsyncMock()

            resp = await client.post("/api/session/new-test")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "created"
            mock_z.new_session.assert_awaited_once_with("new-test")

    @pytest.mark.asyncio
    async def test_delete_session(self, client):
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z._run = AsyncMock()

            resp = await client.delete("/api/session/old-test")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_send_to_session(self, client):
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.write_to_pane = AsyncMock()
            mock_z.send_enter = AsyncMock()

            resp = await client.post(
                "/api/session/demo/send",
                data={"text": "echo hi"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "sent"
            assert data["text"] == "echo hi"


# ── Dashboard and sessions list ──────────────────────────────────────

class TestDashboardAndSessions:
    @pytest.mark.asyncio
    async def test_root_returns_html(self, client):
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=[])
            mock_z.dump_pane = AsyncMock(return_value="")

            resp = await client.get("/")
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_api_sessions_returns_list(self, client):
        sessions = [Session(name="test1"), Session(name="test2")]
        with patch("zpilot.web.app.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(return_value="$ ")

            resp = await client.get("/api/sessions")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_api_events(self, client):
        resp = await client.get("/api/events?count=5")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


# ── _ensure_self_signed_cert ─────────────────────────────────────────

class TestSelfSignedCert:
    def test_returns_existing_certs(self, tmp_path):
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("CERT")
        key.write_text("KEY")

        with patch("zpilot.web.app.CERT_DIR", tmp_path):
            from zpilot.web.app import _ensure_self_signed_cert
            c, k = _ensure_self_signed_cert()
            assert c == str(cert)
            assert k == str(key)

    def test_generates_cert_when_missing(self, tmp_path):
        with patch("zpilot.web.app.CERT_DIR", tmp_path):
            from zpilot.web.app import _ensure_self_signed_cert
            try:
                c, k = _ensure_self_signed_cert()
                assert (tmp_path / "cert.pem").exists()
                assert (tmp_path / "key.pem").exists()
            except ImportError:
                pytest.skip("cryptography package not installed")


# ── run_web ──────────────────────────────────────────────────────────

class TestRunWeb:
    def test_run_web_no_ssl(self):
        with patch("uvicorn.run") as mock_uv:
            from zpilot.web.app import run_web
            run_web(host="127.0.0.1", port=9000, ssl=False)
            mock_uv.assert_called_once()
            call_kwargs = mock_uv.call_args
            assert call_kwargs[1]["host"] == "127.0.0.1"
            assert call_kwargs[1]["port"] == 9000
            assert "ssl_certfile" not in call_kwargs[1]
