# zpilot ⚡

Mission control for AI coding sessions.

Monitor multiple copilot-cli / AI agent terminals from one place.
Get notified when they need input. Jump in when needed.

## Quick Start

```bash
pip install -e .

# Start the dashboard daemon
zpilot up

# Create some AI coding sessions
zpilot new auth-fix "copilot-cli --task 'fix the auth bug'"
zpilot new api-tests "copilot-cli --task 'write API tests'"
zpilot new refactor "copilot-cli --task 'refactor user model'"

# Monitor from anywhere
zpilot              # TUI dashboard (terminal)
zpilot status       # one-shot status check
open http://localhost:8095  # web dashboard (browser)

# Stop when done
zpilot down
```

## Commands

| Command | Description |
|---------|-------------|
| `zpilot` | Launch TUI dashboard (default) |
| `zpilot up` | Start web dashboard as background daemon |
| `zpilot down` | Stop background daemon |
| `zpilot new <name> [cmd]` | Create a new monitored Zellij session |
| `zpilot status` | One-shot status check of all sessions |
| `zpilot web` | Run web dashboard in foreground |
| `zpilot serve` | Start MCP server (for AI agents) |
| `zpilot daemon` | Start background session watcher with notifications |

### Remote / Headless Usage

```bash
ssh remote-box

# Start zpilot (localhost only by default — secure)
zpilot up

# Or expose for remote browser access
zpilot up --host 0.0.0.0

# Create sessions, disconnect — everything keeps running
zpilot new task-1 "copilot-cli ..."
exit

# Reconnect later
ssh remote-box
zpilot status          # sessions are still running
zpilot                 # TUI
zpilot down            # clean shutdown
```

## Web Dashboard

The web dashboard (`zpilot up` or `zpilot web`) provides:

- **Real-time terminal panels** — xterm.js with WebSocket, full ANSI/cursor support
- **Multi-panel layouts** — single, side-by-side, stacked, 2×2 grid
- **Live state detection** — waiting, active, idle, error, exited
- **Event log** — state transitions with timestamps
- **Themes** — Dark, Cyberpunk, Monokai, Light (⚙ settings gear)
- **Session management** — create, dock/undock, monitor

Terminal dimensions sync from the browser to the PTY — panels use the
full available width, not a fixed 80×24.

## State Detection

zpilot automatically detects session state (priority order):

1. **BEL** — terminal bell (`\x07`) = copilot waiting for input → `waiting`
2. **Prompt pattern** — regex matches (e.g., `$ `, `> `, `❯ `) → `waiting`
3. **Quiescence** — no output for N seconds → `idle`
4. **Active** — recent output, no prompt detected → `active`

## Architecture

See [DESIGN.md](DESIGN.md) for full architecture.

```
AI Agent → MCP Server → Zellij CLI Wrapper → Zellij Terminal Multiplexer
                              ↓
                          Detector (state detection)
                              ↓
                           Event Bus
                          ↙        ↘
                    TUI Dashboard  Daemon → Notifications
                                       ↘
                                    Web Dashboard (xterm.js + WebSocket)
```

**Components:**
- **Web Dashboard** — FastAPI + xterm.js with live terminal rendering
- **TUI Dashboard** — Textual-based terminal UI with keyboard shortcuts
- **MCP Server** — exposes session management as MCP tools for AI agents
- **Daemon** — background watcher with pluggable notifications
- **Shell Wrapper** — PTY fork with output logging, FIFO command injection, resize support

**Notification adapters:** ntfy.sh, desktop (`notify-send`), webhook, log file

## Requirements

- Python 3.11+
- [Zellij](https://zellij.dev/) terminal multiplexer
