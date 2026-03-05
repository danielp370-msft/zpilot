# zpilot Design Document

## Overview

**zpilot** is mission control for AI coding sessions. It combines a Zellij terminal
multiplexer MCP server, an idle-detection daemon, a pluggable notification system,
and a terminal dashboard — all built from scratch in Python.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  TUI Dashboard (Textual)                                     │
│  Runs inside a Zellij pane — shows all session status        │
│  Keyboard: [Enter] attach  [n] new session  [q] quit        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│  │ copilot-1│ │ copilot-2│ │ build-3  │ │ test-4   │       │
│  │ 🔔 input │ │ ⏳ busy  │ │ ✅ idle  │ │ ❌ error │       │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │
└──────────────────────┬──────────────────────────────────────┘
                       │ reads events from
┌──────────────────────▼──────────────────────────────────────┐
│  Event Bus (file-based: /tmp/zpilot/events.jsonl)            │
│  Session state changes, idle detections, errors              │
└──────────┬───────────────────────────┬──────────────────────┘
           │                           │
┌──────────▼──────────┐  ┌─────────────▼─────────────────────┐
│  Zellij MCP Server   │  │  Daemon (background watcher)      │
│  Tools for AI agents │  │  Polls panes, detects BEL/idle    │
│  - list_sessions     │  │  Writes events to bus             │
│  - create_pane       │  │  Triggers notifications           │
│  - read_pane         │  └─────────────┬─────────────────────┘
│  - run_command       │                │
│  - check_status      │  ┌─────────────▼─────────────────────┐
│  - launch_copilot    │  │  Notification MCP Server           │
│  etc.                │  │  Pluggable adapters:               │
└──────────┬───────────┘  │  - ntfy.sh (default)              │
           │              │  - Slack webhook                   │
┌──────────▼───────────┐  │  - Desktop (notify-send)          │
│  Zellij (unmodified)  │  │  - Custom webhook                │
│  Terminal multiplexer │  └───────────────────────────────────┘
└──────────────────────┘
```

## Components

### 1. Zellij CLI Wrapper (`zellij.py`)

Thin Python wrapper around `zellij action` and `zellij` CLI commands.
All interaction with Zellij goes through this module.

```python
class ZellijClient:
    async def list_sessions() -> list[Session]
    async def new_session(name, layout=None, cwd=None) -> Session
    async def kill_session(name) -> None
    async def attach_session(name) -> None

    # Pane operations (require ZELLIJ_SESSION_NAME env)
    async def list_panes(session=None) -> list[Pane]
    async def new_pane(name=None, command=None, direction=None, cwd=None) -> Pane
    async def close_pane(pane_id=None) -> None
    async def focus_pane(pane_id) -> None
    async def write_to_pane(text, pane_id=None) -> None
    async def dump_pane(pane_id=None, lines=None) -> str
    async def run_command(command, pane_name=None) -> None
