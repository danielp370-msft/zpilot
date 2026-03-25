"""FastAPI web frontend for zpilot."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import zellij
from ..config import load_config
from ..detector import PaneDetector
from ..events import EventBus
from ..models import PaneState
from ..nodes import NodeRegistry, load_nodes

app = FastAPI(title="zpilot", description="Mission Control for AI Coding Sessions")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

config = load_config()
detector = PaneDetector(config)
event_bus = EventBus(config.events_file)
node_registry = NodeRegistry(load_nodes())


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main dashboard page."""
    sessions = await _get_session_data()
    events = event_bus.recent(20)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "sessions": sessions,
        "events": [e.to_dict() for e in events],
    })


@app.get("/api/sessions")
async def api_sessions():
    """JSON API: session status."""
    return await _get_session_data()


@app.get("/api/events")
async def api_events(count: int = 30):
    """JSON API: recent events."""
    events = event_bus.recent(count)
    return [e.to_dict() for e in events]


@app.get("/api/nodes")
async def api_nodes():
    """JSON API: list configured nodes and connectivity."""
    result = []
    for node in node_registry.all():
        info = {"name": node.name, "transport": node.transport_type, "is_local": node.is_local}
        if not node.is_local:
            try:
                res = await node.transport.exec("echo ok", timeout=5)
                info["reachable"] = res.ok
            except Exception:
                info["reachable"] = False
        else:
            info["reachable"] = True
        result.append(info)
    return result


@app.get("/api/pane/{session_name:path}")
async def api_pane_content(session_name: str, pane_name: str = "main", lines: int = 50):
    """JSON API: pane content. Supports node:session format for remote nodes."""
    node, local_session = _parse_node_session(session_name)
    if node:
        content = await _remote_dump_pane_web(node, local_session)
        clean = _strip_ansi(content)
        return {
            "session": session_name,
            "pane": pane_name,
            "node": node.name,
            "state": "active",
            "idle_seconds": 0,
            "content": clean,
            "lines": clean.strip().splitlines()[-lines:] if clean.strip() else [],
        }
    content = await zellij.dump_pane(session=session_name, pane_name=pane_name, tail_lines=lines)
    clean = _strip_ansi(content)
    state = detector.detect(session_name, "main", clean)
    return {
        "session": session_name,
        "pane": pane_name,
        "state": state.value,
        "idle_seconds": round(detector.get_idle_seconds(session_name, "main"), 1),
        "content": clean,
        "lines": clean.strip().splitlines()[-lines:] if clean.strip() else [],
    }


@app.post("/api/session/{name}")
async def api_create_session(name: str, command: str | None = None):
    """Create a new session."""
    await zellij.new_session(name)
    await asyncio.sleep(1)
    if command:
        await zellij.new_pane(session=name, name="main", command=command)
    return {"status": "created", "session": name}


@app.delete("/api/session/{name}")
async def api_delete_session(name: str):
    """Delete a session."""
    await zellij._run(["delete-session", name, "--force"], check=False)
    return {"status": "deleted", "session": name}


@app.post("/api/session/{name}/adopt")
async def api_adopt_session(name: str):
    """Adopt an unmanaged session by injecting the zpilot shell_wrapper."""
    if zellij.is_managed(name):
        return {"status": "already_managed", "session": name}
    ok = await zellij.adopt_session(name)
    return {
        "status": "adopted" if ok else "failed",
        "session": name,
        "managed": zellij.is_managed(name),
    }


@app.post("/api/session/{name:path}/send")
async def api_send_to_session(name: str, text: str = Form(...)):
    """Send text to a session's focused pane. Supports node:session format."""
    node, local_session = _parse_node_session(name)
    if node:
        import shlex
        safe_text = shlex.quote(text)
        safe_session = shlex.quote(local_session)
        cmd = f"zellij --session {safe_session} action write-chars {safe_text}"
        await node.transport.exec(cmd, timeout=10, force_pty=True)
        enter_cmd = f"zellij --session {safe_session} action write 10"
        await node.transport.exec(enter_cmd, timeout=10, force_pty=True)
        return {"status": "sent", "session": name, "text": text}
    await zellij.write_to_pane(text, session=name)
    await zellij.send_enter(session=name)
    detector.record_input(name, "main")
    return {"status": "sent", "session": name, "text": text}


