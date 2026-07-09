"""StreamingManager — per-channel owner of persistent streaming Claude sessions.

The `streaming` exec mode's coordinator, analogous to TmuxManager for tmux. It
owns one `StreamingSession` per channel, builds its argv via
`SessionManager.build_streaming_argv`, and bridges the raw stream-json events to
the bot's `StreamEvent` contract (via `parse_cc_event`) so the existing
`send_streaming_response` rendering works unchanged.

Lifecycle decisions (per the design):
- `cancel` sends `control_request interrupt` — it does NOT kill the process, so
  the live session survives for the next message. SIGKILL is reserved for
  `kill` (hard reset: /new, engine/model/prompt change, health failure).
- The first message of a FRESH (non-resumed) process is prefixed with the mode
  prompt + Telegram context (via `_build_full_prompt`), exactly like the
  one-shot path's fresh run; subsequent messages in the same live process are
  raw (context already established). A process spawned with `--resume <sid>`
  starts already-primed (context restored), so its first message is raw too.
- Idle reaper: a live process holds ~one `claude`'s worth of RAM while it sits,
  the same footprint as tmux. To avoid that cost when a topic is unused, a
  background reaper kills sessions idle past `idle_timeout_sec`; the next message
  respawns with `--resume` (context restored, ~1-2s warm-up). Busy turns are
  never reaped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from telegram_bot.core.config import Settings
from telegram_bot.core.services.cc_events import StreamEvent, parse_cc_event
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.streaming_session import StreamingSession
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

BotEventCallback = Callable[[StreamEvent], Awaitable[None] | None]

# Prepended to the FIRST message of a fresh streaming session. The "auto-Ctrl+B"
# answer: there is no external way to background a running foreground command, so
# we nudge the model to background long commands itself — then control returns
# at once and follow-up messages are picked up between tools instead of waiting
# out a blocking command. Soft guidance (the model may still run short commands
# in foreground); placed in the first turn so it applies for the whole session.
_STREAMING_NUDGE = (
    "[streaming mode] You stay live across turns and the user can message you "
    "between tool calls. For long-running shell commands, prefer "
    "run_in_background=true so you stay responsive to follow-up messages instead "
    "of blocking on the command; poll with BashOutput when you need the result.\n\n"
)

# Capacity eviction: stdout silence required before a non-busy session counts
# as evictable. Guards active tails (background work) from LRU eviction the
# same way the reaper's both-clocks rule does.
_EVICT_STDOUT_GRACE_SEC = 60.0


class StreamingManager:
    """Owns persistent StreamingSession instances keyed by channel."""

    def __init__(
        self,
        session_manager: SessionManager,
        settings: Settings,
        *,
        idle_timeout_sec: float = 900.0,
        reaper_interval_sec: float = 60.0,
        max_concurrent: int = 3,
    ) -> None:
        self._sm = session_manager
        self._settings = settings
        self._sessions: dict[ChannelKey, StreamingSession] = {}
        # Channels whose live process already established context (first turn done
        # or spawned with --resume). Subsequent messages skip the prompt prefix.
        self._primed: set[ChannelKey] = set()
        # session_id the LIVE process is actually running, per channel. Used to
        # detect reply-to-resume / external session switches (blueprint.session_id
        # changed but the live process is still on the old one).
        self._live_sid: dict[ChannelKey, str] = {}
        # Idle reaper: monotonic timestamp of the last interaction per channel.
        self._last_activity: dict[ChannelKey, float] = {}
        self._idle_timeout_sec = idle_timeout_sec
        self._reaper_interval_sec = reaper_interval_sec
        self._reaper_task: asyncio.Task[None] | None = None
        # Hard cap on concurrent live claude processes (each ~one claude's RAM).
        # When making streaming the default across many topics this bounds memory:
        # spawning beyond the cap evicts the least-recently-used IDLE session.
        self._max_concurrent = max_concurrent
        # Live-status buffers handed over by a finished turn whose process kept
        # working (turn tail): tail status events keep landing in the turn's
        # placeholder message instead of being dropped. Closed by the next
        # turn or by kill()/shutdown(). Duck-typed (async close()) so the
        # manager stays aiogram-free.
        self._tail_buffers: dict[ChannelKey, Any] = {}

    def _touch(self, channel_key: ChannelKey) -> None:
        """Mark a channel as just-used so the idle reaper resets its clock."""
        self._last_activity[channel_key] = time.monotonic()

    async def adopt_tail_buffer(self, channel_key: ChannelKey, buffer: Any) -> None:
        """Keep a finished turn's live buffer alive for the turn tail.

        Replaces (and closes) any previously adopted buffer for the channel.
        """
        await self.close_tail_buffer(channel_key)
        self._tail_buffers[channel_key] = buffer

    async def close_tail_buffer(self, channel_key: ChannelKey) -> None:
        """Close and drop the adopted tail buffer, if any. Idempotent."""
        buf = self._tail_buffers.pop(channel_key, None)
        if buf is not None:
            with contextlib.suppress(Exception):
                await buf.close()

    def is_active(self, channel_key: ChannelKey) -> bool:
        s = self._sessions.get(channel_key)
        return s is not None and s.is_alive

    def is_busy(self, channel_key: ChannelKey) -> bool:
        s = self._sessions.get(channel_key)
        return s is not None and s.is_turn_active

    async def _enforce_capacity(self, incoming_key: ChannelKey) -> None:
        """Keep concurrent live processes under the cap by evicting LRU idle ones.

        Never evicts a busy session (a live turn) — better to briefly exceed the
        cap than to kill work in progress. Evicted sessions respawn with --resume.
        """
        alive = [k for k, s in self._sessions.items() if s.is_alive and k != incoming_key]
        if len(alive) < self._max_concurrent:
            return
        now = time.monotonic()
        idle_lru = sorted(
            (
                k
                for k in alive
                if not self.is_busy(k)
                # An active tail (background work after an early result keeps
                # stdout flowing) is work in progress too — don't evict it,
                # same principle as busy turns. Briefly exceeding the cap is
                # cheaper than killing a background job mid-flight.
                and now - self._sessions[k].last_stream_event >= _EVICT_STDOUT_GRACE_SEC
            ),
            key=lambda k: self._last_activity.get(k, 0.0),
        )
        to_free = len(alive) - self._max_concurrent + 1
        for k in idle_lru[:to_free]:
            logger.info("StreamingManager capacity cap reached — evicting LRU idle %s", k)
            await self.kill(k)

    async def _ensure_session(self, channel_key: ChannelKey) -> StreamingSession:
        blueprint = self._sm._get_session(channel_key)
        target_sid = blueprint.session_id  # reply-to-resume / restore overrides this

        existing = self._sessions.get(channel_key)
        if existing is not None and existing.is_alive:
            live_sid = self._live_sid.get(channel_key)
            # reply-to-resume (or any override) pointed the channel at a DIFFERENT
            # session than the live process runs. Kill it so we respawn with
            # --resume <target>; otherwise the message lands in the wrong session.
            if target_sid and live_sid and target_sid != live_sid:
                logger.info(
                    "StreamingManager session switch for %s: %s -> %s (respawn)",
                    channel_key,
                    live_sid,
                    target_sid,
                )
                await self.kill(channel_key)
            else:
                return existing
        elif existing is not None:
            # Present but dead (natural death / inactivity kill): close it so its
            # runtime-MCP cleanup hook fires before we overwrite the slot, else
            # the temp config file leaks in data/mcp-runtime/.
            await existing.close()

        await self._enforce_capacity(channel_key)

        # Merge the standard bot MCP server into the base config and write a
        # per-process runtime file, exactly like the one-shot path. Without this
        # the streaming process launches with no --mcp-config (the default
        # `.mcp.bot.json` does not exist), so the agent never gets the
        # send_image/send_document/send_message tools.
        mcp_config, runtime_mcp_path = self._sm.prepare_runtime_mcp_config(
            channel_key, blueprint.mcp_config
        )

        def _cleanup_runtime_mcp() -> None:
            with contextlib.suppress(OSError):
                runtime_mcp_path.unlink()

        argv = self._sm.build_streaming_argv(
            target_sid,
            blueprint.mode,
            mcp_config,
            blueprint.chat_id,
            blueprint.thread_id,
            blueprint.model,
        )
        session = StreamingSession(
            argv,
            cwd=blueprint.cwd or None,
            inactivity_kill_sec=self._settings.cc_inactivity_kill_sec,
            on_close=_cleanup_runtime_mcp,
        )
        await session.ensure_started()
        self._sessions[channel_key] = session
        # A resumed process already has context (and is on target_sid); a fresh
        # one does not — its sid is learned from the first result.
        if target_sid:
            self._primed.add(channel_key)
            self._live_sid[channel_key] = target_sid
        else:
            self._primed.discard(channel_key)
            self._live_sid.pop(channel_key, None)
        logger.info(
            "StreamingManager session ready for %s (resume=%s)", channel_key, bool(target_sid)
        )
        return session

    async def send_stream(
        self,
        channel_key: ChannelKey,
        prompt: str,
        on_event: BotEventCallback,
    ) -> str:
        """Start a turn and await its result, bridging events to the bot.

        Caller must route mid-turn messages to `inject`, not here: this starts a
        fresh turn and StreamingSession rejects a concurrent one.
        """
        session = await self._ensure_session(channel_key)
        self._touch(channel_key)
        blueprint = self._sm._get_session(channel_key)

        if channel_key in self._primed:
            text = prompt
        else:
            # Fresh process: prepend the streaming nudge + mode prompt + tg
            # context, like the one-shot fresh run. session_id=None forces the
            # prefix in _build_full_prompt.
            text = _STREAMING_NUDGE + self._sm._build_full_prompt(
                prompt, None, blueprint.mode, blueprint.chat_id, blueprint.thread_id
            )

        active_agents: dict[str, str] = {}
        agent_last_progress: dict[str, float] = {}
        throttle = self._settings.cc_agent_progress_throttle_sec
        captured_sid: str | None = None
        result_seen = False

        async def raw_handler(raw: dict[str, Any]) -> None:
            nonlocal captured_sid, result_seen
            events, new_sid = parse_cc_event(raw, active_agents, agent_last_progress, throttle)
            if new_sid:
                captured_sid = new_sid
            for ev in events:
                # The FIRST `result` is the turn's return value (Streaming-
                # Session resolves it) — don't leak it to the chat. Later
                # results are the turn's TAIL (background continuations after
                # an early result): each marks the end of a wake-up cycle, so
                # forward them for the handler to flush the batched tail text.
                # A counter, not session.is_turn_active: the reader loop can
                # dispatch tail events before send()'s finally flips the flag.
                if ev.type == "result" and not result_seen:
                    result_seen = True
                    continue
                ret = on_event(ev)
                if asyncio.iscoroutine(ret):
                    await ret

        try:
            result = await session.send(text, raw_handler)
        finally:
            # Reset the idle clock to turn END so the 15-min countdown starts
            # after the work finishes, not when it began.
            self._touch(channel_key)
            # Persist the session_id even if the turn was interrupted/killed
            # before `result` — fall back to the blueprint's sid so the next
            # respawn can --resume instead of losing context (M1). captured_sid
            # (from the result event) wins when present.
            sid_to_persist = captured_sid or blueprint.session_id
            if sid_to_persist:
                self._live_sid[channel_key] = sid_to_persist
                await self._sm.persist_session_id(
                    channel_key, sid_to_persist, model=blueprint.model
                )
        self._primed.add(channel_key)
        return result

    async def inject(self, channel_key: ChannelKey, text: str) -> bool:
        """Steer the active turn with a mid-turn message. Returns False if there
        is no live turn to inject into (caller should start a turn instead)."""
        session = self._sessions.get(channel_key)
        if session is None or not session.is_alive or not session.is_turn_active:
            return False
        await session.inject(text)
        self._touch(channel_key)
        return True

    async def cancel(self, channel_key: ChannelKey) -> bool:
        """Stop button: interrupt the current turn WITHOUT killing the process."""
        session = self._sessions.get(channel_key)
        if session is None or not session.is_alive:
            return False
        await session.interrupt()
        return True

    async def kill(self, channel_key: ChannelKey) -> None:
        """Hard reset: terminate the live process (e.g. /new, engine change)."""
        session = self._sessions.pop(channel_key, None)
        self._primed.discard(channel_key)
        self._last_activity.pop(channel_key, None)
        self._live_sid.pop(channel_key, None)
        await self.close_tail_buffer(channel_key)
        if session is not None:
            await session.close()

    # --- Idle reaper -----------------------------------------------------

    async def _reap_idle_once(self) -> list[ChannelKey]:
        """Kill sessions idle past idle_timeout_sec. Returns killed channel keys.

        Skips sessions with a live turn (is_busy) — a long task that keeps the
        process busy is not "idle" even if it predates the timeout. Also skips
        sessions whose STDOUT was recently active: with background tasks the
        CLI ends the visible turn early and keeps working (agent progress and
        task-notification wake-ups keep emitting stream events), so bot-side
        activity alone under-counts real work — both clocks must be stale.
        A process whose API stream hung goes stdout-silent and IS reaped here;
        that's the recovery path for dead SSE connections. Killed sessions
        respawn with --resume on the next message (context preserved).
        """
        now = time.monotonic()
        stale = [
            key
            for key, session in list(self._sessions.items())
            if session.is_alive
            and not session.is_turn_active
            and now - self._last_activity.get(key, now) >= self._idle_timeout_sec
            and now - session.last_stream_event >= self._idle_timeout_sec
        ]
        for key in stale:
            logger.info(
                "StreamingManager reaping idle session for %s (idle >= %.0fs)",
                key,
                self._idle_timeout_sec,
            )
            await self.kill(key)
        return stale

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reaper_interval_sec)
            try:
                await self._reap_idle_once()
            except Exception:
                logger.exception("StreamingManager idle reaper pass failed")

    def start_reaper(self) -> None:
        """Start the background idle reaper. Idempotent."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())
            logger.info(
                "StreamingManager idle reaper started (timeout=%.0fs, interval=%.0fs)",
                self._idle_timeout_sec,
                self._reaper_interval_sec,
            )

    async def stop_reaper(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper_task
            self._reaper_task = None

    async def shutdown(self) -> None:
        await self.stop_reaper()
        for key in list(self._tail_buffers):
            await self.close_tail_buffer(key)
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()
        self._primed.clear()
        self._last_activity.clear()
        self._live_sid.clear()
