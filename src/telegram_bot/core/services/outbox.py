"""Persistent outbox: final responses that failed to send are delivered later.

When Telegram rejects/times out the delivery of a finished CC response, the
response used to vanish silently. Instead it lands here: a JSON-backed queue
that retries with growing pauses until the network returns, and survives bot
restarts (an in-memory buffer would die in exactly the same network incident
that filled it).

Ordering: the queue is FIFO and the worker never skips ahead, so within a
chat/thread replies arrive in the order they were produced. Callers must
also route NEW responses through the outbox while older ones are pending
(see ``has_pending``) — otherwise a fresh reply could overtake a stuck one.

Chunk-level resume: ``next_chunk`` records how much of the entry was already
delivered, so retries don't duplicate chunks that made it through before the
failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram.enums import ParseMode

from telegram_bot.core.services.telegram_utils import send_html_with_fallback
from telegram_bot.core.utils.telegram_html import split_html_message

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SEC = 300.0
_BASE_BACKOFF_SEC = 5.0
_MAX_ENTRIES = 200  # hard cap — drop oldest beyond this, log loudly
# Give up on the head entry after this many failed attempts so a permanently
# undeliverable head can't block the whole queue forever. At the 300s backoff
# cap this is many hours of retrying — long enough to ride out real outages,
# after which the head is dropped to unblock other chats. (audit 2026-07-02.)
_MAX_ATTEMPTS = 50


class Outbox:
    def __init__(self, bot: Bot, path: str | Path) -> None:
        self._bot = bot
        self._path = Path(path)
        self._entries: list[dict[str, Any]] = []
        self._worker: asyncio.Task[None] | None = None
        self._load()

    # --- persistence ---

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, list):
                self._entries = [e for e in data if isinstance(e, dict) and "text" in e]
                if self._entries:
                    logger.info("Outbox: %d undelivered entries loaded", len(self._entries))
        except (json.JSONDecodeError, OSError):
            logger.warning("Outbox: failed to load %s", self._path)

    def _persist(self) -> None:
        try:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(self._entries, ensure_ascii=False))
            tmp.replace(self._path)
        except OSError:
            logger.warning("Outbox: failed to persist", exc_info=True)

    # --- public API ---

    @property
    def size(self) -> int:
        return len(self._entries)

    def has_pending(self, chat_id: int, thread_id: int | None) -> bool:
        return any(
            e.get("chat_id") == chat_id and e.get("thread_id") == thread_id for e in self._entries
        )

    def enqueue(self, chat_id: int, thread_id: int | None, text: str) -> None:
        """Queue a response for delivery and (re)start the worker."""
        if len(self._entries) >= _MAX_ENTRIES:
            dropped = self._entries.pop(0)
            logger.error(
                "Outbox: overflow (%d entries) — dropping oldest entry %s",
                _MAX_ENTRIES,
                dropped.get("id"),
            )
        self._entries.append(
            {
                "id": uuid.uuid4().hex[:12],
                "chat_id": chat_id,
                "thread_id": thread_id,
                "text": text,
                "created_at": time.time(),
                "attempts": 0,
                "next_chunk": 0,
            }
        )
        self._persist()
        logger.info("Outbox: queued response for %s:%s (%d pending)", chat_id, thread_id, self.size)
        self.start()

    def start(self) -> None:
        """Start the delivery worker if entries are pending. Idempotent."""
        if self._entries and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._worker_loop())

    async def shutdown(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        self._persist()

    # --- delivery ---

    async def _deliver(self, entry: dict[str, Any]) -> tuple[bool, bool]:
        """Try to push one entry out.

        Returns ``(delivered, fatal)``:
          - ``(True, False)``  — fully delivered;
          - ``(False, True)``  — permanently undeliverable (corrupt entry with
            no chat_id, or the bot is blocked/deactivated in that chat) → caller
            drops it so it can't block the queue head forever;
          - ``(False, False)`` — transient failure → retry with backoff.
        """
        # Corrupt entry (no chat_id) can never be sent — treat as fatal so the
        # worker drops it instead of crashing on it every pass. (audit 2026-07-02.)
        if entry.get("chat_id") is None:
            logger.warning("Outbox: entry %s has no chat_id, dropping", entry.get("id"))
            return (False, True)

        chunks = split_html_message(entry["text"])
        start = int(entry.get("next_chunk", 0))
        for idx in range(start, len(chunks)):
            chunk = chunks[idx]

            async def _send_html(c: str = chunk) -> Any:
                return await self._bot.send_message(
                    entry["chat_id"],
                    c,
                    message_thread_id=entry["thread_id"],
                    parse_mode=ParseMode.HTML,
                )

            async def _send_plain(c: str = chunk) -> Any:
                return await self._bot.send_message(
                    entry["chat_id"], c, message_thread_id=entry["thread_id"]
                )

            outcome = await send_html_with_fallback(
                send_html=_send_html,
                send_plain=_send_plain,
                label=f"outbox {entry['id']}",
            )
            if outcome.message_id is None:
                # Undelivered. fatal=True means the bot is blocked/deactivated
                # in this chat — no retry will ever succeed, so propagate it up
                # for a drop. Otherwise it's transient (network, flood give-up).
                entry["next_chunk"] = idx  # resume here next time, no duplicates
                self._persist()
                return (False, outcome.fatal)
            entry["next_chunk"] = idx + 1
        return (True, False)

    async def _worker_loop(self) -> None:
        """FIFO delivery with exponential backoff; exits when the queue drains."""
        while self._entries:
            entry = self._entries[0]
            fatal = False
            try:
                delivered, fatal = await self._deliver(entry)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Outbox: delivery attempt crashed", exc_info=True)
                delivered = False
            if delivered:
                self._entries.pop(0)
                self._persist()
                logger.info("Outbox: delivered entry %s (%d remaining)", entry["id"], self.size)
                continue
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            # Drop the head instead of retrying forever when it is permanently
            # undeliverable (fatal) or has exhausted its attempts. A stuck head
            # otherwise blocks delivery for EVERY other chat behind it. This is
            # best-effort delivery of already-failed responses, so a loud drop
            # is the right trade vs. an infinitely-blocked queue. (audit 2026-07-02.)
            if fatal or entry["attempts"] >= _MAX_ATTEMPTS:
                self._entries.pop(0)
                self._persist()
                logger.warning(
                    "Outbox: dropping undeliverable entry %s (%s, attempts=%d, %d remaining)",
                    entry["id"],
                    "fatal" if fatal else "max attempts",
                    entry["attempts"],
                    self.size,
                )
                continue
            self._persist()
            delay = min(_MAX_BACKOFF_SEC, _BASE_BACKOFF_SEC * (2 ** min(entry["attempts"], 6)))
            logger.info(
                "Outbox: entry %s not delivered (attempt %d), retrying in %.0fs",
                entry["id"],
                entry["attempts"],
                delay,
            )
            await asyncio.sleep(delay)
