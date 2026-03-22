# zpilot ⚡

Distributed terminal orchestration for AI coding sessions — manage Zellij sessions across machines from one place.

## What is zpilot?

zpilot is mission control for AI coding agents. It manages Zellij terminal sessions on one or many machines, exposes them via MCP tools so AI agents (like Copilot CLI) can orchestrate work, and provides a web dashboard for visual monitoring. Every zpilot instance is the same binary — it manages local sessions and optionally connects to peers over HTTP.

## Features

- **Local session management** — create, monitor, read, and control Zellij sessions
- **Multi-node mesh** — connect multiple machines over HTTP with bearer-token auth
- **MCP server** — 20+ tools for AI agents to orchestrate terminal sessions
- **Web dashboard** — HTTPS, xterm.js terminals, themes (Dark, Cyberpunk, LCARS, …)
- **TUI dashboard** — Textual-based terminal UI with keyboard navigation
- **Azure devtunnel integration** — one-command internet connectivity (`serve-http --tunnel`)
- **TLS by default** — auto-generated self-signed certs for HTTP; devtunnel provides production TLS
- **Smart state detection** — active, idle, waiting, error, completed, stuck
- **Circuit breaker & retry** — exponential backoff with automatic circuit-breaking on dead nodes
- **Notifications** — ntfy.sh, desktop, webhook, log file adapters

## Quick Start

```bash
# Install
pip install -e .

# Create some AI coding sessions
zpilot new auth-fix "copilot-cli --task 'fix the auth bug'"
zpilot new api-tests "copilot-cli --task 'write API tests'"

# Monitor
zpilot status         # one-shot status check
zpilot up             # start web dashboard + daemon in background
zpilot down           # stop everything
```

## Usage Scenarios

### Scenario 1: Local Session Management

Just managing Zellij sessions on one machine — no network, no config files needed.

```bash
# Create sessions
zpilot new build-funos "make -j8"
zpilot new test-runner "pytest tests/ -v"

# Check status
zpilot status

# Launch TUI dashboard
zpilot

# Or start web dashboard
zpilot up
open https://localhost:8095
```

### Scenario 2: Web Dashboard

The web dashboard provides real-time browser-based monitoring with live terminal panels.

```bash
# Start in background (HTTPS, localhost only)
zpilot up

# Expose to local network
zpilot up --host 0.0.0.0

# Foreground mode
zpilot web --host 0.0.0.0 --port 9000
```

Features:
- **HTTPS by default** — auto-generated self-signed EC certificate
- **xterm.js terminals** — full ANSI/cursor support, synced dimensions
- **Multi-panel layouts** — single, side-by-side, stacked, 2×2 grid
- **Themes** — Dark, Cyberpunk, Monokai, LCARS, Light (⚙ settings gear)
- **Live state detection** — visual indicators for waiting, active, idle, error
- **Event log** — state transitions with timestamps

### Scenario 3: MCP Server for AI Agents

zpilot exposes 20+ MCP tools that AI agents use to orchestrate sessions.

```bash
# Start MCP server (stdio — used by Copilot CLI)
zpilot serve
```

The agent can then:
```
You: "what's happening across my nodes?"
Copilot: *fleet_status* → "dandroid1: 2 sessions (1 building, 1 done). wave2: idle."

You: "the build finished — grab the binary and kick off tests on wave2"
Copilot: *read_pane, run_in_pane* → done

You: "anything stuck?"
Copilot: *check_all* → "funos-test on wave2 idle 12 min"
```

All session tools accept `node:session` syntax for remote operations.

### Scenario 4: Multi-Node Mesh (HTTP)

Connect multiple machines. Each zpilot instance runs `serve-http` and peers connect over HTTP.

**On each remote node:**

```bash
# Generate a shared secret
zpilot token-gen
# → e.g. abc123secrettoken

# Start HTTP server
zpilot serve-http --host 0.0.0.0 --port 8222 --token abc123secrettoken
```

