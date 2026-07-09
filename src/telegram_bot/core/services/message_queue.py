"""MessageQueue — per-chat message queuing and batching for Telegram bot."""

from __future__ import annotations

import asyncio
import collections
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from telegram_bot.core.keyboards import queue_item_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

# Only a short flood-wait is worth sitting through before retrying a best-effort
# queue notification: _send_notification runs under the queue lock, so a long
# sleep would stall the channel's queue. A longer flood-wait → skip the retry.
_NOTIFY_RETRY_CAP_SEC = 10.0


def _new_token() -> str:
    """Short, unforgeable id binding a queue-remove button to a QueueItem.

    Used in callback_data (`qrm:<token>`) instead of a raw message_id:
    message_id is monotonic only per-chat and fully client-controlled in
    callback_data, so it is unsafe as a primary key. The token is looked up
    strictly within the channel's own deque, so a forged `qrm:<token>` finds
    nothing.
    """
    return secrets.token_urlsafe(6)


@dataclass
class QueueItem:
    """One item in the message queue — may contain multiple batched prompts."""

    entries: list[tuple[int, str]]  # (message_id, prompt)
    source_messages: list[Message]
    target_session_id: str | None = None
    token: str = field(default_factory=_new_token)


@dataclass
class ChatQueue:
    """Per-chat queue state."""

    items: collections.deque[QueueItem] = field(default_factory=collections.deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    error_count: int = 0
    # The item currently being processed (popped from `items`, handed to the
    # process callback). Set in `_process_next` right after popleft and cleared
    # when the item finishes. `remove_by_token` uses it to tell "still pending"
    # (removable) apart from "already taken into work" (use Stop) without
    # guessing — closes the not_found-vs-in_flight ambiguity.
    current: QueueItem | None = None


@dataclass
class RemoveResult:
    """Outcome of `MessageQueue.remove_by_token`."""

    status: str  # "removed" | "not_found" | "in_flight"


# Type alias for the process callback
ProcessCallback = Callable[
    [ChannelKey, str, list[Message], str | None],
    Awaitable[None],
]


def _combine_prompts(entries: list[tuple[int, str]]) -> str:
    """Combine prompt entries into a single prompt string.

    Single entry: return the prompt as-is.
    Multiple entries (sorted by message_id): numbered Russian format.
    """
    sorted_entries = sorted(entries, key=lambda e: e[0])

    if len(sorted_entries) == 1:
        return sorted_entries[0][1]

    count = len(sorted_entries)
    parts = [t("cc.batch_during_processing", count=count)]
    for i, (_, prompt) in enumerate(sorted_entries, 1):
        parts.append(f"\n{i}. {prompt}")

    return "\n".join(parts)


class MessageQueue:
    """Central orchestrator for per-chat message processing."""

    def __init__(
        self,
        bot: Bot,
        session_manager: SessionManager,
        process_callback: ProcessCallback,
        startup_batch_window: float = 0.0,
        preempt_idle_sec: float = 0.0,
    ) -> None:
        self._bot = bot
        self._session_manager = session_manager
        self._process_callback = process_callback
        self._queues: dict[ChannelKey, ChatQueue] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._startup_ready = asyncio.Event()
        self._preempt_idle_sec = preempt_idle_sec
        if startup_batch_window > 0:
            logger.info(
                "Startup batch window: %.1fs — collecting messages before processing",
                startup_batch_window,
            )
            task = asyncio.create_task(self._open_after_delay(startup_batch_window))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        else:
            self._startup_ready.set()

    async def _open_after_delay(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._startup_ready.set()
        channels_to_start = [
            key for key, q in self._queues.items() if q.items and not q.lock.locked()
        ]
        logger.info(
            "Startup batch window closed — %d channel(s) ready to process",
            len(channels_to_start),
        )
        for key in channels_to_start:
            self._start_processing(key)

    def _get_queue(self, channel_key: ChannelKey) -> ChatQueue:
        if channel_key not in self._queues:
            self._queues[channel_key] = ChatQueue()
        return self._queues[channel_key]

    def is_busy(self, channel_key: ChannelKey) -> bool:
        """Return True if the channel has active processing or queued items.

        Does NOT create a queue entry for unknown keys — a no-op check must
        not pollute `_queues` with empty `ChatQueue` instances.
        """
        queue = self._queues.get(channel_key)
        if queue is None:
            return False
        return queue.lock.locked() or bool(queue.items)

    def enqueue(
        self,
        channel_key: ChannelKey,
        prompt: str,
        message_id: int,
        source_message: Message,
        target_session_id: str | None = None,
        suppress_notification: bool = False,
    ) -> None:
        """Add a message to the channel's queue.

        Synchronous — no await between state check and mutation to prevent races.
        suppress_notification=True skips the "added to queue" Telegram message.
        Use this when the caller already provides meaningful feedback (e.g. tmux mode).
        """
        queue = self._get_queue(channel_key)

        if self._preempt_idle_sec > 0 and queue.lock.locked():
            idle = self._session_manager.idle_seconds(channel_key)
            if idle is not None and idle >= self._preempt_idle_sec:
                logger.info(
                    "Preemptive cancel for %s: CC idle %.0fs >= %.0fs threshold",
                    channel_key,
                    idle,
                    self._preempt_idle_sec,
                )
                task = asyncio.create_task(self._preempt_cancel(channel_key))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

        if not queue.lock.locked():
            # First message — start processing immediately, no notification
            item = QueueItem(
                entries=[(message_id, prompt)],
                source_messages=[source_message],
                target_session_id=target_session_id,
            )
            queue.items.append(item)
            if not self._startup_ready.is_set():
                logger.info(
                    "MSG_TRACE queue_enqueue channel=%s msg=%d action=startup_buffer "
                    "prompt_len=%d target_sid=%s",
                    channel_key,
                    message_id,
                    len(prompt),
                    target_session_id,
                )
                return
            logger.info(
                "MSG_TRACE queue_enqueue channel=%s msg=%d action=immediate_start "
                "prompt_len=%d target_sid=%s",
                channel_key,
                message_id,
                len(prompt),
                target_session_id,
            )
            self._start_processing(channel_key)
            return

        # Processing is active — try to batch or create new item
        target_key = target_session_id
        batched = False

        # Find last item in deque with matching target
        for item in reversed(queue.items):
            if item.target_session_id == target_key:
                item.entries.append((message_id, prompt))
                item.source_messages.append(source_message)
                batched = True
                # Find position of this item in queue (1-based)
                position = list(queue.items).index(item) + 1
                break

        if batched:
            notification = self._build_notification(
                is_batch=True,
                position=position,
                target_session_id=target_session_id,
            )
        else:
            # Create new QueueItem
            item = QueueItem(
                entries=[(message_id, prompt)],
                source_messages=[source_message],
                target_session_id=target_session_id,
            )
            queue.items.append(item)
            position = len(queue.items)
            notification = self._build_notification(
                is_batch=False,
                position=position,
                target_session_id=target_session_id,
            )
        logger.info(
            "MSG_TRACE queue_enqueue channel=%s msg=%d action=%s position=%d "
            "prompt_len=%d target_sid=%s",
            channel_key,
            message_id,
            "appended_to_existing" if batched else "new_item",
            position,
            len(prompt),
            target_session_id,
        )

        if suppress_notification:
            return

        # The button removes the WHOLE QueueItem (a batch may have coalesced
        # several Telegram messages into one item via ForwardBatcher). Both the
        # batched and new-item branches above leave `item` bound to the relevant
        # QueueItem, so its token is the right target.
        keyboard = queue_item_keyboard(item.token)

        # Send notification (fire and forget, prevent GC via _background_tasks)
        task = asyncio.create_task(
            self._send_notification(channel_key, notification, reply_markup=keyboard)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _build_notification(
        self,
        *,
        is_batch: bool,
        position: int,
        target_session_id: str | None,
    ) -> str:
        """Build composable notification text."""
        if is_batch:
            text = t("ui.queue_added_batch", position=position)
        else:
            text = t("ui.queue_added", position=position)

        if target_session_id is not None:
            short_id = target_session_id[:6]
            text += t("ui.queue_session_suffix", sid=short_id)

        return text

    async def _send_notification(
        self,
        channel_key: ChannelKey,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Send a notification message to the channel."""
        chat_id, thread_id = channel_key
        try:
            await self._bot.send_message(
                chat_id,
                text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
        except TelegramRetryAfter as e:
            # Flood-wait: TelegramRetryAfter is a sibling of TelegramBadRequest,
            # not a subclass, so it fell through to the generic handler and the
            # notification (queue position + cancel button) was silently lost.
            # Wait out a SHORT flood window and retry once; a long one is not
            # worth stalling the queue lock for, so we skip it. (M8, audit
            # 2026-07-02.)
            delay = float(getattr(e, "retry_after", 0) or 0)
            if delay > _NOTIFY_RETRY_CAP_SEC:
                logger.info(
                    "Notification flood-wait %.0fs > cap for %s, skipping", delay, channel_key
                )
                return
            logger.info("Notification flood-wait %.0fs for %s, retrying once", delay, channel_key)
            await asyncio.sleep(delay)
            try:
                await self._bot.send_message(
                    chat_id,
                    text,
                    message_thread_id=thread_id,
                    reply_markup=reply_markup,
                )
            except Exception:
                logger.warning("Queue notification retry failed for %s", channel_key, exc_info=True)
        except TelegramBadRequest:
            logger.warning(
                "TelegramBadRequest sending queue notification to %s",
                channel_key,
                exc_info=True,
            )
        except Exception:
            logger.exception("Failed to send queue notification to %s", channel_key)

    async def _preempt_cancel(self, channel_key: ChannelKey) -> None:
        await self._session_manager.cancel(channel_key)

    def _start_processing(self, channel_key: ChannelKey) -> None:
        """Start the processing loop for a channel."""
        task = asyncio.create_task(self._process_next(channel_key))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _process_next(self, channel_key: ChannelKey) -> None:
        """Process all items in the queue, one at a time, under lock."""
        queue = self._get_queue(channel_key)

        async with queue.lock:
            while queue.items:
                item = queue.items.popleft()
                # Mark in-flight BEFORE the await: a concurrent remove_by_token
                # then reports "in_flight" (use Stop) instead of failing to find
                # the item and lying "already removed". Cleared in finally below.
                queue.current = item

                # Sort entries by message_id (ascending) and combine prompts
                combined_prompt = _combine_prompts(item.entries)
                logger.info(
                    "MSG_TRACE queue_dequeue channel=%s msg_ids=%s prompt_len=%d "
                    "target_sid=%s remaining=%d",
                    channel_key,
                    [mid for mid, _ in item.entries],
                    len(combined_prompt),
                    item.target_session_id,
                    len(queue.items),
                )

                try:
                    await self._process_callback(
                        channel_key,
                        combined_prompt,
                        item.source_messages,
                        item.target_session_id,
                    )
                    queue.error_count = 0
                except Exception:
                    # Drop semantics: the item was already popped above and is
                    # not re-enqueued. The backoff throttles the NEXT item so
                    # consecutive failures don't storm downstream; it is not a
                    # per-item retry. error_count resets on first success.
                    queue.error_count += 1
                    backoff_sec = min(2**queue.error_count, 30)
                    logger.warning(
                        "Queue item dropped for %s after callback error "
                        "(consecutive failures=%d, next-item backoff=%ds)",
                        channel_key,
                        queue.error_count,
                        backoff_sec,
                        exc_info=True,
                    )
                    # Tell the user their turn failed. Without this the drop is
                    # silent — a "Thinking…" placeholder can hang forever and
                    # the user never learns the message was lost. Best-effort
                    # (_send_notification swallows its own send errors).
                    # (H1, audit 2026-07-02.)
                    await self._send_notification(channel_key, t("ui.error_generic"))
                    await asyncio.sleep(backoff_sec)
                finally:
                    # No longer in-flight; subsequent removes target pending items.
                    queue.current = None

    async def clear(self, channel_key: ChannelKey) -> None:
        """Clear the queue for a channel: wait for active processing, then discard pending items."""
        queue = self._get_queue(channel_key)
        pending_count = len(queue.items)
        is_active = queue.lock.locked()
        logger.info(
            "Clearing queue for %s: %d pending items, active=%s",
            channel_key,
            pending_count,
            is_active,
        )

        # Kill CC subprocess first so processing finishes quickly
        await self._session_manager.cancel(channel_key)

        # Wait for processing to finish, then clear under lock
        async with queue.lock:
            queue.items.clear()

    def pending_count(self, channel_key: ChannelKey) -> int:
        """Return number of queued items waiting to be processed."""
        queue = self._queues.get(channel_key)
        if queue is None:
            return 0
        return len(queue.items)

    def remove_by_token(self, channel_key: ChannelKey, token: str) -> RemoveResult:
        """Remove a pending QueueItem by its token (queue-recall button).

        Synchronous on purpose: it mutates the deque within a single event-loop
        tick with no `await` between read and mutation, exactly like `enqueue`.
        That is what makes it race-free against `_process_next` (which pops under
        `queue.lock`) without introducing a second lock — there is no suspension
        point for the loop to interleave with.

        Three outcomes:
          - "removed"   — token found among pending items, dropped.
          - "in_flight" — token belongs to the item currently being processed
                          (already popped, can't be recalled — use Stop).
          - "not_found" — not pending and not current; idempotent "already gone".
        """
        queue = self._queues.get(channel_key)
        if queue is None:
            return RemoveResult("not_found")
        if queue.current is not None and queue.current.token == token:
            return RemoveResult("in_flight")
        for item in queue.items:
            if item.token == token:
                queue.items.remove(item)  # deque.remove is O(n); queue is short
                logger.info(
                    "MSG_TRACE queue_remove channel=%s token=%s msg_ids=%s remaining=%d",
                    channel_key,
                    token,
                    [mid for mid, _ in item.entries],
                    len(queue.items),
                )
                return RemoveResult("removed")
        return RemoveResult("not_found")

    async def cancel(self, channel_key: ChannelKey) -> bool:
        """Cancel current processing: kill CC process, keep queued items. Preserve session.

        Returns True if there was an active process to cancel.
        Queued messages are NOT dropped — the processing loop picks them up
        after the cancelled item finishes.
        """
        queue = self._get_queue(channel_key)
        queue.error_count = 0
        stopped = await self._session_manager.cancel(channel_key)
        return stopped

    async def shutdown(self) -> None:
        """Cancel background tasks, clear all queues."""
        # Cancel notification tasks
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # Clear all channel queues (items only — lock releases naturally)
        for _channel_key, queue in self._queues.items():
            queue.items.clear()

        self._queues.clear()
