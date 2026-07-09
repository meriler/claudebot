#!/usr/bin/env python3
"""Bot MCP server — sends files and messages to Telegram chats.

Runs as a stdio MCP server, started by Claude Code via .mcp.bot.json.
BOT_TOKEN must be set in environment (read from .env by start.sh).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import httpx
from mcp.server.fastmcp import FastMCP

from telegram_bot.core.utils.table_renderer import (
    _header_summary,
    find_tables,
    render_table_as_image,
)
from telegram_bot.core.utils.telegram_html import split_html_message

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("bot-mcp")

mcp = FastMCP("bot")

_TELEGRAM_API = "https://api.telegram.org"
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB — Telegram Bot API limit
_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB — Telegram photo limit
_MAX_MESSAGE_LEN = 4096
_MAX_CAPTION_LEN = 1024
_MAX_MESSAGE_CHUNKS = 10
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_PHOTO_FALLBACK_ERRORS = (
    "IMAGE_PROCESS_FAILED",
    "PHOTO_INVALID_DIMENSIONS",
    "wrong file identifier/http url specified",
    "failed to get http url content",
)


def _env_int(name: str) -> tuple[int | None, str | None]:
    raw = os.environ.get(name, "")
    if raw == "":
        return None, None
    try:
        return int(raw), None
    except ValueError:
        return None, f"Error: {name} must be an int, got {raw!r}"


def _resolve_routing(
    chat_id: int | None = None, thread_id: int | None = None
) -> tuple[int | None, int | None, str | None]:
    env_chat_id, chat_error = _env_int("TELEGRAM_CHAT_ID")
    if chat_error:
        return None, None, chat_error
    env_thread_id, thread_error = _env_int("TELEGRAM_THREAD_ID")
    if thread_error:
        return None, None, thread_error

    lock_context = os.environ.get("TELEGRAM_CONTEXT_LOCK") == "1"
    resolved_chat = chat_id if chat_id is not None else env_chat_id
    resolved_thread = thread_id if thread_id is not None else env_thread_id

    if resolved_chat is None:
        return None, None, "Error: chat_id not provided and TELEGRAM_CHAT_ID is not configured"
    if lock_context:
        if env_chat_id is None:
            return None, None, "Error: TELEGRAM_CONTEXT_LOCK=1 but TELEGRAM_CHAT_ID not configured"
        if chat_id is not None and chat_id != env_chat_id:
            return None, None, "Error: chat_id does not match the current Telegram session"
        if thread_id is not None and thread_id != env_thread_id:
            return None, None, "Error: thread_id does not match the current Telegram session"
    return resolved_chat, resolved_thread, None


def _post_text_only(
    client: httpx.Client, token: str, chat_id: int, text: str, thread_id: int | None = None
) -> str | None:
    """Convert raw markdown *text* to Telegram HTML and send it. Returns error or None.

    Uses the shared core pipeline `split_html_message` (markdown → sanitize →
    balance → newline-aware split) instead of a stripped local converter + a
    naive 4096-char slice. This escapes bare `<`/`>` (so "score < 5" no longer
    triggers a Telegram 400 with no fallback) and never cuts a chunk mid-tag.
    (audit 2026-07-02: dedup of the two telegram_html copies.)
    """
    chunks = split_html_message(text)
    if len(chunks) > _MAX_MESSAGE_CHUNKS:
        max_len = _MAX_MESSAGE_LEN * _MAX_MESSAGE_CHUNKS
        return f"Error: message too long (max {max_len} characters)"
    url = f"{_TELEGRAM_API}/bot{token}/sendMessage"
    for chunk in chunks:
        data: dict[str, str] = {
            "chat_id": str(chat_id),
            "text": chunk,
            "parse_mode": "HTML",
        }
        if thread_id is not None:
            data["message_thread_id"] = str(thread_id)
        resp = client.post(url, data=data)
        if resp.status_code != 200:
            try:
                error = resp.json().get("description", resp.text[:200])
            except Exception:
                error = resp.text[:200]
            return f"Error {resp.status_code}: {error}"
    return None


def _post_table_image(
    client: httpx.Client, token: str, chat_id: int, table_text: str, thread_id: int | None = None
) -> str | None:
    """Render table as PNG and send as photo. Returns error or None."""
    image_path = render_table_as_image(table_text)
    if image_path is None:
        return _post_text_only(client, token, chat_id, table_text, thread_id)
    try:
        caption = _header_summary(table_text)[:_MAX_CAPTION_LEN]
        url = f"{_TELEGRAM_API}/bot{token}/sendPhoto"
        data: dict[str, str] = {"chat_id": str(chat_id), "caption": caption}
        if thread_id is not None:
            data["message_thread_id"] = str(thread_id)
        with open(image_path, "rb") as f:
            resp = client.post(url, data=data, files={"photo": ("table.png", f)})
        if resp.status_code != 200:
            return _post_text_only(client, token, chat_id, table_text, thread_id)
        return None
    finally:
        Path(image_path).unlink(missing_ok=True)


def _post_message(token: str, chat_id: int, text: str, thread_id: int | None = None) -> str:
    if not text:
        return "Error: message is empty"

    table_matches = find_tables(text)
    try:
        with httpx.Client(timeout=60) as client:
            if not table_matches:
                err = _post_text_only(client, token, chat_id, text, thread_id)
                if err:
                    return err
                return "Message sent"

            parts_sent = 0
            last_end = 0
            for match in table_matches:
                before = text[last_end : match.start()]
                if before.strip():
                    err = _post_text_only(client, token, chat_id, before, thread_id)
                    if err:
                        return err
                    parts_sent += 1
                err = _post_table_image(client, token, chat_id, match.group(0), thread_id)
                if err:
                    return err
                parts_sent += 1
                last_end = match.end()

            after = text[last_end:]
            if after.strip():
                err = _post_text_only(client, token, chat_id, after, thread_id)
                if err:
                    return err
                parts_sent += 1

            if parts_sent == 1:
                return "Message sent"
            return f"Messages sent: {parts_sent}"
    except httpx.TimeoutException:
        return "Error: timeout while sending"
    except Exception as exc:
        return f"Error: {exc}"


def _send_to_telegram(
    token: str,
    chat_id: int,
    file_path: Path,
    caption: str,
    thread_id: int | None = None,
    *,
    method: str = "sendDocument",
    file_field: str = "document",
    success_label: str = "Sent",
) -> str:
    """Send file via Telegram Bot API."""
    url = f"{_TELEGRAM_API}/bot{token}/{method}"
    media_caption = caption[:_MAX_CAPTION_LEN]
    caption_tail = caption[_MAX_CAPTION_LEN:]
    data: dict[str, str] = {"chat_id": str(chat_id), "caption": media_caption}
    if thread_id is not None:
        data["message_thread_id"] = str(thread_id)
    try:
        with httpx.Client(timeout=60) as client, open(file_path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > _MAX_FILE_SIZE:
                return f"Error: file too large ({size // 1024 // 1024} MB, max 50 MB)"
            resp = client.post(url, data=data, files={file_field: (file_path.name, f)})
        if resp.status_code == 200:
            result = f"{success_label}: {file_path.name}"
            if caption_tail:
                tail_result = _post_message(token, chat_id, caption_tail, thread_id)
                result = f"{result}; {tail_result}"
            return result
        try:
            error = resp.json().get("description", resp.text[:200])
        except Exception:
            error = resp.text[:200]
        return f"Error {resp.status_code}: {error}"
    except httpx.TimeoutException:
        return "Error: timeout while sending"
    except Exception as exc:
        return f"Error: {exc}"


def _send_document(
    token: str, chat_id: int, file_path: Path, caption: str, thread_id: int | None = None
) -> str:
    """Send file as document via Telegram Bot API."""
    return _send_to_telegram(token, chat_id, file_path, caption, thread_id)


def _send_photo(
    token: str, chat_id: int, file_path: Path, caption: str, thread_id: int | None = None
) -> str:
    """Send image as photo via Telegram Bot API (renders inline, not as file)."""
    return _send_to_telegram(
        token,
        chat_id,
        file_path,
        caption,
        thread_id,
        method="sendPhoto",
        file_field="photo",
        success_label="Photo sent",
    )


def _is_photo_fallback_error(result: str) -> bool:
    if not result.startswith("Error 400"):
        return False
    lowered = result.lower()
    return any(marker.lower() in lowered for marker in _PHOTO_FALLBACK_ERRORS)


def _sensitive_roots() -> list[Path]:
    """Directories that must never be sent, even from an allowed root.

    The CC session runs with bypassPermissions and the system prompt nudges the
    model to call send_document(file_path=...), so a prompt injection from
    downloaded content could try to exfiltrate credentials. Context-lock routes
    the file to the operator's own chat, but a multi-user deployment exposes the
    whole home dir read-only — so we hard-block the credential stores. (S2.)
    """
    home = Path.home()
    return [
        home / ".config",  # ~/.config/cc/secrets.env
        home / ".claude",  # OAuth token + memory
        home / ".ssh",
        home / ".aws",
        home / ".gnupg",
        home / ".password-store",
    ]


def _allowed_roots() -> list[Path]:
    """Base directories a file may be sent from (default-deny outside these).

    Generous by design so legit sends never break: home (vault, code, Downloads,
    Desktop) + the system temp dirs (generated images, scratchpad) + the topic
    cwd + DEFAULT_CWD. Extend via BOT_FILE_ALLOWLIST (os.pathsep-separated) if a
    real path is ever blocked — blocks are logged loudly so that's diagnosable.
    """
    roots = [
        Path.home(),
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/folders"),  # macOS per-user temp
        Path(tempfile.gettempdir()),
        Path.cwd(),
    ]
    default_cwd = os.environ.get("DEFAULT_CWD", "")
    if default_cwd:
        roots.append(Path(default_cwd))
    extra = os.environ.get("BOT_FILE_ALLOWLIST", "")
    if extra:
        roots.extend(Path(p) for p in extra.split(os.pathsep) if p)
    out: list[Path] = []
    for r in roots:
        try:
            out.append(r.resolve())
        except OSError:
            continue
    return out


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_file_path(file_path: str) -> tuple[Path | None, str | None]:
    if not file_path:
        return None, "Error: file_path not provided"
    resolved = Path(file_path)
    if not resolved.exists():
        return None, f"Error: file not found: {file_path}"
    if not resolved.is_file():
        return None, f"Error: not a file: {file_path}"
    # Resolve symlinks BEFORE the allow/deny check so a link inside an allowed
    # root can't point at a sensitive target and escape the guard.
    real = resolved.resolve()
    if any(_is_within(real, deny) for deny in _sensitive_roots()):
        logger.warning("Blocked send of sensitive path: %s (real=%s)", file_path, real)
        return None, "Error: access to this file is denied"
    if not any(_is_within(real, root) for root in _allowed_roots()):
        logger.warning("Blocked send outside allowed roots: %s (real=%s)", file_path, real)
        return None, (
            "Error: file is outside the allowed directories "
            "(add the path to BOT_FILE_ALLOWLIST if needed)"
        )
    return resolved, None


@mcp.tool()
def send_message(
    text: str,
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> str:
    """Send a text message to the current Telegram chat/topic.

    In bot-launched sessions, chat/thread routing is taken from MCP env.
    """
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Error: BOT_TOKEN is not configured"
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    assert resolved_chat is not None
    return _post_message(token, resolved_chat, text, resolved_thread)


@mcp.tool()
def send_document(
    file_path: str,
    caption: str = "",
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> str:
    """Send a document/file to the current Telegram chat/topic."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Error: BOT_TOKEN is not configured"
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    resolved_path, path_error = _resolve_file_path(file_path)
    if path_error:
        return path_error
    assert resolved_chat is not None and resolved_path is not None
    return _send_document(token, resolved_chat, resolved_path, caption, resolved_thread)


