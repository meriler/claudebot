"""Tests for StreamingManager against the fake stream-json `claude`.

build_streaming_argv is monkeypatched to launch the fake, so no real CLI is
spawned. Verifies event bridging (parse_cc_event -> StreamEvent), session_id
persistence, mid-turn inject, interrupt (cancel), and kill.
"""

import asyncio
import sys
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.streaming_manager import StreamingManager

_FAKE = str(Path(__file__).parent / "fakes" / "fake_claude_stream.py")
_KEY = (123, 7)


def _managers(tmp_path: Path, **kwargs) -> tuple[SessionManager, StreamingManager]:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        session_mapping_path=str(tmp_path / "session_mapping.json"),
    )
    sm = SessionManager(settings)
    # Spawn the fake instead of the real `claude`, and avoid a real cwd.
    sm.build_streaming_argv = lambda *a, **k: [sys.executable, _FAKE]  # type: ignore[method-assign]
    sm._get_session(_KEY).cwd = ""
    return sm, StreamingManager(sm, settings, **kwargs)


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_send_stream_bridges_events_and_persists_sid(tmp_path: Path) -> None:
    sm, mgr = _managers(tmp_path)
    events: list[StreamEvent] = []
    try:
        result = await asyncio.wait_for(mgr.send_stream(_KEY, "PING", events.append), timeout=5.0)
        # Fresh session: the first message is prefixed with mode prompt + tg
        # context, so the fake echoes RESULT:<prefix...PING>.
        assert result.startswith("RESULT:")
        assert result.endswith("PING")
        # The fake echoes the (prefixed) first message as an ACK text event.
        text_events = [e for e in events if e.type == "text"]
        assert any("PING" in e.content for e in text_events)
        # No `result` StreamEvent leaks to the chat callback (it's the return).
        assert all(e.type != "result" for e in events)
        # session_id captured and persisted for resume.
        assert sm.get_current_session_id(_KEY) == "fake-sid-1"
    finally:
        await mgr.shutdown()


async def test_inject_steers_open_turn(tmp_path: Path) -> None:
    _sm, mgr = _managers(tmp_path)
    events: list[StreamEvent] = []
    try:
        task = asyncio.create_task(mgr.send_stream(_KEY, "HOLD-A", events.append))
        await _wait_for(lambda: mgr.is_busy(_KEY))
        assert not task.done()

        injected = await mgr.inject(_KEY, "GO")
        assert injected is True
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result == "RESULT:GO"
    finally:
        await mgr.shutdown()


async def test_cancel_interrupts_without_killing(tmp_path: Path) -> None:
    _sm, mgr = _managers(tmp_path)
    events: list[StreamEvent] = []
    try:
        task = asyncio.create_task(mgr.send_stream(_KEY, "HOLD-B", events.append))
        await _wait_for(lambda: mgr.is_busy(_KEY))

        cancelled = await mgr.cancel(_KEY)
        assert cancelled is True
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result == "INTERRUPTED"
        # Process survived the interrupt (cancel != kill).
        assert mgr.is_active(_KEY)
    finally:
        await mgr.shutdown()


async def test_inject_returns_false_when_idle(tmp_path: Path) -> None:
    _sm, mgr = _managers(tmp_path)
    try:
        # No turn ever started.
        assert await mgr.inject(_KEY, "nope") is False
    finally:
        await mgr.shutdown()


async def test_idle_reaper_kills_idle_session(tmp_path: Path) -> None:
    _sm, mgr = _managers(tmp_path, idle_timeout_sec=0.0)
    events: list[StreamEvent] = []
    try:
        await asyncio.wait_for(mgr.send_stream(_KEY, "PING", events.append), timeout=5.0)
        assert mgr.is_active(_KEY)
        killed = await mgr._reap_idle_once()
        assert _KEY in killed
        assert not mgr.is_active(_KEY)
    finally:
        await mgr.shutdown()


async def test_idle_reaper_skips_busy_session(tmp_path: Path) -> None:
    _sm, mgr = _managers(tmp_path, idle_timeout_sec=0.0)
    events: list[StreamEvent] = []
    try:
        task = asyncio.create_task(mgr.send_stream(_KEY, "HOLD-idle", events.append))
        await _wait_for(lambda: mgr.is_busy(_KEY))
        killed = await mgr._reap_idle_once()
        assert _KEY not in killed  # a live turn is never reaped
        assert mgr.is_active(_KEY)
        await mgr.cancel(_KEY)
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        await mgr.shutdown()


async def test_idle_reaper_keeps_recent_session(tmp_path: Path) -> None:
    _sm, mgr = _managers(tmp_path, idle_timeout_sec=300.0)
    events: list[StreamEvent] = []
    try:
        await asyncio.wait_for(mgr.send_stream(_KEY, "PING", events.append), timeout=5.0)
        killed = await mgr._reap_idle_once()
        assert killed == []
        assert mgr.is_active(_KEY)
    finally:
        await mgr.shutdown()


async def test_reply_to_resume_respawns_on_sid_switch(tmp_path: Path) -> None:
    """If the channel's session_id is overridden (reply-to-resume) while a live
    process runs a different session, _ensure_session kills + respawns it."""
    sm, mgr = _managers(tmp_path)
    events: list[StreamEvent] = []
    try:
        await asyncio.wait_for(mgr.send_stream(_KEY, "PING", events.append), timeout=5.0)
        s1 = mgr._sessions[_KEY]
        assert mgr._live_sid[_KEY] == "fake-sid-1"

        # Simulate reply-to-resume: process_queue_item override_session(target).
        sm._get_session(_KEY).session_id = "OTHER-SID"
        s2 = await mgr._ensure_session(_KEY)

        assert s2 is not s1  # respawned, not reused
        assert mgr._live_sid[_KEY] == "OTHER-SID"  # now on the resumed target
        assert mgr.is_active(_KEY)
    finally:
        await mgr.shutdown()