@app.post("/api/session/{name:path}/keys")
async def api_send_keys(name: str, keys: list[str]):
    """Send special keys to a session. Supports node:session format."""
    node, local_session = _parse_node_session(name)
    if node:
        import shlex
        safe_session = shlex.quote(local_session)
        results = []
        for key in keys:
            zj_key = _map_key_to_zellij(key)
            if zj_key is not None:
                cmd = f"zellij --session {safe_session} action write {zj_key}"
                await node.transport.exec(cmd, timeout=10, force_pty=True)
                results.append({"key": key, "sent": True})
            else:
                results.append({"key": key, "sent": False})
        return {"status": "sent", "session": name, "results": results}
    results = []
    for key in keys:
        ok = await zellij.send_special_key(key, session=name)
        results.append({"key": key, "sent": ok})
    detector.record_input(name, "main")
    return {"status": "sent", "session": name, "results": results}


@app.get("/api/pane/{session_name:path}/raw")
async def api_pane_raw(session_name: str, pane_name: str = "main", lines: int = 80):
    """Raw pane content with ANSI codes preserved (for xterm.js). Supports node:session."""
    node, local_session = _parse_node_session(session_name)
    if node:
        content = await _remote_dump_pane_web(node, local_session)
        return {"session": session_name, "content": content}
    content = await zellij.dump_pane(session=session_name, pane_name=pane_name, tail_lines=lines)
    return {"session": session_name, "content": content}


@app.websocket("/ws/terminal/{session_name:path}")
async def ws_terminal(websocket: WebSocket, session_name: str):
    """WebSocket for real-time terminal I/O. Supports node:session format.

    Server → Client: raw terminal output (ANSI preserved)
    Client → Server: keystrokes (raw text or JSON {type: 'key', key: 'arrow_up'})
    """
    node, local_session = _parse_node_session(session_name)
    await websocket.accept()
    last_hash = ""
    last_full_content = ""
    is_focused = True  # assume focused on connect
    ws_cols = 80  # track client terminal dimensions
    ws_rows = 24

    try:
        # Send initial content
        if node:
            content = await _remote_dump_pane_web(node, local_session)
        else:
            content = await zellij.dump_screen_rendered(
                session_name, pane_name="main", cols=ws_cols, rows=ws_rows
            )
        if content:
            normalized = _normalize_for_xterm(content)
            await websocket.send_json({"type": "output", "data": normalized})
            import hashlib
            last_hash = hashlib.md5(content.encode()).hexdigest()
            last_full_content = content

        async def send_updates():
            """Poll for terminal changes and push to client."""
            nonlocal last_hash, last_full_content, is_focused, ws_cols, ws_rows
            while True:
                # Remote polling is slower (network latency)
                interval = 0.5 if node else (0.1 if is_focused else 1.0)
                await asyncio.sleep(interval)
                try:
                    if node:
                        content = await _remote_dump_pane_web(node, local_session)
                    else:
                        content = await zellij.dump_screen_rendered(
                            session_name, pane_name="main",
                            cols=ws_cols, rows=ws_rows,
                        )
                    if not content:
                        if last_full_content:
                            await websocket.send_json({"type": "output", "data": ""})
                            last_hash = ""
                            last_full_content = ""
                        continue
                    import hashlib
                    h = hashlib.md5(content.encode()).hexdigest()
                    if h != last_hash:
                        normalized = _normalize_for_xterm(content)
                        await websocket.send_json({"type": "output", "data": normalized})
                        last_hash = h
                        last_full_content = content
                        if not node:
                            clean = _strip_ansi(content)
                            detector.detect(session_name, "main", clean)
                except (WebSocketDisconnect, RuntimeError):
                    break
                except Exception as exc:
                    import logging
                    logging.getLogger("zpilot.web").warning(
                        "send_updates error for %s: %s", session_name, exc
                    )
                    await asyncio.sleep(2 if node else 1)

        # Run output streaming in background
        output_task = asyncio.create_task(send_updates())

        try:
            while True:
                msg = await websocket.receive_text()
                try:
                    data = json.loads(msg)
                    if data.get("type") == "key":
                        if node:
                            await _remote_send_key(node, local_session, data["key"])
                        else:
                            await zellij.send_special_key(data["key"], session=session_name)
                    elif data.get("type") == "resize":
                        ws_cols = data.get("cols", 80)
                        ws_rows = data.get("rows", 24)
                        if not node:
                            await zellij.resize_pane(ws_cols, ws_rows, session=session_name)
                        # Force refresh so pyte re-renders at new size
                        last_hash = ""
                        last_full_content = ""
                    elif data.get("type") == "focus":
                        is_focused = data.get("visible", True)
                    elif data.get("type") == "refresh":
                        last_hash = ""
                        last_full_content = ""
                    else:
                        text = data.get("data", "")
                        if text:
                            if node:
                                await _remote_write_chars(node, local_session, text)
                            else:
                                await zellij.write_raw_input(text, session=session_name)
                except (json.JSONDecodeError, KeyError):
                    if msg:
                        if node:
                            await _remote_write_chars(node, local_session, msg)
                        else:
                            await zellij.write_raw_input(msg, session=session_name)
                if not node:
                    detector.record_input(session_name, "main")
        finally:
            output_task.cancel()

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


