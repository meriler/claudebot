"""Tests for send_to_streaming_if_busy — the mid-turn steering router."""

from unittest.mock import AsyncMock, MagicMock

from telegram_bot.core.handlers.streaming import send_to_streaming_if_busy

_KEY = (1, 2)


async def test_injects_and_acks_when_busy() -> None:
    sm = MagicMock()
    sm.is_busy.return_value = True
    sm.inject = AsyncMock(return_value=True)
    msg = MagicMock()
    msg.answer = AsyncMock()

    handled = await send_to_streaming_if_busy(_KEY, "steer me", msg, sm)

    assert handled is True
    sm.inject.assert_awaited_once_with(_KEY, "steer me")
    msg.answer.assert_awaited_once()  # ack shown


async def test_falls_through_when_idle() -> None:
    sm = MagicMock()
    sm.is_busy.return_value = False
    sm.inject = AsyncMock()
    msg = MagicMock()
    msg.answer = AsyncMock()

    handled = await send_to_streaming_if_busy(_KEY, "start a turn", msg, sm)

    assert handled is False
    sm.inject.assert_not_awaited()
    msg.answer.assert_not_awaited()


async def test_none_manager_falls_through() -> None:
    msg = MagicMock()
    msg.answer = AsyncMock()
    assert await send_to_streaming_if_busy(_KEY, "x", msg, None) is False
    msg.answer.assert_not_awaited()


async def test_inject_race_returns_false() -> None:
    # is_busy was True but the turn ended before inject landed.
    sm = MagicMock()
    sm.is_busy.return_value = True
    sm.inject = AsyncMock(return_value=False)
    msg = MagicMock()
    msg.answer = AsyncMock()

    handled = await send_to_streaming_if_busy(_KEY, "x", msg, sm)

    assert handled is False
    msg.answer.assert_not_awaited()  # no false "delivered" ack
