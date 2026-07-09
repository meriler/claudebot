"""Tests for startup preflight checks."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from telegram_bot.core.config import Settings
from telegram_bot.core.services.preflight import (
    PreflightError,
    _check_bot_token,
    _check_claude_onboarding,
    _check_tmp,
    _check_vault,
    run_health_checks,
    run_startup_preflight,
)


def make_settings(**kwargs) -> Settings:
    base = {
        "telegram_bot_token": "test-token",
        "allowed_user_ids": [],
        "vault": "",
    }
    base.update(kwargs)
    return Settings(**base)


def test_vault_empty_is_optional_and_passes():
    s = make_settings(vault="")
    r = _check_vault(s, writable=False)
    assert r.ok
    assert "optional" in r.detail


def test_vault_nonexistent_fails(tmp_path):
    s = make_settings(vault=str(tmp_path / "does-not-exist"))
    r = _check_vault(s, writable=False)
    assert not r.ok
    assert "does not exist" in r.detail


def test_vault_valid_passes(tmp_path):
    s = make_settings(vault=str(tmp_path))
    r = _check_vault(s, writable=True)
    assert r.ok


def test_tmp_writable_passes():
    r = _check_tmp(writable=True)
    assert r.ok


def test_bot_token_empty_fails():
    s = make_settings(telegram_bot_token="")
    # pydantic-settings requires telegram_bot_token to be non-empty if defined;
    # but the field is required, so make_settings with "" works via Settings(...).
    # We bypass validation via direct check.
    r = _check_bot_token(s)
    assert not r.ok


def test_bot_token_set_passes():
    s = make_settings(telegram_bot_token="abc123")
    r = _check_bot_token(s)
    assert r.ok


def test_claude_onboarding_missing_file_fails(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with patch.object(Path, "home", return_value=fake_home):
        r = _check_claude_onboarding()
    assert not r.ok
    assert "config not found" in r.detail


def test_claude_onboarding_not_completed_fails(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": False}))
    with patch.object(Path, "home", return_value=fake_home):
        r = _check_claude_onboarding()
    assert not r.ok
    assert "onboarding not completed" in r.detail


def test_claude_onboarding_completed_passes(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    with patch.object(Path, "home", return_value=fake_home):
        r = _check_claude_onboarding()
    assert r.ok


def test_run_startup_preflight_raises_on_failure(tmp_path):
    # An explicitly set but nonexistent VAULT path must still fail startup.
    s = make_settings(vault=str(tmp_path / "does-not-exist"), telegram_bot_token="x")
    with pytest.raises(PreflightError) as exc_info:
        run_startup_preflight(s, topic_config=None)
    assert "vault" in str(exc_info.value)


def test_run_health_checks_returns_list(tmp_path):
    s = make_settings(vault=str(tmp_path), telegram_bot_token="x")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    with patch.object(Path, "home", return_value=fake_home):
        # claude_cli check will run shutil.which("claude") which depends on env
        # so we only verify it's a list and contains expected names
        results = run_health_checks(s, topic_config=None)
    assert isinstance(results, list)
    names = {r.name for r in results}
    assert "vault" in names
    assert "tmp" in names
    assert "bot_token" in names
