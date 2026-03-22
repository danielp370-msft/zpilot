"""Tests for zpilot.devtunnel module."""

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zpilot.devtunnel import (
    DEFAULT_PORT,
    DEFAULT_TUNNEL_NAME,
    TunnelDetail,
    TunnelInfo,
    add_port,
    configure_access,
    create_tunnel,
    get_or_create_tunnel,
    get_tunnel_detail,
    get_tunnel_url,
    host_tunnel,
    is_devtunnel_available,
    list_tunnels,
    stop_hosting,
)

# ── Sample CLI output fixtures ──────────────────────────────────

SAMPLE_LIST_OUTPUT = """\
Found 2 tunnels.

Tunnel ID                           Host Connections     Labels                    Ports                Expiration                Description              
zpilot-gh.aue                       0                                              1                    30 days                                            
my-test.usw2                        1                                              2                    7 days                                             
"""

SAMPLE_SHOW_OUTPUT = """\
Tunnel ID             : zpilot-gh.aue
Description           : 
Labels                : 
Access control        : {+Anonymous [connect]}
Host connections      : 0
Client connections    : 1
Current upload rate   : 0 MB/s (limit: 20 MB/s)
Current download rate : 0 MB/s (limit: 20 MB/s)
Upload total          : 782 KB
Download total        : 850 KB
Ports                 : 1
  8222    auto  https://3w2v04nm-8222.aue.devtunnels.ms/  
Tunnel Expiration     : 30 days
"""

SAMPLE_SHOW_NO_PORTS = """\
Tunnel ID             : zpilot.aue
Description           : 
Labels                : 
Access control        : 
Host connections      : 0
Client connections    : 0
Current upload rate   : 0 MB/s (limit: 20 MB/s)
Current download rate : 0 MB/s (limit: 20 MB/s)
Upload total          : 0 KB
Download total        : 0 KB
Ports                 : 0
Tunnel Expiration     : 30 days
"""

SAMPLE_PORT_LIST = """\
Found 1 tunnel port.

Port Number   Protocol      Current Connections
8222          auto          
"""


# ── is_devtunnel_available ──────────────────────────────────────


class TestIsDevtunnelAvailable:
    def test_available_when_on_path(self):
        with patch("zpilot.devtunnel.shutil.which", return_value="/usr/bin/devtunnel"):
            assert is_devtunnel_available() is True

    def test_unavailable_when_not_on_path(self):
        with patch("zpilot.devtunnel.shutil.which", return_value=None):
            assert is_devtunnel_available() is False


# ── list_tunnels ────────────────────────────────────────────────


class TestListTunnels:
    def test_parse_two_tunnels(self):
        with patch("zpilot.devtunnel._run_devtunnel", return_value=SAMPLE_LIST_OUTPUT):
            tunnels = list_tunnels()
            assert len(tunnels) == 2
            assert tunnels[0].tunnel_id == "zpilot-gh.aue"
            assert tunnels[1].tunnel_id == "my-test.usw2"

    def test_empty_list(self):
        empty_output = "Found 0 tunnels.\n\nTunnel ID   Host Connections   Labels   Ports   Expiration   Description\n"
        with patch("zpilot.devtunnel._run_devtunnel", return_value=empty_output):
            tunnels = list_tunnels()
            assert len(tunnels) == 0

    def test_returns_empty_on_error(self):
        with patch("zpilot.devtunnel._run_devtunnel", side_effect=RuntimeError("not found")):
            tunnels = list_tunnels()
            assert tunnels == []


# ── get_tunnel_detail ───────────────────────────────────────────


class TestGetTunnelDetail:
    def test_parse_show_output(self):
        with patch("zpilot.devtunnel._run_devtunnel", return_value=SAMPLE_SHOW_OUTPUT):
            detail = get_tunnel_detail("zpilot-gh")
            assert detail.tunnel_id == "zpilot-gh.aue"
            assert detail.access_control == "{+Anonymous [connect]}"
            assert detail.host_connections == 0
            assert detail.client_connections == 1
            assert detail.expiration == "30 days"
            assert len(detail.port_entries) == 1
            assert detail.port_entries[0]["port"] == "8222"
            assert detail.port_entries[0]["protocol"] == "auto"
            assert "devtunnels.ms" in detail.port_entries[0]["url"]

    def test_parse_no_ports(self):
        with patch("zpilot.devtunnel._run_devtunnel", return_value=SAMPLE_SHOW_NO_PORTS):
            detail = get_tunnel_detail("zpilot")
            assert detail.tunnel_id == "zpilot.aue"
            assert detail.port_entries == []


