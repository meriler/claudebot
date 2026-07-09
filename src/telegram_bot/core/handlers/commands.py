"""Command handlers for bot-owned slash commands except /tui and /tail."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import math
import os
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from aiogram.types.inaccessible_message import InaccessibleMessage

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.keyboards import (
    _MODEL_OPTIONS,
    RESUME_PAGE_SIZE,
    _format_age,
    _format_size,
    engine_keyboard,
    exec_mode_keyboard,
    model_keyboard,
    resume_keyboard,
    stream_mode_keyboard,
    topic_keyboard,
)
from telegram_bot.core.messages import reset_lang_cache, t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.picker_store import PickerState, PickerStore
from telegram_bot.core.services.providers import engine_display_name
from telegram_bot.core.services.resume_listing import (
    SessionEntry,
    _same_cwd,
    get_last_assistant_message,
    list_sessions,
)
from telegram_bot.core.services.streaming_manager import StreamingManager
from telegram_bot.core.services.telegram_utils import send_html_with_fallback
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import (
    _VALID_ENGINES,
    _VALID_EXEC_MODES,
    _VALID_STREAM_MODES,
    TopicConfig,
    resolve_checkpoint_prompt,
)
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config
from telegram_bot.core.types import ChannelKey, channel_key
from telegram_bot.core.utils.telegram_html import split_html_message

logger = logging.getLogger(__name__)


def _exec_mode_label(mode: str) -> str:
    """Human-facing label per exec_mode.

    Raw "subprocess" must never leak into the "Mode: …" toast — the picker
    button text is the contract surface.
    """
    if mode == "subprocess":
        return t("ui.exec_mode_label_subprocess")
    if mode == "streaming":
        return t("ui.exec_mode_label_streaming")
    if mode == "tmux":
        return t("ui.exec_mode_label_tmux")
    return mode


def _exec_mode_picker_caption(mode: str) -> str:
    return t("ui.exec_mode_picker_caption", current=_exec_mode_label(mode))


router = Router(name="commands")


def _resume_caption(
    cwd: Path,
    *,
    page: int,
    total_pages: int,
    entries: tuple[SessionEntry, ...] = (),
    current_session_id: str | None = None,
) -> str:
    safe_cwd = html.escape(str(cwd))
    text = t("ui.resume_picker_caption_hdr", cwd=safe_cwd, page=page + 1, total=total_pages)
    if not entries:
        return text

    blocks: list[str] = []
    start = page * RESUME_PAGE_SIZE
    for idx, entry in enumerate(entries[start : start + RESUME_PAGE_SIZE], start=start):
        provider = engine_display_name(entry.provider)
        preview = html.escape(entry.preview)
        prefix = "✅ " if entry.session_id == current_session_id else ""
        parts = [
            f"{prefix}{idx + 1}. <b>{provider}</b>",
            _format_age(entry.mtime),
            _format_size(entry.size_bytes),
            f"<code>{html.escape(entry.session_id[:8])}</code>",
        ]
        if entry.session_id == current_session_id:
            parts.append(t("ui.resume_current_marker"))
        block_lines = [" · ".join(parts)]
        if preview:
            block_lines.append(f"   {preview}")
        blocks.append("\n".join(block_lines))
    return "\n\n".join([text, *blocks])


@router.message(Command("health"))
async def handle_health(
    message: Message,
    topic_config: TopicConfig | None = None,
    health_state: object | None = None,
    session_manager: SessionManager | None = None,
    outbox: object | None = None,
) -> None:
    """Show bot health: env vars, paths, CC version, onboarding, runtime state."""
    from telegram_bot.core.config import get_settings
    from telegram_bot.core.services.preflight import run_health_checks

    settings = get_settings()
    try:
        results = run_health_checks(settings, topic_config)
    except Exception as exc:
        await message.answer(f"❌ health check crashed: {exc}")
        return

    lines = ["<b>Bot health</b>", ""]
    for r in results:
        marker = "✅" if r.ok else "❌"
        lines.append(f"{marker} <b>{r.name}</b>: {html.escape(r.detail)}")
    failed = sum(1 for r in results if not r.ok)

    from telegram_bot.core.health import HealthState

    if isinstance(health_state, HealthState):
        lines.append("")
        net_marker = "✅" if health_state.telegram_reachable else "⚠️"
        lines.append(
            f"{net_marker} <b>telegram</b>: "
            + (
                "reachable"
                if health_state.telegram_reachable
                else f"unreachable ({health_state.consecutive_failures} consecutive failures)"
            )
        )
        hours, rem = divmod(health_state.uptime_seconds, 3600)
        lines.append(f"⏱ <b>uptime</b>: {hours}h {rem // 60}m")
        if health_state.pool_resets_total:
            lines.append(
                f"♻️ <b>pool resets</b>: {health_state.pool_resets_today} today / "
                f"{health_state.pool_resets_total} total"
            )
    if session_manager is not None:
        active = sum(1 for s in session_manager._sessions.values() if s.process is not None)
        lines.append(f"🧵 <b>active CC processes</b>: {active}")

    from telegram_bot.core.services.outbox import Outbox

    if isinstance(outbox, Outbox) and outbox.size:
        lines.append(f"📬 <b>undelivered responses</b>: {outbox.size} (retrying)")

    lines.append("")
    if failed:
        lines.append(f"<i>{failed} check(s) failed.</i>")
    else:
        lines.append("<i>All checks passed.</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    logger.debug("User %s started the bot", message.from_user and message.from_user.id)
    is_group = message.chat.type == ChatType.SUPERGROUP
    keyboard = topic_keyboard() if is_group else ReplyKeyboardRemove()
    await message.answer(
        text=t("ui.start_welcome"),
        reply_markup=keyboard,
    )


@router.message(Command("language"))
async def handle_language(message: Message) -> None:
    """Show or switch bot UI language for the current process."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    current = os.environ.get("BOT_LANG", "en")
    if current not in {"en", "ru"}:
        current = "en"

    if len(parts) == 1:
        await message.answer(t("ui.language_current", lang=current))
        return

    lang = parts[1].strip().lower()
    if lang not in {"en", "ru"}:
        await message.answer(t("ui.language_invalid"))
        return

    os.environ["BOT_LANG"] = lang
    reset_lang_cache()
    await message.answer(t("ui.language_changed", lang=lang))


