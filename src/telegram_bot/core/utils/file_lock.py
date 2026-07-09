"""Cross-process file locking via fcntl.flock.

Provides sync FileLock (for scripts) and AsyncFileLock (for async bot code).
Lock file is {path}.lock — separate from the target file to avoid
conflicts with os.replace() during atomic writes.

Linux/macOS only (production and development are on Linux).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import TextIOWrapper
from pathlib import Path
from types import TracebackType


class FileLock:
    """Sync cross-process file lock via fcntl.flock.

    Usage::

        with FileLock("/path/to/data.json"):
            data = json.loads(Path("/path/to/data.json").read_text())
            data["key"] = "value"
            Path("/path/to/data.json").write_text(json.dumps(data))
    """

    def __init__(self, path: str | Path) -> None:
        self._lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
        self._fd: TextIOWrapper | None = None

    def __enter__(self) -> FileLock:
        self._fd = open(self._lock_path, "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None
            # Do NOT unlink the lock file. Deleting it on release creates an
            # unlink-race: a waiter that acquired the lock on the old inode is
            # still in its critical section while a third caller opens the path,
            # gets a NEW inode, and flock-succeeds immediately — two holders at
            # once. BotInstanceLock keeps its lockfile for the same reason. The
            # leftover zero-byte file is harmless. (M10, audit 2026-07-02.)


class AsyncFileLock:
    """Async cross-process file lock — flock via run_in_executor.

    Uses a dedicated ThreadPoolExecutor (not the default) to avoid
    blocking the executor pool during long lock waits.

    Usage::

        async with AsyncFileLock("/path/to/data.json"):
            # read-modify-write under lock
            ...
    """

    _shared_executor: ThreadPoolExecutor | None = None

    def __init__(self, path: str | Path, executor: ThreadPoolExecutor | None = None) -> None:
        self._lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
        self._executor = executor
        self._fd: TextIOWrapper | None = None

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is not None:
            return self._executor
        if AsyncFileLock._shared_executor is None:
            AsyncFileLock._shared_executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="flock"
            )
        return AsyncFileLock._shared_executor

    def _acquire(self) -> None:
        self._fd = open(self._lock_path, "w")  # noqa: SIM115
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def _release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None
            # See FileLock.__exit__: unlinking on release breaks mutual
            # exclusion (unlink-race). Keep the file. (M10, audit 2026-07-02.)

    async def __aenter__(self) -> AsyncFileLock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._get_executor(), self._acquire)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._get_executor(), self._release)


class BotInstanceLockError(RuntimeError):
    """Raised when another bot instance with the same token is already running on this host."""


class BotInstanceLock:
    """Same-host single-instance lock for the bot, keyed on bot token.

    Prevents two bot processes with the same Telegram token starting on the
    same machine (e.g. manual `nohup` + systemd, second systemd unit, IDE +
    service). Does NOT protect against cross-host duplicates — distinct hosts
    do not see each other's lockfiles. For cross-host safety, ensure each
    deployment uses a separate Telegram bot token.

    Metadata (pid, hostname, cwd, started_at) is written into the lockfile
    so the conflict error tells the operator who holds the lock. The lockfile
    is NOT deleted on release — it stays for post-mortem diagnostics.
    """

    def __init__(self, token: str, runtime_dir: Path) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        self._lockfile = Path(runtime_dir) / f"claudebot-{token_hash}.lock"
        self._fd: int | None = None

    @property
    def lockfile(self) -> Path:
        return self._lockfile

    def acquire(self) -> None:
        self._lockfile.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lockfile, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                metadata = json.loads(self._lockfile.read_text())
            except Exception:
                metadata = {"raw": "(unreadable)"}
            os.close(fd)
            raise BotInstanceLockError(
                "Another bot instance is already running on this host:\n"
                f"  PID:      {metadata.get('pid', '?')}\n"
                f"  hostname: {metadata.get('hostname', '?')}\n"
                f"  cwd:      {metadata.get('cwd', '?')}\n"
                f"  started:  {metadata.get('started_at', '?')}\n"
                f"If you're sure no bot is running, remove the lock file:\n"
                f"  rm {self._lockfile}"
            ) from None

        metadata = json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "cwd": str(Path.cwd()),
                "started_at": datetime.now(UTC).isoformat(),
            }
        )
        os.ftruncate(fd, 0)
        os.write(fd, metadata.encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> BotInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()
