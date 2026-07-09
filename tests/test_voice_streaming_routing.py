"""Voice handler routing in streaming mode.

Regression for the bug where dictated (voice) messages were NOT injected
mid-turn in streaming mode — they fell through to the queue and waited for the
running turn to finish, instead of steering it like typed text does. The fix
mirrors text.py's `send_to_streaming_if_busy` branch into the voice batch
callback. These tests drive that callback directly by capturing it from
`forward_batcher.add_voice`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram_bot.core.handlers import voice as voice_mod


async def _drive_voice_batch(monkeypatch, *, exec_mode: str, streaming_busy: bool):
    """Run handle_voice, capture the on_voice_batch closure, invoke it once.

    Returns (streaming_call, tmux_call, enqueue_call) MagicMocks so each test
    can assert which routing branch fired.
    """
    captured: dict = {}

    forward_batcher = MagicMock()
    forward_batcher.get_comment.return_value = ["[Voice, transcription]: разберись ultra think"]

    def add_voice(key, message, recognizing_msg, cb):
        captured["cb"] = cb

    forward_batcher.add_voice.side_effect = add_voice

    # Patch the routing helpers imported into the voice module namespace. Reply-
    # to-resume routing (ensure_ready + switch) now lives in resolve_reply_routing;
    # stub it to "proceed, no reply target" so these tests focus on the hot-path
    # streaming/tmux/enqueue branches.
    monkeypatch.setattr(
        voice_mod, "resolve_reply_routing", AsyncMock(return_value=(False, None, False))
    )
    streaming_call = AsyncMock(return_value=streaming_busy)
    monkeypatch.setattr(voice_mod, "send_to_streaming_if_busy", streaming_call)
    tmux_call = AsyncMock(return_value=False)
    monkeypatch.setattr(voice_mod, "send_to_tmux_if_active", tmux_call)
    enqueue_call = MagicMock()
    monkeypatch.setattr(voice_mod, "enqueue_prompt", enqueue_call)

    session_manager = MagicMock()
    session_manager.reply_requires_provider_switch.return_value = False

    topic_config = MagicMock()
    topic_config.get_topic.return_value = SimpleNamespace(
        exec_mode=exec_mode, attribute_senders="auto"
    )

    message = MagicMock()
    message.voice = SimpleNamespace(file_size=1000)
    message.from_user = SimpleNamespace(id=1)
    message.answer = AsyncMock(return_value=MagicMock())

    last_voice = MagicMock()
    last_voice.reply_to_message = None
    last_voice.answer = AsyncMock()

    await voice_mod.handle_voice(
        message=message,
        bot=MagicMock(),
        session_manager=session_manager,
        transcriber=MagicMock(),
        forward_batcher=forward_batcher,
        message_queue=MagicMock(),
        tmux_manager=MagicMock(),
        topic_config=topic_config,
        settings=SimpleNamespace(allowed_user_ids=[1], attribute_senders="auto"),
        streaming_manager=MagicMock(),
        inbox_reply_handler=None,
    )

    # Invoke the captured batch callback with a single-voice snapshot.
    await captured["cb"]([(last_voice, MagicMock())])
    return streaming_call, tmux_call, enqueue_call


async def test_streaming_busy_injects_voice_not_enqueue(monkeypatch) -> None:
    streaming_call, _tmux, enqueue_call = await _drive_voice_batch(
        monkeypatch, exec_mode="streaming", streaming_busy=True
    )
    streaming_call.assert_awaited_once()
    # The dictated prompt steers the live turn; it must NOT be queued.
    enqueue_call.assert_not_called()
    # And the "ultra think" trigger reached the injector glued together.
    injected_prompt = streaming_call.await_args.args[1]
    assert "ultrathink" in injected_prompt
    assert "ultra think" not in injected_prompt


async def test_streaming_idle_falls_through_to_enqueue(monkeypatch) -> None:
    streaming_call, _tmux, enqueue_call = await _drive_voice_batch(
        monkeypatch, exec_mode="streaming", streaming_busy=False
    )
    streaming_call.assert_awaited_once()  # checked, but no live turn
    enqueue_call.assert_called_once()  # starts a turn via the queue


async def test_non_streaming_skips_inject_branch(monkeypatch) -> None:
    streaming_call, _tmux, enqueue_call = await _drive_voice_batch(
        monkeypatch, exec_mode="subprocess", streaming_busy=True
    )
    # `and` short-circuits on exec_mode != "streaming": injector never consulted.
    streaming_call.assert_not_awaited()
    enqueue_call.assert_called_once()
