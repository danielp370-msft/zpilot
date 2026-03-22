"""Devtunnel integration for zpilot.

Manages Azure Dev Tunnels as an optional way to expose zpilot's HTTP server
to remote nodes without SSH. Devtunnel provides:
  - Public HTTPS URL (TLS handled by devtunnel infrastructure)
  - Access control via GitHub/Microsoft/Entra ID login
  - No firewall/port-forwarding needed

Usage:
  zpilot tunnel-up        — create/start a devtunnel for zpilot HTTP server
  zpilot tunnel-down      — stop hosting
  zpilot tunnel-status    — show tunnel info and URL
  zpilot serve-http --tunnel  — start HTTP server + tunnel together
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass

log = logging.getLogger("zpilot.devtunnel")

# Default tunnel name used by zpilot
DEFAULT_TUNNEL_NAME = "zpilot"
DEFAULT_PORT = 8222


def _devtunnel_bin() -> str | None:
    """Return the path to the devtunnel binary, or None."""
    return shutil.which("devtunnel")


def is_devtunnel_available() -> bool:
    """Check if the ``devtunnel`` CLI is installed and on PATH."""
    return _devtunnel_bin() is not None


@dataclass
class TunnelInfo:
    """Parsed tunnel metadata."""

    tunnel_id: str
    host_connections: int = 0
    labels: str = ""
    ports: int = 0
    expiration: str = ""
    description: str = ""


@dataclass
class TunnelDetail:
    """Detailed tunnel info from ``devtunnel show``."""

    tunnel_id: str
    description: str = ""
    labels: str = ""
    access_control: str = ""
    host_connections: int = 0
    client_connections: int = 0
    port_entries: list[dict[str, str]] | None = None
    expiration: str = ""


def _run_devtunnel(*args: str, check: bool = True) -> str:
    """Run a devtunnel CLI command synchronously and return stdout.

    Raises RuntimeError if devtunnel is not available.
    Raises subprocess.CalledProcessError if *check* is True and the command fails.
    """
    import subprocess

    binary = _devtunnel_bin()
    if not binary:
        raise RuntimeError(
            "devtunnel CLI is not installed or not on PATH. "
            "Install from: https://aka.ms/devtunnels/cli"
        )
    cmd = [binary, *args]
    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"devtunnel {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


async def _run_devtunnel_async(*args: str) -> str:
    """Run a devtunnel CLI command asynchronously and return stdout."""
    binary = _devtunnel_bin()
    if not binary:
        raise RuntimeError("devtunnel CLI is not installed or not on PATH.")
    proc = await asyncio.create_subprocess_exec(
        binary,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"devtunnel {' '.join(args)} failed: {stderr.decode().strip()}"
        )
    return stdout.decode()


# ── Listing / querying ──────────────────────────────────────────


def list_tunnels() -> list[TunnelInfo]:
    """List existing tunnels.

    Parses the table output of ``devtunnel list``.
    """
    try:
        output = _run_devtunnel("list")
    except RuntimeError:
        return []

    tunnels: list[TunnelInfo] = []
    # Skip header lines — data lines start after the column-header row
    in_data = False
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # The header row contains "Tunnel ID"
        if "Tunnel ID" in stripped and "Host Connections" in stripped:
            in_data = True
            continue
        # Skip summary line like "Found N tunnel."
        if stripped.startswith("Found "):
            continue
        if in_data:
            # Parse whitespace-separated columns, but tunnel ID may contain dots
            parts = stripped.split()
            if len(parts) >= 1:
                tunnel_id = parts[0]
                tunnels.append(TunnelInfo(tunnel_id=tunnel_id))
    return tunnels


def get_tunnel_detail(tunnel_id: str) -> TunnelDetail:
    """Get detailed info for a tunnel via ``devtunnel show <id>``."""
    output = _run_devtunnel("show", tunnel_id)
    detail = TunnelDetail(tunnel_id=tunnel_id)
    port_entries: list[dict[str, str]] = []
    in_ports = False

    for line in output.splitlines():
        stripped = line.strip()
        if ":" in stripped and not in_ports:
            key, _, value = stripped.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "tunnel id":
                detail.tunnel_id = value
            elif key == "description":
                detail.description = value
            elif key == "labels":
                detail.labels = value
            elif key == "access control":
                detail.access_control = value
            elif key == "host connections":
                detail.host_connections = int(value) if value.isdigit() else 0
            elif key == "client connections":
                detail.client_connections = int(value) if value.isdigit() else 0
            elif "expiration" in key:
                detail.expiration = value
            elif key == "ports":
                in_ports = True
        elif in_ports and stripped:
            # Port lines look like: "22    auto  https://xxx.devtunnels.ms/"
            # Detect when we've left the ports section (key: value line)
            parts = stripped.split()
            if len(parts) >= 1 and parts[0].isdigit():
                entry: dict[str, str] = {"port": parts[0]}
                if len(parts) >= 2:
                    entry["protocol"] = parts[1]
                if len(parts) >= 3:
                    entry["url"] = parts[2]
                port_entries.append(entry)
            elif ":" in stripped:
                # Back to key-value pairs
                in_ports = False
                key, _, value = stripped.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if "expiration" in key:
                    detail.expiration = value

    detail.port_entries = port_entries
    return detail


def get_tunnel_url(tunnel_id: str, port: int | None = None) -> str | None:
    """Get the public HTTPS URL for a tunnel (optionally for a specific port).

    Returns None if the tunnel has no matching port configured.
    """
    detail = get_tunnel_detail(tunnel_id)
    if not detail.port_entries:
        return None
    for entry in detail.port_entries:
        if port is None or entry.get("port") == str(port):
            url = entry.get("url")
            if url:
                return url.rstrip("/")
    return None


# ── Creating / configuring ──────────────────────────────────────


def create_tunnel(name: str = DEFAULT_TUNNEL_NAME) -> TunnelDetail:
    """Create a new devtunnel.

    Returns the TunnelDetail for the newly created tunnel.
    """
    output = _run_devtunnel("create", "--id", name)
    log.info("Created tunnel: %s", output.strip())
    return get_tunnel_detail(name)


def add_port(
    tunnel_id: str,
    port: int,
    protocol: str = "auto",
) -> dict[str, str]:
    """Add a port forwarding to a tunnel.

    Returns a dict with port details.
    """
    _run_devtunnel("port", "create", tunnel_id, "-p", str(port), "--protocol", protocol)
    # Re-fetch to get the generated URL
    detail = get_tunnel_detail(tunnel_id)
    if detail.port_entries:
        for entry in detail.port_entries:
            if entry.get("port") == str(port):
                return entry
    return {"port": str(port), "protocol": protocol}


def configure_access(tunnel_id: str, anonymous: bool = False) -> None:
    """Configure access control on a tunnel.

    Args:
        tunnel_id: The tunnel identifier.
        anonymous: If True, allow anonymous access (no login required).
    """
    if anonymous:
        _run_devtunnel("access", "create", tunnel_id, "--anonymous")
        log.info("Enabled anonymous access on tunnel %s", tunnel_id)
    else:
        log.info("Access control unchanged for tunnel %s", tunnel_id)


# ── Hosting (long-lived process) ────────────────────────────────


async def host_tunnel(
    tunnel_id: str,
    port: int | None = None,
    allow_anonymous: bool = False,
) -> asyncio.subprocess.Process:
    """Start hosting a tunnel as a background subprocess.

    The returned process must be stopped via :func:`stop_hosting` or by
    killing the process when it is no longer needed.
    """
    binary = _devtunnel_bin()
    if not binary:
        raise RuntimeError("devtunnel CLI is not available")

    cmd = [binary, "host", tunnel_id]
    if port is not None:
        cmd += ["--port-numbers", str(port)]
    if allow_anonymous:
        cmd.append("--allow-anonymous")

    log.info("Starting devtunnel host: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Give it a moment to start
    await asyncio.sleep(1)
    if proc.returncode is not None:
        stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
        raise RuntimeError(f"devtunnel host exited immediately: {stderr}")
    return proc


async def stop_hosting(proc: asyncio.subprocess.Process) -> None:
    """Stop a devtunnel host process gracefully."""
    if proc.returncode is not None:
        return  # already exited
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    log.info("Devtunnel host process stopped")


# ── Convenience ─────────────────────────────────────────────────


def get_or_create_tunnel(
    name: str = DEFAULT_TUNNEL_NAME,
    port: int = DEFAULT_PORT,
    anonymous: bool = False,
) -> str:
    """Ensure a tunnel exists with the given port, return its public URL.

    This is idempotent — safe to call repeatedly.

    Args:
        name: Tunnel name/ID.
        port: Port to forward.
        anonymous: Whether to allow anonymous access.

    Returns:
        The public HTTPS URL for the tunnel+port.

    Raises:
        RuntimeError: If devtunnel is not available or tunnel setup fails.
    """
    if not is_devtunnel_available():
        raise RuntimeError(
            "devtunnel CLI is not installed. "
            "Install from: https://aka.ms/devtunnels/cli"
        )

    # Check if tunnel already exists
    existing = list_tunnels()
    tunnel_exists = any(t.tunnel_id.startswith(name) for t in existing)

    if not tunnel_exists:
        log.info("Creating new tunnel: %s", name)
        create_tunnel(name)
        if anonymous:
            configure_access(name, anonymous=True)

    # Check if port is already configured
    url = get_tunnel_url(name, port)
    if not url:
        log.info("Adding port %d to tunnel %s", port, name)
        port_info = add_port(name, port)
        url = port_info.get("url")

    if not url:
        # Construct the URL from the tunnel ID pattern
        # The tunnel ID from devtunnel includes the cluster (e.g., "zpilot.aue")
        detail = get_tunnel_detail(name)
        tid = detail.tunnel_id
        # Typical pattern: https://<tunnelid-with-dots-replaced>-<port>.<cluster>.devtunnels.ms
        # We can't reliably construct this, so fetch it again
        if detail.port_entries:
            for entry in detail.port_entries:
                if entry.get("port") == str(port) and entry.get("url"):
                    url = entry["url"].rstrip("/")
                    break

    if not url:
        raise RuntimeError(
            f"Could not determine URL for tunnel {name} port {port}. "
            "Try running: devtunnel show " + name
        )

    return url
