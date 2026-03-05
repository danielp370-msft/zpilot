# zpilot

Mission control for AI coding sessions.

Monitor multiple copilot-cli / AI agent terminals from one place.
Get notified when they need input. Jump in when needed.

## Quick Start

```bash
pip install -e .
zpilot              # launch TUI dashboard
zpilot serve        # start MCP server (for AI agents)
zpilot daemon       # start background session watcher
zpilot status       # one-shot status check
```

## Architecture

See [DESIGN.md](DESIGN.md) for full architecture.

**Components:**
- **Zellij MCP Server** — exposes terminal session management as MCP tools
- **Daemon** — background watcher that detects idle/needs-input states
- **TUI Dashboard** — terminal UI showing session status (Textual)
- **Notification system** — pluggable alerts (ntfy.sh, desktop, webhook)

## Requirements

- Python 3.11+
- [Zellij](https://zellij.dev/) terminal multiplexer
