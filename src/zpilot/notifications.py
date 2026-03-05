"""Pluggable notification adapters for zpilot."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from abc import ABC, abstractmethod

from .models import ZpilotConfig

log = logging.getLogger("zpilot.notifications")


class NotificationAdapter(ABC):
    """Base class for notification adapters."""

    @abstractmethod
    async def send(self, title: str, body: str, priority: str = "default") -> bool:
        """Send a notification. Returns True on success."""
        ...

    async def test(self) -> bool:
        """Test connectivity."""
        return await self.send("zpilot", "Test notification", "low")


class LogAdapter(NotificationAdapter):
    """Log notifications to stderr (for testing/development)."""

    async def send(self, title: str, body: str, priority: str = "default") -> bool:
        log.info(f"[NOTIFY] [{priority}] {title}: {body}")
        print(f"🔔 [{priority}] {title}: {body}", file=sys.stderr)
        return True


class DesktopAdapter(NotificationAdapter):
    """Desktop notifications via notify-send (Linux) or osascript (macOS)."""

    async def send(self, title: str, body: str, priority: str = "default") -> bool:
        try:
            if sys.platform == "darwin":
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e",
                    f'display notification "{body}" with title "{title}"',
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            else:
                urgency = {"high": "critical", "low": "low"}.get(priority, "normal")
                proc = await asyncio.create_subprocess_exec(
                    "notify-send", f"--urgency={urgency}", title, body,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            await proc.communicate()
            return proc.returncode == 0
        except FileNotFoundError:
            log.warning("Desktop notification tool not found")
            return False


class NtfyAdapter(NotificationAdapter):
    """Push notifications via ntfy.sh (or self-hosted)."""

    def __init__(self, server: str = "https://ntfy.sh", topic: str = "zpilot"):
        self.url = f"{server.rstrip('/')}/{topic}"

    async def send(self, title: str, body: str, priority: str = "default") -> bool:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.url,
                    content=body,
                    headers={
                        "Title": title,
                        "Priority": {"high": "high", "low": "low"}.get(
                            priority, "default"
                        ),
                        "Tags": "computer,zpilot",
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except Exception as e:
            log.warning(f"ntfy notification failed: {e}")
            return False


class WebhookAdapter(NotificationAdapter):
    """POST JSON to a webhook URL."""

    def __init__(self, url: str):
        self.url = url

    async def send(self, title: str, body: str, priority: str = "default") -> bool:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.url,
                    json={"title": title, "body": body, "priority": priority},
                    timeout=10,
                )
                return 200 <= resp.status_code < 300
        except Exception as e:
            log.warning(f"Webhook notification failed: {e}")
            return False


def create_adapter(config: ZpilotConfig) -> NotificationAdapter:
    """Create a notification adapter from config."""
    adapter_name = config.notify_adapter.lower()
    if adapter_name == "ntfy":
        return NtfyAdapter(server=config.ntfy_server, topic=config.ntfy_topic)
    elif adapter_name == "desktop":
        return DesktopAdapter()
    elif adapter_name == "webhook":
        return WebhookAdapter(url="")  # TODO: add webhook_url to config
    else:
        return LogAdapter()
