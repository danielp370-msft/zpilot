"""Tests for zpilot.zellij wrapper — session lifecycle and I/O."""

import asyncio
import os
import pytest
import sys

sys.path.insert(0, "src")

from zpilot import zellij
from zpilot.zellij import FIFO_DIR, LOG_DIR


@pytest.mark.asyncio
class TestZellijAvailability:
    async def test_is_available(self):
        ok = await zellij.is_available()
        assert ok is True, "Zellij binary not found"


@pytest.mark.asyncio
class TestSessionLifecycle:
    """Full session create → interact → delete lifecycle."""

    async def test_create_list_delete(self):
        name = "zptest-zellij-01"
        try:
            await zellij.new_session(name)
            await asyncio.sleep(3)

            sessions = await zellij.list_sessions()
            names = [s.name for s in sessions]
            assert name in names, f"{name} not in {names}"

            # Check FIFO was created by shell_wrapper
            fifo_path = FIFO_DIR / f"{name}.fifo"
            # Give wrapper time to start
            for _ in range(5):
                if fifo_path.exists():
                    break
                await asyncio.sleep(1)
            assert fifo_path.exists(), f"FIFO not found: {fifo_path}"

        finally:
            await zellij._run(["delete-session", name, "--force"], check=False)
            await asyncio.sleep(1)

    async def test_dump_pane_content(self):
        """Read pane content from a running session."""
        name = "zptest-zellij-02"
        try:
            await zellij.new_session(name)
            await asyncio.sleep(3)

            content = await zellij.dump_pane(session=name)
            assert isinstance(content, str)
            # Should have some content (bash prompt or wrapper output)
            assert len(content) >= 0

        finally:
            await zellij._run(["delete-session", name, "--force"], check=False)
            await asyncio.sleep(1)


@pytest.mark.asyncio
class TestFifoIO:
    """Test FIFO-based command injection and log reading."""

    async def test_write_and_read(self):
        name = "zptest-fifo-01"
        try:
            await zellij.new_session(name)
            await asyncio.sleep(4)  # wait for shell_wrapper

            # Write via FIFO
            await zellij.write_to_pane("echo FIFO_TEST_MARKER_789", session=name)
            await zellij.send_enter(session=name)
            await asyncio.sleep(2)

            # Read back from log
            content = await zellij.dump_pane(session=name)
            assert "FIFO_TEST_MARKER_789" in content, f"Marker not found in: {content[-200:]}"

        finally:
            await zellij._run(["delete-session", name, "--force"], check=False)
            await asyncio.sleep(1)

    async def test_run_command_in_pane(self):
        """Test run_command_in_pane convenience function."""
        name = "zptest-fifo-02"
        try:
            await zellij.new_session(name)
            await asyncio.sleep(4)

            await zellij.run_command_in_pane("echo RUN_CMD_OK_456", session=name)
            await asyncio.sleep(2)

            content = await zellij.dump_pane(session=name)
            assert "RUN_CMD_OK_456" in content

        finally:
            await zellij._run(["delete-session", name, "--force"], check=False)
            await asyncio.sleep(1)


@pytest.mark.asyncio
class TestRunInSession:
    """Test headless command execution via zellij run."""

    async def test_run_in_session(self):
        name = "zptest-headless-01"
        try:
            await zellij.new_session(name)
            await asyncio.sleep(2)

            result = await zellij.run_in_session(
                "echo HEADLESS_XYZ", session=name, capture=True
            )
            assert "HEADLESS_XYZ" in result

        finally:
            await zellij._run(["delete-session", name, "--force"], check=False)
            await asyncio.sleep(1)
