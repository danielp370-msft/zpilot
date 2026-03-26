"""Security utilities for zpilot — token hygiene, file permissions, rate limiting."""

from __future__ import annotations

import logging
import os
import secrets
import stat
import time
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

TOKEN_DIR = Path(os.environ.get("ZPILOT_CONFIG_DIR", "~/.config/zpilot")).expanduser() / "tokens"


# ── Token masking ──────────────────────────────────────────────────

def mask_token(token: str, visible: int = 4) -> str:
    """Mask a token for safe display/logging: show first and last `visible` chars.

    >>> mask_token("1HHPFM2h2rjG_S5r4POfxsgLTlQVUgW2gLx8kHhE8lY")
    '1HHP...8lY'
    >>> mask_token("short")
    '****'
    >>> mask_token("")
    '(empty)'
    """
    if not token:
        return "(empty)"
    if len(token) <= visible * 2:
        return "****"
    return f"{token[:visible]}...{token[-3:]}"


# ── Token file management ─────────────────────────────────────────

def _ensure_token_dir() -> Path:
    """Create the token directory with restricted permissions."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_DIR.chmod(0o700)
    return TOKEN_DIR


def save_token(name: str, token: str) -> Path:
    """Save a token to a secure file (chmod 600).

    Returns the path to the token file.
    """
    _ensure_token_dir()
    token_path = TOKEN_DIR / f"{name}.key"
    token_path.write_text(token + "\n")
    token_path.chmod(0o600)
    log.info("Token saved to %s (permissions: 600)", token_path)
    return token_path


def load_token(name_or_path: str | Path) -> str | None:
    """Load a token by name (from token dir) or by file path.

    If name_or_path contains a slash or ends in .key, treat as a file path.
    Otherwise, look up in TOKEN_DIR/{name}.key.
    """
    s = str(name_or_path)
    if "/" in s or s.endswith(".key"):
        p = Path(s).expanduser()
    else:
        p = TOKEN_DIR / f"{s}.key"

    if not p.exists():
        log.warning("Token file not found: %s", p)
        return None
    token = p.read_text().strip()
    return token if token else None


def resolve_token(
    *,
    explicit: str | None = None,
    config_token: str = "",
    env_var: str = "ZPILOT_HTTP_TOKEN",
    token_name: str = "http",
    auto_generate: bool = True,
) -> str:
    """Resolve a token from multiple sources in priority order:

    1. Explicit value (CLI --token-file or --token)
    2. Config file value (config.toml [http] token or token_file)
    3. Environment variable
    4. Token file (~/.config/zpilot/tokens/{name}.key)
    5. Auto-generate and save (if auto_generate=True)

    Never logs the actual token value.
    """
    # 1. Explicit
    if explicit:
        log.debug("Using explicitly provided token")
        return explicit

    # 2. Config value — check if it's a file reference
    if config_token:
        if config_token.startswith("file:"):
            file_path = config_token[5:]
            loaded = load_token_from_file(file_path)
            if loaded:
                log.debug("Token loaded from config file reference: %s", file_path)
                return loaded
            log.warning("Token file referenced in config not found: %s", file_path)
        else:
            log.debug("Using token from config")
            return config_token

    # 3. Environment variable
    env_token = os.environ.get(env_var, "")
    if env_token:
        log.debug("Using token from %s environment variable", env_var)
        return env_token

    # 4. Token file
    saved = load_token(token_name)
    if saved:
        log.debug("Token loaded from %s", TOKEN_DIR / f"{token_name}.key")
        return saved

    # 5. Auto-generate
    if auto_generate:
        token = secrets.token_urlsafe(32)
        save_token(token_name, token)
        log.warning(
            "No token configured — generated and saved to %s/%s.key (token: %s)",
            TOKEN_DIR, token_name, mask_token(token),
        )
        return token

    raise ValueError(f"No token available for '{token_name}' and auto_generate=False")


# ── File permissions checking ─────────────────────────────────────

def check_file_permissions(path: str | Path, warn_only: bool = True) -> bool:
    """Check that a sensitive file has restricted permissions (owner-only).

    Like SSH's StrictModes — warns or errors if group/other can read.
    Returns True if permissions are OK.
    """
    p = Path(path)
    if not p.exists():
        return True  # nothing to check

    try:
        st = p.stat()
    except OSError:
        return True

    mode = st.st_mode
    is_ok = True

    # Check group and other read/write bits
    if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
        perms = oct(mode & 0o777)
        msg = (
            f"Insecure permissions {perms} on {p} — "
            f"group/other can read. Fix with: chmod 600 {p}"
        )
        if warn_only:
            log.warning(msg)
        else:
            raise PermissionError(msg)
        is_ok = False

    return is_ok


def audit_config_permissions() -> list[str]:
    """Check permissions on all sensitive zpilot config files.

    Returns a list of warning messages (empty = all OK).
    """
    config_dir = Path(os.environ.get("ZPILOT_CONFIG_DIR", "~/.config/zpilot")).expanduser()
    warnings: list[str] = []

    sensitive_files = [
        config_dir / "config.toml",
        config_dir / "nodes.toml",
    ]
    # Also check token files
    if TOKEN_DIR.exists():
        sensitive_files.extend(TOKEN_DIR.glob("*.key"))

    for f in sensitive_files:
        if f.exists():
            st = f.stat()
            mode = st.st_mode
            if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
                perms = oct(mode & 0o777)
                warnings.append(f"⚠ Insecure permissions {perms} on {f} — run: chmod 600 {f}")

    return warnings


# ── Auth rate limiting ────────────────────────────────────────────

class AuthRateLimiter:
    """Track failed auth attempts per IP. Lockout after max_failures."""

    def __init__(self, max_failures: int = 10, lockout_seconds: float = 60.0):
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)

    def record_failure(self, client_ip: str) -> None:
        """Record a failed auth attempt."""
        now = time.monotonic()
        self._failures[client_ip].append(now)
        # Prune old entries
        cutoff = now - self.lockout_seconds
        self._failures[client_ip] = [t for t in self._failures[client_ip] if t > cutoff]

    def is_locked_out(self, client_ip: str) -> bool:
        """Check if a client IP is currently locked out."""
        now = time.monotonic()
        cutoff = now - self.lockout_seconds
        recent = [t for t in self._failures.get(client_ip, []) if t > cutoff]
        self._failures[client_ip] = recent
        return len(recent) >= self.max_failures

    def record_success(self, client_ip: str) -> None:
        """Clear failures for an IP on successful auth."""
        self._failures.pop(client_ip, None)
