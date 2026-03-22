# zpilot Design Document

## Overview

**zpilot** is mission control for AI coding sessions across multiple machines.
Every zpilot instance is the **same binary** — it always manages local Zellij
sessions (node role) and can optionally connect to other zpilot instances
(hub role). There is no separate "agent" vs "hub" — every zpilot is both.

Key value propositions:
1. **Disconnect/reconnect resilience** — sessions persist in Zellij; agents reconnect
2. **Multi-node orchestration** — manage terminal sessions across SSH-reachable machines
3. **AI agent spawning** — launch Copilot CLI (or any agent) on any node, feed it tasks
4. **Smart monitoring** — track session health, detect idle/stuck/completed, keep nodes busy
5. **File transfer** — move files between nodes (build artifacts, configs, logs)
6. **Unified MCP** — single MCP server aggregates all nodes for the calling agent
7. **Peer topology** — any zpilot can connect to any other; hubs can chain through hubs

## Architecture

Every zpilot instance runs the same code. The difference is just configuration:
a zpilot with no `[nodes]` section only manages local sessions. Add nodes and
it becomes a hub that aggregates them. A remote zpilot can itself have nodes,
creating a natural tree / mesh.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Copilot CLI  ← this IS the zpilot UI                                    │
│  "check all nodes" / "launch build on dandroid1" / "anything stuck?"     │
│  Natural language orchestration via zpilot MCP tools                     │
└────────────────────────────┬────────────────────────────────────────────┘
                             │ MCP (stdio)
┌────────────────────────────▼────────────────────────────────────────────┐
│  zpilot (your machine)                                                   │
│                                                                          │
│  ┌─ Local ─────────────────────────────────────────────────────────┐     │
│  │  Zellij sessions (local node — always present)                   │     │
│  │  Daemon + Detector + Event Bus (monitoring)                      │     │
│  └──────────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  ┌─ Monitor ───────────────────────────────────────────────────────┐     │
│  │  Polls all nodes on interval (configurable, default 30s)         │     │
│  │  Tracks: session state, idle time, completions, errors           │     │
│  │  Emits: events, alerts, utilization metrics                      │     │
│  │  Provides: fleet_status(), busy/idle summary, recommendations    │     │
│  └──────────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  ┌─ Transport Layer ───────────────────────────────────────────────┐     │
│  │  SSH (primary) │ DevBox (optional) │ Docker (optional)           │     │
│  └─────┬───────────────────┬───────────────────────────────────────┘     │
└────────┼───────────────────┼────────────────────────────────────────────┘
         │                   │
    ┌────▼──────────┐  ┌─────▼─────────────┐
    │ zpilot         │  │ zpilot             │
    │ @ dandroid1    │  │ @ jump-host        │  ← itself a hub!
    │ (Dev Box+WSL)  │  │                    │
    │                │  │  ┌──────────────┐  │
    │ Zellij sessions│  │  │ zpilot       │  │  ← nodes behind jump host
    │ Daemon+Monitor │  │  │ @ wave2-cde  │  │
    └────────────────┘  │  │ Zellij+Daemon│  │
                        │  └──────────────┘  │
                        │  ┌──────────────┐  │
                        │  │ zpilot       │  │
                        │  │ @ gpu-builder│  │
                        │  │ Zellij+Daemon│  │
                        │  └──────────────┘  │
                        └────────────────────┘

Every box labeled "zpilot" runs the same binary.
```

## Monitoring & Orchestration

The monitor runs as a background loop inside every zpilot instance. It tracks
local sessions and (if configured) remote nodes, building a real-time picture
of what's busy, idle, stuck, or finished.

### What It Tracks

| Signal | How Detected | Meaning |
|--------|-------------|---------|
| **Idle** | No new output for N seconds (configurable) | Session waiting for input or agent thinking |
| **Stuck** | Idle > threshold + same screen content | Agent may be hung, needs intervention |
| **Completed** | Detector sees shell prompt return after task | Agent finished its work |
| **Errored** | Exit code != 0, error patterns in output | Something went wrong |
| **Busy** | Continuous output, CPU activity | Actively working |
| **Disconnected** | Transport `is_alive()` returns false | Node unreachable |

### Monitor Data Model

```python
@dataclass
class SessionHealth:
    node: str
    session: str
    state: str            # busy, idle, stuck, completed, errored
    idle_seconds: float
    last_output_preview: str   # last ~200 chars of output
    error_detected: bool
    started_at: float
    task_description: str | None   # what was this session launched to do?

