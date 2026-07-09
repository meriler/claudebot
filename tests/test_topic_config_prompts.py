"""Tests for per-context system-prompt storage in TopicConfig (TASK-1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_bot.core.services.topic_config import TopicConfig


def _write(config_path: Path, data: dict) -> None:
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_legacy_config_without_chat_prompts_loads(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write(config_path, {"topics": {"42": {"name": "Demo", "type": "project", "mode": "free"}}})

    tc = TopicConfig(str(config_path), ".")

    assert tc.get_chat_prompt(123) is None
    assert tc.get_chat_prompt(None) is None
    assert tc.get_topic(42).name == "Demo"


def test_chat_prompts_parsed(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write(config_path, {"chat_prompts": {"555": "будь лаконичнее"}, "topics": {}})

    tc = TopicConfig(str(config_path), ".")

    assert tc.get_chat_prompt(555) == "будь лаконичнее"
    assert tc.get_chat_prompt(999) is None


def test_blank_chat_prompt_ignored(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write(config_path, {"chat_prompts": {"1": "   "}, "topics": {}})

    tc = TopicConfig(str(config_path), ".")

    assert tc.get_chat_prompt(1) is None


@pytest.mark.asyncio
async def test_update_chat_prompt_roundtrip(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write(config_path, {"topics": {}})
    tc = TopicConfig(str(config_path), ".")

    assert await tc.update_chat_prompt(777, "пиши рецептами")
    # mtime cache picks up the new write without restart
    assert tc.get_chat_prompt(777) == "пиши рецептами"

    # File stays valid JSON and contains the key
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["chat_prompts"]["777"] == "пиши рецептами"


@pytest.mark.asyncio
async def test_update_chat_prompt_reset_removes_key(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write(config_path, {"chat_prompts": {"5": "X"}, "topics": {}})
    tc = TopicConfig(str(config_path), ".")

    assert await tc.update_chat_prompt(5, None)
    assert tc.get_chat_prompt(5) is None
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert "5" not in on_disk.get("chat_prompts", {})


@pytest.mark.asyncio
async def test_update_chat_prompt_blank_resets(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write(config_path, {"chat_prompts": {"5": "X"}, "topics": {}})
    tc = TopicConfig(str(config_path), ".")

    assert await tc.update_chat_prompt(5, "   ")
    assert tc.get_chat_prompt(5) is None


@pytest.mark.asyncio
async def test_update_system_prompt_set_and_reset(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write(config_path, {"topics": {"42": {"name": "Demo", "type": "project", "mode": "free"}}})
    tc = TopicConfig(str(config_path), ".")

    assert await tc.update_system_prompt(42, "отвечай как ревьюер")
    assert tc.get_topic(42).system_prompt == "отвечай как ревьюер"

    assert await tc.update_system_prompt(42, None)
    assert tc.get_topic(42).system_prompt is None
    # other fields preserved
    assert tc.get_topic(42).name == "Demo"


@pytest.mark.asyncio
async def test_update_preserves_default_system_prompt(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write(
        config_path,
        {"default_system_prompt": "персона", "chat_prompts": {}, "topics": {}},
    )
    tc = TopicConfig(str(config_path), ".")

    await tc.update_chat_prompt(9, "txt")

    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["default_system_prompt"] == "персона"
    assert on_disk["chat_prompts"]["9"] == "txt"
