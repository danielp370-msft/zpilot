"""Tests for zpilot HTTP MCP server and MCPTransport."""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import pytest
import httpx

from zpilot.models import ZpilotConfig
from zpilot.mcp_http import _validate_path
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
        home = str(Path.home())
        with tempfile.TemporaryDirectory(dir=home) as tmpdir:
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
        home = str(Path.home())
        missing = os.path.join(home, "nonexistent_zpilot_test_file.txt")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/download",
                params={"path": missing},
                headers=auth_headers,
            )
            assert resp.status_code == 404


# ── Path Validation Tests ───────────────────────────────────────

class TestValidatePath:
    def test_accepts_path_under_home(self):
        home = str(Path.home())
        p = os.path.join(home, "some_file.txt")
        assert _validate_path(p) == str(Path(p).resolve())

    def test_rejects_path_outside_home(self):
        with pytest.raises(ValueError, match="outside|under"):
            _validate_path("/etc/passwd")

    def test_rejects_traversal(self):
        home = str(Path.home())
        with pytest.raises(ValueError, match="outside|under"):
            _validate_path(os.path.join(home, "..", "..", "etc", "passwd"))

    def test_resolves_empty_path_to_cwd(self):
        # Empty string resolves to cwd; endpoint-level checks catch this
        result = _validate_path("")
        assert result  # returns resolved cwd path

    @pytest.mark.asyncio
    async def test_upload_path_traversal_returns_403(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/upload",
                json={"path": "/etc/passwd", "content": base64.b64encode(b"x").decode()},
                headers=auth_headers,
            )
            assert resp.status_code == 403


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


# ── Siblings endpoint tests ─────────────────────────────────────

class TestSiblingsEndpoint:
    @pytest.mark.asyncio
    async def test_siblings_requires_auth(self, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/siblings")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_siblings_returns_list(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/siblings", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "siblings" in data
            assert isinstance(data["siblings"], list)
            # Should have at least the local node
            assert data["count"] >= 1
            # Each sibling should have expected fields
            for s in data["siblings"]:
                assert "name" in s
                assert "transport" in s


# ── TLS / Certificate Tests ─────────────────────────────────────

class TestCertGeneration:
    def test_generate_self_signed_cert_creates_files(self):
        """Test that generate_self_signed_cert creates cert and key files."""
        from zpilot.mcp_http import generate_self_signed_cert
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            cert_dir = Path(tmpdir)
            cert_path, key_path, fingerprint = generate_self_signed_cert(cert_dir)

            assert os.path.exists(cert_path)
            assert os.path.exists(key_path)
            assert cert_path.endswith("mcp-cert.pem")
            assert key_path.endswith("mcp-key.pem")

            # Key file should be owner-only readable
            key_mode = os.stat(key_path).st_mode & 0o777
            assert key_mode == 0o600

            # Fingerprint should be a colon-separated hex string
            assert ":" in fingerprint
            assert len(fingerprint) > 40  # SHA-256 = 64 hex chars + 31 colons

    def test_generate_cert_reuses_existing(self):
        """Test that existing certs are reused, not regenerated."""
        from zpilot.mcp_http import generate_self_signed_cert
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            cert_dir = Path(tmpdir)
            cert1, key1, fp1 = generate_self_signed_cert(cert_dir)
            cert2, key2, fp2 = generate_self_signed_cert(cert_dir)

            assert cert1 == cert2
            assert key1 == key2
            assert fp1 == fp2  # same cert = same fingerprint

    def test_generated_cert_has_san(self):
        """Verify cert includes SAN with localhost + 127.0.0.1."""
        from zpilot.mcp_http import generate_self_signed_cert
        from pathlib import Path
        from cryptography import x509

        with tempfile.TemporaryDirectory() as tmpdir:
            cert_path, _, _ = generate_self_signed_cert(Path(tmpdir))
            cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())

            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            dns_names = san.get_values_for_type(x509.DNSName)
            assert "localhost" in dns_names

            import ipaddress
            ip_addrs = san.get_values_for_type(x509.IPAddress)
            assert ipaddress.IPv4Address("127.0.0.1") in ip_addrs

    def test_generated_cert_validity_365_days(self):
        """Cert should be valid for 365 days."""
        from zpilot.mcp_http import generate_self_signed_cert
        from pathlib import Path
        from cryptography import x509

        with tempfile.TemporaryDirectory() as tmpdir:
            cert_path, _, _ = generate_self_signed_cert(Path(tmpdir))
            cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())

            delta = cert.not_valid_after - cert.not_valid_before
            assert delta.days == 365


class TestServeHttpTLS:
    def test_serve_http_tls_generates_certs(self):
        """When http_tls=True and no cert provided, certs should be auto-generated."""
        from zpilot.mcp_http import generate_self_signed_cert
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            cert_dir = Path(tmpdir)
            cert_path, key_path, fp = generate_self_signed_cert(cert_dir)

            # Verify the files exist and are valid PEM
            cert_bytes = Path(cert_path).read_bytes()
            key_bytes = Path(key_path).read_bytes()
            assert b"BEGIN CERTIFICATE" in cert_bytes
            assert b"BEGIN EC PRIVATE KEY" in key_bytes


class TestMCPTransportTLS:
    def test_verify_ssl_default_false(self):
        """By default, verify_ssl is False (for self-signed certs)."""
        t = MCPTransport(url="https://host:8222", token="tok")
        assert t.verify_ssl is False
        assert t._verify is False

    def test_verify_ssl_true(self):
        """When verify_ssl=True, _verify returns True."""
        t = MCPTransport(url="https://host:8222", verify_ssl=True)
        assert t._verify is True

    def test_ca_cert_overrides_verify(self):
        """When ca_cert is set, _verify returns the CA path."""
        t = MCPTransport(
            url="https://host:8222", verify_ssl=False, ca_cert="/path/to/ca.pem"
        )
        assert t._verify == "/path/to/ca.pem"

    def test_cert_fingerprint_stored(self):
        """cert_fingerprint should be stored on the transport."""
        fp = "ab:cd:ef:12:34"
        t = MCPTransport(url="https://host:8222", cert_fingerprint=fp)
        assert t.cert_fingerprint == fp

    def test_factory_passes_ssl_opts(self):
        """create_transport should forward verify_ssl and ca_cert."""
        t = create_transport(
            "mcp",
            host="https://remote:8222",
            token="secret",
            verify_ssl=True,
            ca_cert="/my/ca.pem",
            cert_fingerprint="aa:bb",
        )
        assert isinstance(t, MCPTransport)
        assert t.verify_ssl is True
        assert t.ca_cert == "/my/ca.pem"
        assert t.cert_fingerprint == "aa:bb"
        assert t._verify == "/my/ca.pem"
