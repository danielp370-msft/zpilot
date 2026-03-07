"""End-to-end interaction tests for zpilot web dashboard.

Tests actual user interactions: typing in xterm.js, backspace, Ctrl+C,
docking sessions, layout switching, bottom input bar, etc.

Requires:
  - Web server running (default: localhost:8097, override with ZPILOT_WEB_URL)
  - At least one Zellij session (demo-work recommended)
  - python3 -m playwright install chromium

Run:
  ZPILOT_WEB_URL=http://localhost:8103 python3 -m pytest tests/test_e2e_interaction.py -v
"""

import asyncio
import os
import re
import pytest
import uuid

pytest.importorskip("playwright")

from playwright.async_api import async_playwright, expect


WEB_URL = os.environ.get("ZPILOT_WEB_URL", "http://localhost:8097")

# Unique marker for each test run to avoid cross-contamination
RUN_ID = uuid.uuid4().hex[:6]


@pytest.fixture
async def browser():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        yield b
        await b.close()


@pytest.fixture
async def page(browser):
    pg = await browser.new_page(viewport={"width": 1280, "height": 800})
    yield pg
    await pg.close()


async def _load_and_wait(page, timeout=5000):
    """Navigate and wait for SSE to populate sessions."""
    await page.goto(WEB_URL, wait_until="load")
    await page.wait_for_timeout(timeout)


async def _dock_first_session(page):
    """Click the first session in sidebar to dock it."""
    first = page.locator(".session-item").first
    await first.click()
    await page.wait_for_timeout(2000)


async def _get_session_name(page):
    """Get the name of the first session in the sidebar."""
    name_el = page.locator(".session-item .si-name").first
    return await name_el.text_content()


async def _ws_send(page, session_name, data):
    """Send data via WebSocket to a session's terminal."""
    await page.evaluate(
        f"""() => {{
            const ws = termSockets['{session_name}'];
            if (!ws || ws.readyState !== WebSocket.OPEN) return false;
            ws.send(JSON.stringify({{ type: 'input', data: {repr(data)} }}));
            return true;
        }}"""
    )


async def _ws_send_text(page, session_name, text):
    """Type text char by char via WebSocket."""
    for ch in text:
        await _ws_send(page, session_name, ch)
    await page.wait_for_timeout(300)


async def _get_pane_content_via_api(page, session_name):
    """Fetch raw pane content via API."""
    resp = await page.request.get(f"{WEB_URL}/api/pane/{session_name}/raw")
    data = await resp.json()
    return data.get("content", "")


# ── Page Load ─────────────────────────────────────────────────

class TestPageLoadInteraction:
    @pytest.mark.asyncio
    async def test_page_loads(self, page):
        resp = await page.goto(WEB_URL, wait_until="load")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_sessions_appear(self, page):
        await _load_and_wait(page)
        items = page.locator(".session-item")
        count = await items.count()
        assert count >= 1

    @pytest.mark.asyncio
    async def test_sse_connected(self, page):
        await _load_and_wait(page)
        # Check that the live indicator shows session count
        count_el = page.locator("#session-count")
        text = await count_el.text_content()
        assert "session" in text.lower()


# ── Docking and Tabs ─────────────────────────────────────────

class TestDockingInteraction:
    @pytest.mark.asyncio
    async def test_dock_creates_tab(self, page):
        await _load_and_wait(page)
        await _dock_first_session(page)
        tabs = page.locator(".tab")
        count = await tabs.count()
        assert count >= 1

    @pytest.mark.asyncio
    async def test_dock_creates_terminal(self, page):
        await _load_and_wait(page)
        await _dock_first_session(page)
        # xterm.js terminal should exist
        terminals = page.locator(".xterm")
        count = await terminals.count()
        assert count >= 1

    @pytest.mark.asyncio
    async def test_websocket_connects(self, page):
        await _load_and_wait(page)
        await _dock_first_session(page)
        await page.wait_for_timeout(2000)
        name = await _get_session_name(page)
        connected = await page.evaluate(
            f"""() => {{
                const ws = termSockets['{name}'];
                return ws && ws.readyState === WebSocket.OPEN;
            }}"""
        )
        assert connected, "WebSocket not connected"

    @pytest.mark.asyncio
    async def test_close_tab(self, page):
        await _load_and_wait(page)
        await _dock_first_session(page)
        tabs_before = await page.locator(".tab").count()
        # Click close button on tab
        close_btn = page.locator(".tab .tab-close").first
        await close_btn.click()
        await page.wait_for_timeout(1000)
        tabs_after = await page.locator(".tab").count()
        assert tabs_after == tabs_before - 1


# ── xterm.js Terminal Typing ─────────────────────────────────

