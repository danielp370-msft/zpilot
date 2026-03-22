"""Tests for zpilot.transport module."""

import asyncio
import pytest
from zpilot.transport import (
    ExecResult,
    LocalTransport,
    SSHTransport,
    Transport,
    create_transport,
)


class TestExecResult:
    def test_ok_zero(self):
        r = ExecResult(returncode=0, stdout="hi", stderr="")
        assert r.ok is True

    def test_ok_nonzero(self):
        r = ExecResult(returncode=1, stdout="", stderr="err")
        assert r.ok is False


class TestLocalTransport:
    @pytest.mark.asyncio
    async def test_exec_echo(self):
        t = LocalTransport()
        r = await t.exec("echo hello")
        assert r.ok
        assert "hello" in r.stdout

    @pytest.mark.asyncio
    async def test_exec_fail(self):
        t = LocalTransport()
        r = await t.exec("false")
        assert not r.ok

    @pytest.mark.asyncio
    async def test_is_alive(self):
        t = LocalTransport()
        assert await t.is_alive() is True

    @pytest.mark.asyncio
    async def test_read_file(self):
        t = LocalTransport()
        # /etc/hostname should exist on Linux
        content = await t.read_file("/etc/hostname")
        assert len(content.strip()) > 0

    @pytest.mark.asyncio
    async def test_list_dir(self):
        t = LocalTransport()
        entries = await t.list_dir("/tmp")
        assert isinstance(entries, list)

    @pytest.mark.asyncio
    async def test_exec_timeout(self):
        t = LocalTransport()
        r = await t.exec("sleep 10", timeout=0.5)
        # Should fail (killed or timed out)
        assert not r.ok


class TestSSHTransport:
    def test_build_command(self):
        t = SSHTransport(host="box1", user="dan", port=2222)
        # Can't actually SSH, but verify construction
        assert t.host == "box1"
        assert t.user == "dan"

    def test_wsl_wrapping(self):
        t = SSHTransport(host="box1", wsl_distro="Ubuntu", wsl_user="dan")
        assert t.wsl_distro == "Ubuntu"


class TestFactory:
    def test_local(self):
        t = create_transport("local")
        assert isinstance(t, LocalTransport)

    def test_ssh(self):
        t = create_transport("ssh", host="myhost")
        assert isinstance(t, SSHTransport)
        assert t.host == "myhost"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown transport"):
            create_transport("magic")


class TestMCPTransportFactory:
    def test_mcp_factory(self):
        from zpilot.transport import MCPTransport
        t = create_transport("mcp", host="https://example.com:8222")
        assert isinstance(t, MCPTransport)
        assert t.base_url == "https://example.com:8222"

    def test_mcp_factory_defaults(self):
        from zpilot.transport import MCPTransport
        t = create_transport("mcp", host="https://example.com:8222")
        assert t.max_retries == 3
        assert t.retry_delay == 2.0

    def test_mcp_factory_custom_retry(self):
        from zpilot.transport import MCPTransport
        t = create_transport(
            "mcp", host="https://example.com:8222",
            max_retries=5, retry_delay=1.0,
        )
        assert t.max_retries == 5
        assert t.retry_delay == 1.0


class TestMCPTransportRetry:
    @pytest.mark.asyncio
    async def test_exec_retries_on_failure(self):
        """Verify exec retries and returns error after all attempts fail."""
        from zpilot.transport import MCPTransport
        t = MCPTransport(
            url="http://localhost:1",  # unreachable
            max_retries=2,
            retry_delay=0.01,  # fast for tests
        )
        result = await t.exec("echo hi", timeout=1.0)
        assert not result.ok
        assert "after 2 attempts" in result.stderr

    @pytest.mark.asyncio
    async def test_is_alive_retries_on_failure(self):
        """Verify is_alive retries and returns False after failures."""
        from zpilot.transport import MCPTransport
        t = MCPTransport(
            url="http://localhost:1",
            max_retries=2,
            retry_delay=0.01,
        )
        alive = await t.is_alive()
        assert alive is False

    def test_url_strip_mcp_suffix(self):
        from zpilot.transport import MCPTransport
        t = MCPTransport(url="https://host:8222/mcp")
        assert t.base_url == "https://host:8222"

    def test_url_strip_trailing_slash(self):
        from zpilot.transport import MCPTransport
        t = MCPTransport(url="https://host:8222/")
        assert t.base_url == "https://host:8222"