async def _reset_channel(
    message: Message,
    key: ChannelKey,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
    bot_settings: Settings,
    streaming_manager: StreamingManager,
) -> None:
    """Unified reset path for /new, /clear, and the "Новый чат" reply button.

    Live tmux → clear_context respawns a fresh TUI immediately.
    Dormant tmux → drop stale state and start a fresh TUI immediately.
    Otherwise → full subprocess reset + ui.new_session.

    When checkpoint-on-reset is enabled (per-topic, or the bot-wide default),
    the engine is asked to write a background checkpoint of the work just done
    before its context is dropped — so a forgotten manual checkpoint never
    loses the session. tmux parks the live TUI aside; subprocess fires a
    detached `--resume` run. Both are headless (no Telegram output).
    """
    settings = topic_config.get_topic(key[1])
    checkpoint_prompt = resolve_checkpoint_prompt(
        settings,
        global_enabled=bot_settings.checkpoint_on_reset,
        global_prompt=bot_settings.checkpoint_prompt or t("ui.checkpoint_prompt"),
    )
    # Streaming: hard-kill the live process so the next message starts fresh
    # (or resumes from the checkpoint). No-op if no streaming session. The
    # checkpoint below still runs on the persisted session_id via a one-shot
    # --resume, independent of this live process.
    await streaming_manager.kill(key)
    if tmux_manager.is_active(key):
        # clear_context respawns the tmux session; _spawn_tmux can fail
        # (tmux server shutdown race, readiness timeout, etc.). Without a
        # catch here the RuntimeError reaches aiogram's error middleware
        # and the user sees nothing — "Новый чат" becomes a silent button.
        try:
            reset_live = await tmux_manager.clear_context(
                key, session_manager, checkpoint_prompt=checkpoint_prompt
            )
        except RuntimeError:
            logger.warning("clear_context failed for %s", key, exc_info=True)
            await message.answer(t("ui.reset_failed"))
            return
        if reset_live:
            session = session_manager._get_session(key)
            await message.answer(
                t("ui.tmux_started_engine", engine=engine_display_name(session.engine))
            )
            return
        logger.info("clear_context found no live tmux for %s; starting fresh", key)

    if settings.exec_mode == "tmux":
        await tmux_manager.kill(key)
        forward_batcher.clear(key)
        await message_queue.clear(key)
        await session_manager.kill_session(key)
        session = session_manager._get_session(key)
        try:
            await tmux_manager.start_session(
                key,
                mode=session.mode,
                cwd=session.cwd,
                mcp_config=session.mcp_config,
                chat_id=session.chat_id,
                session_manager=session_manager,
                provider=session.engine,
                model=session.model,
            )
        except RuntimeError:
            logger.warning("fresh tmux start failed for %s", key, exc_info=True)
            await message.answer(t("ui.reset_failed"))
            return
        await message.answer(
            t("ui.tmux_started_engine", engine=engine_display_name(session.engine))
        )
        return

    # subprocess mode: fire a detached `--resume` checkpoint on the old
    # session_id before kill_session wipes the mapping. Best-effort and
    # headless — its output is discarded, not streamed to Telegram.
    if checkpoint_prompt:
        await session_manager.spawn_background_checkpoint(key, checkpoint_prompt)
    forward_batcher.clear(key)
    await message_queue.clear(key)
    await session_manager.kill_session(key)
    await message.answer(t("ui.new_session"))


