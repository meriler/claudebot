"""Video note (round video / кружок) handler.

Downloads the video_note, extracts 3 frames (start/middle/end) + transcribes
audio, then dispatches as a media-style prompt so CC can both read the
transcript and Read() the frame files.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.types import Message

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers._dispatch import enqueue_prompt, resolve_reply_routing
from telegram_bot.core.handlers.photo import _get_tmp_dir
from telegram_bot.core.handlers.streaming import (
    send_to_tmux_if_active,
)
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.forward_batcher import _process_video_note
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.transcriber import Transcriber
from telegram_bot.core.types import channel_key

if TYPE_CHECKING:
    from telegram_bot.core.handlers.forward import ForwardBatcher

logger = logging.getLogger(__name__)

router = Router(name="video_note")


@router.message(F.video_note)
async def handle_video_note(
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
    key = channel_key(message)
    logger.info("Video note in %s", key)

    status = await message.answer(t("ui.recognizing_voice"))

    tmp_dir = _get_tmp_dir(session_manager.file_cache_dir)
    text, frame_paths = await _process_video_note(message, bot, transcriber, tmp_dir)

    parts: list[str] = [text]
    for fp in frame_paths:
        parts.append(t("cc.attached_file", path=str(fp)))
    prompt = "\n".join(parts)

    # Replace the status bubble with the transcript line (frames stay in prompt only)
    try:
        await status.edit_text(text)
    except Exception:
        logger.debug("Failed to edit video_note status message", exc_info=True)

    # Full reply-to-resume routing (engine/exec switch + ensure_ready + tmux
    # session switch), shared with text.py — so a reply-kruzhok to a different
    # engine actually switches instead of bailing/mis-routing. Runs after the
    # transcript is shown, mirroring voice. (M3→M4/M5, audit 2026-07-02.)
    bail, target_session_id, tmux_switched = await resolve_reply_routing(
        key,
        message,
        message.reply_to_message,
        session_manager=session_manager,
        topic_config=topic_config,
        tmux_manager=tmux_manager,
        message_queue=message_queue,
    )
    if bail:
        return

    if await send_to_tmux_if_active(key, prompt, message, tmux_manager):
        return

    enqueue_prompt(
        key,
        prompt,
        message,
        message_queue,
        tmux_manager,
        target_session_id=target_session_id,
        inject_reply_if_no_target=not tmux_switched,
        settings=settings,
        topic_config=topic_config,
    )
