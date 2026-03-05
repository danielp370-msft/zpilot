"""FastAPI web frontend for zpilot."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
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
async def api_pane_content(session_name: str, pane_name: str = "worker", lines: int = 50):
    """JSON API: pane content."""
    content = await zellij.dump_pane(session=session_name, pane_name=pane_name, tail_lines=lines)
    clean = _strip_ansi(content)
    state = detector.detect(session_name, pane_name, content)
    return {
        "session": session_name,
        "pane": pane_name,
        "state": state.value,
        "idle_seconds": round(detector.get_idle_seconds(session_name, pane_name), 1),
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
    return {"status": "sent", "session": name, "text": text}


import re as _re

def _strip_ansi(text: str) -> str:
    """Strip ANSI escape codes and control characters from text."""
    text = _re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', text)  # CSI sequences (incl ?-prefixed)
    text = _re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)  # OSC sequences
    text = _re.sub(r'\x1b[()][0-9A-B]', '', text)          # charset switches
    text = _re.sub(r'\x1b[=>]', '', text)                   # keypad mode
    text = _re.sub(r'[\x00-\x08\x0e-\x1f]', '', text)      # control chars (keep \n \t)
    return text


@app.get("/api/stream")
async def event_stream():
    """SSE endpoint for live event updates."""
    async def generate():
        last_count = len(event_bus.recent(999))
        while True:
            await asyncio.sleep(2)
            events = event_bus.recent(999)
            if len(events) > last_count:
                new_events = events[last_count:]
                last_count = len(events)
                for ev in new_events:
                    data = json.dumps(ev.to_dict())
                    yield f"data: {data}\n\n"
            # Also send periodic session status
            sessions = await _get_session_data()
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
            state = detector.detect(s.name, "focused", content)
            idle = detector.get_idle_seconds(s.name, "focused")
            raw_lines = content.strip().splitlines()[-3:] if content.strip() else []
            clean_lines = [_strip_ansi(l) for l in raw_lines]
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
