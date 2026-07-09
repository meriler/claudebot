"""StreamingSession — one persistent `claude --input-format stream-json` process.

This is the engine for the `streaming` exec mode (Phase 3 of the queue/inject
feature). Unlike the one-shot subprocess path (`claude -p <prompt>`, dies after
one turn), a StreamingSession keeps a single Claude Code process alive and feeds
user messages into its stdin as newline-delimited JSON. A message sent while a
turn is in progress is picked up by Claude on the next agent-loop iteration —
i.e. BETWEEN tool calls — which is the terminal-style steering the one-shot path
cannot provide (empirically verified, CLI 2.1.x).

Deliberately standalone and engine-only: it does NOT touch SessionManager,
MessageQueue, or the bot. Wiring (exec_mode, lifecycle, queue pass-through,
reaper, watchdog) happens in later increments. Tested against a fake `claude`
that speaks stream-json (tests/fakes/fake_claude_stream.py).

Control protocol (verified against the CLI binary): the live process accepts
`interrupt`, `set_model`, `set_permission_mode`, `can_use_tool`, `mcp_message`,
`hook_callback`, `initialize`, `control_cancel_request`. There is NO "move a
running command to background" control — that is a TUI-only affordance, so the
"auto-Ctrl+B" effect is achieved by the model preferring `run_in_background`,
not by an external signal here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Match the one-shot path's stdout line limit: Claude embeds base64 (e.g. PDFs)
# in stream-json, so lines can be large. Default StreamReader limit (64 KiB)
# would raise LimitOverrunError on those.
_STDOUT_LINE_LIMIT = 10 * 1024 * 1024

# Event dispatched to the per-turn callback. Raw parsed stream-json dict; the
# bot-facing StreamEvent mapping lives in the integration layer, not here.
StreamEventDict = dict[str, Any]
EventCallback = Callable[[StreamEventDict], Awaitable[None] | None]


class StreamingProcessDeadError(RuntimeError):
    """Raised when the persistent process exits while a turn is awaiting result."""


def _user_message(text: str) -> dict[str, Any]:
    """Build a stream-json user input message."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _interrupt_request(request_id: str) -> dict[str, Any]:
    """Build a control_request interrupt (hard-stops the current turn)."""
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": "interrupt"},
    }


