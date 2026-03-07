"""MCP server exposing Zellij session management tools."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import zellij
from .config import load_config
from .detector import PaneDetector
from .events import EventBus
from .models import PaneState, ZpilotConfig

log = logging.getLogger("zpilot.mcp")


def create_mcp_server(config: ZpilotConfig | None = None) -> Server:
    """Create and configure the MCP server with all tools."""
    config = config or load_config()
    server = Server("zpilot")
    detector = PaneDetector(config)
    event_bus = EventBus(config.events_file)

    # ── Tool definitions ────────────────────────────────────────

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="list_sessions",
                description="List all Zellij terminal sessions with their status",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="create_session",
                description="Create a new Zellij terminal session",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Session name",
                        },
                        "layout": {
                            "type": "string",
                            "description": "Optional layout file path",
                        },
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="kill_session",
                description="Kill a Zellij session by name",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Session name to kill",
                        },
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="create_pane",
                description="Create a new pane in a session, optionally running a command",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {
                            "type": "string",
                            "description": "Target session name",
                        },
                        "name": {
                            "type": "string",
                            "description": "Pane name",
                        },
                        "command": {
                            "type": "string",
                            "description": "Command to run in the pane",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                            "description": "Split direction",
                        },
                        "floating": {
                            "type": "boolean",
                            "description": "Create as floating pane",
                        },
                    },
                },
            ),
            Tool(
                name="read_pane",
                description="Read the current screen content of a pane",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {
                            "type": "string",
                            "description": "Session name (uses current if omitted)",
                        },
                        "full": {
                            "type": "boolean",
                            "description": "Include full scrollback (not just visible)",
                        },
                    },
                },
            ),
            Tool(
                name="write_to_pane",
                description="Send text/keystrokes to a pane",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Text to send",
                        },
                        "session": {
                            "type": "string",
                            "description": "Target session",
                        },
                    },
                    "required": ["text"],
                },
            ),
            Tool(
                name="run_in_pane",
                description="Execute a shell command in a pane (types it and presses Enter)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Command to execute",
                        },
                        "session": {
                            "type": "string",
                            "description": "Target session",
                        },
                    },
                    "required": ["command"],
                },
            ),
            Tool(
                name="launch_copilot",
                description="Create a new pane and start copilot-cli (or other AI agent) in it",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {
                            "type": "string",
                            "description": "Session name (creates new if needed)",
                        },
                        "pane_name": {
                            "type": "string",
                            "description": "Name for the pane",
                        },
                        "agent_command": {
                            "type": "string",
                            "description": "Command to launch (default: copilot-cli)",
                            "default": "copilot-cli",
                        },
                        "task": {
                            "type": "string",
                            "description": "Initial task/prompt to send to the agent",
                        },
                    },
                    "required": ["session"],
                },
            ),
            Tool(
                name="check_status",
                description="Check the status (active/idle/waiting/error) of a session's pane",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {
                            "type": "string",
                            "description": "Session name",
                        },
                    },
                    "required": ["session"],
                },
            ),
            Tool(
                name="check_all",
                description="Get a status summary of all sessions",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="recent_events",
                description="Get recent events from the zpilot event bus",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "description": "Number of events to return (default 20)",
                            "default": 20,
                        },
                    },
                },
            ),
            Tool(
                name="search_pane",
                description="Search a session's full scrollback buffer for a pattern (grep-style). Returns matching lines with line numbers.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {
                            "type": "string",
                            "description": "Session name",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Text or regex pattern to search for",
                        },
                        "context": {
                            "type": "integer",
                            "description": "Lines of context around each match (default 2)",
                            "default": 2,
                        },
                    },
                    "required": ["session", "pattern"],
                },
            ),
            Tool(
                name="get_output_history",
                description="Get the last N lines from a session's scrollback. Use for reviewing what happened recently without reading the entire buffer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {
                            "type": "string",
                            "description": "Session name",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "Number of lines to return from end of scrollback (default 50)",
                            "default": 50,
                        },
                    },
                    "required": ["session"],
                },
            ),
        ]

    # ── Tool implementations ────────────────────────────────────

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            result = await _dispatch(name, arguments, config, detector, event_bus)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    return server


async def _dispatch(
    name: str,
    args: dict[str, Any],
    config: ZpilotConfig,
    detector: PaneDetector,
    event_bus: EventBus,
) -> str:
    """Dispatch a tool call to the appropriate handler."""

    if name == "list_sessions":
        sessions = await zellij.list_sessions()
        if not sessions:
            return "No Zellij sessions found."
        lines = []
        for s in sessions:
            marker = " (current)" if s.is_current else ""
            lines.append(f"  {s.name}{marker}")
        return "Sessions:\n" + "\n".join(lines)

    elif name == "create_session":
        session_name = args["name"]
        layout = args.get("layout")
        await zellij.new_session(session_name, layout=layout)
        return f"Created session '{session_name}'"

    elif name == "kill_session":
        await zellij.kill_session(args["name"])
        return f"Killed session '{args['name']}'"

    elif name == "create_pane":
        await zellij.new_pane(
            session=args.get("session"),
            name=args.get("name"),
            command=args.get("command"),
            direction=args.get("direction"),
            floating=args.get("floating", False),
        )
        return f"Created pane" + (f" '{args.get('name')}'" if args.get("name") else "")

    elif name == "read_pane":
        content = await zellij.dump_pane(
            session=args.get("session"),
            full=args.get("full", False),
        )
        return content if content else "(empty pane)"

    elif name == "write_to_pane":
        await zellij.write_to_pane(args["text"], session=args.get("session"))
        sess = args.get("session", "current")
        detector.record_input(sess, "focused")
        return f"Sent {len(args['text'])} chars to pane"

    elif name == "run_in_pane":
        await zellij.run_command_in_pane(
            args["command"], session=args.get("session")
        )
        sess = args.get("session", "current")
        detector.record_input(sess, "focused")
        return f"Executed: {args['command']}"

    elif name == "launch_copilot":
        session = args["session"]
        pane_name = args.get("pane_name", "copilot")
        agent_cmd = args.get("agent_command", "copilot-cli")
        task = args.get("task")

        # Ensure session exists
        sessions = await zellij.list_sessions()
        session_names = [s.name for s in sessions]
        if session not in session_names:
            await zellij.new_session(session)

        # Create pane with the agent command
        await zellij.new_pane(
            session=session, name=pane_name, command=agent_cmd
        )

        # If a task was provided, wait a moment then send it
        if task:
            import asyncio
            await asyncio.sleep(2)  # let the agent start
            await zellij.write_to_pane(task, session=session)
            await zellij.send_enter(session=session)
            return f"Launched {agent_cmd} in {session}:{pane_name} with task: {task}"

        return f"Launched {agent_cmd} in {session}:{pane_name}"

    elif name == "check_status":
        session = args["session"]
        content = await zellij.dump_pane(session=session)
        state = detector.detect(session=session, pane="focused", content=content)
        idle_secs = detector.get_idle_seconds(session, "focused")
        last_lines = content.strip().splitlines()[-3:] if content.strip() else []
        return json.dumps({
            "session": session,
            "state": state.value,
            "idle_seconds": round(idle_secs, 1),
            "last_lines": last_lines,
        }, indent=2)

    elif name == "check_all":
        sessions = await zellij.list_sessions()
        if not sessions:
            return "No sessions found."
        results = []
        for s in sessions:
            try:
                content = await zellij.dump_pane(session=s.name)
                state = detector.detect(
                    session=s.name, pane="focused", content=content
                )
                idle = detector.get_idle_seconds(s.name, "focused")
                last_line = ""
                lines = content.strip().splitlines()
                if lines:
                    last_line = lines[-1][:60]
                results.append({
                    "session": s.name,
                    "state": state.value,
                    "idle": round(idle, 1),
                    "last": last_line,
                })
            except Exception as e:
                results.append({
                    "session": s.name,
                    "state": "error",
                    "error": str(e),
                })
        return json.dumps(results, indent=2)

    elif name == "recent_events":
        count = args.get("count", 20)
        events = event_bus.recent(count)
        if not events:
            return "No recent events."
        lines = []
        for ev in events:
            import datetime
            ts = datetime.datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S")
            lines.append(
                f"  {ts}  {ev.session}:{ev.pane or '-'}  "
                f"{ev.old_state or '?'} → {ev.new_state}  {ev.details or ''}"
            )
        return "Recent events:\n" + "\n".join(lines)

    elif name == "search_pane":
        import re
        session = args["session"]
        pattern = args["pattern"]
        ctx = args.get("context", 2)
        # Get full scrollback
        content = await zellij.dump_pane(session=session, full=True)
        if not content:
            return "Pane is empty."
        all_lines = content.splitlines()
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            # Fall back to literal search
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
        matches = []
        for i, line in enumerate(all_lines):
            if regex.search(line):
                start = max(0, i - ctx)
                end = min(len(all_lines), i + ctx + 1)
                snippet = []
                for j in range(start, end):
                    marker = ">>>" if j == i else "   "
                    snippet.append(f"{marker} {j+1}: {all_lines[j]}")
                matches.append("\n".join(snippet))
        if not matches:
            return f"No matches for '{pattern}' in {session} scrollback ({len(all_lines)} lines searched)."
        header = f"Found {len(matches)} match(es) in {session} ({len(all_lines)} lines):\n"
        return header + "\n---\n".join(matches[:30])  # cap at 30 matches

    elif name == "get_output_history":
        session = args["session"]
        num_lines = args.get("lines", 50)
        content = await zellij.dump_pane(session=session, full=True)
        if not content:
            return "(empty pane)"
        all_lines = content.strip().splitlines()
        tail = all_lines[-num_lines:]
        total = len(all_lines)
        header = f"Last {len(tail)} of {total} lines from {session}:\n"
        numbered = [f"{total - len(tail) + i + 1}: {line}" for i, line in enumerate(tail)]
        return header + "\n".join(numbered)

    else:
        return f"Unknown tool: {name}"


async def serve() -> None:
    """Run the MCP server via stdio."""
    config = load_config()
    server = create_mcp_server(config)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
