"""Shared enqueue helper for message handlers (text/voice/photo/forward).

Before this module, each of the four input handlers inlined the same
6-8 lines at the end of their callback: resolve target_session_id →
optionally build+inject reply context → enqueue with tmux-aware
suppress_notification flag. The differences across handlers are in the
reply-resolution strategy (e.g. photo/forward use the batcher's text
reply, text/voice use the message's own reply_to), so those stay in
the handlers. The final enqueue step is invariant and lives here.

Keep this module intentionally small: no orchestration, no ensure_ready,
no tmux-active short-circuit — those decisions belong to each handler
because they have handler-specific quirks (text.py runs switch_session
for reply-to-resume in tmux; voice.py dispatches from a batch snapshot;
photo.py short-circuits on all-failed downloads; forward.py skips
inject_reply_context entirely). Keeping that logic in the handlers
avoids a "mega-dispatch" that needs a policy argument per quirk.

Dependency direction is one-way: handlers → _dispatch → streaming →
services. Do NOT import anything from handlers here — that would
create a cycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.types import Message

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers.streaming import (
    build_reply_context,
    ensure_exec_mode_ready,
    inject_reply_context,
)
from telegram_bot.core.messages import t
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.providers import engine_display_name
from telegram_bot.core.services.sender_attribution import (
    build_sender_prefix,
    resolve_attribution_mode,
)
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import ChannelKey

if TYPE_CHECKING:
    from telegram_bot.core.services.claude import SessionManager

logger = logging.getLogger(__name__)


async def resolve_reply_routing(
    key: ChannelKey,
    source_msg: Message,
    reply_to_message: Message | None,
    *,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
) -> tuple[bool, str | None, bool]:
    """Shared reply-to-resume routing for every input handler.

    Does, in order: (1) provider/exec-mode switch when the reply targets a
    different engine/mode (updates topic_config, kills a stale tmux, clears the
    provider session, notifies); (2) ``ensure_exec_mode_ready``; (3) a tmux
    session switch when tmux is live and the reply points at another session.
    User-facing notices go to ``source_msg.answer``.

    Returns ``(bail, target_session_id, tmux_switched)``:
      - ``bail`` — the caller must ``return`` immediately (a notice was sent);
      - ``target_session_id`` — pass to ``enqueue_prompt`` (None in tmux mode,
        which manages its own session);
      - ``tmux_switched`` — True when step 3 already consumed the reply target,
        so the caller must set ``inject_reply_if_no_target=not tmux_switched``.

    Extracted verbatim from text.py so voice/photo/forward/video_note behave
    identically instead of bailing with tui_session_missing (M4) or silently
    routing a reply into the current tmux session (M5). (audit 2026-07-02.)

    ``reply_to_message`` is passed explicitly because handlers resolve it
    differently (text/voice/video_note use the message's own reply_to; photo/
    forward use the batcher's captured text-reply).
    """
    reply_ref = None
    if reply_to_message is not None:
        reply_ref = session_manager.resolve_reply_reference(reply_to_message.message_id, key)

    if reply_ref is not None:
        settings = topic_config.get_topic(key[1])
        if reply_ref.provider not in {"claude", "codex"}:
            await source_msg.answer(t("ui.tui_session_missing"))
            return True, None, False
        target_exec_mode = reply_ref.exec_mode
        if target_exec_mode is not None and target_exec_mode not in {
            "subprocess",
            "tmux",
            "streaming",
        }:
            await source_msg.answer(t("ui.tui_session_missing"))
            return True, None, False
        provider_changed = reply_ref.provider != settings.engine
        exec_mode_changed = target_exec_mode is not None and target_exec_mode != settings.exec_mode
        if provider_changed or exec_mode_changed:
            if tmux_manager.is_processing(key) or message_queue.is_busy(key):
                await source_msg.answer(t("ui.exec_mode_busy"))
                return True, None, False
            thread_id = key[1]
            if thread_id is None:
                await source_msg.answer(t("ui.engine_not_in_forum"))
                return True, None, False
            if exec_mode_changed and provider_changed:
                assert target_exec_mode is not None
                ok = await topic_config.update_engine_model_exec_mode(
                    thread_id,
                    reply_ref.provider,  # type: ignore[arg-type]
                    None,
                    target_exec_mode,
                )
                if not ok:
                    await source_msg.answer(t("ui.engine_write_failed"))
                    return True, None, False
            elif exec_mode_changed:
                assert target_exec_mode is not None
                ok = await topic_config.update_exec_mode(thread_id, target_exec_mode)
                if not ok:
                    await source_msg.answer(t("ui.exec_mode_write_failed"))
                    return True, None, False
            elif provider_changed:
                ok = await topic_config.update_engine_model(
                    thread_id,
                    reply_ref.provider,  # type: ignore[arg-type]
                    None,
                )
                if not ok:
                    await source_msg.answer(t("ui.engine_write_failed"))
                    return True, None, False
            # Any exec_mode change away from a live tmux session must kill it,
            # else send_to_tmux_if_active would steal the prompt into the now-
            # orphaned tmux instead of routing to the target mode.
            if tmux_manager.is_active(key) and (provider_changed or exec_mode_changed):
                await tmux_manager.kill(key)
            if provider_changed:
                await session_manager.clear_provider_session(key, mark_fresh=False)
            logger.info(
                "reply switched context for %s: %s/%s/%s -> %s/%s/%s",
                key,
                settings.engine,
                settings.model,
                settings.exec_mode,
                reply_ref.provider,
                reply_ref.model,
                reply_ref.exec_mode or settings.exec_mode,
            )
            if provider_changed:
                await source_msg.answer(
                    t("ui.reply_engine_switched", engine=engine_display_name(reply_ref.provider))
                )

    if not await ensure_exec_mode_ready(
        key, topic_config, tmux_manager, session_manager, source_msg
    ):
        return True, None, False

    target_session_id = reply_ref.session_id if reply_ref is not None else None
    tmux_switched = False

    # Tmux mode: switch CC session if the reply targets a different one.
    if tmux_manager.is_active(key) and target_session_id:
        assert reply_ref is not None
        current_sid = tmux_manager.get_session_id(key)
        if current_sid != target_session_id:
            ok = await tmux_manager.switch_session(key, target_session_id, session_manager)
            if not ok:
                await source_msg.answer(t("ui.tui_session_missing"))
                return True, None, False
            tmux_switched = True
            await source_msg.answer(
                t(
                    "ui.session_switched_engine",
                    engine=engine_display_name(reply_ref.provider),
                    sid=target_session_id[:8],
                )
            )
        target_session_id = None  # tmux manages session state internally

    return False, target_session_id, tmux_switched


def apply_sender_attribution(
    prompt: str,
    source_msg: Message,
    key: ChannelKey,
    settings: Settings,
    topic_config: TopicConfig,
) -> str:
    """Prepend the "[Message from: <name>]" line per the resolved mode, or return
    prompt unchanged.

    The single source of attribution logic. enqueue_prompt uses it for the queue
    path; the streaming/tmux hot paths (which bypass the queue and would
    otherwise skip attribution entirely — H4, audit 2026-07-02) call it directly
    on the text they inject.
    """
    mode = resolve_attribution_mode(
        topic_config.get_topic(key[1]).attribute_senders,
        settings.attribute_senders,
    )
    prefix = build_sender_prefix(
        source_msg, mode=mode, allowed_user_count=len(settings.allowed_user_ids)
    )
    return f"{prefix}\n{prompt}" if prefix else prompt


def enqueue_prompt(
    key: ChannelKey,
    prompt: str,
    source_msg: Message,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    *,
    target_session_id: str | None,
    inject_reply_if_no_target: bool,
    settings: Settings | None = None,
    topic_config: TopicConfig | None = None,
) -> None:
    """Final enqueue step shared by text/voice/photo/forward handlers.

    - If `target_session_id` is None AND `inject_reply_if_no_target` is True,
      builds a reply context from source_msg.reply_to_message and injects it
      into the prompt. This covers the case where the user replied to a bot
      message that has no associated session (briefing, reminder).
    - forward.py passes `inject_reply_if_no_target=False`: forwarded batches
      already carry their own structure; injecting a reply quote would
      duplicate the user's intent.
    - When `settings` and `topic_config` are both supplied, prepends a sender
      attribution line ("[Message from: <name>]") per the resolved mode — one
      place that covers every input handler. Applied last so the attribution
      sits at the very top of the final prompt, above any reply context.
    - Always enqueues with `suppress_notification=tmux_manager.is_active(key)`
      because in tmux mode the CC TUI provides its own position feedback
      (thinking placeholder, live buffer) — the queue's "added to position N"
      message is redundant.
    """
    if target_session_id is None and inject_reply_if_no_target:
        reply_context = build_reply_context(source_msg)
        if reply_context:
            prompt = inject_reply_context(prompt, reply_context)
    # Attribution needs both; pass both or neither. Asserting the invariant
    # surfaces a partial call instead of silently skipping attribution.
    assert (settings is None) == (topic_config is None), (
        "enqueue_prompt: pass both settings and topic_config, or neither"
    )
    if settings is not None and topic_config is not None:
        prompt = apply_sender_attribution(prompt, source_msg, key, settings, topic_config)
    logger.info(
        "MSG_TRACE enqueue_prompt channel=%s msg=%d prompt_len=%d target_sid=%s",
        key,
        source_msg.message_id,
        len(prompt),
        target_session_id,
    )
    message_queue.enqueue(
        key,
        prompt,
        source_msg.message_id,
        source_msg,
        target_session_id=target_session_id,
        suppress_notification=tmux_manager.is_active(key),
    )
