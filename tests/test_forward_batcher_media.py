"""Regression tests for ForwardBatcher media-buffer snapshot handling.

Covers C1 (audit 2026-07-02): a blanket `cb.media_buffer.clear()` at the end
of `_process_batch` dropped photos/documents that arrived via `add_media`
DURING the media callback await. The fix mirrors the comment/voice handling —
delete only the snapshot prefix, keeping messages that arrived mid-processing
for the next debounce cycle.

The media path of `_process_batch` never reads Message attributes (it just
forwards the list to the callback), so opaque sentinels stand in for real
aiogram Messages here.
"""

import asyncio

from telegram_bot.core.services.forward_batcher import ChatBatch, ForwardBatcher

_KEY = (123, None)


async def test_force_flush_waits_for_inflight_processing() -> None:
    """M1 (audit 2026-07-02): a buffer-overflow force-flush must chain after the
    in-flight processing_task, not run a parallel _process_batch on the channel.
    """
    batcher = ForwardBatcher()
    order: list[str] = []
    release = asyncio.Event()

    async def slow_prev() -> None:
        await release.wait()
        order.append("prev")

    prev = asyncio.create_task(slow_prev())

    async def fake_process(channel_key, collected) -> None:  # type: ignore[no-untyped-def]
        order.append("flush")

    batcher._process_batch = fake_process  # type: ignore[assignment]
    batcher._batches[_KEY] = ChatBatch()

    task = asyncio.create_task(batcher._run_processing_after(prev, _KEY, []))
    await asyncio.sleep(0.03)
    assert order == []  # force-flush is blocked on the in-flight task

    release.set()
    await task
    assert order == ["prev", "flush"]  # serialized: previous finished first


async def test_media_arriving_during_callback_is_not_lost() -> None:
    """Photo B sent while media_cb(A) is awaiting must survive for the next batch."""
    batcher = ForwardBatcher()
    msg_a = object()
    msg_b = object()
    seen: list[list[object]] = []

    async def media_cb(items: list[object]) -> None:
        # Snapshot of what the callback actually received.
        seen.append(list(items))
        # A second photo lands mid-processing, exactly like a user double-send
        # during a slow cold-start.
        batcher._batches[_KEY].media_buffer.append(msg_b)

    batcher._batches[_KEY] = ChatBatch(
        media_buffer=[msg_a],
        media_callback=media_cb,  # type: ignore[arg-type]
    )

    await batcher._process_batch(_KEY, [])

    # The callback saw exactly the snapshot taken before the await.
    assert seen == [[msg_a]]
    # msg_b, appended during the await, was preserved (not cleared).
    assert batcher._batches[_KEY].media_buffer == [msg_b]


async def test_media_fully_drained_when_nothing_arrives_mid_await() -> None:
    """No mid-await additions → snapshot prefix delete empties the buffer."""
    batcher = ForwardBatcher()
    msg_a = object()
    msg_b = object()
    seen: list[list[object]] = []

    async def media_cb(items: list[object]) -> None:
        seen.append(list(items))

    batcher._batches[_KEY] = ChatBatch(
        media_buffer=[msg_a, msg_b],
        media_callback=media_cb,  # type: ignore[arg-type]
    )

    await batcher._process_batch(_KEY, [])

    assert seen == [[msg_a, msg_b]]
    assert batcher._batches[_KEY].media_buffer == []
