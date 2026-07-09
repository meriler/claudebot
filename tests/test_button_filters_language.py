"""H5 (audit 2026-07-02): reply-keyboard button filters must survive a runtime
/language switch.

Root cause: `@router.message(F.text == t("ui.btn_cancel"))` freezes the current
language's label at import time, so after /language flips BOT_LANG the button no
longer matches and the press leaks to Claude as a prompt. The fix matches the
button against ALL languages' labels via a runtime predicate.
"""

from telegram_bot.core.handlers.cancel import _CANCEL_TEXTS, _is_cancel_text
from telegram_bot.core.handlers.mode import _CHECKPOINT_TEXTS, _is_checkpoint_text
from telegram_bot.core.messages import MESSAGES, all_translations


def test_all_translations_collects_every_language() -> None:
    got = all_translations("ui.btn_cancel")
    expected = {table["ui.btn_cancel"] for table in MESSAGES.values() if "ui.btn_cancel" in table}
    assert got == expected
    assert len(got) >= 2  # at least EN + RU differ for cancel


def test_cancel_button_matches_both_languages() -> None:
    # Both the English and Russian labels must trigger the handler regardless
    # of the currently-active BOT_LANG.
    assert _is_cancel_text(MESSAGES["en"]["ui.btn_cancel"])
    assert _is_cancel_text(MESSAGES["ru"]["ui.btn_cancel"])
    assert all_translations("ui.btn_cancel") == _CANCEL_TEXTS


def test_checkpoint_button_matches_both_languages() -> None:
    assert _is_checkpoint_text(MESSAGES["en"]["ui.btn_checkpoint"])
    assert _is_checkpoint_text(MESSAGES["ru"]["ui.btn_checkpoint"])
    assert all_translations("ui.btn_checkpoint") == _CHECKPOINT_TEXTS


def test_predicates_reject_unrelated_and_none() -> None:
    assert not _is_cancel_text(None)
    assert not _is_cancel_text("just a normal message")
    assert not _is_checkpoint_text(None)
    assert not _is_checkpoint_text("not the button")
