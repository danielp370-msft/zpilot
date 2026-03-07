"""Idle and completion detection for terminal panes."""

from __future__ import annotations

import hashlib
import re
import time

from .models import PaneState, ZpilotConfig


class PaneDetector:
    """Detects the state of a terminal pane from its screen content."""

    def __init__(self, config: ZpilotConfig):
        self.config = config
        self._prompt_res = [re.compile(p) for p in config.prompt_patterns]
        self._error_res = [re.compile(p) for p in config.error_patterns]

        # Per-pane tracking: key = (session, pane_name)
        self._last_hash: dict[str, str] = {}
        self._last_change_time: dict[str, float] = {}
        self._last_state: dict[str, PaneState] = {}
        self._last_input_time: dict[str, float] = {}  # track user input activity

    def _key(self, session: str, pane: str) -> str:
        return f"{session}:{pane}"

    def _content_hash(self, content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()

    def detect(
        self,
        session: str,
        pane: str,
        content: str,
        now: float | None = None,
    ) -> PaneState:
        """Analyze pane content and return its current state.

        Call this periodically with the latest screen dump.
        """
        now = now or time.time()
        key = self._key(session, pane)
        content_hash = self._content_hash(content)

        # Check if content changed since last check
        prev_hash = self._last_hash.get(key)
        changed = content_hash != prev_hash
        if changed:
            self._last_hash[key] = content_hash
            self._last_change_time[key] = now

        last_change = self._last_change_time.get(key, now)
        last_input = self._last_input_time.get(key, 0)
        last_activity = max(last_change, last_input)
        idle_seconds = now - last_activity

        # Get the last few non-empty lines for pattern matching
        lines = [l for l in content.splitlines() if l.strip()]
        last_lines = lines[-5:] if lines else []

        # 1. Check for error patterns
        for line in last_lines:
            for pattern in self._error_res:
                if pattern.search(line):
                    self._last_state[key] = PaneState.ERROR
                    return PaneState.ERROR

        # 2. Check for prompt patterns (= waiting for input)
        has_prompt = False
        check_lines = last_lines[-2:] if last_lines else []
        for line in check_lines:
            for pattern in self._prompt_res:
                if pattern.search(line.strip()):
                    has_prompt = True
                    break
            if has_prompt:
                break

        if has_prompt and idle_seconds >= 1.0:
            # Prompt is visible and content is stable
            self._last_state[key] = PaneState.WAITING
            return PaneState.WAITING

        # 3. Check for BEL character (terminal bell = needs attention)
        if self.config.bel_detection and "\x07" in content:
            self._last_state[key] = PaneState.WAITING
            return PaneState.WAITING

        # 4. Content is changing = active
        if changed or idle_seconds < 3.0:
            self._last_state[key] = PaneState.ACTIVE
            return PaneState.ACTIVE

        # 5. Check for idle (no output change for threshold)
        if idle_seconds >= self.config.idle_threshold:
            self._last_state[key] = PaneState.IDLE
            return PaneState.IDLE

        # 6. Between active and idle — still active but slowing down
        self._last_state[key] = PaneState.ACTIVE
        return PaneState.ACTIVE

    def get_idle_seconds(self, session: str, pane: str) -> float:
        """Get how long a pane has been idle (since last output OR input)."""
        key = self._key(session, pane)
        now = time.time()
        last_change = self._last_change_time.get(key, now)
        last_input = self._last_input_time.get(key, 0)
        return now - max(last_change, last_input)

    def record_input(self, session: str, pane: str = "focused") -> None:
        """Record that user/AI sent input to this pane (resets idle timer)."""
        key = self._key(session, pane)
        self._last_input_time[key] = time.time()

    def get_last_state(self, session: str, pane: str) -> PaneState:
        """Get the last detected state for a pane."""
        key = self._key(session, pane)
        return self._last_state.get(key, PaneState.UNKNOWN)
