"""Tests for append-style system-prompt assembly + tmux parity (TASK-3)."""

from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.claude import _TELEGRAM_SYSTEM_PROMPT, SessionManager
from telegram_bot.core.services.topic_config import TopicConfig


def _make_manager(tmp_path: Path, config: dict) -> SessionManager:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    tc = TopicConfig(str(config_path), str(tmp_path))
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        project_root=str(tmp_path),
        default_cwd=".",
    )
    return SessionManager(settings, tc)


def test_topic_prompt_appends_persona_and_custom(tmp_path: Path) -> None:
    mgr = _make_manager(
        tmp_path,
        {
            "default_system_prompt": "ПЕРСОНА",
            "topics": {
                "42": {"name": "D", "type": "project", "mode": "free", "system_prompt": "КАСТОМ"}
            },
        },
    )

    result = mgr._topic_system_prompt(42)

    assert _TELEGRAM_SYSTEM_PROMPT in result
    assert "ПЕРСОНА" in result
    assert "КАСТОМ" in result
    # order: base, persona, custom
    assert result.index("ПЕРСОНА") < result.index("КАСТОМ")


def test_named_topic_without_custom_gets_auto_theme(tmp_path: Path) -> None:
    """A named topic with no custom prompt still gets its name as the theme."""
    mgr = _make_manager(
        tmp_path,
        {
            "default_system_prompt": "ПЕРСОНА",
            "topics": {"42": {"name": "Коты", "type": "project", "mode": "free"}},
        },
    )

    result = mgr._topic_system_prompt(42)

    assert "ПЕРСОНА" in result
    # name is injected as the conversation theme even without a custom prompt
    assert "Коты" in result
    assert "subject" in result.lower()
    # order: persona before the auto-theme line
    assert result.index("ПЕРСОНА") < result.index("Коты")


def test_unnamed_topic_no_auto_theme(tmp_path: Path) -> None:
    """An empty name adds no auto-theme line — persona only."""
    mgr = _make_manager(
        tmp_path,
        {
            "default_system_prompt": "ПЕРСОНА",
            "topics": {"42": {"name": "", "type": "project", "mode": "free"}},
        },
    )

    result = mgr._topic_system_prompt(42)

    assert result == f"{_TELEGRAM_SYSTEM_PROMPT}\n\nПЕРСОНА"


def test_freeform_topic_skips_auto_theme(tmp_path: Path) -> None:
    """Free-for-all topics (Рандом/general/etc.) get no on-topic focus."""
    mgr = _make_manager(
        tmp_path,
        {
            "default_system_prompt": "ПЕРСОНА",
            "topics": {"8": {"name": "Рандом", "type": "project", "mode": "free"}},
        },
    )

    result = mgr._topic_system_prompt(8)

    # name present but NOT injected as a theme, no focus instruction
    assert result == f"{_TELEGRAM_SYSTEM_PROMPT}\n\nПЕРСОНА"
    assert "subject" not in result.lower()


def test_dm_uses_per_chat_prompt(tmp_path: Path) -> None:
    mgr = _make_manager(
        tmp_path,
        {"default_system_prompt": "ПЕРСОНА", "chat_prompts": {"100": "ЛИЧНЫЙ"}, "topics": {}},
    )

    result = mgr._topic_system_prompt(None, 100)

    assert "ПЕРСОНА" in result
    assert "ЛИЧНЫЙ" in result
    assert result.index("ПЕРСОНА") < result.index("ЛИЧНЫЙ")


def test_dm_without_per_chat_prompt(tmp_path: Path) -> None:
    mgr = _make_manager(
        tmp_path,
        {"default_system_prompt": "ПЕРСОНА", "topics": {}},
    )

    result = mgr._topic_system_prompt(None, 100)

    assert result == f"{_TELEGRAM_SYSTEM_PROMPT}\n\nПЕРСОНА"


def test_tmux_startup_args_carry_topic_prompt(tmp_path: Path) -> None:
    mgr = _make_manager(
        tmp_path,
        {
            "default_system_prompt": "ПЕРСОНА",
            "topics": {
                "42": {"name": "D", "type": "project", "mode": "free", "system_prompt": "КАСТОМ"}
            },
        },
    )

    args = mgr.build_tmux_startup_args(
        mode="free",
        session_id_new="00000000-0000-0000-0000-000000000001",
        thread_id=42,
    )

    assert "--append-system-prompt" in args
    idx = args.index("--append-system-prompt")
    prompt = args[idx + 1]
    assert "КАСТОМ" in prompt
    assert "ПЕРСОНА" in prompt


def test_tmux_startup_args_default_context(tmp_path: Path) -> None:
    mgr = _make_manager(
        tmp_path,
        {"default_system_prompt": "ПЕРСОНА", "topics": {}},
    )

    args = mgr.build_tmux_startup_args(
        mode="free",
        session_id_new="00000000-0000-0000-0000-000000000002",
    )

    idx = args.index("--append-system-prompt")
    prompt = args[idx + 1]
    # No thread/chat context → base + global persona, no custom layer
    assert prompt == f"{_TELEGRAM_SYSTEM_PROMPT}\n\nПЕРСОНА"
