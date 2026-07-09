"""Tests for the persistent outbox (undelivered final responses)."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_bot.core.services.outbox import Outbox
from telegram_bot.core.services.telegram_utils import SendOutcome


def _outbox(tmp_path: Path) -> Outbox:
    return Outbox(MagicMock(), tmp_path / "outbox.json")


async def test_enqueue_persists_and_survives_reload(tmp_path: Path) -> None:
    box = _outbox(tmp_path)
    box.enqueue(123, 7, "hello")
    await box.shutdown()

    reloaded = _outbox(tmp_path)
    assert reloaded.size == 1
    assert reloaded.has_pending(123, 7)
    assert not reloaded.has_pending(123, 8)


async def test_delivery_removes_entry(tmp_path: Path) -> None:
    box = _outbox(tmp_path)
    ok = SendOutcome(message_id=1, fatal=False)
    with patch(
        "telegram_bot.core.services.outbox.send_html_with_fallback",
        AsyncMock(return_value=ok),
    ):
        box.enqueue(123, None, "hello")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if box.size == 0:
                break
    assert box.size == 0
    assert json.loads((tmp_path / "outbox.json").read_text()) == []


async def test_transient_failure_keeps_entry_and_chunk_position(tmp_path: Path) -> None:
    """A transient (non-fatal) failure must keep the entry for retry."""
    box = _outbox(tmp_path)
    transient = SendOutcome(message_id=None, fatal=False)
    with patch(
        "telegram_bot.core.services.outbox.send_html_with_fallback",
        AsyncMock(return_value=transient),
    ):
        box.enqueue(123, None, "hello")
        await asyncio.sleep(0.05)
    await box.shutdown()
    assert box.size == 1
    entry = json.loads((tmp_path / "outbox.json").read_text())[0]
    assert entry["attempts"] >= 1
    assert entry["next_chunk"] == 0


async def test_fatal_head_dropped_unblocks_queue(tmp_path: Path) -> None:
    """A fatal (bot-blocked) head must be dropped so entries behind it deliver.

    Head-of-line fix (audit 2026-07-02): chat A is blocked (fatal), chat B must
    still receive its message instead of being stuck forever behind A."""
    box = _outbox(tmp_path)
    delivered: list[int] = []

    async def fake_send(*, send_html, send_plain, label):  # type: ignore[no-untyped-def]
        # send_html closes over the entry's chat_id; invoke it to learn which
        # chat this is. Chat 111 (blocked) → fatal; chat 222 → delivered.
        sent = MagicMock()
        box._bot.send_message = AsyncMock(return_value=sent)
        await send_html()
        chat_id = box._bot.send_message.await_args.args[0]
        if chat_id == 111:
            return SendOutcome(message_id=None, fatal=True)
        delivered.append(chat_id)
        return SendOutcome(message_id=1, fatal=False)

    with patch("telegram_bot.core.services.outbox.send_html_with_fallback", fake_send):
        box.enqueue(111, None, "to blocked chat")  # head — fatal
        box.enqueue(222, None, "to good chat")  # behind it — must still arrive
        for _ in range(200):
            await asyncio.sleep(0.01)
            if box.size == 0:
                break

    assert box.size == 0  # both cleared: 111 dropped, 222 delivered
    assert delivered == [222]  # the good chat got its message


async def test_corrupt_entry_without_chat_id_dropped(tmp_path: Path) -> None:
    """An entry with no chat_id can never send — it must be dropped, not loop."""
    box = _outbox(tmp_path)
    # Inject a corrupt entry directly (bypassing enqueue, which always sets chat_id).
    box._entries.append(
        {"id": "corrupt", "chat_id": None, "thread_id": None, "text": "x", "next_chunk": 0}
    )
    with patch(
        "telegram_bot.core.services.outbox.send_html_with_fallback",
        AsyncMock(side_effect=AssertionError("must not attempt send for corrupt entry")),
    ):
        box.start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if box.size == 0:
                break
    assert box.size == 0  # dropped, not stuck


async def test_max_attempts_drops_head(tmp_path: Path, monkeypatch) -> None:
    """After _MAX_ATTEMPTS transient failures the head is dropped to unblock."""
    monkeypatch.setattr("telegram_bot.core.services.outbox._MAX_ATTEMPTS", 2)
    monkeypatch.setattr("telegram_bot.core.services.outbox._BASE_BACKOFF_SEC", 0.0)
    box = _outbox(tmp_path)
    transient = SendOutcome(message_id=None, fatal=False)
    with patch(
        "telegram_bot.core.services.outbox.send_html_with_fallback",
        AsyncMock(return_value=transient),
    ):
        box.enqueue(123, None, "never delivers")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if box.size == 0:
                break
    assert box.size == 0  # given up after 2 attempts, head dropped


async def test_fifo_order_within_channel(tmp_path: Path) -> None:
    box = _outbox(tmp_path)
    delivered: list[str] = []

    async def fake_send(*, send_html, send_plain, label):  # type: ignore[no-untyped-def]
        delivered.append(label)
        return SendOutcome(message_id=1, fatal=False)

    with patch("telegram_bot.core.services.outbox.send_html_with_fallback", fake_send):
        box.enqueue(123, None, "first")
        box.enqueue(123, None, "second")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if box.size == 0:
                break

    assert box.size == 0
    assert len(delivered) == 2  # one chunk each, in order enqueued


def test_overflow_drops_oldest(tmp_path: Path) -> None:
    box = _outbox(tmp_path)
    with patch.object(Outbox, "start"):  # don't actually run the worker
        for i in range(205):
            box.enqueue(123, None, f"msg {i}")
    assert box.size == 200
