"""Tests for the checkpoint-on-reset feature.

When `checkpoint_on_reset` is enabled for a topic, a /new, /clear, or
"Новый чат" reset on a live tmux session first parks the old TUI under a
side session and asks it (via `checkpoint_prompt`) to write a background
checkpoint, then spawns the fresh session. The parked session is reaped
once it goes idle.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import types
from pathlib import Path

import pytest

from telegram_bot.core.services.topic_config import (
    _DEFAULT_CHECKPOINT_PROMPT,
    TopicConfig,
    TopicSettings,
    resolve_checkpoint_prompt,
)

# --- config parsing ---------------------------------------------------------


def test_checkpoint_defaults_inherit(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps({"topics": {"7": {"name": "X", "type": "assistant", "mode": "free"}}}),
        encoding="utf-8",
    )
    topic = TopicConfig(str(config_path), ".").get_topic(7)
    # None → inherit the bot-wide default
    assert topic.checkpoint_on_reset is None
    assert topic.checkpoint_prompt is None


def test_checkpoint_parsed_from_config(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "7": {
                        "name": "X",
                        "type": "assistant",
                        "mode": "free",
                        "checkpoint_on_reset": True,
                        "checkpoint_prompt": "  /чекпоинт  ",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    topic = TopicConfig(str(config_path), ".").get_topic(7)
    assert topic.checkpoint_on_reset is True
    # whitespace is trimmed on parse
    assert topic.checkpoint_prompt == "/чекпоинт"


def test_checkpoint_explicit_false_parsed(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "7": {
                        "name": "X",
                        "type": "assistant",
                        "mode": "free",
                        "checkpoint_on_reset": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    topic = TopicConfig(str(config_path), ".").get_topic(7)
    # explicit False is preserved (overrides a True global default)
    assert topic.checkpoint_on_reset is False


def test_checkpoint_on_reset_non_bool_inherits(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "7": {
                        "name": "X",
                        "type": "assistant",
                        "mode": "free",
                        "checkpoint_on_reset": "yes",
                        "checkpoint_prompt": "   ",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    topic = TopicConfig(str(config_path), ".").get_topic(7)
    # non-bool collapses to None (inherit), blank prompt collapses to None
    assert topic.checkpoint_on_reset is None
    assert topic.checkpoint_prompt is None


def _topic(**kw) -> TopicSettings:
    base = dict(name="", type="assistant", mode="free", cwd=None, mcp_config=None)
    base.update(kw)
    return TopicSettings(**base)


def test_resolve_disabled_returns_none() -> None:
    # off everywhere
    assert resolve_checkpoint_prompt(_topic(), global_enabled=False) is None
    # topic explicitly off beats global on
    assert resolve_checkpoint_prompt(_topic(checkpoint_on_reset=False), global_enabled=True) is None


def test_resolve_inherits_global_enable() -> None:
    # topic None inherits global True
    assert resolve_checkpoint_prompt(_topic(), global_enabled=True) == _DEFAULT_CHECKPOINT_PROMPT
    # topic True wins over global False
    assert (
        resolve_checkpoint_prompt(_topic(checkpoint_on_reset=True), global_enabled=False)
        == _DEFAULT_CHECKPOINT_PROMPT
    )


def test_resolve_prompt_precedence() -> None:
    # per-topic prompt wins
    assert (
        resolve_checkpoint_prompt(
            _topic(checkpoint_on_reset=True, checkpoint_prompt="/чекпоинт"),
            global_enabled=False,
            global_prompt="GLOBAL",
        )
        == "/чекпоинт"
    )
    # falls back to global prompt when topic prompt absent
    assert (
        resolve_checkpoint_prompt(
            _topic(checkpoint_on_reset=True),
            global_enabled=False,
            global_prompt="GLOBAL",
        )
        == "GLOBAL"
    )


# --- tmux parking + reaper --------------------------------------------------


def _make_manager():
    """A TmuxManager with only the state the methods under test touch.

    Bypasses the heavy __init__ so the real bound methods (and their
    `self._tmux_alive` etc. lookups) resolve against a genuine instance.
    """
    from telegram_bot.core.services.tmux_manager import TmuxManager

    mgr = TmuxManager.__new__(TmuxManager)
    mgr._checkpoint_reapers = set()
    return mgr


@pytest.mark.asyncio
async def test_park_and_checkpoint_renames_and_sends(monkeypatch) -> None:
    from telegram_bot.core.services import tmux_manager as tm

    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    sent: list[tuple[str, str, bool]] = []

    async def fake_send(name, text, *, submit_enter=True):
        sent.append((name, text, submit_enter))

    # Don't actually spawn a reaper background task in this unit test.
    async def fake_reap(self, name):
        return None

    monkeypatch.setattr(tm.subprocess, "run", fake_run)
    monkeypatch.setattr(tm, "send_text_to_tmux", fake_send)
    monkeypatch.setattr(tm.TmuxManager, "_reap_parked_session", fake_reap)

    mgr = _make_manager()
    parked = await mgr._park_and_checkpoint(
        old_name="cc-1-0", prompt="/чекпоинт", provider="claude"
    )

    assert parked is not None
    assert parked.startswith("cc-1-0-ckpt-")
    # rename happened first, targeting the live session name
    assert ["tmux", "rename-session", "-t", "cc-1-0", parked] in calls
    # the prompt was pasted into the parked (not original) session
    assert sent and sent[0][0] == parked and sent[0][1] == "/чекпоинт"
    # a reaper task was registered
    assert len(mgr._checkpoint_reapers) == 1


@pytest.mark.asyncio
async def test_park_and_checkpoint_skips_when_rename_fails(monkeypatch) -> None:
    from telegram_bot.core.services import tmux_manager as tm

    def fake_run(argv, *a, **k):
        if argv[:2] == ["tmux", "rename-session"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such session")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    sent: list = []

    async def fake_send(name, text, *, submit_enter=True):
        sent.append(name)

    monkeypatch.setattr(tm.subprocess, "run", fake_run)
    monkeypatch.setattr(tm, "send_text_to_tmux", fake_send)

    mgr = _make_manager()
    parked = await mgr._park_and_checkpoint(old_name="cc-1-0", prompt="x", provider="claude")

    assert parked is None
    assert sent == []  # nothing pasted when the session could not be parked
    assert not mgr._checkpoint_reapers


@pytest.mark.asyncio
async def test_reaper_kills_when_pane_goes_idle(monkeypatch) -> None:
    from telegram_bot.core.services import tmux_manager as tm

    # Collapse the timing knobs so the test runs instantly.
    monkeypatch.setattr(tm, "_CHECKPOINT_GRACE_SEC", 0.0)
    monkeypatch.setattr(tm, "_CHECKPOINT_POLL_SEC", 0.0)
    monkeypatch.setattr(tm, "_CHECKPOINT_IDLE_STABLE_SEC", 0.0)
    monkeypatch.setattr(tm, "_CHECKPOINT_MAX_RUNTIME_SEC", 5.0)

    panes = iter(["working 1s", "working 2s", "idle", "idle", "idle"])

    async def fake_capture(name):
        try:
            return next(panes)
        except StopIteration:
            return "idle"

    killed: list[str] = []

    def fake_run(argv, *a, **k):
        if argv[:2] == ["tmux", "kill-session"]:
            killed.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tm, "capture_pane", fake_capture)
    monkeypatch.setattr(tm.subprocess, "run", fake_run)
    monkeypatch.setattr(tm, "_tmux_alive_fn", lambda name: True)

    mgr = _make_manager()
    await asyncio.wait_for(mgr._reap_parked_session("cc-1-0-ckpt-dead"), timeout=2.0)

    assert killed == ["cc-1-0-ckpt-dead"]


@pytest.mark.asyncio
async def test_reaper_returns_early_when_session_already_gone(monkeypatch) -> None:
    from telegram_bot.core.services import tmux_manager as tm

    monkeypatch.setattr(tm, "_CHECKPOINT_GRACE_SEC", 0.0)
    monkeypatch.setattr(tm, "_CHECKPOINT_POLL_SEC", 0.0)

    killed: list[str] = []

    def fake_run(argv, *a, **k):
        if argv[:2] == ["tmux", "kill-session"]:
            killed.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tm.subprocess, "run", fake_run)
    monkeypatch.setattr(tm, "_tmux_alive_fn", lambda name: False)

    mgr = _make_manager()
    await asyncio.wait_for(mgr._reap_parked_session("cc-1-0-ckpt-gone"), timeout=2.0)

    # finally-block still issues a defensive kill-session
    assert killed == ["cc-1-0-ckpt-gone"]


@pytest.mark.asyncio
async def test_reap_orphans_kills_only_ckpt_sessions(monkeypatch) -> None:
    from telegram_bot.core.services import tmux_manager as tm

    listing = "cc-1-0\ncc-1-0-ckpt-aaaa\ncc-2-5\ncc-2-5-ckpt-bbbb\n"
    killed: list[str] = []

    def fake_run(argv, *a, **k):
        if argv[:2] == ["tmux", "list-sessions"]:
            return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr="")
        if argv[:2] == ["tmux", "kill-session"]:
            killed.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tm.subprocess, "run", fake_run)

    mgr = _make_manager()
    count = await mgr.reap_orphan_checkpoint_sessions()

    assert count == 2
    assert killed == ["cc-1-0-ckpt-aaaa", "cc-2-5-ckpt-bbbb"]


# --- subprocess mode --------------------------------------------------------


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = 0
        self.stdin = None

    async def wait(self) -> int:
        return 0


def _make_session_manager():
    """A SessionManager skeleton with just the fields the method touches."""
    from telegram_bot.core.services.claude import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    mgr._checkpoint_tasks = set()
    mgr._settings = types.SimpleNamespace(cc_query_timeout_sec=600)
    return mgr


@pytest.mark.asyncio
async def test_subprocess_checkpoint_spawns_resume(monkeypatch) -> None:
    from telegram_bot.core.services import claude as cl
    from telegram_bot.core.services.claude import SessionData

    key = (123, 7)
    mgr = _make_session_manager()
    mgr._sessions[key] = SessionData(session_id="abc-123", cwd="/work", engine="claude")

    captured: dict = {}

    def fake_build(prompt, session):
        captured["prompt"] = prompt
        captured["session_id"] = session.session_id
        return types.SimpleNamespace(
            argv=["claude", "--resume", session.session_id, "-p", "--", prompt],
            cwd=session.cwd,
            stdin_text=None,
        )

    spawned: dict = {}

    async def fake_exec(*argv, **kwargs):
        spawned["argv"] = list(argv)
        spawned["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    monkeypatch.setattr(cl.SessionManager, "_build_exec_command", staticmethod(fake_build))
    monkeypatch.setattr(cl.asyncio, "create_subprocess_exec", fake_exec)

    ok = await mgr.spawn_background_checkpoint(key, "/чекпоинт")

    assert ok is True
    assert captured["session_id"] == "abc-123"
    assert "--resume" in spawned["argv"] and "abc-123" in spawned["argv"]
    assert spawned["cwd"] == "/work"
    # let the watchdog task run to completion so it doesn't leak
    await asyncio.gather(*list(mgr._checkpoint_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_subprocess_checkpoint_skips_without_session(monkeypatch) -> None:
    from telegram_bot.core.services import claude as cl

    key = (123, 7)
    mgr = _make_session_manager()  # no session registered

    async def fake_exec(*argv, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("should not spawn without a live session")

    monkeypatch.setattr(cl.asyncio, "create_subprocess_exec", fake_exec)

    ok = await mgr.spawn_background_checkpoint(key, "/чекпоинт")
    assert ok is False
    assert not mgr._checkpoint_tasks
