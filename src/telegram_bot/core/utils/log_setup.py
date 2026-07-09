"""Logging configuration: optional self-rotating file + polling-noise throttle.

Rotation must live inside the process: under launchd the StandardOutPath fd is
held by launchd, so external rotation (newsyslog/mv) leaves the bot writing to
the old inode forever. With LOG_FILE set the bot owns the file and rotates it
itself; launchd's StandardOutPath then only catches early-startup crashes.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 3

# Repeated polling failures (Telegram unreachable — normal here) are collapsed:
# first occurrence logs, repeats within the window are counted and summarized.
_THROTTLE_WINDOW_SEC = 300.0
_POLL_ERROR_MARKER = "Failed to fetch updates"


class PollingNoiseFilter(logging.Filter):
    """Throttle identical 'Failed to fetch updates' bursts to one line per window."""

    def __init__(self, window_sec: float = _THROTTLE_WINDOW_SEC) -> None:
        super().__init__()
        self._window_sec = window_sec
        self._last_emit = 0.0
        self._suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if _POLL_ERROR_MARKER not in record.getMessage():
            if self._suppressed:
                # Different message arrived (e.g. polling recovered) — flush the count.
                # Reset BEFORE emitting: the emit re-enters this filter.
                count, self._suppressed = self._suppressed, 0
                logging.getLogger(record.name).info(
                    "(suppressed %d repeated polling errors)", count
                )
            return True
        now = time.monotonic()
        if now - self._last_emit >= self._window_sec:
            if self._suppressed:
                record.msg = f"{record.msg} (+{self._suppressed} suppressed)"
                self._suppressed = 0
            self._last_emit = now
            return True
        self._suppressed += 1
        return False


def setup_logging(log_file: str = "") -> None:
    handler: logging.Handler
    if log_file:
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("aiogram.dispatcher").addFilter(PollingNoiseFilter())
