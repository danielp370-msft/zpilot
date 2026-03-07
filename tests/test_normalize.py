"""Tests for _normalize_for_xterm() and _strip_ansi() edge cases."""

import sys
import pytest

sys.path.insert(0, "src")

from zpilot.web.app import _normalize_for_xterm, _strip_ansi


class TestNormalizeForXterm:
    """Test ANSI normalization for xterm.js rendering."""

    def test_preserves_sgr_colors(self):
        """SGR color codes (ending with 'm') should be kept."""
        text = "\x1b[31mred\x1b[0m normal \x1b[1;32mbold green\x1b[0m"
        result = _normalize_for_xterm(text)
        assert "\x1b[31m" in result
        assert "\x1b[0m" in result
        assert "\x1b[1;32m" in result
        assert "red" in result
        assert "bold green" in result

    def test_strips_cursor_positioning(self):
        """CSI cursor movement (A, B, C, D, K) should be stripped."""
        text = "\x1b[5Aup\x1b[3Bdown\x1b[2Cright\x1b[1Dleft\x1b[Kcleared"
        result = _normalize_for_xterm(text)
        assert "\x1b[5A" not in result
        assert "\x1b[3B" not in result
        assert "up" in result
        assert "down" in result
        assert "cleared" in result

    def test_strips_cursor_home(self):
        """Cursor home (H) should be stripped."""
        text = "\x1b[5;10Hplaced here"
        result = _normalize_for_xterm(text)
        assert "\x1b[5;10H" not in result
        assert "placed here" in result

    def test_converts_lf_to_crlf(self):
        """Bare \\n must become \\r\\n for xterm.js."""
        text = "line1\nline2\nline3"
        result = _normalize_for_xterm(text)
        assert result == "line1\r\nline2\r\nline3"

    def test_no_double_crlf(self):
        """Existing \\r\\n should not become \\r\\r\\n."""
        text = "line1\r\nline2\r\nline3"
        result = _normalize_for_xterm(text)
        assert "\r\r\n" not in result
        assert result == "line1\r\nline2\r\nline3"

    def test_strips_osc_sequences(self):
        """OSC title sequences should be removed."""
        text = "\x1b]0;user@host: ~/dir\x07prompt$ "
        result = _normalize_for_xterm(text)
        assert "\x1b]" not in result
        assert "prompt$ " in result

    def test_strips_charset_and_keypad(self):
        text = "\x1b(B\x1b)0\x1b=\x1b>visible"
        result = _normalize_for_xterm(text)
        assert "visible" in result
        assert "\x1b(" not in result
        assert "\x1b=" not in result

    def test_collapses_blank_lines(self):
        """More than 2 consecutive newlines collapsed to 2."""
        text = "top\n\n\n\n\nbottom"
        result = _normalize_for_xterm(text)
        # After normalization: \n\n then CRLF conversion
        assert "\r\n\r\n" in result
        # Should not have more than 2 consecutive CRLFs
        assert "\r\n\r\n\r\n" not in result

    def test_full_screen_app_last_frame(self):
        """Full-screen apps (clear screen) should show last frame only."""
        text = "frame1 old\x1b[2Jframe2 current"
        result = _normalize_for_xterm(text)
        assert "frame1 old" not in result
        assert "frame2 current" in result

    def test_alt_buffer_detection(self):
        """Alt screen buffer markers should trigger frame detection."""
        text = "normal shell\x1b[?1049hvim content\x1b[?1049lback to shell"
        result = _normalize_for_xterm(text)
        assert "back to shell" in result

    def test_empty_input(self):
        assert _normalize_for_xterm("") == ""

    def test_plain_text_passthrough(self):
        text = "hello world"
        result = _normalize_for_xterm(text)
        assert result == "hello world"

    def test_mixed_sgr_and_cursor(self):
        """SGR kept, cursor stripped, in interleaved sequence."""
        text = "\x1b[32m\x1b[2;1Hgreen text\x1b[0m\x1b[10A"
        result = _normalize_for_xterm(text)
        assert "\x1b[32m" in result  # SGR kept
        assert "\x1b[0m" in result   # SGR kept
        assert "\x1b[2;1H" not in result  # cursor stripped
        assert "\x1b[10A" not in result   # cursor stripped
        assert "green text" in result

    def test_control_chars_stripped(self):
        """Non-printable control chars removed (except \\n, \\t, \\r, ESC)."""
        text = "hello\x01\x02\x03world"
        result = _normalize_for_xterm(text)
        assert "\x01" not in result
        assert "\x02" not in result
        assert "helloworld" in result

    def test_preserves_tab(self):
        text = "col1\tcol2\tcol3"
        result = _normalize_for_xterm(text)
        assert "\t" in result

    def test_realistic_bash_prompt(self):
        """Realistic bash prompt with colors, OSC title, bracket paste."""
        raw = (
            "\x1b]0;user@host: ~/project\x07"  # OSC title
            "\x1b[?2004h"                        # bracket paste on
            "\x1b[01;32muser@host\x1b[00m:"     # green user
            "\x1b[01;34m~/project\x1b[00m$ "    # blue path
            "ls -la\r\n"
            "\x1b[?2004l"                        # bracket paste off
            "total 42\r\n"
            "drwxr-xr-x 5 user user 4096 Mar  7 file1\r\n"
        )
        result = _normalize_for_xterm(raw)
        assert "\x1b[01;32m" in result  # colors preserved
        assert "user@host" in result
        assert "ls -la" in result
        assert "total 42" in result
        assert "\x1b]0;" not in result  # OSC stripped

    def test_multiple_clear_screens_keeps_last(self):
        """Multiple clear screens — only last frame matters."""
        text = "old1\x1b[2Jold2\x1b[2Jold3\x1b[2Jfinal content here"
        result = _normalize_for_xterm(text)
        assert "old1" not in result
        assert "old2" not in result
        assert "old3" not in result
        assert "final content here" in result


class TestStripAnsiFullScreen:
    """Test _strip_ansi with full-screen app patterns."""

    def test_clear_screen_keeps_last_frame(self):
        text = "frame1\x1b[2Jframe2"
        result = _strip_ansi(text)
        assert "frame1" not in result
        assert "frame2" in result

    def test_alt_buffer_enter_exit(self):
        text = "shell\x1b[?1049hvim\x1b[?1049lshell again"
        result = _strip_ansi(text)
        assert "shell again" in result

    def test_empty_frames_skipped(self):
        """Empty frames between clears should be skipped."""
        text = "content\x1b[2J\x1b[2J\x1b[2Jactual frame"
        result = _strip_ansi(text)
        assert "actual frame" in result

    def test_blank_line_collapse(self):
        text = "a\n\n\n\n\nb"
        result = _strip_ansi(text)
        assert result == "a\n\nb"

    def test_preserves_double_newline(self):
        text = "a\n\nb"
        result = _strip_ansi(text)
        assert result == "a\n\nb"
