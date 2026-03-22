"""Tests for zpilot.monitor and multi-node models."""

import pytest
import time

from zpilot.models import (
    FleetStatus, NodeHealth, NodeState, PaneState, SessionHealth, Event,
)


class TestSessionHealth:
    def test_basic(self):
        sh = SessionHealth(node="local", session="dev", state=PaneState.ACTIVE)
        assert sh.node == "local"
        assert sh.state == PaneState.ACTIVE


class TestNodeHealth:
    def test_counts(self):
        nh = NodeHealth(
            name="box1",
            state=NodeState.ONLINE,
            sessions=[
                SessionHealth(node="box1", session="s1", state=PaneState.ACTIVE),
                SessionHealth(node="box1", session="s2", state=PaneState.IDLE),
                SessionHealth(node="box1", session="s3", state=PaneState.ACTIVE),
                SessionHealth(node="box1", session="s4", state=PaneState.WAITING),
            ],
        )
        assert nh.busy_count == 2
        assert nh.idle_count == 2  # IDLE + WAITING
        assert nh.total_sessions == 4

    def test_empty(self):
        nh = NodeHealth(name="empty")
        assert nh.busy_count == 0
        assert nh.total_sessions == 0


class TestFleetStatus:
    def test_summary(self):
        fs = FleetStatus(nodes=[
            NodeHealth(name="a", state=NodeState.ONLINE, sessions=[
                SessionHealth(node="a", session="s1", state=PaneState.ACTIVE),
            ]),
            NodeHealth(name="b", state=NodeState.OFFLINE),
        ])
        assert fs.online_count == 1
        assert fs.total_nodes == 2
        assert fs.total_sessions == 1
        assert fs.total_busy == 1
        s = fs.summary()
        assert "1/2 nodes online" in s
        assert "1 sessions" in s

    def test_empty_fleet(self):
        fs = FleetStatus()
        assert fs.total_nodes == 0
        assert "0/0" in fs.summary()


class TestEventNodeField:
    def test_default_local(self):
        e = Event(event_type="state_change", session="s1", new_state="idle")
        assert e.node == "local"

    def test_round_trip(self):
        e = Event(event_type="state_change", session="s1", new_state="idle", node="box1")
        d = e.to_dict()
        assert d["node"] == "box1"
        e2 = Event.from_dict(d)
        assert e2.node == "box1"

    def test_from_dict_missing_node(self):
        # Old events without node field should default to "local"
        e = Event.from_dict({"type": "state_change", "session": "s", "new_state": "idle"})
        assert e.node == "local"


class TestNodeState:
    def test_values(self):
        assert NodeState.ONLINE.value == "online"
        assert NodeState.OFFLINE.value == "offline"
        assert NodeState.UNREACHABLE.value == "unreachable"
