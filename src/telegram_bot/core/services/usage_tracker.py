"""UsageTracker — pinned Telegram message with live CC usage stats.

Supports two modes:
- **tmux mode**: polls `tmux capture-pane` to read the CC TUI status bar.
- **subprocess mode**: receives usage data from CC stream-json events
  (`rate_limit_event` and `result` with `modelUsage`/`total_cost_usd`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from telegram_bot.core.messages import t
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))

# Regex for the CC TUI status bar captured via `tmux capture-pane -p`.
# Example line (after ANSI strip):
#   ~/cc  Opus 4.6 (1M context) ░░░░░░░░░░ 5% 5h ░░░░░ 8% ⟳3h2m  7d ▓░░░░ 24% ⟳1d11h
_STATUS_RE = re.compile(
    r"(\S+(?:\s+\S+)?)"  # (1) model name, e.g. "Opus 4.6" or "Sonnet 4.6"
    r"(?:\s+(\([^)]+\)))?\s+"  # (2) optional "(1M context)"
    r"[░▓]+"  # context bar
    r"\s+(\d+)%"  # (3) context %
    r"\s+5h\s+"  # literal "5h"
    r"[░▓]+"  # 5h bar
    r"\s+(\d+)%"  # (4) 5h %
    r"\s+⟳(\S+)"  # (5) 5h reset
    r"\s+7d\s+"  # literal "7d"
    r"[░▓]+"  # 7d bar
    r"\s+(\d+)%"  # (6) 7d %
    r"\s+⟳(\S+)"  # (7) 7d reset
)


@dataclass(frozen=True, slots=True)
class UsageData:
    model: str
    context_pct: int
    rate_5h_pct: int
    rate_5h_resets: str
    rate_7d_pct: int
    rate_7d_resets: str


@dataclass(frozen=True, slots=True)
class SubprocessUsageData:
    """Usage data extracted from CC stream-json events."""

    model: str = ""
    rl_5h_status: str = ""
    rl_5h_resets_at: int = 0
    rl_7d_status: str = ""
    rl_7d_resets_at: int = 0
    context_pct: int = 0


@dataclass
class _TrackerState:
    channel_key: ChannelKey
    chat_id: int
    thread_id: int | None
    tmux_session_name: str
    message_id: int
    task: asyncio.Task[None]
    started_at: str
    last_data: UsageData | None = None


@dataclass
class _SubprocessTrackerState:
    channel_key: ChannelKey
    chat_id: int
    thread_id: int | None
    message_id: int
    started_at: str
    last_data: SubprocessUsageData = field(default_factory=SubprocessUsageData)
    session_id: str = ""


def _bar(pct: int, length: int = 10) -> str:
    filled = pct * length // 100
    return "█" * filled + "░" * (length - filled)


def _format_pin(
    data: UsageData | None,
    started_at: str,
    finished: bool = False,
    tmux_session: str = "",
) -> str:
    header = t("ui.usage_finished") if finished else "\U0001f4ca"
    model_str = f" {data.model}" if data else ""
    session_line = f"\n🔗 tmux: {tmux_session}" if tmux_session else ""
    started_label = t("ui.usage_started")
    if data is None:
        return (
            f"{header}{model_str}\n"
            f"{t('ui.usage_context_empty')}\n"
            f"⏱ 5h: —\n"
            f"\U0001f4c5 7d: —\n"
            f"\U0001f552 {started_label}: {started_at}"
            f"{session_line}"
        )
    ctx_bar = _bar(data.context_pct)
    return (
        f"{header}{model_str}\n"
        f"\U0001f9e0 {ctx_bar} {data.context_pct}%\n"
        f"⏱ 5h: {data.rate_5h_pct}% (↻{data.rate_5h_resets})\n"
        f"\U0001f4c5 7d: {data.rate_7d_pct}% (↻{data.rate_7d_resets})\n"
        f"\U0001f552 {started_label}: {started_at}"
        f"{session_line}"
    )


def _format_resets_at(epoch: int) -> str:
    if not epoch:
        return "—"
    dt = datetime.fromtimestamp(epoch, tz=_MSK)
    now = datetime.now(_MSK)
    delta = dt - now
    if delta.total_seconds() <= 0:
        return t("ui.usage_now")
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes = remainder // 60
    if hours > 0:
        return t("ui.usage_hours_minutes", hours=hours, minutes=minutes)
    return t("ui.usage_minutes", minutes=minutes)


def _rl_line(label: str, status: str, resets_at: int) -> str:
    if not status:
        return f"{label}: —"
    icon = "✅" if status == "allowed" else "⚠️"
    resets = _format_resets_at(resets_at)
    return f"{label}: {icon} {status} (↻{resets})"


def _format_subprocess_pin(
    data: SubprocessUsageData,
    started_at: str,
    finished: bool = False,
    session_id: str = "",
) -> str:
    header = t("ui.usage_finished") if finished else "📊"
    model_str = f" {data.model}" if data.model else ""
    parts = [f"{header}{model_str}"]
    if data.context_pct:
        ctx_bar = _bar(data.context_pct)
        parts.append(f"🧠 {ctx_bar} {data.context_pct}%")
    parts.append(f"⏱ {_rl_line('5h', data.rl_5h_status, data.rl_5h_resets_at)}")
    parts.append(f"📅 {_rl_line('7d', data.rl_7d_status, data.rl_7d_resets_at)}")
    parts.append(f"🕒 {t('ui.usage_started')}: {started_at}")
    if session_id:
        parts.append(f"🔗 session: {session_id}")
    return "\n".join(parts)


async def _read_usage(tmux_session_name: str) -> UsageData | None:
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "capture-pane", "-p", "-t", tmux_session_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=3.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in reversed((result.stdout or "").splitlines()):
        m = _STATUS_RE.search(line)
        if m:
            model_name = m.group(1)
            if m.group(2):
                model_name = f"{model_name} {m.group(2)}"
            return UsageData(
                model=model_name,
                context_pct=int(m.group(3)),
                rate_5h_pct=int(m.group(4)),
                rate_5h_resets=m.group(5),
                rate_7d_pct=int(m.group(6)),
                rate_7d_resets=m.group(7),
            )
    return None


class UsageTracker:
    def __init__(self, bot: Bot, *, update_interval: int = 30, pins_path: str = "") -> None:
        self._bot = bot
        self._update_interval = update_interval
        self._trackers: dict[ChannelKey, _TrackerState] = {}
        self._subprocess_trackers: dict[ChannelKey, _SubprocessTrackerState] = {}
        # Pinned usage-message ids survive restarts (else every restart spawns
        # a fresh pin and topics silt up with stale ones). Best effort.
        self._persistent_pins: dict[ChannelKey, int] = {}
        self._pins_path = Path(pins_path) if pins_path else None
        self._load_pins()

    def _load_pins(self) -> None:
        if self._pins_path is None or not self._pins_path.exists():
            return
        try:
            raw = json.loads(self._pins_path.read_text())
            for key, msg_id in raw.items():
                chat_s, _, thread_s = key.partition(":")
                thread = None if thread_s in ("", "None") else int(thread_s)
                self._persistent_pins[(int(chat_s), thread)] = int(msg_id)
        except (json.JSONDecodeError, ValueError, OSError):
            logger.warning("UsageTracker: failed to load pins from %s", self._pins_path)

    def _save_pins(self) -> None:
        if self._pins_path is None:
            return
        try:
            data = {f"{k[0]}:{k[1]}": v for k, v in self._persistent_pins.items()}
            tmp = self._pins_path.with_name(self._pins_path.name + ".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._pins_path)
        except OSError:
            logger.warning("UsageTracker: failed to save pins", exc_info=True)

    async def start(
        self,
        channel_key: ChannelKey,
        chat_id: int,
        thread_id: int | None,
        tmux_session_name: str,
    ) -> None:
        await self.stop(channel_key)
        started_at = datetime.now(_MSK).strftime("%H:%M")
        text = _format_pin(None, started_at, tmux_session=tmux_session_name)
        try:
            sent = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=thread_id,
                disable_notification=True,
            )
        except Exception:
            logger.warning("UsageTracker: failed to send pin message", exc_info=True)
            return

        try:
            await self._bot.pin_chat_message(
                chat_id=chat_id,
                message_id=sent.message_id,
                disable_notification=True,
            )
        except Exception:
            logger.warning("UsageTracker: failed to pin message (permissions?)", exc_info=True)

        task = asyncio.create_task(
            self._poll_loop(channel_key, chat_id, sent.message_id, tmux_session_name, started_at)
        )
        self._trackers[channel_key] = _TrackerState(
            channel_key=channel_key,
            chat_id=chat_id,
            thread_id=thread_id,
            tmux_session_name=tmux_session_name,
            message_id=sent.message_id,
            task=task,
            started_at=started_at,
        )
        logger.info(
            "UsageTracker started for %s (msg_id=%d, tmux=%s)",
            channel_key,
            sent.message_id,
            tmux_session_name,
        )

    async def start_subprocess(
        self,
        channel_key: ChannelKey,
        chat_id: int,
        thread_id: int | None,
    ) -> None:
        """Start usage tracking for subprocess mode (no tmux polling).

        Reuses the existing pinned message for this channel if available,
        editing it instead of creating a new one each time.
        """
        # Finalize any in-progress tracker without deleting the pin
        old = self._subprocess_trackers.pop(channel_key, None)
        if old is not None:
            self._persistent_pins[channel_key] = old.message_id
            self._save_pins()

        started_at = datetime.now(_MSK).strftime("%H:%M")
        text = _format_subprocess_pin(SubprocessUsageData(), started_at)
        existing_pin = self._persistent_pins.get(channel_key)

        if existing_pin is not None:
            try:
                await self._bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=existing_pin,
                )
                self._subprocess_trackers[channel_key] = _SubprocessTrackerState(
                    channel_key=channel_key,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    message_id=existing_pin,
                    started_at=started_at,
                )
                logger.info(
                    "UsageTracker subprocess reused pin for %s (msg_id=%d)",
                    channel_key,
                    existing_pin,
                )
                return
            except Exception:
                logger.info(
                    "UsageTracker: stale pin %d for %s, creating new",
                    existing_pin,
                    channel_key,
                )
                self._persistent_pins.pop(channel_key, None)
                self._save_pins()

        try:
            sent = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=thread_id,
                disable_notification=True,
            )
        except Exception:
            logger.warning("UsageTracker: failed to send subprocess pin", exc_info=True)
            return

        try:
            await self._bot.pin_chat_message(
                chat_id=chat_id,
                message_id=sent.message_id,
                disable_notification=True,
            )
        except Exception:
            logger.warning("UsageTracker: failed to pin subprocess message", exc_info=True)

        self._persistent_pins[channel_key] = sent.message_id
        self._save_pins()
        self._subprocess_trackers[channel_key] = _SubprocessTrackerState(
            channel_key=channel_key,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=sent.message_id,
            started_at=started_at,
        )
        logger.info(
            "UsageTracker subprocess started for %s (msg_id=%d)",
            channel_key,
            sent.message_id,
        )

    async def update_from_event(
        self,
        channel_key: ChannelKey,
        usage_info: dict[str, Any],
    ) -> None:
        """Update subprocess usage pin from a stream-json usage_data event."""
        state = self._subprocess_trackers.get(channel_key)
        if state is None:
            return

        rl = usage_info.get("rate_limit") or {}
        model_usage = usage_info.get("model_usage") or {}
        is_final = usage_info.get("is_final", False)

        cur = state.last_data
        model = cur.model
        context_pct = cur.context_pct
        rl_5h_status = cur.rl_5h_status
        rl_5h_resets = cur.rl_5h_resets_at
        rl_7d_status = cur.rl_7d_status
        rl_7d_resets = cur.rl_7d_resets_at

        if rl:
            rl_type = rl.get("rateLimitType", "")
            status = rl.get("status", "")
            resets = rl.get("resetsAt", 0)
            if "five" in rl_type:
                rl_5h_status = status
                rl_5h_resets = resets
            elif "daily" in rl_type or "week" in rl_type:
                rl_7d_status = status
                rl_7d_resets = resets

        if model_usage:
            model = next(iter(model_usage), model)
            info: Any = next(iter(model_usage.values()), {})
            if isinstance(info, dict):
                ctx_window = info.get("contextWindow", 0)
                if ctx_window > 0:
                    total_tokens = (
                        info.get("inputTokens", 0)
                        + info.get("outputTokens", 0)
                        + info.get("cacheReadInputTokens", 0)
                        + info.get("cacheCreationInputTokens", 0)
                    )
                    context_pct = min(100, total_tokens * 100 // ctx_window)

        new_data = SubprocessUsageData(
            model=model,
            rl_5h_status=rl_5h_status,
            rl_5h_resets_at=rl_5h_resets,
            rl_7d_status=rl_7d_status,
            rl_7d_resets_at=rl_7d_resets,
            context_pct=context_pct,
        )

        if new_data != cur:
            state.last_data = new_data
            text = _format_subprocess_pin(
                new_data,
                state.started_at,
                finished=is_final,
                session_id=state.session_id,
            )
            await self._edit(state.chat_id, state.message_id, text)

    def set_subprocess_session_id(self, channel_key: ChannelKey, session_id: str) -> None:
        state = self._subprocess_trackers.get(channel_key)
        if state is not None:
            state.session_id = session_id

    async def stop_subprocess(self, channel_key: ChannelKey) -> None:
        state = self._subprocess_trackers.pop(channel_key, None)
        if state is None:
            return
        self._persistent_pins[channel_key] = state.message_id
        self._save_pins()
        text = _format_subprocess_pin(
            state.last_data,
            state.started_at,
            finished=True,
            session_id=state.session_id,
        )
        await self._edit(state.chat_id, state.message_id, text)
        logger.info("UsageTracker subprocess stopped for %s", channel_key)

    async def stop(self, channel_key: ChannelKey) -> None:
        state = self._trackers.pop(channel_key, None)
        if state is None:
            await self.stop_subprocess(channel_key)
            return
        state.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await state.task
        data = await _read_usage(state.tmux_session_name)
        final_data = data or state.last_data
        text = _format_pin(
            final_data,
            state.started_at,
            finished=True,
            tmux_session=state.tmux_session_name,
        )
        await self._edit(state.chat_id, state.message_id, text)
        logger.info("UsageTracker stopped for %s", channel_key)

    async def stop_all(self) -> None:
        keys = list(self._trackers.keys())
        for key in keys:
            await self.stop(key)
        sp_keys = list(self._subprocess_trackers.keys())
        for key in sp_keys:
            await self.stop_subprocess(key)

    async def _poll_loop(
        self,
        channel_key: ChannelKey,
        chat_id: int,
        message_id: int,
        tmux_session_name: str,
        started_at: str,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._update_interval)
                data = await _read_usage(tmux_session_name)
                state = self._trackers.get(channel_key)
                if state is None:
                    break
                if data is not None and data != state.last_data:
                    text = _format_pin(data, started_at, tmux_session=tmux_session_name)
                    await self._edit(chat_id, message_id, text)
                    state.last_data = data
        except asyncio.CancelledError:
            pass

    async def _edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
            )
        except TelegramRetryAfter as e:
            logger.warning("UsageTracker flood wait: %ds", e.retry_after)
            await asyncio.sleep(e.retry_after)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.warning("UsageTracker edit failed: %s", e)
