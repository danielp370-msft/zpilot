"""HTTP MCP server for distributed zpilot.

Each node runs its own zpilot HTTP server, exposing:
  - /mcp          — MCP protocol endpoint (StreamableHTTP)
  - /health       — unauthenticated health check
  - /api/exec     — execute a command on this node
  - /api/upload   — upload a file to this node
  - /api/download — download a file from this node
  - /api/siblings — list known peer nodes (mesh discovery)

All endpoints except /health require Bearer token authentication.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .config import load_config
from .mcp_server import create_mcp_server
from .models import ZpilotConfig
from .nodes import NodeRegistry, load_nodes

log = logging.getLogger("zpilot.http")


def generate_token() -> str:
    """Generate a secure auth token for zpilot HTTP server."""
    return secrets.token_urlsafe(32)


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
        """Return known peer nodes for mesh discovery."""
        registry = NodeRegistry(load_nodes())
        siblings = []
        for node in registry.all():
            siblings.append({
                "name": node.name,
                "transport": node.transport_type,
                "host": node.host or "(local)",
                "labels": node.labels,
            })
        return {"siblings": siblings, "count": len(siblings)}

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
    """Run the HTTP MCP server."""
    import uvicorn

    config = config or load_config()
    app = create_http_app(config)

    host = config.http_host
    port = config.http_port

    log.info("zpilot HTTP server starting on %s:%d", host, port)

    if hasattr(app, "_zpilot_token"):
        log.info("Auth token: %s", app._zpilot_token)  # type: ignore[attr-defined]

    server_config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()