import re as _re


def _parse_node_session(name: str):
    """Parse 'node:session' format. Returns (Node, session_name) or (None, name)."""
    if ":" in name:
        node_name, session = name.split(":", 1)
        try:
            node = node_registry.get(node_name)
            if not node.is_local:
                return node, session
        except KeyError:
            pass
    return None, name


async def _remote_send_key(node, session: str, key: str):
    """Send a special key to a remote session."""
    import shlex
    safe_session = shlex.quote(session)
    zj_key = _map_key_to_zellij(key)
    if zj_key is not None:
        cmd = f"zellij --session {safe_session} action write {zj_key}"
        await node.transport.exec(cmd, timeout=10, force_pty=True)


async def _remote_write_chars(node, session: str, text: str):
    """Write raw text to a remote session."""
    import shlex
    safe_session = shlex.quote(session)
    safe_text = shlex.quote(text)
    cmd = f"zellij --session {safe_session} action write-chars {safe_text}"
    await node.transport.exec(cmd, timeout=10, force_pty=True)


# Key name → zellij byte value mapping
_KEY_MAP = {
    "enter": "10",
    "tab": "9",
    "escape": "27",
    "backspace": "127",
    "arrow_up": "27 91 65",
    "arrow_down": "27 91 66",
    "arrow_right": "27 91 67",
    "arrow_left": "27 91 68",
    "ctrl_c": "3",
    "ctrl_d": "4",
    "ctrl_z": "26",
    "ctrl_l": "12",
    "ctrl_a": "1",
    "ctrl_e": "5",
    "ctrl_r": "18",
    "ctrl_u": "21",
    "ctrl_w": "23",
}


def _map_key_to_zellij(key: str):
    """Map a key name to zellij 'action write' byte values."""
    return _KEY_MAP.get(key.lower())

def _strip_ansi(text: str) -> str:
    """Strip ANSI escape codes and control characters from text.
    
    For full-screen apps (top, htop, vim), detects screen-clear sequences
    and only keeps the last frame to prevent infinite scrolling.
    """
    # Detect full-screen redraws: split on clear-screen or cursor-home sequences
    # \x1b[H = cursor home, \x1b[2J = clear screen, \x1b[?1049h = alt screen buffer
    frames = _re.split(r'\x1b\[2J|\x1b\[\?1049[hl]', text)
    if len(frames) > 1:
        # Full-screen app detected — use last non-empty frame
        for frame in reversed(frames):
            if frame.strip():
                text = frame
                break

    text = _re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', text)  # CSI sequences (incl ?-prefixed)
    text = _re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)  # OSC sequences
    text = _re.sub(r'\x1b[()][0-9A-B]', '', text)          # charset switches
    text = _re.sub(r'\x1b[=>]', '', text)                   # keypad mode
    text = _re.sub(r'[\x00-\x08\x0e-\x1f]', '', text)      # control chars (keep \n \t)
    # Collapse runs of blank lines (from cursor positioning) into single blank line
    text = _re.sub(r'\n{3,}', '\n\n', text)
    return text