class StreamingSession:
    """A single persistent Claude Code process driven over stream-json stdio."""

    def __init__(
        self,
        argv: list[str],
        cwd: str | None = None,
        *,
        inactivity_kill_sec: float = 300.0,
        poll_sec: float = 30.0,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        # Called once when the process is torn down — used to unlink the
        # per-process runtime MCP config file (best-effort, must not raise).
        self._on_close = on_close
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stdin_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

        # Per-turn state. Only one turn is awaited at a time; mid-turn messages
        # go through `inject` (fire-and-forget) and fold into the active turn.
        # _on_event deliberately OUTLIVES the turn: with background tasks the
        # CLI emits an early `result` and keeps working (task-notification
        # wake-ups re-enter the agent loop), so post-turn events must still
        # reach the last turn's callback instead of being dropped. It is
        # replaced by the next send() and cleared on process teardown.
        self._turn_active = False
        self._turn_result: asyncio.Future[str] | None = None
        self._on_event: EventCallback | None = None

        # Watchdog: if a turn produces no stdout for inactivity_kill_sec, the
        # process is presumed stuck — kill it so the turn fails instead of
        # hanging the channel forever. Only enforced WHILE a turn is active; an
        # idle process legitimately sits silent waiting for input. poll_sec is
        # the readline wake-up cadence that lets the loop check the deadline.
        self._inactivity_kill_sec = inactivity_kill_sec
        self._poll_sec = poll_sec

        # Timestamps: last stdout line and last result.
        self.last_stream_event: float = 0.0
        self.last_result_at: float = 0.0
        self._interrupt_seq = 0

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def is_turn_active(self) -> bool:
        return self._turn_active

    async def ensure_started(self) -> None:
        """Spawn the process and reader loop if not already running. Idempotent."""
        async with self._start_lock:
            if self.is_alive:
                return
            self._process = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                cwd=self._cwd,
                limit=_STDOUT_LINE_LIMIT,
            )
            self.last_stream_event = time.monotonic()
            self._reader_task = asyncio.create_task(self._reader_loop(self._process))
            logger.info("StreamingSession started (pid=%d)", self._process.pid)

    async def send(self, text: str, on_event: EventCallback) -> str:
        """Start a turn with `text` and await its result.

        Must be called only when no turn is active (caller routes mid-turn
        messages to `inject`). Returns the turn's `result` text.
        """
        await self.ensure_started()
        if self._turn_active:
            raise RuntimeError("send() called while a turn is active; use inject()")

        loop = asyncio.get_running_loop()
        self._turn_result = loop.create_future()
        self._on_event = on_event
        # Reset the inactivity clock to turn start so the watchdog measures
        # silence within THIS turn, not since the process last spoke.
        self.last_stream_event = time.monotonic()
        self._turn_active = True
        try:
            await self._write(_user_message(text))
            return await self._turn_result
        finally:
            self._turn_active = False
            # _on_event is intentionally NOT cleared — see __init__: the CLI
            # can keep emitting events after this turn's `result` (background
            # tasks), and those must still reach the chat.
            self._turn_result = None

    async def inject(self, text: str) -> None:
        """Write a user message mid-turn without starting a new turn.

        Claude folds it into the active turn at the next agent-loop boundary
        (between tools). Events keep flowing to the active turn's callback and
        the same result future resolves once the (now-steered) turn ends.
        """
        if not self.is_alive:
            raise StreamingProcessDeadError("inject() on a dead process")
        await self._write(_user_message(text))

    async def interrupt(self) -> None:
        """Hard-stop the current turn (control_request interrupt).

        This is NOT "background the command" — it aborts the turn (the only
        external lever the headless protocol exposes). Used for the Stop button.
        """
        if not self.is_alive:
            return
        self._interrupt_seq += 1
        await self._write(_interrupt_request(f"int_{self._interrupt_seq}"))

    async def close(self) -> None:
        """Terminate the process and stop the reader. Best-effort, idempotent."""
        proc = self._process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        self._fail_pending_turn(StreamingProcessDeadError("session closed"))
        self._process = None
        self._reader_task = None
        self._on_event = None
        if self._on_close is not None:
            with contextlib.suppress(Exception):
                self._on_close()
            self._on_close = None

    async def _write(self, obj: dict[str, Any]) -> None:
        """Serialize one JSON object as an NDJSON line to stdin under lock."""
        proc = self._process
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise StreamingProcessDeadError("write to a dead process")
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._stdin_lock:
            try:
                proc.stdin.write(line)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise StreamingProcessDeadError("stdin broken") from exc

    async def _reader_loop(self, proc: asyncio.subprocess.Process) -> None:
        """Read stdout forever; dispatch events; resolve turn future on result.

        Unlike the one-shot `_read_stream`, this does NOT stop at the first
        `result` — the process lives across many turns, so it continues until
        stdout EOF (process death). A poll timeout drives the inactivity
        watchdog: if a turn goes silent past inactivity_kill_sec the process is
        presumed stuck and killed, so send() fails instead of hanging forever.
        """
        assert proc.stdout is not None
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=self._poll_sec)
                except TimeoutError:
                    # Inactivity only matters during a turn; an idle process
                    # legitimately sits silent waiting for the next message.
                    if (
                        self._turn_active
                        and time.monotonic() - self.last_stream_event >= self._inactivity_kill_sec
                    ):
                        logger.warning(
                            "StreamingSession inactivity kill: %.0fs silent mid-turn",
                            self._inactivity_kill_sec,
                        )
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        break
                    continue
                if not raw:
                    break  # EOF — process exited
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                self.last_stream_event = time.monotonic()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("StreamingSession: non-JSON line: %.120s", line)
                    continue
                await self._dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("StreamingSession reader loop crashed")
        finally:
            # EOF / death: surface to any awaiting turn so send() doesn't hang.
            with contextlib.suppress(Exception):
                await proc.wait()
            logger.info(
                "StreamingSession reader exited (returncode=%s)",
                None if self._process is None else self._process.returncode,
            )
            self._fail_pending_turn(StreamingProcessDeadError("process exited"))
            self._on_event = None  # dead process emits nothing; drop the ref
            # Natural death (EOF / crash): close() may never be called, so fire
            # the cleanup hook here too. Idempotent — clears _on_close after.
            if self._on_close is not None:
                with contextlib.suppress(Exception):
                    self._on_close()
                self._on_close = None

    async def _dispatch(self, event: StreamEventDict) -> None:
        cb = self._on_event
        if cb is not None:
            try:
                res = cb(event)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.exception("StreamingSession on_event callback failed")
        if event.get("type") == "result":
            self.last_result_at = time.monotonic()
            fut = self._turn_result
            if fut is not None and not fut.done():
                fut.set_result(str(event.get("result", "")))

    def _fail_pending_turn(self, exc: BaseException) -> None:
        fut = self._turn_result
        if fut is not None and not fut.done():
            fut.set_exception(exc)
