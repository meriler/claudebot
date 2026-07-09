"""Shared health counters for the healthcheck and heartbeat loops."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

_DAY_SECONDS = 24 * 3600


@dataclass
class HealthState:
    """Counters shared by the healthcheck loop (writer) and heartbeat loop (reader)."""

    started_at: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    pool_resets_total: int = 0
    pool_resets_today: int = 0
    pool_resets_day_start: float = field(default_factory=time.time)
    last_pool_reset_at: float | None = None
    last_healthcheck_ok_at: float | None = None
    telegram_reachable: bool = True

    def note_success(self) -> None:
        self.consecutive_failures = 0
        self.telegram_reachable = True
        self.last_healthcheck_ok_at = time.time()

    def note_failure(self) -> int:
        self.consecutive_failures += 1
        self.telegram_reachable = False
        return self.consecutive_failures

    def note_pool_reset(self) -> None:
        self._roll_day_if_needed()
        self.pool_resets_total += 1
        self.pool_resets_today += 1
        self.last_pool_reset_at = time.time()

    def _roll_day_if_needed(self) -> None:
        now = time.time()
        if now - self.pool_resets_day_start >= _DAY_SECONDS:
            self.pool_resets_day_start = now
            self.pool_resets_today = 0

    @property
    def uptime_seconds(self) -> int:
        return int(time.time() - self.started_at)

    def status(self) -> str:
        """FR-18: ok / degraded (shutdown is set explicitly by the sender)."""
        if self.consecutive_failures > 0:
            return "degraded"
        if self.last_pool_reset_at is not None and time.time() - self.last_pool_reset_at < 60:
            return "degraded"
        return "ok"