@mcp.tool()
def send_image(
    file_path: str,
    caption: str = "",
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> str:
    """Send an image to the current Telegram chat/topic, inline when possible."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Error: BOT_TOKEN is not configured"
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    resolved_path, path_error = _resolve_file_path(file_path)
    if path_error:
        return path_error
    assert resolved_chat is not None and resolved_path is not None
    size = resolved_path.stat().st_size
    is_image = resolved_path.suffix.lower() in _IMAGE_EXTENSIONS and size <= _MAX_PHOTO_SIZE
    if not is_image:
        return _send_document(token, resolved_chat, resolved_path, caption, resolved_thread)
    result = _send_photo(token, resolved_chat, resolved_path, caption, resolved_thread)
    if _is_photo_fallback_error(result):
        return _send_document(token, resolved_chat, resolved_path, caption, resolved_thread)
    return result


def _safe_ext(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = Path(path).name.split("?")[0] or "image.png"
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", name)[:100]
    ext = Path(sanitized).suffix.lower()
    return ext if ext in _IMAGE_EXTENSIONS else ".png"


def _download_url(url: str) -> tuple[Path | None, str | None]:
    if not url.startswith("https://"):
        return None, "Error: only https URLs are supported"

    ext = _safe_ext(url)
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    dest = Path(tmp_path)
    max_mb = _MAX_FILE_SIZE // 1024 // 1024
    total = 0
    ok = False
    try:
        with (
            httpx.Client(timeout=30, follow_redirects=True) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code != 200:
                return None, f"Error: download failed ({resp.status_code})"
            final = str(resp.url)
            if not final.startswith("https://"):
                return None, "Error: redirect to non-https URL"
            with os.fdopen(fd, "wb") as f:
                fd = -1
                for chunk in resp.iter_bytes(8192):
                    total += len(chunk)
                    if total > _MAX_FILE_SIZE:
                        return None, f"Error: file exceeds {max_mb} MB"
                    f.write(chunk)
        ok = total > 0
    except httpx.TimeoutException:
        return None, "Error: timeout while downloading"
    except Exception as exc:
        return None, f"Error while downloading: {exc}"
    finally:
        if fd >= 0:
            os.close(fd)
        if not ok:
            dest.unlink(missing_ok=True)

    if not ok:
        return None, "Error: empty file"
    return dest, None


@mcp.tool()
def send_url_image(
    url: str,
    caption: str = "",
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> str:
    """Download an image from URL and send it to the current Telegram chat/topic.

    Use this after mj_imagine or any tool that returns an image URL.
    Combines download + send in one step.
    """
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Error: BOT_TOKEN is not configured"
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    assert resolved_chat is not None
    if not url:
        return "Error: url not provided"

    dest, dl_error = _download_url(url)
    if dl_error:
        return dl_error
    assert dest is not None

    try:
        size = dest.stat().st_size
        is_image = dest.suffix.lower() in _IMAGE_EXTENSIONS and size <= _MAX_PHOTO_SIZE
        if not is_image:
            return _send_document(token, resolved_chat, dest, caption, resolved_thread)
        result = _send_photo(token, resolved_chat, dest, caption, resolved_thread)
        if _is_photo_fallback_error(result):
            return _send_document(token, resolved_chat, dest, caption, resolved_thread)
        return result
    finally:
        dest.unlink(missing_ok=True)


if __name__ == "__main__":
    mcp.run()
