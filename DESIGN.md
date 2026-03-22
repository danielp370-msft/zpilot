# zpilot Design Document

## Overview

**zpilot** is mission control for AI coding sessions across multiple machines.
Every zpilot instance is the **same binary** — it always manages local Zellij
sessions and can optionally connect to other zpilot instances over HTTP.
There is no separate "agent" vs "hub" — every zpilot is both.

Key value propositions:
1. **Disconnect/reconnect resilience** — sessions persist in Zellij; agents reconnect
2. **Multi-node orchestration** — manage terminal sessions across machines via HTTP/MCP
3. **AI agent spawning** — launch Copilot CLI (or any agent) on any node, feed it tasks
4. **Smart monitoring** — track session health, detect idle/stuck/completed, keep nodes busy
5. **File transfer** — move files between nodes (build artifacts, configs, logs)
6. **Unified MCP** — single MCP server aggregates all nodes for the calling agent
7. **Peer topology** — any zpilot can connect to any other; mesh of HTTP peers

## Architecture

Every zpilot instance runs the same code. The difference is just configuration:
a zpilot with no `[nodes]` section only manages local sessions. Add nodes and
it becomes a hub that aggregates them. A remote zpilot can itself have nodes,
creating a natural mesh.

### Connectivity Model

Nodes communicate over **HTTP with bearer-token auth**, typically tunneled via
Azure devtunnels for zero-config internet connectivity. This replaced the
original SSH-based transport for cross-network reachability.

```
+-------------------------------------------------------------------------+
|  Copilot CLI  <- this IS the zpilot UI                                  |
|  "check all nodes" / "launch build on dandroid1" / "anything stuck?"    |
|  Natural language orchestration via zpilot MCP tools                    |
+----------------------------+--------------------------------------------+
                             | MCP (stdio)
+----------------------------v--------------------------------------------+
|  zpilot (your machine)                                                  |
|                                                                         |
|  +- MCP Server (mcp_server.py) ------------------------------------+   |
|  |  20 tools: list_sessions, create_pane, read_pane, fleet_status  |   |
|  |  _parse_session("node:session") -> routes to correct node       |   |
|  +-----------------------------------------------------------------+   |
|                                                                         |
|  +- HTTP Server (mcp_http.py) -------------------------------------+   |
|  |  FastAPI app with bearer-token auth                              |   |
|  |  /health          -> unauthenticated health check                |   |
|  |  /mcp             -> StreamableHTTP MCP endpoint                 |   |
|  |  /api/exec        -> run commands on this node                   |   |
|  |  /api/upload      -> upload files (base64)                       |   |
|  |  /api/download    -> download files (base64)                     |   |
|  |  /api/siblings    -> list known peer nodes                       |   |
|  +-----------------------------------------------------------------+   |
|                                                                         |
|  +- Transport Layer (transport.py) --------------------------------+   |
|  |  LocalTransport   -> subprocess on same machine                  |   |
|  |  SSHTransport     -> SSH exec/scp (legacy, WSL-aware)            |   |
|  |  MCPTransport     -> HTTP calls to remote zpilot REST API        |   |
|  +------+---------------------------+------------------------------+   |
|         |                           |                                   |
+---------+---------------------------+-----------------------------------+
          | HTTP/devtunnel            | HTTP/devtunnel
   +------v----------+        +------v----------+
   | zpilot           |        | zpilot           |
   | @ dandroid1      |        | @ wave2-cde      |
   | (Dev Box+WSL)    |        | (CDE)            |
   |                  |        |                  |
   | serve-http :8222 |        | serve-http :8222 |
   | Zellij sessions  |        | Zellij sessions  |
   +------------------+        +------------------+

Every box labeled "zpilot" runs the same binary.
MCPTransport calls -> /api/exec, /api/upload, /api/download on the peer.
```

### How Remote Operations Work

When a tool call includes a node prefix (e.g. `dandroid1:my-session`):

1. `_parse_session("dandroid1:my-session", registry)` returns `(Node, "my-session")`
2. For most operations, `_remote_zellij(node, args)` calls
   `node.transport.exec("zellij {args}")` on the remote node
3. For pane reading, `_remote_dump_pane(node, session)` uses a tmpfile trick
   because `zellij dump-screen` requires a TTY-attached client
4. MCPTransport.exec() -> HTTP POST to `/api/exec` on the remote zpilot

For local operations, `_parse_session()` returns `(None, session_name)` and
the tool calls the local `zellij` module directly.

