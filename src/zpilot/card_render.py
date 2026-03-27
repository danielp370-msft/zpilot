"""Context-adaptive session card rendering.

Detects session type and renders an appropriate mini-preview:
  Mode 1 (visual):  Mini block-char thumbnail (cmatrix, htop, vim, or high-velocity output)
  Mode 2 (copilot): Last action + outcome summary
  Mode 3 (build):   Progress indicator + last meaningful line
  Mode 4 (shell):   Last command + idle status

Output velocity tracking: sessions pushing lots of data get the visual
mini-render by default — if bytes are flowing fast, the user cares about
what it looks like, not parsed text.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionMode(Enum):
    VISUAL = "visual"      # TUI app, full-screen program, or high-velocity output
    COPILOT = "copilot"    # AI agent (copilot-cli, aider, etc.)
    BUILD = "build"        # Build/CI/test process
    SHELL = "shell"        # Interactive shell


@dataclass
class CardContent:
    """Adaptive card content for a session."""
    mode: SessionMode
    icon: str              # emoji icon
    status_line: str       # one-line summary of what's happening
    preview: str           # multi-line preview (mini-render or text)
    heat: float = 0.0
    idle_secs: float = 0.0


# ── Detection ────────────────────────────────────────────────

# Programs that are full-screen TUI/visual apps
_VISUAL_PROGRAMS = {
    "cmatrix", "htop", "btop", "top", "vim", "nvim", "vi", "nano",
    "less", "more", "man", "tmux", "zellij", "mc", "ranger", "nnn",
    "lazygit", "lazydocker", "k9s", "tig",
}

# Programs that are AI agents
_COPILOT_PROGRAMS = {
    "copilot-cli", "copilot", "aider", "cursor", "claude",
}

# Build/CI command prefixes
_BUILD_PATTERNS = [
    (re.compile(r"(?:npm|yarn|pnpm)\s+(?:run\s+)?(?:build|test|install|start|dev)\b"), "📦"),
    (re.compile(r"(?:make|cmake|cargo|go)\s+(?:build|test|install|run)\b"), "🔨"),
    (re.compile(r"(?:pytest|python.*-m\s+pytest|jest|mocha|rspec)\b"), "🧪"),
    (re.compile(r"(?:pip|pip3)\s+install\b"), "📦"),
    (re.compile(r"\b(?:gcc|g\+\+|clang|rustc|javac)\s+-"), "🔨"),
    (re.compile(r"(?:docker|podman)\s+(?:build|run|compose)\b"), "🐳"),
    (re.compile(r"(?:terraform|pulumi|cdk)\s+(?:apply|plan|deploy)\b"), "☁️"),
    (re.compile(r"\bgit\s+(?:push|pull|fetch|clone|rebase|merge)\b"), "📤"),
]

# Screen content patterns that indicate full-screen TUI
_VISUAL_CONTENT_PATTERNS = [
    re.compile(r"[\u2500-\u257f]{5,}"),   # box drawing chars (lots of them)
    re.compile(r"[\u2588\u2591-\u2593]{10,}"),  # block chars
    re.compile(r"\x1b\[\d+;\d+H.{0,3}\x1b\[\d+;\d+H"),  # cursor positioning
]

# Copilot-specific output patterns
_COPILOT_PATTERNS = {
    "thinking": [
        re.compile(r"Thinking|thinking\.\.\.", re.I),
        re.compile(r"⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏"),  # spinner chars
    ],
    "tool_calling": [
        re.compile(r"(?:Running|Calling|Using)\s+(?:tool|function)", re.I),
        re.compile(r"(?:bash|grep|view|edit|create)\s*\(", re.I),
        re.compile(r"Tool call:", re.I),
    ],
    "editing": [
        re.compile(r"(?:Edited|Created|Modified|Updated)\s+\S+", re.I),
        re.compile(r"✓.*(?:edit|create|wrote)", re.I),
    ],
    "waiting": [
        re.compile(r"⏎\s*to send|Press Enter|waiting for input", re.I),
        re.compile(r"Copilot is ready", re.I),
        re.compile(r"^>\s*$"),
        re.compile(r"Type @.*mention|Describe a task", re.I),
        re.compile(r"GitHub Copilot v\d", re.I),
    ],
    "error": [
        re.compile(r"Error:.*(?:tool|agent|copilot)", re.I),
        re.compile(r"Traceback \(most recent"),
    ],
}


# ── Output velocity tracker ──────────────────────────────────

# Bytes/sec threshold: above this, treat as high-velocity (visual render)
VELOCITY_THRESHOLD = 500  # 500 bytes/sec = fast output


@dataclass
class _VelocitySample:
    content_len: int = 0
    timestamp: float = 0.0


class VelocityTracker:
    """Track output velocity (bytes/sec) per session.

    Call `update(session, content_len)` each poll cycle.
    Call `get_velocity(session)` to get smoothed bytes/sec.
    """

    def __init__(self, smoothing: float = 0.3):
        self._samples: dict[str, _VelocitySample] = {}
        self._velocities: dict[str, float] = {}
        self._smoothing = smoothing  # EMA smoothing factor (0-1, higher = more responsive)

    def update(self, session: str, content_len: int) -> float:
        """Record new content length, return current velocity (bytes/sec)."""
        now = time.monotonic()
        prev = self._samples.get(session)

        if prev is None or prev.timestamp == 0.0:
            self._samples[session] = _VelocitySample(content_len, now)
            self._velocities[session] = 0.0
            return 0.0

        dt = now - prev.timestamp
        if dt < 0.1:  # too fast, skip
            return self._velocities.get(session, 0.0)

        delta_bytes = max(0, content_len - prev.content_len)
        instant_vel = delta_bytes / dt

        # Exponential moving average
        old_vel = self._velocities.get(session, 0.0)
        new_vel = self._smoothing * instant_vel + (1 - self._smoothing) * old_vel

        self._samples[session] = _VelocitySample(content_len, now)
        self._velocities[session] = new_vel
        return new_vel

    def get_velocity(self, session: str) -> float:
        """Get current smoothed velocity for a session."""
        return self._velocities.get(session, 0.0)

    def is_high_velocity(self, session: str) -> bool:
        """Check if session output is flowing fast enough for visual render."""
        return self.get_velocity(session) > VELOCITY_THRESHOLD


# Global tracker instance (shared across polls)
velocity_tracker = VelocityTracker()


def detect_mode(name: str, content: str, raw_content: str = "",
                session_velocity: float = 0.0) -> SessionMode:
    """Detect what mode a session is operating in.

    Args:
        session_velocity: output velocity in bytes/sec. If high, defaults to visual mode.
    """
    name_lower = name.lower()

    # Check name-based hints first (strongest signal)
    for prog in _COPILOT_PROGRAMS:
        if prog in name_lower:
            return SessionMode.COPILOT

    for prog in _VISUAL_PROGRAMS:
        if prog in name_lower:
            return SessionMode.VISUAL

    lines = content.strip().splitlines()

    # High velocity output → visual mode (the screen IS the content)
    # But only if it doesn't match a known build/copilot pattern
    if session_velocity > VELOCITY_THRESHOLD:
        # Quick check: if it looks like a build, keep it as build
        for line in reversed(lines[-10:]):
            for pat, _ in _BUILD_PATTERNS:
                if pat.search(line):
                    return SessionMode.BUILD
        return SessionMode.VISUAL

    # Check for build/CI processes BEFORE copilot content (avoid false positives)
    for line in reversed(lines[-20:]):
        for pat, _ in _BUILD_PATTERNS:
            if pat.search(line):
                return SessionMode.BUILD

    # Check if content suggests copilot (agent-specific patterns)
    last_chunk = content[-500:] if len(content) > 500 else content
    for patterns in _COPILOT_PATTERNS.values():
        for pat in patterns:
            if pat.search(last_chunk):
                return SessionMode.COPILOT

    # Check for visual/TUI apps (by content)
    if lines:
        last_line = lines[-1].strip()
        for prog in _VISUAL_PROGRAMS:
            if prog in last_line:
                return SessionMode.VISUAL

    # Check screen content patterns for TUI detection
    for pat in _VISUAL_CONTENT_PATTERNS:
        if pat.search(content[-2000:] if len(content) > 2000 else content):
            return SessionMode.VISUAL

    return SessionMode.SHELL


# ── Mode-specific renderers ──────────────────────────────────

def render_card(
    name: str,
    content: str,
    state: str = "unknown",
    idle_secs: float = 0.0,
    heat: float = 0.0,
    copilot: bool = False,
    pyte_screen: Any = None,
    card_rows: int = 6,
    card_cols: int = 30,
) -> CardContent:
    """Render adaptive card content for a session.

    Automatically tracks output velocity and uses it for mode detection.
    High-velocity sessions get the visual mini-render by default.
    """
    # Update velocity tracker
    vel = velocity_tracker.update(name, len(content))

    mode = (SessionMode.COPILOT if copilot
            else detect_mode(name, content, session_velocity=vel))

    if mode == SessionMode.VISUAL:
        return _render_visual(name, content, pyte_screen, card_rows, card_cols, heat, idle_secs, vel)
    elif mode == SessionMode.COPILOT:
        return _render_copilot(name, content, heat, idle_secs)
    elif mode == SessionMode.BUILD:
        return _render_build(name, content, heat, idle_secs)
    else:
        return _render_shell(name, content, state, heat, idle_secs)


def _render_visual(
    name: str, content: str,
    pyte_screen: Any, rows: int, cols: int,
    heat: float, idle_secs: float,
    velocity: float = 0.0,
) -> CardContent:
    """Mode 1: Mini block-char thumbnail for visual/TUI apps or high-velocity output."""
    if pyte_screen:
        preview = _mini_render_pyte(pyte_screen, rows, cols)
    else:
        preview = _mini_render_text(content, rows, cols)

    if velocity > VELOCITY_THRESHOLD:
        vel_str = f"{velocity:.0f} B/s"
        status = f"🖥️  Streaming ({vel_str})"
    else:
        status = "🖥️  Visual app running"

    return CardContent(
        mode=SessionMode.VISUAL,
        icon="🖥️",
        status_line=status,
        preview=preview,
        heat=heat,
        idle_secs=idle_secs,
    )


def _render_copilot(
    name: str, content: str,
    heat: float, idle_secs: float,
) -> CardContent:
    """Mode 2: Copilot/AI agent activity summary."""
    last_chunk = content[-2000:] if len(content) > 2000 else content
    lines = last_chunk.strip().splitlines()

    # Determine current copilot state
    copilot_state = "idle"
    state_detail = ""

    # Check in priority order (most specific first)
    for state_name, patterns in [
        ("error", _COPILOT_PATTERNS["error"]),
        ("editing", _COPILOT_PATTERNS["editing"]),
        ("tool_calling", _COPILOT_PATTERNS["tool_calling"]),
        ("thinking", _COPILOT_PATTERNS["thinking"]),
        ("waiting", _COPILOT_PATTERNS["waiting"]),
    ]:
        for pat in patterns:
            for line in reversed(lines[-30:]):
                m = pat.search(line)
                if m:
                    copilot_state = state_name
                    state_detail = _clean_for_display(line.strip())[:60]
                    break
            if copilot_state != "idle":
                break
        if copilot_state != "idle":
            break

    state_icons = {
        "thinking": "🧠",
        "tool_calling": "🔧",
        "editing": "✏️",
        "waiting": "💬",
        "error": "❌",
        "idle": "🤖",
    }
    state_labels = {
        "thinking": "Thinking...",
        "tool_calling": "Calling tools",
        "editing": "Editing files",
        "waiting": "Waiting for input",
        "error": "Error detected",
        "idle": "Active",
    }

    icon = state_icons.get(copilot_state, "🤖")
    label = state_labels.get(copilot_state, "Active")
    status = f"{icon} {label}"

    # Build preview: last few meaningful lines
    preview_lines = []
    for line in reversed(lines[-15:]):
        clean = _clean_for_display(line)
        if clean and len(clean) > 2:
            preview_lines.insert(0, clean)
            if len(preview_lines) >= 4:
                break

    if state_detail and state_detail not in "\n".join(preview_lines):
        preview_lines.insert(0, f"► {state_detail}")

    preview = "\n".join(preview_lines[-4:]) if preview_lines else "(no output)"

    return CardContent(
        mode=SessionMode.COPILOT,
        icon=icon,
        status_line=status,
        preview=preview,
        heat=heat,
        idle_secs=idle_secs,
    )


def _render_build(
    name: str, content: str,
    heat: float, idle_secs: float,
) -> CardContent:
    """Mode 3: Build/CI progress display."""
    lines = content.strip().splitlines()

    # Find the build command and its progress
    build_cmd = ""
    progress_line = ""
    last_meaningful = ""

    for line in reversed(lines[-30:]):
        clean = _clean_for_display(line)
        if not clean or len(clean) < 3:
            continue
        if not last_meaningful:
            last_meaningful = clean

        # Look for build commands
        for pat, icon in _BUILD_PATTERNS:
            if pat.search(clean):
                build_cmd = f"{icon} {clean[:50]}"
                break
        if build_cmd:
            break

        # Look for progress indicators
        if not progress_line:
            if re.search(r"\d+[/%]\s|\d+\s*(?:of|/)\s*\d+|\.{3,}$|passed|failed|error", clean, re.I):
                progress_line = clean[:50]

    status = build_cmd or "🔨 Building..."
    preview = progress_line or last_meaningful or "(running)"

    # Try to extract test/build results
    result_lines = []
    for line in lines[-10:]:
        clean = _clean_for_display(line)
        if clean and re.search(r"pass|fail|error|warn|success|complet|✓|✗|❌|✅", clean, re.I):
            result_lines.append(clean[:60])

    if result_lines:
        preview = "\n".join(result_lines[-3:])

    return CardContent(
        mode=SessionMode.BUILD,
        icon="🔨",
        status_line=status[:50],
        preview=preview,
        heat=heat,
        idle_secs=idle_secs,
    )


def _render_shell(
    name: str, content: str, state: str,
    heat: float, idle_secs: float,
) -> CardContent:
    """Mode 4: Interactive shell display."""
    lines = content.strip().splitlines()

    # Find last command (line with $ or > prompt) and output after it
    last_cmd = ""
    output_after = []
    in_output = False

    for line in reversed(lines[-20:]):
        clean = _clean_for_display(line)
        if not clean:
            continue

        if in_output:
            if re.match(r"^[\w@\-\.]+[:\$#>~]|^\$\s|^>\s", clean):
                last_cmd = clean[:50]
                break
            output_after.insert(0, clean)
            if len(output_after) >= 3:
                break
        else:
            # Check if this is a prompt line
            if re.match(r"^[\w@\-\.]+[:\$#>~]|^\$\s|^>\s", clean):
                last_cmd = clean[:50]
                break
            output_after.insert(0, clean)
            in_output = True

    state_map = {
        "waiting": ("💤", "Shell idle"),
        "active": ("⚡", "Running"),
        "idle": ("💤", "Idle"),
        "error": ("❌", "Error"),
        "exited": ("⏹", "Exited"),
    }
    icon, label = state_map.get(state, ("❓", state))

    if last_cmd:
        status = f"{icon} {last_cmd}"
    else:
        status = f"{icon} {label}"

    preview = "\n".join(output_after[-3:]) if output_after else last_cmd or "(empty)"

    return CardContent(
        mode=SessionMode.SHELL,
        icon=icon,
        status_line=status[:50],
        preview=preview,
        heat=heat,
        idle_secs=idle_secs,
    )


# ── Mini visual renderer ─────────────────────────────────────

# Unicode half-block character for 2-row-per-char vertical compression
_UPPER_HALF = "▀"
_LOWER_HALF = "▄"
_FULL_BLOCK = "█"
_LIGHT_SHADE = "░"
_MEDIUM_SHADE = "▒"
_DARK_SHADE = "▓"


def _mini_render_pyte(screen: Any, target_rows: int, target_cols: int) -> str:
    """Render a pyte Screen into a mini thumbnail using block characters.

    Uses vertical half-block compression: each output row represents 2 screen rows.
    Characters are mapped to brightness levels → block shading.
    """
    src_rows = screen.lines
    src_cols = screen.columns

    # Calculate scale factors
    row_scale = max(1, (src_rows + target_rows - 1) // target_rows)
    col_scale = max(1, (src_cols + target_cols - 1) // target_cols)

    # Use 2x vertical compression with half blocks
    out_lines = []
    for out_r in range(target_rows):
        line = []
        src_r = out_r * row_scale * 2  # 2 screen rows per output row

        for out_c in range(target_cols):
            src_c = out_c * col_scale

            # Sample top half
            top_bright = _sample_brightness(screen, src_r, src_c, row_scale, col_scale)
            # Sample bottom half
            bot_bright = _sample_brightness(screen, src_r + row_scale, src_c, row_scale, col_scale)

            line.append(_brightness_to_block(top_bright, bot_bright))

        out_lines.append("".join(line).rstrip())

    # Trim trailing empty lines
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()

    return "\n".join(out_lines) if out_lines else "(empty screen)"


def _sample_brightness(screen: Any, start_r: int, start_c: int,
                       row_span: int, col_span: int) -> float:
    """Sample average brightness of a region. Returns 0.0 (empty) to 1.0 (full)."""
    total = 0
    count = 0
    for r in range(start_r, min(start_r + row_span, screen.lines)):
        for c in range(start_c, min(start_c + col_span, screen.columns)):
            char = screen.buffer[r][c]
            ch = char.data if char.data else " "
            if ch.strip():
                total += 1
            count += 1

    return total / count if count > 0 else 0.0


def _brightness_to_block(top: float, bot: float) -> str:
    """Convert top/bottom brightness to a block character."""
    t = top > 0.15  # has content
    b = bot > 0.15
    if t and b:
        return _FULL_BLOCK
    elif t and not b:
        return _UPPER_HALF
    elif not t and b:
        return _LOWER_HALF
    else:
        return " "


def _mini_render_text(content: str, target_rows: int, target_cols: int) -> str:
    """Fallback: render plain text content into a mini thumbnail."""
    lines = content.strip().splitlines()
    if not lines:
        return "(empty)"

    src_rows = len(lines)
    row_scale = max(1, (src_rows + target_rows * 2 - 1) // (target_rows * 2))

    out_lines = []
    for out_r in range(target_rows):
        line = []
        src_r = out_r * row_scale * 2

        for out_c in range(target_cols):
            src_c = out_c * 2  # horizontal compression

            # Sample top and bottom
            top_has = _text_has_content(lines, src_r, src_c, row_scale, 2)
            bot_has = _text_has_content(lines, src_r + row_scale, src_c, row_scale, 2)

            if top_has and bot_has:
                line.append(_FULL_BLOCK)
            elif top_has:
                line.append(_UPPER_HALF)
            elif bot_has:
                line.append(_LOWER_HALF)
            else:
                line.append(" ")

        out_lines.append("".join(line).rstrip())

    while out_lines and not out_lines[-1].strip():
        out_lines.pop()

    return "\n".join(out_lines) if out_lines else "(empty)"


def _text_has_content(lines: list[str], start_r: int, start_c: int,
                      row_span: int, col_span: int) -> bool:
    """Check if a region of text lines has non-space content."""
    for r in range(start_r, start_r + row_span):
        if r >= len(lines):
            return False
        line = lines[r]
        for c in range(start_c, start_c + col_span):
            if c < len(line) and line[c].strip():
                return True
    return False


# ── Helpers ───────────────────────────────────────────────────

_ansi_re = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[\(\)][A-B0-2]"
    r"|\x1b[><=cNOM78DEHZ]"
    r"|\x07"
)


def _clean_for_display(text: str) -> str:
    """Strip ANSI escapes and control chars for card display."""
    clean = _ansi_re.sub("", text)
    clean = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", clean)
    clean = re.sub(r"\^\[[\[\]()><=?][^\n]*", "", clean)
    clean = re.sub(r"\^\[", "", clean)
    return clean.strip()


def format_idle(secs: float) -> str:
    """Human-readable idle time."""
    if secs < 60:
        return f"{secs:.0f}s"
    elif secs < 3600:
        return f"{secs / 60:.0f}m"
    else:
        return f"{secs / 3600:.1f}h"
