"""FastAPI web frontend for zpilot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
    """JSON API: session status (aggregated from all nodes — mesh aware)."""
    return await _get_session_data()


@app.get("/api/sessions/local")
async def api_sessions_local():
    """List local sessions only. Symmetric endpoint matching MCP server.

    Peers call this to discover what sessions are available on this node.
    """
    import socket as _socket
    try:
        sessions = await zellij.list_sessions()
    except Exception:
        return {"node": _socket.gethostname(), "sessions": []}

    result = []
    for s in sessions:
        entry = {"name": s.name, "is_current": s.is_current, "managed": s.managed}
        try:
            content = await zellij.dump_pane(session=s.name, tail_lines=3)
            lines = content.strip().splitlines()[-3:] if content.strip() else []
            entry["last_lines"] = lines
            entry["last_line"] = lines[-1][:80] if lines else ""
        except Exception:
            entry["last_lines"] = []
            entry["last_line"] = ""
        result.append(entry)
    return {"node": _socket.gethostname(), "sessions": result}


@app.get("/api/peers")
async def api_peers():
    """List directly-reachable peers. Symmetric endpoint for mesh topology."""
    import socket as _socket
    peers = []
    for node in node_registry.remote_nodes():
        peers.append({
            "name": node.name,
            "transport": node.transport_type,
            "labels": node.labels,
        })
    return {"node": _socket.gethostname(), "peers": peers}


@app.get("/api/events")
async def api_events(count: int = 30):
    """JSON API: recent events."""
    events = event_bus.recent(count)
    return [e.to_dict() for e in events]


@app.api_route(
    "/api/relay/{node_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def api_relay(node_name: str, path: str, request: Request):
    """Relay an API request to a peer node for mesh routing."""
    from starlette.responses import JSONResponse as JR

    node = node_registry.get(node_name)
    if not node:
        return JR({"error": f"Unknown node: {node_name}"}, status_code=404)

    target_path = f"/{path}"
    if request.url.query:
        target_path += f"?{request.url.query}"

    try:
        if request.method == "GET":
            result = await node.transport.api_get(target_path, timeout=15.0)
        else:
            try:
                body = await request.json()
            except Exception:
                body = {}
            result = await node.transport.api_post(
                target_path, json=body, timeout=15.0
            )
        return result
    except (NotImplementedError, ConnectionError) as exc:
        return JR(
            {"error": f"Cannot reach {node_name}: {exc}"},
            status_code=502,
        )


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
async def api_send_to_session(name: str, request: Request):
    """Send text to a session's focused pane. Supports node:session format.

    Accepts JSON {"text": "..."} or form data text=...
    """
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        body = await request.json()
        text = body.get("text", "")
    else:
        form = await request.form()
        text = form.get("text", "")
    node, local_session = _parse_node_session(name)
    if node:
        # Symmetric: call peer's own /api/session endpoint
        try:
            result = await node.transport.api_post(
                f"/api/session/{local_session}/send",
                json={"text": text},
            )
            return result
        except (NotImplementedError, Exception):
            # Fallback to exec for SSH transport
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
        # Symmetric: call peer's own /api/session endpoint
        try:
            result = await node.transport.api_post(
                f"/api/session/{local_session}/keys",
                json=keys,
            )
            return result
        except (NotImplementedError, Exception):
            # Fallback to exec for SSH transport
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


@app.post("/api/session/{name:path}/resize")
async def api_resize_session(name: str, request: Request):
    """Resize a session's terminal. Supports node:session format.

    Accepts JSON {"cols": N, "rows": N} or query params.
    Every zpilot node exposes this same endpoint — symmetric architecture.
    """
    body = await request.json()
    cols = body.get("cols", 80)
    rows = body.get("rows", 24)
    node, local_session = _parse_node_session(name)
    if node:
        await _remote_resize_pane(node, local_session, cols, rows)
        return {"status": "resized", "session": name, "cols": cols, "rows": rows}
    # Local: use FIFO if available, stty fallback
    ok = await zellij.resize_pane(cols, rows, session=name)
    if not ok:
        # No FIFO (not a zpilot-managed session) — use stty fallback
        import shlex
        safe_session = shlex.quote(name)
        cmd = (
            f"zellij --session {safe_session} action write-chars "
            f"{shlex.quote(f'stty rows {rows} cols {cols}; clear')}"
        )
        result = await asyncio.create_subprocess_shell(cmd)
        await result.wait()
        enter_cmd = f"zellij --session {safe_session} action write 10"
        result2 = await asyncio.create_subprocess_shell(enter_cmd)
        await result2.wait()
    return {"status": "resized", "session": name, "cols": cols, "rows": rows}


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
    resize_task = None  # debounce remote resize
    ws_cols = 80  # track client terminal dimensions
    ws_rows = 24

    try:
        # Send initial content
        if node:
            content = await _remote_dump_pane_web(node, local_session, cols=ws_cols, rows=ws_rows)
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
                        content = await _remote_dump_pane_web(node, local_session, cols=ws_cols, rows=ws_rows)
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
                        new_cols = data.get("cols", 80)
                        new_rows = data.get("rows", 24)
                        size_changed = (new_cols != ws_cols or new_rows != ws_rows)
                        ws_cols = new_cols
                        ws_rows = new_rows
                        if size_changed:
                            if node:
                                # Debounce: cancel pending resize, schedule after 500ms
                                if resize_task and not resize_task.done():
                                    resize_task.cancel()
                                async def _do_remote_resize(n=node, s=local_session, c=ws_cols, r=ws_rows):
                                    await asyncio.sleep(0.5)
                                    await _remote_resize_pane(n, s, c, r)
                                resize_task = asyncio.create_task(_do_remote_resize())
                            else:
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


# ── Raw PTY byte streaming ─────────────────────────────────────────────
#
# Unlike /ws/terminal (which polls + re-renders through pyte), this
# endpoint tails the raw log file and streams bytes directly to the
# client. xterm.js handles ANSI natively, so no server-side rendering.
# Input goes through the FIFO (or remote relay) unchanged.

LOG_DIR = Path("/tmp/zpilot/logs")
FIFO_DIR = Path("/tmp/zpilot/fifos")
_pty_log = logging.getLogger("zpilot.pty")


@app.websocket("/ws/pty/{session_name:path}")
async def ws_pty_stream(websocket: WebSocket, session_name: str):
    """Raw PTY byte stream over WebSocket.

    Server → Client: binary frames with raw PTY output (ANSI preserved)
    Client → Server: JSON messages:
      - {type: "input", data: "..."} — raw keystrokes
      - {type: "resize", cols: N, rows: N} — terminal resize
      - plain text — treated as raw input
    """
    node, local_session = _parse_node_session(session_name)
    await websocket.accept()

    if node:
        # Remote session: relay through the peer's /ws/pty endpoint
        await _relay_remote_pty(websocket, node, local_session)
        return

    # Local session: tail the log file + write to FIFO
    log_file = LOG_DIR / f"{local_session}--main.log"
    fifo_path = FIFO_DIR / f"{local_session}.fifo"

    if not log_file.exists():
        await websocket.send_json({"type": "error", "data": f"No log file for session {local_session}"})
        await websocket.close()
        return

    _pty_log.info("PTY stream opened for %s", session_name)

    # Send existing content first (catch-up)
    try:
        with open(log_file, "rb") as f:
            existing = f.read()
        if existing:
            # Send in chunks to avoid huge frames
            chunk_size = 16384
            for i in range(0, len(existing), chunk_size):
                await websocket.send_bytes(existing[i:i + chunk_size])
    except Exception as e:
        _pty_log.warning("Error reading initial log for %s: %s", session_name, e)

    # Tail the log file using inotify-like polling
    stop_event = asyncio.Event()

    async def tail_log():
        """Continuously tail the log file and push new bytes."""
        try:
            fd = os.open(str(log_file), os.O_RDONLY)
            # Seek to end (we already sent existing content)
            os.lseek(fd, 0, os.SEEK_END)

            while not stop_event.is_set():
                data = os.read(fd, 8192)
                if data:
                    try:
                        await websocket.send_bytes(data)
                    except (WebSocketDisconnect, RuntimeError):
                        break
                else:
                    # No new data — wait briefly before retrying
                    await asyncio.sleep(0.02)  # 50Hz max poll rate
        except (OSError, WebSocketDisconnect, RuntimeError):
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    async def write_to_fifo(data: bytes):
        """Write input data to the session's FIFO."""
        if not fifo_path.exists():
            return
        try:
            fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, data)
            os.close(fd)
        except OSError as e:
            _pty_log.debug("FIFO write error for %s: %s", session_name, e)

    # Start tail in background
    tail_task = asyncio.create_task(tail_log())

    try:
        while True:
            msg = await websocket.receive()

            if msg.get("type") == "websocket.disconnect":
                break

            # Handle binary frames (raw input)
            if "bytes" in msg and msg["bytes"]:
                await write_to_fifo(msg["bytes"])
                continue

            # Handle text frames (JSON or plain text)
            text = msg.get("text", "")
            if not text:
                continue

            try:
                data = json.loads(text)
                msg_type = data.get("type", "")

                if msg_type == "input":
                    raw = data.get("data", "")
                    if raw:
                        await write_to_fifo(raw.encode("utf-8"))
                elif msg_type == "resize":
                    cols = data.get("cols", 80)
                    rows = data.get("rows", 24)
                    resize_cmd = f"\x00RESIZE:{cols},{rows}\n".encode()
                    await write_to_fifo(resize_cmd)
                else:
                    # Unknown type, treat data field as input
                    raw = data.get("data", "")
                    if raw:
                        await write_to_fifo(raw.encode("utf-8"))
            except json.JSONDecodeError:
                # Plain text → raw input
                if text:
                    await write_to_fifo(text.encode("utf-8"))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        _pty_log.warning("PTY stream error for %s: %s", session_name, e)
    finally:
        stop_event.set()
        tail_task.cancel()
        _pty_log.info("PTY stream closed for %s", session_name)