## Monitoring & Health

The monitor runs as a background loop inside every zpilot instance. It tracks
local sessions and (if configured) remote nodes, building a real-time picture
of what's busy, idle, stuck, or finished.

### What It Tracks

| Signal | How Detected | Meaning |
|--------|-------------|---------|
| **Active** | Output changing | Session is producing output |
| **Idle** | No output for N seconds | Waiting for input or doing nothing |
| **Completed** | Matches completion pattern | Build/test finished successfully |
| **Errored** | Matches error pattern | Something failed |
| **Stuck** | Idle > threshold + no progress | Needs attention |
| **Disconnected** | Transport unreachable | Node is down |

### Monitor Architecture

```
Monitor.poll_all()
  for each node in registry:
    node.transport.is_alive()  -> connectivity check
    node.transport.exec("zellij list-sessions ...")  -> session list
    for each session:
      dump pane content
      detector.analyze()  -> classify state
      emit events on state transitions
  build FleetStatus with NodeHealth + SessionHealth
```

The `PaneDetector` classifies pane content using configurable regex patterns
(prompt patterns, error patterns, completion patterns). The `EventBus` stores
events as JSONL for history.

### Health Checking

`health_check_nodes()` provides a structured health check across all nodes,
returning connectivity status and response time. MCPTransport includes
automatic retry with exponential backoff (configurable `max_retries` and
`retry_delay`) for resilience against transient network failures.

## MCP Tools

zpilot exposes these tools via the Model Context Protocol:

### Session Management
| Tool | Description |
|------|-------------|
| `list_sessions` | List all local Zellij sessions |
| `create_session` | Create a new session (supports `node:name` for remote) |
| `kill_session` | Kill a session by name |
| `create_pane` | Create a pane in a session (split direction, floating, command) |
| `read_pane` | Read visible pane content (or full scrollback) |
| `write_to_pane` | Send text to a pane |
| `run_in_pane` | Execute a command (types + enter) |
| `launch_copilot` | Start copilot-cli in a new pane with an optional task |
| `send_keys` | Send special keys (ctrl combos, arrows, function keys) |
| `search_pane` | Grep-style search through scrollback |
| `get_output_history` | Get last N lines from scrollback |

### Fleet Management
| Tool | Description |
|------|-------------|
| `list_nodes` | List all configured nodes with transport info |
| `ping_node` | Check if a specific node is reachable |
| `fleet_status` | Full health summary of all nodes and sessions |
| `node_sessions` | List sessions on a specific node |
| `list_siblings` | List known peer nodes (for mesh discovery) |

### Status & Events
| Tool | Description |
|------|-------------|
| `check_status` | Get state of a session's pane (active/idle/waiting/error) |
| `check_all` | Status summary across all sessions |
| `recent_events` | Get recent events from the event bus |

All session tools accept `node:session` syntax for remote operations.

## UI Surfaces

zpilot has no built-in GUI. All interaction happens through:

**a) Copilot CLI** — conversational orchestration (primary)

```
You: "what's happening across my nodes?"
Copilot: *fleet_status* -> "dandroid1: 2 sessions (1 building, 1 done).
         wave2: idle. gpu-builder: unreachable since 10 min ago."

You: "the build on dandroid1 finished -- grab the binary and kick off
      tests on wave2"
Copilot: *download_file, upload_file, launch_copilot*

You: "anything stuck?"
Copilot: *stuck_sessions* -> "funos-test on wave2 idle 12 min"
```

**b) TUI Dashboard** — live visual display (Textual)

```
+- zpilot ---------------------------------------------------------------+
| Nodes        *dandroid1  *wave2  .gpu-builder  *local            18:47 |
| +-dandroid1--------------+-wave2--------------+-local--------------+   |
| | > build-funos  BUSY    |   (idle)           | copilot-3  BUSY    |   |
| |   2m active            |                    |   12s active       |   |
| |   copilot-1    DONE    |                    |                    |   |
| |   completed 3m ago     |                    |                    |   |
| +------------------------+--------------------+--------------------+   |
| Fleet: 3/4 nodes online | 2 busy | 1 done | 0 stuck | util: 50%      |
| Events                                                                 |
|  18:44  dandroid1/copilot-1  ACTIVE -> COMPLETED                       |
|  18:42  dandroid1/build-funos  IDLE -> ACTIVE                          |
|  18:40  gpu-builder  DISCONNECTED                                      |
+------------------------------------------------------------------------+
```

