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

    # Start daemon if not already running
    from .daemon import is_daemon_running as _daemon_running
    daemon_pid_file = PID_DIR / "daemon.pid"
    daemon_already = _daemon_running()
    if not daemon_already:
        daemon_cmd = [sys.executable, "-m", "zpilot.cli", "daemon", "start"]
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            stdout=open(PID_DIR / "daemon.log", "w"),
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
        stdout=open(PID_DIR / "web.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    web_pid_file.write_text(str(proc.pid))

    display_host = 'localhost' if host == '127.0.0.1' else host
    click.echo(f"⚡ zpilot is up!")
    click.echo(f"   Dashboard: {proto}://{display_host}:{port}")
    click.echo(f"   Web PID:   {proc.pid}")
    if not daemon_already:
        click.echo(f"   Daemon:    started")
    else:
        click.echo(f"   Daemon:    already running (PID {daemon_already})")
    click.echo(f"   Log:       {PID_DIR / 'web.log'}")
    click.echo(f"   Stop with: zpilot down")

    if open_browser:
        import webbrowser
        webbrowser.open(f"http://localhost:{port}")


@main.command()
def down() -> None:
    """Stop zpilot background services (web + daemon)."""
    import os
    import signal

    PID_DIR.mkdir(parents=True, exist_ok=True)
    stopped = []

    for name, pid_filename in [("web", "web.pid"), ("daemon", "zpilot.pid")]:
        pid_file = PID_DIR / pid_filename
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


if __name__ == "__main__":
    main()
