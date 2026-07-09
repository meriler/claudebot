"""Entry point for the public Telegram-Claude-Code bot."""

import argparse
import asyncio
import inspect
import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from telegram_bot.core.config import get_settings
from telegram_bot.core.handlers.cancel import router as cancel_router
from telegram_bot.core.handlers.commands import router as commands_router
from telegram_bot.core.handlers.forum_topic import router as forum_topic_router
from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.handlers.forward import router as forward_router
from telegram_bot.core.handlers.mode import router as mode_router
from telegram_bot.core.handlers.photo import cleanup_old_tmp_files, ensure_tmp_dir
from telegram_bot.core.handlers.photo import router as photo_router
from telegram_bot.core.handlers.streaming import send_streaming_response
from telegram_bot.core.handlers.tail import router as tail_router
from telegram_bot.core.handlers.text import router as text_router
from telegram_bot.core.handlers.unsupported import router as unsupported_router
from telegram_bot.core.handlers.video_note import router as video_note_router
from telegram_bot.core.handlers.voice import router as voice_router
from telegram_bot.core.health import (
    HealthState,
    run_healthcheck_loop,
    run_heartbeat_loop,
    send_shutdown_heartbeat,
)
from telegram_bot.core.keyboards import topic_keyboard
from telegram_bot.core.messages import reset_lang_cache, t
from telegram_bot.core.middleware.auth import AuthMiddleware
from telegram_bot.core.services.bot_commands import setup_bot_commands
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.media_sender import MediaSender
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.outbox import Outbox
from telegram_bot.core.services.picker_store import PickerStore
from telegram_bot.core.services.preflight import PreflightError, run_startup_preflight
from telegram_bot.core.services.streaming_manager import StreamingManager
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults
from telegram_bot.core.services.transcriber import Transcriber
from telegram_bot.core.services.usage_tracker import UsageTracker
from telegram_bot.core.types import ChannelKey
from telegram_bot.core.utils.file_lock import BotInstanceLock, BotInstanceLockError
from telegram_bot.core.utils.log_setup import setup_logging

logger = logging.getLogger(__name__)


async def _safe_shutdown_step(label: str, fn: Callable[[], object]) -> None:
    """Run one shutdown step, isolating its failure from the rest.

    H7 (audit 2026-07-02): the shutdown was a flat await-sequence, so a failure
    in an early step (e.g. usage_tracker.stop_all hitting TelegramNetworkError)
    skipped everything after it — including the critical
    session_manager.save_mapping(). Each step now logs-and-continues so
    persistence still runs. `fn` may be sync or return an awaitable.
    """
    try:
        result = fn()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning("Shutdown step %r failed, continuing", label, exc_info=True)


async def process_queue_item(
    channel_key: ChannelKey,
    prompt: str,
    source_messages: list[Message],
    target_session_id: str | None,
    *,
    bot: Bot,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    media_sender: MediaSender,
    topic_config: TopicConfig,
    streaming_manager: StreamingManager,
    usage_tracker: UsageTracker | None = None,
    outbox: Outbox | None = None,
) -> None:
    """Send a queued prompt to CC; on session change, notify the user."""
    old_session_id = session_manager.get_current_session_id(channel_key)

    # After kill/reset, ignore reply-to-resume on the next message.
    if session_manager.consume_fresh_start(channel_key):
        target_session_id = None

    if target_session_id is not None:
        await session_manager.override_session(channel_key, target_session_id)

    session_changed = target_session_id is not None and target_session_id != old_session_id
    if session_changed and target_session_id:
        chat_id, thread_id = channel_key
        notification = t("ui.session_switched", sid=target_session_id[:8])
        try:
            await bot.send_message(
                chat_id,
                notification,
                reply_markup=topic_keyboard(),
                message_thread_id=thread_id,
            )
        except TelegramBadRequest:
            logger.warning(
                "Failed to send session switch notification (stale thread_id=%s)",
                thread_id,
                exc_info=True,
            )

    reply_message = source_messages[-1] if source_messages else None
    if reply_message is None:
        return
    # Pass streaming_manager ONLY for streaming-mode topics — its presence is the
    # signal send_streaming_response uses to route to the persistent process.
    # Guard: the streaming path (build_streaming_argv) only knows how to launch
    # `claude`. A topic on the default streaming exec_mode but with engine=codex
    # would otherwise route here and SILENTLY run claude instead of codex. Route
    # non-claude engines to the one-shot subprocess path, which is engine-aware.
    topic = topic_config.get_topic(channel_key[1])
    streaming_ok = topic.exec_mode == "streaming" and topic.engine == "claude"
    sm_arg = streaming_manager if streaming_ok else None
    await send_streaming_response(
        reply_message,
        session_manager,
        channel_key,
        prompt,
        tmux_manager=tmux_manager,
        topic_config=topic_config,
        media_sender=media_sender,
        usage_tracker=usage_tracker,
        outbox=outbox,
        streaming_manager=sm_arg,
    )