async def _relay_remote_pty(websocket: WebSocket, node, session: str):
    """Relay a PTY stream to/from a remote node's /ws/pty endpoint."""
    import httpx

    transport_opts = node.transport_opts or {}
    base_url = transport_opts.get("url", "") or node.host or ""
    token = transport_opts.get("token", "")
    verify_ssl = transport_opts.get("verify_ssl", True)

    if not base_url:
        await websocket.send_json({"type": "error", "data": f"No URL for node {node.name}"})
        await websocket.close()
        return

    # Convert http(s) to ws(s)
    ws_url = base_url.rstrip("/")
    if ws_url.startswith("https://"):
        ws_url = "wss://" + ws_url[8:]
    elif ws_url.startswith("http://"):
        ws_url = "ws://" + ws_url[7:]
    ws_url = f"{ws_url}/ws/pty/{session}"

    _pty_log.info("Relaying PTY stream %s:%s via %s", node.name, session, ws_url)

    try:
        import websockets
        import ssl as _ssl

        ssl_ctx = None
        if ws_url.startswith("wss://") and not verify_ssl:
            ssl_ctx = _ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = _ssl.CERT_NONE

        extra_headers = {}
        if token:
            extra_headers["Authorization"] = f"Bearer {token}"

        async with websockets.connect(
            ws_url,
            ssl=ssl_ctx,
            additional_headers=extra_headers,
        ) as remote_ws:
            stop = asyncio.Event()

            async def forward_to_client():
                """Remote → Client."""
                try:
                    async for msg in remote_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception:
                    stop.set()

            async def forward_to_remote():
                """Client → Remote."""
                try:
                    while not stop.is_set():
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if "bytes" in msg and msg["bytes"]:
                            await remote_ws.send(msg["bytes"])
                        elif "text" in msg and msg["text"]:
                            await remote_ws.send(msg["text"])
                except (WebSocketDisconnect, Exception):
                    pass
                finally:
                    stop.set()

            # Run both directions concurrently
            done, pending = await asyncio.wait(
                [asyncio.create_task(forward_to_client()),
                 asyncio.create_task(forward_to_remote())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

    except ImportError:
        # websockets not installed — fall back to polling mode
        _pty_log.warning("websockets package not installed, cannot relay PTY for %s:%s", node.name, session)
        await websocket.send_json({"type": "error", "data": "Remote PTY relay requires 'websockets' package"})
    except Exception as e:
        _pty_log.warning("Remote PTY relay error for %s:%s: %s", node.name, session, e)
        await websocket.send_json({"type": "error", "data": str(e)})
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


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
    """Send a special key to a remote session via peer's API."""
    try:
        await node.transport.api_post(
            f"/api/session/{session}/keys",
            json=[_map_key_to_zellij(key) or key],
        )
    except (NotImplementedError, ConnectionError):
        # Fallback for SSH transport or remote without endpoint
        import shlex
        safe_session = shlex.quote(session)
        zj_key = _map_key_to_zellij(key)
        if zj_key is not None:
            cmd = f"zellij --session {safe_session} action write {zj_key}"
            await node.transport.exec(cmd, timeout=10, force_pty=True)


async def _remote_write_chars(node, session: str, text: str):
    """Write raw text to a remote session via peer's API."""
    try:
        await node.transport.api_post(
            f"/api/session/{session}/send",
            json={"text": text},
        )
    except (NotImplementedError, ConnectionError):
        # Fallback for SSH transport or remote without endpoint
        import shlex
        safe_session = shlex.quote(session)
        safe_text = shlex.quote(text)
        cmd = f"zellij --session {safe_session} action write-chars {safe_text}"
        await node.transport.exec(cmd, timeout=10, force_pty=True)


async def _remote_resize_pane(node, session: str, cols: int, rows: int):
    """Resize a remote session via peer's own /api/session/{name}/resize.

    Symmetric: calls the same endpoint the peer exposes locally.
    The peer handles it with its own zellij.resize_pane() or stty fallback.
    """
    try:
        await node.transport.api_post(
            f"/api/session/{session}/resize",
            json={"cols": cols, "rows": rows},
        )
        return
    except (NotImplementedError, ConnectionError):
        pass  # Fall through to stty fallback
    # Fallback: ad-hoc stty (SSH transport or old remote code)
    import shlex
    safe_session = shlex.quote(session)
    resize_cmd = (
        f"zellij --session {safe_session} action write-chars "
        f"{shlex.quote(f'stty rows {rows} cols {cols}; clear')}"
    )
    await node.transport.exec(resize_cmd, timeout=10, force_pty=True)
    enter_cmd = f"zellij --session {safe_session} action write 10"
    await node.transport.exec(enter_cmd, timeout=10, force_pty=True)


# Key name → zellij byte value mapping
from zpilot.keys import map_key_to_zellij as _map_key_to_zellij

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


def _discover_shell_wrapper_sessions(exclude: set[str] | None = None) -> list[dict]:
    """Discover shell_wrapper sessions from /tmp/zpilot/logs/ not already in Zellij."""
    import glob as _glob
    exclude = exclude or set()
    entries = []
    log_dir = "/tmp/zpilot/logs"
    fifo_dir = "/tmp/zpilot/fifos"
    for path in _glob.glob(os.path.join(log_dir, "*--main.log")):
        fname = os.path.basename(path)
        name = fname.rsplit("--main.log", 1)[0]
        if not name or name in exclude:
            continue
        # Check if session is still alive: FIFO exists AND a process has it open
        fifo_path = os.path.join(fifo_dir, f"{name}.fifo")
        alive = False
        if os.path.exists(fifo_path):
            try:
                # Try opening FIFO non-blocking; if no reader, it's dead
                fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
                alive = True
            except OSError:
                alive = False
        # Read last few lines for preview
        last_lines: list[str] = []
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2048))
                tail = f.read().decode("utf-8", errors="replace")
                raw_lines = tail.strip().splitlines()[-3:]
                last_lines = [_strip_ansi(l)[:80] for l in raw_lines]
        except Exception:
            pass
        entries.append({
            "name": name,
            "node": "local",
            "state": "active" if alive else "exited",
            "idle_seconds": 0,
            "is_current": False,
            "managed": True,  # shell_wrapper sessions are always managed
            "last_lines": last_lines,
            "last_line": last_lines[-1] if last_lines else "",
            "pty_only": True,  # hint: no Zellij, use /ws/pty/ only
        })
    return entries


