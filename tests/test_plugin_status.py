"""Tests for /api/plugin-status endpoints."""

import sys
import time

sys.path.insert(0, "src")

import pytest
from httpx import AsyncClient, ASGITransport
from zpilot.web.app import app, _plugin_status


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture(autouse=True)
def reset_plugin_status():
    """Reset the in-memory plugin status before each test."""
    import zpilot.web.app as web_app
    web_app._plugin_status = {}
    yield
    web_app._plugin_status = {}


@pytest.mark.asyncio
class TestPluginStatusPost:
    async def test_post_stores_data(self, client):
        """POST /api/plugin-status stores the JSON body."""
        payload = {
            "panes": [{"id": 1, "title": "main"}],
            "tabs": [{"name": "Tab #1"}],
            "session": "my-session",
        }
        async with client:
            resp = await client.post("/api/plugin-status", json=payload)
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    async def test_post_overwrites_previous(self, client):
        """Second POST replaces the first."""
        async with client:
            await client.post("/api/plugin-status", json={"v": 1})
            await client.post("/api/plugin-status", json={"v": 2})
            resp = await client.get("/api/plugin-status")
            data = resp.json()
            assert data["data"]["v"] == 2

    async def test_post_sets_timestamp(self, client):
        """POST records an updated_at timestamp."""
        before = time.time()
        async with client:
            await client.post("/api/plugin-status", json={"hello": "world"})
            resp = await client.get("/api/plugin-status")
            data = resp.json()
            assert "updated_at" in data
            assert data["updated_at"] >= before


@pytest.mark.asyncio
class TestPluginStatusGet:
    async def test_get_empty_initially(self, client):
        """GET /api/plugin-status returns empty dict when no data posted."""
        async with client:
            resp = await client.get("/api/plugin-status")
            assert resp.status_code == 200
            assert resp.json() == {}

    async def test_get_returns_posted_data(self, client):
        """GET returns exactly what was POSTed."""
        payload = {
            "panes": [{"id": 1, "title": "editor"}, {"id": 2, "title": "terminal"}],
            "session_name": "dev",
        }
        async with client:
            await client.post("/api/plugin-status", json=payload)
            resp = await client.get("/api/plugin-status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["data"] == payload

    async def test_roundtrip_complex_payload(self, client):
        """Complex nested payload survives roundtrip."""
        payload = {
            "panes": [
                {"id": 1, "title": "main", "is_focused": True, "rows": 40, "cols": 120},
                {"id": 2, "title": "sidebar", "is_focused": False, "rows": 40, "cols": 30},
            ],
            "tabs": [
                {"name": "Tab #1", "active": True, "pane_ids": [1, 2]},
                {"name": "Tab #2", "active": False, "pane_ids": [3]},
            ],
            "session": {"name": "zpilot-dev", "connected_clients": 1},
        }
        async with client:
            await client.post("/api/plugin-status", json=payload)
            resp = await client.get("/api/plugin-status")
            data = resp.json()
            assert data["data"]["panes"][0]["title"] == "main"
            assert data["data"]["tabs"][1]["name"] == "Tab #2"
            assert data["data"]["session"]["connected_clients"] == 1
