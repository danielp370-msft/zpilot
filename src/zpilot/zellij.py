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


def is_managed(name: str) -> bool:
    """Check if a session has zpilot shell_wrapper (FIFO + log) active."""
    fifo = FIFO_DIR / f"{name}.fifo"
    logs = list(LOG_DIR.glob(f"{name}--*.log"))
    return fifo.exists() and len(logs) > 0


async def adopt_session(name: str) -> bool:
    """Adopt a plain Zellij session by injecting the shell_wrapper.

    Uses ``zellij run --in-place`` to replace the current pane command
    with the shell_wrapper, which sets up FIFO-based input and log-based
    output capture.  When the wrapper exits, the original pane is restored.

    Returns True if adoption was initiated, False if already managed.
    """
    if is_managed(name):
        return False
    wrapper_cmd = ["python3", str(SHELL_WRAPPER), name]
    await _run(
        ["--session", name, "run", "--in-place", "--"] + wrapper_cmd,
        check=False,
    )
    # Wait for shell_wrapper to create the FIFO
    for _ in range(10):
        await asyncio.sleep(0.5)
        if is_managed(name):
            return True
    return False


async def list_sessions() -> list[Session]:
    """List all Zellij sessions with managed status."""
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
            sessions.append(Session(
                name=name,
                is_current=is_current,
                managed=is_managed(name),
            ))
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
        import shlex
        if command:
            actual_command = f"script -f -q {shlex.quote(log_file)} -c {shlex.quote(command)}"
        else:
            actual_command = f"script -f -q {shlex.quote(log_file)}"

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


async def resize_pane(cols: int, rows: int, session: str | None = None) -> bool:
    """Resize the PTY in the shell wrapper via the FIFO control channel."""
    if session:
        fifo = FIFO_DIR / f"{session}.fifo"
        if fifo.exists():
            try:
                fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, f"\x00RESIZE:{cols},{rows}\n".encode())
                os.close(fd)
                return True
            except OSError:
                pass
    return False


async def write_raw_input(
    text: str,
    session: str | None = None,
) -> None:
    """Write raw terminal input (from xterm.js) including control chars.

    Unlike write_to_pane, this properly handles backspace, arrow keys, and
    other escape sequences by using FIFO (raw bytes) or per-byte write fallback.
    """
    data = text.encode("utf-8", errors="replace")
    # FIFO path: handles everything (control chars, escape sequences, printable)
    if session:
        fifo = FIFO_DIR / f"{session}.fifo"
        if fifo.exists():
            try:
                fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, data)
                os.close(fd)
                return
            except OSError:
                pass
    # Fallback: check if data contains control chars or escape sequences
    has_control = any(b < 0x20 or b == 0x7f for b in data) or b'\x1b' in data
    if has_control:
        await write_bytes(data, session)
    else:
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


# Escape sequence map for named special keys
SPECIAL_KEYS: dict[str, bytes] = {
    "enter": b"\n",
    "tab": b"\t",
    "escape": b"\x1b",
    "backspace": b"\x7f",
    "ctrl_c": b"\x03",
    "ctrl_d": b"\x04",
    "ctrl_z": b"\x1a",
    "ctrl_l": b"\x0c",
    "ctrl_a": b"\x01",
    "ctrl_e": b"\x05",
    "ctrl_r": b"\x12",
    "ctrl_u": b"\x15",
    "ctrl_w": b"\x17",
    "arrow_up": b"\x1b[A",
    "arrow_down": b"\x1b[B",
    "arrow_right": b"\x1b[C",
    "arrow_left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "page_up": b"\x1b[5~",
    "page_down": b"\x1b[6~",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}


async def send_special_key(
    key_name: str,
    session: str | None = None,
) -> bool:
    """Send a named special key (arrow_up, ctrl_c, f1, etc.).

    Returns True if key was recognized, False otherwise.
    """
    key_bytes = SPECIAL_KEYS.get(key_name.lower())
    if key_bytes is None:
        return False
    # Prefer FIFO for speed
    if session:
        fifo = FIFO_DIR / f"{session}.fifo"
        if fifo.exists():
            try:
                fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, key_bytes)
                os.close(fd)
                return True
            except OSError:
                pass
    await write_bytes(key_bytes, session)
    return True