@dataclass
class NodeHealth:
    node: str
    reachable: bool
    sessions: list[SessionHealth]
    utilization: float    # fraction of sessions that are "busy"
    last_polled: float

@dataclass
class FleetStatus:
    nodes: list[NodeHealth]
    total_sessions: int
    busy: int
    idle: int
    stuck: int
    completed: int
    errored: int
    unreachable_nodes: list[str]
```

### MCP Monitoring Tools

| Tool | Description |
|------|-------------|
| `fleet_status` | Cross-node summary: how many busy/idle/stuck/completed per node |
| `node_health(node)` | Detailed health for one node (sessions, utilization, reachability) |
| `session_health(node, session)` | Health of a specific session (state, idle time, errors) |
| `stuck_sessions` | List sessions that appear stuck (idle > threshold, no progress) |
| `completed_sessions` | List sessions that finished their tasks |
| `idle_nodes` | List nodes with no busy sessions (candidates for new work) |

### Smart Behaviors

The monitor doesn't just report — it can suggest and act:

1. **Idle node alerts** — "dandroid1 has 0 active sessions, wave2 has 0. 
   You have 2 nodes doing nothing."

2. **Stuck detection** — "Session `build-funos` on dandroid1 has been idle
   for 15 minutes with the same output. Possible hang."

3. **Completion harvesting** — when a `launch_copilot` session completes,
   the monitor captures the final output and stores it as an event, so the
   orchestrating agent can read results without polling.

4. **Auto-reconnect** — if a node goes unreachable, monitor retries with
   backoff. When it comes back, it re-syncs session state.

5. **Utilization metrics** — "Fleet utilization: 3/8 sessions busy (37%).
   Nodes dandroid1 and gpu-builder are idle."

### Polling vs Push

For now, the monitor **polls** each node periodically (default: 30s for health,
5s for active sessions being watched). This keeps the remote side simple — no
daemon required for basic monitoring, just zpilot-agent responding to queries.

Future optimization: zpilot-agent on the remote side can optionally run a
lightweight watcher that pushes events over a persistent SSH channel, reducing
polling overhead for large fleets.

## Transport Layer

The transport layer is **fully decoupled from node identity**. A node is just
a name + metadata. The transport is how you reach it. Any machine reachable by
SSH (Linux VM, Dev Box WSL, Wave appliance, Raspberry Pi, cloud instance) is a
valid zpilot node — no special integration required.

### Transport Protocol

```python
class Transport(Protocol):
    """How zpilot talks to a node. All transports implement this."""
    async def exec(command: str, timeout: float = 300) -> ExecResult
    async def exec_stream(command: str) -> AsyncIterator[str]   # streaming stdout
    async def upload(local_path: str, remote_path: str) -> None
    async def download(remote_path: str, local_path: str) -> None
    async def list_dir(remote_path: str) -> list[FileInfo]
    async def read_file(remote_path: str, tail: int | None = None) -> str
    async def write_file(remote_path: str, content: str | bytes) -> None
    async def is_alive() -> bool
    async def connect() -> None
    async def disconnect() -> None

@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

@dataclass
class FileInfo:
    name: str
    path: str
    size: int
    is_dir: bool
    modified: float
