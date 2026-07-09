"""Tests for the shared reply-to-resume router (M4/M5, audit 2026-07-02).

resolve_reply_routing is extracted from text.py and used by every input handler
(text/voice/photo/forward/video_note) so they all switch engine/session on a
reply instead of bailing (M4) or mis-routing (M5).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram_bot.core.handlers import _dispatch as dispatch_mod
from telegram_bot.core.handlers._dispatch import resolve_reply_routing
from telegram_bot.core.services.claude import ReplySessionRef

_KEY = (42, 7)


def _deps(*, topic, reply_ref=None, tmux_active=False, tmux_sid=None, busy=False):
    sm = MagicMock()
    sm.resolve_reply_reference.return_value = reply_ref
    sm.clear_provider_session = AsyncMock()
    tc = MagicMock()
    tc.get_topic.return_value = topic
    tc.update_engine_model = AsyncMock(return_value=True)
    tc.update_exec_mode = AsyncMock(return_value=True)
    tc.update_engine_model_exec_mode = AsyncMock(return_value=True)
    tm = MagicMock()
    tm.is_processing.return_value = False
    tm.is_active.return_value = tmux_active
    tm.get_session_id.return_value = tmux_sid
    tm.kill = AsyncMock()
    tm.switch_session = AsyncMock(return_value=True)
    mq = MagicMock()
    mq.is_busy.return_value = busy
    return sm, tc, tm, mq


def _topic(engine="claude", exec_mode="streaming"):
    return SimpleNamespace(engine=engine, exec_mode=exec_mode, model=None)


def _src():
    return SimpleNamespace(answer=AsyncMock())


async def _call(monkeypatch, *, reply_to, deps, ready=True):
    monkeypatch.setattr(dispatch_mod, "ensure_exec_mode_ready", AsyncMock(return_value=ready))
    sm, tc, tm, mq = deps
    return await resolve_reply_routing(
        _KEY,
        _src(),
        reply_to,
        session_manager=sm,
        topic_config=tc,
        tmux_manager=tm,
        message_queue=mq,
    )


async def test_no_reply_proceeds(monkeypatch) -> None:
    deps = _deps(topic=_topic())
    bail, target_sid, switched = await _call(monkeypatch, reply_to=None, deps=deps)
    assert (bail, target_sid, switched) == (False, None, False)


async def test_unknown_provider_bails(monkeypatch) -> None:
    ref = ReplySessionRef(session_id="s", provider="gemini")  # not claude/codex
    deps = _deps(topic=_topic(), reply_ref=ref)
    bail, _sid, _sw = await _call(monkeypatch, reply_to=SimpleNamespace(message_id=5), deps=deps)
    assert bail is True


async def test_provider_switch_performed_then_proceeds(monkeypatch) -> None:
    # Reply targets codex while topic is on claude → switch, then proceed.
    ref = ReplySessionRef(session_id="s", provider="codex", exec_mode="streaming")
    deps = _deps(topic=_topic(engine="claude", exec_mode="streaming"), reply_ref=ref)
    sm, tc, _tm, _mq = deps
    bail, target_sid, switched = await _call(
        monkeypatch, reply_to=SimpleNamespace(message_id=5), deps=deps
    )
    assert bail is False
    tc.update_engine_model.assert_awaited_once()  # engine written
    sm.clear_provider_session.assert_awaited_once()  # provider session cleared
    assert target_sid == "s"  # streaming keeps the target session id
    assert switched is False  # not a tmux session switch


async def test_switch_blocked_when_busy(monkeypatch) -> None:
    ref = ReplySessionRef(session_id="s", provider="codex", exec_mode="streaming")
    deps = _deps(topic=_topic(engine="claude"), reply_ref=ref, busy=True)
    bail, _sid, _sw = await _call(monkeypatch, reply_to=SimpleNamespace(message_id=5), deps=deps)
    assert bail is True  # exec_mode_busy


async def test_ensure_ready_failure_bails(monkeypatch) -> None:
    deps = _deps(topic=_topic())
    bail, _sid, _sw = await _call(monkeypatch, reply_to=None, deps=deps, ready=False)
    assert bail is True


async def test_tmux_session_switch(monkeypatch) -> None:
    # Same provider/mode (tmux), reply targets a different live session → switch.
    ref = ReplySessionRef(session_id="target-sid", provider="claude", exec_mode="tmux")
    deps = _deps(
        topic=_topic(engine="claude", exec_mode="tmux"),
        reply_ref=ref,
        tmux_active=True,
        tmux_sid="current-sid",
    )
    _sm, _tc, tm, _mq = deps
    bail, target_sid, switched = await _call(
        monkeypatch, reply_to=SimpleNamespace(message_id=5), deps=deps
    )
    assert bail is False
    tm.switch_session.assert_awaited_once()
    assert switched is True
    assert target_sid is None  # tmux manages session state internally
