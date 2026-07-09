"""Regression: every _reset_channel caller must forward streaming_manager.

Guards against the class of bug where a new required arg is added to
_reset_channel but a caller (e.g. the "Новый чат" reply button) is missed —
which crashed with TypeError in production.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import telegram_bot.core.handlers.commands as cmd_mod
import telegram_bot.core.handlers.mode as mode_mod


async def test_new_chat_button_forwards_streaming_manager() -> None:
    sentinel = MagicMock(name="streaming_manager")
    with patch.object(mode_mod, "_reset_channel", new=AsyncMock()) as rc:
        await mode_mod.handle_new_chat_button(
            message=MagicMock(),
            session_manager=MagicMock(),
            message_queue=MagicMock(),
            forward_batcher=MagicMock(),
            tmux_manager=MagicMock(),
            topic_config=MagicMock(),
            settings=MagicMock(),
            streaming_manager=sentinel,
        )
    rc.assert_awaited_once()
    assert sentinel in rc.await_args.args  # forwarded, not dropped


async def test_new_command_forwards_streaming_manager() -> None:
    sentinel = MagicMock(name="streaming_manager")
    with patch.object(cmd_mod, "_reset_channel", new=AsyncMock()) as rc:
        await cmd_mod.handle_new(
            message=MagicMock(),
            session_manager=MagicMock(),
            message_queue=MagicMock(),
            forward_batcher=MagicMock(),
            tmux_manager=MagicMock(),
            topic_config=MagicMock(),
            settings=MagicMock(),
            streaming_manager=sentinel,
        )
    rc.assert_awaited_once()
    assert sentinel in rc.await_args.args


async def test_clear_command_forwards_streaming_manager() -> None:
    sentinel = MagicMock(name="streaming_manager")
    with patch.object(cmd_mod, "_reset_channel", new=AsyncMock()) as rc:
        await cmd_mod.handle_clear(
            message=MagicMock(),
            session_manager=MagicMock(),
            message_queue=MagicMock(),
            forward_batcher=MagicMock(),
            tmux_manager=MagicMock(),
            topic_config=MagicMock(),
            settings=MagicMock(),
            streaming_manager=sentinel,
        )
    rc.assert_awaited_once()
    assert sentinel in rc.await_args.args