```

### Built-in Transports

**SSHTransport** — the universal default. Works with anything SSH-reachable:
- Linux VMs, cloud instances, CDEs, Wave appliances, Raspberry Pis, containers
- Dev Boxes (via WSL — SSH into Windows, then exec in WSL)
- Uses `asyncio.create_subprocess_exec` with `ssh` / `scp`
- ControlMaster for persistent connections (no re-auth per command)
- `exec_stream()` uses `ssh -t` for streaming output
- Supports jump hosts (`ProxyJump`), custom ports, identity files
- Auto-reconnect with exponential backoff on network drop
- Config: just `host` (can be an SSH config alias with everything baked in)

**LocalTransport** — runs commands directly on this machine:
- `asyncio.create_subprocess_exec` for exec
- `shutil.copy` for file transfer
- Always alive, zero latency

### Optional / Extension Transports

These are separate modules that can be installed if needed. They implement
the same `Transport` protocol but use platform-specific APIs:

**DevBoxTransport** (optional: `zpilot[devbox]`):
- Uses DevBox Task API for command execution (fire-and-forget + poll)
- Useful for: bootstrapping SSH on a new DevBox, power management
- Limitations: no streaming, requires active user session, slow

**AzureCLITransport** (optional: `zpilot[azure]`):
- `az ssh vm` for Azure VMs without public IPs (via Azure Bastion / AAD)
- `az serial-console` for emergency access

**DockerTransport** (optional: `zpilot[docker]`):
- `docker exec` for containers
- `docker cp` for file transfer

Transport selection is config-driven — the hub just calls the `Transport`
interface and doesn't know or care what's behind it.

## Components

### 1. Node Registry (`nodes.py`) — NEW

A node is just a named machine with a transport config. The registry
is completely transport-agnostic — it stores connection params, the
transport layer interprets them.

```python
@dataclass
class Node:
    name: str                         # e.g. "dandroid1", "wave2", "pi-cluster-3"
    transport: str                    # "ssh" | "local" | "devbox" | "docker" | ...
    host: str | None = None           # transport-specific target (SSH: user@host)
    labels: dict[str, str] = field(default_factory=dict)  # arbitrary metadata
    transport_opts: dict[str, Any] = field(default_factory=dict)  # transport-specific config
    agent_installed: bool = False     # has zpilot-agent been bootstrapped?
    status: str = "unknown"           # online, offline, unknown
    last_seen: float = 0.0

class NodeRegistry:
    def load(config_path) -> list[Node]
    def save() -> None
    def add(node: Node) -> None
    def remove(name: str) -> None
    def get(name: str) -> Node
    def list(label: str | None = None) -> list[Node]  # filter by label
    async def ping(name: str) -> bool
    async def ping_all() -> dict[str, bool]
```

Config in `~/.config/zpilot/nodes.toml`:
```toml
# Simplest possible node — just needs SSH access
[nodes.my-vm]
transport = "ssh"
host = "dan@10.0.0.42"

# SSH config alias (all connection details in ~/.ssh/config)
[nodes.wave2]
transport = "ssh"
host = "wave2"                  # references ~/.ssh/config entry
labels = { team = "dpu", chip = "s3" }

# Dev Box with WSL — still just SSH, with extras in transport_opts
[nodes.dandroid1]
transport = "ssh"
host = "dandroid1"
labels = { type = "devbox", os = "wsl-ubuntu" }
[nodes.dandroid1.transport_opts]
wsl_distro = "Ubuntu-24.04"    # wrap commands in wsl -d ...
wsl_user = "danielp"
login_shell = true              # use bash -lc (needed for custom PATH)

# Local machine — no config needed
[nodes.local]
transport = "local"

# Cloud VM via Azure Bastion (no public IP)
[nodes.gpu-builder]
transport = "ssh"
host = "azureuser@gpu-builder-vm"
[nodes.gpu-builder.transport_opts]
proxy_command = "az ssh proxy -n gpu-builder-vm -g mygroup"

