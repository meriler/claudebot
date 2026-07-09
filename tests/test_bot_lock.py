"""Tests for BotInstanceLock — same-host single-instance lock."""

from __future__ import annotations

import json
import os
import socket

import pytest

from telegram_bot.core.utils.file_lock import (
    BotInstanceLock,
    BotInstanceLockError,
)


def test_acquire_writes_metadata(tmp_path):
    lock = BotInstanceLock("token-abc", runtime_dir=tmp_path)
    lock.acquire()
    try:
        data = json.loads(lock.lockfile.read_text())
        assert data["pid"] == os.getpid()
        assert data["hostname"] == socket.gethostname()
        assert "started_at" in data
    finally:
        lock.release()


def test_second_acquire_raises_with_pid(tmp_path):
    lock1 = BotInstanceLock("same-token", runtime_dir=tmp_path)
    lock1.acquire()
    try:
        lock2 = BotInstanceLock("same-token", runtime_dir=tmp_path)
        with pytest.raises(BotInstanceLockError) as exc_info:
            lock2.acquire()
        assert str(os.getpid()) in str(exc_info.value)
        assert socket.gethostname() in str(exc_info.value)
    finally:
        lock1.release()


def test_different_tokens_dont_conflict(tmp_path):
    a = BotInstanceLock("token-a", runtime_dir=tmp_path)
    b = BotInstanceLock("token-b", runtime_dir=tmp_path)
    a.acquire()
    try:
        b.acquire()  # different lockfile, must succeed
        b.release()
    finally:
        a.release()


def test_release_allows_reacquire(tmp_path):
    lock = BotInstanceLock("token-x", runtime_dir=tmp_path)
    lock.acquire()
    lock.release()
    # Second acquire on the same (now-released) file must succeed
    lock2 = BotInstanceLock("token-x", runtime_dir=tmp_path)
    lock2.acquire()
    lock2.release()


def test_lockfile_persists_after_release(tmp_path):
    """Lockfile is NOT deleted on release — kept for post-mortem diagnostics."""
    lock = BotInstanceLock("token-y", runtime_dir=tmp_path)
    lock.acquire()
    lockfile = lock.lockfile
    lock.release()
    assert lockfile.exists()


def test_context_manager(tmp_path):
    with BotInstanceLock("token-cm", runtime_dir=tmp_path) as lock:
        assert lock.lockfile.exists()
