"""Terminal thumbnail renderer — pyte screen buffer → PNG image.

Renders the terminal screen state as a small PNG image that looks like
a real terminal screenshot. Used for session card thumbnails in the
web dashboard.
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# ── Configuration ─────────────────────────────────────────────

THUMB_COLS = 80       # terminal columns to render
THUMB_ROWS = 24       # terminal rows to render
CHAR_W = 6            # pixels per character width
CHAR_H = 11           # pixels per character height
FONT_SIZE = 10
PAD = 4               # padding around content

BG_COLOR = (30, 30, 46)        # dark background (catppuccin mocha base)
DEFAULT_FG = (205, 214, 244)   # light text
CURSOR_COLOR = (245, 194, 231) # pink cursor

# Cache: session_name → (timestamp, png_bytes)
_cache: dict[str, tuple[float, bytes]] = {}
CACHE_TTL = 3.0  # seconds

# ── Color palette ─────────────────────────────────────────────

_ANSI_COLORS = {
    "black":   (69, 71, 90),
    "red":     (243, 139, 168),
    "green":   (166, 227, 161),
    "yellow":  (249, 226, 175),
    "blue":    (137, 180, 250),
    "magenta": (245, 194, 231),
    "cyan":    (148, 226, 213),
    "white":   (186, 194, 222),
    "default": DEFAULT_FG,
}

_ANSI_BRIGHT = {
    "black":   (88, 91, 112),
    "red":     (243, 139, 168),
    "green":   (166, 227, 161),
    "yellow":  (249, 226, 175),
    "blue":    (137, 180, 250),
    "magenta": (245, 194, 231),
    "cyan":    (148, 226, 213),
    "white":   (205, 214, 244),
}

_BG_COLORS = {
    "black":   (30, 30, 46),
    "red":     (60, 30, 36),
    "green":   (30, 50, 36),
    "yellow":  (55, 50, 30),
    "blue":    (30, 36, 60),
    "magenta": (50, 30, 50),
    "cyan":    (30, 50, 50),
    "white":   (60, 60, 66),
    "default": BG_COLOR,
}


def _resolve_fg(color: str, bold: bool = False) -> tuple[int, int, int]:
    """Resolve a pyte foreground color to RGB."""
    if color == "default":
        return DEFAULT_FG
    if color in _ANSI_COLORS:
        return _ANSI_BRIGHT[color] if bold else _ANSI_COLORS[color]
    # 256-color: string of int
    try:
        idx = int(color)
        return _256_to_rgb(idx)
    except (ValueError, TypeError):
        pass
    # 24-bit hex: "aabbcc"
    if isinstance(color, str) and len(color) == 6:
        try:
            return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
        except ValueError:
            pass
    return DEFAULT_FG


def _resolve_bg(color: str) -> tuple[int, int, int]:
    """Resolve a pyte background color to RGB."""
    if color == "default":
        return BG_COLOR
    if color in _BG_COLORS:
        return _BG_COLORS[color]
    try:
        idx = int(color)
        return _256_to_rgb(idx)
    except (ValueError, TypeError):
        pass
    if isinstance(color, str) and len(color) == 6:
        try:
            return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
        except ValueError:
            pass
    return BG_COLOR


def _256_to_rgb(idx: int) -> tuple[int, int, int]:
    """Convert 256-color index to RGB."""
    if idx < 16:
        basic = [
            (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
            (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
            (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
            (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
        ]
        return basic[idx]
    if idx < 232:
        idx -= 16
        r = (idx // 36) * 51
        g = ((idx % 36) // 6) * 51
        b = (idx % 6) * 51
        return (r, g, b)
    # Grayscale
    v = 8 + (idx - 232) * 10
    return (v, v, v)


# ── Font loading ──────────────────────────────────────────────

_font: ImageFont.FreeTypeFont | None = None


def _get_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _font
    if _font is not None:
        return _font
    mono_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
    ]
    for p in mono_paths:
        if os.path.exists(p):
            _font = ImageFont.truetype(p, FONT_SIZE)
            return _font
    _font = ImageFont.load_default()
    return _font


# ── Renderer ──────────────────────────────────────────────────

def render_thumbnail(
    pyte_screen: Any,
    cols: int | None = None,
    rows: int | None = None,
    scale: float = 1.0,
) -> bytes:
    """Render a pyte Screen object to a PNG thumbnail.

    Returns PNG image bytes.
    """
    cols = cols or min(pyte_screen.columns, THUMB_COLS)
    rows = rows or min(pyte_screen.lines, THUMB_ROWS)
    font = _get_font()

    cw = int(CHAR_W * scale)
    ch = int(CHAR_H * scale)
    pad = int(PAD * scale)

    img_w = cols * cw + pad * 2
    img_h = rows * ch + pad * 2

    img = Image.new("RGB", (img_w, img_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    for row in range(rows):
        if row >= pyte_screen.lines:
            break
        row_buf = pyte_screen.buffer[row]
        for col in range(cols):
            if col >= pyte_screen.columns:
                break
            char = row_buf[col]
            ch_str = char.data if char.data else " "

            fg = _resolve_fg(char.fg, bold=char.bold)
            bg = _resolve_bg(char.bg)

            if char.reverse:
                fg, bg = bg, fg

            x = pad + col * cw
            y = pad + row * ch

            # Draw background if not default
            if bg != BG_COLOR:
                draw.rectangle([x, y, x + cw - 1, y + ch - 1], fill=bg)

            # Draw character
            if ch_str.strip():
                draw.text((x, y), ch_str, fill=fg, font=font)

    # Draw cursor
    cx = pad + pyte_screen.cursor.x * cw
    cy = pad + pyte_screen.cursor.y * ch
    if 0 <= pyte_screen.cursor.x < cols and 0 <= pyte_screen.cursor.y < rows:
        draw.rectangle(
            [cx, cy + ch - 2, cx + cw - 1, cy + ch - 1],
            fill=CURSOR_COLOR,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_thumbnail_from_log(
    session: str,
    cols: int = THUMB_COLS,
    rows: int = THUMB_ROWS,
) -> bytes | None:
    """Render a thumbnail from a session's log file.

    Uses pyte to replay the log and then renders the screen.
    Returns PNG bytes or None if no log found.
    """
    # Check cache
    cached = _cache.get(session)
    if cached and (time.monotonic() - cached[0]) < CACHE_TTL:
        return cached[1]

    import pyte

    log_dir = Path("/tmp/zpilot/logs")
    raw = ""

    # Try session--main.log first
    log_file = log_dir / f"{session}--main.log"
    if log_file.exists() and log_file.stat().st_size > 0:
        raw = log_file.read_bytes().decode("utf-8", errors="replace")

    if not raw:
        # Try any matching log
        logs = sorted(log_dir.glob(f"{session}--*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            raw = logs[0].read_bytes().decode("utf-8", errors="replace")

    if not raw:
        log_file = log_dir / f"{session}.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            raw = log_file.read_bytes().decode("utf-8", errors="replace")

    if not raw:
        return None

    # Feed only tail through pyte
    max_bytes = 200_000
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(raw)

    png_bytes = render_thumbnail(screen, cols, rows)

    # Cache it
    _cache[session] = (time.monotonic(), png_bytes)
    return png_bytes
