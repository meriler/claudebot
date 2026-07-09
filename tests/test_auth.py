"""AuthMiddleware: whitelist gating and the stranger-reply cooldown."""

from __future__ import annotations

from typing import Any

from telegram_bot.core.middleware.auth import AuthMiddleware


class _FakeUser:
    def __init__(self, user_id: int, is_bot: bool = False) -> None:
        self.id = user_id
        self.is_bot = is_bot


class _FakeMessage:
    """Minimal stand-in for aiogram Message — passes isinstance via patching.

    AuthMiddleware._send_refusal calls ``event.answer``; we record every call so
    a test can assert how many refusals were actually sent.
    """

    def __init__(self, user_id: int | None, is_bot: bool = False) -> None:
        self.from_user = _FakeUser(user_id, is_bot) if user_id is not None else None
        self.forum_topic_created = None
        self.forum_topic_edited = None
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


class _FakeUpdate:
    """Stand-in for aiogram Update — carries the concrete inner event."""

    def __init__(self, inner: _FakeMessage) -> None:
        self.event = inner


async def _noop_handler(event: Any, data: dict[str, Any]) -> str:
    return "handled"


def _patch_message_type(monkeypatch) -> None:
    """Make isinstance(event, Message) true for _FakeMessage in the module."""
    import telegram_bot.core.middleware.auth as auth_mod

    monkeypatch.setattr(auth_mod, "Message", _FakeMessage)


def _patch_types(monkeypatch) -> None:
    """Patch both Message and Update so fakes pass the isinstance checks."""
    import telegram_bot.core.middleware.auth as auth_mod

    monkeypatch.setattr(auth_mod, "Message", _FakeMessage)
    monkeypatch.setattr(auth_mod, "Update", _FakeUpdate)


async def test_update_wrapper_allowed_user_passes(monkeypatch) -> None:
    # S3: registered on dp.update, the middleware unwraps Update.event and gates.
    _patch_types(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    upd = _FakeUpdate(_FakeMessage(42))
    assert await mw(_noop_handler, upd, {}) == "handled"


async def test_update_wrapper_stranger_blocked(monkeypatch) -> None:
    _patch_types(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    inner = _FakeMessage(999)
    upd = _FakeUpdate(inner)
    assert await mw(_noop_handler, upd, {}) is None
    assert inner.answers == ["nope"]  # refusal reaches the unwrapped inner event


async def test_update_without_from_user_dropped_silently(monkeypatch) -> None:
    # An update type with no from_user (poll, channel_post) → nothing to
    # authorize → dropped, but no refusal spam.
    _patch_types(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    inner = _FakeMessage(None)  # from_user is None
    upd = _FakeUpdate(inner)
    assert await mw(_noop_handler, upd, {}) is None
    assert inner.answers == []


async def test_allowed_user_passes_to_handler(monkeypatch) -> None:
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    msg = _FakeMessage(42)

    result = await mw(_noop_handler, msg, {})

    assert result == "handled"
    assert msg.answers == []  # owner never gets the refusal


async def test_stranger_blocked_and_gets_refusal(monkeypatch) -> None:
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    msg = _FakeMessage(999)

    result = await mw(_noop_handler, msg, {})

    assert result is None  # handler never runs for strangers
    assert msg.answers == ["nope"]


async def test_bot_sender_never_refused(monkeypatch) -> None:
    # Forum topics echo the bot's own posts back as updates (from_user is the
    # bot). Replying would make the bot answer itself in the topic.
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    echo = _FakeMessage(1000000001, is_bot=True)

    result = await mw(_noop_handler, echo, {})

    assert result is None  # still gated out of handlers
    assert echo.answers == []  # but no self-reply in the topic


def _forum_created(user_id: int | None, is_bot: bool = False) -> _FakeMessage:
    msg = _FakeMessage(user_id, is_bot)
    msg.forum_topic_created = object()  # mark as a topic-created service message
    return msg


async def test_forum_topic_from_bot_passes(monkeypatch) -> None:
    # The bot creating a topic via Bot API (from_user is the bot) must reach the
    # handler so topic_config.json gets updated. (S4)
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    evt = _forum_created(1000000001, is_bot=True)

    result = await mw(_noop_handler, evt, {})

    assert result == "handled"


async def test_forum_topic_from_whitelisted_user_passes(monkeypatch) -> None:
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    evt = _forum_created(42)

    assert await mw(_noop_handler, evt, {}) == "handled"


async def test_forum_topic_from_stranger_admin_blocked(monkeypatch) -> None:
    # S4 (audit 2026-07-02): a non-whitelisted supergroup admin creating a topic
    # must NOT trigger registration/welcome — the handler must not run.
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope")
    evt = _forum_created(999)  # human, not whitelisted, not a bot

    result = await mw(_noop_handler, evt, {})

    assert result is None  # dropped
    assert evt.answers == []  # service message: no refusal spam


async def test_empty_reply_stays_silent(monkeypatch) -> None:
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="")
    msg = _FakeMessage(999)

    await mw(_noop_handler, msg, {})

    assert msg.answers == []  # default behaviour: ignore silently


async def test_cooldown_blocks_repeat_within_window(monkeypatch) -> None:
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope", reply_cooldown_sec=600)

    first = _FakeMessage(999)
    await mw(_noop_handler, first, {})
    second = _FakeMessage(999)
    await mw(_noop_handler, second, {})

    assert first.answers == ["nope"]
    assert second.answers == []  # silenced by cooldown


async def test_cooldown_allows_after_window(monkeypatch) -> None:
    _patch_message_type(monkeypatch)
    clock = {"now": 1000.0}
    import telegram_bot.core.middleware.auth as auth_mod

    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: clock["now"])
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope", reply_cooldown_sec=600)

    first = _FakeMessage(999)
    await mw(_noop_handler, first, {})
    clock["now"] += 601  # past the window
    second = _FakeMessage(999)
    await mw(_noop_handler, second, {})

    assert first.answers == ["nope"]
    assert second.answers == ["nope"]  # window elapsed, reply again


async def test_cooldown_is_per_user(monkeypatch) -> None:
    _patch_message_type(monkeypatch)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope", reply_cooldown_sec=600)

    a = _FakeMessage(111)
    b = _FakeMessage(222)
    await mw(_noop_handler, a, {})
    await mw(_noop_handler, b, {})

    assert a.answers == ["nope"]
    assert b.answers == ["nope"]  # different stranger, own cooldown slot


def test_sweep_bounds_map_under_unique_flood(monkeypatch) -> None:
    import telegram_bot.core.middleware.auth as auth_mod

    clock = {"now": 1000.0}
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(auth_mod, "_COOLDOWN_MAP_SWEEP_THRESHOLD", 3)
    mw = AuthMiddleware(allowed_user_ids=[42], unauthorized_reply="nope", reply_cooldown_sec=600)

    # Fill the map with stale entries, then advance past the window.
    for uid in range(3):
        mw._cooldown_ok(uid)
    clock["now"] += 601  # all three now expired

    # Next unique sender trips the sweep, which drops the expired entries.
    mw._cooldown_ok(99)

    assert set(mw._last_reply_at) == {99}
