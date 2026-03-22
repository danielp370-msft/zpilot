"""CLI entry point for zpilot."""

from __future__ import annotations

import asyncio
import logging
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


@main.command()
@click.option("--poll-interval", type=float, default=None, help="Seconds between polls")
@click.option("--idle-threshold", type=float, default=None, help="Seconds to consider idle")
def daemon(poll_interval: float | None, idle_threshold: float | None) -> None:
    """Start the background session watcher daemon."""
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
    """Launch the TUI dashboard."""
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


PID_DIR = __import__("pathlib").Path("/tmp/zpilot")


@main.command()
@click.option("--host", default="127.0.0.1", help="Bind address (default: localhost only)")
@click.option("--port", type=int, default=8095, help="Port number")
@click.option("--no-ssl", is_flag=True, help="Disable SSL")
@click.option("--open", "open_browser", is_flag=True, help="Open browser after starting")
def up(host: str, port: int, no_ssl: bool, open_browser: bool) -> None:
    """Start zpilot services in the background.

    Launches the web dashboard as a background daemon with a pidfile.
    Binds to 127.0.0.1 by default — use --host 0.0.0.0 for remote access.
    Uses HTTPS by default with an auto-generated self-signed certificate.
    """
    import os
    import subprocess

    PID_DIR.mkdir(parents=True, exist_ok=True)
    web_pid_file = PID_DIR / "web.pid"
    proto = "http" if no_ssl else "https"

    # Check if already running
    if web_pid_file.exists():
        try:
            pid = int(web_pid_file.read_text().strip())
            os.kill(pid, 0)  # check if alive
            click.echo(f"⚡ zpilot is already running (PID {pid})")
            click.echo(f"   Dashboard: {proto}://localhost:{port}")
            click.echo(f"   Stop with: zpilot down")
            return
        except (ProcessLookupError, ValueError):
            web_pid_file.unlink(missing_ok=True)

    # Start web server as a detached subprocess
    cmd = [sys.executable, "-m", "zpilot.web.app", "--host", host, "--port", str(port)]
    if no_ssl:
        cmd.append("--no-ssl")
    proc = subprocess.Popen(
        cmd,
        stdout=open(PID_DIR / "web.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    web_pid_file.write_text(str(proc.pid))

    display_host = 'localhost' if host == '127.0.0.1' else host
    click.echo(f"⚡ zpilot is up!")
    click.echo(f"   Dashboard: {proto}://{display_host}:{port}")
    click.echo(f"   PID:       {proc.pid}")
    click.echo(f"   Log:       {PID_DIR / 'web.log'}")
    click.echo(f"   Stop with: zpilot down")

    if open_browser:
        import webbrowser
        webbrowser.open(f"http://localhost:{port}")


@main.command()
def down() -> None:
    """Stop zpilot background services."""
    import os
    import signal

    PID_DIR.mkdir(parents=True, exist_ok=True)
    web_pid_file = PID_DIR / "web.pid"

    if not web_pid_file.exists():
        click.echo("zpilot is not running (no pidfile found)")
        return

    try:
        pid = int(web_pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        click.echo(f"⚡ zpilot stopped (PID {pid})")
    except ProcessLookupError:
        click.echo("zpilot was not running (stale pidfile)")
    except ValueError:
        click.echo("Invalid pidfile")
    finally:
        web_pid_file.unlink(missing_ok=True)


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
def serve_http_cmd(host: str | None, port: int | None, token: str | None) -> None:
    """Start the MCP server (HTTP transport for distributed zpilot)."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = load_config()
    if host is not None:
        config.http_host = host
    if port is not None:
        config.http_port = port
    if token is not None:
        config.http_token = token

    from .mcp_http import serve_http
    asyncio.run(serve_http(config))


@main.command("token-gen")
def token_gen() -> None:
    """Generate a secure auth token for zpilot HTTP server."""
    import secrets
    token = secrets.token_urlsafe(32)
    click.echo(token)


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


if __name__ == "__main__":
    main()
