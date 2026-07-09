"""Forwarded-message handler entry point.

Thin router around `core/services/forward_batcher` — the batching, media
download, and prompt formatting live there; this module owns the
aiogram `@router.message(F.forward_origin)` registration and the
single `handle_forward` callback.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers._dispatch import enqueue_prompt, resolve_reply_routing
from telegram_bot.core.handlers.streaming import send_to_tmux_if_active
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.forward_batcher import (
    ChatBatch,
    ForwardBatcher,
    ForwardedMessage,
    SenderInfo,
    _download_document,
    _download_photo,
    _extract_sender_info,
    _format_batch_prompt,
    _format_sender,
    _process_forwarded_message,
    _transcribe_voice,
    sanitize_forwarded_content,
    unparse_entities,
)
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.transcriber import Transcriber
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)

router = Router(name="forward")

# Re-exports — tests and sibling handlers import these names from
# `forward.py` for historical reasons; listing them in __all__ makes the
# re-export explicit for mypy.
__all__ = [
    "ChatBatch",
    "ForwardBatcher",
    "ForwardedMessage",
    "SenderInfo",
    "_download_document",
    "_download_photo",
    "_extract_sender_info",
    "_format_batch_prompt",
    "_format_sender",
    "_process_forwarded_message",
    "_transcribe_voice",
    "handle_forward",
    "router",
    "sanitize_forwarded_content",
    "unparse_entities",
]


@router.message(F.forward_origin)
async def handle_forward(
    message: Message,
    bot: Bot,
    session_manager: SessionManager,
    transcriber: Transcriber,
    forward_batcher: ForwardBatcher,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
    settings: Settings,
) -> None:
    """Handle forwarded messages: collect into batch, process media, enqueue to MessageQueue."""
    key = channel_key(message)
    logger.info("Forward message in %s", key)

    async def on_batch(raw_messages: list[Message]) -> None:
        # Read comment and reply context before async boundary to avoid race with _cleanup()
        comment = forward_batcher.get_comment(key)
        text_reply = forward_batcher.get_text_reply_to_message(key)

        # Show processing status to user
        last_msg = forward_batcher.get_last_message(key) or message
        # Full reply-to-resume routing (engine/exec switch + ensure_ready + tmux
        # session switch), shared with text.py — a reply targeting a different
        # engine now switches instead of bailing. Forward resolves its reply
        # target from the batcher's captured text-reply. (M4/M5, audit 2026-07-02.)
        bail, target_session_id, _tmux_switched = await resolve_reply_routing(
            key,
            last_msg,
            text_reply,
            session_manager=session_manager,
            topic_config=topic_config,
            tmux_manager=tmux_manager,
            message_queue=message_queue,
        )
        if bail:
            return
        await last_msg.answer(t("ui.processing_forwards"))

        # Process all messages in parallel (download media, transcribe voice)
        processed = await asyncio.gather(
            *[
                _process_forwarded_message(
                    msg,
                    bot,
                    transcriber,
                    session_manager.file_cache_dir,
                )
                for msg in raw_messages
            ],
        )
        batch = list(processed)
        prompt = _format_batch_prompt(batch, comment)

        # Tmux with active tail: send directly to CC stdin, bypass queue.
        if await send_to_tmux_if_active(key, prompt, last_msg, tmux_manager):
            return

        # target_session_id was resolved by resolve_reply_routing above (None in
        # tmux mode). `last_msg.from_user` is the whitelisted person who forwarded
        # this to the bot — so attribution labels the forwarder, not the original
        # authors of the forwarded content (those are rendered inside the batch
        # body by forward_batcher._format_sender). That's the intended target.
        enqueue_prompt(
            key,
            prompt,
            last_msg,
            message_queue,
            tmux_manager,
            target_session_id=target_session_id,
            # Forwarded batches already have their own structure; injecting a
            # reply quote would confuse the agent about whose content is whose.
            inject_reply_if_no_target=False,
            settings=settings,
            topic_config=topic_config,
        )

    forward_batcher.add(key, message, on_batch)
