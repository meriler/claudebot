"""Catch-all handler for unsupported message types.

MUST be included LAST in the dispatcher — after all specific routers.
Otherwise it will intercept messages meant for other handlers.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message

from telegram_bot.core.messages import t

logger = logging.getLogger(__name__)

router = Router(name="unsupported")

# message attribute → i18n key with the tip for that content type
_TIPS: dict[str, str] = {
    "video": "ui.unsupported_video",
    "sticker": "ui.unsupported_sticker",
    "contact": "ui.unsupported_contact",
    "location": "ui.unsupported_location",
    "audio": "ui.unsupported_audio",
    "animation": "ui.unsupported_animation",
}


@router.message()
async def handle_unsupported(message: Message) -> None:
    for attr, tip_key in _TIPS.items():
        if getattr(message, attr, None):
            await message.reply(t(tip_key))
            return
    await message.reply(t("ui.unsupported_generic"))
