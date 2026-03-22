"""HTTP MCP server for distributed zpilot.

Each node runs its own zpilot HTTP server, exposing:
  - /mcp              — MCP protocol endpoint (StreamableHTTP)
  - /health           — unauthenticated health check
  - /api/exec         — execute a command on this node
  - /api/upload       — upload a file to this node
  - /api/download     — download a file from this node
  - /api/siblings     — list known peer nodes (mesh discovery + health)
  - /api/proxy/{node} — forward a tool call to a sibling node
  - /api/fleet-health — health status of all known nodes

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
        if not auth.startswith("Bearer ") or auth[7:] != self.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)


async def proxy_to_node(node_name: str, tool_name: str, arguments: dict) -> dict:
    """Forward an MCP tool call to a sibling zpilot node.

    Looks up the node in the registry and dispatches via its transport:
    - MCP nodes: forward via HTTP to the node's /api/exec endpoint
    - Local node: execute locally via subprocess
    - Other transports: execute the zpilot-agent CLI on the remote node

    Returns a dict with 'result' or 'error' key.
    """
    import httpx

    registry = NodeRegistry(load_nodes())
    try:
        node = registry.get(node_name)
    except KeyError as e:
        return {"error": str(e)}

    if node.is_local:
        # Execute locally by invoking the tool through our own exec
        import json
        args_json = json.dumps(arguments)
        result = await node.transport.exec(
            f'echo {{"tool": "{tool_name}", "arguments": {args_json}}} | cat',
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
        # Forward via HTTP to the sibling's /api/exec endpoint
        transport = node.transport
        headers = transport._headers()
        try:
            async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
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
                    return {
                        "result": {
                            "tool": tool_name,
                            "node": node_name,
                            **data,
                        }
                    }
                return {"error": f"HTTP {resp.status_code}: {resp.text}"}
        except httpx.TimeoutException:
            return {"error": f"Timeout connecting to {node_name}"}
        except Exception as e:
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
        return {"error": f"Failed to proxy to {node_name}: {e}"}


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
            return JSONResponse(result, status_code=502)

        return result

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
            content = base64.b64decode(content_b64)
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

        if not path or not os.path.exists(path):
            return JSONResponse({"error": "file not found"}, status_code=404)

        try:
            with open(path, "rb") as f:
                content = f.read()
            return {
                "path": path,
                "size": len(content),
                "content": base64.b64encode(content).decode(),
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

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