# ── get_tunnel_url ──────────────────────────────────────────────


class TestGetTunnelUrl:
    def test_returns_url_for_port(self):
        with patch("zpilot.devtunnel._run_devtunnel", return_value=SAMPLE_SHOW_OUTPUT):
            url = get_tunnel_url("zpilot-gh", port=8222)
            assert url == "https://3w2v04nm-8222.aue.devtunnels.ms"

    def test_returns_first_url_when_no_port_specified(self):
        with patch("zpilot.devtunnel._run_devtunnel", return_value=SAMPLE_SHOW_OUTPUT):
            url = get_tunnel_url("zpilot-gh")
            assert url is not None
            assert "devtunnels.ms" in url

    def test_returns_none_when_no_ports(self):
        with patch("zpilot.devtunnel._run_devtunnel", return_value=SAMPLE_SHOW_NO_PORTS):
            url = get_tunnel_url("zpilot")
            assert url is None

    def test_url_format_validation(self):
        with patch("zpilot.devtunnel._run_devtunnel", return_value=SAMPLE_SHOW_OUTPUT):
            url = get_tunnel_url("zpilot-gh", port=8222)
            assert url.startswith("https://")
            assert ".devtunnels.ms" in url
            assert not url.endswith("/")


# ── create_tunnel ───────────────────────────────────────────────


class TestCreateTunnel:
    def test_create_calls_devtunnel(self):
        with patch("zpilot.devtunnel._run_devtunnel") as mock_run:
            mock_run.side_effect = [
                "Created tunnel zpilot.aue\n",  # create
                SAMPLE_SHOW_NO_PORTS,  # get_tunnel_detail
            ]
            detail = create_tunnel("zpilot")
            # First call should be create
            mock_run.assert_any_call("create", "--id", "zpilot")
            assert detail.tunnel_id == "zpilot.aue"


# ── add_port ────────────────────────────────────────────────────


class TestAddPort:
    def test_add_port_calls_devtunnel(self):
        with patch("zpilot.devtunnel._run_devtunnel") as mock_run:
            mock_run.side_effect = [
                "Port 8222 created\n",  # port create
                SAMPLE_SHOW_OUTPUT,  # get_tunnel_detail
            ]
            result = add_port("zpilot-gh", 8222)
            mock_run.assert_any_call(
                "port", "create", "zpilot-gh", "-p", "8222", "--protocol", "auto"
            )
            assert result["port"] == "8222"


# ── configure_access ────────────────────────────────────────────


class TestConfigureAccess:
    def test_anonymous_access(self):
        with patch("zpilot.devtunnel._run_devtunnel") as mock_run:
            mock_run.return_value = "OK\n"
            configure_access("zpilot", anonymous=True)
            mock_run.assert_called_once_with(
                "access", "create", "zpilot", "--anonymous"
            )

    def test_no_change_when_not_anonymous(self):
        with patch("zpilot.devtunnel._run_devtunnel") as mock_run:
            configure_access("zpilot", anonymous=False)
            mock_run.assert_not_called()


# ── host_tunnel ─────────────────────────────────────────────────


