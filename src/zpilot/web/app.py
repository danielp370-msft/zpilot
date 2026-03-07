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
            await websocket.send_json({"type": "output", "data": content})
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
                            if delta:
                                await websocket.send_json({"type": "append", "data": delta})
                        else:
                            await websocket.send_json({"type": "output", "data": content})
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
                        pass  # Could forward resize to Zellij
                    else:
                        # Raw text input
                        text = data.get("data", "")
                        if text:
                            await zellij.write_to_pane(text, session=session_name)
                except (json.JSONDecodeError, KeyError):
                    # Plain text — send directly as keystrokes
                    if msg:
                        await zellij.write_to_pane(msg, session=session_name)
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
                "last_lines": clean_lines,
                "last_line": clean_lines[-1][:80] if clean_lines else "",
            })
        except Exception as e:
            result.append({
                "name": s.name,
                "state": "unknown",
                "idle_seconds": 0,
                "is_current": s.is_current,
                "last_lines": [],
                "last_line": f"error: {e}",
            })
    return sorted(result, key=lambda s: s["name"])


def run_web(host: str = "0.0.0.0", port: int = 8095):
    """Run the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
