"""Tests for the live+ stream_mode: intermediate 💬 comments + 🧠 reasoning.

Covers three layers:
- parser (cc_events): thinking blocks become events, text still does too;
- renderer (streaming): live+ dispatch emits/​gates blocks, final dedup;
- config (topic_config): live+ is a valid mode, stream_thinking round-trips.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telegram_bot.core.handlers.streaming import (
    _handle_event_liveplus,
    _norm_text,
    _StreamCtx,
)
from telegram_bot.core.services.cc_events import StreamEvent, parse_cc_event
from telegram_bot.core.services.topic_config import TopicConfig

_KEY = (1, 2)


# --- parser ---------------------------------------------------------------


def _parse(data: dict) -> list[StreamEvent]:
    events, _ = parse_cc_event(data, {}, {}, 0.0)
    return events


def test_parser_emits_thinking_event() -> None:
    events = _parse(
        {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": "let me reason"}]},
        }
    )
    assert [(e.type, e.content) for e in events] == [("thinking", "let me reason")]


def test_parser_still_emits_text_event() -> None:
    events = _parse(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "checking X"}]},
        }
    )
    assert [(e.type, e.content) for e in events] == [("text", "checking X")]


def test_parser_drops_empty_thinking() -> None:
    events = _parse(
        {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": ""}]},
        }
    )
    assert events == []


# --- renderer -------------------------------------------------------------


def _make_ctx(*, show_thinking: bool) -> _StreamCtx:
    message = MagicMock()
    message.answer = AsyncMock(return_value=MagicMock(message_id=111))
    message.chat = MagicMock(id=1)
    return _StreamCtx(
        message=message,
        channel_key=_KEY,
        session_manager=MagicMock(),
        tmux_manager=None,
        stream_mode="live+",
        used_tmux=False,
        live_buffer=None,
        sent_message_ids=[],
        show_thinking=show_thinking,
    )


async def test_liveplus_text_emits_with_marker_and_records() -> None:
    ctx = _make_ctx(show_thinking=False)
    await _handle_event_liveplus(ctx, StreamEvent("text", "checking the logs"))

    ctx.message.answer.assert_awaited()
    sent = ctx.message.answer.await_args.args[0]
    assert sent.startswith("💬")
    assert "checking the logs" in sent
    # normalized content tracked for final dedup; message_id recorded.
    assert ctx.emitted_text_norm == ["checking the logs"]
    assert ctx.liveplus_msg_ids == [111]


async def test_liveplus_thinking_gated_off_by_default() -> None:
    ctx = _make_ctx(show_thinking=False)
    await _handle_event_liveplus(ctx, StreamEvent("thinking", "internal monologue"))

    ctx.message.answer.assert_not_awaited()
    assert ctx.emitted_text_norm == []


async def test_liveplus_thinking_shown_when_enabled() -> None:
    ctx = _make_ctx(show_thinking=True)
    await _handle_event_liveplus(ctx, StreamEvent("thinking", "internal monologue"))

    ctx.message.answer.assert_awaited()
    sent = ctx.message.answer.await_args.args[0]
    assert sent.startswith("🧠")
    assert "internal monologue" in sent


def test_norm_text_collapses_whitespace() -> None:
    # Final dedup compares normalized text: the result event repeats the last
    # block but may differ in trailing/interspersed whitespace.
    assert _norm_text("found\n\n  it   now") == _norm_text("found it now")


# --- config ---------------------------------------------------------------


def _tc(tmp_path: Path, topic: dict) -> TopicConfig:
    path = tmp_path / "topic_config.json"
    path.write_text(json.dumps({"topics": {"42": topic}}, ensure_ascii=False), encoding="utf-8")
    return TopicConfig(str(path), ".")


def test_liveplus_is_a_valid_stream_mode(tmp_path: Path) -> None:
    tc = _tc(tmp_path, {"name": "D", "type": "project", "mode": "free", "stream_mode": "live+"})
    assert tc.get_topic(42).stream_mode == "live+"


def test_invalid_stream_mode_falls_back(tmp_path: Path) -> None:
    tc = _tc(tmp_path, {"name": "D", "type": "project", "mode": "free", "stream_mode": "bogus"})
    assert tc.get_topic(42).stream_mode == "live+"  # _DEFAULT_STREAM_MODE


def test_stream_thinking_parsed(tmp_path: Path) -> None:
    tc = _tc(tmp_path, {"name": "D", "type": "project", "mode": "free", "stream_thinking": True})
    assert tc.get_topic(42).stream_thinking is True


def test_stream_thinking_defaults_false(tmp_path: Path) -> None:
    tc = _tc(tmp_path, {"name": "D", "type": "project", "mode": "free"})
    assert tc.get_topic(42).stream_thinking is False


def test_non_bool_stream_thinking_ignored(tmp_path: Path) -> None:
    tc = _tc(tmp_path, {"name": "D", "type": "project", "mode": "free", "stream_thinking": "yes"})
    assert tc.get_topic(42).stream_thinking is False


async def test_update_stream_thinking_persists(tmp_path: Path) -> None:
    tc = _tc(tmp_path, {"name": "D", "type": "project", "mode": "free"})
    ok = await tc.update_stream_thinking(42, True)
    assert ok is True
    tc2 = TopicConfig(str(tmp_path / "topic_config.json"), ".")
    assert tc2.get_topic(42).stream_thinking is True
