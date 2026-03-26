"""Core operations — shared logic for MCP tools, REST endpoints, and CLI.

Every public function here returns structured data (dicts/lists).
Protocol layers (MCP, REST, CLI) handle formatting/serialization.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re as _re
import shlex
import time
from pathlib import Path
from typing import Any

from . import zellij
from .detector import PaneDetector
from .events import EventBus
from .models import ZpilotConfig
from .nodes import Node, NodeRegistry

log = logging.getLogger("zpilot.ops")

_ansi_re = _re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([A-B]")


def _strip_ansi(text: str) -> str:
    return _ansi_re.sub("", text)


# ── Session parsing ──────────────────────────────────────────

def parse_session(session: str | None, registry: NodeRegistry | None) -> tuple[Node | None, str | None]:
    """Parse 'node:session' → (Node, session) or (None, session) for local."""
    if not session:
        return None, None
    if ":" in session and registry:
        node_name, sess_name = session.split(":", 1)
        try:
            node = registry.get(node_name)
            if not node.is_local:
                return node, sess_name
        except (KeyError, ValueError):
            pass
    return None, session


async def _remote_zellij(node: Node, zellij_args: str, timeout: float = 15.0) -> str:
    result = await node.transport.exec(f"zellij {zellij_args}", timeout=timeout)
    return result.stdout if result.ok else ""


async def _remote_dump_pane(node: Node, session: str, full: bool = False) -> str:
    """Get pane content from a remote node."""
    # Try symmetric API first
    try:
        endpoint = f"/api/session/{session}/screen"
        data = await node.transport.api_get(endpoint, timeout=10.0)
        if isinstance(data, dict) and data.get("content"):
            return data["content"]
    except Exception:
        pass
    # Fallback to exec
    safe = shlex.quote(session)
    flag = "--full" if full else ""
    cmd = f"zellij --session {safe} action dump-screen /dev/stdout {flag}"
    result = await node.transport.exec(cmd, timeout=10.0, force_pty=True)
    return result.stdout if result.ok else ""


# ══════════════════════════════════════════════════════════════
# SESSION OPERATIONS
# ══════════════════════════════════════════════════════════════

async def list_sessions() -> list[dict]:
    """List local Zellij sessions."""
    sessions = await zellij.list_sessions()
    return [
        {"name": s.name, "is_current": s.is_current, "managed": s.managed, "exited": s.exited}
        for s in sessions
    ]


async def list_sessions_full(
    detector: PaneDetector,
    registry: NodeRegistry | None = None,
) -> list[dict]:
    """List all local sessions with state, idle, heat, and preview lines.

    Used by both /api/sessions (mcp_http) and check_all (MCP tool).
    """
    sessions = await zellij.list_sessions()
    seen: set[str] = set()
    result = []

    for s in sessions:
        seen.add(s.name)
        if s.exited:
            result.append({
                "name": s.name, "is_current": s.is_current, "managed": s.managed,
                "state": "exited", "idle_seconds": 0, "heat": 0.0,
                "last_lines": [], "last_line": "",
            })
            continue
        entry: dict[str, Any] = {
            "name": s.name, "is_current": s.is_current, "managed": s.managed,
            "state": "active",
        }
        try:
            content = await zellij.dump_pane(session=s.name, tail_lines=3)
            clean = _strip_ansi(content) if content else ""
            state = detector.detect(s.name, "main", clean)
            entry["state"] = state.value
            entry["idle_seconds"] = round(detector.get_idle_seconds(s.name, "main"), 1)
            entry["heat"] = round(detector.get_heat(s.name, "main"), 3)
            lines = clean.strip().splitlines()[-3:] if clean.strip() else []
            entry["last_lines"] = lines
            entry["last_line"] = lines[-1][:80] if lines else ""
        except Exception:
            entry["idle_seconds"] = 0
            entry["heat"] = 0.0
            entry["last_lines"] = []
            entry["last_line"] = ""
        result.append(entry)

    # Shell-wrapper-only sessions (PTY logs without Zellij)
    result.extend(_discover_shell_wrapper_sessions(seen))

    return result


def _discover_shell_wrapper_sessions(seen: set[str]) -> list[dict]:
    """Find PTY-only sessions from log/fifo files."""
    import glob as _glob
    log_dir = "/tmp/zpilot/logs"
    fifo_dir = "/tmp/zpilot/fifos"
    entries = []
    for path in _glob.glob(os.path.join(log_dir, "*--main.log")):
        fname = os.path.basename(path)
        name = fname.rsplit("--main.log", 1)[0]
        if not name or name in seen:
            continue
        has_fifo = os.path.exists(os.path.join(fifo_dir, f"{name}.fifo"))
        alive = False
        if has_fifo:
            try:
                fd = os.open(os.path.join(fifo_dir, f"{name}.fifo"), os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
                alive = True
            except OSError:
                pass
        last_lines: list[str] = []
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                sz = f.tell()
                f.seek(max(0, sz - 2048))
                tail = f.read().decode("utf-8", errors="replace")
                last_lines = tail.strip().splitlines()[-3:]
        except Exception:
            pass
        entries.append({
            "name": name, "is_current": False, "managed": True,
            "last_lines": last_lines,
            "last_line": last_lines[-1][:80] if last_lines else "",
            "state": "active" if alive else "exited",
            "idle_seconds": 0, "heat": 0.0, "pty_only": True,
        })
    return entries


async def create_session(name: str, layout: str | None = None,
                         registry: NodeRegistry | None = None) -> dict:
    """Create a new session (local or remote)."""
    node, sess = parse_session(name, registry)
    if node:
        cmd = f"zellij --session {shlex.quote(sess)} &"
        await node.transport.exec(cmd, timeout=10)
        return {"status": "created", "session": sess, "node": node.name}
    await zellij.new_session(name, layout=layout)
    return {"status": "created", "session": name, "node": "local"}


async def kill_session(name: str, registry: NodeRegistry | None = None) -> dict:
    """Kill/delete a session."""
    node, sess = parse_session(name, registry)
    if node:
        await _remote_zellij(node, f"delete-session {shlex.quote(sess)} --force")
        return {"status": "killed", "session": sess, "node": node.name}
    await zellij.kill_session(name)
    return {"status": "killed", "session": name, "node": "local"}


# ══════════════════════════════════════════════════════════════
# PANE OPERATIONS
# ══════════════════════════════════════════════════════════════

async def read_pane(session: str | None = None, full: bool = False,
                    registry: NodeRegistry | None = None) -> str:
    """Read pane content (local or remote)."""
    node, sess = parse_session(session, registry)
    if node:
        return await _remote_dump_pane(node, sess, full=full)
    content = await zellij.dump_pane(session=sess, full=full)
    return content or ""


async def check_status(session: str, detector: PaneDetector,
                       registry: NodeRegistry | None = None) -> dict:
    """Get session state, idle, heat, and preview lines."""
    node, sess = parse_session(session, registry)
    if node:
        try:
            content = await _remote_dump_pane(node, sess, full=True)
        except Exception as e:
            return {"session": session, "state": "error", "error": str(e)}
        clean = _strip_ansi(content) if content else ""
        state = detector.detect(session=session, pane="focused", content=clean)
        idle = detector.get_idle_seconds(session, "focused")
        heat = detector.get_heat(session, "focused")
        last_lines = clean.strip().splitlines()[-3:] if clean.strip() else []
        return {
            "session": session, "node": node.name,
            "state": state.value, "idle_seconds": round(idle, 1),
            "heat": round(heat, 3), "last_lines": last_lines,
        }
    content = await zellij.dump_pane(session=sess)
    clean = _strip_ansi(content) if content else ""
    state = detector.detect(session=session, pane="focused", content=clean)
    idle = detector.get_idle_seconds(session, "focused")
    heat = detector.get_heat(session, "focused")
    last_lines = clean.strip().splitlines()[-3:] if clean.strip() else []
    return {
        "session": session, "state": state.value,
        "idle_seconds": round(idle, 1), "heat": round(heat, 3),
        "last_lines": last_lines,
    }


async def write_to_pane(text: str, session: str | None = None,
                        detector: PaneDetector | None = None,
                        registry: NodeRegistry | None = None) -> dict:
    """Send text to a pane (no enter)."""
    node, sess = parse_session(session, registry)
    if node:
        escaped = shlex.quote(text)
        safe_sess = shlex.quote(sess) if sess else None
        zj_args = f"--session {safe_sess} action write-chars {escaped}" if safe_sess else f"action write-chars {escaped}"
        await _remote_zellij(node, zj_args)
        if detector:
            detector.record_input(f"{node.name}:{sess}" if sess else node.name, "focused")
        return {"status": "sent", "chars": len(text), "node": node.name, "session": sess}
    await zellij.write_to_pane(text, session=session)
    if detector:
        detector.record_input(session or "current", "focused")
    return {"status": "sent", "chars": len(text), "node": "local", "session": session}


async def run_in_pane(command: str, session: str | None = None,
                      detector: PaneDetector | None = None,
                      registry: NodeRegistry | None = None) -> dict:
    """Send text + enter to a pane."""
    node, sess = parse_session(session, registry)
    if node:
        escaped = shlex.quote(command)
        safe_sess = shlex.quote(sess) if sess else None
        zj_args = f"--session {safe_sess} action write-chars {escaped}" if safe_sess else f"action write-chars {escaped}"
        await _remote_zellij(node, zj_args)
        enter_args = f"--session {safe_sess} action write 10" if safe_sess else "action write 10"
        await _remote_zellij(node, enter_args)
        if detector:
            detector.record_input(f"{node.name}:{sess}" if sess else node.name, "focused")
        return {"status": "executed", "command": command, "node": node.name, "session": sess}
    await zellij.run_command_in_pane(command, session=session)
    if detector:
        detector.record_input(session or "current", "focused")
    return {"status": "executed", "command": command, "node": "local", "session": session}


async def send_keys(session: str, keys: list[str],
                    detector: PaneDetector | None = None,
                    registry: NodeRegistry | None = None) -> dict:
    """Send special keys to a session."""
    node, sess = parse_session(session, registry)
    results = []
    if node:
        safe_sess = shlex.quote(sess) if sess else None
        for key_name in keys:
            key_bytes = zellij.SPECIAL_KEYS.get(key_name)
            if key_bytes is not None:
                for byte_val in key_bytes:
                    zj_args = f"--session {safe_sess} action write {byte_val}" if safe_sess else f"action write {byte_val}"
                    await _remote_zellij(node, zj_args)
                results.append({"key": key_name, "ok": True})
            else:
                results.append({"key": key_name, "ok": False, "error": "unknown key"})
    else:
        for key_name in keys:
            ok = await zellij.send_special_key(key_name, session=sess)
            results.append({"key": key_name, "ok": ok})
    if detector:
        detector.record_input(session, "focused")
    return {"status": "keys_sent", "session": session, "results": results}


async def resize_session(name: str, cols: int = 80, rows: int = 24) -> dict:
    """Resize a local session terminal."""
    try:
        zellij.resize_pane(name, cols, rows)
    except Exception:
        safe_name = shlex.quote(name)
        stty_cmd = (
            f"zellij --session {safe_name} action write-chars "
            f"{shlex.quote(f'stty rows {rows} cols {cols}; clear')}"
        )
        enter_cmd = f"zellij --session {safe_name} action write 10"
        proc = await asyncio.create_subprocess_shell(
            f"{stty_cmd} && {enter_cmd}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    return {"status": "resized", "session": name, "cols": cols, "rows": rows}


async def get_screen(name: str, cols: int = 80, rows: int = 24) -> dict:
    """Get rendered screen content for a session."""
    # Primary: pyte-based rendering
    try:
        from .zellij import dump_screen_rendered
        content = await dump_screen_rendered(name, cols=cols, rows=rows)
        if content and content.strip():
            return {"session": name, "content": content, "method": "pyte"}
    except Exception:
        pass
    # Fallback: zellij dump-screen
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp_path = tmp.name
        safe_name = shlex.quote(name)
        cmd = f"zellij --session {safe_name} action dump-screen {tmp_path}"
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        content = ""
        if os.path.exists(tmp_path):
            with open(tmp_path) as f:
                content = f.read()
            os.unlink(tmp_path)
        if content:
            return {"session": name, "content": content, "method": "dump"}
    except Exception:
        pass
    return {"session": name, "content": "", "method": "none"}


async def search_pane(session: str, pattern: str, context: int = 2,
                      registry: NodeRegistry | None = None) -> dict:
    """Search pane scrollback for a pattern."""
    node, sess = parse_session(session, registry)
    if node:
        content = await _remote_dump_pane(node, sess, full=True)
    else:
        content = await zellij.dump_pane(session=sess, full=True)
    if not content:
        return {"session": session, "matches": [], "total_lines": 0}

    all_lines = content.splitlines()
    try:
        regex = _re.compile(pattern, _re.IGNORECASE)
    except _re.error:
        regex = _re.compile(_re.escape(pattern), _re.IGNORECASE)

    matches = []
    for i, line in enumerate(all_lines):
        if regex.search(line):
            start = max(0, i - context)
            end = min(len(all_lines), i + context + 1)
            snippet = []
            for j in range(start, end):
                snippet.append({"line_num": j + 1, "text": all_lines[j], "match": j == i})
            matches.append(snippet)
    return {"session": session, "matches": matches[:30], "total_lines": len(all_lines)}


async def get_output_history(session: str, lines: int = 50,
                             registry: NodeRegistry | None = None) -> dict:
    """Get the last N lines from pane scrollback."""
    node, sess = parse_session(session, registry)
    if node:
        content = await _remote_dump_pane(node, sess, full=True)
    else:
        content = await zellij.dump_pane(session=sess, full=True)
    if not content:
        return {"session": session, "lines": [], "total_lines": 0}
    all_lines = content.strip().splitlines()
    tail = all_lines[-lines:]
    return {"session": session, "lines": tail, "total_lines": len(all_lines)}


# ══════════════════════════════════════════════════════════════
# EXEC / FILE OPERATIONS
# ══════════════════════════════════════════════════════════════

# Commands allowed via /api/exec. First token of the command must match.
EXEC_ALLOWLIST: set[str] = {
    "cat", "echo", "env", "false", "git", "grep", "head", "hostname",
    "id", "kill", "ls", "mkdir", "pip", "pip3", "ps", "pwd", "rm",
    "tail", "test", "touch", "true", "uname", "wc", "which", "whoami",
    "zellij", "zpilot",
}

# Shell meta-characters that could bypass allowlist via chaining
_SHELL_META = {"&&", "||", ";", "|", "`", "$(", ">", "<", "\n"}


def _check_exec_allowlist(command: str) -> str | None:
    """Validate command against allowlist. Returns error message or None."""
    stripped = command.strip()
    if not stripped:
        return "Empty command"
    # Block shell meta-characters that chain commands
    for meta in _SHELL_META:
        if meta in stripped:
            return f"Shell meta-character not allowed: {meta!r}"
    # Extract first token (the binary name)
    first_token = shlex.split(stripped)[0] if stripped else ""
    binary = os.path.basename(first_token)
    if binary not in EXEC_ALLOWLIST:
        return f"Command not in allowlist: {binary!r}"
    return None


async def exec_command(command: str, timeout: float = 30.0,
                       *, allow_unsafe: bool = False) -> dict:
    """Execute a shell command on this node.

    Commands are validated against EXEC_ALLOWLIST unless allow_unsafe=True.
    Local callers (CLI, internal) can bypass with allow_unsafe=True.
    """
    if not allow_unsafe:
        err = _check_exec_allowlist(command)
        if err:
            return {"returncode": -1, "stdout": "", "stderr": f"Blocked: {err}"}
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "returncode": proc.returncode or 0,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {"returncode": -1, "stdout": "", "stderr": "Command timed out"}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


# ══════════════════════════════════════════════════════════════
# NODE / FLEET OPERATIONS
# ══════════════════════════════════════════════════════════════

def list_nodes(registry: NodeRegistry) -> list[dict]:
    """List all configured nodes."""
    return [
        {
            "name": n.name, "transport": n.transport_type,
            "host": n.host or "(local)", "is_local": n.is_local,
            "labels": n.labels,
        }
        for n in registry.all()
    ]


async def ping_node(registry: NodeRegistry, node_name: str) -> dict:
    """Check if a node is reachable."""
    node = registry.get(node_name)
    try:
        alive = await node.transport.is_alive()
        return {"node": node_name, "reachable": alive}
    except Exception as e:
        return {"node": node_name, "reachable": False, "error": str(e)}


def list_peers(registry: NodeRegistry) -> list[dict]:
    """List directly-reachable non-local peers."""
    import socket
    return [
        {"name": n.name, "transport": n.transport_type, "labels": n.labels}
        for n in registry.all() if not n.is_local
    ]


async def recent_events(event_bus: EventBus, count: int = 20) -> list[dict]:
    """Get recent events from the event bus."""
    events = event_bus.recent(count)
    return [ev.to_dict() for ev in events]
