"""Rich TUI renderers for flow content.

Converts flow data into Rich renderables for the Textual TUI dashboard.
Each MIME category gets an appropriate renderer:
  text/markdown  → Markdown widget
  application/json → colored JSON tree
  text/html → stripped Rich markup
  text/plain → syntax-highlighted text
  image/* → half-block pixel art
  data/csv → DataTable
  diffs → syntax-highlighted unified diff
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from rich.json import JSON as RichJSON
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


def render_flow_rich(name: str, mime: str, source_path: str | None = None,
                     content: str | None = None, max_lines: int = 30) -> Any:
    """Render flow content as a Rich renderable for TUI display.

    Returns a Rich object (Text, Panel, Table, etc.) suitable for
    RichLog.write() or Static.update().
    """
    if not content and source_path and os.path.exists(source_path):
        try:
            with open(source_path, "rb") as f:
                raw = f.read(50000)
            content = raw.decode("utf-8", errors="replace")
        except Exception:
            content = "(unreadable)"

    if not content:
        return Text("(empty flow)", style="dim")

    # Route to appropriate renderer
    if mime == "text/markdown" or mime == "text/x-markdown":
        return _render_markdown(name, content)
    elif mime == "application/json":
        return _render_json(name, content)
    elif mime == "text/html":
        return _render_html(name, content)
    elif mime.startswith("text/x-diff") or mime == "text/x-patch":
        return _render_diff(name, content)
    elif mime == "text/csv":
        return _render_csv(name, content, max_lines)
    elif mime.startswith("text/"):
        return _render_text(name, content, mime, max_lines)
    elif mime.startswith("image/"):
        return _render_image_blocks(name, source_path)
    else:
        return _render_binary(name, source_path, content)


def _render_markdown(name: str, content: str) -> Panel:
    """Render markdown as Rich markup."""
    # Convert basic markdown to Rich markup
    lines = content.split("\n")
    parts = []
    for line in lines[:40]:
        if line.startswith("# "):
            parts.append(Text(line[2:], style="bold magenta"))
        elif line.startswith("## "):
            parts.append(Text(line[3:], style="bold cyan"))
        elif line.startswith("### "):
            parts.append(Text(line[4:], style="bold"))
        elif line.startswith("- ") or line.startswith("* "):
            parts.append(Text(f"  • {line[2:]}", style=""))
        elif line.startswith("```"):
            parts.append(Text("─" * 30, style="dim"))
        elif re.match(r"^\d+\.\s", line):
            parts.append(Text(f"  {line}", style=""))
        elif line.startswith("> "):
            parts.append(Text(f"  │ {line[2:]}", style="italic dim"))
        else:
            # Bold and italic
            rendered = line
            rendered = re.sub(r"\*\*(.+?)\*\*", r"[bold]\1[/bold]", rendered)
            rendered = re.sub(r"\*(.+?)\*", r"[italic]\1[/italic]", rendered)
            rendered = re.sub(r"`(.+?)`", r"[cyan]\1[/cyan]", rendered)
            parts.append(Text.from_markup(rendered))

    text = Text("\n").join(parts)
    return Panel(text, title=f"📝 {name}", border_style="blue")


def _render_json(name: str, content: str) -> Panel:
    """Render JSON with syntax highlighting."""
    try:
        parsed = json.loads(content)
        rich_json = RichJSON(json.dumps(parsed, indent=2))
        return Panel(rich_json, title=f"📋 {name}", border_style="yellow")
    except json.JSONDecodeError:
        return Panel(Text(content[:500], style="red"), title=f"📋 {name} (invalid JSON)")


def _render_html(name: str, content: str) -> Panel:
    """Strip HTML tags, convert to Rich markup."""
    # Basic tag stripping with semantic conversion
    text = content
    text = re.sub(r"<h[1-3][^>]*>(.*?)</h[1-3]>", r"\n[bold magenta]\1[/bold magenta]\n", text, flags=re.S)
    text = re.sub(r"<b>(.*?)</b>|<strong>(.*?)</strong>", r"[bold]\1\2[/bold]", text, flags=re.S)
    text = re.sub(r"<i>(.*?)</i>|<em>(.*?)</em>", r"[italic]\1\2[/italic]", text, flags=re.S)
    text = re.sub(r"<code>(.*?)</code>", r"[cyan]\1[/cyan]", text, flags=re.S)
    text = re.sub(r"<li>(.*?)</li>", r"  • \1", text, flags=re.S)
    text = re.sub(r"<p>(.*?)</p>", r"\1\n", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)  # strip remaining tags
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Panel(Text.from_markup(text[:1000]), title=f"🌐 {name}", border_style="green")


def _render_diff(name: str, content: str) -> Panel:
    """Render unified diff with syntax highlighting."""
    syntax = Syntax(content[:3000], "diff", theme="monokai", line_numbers=True)
    return Panel(syntax, title=f"📝 {name}", border_style="cyan")


def _render_csv(name: str, content: str, max_lines: int) -> Panel:
    """Render CSV as a Rich table."""
    import csv
    import io
    reader = csv.reader(io.StringIO(content))
    table = Table(title=name, show_lines=True, border_style="blue")
    headers = next(reader, None)
    if headers:
        for h in headers:
            table.add_column(h.strip(), style="cyan")
        for i, row in enumerate(reader):
            if i >= max_lines:
                table.add_row(*["..." for _ in headers])
                break
            table.add_row(*[c.strip() for c in row])
    return Panel(table, title=f"📊 {name}", border_style="blue")


def _render_text(name: str, content: str, mime: str, max_lines: int) -> Panel:
    """Render plain text with optional syntax highlighting."""
    # Detect language from MIME or content
    lang = None
    if "python" in mime:
        lang = "python"
    elif "javascript" in mime or "typescript" in mime:
        lang = "javascript"
    elif "yaml" in mime:
        lang = "yaml"
    elif content.strip().startswith("def ") or content.strip().startswith("import "):
        lang = "python"

    lines = content.split("\n")[:max_lines]
    text = "\n".join(lines)

    if lang:
        syntax = Syntax(text, lang, theme="monokai", line_numbers=True)
        return Panel(syntax, title=f"📄 {name}", border_style="white")
    return Panel(Text(text), title=f"📄 {name}", border_style="white")


def _render_image_blocks(name: str, source_path: str | None) -> Panel:
    """Render image as half-block pixel art."""
    if not source_path or not os.path.exists(source_path):
        return Panel(Text("(image not found)", style="dim"), title=f"🖼 {name}")

    try:
        from PIL import Image
        img = Image.open(source_path).convert("RGB")

        # Scale to fit terminal (~60 chars wide, ~20 rows with half-blocks)
        target_w, target_h = 60, 20
        img_w, img_h = img.size
        scale = min(target_w / img_w, (target_h * 2) / img_h)
        new_w = max(1, int(img_w * scale))
        new_h = max(2, int(img_h * scale))
        if new_h % 2:
            new_h += 1
        img = img.resize((new_w, new_h), Image.Resampling.NEAREST)

        lines = []
        for y in range(0, new_h, 2):
            line = Text()
            for x in range(new_w):
                top = img.getpixel((x, y))
                bot = img.getpixel((x, y + 1)) if y + 1 < new_h else (0, 0, 0)
                # Use half-block: top color as foreground, bottom as background
                fg = f"#{top[0]:02x}{top[1]:02x}{top[2]:02x}"
                bg = f"#{bot[0]:02x}{bot[1]:02x}{bot[2]:02x}"
                line.append("▀", style=f"{fg} on {bg}")
            lines.append(line)

        result = Text("\n").join(lines)
        return Panel(result, title=f"🖼 {name} ({img_w}×{img_h})", border_style="magenta")
    except Exception as e:
        return Panel(Text(f"(render error: {e})", style="red"), title=f"🖼 {name}")


def _render_binary(name: str, source_path: str | None, content: str) -> Panel:
    """Render binary as hex dump."""
    if source_path and os.path.exists(source_path):
        with open(source_path, "rb") as f:
            head = f.read(256)
    else:
        head = content[:256].encode("utf-8", errors="replace")

    lines = []
    for i in range(0, len(head), 16):
        chunk = head[i:i+16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"[dim]{i:08x}[/dim]  {hexs:<48s}  [cyan]{asci}[/cyan]")

    return Panel(
        Text.from_markup("\n".join(lines)),
        title=f"📦 {name} (binary)",
        border_style="red",
    )


# ── Sparkline / bar chart helpers ─────────────────────────────

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 40) -> Text:
    """Render a sparkline from numeric values."""
    if not values:
        return Text("(no data)", style="dim")
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1.0
    text = Text()
    for v in values[-width:]:
        idx = min(len(_SPARK_CHARS) - 1, int((v - mn) / rng * (len(_SPARK_CHARS) - 1)))
        text.append(_SPARK_CHARS[idx], style="green")
    return text


def hbar(label: str, value: float, max_val: float = 1.0,
         width: int = 30, color: str = "green") -> Text:
    """Render a horizontal bar chart row."""
    pct = min(1.0, value / max_val) if max_val > 0 else 0
    filled = int(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    pct_str = f"{pct*100:.0f}%"
    text = Text()
    text.append(f"{label:>12s} ", style="bold")
    text.append(bar, style=color)
    text.append(f" {pct_str}", style="dim")
    return text