class TestHostTunnel:
    @pytest.mark.asyncio
    async def test_starts_background_process(self):
        mock_proc = AsyncMock()
        mock_proc.returncode = None  # still running
        mock_proc.stderr = AsyncMock()

        with patch("zpilot.devtunnel._devtunnel_bin", return_value="/usr/bin/devtunnel"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                proc = await host_tunnel("zpilot", port=8222)
                assert proc is mock_proc

    @pytest.mark.asyncio
    async def test_raises_when_no_binary(self):
        with patch("zpilot.devtunnel._devtunnel_bin", return_value=None):
            with pytest.raises(RuntimeError, match="not available"):
                await host_tunnel("zpilot")

    @pytest.mark.asyncio
    async def test_anonymous_flag(self):
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr = AsyncMock()

        with patch("zpilot.devtunnel._devtunnel_bin", return_value="/usr/bin/devtunnel"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
                await host_tunnel("zpilot", port=8222, allow_anonymous=True)
                call_args = mock_exec.call_args[0]
                assert "--allow-anonymous" in call_args
                assert "--port-numbers" in call_args


# ── stop_hosting ────────────────────────────────────────────────


class TestStopHosting:
    @pytest.mark.asyncio
    async def test_stop_already_exited(self):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0  # already exited
        await stop_hosting(mock_proc)
        mock_proc.terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_running_process(self):
        mock_proc = AsyncMock()
        mock_proc.returncode = None

        async def set_exited():
            mock_proc.returncode = 0

        mock_proc.wait = AsyncMock(side_effect=set_exited)
        await stop_hosting(mock_proc)
        mock_proc.terminate.assert_called_once()


# ── get_or_create_tunnel ────────────────────────────────────────


class TestGetOrCreateTunnel:
    def test_existing_tunnel_with_port(self):
        """When tunnel and port both exist, just return the URL."""
        with patch("zpilot.devtunnel.is_devtunnel_available", return_value=True):
            with patch(
                "zpilot.devtunnel.list_tunnels",
                return_value=[TunnelInfo(tunnel_id="zpilot.aue")],
            ):
                with patch(
                    "zpilot.devtunnel.get_tunnel_url",
                    return_value="https://abc-8222.aue.devtunnels.ms",
                ):
                    url = get_or_create_tunnel("zpilot", 8222)
                    assert url == "https://abc-8222.aue.devtunnels.ms"

    def test_creates_tunnel_when_missing(self):
        """When no tunnel exists, create one and add port."""
        with patch("zpilot.devtunnel.is_devtunnel_available", return_value=True):
            with patch("zpilot.devtunnel.list_tunnels", return_value=[]):
                with patch("zpilot.devtunnel.create_tunnel") as mock_create:
                    with patch("zpilot.devtunnel.get_tunnel_url", return_value=None):
                        with patch(
                            "zpilot.devtunnel.add_port",
                            return_value={"port": "8222", "url": "https://new-8222.aue.devtunnels.ms/"},
                        ):
                            url = get_or_create_tunnel("zpilot", 8222)
                            mock_create.assert_called_once_with("zpilot")
                            assert "devtunnels.ms" in url

    def test_raises_when_devtunnel_unavailable(self):
        with patch("zpilot.devtunnel.is_devtunnel_available", return_value=False):
            with pytest.raises(RuntimeError, match="not installed"):
                get_or_create_tunnel()

    def test_idempotent_existing_tunnel_and_port(self):
        """Calling twice with same params should not create anything."""
        with patch("zpilot.devtunnel.is_devtunnel_available", return_value=True):
            with patch(
                "zpilot.devtunnel.list_tunnels",
                return_value=[TunnelInfo(tunnel_id="zpilot.aue")],
            ):
                with patch(
                    "zpilot.devtunnel.get_tunnel_url",
                    return_value="https://xyz-8222.aue.devtunnels.ms",
                ):
                    with patch("zpilot.devtunnel.create_tunnel") as mock_create:
                        with patch("zpilot.devtunnel.add_port") as mock_add:
                            url = get_or_create_tunnel("zpilot", 8222)
                            mock_create.assert_not_called()
                            mock_add.assert_not_called()
                            assert url == "https://xyz-8222.aue.devtunnels.ms"


# ── CLI commands ────────────────────────────────────────────────


class TestCLICommands:
    """Test the tunnel CLI commands via Click runner."""

    def test_tunnel_status_help(self):
        r = subprocess.run(
            ["python3", "-m", "zpilot.cli", "tunnel-status", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0
        assert "tunnel" in r.stdout.lower() or "devtunnel" in r.stdout.lower()

    def test_tunnel_up_help(self):
        r = subprocess.run(
            ["python3", "-m", "zpilot.cli", "tunnel-up", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0
        assert "--port" in r.stdout
        assert "--name" in r.stdout

    def test_tunnel_down_help(self):
        r = subprocess.run(
            ["python3", "-m", "zpilot.cli", "tunnel-down", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0
