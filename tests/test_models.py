"""Unit tests for zpilot.models."""

import time
import pytest
import sys

sys.path.insert(0, "src")

from zpilot.models import PaneState, Pane, Session, Event, ZpilotConfig


class TestPaneState:
    def test_values(self):
        assert PaneState.ACTIVE == "active"
        assert PaneState.IDLE == "idle"
        assert PaneState.WAITING == "waiting"
        assert PaneState.ERROR == "error"
        assert PaneState.EXITED == "exited"
        assert PaneState.UNKNOWN == "unknown"

    def test_str_enum(self):
        assert str(PaneState.ACTIVE) == "PaneState.ACTIVE"
        assert PaneState.ACTIVE.value == "active"


class TestEvent:
    def test_default_timestamp(self):
        ev = Event(session="s1", new_state="active")
        assert ev.timestamp > 0
        assert ev.event_type == "state_change"

    def test_to_dict(self):
        ev = Event(
            timestamp=1000.0,
            event_type="state_change",
            session="test",
            pane="main",
            old_state="idle",
            new_state="active",
            details="woke up",
        )
        d = ev.to_dict()
        assert d["ts"] == 1000.0
        assert d["type"] == "state_change"
        assert d["session"] == "test"
        assert d["pane"] == "main"
        assert d["old_state"] == "idle"
        assert d["new_state"] == "active"
        assert d["details"] == "woke up"

    def test_from_dict(self):
        d = {
            "ts": 2000.0,
            "type": "info",
            "session": "s2",
            "pane": "worker",
            "old_state": None,
            "new_state": "idle",
            "details": None,
        }
        ev = Event.from_dict(d)
        assert ev.timestamp == 2000.0
        assert ev.event_type == "info"
        assert ev.session == "s2"

    def test_roundtrip(self):
        ev = Event(session="test", new_state="waiting", details="bell rang")
        d = ev.to_dict()
        ev2 = Event.from_dict(d)
        assert ev2.session == ev.session
        assert ev2.new_state == ev.new_state
        assert ev2.details == ev.details


class TestZpilotConfig:
    def test_defaults(self):
        cfg = ZpilotConfig()
        assert cfg.poll_interval == 5.0
        assert cfg.idle_threshold == 30.0
        assert cfg.bel_detection is True
        assert cfg.notify_adapter == "log"
        assert len(cfg.prompt_patterns) >= 2
        assert len(cfg.error_patterns) >= 2

    def test_custom(self):
        cfg = ZpilotConfig(poll_interval=2.0, idle_threshold=10.0, bel_detection=False)
        assert cfg.poll_interval == 2.0
        assert cfg.idle_threshold == 10.0
        assert cfg.bel_detection is False


class TestPane:
    def test_defaults(self):
        p = Pane(pane_id=1)
        assert p.pane_id == 1
        assert p.name is None
        assert p.state == PaneState.UNKNOWN
        assert p.last_lines == []


class TestSession:
    def test_defaults(self):
        s = Session(name="test")
        assert s.name == "test"
        assert s.is_current is False
        assert s.panes == []
        assert s.tabs == []
