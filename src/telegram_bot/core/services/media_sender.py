"""Auto-send media to Telegram when CC tool_result contains image URLs.

Intercepts ``media_url`` events from ``cc_events.parse_cc_event`` and
downloads + sends images to the originating Telegram chat. Fire-and-forget
pattern with GC-safe task tracking.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import tempfile
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from aiogram.types import FSInputFile

if TYPE_CHECKING:
    from aiogram import Bot

from telegram_bot.core.messages import t
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
_DOWNLOAD_TIMEOUT = 30  # seconds
_MAX_URLS_PER_CHANNEL = 500
_MAX_DOWNLOADS_PER_MINUTE = 10
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB — Telegram sendPhoto limit


def _normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if "discordapp.com" in parsed.netloc or "discord.com" in parsed.netloc:
        return parsed._replace(query="").geturl()
    return url


def _safe_filename(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    basename = Path(parsed.path).name.split("?")[0] or "image"
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", basename)[:100]
    ext = Path(sanitized).suffix.lower()
    if ext not in _IMAGE_EXTENSIONS:
        ext = ".png"
    return sanitized, ext


class MediaSender:
    """Download and send media to Telegram. Fire-and-forget pattern."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._sent_urls: dict[ChannelKey, OrderedDict[str, None]] = {}
        self._download_timestamps: dict[ChannelKey, list[float]] = {}
        self._download_semaphore = asyncio.Semaphore(3)

    def schedule_send(
        self,
        url: str,
        chat_id: int,
        thread_id: int | None,
        channel_key: ChannelKey,
    ) -> None:
        normalized = _normalize_url(url)
        urls = self._sent_urls.setdefault(channel_key, OrderedDict())
        if normalized in urls:
            return
        urls[normalized] = None
        while len(urls) > _MAX_URLS_PER_CHANNEL:
            urls.popitem(last=False)

        if not self._check_rate_limit(channel_key):
            logger.warning("Rate limit exceeded for %s, skipping media", channel_key)
            return

        task = asyncio.create_task(self._send(url, chat_id, thread_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def clear_channel(self, channel_key: ChannelKey) -> None:
        self._sent_urls.pop(channel_key, None)
        self._download_timestamps.pop(channel_key, None)

    def _check_rate_limit(self, channel_key: ChannelKey) -> bool:
        now = time.monotonic()
        timestamps = self._download_timestamps.setdefault(channel_key, [])
        cutoff = now - 60.0
        self._download_timestamps[channel_key] = [ts for ts in timestamps if ts > cutoff]
        if len(self._download_timestamps[channel_key]) >= _MAX_DOWNLOADS_PER_MINUTE:
            return False
        self._download_timestamps[channel_key].append(now)
        return True

    async def _send(self, url: str, chat_id: int, thread_id: int | None) -> None:
        local_path: Path | None = None
        try:
            async with self._download_semaphore:
                local_path = await self._download(url)
            if local_path is None:
                return
            size = local_path.stat().st_size
            is_photo = local_path.suffix.lower() in _IMAGE_EXTENSIONS and size <= _MAX_PHOTO_SIZE
            if is_photo:
                try:
                    await self._bot.send_photo(
                        chat_id,
                        photo=FSInputFile(local_path),
                        message_thread_id=thread_id,
                    )
                    return
                except Exception as e:
                    logger.debug("send_photo fallback to document: %s", e)
            await self._bot.send_document(
                chat_id,
                document=FSInputFile(local_path),
                message_thread_id=thread_id,
                caption=t("ui.file_too_large_preview") if size > _MAX_PHOTO_SIZE else None,
            )
        except Exception as e:
            logger.warning("Auto-send media failed for %s: %s", url[:80], e)
        finally:
            if local_path is not None:
                local_path.unlink(missing_ok=True)

    @staticmethod
    async def _url_is_safe(url: str) -> bool:
        """SSRF guard: resolve the URL host and reject internal/reserved IPs.

        A CC tool_result could hand us an image_url pointing at an internal
        service (tailnet endpoint, localhost, cloud metadata 169.254.169.254);
        without this the bot would fetch it and repost the response. We resolve
        every address the host maps to and block private/loopback/link-local/
        reserved/multicast ranges. Checked on the initial URL AND on the final
        URL after redirects. (S6, audit 2026-07-02.)
        """
        host = urllib.parse.urlparse(url).hostname
        if not host:
            return False
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        except Exception:
            logger.warning("SSRF guard: cannot resolve host %s", host)
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                logger.warning("SSRF guard: blocked internal address %s for %s", ip, host)
                return False
        return True

    async def _download(self, url: str) -> Path | None:
        if not url.startswith("https://"):
            logger.warning("Rejected non-https URL: %s", url[:80])
            return None
        if not await self._url_is_safe(url):
            return None

        _, ext = _safe_filename(url)
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        tmp = Path(tmp_path)
        total = 0
        ok = False
        try:
            async with (
                httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client,
                client.stream("GET", url) as resp,
            ):
                if resp.status_code != 200:
                    logger.warning("Download failed: HTTP %d for %s", resp.status_code, url[:80])
                    return None
                final_url = str(resp.url)
                if not final_url.startswith("https://"):
                    logger.warning("Redirect to non-https: %s", final_url[:80])
                    return None
                # follow_redirects=True could have landed on an internal host
                # even if the initial URL was public — re-check the final host.
                if final_url != url and not await self._url_is_safe(final_url):
                    logger.warning("SSRF guard: redirect to internal host: %s", final_url[:80])
                    return None
                with os.fdopen(fd, "wb") as f:
                    fd = -1
                    async for chunk in resp.aiter_bytes(8192):
                        total += len(chunk)
                        if total > _MAX_DOWNLOAD_BYTES:
                            logger.warning("Download exceeds %d bytes", _MAX_DOWNLOAD_BYTES)
                            return None
                        f.write(chunk)
            ok = total > 0
        except Exception as e:
            logger.warning("Download error for %s: %s", url[:80], e)
            return None
        finally:
            if fd >= 0:
                os.close(fd)
            if not ok:
                tmp.unlink(missing_ok=True)

        return tmp if ok else None
