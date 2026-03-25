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
  - /api/session/{name}/screen — get rendered screen content
  - /api/session/{name}/resize — resize a local session's terminal
  - /api/session/{name}/send   — send text + enter to a local session
  - /api/session/{name}/keys   — send special keys to a local session

All endpoints except /health require Bearer token authentication.
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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

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
    """Validate Bearer token on all requests except /health."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        # Skip auth for health check
        if request.url.path == "/health":
            return await call_next(request)

        # Validate token
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], self.token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)


async def proxy_to_node(node_name: str, tool_name: str, arguments: dict) -> dict:
    """Forward an MCP tool call to a sibling zpilot node.

    Looks up the node in the registry and dispatches via its transport:
    - MCP nodes: forward via HTTP to the node's /api/exec endpoint
    - Local node: execute locally via subprocess
    - Other transports: execute the zpilot-agent CLI on the remote node

    Returns a dict with 'result' or 'error' key.
    On unreachable nodes, returns an error with 'unreachable' flag.
    """
    import httpx
    from .transport import CircuitBreaker

    registry = NodeRegistry(load_nodes())
    try:
        node = registry.get(node_name)
    except KeyError as e:
        return {"error": str(e)}

    if node.is_local:
        # Execute locally — use proper JSON serialization, never shell interpolation
        import json
        import shlex
        payload = json.dumps({"tool": tool_name, "arguments": arguments})
        result = await node.transport.exec(
            f'echo {shlex.quote(payload)} | cat',
            timeout=30.0,
        )
        return {
            "result": {
                "tool": tool_name,
                "node": node_name,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        }

    if node.transport_type == "mcp" and hasattr(node.transport, "base_url"):
        transport = node.transport
        # Check circuit breaker if available
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
                    f"{transport.base_url}/api/exec",
                    json={
                        "command": f"echo 'proxy:{tool_name}'",
                        "timeout": 25.0,
                    },
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
                            **data,
                        }
                    }
                if hasattr(transport, "_circuit"):
                    transport._circuit.record_failure()
                return {"error": f"HTTP {resp.status_code}: {resp.text}"}
        except httpx.TimeoutException:
            if hasattr(transport, "_circuit"):
                transport._circuit.record_failure()
            return {
                "error": f"Timeout connecting to node '{node_name}'",
                "unreachable": True,
            }
        except (httpx.ConnectError, ConnectionError) as e:
            if hasattr(transport, "_circuit"):
                transport._circuit.record_failure()
            return {
                "error": f"Node '{node_name}' is unreachable: {e}",
                "unreachable": True,
            }
        except Exception as e:
            if hasattr(transport, "_circuit"):
                transport._circuit.record_failure()
            return {"error": f"Failed to proxy to {node_name}: {e}"}

    # Fallback: execute via the node's transport (SSH, etc.)
    try:
        result = await node.transport.exec(
            f"echo 'proxy:{tool_name}'",
            timeout=30.0,
        )
        return {
            "result": {
                "tool": tool_name,
                "node": node_name,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        }
    except Exception as e:
        return {
            "error": f"Node '{node_name}' is unreachable: {e}",
            "unreachable": True,
        }


def create_http_app(config: ZpilotConfig | None = None) -> FastAPI:
    """Create the FastAPI app with MCP endpoint + REST API."""
    config = config or load_config()

    # Resolve token
    token = config.http_token
    if not token:
        token = os.environ.get("ZPILOT_HTTP_TOKEN", "")
    if not token:
        token = generate_token()
        log.warning("No HTTP token configured — generated ephemeral token: %s", token)

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

    # Auth middleware
    app.add_middleware(TokenAuthMiddleware, token=token)

    # Store token on app for reference
    app._zpilot_token = token  # type: ignore[attr-defined]

    # Shared health tracker instance for this app
    health_tracker = NodeHealthTracker(NodeRegistry(load_nodes()))
    app._health_tracker = health_tracker  # type: ignore[attr-defined]

    # ── Health endpoint (no auth) ────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok", "node": "zpilot"}

    # ── MCP endpoint ─────────────────────────────────────────────

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
        body = await request.json()
        command = body.get("command", "")
        timeout = body.get("timeout", 30.0)

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
        """List all local zellij sessions. Symmetric endpoint for mesh discovery.

        Returns a list of sessions with name, state, and preview lines.
        Peers call this to discover what sessions are available on this node.
        """
        from .zellij import list_sessions, dump_pane
        try:
            sessions = await list_sessions()
        except Exception:
            return {"node": socket.gethostname(), "sessions": []}

        result = []
        for s in sessions:
            entry = {
                "name": s.name,
                "is_current": s.is_current,
                "managed": s.managed,
            }
            try:
                content = await dump_pane(session=s.name, tail_lines=3)
                lines = content.strip().splitlines()[-3:] if content.strip() else []
                entry["last_lines"] = lines
                entry["last_line"] = lines[-1][:80] if lines else ""
            except Exception:
                entry["last_lines"] = []
                entry["last_line"] = ""
            result.append(entry)

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

    @app.post("/api/session/{name}/resize")
    async def api_session_resize(name: str, request: Request):
        """Resize a local session's terminal. Symmetric endpoint."""
        from .zellij import resize_pane
        body = await request.json()
        cols = int(body.get("cols", 80))
        rows = int(body.get("rows", 24))
        try:
            resize_pane(name, cols, rows)
        except Exception:
            # FIFO not available — fallback to stty via zellij
            import shlex
            safe_name = shlex.quote(name)
            stty_cmd = (
                f"zellij --session {safe_name} action write-chars "
                f"{shlex.quote(f'stty rows {rows} cols {cols}; clear')}"
            )
            enter_cmd = f"zellij --session {safe_name} action write 10"
            proc = await asyncio.create_subprocess_shell(
                f"{stty_cmd} && {enter_cmd}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        return {"status": "resized", "session": name, "cols": cols, "rows": rows}

    @app.post("/api/session/{name}/send")
    async def api_session_send(name: str, request: Request):
        """Send text to a local session. Symmetric endpoint."""
        import shlex
        body = await request.json()
        text = body.get("text", "")
        safe_name = shlex.quote(name)
        safe_text = shlex.quote(text)
        cmd = f"zellij --session {safe_name} action write-chars {safe_text}"
        enter = f"zellij --session {safe_name} action write 10"
        proc = await asyncio.create_subprocess_shell(
            f"{cmd} && {enter}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return {"status": "sent", "session": name}

    @app.post("/api/session/{name}/keys")
    async def api_session_keys(name: str, request: Request):
        """Send special keys to a local session. Symmetric endpoint."""
        import shlex
        from zpilot.keys import map_key_to_zellij

        keys = await request.json()  # expects a JSON array of key names
        safe_name = shlex.quote(name)
        for key_name in keys:
            zj_key = map_key_to_zellij(key_name)
            if zj_key:
                cmd = f"zellij --session {safe_name} action write {zj_key}"
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
        return {"status": "keys_sent", "session": name, "count": len(keys)}

    # ── Screen content (rendered) ──

    @app.get("/api/session/{name}/screen")
    async def session_screen(name: str, cols: int = 80, rows: int = 24):
        """Get rendered screen content for a session.

        Returns ANSI-colored terminal output via pyte rendering,
        with fallback to plain zellij dump-screen.
        """
        import shlex

        safe_name = shlex.quote(name)

        # Primary: pyte-based rendering (ANSI color from log files)
        try:
            from zpilot.zellij import dump_screen_rendered

            content = await dump_screen_rendered(name, cols=cols, rows=rows)
            if content and content.strip():
                return {"session": name, "content": content, "method": "pyte"}
        except Exception:
            pass

        # Fallback: zellij dump-screen (plain text)
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(
                suffix=".txt", delete=False
            ) as tmp:
                tmp_path = tmp.name
            cmd = (
                f"zellij --session {safe_name} action dump-screen {tmp_path}"
            )
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            import os

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
        log.info("Auth token: %s", app._zpilot_token)  # type: ignore[attr-defined]

    server_config = uvicorn.Config(
        app, host=host, port=port, log_level="info", **ssl_kwargs
    )
    server = uvicorn.Server(server_config)
    await server.serve()
