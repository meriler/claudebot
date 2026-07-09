"""Tests for the thinking-trigger normalization (`normalize_thinking_trigger`)."""

from __future__ import annotations

import pytest

from telegram_bot.core.utils.text_normalize import normalize_thinking_trigger


@pytest.mark.parametrize(
    "src,expected",
    [
        ("ultra think", "ultrathink"),
        ("Ultra Think", "ultrathink"),
        ("ULTRA THINK", "ultrathink"),
        ("ultra  think", "ultrathink"),  # double space
        ("ultra\nthink", "ultrathink"),  # line break between parts
        (
            "разберись с этим, ultra think пожалуйста",
            "разберись с этим, ultrathink пожалуйста",
        ),
        ("ultra think про задачу", "ultrathink про задачу"),
    ],
)
def test_glues_split_trigger(src: str, expected: str) -> None:
    assert normalize_thinking_trigger(src) == expected


@pytest.mark.parametrize(
    "src",
    [
        "ultrathink",  # already correct — no space, untouched
        "think hard",  # different trigger, not ours
        "ультра синк",  # cyrillic, not the latin keyword
        "ultrasonic think tank",  # \b guards: not "ultra"+ws+"think"
        "обычное сообщение без триггера",
    ],
)
def test_leaves_other_text_unchanged(src: str) -> None:
    assert normalize_thinking_trigger(src) == src


def test_replaces_every_occurrence() -> None:
    result = normalize_thinking_trigger("ultra think и ещё раз Ultra  Think")
    assert result == "ultrathink и ещё раз ultrathink"
