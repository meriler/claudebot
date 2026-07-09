"""Shared types for the telegram bot."""

from __future__ import annotations

from aiogram.types import Message

GENERAL_TOPIC_ID = 0

ChannelKey = tuple[int, int | None]
"""Compound key (chat_id, thread_id) identifying a unique conversation channel.

When thread_id is None, represents classic (non-topic) chat mode.
"""


def channel_key(message: Message) -> ChannelKey:
    """Extract ChannelKey from an aiogram Message."""
    thread_id = message.message_thread_id
    if thread_id is None and getattr(message.chat, "is_forum", False):
        thread_id = GENERAL_TOPIC_ID
    return (message.chat.id, thread_id)
