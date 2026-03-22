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

app = FastAPI(title="zpilot", description="Mission Control for AI Coding Sessions")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

config = load_config()
detector = PaneDetector(config)
event_bus = EventBus(config.events_file)


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


@app.get("/api/pane/{session_name}")
async def api_pane_content(session_name: str, pane_name: str = "main", lines: int = 50):
    """JSON API: pane content."""
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


@app.post("/api/session/{name}/send")
async def api_send_to_session(name: str, text: str = Form(...)):
    """Send text to a session's focused pane."""
    await zellij.write_to_pane(text, session=name)
    await zellij.send_enter(session=name)
    detector.record_input(name, "main")
    return {"status": "sent", "session": name, "text": text}


@app.post("/api/session/{name}/keys")
async def api_send_keys(name: str, keys: list[str]):
    """Send special keys to a session."""
    results = []
    for key in keys:
        ok = await zellij.send_special_key(key, session=name)
        results.append({"key": key, "sent": ok})
    detector.record_input(name, "main")
    return {"status": "sent", "session": name, "results": results}


@app.get("/api/pane/{session_name}/raw")
async def api_pane_raw(session_name: str, pane_name: str = "main", lines: int = 80):
    """Raw pane content with ANSI codes preserved (for xterm.js)."""
    content = await zellij.dump_pane(session=session_name, pane_name=pane_name, tail_lines=lines)
    return {"session": session_name, "content": content}


@app.websocket("/ws/terminal/{session_name}")
async def ws_terminal(websocket: WebSocket, session_name: str):
    """WebSocket for real-time terminal I/O.

    Server → Client: raw terminal output (ANSI preserved)
    Client → Server: keystrokes (raw text or JSON {type: 'key', key: 'arrow_up'})
    """
    await websocket.accept()
    last_hash = ""
    last_full_content = ""

    try:
        # Send initial content
        content = await zellij.dump_pane(session=session_name, pane_name="main", tail_lines=200)
        if content:
            normalized = _normalize_for_xterm(content)
            await websocket.send_json({"type": "output", "data": normalized})
            import hashlib
            last_hash = hashlib.md5(content.encode()).hexdigest()
            last_full_content = content

        async def send_updates():
            """Poll for terminal changes and push to client."""
            nonlocal last_hash, last_full_content
            while True:
                await asyncio.sleep(0.3)  # 300ms polling
                try:
                    content = await zellij.dump_pane(
                        session=session_name, pane_name="main", tail_lines=200
                    )
                    if not content:
                        continue
                    import hashlib
                    h = hashlib.md5(content.encode()).hexdigest()
                    if h != last_hash:
                        # Send incremental: if new content starts with old, send only the delta
                        if last_full_content and content.startswith(last_full_content):
                            delta = content[len(last_full_content):]
                            # If delta contains clear-screen or cursor positioning,
                            # send full output so xterm.js can replay from clean state
                            has_cursor = delta and _re.search(r'\x1b\[[0-9;]*[HfJ]', delta)
                            if delta and ('\x1b[2J' in delta or '\x1b[?1049' in delta or has_cursor):
                                normalized = _normalize_for_xterm(content)
                                await websocket.send_json({"type": "output", "data": normalized})
                            elif delta:
                                normalized_delta = _normalize_for_xterm(delta)
                                await websocket.send_json({"type": "append", "data": normalized_delta})
                        else:
                            normalized = _normalize_for_xterm(content)
                            await websocket.send_json({"type": "output", "data": normalized})
                        last_hash = h
                        last_full_content = content
                        # Update detector with fresh content
                        clean = _strip_ansi(content)
                        detector.detect(session_name, "main", clean)
                except Exception:
                    break

        # Run output streaming in background
        output_task = asyncio.create_task(send_updates())

        try:
            while True:
                msg = await websocket.receive_text()
                try:
                    data = json.loads(msg)
                    if data.get("type") == "key":
                        # Named special key
                        await zellij.send_special_key(data["key"], session=session_name)
                    elif data.get("type") == "resize":
                        cols = data.get("cols", 80)
                        rows = data.get("rows", 24)
                        await zellij.resize_pane(cols, rows, session=session_name)
                    elif data.get("type") == "refresh":
                        # Force full content resend on next poll
                        last_hash = ""
                        last_full_content = ""
                    else:
                        # Raw terminal input (may contain control chars)
                        text = data.get("data", "")
                        if text:
                            await zellij.write_raw_input(text, session=session_name)
                except (json.JSONDecodeError, KeyError):
                    # Plain text — send directly as raw input
                    if msg:
                        await zellij.write_raw_input(msg, session=session_name)
                detector.record_input(session_name, "main")
        finally:
            output_task.cancel()

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


import re as _re

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
    # Strip ?-prefixed mode set/reset sequences
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
    # Collapse excessive blank lines
    text = _re.sub(r'\n{3,}', '\n\n', text)
    # Strip trailing blank lines before final prompt
    text = _re.sub(r'\n{2,}$', '\n', text)
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
    """Get session status data."""
    try:
        sessions = await zellij.list_sessions()
    except Exception:
        return []

    result = []
    for s in sessions:
        try:
            # Try to read any named pane log
            content = await zellij.dump_pane(session=s.name)
            clean = _strip_ansi(content)
            # Use "main" as pane key to match /api/pane default
            state = detector.detect(s.name, "main", clean)
            idle = detector.get_idle_seconds(s.name, "main")
            clean_lines = clean.strip().splitlines()[-3:] if clean.strip() else []
            result.append({
                "name": s.name,
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
                "state": "unknown",
                "idle_seconds": 0,
                "is_current": s.is_current,
                "managed": s.managed,
                "last_lines": [],
                "last_line": f"error: {e}",
            })
    return sorted(result, key=lambda s: s["name"])


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