def _normalize_for_xterm(text: str) -> str:
    """Normalize terminal log content for xterm.js rendering.

    Keeps SGR color codes (\\x1b[...m) but strips cursor positioning and other
    CSI sequences that cause misalignment when replaying at different terminal sizes.
    Converts \\n to \\r\\n for proper xterm.js line handling.
    """
    # Detect full-screen apps and keep last frame (strip clear-screen markers)
    frames = _re.split(r'\x1b\[2J|\x1b\[\?1049[hl]', text)
    if len(frames) > 1:
        for frame in reversed(frames):
            if frame.strip():
                text = frame
                break

    # Strip OSC sequences (title setting, hyperlinks, etc.)
    text = _re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)
    # Strip charset switches and keypad mode
    text = _re.sub(r'\x1b[()][0-9A-B]', '', text)
    text = _re.sub(r'\x1b[=>]', '', text)
    # Strip lines consisting entirely of ?-prefixed mode sequences (e.g.
    # bracketed paste toggles \x1b[?2004l/h) BEFORE generic stripping, so
    # they don't leave behind empty lines
    text = _re.sub(r'\n(?:\x1b\[\?[0-9;]*[a-zA-Z])+\s*(?=\n|$)', '', text)
    # Strip remaining inline ?-prefixed mode set/reset sequences
    text = _re.sub(r'\x1b\[\?[0-9;]*[a-zA-Z]', '', text)
    # Strip scroll region
    text = _re.sub(r'\x1b\[[0-9;]*r', '', text)
    # Preserve cursor positioning (H, f) for xterm.js — needed for in-place
    # animations, progress bars, TUI apps. Also keep relative movement (A/B/C/D)
    # and inline editing (K/J/m/P/@/L/M).
    # Strip non-printable control chars except \b, \n, \t, \r, \x1b, \x7f
    text = _re.sub(r'[\x00-\x07\x0e-\x1a\x1c-\x1f]', '', text)
    # Normalize \r\n to \n for consistent handling, but preserve bare \r
    # (xterm.js uses \r correctly as carriage return to column 0)
    text = text.replace('\r\n', '\n')
    # NOTE: We no longer collapse blank lines or strip trailing blanks.
    # dump_screen_rendered() returns the full terminal geometry (all rows),
    # and blank lines represent real screen positions.  Stripping them causes
    # content to cluster at the top of the web terminal viewport.
    # Convert \n to \r\n for xterm.js (needs CR to return to column 0)
    text = text.replace('\n', '\r\n')
    return text


# ── Plugin status store (in-memory) ──────────────────────────────────
_plugin_status: dict = {}


@app.post("/api/plugin-status")
async def api_plugin_status_post(request: Request):
    """Receive plugin status reports from the Zellij WASM plugin."""
    global _plugin_status
    body = await request.json()
    _plugin_status = {
        "data": body,
        "updated_at": time.time(),
    }
    return {"status": "ok"}


@app.get("/api/plugin-status")
async def api_plugin_status_get():
    """Return the latest plugin status report."""
    return _plugin_status


# ── Plugin command queue (daemon → plugin) ────────────────────────────
_plugin_commands: list[dict] = []


@app.post("/api/plugin-commands")
async def api_plugin_commands_post(request: Request):
    """Queue a command for the plugin to execute (e.g., write to pane)."""
    body = await request.json()
    _plugin_commands.append(body)
    return {"status": "queued", "queue_length": len(_plugin_commands)}


@app.get("/api/plugin-commands")
async def api_plugin_commands_get():
    """Plugin polls this to get pending commands. Drains the queue."""
    global _plugin_commands
    commands = _plugin_commands[:]
    _plugin_commands = []
    return {"commands": commands}