async def test_capacity_cap_evicts_lru_idle(tmp_path: Path) -> None:
    sm, mgr = _managers(tmp_path, max_concurrent=2)
    k_a, k_b, k_c = (10, 1), (10, 2), (10, 3)
    for k in (k_a, k_b, k_c):
        sm._get_session(k).cwd = ""
    import time as _time

    try:
        await asyncio.wait_for(mgr.send_stream(k_a, "PING", lambda e: None), timeout=5.0)
        await asyncio.wait_for(mgr.send_stream(k_b, "PING", lambda e: None), timeout=5.0)
        assert mgr.is_active(k_a) and mgr.is_active(k_b)

        # Eviction now requires stdout silence too (active tails are protected).
        # Age both sessions' stdout clocks past the grace bar so they're evictable.
        for k in (k_a, k_b):
            mgr._sessions[k].last_stream_event = _time.monotonic() - 9_999

        # k_c exceeds cap=2 -> evict the least-recently-used idle (k_a).
        await asyncio.wait_for(mgr.send_stream(k_c, "PING", lambda e: None), timeout=5.0)
        assert not mgr.is_active(k_a)  # evicted
        assert mgr.is_active(k_b)
        assert mgr.is_active(k_c)
    finally:
        await mgr.shutdown()


async def test_capacity_cap_spares_active_tail(tmp_path: Path) -> None:
    """A non-busy session whose stdout is still fresh (background tail) is NOT
    evicted even over the cap — mirrors the reaper's both-clocks protection."""
    import time as _time

    sm, mgr = _managers(tmp_path, max_concurrent=1)
    k_a, k_b = (11, 1), (11, 2)
    for k in (k_a, k_b):
        sm._get_session(k).cwd = ""
    try:
        await asyncio.wait_for(mgr.send_stream(k_a, "PING", lambda e: None), timeout=5.0)
        # k_a's turn is done but its stdout is fresh (simulated tail) -> spared.
        mgr._sessions[k_a].last_stream_event = _time.monotonic()
        await asyncio.wait_for(mgr.send_stream(k_b, "PING", lambda e: None), timeout=5.0)
        assert mgr.is_active(k_a)  # tail protected, cap briefly exceeded
        assert mgr.is_active(k_b)
    finally:
        await mgr.shutdown()


async def test_kill_terminates_session(tmp_path: Path) -> None:
    _sm, mgr = _managers(tmp_path)
    events: list[StreamEvent] = []
    try:
        await asyncio.wait_for(mgr.send_stream(_KEY, "PING", events.append), timeout=5.0)
        assert mgr.is_active(_KEY)
        await mgr.kill(_KEY)
        assert not mgr.is_active(_KEY)
    finally:
        await mgr.shutdown()


async def test_idle_reaper_skips_stdout_active_session(tmp_path: Path) -> None:
    """Background work keeps the CLI's stdout alive while the bot-side turn is
    long over. The reaper must respect stdout recency — killing here is the
    background-work-killed-every-15-min bug (2026-07-07). Reap only when BOTH
    clocks are stale (second half of the test)."""
    import time as _time

    _sm, mgr = _managers(tmp_path, idle_timeout_sec=300.0)
    events: list[StreamEvent] = []
    try:
        await asyncio.wait_for(mgr.send_stream(_KEY, "PING", events.append), timeout=5.0)
        session = mgr._sessions[_KEY]

        # Bot-side clock stale, stdout fresh -> NOT reaped.
        mgr._last_activity[_KEY] = _time.monotonic() - 9_999
        session.last_stream_event = _time.monotonic()
        assert await mgr._reap_idle_once() == []
        assert mgr.is_active(_KEY)

        # Both clocks stale -> reaped (hung/dead-SSE recovery path).
        session.last_stream_event = _time.monotonic() - 9_999
        killed = await mgr._reap_idle_once()
        assert _KEY in killed
        assert not mgr.is_active(_KEY)
    finally:
        await mgr.shutdown()


async def test_tail_events_bridge_after_early_result(tmp_path: Path) -> None:
    """After an early result (background-task pattern) tail events keep
    flowing to the chat callback: text for batching AND the cycle-ending
    `result` (mid-turn results stay filtered — see the PING test above)."""
    _sm, mgr = _managers(tmp_path)
    events: list[StreamEvent] = []
    try:
        result = await asyncio.wait_for(mgr.send_stream(_KEY, "TAIL-x", events.append), timeout=5.0)
        assert result == ""  # the early result ended the visible turn
        await _wait_for(lambda: any(e.type == "text" and "TAILTEXT:" in e.content for e in events))
        await _wait_for(
            lambda: any(e.type == "result" and "TAIL-DONE" in e.content for e in events)
        )
    finally:
        await mgr.shutdown()


class _FakeBuffer:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_tail_buffer_lifecycle(tmp_path: Path) -> None:
    """Adopted tail buffers close on replacement, explicit close, and kill."""
    _sm, mgr = _managers(tmp_path)
    try:
        b1, b2 = _FakeBuffer(), _FakeBuffer()
        await mgr.adopt_tail_buffer(_KEY, b1)
        await mgr.adopt_tail_buffer(_KEY, b2)  # replaces -> closes b1
        assert b1.closed and not b2.closed
        await mgr.close_tail_buffer(_KEY)
        assert b2.closed
        await mgr.close_tail_buffer(_KEY)  # idempotent on empty

        b3 = _FakeBuffer()
        await mgr.adopt_tail_buffer(_KEY, b3)
        await mgr.kill(_KEY)  # kill closes the adopted buffer too
        assert b3.closed
    finally:
        await mgr.shutdown()
