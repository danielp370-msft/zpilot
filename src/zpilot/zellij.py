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
LOG_DIR = Path(tempfile.gettempdir()) / "zpilot" / "logs"
FIFO_DIR = Path(tempfile.gettempdir()) / "zpilot" / "fifos"
SHELL_WRAPPER = Path(__file__).parent / "shell_wrapper.py"


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
    log: bool = True,
    command: str | None = None,
) -> str:
    """Create a new Zellij session with monitored shell wrapper.

    The wrapper provides:
      - Output logging to /tmp/zpilot/logs/<name>--main.log
      - Command injection via /tmp/zpilot/fifos/<name>.fifo
    """
    args = ["--session", name]
    if layout:
        args.extend(["--layout", layout])
    if cwd:
        args.extend(["--new-session-with-layout", cwd])

    if detached:
        proc = await asyncio.create_subprocess_exec(
            ZELLIJ_BIN, *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            env={**os.environ, "ZELLIJ_AUTO_ATTACH": "false"},
            start_new_session=True,
        )
        await asyncio.sleep(1)

    if log and not layout:
        # Close the "About Zellij" floating pane that steals focus
        await _action(name, ["close-pane"], check=False)
        await asyncio.sleep(0.3)

        # Launch the monitored shell wrapper in a new pane (takes focus)
        wrapper_cmd = ["python3", str(SHELL_WRAPPER), name]
        if command:
            wrapper_cmd.append(command)
        await _run(
            ["--session", name, "run", "--"] + wrapper_cmd,
            check=False,
        )
        await asyncio.sleep(1)

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
    log: bool = True,
) -> str | None:
    """Create a new pane. If log=True, wraps command with `script` for output capture.

    Returns the log file path if logging is enabled.
    """
    log_file = None
    actual_command = command

    if log and name:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        session_part = session or "default"
        log_file = str(LOG_DIR / f"{session_part}--{name}.log")
        # Wrap command with `script -f` for real-time output logging
        if command:
            actual_command = f"script -f -q {log_file} -c '{command}'"
        else:
            actual_command = f"script -f -q {log_file}"

    args = ["new-pane"]
    if name:
        args.extend(["--name", name])
    if direction:
        args.extend(["--direction", direction])
    if cwd:
        args.extend(["--cwd", cwd])
    if floating:
        args.append("--floating")
    if actual_command:
        args.append("--")
        args.extend(["bash", "-c", actual_command])
    await _action(session, args)
    return log_file


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
    """Write text to the session's monitored shell.

    Uses FIFO injection (works headlessly) with fallback to action write-chars.
    """
    if session:
        fifo = FIFO_DIR / f"{session}.fifo"
        if fifo.exists():
            try:
                fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, text.encode())
                os.close(fd)
                return
            except OSError:
                pass  # FIFO not ready, fall back
    # Fallback: write-chars (only works with attached client)
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
    if session:
        fifo = FIFO_DIR / f"{session}.fifo"
        if fifo.exists():
            try:
                fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, b"\n")
                os.close(fd)
                return
            except OSError:
                pass
    await write_bytes(b"\n", session)


async def send_ctrl_c(session: str | None = None) -> None:
    """Send Ctrl-C to focused pane."""
    await write_bytes(b"\x03", session)


async def dump_pane(
    session: str | None = None,
    pane_name: str | None = None,
    full: bool = False,
    tail_lines: int = 50,
) -> str:
    """Read pane content. Tries log file first (works headless), falls back to dump-screen.

    Args:
        session: Session name
        pane_name: Pane name (used to find log file)
        full: Include full scrollback
        tail_lines: Number of lines to return from the end
    """
    # Strategy 1: Read from log file (works headless)
    if pane_name:
        session_part = session or "default"
        log_file = LOG_DIR / f"{session_part}--{pane_name}.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            content = log_file.read_text(errors="replace")
            if not full:
                lines = content.splitlines()
                content = "\n".join(lines[-tail_lines:])
            return content

    # Strategy 2: Try all log files for this session
    if session:
        pattern = f"{session}--*.log"
        logs = sorted(LOG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            content = logs[0].read_text(errors="replace")
            if not full:
                lines = content.splitlines()
                content = "\n".join(lines[-tail_lines:])
            return content

    # Strategy 3: dump-screen (requires attached client)
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    dump_file = DUMP_DIR / f"dump-{session or 'current'}.txt"
    args = ["dump-screen", str(dump_file)]
    if full:
        args.append("--full")
    await _action(session, args, check=False)
    if dump_file.exists() and dump_file.stat().st_size > 0:
        return dump_file.read_text()

    # Strategy 4: Run a command inside the session to capture state
    if session:
        marker = f"__zpilot_probe_{os.getpid()}__"
        probe_file = DUMP_DIR / f"probe-{session}.txt"
        await _run(
            ["--session", session, "run", "--floating", "--close-on-exit",
             "--", "bash", "-c", f"echo {marker} > {probe_file}"],
            check=False,
        )
        await asyncio.sleep(0.5)
        if probe_file.exists():
            content = probe_file.read_text()
            probe_file.unlink(missing_ok=True)
            if marker in content:
                return "(session alive, but no screen content available — use named panes for monitoring)"

    return ""


async def run_command_in_pane(
    command: str,
    session: str | None = None,
) -> None:
    """Type a command and press Enter in the focused pane."""
    await write_to_pane(command, session)
    await send_enter(session)


async def run_in_session(
    command: str,
    session: str,
    capture: bool = False,
    timeout: float = 10.0,
) -> str:
    """Run a command inside a session using `zellij run`. Works headless.

    If capture=True, writes output to a temp file and returns it.
    """
    if capture:
        out_file = DUMP_DIR / f"run-{session}-{os.getpid()}.txt"
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        wrapped = f"bash -c '{command} > {out_file} 2>&1'"
        await _run(
            ["--session", session, "run", "--floating", "--close-on-exit",
             "--", "bash", "-c", f"{command} > {out_file} 2>&1"],
            check=False,
        )
        await asyncio.sleep(min(timeout, 2.0))
        if out_file.exists():
            result = out_file.read_text()
            out_file.unlink(missing_ok=True)
            return result
        return ""
    else:
        await _run(
            ["--session", session, "run", "--floating", "--close-on-exit",
             "--", "bash", "-c", command],
            check=False,
        )
        return ""


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
