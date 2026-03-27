"""HTTP MCP server for distributed zpilot.

Each node runs its own zpilot HTTP server, exposing:
  - /mcp              — MCP protocol endpoint (StreamableHTTP)
  - /health           — unauthenticated health check
  - /api/exec         — execute a command on this node
  - /api/upload       — upload a file to this node
  - /api/download     — download a file from this node
  - /api/siblings     — list known peer nodes (mesh discovery + health)
  - /api/proxy/{node} — forward a tool call to a sibling node
  - /api/relay/{node}/{path} — generic HTTP relay for mesh routing
  - /api/fleet-health — health status of all known nodes
  - /api/sessions              — list local sessions (mesh discovery)
  - /api/peers                 — list directly-reachable peers
  - /api/mesh/join             — accept new node into mesh (invite-based)
  - /ws/pty/{session}          — raw PTY byte stream (WebSocket)
  - /api/session/{name}/screen — get rendered screen content
  - /api/session/{name}/resize — resize a local session's terminal
  - /api/session/{name}/send   — send text + enter to a local session
  - /api/session/{name}/keys   — send special keys to a local session

All endpoints except /health require Bearer token authentication.
The /api/mesh/join endpoint uses invite-based auth (no bearer token needed).
TLS is enabled by default for all network communication.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import ipaddress
import logging
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

import inspect

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .config import load_config
from .mcp_server import create_mcp_server
from .models import ZpilotConfig
from .monitor import NodeHealthTracker
from .nodes import NodeRegistry, load_nodes

log = logging.getLogger("zpilot.http")


CERT_DIR = Path("~/.config/zpilot/certs").expanduser()
ZPILOT_TRANSFER_DIR = Path("~/.local/share/zpilot/transfers").expanduser()
MAX_TRANSFER_SIZE = 100 * 1024 * 1024  # 100 MB


def _validate_path(path: str) -> str:
    """Validate and resolve a file path for upload/download.

    Ensures the path is under the user's home directory and contains
    no directory traversal components after resolution.

    Returns the resolved absolute path string.
    Raises ValueError if the path is unsafe.
    """
    resolved = Path(path).resolve()
    home = Path.home()
    if not str(resolved).startswith(str(home)):
        raise ValueError(f"Path must be under user home directory: {home}")
    # Extra safety: reject if '..' still present in any component
    if ".." in resolved.parts:
        raise ValueError("Path traversal not allowed")
    return str(resolved)


def generate_token() -> str:
    """Generate a secure auth token for zpilot HTTP server."""
    return secrets.token_urlsafe(32)


def generate_self_signed_cert(
    cert_dir: Path | None = None,
) -> tuple[str, str, str]:
    """Generate a self-signed TLS certificate for the zpilot HTTP server.

    Returns (cert_path, key_path, fingerprint).
    Reuses existing certs if they already exist.
    """
    cert_dir = cert_dir or CERT_DIR
    cert_file = cert_dir / "mcp-cert.pem"
    key_file = cert_dir / "mcp-key.pem"

    if cert_file.exists() and key_file.exists():
        # Load existing cert to get fingerprint
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        fp = cert.fingerprint(hashes.SHA256()).hex(":")
        return str(cert_file), str(key_file), fp

    cert_dir.mkdir(parents=True, exist_ok=True)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "zpilot"),
    ])
    san_names = [x509.DNSName("localhost")]
    san_ips = [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]

    # Add machine hostname
    try:
        hostname = socket.gethostname()
        if hostname and hostname != "localhost":
            san_names.append(x509.DNSName(hostname))
    except Exception:
        pass

    # Add local network IPs
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            try:
                san_ips.append(x509.IPAddress(ipaddress.IPv4Address(addr)))
            except ValueError:
                try:
                    san_ips.append(x509.IPAddress(ipaddress.IPv6Address(addr)))
                except ValueError:
                    pass
    except Exception:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(san_names + san_ips),
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

    fp = cert.fingerprint(hashes.SHA256()).hex(":")
    return str(cert_file), str(key_file), fp


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on all requests except /health and /api/mesh/join.

    Includes rate limiting: locks out IPs after repeated auth failures.
    """

    def __init__(self, app, token: str, rate_limiter=None):
        super().__init__(app)
        self.token = token
        self.rate_limiter = rate_limiter

    async def dispatch(self, request: Request, call_next):
        # Skip auth for health check and mesh join (uses invite-based auth)
        if request.url.path in ("/health", "/api/mesh/join"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        # Check rate limit before even validating
        if self.rate_limiter and self.rate_limiter.is_locked_out(client_ip):
            return JSONResponse(
                {"error": "Too many failed attempts. Try again later."},
                status_code=429,
            )

        # Validate token
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], self.token):
            if self.rate_limiter:
                self.rate_limiter.record_failure(client_ip)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if self.rate_limiter:
            self.rate_limiter.record_success(client_ip)

        return await call_next(request)