async def _get_session_data() -> list[dict]:
    """Get session status data, including remote nodes."""
    result = []
    seen_names: set[str] = set()

    # ── Local Zellij sessions ──
    try:
        sessions = await zellij.list_sessions()
    except Exception:
        sessions = []

    for s in sessions:
        seen_names.add(s.name)
        if s.exited:
            result.append({
                "name": s.name,
                "node": "local",
                "state": "exited",
                "idle_seconds": 0,
                "is_current": False,
                "managed": s.managed,
                "last_lines": [],
                "last_line": "(exited)",
            })
            continue
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

    # ── Local shell_wrapper sessions (PTY-only, not in Zellij) ──
    result.extend(_discover_shell_wrapper_sessions(seen_names))

    # ── Remote node sessions (symmetric: call peer's /api/sessions) ──
    async def _fetch_remote_sessions(node):
        """Fetch sessions from a remote peer via its own /api/sessions endpoint."""
        entries = []
        try:
            data = await node.transport.api_get("/api/sessions", timeout=10.0)
            for s in data.get("sessions", []):
                remote_key = f"{node.name}:{s['name']}"
                last_lines = s.get("last_lines", [])
                entries.append({
                    "name": remote_key,
                    "node": node.name,
                    "state": s.get("state", "active"),
                    "idle_seconds": 0,
                    "is_current": False,
                    "managed": s.get("managed", False),
                    "last_lines": last_lines,
                    "last_line": last_lines[-1][:80] if last_lines else "",
                    **({"pty_only": True} if s.get("pty_only") else {}),
                })
            return entries
        except (NotImplementedError, ConnectionError):
            pass
        # Fallback: exec for SSH transport or old remote code
        try:
            res = await node.transport.exec(
                "zellij list-sessions --no-formatting 2>/dev/null", timeout=10.0
            )
            if not res.ok or not res.stdout.strip():
                return entries
            for line in res.stdout.strip().splitlines():
                sess_name = line.strip().split()[0] if line.strip() else ""
                if not sess_name:
                    continue
                remote_key = f"{node.name}:{sess_name}"
                entries.append({
                    "name": remote_key,
                    "node": node.name,
                    "state": "active",
                    "idle_seconds": 0,
                    "is_current": False,
                    "managed": False,
                    "last_lines": [],
                    "last_line": "",
                })
        except Exception:
            pass
        return entries

    # Fetch all remote nodes in parallel
    remote_tasks = [_fetch_remote_sessions(n) for n in node_registry.remote_nodes()]
    if remote_tasks:
        remote_results = await asyncio.gather(*remote_tasks, return_exceptions=True)
        for r in remote_results:
            if isinstance(r, list):
                result.extend(r)

    return sorted(result, key=lambda s: s["name"])