**On your hub machine, configure `~/.config/zpilot/nodes.toml`:**

```toml
[nodes.dandroid1]
transport = "mcp"
url = "https://dandroid1.internal:8222"
token = "abc123secrettoken"

[nodes.wave2]
transport = "mcp"
url = "https://wave2.internal:8222"
token = "abc123secrettoken"
```

Now `zpilot fleet`, `zpilot nodes`, and all MCP tools see all nodes.

### Scenario 5: Devtunnel for Internet Access

Azure devtunnels give you a public HTTPS URL with zero firewall configuration — the easiest way to set up remote access.

**One-command setup on the remote node:**

```bash
# Start HTTP server + devtunnel in one shot
zpilot serve-http --tunnel --tunnel-name zpilot --port 8222
# → 🔗 Devtunnel URL: https://abc123-8222.usw2.devtunnels.ms
```

**Or manage the tunnel separately:**

```bash
# Create tunnel
zpilot tunnel-up --port 8222

# Check status
zpilot tunnel-status

# Start HTTP server
zpilot serve-http --port 8222

# Stop tunnel
zpilot tunnel-down
```

**Add to nodes.toml on the hub:**

```toml
[nodes.dandroid1]
transport = "mcp"
url = "https://abc123-8222.usw2.devtunnels.ms"
token = "shared-secret"
```

### Scenario 6: SSH Transport (Legacy)

SSH transport still works and is maintained for backward compatibility. It requires direct SSH network access (firewall/VPN dependent) and lacks the retry/circuit-breaker resilience of MCP transport.

```toml
# nodes.toml — SSH (legacy)
[nodes.dandroid1]
transport = "ssh"
host = "dandroid1.internal"
user = "danielp"
# Optional WSL support:
# wsl_distro = "Ubuntu"
# wsl_user = "danielp"
```

**Migrating to MCP:** On the remote node, run `zpilot serve-http --tunnel`. Then change `transport = "ssh"` to `transport = "mcp"` in nodes.toml and set `url` to the devtunnel URL plus a shared `token`. No SSH keys or firewall rules needed.

## Configuration Reference

### `~/.config/zpilot/config.toml`

```toml
[general]
poll_interval = 30          # seconds between fleet health polls
active_poll_interval = 5    # seconds when watching a specific session
idle_threshold = 60         # seconds of no output → idle
stuck_threshold = 300       # seconds idle + no progress → stuck

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
adapter = "ntfy"            # ntfy | desktop | webhook | log
topic = "zpilot"
notify_on = ["stuck", "completed", "errored", "disconnected"]

[http]
host = "0.0.0.0"
port = 8222
token = "your-secret-token"  # or set ZPILOT_HTTP_TOKEN env var
```

### `~/.config/zpilot/nodes.toml`

