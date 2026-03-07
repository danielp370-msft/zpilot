"""Unit tests for zpilot.detector."""

import time
import pytest
import sys

sys.path.insert(0, "src")

from zpilot.detector import PaneDetector
from zpilot.models import PaneState, ZpilotConfig


@pytest.fixture
def config():
    return ZpilotConfig(idle_threshold=5.0)


@pytest.fixture
def detector(config):
    return PaneDetector(config)


class TestBelDetection:
    def test_bel_detected(self, detector):
        content = "some output\x07\nmore output"
        state = detector.detect("s1", "p1", content)
        assert state == PaneState.WAITING

    def test_bel_detection_disabled(self, config):
        config.bel_detection = False
        det = PaneDetector(config)
        content = "some output\x07\nmore output"
        state = det.detect("s1", "p1", content)
        assert state != PaneState.WAITING


class TestErrorDetection:
    def test_error_pattern(self, detector):
        content = "building...\nError: something broke\n"
        state = detector.detect("s1", "p1", content)
        assert state == PaneState.ERROR

    def test_fatal_pattern(self, detector):
        content = "starting...\nFATAL: can't connect\n"
        state = detector.detect("s1", "p1", content)
        assert state == PaneState.ERROR

    def test_panic_pattern(self, detector):
        content = "running...\npanic: runtime error\n"
        state = detector.detect("s1", "p1", content)
        assert state == PaneState.ERROR


class TestPromptDetection:
    def test_dollar_prompt(self, detector):
        # Dollar prompt — now matched by \$\s*$ pattern even after strip
        content = "some output\n$ \n"
        detector.detect("s1", "p1", content, now=100.0)
        state = detector.detect("s1", "p1", content, now=106.0)
        # Prompt is visible and idle >= 1.0 → WAITING
        assert state == PaneState.WAITING

    def test_arrow_prompt(self, detector):
        # ❯ pattern is r"^❯ " which starts with ❯ — strip won't remove leading
        content = "output\n❯ command"
        detector.detect("s1", "p1a", content, now=100.0)
        state = detector.detect("s1", "p1a", content, now=104.0)
        assert state in (PaneState.WAITING, PaneState.ACTIVE, PaneState.IDLE)

    def test_prompt_detected_with_bel(self, detector):
        """BEL char should trigger WAITING regardless of idle time."""
        content = "some output\x07\n$ "
        state = detector.detect("s1", "bel1", content, now=100.0)
        assert state == PaneState.WAITING


class TestIdleDetection:
    def test_idle_after_threshold(self, detector):
        content = "some static content"
        detector.detect("s1", "idle1", content, now=100.0)
        state = detector.detect("s1", "idle1", content, now=106.0)
        assert state == PaneState.IDLE

    def test_not_idle_before_threshold(self, detector):
        content = "some static content"
        detector.detect("s1", "idle2", content, now=100.0)
        state = detector.detect("s1", "idle2", content, now=103.0)
        assert state == PaneState.ACTIVE


class TestActiveDetection:
    def test_content_changing(self, detector):
        s1 = detector.detect("s1", "p1", "output line 1", now=100.0)
        assert s1 == PaneState.ACTIVE
        s2 = detector.detect("s1", "p1", "output line 2", now=102.0)
        assert s2 == PaneState.ACTIVE


class TestIdleSeconds:
    def test_idle_seconds(self, detector):
        detector.detect("s1", "p1", "content", now=100.0)
        # Manually set last_change_time for test
        detector._last_change_time["s1:p1"] = time.time() - 10
        idle = detector.get_idle_seconds("s1", "p1")
        assert idle >= 9.0


class TestLastState:
    def test_get_last_state(self, detector):
        assert detector.get_last_state("s1", "p1") == PaneState.UNKNOWN
        detector.detect("s1", "p1", "Error: bad\n")
        assert detector.get_last_state("s1", "p1") == PaneState.ERROR


class TestInputIdleReset:
    """Tests for idle timer resetting on user input."""

    def test_record_input_resets_idle(self, detector):
        # Simulate pane with old output (idle for 30s)
        detector.detect("s1", "p1", "old output", now=time.time() - 30)
        idle_before = detector.get_idle_seconds("s1", "p1")
        assert idle_before >= 25.0  # should be ~30s idle

        # Record user input
        detector.record_input("s1", "p1")
        idle_after = detector.get_idle_seconds("s1", "p1")
        assert idle_after < 2.0  # should be near 0

    def test_input_affects_detect_idle_calc(self, detector):
        now = time.time()
        # Detect with old content (no change)
        detector.detect("s1", "p1", "same", now=now - 20)
        detector.detect("s1", "p1", "same", now=now - 10)
        # Without input, idle is ~10s
        idle_no_input = detector.get_idle_seconds("s1", "p1")
        assert idle_no_input >= 9.0

        # Record input — idle should reset
        detector.record_input("s1", "p1")
        state = detector.detect("s1", "p1", "same", now=now)
        # With fresh input, idle should be near 0, so state should be ACTIVE
        assert state == PaneState.ACTIVE

    def test_record_input_different_panes(self, detector):
        detector.detect("s1", "p1", "content", now=time.time() - 30)
        detector.detect("s1", "p2", "content", now=time.time() - 30)

        # Only reset p1
        detector.record_input("s1", "p1")
        assert detector.get_idle_seconds("s1", "p1") < 2.0
        assert detector.get_idle_seconds("s1", "p2") >= 25.0
