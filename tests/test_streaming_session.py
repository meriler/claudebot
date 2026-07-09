"""Tests for StreamingSession against a fake stream-json `claude`.

Verifies engine mechanics only (delivery, dispatch, turn resolution, mid-turn
inject, interrupt, process death) — not model behavior, which is verified
empirically against the real CLI.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from telegram_bot.core.services.streaming_session import (
    StreamingProcessDeadError,
    StreamingSession,
)

_FAKE = str(Path(__file__).parent / "fakes" / "fake_claude_stream.py")


def _session() -> StreamingSession:
    return StreamingSession([sys.executable, _FAKE])


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Poll until predicate() is truthy or timeout (avoids racy fixed sleeps)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_send_returns_result_and_streams_events() -> None:
    s = _session()
    events: list[dict] = []
    try:
        result = await asyncio.wait_for(s.send("PING", events.append), timeout=3.0)
        assert result == "RESULT:PING"
        types = [e.get("type") for e in events]
        assert "assistant" in types
        assert "result" in types
    finally:
        await s.close()


async def test_inject_resolves_held_turn() -> None:
    """A turn left open (HOLD) resolves once a mid-turn message is injected —
    models steering: the injected message folds into the active turn."""
    s = _session()
    events: list[dict] = []

    def on_event(e: dict) -> None:
        events.append(e)

    try:
        send_task = asyncio.create_task(s.send("HOLD-1", on_event))
        # Turn started once the fake acked the HOLD message.
        await _wait_for(
            lambda: any(
                e.get("type") == "assistant" and "ACK:HOLD-1" in e["message"]["content"][0]["text"]
                for e in events
            )
        )
        assert not send_task.done()  # no result yet — turn is open

        await s.inject("GO")
        result = await asyncio.wait_for(send_task, timeout=3.0)
        assert result == "RESULT:GO"

        texts = [e["message"]["content"][0]["text"] for e in events if e.get("type") == "assistant"]
        assert texts == ["ACK:HOLD-1", "ACK:GO"]  # both seen, in order
    finally:
        await s.close()


async def test_interrupt_ends_open_turn() -> None:
    s = _session()
    events: list[dict] = []
    try:
        send_task = asyncio.create_task(s.send("HOLD-2", events.append))
        await _wait_for(lambda: any(e.get("type") == "assistant" for e in events))
        assert not send_task.done()

        await s.interrupt()
        result = await asyncio.wait_for(send_task, timeout=3.0)
        assert result == "INTERRUPTED"
    finally:
        await s.close()


async def test_process_death_fails_pending_turn() -> None:
    s = _session()
    events: list[dict] = []
    send_task = asyncio.create_task(s.send("HOLD-3", events.append))
    await _wait_for(lambda: any(e.get("type") == "assistant" for e in events))

    await s.close()  # kills the process mid-turn
    with pytest.raises(StreamingProcessDeadError):
        await asyncio.wait_for(send_task, timeout=3.0)


async def test_ensure_started_idempotent() -> None:
    s = _session()
    try:
        await s.ensure_started()
        pid1 = s._process.pid  # type: ignore[union-attr]
        await s.ensure_started()
        pid2 = s._process.pid  # type: ignore[union-attr]
        assert pid1 == pid2
        assert s.is_alive
    finally:
        await s.close()
    assert not s.is_alive


async def test_inactivity_watchdog_kills_stuck_turn() -> None:
    """A turn that goes silent (HOLD acks then waits) past the inactivity
    deadline is killed, so send() fails instead of hanging forever."""
    s = StreamingSession([sys.executable, _FAKE], inactivity_kill_sec=0.3, poll_sec=0.1)
    try:
        with pytest.raises(StreamingProcessDeadError):
            await asyncio.wait_for(s.send("HOLD-stuck", lambda e: None), timeout=5.0)
        assert not s.is_alive  # watchdog killed the process
    finally:
        await s.close()


async def test_send_rejects_concurrent_turn() -> None:
    s = _session()
    events: list[dict] = []
    try:
        send_task = asyncio.create_task(s.send("HOLD-4", events.append))
        await _wait_for(lambda: s.is_turn_active)
        with pytest.raises(RuntimeError):
            await s.send("SECOND", events.append)
        await s.interrupt()
        await asyncio.wait_for(send_task, timeout=3.0)
    finally:
        await s.close()


async def test_tail_events_after_early_result_still_dispatched() -> None:
    """Background-task pattern (CLI 2.1.x): the process emits an early empty
    `result` (turn over for the client) and KEEPS working. Post-turn events
    must still reach the last turn's callback — dropping them is the
    silent-bot-while-transcript-grows bug (2026-07-07)."""
    s = _session()
    events: list[dict] = []
    try:
        result = await asyncio.wait_for(s.send("TAIL-1", events.append), timeout=3.0)
        assert result == ""  # the early result ended the visible turn
        assert not s.is_turn_active
        # The tail (assistant text emitted AFTER the result) still arrives.
        await _wait_for(
            lambda: any(
                e.get("type") == "assistant" and "TAILTEXT:" in e["message"]["content"][0]["text"]
                for e in events
            )
        )
        # The tail's own `result` resolves nothing (no pending future) and
        # must not crash the reader loop.
        await _wait_for(lambda: sum(1 for e in events if e.get("type") == "result") >= 2)
        assert s.is_alive
    finally:
        await s.close()


async def test_close_clears_tail_callback() -> None:
    """After teardown the retained callback is dropped (no dangling refs)."""
    s = _session()
    events: list[dict] = []
    try:
        await asyncio.wait_for(s.send("PING", events.append), timeout=3.0)
        assert s._on_event is not None  # retained for the tail
    finally:
        await s.close()
    assert s._on_event is None