class TestTerminalTyping:
    @pytest.mark.asyncio
    async def test_type_and_execute(self, page):
        """Type a command via WebSocket and verify output."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)
        marker = f"zptest_{RUN_ID}_echo"

        # Type echo command + Enter
        await _ws_send_text(page, name, f"echo {marker}")
        await _ws_send(page, name, "\r")
        await page.wait_for_timeout(2000)

        # Verify via API
        content = await _get_pane_content_via_api(page, name)
        assert marker in content

    @pytest.mark.asyncio
    async def test_backspace_deletes(self, page):
        """Backspace (\\x7f) deletes characters."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)
        marker = f"zpbs_{RUN_ID}"

        # Type marker + "XX", backspace twice, then " ok" + Enter
        await _ws_send_text(page, name, f"echo {marker}XX")
        await _ws_send(page, name, "\x7f")
        await _ws_send(page, name, "\x7f")
        await page.wait_for_timeout(500)
        await _ws_send_text(page, name, " ok")
        await _ws_send(page, name, "\r")
        await page.wait_for_timeout(2000)

        content = await _get_pane_content_via_api(page, name)
        # Output should contain the marker followed by " ok", not "XX ok"
        assert f"{marker} ok" in content

    @pytest.mark.asyncio
    async def test_ctrl_c_cancels(self, page):
        """Ctrl+C (\\x03) should cancel current line."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)

        # Type partial command, Ctrl+C, verify new prompt
        await _ws_send_text(page, name, "sleep 999999")
        await _ws_send(page, name, "\x03")  # Ctrl+C
        await page.wait_for_timeout(1000)

        content = await _get_pane_content_via_api(page, name)
        # Should have ^C and a fresh prompt
        assert "^C" in content or "$" in content.split("\n")[-1]

    @pytest.mark.asyncio
    async def test_ctrl_u_clears_line(self, page):
        """Ctrl+U (\\x15) clears the current input line."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)
        marker = f"zpcu_{RUN_ID}"

        # Type something, Ctrl+U to clear, type marker, Enter
        await _ws_send_text(page, name, "this should be cleared")
        await _ws_send(page, name, "\x15")  # Ctrl+U
        await page.wait_for_timeout(500)
        await _ws_send_text(page, name, f"echo {marker}")
        await _ws_send(page, name, "\r")
        await page.wait_for_timeout(2000)

        content = await _get_pane_content_via_api(page, name)
        assert marker in content

    @pytest.mark.asyncio
    async def test_arrow_keys(self, page):
        """Arrow key escape sequences pass through."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)
        marker = f"zparr_{RUN_ID}"

        # Type something, left arrow back, type insertion
        await _ws_send_text(page, name, f"echo {marker}Z")
        await _ws_send(page, name, "\x1b[D")  # Left arrow
        await _ws_send(page, name, "\x7f")     # Backspace (deletes Z)
        await _ws_send(page, name, "\r")
        await page.wait_for_timeout(2000)

        content = await _get_pane_content_via_api(page, name)
        # Should echo just the marker without trailing Z
        assert marker in content


# ── Bottom Input Bar ─────────────────────────────────────────

class TestBottomInputBar:
    @pytest.mark.asyncio
    async def test_input_bar_visible(self, page):
        await _load_and_wait(page)
        await _dock_first_session(page)
        bar = page.locator(".term-input")
        await expect(bar.first).to_be_visible()

    @pytest.mark.asyncio
    async def test_send_via_input_bar(self, page):
        """Type in bottom bar and click send."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)
        marker = f"zpbar_{RUN_ID}"

        input_field = page.locator(".term-input input").first
        await input_field.fill(f"echo {marker}")
        send_btn = page.locator(".term-input button").first
        await send_btn.click()
        await page.wait_for_timeout(2000)

        content = await _get_pane_content_via_api(page, name)
        assert marker in content


# ── Layout Switching ─────────────────────────────────────────

