"""MCP server exposing Zellij session management tools."""

from __future__ import annotations

import json
import logging
import re as _re
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import ops, zellij
from .config import load_config
from .detector import PaneDetector
from .events import EventBus
from .models import PaneState, ZpilotConfig
from .nodes import Node, NodeRegistry, load_nodes
from .monitor import Monitor, NodeHealthTracker, health_check_nodes

log = logging.getLogger("zpilot.mcp")


def create_mcp_server(config: ZpilotConfig | None = None) -> Server:
    """Create and configure the MCP server with all tools."""
    config = config or load_config()
    server = Server("zpilot")
    detector = PaneDetector(config)
    event_bus = EventBus(config.events_file)
    registry = NodeRegistry(load_nodes())
    monitor = Monitor(registry, config, event_bus)
    health_tracker = NodeHealthTracker(registry)

    # Common node param schema fragment
    NODE_PARAM = {
        "type": "string",
        "description": "Target node (default 'local'). Use list_nodes to see available.",
        "default": "local",
    }

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
                            "description": "Target session name. Use 'node:session' for remote nodes (e.g. 'dandroid1:zpilot-test')",
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
                            "description": "Session name (uses current if omitted). Use 'node:session' for remote nodes.",
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
                            "description": "Target session. Use 'node:session' for remote nodes.",
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
                            "description": "Target session. Use 'node:session' for remote nodes (e.g. 'dandroid1:zpilot-test')",
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
                            "description": "Session name. Use 'node:session' for remote nodes.",
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
                            "description": "Session name. Use 'node:session' for remote nodes.",
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
                            "description": "Session name. Use 'node:session' for remote nodes.",
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
            Tool(
                name="send_keys",
                description=(
                    "Send special keys to a session (arrow keys, ctrl combos, function keys). "
                    "Supported keys: enter, tab, escape, backspace, ctrl_c, ctrl_d, ctrl_z, ctrl_l, "
                    "ctrl_a, ctrl_e, ctrl_r, ctrl_u, ctrl_w, arrow_up, arrow_down, arrow_left, "
                    "arrow_right, home, end, page_up, page_down, insert, delete, f1-f12. "
                    "Can send multiple keys in sequence."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {
                            "type": "string",
                            "description": "Session name. Use 'node:session' for remote nodes.",
                        },
                        "keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of key names to send in order (e.g. ['arrow_up', 'enter'])",
                        },
                    },
                    "required": ["session", "keys"],
                },
            ),
            # ── Fleet management tools ──
            Tool(
                name="list_nodes",
                description="List all configured zpilot nodes and their connectivity status",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="ping_node",
                description="Check if a node is reachable",
                inputSchema={
                    "type": "object",
                    "properties": {"node": NODE_PARAM},
                    "required": ["node"],
                },
            ),
            Tool(
                name="fleet_status",
                description="Get health summary of all nodes — sessions, states, idle times",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="node_sessions",
                description="List sessions on a specific remote node",
                inputSchema={
                    "type": "object",
                    "properties": {"node": NODE_PARAM},
                    "required": ["node"],
                },
            ),
            Tool(
                name="list_siblings",
                description="List known peer nodes (for mesh discovery). Returns node names, transport types, and labels.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="fleet_health",
                description="Get health summary of all nodes — latency, state, last_seen",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    # ── Tool implementations ────────────────────────────────────

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            result = await _dispatch(
                name, arguments, config, detector, event_bus,
                registry, monitor, health_tracker,
            )
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
    registry: NodeRegistry | None = None,
    monitor: Monitor | None = None,
    health_tracker: NodeHealthTracker | None = None,
) -> str:
    """Dispatch a tool call to the appropriate handler."""

    # ── Fleet management tools ──────────────────────────────────

    if name == "list_nodes":
        reg = registry or NodeRegistry()
        nodes = ops.list_nodes(reg)
        lines = []
        for n in nodes:
            labels = ", ".join(f"{k}={v}" for k, v in n["labels"].items()) or "-"
            lines.append(f"  {n['name']}  [{n['transport']}]  {n['host']}  labels: {labels}")
        return f"Nodes ({len(nodes)}):\n" + "\n".join(lines)

    elif name == "ping_node":
        reg = registry or NodeRegistry()
        result = await ops.ping_node(reg, args["node"])
        icon = "✓" if result["reachable"] else "✗"
        status = "reachable" if result["reachable"] else result.get("error", "unreachable")
        return f"{icon} {result['node']}: {status}"

    elif name == "fleet_status":
        if monitor:
            fleet = await monitor.poll_all()
            lines = [fleet.summary(), ""]
            # Connectivity and latency data
            health_data = {}
            if registry:
                health_data = await health_check_nodes(registry)
            for nh in fleet.nodes:
                status = f"{'●' if nh.state.value == 'online' else '○'} {nh.name}: {nh.state.value}"
                if nh.error:
                    status += f" ({nh.error})"
                if nh.sessions:
                    status += f" — {nh.total_sessions} sessions ({nh.busy_count} busy, {nh.idle_count} idle)"
                hd = health_data.get(nh.name)
                if hd:
                    status += f" [{hd['latency_ms']:.0f}ms]"
                    if hd.get("error"):
                        status += f" ⚠ {hd['error']}"
                lines.append(status)
            stuck = monitor.stuck_sessions()
            if stuck:
                lines.append(f"\n⚠ {len(stuck)} stuck session(s):")
                for s in stuck:
                    lines.append(f"  {s.node}:{s.session} idle {s.idle_seconds:.0f}s")
            return "\n".join(lines)
        return "Monitor not available."

    elif name == "list_siblings":
        reg = registry or NodeRegistry()
        nodes = ops.list_nodes(reg)
        siblings = [{"name": n["name"], "transport": n["transport"], "host": n["host"], "labels": n["labels"]} for n in nodes]
        import json as _json
        return _json.dumps({"siblings": siblings, "count": len(siblings)}, indent=2)

    elif name == "fleet_health":
        tracker = health_tracker or NodeHealthTracker(registry or NodeRegistry())
        health_data = await tracker.check_all()
        nodes_list = tracker.all_health()
        lines = []
        online = sum(1 for n in nodes_list if n.get("state") == "online")
        total = len(nodes_list)
        lines.append(f"Fleet Health: {online}/{total} nodes online")
        lines.append("")
        for n in nodes_list:
            icon = "●" if n.get("state") == "online" else "◌" if n.get("state") == "degraded" else "○"
            line = f"  {icon} {n['name']}: {n.get('state', 'unknown')}"
            lat = n.get("latency_ms", 0)
            if lat:
                line += f" [{lat:.0f}ms]"
            last_seen = n.get("last_seen")
            if last_seen:
                import time as _time
                ago = _time.time() - last_seen
                if ago < 60:
                    line += f" (seen {ago:.0f}s ago)"
                else:
                    line += f" (seen {ago / 60:.0f}m ago)"
            err = n.get("error")
            if err:
                line += f" ⚠ {err}"
            lines.append(line)
        return "\n".join(lines)

    elif name == "node_sessions":
        reg = registry or NodeRegistry()
        node = reg.get(args["node"])
        if node.is_local:
            sessions = await zellij.list_sessions()
            if not sessions:
                return f"No sessions on {node.name}."
            lines = [f"  {s.name}" + (" (current)" if s.is_current else "") for s in sessions]
            return f"Sessions on {node.name}:\n" + "\n".join(lines)
        else:
            result = await node.transport.exec(
                "zellij list-sessions --no-formatting 2>/dev/null", timeout=15.0
            )
            if not result.ok or not result.stdout.strip():
                return f"No sessions on {node.name} (or unreachable)."
            return f"Sessions on {node.name}:\n" + result.stdout

    # ── Original single-node tools (with remote node support) ────

    elif name == "list_sessions":
        sessions = await ops.list_sessions()
        if not sessions:
            return "No Zellij sessions found."
        lines = []
        for s in sessions:
            marker = " (current)" if s["is_current"] else ""
            lines.append(f"  {s['name']}{marker}")
        return "Sessions:\n" + "\n".join(lines)

    elif name == "create_session":
        result = await ops.create_session(args["name"], args.get("layout"), registry)
        node = result.get("node", "local")
        return f"Created session '{result['session']}'" + (f" on {node}" if node != "local" else "")

    elif name == "kill_session":
        result = await ops.kill_session(args["name"], registry)
        node = result.get("node", "local")
        return f"Killed session '{result['session']}'" + (f" on {node}" if node != "local" else "")

    elif name == "create_pane":
        # create_pane stays here — Zellij-specific with many options
        import shlex
        node, sess = ops.parse_session(args.get("session"), registry)
        if node:
            safe_sess = shlex.quote(sess) if sess else None
            zj_args = f"--session {safe_sess} action new-pane" if safe_sess else "action new-pane"
            if args.get("direction"):
                zj_args += f" --direction {shlex.quote(args['direction'])}"
            if args.get("floating"):
                zj_args += " --floating"
            if args.get("command"):
                zj_args += f" -- {shlex.quote(args['command'])}"
            await ops._remote_zellij(node, zj_args)
            return f"Created pane on {node.name}:{sess or 'current'}"
        await zellij.new_pane(
            session=args.get("session"),
            name=args.get("name"),
            command=args.get("command"),
            direction=args.get("direction"),
            floating=args.get("floating", False),
        )
        return f"Created pane" + (f" '{args.get('name')}'" if args.get("name") else "")

    elif name == "read_pane":
        content = await ops.read_pane(args.get("session"), args.get("full", False), registry)
        return content.strip() if content.strip() else "(empty pane)"

    elif name == "write_to_pane":
        result = await ops.write_to_pane(args["text"], args.get("session"), detector, registry)
        node = result.get("node", "local")
        sess = result.get("session", "current")
        if node != "local":
            return f"Sent {result['chars']} chars to pane on {node}:{sess or 'current'}"
        return f"Sent {result['chars']} chars to pane"

    elif name == "run_in_pane":
        result = await ops.run_in_pane(args["command"], args.get("session"), detector, registry)
        node = result.get("node", "local")
        if node != "local":
            return f"Executed on {node}:{result.get('session', 'current')}: {args['command']}"
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
        result = await ops.check_status(args["session"], detector, registry)
        return json.dumps(result, indent=2)

    elif name == "check_all":
        sessions = await ops.list_sessions()
        if not sessions:
            return "No sessions found."
        results = []
        for s in sessions:
            try:
                status = await ops.check_status(s["name"], detector, registry)
                last_lines = status.get("last_lines", [])
                results.append({
                    "session": s["name"],
                    "state": status.get("state", "unknown"),
                    "idle": status.get("idle_seconds", 0),
                    "last": last_lines[-1][:60] if last_lines else "",
                })
            except Exception as e:
                results.append({"session": s["name"], "state": "error", "error": str(e)})
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
        result = await ops.search_pane(
            args["session"], args["pattern"], args.get("context", 2), registry
        )
        if not result["matches"]:
            return f"No matches for '{args['pattern']}' in {args['session']} ({result['total_lines']} lines searched)."
        formatted = []
        for snippet in result["matches"]:
            lines = []
            for entry in snippet:
                marker = ">>>" if entry["match"] else "   "
                lines.append(f"{marker} {entry['line_num']}: {entry['text']}")
            formatted.append("\n".join(lines))
        header = f"Found {len(result['matches'])} match(es) in {args['session']} ({result['total_lines']} lines):\n"
        return header + "\n---\n".join(formatted[:30])

    elif name == "get_output_history":
        result = await ops.get_output_history(args["session"], args.get("lines", 50), registry)
        if not result["lines"]:
            return "(empty pane)"
        total = result["total_lines"]
        tail = result["lines"]
        header = f"Last {len(tail)} of {total} lines from {args['session']}:\n"
        offset = total - len(tail)
        numbered = [f"{offset + i + 1}: {line}" for i, line in enumerate(tail)]
        return header + "\n".join(numbered)

    elif name == "send_keys":
        result = await ops.send_keys(args["session"], args["keys"], detector, registry)
        lines = []
        for r in result["results"]:
            if r["ok"]:
                lines.append(f"✓ {r['key']}")
            else:
                available = ", ".join(sorted(zellij.SPECIAL_KEYS.keys()))
                lines.append(f"✗ {r['key']} ({r.get('error', 'unknown')} — available: {available})")
        return f"Sent {len(args['keys'])} key(s) to {args['session']}:\n" + "\n".join(lines)

    else:
        return f"Unknown tool: {name}"


async def serve() -> None:
    """Run the MCP server via stdio."""
    config = load_config()
    server = create_mcp_server(config)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