**c) Web Dashboard** — same TUI served via `textual-web`

All three UIs call the exact same MCP tools.

## CLI Interface

```bash
# ── Core ──
zpilot serve              # start MCP server (stdio) — primary entry point
zpilot serve-http         # start HTTP server for remote access (FastAPI+uvicorn)
zpilot serve-http --tunnel  # start HTTP server + devtunnel (easiest remote setup)

# ── Session Management ──
zpilot up                 # start web dashboard + daemon in background
zpilot down               # stop background services
zpilot status             # one-shot session status check
zpilot new <name> [cmd]   # create a new tracked Zellij session
zpilot fleet              # fleet health check across all nodes

# ── Node Management ──
zpilot nodes              # list configured nodes with transport info
zpilot ping <node>        # check connectivity to a specific node
zpilot config             # show current configuration

# ── Devtunnel ──
zpilot tunnel-up          # create/reuse a devtunnel and print the public URL
zpilot tunnel-down        # stop devtunnel hosting
zpilot tunnel-status      # show tunnel status and URLs

# ── Security ──
zpilot token-gen          # generate a cryptographically secure bearer token
```

## Configuration

### `~/.config/zpilot/config.toml`

```toml
[general]
poll_interval = 30          # seconds between fleet health polls
active_poll_interval = 5    # seconds when watching a specific session
idle_threshold = 60         # seconds of no output = idle
stuck_threshold = 300       # seconds idle + no progress = stuck

[detection]
prompt_patterns = [
    "^\\$ $",
    "^> $",
    "^> ",
    "^\\(copilot\\)",
]
error_patterns = [
    "^Error:",
    "^FATAL:",
    "^panic:",
]
completion_patterns = [
    "^All .* passed",
    "^Build succeeded",
]

[notifications]
enabled = true
adapter = "ntfy"
topic = "zpilot"
notify_on = ["stuck", "completed", "errored", "disconnected"]

[http]
host = "0.0.0.0"
port = 8222
token = "your-secret-token-here"  # or use ZPILOT_HTTP_TOKEN env var
```

### `~/.config/zpilot/nodes.toml`

```toml
# ─── SSH transport (LEGACY — maintained for backward compatibility) ───
# Requires direct SSH network access (firewall/VPN dependent).
# Consider migrating to MCP transport below.
[nodes.dandroid1-ssh]
transport = "ssh"
host = "dandroid1.internal"
user = "danielp"
labels = { os = "windows-wsl", gpu = "false" }

# ─── MCP/HTTP transport (RECOMMENDED — works through devtunnels) ───
# No firewall/VPN needed. Built-in retry, circuit breaker, TLS.
[nodes.dandroid1]
transport = "mcp"
host = "https://abc123-8222.usw2.devtunnels.ms"
token = "shared-secret-token"
labels = { os = "windows-wsl", gpu = "false" }

# Local node is always implicit — no config needed
```

## Security Model

1. **Bearer token auth** — every HTTP endpoint except `/health` requires a
   valid `Authorization: Bearer <token>` header
2. **Azure devtunnel ACLs** — devtunnels can restrict access by Entra ID
   (e.g. `devtunnel access create <tunnel> --tenant`)
3. **Transport-layer encryption** — devtunnels provide TLS automatically
4. **Token generation** — `zpilot token-gen` generates cryptographically
   secure tokens; store in config.toml or `ZPILOT_HTTP_TOKEN` env var

### Devtunnel Setup (per remote node)

```bash
# On the remote machine (e.g. dandroid1):
devtunnel create zpilot-host --allow-anonymous  # or restrict with --tenant
devtunnel port create zpilot-host -p 8222
zpilot serve-http  # starts on 0.0.0.0:8222

# In a separate terminal (or as a service):
devtunnel host zpilot-host
# -> prints URL like: https://abc123-8222.usw2.devtunnels.ms

# On your hub machine, add to nodes.toml:
# [nodes.dandroid1]
# transport = "mcp"
# host = "https://abc123-8222.usw2.devtunnels.ms"
# token = "..."
```

## File Structure

