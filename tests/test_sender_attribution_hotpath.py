"""H4 (audit 2026-07-02): the streaming/tmux hot paths must attribute the sender.

Attribution used to live only in enqueue_prompt, which the hot paths bypass —
so a second whitelisted person writing mid-turn reached the engine unattributed.
apply_sender_attribution is now the shared helper both paths use.
"""

from types import SimpleNamespace

from telegram_bot.core.handlers._dispatch import apply_sender_attribution

_KEY = (-100, 7)


def _msg(first: str, username: str | None = None):
    return SimpleNamespace(
        from_user=SimpleNamespace(first_name=first, last_name=None, username=username)
    )


def _topic_config(attribute_senders="auto"):
    tc = SimpleNamespace()
    tc.get_topic = lambda tid: SimpleNamespace(attribute_senders=attribute_senders)
    return tc


def test_multi_user_auto_prefixes_sender() -> None:
    settings = SimpleNamespace(attribute_senders="auto", allowed_user_ids=[1, 2])
    out = apply_sender_attribution("привет", _msg("Мария"), _KEY, settings, _topic_config())
    assert out != "привет"
    assert "Мария" in out
    assert out.endswith("привет")  # original text preserved below the prefix


def test_single_user_auto_leaves_text_unchanged() -> None:
    settings = SimpleNamespace(attribute_senders="auto", allowed_user_ids=[1])
    out = apply_sender_attribution("привет", _msg("Алексей"), _KEY, settings, _topic_config())
    assert out == "привет"  # byte-for-byte unchanged for a personal bot


def test_never_mode_leaves_text_unchanged_even_multi_user() -> None:
    settings = SimpleNamespace(attribute_senders="never", allowed_user_ids=[1, 2])
    out = apply_sender_attribution("привет", _msg("Мария"), _KEY, settings, _topic_config("never"))
    assert out == "привет"


def test_topic_mode_overrides_global() -> None:
    # Global says never, topic says always → topic wins → attributed.
    settings = SimpleNamespace(attribute_senders="never", allowed_user_ids=[1, 2])
    out = apply_sender_attribution("привет", _msg("Мария"), _KEY, settings, _topic_config("always"))
    assert "Мария" in out