# Docker container
[nodes.test-container]
transport = "docker"
host = "funos-build-env"        # container name/id
```

The key insight: **`~/.ssh/config` already solves most connection complexity**
(jump hosts, ports, keys, proxy commands). zpilot nodes.toml just needs a name
and an SSH alias. Everything else is optional.

### 3. Hub MCP Server (`hub.py`) — NEW (replaces `mcp_server.py`)

The hub is the single MCP server that agents talk to. It routes every tool
call through the transport layer to the appropriate node.

**Node Tools:**
| Tool | Description |
|------|-------------|
| `list_nodes` | List all configured nodes with connectivity status |
| `ping_node` | Check if a node is reachable |
| `add_node` | Add a new node to the registry |
| `bootstrap_node` | Install zpilot-agent + Zellij on a node |

**Session Tools (node-aware):**
| Tool | Description |
|------|-------------|
| `list_sessions` | List Zellij sessions on a node (or all nodes) |
| `create_session` | Create a named Zellij session on a node |
| `kill_session` | Kill a session on a node |

**Pane Tools (node-aware):**
| Tool | Description |
|------|-------------|
| `read_pane` | Dump screen content from a session on a node |
| `write_to_pane` | Send text/keystrokes to a session on a node |
| `run_command` | Execute a shell command in a session on a node |
| `send_keys` | Send special keys (ctrl-c, arrows, etc.) |
| `search_pane` | Search scrollback buffer on a node |
| `get_output_history` | Get last N lines from a session |

**AI Session Tools (node-aware):**
| Tool | Description |
|------|-------------|
| `launch_copilot` | Start Copilot CLI on a node with a task |
| `check_status` | Status of a session on a node |
| `check_all` | Cross-node status of all sessions |

**File Transfer Tools:**
| Tool | Description |
|------|-------------|
| `upload_file` | Copy a local file to a node |
| `download_file` | Copy a file from a node to local |
| `transfer_file` | Copy a file between two nodes |
| `list_files` | List directory contents on a node |
| `read_remote_file` | Read (tail) a file on a node |

**Event Tools:**
| Tool | Description |
|------|-------------|
| `recent_events` | Events from a node (or all nodes) |

Every tool that accepts `node` defaults to `"local"` if omitted, preserving
backward compatibility with the single-node design.

### 4. zpilot-agent (`agent.py`) — NEW

Lightweight process that runs on each remote node. Wraps the local Zellij
CLI and exposes a simple JSON-RPC interface over stdin/stdout (invoked via SSH).

The hub calls: `ssh node zpilot-agent <command> <args_json>`

Commands:
- `list-sessions` → JSON array of sessions
- `dump-pane <session>` → screen content
- `write-pane <session> <text>` → send text
- `run-command <session> <cmd>` → type + enter
- `new-session <name>` → create session
- `launch-copilot <session> <task>` → spawn agent
- `events [count]` → recent events from local event bus
- `ping` → `{"status": "ok", "zellij": true/false}`

This keeps the remote side simple — no server process to maintain, no ports
to open. SSH invokes zpilot-agent per command.

For long-running monitoring, the daemon still runs on each node independently
and writes to the local event bus. The hub can periodically pull events.

### 5. Zellij CLI Wrapper (`zellij.py`) — EXISTING

Thin Python wrapper around `zellij action` and `zellij` CLI commands.
Unchanged — used by zpilot-agent on each node.

### 6. Idle/Completion Detector (`detector.py`) — EXISTING

Detects when an AI session needs attention.

**Detection signals (priority order):**
1. **BEL character (`\x07`)** — Terminal bell = copilot-cli waiting for input
2. **Prompt pattern match** — Regex matches known prompt patterns
3. **Output quiescence** — No new output for N seconds (configurable, default 30s)
4. **Process exit** — Child process in pane has exited

**Implementation approach:**
- Periodically dump pane content via `zellij action dump-screen`
- Diff against previous dump to detect changes
- Scan for BEL in raw terminal output (requires reading from pane scrollback)
- Track timestamps of last output change per pane

**Pane states:**
```
ACTIVE   — producing output, busy
IDLE     — no output change for idle_threshold seconds
WAITING  — BEL detected or prompt pattern matched (needs human input)
ERROR    — error pattern detected in output
EXITED   — shell/process has exited
UNKNOWN  — not yet categorized
```

### 4. Event Bus (`events.py`)

Simple file-based event system. Events are JSON lines appended to a file.

```python
@dataclass
class Event:
    timestamp: float
    type: str          # "state_change", "notification", "error"
    session: str
    pane: str | None
    old_state: str | None
    new_state: str
    details: str | None

