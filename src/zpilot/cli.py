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
def web(host: str, port: int) -> None:
    """Launch the web dashboard."""
    ensure_config()
    from .web.app import run_web
    click.echo(f"🌐 zpilot web dashboard: http://localhost:{port}")
    run_web(host=host, port=port)


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


if __name__ == "__main__":
    main()