async def dump_screen_rendered(
    session: str,
    pane_name: str | None = None,
    cols: int = 200,
    rows: int = 50,
) -> str:
    """Read pane log file and render through pyte terminal emulator.

    Returns the rendered screen state (what you'd see on screen) rather
    than raw PTY output.  This avoids cursor-movement artifacts from
    bash readline (up-arrow history) that cause scroll jumps in the web UI.
    """
    import pyte

    # Find the log file
    raw = ""
    if pane_name:
        log_file = LOG_DIR / f"{session}--{pane_name}.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            raw = log_file.read_bytes().decode("utf-8", errors="replace")
    if not raw:
        pattern = f"{session}--*.log"
        logs = sorted(LOG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            raw = logs[0].read_bytes().decode("utf-8", errors="replace")
    if not raw:
        # Fallback: check for {session}.log (non-pane log files)
        log_file = LOG_DIR / f"{session}.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            raw = log_file.read_bytes().decode("utf-8", errors="replace")
    if not raw:
        return ""

    # Only feed the tail of the log to keep rendering fast.
    # 200KB is enough to fill any reasonable screen several times over.
    max_bytes = 200_000
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]

    # Render through pyte
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(raw)

    # Build output with ANSI colors from pyte buffer
    lines = []
    for row_idx in range(rows):
        line_parts: list[str] = []
        prev_fg = "default"
        prev_bg = "default"
        prev_bold = False
        prev_underline = False
        prev_reverse = False

        # Walk columns, find last non-space to avoid trailing padding
        row_buf = screen.buffer[row_idx]
        last_col = -1
        for c in range(cols - 1, -1, -1):
            if row_buf[c].data.strip():
                last_col = c
                break

        for col in range(last_col + 1):
            char = row_buf[col]
            ch = char.data if char.data else " "
            fg, bg = char.fg, char.bg
            bold = char.bold
            underline = getattr(char, "underscore", False)
            reverse = char.reverse

            # Emit SGR only when attributes change
            if fg != prev_fg or bg != prev_bg or bold != prev_bold or underline != prev_underline or reverse != prev_reverse:
                codes: list[str] = ["0"]  # reset first
                if bold:
                    codes.append("1")
                if underline:
                    codes.append("4")
                if reverse:
                    codes.append("7")
                if fg != "default":
                    codes.append(_pyte_color_to_sgr(fg, is_bg=False))
                if bg != "default":
                    codes.append(_pyte_color_to_sgr(bg, is_bg=True))
                line_parts.append(f"\x1b[{';'.join(codes)}m")
                prev_fg, prev_bg = fg, bg
                prev_bold, prev_underline, prev_reverse = bold, underline, reverse

            line_parts.append(ch)

        # Reset at end of line if styling was active
        if prev_fg != "default" or prev_bg != "default" or prev_bold:
            line_parts.append("\x1b[0m")

        lines.append("".join(line_parts))

    # Keep all rows — trailing blank lines represent the actual terminal
    # geometry.  Stripping them causes content to cluster at the top when
    # the web UI clears the screen and writes from home position.

    # Append cursor positioning so xterm.js places the cursor correctly
    cursor_row = screen.cursor.y
    cursor_col = screen.cursor.x
    result = "\n".join(lines)
    # ANSI cursor position is 1-based: \x1b[row;colH
    result += f"\x1b[{cursor_row + 1};{cursor_col + 1}H"

    return result


# ── ANSI color helpers for pyte buffer ──────────────────────────

_NAMED_FG = {
    "black": "30", "red": "31", "green": "32", "brown": "33",
    "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
    "white": "37", "default": "39",
}
_NAMED_BG = {
    "black": "40", "red": "41", "green": "42", "brown": "43",
    "yellow": "43", "blue": "44", "magenta": "45", "cyan": "46",
    "white": "47", "default": "49",
}


def _pyte_color_to_sgr(color: str, is_bg: bool = False) -> str:
    """Convert a pyte color value to an SGR parameter string."""
    table = _NAMED_BG if is_bg else _NAMED_FG
    if color in table:
        return table[color]
    # 256-color: pyte stores as e.g. "123" (string of int)
    if color.isdigit():
        prefix = "48" if is_bg else "38"
        return f"{prefix};5;{color}"
    # 24-bit hex: pyte stores as "aabbcc"
    if len(color) == 6:
        try:
            r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
            prefix = "48" if is_bg else "38"
            return f"{prefix};2;{r};{g};{b}"
        except ValueError:
            pass
    return "39" if not is_bg else "49"


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
