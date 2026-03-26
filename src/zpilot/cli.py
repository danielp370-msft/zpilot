"""CLI entry point for zpilot."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import click

from .config import ensure_config, load_config


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """zpilot — Mission control for AI coding sessions."""
    if ctx.invoked_subcommand is None:
        # Default: launch TUI dashboard
        ctx.invoke(dashboard)


@main.command()
def serve() -> None:
    """Start the MCP server (stdio transport)."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    from .mcp_server import serve as mcp_serve
    asyncio.run(mcp_serve())


@main.group(invoke_without_command=True)
@click.pass_context
def daemon(ctx: click.Context) -> None:
    """Manage the zpilot background daemon."""
    if ctx.invoked_subcommand is None:
        # Default: start the daemon in foreground (backward compatible)
        ctx.invoke(daemon_start, foreground=True)


@daemon.command("start")
@click.option("--poll-interval", type=float, default=None, help="Seconds between polls")
@click.option("--idle-threshold", type=float, default=None, help="Seconds to consider idle")
@click.option("--foreground", "-f", is_flag=True, default=False, help="Run in foreground (default when called directly)")
def daemon_start(poll_interval: float | None = None, idle_threshold: float | None = None, foreground: bool = False) -> None:
    """Start the daemon (foreground by default, or via systemd)."""
    from .daemon import is_daemon_running

    existing = is_daemon_running()
    if existing:
        click.echo(f"⚠️  Daemon already running (PID {existing})")
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    config = load_config()
    if poll_interval is not None:
        config.poll_interval = poll_interval
    if idle_threshold is not None:
        config.idle_threshold = idle_threshold

    from .daemon import run_daemon
    asyncio.run(run_daemon(config))


@daemon.command("stop")
def daemon_stop() -> None:
    """Stop the running daemon."""
    import signal as sig
    from .daemon import is_daemon_running, read_pid_file

    pid = is_daemon_running()
    if pid is None:
        click.echo("No daemon running.")
        return

    try:
        os.kill(pid, sig.SIGTERM)
        click.echo(f"✅ Sent SIGTERM to daemon (PID {pid})")
    except ProcessLookupError:
        click.echo("Daemon process not found (stale PID file).")
    except PermissionError:
        click.echo(f"❌ Permission denied to stop PID {pid}")


@daemon.command("status")
def daemon_status() -> None:
    """Check if the daemon is running."""
    from .daemon import is_daemon_running

    pid = is_daemon_running()
    if pid:
        click.echo(f"✅ Daemon running (PID {pid})")
    else:
        click.echo("⏹  Daemon not running")


@daemon.command("install")
def daemon_install() -> None:
    """Install systemd user unit for auto-start."""
    from .daemon import install_systemd_unit

    path = install_systemd_unit()
    click.echo(f"✅ Installed {path}")
    click.echo("Run: systemctl --user daemon-reload")
    click.echo("Run: zpilot daemon enable")


@daemon.command("uninstall")
def daemon_uninstall() -> None:
    """Remove systemd user unit."""
    from .daemon import uninstall_systemd_unit

    if uninstall_systemd_unit():
        click.echo("✅ Removed zpilot.service")
        click.echo("Run: systemctl --user daemon-reload")
    else:
        click.echo("No unit file found.")


