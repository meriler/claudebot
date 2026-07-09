"""M10 (audit 2026-07-02): FileLock/AsyncFileLock must NOT unlink the lock file
on release — deleting it opens an unlink-race that breaks mutual exclusion.

These verify the observable behaviour: the lock file survives release, and
sequential acquire/release cycles still work (the lock is re-usable).
"""

from pathlib import Path

from telegram_bot.core.utils.file_lock import AsyncFileLock, FileLock


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


def test_sync_lock_file_survives_release(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    lock_path = _lock_path(target)
    with FileLock(target):
        assert lock_path.exists()
    # The fix: the lock file is NOT removed on release (unlink-race guard).
    assert lock_path.exists()


def test_sync_lock_reusable_across_cycles(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    for _ in range(3):
        with FileLock(target):
            pass  # acquire + release repeatedly must not error


async def test_async_lock_file_survives_release(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    lock_path = _lock_path(target)
    async with AsyncFileLock(target):
        assert lock_path.exists()
    assert lock_path.exists()


async def test_async_lock_reusable_across_cycles(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    for _ in range(3):
        async with AsyncFileLock(target):
            pass
