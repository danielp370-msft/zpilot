"""Extended monitor tests with mocked node transports.

Tests Monitor class methods: poll_node, poll_all, stuck_sessions,
idle_nodes, _parse_remote_sessions, and health_check_nodes.
"""

import sys

sys.path.insert(0, "src")

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zpilot.events import EventBus
from zpilot.models import (
    FleetStatus,
    NodeHealth,
    NodeState,
    PaneState,
    Session,
    SessionHealth,
    ZpilotConfig,
)
from zpilot.monitor import Monitor, _parse_remote_sessions, health_check_nodes
from zpilot.nodes import Node, NodeRegistry


@pytest.fixture
def config():
    return ZpilotConfig(poll_interval=1.0, idle_threshold=5.0)


@pytest.fixture
def event_bus(tmp_path):
    return EventBus(str(tmp_path / "events.jsonl"))


@pytest.fixture
def local_node():
    node = Node(name="local", transport_type="local")
    return node


@pytest.fixture
def remote_node():
    node = Node(name="remote1", transport_type="ssh", host="10.0.0.1")
    mock_transport = AsyncMock()
    node._transport = mock_transport
    return node


# ── _parse_remote_sessions ───────────────────────────────────────────

class TestParseRemoteSessions:
    def test_simple_names(self):
        output = "session-1\nsession-2\nsession-3\n"
        result = _parse_remote_sessions(output)
        assert len(result) == 3
        assert result[0].name == "session-1"
        assert result[2].name == "session-3"

    def test_zellij_format_with_metadata(self):
        output = "my-build [Created 2h ago]\nother [Created 5m ago]\n"
        result = _parse_remote_sessions(output)
        assert len(result) == 2
        assert result[0].name == "my-build"
        assert result[1].name == "other"

    def test_empty_output(self):
        result = _parse_remote_sessions("")
        assert result == []

    def test_blank_lines_ignored(self):
        output = "sess1\n\n\nsess2\n"
        result = _parse_remote_sessions(output)
        assert len(result) == 2


# ── Monitor.poll_node (local) ────────────────────────────────────────

