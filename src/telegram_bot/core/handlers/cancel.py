"""Cancel callback and text message handlers — manage CC process via MessageQueue."""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from telegram_bot.core.messages import all_translations, t
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.streaming_manager import StreamingManager
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.types import GENERAL_TOPIC_ID, ChannelKey, channel_key

logger = logging.getLogger(__name__)

router = Router(name="cancel")

# chat-type filter is applied only to dp.message (see __main__), NOT to
# dp.callback_query — so callback handlers must guard the chat type themselves.
_ALLOWED_CALLBACK_CHAT_TYPES = {ChatType.PRIVATE, ChatType.SUPERGROUP}


def _callback_channel_key(message: Message | InaccessibleMessage) -> ChannelKey:
    """Extract a normalized ChannelKey from a callback message.

    Mirrors `types.channel_key`: in a forum supergroup the General topic has
    `message_thread_id is None`, which `channel_key` maps to GENERAL_TOPIC_ID.
    The queue is keyed by `channel_key`, so the callback MUST normalize the same
    way — otherwise a General-topic queue stored under (chat, 0) is searched
    under (chat, None) and the lookup silently misses.
    """
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is None and getattr(message.chat, "is_forum", False):
        thread_id = GENERAL_TOPIC_ID
    return (message.chat.id, thread_id)


@router.callback_query(F.data == "cancel_cc")
async def handle_cancel_cc(
    callback: CallbackQuery,
    queue: MessageQueue,
    tmux_manager: TmuxManager,
    streaming_manager: StreamingManager,
) -> None:
    """Handle cancel button press: interrupt tmux/streaming CC or kill subprocess, keep queue."""
    message = callback.message
    if message is None:
        await callback.answer()
        return

    key = _callback_channel_key(message)
    tmux_acted = tmux_manager.is_active(key)
    if tmux_acted:
        await tmux_manager.cancel(key)
    # Streaming: interrupt the live turn (control_request) — does NOT kill the
    # persistent process, so the next message continues the same session.
    streaming_acted = streaming_manager.is_active(key)
    if streaming_acted:
        await streaming_manager.cancel(key)
    cancelled = await queue.cancel(key)
    acted = cancelled or tmux_acted or streaming_acted

    if acted:
        logger.info("User cancelled CC processing for %s", key)
        try:
            await message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
        except Exception:
            logger.debug("Failed to remove cancel buttons", exc_info=True)
        pending = queue.pending_count(key)
        if pending > 0:
            await message.answer(t("ui.cancelled_queue_pending", count=pending))
        else:
            await message.answer(t("ui.cancelled"))
    else:
        logger.debug("Cancel pressed but no active process for %s", key)
        try:
            await message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
        except Exception:
            logger.debug("Failed to remove orphaned cancel buttons", exc_info=True)

    await callback.answer(t("ui.already_finished") if not acted else None)


@router.callback_query(F.data.startswith("qrm:"))
async def handle_queue_remove(callback: CallbackQuery, queue: MessageQueue) -> None:
    """Queue-recall button: drop a pending QueueItem by its token.

    Removes the whole item (a batch may have merged several messages). The
    currently-processing item can't be recalled — that path answers in_flight
    ("use Stop"). auth is applied via dp.callback_query middleware; chat type is
    guarded here because the chat-type filter sits only on dp.message.
    """
    message = callback.message
    if message is None:
        await callback.answer()
        return
    if getattr(message.chat, "type", None) not in _ALLOWED_CALLBACK_CHAT_TYPES:
        await callback.answer()
        return

    data = callback.data or ""
    token = data.split(":", 1)[1] if ":" in data else ""
    if not token:
        await callback.answer()
        return

    key = _callback_channel_key(message)
    result = queue.remove_by_token(key, token)

    if result.status == "removed":
        logger.info("User removed queued item for %s (token=%s)", key, token)
        with contextlib.suppress(Exception):
            await message.edit_text(t("ui.queue_removed"))  # type: ignore[union-attr]
        await callback.answer()
    elif result.status == "in_flight":
        # Already taken into work — can't recall, only Stop.
        await callback.answer(t("ui.queue_in_flight"))
    else:  # not_found — idempotent: drop the stale button, confirm gone
        with contextlib.suppress(Exception):
            await message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
        await callback.answer(t("ui.queue_removed"))


_CANCEL_TEXTS = all_translations("ui.btn_cancel")


def _is_cancel_text(text: str | None) -> bool:
    # Match against all languages so the button survives a runtime /language
    # switch (see all_translations docstring). (H5, audit 2026-07-02.)
    return text is not None and text.strip() in _CANCEL_TEXTS


@router.message(F.text.func(_is_cancel_text))
async def handle_cancel_text(
    message: Message,
    queue: MessageQueue,
    tmux_manager: TmuxManager,
    streaming_manager: StreamingManager,
) -> None:
    """Reply-keyboard cancel: interrupt tmux/streaming CC or kill subprocess, clear queue."""
    key = channel_key(message)
    tmux_acted = tmux_manager.is_active(key)
    if tmux_acted:
        await tmux_manager.cancel(key)
    streaming_acted = streaming_manager.is_active(key)
    if streaming_acted:
        await streaming_manager.cancel(key)
    cancelled = await queue.cancel(key)

    if cancelled or tmux_acted or streaming_acted:
        logger.info("User cancelled CC processing (text) for %s", key)
        pending = queue.pending_count(key)
        if pending > 0:
            await message.answer(t("ui.cancelled_queue_pending", count=pending))
        else:
            await message.answer(t("ui.cancelled"))
    else:
        logger.debug("Cancel text pressed but no active process for %s", key)
        await message.answer(t("ui.nothing_to_cancel"))