```

Key design decisions:
- Uses `asyncio.create_subprocess_exec` for all CLI calls
- Session-scoped operations use `zellij --session <name> action ...`
- Screen dumps via `zellij action dump-screen /tmp/zpilot/dump-{pane_id}.txt`
- Returns typed dataclasses, not raw strings

### 2. MCP Server (`mcp_server.py`)

Exposes Zellij operations as MCP tools. Runs via stdio transport.

**Session Tools:**
| Tool | Description |
|------|-------------|
| `list_sessions` | List all Zellij sessions with status |
| `create_session` | Create named session with optional layout |
| `kill_session` | Kill a session by name |
| `session_summary` | AI-friendly summary of all sessions |

**Pane Tools:**
| Tool | Description |
|------|-------------|
| `list_panes` | List panes in a session |
| `create_pane` | Create a named pane with optional command |
| `read_pane` | Dump screen content (last N lines) |
| `write_to_pane` | Send keystrokes/text to a pane |
| `run_in_pane` | Execute a command in a named pane |
| `close_pane` | Close a pane by name |

**AI Session Tools:**
| Tool | Description |
|------|-------------|
| `launch_copilot` | Create pane + start copilot-cli with a task |
| `check_all` | Status check across all tracked panes |
| `get_pane_activity` | Idle time, last output, state for a pane |

### 3. Idle/Completion Detector (`detector.py`)

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

### 7. TUI Dashboard (`tui/dashboard.py`)

Built with Textual. Runs inside a Zellij pane.

**Layout:**
```
┌─ zpilot ─────────────────────────────────────────────────┐
│ Sessions                                          12:34  │
│ ┌────────────────┬────────────────┬────────────────┐     │
│ │ ▶ copilot-1    │   copilot-2    │   build-srv    │     │
│ │ 🔔 WAITING     │ ⏳ ACTIVE      │ ✅ IDLE        │     │
│ │ idle: 45s      │ busy: 2m       │ idle: 5m       │     │
│ │ last: "Done.   │ last: "Compil  │ last: "$ "     │     │
│ │  Need review"  │  ing src/..."  │                │     │
│ └────────────────┴────────────────┴────────────────┘     │
│                                                           │
│ Events                                                    │
│  09:14:22  copilot-1  ACTIVE → WAITING (BEL detected)   │
│  09:12:01  copilot-2  IDLE → ACTIVE                      │
│  09:10:45  build-srv  ACTIVE → IDLE                      │
│                                                           │
│ [n]ew session  [a]ttach  [k]ill  [r]efresh  [q]uit      │
└──────────────────────────────────────────────────────────┘
```

**Actions:**
- `n` — create new session (prompts for name + command)
- `Enter` / `a` — switch to selected session's Zellij tab
- `k` — kill selected session
- `r` — force refresh
- `q` — quit dashboard (sessions keep running)

## CLI Interface

```bash
zpilot                    # launch TUI dashboard
zpilot serve              # start MCP server (stdio)
zpilot daemon             # start background watcher
zpilot status             # one-shot status of all sessions
zpilot new <name> [cmd]   # create a new tracked session
zpilot notify-test        # test notification delivery
zpilot config             # show/edit configuration
```

## Configuration

`~/.config/zpilot/config.toml`:

```toml
[general]
poll_interval = 5           # seconds between daemon polls
idle_threshold = 30         # seconds of no output = idle
events_file = "/tmp/zpilot/events.jsonl"

[detection]
bel_enabled = true          # detect \x07 terminal bell
prompt_patterns = [         # regex patterns that indicate "waiting for input"
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

[notifications]
enabled = true
adapter = "ntfy"
topic = "zpilot"
server = "https://ntfy.sh"
notify_on = ["waiting", "error", "exited"]

[sessions]
auto_track = true           # automatically track new Zellij sessions
```

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
│       ├── daemon.py           # Background session watcher
│       ├── detector.py         # Idle/BEL/prompt detection
│       ├── events.py           # Event bus (file-based)
│       ├── mcp_server.py       # MCP server (tools)
│       ├── models.py           # Dataclasses (Session, Pane, Event, etc.)
│       ├── notifications.py    # Notification adapters
│       ├── zellij.py           # Zellij CLI wrapper
│       └── tui/
│           ├── __init__.py
│           └── dashboard.py    # Textual dashboard
└── tests/
    ├── test_detector.py
    ├── test_events.py
    ├── test_zellij.py
    └── test_notifications.py
```

## Dependencies

- **mcp** — MCP SDK (already installed)
- **click** — CLI framework (already installed)
- **textual** — TUI framework
- **tomli** — TOML config parsing (stdlib in 3.11+)
- **httpx** — async HTTP for notification adapters

No Rust, no WASM, no heavy dependencies. Pure Python.

## Open Questions

1. Should the TUI also be usable as a web UI (Textual supports `textual-web`)?
2. Should we support tmux as an alternative backend to Zellij?
3. Should the MCP server and daemon be the same process or separate?
4. How to handle copilot-cli authentication across multiple sessions?
