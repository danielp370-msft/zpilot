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
