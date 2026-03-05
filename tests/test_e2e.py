"""End-to-end test for zpilot."""

import asyncio
import sys
sys.path.insert(0, "src")

from zpilot import zellij
from zpilot.detector import PaneDetector
from zpilot.events import EventBus
from zpilot.models import Event
from zpilot.config import load_config


async def main():
    # 1. Check zellij available
    ok = await zellij.is_available()
    print(f"1. Zellij available: {ok}")
    assert ok, "Zellij not found"

    # 2. List sessions (should be empty or existing)
    sessions = await zellij.list_sessions()
    print(f"2. Sessions before: {[s.name for s in sessions]}")

    # 3. Create test session
    await zellij.new_session("zptest-1")
    await asyncio.sleep(2)

    sessions = await zellij.list_sessions()
    names = [s.name for s in sessions]
    print(f"3. Session created: {names}")
    assert "zptest-1" in names, f"zptest-1 not found in {names}"

    # 4. Create a named pane with logging (this is the key feature)
    log_file = await zellij.new_pane(
        session="zptest-1", name="worker", floating=True
    )
    print(f"4. Created pane 'worker', log: {log_file}")
    await asyncio.sleep(2)

    # 5. Send a command to the session's pane via write-chars
    await zellij.write_to_pane("echo ZPILOT_MARKER_12345", session="zptest-1")
    await zellij.send_enter(session="zptest-1")
    await asyncio.sleep(2)

    # 6. Read pane content from log file
    content = await zellij.dump_pane(session="zptest-1", pane_name="worker")
    print(f"5-6. Pane content length: {len(content)} chars")
    has_marker = "ZPILOT_MARKER_12345" in content
    print(f"     Marker found: {has_marker}")
    if content:
        print(f"     Last 3 lines: {content.strip().splitlines()[-3:]}")

    # 7. Test run_in_session (headless command execution)
    result = await zellij.run_in_session(
        "echo HEADLESS_RUN_OK", session="zptest-1", capture=True
    )
    print(f"7. Headless run result: {repr(result.strip())}")
    assert "HEADLESS_RUN_OK" in result, f"Headless run failed: {result}"

    # 8. Test detector
    config = load_config()
    detector = PaneDetector(config)
    state = detector.detect("zptest-1", "worker", content)
    print(f"8. Detected state: {state.value}")

    # 9. Test event bus
    bus = EventBus()
    bus.clear()
    bus.emit(Event(event_type="test", session="zptest-1", new_state="active", details="e2e"))
    bus.emit(Event(event_type="state_change", session="zptest-1",
                   old_state="unknown", new_state="idle"))
    events = bus.recent(5)
    print(f"9. Events written/read: {len(events)}")
    assert len(events) == 2, f"Expected 2 events, got {len(events)}"

    # 10. Test notification adapter
    from zpilot.notifications import create_adapter
    adapter = create_adapter(config)
    notified = await adapter.send("zpilot e2e", "Test passed!", "default")
    print(f"10. Notification sent: {notified}")

    # 11. Cleanup
    await zellij._run(["delete-session", "zptest-1", "--force"], check=False)
    await asyncio.sleep(0.5)
    sessions = await zellij.list_sessions()
    remaining = [s.name for s in sessions if s.name.startswith("zptest-")]
    print(f"11. Cleanup done, remaining: {remaining}")

    print("\n=== ALL E2E TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
