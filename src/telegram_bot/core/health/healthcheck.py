"""Active healthcheck loop: detects a stuck TLS pool and recreates it in place.

The bot's aiohttp pool can cache broken TLS state after the system proxy
switches upstream nodes; the pool then retries forever and never recovers
without intervention. Every cycle this loop calls get_me() through a fresh,
single-use session (so the check itself can never get stuck) and recreates
the bot's main session after repeated failures.

Network failures never exit the process (degraded mode — a Telegram outage
is normal here and a crash-loop would kill in-flight Claude sessions).
The fatal path fires only when recreating the session itself keeps raising:
that is internal breakage launchd should restart us out of.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import GetMe

from telegram_bot.core.health.state import HealthState

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

_INTERVAL_SEC = 60.0
_RESET_AFTER_FAILURES = 3  # FR-7
_RESET_EVERY_N_WHILE_DOWN = 10  # keep retrying a fresh pool during long outages
_CRITICAL_EVERY_N = 30  # remind in the log once per ~30 min of outage
_FATAL_RECREATE_ERRORS = 3  # non-network breakage → graceful stop


async def _probe(bot: Bot, timeout_sec: float = 15.0) -> None:
    """One get_me() through a throwaway session sharing no state with the bot."""
    session = AiohttpSession(limit=1)
    # Single-use, no keep-alive: the probe must never inherit or cache a
    # broken connection (force_close) and never hold sockets between cycles.
    session._connector_init["force_close"] = True
    try:
        await asyncio.wait_for(session(bot, GetMe()), timeout=timeout_sec)
    finally:
        await session.close()


def _recreate_main_session(bot: Bot) -> AiohttpSession:
    return AiohttpSession()


async def run_healthcheck_loop(
    bot: Bot,
    state: HealthState,
    *,
    on_fatal: Callable[[], None] | None = None,
    interval_sec: float = _INTERVAL_SEC,
) -> None:
    """Run forever; cancelled on shutdown."""
    recreate_errors = 0
    was_after_reset = False
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await _probe(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures = state.note_failure()
            logger.warning(
                "Healthcheck failed (%d consecutive): %s: %s",
                failures,
                type(exc).__name__,
                exc,
            )
            should_reset = failures == _RESET_AFTER_FAILURES or (
                failures > _RESET_AFTER_FAILURES
                and (failures - _RESET_AFTER_FAILURES) % _RESET_EVERY_N_WHILE_DOWN == 0
            )
            if should_reset:
                try:
                    old_session = bot.session
                    bot.session = _recreate_main_session(bot)
                    await old_session.close()
                    state.note_pool_reset()
                    was_after_reset = True
                    recreate_errors = 0
                    logger.info(
                        "AiohttpSession recreated after %d consecutive healthcheck failures",
                        failures,
                    )
                except Exception:
                    recreate_errors += 1
                    logger.exception(
                        "Failed to recreate AiohttpSession (%d in a row)", recreate_errors
                    )
                    if recreate_errors >= _FATAL_RECREATE_ERRORS and on_fatal is not None:
                        logger.critical(
                            "Session recreation keeps failing — internal breakage, "
                            "requesting graceful stop (launchd will restart us)"
                        )
                        on_fatal()
                        return
            if failures % _CRITICAL_EVERY_N == 0:
                logger.critical(
                    "Telegram unreachable for %d consecutive healthchecks (~%d min) — "
                    "degraded mode, process stays up to protect in-flight sessions",
                    failures,
                    int(failures * interval_sec / 60),
                )
            continue

        if state.consecutive_failures > 0 and was_after_reset:
            logger.info("Healthcheck recovered after pool reset")
        elif state.consecutive_failures > 0:
            logger.info("Healthcheck recovered after %d failures", state.consecutive_failures)
        was_after_reset = False
        recreate_errors = 0
        state.note_success()