async def _remote_dump_pane_web(node, session: str, cols: int = 80, rows: int = 24) -> str:
    """Dump pane content from a remote node.

    Strategy: try symmetric API first (/api/session/{name}/screen),
    fall back to exec-based pyte rendering, then plain dump-screen.
    """
    import shlex

    # Primary: symmetric API call to peer
    try:
        data = await node.transport.api_get(
            f"/api/session/{session}/screen?cols={cols}&rows={rows}",
            timeout=15.0,
        )
        content = data.get("content", "")
        if content and content.strip():
            return content
    except (NotImplementedError, ConnectionError):
        pass

    # Fallback: exec-based approaches
    safe_session = shlex.quote(session)
    py_session = repr(session)

    # Pyte-based rendering via exec
    cmd_pyte = (
        f"python3 -c \""
        f"import asyncio; from zpilot.zellij import dump_screen_rendered; "
        f"print(asyncio.run(dump_screen_rendered({py_session}, cols={cols}, rows={rows})), end='')"
        f"\""
    )
    result = await node.transport.exec(cmd_pyte, timeout=15)
    if result.ok and result.stdout and result.stdout.strip():
        return result.stdout

    # Plain zellij dump-screen
    cmd_dump = (
        f"TMP=$(mktemp) && "
        f"zellij --session {safe_session} action dump-screen $TMP && "
        f"cat $TMP && rm -f $TMP"
    )
    result = await node.transport.exec(cmd_dump, timeout=10)
    if result.ok and result.stdout:
        return result.stdout
    return ""


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
