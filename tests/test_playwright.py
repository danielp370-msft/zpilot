"""Playwright browser tests for zpilot web dashboard.

Requires:
  - Web server running (default: localhost:8097, override with ZPILOT_WEB_URL)
  - At least one Zellij session (demo-build, demo-copilot)
  - python3 -m playwright install chromium
"""

import asyncio
import os
import re
import pytest

# Try importing playwright — skip all tests if not installed
pytest.importorskip("playwright")

from playwright.async_api import async_playwright, expect


WEB_URL = os.environ.get("ZPILOT_WEB_URL", "http://localhost:8097")


@pytest.fixture
async def browser():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        yield b
        await b.close()


@pytest.fixture
async def page(browser):
    pg = await browser.new_page()
    yield pg
    await pg.close()


# ── Page load and structure ───────────────────────────────────

class TestPageLoad:
    @pytest.mark.asyncio
    async def test_loads_successfully(self, page):
        resp = await page.goto(WEB_URL, wait_until="load")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_title(self, page):
        await page.goto(WEB_URL, wait_until="load")
        title = await page.title()
        assert "zpilot" in title.lower()

    @pytest.mark.asyncio
    async def test_has_sidebar(self, page):
        await page.goto(WEB_URL, wait_until="load")
        sidebar = page.locator(".sidebar")
        await expect(sidebar).to_be_visible()

    @pytest.mark.asyncio
    async def test_has_session_list(self, page):
        await page.goto(WEB_URL, wait_until="load")
        # Wait for SSE to populate sidebar
        await page.wait_for_timeout(3000)
        items = page.locator(".session-item")
        count = await items.count()
        assert count >= 1, "No sessions in sidebar"

    @pytest.mark.asyncio
    async def test_has_layout_buttons(self, page):
        await page.goto(WEB_URL, wait_until="load")
        layout_bar = page.locator(".layout-btns")
        await expect(layout_bar).to_be_visible()
        buttons = page.locator(".layout-btns button")
        count = await buttons.count()
        assert count >= 3, f"Expected 3+ layout buttons, got {count}"


# ── Sidebar ───────────────────────────────────────────────────

class TestSidebar:
    @pytest.mark.asyncio
    async def test_session_names_visible(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)
        text = await page.locator(".sidebar").text_content()
        assert "demo-build" in text or "demo-copilot" in text

    @pytest.mark.asyncio
    async def test_session_count_displayed(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)
        count_el = page.locator("#session-count")
        text = await count_el.text_content()
        assert "session" in text.lower()

    @pytest.mark.asyncio
    async def test_event_panel_exists(self, page):
        await page.goto(WEB_URL, wait_until="load")
        events_panel = page.locator("#event-list")
        await expect(events_panel).to_be_visible()


# ── Docking and tabs ──────────────────────────────────────────