@daemon.command("enable")
def daemon_enable() -> None:
    """Enable daemon auto-start via systemd."""
    import subprocess
    result = subprocess.run(
        ["systemctl", "--user", "enable", "zpilot.service"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        click.echo("✅ zpilot daemon enabled (starts on login)")
    else:
        click.echo(f"❌ {result.stderr.strip()}")


@daemon.command("disable")
def daemon_disable() -> None:
    """Disable daemon auto-start via systemd."""
    import subprocess
    result = subprocess.run(
        ["systemctl", "--user", "disable", "zpilot.service"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        click.echo("✅ zpilot daemon disabled")
    else:
        click.echo(f"❌ {result.stderr.strip()}")


@main.command()
def status() -> None:
    """One-shot status check of all Zellij sessions."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    async def _status() -> None:
        from . import zellij
        from .detector import PaneDetector

        config = load_config()

        if not await zellij.is_available():
            click.echo("❌ Zellij is not installed or not in PATH", err=True)
            sys.exit(1)

        sessions = await zellij.list_sessions()
        if not sessions:
            click.echo("No Zellij sessions found.")
            return

        detector = PaneDetector(config)
        state_icons = {
            "active": "⏳",
            "idle": "✅",
            "waiting": "🔔",
            "error": "❌",
            "exited": "🏁",
            "unknown": "❓",
        }

        for s in sessions:
            try:
                content = await zellij.dump_pane(session=s.name)
                state = detector.detect(s.name, "focused", content)
                idle = detector.get_idle_seconds(s.name, "focused")
                icon = state_icons.get(state.value, "❓")
                marker = " (current)" if s.is_current else ""
                last = ""
                lines = content.strip().splitlines()
                if lines:
                    last = lines[-1][:50]
                click.echo(f"  {icon} {s.name}{marker}  [{state.value}]  idle={idle:.0f}s  {last}")
            except Exception as e:
                click.echo(f"  ❓ {s.name}  [error: {e}]")

    asyncio.run(_status())


@main.command()
@click.argument("name")
@click.argument("command", required=False, default=None)
def new(name: str, command: str | None) -> None:
    """Create a new tracked Zellij session."""

    async def _new() -> None:
        from . import zellij

        if not await zellij.is_available():
            click.echo("❌ Zellij not found", err=True)
            sys.exit(1)

        await zellij.new_session(name)
        click.echo(f"✅ Created session '{name}'")
        if command:
            import asyncio as aio
            await aio.sleep(1)
            await zellij.run_command_in_pane(command, session=name)
            click.echo(f"   Running: {command}")

    asyncio.run(_new())


@main.command()
def dashboard() -> None:
    """Open the zpilot web dashboard.

    If already running, prints the URL.
    If not running, starts it in the background and prints the URL.
    """
    ensure_config()
    existing = _find_running_web()
    if existing:
        pid, port = existing
        click.echo(f"🌐 http://localhost:{port}")
        return

    # Auto-start
    import json
    import subprocess

    port = _find_free_port()
    pid_dir = _user_pid_dir()

    # Start daemon if needed
    from .daemon import is_daemon_running as _daemon_running
    if not _daemon_running():
        daemon_cmd = [sys.executable, "-m", "zpilot.cli", "daemon", "start"]
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            stdout=open(pid_dir / "daemon.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        (pid_dir / "daemon.pid").write_text(str(daemon_proc.pid))

    # Start web server (no SSL for localhost simplicity)
    cmd = [sys.executable, "-m", "zpilot.web.app", "--host", "127.0.0.1", "--port", str(port), "--no-ssl"]
    proc = subprocess.Popen(
        cmd,
        stdout=open(pid_dir / "web.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    state_file = pid_dir / "web.state"
    state_file.write_text(json.dumps({"pid": proc.pid, "port": port, "host": "127.0.0.1", "ssl": False}))
    (pid_dir / "web.pid").write_text(str(proc.pid))

    click.echo(f"🌐 http://localhost:{port}")
    click.echo(f"   (started in background, PID {proc.pid})")


@main.command()
def tui() -> None:
    """Launch the TUI dashboard (terminal UI)."""
    ensure_config()
    from .tui.dashboard import ZpilotApp
    app = ZpilotApp()
    app.run()


@main.command()
@click.option("--host", default="0.0.0.0", help="Bind address")
@click.option("--port", type=int, default=8095, help="Port number")
@click.option("--no-ssl", is_flag=True, help="Disable SSL")
def web(host: str, port: int, no_ssl: bool) -> None:
    """Launch the web dashboard (foreground)."""
    ensure_config()
    from .web.app import run_web
    proto = "http" if no_ssl else "https"
    click.echo(f"🌐 zpilot web dashboard: {proto}://localhost:{port}")
    run_web(host=host, port=port, ssl=not no_ssl)


def _user_pid_dir() -> __import__("pathlib").Path:
    """Per-user pid/state directory: /tmp/zpilot-<uid>."""
    from pathlib import Path
    d = Path(f"/tmp/zpilot-{os.getuid()}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _find_running_web() -> tuple[int, int] | None:
    """Return (pid, port) of an already-running zpilot web server for this user, or None."""
    pid_dir = _user_pid_dir()
    state_file = pid_dir / "web.state"
    if not state_file.exists():
        return None
    try:
        import json
        state = json.loads(state_file.read_text())
        pid = int(state["pid"])
        port = int(state["port"])
        os.kill(pid, 0)  # check alive
        return (pid, port)
    except (ProcessLookupError, ValueError, KeyError, json.JSONDecodeError):
        state_file.unlink(missing_ok=True)
        return None


def _find_free_port(preferred: int = 8095) -> int:
    """Return preferred port if free, otherwise pick a random free port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


PID_DIR = _user_pid_dir  # kept as callable for back-compat in down()


@main.command()
@click.option("--host", default="127.0.0.1", help="Bind address (default: localhost only)")
@click.option("--port", type=int, default=None, help="Port number (auto-assigned if omitted)")
@click.option("--no-ssl", is_flag=True, help="Disable SSL")
@click.option("--open", "open_browser", is_flag=True, help="Open browser after starting")
def up(host: str, port: int | None, no_ssl: bool, open_browser: bool) -> None:
    """Start zpilot services in the background.

    Launches the web dashboard as a background daemon with a pidfile.
    Each user gets their own instance on an auto-assigned port.
    Binds to 127.0.0.1 by default — use --host 0.0.0.0 for remote access.
    Uses HTTPS by default with an auto-generated self-signed certificate.
    """
    import json
    import subprocess

    proto = "http" if no_ssl else "https"
    pid_dir = _user_pid_dir()

    # Check if already running
    existing = _find_running_web()
    if existing:
        pid, running_port = existing
        click.echo(f"⚡ zpilot is already running (PID {pid})")
        click.echo(f"   🌐 {proto}://localhost:{running_port}")
        click.echo(f"   Stop with: zpilot down")
        return

    # Pick port
    if port is None:
        port = _find_free_port()

    # Start daemon if not already running
    from .daemon import is_daemon_running as _daemon_running
    daemon_pid_file = pid_dir / "daemon.pid"
    daemon_already = _daemon_running()
    if not daemon_already:
        daemon_cmd = [sys.executable, "-m", "zpilot.cli", "daemon", "start"]
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            stdout=open(pid_dir / "daemon.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        daemon_pid_file.write_text(str(daemon_proc.pid))

    # Start web server as a detached subprocess
    cmd = [sys.executable, "-m", "zpilot.web.app", "--host", host, "--port", str(port)]
    if no_ssl:
        cmd.append("--no-ssl")
    proc = subprocess.Popen(
        cmd,
        stdout=open(pid_dir / "web.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Save state with pid + port
    state_file = pid_dir / "web.state"
    state_file.write_text(json.dumps({"pid": proc.pid, "port": port, "host": host, "ssl": not no_ssl}))
    # Back-compat pid file
    (pid_dir / "web.pid").write_text(str(proc.pid))

    display_host = 'localhost' if host == '127.0.0.1' else host
    click.echo(f"⚡ zpilot is up!")
    click.echo(f"   🌐 {proto}://{display_host}:{port}")
    click.echo(f"   Web PID:   {proc.pid}")
    if not daemon_already:
        click.echo(f"   Daemon:    started")
    else:
        click.echo(f"   Daemon:    already running (PID {daemon_already})")
    click.echo(f"   Log:       {pid_dir / 'web.log'}")
    click.echo(f"   Stop with: zpilot down")

    if open_browser:
        import webbrowser
        webbrowser.open(f"{proto}://{display_host}:{port}")


@main.command()
def down() -> None:
    """Stop zpilot background services (web + daemon)."""
    import signal

    pid_dir = _user_pid_dir()
    stopped = []

    for name, pid_filename in [("web", "web.pid"), ("daemon", "zpilot.pid")]:
        pid_file = pid_dir / pid_filename
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            stopped.append(f"{name} (PID {pid})")
        except ProcessLookupError:
            pass  # already gone
        except ValueError:
            pass
        finally:
            pid_file.unlink(missing_ok=True)

    # Clean up state file too
    (pid_dir / "web.state").unlink(missing_ok=True)

    if stopped:
        click.echo(f"⚡ zpilot stopped: {', '.join(stopped)}")
    else:
        click.echo("zpilot is not running")


@main.command("notify-test")
def notify_test() -> None:
    """Send a test notification."""

    async def _test() -> None:
        config = load_config()
        from .notifications import create_adapter
        adapter = create_adapter(config)
        ok = await adapter.test()
        if ok:
            click.echo(f"✅ Notification sent via {config.notify_adapter}")
        else:
            click.echo(f"❌ Notification failed via {config.notify_adapter}", err=True)

    asyncio.run(_test())


@main.command()
def config() -> None:
    """Show current configuration."""
    ensure_config()
    cfg = load_config()
    click.echo(f"Config file: {ensure_config.__module__}")
    for field_name in cfg.__dataclass_fields__:
        click.echo(f"  {field_name}: {getattr(cfg, field_name)}")


@main.command()
def nodes() -> None:
    """List configured nodes."""
    from .nodes import load_nodes
    node_list = load_nodes()
    click.echo(f"Nodes ({len(node_list)}):")
    for n in node_list:
        host = n.host or "(local)"
        click.echo(f"  {n.name}  [{n.transport_type}]  {host}")


@main.command("serve-http")
@click.option("--host", default=None, help="Bind address (default: from config)")
@click.option("--port", type=int, default=None, help="Port number (default: from config)")
@click.option("--token", default=None, help="Auth token (overrides config)")
@click.option("--tunnel", is_flag=True, default=False, help="Also start a devtunnel to expose the server publicly")
@click.option("--tunnel-name", default="zpilot", help="Devtunnel name (default: zpilot)")
@click.option("--tunnel-anonymous", is_flag=True, default=False, help="Allow anonymous tunnel access (for testing)")
def serve_http_cmd(
    host: str | None,
    port: int | None,
    token: str | None,
    tunnel: bool,
    tunnel_name: str,
    tunnel_anonymous: bool,
) -> None:
    """Start the MCP server (HTTP transport for distributed zpilot).

    Use --tunnel to also create a devtunnel, giving a public HTTPS URL
    with TLS handled by devtunnel infrastructure (no self-signed certs needed).
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config()
    if host is not None:
        config.http_host = host
    if port is not None:
        config.http_port = port
    if token is not None:
        config.http_token = token

    if tunnel:
        from .devtunnel import get_or_create_tunnel, host_tunnel, is_devtunnel_available

        if not is_devtunnel_available():
            click.echo("❌ devtunnel CLI not found. Install from: https://aka.ms/devtunnels/cli", err=True)
            sys.exit(1)
        try:
            tunnel_port = port or config.http_port
            tunnel_url = get_or_create_tunnel(
                name=tunnel_name, port=tunnel_port, anonymous=tunnel_anonymous
            )
            click.echo(f"🔗 Devtunnel URL: {tunnel_url}")
            click.echo(f"   Use in nodes.toml:  url = \"{tunnel_url}\"")
            # Start hosting the tunnel in background
            click.echo("🚇 Starting devtunnel host...")
            import threading

            def _run_tunnel_host():
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(host_tunnel(tunnel_name, port=tunnel_port, allow_anonymous=tunnel_anonymous))
                except Exception as e:
                    logging.getLogger(__name__).warning("Devtunnel host error: %s", e)
                finally:
                    loop.close()

            tunnel_thread = threading.Thread(
                target=_run_tunnel_host, daemon=True
            )
            tunnel_thread.start()
            click.echo("✅ Devtunnel hosting started")
        except RuntimeError as e:
            click.echo(f"⚠️  Devtunnel setup failed: {e}", err=True)
            click.echo("   Continuing without tunnel...", err=True)

    from .mcp_http import serve_http
    asyncio.run(serve_http(config))


@main.command("token-gen")
def token_gen() -> None:
    """Generate a secure auth token for zpilot HTTP server."""
    import secrets
    token = secrets.token_urlsafe(32)
    click.echo(token)


# ── Mesh invite / join ──────────────────────────────────────────

@main.command()
@click.option("--url", default=None,
              help="URL where this node is reachable (e.g. https://host:8222)")
@click.option("--name", "node_name", default=None,
              help="Name for the invited node (suggested)")
@click.option("--expires", default=60, type=int,
              help="Invite validity in minutes (default: 60)")
def invite(url: str | None, node_name: str | None, expires: int) -> None:
    """Generate a mesh invite for a new node to join.

    Any zpilot can invite. The invite token contains this node's
    URL and a one-time secret. The joining node uses it to connect.

    Example:
      zpilot invite --url https://myhost:8222
      zpilot invite --url https://myhost:8222 --name build-server
    """
    from .mesh import generate_invite
    from .config import load_config
    import socket as _socket

    cfg = load_config()

    # Figure out our reachable URL
    if not url:
        host = cfg.http_host
        port = cfg.http_port
        scheme = "https" if cfg.http_tls else "http"
        if host in ("0.0.0.0", "127.0.0.1", "localhost"):
            # Try to use hostname for a more useful URL
            hostname = _socket.gethostname()
            click.echo(f"⚠ Server listens on {host} — using hostname '{hostname}'")
            click.echo(f"  Pass --url if the joiner needs a different address\n")
            host = hostname
        url = f"{scheme}://{host}:{port}"

    inviter_name = _socket.gethostname()

    token, inv = generate_invite(
        inviter_url=url,
        inviter_name=inviter_name,
        expires_minutes=expires,
        suggested_name=node_name or "",
    )

    click.echo(f"✉ Mesh invite generated")
    click.echo(f"  Inviter:  {inviter_name} ({url})")
    if node_name:
        click.echo(f"  For node: {node_name}")
    click.echo(f"  Expires:  {expires} minutes\n")
    click.echo(f"Run this on the new node:\n")
    click.echo(f"  zpilot join --token {token}\n")
    click.echo(f"Or if zpilot isn't installed yet:\n")
    click.echo(f"  pip install -e /path/to/zpilot && zpilot join --token {token}")


@main.command()
@click.option("--token", required=True, help="Invite token from zpilot invite")
@click.option("--name", "my_name", default=None,
              help="Name for this node (default: hostname)")
@click.option("--url", "my_url", default=None,
              help="URL where this node will be reachable")
@click.option("--port", default=None, type=int,
              help="Port for local zpilot server (default: from config or 8222)")
def join(token: str, my_name: str | None, my_url: str | None, port: int | None) -> None:
    """Join a zpilot mesh using an invite token.

    Decodes the invite, contacts the inviting node, exchanges
    credentials, and adds both sides to each other's config.

    Example:
      zpilot join --token <token>
      zpilot join --token <token> --name my-server --url https://myhost:9000
    """
    import socket as _socket

    from .mesh import (
        decode_invite,
        add_node_to_config,
        update_node_in_config,
        build_join_request,
        node_exists,
    )
    from .config import load_config

    # Decode the invite
    try:
        inv = decode_invite(token)
    except Exception as e:
        click.echo(f"✗ Invalid invite token: {e}", err=True)
        sys.exit(1)

    inviter_url = inv["url"]
    inviter_name = inv["inviter"]
    invite_secret = inv["secret"]
    suggested_name = inv.get("suggested_name", "")
    expires = inv["expires"]

    import time
    if time.time() > expires:
        click.echo("✗ Invite has expired. Ask for a new one.", err=True)
        sys.exit(1)

    click.echo(f"🔗 Joining mesh via {inviter_name} ({inviter_url})")
    if suggested_name:
        click.echo(f"  Suggested name: {suggested_name}")

    # Determine our identity
    if not my_name:
        my_name = suggested_name or _socket.gethostname()

    cfg = load_config()
    my_port = port or cfg.http_port
    my_scheme = "https" if cfg.http_tls else "http"

    # Generate our bearer token if not configured
    my_token = cfg.http_token
    if not my_token:
        import secrets as _secrets
        my_token = _secrets.token_urlsafe(32)
        click.echo(f"  Generated auth token (save to config.toml [http] token):")
        click.echo(f"    {my_token}")

    # Figure out our URL
    if not my_url:
        my_host = cfg.http_host
        if my_host in ("0.0.0.0", "127.0.0.1", "localhost"):
            my_host = _socket.gethostname()
        my_url = f"{my_scheme}://{my_host}:{my_port}"

    click.echo(f"  This node: {my_name} ({my_url})")

    # Contact the inviter
    click.echo(f"\n  Contacting {inviter_name}...")

    import httpx

    join_payload = build_join_request(
        invite_secret=invite_secret,
        node_name=my_name,
        node_url=my_url,
        node_token=my_token,
        labels={"hostname": _socket.gethostname()},
    )

    try:
        resp = httpx.post(
            f"{inviter_url.rstrip('/')}/api/mesh/join",
            json=join_payload,
            verify=False,
            timeout=15.0,
        )
    except httpx.ConnectError as e:
        click.echo(f"\n✗ Cannot reach inviter at {inviter_url}: {e}", err=True)
        click.echo("  Is the zpilot server running on that node?", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n✗ Connection failed: {e}", err=True)
        sys.exit(1)

    if resp.status_code != 200:
        try:
            err = resp.json().get("error", resp.text)
        except Exception:
            err = resp.text
        click.echo(f"\n✗ Join rejected ({resp.status_code}): {err}", err=True)
        sys.exit(1)

    data = resp.json()
    if not data.get("ok"):
        click.echo(f"\n✗ Join failed: {data.get('error', 'unknown')}", err=True)
        sys.exit(1)

    # Add the inviter to our nodes.toml
    inviter_info = data["inviter"]
    inv_name = inviter_info["name"]
    inv_url = inviter_info["url"]
    inv_token = inviter_info["token"]

    click.echo(f"  ✓ Accepted by {inv_name}")

    if node_exists(inv_name):
        click.echo(f"  Updating existing node '{inv_name}'")
        update_node_in_config(inv_name, inv_url, inv_token, verify_ssl=False)
    else:
        add_node_to_config(inv_name, inv_url, inv_token, verify_ssl=False)
        click.echo(f"  Added {inv_name} → nodes.toml")

    # Add any peers the inviter told us about
    peers = data.get("peers", [])
    for peer in peers:
        pname = peer["name"]
        if pname == my_name:
            continue  # skip self
        if node_exists(pname):
            continue  # already known
        try:
            add_node_to_config(
                pname, peer["url"], peer["token"],
                labels=peer.get("labels", {}),
                verify_ssl=False,
            )
            click.echo(f"  Added peer {pname} → nodes.toml")
        except Exception as e:
            click.echo(f"  ⚠ Could not add peer {pname}: {e}")

    click.echo(f"\n✓ Joined mesh! {my_name} ↔ {inv_name}")
    click.echo(f"  {data.get('message', '')}")
    click.echo(f"\n  Start server: zpilot serve-http --port {my_port}")
    click.echo(f"  Check fleet:  zpilot fleet")


@main.command()
def fleet() -> None:
    """One-shot fleet health check across all nodes."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    async def _fleet() -> None:
        from .nodes import NodeRegistry, load_nodes
        from .monitor import Monitor
        from .events import EventBus

        cfg = load_config()
        reg = NodeRegistry(load_nodes())
        bus = EventBus(cfg.events_file)
        mon = Monitor(reg, cfg, bus)
        status = await mon.poll_all()

        click.echo(status.summary())
        for nh in status.nodes:
            icon = "●" if nh.state.value == "online" else "○"
            line = f"  {icon} {nh.name}: {nh.state.value}"
            if nh.error:
                line += f" ({nh.error})"
            if nh.sessions:
                line += f" — {nh.total_sessions} sessions ({nh.busy_count} busy)"
            click.echo(line)

        stuck = mon.stuck_sessions()
        if stuck:
            click.echo(f"\n⚠ {len(stuck)} stuck session(s):")
            for s in stuck:
                click.echo(f"  {s.node}:{s.session} idle {s.idle_seconds:.0f}s")

    asyncio.run(_fleet())


@main.command("install-zellij-plugin")
@click.option("--source", default=None, type=click.Path(exists=True),
              help="Path to zpilot_zellij_plugin.wasm file")
@click.option("--config", "update_config", is_flag=True, default=False,
              help="Update Zellij config.kdl to load the plugin")
def install_zellij_plugin(source: str | None, update_config: bool) -> None:
    """Install the zpilot Zellij WASM plugin."""
    import shutil
    from pathlib import Path

    wasm_name = "zpilot_zellij_plugin.wasm"

    # Resolve source WASM file
    if source:
        wasm_src = Path(source)
    else:
        # Look in the zpilot package directory first
        pkg_dir = Path(__file__).parent
        wasm_src = pkg_dir / wasm_name
        if not wasm_src.exists():
            # Fall back to project build directory
            wasm_src = (
                pkg_dir.parent.parent
                / "zpilot-zellij-plugin"
                / "target"
                / "wasm32-wasip1"
                / "release"
                / wasm_name
            )

    if not wasm_src.exists():
        click.echo(f"❌ WASM plugin not found: {wasm_src}", err=True)
        click.echo("  Build it first or use --source to specify the path.", err=True)
        sys.exit(1)

    # Copy to Zellij plugin directory
    plugin_dir = Path.home() / ".config" / "zellij" / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    dest = plugin_dir / wasm_name
    shutil.copy2(str(wasm_src), str(dest))
    click.echo(f"✅ Installed {wasm_name} → {dest}")

    # Optionally update Zellij config.kdl
    if update_config:
        config_path = Path.home() / ".config" / "zellij" / "config.kdl"
        plugin_block = (
            '\nplugins {\n'
            f'    zpilot location="file:{dest}"\n'
            '}\n'
        )
        if config_path.exists():
            content = config_path.read_text()
            if "zpilot" in content:
                click.echo("ℹ️  Zellij config already references zpilot plugin")
            else:
                config_path.write_text(content + plugin_block)
                click.echo(f"✅ Updated {config_path}")
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(plugin_block)
            click.echo(f"✅ Created {config_path}")


@main.command()
@click.argument("node_name")
def ping(node_name: str) -> None:
    """Ping a specific node to check connectivity."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    async def _ping() -> None:
        from .nodes import NodeRegistry, load_nodes
        reg = NodeRegistry(load_nodes())
        node = reg.get(node_name)
        try:
            alive = await node.transport.is_alive()
            icon = "✓" if alive else "✗"
            click.echo(f"{icon} {node.name}: {'reachable' if alive else 'unreachable'}")
        except Exception as e:
            click.echo(f"✗ {node.name}: {e}")

    asyncio.run(_ping())


# ── Devtunnel commands ──────────────────────────────────────────


@main.command("tunnel-up")
@click.option("--port", type=int, default=8222, help="Local port to expose (default: 8222)")
@click.option("--name", default="zpilot", help="Tunnel name (default: zpilot)")
@click.option("--anonymous", is_flag=True, default=False, help="Allow anonymous access")
@click.option("--host-tunnel", "do_host", is_flag=True, default=False, help="Also start hosting (background process)")
def tunnel_up(port: int, name: str, anonymous: bool, do_host: bool) -> None:
    """Start a devtunnel to expose zpilot HTTP server.

    Creates (or reuses) a devtunnel with the given name and port,
    prints the public URL, and optionally starts hosting.
    Requires the devtunnel CLI: https://aka.ms/devtunnels/cli
    """
    from .devtunnel import get_or_create_tunnel, host_tunnel, is_devtunnel_available

    if not is_devtunnel_available():
        click.echo("❌ devtunnel CLI not found.", err=True)
        click.echo("   Install from: https://aka.ms/devtunnels/cli", err=True)
        sys.exit(1)

    try:
        url = get_or_create_tunnel(name=name, port=port, anonymous=anonymous)
        click.echo(f"🔗 Tunnel URL: {url}")
        click.echo(f"   Tunnel:     {name}")
        click.echo(f"   Port:       {port}")
        if anonymous:
            click.echo(f"   Access:     anonymous")
        click.echo()
        click.echo(f"   Add to nodes.toml:")
        click.echo(f'   [nodes.remote]')
        click.echo(f'   transport = "mcp"')
        click.echo(f'   url = "{url}"')
        click.echo(f'   token = "<your-shared-secret>"')
    except RuntimeError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)

    if do_host:
        click.echo()
        click.echo("🚀 Starting tunnel host (Ctrl+C to stop)...")

        async def _host() -> None:
            proc = await host_tunnel(name, port=port, allow_anonymous=anonymous)
            try:
                await proc.wait()
            except asyncio.CancelledError:
                proc.terminate()
                await proc.wait()

        try:
            asyncio.run(_host())
        except KeyboardInterrupt:
            click.echo("\n⏹  Tunnel host stopped.")


@main.command("tunnel-down")
@click.option("--name", default="zpilot", help="Tunnel name (default: zpilot)")
def tunnel_down(name: str) -> None:
    """Stop devtunnel hosting.

    Note: This deletes the tunnel's port forwarding session.
    The tunnel itself persists and can be reused with tunnel-up.
    """
    from .devtunnel import is_devtunnel_available

    if not is_devtunnel_available():
        click.echo("❌ devtunnel CLI not found.", err=True)
        sys.exit(1)

    import subprocess
    import shutil

    binary = shutil.which("devtunnel")
    result = subprocess.run(
        [binary, "unhost", name],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        click.echo(f"⏹  Tunnel '{name}' hosting stopped.")
    else:
        # devtunnel unhost may not exist in all versions; try kill approach
        click.echo(f"ℹ️  {result.stderr.strip() or 'No active hosting session found.'}")


@main.command("tunnel-status")
def tunnel_status() -> None:
    """Show devtunnel status and URL.

    Lists configured tunnels and their public URLs.
    Requires the devtunnel CLI: https://aka.ms/devtunnels/cli
    """
    from .devtunnel import get_tunnel_detail, is_devtunnel_available, list_tunnels

    if not is_devtunnel_available():
        click.echo("❌ devtunnel CLI not found.", err=True)
        click.echo("   Install from: https://aka.ms/devtunnels/cli", err=True)
        sys.exit(1)

    tunnels = list_tunnels()
    if not tunnels:
        click.echo("No tunnels found. Create one with: zpilot tunnel-up")
        return

    for t in tunnels:
        try:
            detail = get_tunnel_detail(t.tunnel_id)
            click.echo(f"🔗 {detail.tunnel_id}")
            if detail.access_control:
                click.echo(f"   Access:      {detail.access_control}")
            click.echo(f"   Connections: {detail.host_connections} host, {detail.client_connections} client")
            click.echo(f"   Expiration:  {detail.expiration}")
            if detail.port_entries:
                for pe in detail.port_entries:
                    url = pe.get("url", "")
                    click.echo(f"   Port {pe['port']}: {url}")
            else:
                click.echo("   No ports configured")
            click.echo()
        except RuntimeError as e:
            click.echo(f"  ⚠️  {t.tunnel_id}: {e}")


if __name__ == "__main__":
    main()