@router.message(Command("new"))
async def handle_new(
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
    logger.debug("User %s requested new session", message.from_user and message.from_user.id)
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


@router.message(Command("clear"))
async def handle_clear(
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
    logger.debug("User %s requested clear", message.from_user and message.from_user.id)
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


@router.message(Command("cancel"))
async def handle_cancel_command(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    streaming_manager: StreamingManager,
) -> None:
    key = channel_key(message)
    tmux_acted = tmux_manager.is_active(key)
    if tmux_acted:
        await tmux_manager.cancel(key)
    streaming_acted = streaming_manager.is_active(key)
    if streaming_acted:
        await streaming_manager.cancel(key)
    cancelled = await message_queue.cancel(key)
    if cancelled or tmux_acted or streaming_acted:
        logger.debug("User cancelled CC processing (command) for %s", key)
        await message.answer(t("ui.cancelled"))
    else:
        await message.answer(t("ui.nothing_to_cancel"))


@router.message(Command("kill"))
async def handle_kill(
    message: Message, tmux_manager: TmuxManager, streaming_manager: StreamingManager
) -> None:
    """Kill the live tmux OR streaming session in the current topic."""
    key = channel_key(message)
    tmux_active = tmux_manager.is_active(key)
    streaming_active = streaming_manager.is_active(key)
    if not tmux_active and not streaming_active:
        await message.answer(t("ui.tmux_not_active"))
        return
    logger.debug(
        "User %s killed session for %s (tmux=%s, streaming=%s)",
        message.from_user and message.from_user.id,
        key,
        tmux_active,
        streaming_active,
    )
    if tmux_active:
        await tmux_manager.kill(key)
    if streaming_active:
        await streaming_manager.kill(key)
    await message.answer(t("ui.tmux_killed"))


@router.message(Command("resume"))
async def handle_resume(
    message: Message,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    picker_store: PickerStore,
    bot_defaults: BotDefaults,
) -> None:
    """Open server-side picker with resumable Claude/Codex sessions."""
    key = channel_key(message)
    if key[1] is None:
        await message.answer(t("ui.resume_not_in_forum"))
        return

    runtime = resolve_topic_runtime_config(topic_config.get_topic(key[1]), bot_defaults)
    entries = tuple(await asyncio.to_thread(list_sessions, runtime.cwd))
    if not entries:
        await message.answer(t("ui.resume_no_sessions"))
        return

    token = picker_store.put(
        PickerState(
            chat_id=key[0],
            thread_id=key[1],
            cwd=runtime.cwd,
            engine=runtime.engine,
            entries=entries,
            created_at=time.time(),
        )
    )
    total_pages = max(1, math.ceil(len(entries) / 8))
    current_session_id = tmux_manager.get_active_session_id(key)
    await message.answer(
        _resume_caption(
            runtime.cwd,
            page=0,
            total_pages=total_pages,
            entries=entries,
            current_session_id=current_session_id,
        ),
        reply_markup=resume_keyboard(
            entries,
            page=0,
            current_session_id=current_session_id,
            token=token,
        ),
        parse_mode="HTML",
    )


def _callback_key(callback: CallbackQuery) -> ChannelKey | None:
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        return None
    return (callback.message.chat.id, callback.message.message_thread_id)


async def _stale_resume_picker(callback: CallbackQuery) -> None:
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(t("ui.resume_picker_stale"), reply_markup=None)
    await callback.answer(t("ui.resume_picker_stale"), show_alert=True)


async def _answer_callback_safely(
    callback: CallbackQuery, text: str | None = None, *, show_alert: bool = False
) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer(text, show_alert=show_alert)


async def _replay_last_assistant_message(
    message: Message,
    entry: SessionEntry,
    key: ChannelKey,
    session_manager: SessionManager,
) -> None:
    content = await asyncio.to_thread(
        get_last_assistant_message,
        entry.provider,
        entry.transcript_path,
    )
    if not content:
        return

    for chunk in split_html_message(content):

        async def _send_html(c: str = chunk) -> object:
            return await message.answer(c, parse_mode="HTML")

        async def _send_plain(c: str = chunk) -> object:
            return await message.answer(c)

        outcome = await send_html_with_fallback(
            send_html=_send_html,
            send_plain=_send_plain,
            label=f"resume replay {key}",
        )
        if outcome.message_id is not None:
            session_manager.record_message(
                outcome.message_id,
                entry.session_id,
                key,
                provider=entry.provider,
                model=None,
            )
        if outcome.fatal:
            return


@router.callback_query(F.data.startswith("rs:p:"))
async def on_resume_page(
    callback: CallbackQuery,
    picker_store: PickerStore,
    tmux_manager: TmuxManager,
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _stale_resume_picker(callback)
        return
    _, _, token, raw_page = parts
    state = picker_store.get(token)
    key = _callback_key(callback)
    if state is None or key != (state.chat_id, state.thread_id):
        await _stale_resume_picker(callback)
        return
    try:
        page = int(raw_page)
    except ValueError:
        await _stale_resume_picker(callback)
        return
    total_pages = max(1, math.ceil(len(state.entries) / 8))
    page = max(0, min(page, total_pages - 1))
    try:
        await callback.message.edit_text(
            _resume_caption(
                state.cwd,
                page=page,
                total_pages=total_pages,
                entries=state.entries,
                current_session_id=tmux_manager.get_active_session_id(key),
            ),
            reply_markup=resume_keyboard(
                state.entries,
                page=page,
                current_session_id=tmux_manager.get_active_session_id(key),
                token=token,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("rs:s:"))
async def on_resume_pick(
    callback: CallbackQuery,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    picker_store: PickerStore,
    bot_defaults: BotDefaults,
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _stale_resume_picker(callback)
        return
    _, _, token, raw_idx = parts
    state = picker_store.get(token)
    key = _callback_key(callback)
    if state is None or key != (state.chat_id, state.thread_id):
        await _stale_resume_picker(callback)
        return
    runtime = resolve_topic_runtime_config(topic_config.get_topic(key[1]), bot_defaults)
    if not _same_cwd(runtime.cwd, state.cwd):
        await _stale_resume_picker(callback)
        return
    try:
        idx = int(raw_idx)
    except ValueError:
        await _stale_resume_picker(callback)
        return
    if idx < 0:
        await _stale_resume_picker(callback)
        return
    try:
        entry = state.entries[idx]
    except IndexError:
        await _stale_resume_picker(callback)
        return

    await _answer_callback_safely(callback, t("ui.resume_starting"))
    result = await tmux_manager.switch_or_start_session(
        key,
        entry.session_id,
        entry.provider,
        entry.transcript_path,
        session_manager=session_manager,
        topic_config=topic_config,
        defaults=bot_defaults,
    )
    if result.kind == "target_missing":
        await callback.message.edit_text(t("ui.resume_target_missing"), reply_markup=None)
        return
    if result.kind in {"invalid_id", "spawn_failed", "config_write_failed"}:
        key_name = (
            "ui.resume_spawn_failed_engine_changed"
            if result.kind == "spawn_failed" and result.engine_changed
            else f"ui.resume_{result.kind}"
        )
        await callback.message.edit_text(
            t(key_name, engine=entry.provider),
            reply_markup=None,
        )
        return

    picker_store.drop(token)
    if result.kind == "already_on_it":
        await callback.message.edit_text(t("ui.resume_already_on_it"), reply_markup=None)
        await _replay_last_assistant_message(callback.message, entry, key, session_manager)
        return

    message_key = "ui.resume_switched" if result.kind == "switched" else "ui.resume_started"
    text = t(message_key, sid=entry.session_id[:8])
    if result.engine_changed:
        text += "\n" + t("ui.resume_engine_switched", engine=entry.provider)
    await callback.message.edit_text(text, reply_markup=None, parse_mode="HTML")
    await _replay_last_assistant_message(callback.message, entry, key, session_manager)


@router.callback_query(F.data.startswith("rs:cancel:"))
async def on_resume_cancel(callback: CallbackQuery, picker_store: PickerStore) -> None:
    if callback.data is not None:
        picker_store.drop(callback.data.rsplit(":", 1)[-1])
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(t("ui.resume_cancelled"), reply_markup=None)
    await callback.answer()


@router.message(Command("stream"))
async def handle_stream_mode(message: Message, topic_config: TopicConfig) -> None:
    """Show a 3-button picker to switch stream_mode for the current topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.stream_mode_not_in_forum"))
        return
    topic = topic_config.get_topic(thread_id)
    current = topic.stream_mode
    await message.answer(
        t("ui.stream_mode_picker_caption", current=current),
        reply_markup=stream_mode_keyboard(current, topic.stream_thinking),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("stream_mode:"))
async def on_stream_mode_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
) -> None:
    """Apply a new stream_mode for the topic the picker was posted in."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    # InaccessibleMessage has no thread_id/edit methods — bail out if the
    # picker message is no longer reachable (e.g. deleted, chat lost).
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    _, _, mode = callback.data.partition(":")
    if mode not in _VALID_STREAM_MODES:
        await callback.answer(t("ui.stream_mode_invalid"), show_alert=True)
        return

    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(
            t("ui.stream_mode_not_in_forum"),
            show_alert=True,
        )
        return

    ok = await topic_config.update_stream_mode(thread_id, mode)  # type: ignore[arg-type]
    if not ok:
        await callback.answer(t("ui.stream_mode_write_failed"), show_alert=True)
        return

    thinking_on = topic_config.get_topic(thread_id).stream_thinking
    # Refresh both caption and keyboard so the visible current value matches the checkmark.
    try:
        await callback.message.edit_text(
            t("ui.stream_mode_picker_caption", current=mode),
            reply_markup=stream_mode_keyboard(mode, thinking_on),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh stream_mode picker", exc_info=True)
    await callback.answer(t("ui.stream_mode_changed", mode=mode))


@router.callback_query(F.data == "stream_thinking:toggle")
async def on_stream_thinking_toggle(
    callback: CallbackQuery,
    topic_config: TopicConfig,
) -> None:
    """Flip the reasoning-streaming toggle for the current topic (live+ only)."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.stream_mode_not_in_forum"), show_alert=True)
        return

    topic = topic_config.get_topic(thread_id)
    new_state = not topic.stream_thinking
    ok = await topic_config.update_stream_thinking(thread_id, new_state)
    if not ok:
        await callback.answer(t("ui.stream_mode_write_failed"), show_alert=True)
        return

    try:
        await callback.message.edit_text(
            t("ui.stream_mode_picker_caption", current=topic.stream_mode),
            reply_markup=stream_mode_keyboard(topic.stream_mode, new_state),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh stream_mode picker", exc_info=True)
    await callback.answer(
        t("ui.stream_thinking_changed", state=t("ui.state_on" if new_state else "ui.state_off"))
    )


@router.message(Command("mode"))
async def handle_mode_command(message: Message, topic_config: TopicConfig) -> None:
    """Show a 2-button picker to switch exec_mode for the current topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.exec_mode_not_in_forum"))
        return
    current = topic_config.get_topic(thread_id).exec_mode
    await message.answer(
        _exec_mode_picker_caption(current),
        reply_markup=exec_mode_keyboard(current),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("exec_mode:"))
async def on_exec_mode_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    streaming_manager: StreamingManager,
) -> None:
    """Apply a new exec_mode for the topic the picker was posted in.

    Order matters: busy-check precedes any side-effect, and tmux.kill strictly
    precedes the config write on tmux→subprocess (Decision 2 — if we wrote
    first and crashed, the next message would race a still-running tmux
    session against a fresh subprocess under the new mode).
    """
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    # InaccessibleMessage has no thread_id / edit methods — bail out if the
    # picker message is no longer reachable.
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, new_mode = callback.data.partition(":")
    # Re-validate against the whitelist even though the keyboard only emits
    # two canonical values — raw callback.data is user-controlled.
    if new_mode not in _VALID_EXEC_MODES:
        await callback.answer(t("ui.exec_mode_invalid"), show_alert=True)
        return

    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.exec_mode_not_in_forum"), show_alert=True)
        return

    key = (callback.message.chat.id, thread_id)
    previous_mode = topic_config.get_topic(thread_id).exec_mode

    if new_mode == previous_mode:
        await callback.answer(t("ui.exec_mode_already", mode=_exec_mode_label(new_mode)))
        return

    # Busy-check covers all channels: tmux's processing flag, the
    # subprocess-path MessageQueue (lock held OR items pending), AND a live
    # streaming turn. Either way we refuse the switch without touching state.
    if (
        tmux_manager.is_processing(key)
        or message_queue.is_busy(key)
        or streaming_manager.is_busy(key)
    ):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    # Leaving a mode kills its live process first, then we persist. Reverse
    # order would orphan the old session if the write fails.
    if previous_mode == "tmux" and new_mode == "subprocess":
        await tmux_manager.kill(key)
    if previous_mode == "streaming":
        await streaming_manager.kill(key)

    ok = await topic_config.update_exec_mode(thread_id, new_mode)
    if not ok:
        await callback.answer(t("ui.exec_mode_write_failed"), show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else None
    logger.info(
        "exec_mode switched: user_id=%s thread_id=%s previous_mode=%s new_mode=%s",
        user_id,
        thread_id,
        previous_mode,
        new_mode,
    )

    # Refresh both caption and keyboard so the visible current value matches the checkmark.
    try:
        await callback.message.edit_text(
            _exec_mode_picker_caption(new_mode),
            reply_markup=exec_mode_keyboard(new_mode),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh exec_mode picker", exc_info=True)
    await callback.answer(t("ui.exec_mode_changed", mode=_exec_mode_label(new_mode)))


@router.message(Command("engine"))
async def handle_engine_command(message: Message, topic_config: TopicConfig) -> None:
    """Show provider engine picker for the current forum topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.engine_not_in_forum"))
        return
    settings = topic_config.get_topic(thread_id)
    await message.answer(
        t(
            "ui.engine_picker_caption",
            engine=engine_display_name(settings.engine),
        ),
        reply_markup=engine_keyboard(settings.engine),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("engine:"))
async def on_engine_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    session_manager: SessionManager,
    streaming_manager: StreamingManager,
) -> None:
    """Apply provider engine changes for the picker topic."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, raw_value = callback.data.partition(":")
    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.engine_not_in_forum"), show_alert=True)
        return
    key = (callback.message.chat.id, thread_id)
    current = topic_config.get_topic(thread_id)

    if (
        tmux_manager.is_processing(key)
        or message_queue.is_busy(key)
        or streaming_manager.is_busy(key)
    ):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    if raw_value not in _VALID_ENGINES:
        await callback.answer(t("ui.engine_invalid"), show_alert=True)
        return
    new_engine = raw_value

    if new_engine == current.engine:
        await callback.answer(t("ui.engine_already"))
        return

    ok = await topic_config.update_engine_model(thread_id, new_engine, None)  # type: ignore[arg-type]
    if not ok:
        await callback.answer(t("ui.engine_write_failed"), show_alert=True)
        return

    if tmux_manager.is_active(key):
        await tmux_manager.kill(key)
    # Streaming holds the old engine in its live process — kill so the next
    # message respawns with the new engine.
    if streaming_manager.is_active(key):
        await streaming_manager.kill(key)
    await session_manager.clear_provider_session(key)

    logger.info(
        "engine switched: user_id=%s thread_id=%s previous=%s new=%s model=%s",
        callback.from_user.id if callback.from_user else None,
        thread_id,
        current.engine,
        new_engine,
        current.model,
    )
    engine_name = engine_display_name(new_engine)
    try:
        await callback.message.edit_text(
            t("ui.engine_picker_caption", engine=engine_name),
            reply_markup=engine_keyboard(new_engine),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh engine picker", exc_info=True)
    await callback.answer(t("ui.engine_changed", engine=engine_name))
    await callback.message.answer(t("ui.engine_changed_new_session", engine=engine_name))


def _model_display(model_id: str | None) -> str:
    for mid, label in _MODEL_OPTIONS:
        if mid == model_id:
            return label
    return model_id or "default"


@router.message(Command("model"))
async def handle_model_command(message: Message, topic_config: TopicConfig) -> None:
    """Show model picker for the current forum topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.model_not_in_forum"))
        return
    settings = topic_config.get_topic(thread_id)
    current = settings.model
    label = _model_display(current)
    await message.answer(
        t("ui.model_picker_caption", label=html.escape(label)),
        reply_markup=model_keyboard(current),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("model:"))
async def on_model_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    session_manager: SessionManager,
    streaming_manager: StreamingManager,
) -> None:
    """Apply model change for the picker topic."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, new_model = callback.data.partition(":")
    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.model_not_in_forum"), show_alert=True)
        return
    key = (callback.message.chat.id, thread_id)

    if (
        tmux_manager.is_processing(key)
        or message_queue.is_busy(key)
        or streaming_manager.is_busy(key)
    ):
        await callback.answer(t("ui.busy_wait"), show_alert=True)
        return

    valid_ids = {mid for mid, _ in _MODEL_OPTIONS}
    if new_model not in valid_ids:
        await callback.answer(t("ui.model_invalid"), show_alert=True)
        return

    current = topic_config.get_topic(thread_id)
    if new_model == current.model:
        await callback.answer(t("ui.model_already"))
        return

    ok = await topic_config.update_model(thread_id, new_model)
    if not ok:
        await callback.answer(t("ui.model_write_failed"), show_alert=True)
        return

    # A model change needs a fresh CC process (--model is a launch flag, not a
    # runtime switch). streaming/subprocess resume the same transcript with
    # --resume, so we kill the live process but KEEP the session_id → the next
    # message continues the same conversation on the new model. tmux loses its
    # session_id on kill, so there the model applies on a fresh session.
    if current.exec_mode == "tmux":
        if tmux_manager.is_active(key):
            await tmux_manager.kill(key)
        await session_manager.clear_provider_session(key)
        continuity = False
    else:
        if streaming_manager.is_active(key):
            await streaming_manager.kill(key)
        continuity = True

    label = _model_display(new_model)
    logger.info(
        "model switched: user_id=%s thread_id=%s previous=%s new=%s exec_mode=%s continuity=%s",
        callback.from_user.id if callback.from_user else None,
        thread_id,
        current.model,
        new_model,
        current.exec_mode,
        continuity,
    )
    try:
        await callback.message.edit_text(
            t("ui.model_picker_caption", label=html.escape(label)),
            reply_markup=model_keyboard(new_model),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh model picker", exc_info=True)
    await callback.answer(t("ui.model_changed", label=label))
    tail = t("ui.model_tail_continuity") if continuity else t("ui.model_tail_new_session")
    await callback.message.answer(
        t("ui.model_changed_note", label=html.escape(label), tail=tail),
        parse_mode="HTML",
    )


def _sysprompt_apply_keyboard(chat_id: int, thread_id: int | None) -> InlineKeyboardMarkup:
    """Single inline button that resets the context's session so a freshly
    edited system prompt takes effect (CC applies --append-system-prompt on a
    new session only)."""
    tid = "none" if thread_id is None else str(thread_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("ui.sysprompt_apply_btn"),
                    callback_data=f"sysprompt_apply:{chat_id}:{tid}",
                )
            ]
        ]
    )


@router.message(Command("sysprompt"))
async def handle_sysprompt_command(
    message: Message,
    command: CommandObject,
    topic_config: TopicConfig,
) -> None:
    """Set / show / reset the custom system prompt for the current context.

    Context: a forum topic (thread_id set) stores it in topics[id].system_prompt;
    a DM / topicless chat (thread_id None) stores it under chat_prompts[chat_id].
    The prompt is APPENDED on top of Klava's persona (see _topic_system_prompt).

    Usage:
      /sysprompt <текст>   — задать (многострочно или ответом на сообщение)
      /sysprompt           — показать текущий
      /sysprompt reset     — сбросить на дефолтную персону
    """
    chat_id, thread_id = channel_key(message)
    in_topic = thread_id is not None

    raw = (command.args or "").strip()
    if not raw and message.reply_to_message is not None:
        reply = message.reply_to_message
        raw = (reply.text or reply.caption or "").strip()

    scope = t("ui.sysprompt_scope_topic") if in_topic else t("ui.sysprompt_scope_chat")

    # Show mode — empty payload and no reply text.
    if not raw:
        current = (
            topic_config.get_topic(thread_id).system_prompt
            if in_topic
            else topic_config.get_chat_prompt(chat_id)
        )
        if current:
            # Keep the whole reply well under Telegram's 4096-char limit — a
            # prompt set via a long reply could otherwise overflow one message.
            preview = (
                current if len(current) <= 3500 else current[:3500] + t("ui.sysprompt_truncated")
            )
            body = t(
                "ui.sysprompt_show_current",
                scope=scope,
                preview=html.escape(preview),
            )
        else:
            body = t("ui.sysprompt_not_set", scope=scope)
        await message.answer(body, parse_mode="HTML")
        return

    is_reset = raw.lower() == "reset"
    new_value: str | None = None if is_reset else raw

    if thread_id is not None:
        ok = await topic_config.update_system_prompt(thread_id, new_value)
    else:
        ok = await topic_config.update_chat_prompt(chat_id, new_value)

    if not ok:
        await message.answer(t("ui.sysprompt_save_failed"))
        return

    logger.info(
        "sysprompt %s: user_id=%s chat_id=%s thread_id=%s",
        "reset" if is_reset else "set",
        message.from_user.id if message.from_user else None,
        chat_id,
        thread_id,
    )

    if is_reset:
        head = t("ui.sysprompt_reset_done", scope=scope)
    else:
        head = t("ui.sysprompt_saved", scope=scope)
    body = f"{head}\n{t('ui.sysprompt_applies_next_session')}"
    # The custom prompt is wired only into the Claude CLI (--append-system-prompt).
    # The Codex engine has no equivalent injection, so warn instead of silently
    # ignoring it for codex topics.
    if thread_id is not None and topic_config.get_topic(thread_id).engine == "codex":
        body += "\n\n" + t("ui.sysprompt_codex_warning")
    await message.answer(
        body,
        reply_markup=_sysprompt_apply_keyboard(chat_id, thread_id),
    )


@router.callback_query(F.data.startswith("sysprompt_apply:"))
async def on_sysprompt_apply_click(
    callback: CallbackQuery,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    session_manager: SessionManager,
    streaming_manager: StreamingManager,
) -> None:
    """Apply an edited system prompt now by resetting the context's session."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        chat_id = int(parts[1])
        thread_id = None if parts[2] == "none" else int(parts[2])
    except ValueError:
        await callback.answer()
        return
    key: ChannelKey = (chat_id, thread_id)

    # The button is always sent into the chat it targets, so its embedded key
    # must match where the click happened. A mismatch means a forged/stale
    # callback aimed at another chat — refuse to reset a session elsewhere.
    expected = channel_key(callback.message)
    if expected != key:
        await callback.answer()
        return

    if (
        tmux_manager.is_processing(key)
        or message_queue.is_busy(key)
        or streaming_manager.is_busy(key)
    ):
        await callback.answer(t("ui.busy_wait"), show_alert=True)
        return

    # Kill the live streaming process too, not just tmux. --append-system-prompt
    # only takes effect on a FRESH session, so clear_provider_session below wipes
    # the session_id and the next message starts a new process with the edited
    # prompt. Without killing the streaming session, streaming exec_mode (the
    # default for most topics) would keep answering on the old prompt — the
    # button silently did nothing. (H3, audit 2026-07-02.)
    if streaming_manager.is_active(key):
        await streaming_manager.kill(key)
    if tmux_manager.is_active(key):
        await tmux_manager.kill(key)
    await session_manager.clear_provider_session(key)

    logger.info("sysprompt applied (session reset): chat_id=%s thread_id=%s", chat_id, thread_id)
    await callback.answer(t("ui.sysprompt_applied_toast"))
    with contextlib.suppress(Exception):
        await callback.message.answer(t("ui.sysprompt_applied"))