class EventBus:
    def emit(event: Event) -> None       # append to events.jsonl
    def subscribe(callback) -> None       # tail -f style reader
    def recent(n=50) -> list[Event]       # last N events
```

File location: `/tmp/zpilot/events.jsonl`

Why file-based:
- Zero dependencies (no Redis, no message broker)
- Easy to debug (just `tail -f`)
- Multiple readers (daemon, TUI, notification service)
- Survives process restarts

### 5. Daemon (`daemon.py`)

Background process that continuously monitors sessions.

```
Loop every poll_interval (default 5s):
  1. List all Zellij sessions
  2. For each tracked pane:
     a. Dump screen content
     b. Diff against previous dump
     c. Run detector (BEL, prompt, idle, error patterns)
     d. If state changed → emit event
  3. If state == WAITING and notification enabled:
     → fire notification via configured adapter
```

Run as: `zpilot daemon --poll-interval 5`

### 6. Notification Adapters (`notifications.py`)

Pluggable notification system. Each adapter implements:

```python
class NotificationAdapter(Protocol):
    async def send(title: str, body: str, priority: str = "default") -> bool
    async def test() -> bool  # connectivity check
```

**Built-in adapters:**
- `NtfyAdapter` — POST to ntfy.sh topic (self-hostable, has mobile apps)
- `DesktopAdapter` — `notify-send` on Linux, `osascript` on macOS
- `WebhookAdapter` — POST JSON to any URL
- `LogAdapter` — just log to file (for testing)

Config via `~/.config/zpilot/config.toml`:
```toml
[notifications]
adapter = "ntfy"
topic = "zpilot-alerts"
server = "https://ntfy.sh"  # or self-hosted
```

### 7. UI Surfaces

All UIs connect to zpilot via the same MCP interface. They're just different
ways to visualize and interact with the same fleet data.

**a) Copilot CLI** — conversational orchestration (primary)

```
You: "what's happening across my nodes?"
Copilot: *fleet_status* → "dandroid1: 2 sessions (1 building, 1 done).
         wave2: idle. gpu-builder: unreachable since 10 min ago."

You: "the build on dandroid1 finished — grab the binary and kick off
      tests on wave2"
Copilot: *download_file, upload_file, launch_copilot*

You: "anything stuck?"
Copilot: *stuck_sessions* → "funos-test on wave2 idle 12 min"
```

**b) TUI Dashboard** — live visual display (Textual)

```
┌─ zpilot ──────────────────────────────────────────────────────────┐
│ Nodes        ●dandroid1  ●wave2  ○gpu-builder  ●local      18:47 │
│ ┌─dandroid1────────────┬─wave2──────────────┬─local────────────┐ │
│ │ ▶ build-funos  BUSY  │   (idle)           │ copilot-3  BUSY  │ │
│ │   2m active          │                    │   12s active     │ │
│ │   copilot-1    DONE  │                    │                  │ │
│ │   ✓ completed 3m ago │                    │                  │ │
│ └──────────────────────┴────────────────────┴──────────────────┘ │
│ Fleet: 3/4 nodes online │ 2 busy │ 1 done │ 0 stuck │ util: 50%│
│ Events                                                           │
│  18:44  dandroid1/copilot-1  ACTIVE → COMPLETED                 │
│  18:42  dandroid1/build-funos  IDLE → ACTIVE                    │
│  18:40  gpu-builder  DISCONNECTED                                │
└──────────────────────────────────────────────────────────────────┘
```

Auto-refreshes by polling zpilot MCP tools (`fleet_status`, `recent_events`).
Keyboard shortcuts for quick actions (new session, kill, attach).

**c) Web Dashboard** — same as TUI but in a browser

Uses `textual-web` to serve the same Textual app over HTTP. Zero additional
code — just `textual-web zpilot.tui:DashboardApp`.

All three UIs call the exact same MCP tools. zpilot doesn't know or care
which UI is connected — it just serves tool calls.

## CLI Interface

```bash
zpilot serve              # start MCP server (stdio) — primary entry point
zpilot status             # one-shot fleet status (all nodes, sessions, health)
zpilot nodes              # list configured nodes with ping status
zpilot ping [node]        # ping one or all nodes
zpilot new <node> <name> [cmd]  # create a new session on a node
zpilot bootstrap <node>   # install zpilot + Zellij on a remote node
zpilot config             # show/edit configuration
```

## Configuration

`~/.config/zpilot/config.toml`:

```toml
[general]
poll_interval = 30          # seconds between fleet health polls
active_poll_interval = 5    # seconds when watching a specific session
idle_threshold = 60         # seconds of no output = idle
stuck_threshold = 300       # seconds idle + no progress = stuck

