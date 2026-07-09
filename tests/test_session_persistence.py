"""Tests for atomic state writes and fresh-channel persistence in SessionManager."""

import json
import os
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.claude import SessionManager


def _manager(tmp_path: Path) -> SessionManager:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        session_mapping_path=str(tmp_path / "session_mapping.json"),
    )
    return SessionManager(settings)


def test_atomic_write_json_replaces_not_truncates(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"old": true}')

    SessionManager._atomic_write_json(target, {"new": 1})

    assert json.loads(target.read_text()) == {"new": 1}
    # No leftover tmp file after a successful write
    assert not (tmp_path / "state.json.tmp").exists()


def test_atomic_write_json_sets_owner_only_permissions(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    SessionManager._atomic_write_json(target, {})
    assert (os.stat(target).st_mode & 0o777) == 0o600


def test_save_channel_sessions_survives_reload(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager._channel_sessions["123:456"] = "session-abc"
    manager._save_channel_sessions()

    fresh = _manager(tmp_path)
    fresh.load_mapping()
    assert fresh._channel_sessions == {"123:456": "session-abc"}


def test_fresh_channels_persist_across_restart(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager._channel_sessions["123:456"] = "session-abc"
    manager._fresh_channels.add("777:1")
    manager._save_channel_sessions()

    restarted = _manager(tmp_path)
    restarted.load_mapping()
    assert restarted._fresh_channels == {"777:1"}
    # The reserved key must not leak into the session mapping itself
    assert "__fresh__" not in restarted._channel_sessions


def test_consume_fresh_start_persists_consumption(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager._fresh_channels.add("123:456")
    manager._save_channel_sessions()

    assert manager.consume_fresh_start((123, 456)) is True
    assert manager.consume_fresh_start((123, 456)) is False

    restarted = _manager(tmp_path)
    restarted.load_mapping()
    assert restarted._fresh_channels == set()


def test_legacy_channel_sessions_without_fresh_key_load_fine(tmp_path: Path) -> None:
    path = tmp_path / "channel_sessions.json"
    path.write_text(json.dumps({"1:2": "sid"}))

    manager = _manager(tmp_path)
    manager.load_mapping()
    assert manager._channel_sessions == {"1:2": "sid"}
    assert manager._fresh_channels == set()


def test_save_mapping_writes_atomically(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager._msg_sessions[42] = "session-xyz"
    manager.save_mapping()

    data = json.loads((tmp_path / "session_mapping.json").read_text())
    assert data == {"42": "session-xyz"}
    assert (os.stat(tmp_path / "session_mapping.json").st_mode & 0o777) == 0o600