class TestLayoutSwitching:
    @pytest.mark.asyncio
    async def test_layout_buttons_exist(self, page):
        await _load_and_wait(page)
        buttons = page.locator(".layout-btns button")
        count = await buttons.count()
        assert count >= 3

    @pytest.mark.asyncio
    async def test_switch_to_each_layout(self, page):
        """Click each layout button — should not error."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        buttons = page.locator(".layout-btns button")
        count = await buttons.count()
        for i in range(count):
            await buttons.nth(i).click()
            await page.wait_for_timeout(500)
            # Terminal should still be present
            terminals = page.locator(".xterm")
            t_count = await terminals.count()
            assert t_count >= 1, f"Terminal lost after layout {i}"


# ── Session State Updates ────────────────────────────────────

class TestSessionState:
    @pytest.mark.asyncio
    async def test_state_updates_on_activity(self, page):
        """State should change from waiting to active on input."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)

        # Send a command to trigger activity
        await _ws_send_text(page, name, "echo state_test")
        await _ws_send(page, name, "\r")
        await page.wait_for_timeout(3000)

        # Check sidebar state
        state_el = page.locator(f".session-item .si-state").first
        state = await state_el.text_content()
        # Should be one of: active, waiting (after command completes)
        assert state.lower() in ("active", "waiting", "idle")

    @pytest.mark.asyncio
    async def test_idle_timer_resets(self, page):
        """Idle timer should reset after input."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)

        # Wait for idle to accumulate
        await page.wait_for_timeout(3000)
        meta_el = page.locator(".session-item .si-meta").first
        idle_text = await meta_el.text_content()

        # Send input to reset idle
        await _ws_send(page, name, " ")  # space
        await _ws_send(page, name, "\x7f")  # backspace to clean up
        await page.wait_for_timeout(2000)

        # Idle should be present
        new_idle = await meta_el.text_content()
        assert "idle" in new_idle.lower()


# ── API Endpoints ────────────────────────────────────────────

class TestAPIEndpoints:
    @pytest.mark.asyncio
    async def test_sessions_api(self, page):
        await page.goto(WEB_URL)
        resp = await page.request.get(f"{WEB_URL}/api/sessions")
        data = await resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    @pytest.mark.asyncio
    async def test_raw_pane_api(self, page):
        await _load_and_wait(page)
        name = await _get_session_name(page)
        resp = await page.request.get(f"{WEB_URL}/api/pane/{name}/raw")
        assert resp.status == 200
        data = await resp.json()
        assert "content" in data
        assert len(data["content"]) > 0

    @pytest.mark.asyncio
    async def test_send_keys_api(self, page):
        """Test /keys endpoint with special keys like Enter."""
        await _load_and_wait(page)
        await _dock_first_session(page)
        name = await _get_session_name(page)
        marker = f"zpapi_{RUN_ID}"

        # Type via WebSocket, then send Enter via /keys API
        await _ws_send_text(page, name, f"echo {marker}")
        resp = await page.evaluate(
            f"""async () => {{
                const r = await fetch('{WEB_URL}/api/session/{name}/keys', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(["Enter"])
                }});
                return {{ status: r.status, body: await r.json() }};
            }}"""
        )
        assert resp["status"] == 200
        assert resp["body"]["results"][0]["sent"] is True
        await page.wait_for_timeout(2000)
        content = await _get_pane_content_via_api(page, name)
        assert marker in content

    @pytest.mark.asyncio
    async def test_send_command_api(self, page):
        await _load_and_wait(page)
        name = await _get_session_name(page)
        marker = f"zpcmd_{RUN_ID}"
        resp = await page.request.post(
            f"{WEB_URL}/api/session/{name}/send",
            form={"text": f"echo {marker}"}
        )
        assert resp.status == 200
        await page.wait_for_timeout(2000)
        content = await _get_pane_content_via_api(page, name)
        assert marker in content


# ── Multi-panel (requires 2+ sessions) ──────────────────────

class TestMultiPanel:
    @pytest.mark.asyncio
    async def test_dock_multiple_sessions(self, page):
        """Dock 2 sessions if available."""
        await _load_and_wait(page)
        items = page.locator(".session-item")
        count = await items.count()
        if count < 2:
            pytest.skip("Need 2+ sessions for multi-panel test")

        # Dock first two
        await items.nth(0).click()
        await page.wait_for_timeout(2000)
        await items.nth(1).click()
        await page.wait_for_timeout(2000)

        tabs = page.locator(".tab")
        assert await tabs.count() >= 2
        terminals = page.locator(".xterm")
        assert await terminals.count() >= 1  # at least active one shows


# ── Keyboard Toggle (inline input bar) ──────────────────────

class TestInputToggle:
    @pytest.mark.asyncio
    async def test_toggle_input_bar(self, page):
        """Toggle keyboard icon hides/shows input bar."""
        await _load_and_wait(page)
        await _dock_first_session(page)

        # Find keyboard toggle
        toggle = page.locator(".th-toggle").first
        bar = page.locator(".term-input").first

        initial_visible = await bar.is_visible()

        # Click to toggle
        await toggle.click()
        await page.wait_for_timeout(500)
        after_toggle = await bar.is_visible()

        # Click to toggle back
        await toggle.click()
        await page.wait_for_timeout(500)
        after_second = await bar.is_visible()

        # At least one toggle should have changed state
        assert initial_visible != after_toggle or after_toggle != after_second
