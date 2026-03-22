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