@app.get("/api/stream")
async def event_stream():
    """SSE endpoint for live event updates with built-in state change detection."""
    async def generate():
        last_count = len(event_bus.recent(999))
        prev_states: dict[str, str] = {}  # session -> last known state

        while True:
            await asyncio.sleep(2)

            # Check for new events from daemon/external
            events = event_bus.recent(999)
            if len(events) > last_count:
                new_events = events[last_count:]
                last_count = len(events)
                for ev in new_events:
                    data = json.dumps(ev.to_dict())
                    yield f"data: {data}\n\n"

            # Poll session status and detect state changes inline
            sessions = await _get_session_data()
            for s in sessions:
                old = prev_states.get(s["name"])
                cur = s["state"]
                if old and old != cur:
                    from ..models import Event as EventModel
                    ev = EventModel(session=s["name"], pane="main",
                               old_state=old, new_state=cur)
                    event_bus.emit(ev)
                    last_count += 1
                    yield f"data: {json.dumps(ev.to_dict())}\n\n"
                prev_states[s["name"]] = cur

            yield f"event: status\ndata: {json.dumps(sessions)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _get_session_data() -> list[dict]:
    """Get session status data, including remote nodes."""
    result = []

    # ── Local sessions ──
    try:
        sessions = await zellij.list_sessions()
    except Exception:
        sessions = []

    for s in sessions:
        try:
            content = await zellij.dump_pane(session=s.name)
            clean = _strip_ansi(content)
            state = detector.detect(s.name, "main", clean)
            idle = detector.get_idle_seconds(s.name, "main")
            clean_lines = clean.strip().splitlines()[-3:] if clean.strip() else []
            result.append({
                "name": s.name,
                "node": "local",
                "state": state.value,
                "idle_seconds": round(idle, 1),
                "is_current": s.is_current,
                "managed": s.managed,
                "last_lines": clean_lines,
                "last_line": clean_lines[-1][:80] if clean_lines else "",
            })
        except Exception as e:
            result.append({
                "name": s.name,
                "node": "local",
                "state": "unknown",
                "idle_seconds": 0,
                "is_current": s.is_current,
                "managed": s.managed,
                "last_lines": [],
                "last_line": f"error: {e}",
            })

    # ── Remote node sessions ──
    for node in node_registry.remote_nodes():
        try:
            res = await node.transport.exec(
                "zellij list-sessions --no-formatting 2>/dev/null", timeout=10.0
            )
            if not res.ok or not res.stdout.strip():
                continue
            for line in res.stdout.strip().splitlines():
                sess_name = line.strip().split()[0] if line.strip() else ""
                if not sess_name:
                    continue
                # Fetch a few lines of content for preview
                remote_key = f"{node.name}:{sess_name}"
                try:
                    content = await _remote_dump_pane_web(node, sess_name)
                    clean = _strip_ansi(content)
                    clean_lines = clean.strip().splitlines()[-3:] if clean.strip() else []
                except Exception:
                    clean_lines = []
                result.append({
                    "name": remote_key,
                    "node": node.name,
                    "state": "active",
                    "idle_seconds": 0,
                    "is_current": False,
                    "managed": False,
                    "last_lines": clean_lines,
                    "last_line": clean_lines[-1][:80] if clean_lines else "",
                })
        except Exception:
            continue

    return sorted(result, key=lambda s: s["name"])


async def _remote_dump_pane_web(node, session: str) -> str:
    """Dump pane content from a remote node."""
    import shlex
    safe_session = shlex.quote(session)
    cmd = (
        f"TMP=$(mktemp) && "
        f"zellij --session {safe_session} action dump-screen $TMP && "
        f"cat $TMP && rm -f $TMP"
    )
    result = await node.transport.exec(cmd, timeout=15, force_pty=True)
    return result.stdout if result.ok else ""


CERT_DIR = __import__("pathlib").Path("/tmp/zpilot")


def _ensure_self_signed_cert():
    """Generate a self-signed cert if one doesn't exist."""
    cert_file = CERT_DIR / "cert.pem"
    key_file = CERT_DIR / "key.pem"
    if cert_file.exists() and key_file.exists():
        return str(cert_file), str(key_file)

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "zpilot"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    key_file.chmod(0o600)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cert_file), str(key_file)


def run_web(host: str = "0.0.0.0", port: int = 8095, ssl: bool = True):
    """Run the web server."""
    import uvicorn
    kwargs = {}
    if ssl:
        try:
            cert, key = _ensure_self_signed_cert()
            kwargs["ssl_certfile"] = cert
            kwargs["ssl_keyfile"] = key
        except ImportError:
            import sys
            print("⚠️  cryptography package not installed — running without SSL", file=sys.stderr)
            print("   Install with: pip install cryptography", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="info", **kwargs)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--no-ssl", action="store_true")
    args = parser.parse_args()
    run_web(host=args.host, port=args.port, ssl=not args.no_ssl)
