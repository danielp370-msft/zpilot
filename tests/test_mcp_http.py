"""Tests for zpilot HTTP MCP server and MCPTransport."""

from __future__ import annotations

import base64
import os
import tempfile

import pytest
import httpx

from zpilot.models import ZpilotConfig
from zpilot.transport import MCPTransport, create_transport, ExecResult


# ── Fixture: FastAPI test app ───────────────────────────────────

@pytest.fixture
def test_token():
    return "test-secret-token-42"


@pytest.fixture
def app(test_token):
    """Create a test FastAPI app with a known token."""
    from zpilot.mcp_http import create_http_app

    config = ZpilotConfig(http_token=test_token)
    return create_http_app(config)


@pytest.fixture
def auth_headers(test_token):
    return {"Authorization": f"Bearer {test_token}"}


# ── HTTP Server Tests ───────────────────────────────────────────

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_200(self, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["node"] == "zpilot"

    @pytest.mark.asyncio
    async def test_health_no_auth_required(self, app):
        """Health endpoint should work without any auth header."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_exec_without_token_returns_401(self, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/exec", json={"command": "echo hi"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_exec_with_wrong_token_returns_401(self, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/exec",
                json={"command": "echo hi"},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_exec_with_valid_token_accepted(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/exec",
                json={"command": "echo hi"},
                headers=auth_headers,
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_mcp_without_token_returns_401(self, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/mcp", content=b"{}")
            assert resp.status_code == 401


class TestExecEndpoint:
    @pytest.mark.asyncio
    async def test_exec_echo(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/exec",
                json={"command": "echo hello-zpilot"},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["returncode"] == 0
            assert "hello-zpilot" in data["stdout"]

    @pytest.mark.asyncio
    async def test_exec_failure(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/exec",
                json={"command": "false"},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["returncode"] != 0

    @pytest.mark.asyncio
    async def test_exec_captures_stderr(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/exec",
                json={"command": "echo err >&2"},
                headers=auth_headers,
            )
            data = resp.json()
            assert "err" in data["stderr"]


class TestUploadDownloadEndpoints:
    @pytest.mark.asyncio
    async def test_upload_and_download(self, app, auth_headers):
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = os.path.join(tmpdir, "test_upload.txt")
            content = b"hello from zpilot test"
            content_b64 = base64.b64encode(content).decode()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Upload
                resp = await client.post(
                    "/api/upload",
                    json={"path": target_path, "content": content_b64},
                    headers=auth_headers,
                )
                assert resp.status_code == 200
                assert resp.json()["status"] == "ok"
                assert os.path.exists(target_path)

                # Download
                resp = await client.get(
                    "/api/download",
                    params={"path": target_path},
                    headers=auth_headers,
                )
                assert resp.status_code == 200
                data = resp.json()
                assert base64.b64decode(data["content"]) == content

    @pytest.mark.asyncio
    async def test_upload_missing_path(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/upload",
                json={"path": "", "content": ""},
                headers=auth_headers,
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_download_missing_file(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/download",
                params={"path": "/nonexistent/file.txt"},
                headers=auth_headers,
            )
            assert resp.status_code == 404


# ── MCPTransport Tests ──────────────────────────────────────────

class TestMCPTransport:
    def test_url_normalization(self):
        t = MCPTransport(url="https://host:8222/mcp", token="tok")
        assert t.base_url == "https://host:8222"

    def test_url_normalization_trailing_slash(self):
        t = MCPTransport(url="https://host:8222/", token="tok")
        assert t.base_url == "https://host:8222"

    def test_headers_with_token(self):
        t = MCPTransport(url="https://host:8222", token="my-token")
        h = t._headers()
        assert h["Authorization"] == "Bearer my-token"

    def test_headers_without_token(self):
        t = MCPTransport(url="https://host:8222")
        h = t._headers()
        assert "Authorization" not in h

    @pytest.mark.asyncio
    async def test_exec_via_http(self, app, auth_headers, test_token):
        """Test MCPTransport.exec() by mocking the HTTP server with the FastAPI app."""
        transport = httpx.ASGITransport(app=app)

        # Patch MCPTransport to use the test transport
        t = MCPTransport(url="http://test", token=test_token)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/exec",
                json={"command": "echo mcp-test", "timeout": 10},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["returncode"] == 0
            assert "mcp-test" in data["stdout"]

    @pytest.mark.asyncio
    async def test_is_alive_via_http(self, app, test_token):
        """Test is_alive() hits the health endpoint."""
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200


class TestTransportFactory:
    def test_mcp_transport_creation(self):
        t = create_transport("mcp", host="https://remote:8222", token="secret")
        assert isinstance(t, MCPTransport)
        assert t.base_url == "https://remote:8222"
        assert t.token == "secret"

    def test_mcp_transport_via_url_opt(self):
        t = create_transport("mcp", url="https://remote:8222/mcp")
        assert isinstance(t, MCPTransport)
        assert t.base_url == "https://remote:8222"

    def test_mcp_transport_missing_url_raises(self):
        with pytest.raises(ValueError, match="MCP transport requires"):
            create_transport("mcp")


# ── Token generation test ───────────────────────────────────────

class TestTokenGeneration:
    def test_generate_token(self):
        from zpilot.mcp_http import generate_token
        token = generate_token()
        assert len(token) > 20
        # Should be URL-safe
        assert all(c.isalnum() or c in "-_" for c in token)

    def test_tokens_are_unique(self):
        from zpilot.mcp_http import generate_token
        tokens = {generate_token() for _ in range(10)}
        assert len(tokens) == 10  # all unique
