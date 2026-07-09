"""Tests for the /sysprompt command and its apply callback (TASK-4)."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.handlers.commands import (
    handle_sysprompt_command,
    on_sysprompt_apply_click,
)
from telegram_bot.core.tui.routing import route_slash_command


def _msg(chat_id: int, thread_id: int | None, *, reply_text: str | None = None):
    chat = types.SimpleNamespace(id=chat_id, is_forum=thread_id is not None)
    reply = types.SimpleNamespace(text=reply_text, caption=None) if reply_text else None
    return types.SimpleNamespace(
        chat=chat,
        message_thread_id=thread_id,
        reply_to_message=reply,
        from_user=types.SimpleNamespace(id=1),
        answer=AsyncMock(),
    )


def _cmd(args: str | None):
    return types.SimpleNamespace(args=args)


def _topic_config():
    tc = AsyncMock()
    # get_* are sync in the real class; AsyncMock would return coroutines.
    tc.get_topic = lambda tid: types.SimpleNamespace(system_prompt=None, engine="claude")
    tc.get_chat_prompt = lambda cid: None
    tc.update_system_prompt = AsyncMock(return_value=True)
    tc.update_chat_prompt = AsyncMock(return_value=True)
    return tc


# --- routing (TASK-2) ---


def test_routing_reserves_sysprompt() -> None:
    assert route_slash_command("/sysprompt отвечай рецептами") == "bot"
    assert route_slash_command("/sysprompt") == "bot"


# --- /sysprompt command ---


@pytest.mark.asyncio
async def test_set_in_topic_calls_update_system_prompt() -> None:
    tc = _topic_config()
    msg = _msg(chat_id=-100, thread_id=42)

    await handle_sysprompt_command(msg, _cmd("будь краток"), tc)

    tc.update_system_prompt.assert_awaited_once_with(42, "будь краток")
    tc.update_chat_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_in_dm_calls_update_chat_prompt() -> None:
    tc = _topic_config()
    msg = _msg(chat_id=555, thread_id=None)

    await handle_sysprompt_command(msg, _cmd("личный стиль"), tc)

    tc.update_chat_prompt.assert_awaited_once_with(555, "личный стиль")
    tc.update_system_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_topic_gets_warning() -> None:
    tc = _topic_config()
    tc.get_topic = lambda tid: types.SimpleNamespace(system_prompt=None, engine="codex")
    msg = _msg(chat_id=-100, thread_id=42)

    await handle_sysprompt_command(msg, _cmd("будь краток"), tc)

    tc.update_system_prompt.assert_awaited_once_with(42, "будь краток")
    sent = msg.answer.await_args.args[0]
    assert "Codex" in sent  # warns the prompt won't apply to the codex engine


@pytest.mark.asyncio
async def test_reset_in_topic_writes_none() -> None:
    tc = _topic_config()
    msg = _msg(chat_id=-100, thread_id=42)

    await handle_sysprompt_command(msg, _cmd("reset"), tc)

    tc.update_system_prompt.assert_awaited_once_with(42, None)


@pytest.mark.asyncio
async def test_empty_payload_is_show_mode() -> None:
    tc = _topic_config()
    msg = _msg(chat_id=-100, thread_id=42)

    await handle_sysprompt_command(msg, _cmd(None), tc)

    tc.update_system_prompt.assert_not_awaited()
    tc.update_chat_prompt.assert_not_awaited()
    msg.answer.assert_awaited()  # shows current state


@pytest.mark.asyncio
async def test_reply_text_used_when_no_args() -> None:
    tc = _topic_config()
    msg = _msg(chat_id=555, thread_id=None, reply_text="длинный промт из reply")

    await handle_sysprompt_command(msg, _cmd(None), tc)

    tc.update_chat_prompt.assert_awaited_once_with(555, "длинный промт из reply")


# --- apply callback ---


def _cb_message(chat_id: int, thread_id: int | None):
    """A bot reply that carries the apply button, in the chat it targets."""
    chat = types.SimpleNamespace(id=chat_id, is_forum=thread_id is not None)
    return types.SimpleNamespace(
        chat=chat,
        message_thread_id=thread_id,
        answer=AsyncMock(),
    )


def _stream(*, active: bool = False, busy: bool = False):
    """StreamingManager mock: is_active/is_busy are sync in the real class."""
    stream = AsyncMock()
    stream.is_active = lambda key: active
    stream.is_busy = lambda key: busy
    stream.kill = AsyncMock()
    return stream


@pytest.mark.asyncio
async def test_apply_callback_resets_session() -> None:
    tmux = AsyncMock()
    tmux.is_processing = lambda key: False
    tmux.is_active = lambda key: True
    tmux.kill = AsyncMock()
    mq = types.SimpleNamespace(is_busy=lambda key: False)
    sm = AsyncMock()
    sm.clear_provider_session = AsyncMock()
    stream = _stream(active=True)

    cb = types.SimpleNamespace(
        data="sysprompt_apply:-100:42",
        message=_cb_message(-100, 42),
        answer=AsyncMock(),
    )

    await on_sysprompt_apply_click(cb, tmux, mq, sm, stream)

    # H3: the live streaming session must be killed too, not just tmux.
    stream.kill.assert_awaited_once_with((-100, 42))
    tmux.kill.assert_awaited_once_with((-100, 42))
    sm.clear_provider_session.assert_awaited_once_with((-100, 42))


@pytest.mark.asyncio
async def test_apply_callback_streaming_busy_blocks() -> None:
    """H3: a live streaming turn must block the reset, like tmux/queue busy."""
    tmux = AsyncMock()
    tmux.is_processing = lambda key: False
    tmux.is_active = lambda key: False
    tmux.kill = AsyncMock()
    mq = types.SimpleNamespace(is_busy=lambda key: False)
    sm = AsyncMock()
    sm.clear_provider_session = AsyncMock()
    stream = _stream(active=True, busy=True)

    cb = types.SimpleNamespace(
        data="sysprompt_apply:-100:42",
        message=_cb_message(-100, 42),
        answer=AsyncMock(),
    )

    await on_sysprompt_apply_click(cb, tmux, mq, sm, stream)

    sm.clear_provider_session.assert_not_awaited()
    stream.kill.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_callback_dm_key_parsed() -> None:
    tmux = AsyncMock()
    tmux.is_processing = lambda key: False
    tmux.is_active = lambda key: False
    tmux.kill = AsyncMock()
    mq = types.SimpleNamespace(is_busy=lambda key: False)
    sm = AsyncMock()
    sm.clear_provider_session = AsyncMock()

    stream = _stream(active=False)
    cb = types.SimpleNamespace(
        data="sysprompt_apply:555:none",
        message=_cb_message(555, None),
        answer=AsyncMock(),
    )

    await on_sysprompt_apply_click(cb, tmux, mq, sm, stream)

    sm.clear_provider_session.assert_awaited_once_with((555, None))
    tmux.kill.assert_not_awaited()  # not active → must not kill
    stream.kill.assert_not_awaited()  # not active → must not kill


@pytest.mark.asyncio
async def test_apply_callback_rejects_mismatched_chat() -> None:
    """A callback whose embedded key differs from the button's chat is refused."""
    tmux = AsyncMock()
    tmux.is_processing = lambda key: False
    tmux.is_active = lambda key: True
    tmux.kill = AsyncMock()
    mq = types.SimpleNamespace(is_busy=lambda key: False)
    sm = AsyncMock()
    sm.clear_provider_session = AsyncMock()

    stream = _stream(active=True)
    cb = types.SimpleNamespace(
        data="sysprompt_apply:-100:42",  # targets chat -100
        message=_cb_message(-999, 42),  # but lives in chat -999
        answer=AsyncMock(),
    )

    await on_sysprompt_apply_click(cb, tmux, mq, sm, stream)

    sm.clear_provider_session.assert_not_awaited()
    tmux.kill.assert_not_awaited()
    stream.kill.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_callback_busy_blocks() -> None:
    tmux = AsyncMock()
    tmux.is_processing = lambda key: True
    tmux.kill = AsyncMock()
    mq = types.SimpleNamespace(is_busy=lambda key: False)
    sm = AsyncMock()
    sm.clear_provider_session = AsyncMock()

    stream = _stream(active=False)
    cb = types.SimpleNamespace(
        data="sysprompt_apply:-100:42",
        message=_cb_message(-100, 42),
        answer=AsyncMock(),
    )

    await on_sysprompt_apply_click(cb, tmux, mq, sm, stream)

    sm.clear_provider_session.assert_not_awaited()
    tmux.kill.assert_not_awaited()
