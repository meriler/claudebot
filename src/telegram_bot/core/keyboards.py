"""Core keyboard layouts — topic and stream mode controls."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from telegram_bot.core.messages import t
from telegram_bot.core.services.resume_listing import SessionEntry

RESUME_PAGE_SIZE = 9
_RESUME_BUTTONS_PER_ROW = 3
_TELEGRAM_BUTTON_TEXT_LIMIT = 64


def topic_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard with new chat, cancel, and a TUI-snapshot shortcut.
    The TUI button sends the i18n text `t("ui.btn_tui")` (e.g. "TUI 🖥");
    `handle_tui_button` in `handlers/tail.py` listens for that exact text
    and forwards to `handle_tail_command`, so the user gets the same
    `/tui` snapshot with one keyboard tap instead of typing the slash
    command."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t("ui.btn_new_chat")),
                KeyboardButton(text=t("ui.btn_checkpoint")),
                KeyboardButton(text=t("ui.btn_cancel")),
                KeyboardButton(text=t("ui.btn_tui")),
            ],
        ],
        is_persistent=True,
        resize_keyboard=True,
    )


def stream_mode_keyboard(
    current: str | None = None, thinking_on: bool = False
) -> InlineKeyboardMarkup:
    """Picker for per-topic stream_mode.

    Marks the currently active mode with ✅ so the user can see what's on
    without reading the caption twice. A second row toggles reasoning
    streaming — meaningful only under the live+ mode.
    """

    def _label(mode: str, text: str) -> str:
        return f"✅ {text}" if current == mode else text

    thinking_label = t(
        "ui.thinking_toggle", state=t("ui.state_on" if thinking_on else "ui.state_off")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_label("verbose", "📜 verbose"),
                    callback_data="stream_mode:verbose",
                ),
                InlineKeyboardButton(
                    text=_label("live", "🔄 live"),
                    callback_data="stream_mode:live",
                ),
                InlineKeyboardButton(
                    text=_label("minimal", "🤫 minimal"),
                    callback_data="stream_mode:minimal",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_label("live+", "💬 live+"),
                    callback_data="stream_mode:live+",
                ),
                InlineKeyboardButton(
                    text=thinking_label,
                    callback_data="stream_thinking:toggle",
                ),
            ],
        ],
    )


def exec_mode_keyboard(current: str | None = None) -> InlineKeyboardMarkup:
    """Two-button picker for per-topic exec_mode.

    Marks the currently active mode with ✅ so the user can see what's on
    without reading the caption twice.
    """

    def _label(mode: str, text: str) -> str:
        return f"✅ {text}" if current == mode else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_label("subprocess", t("ui.exec_mode_label_subprocess")),
                    callback_data="exec_mode:subprocess",
                ),
                InlineKeyboardButton(
                    text=_label("streaming", t("ui.exec_mode_label_streaming")),
                    callback_data="exec_mode:streaming",
                ),
                InlineKeyboardButton(
                    text=_label("tmux", t("ui.exec_mode_label_tmux")),
                    callback_data="exec_mode:tmux",
                ),
            ],
        ],
    )


def queue_item_keyboard(token: str) -> InlineKeyboardMarkup:
    """Single "remove from queue" button bound to a QueueItem by its token.

    Attached to the "added to queue" notification. The token (not a raw
    message_id) identifies the pending item in MessageQueue; the handler
    in handlers/cancel.py looks it up under the channel's deque and removes
    the whole item. message_id is monotonic only per-chat and forgeable in
    callback_data, so a server-side token is used instead.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("ui.queue_remove_btn"),
                    callback_data=f"qrm:{token}",
                ),
            ],
        ],
    )


def engine_keyboard(current_engine: str | None = None) -> InlineKeyboardMarkup:
    """Two-button picker for provider engine."""

    def _engine_label(engine: str, text: str) -> str:
        return f"✅ {text}" if current_engine == engine else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_engine_label("claude", "Claude Code"),
                    callback_data="engine:claude",
                ),
                InlineKeyboardButton(
                    text=_engine_label("codex", "Codex"),
                    callback_data="engine:codex",
                ),
            ],
        ],
    )


_MODEL_OPTIONS: list[tuple[str, str]] = [
    ("claude-opus-4-8", "Opus 4.8"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-haiku-4-5", "Haiku 4.5"),
]


def model_keyboard(current_model: str | None = None) -> InlineKeyboardMarkup:
    """Three-button picker for Claude model."""
    buttons = []
    for model_id, label in _MODEL_OPTIONS:
        is_current = current_model == model_id
        text = f"✅ {label}" if is_current else label
        buttons.append(InlineKeyboardButton(text=text, callback_data=f"model:{model_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def resume_keyboard(
    entries: tuple[SessionEntry, ...] | list[SessionEntry],
    *,
    page: int,
    current_session_id: str | None,
    token: str,
) -> InlineKeyboardMarkup:
    """Inline picker for /resume sessions."""
    total_pages = max(1, (len(entries) + RESUME_PAGE_SIZE - 1) // RESUME_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * RESUME_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    _ = current_session_id
    end = min(start + RESUME_PAGE_SIZE, len(entries))
    row: list[InlineKeyboardButton] = []
    for idx in range(start, end):
        row.append(
            InlineKeyboardButton(
                text=str(idx + 1),
                callback_data=f"rs:s:{token}:{idx}",
            )
        )
        if len(row) == _RESUME_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(text="◀", callback_data=f"rs:p:{token}:{max(page - 1, 0)}"),
            InlineKeyboardButton(
                text=t("ui.page_of", page=page + 1, total=total_pages),
                callback_data=f"rs:p:{token}:{page}",
            ),
            InlineKeyboardButton(
                text="▶",
                callback_data=f"rs:p:{token}:{min(page + 1, total_pages - 1)}",
            ),
        ]
    )
    rows.append([InlineKeyboardButton(text="✕", callback_data=f"rs:cancel:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}K"
    return f"{size / (1024 * 1024):.1f}M"


def _format_age(mtime: float) -> str:
    import time

    age = max(0, int(time.time() - mtime))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    if age < 86400:
        return f"{age // 3600}h"
    return f"{age // 86400}d"


def _truncate_button_text(text: str) -> str:
    if len(text) <= _TELEGRAM_BUTTON_TEXT_LIMIT:
        return text
    return text[: _TELEGRAM_BUTTON_TEXT_LIMIT - 1].rstrip() + "…"
