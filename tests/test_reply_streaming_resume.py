"""Regression test for reply-to-resume in streaming mode.

Streaming is the default exec mode and supports reply-to-resume (the live
process respawns with --resume). A guard in `handle_text` predating streaming's
routing only whitelisted subprocess/tmux, so every reply to a streaming answer
wrongly bailed with `ui.tui_session_missing`
("Session unavailable (created before migration)…"). These tests pin the fix:
a streaming reply target must flow through to `enqueue_prompt` with the resolved
session id, and only a genuinely unknown exec mode may surface the error.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_bot.core.handlers import _dispatch as dispatch_mod
from telegram_bot.core.handlers import text as text_handler
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import ReplySessionRef


def _topic(engine: str = "claude", exec_mode: str = "streaming", model=None):
    return SimpleNamespace(
        engine=engine, exec_mode=exec_mode, model=model, attribute_senders="auto"
    )


def _message(text: str, *, reply_to_id: int | None = None):
    reply_to = SimpleNamespace(message_id=reply_to_id) if reply_to_id is not None else None
    return SimpleNamespace(
        text=text,
        entities=None,
        message_id=100,
        message_thread_id=7,
        chat=SimpleNamespace(id=42, is_forum=True),
        from_user=SimpleNamespace(id=1),
        reply_to_message=reply_to,
        answer=AsyncMock(),
    )


async def _run(monkeypatch, reply_ref, *, topic, tmux_active=False):
    # Reply-to-resume routing now lives in _dispatch.resolve_reply_routing;
    # ensure_exec_mode_ready is called from there, so patch it on _dispatch.
    monkeypatch.setattr(dispatch_mod, "ensure_exec_mode_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(text_handler, "ensure_exec_mode_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(text_handler, "send_to_streaming_if_busy", AsyncMock(return_value=False))
    monkeypatch.setattr(text_handler, "send_to_tmux_if_active", AsyncMock(return_value=False))
    enqueue = MagicMock()
    monkeypatch.setattr(text_handler, "enqueue_prompt", enqueue)

    session_manager = MagicMock()
    session_manager.resolve_reply_reference.return_value = reply_ref
    session_manager.clear_provider_session = AsyncMock()

    topic_config = MagicMock()
    topic_config.get_topic.return_value = topic
    topic_config.update_exec_mode = AsyncMock(return_value=True)
    topic_config.update_engine_model = AsyncMock(return_value=True)
    topic_config.update_engine_model_exec_mode = AsyncMock(return_value=True)

    tmux_manager = MagicMock()
    tmux_manager.is_active.return_value = tmux_active
    tmux_manager.is_processing.return_value = False

    async def _kill(_key):
        # Killing the session makes it inactive — mirror production so the
        # later is_active() check skips the tmux-switch branch.
        tmux_manager.is_active.return_value = False

    tmux_manager.kill = AsyncMock(side_effect=_kill)

    message_queue = MagicMock()
    message_queue.is_busy.return_value = False

    msg = _message("continue please", reply_to_id=55)

    # add_text is sync fire-and-forget in production (the real batcher debounces
    # on the event loop). Capture the callback and run it inline afterwards.
    captured: dict = {}

    def _add_text(_key, txt, source_msg, on_text):
        captured["txt"] = txt
        captured["msg"] = source_msg
        captured["cb"] = on_text

    batcher = MagicMock()
    batcher.add_text = _add_text

    await text_handler.handle_text(
        msg,
        session_manager,
        batcher,
        message_queue,
        tmux_manager,
        topic_config,
        SimpleNamespace(allowed_user_ids=[1], attribute_senders="auto"),
        MagicMock(),
    )
    await captured["cb"](captured["txt"], captured["msg"])
    return msg, enqueue, tmux_manager


async def test_streaming_reply_resumes_session(monkeypatch) -> None:
    ref = ReplySessionRef(session_id="sess-abc", provider="claude", exec_mode="streaming")
    msg, enqueue, _tmux = await _run(monkeypatch, ref, topic=_topic(exec_mode="streaming"))

    # The misleading migration error must NOT be shown.
    for call in msg.answer.await_args_list:
        assert call.args[0] != t("ui.tui_session_missing")
    # The reply must flow through to enqueue with the resolved session id.
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["target_session_id"] == "sess-abc"


async def test_unknown_exec_mode_still_bails(monkeypatch) -> None:
    # Defensive: a mode outside the valid set surfaces the error and stops.
    ref = ReplySessionRef(session_id="sess-x", provider="claude", exec_mode="warp-drive")
    msg, enqueue, _tmux = await _run(monkeypatch, ref, topic=_topic(exec_mode="streaming"))

    msg.answer.assert_awaited()
    assert msg.answer.await_args_list[0].args[0] == t("ui.tui_session_missing")
    enqueue.assert_not_called()


async def test_tmux_topic_reply_to_streaming_kills_tmux(monkeypatch) -> None:
    # Reply to a streaming answer while the topic is in tmux mode: the live tmux
    # session must be killed so the prompt routes to streaming, not the orphaned
    # tmux. Regression guard for the kill condition generalized to exec_mode_changed.
    ref = ReplySessionRef(session_id="sess-strm", provider="claude", exec_mode="streaming")
    msg, _enqueue, tmux = await _run(
        monkeypatch, ref, topic=_topic(exec_mode="tmux"), tmux_active=True
    )

    tmux.kill.assert_awaited_once()
    for call in msg.answer.await_args_list:
        assert call.args[0] != t("ui.tui_session_missing")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
