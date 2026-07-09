"""H7 (audit 2026-07-02): one failing shutdown step must not skip the rest.

The shutdown was a flat await-sequence, so a TelegramNetworkError in an early
step (usage_tracker.stop_all) aborted everything after it, including the
critical session_manager.save_mapping(). _safe_shutdown_step isolates each step.
"""

from unittest.mock import AsyncMock, MagicMock

from telegram_bot.__main__ import _safe_shutdown_step


async def test_async_step_failure_is_swallowed() -> None:
    boom = AsyncMock(side_effect=RuntimeError("network down"))
    # Must not raise — the caller continues to the next step.
    await _safe_shutdown_step("boom", boom)
    boom.assert_awaited_once()


async def test_sync_step_failure_is_swallowed() -> None:
    boom = MagicMock(side_effect=ValueError("bad state"))
    await _safe_shutdown_step("boom", boom)
    boom.assert_called_once()


async def test_async_step_success_runs() -> None:
    ok = AsyncMock()
    await _safe_shutdown_step("ok", ok)
    ok.assert_awaited_once()


async def test_sequence_continues_past_a_failure() -> None:
    """The real bug: a later critical step still runs after an earlier failure."""
    calls: list[str] = []

    def failing() -> None:
        calls.append("failing")
        raise RuntimeError("first step down")

    def save_mapping() -> None:
        calls.append("save_mapping")

    await _safe_shutdown_step("failing", failing)
    await _safe_shutdown_step("save_mapping", save_mapping)

    assert calls == ["failing", "save_mapping"]  # critical step reached
