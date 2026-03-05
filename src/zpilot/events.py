"""File-based event bus for zpilot."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .models import Event


class EventBus:
    """Simple file-based event bus using JSONL."""

    def __init__(self, events_file: str = "/tmp/zpilot/events.jsonl"):
        self.path = Path(events_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._callbacks: list = []

    def emit(self, event: Event) -> None:
        """Append an event to the events file."""
        with open(self.path, "a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")
        # Fire sync callbacks
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def on_event(self, callback) -> None:
        """Register a callback for new events."""
        self._callbacks.append(callback)

    def recent(self, n: int = 50) -> list[Event]:
        """Read the last N events."""
        if not self.path.exists():
            return []
        lines = self.path.read_text().strip().splitlines()
        events = []
        for line in lines[-n:]:
            try:
                events.append(Event.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
        return events

    def all_events(self) -> list[Event]:
        """Read all events."""
        return self.recent(n=999999)

    def clear(self) -> None:
        """Clear the events file."""
        if self.path.exists():
            self.path.write_text("")

    async def tail(self, callback) -> None:
        """Async tail -f style event reader. Runs forever."""
        if not self.path.exists():
            self.path.touch()

        # Start reading from end of file
        with open(self.path) as f:
            f.seek(0, 2)  # seek to end
            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            event = Event.from_dict(json.loads(line))
                            await callback(event)
                        except (json.JSONDecodeError, KeyError):
                            pass
                else:
                    await asyncio.sleep(0.5)
