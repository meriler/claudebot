"""Text message handler — forwards text to Claude Code via MessageQueue."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.types import Message

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers._dispatch import (
    apply_sender_attribution,
    enqueue_prompt,
    resolve_reply_routing,
)
from telegram_bot.core.handlers.forward import ForwardBatcher, unparse_entities
from telegram_bot.core.handlers.streaming import (
    ensure_exec_mode_ready,
    send_to_streaming_if_busy,
    send_to_tmux_if_active,
)
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.streaming_manager import StreamingManager
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.tui.routing import route_slash_command
from telegram_bot.core.types import channel_key
from telegram_bot.core.utils.text_normalize import normalize_thinking_trigger

logger = logging.getLogger(__name__)

router = Router(name="text")


@router.message(F.text)
async def handle_text(
    message: Message,
    session_manager: SessionManager,
    forward_batcher: ForwardBatcher,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
    settings: Settings,
    streaming_manager: StreamingManager,
    inbox_reply_handler: Callable[[Message, MessageQueue], Awaitable[bool]] | None = None,
) -> None:
    # The sender's own text is trusted as content; sender attribution (added in
    # enqueue_prompt) sanitizes the *display name* for multi-user setups.
    # Alias the bot Settings: the on_text_only closure below rebinds the name
    # `settings` to a per-topic TopicSettings, which would shadow this param.
    bot_settings = settings
    text = unparse_entities(message.text, message.entities)
    if not text.strip():
        return
    # Glue a dictated/typed "ultra think" into the "ultrathink" trigger before
    # any routing — applied here so every downstream path (slash check, tmux
    # stdin, streaming inject, queue) sees the corrected text.
    text = normalize_thinking_trigger(text)

    key = channel_key(message)
    logger.info(
        "MSG_TRACE handle_text channel=%s msg=%d text_len=%d user=%s",
        key,
        message.message_id,
        len(text),
        message.from_user and message.from_user.id,
    )

    if inbox_reply_handler is not None and await inbox_reply_handler(message, message_queue):
        return

    # Slash-command forwarding for tmux topics (Decision 9, Wave 3).
    # Non-whitelist slash commands (`/model`, `/compact`, `/mcp`, …) go
    # directly to the CC TUI via send-keys, bypassing the forward batcher
    # and MessageQueue. Bot-reserved commands from BOT_RESERVED_COMMANDS
    # (`/new`, `/kill`, `/cancel`, …) never reach this handler — aiogram
    # routes them by Command() filter first. Reply-to-resume is not applied:
    # slash commands logically belong to the currently-live TUI, not to
    # whatever session the reply-target references.
    if (
        text.startswith("/")
        and route_slash_command(text) == "tui"
        and topic_config.get_topic(key[1]).exec_mode == "tmux"
    ):
        if not await ensure_exec_mode_ready(
            key, topic_config, tmux_manager, session_manager, message
        ):
            return
        if await send_to_tmux_if_active(key, text, message, tmux_manager):
            return

    async def on_text_only(text: str, source_msg: Message) -> None:
        """Called when text message has no accompanying forwards."""
        # Full reply-to-resume routing (engine/exec switch + ensure_ready + tmux
        # session switch) lives in the shared helper so every input handler
        # behaves identically. (M4/M5, audit 2026-07-02.)
        bail, target_session_id, tmux_switched = await resolve_reply_routing(
            key,
            source_msg,
            source_msg.reply_to_message,
            session_manager=session_manager,
            topic_config=topic_config,
            tmux_manager=tmux_manager,
            message_queue=message_queue,
        )
        if bail:
            return

        # The queue path attributes the sender inside enqueue_prompt, but the two
        # hot paths below bypass the queue and return early — so they must attribute
        # the injected text themselves, or a second whitelisted person writing
        # mid-turn would reach the engine unattributed. (H4, audit 2026-07-02.)
        # Uses bot_settings (the closure rebinds `settings` to per-topic settings).
        attributed_text = apply_sender_attribution(
            text, source_msg, key, bot_settings, topic_config
        )

        # Streaming: a live turn -> inject (steer between tool calls), bypass
        # the queue. No live turn -> fall through to enqueue, which starts a
        # turn via send_streaming_response. `and` short-circuits so the busy
        # check only runs for streaming-mode topics.
        if topic_config.get_topic(
            key[1]
        ).exec_mode == "streaming" and await send_to_streaming_if_busy(
            key, attributed_text, source_msg, streaming_manager
        ):
            return

        # Tmux with active tail: send directly to CC stdin, bypass queue.
        if await send_to_tmux_if_active(key, attributed_text, source_msg, tmux_manager):
            return

        enqueue_prompt(
            key,
            text,
            source_msg,
            message_queue,
            tmux_manager,
            target_session_id=target_session_id,
            # tmux_switched already consumed the reply target in switch_session;
            # a second reply-context injection would double-reference it.
            inject_reply_if_no_target=not tmux_switched,
            settings=bot_settings,
            topic_config=topic_config,
        )

    # Add to batcher — will wait for forwards or process alone after debounce
    forward_batcher.add_text(key, text, message, on_text_only)
