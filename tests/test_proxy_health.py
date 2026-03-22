"""Tests for proxy routing, NodeHealthTracker, and fleet_health MCP tool."""

from __future__ import annotations

import asyncio
import time

import pytest
import httpx

from zpilot.models import ZpilotConfig
from zpilot.monitor import NodeHealthTracker, health_check_nodes
from zpilot.nodes import Node, NodeRegistry
from zpilot.transport import LocalTransport, ExecResult


# ── Fixtures ────────────────────────────────────────────────────

@pytest.fixture
def test_token():
    return "test-secret-token-proxy"


@pytest.fixture
def app(test_token):
    from zpilot.mcp_http import create_http_app
    config = ZpilotConfig(http_token=test_token)
    return create_http_app(config)


@pytest.fixture
def auth_headers(test_token):
    return {"Authorization": f"Bearer {test_token}"}


@pytest.fixture
def local_registry():
    """Registry with only the local node."""
    return NodeRegistry([Node(name="local", transport_type="local")])


@pytest.fixture
def mixed_registry():
    """Registry with local + a fake remote node."""
    nodes = [
        Node(name="local", transport_type="local"),
        Node(name="remote1", transport_type="ssh", host="fake-host"),
    ]
    return NodeRegistry(nodes)


class FakeTransport(LocalTransport):
    """A transport that simulates remote behavior for testing."""

    def __init__(self, alive: bool = True, latency: float = 0.01):
        self._alive = alive
        self._latency = latency

    async def is_alive(self) -> bool:
        await asyncio.sleep(self._latency)
        if not self._alive:
            raise ConnectionError("Fake transport unreachable")
        return True

    async def exec(self, command: str, timeout: float = 30.0, **kwargs) -> ExecResult:
        await asyncio.sleep(self._latency)
        if not self._alive:
            return ExecResult(-1, "", "unreachable")
        return ExecResult(0, f"ok: {command}", "")


def make_node(name: str, alive: bool = True, transport_type: str = "ssh") -> Node:
    """Create a node with a FakeTransport for testing."""
    node = Node(name=name, transport_type=transport_type, host="fake")
    node._transport = FakeTransport(alive=alive)
    return node


# ── NodeHealthTracker Tests ─────────────────────────────────────

