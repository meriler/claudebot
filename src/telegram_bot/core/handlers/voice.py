"""Voice message handler — defers transcription to ForwardBatcher for co-batching."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.types import Message

from telegram_bot.core.config import MAX_VOICE_SIZE_BYTES, Settings
from telegram_bot.core.handlers._dispatch import (
    apply_sender_attribution,
    enqueue_prompt,
    resolve_reply_routing,
)
from telegram_bot.core.handlers.streaming import (
    send_to_streaming_if_busy,
    send_to_tmux_if_active,
)
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.streaming_manager import StreamingManager
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.transcriber import Transcriber
from telegram_bot.core.types import channel_key
from telegram_bot.core.utils.text_normalize import normalize_thinking_trigger

if TYPE_CHECKING:
    from telegram_bot.core.handlers.forward import ForwardBatcher

logger = logging.getLogger(__name__)

router = Router(name="voice")


@router.message(F.voice)
async def handle_voice(
    message: Message,
    bot: Bot,
    session_manager: SessionManager,
    transcriber: Transcriber,
    forward_batcher: ForwardBatcher,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
    settings: Settings,
    streaming_manager: StreamingManager,
    inbox_reply_handler: Callable[[Message, MessageQueue], Awaitable[bool]] | None = None,
) -> None:
    key = channel_key(message)
    logger.debug("Voice message from user %s", message.from_user and message.from_user.id)

    # Check file size before anything else
    if message.voice and message.voice.file_size and message.voice.file_size > MAX_VOICE_SIZE_BYTES:
        await message.answer(t("ui.voice_too_large"))
        return

    # Show recognizing status immediately; ForwardBatcher._process_batch will edit it
    # with the transcript once transcription completes.
    recognizing_msg = await message.answer(t("ui.recognizing_voice"))

    async def on_voice_batch(voice_snapshot: list[tuple[Message, Message]]) -> None:
        """Handle voice-only batch after transcription has completed.

        By the time this runs, forward_batcher._process_batch has already transcribed
        every voice in the snapshot and appended the results to cb.comment. We read
        them back via get_comment(), then dispatch to tmux or the message queue.
        """
        transcripts = forward_batcher.get_comment(key)
        if not transcripts:
            # All transcriptions failed or returned nothing. The "recognizing…"
            # status MUST reach a terminal state — a silently parked status
            # reads as "the bot ate my voice message".
            with contextlib.suppress(Exception):
                await recognizing_msg.edit_text(t("ui.voice_recognition_failed"))
            return

        # Check inbox reply for every voice in the batch — if any one is a reply
        # to an inbox report, that path wins and the executor is launched.
        if inbox_reply_handler is not None:
            for voice_msg, _ in voice_snapshot:
                if await inbox_reply_handler(voice_msg, message_queue):
                    return

        # Transcripts already carry the "[Voice, transcription]:" short prefix
        # (added by _try_transcribe_voice). Joining them produces the full prompt
        # without double-prefixing.
        prompt = "\n".join(transcripts)
        # Dictation splits the "ultrathink" trigger into "ultra think"; glue it
        # back before the prompt is routed to tmux stdin or the queue.
        prompt = normalize_thinking_trigger(prompt)

        # Reply target and context track the last voice in the batch
        last_voice_msg = voice_snapshot[-1][0]

        # Full reply-to-resume routing (engine/exec switch + ensure_ready + tmux
        # session switch), shared with text.py. Previously voice only bailed with
        # tui_session_missing on a provider switch (M4) and computed the tmux
        # target too late (M5). (audit 2026-07-02.)
        bail, target_session_id, tmux_switched = await resolve_reply_routing(
            key,
            last_voice_msg,
            last_voice_msg.reply_to_message,
            session_manager=session_manager,
            topic_config=topic_config,
            tmux_manager=tmux_manager,
            message_queue=message_queue,
        )
        if bail:
            return

        # Hot paths bypass the queue (where attribution lives) — attribute the
        # injected transcript here so a second whitelisted person's dictation
        # mid-turn is not sent to the engine unattributed. (H4, audit 2026-07-02.)
        attributed_prompt = apply_sender_attribution(
            prompt, last_voice_msg, key, settings, topic_config
        )

        # Streaming: a live turn -> inject (steer between tool calls), bypass
        # the queue — mirrors text.py so a dictated message mid-turn steers
        # instead of waiting in the queue for the turn to finish. No live turn
        # -> fall through to enqueue, which starts a turn. `and` short-circuits
        # so the busy check only runs for streaming-mode topics.
        if topic_config.get_topic(
            key[1]
        ).exec_mode == "streaming" and await send_to_streaming_if_busy(
            key, attributed_prompt, last_voice_msg, streaming_manager
        ):
            return

        # Tmux with active tail: send directly to CC stdin, bypass queue.
        if await send_to_tmux_if_active(key, attributed_prompt, last_voice_msg, tmux_manager):
            return

        enqueue_prompt(
            key,
            prompt,
            last_voice_msg,
            message_queue,
            tmux_manager,
            target_session_id=target_session_id,
            inject_reply_if_no_target=not tmux_switched,
            settings=settings,
            topic_config=topic_config,
        )

    forward_batcher.add_voice(key, message, recognizing_msg, on_voice_batch)