class TestPollNodeLocal:
    @pytest.mark.asyncio
    async def test_poll_local_online(self, config, event_bus, local_node):
        sessions = [Session(name="build-session")]
        monitor = Monitor(NodeRegistry([local_node]), config, event_bus)

        with patch("zpilot.monitor.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(return_value="$ make\nCompiling...\n")

            nh = await monitor.poll_node(local_node)

            assert nh.state == NodeState.ONLINE
            assert nh.name == "local"
            assert len(nh.sessions) == 1
            assert nh.sessions[0].session == "build-session"

    @pytest.mark.asyncio
    async def test_poll_local_no_sessions(self, config, event_bus, local_node):
        monitor = Monitor(NodeRegistry([local_node]), config, event_bus)

        with patch("zpilot.monitor.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=[])
            mock_z.dump_pane = AsyncMock(return_value="")

            nh = await monitor.poll_node(local_node)
            assert nh.state == NodeState.ONLINE
            assert nh.sessions == []


# ── Monitor.poll_node (remote) ───────────────────────────────────────

class TestPollNodeRemote:
    @pytest.mark.asyncio
    async def test_poll_remote_online(self, config, event_bus, remote_node):
        monitor = Monitor(NodeRegistry([remote_node]), config, event_bus)

        remote_node._transport.is_alive = AsyncMock(return_value=True)
        # First exec: list sessions, second exec: dump pane
        exec_result_sessions = MagicMock()
        exec_result_sessions.ok = True
        exec_result_sessions.stdout = "remote-sess\n"

        exec_result_pane = MagicMock()
        exec_result_pane.ok = True
        exec_result_pane.stdout = "$ hello\n"

        remote_node._transport.exec = AsyncMock(
            side_effect=[exec_result_sessions, exec_result_pane]
        )

        nh = await monitor.poll_node(remote_node)

        assert nh.state == NodeState.ONLINE
        assert len(nh.sessions) == 1
        assert nh.sessions[0].session == "remote-sess"

    @pytest.mark.asyncio
    async def test_poll_remote_unreachable(self, config, event_bus, remote_node):
        monitor = Monitor(NodeRegistry([remote_node]), config, event_bus)
        remote_node._transport.is_alive = AsyncMock(return_value=False)

        nh = await monitor.poll_node(remote_node)

        assert nh.state == NodeState.UNREACHABLE
        assert nh.error == "ping failed"

    @pytest.mark.asyncio
    async def test_poll_remote_transport_error(self, config, event_bus, remote_node):
        monitor = Monitor(NodeRegistry([remote_node]), config, event_bus)
        remote_node._transport.is_alive = AsyncMock(
            side_effect=ConnectionError("ssh timeout")
        )

        nh = await monitor.poll_node(remote_node)

        assert nh.state == NodeState.OFFLINE
        assert "ssh timeout" in nh.error


# ── Monitor.poll_all ─────────────────────────────────────────────────

class TestPollAll:
    @pytest.mark.asyncio
    async def test_poll_all_multiple_nodes(self, config, event_bus, local_node, remote_node):
        registry = NodeRegistry([local_node, remote_node])
        monitor = Monitor(registry, config, event_bus)

        with patch("zpilot.monitor.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=[Session(name="s1")])
            mock_z.dump_pane = AsyncMock(return_value="$ ")

            remote_node._transport.is_alive = AsyncMock(return_value=False)

            fleet = await monitor.poll_all()

            assert isinstance(fleet, FleetStatus)
            assert fleet.total_nodes == 2
            assert fleet.online_count == 1  # only local

    @pytest.mark.asyncio
    async def test_poll_all_handles_exceptions(self, config, event_bus):
        bad_node = Node(name="bad", transport_type="ssh", host="x.x.x.x")
        bad_transport = AsyncMock()
        bad_transport.is_alive = AsyncMock(side_effect=Exception("boom"))
        bad_node._transport = bad_transport

        registry = NodeRegistry([bad_node])
        monitor = Monitor(registry, config, event_bus)

        fleet = await monitor.poll_all()
        # The exception node should appear as OFFLINE
        assert fleet.total_nodes == 1
        assert fleet.nodes[0].state == NodeState.OFFLINE


# ── stuck_sessions / idle_nodes ──────────────────────────────────────

class TestStuckAndIdle:
    def test_stuck_sessions(self, config, event_bus, local_node):
        monitor = Monitor(NodeRegistry([local_node]), config, event_bus)
        monitor.stuck_threshold = 100.0

        # Build a fleet status manually
        monitor._fleet = FleetStatus(nodes=[
            NodeHealth(
                name="local",
                state=NodeState.ONLINE,
                sessions=[
                    SessionHealth(
                        node="local", session="stuck1",
                        state=PaneState.IDLE, idle_seconds=200.0,
                    ),
                    SessionHealth(
                        node="local", session="ok",
                        state=PaneState.ACTIVE, idle_seconds=5.0,
                    ),
                    SessionHealth(
                        node="local", session="stuck2",
                        state=PaneState.ACTIVE, idle_seconds=500.0,
                    ),
                ],
            ),
        ])

        stuck = monitor.stuck_sessions()
        assert len(stuck) == 2
        names = {s.session for s in stuck}
        assert "stuck1" in names
        assert "stuck2" in names

    def test_idle_nodes(self, config, event_bus, local_node):
        monitor = Monitor(NodeRegistry([local_node]), config, event_bus)

        monitor._fleet = FleetStatus(nodes=[
            NodeHealth(
                name="local", state=NodeState.ONLINE,
                sessions=[
                    SessionHealth(
                        node="local", session="s1",
                        state=PaneState.IDLE, idle_seconds=50.0,
                    ),
                ],
            ),
            NodeHealth(
                name="busy-node", state=NodeState.ONLINE,
                sessions=[
                    SessionHealth(
                        node="busy", session="s2",
                        state=PaneState.ACTIVE, idle_seconds=1.0,
                    ),
                ],
            ),
        ])

        idle = monitor.idle_nodes()
        assert len(idle) == 1
        assert idle[0].name == "local"


# ── Monitor.run / stop ───────────────────────────────────────────────

class TestMonitorRunStop:
    @pytest.mark.asyncio
    async def test_run_and_stop(self, config, event_bus, local_node):
        monitor = Monitor(NodeRegistry([local_node]), config, event_bus)

        with patch.object(monitor, "poll_all", new_callable=AsyncMock, return_value=FleetStatus()):
            # Stop after one iteration
            async def stop_soon():
                await asyncio.sleep(0.1)
                monitor.stop()

            task = asyncio.create_task(stop_soon())
            await monitor.run(interval=0.05)
            await task

            assert monitor._running is False
            monitor.poll_all.assert_awaited()


# ── health_check_nodes ───────────────────────────────────────────────

class TestHealthCheckNodes:
    @pytest.mark.asyncio
    async def test_health_check_with_mocked_nodes(self):
        alive_node = Node(name="alive", transport_type="local")
        alive_transport = AsyncMock()
        alive_transport.is_alive = AsyncMock(return_value=True)
        alive_node._transport = alive_transport

        dead_node = Node(name="dead", transport_type="ssh", host="x.x.x.x")
        dead_transport = AsyncMock()
        dead_transport.is_alive = AsyncMock(return_value=False)
        dead_node._transport = dead_transport

        registry = NodeRegistry([alive_node, dead_node])
        results = await health_check_nodes(registry)

        assert results["alive"]["alive"] is True
        assert results["alive"]["error"] is None
        assert results["alive"]["latency_ms"] >= 0

        assert results["dead"]["alive"] is False

    @pytest.mark.asyncio
    async def test_health_check_transport_error(self):
        err_node = Node(name="err", transport_type="ssh", host="x")
        err_transport = AsyncMock()
        err_transport.is_alive = AsyncMock(side_effect=OSError("conn refused"))
        err_node._transport = err_transport

        registry = NodeRegistry([err_node])
        results = await health_check_nodes(registry)

        assert results["err"]["alive"] is False
        assert "conn refused" in results["err"]["error"]


# ── State change event emission ──────────────────────────────────────

class TestStateChangeEvents:
    @pytest.mark.asyncio
    async def test_emits_event_on_state_change(self, config, event_bus, local_node):
        monitor = Monitor(NodeRegistry([local_node]), config, event_bus)

        sessions = [Session(name="changing-session")]

        with patch("zpilot.monitor.zellij") as mock_z:
            mock_z.list_sessions = AsyncMock(return_value=sessions)
            mock_z.dump_pane = AsyncMock(return_value="$ ")

            # First poll sets initial state
            await monitor.poll_node(local_node)
            # Change output to trigger state change
            mock_z.dump_pane = AsyncMock(return_value="Error: build failed\n")
            await monitor.poll_node(local_node)

            # Check events were emitted
            events = event_bus.recent(10)
            state_changes = [e for e in events if e.event_type == "state_change"]
            assert len(state_changes) >= 1
