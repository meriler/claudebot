"""Tests for the self-heal healthcheck loop and heartbeat client."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_bot.core.health.healthcheck import run_healthcheck_loop
from telegram_bot.core.health.heartbeat import build_payload, run_heartbeat_loop
from telegram_bot.core.health.state import HealthState


def test_health_state_defaults() -> None:
    state = HealthState()
    assert state.consecutive_failures == 0
    assert state.pool_resets_total == 0
    assert state.telegram_reachable is True
    assert state.status() == "ok"


def test_health_state_pool_resets_today_resets_after_24h() -> None:
    state = HealthState()
    state.note_pool_reset()
    assert state.pool_resets_today == 1
    state.pool_resets_day_start = time.time() - 25 * 3600
    state.note_pool_reset()
    assert state.pool_resets_today == 1  # day rolled, counter restarted
    assert state.pool_resets_total == 2


def test_build_payload_status_ok() -> None:
    state = HealthState()
    payload = build_payload(state)
    assert payload["client_name"] == "klava"
    assert payload["status"] == "ok"
    assert payload["telegram_reachable"] is True
    assert isinstance(payload["uptime_seconds"], int)
    assert isinstance(payload["started_at"], str)
    assert isinstance(payload["ts"], str)


def test_build_payload_status_degraded_when_failures_present() -> None:
    state = HealthState()
    state.note_failure()
    assert build_payload(state)["status"] == "degraded"


def test_build_payload_status_degraded_after_recent_pool_reset() -> None:
    state = HealthState()
    state.note_pool_reset()
    state.note_success()  # failures back to 0, but reset was <60s ago
    assert build_payload(state)["status"] == "degraded"


def test_build_payload_no_secret_anywhere() -> None:
    state = HealthState()
    payload = build_payload(state)
    assert "secret" not in str(payload).lower()


def test_payload_shutdown_status_override() -> None:
    state = HealthState()
    assert build_payload(state, status="shutdown")["status"] == "shutdown"


async def test_healthcheck_resets_session_after_3_failures() -> None:
    bot = MagicMock()
    old_session = AsyncMock()
    bot.session = old_session
    state = HealthState()

    with patch(
        "telegram_bot.core.health.healthcheck._probe",
        AsyncMock(side_effect=ConnectionError("down")),
    ):
        task = asyncio.create_task(run_healthcheck_loop(bot, state, interval_sec=0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if state.pool_resets_total:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert state.pool_resets_total == 1
    assert state.consecutive_failures >= 3
    old_session.close.assert_awaited()
    assert bot.session is not old_session


async def test_healthcheck_never_exits_on_network_failures() -> None:
    """Critique deviation from FR-9: long outage = degraded mode, not sys.exit."""
    bot = MagicMock()
    bot.session = AsyncMock()
    state = HealthState()
    fatal_called = []

    with patch(
        "telegram_bot.core.health.healthcheck._probe",
        AsyncMock(side_effect=ConnectionError("down")),
    ):
        task = asyncio.create_task(
            run_healthcheck_loop(
                bot, state, on_fatal=lambda: fatal_called.append(1), interval_sec=0.001
            )
        )
        for _ in range(300):
            await asyncio.sleep(0.001)
            if state.consecutive_failures > 35:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert state.consecutive_failures > 30  # well past the old exit threshold
    assert not fatal_called  # network failures never trigger the fatal path


async def test_healthcheck_recovery_resets_counter() -> None:
    bot = MagicMock()
    bot.session = AsyncMock()
    state = HealthState()
    results = [ConnectionError("down"), ConnectionError("down"), None, None]

    async def probe(_bot: object) -> None:
        outcome = results.pop(0) if results else None
        if outcome is not None:
            raise outcome

    with patch("telegram_bot.core.health.healthcheck._probe", probe):
        task = asyncio.create_task(run_healthcheck_loop(bot, state, interval_sec=0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if state.last_healthcheck_ok_at is not None:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert state.consecutive_failures == 0
    assert state.telegram_reachable is True


async def test_heartbeat_noop_when_url_empty() -> None:
    state = HealthState()
    with patch("telegram_bot.core.health.heartbeat._post", AsyncMock()) as post:
        task = asyncio.create_task(run_heartbeat_loop(state, url="", interval_sec=0.001))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    post.assert_not_awaited()


async def test_heartbeat_survives_send_errors(caplog: pytest.LogCaptureFixture) -> None:
    state = HealthState()
    calls = []

    async def failing_post(*args: object) -> None:
        calls.append(1)
        raise OSError("receiver down")

    with patch("telegram_bot.core.health.heartbeat._post", failing_post):
        task = asyncio.create_task(
            run_heartbeat_loop(state, url="http://localhost:1/x", interval_sec=0.01)
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(calls) >= 2  # loop kept going after errors
    assert any("Heartbeat send failed" in r.message for r in caplog.records)
