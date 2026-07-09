"""Heartbeat client: pings an external dead-man receiver once a minute.

The receiver (e.g. a healthchecks.io check) alerts the owner when pings stop —
that is the only alert channel that works when Telegram itself is the thing
that is down. trust_env=False is deliberate: the ping must bypass the system
proxy and reach the receiver even when the proxy is broken.

Fire-and-forget: send errors are logged and never retried (FR-20); the
receiver tolerates gaps by design.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import socket

import aiohttp

from telegram_bot.core.health.state import HealthState

logger = logging.getLogger(__name__)

_CLIENT_NAME = "klava"
_INTERVAL_SEC = 60.0
_NOOP_LOG_EVERY = 10  # cycles between "disabled" debug lines (FR-12)
_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)


def _iso(ts: float | None = None) -> str:
    dt = (
        datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
        if ts is not None
        else datetime.datetime.now(tz=datetime.UTC)
    )
    return dt.isoformat(timespec="seconds")


def build_payload(state: HealthState, *, status: str | None = None) -> dict[str, object]:
    """FR-17 payload. No secrets here — the secret travels in a header only."""
    return {
        "client_name": _CLIENT_NAME,
        "host": socket.gethostname(),
        "status": status if status is not None else state.status(),
        "telegram_reachable": state.telegram_reachable,
        "telegram_errors_consecutive": state.consecutive_failures,
        "pool_resets_today": state.pool_resets_today,
        "uptime_seconds": state.uptime_seconds,
        "started_at": _iso(state.started_at),
        "ts": _iso(),
    }


def _headers(secret: str) -> dict[str, str]:
    return {"X-Heartbeat-Secret": secret} if secret else {}


def _make_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        trust_env=False,  # bypass system proxy on purpose — see module docstring
        connector=aiohttp.TCPConnector(force_close=True, limit=1),
        timeout=_TIMEOUT,
    )


async def _post(
    session: aiohttp.ClientSession, url: str, secret: str, payload: dict[str, object]
) -> None:
    async with session.post(url, json=payload, headers=_headers(secret)) as resp:
        if resp.status in (401, 403):
            logger.error(
                "Heartbeat receiver rejected the request (%d) — check HEARTBEAT_SECRET",
                resp.status,
            )
        elif resp.status >= 400:
            logger.warning("Heartbeat receiver returned %d", resp.status)


async def run_heartbeat_loop(
    state: HealthState,
    *,
    url: str,
    secret: str = "",
    interval_sec: float = _INTERVAL_SEC,
) -> None:
    """Run forever; cancelled on shutdown."""
    if not url:
        cycle = 0
        while True:
            await asyncio.sleep(interval_sec * _NOOP_LOG_EVERY)
            cycle += 1
            logger.debug("heartbeat disabled (HEARTBEAT_URL not set)")
    if not secret:
        logger.warning("HEARTBEAT_URL set without HEARTBEAT_SECRET — sending unsigned pings")
    logger.info("heartbeat target: %s", url)
    session = _make_session()
    try:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                await _post(session, url, secret, build_payload(state))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Heartbeat send failed: %s: %s", type(exc).__name__, exc)
    finally:
        with contextlib.suppress(Exception):
            await session.close()


async def send_shutdown_heartbeat(state: HealthState, *, url: str, secret: str = "") -> None:
    """FR-22: one final ping with status=shutdown, max 3 seconds, best effort."""
    if not url:
        return
    try:
        async with _make_session() as session:
            await asyncio.wait_for(
                _post(session, url, secret, build_payload(state, status="shutdown")),
                timeout=3,
            )
    except Exception as exc:
        logger.warning("Shutdown heartbeat not delivered: %s: %s", type(exc).__name__, exc)