[detection]
prompt_patterns = [         # regex patterns indicating "waiting for input"
    "^\\$ $",
    "^> $",
    "^❯ ",
    "^\\(copilot\\)",
]
error_patterns = [
    "^Error:",
    "^FATAL:",
    "^panic:",
]
completion_patterns = [
    "^✓ All .* passed",
    "^Build succeeded",
]

[notifications]
enabled = true
adapter = "ntfy"            # "ntfy", "webhook", "none"
topic = "zpilot"
notify_on = ["stuck", "completed", "errored", "disconnected"]
```

Node config in `~/.config/zpilot/nodes.toml` (see Node Registry section above).

## File Structure

```
zpilot/
├── DESIGN.md
├── README.md
├── pyproject.toml
├── src/
│   └── zpilot/
│       ├── __init__.py
│       ├── cli.py              # Click CLI entry point
│       ├── config.py           # Configuration loading
│       ├── daemon.py           # Background session watcher (local)
│       ├── detector.py         # Idle/completion/error detection
│       ├── events.py           # Event bus (file-based)
│       ├── mcp_server.py       # MCP server — every tool, node-aware
│       ├── models.py           # Node, Session, Pane, Event, Health, etc.
│       ├── monitor.py          # Fleet health monitor (polls nodes)
│       ├── nodes.py            # Node registry (loads nodes.toml)
│       ├── notifications.py    # Notification adapters
│       ├── transport.py        # Transport protocol + implementations
│       ├── transports/
│       │   ├── __init__.py
│       │   ├── ssh.py          # SSHTransport
│       │   ├── local.py        # LocalTransport
│       │   ├── devbox.py       # DevBoxTransport (optional)
│       │   └── docker.py       # DockerTransport (optional)
│       └── zellij.py           # Zellij CLI wrapper (used on each node)
└── tests/
    ├── test_detector.py
    ├── test_events.py
    ├── test_monitor.py
    ├── test_transport.py
    ├── test_nodes.py
    └── test_zellij.py
```

## Dependencies

- **mcp** — MCP SDK (already installed)
- **click** — CLI framework (already installed)
- **tomli** — TOML config parsing (stdlib in 3.11+)
- **httpx** — async HTTP for notification adapters

Optional extras:
- `zpilot[devbox]` — DevBox transport (needs `@microsoft/devbox-mcp`)
- `zpilot[docker]` — Docker transport

No Rust, no WASM, no TUI framework. Pure Python + SSH.

## Open Questions

1. Should `zpilot serve` also start the monitor loop, or should that be a
   separate `zpilot monitor` process? (Leaning: same process, background task.)
2. How to handle SSH key/auth differences across nodes? (Leaning: rely on
   `~/.ssh/config` and ssh-agent — zpilot doesn't manage keys.)
3. Should there be a web dashboard eventually? (Leaning: maybe later, not MVP.)
4. Event storage: per-node JSONL or centralized SQLite on the hub?
5. Should the monitor be able to auto-restart failed sessions?
