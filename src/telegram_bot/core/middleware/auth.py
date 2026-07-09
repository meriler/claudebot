"""Authentication middleware — whitelist by user ID."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

logger = logging.getLogger(__name__)

# Above this many tracked strangers, sweep expired entries so a flood of
# unique sender IDs can't grow the cooldown map without bound.
_COOLDOWN_MAP_SWEEP_THRESHOLD = 1000


def _is_forum_topic_event(event: TelegramObject) -> bool:
    """Service messages emitted by Telegram when a forum topic is created/edited.

    These bypass the user whitelist because `from_user` is whoever triggered the
    change — often the bot itself (when CC creates topics via Bot API), or a
    group admin with topic permissions. The forum_topic handler needs to see them
    regardless of who fired them, otherwise topic_config.json is never updated
    when the bot creates a topic.
    """
    if not isinstance(event, Message):
        return False
    return event.forum_topic_created is not None or event.forum_topic_edited is not None


class AuthMiddleware(BaseMiddleware):
    def __init__(
        self,
        allowed_user_ids: list[int],
        unauthorized_reply: str = "",
        reply_cooldown_sec: float = 600.0,
    ) -> None:
        self.allowed_user_ids = set(allowed_user_ids)
        # Optional canned reply for strangers. Empty → stay silent (default).
        self.unauthorized_reply = unauthorized_reply
        # At most one refusal per stranger per this window — stops a spammer
        # from making the bot answer every message and tripping Telegram's
        # global send flood limit (which would also stall the owner's replies).
        self.reply_cooldown_sec = reply_cooldown_sec
        self._last_reply_at: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Registered on dp.update, so `event` is the Update wrapper — unwrap to
        # the concrete inner event (message, callback_query, my_chat_member,
        # inline_query, …). This gates EVERY update type uniformly instead of
        # only the two observers we happened to register on, so a future handler
        # on a new update type is auth-gated by default, not by accident. (S3,
        # audit 2026-07-02.) Non-Update events (direct unit-test calls) pass
        # through unwrapped.
        inner = event.event if isinstance(event, Update) else event

        if _is_forum_topic_event(inner):
            # Bypass the whitelist ONLY for the bot's own topic creation
            # (from_user is a bot — CC creates topics via Bot API and Telegram
            # echoes them back) or a whitelisted user. A non-whitelisted
            # supergroup admin with topic permissions must NOT be able to make
            # the bot register topics into topic_config.json and fire welcome
            # messages. (S4, audit 2026-07-02.)
            ft_user = getattr(inner, "from_user", None)
            ft_uid = getattr(ft_user, "id", None)
            if bool(getattr(ft_user, "is_bot", False)) or (
                ft_uid is not None and ft_uid in self.allowed_user_ids
            ):
                return await handler(event, data)
            logger.warning("Ignored forum_topic event from unauthorized user: %s", ft_uid)
            return None
        user = getattr(inner, "from_user", None)
        if user is None or user.id not in self.allowed_user_ids:
            # Unauthorized access is a security signal — leave a WARNING so
            # `journalctl -p warning -u telegram-bot` surfaces a probe or
            # a leaked-token pattern. DEBUG hid this in the stream of
            # per-event traces (wave 2.8 review finding).
            user_id = getattr(user, "id", None)
            # Only log/refuse when there IS an identifiable user. Update types
            # with no from_user (channel_post, poll, message_reaction_count) are
            # dropped silently — there is no user to authorize, and logging every
            # one would be noise now that auth runs at the dp.update level.
            if user_id is not None:
                logger.warning("Ignored update from unauthorized user: %s", user_id)
            # Never refuse bots — including ourselves. In forum topics Telegram
            # echoes the bot's own posts back as updates (from_user is the bot),
            # so replying here would make the bot answer its own messages in the
            # topic, visible to everyone. Refusals go to real humans only.
            is_bot = bool(getattr(user, "is_bot", False))
            if (
                self.unauthorized_reply
                and user_id is not None
                and not is_bot
                and self._cooldown_ok(user_id)
            ):
                await self._send_refusal(inner)
            return None
        return await handler(event, data)

    def _cooldown_ok(self, user_id: int) -> bool:
        """True if enough time has passed to reply to this stranger again.

        Updates the last-reply timestamp as a side effect when it returns True,
        so the caller doesn't need a second bookkeeping step. Uses a monotonic
        clock so wall-clock jumps can't widen or shrink the window.
        """
        now = time.monotonic()
        last = self._last_reply_at.get(user_id)
        if last is not None and now - last < self.reply_cooldown_sec:
            return False
        if len(self._last_reply_at) >= _COOLDOWN_MAP_SWEEP_THRESHOLD:
            self._sweep_expired(now)
        self._last_reply_at[user_id] = now
        return True

    def _sweep_expired(self, now: float) -> None:
        """Drop cooldown entries past their window to bound memory under flood."""
        expired = [
            uid for uid, ts in self._last_reply_at.items() if now - ts >= self.reply_cooldown_sec
        ]
        for uid in expired:
            del self._last_reply_at[uid]

    async def _send_refusal(self, event: TelegramObject) -> None:
        """Answer a stranger with the canned refusal line.

        Best-effort: a stranger blocking the bot (or other Telegram errors)
        must not crash the middleware, so failures are swallowed with a log.
        """
        try:
            if isinstance(event, Message):
                await event.answer(self.unauthorized_reply)
            elif isinstance(event, CallbackQuery):
                await event.answer(self.unauthorized_reply, show_alert=True)
        except Exception:  # never let a reply failure break auth
            logger.warning("Failed to send unauthorized reply", exc_info=True)
