"""New-chat and checkpoint handlers — reply button shortcuts."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers.commands import _reset_channel
from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.handlers.streaming import (
    ensure_exec_mode_ready,
    send_to_tmux_if_active,
)
from telegram_bot.core.messages import all_translations, t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.streaming_manager import StreamingManager
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)

router = Router(name="mode")

_NEW_CHAT_TEXTS = frozenset({"new chat", "новый чат"})


def _is_new_chat_text(text: str | None) -> bool:
    return text is not None and text.strip().lower() in _NEW_CHAT_TEXTS


@router.message(F.text.func(_is_new_chat_text))
async def handle_new_chat_button(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
    settings: Settings,
    streaming_manager: StreamingManager,
) -> None:
    key = channel_key(message)
    logger.info("New chat (topic reset) for %s", key)
    await _reset_channel(
        message,
        key,
        session_manager,
        message_queue,
        forward_batcher,
        tmux_manager,
        topic_config,
        settings,
        streaming_manager,
    )


_CHECKPOINT_TEXTS = all_translations("ui.btn_checkpoint")


def _is_checkpoint_text(text: str | None) -> bool:
    # Match against all languages so the button survives a runtime /language
    # switch (see all_translations docstring). (H5, audit 2026-07-02.)
    return text is not None and text.strip() in _CHECKPOINT_TEXTS


@router.message(F.text.func(_is_checkpoint_text))
async def handle_checkpoint_button(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    prompt = t("ui.checkpoint_prompt")
    logger.info("Checkpoint button pressed for %s", key)

    if (
        topic_config.get_topic(key[1]).exec_mode == "tmux"
        and await ensure_exec_mode_ready(key, topic_config, tmux_manager, session_manager, message)
        and await send_to_tmux_if_active(key, prompt, message, tmux_manager)
    ):
        return

    message_queue.enqueue(key, prompt, message.message_id, message)