class TestNodeHealthTracker:
    @pytest.mark.asyncio
    async def test_local_node_always_online(self, local_registry):
        tracker = NodeHealthTracker(local_registry)
        result = await tracker.check_all()
        assert "local" in result
        assert result["local"]["state"] == "online"
        assert result["local"]["latency_ms"] == 0.0

    @pytest.mark.asyncio
    async def test_online_remote_node(self):
        node = make_node("node1", alive=True)
        registry = NodeRegistry([Node(name="local"), node])
        tracker = NodeHealthTracker(registry)

        result = await tracker.check_node(node)
        assert result["state"] == "online"
        assert result["consecutive_failures"] == 0
        assert result["last_seen"] is not None
        assert result["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_offline_after_threshold(self):
        node = make_node("node2", alive=False)
        registry = NodeRegistry([Node(name="local"), node])
        tracker = NodeHealthTracker(registry, offline_threshold=2)

        # First failure → degraded
        result = await tracker.check_node(node)
        assert result["state"] == "degraded"
        assert result["consecutive_failures"] == 1

        # Second failure → offline
        result = await tracker.check_node(node)
        assert result["state"] == "offline"
        assert result["consecutive_failures"] == 2

    @pytest.mark.asyncio
    async def test_recovery_from_offline(self):
        node = make_node("node3", alive=False)
        registry = NodeRegistry([Node(name="local"), node])
        tracker = NodeHealthTracker(registry, offline_threshold=1)

        # Go offline
        await tracker.check_node(node)
        assert tracker.get_health("node3")["state"] == "offline"

        # Come back online
        node._transport = FakeTransport(alive=True)
        result = await tracker.check_node(node)
        assert result["state"] == "online"
        assert result["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_check_all_returns_all_nodes(self):
        nodes = [
            Node(name="local"),
            make_node("a", alive=True),
            make_node("b", alive=False),
        ]
        registry = NodeRegistry(nodes)
        tracker = NodeHealthTracker(registry, offline_threshold=1)

        result = await tracker.check_all()
        assert len(result) == 3
        assert result["local"]["state"] == "online"
        assert result["a"]["state"] == "online"
        assert result["b"]["state"] == "offline"

    @pytest.mark.asyncio
    async def test_all_health_returns_list(self):
        nodes = [Node(name="local"), make_node("x", alive=True)]
        registry = NodeRegistry(nodes)
        tracker = NodeHealthTracker(registry)
        await tracker.check_all()

        health_list = tracker.all_health()
        assert len(health_list) == 2
        names = {h["name"] for h in health_list}
        assert "local" in names
        assert "x" in names

    @pytest.mark.asyncio
    async def test_get_health_single_node(self, local_registry):
        tracker = NodeHealthTracker(local_registry)
        await tracker.check_all()

        h = tracker.get_health("local")
        assert h["state"] == "online"

    @pytest.mark.asyncio
    async def test_get_health_unknown_node(self, local_registry):
        tracker = NodeHealthTracker(local_registry)
        h = tracker.get_health("nonexistent")
        assert h["state"] == "unknown"

    @pytest.mark.asyncio
    async def test_degraded_threshold(self):
        node = make_node("deg", alive=False)
        registry = NodeRegistry([Node(name="local"), node])
        tracker = NodeHealthTracker(
            registry, degraded_threshold=1, offline_threshold=3
        )

        result = await tracker.check_node(node)
        assert result["state"] == "degraded"

        await tracker.check_node(node)
        assert tracker.get_health("deg")["state"] == "degraded"

        await tracker.check_node(node)
        assert tracker.get_health("deg")["state"] == "offline"

    def test_stop(self, local_registry):
        tracker = NodeHealthTracker(local_registry)
        tracker.stop()
        assert not tracker._running


# ── Proxy Endpoint Tests ────────────────────────────────────────

class TestProxyEndpoint:
    @pytest.mark.asyncio
    async def test_proxy_requires_auth(self, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/proxy/local",
                json={"tool": "echo", "arguments": {}},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_proxy_requires_tool_field(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/proxy/local",
                json={"arguments": {}},
                headers=auth_headers,
            )
            assert resp.status_code == 400
            assert "tool" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_proxy_unknown_node_returns_502(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/proxy/nonexistent-node",
                json={"tool": "ping", "arguments": {}},
                headers=auth_headers,
            )
            assert resp.status_code == 502
            assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_proxy_local_node_succeeds(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/proxy/local",
                json={"tool": "echo", "arguments": {"msg": "hello"}},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "result" in data
            assert data["result"]["node"] == "local"

    @pytest.mark.asyncio
    async def test_proxy_invalid_json(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/proxy/local",
                content=b"not-json",
                headers={**auth_headers, "Content-Type": "application/json"},
            )
            assert resp.status_code == 400


# ── Fleet Health Endpoint Tests ─────────────────────────────────

class TestFleetHealthEndpoint:
    @pytest.mark.asyncio
    async def test_fleet_health_requires_auth(self, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/fleet-health")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_fleet_health_returns_nodes(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/fleet-health", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "nodes" in data
            assert "summary" in data
            assert "timestamp" in data
            assert data["summary"]["total"] >= 1
            # Local node should be online
            local_nodes = [n for n in data["nodes"] if n["name"] == "local"]
            assert len(local_nodes) == 1
            assert local_nodes[0]["state"] == "online"

    @pytest.mark.asyncio
    async def test_fleet_health_summary_fields(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/fleet-health", headers=auth_headers)
            summary = resp.json()["summary"]
            assert "total" in summary
            assert "online" in summary
            assert "offline" in summary
            assert "degraded" in summary


# ── Enhanced Siblings Endpoint Tests ────────────────────────────

class TestEnhancedSiblings:
    @pytest.mark.asyncio
    async def test_siblings_include_health_fields(self, app, auth_headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/siblings", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "siblings" in data
            for sibling in data["siblings"]:
                assert "name" in sibling
                assert "transport" in sibling
                assert "state" in sibling
                assert "latency_ms" in sibling
                assert "last_seen" in sibling
                assert "capabilities" in sibling


# ── Fleet Health MCP Tool Tests ─────────────────────────────────

class TestFleetHealthMCPTool:
    @pytest.mark.asyncio
    async def test_fleet_health_tool_dispatch(self):
        from zpilot.mcp_server import _dispatch
        from zpilot.detector import PaneDetector
        from zpilot.events import EventBus

        config = ZpilotConfig()
        detector = PaneDetector(config)
        event_bus = EventBus(config.events_file)
        registry = NodeRegistry([Node(name="local")])
        tracker = NodeHealthTracker(registry)

        result = await _dispatch(
            "fleet_health", {}, config, detector, event_bus,
            registry=registry, health_tracker=tracker,
        )
        assert "Fleet Health" in result
        assert "local" in result
        assert "online" in result

    @pytest.mark.asyncio
    async def test_fleet_health_tool_with_remote_nodes(self):
        from zpilot.mcp_server import _dispatch
        from zpilot.detector import PaneDetector
        from zpilot.events import EventBus

        config = ZpilotConfig()
        detector = PaneDetector(config)
        event_bus = EventBus(config.events_file)
        nodes = [
            Node(name="local"),
            make_node("healthy", alive=True),
            make_node("down", alive=False),
        ]
        registry = NodeRegistry(nodes)
        tracker = NodeHealthTracker(registry, offline_threshold=1)

        result = await _dispatch(
            "fleet_health", {}, config, detector, event_bus,
            registry=registry, health_tracker=tracker,
        )
        assert "healthy" in result
        assert "down" in result
        assert "online" in result


# ── proxy_to_node Function Tests ────────────────────────────────

class TestProxyToNode:
    @pytest.mark.asyncio
    async def test_proxy_to_unknown_node(self):
        from zpilot.mcp_http import proxy_to_node
        result = await proxy_to_node("nonexistent", "ping", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_proxy_to_local_node(self):
        from zpilot.mcp_http import proxy_to_node
        result = await proxy_to_node("local", "echo", {"msg": "hi"})
        assert "result" in result
        assert result["result"]["node"] == "local"


# ── health_check_nodes backward compat ──────────────────────────

class TestHealthCheckNodes:
    @pytest.mark.asyncio
    async def test_health_check_nodes_local(self, local_registry):
        result = await health_check_nodes(local_registry)
        assert "local" in result
        assert result["local"]["alive"] is True
        assert result["local"]["latency_ms"] >= 0
        assert result["local"]["error"] is None
