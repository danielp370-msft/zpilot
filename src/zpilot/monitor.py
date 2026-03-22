"""Fleet monitor — polls nodes and tracks health across the fleet."""

from __future__ import annotations

import asyncio
import logging
import time

from . import zellij
from .detector import PaneDetector
from .events import EventBus
from .models import (
    Event, FleetStatus, NodeHealth, NodeState,
    PaneState, SessionHealth, ZpilotConfig,
)
from .nodes import Node, NodeRegistry

log = logging.getLogger("zpilot.monitor")


class Monitor:
    """Monitors all nodes in the fleet, tracks session health."""

    def __init__(
        self,
        registry: NodeRegistry,
        config: ZpilotConfig,
        event_bus: EventBus,
    ):
        self.registry = registry
        self.config = config
        self.event_bus = event_bus
        self.detector = PaneDetector(config)
        self._running = False
        self._fleet: FleetStatus = FleetStatus()
        self._prev_states: dict[str, PaneState] = {}  # "node:session:pane" → state
        self.stuck_threshold: float = 300.0  # 5 min idle = stuck

    @property
    def fleet_status(self) -> FleetStatus:
        return self._fleet

    async def poll_node(self, node: Node) -> NodeHealth:
        """Poll a single node for session health."""
        health = NodeHealth(name=node.name)

        # Check connectivity
        try:
            alive = await node.transport.is_alive()
            if not alive:
                health.state = NodeState.UNREACHABLE
                health.error = "ping failed"
                return health
        except Exception as e:
            health.state = NodeState.OFFLINE
            health.error = str(e)
            return health

        health.state = NodeState.ONLINE
        health.last_ping = time.time()

        # Get sessions
        try:
            if node.is_local:
                sessions = await zellij.list_sessions()
            else:
                # Remote: run zpilot-agent list-sessions
                result = await node.transport.exec(
                    "zpilot-agent list-sessions 2>/dev/null || "
                    "zellij list-sessions --no-formatting 2>/dev/null",
                    timeout=15.0,
                )
                if not result.ok:
                    health.sessions = []
                    return health
                # Parse remote session listing
                sessions = _parse_remote_sessions(result.stdout)
        except Exception as e:
            log.warning(f"Failed to list sessions on {node.name}: {e}")
            health.error = str(e)
            return health

        # Check each session
        for session in sessions:
            sh = await self._check_session(node, session.name)
            health.sessions.append(sh)

            # Detect state changes and emit events
            key = f"{node.name}:{session.name}:focused"
            prev = self._prev_states.get(key, PaneState.UNKNOWN)
            if sh.state != prev:
                self.event_bus.emit(Event(
                    event_type="state_change",
                    session=session.name,
                    pane="focused",
                    old_state=prev.value,
                    new_state=sh.state.value,
                    node=node.name,
                    details=f"idle={sh.idle_seconds:.0f}s",
                ))
            self._prev_states[key] = sh.state

        return health

    async def _check_session(self, node: Node, session_name: str) -> SessionHealth:
        """Get health of a single session."""
        try:
            if node.is_local:
                content = await zellij.dump_pane(session=session_name)
            else:
                result = await node.transport.exec(
                    f"zpilot-agent dump-pane {session_name} 2>/dev/null || "
                    f"zellij action dump-screen /dev/stdout --session {session_name} 2>/dev/null",
                    timeout=10.0,
                )
                content = result.stdout if result.ok else ""

            state = self.detector.detect(
                session=f"{node.name}:{session_name}",
                pane="focused",
                content=content,
            )
            idle = self.detector.get_idle_seconds(
                f"{node.name}:{session_name}", "focused"
            )
            last_line = ""
            lines = content.strip().splitlines()
            if lines:
                last_line = lines[-1][:80]

            return SessionHealth(
                node=node.name,
                session=session_name,
                state=state,
                idle_seconds=idle,
                last_line=last_line,
            )
        except Exception as e:
            return SessionHealth(
                node=node.name,
                session=session_name,
                state=PaneState.UNKNOWN,
                error=str(e),
            )

    async def poll_all(self) -> FleetStatus:
        """Poll all nodes concurrently."""
        nodes = self.registry.all()
        tasks = [self.poll_node(node) for node in nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        fleet = FleetStatus()
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                fleet.nodes.append(NodeHealth(
                    name=nodes[i].name,
                    state=NodeState.OFFLINE,
                    error=str(result),
                ))
            else:
                fleet.nodes.append(result)

        self._fleet = fleet
        return fleet

    def stuck_sessions(self) -> list[SessionHealth]:
        """Sessions idle longer than stuck_threshold."""
        stuck = []
        for node_health in self._fleet.nodes:
            for sh in node_health.sessions:
                if sh.state in (PaneState.IDLE, PaneState.ACTIVE) and \
                   sh.idle_seconds >= self.stuck_threshold:
                    stuck.append(sh)
        return stuck

    def idle_nodes(self) -> list[NodeHealth]:
        """Nodes that are online with zero active sessions."""
        return [
            n for n in self._fleet.nodes
            if n.state == NodeState.ONLINE and n.busy_count == 0
        ]

    async def run(self, interval: float | None = None) -> None:
        """Main monitor loop — polls fleet periodically."""
        interval = interval or self.config.poll_interval
        self._running = True
        log.info(f"Monitor started (interval={interval}s, nodes={len(self.registry)})")

        while self._running:
            try:
                await self.poll_all()
                log.debug(f"Fleet: {self._fleet.summary()}")
            except Exception as e:
                log.error(f"Monitor poll error: {e}")
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False


def _parse_remote_sessions(output: str):
    """Parse `zellij list-sessions` output into Session-like objects."""
    from .models import Session as SessionModel

    sessions = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # zellij list-sessions output: "session_name [Created ...]" or just "session_name"
        name = line.split()[0] if line else ""
        if name:
            sessions.append(SessionModel(name=name))
    return sessions


async def health_check_nodes(
    registry: NodeRegistry,
) -> dict[str, dict]:
    """Check connectivity and latency for all nodes in the registry.

    Returns a dict mapping node names to health info:
        {
            "node_name": {
                "alive": bool,
                "latency_ms": float,
                "error": str | None,
            }
        }
    """
    results: dict[str, dict] = {}
    for node in registry.all():
        t0 = time.monotonic()
        error = None
        try:
            alive = await node.transport.is_alive()
        except Exception as e:
            alive = False
            error = str(e)
        latency_ms = (time.monotonic() - t0) * 1000
        results[node.name] = {
            "alive": alive,
            "latency_ms": round(latency_ms, 1),
            "error": error,
        }
    return results


class NodeHealthTracker:
    """Periodically checks node health via /health endpoint and tracks latency.

    Maintains per-node state with automatic online/offline transitions
    based on configurable failure thresholds.
    """

    def __init__(
        self,
        registry: NodeRegistry,
        *,
        check_interval: float = 30.0,
        offline_threshold: int = 3,
        degraded_threshold: int = 1,
    ):
        self.registry = registry
        self.check_interval = check_interval
        self.offline_threshold = offline_threshold
        self.degraded_threshold = degraded_threshold
        self._running = False
        # Per-node health data: node_name → {...}
        self._health: dict[str, dict] = {}

    def _init_node(self, name: str) -> dict:
        """Create default health entry for a node."""
        entry = {
            "state": "unknown",
            "latency_ms": 0.0,
            "last_seen": None,
            "consecutive_failures": 0,
            "last_check": None,
            "error": None,
            "capabilities": {},
        }
        self._health[name] = entry
        return entry

    async def check_node(self, node: Node) -> dict:
        """Ping a single node's /health endpoint and update tracking state."""
        entry = self._health.get(node.name) or self._init_node(node.name)

        if node.is_local:
            # Local node is always online
            entry.update({
                "state": "online",
                "latency_ms": 0.0,
                "last_seen": time.time(),
                "consecutive_failures": 0,
                "last_check": time.time(),
                "error": None,
            })
            return entry

        t0 = time.monotonic()
        try:
            alive = await node.transport.is_alive()
            latency_ms = round((time.monotonic() - t0) * 1000, 1)

            if alive:
                entry["latency_ms"] = latency_ms
                entry["last_seen"] = time.time()
                entry["consecutive_failures"] = 0
                entry["error"] = None
                entry["state"] = "online"
                # Try to fetch capabilities from health response if MCP transport
                if node.transport_type == "mcp":
                    caps = await self._fetch_capabilities(node)
                    if caps:
                        entry["capabilities"] = caps
            else:
                entry["consecutive_failures"] += 1
                entry["error"] = "health check returned not alive"
                entry["latency_ms"] = latency_ms
        except Exception as e:
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            entry["consecutive_failures"] += 1
            entry["error"] = str(e)
            entry["latency_ms"] = latency_ms

        entry["last_check"] = time.time()

        # State transitions
        failures = entry["consecutive_failures"]
        if failures >= self.offline_threshold:
            entry["state"] = "offline"
        elif failures >= self.degraded_threshold:
            entry["state"] = "degraded"

        self._health[node.name] = entry
        return entry

    async def _fetch_capabilities(self, node: Node) -> dict:
        """Fetch extended health info from an MCP node's /health endpoint."""
        try:
            import httpx
            transport = node.transport
            if not hasattr(transport, "base_url"):
                return {}
            async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
                resp = await client.get(f"{transport.base_url}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        k: v for k, v in data.items()
                        if k not in ("status",)
                    }
        except Exception:
            pass
        return {}

    async def check_all(self) -> dict[str, dict]:
        """Check health of all nodes in the registry."""
        for node in self.registry.all():
            await self.check_node(node)
        return dict(self._health)

    def get_health(self, node_name: str | None = None) -> dict:
        """Get current health data. If node_name given, return that node's data."""
        if node_name:
            if node_name in self._health:
                return self._health[node_name]
            return {"name": node_name, "state": "unknown", "latency_ms": 0.0,
                    "last_seen": None, "consecutive_failures": 0,
                    "last_check": None, "error": None, "capabilities": {}}
        return dict(self._health)

    def all_health(self) -> list[dict]:
        """Return health data for all tracked nodes as a list."""
        result = []
        for node in self.registry.all():
            if node.name in self._health:
                entry = self._health[node.name]
            else:
                entry = {"state": "unknown", "latency_ms": 0.0,
                         "last_seen": None, "consecutive_failures": 0,
                         "last_check": None, "error": None, "capabilities": {}}
            result.append({"name": node.name, **entry})
        return result

    async def run(self) -> None:
        """Periodically check all nodes."""
        self._running = True
        log.info(
            "NodeHealthTracker started (interval=%.0fs, offline_threshold=%d)",
            self.check_interval, self.offline_threshold,
        )
        while self._running:
            try:
                await self.check_all()
            except Exception as e:
                log.error("NodeHealthTracker error: %s", e)
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        self._running = False
