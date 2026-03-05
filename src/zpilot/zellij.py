"""Async wrapper around the Zellij CLI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from .models import Pane, Session

ZELLIJ_BIN = os.environ.get("ZPILOT_ZELLIJ_BIN", "zellij")
DUMP_DIR = Path(tempfile.gettempdir()) / "zpilot" / "dumps"


async def _run(args: list[str], check: bool = True) -> str:
    """Run a zellij command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        ZELLIJ_BIN, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"zellij {' '.join(args)} failed (rc={proc.returncode}): "
            f"{stderr.decode().strip()}"
        )
    return stdout.decode()


async def _action(session: str | None, args: list[str], check: bool = True) -> str:
    """Run a zellij action, optionally targeting a session."""
    cmd = []
    if session:
        cmd.extend(["--session", session])
    cmd.append("action")
    cmd.extend(args)
    return await _run(cmd, check=check)


# ── Session operations ──────────────────────────────────────────────


async def list_sessions() -> list[Session]:
    """List all Zellij sessions."""
    raw = await _run(["list-sessions", "--no-formatting", "--short"], check=False)
    sessions = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "session_name [CURRENT]" or just "session_name"
        is_current = "[CURRENT]" in line or "(current)" in line.lower()
        name = re.split(r"\s+\[", line)[0].strip()
        if name:
            sessions.append(Session(name=name, is_current=is_current))
    return sessions


async def new_session(
    name: str,
    layout: str | None = None,
    cwd: str | None = None,
    detached: bool = True,
) -> str:
    """Create a new Zellij session. Returns session name."""
    args = ["--session", name]
    if layout:
        args.extend(["--layout", layout])
    if cwd:
        args.extend(["--new-session-with-layout", cwd])
    if detached:
        # Start detached by launching in background
        proc = await asyncio.create_subprocess_exec(
            ZELLIJ_BIN, *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            env={**os.environ, "ZELLIJ_AUTO_ATTACH": "false"},
            start_new_session=True,
        )
        # Give it a moment to start
        await asyncio.sleep(0.5)
    return name


async def kill_session(name: str) -> None:
    """Kill a Zellij session by name."""
    await _run(["kill-session", name])


async def delete_session(name: str) -> None:
    """Delete a dead Zellij session."""
    await _run(["delete-session", name], check=False)


# ── Pane operations ─────────────────────────────────────────────────


async def list_panes(session: str | None = None) -> list[Pane]:
    """List panes in a session. Parses `zellij action query-tab-names` and dump-layout."""
    # Use dump-layout for structured pane info
    raw = await _action(session, ["dump-layout"], check=False)
    panes = []
    # Parse layout output - format varies by version
    # Fallback: just return a basic list
    pane_id = 0
    for line in raw.strip().splitlines():
        if "pane" in line.lower():
            panes.append(Pane(pane_id=pane_id, name=f"pane-{pane_id}"))
            pane_id += 1
    return panes if panes else [Pane(pane_id=0, name="default")]


async def new_pane(
    session: str | None = None,
    name: str | None = None,
    command: str | None = None,
    direction: str | None = None,
    cwd: str | None = None,
    floating: bool = False,
) -> None:
    """Create a new pane."""
    args = ["new-pane"]
    if name:
        args.extend(["--name", name])
    if direction:
        args.extend(["--direction", direction])
    if cwd:
        args.extend(["--cwd", cwd])
    if floating:
        args.append("--floating")
    if command:
        args.append("--")
        args.extend(command.split())
    await _action(session, args)


async def close_pane(session: str | None = None) -> None:
    """Close the currently focused pane."""
    await _action(session, ["close-pane"])


async def focus_pane(session: str | None = None, direction: str = "right") -> None:
    """Focus pane in a direction (up/down/left/right)."""
    await _action(session, ["move-focus", direction])


async def write_to_pane(
    text: str,
    session: str | None = None,
) -> None:
    """Write text/keystrokes to the focused pane."""
    # write-chars sends literal text
    await _action(session, ["write-chars", text])


async def write_bytes(
    data: bytes,
    session: str | None = None,
) -> None:
    """Write raw bytes to the focused pane (for Enter, Ctrl-C, etc)."""
    # write sends raw bytes
    for byte in data:
        await _action(session, ["write", str(byte)])


async def send_enter(session: str | None = None) -> None:
    """Send Enter key to focused pane."""
    await write_bytes(b"\n", session)


async def send_ctrl_c(session: str | None = None) -> None:
    """Send Ctrl-C to focused pane."""
    await write_bytes(b"\x03", session)


async def dump_pane(
    session: str | None = None,
    full: bool = False,
) -> str:
    """Dump the screen content of the focused pane. Returns the text."""
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    dump_file = DUMP_DIR / f"dump-{session or 'current'}.txt"
    args = ["dump-screen", str(dump_file)]
    if full:
        args.append("--full")
    await _action(session, args, check=False)
    if dump_file.exists():
        content = dump_file.read_text()
        return content
    return ""


async def run_command_in_pane(
    command: str,
    session: str | None = None,
) -> None:
    """Type a command and press Enter in the focused pane."""
    await write_to_pane(command, session)
    await send_enter(session)


# ── Tab operations ──────────────────────────────────────────────────


async def query_tab_names(session: str | None = None) -> list[str]:
    """Get tab names for a session."""
    raw = await _action(session, ["query-tab-names"], check=False)
    return [t.strip() for t in raw.strip().splitlines() if t.strip()]


async def go_to_tab(session: str | None = None, index: int = 0) -> None:
    """Switch to a tab by index (1-based)."""
    await _action(session, ["go-to-tab", str(index)])


async def new_tab(session: str | None = None, name: str | None = None) -> None:
    """Create a new tab."""
    args = ["new-tab"]
    if name:
        args.extend(["--name", name])
    await _action(session, args)


# ── Utility ─────────────────────────────────────────────────────────


async def is_available() -> bool:
    """Check if Zellij is installed and accessible."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ZELLIJ_BIN, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except FileNotFoundError:
        return False
