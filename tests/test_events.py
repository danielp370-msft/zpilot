"""Unit tests for zpilot.events EventBus."""

import json
import tempfile
import pytest
import sys

sys.path.insert(0, "src")

from zpilot.events import EventBus
from zpilot.models import Event


@pytest.fixture
def bus(tmp_path):
    events_file = str(tmp_path / "events.jsonl")
    return EventBus(events_file)


class TestEmit:
    def test_emit_creates_file(self, bus):
        ev = Event(session="s1", new_state="active")
        bus.emit(ev)
        assert bus.path.exists()

    def test_emit_appends(self, bus):
        bus.emit(Event(session="s1", new_state="active"))
        bus.emit(Event(session="s2", new_state="idle"))
        lines = bus.path.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_emit_valid_json(self, bus):
        ev = Event(session="s1", new_state="active", details="test")
        bus.emit(ev)
        data = json.loads(bus.path.read_text().strip())
        assert data["session"] == "s1"
        assert data["new_state"] == "active"


class TestRecent:
    def test_recent_empty(self, bus):
        events = bus.recent(10)
        assert events == []

    def test_recent_returns_events(self, bus):
        bus.emit(Event(session="s1", new_state="active"))
        bus.emit(Event(session="s2", new_state="idle"))
        bus.emit(Event(session="s3", new_state="error"))
        events = bus.recent(2)
        assert len(events) == 2
        assert events[0].session == "s2"
        assert events[1].session == "s3"

    def test_recent_all(self, bus):
        for i in range(5):
            bus.emit(Event(session=f"s{i}", new_state="active"))
        events = bus.recent(100)
        assert len(events) == 5


class TestClear:
    def test_clear(self, bus):
        bus.emit(Event(session="s1", new_state="active"))
        bus.clear()
        assert bus.recent(10) == []
        assert bus.path.exists()
        assert bus.path.read_text() == ""


class TestCallbacks:
    def test_callback_fired(self, bus):
        received = []
        bus.on_event(lambda ev: received.append(ev))
        bus.emit(Event(session="s1", new_state="active"))
        assert len(received) == 1
        assert received[0].session == "s1"

    def test_callback_exception_ignored(self, bus):
        def bad_callback(ev):
            raise RuntimeError("oops")

        bus.on_event(bad_callback)
        bus.emit(Event(session="s1", new_state="active"))  # should not raise


class TestAllEvents:
    def test_all_events(self, bus):
        for i in range(10):
            bus.emit(Event(session=f"s{i}", new_state="active"))
        events = bus.all_events()
        assert len(events) == 10
