"""M7 (audit 2026-07-02): send_html_with_fallback must surface fatal=True when
the bot is blocked on the retry or plain-fallback branches, not only on the
first HTML attempt. Otherwise callers (outbox, streaming) keep pushing chunks
to a chat that has blocked the bot.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from telegram_bot.core.services.telegram_utils import send_html_with_fallback


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=MagicMock(), message="bot was blocked by the user")


async def test_forbidden_on_retry_is_fatal(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())  # skip the flood-wait
    flood = TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=1)
    send_html = AsyncMock(side_effect=[flood, _forbidden()])
    send_plain = AsyncMock()

    out = await send_html_with_fallback(send_html=send_html, send_plain=send_plain, label="t")

    assert out.message_id is None
    assert out.fatal is True  # blocked during retry → fatal
    send_plain.assert_not_awaited()  # flood path never cascades to plain


async def test_forbidden_on_plain_fallback_is_fatal() -> None:
    bad = TelegramBadRequest(method=MagicMock(), message="can't parse entities")
    send_html = AsyncMock(side_effect=bad)
    send_plain = AsyncMock(side_effect=_forbidden())

    out = await send_html_with_fallback(send_html=send_html, send_plain=send_plain, label="t")

    assert out.message_id is None
    assert out.fatal is True  # blocked during plain fallback → fatal


async def test_forbidden_on_first_attempt_is_fatal() -> None:
    send_html = AsyncMock(side_effect=_forbidden())
    out = await send_html_with_fallback(send_html=send_html, send_plain=AsyncMock(), label="t")
    assert out.fatal is True


async def test_successful_send_not_fatal() -> None:
    sent = MagicMock(message_id=42, text="hi")
    out = await send_html_with_fallback(
        send_html=AsyncMock(return_value=sent), send_plain=AsyncMock(), label="t"
    )
    assert out.message_id == 42
    assert out.fatal is False