```toml
# ─── MCP/HTTP transport (RECOMMENDED) ───
[nodes.dandroid1]
transport = "mcp"
url = "https://abc123-8222.usw2.devtunnels.ms"
token = "shared-secret"
labels = { os = "windows-wsl", gpu = "false" }

# ─── SSH transport (LEGACY) ───
[nodes.old-server]
transport = "ssh"
host = "old-server.internal"
user = "danielp"
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `zpilot` | Launch TUI dashboard (default) |
| `zpilot serve` | Start MCP server (stdio transport for AI agents) |
| `zpilot serve-http` | Start HTTP server for remote access |
| `zpilot serve-http --tunnel` | Start HTTP server + devtunnel (easiest remote setup) |
| `zpilot up` | Start web dashboard + daemon in background |
| `zpilot down` | Stop background services |
| `zpilot status` | One-shot session status check |
| `zpilot new <name> [cmd]` | Create a new tracked Zellij session |
| `zpilot web` | Run web dashboard in foreground |
| `zpilot fleet` | Fleet health check across all nodes |
| `zpilot nodes` | List configured nodes with transport info |
| `zpilot ping <node>` | Check connectivity to a node |
| `zpilot config` | Show current configuration |
| `zpilot token-gen` | Generate a secure bearer token |
| `zpilot tunnel-up` | Create/reuse a devtunnel, print public URL |
| `zpilot tunnel-down` | Stop devtunnel hosting |
| `zpilot tunnel-status` | Show devtunnel status and URLs |
| `zpilot daemon start` | Start background session watcher |
| `zpilot daemon stop` | Stop daemon |
| `zpilot daemon status` | Check daemon status |
| `zpilot daemon install` | Install systemd user unit |
| `zpilot notify-test` | Send a test notification |
| `zpilot install-zellij-plugin` | Install the zpilot Zellij WASM plugin |

## Security

- **TLS by default** — HTTP server uses auto-generated self-signed EC certificates; devtunnels provide production-grade TLS automatically
- **Bearer token auth** — every HTTP endpoint (except `/health`) requires `Authorization: Bearer <token>`
- **Azure devtunnel ACLs** — restrict access by Entra ID tenant: `devtunnel access create <tunnel> --tenant`
- **Token generation** — `zpilot token-gen` produces cryptographically secure tokens
- **Localhost binding** — `zpilot up` binds to `127.0.0.1` by default; use `--host 0.0.0.0` for remote access
- **Circuit breaker** — stops wasting time on dead nodes after consecutive failures

## Architecture

See [DESIGN.md](DESIGN.md) for the full design document.

```
+--------------------------------------------------------------------+
|  Copilot CLI / AI Agent                                            |
+----------------------------+---------------------------------------+
                             | MCP (stdio)
+----------------------------v---------------------------------------+
|  zpilot (your machine)                                             |
|                                                                    |
|  MCP Server ─── 20+ tools (session mgmt, fleet, events)           |
|  HTTP Server ── /health, /mcp, /api/exec, /api/upload, /api/download|
|  Transport ──── Local | SSH (legacy) | MCP/HTTP (recommended)      |
+--------+----------------------------+-----------------------------+
         | HTTP/devtunnel             | HTTP/devtunnel
  +------v----------+         +------v----------+
  | zpilot           |         | zpilot           |
  | @ dandroid1      |         | @ wave2-cde      |
  | serve-http :8222 |         | serve-http :8222 |
  +------------------+         +------------------+
```

**Components:**
- **MCP Server** — exposes session management as MCP tools for AI agents
- **HTTP Server** — FastAPI with bearer-token auth, REST API + MCP endpoint
- **Web Dashboard** — FastAPI + xterm.js with live terminal rendering and themes
- **TUI Dashboard** — Textual-based terminal UI with keyboard shortcuts
- **Daemon** — background watcher with pluggable notifications
- **Transport** — pluggable backends: Local (subprocess), SSH (legacy), MCP/HTTP (recommended)

## State Detection

zpilot automatically detects session state (priority order):

1. **BEL** — terminal bell (`\x07`) = copilot waiting for input → `waiting`
2. **Prompt pattern** — regex matches (e.g., `$ `, `> `, `❯ `) → `waiting`
3. **Quiescence** — no output for N seconds → `idle`
4. **Active** — recent output, no prompt detected → `active`

Configure patterns in `config.toml` under `[detection]`.

## Web Dashboard

The web dashboard (`zpilot up` or `zpilot web`) features:

- **HTTPS by default** — auto-generated self-signed EC certificate
- **Real-time xterm.js terminals** — full ANSI support, synced dimensions
- **Multi-panel layouts** — single, side-by-side, stacked, 2×2 grid
- **Themes** — Dark, Cyberpunk, Monokai, LCARS, Light
- **Live state detection** — visual indicators with color-coded states
- **Event log** — state transitions with timestamps
- **Session management** — create, dock/undock, monitor

## Requirements

- Python 3.11+
- [Zellij](https://zellij.dev/) terminal multiplexer
- Optional: [Azure devtunnel CLI](https://aka.ms/devtunnels/cli) for remote access
- Optional: SSH client (for legacy SSH transport)
