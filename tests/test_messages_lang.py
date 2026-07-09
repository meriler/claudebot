"""M9 (audit 2026-07-02): the localization language must come from Settings/.env.

messages._get_lang() reads os.environ["BOT_LANG"], which launchd does not set,
so the UI fell back to English after every restart. The fix seeds
os.environ["BOT_LANG"] from settings.bot_lang at startup. These tests lock in
the mechanism that fix relies on: a seeded BOT_LANG + cache reset switches t().

monkeypatch.setenv auto-restores BOT_LANG so these don't leak into other tests.
"""

from telegram_bot.core.messages import _get_lang, reset_lang_cache, t


def _seed(monkeypatch, lang: str) -> None:
    monkeypatch.setenv("BOT_LANG", lang)
    reset_lang_cache()


def test_seeded_russian_switches_translation(monkeypatch) -> None:
    _seed(monkeypatch, "ru")
    assert _get_lang() == "ru"
    # A key that differs between languages proves the switch took effect.
    assert t("ui.btn_cancel") == "Отменить ❌"


def test_seeded_english(monkeypatch) -> None:
    _seed(monkeypatch, "en")
    assert _get_lang() == "en"
    assert t("ui.btn_cancel") == "Cancel ❌"


def test_unknown_lang_falls_back_to_english(monkeypatch) -> None:
    _seed(monkeypatch, "klingon")
    assert _get_lang() == "en"


def test_cache_reset_required_to_switch(monkeypatch) -> None:
    _seed(monkeypatch, "ru")
    assert _get_lang() == "ru"
    _seed(monkeypatch, "en")  # _seed calls reset_lang_cache, so the switch shows
    assert _get_lang() == "en"


def teardown_module(_module) -> None:
    # Restore the process-wide lang cache to whatever the real env says.
    reset_lang_cache()