```
zpilot/
  DESIGN.md
  README.md
  pyproject.toml
  src/
    zpilot/
      __init__.py
      cli.py              # Click CLI entry point
      config.py           # Configuration loading (~/.config/zpilot/)
      daemon.py           # Background session watcher
      detector.py         # Idle/completion/error detection (PaneDetector)
      devtunnel.py        # Azure devtunnel CLI wrapper (create, host, list)
      events.py           # Event bus (file-based JSONL)
      mcp_http.py         # FastAPI HTTP server (REST API + MCP endpoint)
      mcp_server.py       # MCP server — all tools, node-aware routing
      models.py           # Data models: Pane, Session, Event, Health, etc.
      monitor.py          # Fleet health monitor (polls nodes)
      nodes.py            # Node registry (loads nodes.toml)
      notifications.py    # Notification adapters
      shell_wrapper.py    # PTY fork with output logging, FIFO injection
      transport.py        # Transport ABC + Local/SSH/MCP implementations
      zellij.py           # Zellij CLI wrapper
      tui/                # Textual-based TUI dashboard
      web/                # Web dashboard (FastAPI + xterm.js + themes)
  tests/
    conftest.py
    test_cli.py
    test_cli_runner.py
    test_daemon.py
    test_dashboard.py
    test_detector.py
    test_devtunnel.py
    test_events.py
    test_install_plugin.py
    test_mcp.py
    test_mcp_http.py
    test_models.py
    test_monitor.py
    test_monitor_extended.py
    test_nodes.py
    test_normalize.py
    test_plugin_status.py
    test_proxy_health.py
    test_resilience.py
    test_transport.py
    test_web_api.py
    test_web_endpoints.py
    test_web_extended.py
```

## Transport Comparison

| Feature | Local | SSH (legacy) | MCP/HTTP (recommended) |
|---------|-------|--------------|------------------------|
| **Setup** | Zero config | SSH keys + reachability | `serve-http` + bearer token |
| **Network** | Same machine | Direct SSH access (firewall/VPN) | Any HTTP path (devtunnel OK) |
| **Encryption** | N/A | SSH | TLS (devtunnel auto, or self-signed) |
| **Auth** | N/A | SSH keys | Bearer token |
| **Retry/resilience** | N/A | 2 retries on transient SSH errors | Exponential backoff + circuit breaker |
| **File transfer** | `cp` | `scp` | Base64 over HTTP |
| **WSL support** | N/A | `wsl_distro` + `wsl_user` | Transparent (zpilot runs inside WSL) |
| **Internet traversal** | No | Requires VPN/port-forward | devtunnel (zero config) |
| **Status** | Stable | Legacy (maintained) | Recommended |

## Dependencies

- **mcp** — MCP SDK (stdio + StreamableHTTP)
- **click** — CLI framework
- **httpx** — async HTTP client (for MCPTransport + notifications)
- **fastapi** — HTTP server framework
- **uvicorn** — ASGI server
- **textual** — TUI dashboard framework
- **jinja2** — template rendering
- **websockets** — WebSocket support
- **cryptography** — TLS certificate generation + token generation

## Roadmap

### Phase 1: Local MCP Server (done)
Single-node Zellij management with MCP tools.

### Phase 2: Multi-Node + HTTP (done)
SSH and MCP transports, HTTP server, bearer-token auth, devtunnel support.

### Phase 3: Sibling Registry (done)
- `list_siblings` MCP tool — nodes can discover each other's peers
- `/api/siblings` HTTP endpoint — returns known nodes for mesh discovery

### Phase 4: Devtunnel Integration (done)
- `~/bin/zpilot-http-host.sh` helper script for remote nodes
- Documented devtunnel ACL setup and node configuration

### Phase 5: Resilience & Health Monitoring (done)
- MCPTransport retry with exponential backoff (configurable max_retries, retry_delay)
- `health_check_nodes()` in monitor.py — structured health checks
- Health data wired into `fleet_status` tool response

### Phase 6: TLS Encryption (done)
- Auto-generated self-signed EC certificates for HTTPS
- `verify_ssl`, `ca_cert`, `cert_fingerprint` options on MCPTransport
- Web dashboard serves over HTTPS by default

### Phase 7: Circuit Breaker & Resilience (done)
- `CircuitBreaker` class in transport.py (CLOSED → OPEN → HALF_OPEN)
- Configurable failure threshold and recovery timeout
- Short-circuits requests to known-dead nodes

### Phase 8: Devtunnel Integration (done)
- `devtunnel.py` — create, host, list, inspect Azure devtunnels
- CLI commands: `tunnel-up`, `tunnel-down`, `tunnel-status`
- `serve-http --tunnel` — one-command remote access setup
- Documented ACL setup and node configuration

### Future
- Auto-discovery via sibling gossip protocol
- Session migration between nodes
- Multi-pane layout templates
- Resource-aware scheduling (GPU, memory)
