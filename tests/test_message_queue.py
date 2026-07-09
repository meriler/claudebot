"""Tests for MessageQueue queue-recall (remove_by_token) and in-flight tracking."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from telegram_bot.core.services.message_queue import (
    ChatQueue,
    MessageQueue,
    QueueItem,
    RemoveResult,
)


def _queue(**kwargs) -> MessageQueue:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    session_manager = MagicMock()
    process_callback = AsyncMock()
    return MessageQueue(
        bot,
        session_manager,
        process_callback,
        startup_batch_window=0.0,
        preempt_idle_sec=0.0,
        **kwargs,
    )


def _item(token: str, msg_id: int = 1) -> QueueItem:
    return QueueItem(entries=[(msg_id, "p")], source_messages=[MagicMock()], token=token)


def test_tokens_unique() -> None:
    tokens = {
        QueueItem(entries=[(i, "p")], source_messages=[MagicMock()]).token for i in range(200)
    }
    assert len(tokens) == 200


def test_remove_pending_item() -> None:
    mq = _queue()
    key = (123, None)
    q = mq._get_queue(key)
    q.items.append(_item("AAA", 1))
    q.items.append(_item("BBB", 2))

    assert mq.remove_by_token(key, "AAA") == RemoveResult("removed")
    assert [it.token for it in q.items] == ["BBB"]


def test_remove_unknown_channel_is_not_found() -> None:
    mq = _queue()
    assert mq.remove_by_token((999, None), "whatever") == RemoveResult("not_found")


def test_double_remove_is_idempotent_not_found() -> None:
    mq = _queue()
    key = (1, None)
    q = mq._get_queue(key)
    q.items.append(_item("TOK"))
    assert mq.remove_by_token(key, "TOK").status == "removed"
    assert mq.remove_by_token(key, "TOK").status == "not_found"


def test_remove_current_item_is_in_flight() -> None:
    mq = _queue()
    key = (1, None)
    q = mq._get_queue(key)
    current = _item("LIVE")
    q.current = current
    # The in-flight item is not in `items`; it must report in_flight, not not_found.
    assert mq.remove_by_token(key, "LIVE") == RemoveResult("in_flight")
    assert q.current is current  # not mutated by the lookup


def test_forged_token_finds_nothing() -> None:
    mq = _queue()
    key = (1, None)
    q = mq._get_queue(key)
    q.items.append(_item("REAL"))
    assert mq.remove_by_token(key, "../../etc/passwd").status == "not_found"
    assert len(q.items) == 1


async def test_notification_retries_after_short_flood_wait(monkeypatch) -> None:
    """M8 (audit 2026-07-02): a short flood-wait must be retried, not lost."""
    from aiogram.exceptions import TelegramRetryAfter

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())  # don't actually wait
    mq = _queue()
    flood = TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=2)
    mq._bot.send_message = AsyncMock(side_effect=[flood, MagicMock()])
    await mq._send_notification((1, None), "hi")
    assert mq._bot.send_message.await_count == 2  # first flood, retry succeeded


async def test_notification_skips_long_flood_wait(monkeypatch) -> None:
    """A flood-wait beyond the cap is skipped so the queue lock isn't stalled."""
    from aiogram.exceptions import TelegramRetryAfter

    slept = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", slept)
    mq = _queue()
    mq._bot.send_message = AsyncMock(
        side_effect=TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=999)
    )
    await mq._send_notification((1, None), "hi")
    assert mq._bot.send_message.await_count == 1  # no retry
    slept.assert_not_awaited()  # never slept for the long wait


async def test_callback_exception_notifies_user_and_drains(monkeypatch) -> None:
    """H1 (audit 2026-07-02): a callback exception must not silently drop the
    turn. The user gets an error notification and the item is drained (no
    stuck in-flight, no forever-hanging 'Thinking…')."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())  # skip the backoff wait

    bot = MagicMock()
    bot.send_message = AsyncMock()
    cb = AsyncMock(side_effect=RuntimeError("boom"))
    mq = MessageQueue(bot, MagicMock(), cb, startup_batch_window=0.0, preempt_idle_sec=0.0)
    key = (123, None)
    q = mq._get_queue(key)
    q.items.append(_item("TOK"))

    await mq._process_next(key)

    assert not q.items  # drained, not stuck
    assert q.current is None  # in-flight cleared
    bot.send_message.assert_awaited_once()  # user notified of the failure


async def test_enqueue_while_busy_then_remove_and_in_flight() -> None:
    """End-to-end: first msg starts processing (becomes current), second is
    queued with a token and is removable; the in-flight first reports in_flight."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def cb(channel_key, prompt, source_messages, target_session_id):
        started.set()
        await release.wait()

    bot = MagicMock()
    bot.send_message = AsyncMock()
    mq = MessageQueue(bot, MagicMock(), cb, startup_batch_window=0.0, preempt_idle_sec=0.0)
    key = (123, 7)

    # First message: starts processing immediately, no notification.
    mq.enqueue(key, "first", 1, MagicMock(message_id=1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    q = mq._get_queue(key)
    assert q.current is not None
    assert q.current.entries[0][1] == "first"

    # Processing is active -> second message is queued (and notified with a button).
    mq.enqueue(key, "second", 2, MagicMock(message_id=2))
    assert len(q.items) == 1
    pending_token = q.items[0].token

    # In-flight item cannot be recalled.
    assert mq.remove_by_token(key, q.current.token).status == "in_flight"
    # Pending item can.
    assert mq.remove_by_token(key, pending_token).status == "removed"
    assert len(q.items) == 0

    # Let the first finish; current clears.
    release.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if q.current is None:
            break
    assert q.current is None


async def test_chatqueue_defaults() -> None:
    q = ChatQueue()
    assert q.current is None
    assert len(q.items) == 0