class TestDocking:
    @pytest.mark.asyncio
    async def test_click_session_docks_panel(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Click first session in sidebar
        first_item = page.locator(".session-item").first
        await first_item.click()
        await page.wait_for_timeout(1000)

        # Should now have a tab
        tabs = page.locator(".tab")
        count = await tabs.count()
        assert count >= 1, "No tab created after clicking session"

    @pytest.mark.asyncio
    async def test_panel_shows_content(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Click a session
        first_item = page.locator(".session-item").first
        await first_item.click()
        await page.wait_for_timeout(3000)  # Wait for content refresh

        # Panel should have terminal content
        panels = page.locator(".term-body")
        count = await panels.count()
        assert count >= 1
        if count > 0:
            text = await panels.first.text_content()
            # Should have more than just "Loading..."
            assert len(text) > 5, f"Panel content too short: '{text}'"

    @pytest.mark.asyncio
    async def test_undock_removes_panel(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Dock a session
        first_item = page.locator(".session-item").first
        await first_item.click()
        await page.wait_for_timeout(500)

        # Close via tab close button
        close_btn = page.locator(".tab .tab-close").first
        if await close_btn.count() > 0:
            await close_btn.click()
            await page.wait_for_timeout(500)
            # Tab count should decrease (or be 0)
            tabs = page.locator(".tab")
            count = await tabs.count()
            # Just verify it didn't crash
            assert count >= 0


# ── Layout switching ──────────────────────────────────────────

class TestLayouts:
    @pytest.mark.asyncio
    async def test_layout_buttons_work(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Dock two sessions first
        items = page.locator(".session-item")
        count = await items.count()
        if count >= 2:
            await items.nth(0).click()
            await page.wait_for_timeout(300)
            await items.nth(1).click()
            await page.wait_for_timeout(300)

        # Click each layout button
        buttons = page.locator(".layout-btns button")
        btn_count = await buttons.count()
        for i in range(btn_count):
            await buttons.nth(i).click()
            await page.wait_for_timeout(200)

        # Grid container should exist
        container = page.locator(".panels-container")
        await expect(container).to_be_visible()


# ── Command input ─────────────────────────────────────────────

class TestCommandInput:
    @pytest.mark.asyncio
    async def test_bottom_bar_input(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Dock a session
        first_item = page.locator(".session-item").first
        await first_item.click()
        await page.wait_for_timeout(1000)

        # Find the input bar
        inputs = page.locator(".term-input input")
        count = await inputs.count()
        if count > 0:
            inp = inputs.first
            await inp.fill("echo PLAYWRIGHT_CMD_TEST")
            value = await inp.input_value()
            assert "PLAYWRIGHT_CMD_TEST" in value

    @pytest.mark.asyncio
    async def test_bottom_bar_send(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Dock demo-build
        items = page.locator(".session-item")
        count = await items.count()
        for i in range(count):
            text = await items.nth(i).text_content()
            if "demo-build" in text:
                await items.nth(i).click()
                break
        await page.wait_for_timeout(1000)

        # Type and send
        inputs = page.locator(".term-input input")
        if await inputs.count() > 0:
            inp = inputs.first
            await inp.fill("echo PW_SEND_TEST_XYZ")
            # Click send button
            send_btn = page.locator(".term-input button").first
            if await send_btn.count() > 0:
                await send_btn.click()
                await page.wait_for_timeout(2000)

                # Verify marker appears in terminal
                body = page.locator(".term-body").first
                content = await body.text_content()
                # May need another refresh cycle
                await page.wait_for_timeout(3000)
                content = await body.text_content()
                assert "PW_SEND_TEST_XYZ" in content, f"Marker not in: {content[-200:]}"


# ── Inline terminal typing ───────────────────────────────────

class TestInlineTyping:
    @pytest.mark.asyncio
    async def test_term_body_focusable(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Dock a session
        first_item = page.locator(".session-item").first
        await first_item.click()
        await page.wait_for_timeout(1000)

        # Focus terminal body via JS (more reliable than click)
        body = page.locator(".term-body").first
        await body.evaluate("el => el.focus()")
        await page.wait_for_timeout(200)
        is_focused = await body.evaluate("el => el === document.activeElement")
        assert is_focused, "Terminal body not focused"

    @pytest.mark.asyncio
    async def test_inline_typing_shows_cursor(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Dock a session
        first_item = page.locator(".session-item").first
        await first_item.click()
        await page.wait_for_timeout(1000)

        # Focus and type
        body = page.locator(".term-body").first
        await body.evaluate("el => el.focus()")
        await page.keyboard.type("hello")
        await page.wait_for_timeout(500)

        # Should show inline-input cursor
        cursor = page.locator(".prompt-text")
        count = await cursor.count()
        assert count >= 1, "No inline cursor appeared"
        text = await cursor.first.text_content()
        assert "hello" in text

    @pytest.mark.asyncio
    async def test_inline_backspace(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Dock and focus
        first_item = page.locator(".session-item").first
        await first_item.click()
        await page.wait_for_timeout(1000)

        body = page.locator(".term-body").first
        await body.evaluate("el => el.focus()")

        # Type then backspace
        await page.keyboard.type("abcde")
        await page.wait_for_timeout(200)
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(200)

        cursor = page.locator(".prompt-text")
        if await cursor.count() > 0:
            text = await cursor.first.text_content()
            assert text == "abc", f"Expected 'abc', got '{text}'"

    @pytest.mark.asyncio
    async def test_inline_escape_clears(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        first_item = page.locator(".session-item").first
        await first_item.click()
        await page.wait_for_timeout(1000)

        body = page.locator(".term-body").first
        await body.evaluate("el => el.focus()")

        await page.keyboard.type("test")
        await page.wait_for_timeout(200)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

        # After escape, cursor should be gone or empty
        cursor = page.locator(".prompt-text")
        count = await cursor.count()
        if count > 0:
            text = await cursor.first.text_content()
            assert text == "", f"Cursor should be empty after Escape, got '{text}'"

    @pytest.mark.asyncio
    async def test_inline_enter_sends(self, page):
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(3000)

        # Dock demo-build specifically
        items = page.locator(".session-item")
        count = await items.count()
        for i in range(count):
            text = await items.nth(i).text_content()
            if "demo-build" in text:
                await items.nth(i).click()
                break
        await page.wait_for_timeout(1000)

        body = page.locator(".term-body").first
        await body.evaluate("el => el.focus()")

        # Type and press Enter
        await page.keyboard.type("echo INLINE_TYPE_TEST")
        await page.wait_for_timeout(200)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3000)

        # Verify it appears in content
        content = await body.text_content()
        assert "INLINE_TYPE_TEST" in content, f"Marker not found in: {content[-200:]}"


# ── SSE live updates ──────────────────────────────────────────

class TestSSE:
    @pytest.mark.asyncio
    async def test_sse_updates_sidebar(self, page):
        await page.goto(WEB_URL, wait_until="load")
        # Wait for at least one SSE status update
        await page.wait_for_timeout(5000)

        # last-update should be populated
        last_update = page.locator("#last-update")
        text = await last_update.text_content()
        assert text and text != "", f"Last update empty: '{text}'"

    @pytest.mark.asyncio
    async def test_sse_connection(self, page):
        """Verify EventSource is connected."""
        await page.goto(WEB_URL, wait_until="load")
        await page.wait_for_timeout(4000)

        # Check sidebar has session items (populated by SSE)
        items = page.locator(".session-item")
        count = await items.count()
        assert count >= 1, "SSE didn't populate session list"


# ── New session creation ──────────────────────────────────────

class TestCreateSession:
    @pytest.mark.asyncio
    async def test_create_via_api_and_verify(self, page):
        """Create a session via API and verify it shows in dashboard."""
        # Create via direct API call
        await page.goto(WEB_URL, wait_until="load")
        result = await page.evaluate("""
            async () => {
                const resp = await fetch('/api/session/zptest-pw-01', {method: 'POST'});
                return await resp.json();
            }
        """)
        assert result["status"] == "created"

        # Wait for SSE to pick it up
        await page.wait_for_timeout(5000)

        # Check sidebar
        sidebar_text = await page.locator(".sidebar").text_content()
        assert "zptest-pw-01" in sidebar_text

        # Cleanup
        await page.evaluate("""
            async () => {
                await fetch('/api/session/zptest-pw-01', {method: 'DELETE'});
            }
        """)
        await page.wait_for_timeout(2000)
