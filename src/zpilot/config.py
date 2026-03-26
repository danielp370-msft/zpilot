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

    http = data.get("http", {})
    cfg.http_host = os.environ.get("ZPILOT_HTTP_HOST", http.get("host", cfg.http_host))
    cfg.http_port = int(os.environ.get("ZPILOT_HTTP_PORT", str(http.get("port", cfg.http_port))))

    # Token resolution: support token_file (file: prefix or separate key)
    raw_token = os.environ.get("ZPILOT_HTTP_TOKEN", http.get("token", cfg.http_token))
    token_file = http.get("token_file", "")
    if raw_token and raw_token.startswith("file:"):
        # Inline file reference: token = "file:~/.config/zpilot/tokens/http.key"
        from .security import load_token
        loaded = load_token(raw_token[5:])
        cfg.http_token = loaded or ""
    elif token_file:
        # Separate key: token_file = "~/.config/zpilot/tokens/http.key"
        from .security import load_token
        loaded = load_token(token_file)
        cfg.http_token = loaded or ""
    else:
        cfg.http_token = raw_token

    tls_env = os.environ.get("ZPILOT_HTTP_TLS", "")
    if tls_env:
        cfg.http_tls = tls_env.lower() not in ("0", "false", "no", "off")
    else:
        cfg.http_tls = http.get("tls", cfg.http_tls)
    cfg.http_cert_file = os.environ.get("ZPILOT_HTTP_CERT", http.get("cert_file", cfg.http_cert_file))
    cfg.http_key_file = os.environ.get("ZPILOT_HTTP_KEY", http.get("key_file", cfg.http_key_file))

    return cfg


def ensure_config() -> None:
    """Create default config file if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG_TOML)
