"""Configuration loading for zpilot."""

from __future__ import annotations

import os
from pathlib import Path

from .models import ZpilotConfig

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_DIR = Path(os.environ.get("ZPILOT_CONFIG_DIR", "~/.config/zpilot")).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.toml"

DEFAULT_CONFIG_TOML = """\
[general]
poll_interval = 5
idle_threshold = 30
events_file = "/tmp/zpilot/events.jsonl"

[detection]
bel_enabled = true
prompt_patterns = [
    '^\\\\$ $',
    '^> $',
    '^❯ ',
]
error_patterns = [
    '^Error:',
    '^FATAL:',
    '^panic:',
]

[notifications]
enabled = true
adapter = "log"
notify_on = ["waiting", "error", "exited"]

[notifications.ntfy]
topic = "zpilot"
server = "https://ntfy.sh"
"""


def load_config() -> ZpilotConfig:
    """Load configuration from TOML file, falling back to defaults."""
    cfg = ZpilotConfig()

    if not CONFIG_FILE.exists():
        return cfg

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    general = data.get("general", {})
    cfg.poll_interval = general.get("poll_interval", cfg.poll_interval)
    cfg.idle_threshold = general.get("idle_threshold", cfg.idle_threshold)
    cfg.events_file = general.get("events_file", cfg.events_file)

    detection = data.get("detection", {})
    cfg.bel_detection = detection.get("bel_enabled", cfg.bel_detection)
    if "prompt_patterns" in detection:
        cfg.prompt_patterns = detection["prompt_patterns"]
    if "error_patterns" in detection:
        cfg.error_patterns = detection["error_patterns"]

    notif = data.get("notifications", {})
    cfg.notify_enabled = notif.get("enabled", cfg.notify_enabled)
    cfg.notify_adapter = notif.get("adapter", cfg.notify_adapter)
    if "notify_on" in notif:
        cfg.notify_on = notif["notify_on"]

    ntfy = notif.get("ntfy", {})
    cfg.ntfy_topic = ntfy.get("topic", cfg.ntfy_topic)
    cfg.ntfy_server = ntfy.get("server", cfg.ntfy_server)

    return cfg


def ensure_config() -> None:
    """Create default config file if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG_TOML)