async def proxy_to_node(node_name: str, tool_name: str, arguments: dict) -> dict:
    """Forward an MCP tool call to a sibling zpilot node.

    Uses the node's /api/tool endpoint for proper MCP tool forwarding.
    Falls back to /api/exec for SSH transports.

    Returns a dict with 'result' or 'error' key.
    """
    import httpx

    registry = NodeRegistry(load_nodes())
    try:
        node = registry.get(node_name)
    except KeyError as e:
        return {"error": str(e)}

    if node.is_local:
        # Execute locally via the dispatch function
        from .mcp_server import _dispatch
        from .detector import PaneDetector
        from .events import EventBus
        from .monitor import Monitor

        config = load_config()
        detector = PaneDetector(config)
        event_bus = EventBus(config.events_file)
        reg = NodeRegistry(load_nodes())
        monitor = Monitor(reg, config, event_bus)
        try:
            text = await _dispatch(
                tool_name, arguments, config, detector, event_bus,
                reg, monitor, None,
            )
            return {"result": {"tool": tool_name, "node": node_name, "text": text}}
        except Exception as e:
            return {"error": str(e)}

    if node.transport_type == "mcp" and hasattr(node.transport, "base_url"):
        transport = node.transport
        if hasattr(transport, "_circuit") and not transport._circuit.allow_request():
            return {
                "error": f"Node '{node_name}' is unreachable (circuit breaker open)",
                "unreachable": True,
            }
        headers = transport._headers()
        try:
            verify = getattr(transport, '_verify', False)
            async with httpx.AsyncClient(verify=verify, timeout=30.0) as client:
                resp = await client.post(
                    f"{transport.base_url}/api/tool",
                    json={"tool": tool_name, "arguments": arguments},
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if hasattr(transport, "_circuit"):
                        transport._circuit.record_success()
                    return {
                        "result": {
                            "tool": tool_name,
                            "node": node_name,
                            "text": data.get("result", ""),
                        }
                    }
                if hasattr(transport, "_circuit"):
                    transport._circuit.record_failure()
                return {"error": f"HTTP {resp.status_code}: {resp.text}"}
        except httpx.TimeoutException:
            if hasattr(transport, "_circuit"):
                transport._circuit.record_failure()
            return {"error": f"Timeout proxying to '{node_name}'", "unreachable": True}
        except (httpx.ConnectError, ConnectionError) as e:
            if hasattr(transport, "_circuit"):
                transport._circuit.record_failure()
            return {"error": f"Node '{node_name}' unreachable: {e}", "unreachable": True}
        except Exception as e:
            if hasattr(transport, "_circuit"):
                transport._circuit.record_failure()
            return {"error": f"Proxy to {node_name} failed: {e}"}

    # Fallback for SSH transport: use transport.api_post if available
    try:
        data = await node.transport.api_post(
            "/api/tool",
            json={"tool": tool_name, "arguments": arguments},
            timeout=30.0,
        )
        return {"result": {"tool": tool_name, "node": node_name, "text": data.get("result", "")}}
    except NotImplementedError:
        return {"error": f"Node '{node_name}' transport does not support tool proxy"}
    except Exception as e:
        return {"error": f"Node '{node_name}' unreachable: {e}", "unreachable": True}


def create_http_app(config: ZpilotConfig | None = None) -> FastAPI:
    """Create the FastAPI app with MCP endpoint + REST API."""
    config = config or load_config()

    # Resolve token
    from .security import resolve_token, mask_token, audit_config_permissions, AuthRateLimiter

    token = resolve_token(
        config_token=config.http_token,
        env_var="ZPILOT_HTTP_TOKEN",
        token_name="http",
    )

    # Audit config file permissions on startup
    perm_warnings = audit_config_permissions()
    for w in perm_warnings:
        log.warning(w)

    mcp_server = create_mcp_server(config)
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        json_response=False,  # SSE streaming
        stateless=False,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with session_manager.run():
            yield

    app = FastAPI(title="zpilot", lifespan=lifespan)

    # Auth middleware with rate limiting
    rate_limiter = AuthRateLimiter(max_failures=10, lockout_seconds=60.0)
    app.add_middleware(TokenAuthMiddleware, token=token, rate_limiter=rate_limiter)

    # Store token on app for reference
    app._zpilot_token = token  # type: ignore[attr-defined]

    # Shared health tracker instance for this app
    health_tracker = NodeHealthTracker(NodeRegistry(load_nodes()))
    app._health_tracker = health_tracker  # type: ignore[attr-defined]

    # Shared state for tool proxy and other endpoints
    from .detector import PaneDetector
    from .events import EventBus
    from .monitor import Monitor

    _config = config
    _detector = PaneDetector(config)
    _event_bus = EventBus(config.events_file)
    _registry = NodeRegistry(load_nodes())
    _monitor = Monitor(_registry, config, _event_bus)
    _health_tracker = health_tracker

    # ── Health endpoint (no auth) ────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok", "node": "zpilot"}

    # ── MCP endpoint ─────────────────────────────────────────────
    # MCP >= 1.26 changed handle_request to ASGI (scope, receive, send).
    # Detect the API and mount accordingly.

    _hr_sig = inspect.signature(session_manager.handle_request)
    _hr_params = list(_hr_sig.parameters.keys())

    if "scope" in _hr_params:
        # MCP >= 1.26: mount as raw ASGI app at /mcp
        app.mount("/mcp", app=session_manager.handle_request)
    else:
        # MCP < 1.26: Starlette Request-based API
        @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
        async def mcp_endpoint(request: Request):
            return await session_manager.handle_request(request)

    # ── REST API for transport operations ────────────────────────

    @app.get("/api/siblings")
    async def api_siblings():
        """Return known peer nodes with health status for mesh discovery."""
        registry = NodeRegistry(load_nodes())
        # Quick health check for all nodes
        await health_tracker.check_all()
        siblings = []
        for node in registry.all():
            entry = health_tracker.get_health(node.name)
            siblings.append({
                "name": node.name,
                "transport": node.transport_type,
                "host": node.host or "(local)",
                "labels": node.labels,
                "state": entry.get("state", "unknown"),
                "latency_ms": entry.get("latency_ms", 0.0),
                "last_seen": entry.get("last_seen"),
                "capabilities": entry.get("capabilities", {}),
            })
        return {"siblings": siblings, "count": len(siblings)}

    @app.post("/api/proxy/{node_name}")
    async def api_proxy(node_name: str, request: Request):
        """Proxy a tool call to a sibling node's zpilot instance."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400
            )

        tool_name = body.get("tool", "")
        arguments = body.get("arguments", {})

        if not tool_name:
            return JSONResponse(
                {"error": "'tool' field is required"}, status_code=400
            )

        result = await proxy_to_node(node_name, tool_name, arguments)

        if "error" in result:
            status = 503 if result.get("unreachable") else 502
            return JSONResponse(
                {"error": result["error"]}, status_code=status
            )

        return result

    # ── Generic HTTP relay for mesh routing ──

    @app.api_route(
        "/api/relay/{node_name}/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE"],
    )
    async def api_relay(node_name: str, path: str, request: Request):
        """Relay an API request to a peer node.

        Enables multi-hop mesh routing: if A can reach B and B can reach C,
        A can call B's /api/relay/C/api/sessions to get C's sessions.
        The path is forwarded as-is to the target node's API.
        """
        node = node_registry.get(node_name)
        if not node:
            return JSONResponse(
                {"error": f"Unknown node: {node_name}"}, status_code=404
            )

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
            return JSONResponse(
                {"error": f"Cannot reach {node_name}: {exc}"},
                status_code=502,
            )

    @app.get("/api/fleet-health")
    async def fleet_health():
        """Return health status of all known nodes."""
        await health_tracker.check_all()
        nodes = health_tracker.all_health()
        online = sum(1 for n in nodes if n.get("state") == "online")
        return {
            "nodes": nodes,
            "summary": {
                "total": len(nodes),
                "online": online,
                "offline": sum(1 for n in nodes if n.get("state") == "offline"),
                "degraded": sum(1 for n in nodes if n.get("state") == "degraded"),
            },
            "timestamp": time.time(),
        }

    @app.post("/api/exec")
    async def api_exec(request: Request):
        """Execute a command on this node."""
        from . import ops
        body = await request.json()
        command = body.get("command", "")
        timeout = body.get("timeout", 30.0)
        result = await ops.exec_command(command, timeout=timeout)
        return result

    @app.post("/api/upload")
    async def api_upload(request: Request):
        """Upload a file to this node."""
        body = await request.json()
        path = body.get("path", "")
        content_b64 = body.get("content", "")

        if not path:
            return JSONResponse({"error": "path required"}, status_code=400)

        try:
            path = _validate_path(path)
        except ValueError as e:
            return JSONResponse({"error": f"Forbidden: {e}"}, status_code=403)

        try:
            content = base64.b64decode(content_b64)
            if len(content) > MAX_TRANSFER_SIZE:
                return JSONResponse(
                    {"error": f"File too large ({len(content)} bytes). Max: {MAX_TRANSFER_SIZE} bytes"},
                    status_code=413,
                )
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
            return {"status": "ok", "path": path, "size": len(content)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/download")
    async def api_download(request: Request):
        """Download a file from this node."""
        path = request.query_params.get("path", "")

        if not path:
            return JSONResponse({"error": "file not found"}, status_code=404)

        try:
            path = _validate_path(path)
        except ValueError as e:
            return JSONResponse({"error": f"Forbidden: {e}"}, status_code=403)

        if not os.path.exists(path):
            return JSONResponse({"error": "file not found"}, status_code=404)

        try:
            file_size = os.path.getsize(path)
            if file_size > MAX_TRANSFER_SIZE:
                return JSONResponse(
                    {"error": f"File too large ({file_size} bytes). Max: {MAX_TRANSFER_SIZE} bytes"},
                    status_code=413,
                )
            with open(path, "rb") as f:
                content = f.read()
            return {
                "path": path,
                "size": len(content),
                "content": base64.b64encode(content).decode(),
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- Session API (symmetric: every node exposes these) ---

    @app.get("/api/sessions")
    async def api_sessions():
        """List all local sessions (Zellij + shell_wrapper). Symmetric endpoint for mesh discovery.

        Returns a list of sessions with name, state, and preview lines.
        Peers call this to discover what sessions are available on this node.
        """
        from . import ops
        from .detector import PaneDetector
        from .models import ZpilotConfig

        _det = getattr(api_sessions, '_detector', None)
        if _det is None:
            _det = PaneDetector(ZpilotConfig())
            api_sessions._detector = _det

        result = await ops.list_sessions_full(_det)
        return {"node": socket.gethostname(), "sessions": result}

    @app.get("/api/peers")
    async def api_peers():
        """List this node's directly-reachable peers (from its nodes.toml).

        Enables mesh topology: any node can discover the network graph
        by querying peers, who in turn list their own peers.
        """
        registry = NodeRegistry(load_nodes())
        peers = []
        for node in registry.all():
            if node.is_local:
                continue
            peers.append({
                "name": node.name,
                "transport": node.transport_type,
                "labels": node.labels,
            })
        return {"node": socket.gethostname(), "peers": peers}

    # ── Mesh join endpoint ──────────────────────────────────────

    @app.post("/api/mesh/join")
    async def api_mesh_join(request: Request):
        """Accept a new node into the mesh via invite token.

        No bearer token required — uses one-time invite secret.
        The joining node sends its identity + credentials, and
        this node adds it to nodes.toml and returns its own
        credentials + known peers.
        """
        from .mesh import (
            validate_invite,
            mark_invite_used,
            add_node_to_config,
            update_node_in_config,
            node_exists,
            build_join_response,
        )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"},
                status_code=400,
            )

        invite_secret = body.get("secret", "")
        joiner_name = body.get("name", "")
        joiner_url = body.get("url", "")
        joiner_token = body.get("token", "")
        joiner_labels = body.get("labels", {})

        if not invite_secret or not joiner_name:
            return JSONResponse(
                {"ok": False, "error": "Missing required fields: secret, name"},
                status_code=400,
            )

        # Validate the invite
        inv = validate_invite(invite_secret)
        if inv is None:
            return JSONResponse(
                {"ok": False, "error": "Invalid, expired, or already-used invite"},
                status_code=403,
            )

        # Mark invite as used
        mark_invite_used(invite_secret, joiner_name)

        # Add the joining node to our config
        try:
            if node_exists(joiner_name):
                update_node_in_config(
                    joiner_name, joiner_url, joiner_token,
                    labels=joiner_labels, verify_ssl=False,
                )
                log.info("Mesh join: updated existing node '%s' (%s)", joiner_name, joiner_url)
            else:
                add_node_to_config(
                    joiner_name, joiner_url, joiner_token,
                    labels=joiner_labels, verify_ssl=False,
                )
                log.info("Mesh join: added new node '%s' (%s)", joiner_name, joiner_url)
        except Exception as e:
            log.error("Mesh join: failed to add node '%s': %s", joiner_name, e)
            return JSONResponse(
                {"ok": False, "error": f"Failed to register node: {e}"},
                status_code=500,
            )

        # Build our info for the joiner
        my_name = socket.gethostname()
        my_url = inv.get("inviter_url", "")
        my_token = getattr(app, "_zpilot_token", "")

        # Collect known peers to share with the joiner
        peers = []
        registry = NodeRegistry(load_nodes())
        for node in registry.all():
            if node.is_local or node.name == joiner_name:
                continue
            peer_info = {
                "name": node.name,
                "url": node.host or "",
                "token": node.transport_opts.get("token", ""),
                "labels": node.labels,
            }
            # Only share peers that have MCP transport (have URL+token)
            if peer_info["url"] and peer_info["token"]:
                peers.append(peer_info)

        # Reload the node registry to pick up the new node
        try:
            new_registry = NodeRegistry(load_nodes())
            app._health_tracker = NodeHealthTracker(new_registry)
        except Exception:
            pass  # non-fatal

        return build_join_response(
            inviter_name=my_name,
            inviter_url=my_url,
            inviter_token=my_token,
            peers=peers,
        )

    @app.post("/api/session/{name}/resize")
    async def api_session_resize(name: str, request: Request):
        """Resize a local session's terminal. Symmetric endpoint."""
        from . import ops
        body = await request.json()
        cols = int(body.get("cols", 80))
        rows = int(body.get("rows", 24))
        await ops.resize_session(name, cols, rows)
        return {"status": "resized", "session": name, "cols": cols, "rows": rows}

    @app.post("/api/session/{name}/send")
    async def api_session_send(name: str, request: Request):
        """Send text to a local session. Symmetric endpoint."""
        from . import ops
        body = await request.json()
        text = body.get("text", "")
        await ops.run_in_pane(name, text)
        return {"status": "sent", "session": name}

    @app.post("/api/session/{name}/keys")
    async def api_session_keys(name: str, request: Request):
        """Send special keys to a local session. Symmetric endpoint."""
        from . import ops
        keys = await request.json()  # expects a JSON array of key names
        await ops.send_keys(name, keys)
        return {"status": "keys_sent", "session": name, "count": len(keys)}

    # ── Screen content (rendered) ──

    @app.get("/api/session/{name}/screen")
    async def session_screen(name: str, cols: int = 80, rows: int = 24):
        """Get rendered screen content for a session.

        Returns ANSI-colored terminal output via pyte rendering,
        with fallback to plain zellij dump-screen.
        """
        from . import ops
        result = await ops.get_screen(name, cols=cols, rows=rows)
        return {"session": name, "content": result.get("content", ""), "method": result.get("method", "none")}

    # ── Tool proxy endpoint ──

    @app.post("/api/tool")
    async def api_tool(request: Request):
        """Execute an MCP tool locally. Used for cross-node tool forwarding.

        Accepts: {"tool": "read_pane", "arguments": {"session": "hello"}}
        Returns: {"result": "...tool output text..."}
        """
        from .mcp_server import create_mcp_server, _dispatch
        from .detector import PaneDetector
        from .events import EventBus
        from .monitor import Monitor

        body = await request.json()
        tool_name = body.get("tool", "")
        arguments = body.get("arguments", {})
        if not tool_name:
            return JSONResponse({"error": "tool name required"}, status_code=400)

        try:
            text = await _dispatch(
                tool_name, arguments,
                _config, _detector, _event_bus,
                _registry, _monitor, _health_tracker,
            )
            return {"result": text}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── Session thumbnail ──

    @app.get("/api/session/{name}/thumbnail.png")
    async def api_session_thumbnail(name: str):
        """Render a PNG thumbnail of the session's terminal screen."""
        import io
        from .thumbnail import render_thumbnail_from_log
        png_bytes = render_thumbnail_from_log(name)
        if not png_bytes:
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(buf, format="PNG")
            png_bytes = buf.getvalue()
        from starlette.responses import Response
        return Response(content=png_bytes, media_type="image/png",
                        headers={"Cache-Control": "no-cache, max-age=3"})

    # ── Raw PTY WebSocket ──

    @app.websocket("/ws/pty/{session_name:path}")
    async def ws_pty(websocket: WebSocket, session_name: str):
        """Raw PTY byte stream over WebSocket.

        Tails the shell_wrapper log file and streams raw bytes as binary
        frames. Input from client is written to the session FIFO.
        Much lower latency than the poll-based /ws/terminal/ endpoint
        and lets xterm.js handle ANSI natively.
        """
        await websocket.accept()
        import os as _os

        log_dir = "/tmp/zpilot/logs"
        fifo_dir = "/tmp/zpilot/fifos"
        log_path = _os.path.join(log_dir, f"{session_name}--main.log")
        fifo_path = _os.path.join(fifo_dir, f"{session_name}.fifo")

        # Validate paths (no traversal)
        real_log = _os.path.realpath(log_path)
        real_fifo = _os.path.realpath(fifo_path)
        if not real_log.startswith(_os.path.realpath(log_dir)):
            await websocket.close(code=1008, reason="invalid session")
            return
        if not real_fifo.startswith(_os.path.realpath(fifo_dir)):
            await websocket.close(code=1008, reason="invalid session")
            return

        if not _os.path.exists(log_path):
            await websocket.close(code=1008, reason="session log not found")
            return

        stop = asyncio.Event()

        async def _tail():
            """Tail log file and send raw bytes."""
            try:
                fd = _os.open(log_path, _os.O_RDONLY)
                try:
                    # Send existing content (catch-up)
                    while True:
                        chunk = _os.read(fd, 65536)
                        if not chunk:
                            break
                        await websocket.send_bytes(chunk)

                    # Tail for new content
                    while not stop.is_set():
                        chunk = _os.read(fd, 65536)
                        if chunk:
                            await websocket.send_bytes(chunk)
                        else:
                            await asyncio.sleep(0.02)  # 50Hz max poll
                finally:
                    _os.close(fd)
            except (WebSocketDisconnect, Exception):
                stop.set()

        async def _recv():
            """Receive input from client and write to FIFO."""
            try:
                while not stop.is_set():
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    text = msg.get("text")
                    data = msg.get("bytes")
                    if text:
                        import json as _json
                        try:
                            obj = _json.loads(text)
                            if obj.get("type") == "resize":
                                cols = int(obj.get("cols", 80))
                                rows = int(obj.get("rows", 24))
                                payload = f"\x00RESIZE:{cols},{rows}\n".encode()
                                try:
                                    fifo_fd = _os.open(fifo_path, _os.O_WRONLY | _os.O_NONBLOCK)
                                    _os.write(fifo_fd, payload)
                                    _os.close(fifo_fd)
                                except OSError:
                                    pass
                            elif obj.get("type") == "input":
                                payload = obj.get("data", "").encode()
                                try:
                                    fifo_fd = _os.open(fifo_path, _os.O_WRONLY | _os.O_NONBLOCK)
                                    _os.write(fifo_fd, payload)
                                    _os.close(fifo_fd)
                                except OSError:
                                    pass
                        except (ValueError, TypeError):
                            pass
                    elif data:
                        try:
                            fifo_fd = _os.open(fifo_path, _os.O_WRONLY | _os.O_NONBLOCK)
                            _os.write(fifo_fd, data)
                            _os.close(fifo_fd)
                        except OSError:
                            pass
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                stop.set()

        try:
            await asyncio.gather(_tail(), _recv())
        except Exception:
            pass

    return app


async def serve_http(config: ZpilotConfig | None = None) -> None:
    """Run the HTTP MCP server with optional TLS encryption."""
    import uvicorn

    config = config or load_config()
    app = create_http_app(config)

    host = config.http_host
    port = config.http_port

    ssl_kwargs: dict = {}
    scheme = "http"

    if config.http_tls:
        scheme = "https"
        cert_path = config.http_cert_file
        key_path = config.http_key_file

        if cert_path and key_path:
            # User-provided certs
            ssl_kwargs["ssl_certfile"] = cert_path
            ssl_kwargs["ssl_keyfile"] = key_path
            log.info("TLS enabled with user-provided cert: %s", cert_path)
        else:
            # Auto-generate self-signed certs
            try:
                cert_path, key_path, fingerprint = generate_self_signed_cert()
                ssl_kwargs["ssl_certfile"] = cert_path
                ssl_kwargs["ssl_keyfile"] = key_path
                log.info("TLS enabled with auto-generated self-signed cert")
                log.info("Cert fingerprint (SHA-256): %s", fingerprint)
            except ImportError:
                log.warning(
                    "cryptography package not installed — running without TLS. "
                    "Install with: pip install cryptography"
                )
                scheme = "http"

    log.info("zpilot HTTP server starting on %s://%s:%d", scheme, host, port)

    if hasattr(app, "_zpilot_token"):
        from .security import mask_token
        log.info("Auth token: %s", mask_token(app._zpilot_token))  # type: ignore[attr-defined]

    server_config = uvicorn.Config(
        app, host=host, port=port, log_level="info", **ssl_kwargs
    )
    server = uvicorn.Server(server_config)
    await server.serve()