async def _start(preflight_only: bool = False) -> None:
    settings = get_settings()
    setup_logging(settings.log_file)

    # Seed the localization module from Settings (.env). messages._get_lang()
    # reads os.environ["BOT_LANG"], which the launchd plist does NOT set — so
    # without this seed the whole UI silently falls back to English after every
    # restart (BOT_LANG=ru in .env reaches Settings but not os.environ). The
    # runtime /language command still overrides os.environ live. (M9, audit
    # 2026-07-02.)
    os.environ["BOT_LANG"] = settings.bot_lang
    reset_lang_cache()

    # Preflight checks — fail fast before any side effects (bot creation,
    # MCP wiring, tmux state restore). Reads topic_config to be engine-aware.
    topic_config = TopicConfig(settings.topic_config_path, settings.project_root)
    try:
        run_startup_preflight(settings, topic_config)
    except PreflightError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if preflight_only:
        logger.info("preflight-only mode: all checks passed, exiting")
        return

    # Same-host single-instance lock — prevents nohup + systemd duplicates,
    # second IDE run, etc. Does NOT protect against cross-host duplicates
    # (different hosts should use different bot tokens).
    bot_lock = BotInstanceLock(settings.telegram_bot_token, runtime_dir=Path.home() / ".claude")
    try:
        bot_lock.acquire()
    except BotInstanceLockError as exc:
        logger.error(str(exc))
        sys.exit(1)

    bot = Bot(token=settings.telegram_bot_token)
    try:
        await setup_bot_commands(bot)
    except Exception:
        logger.warning("Failed to set Telegram bot commands", exc_info=True)

    tmux_manager = TmuxManager(
        sessions_dir=Path(settings.project_root) / settings.tmux_sessions_dir,
    )
    tmux_manager.wire_live_buffer(bot=bot, topic_config=topic_config)
    usage_tracker = UsageTracker(
        bot,
        update_interval=settings.usage_pin_update_interval_sec,
        pins_path=str(Path(settings.session_mapping_path).with_name("usage_pins.json")),
    )
    tmux_manager.wire_usage_tracker(usage_tracker=usage_tracker, settings=settings)
    tmux_manager.restore_all()
    # Kill any checkpoint sessions orphaned by a restart mid-checkpoint —
    # they are intentionally not persisted, so nothing else would reap them.
    await tmux_manager.reap_orphan_checkpoint_sessions()
    # Background sentinels: idle-modal alerts + stuck-tail detection. Were
    # implemented but never started until 2026-06-11 — alerts silently off.
    tmux_manager.start_modal_watchdog()
    tmux_manager.start_transcript_watchdog()
    session_manager = SessionManager(settings, topic_config=topic_config)
    streaming_manager = StreamingManager(session_manager, settings)
    streaming_manager.start_reaper()  # auto-kill idle streaming sessions (free RAM)
    media_sender = MediaSender(bot)
    outbox = Outbox(bot, Path(settings.session_mapping_path).with_name("outbox.json"))
    outbox.start()  # deliver anything left over from before the restart
    transcriber = Transcriber(settings)
    forward_batcher = ForwardBatcher(bot=bot, transcriber=transcriber)

    async def _process_queue_item(
        channel_key: ChannelKey,
        prompt: str,
        source_messages: list[Message],
        target_session_id: str | None,
    ) -> None:
        await process_queue_item(
            channel_key,
            prompt,
            source_messages,
            target_session_id,
            bot=bot,
            session_manager=session_manager,
            tmux_manager=tmux_manager,
            media_sender=media_sender,
            topic_config=topic_config,
            streaming_manager=streaming_manager,
            usage_tracker=usage_tracker,
            outbox=outbox,
        )

    message_queue = MessageQueue(
        bot,
        session_manager,
        _process_queue_item,
        startup_batch_window=settings.startup_batch_window_sec,
        preempt_idle_sec=settings.cc_preempt_idle_sec,
    )

    dp = Dispatcher()
    auth = AuthMiddleware(
        allowed_user_ids=settings.allowed_user_ids,
        unauthorized_reply=settings.unauthorized_reply,
        reply_cooldown_sec=settings.unauthorized_reply_cooldown_sec,
    )
    # Register at the update level so auth covers EVERY update type (including
    # any future handler on a new type), not just message/callback_query. The
    # middleware unwraps Update.event internally. (S3, audit 2026-07-02.)
    dp.update.outer_middleware(auth)
    dp.message.filter(F.chat.type.in_({ChatType.PRIVATE, ChatType.SUPERGROUP}))

    # Order: commands -> cancel -> mode -> forward -> voice -> photo -> text
    # Forward BEFORE voice/photo so forwarded media is batched, not handled directly.
    # forum_topic_router runs first so topic_config.json is updated BEFORE
    # any text/forward handler tries to read mode/cwd for the new thread.
    dp.include_router(forum_topic_router)
    dp.include_router(commands_router)
    dp.include_router(cancel_router)
    dp.include_router(mode_router)
    dp.include_router(forward_router)
    dp.include_router(voice_router)
    dp.include_router(video_note_router)
    dp.include_router(photo_router)
    dp.include_router(tail_router)
    dp.include_router(text_router)
    dp.include_router(unsupported_router)  # LAST: catch-all for unsupported types

    dp["session_manager"] = session_manager
    dp["transcriber"] = transcriber
    dp["forward_batcher"] = forward_batcher
    dp["message_queue"] = message_queue
    dp["queue"] = message_queue
    dp["settings"] = settings
    dp["topic_config"] = topic_config
    dp["tmux_manager"] = tmux_manager
    dp["streaming_manager"] = streaming_manager

    _default_cwd = Path(settings.default_cwd)
    if not _default_cwd.is_absolute():
        _default_cwd = Path(settings.project_root) / _default_cwd
    dp["picker_store"] = PickerStore()
    dp["bot_defaults"] = BotDefaults(
        cwd=_default_cwd,
        mcp_config=Path(settings.project_root) / ".mcp.bot.json",
    )

    ensure_tmp_dir(session_manager.file_cache_dir)
    cleanup_old_tmp_files(session_manager.file_cache_dir)
    session_manager.load_mapping()
    session_manager.start_cleanup()

    periodic_cleanup_interval = 6 * 3600

    async def _periodic_tmp_cleanup() -> None:
        while True:
            await asyncio.sleep(periodic_cleanup_interval)
            try:
                deleted = cleanup_old_tmp_files(session_manager.file_cache_dir)
                logger.info("Periodic tmp cleanup: deleted %d files", deleted)
            except Exception:
                logger.warning("Periodic tmp cleanup failed", exc_info=True)

    cleanup_task = asyncio.create_task(_periodic_tmp_cleanup())

    health_state = HealthState()
    dp["health_state"] = health_state
    dp["outbox"] = outbox

    _fatal_stop: list[asyncio.Future[None]] = []

    def _request_stop() -> None:
        _fatal_stop.append(asyncio.ensure_future(dp.stop_polling()))

    healthcheck_task = asyncio.create_task(
        run_healthcheck_loop(bot, health_state, on_fatal=_request_stop)
    )
    heartbeat_task = asyncio.create_task(
        run_heartbeat_loop(
            health_state,
            url=settings.heartbeat_url,
            secret=settings.heartbeat_secret,
        )
    )
    if not settings.heartbeat_url:
        logger.info("heartbeat disabled (HEARTBEAT_URL not set)")

    async def _on_shutdown() -> None:
        logger.info("Shutting down: cleaning up sessions...")
        # Task cancellations don't raise here; the awaited steps below might.
        cleanup_task.cancel()
        healthcheck_task.cancel()
        heartbeat_task.cancel()
        await _safe_shutdown_step(
            "shutdown_heartbeat",
            lambda: send_shutdown_heartbeat(
                health_state, url=settings.heartbeat_url, secret=settings.heartbeat_secret
            ),
        )
        await _safe_shutdown_step("usage_tracker.stop_all", usage_tracker.stop_all)
        await _safe_shutdown_step("forward_batcher.shutdown", forward_batcher.shutdown)
        await _safe_shutdown_step("message_queue.shutdown", message_queue.shutdown)
        await _safe_shutdown_step("streaming_manager.shutdown", streaming_manager.shutdown)
        # Save before shutdown(): it clears _sessions, which save_mapping reads
        # to persist session_ids of streams that were in flight at SIGTERM.
        await _safe_shutdown_step("session_manager.save_mapping", session_manager.save_mapping)
        await _safe_shutdown_step("session_manager.shutdown", session_manager.shutdown)
        await _safe_shutdown_step("stop_modal_watchdog", tmux_manager.stop_modal_watchdog)
        await _safe_shutdown_step("stop_transcript_watchdog", tmux_manager.stop_transcript_watchdog)
        await _safe_shutdown_step("outbox.shutdown", outbox.shutdown)
        await _safe_shutdown_step("tmux_manager.persist_state", tmux_manager.persist_state)
        await _safe_shutdown_step("bot_lock.release", bot_lock.release)

    dp.shutdown.register(_on_shutdown)

    loop = asyncio.get_running_loop()
    _pending_stop: asyncio.Future[None] | None = None

    def _stop() -> None:
        nonlocal _pending_stop
        _pending_stop = asyncio.ensure_future(dp.stop_polling())

    loop.add_signal_handler(signal.SIGTERM, _stop)
    loop.add_signal_handler(signal.SIGINT, _stop)

    logger.info("Starting bot, allowed users: %d", len(settings.allowed_user_ids))
    await dp.start_polling(bot, handle_signals=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="telegram-bot")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run startup preflight checks and exit (no polling). For dry-run.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_start(preflight_only=args.preflight_only))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
