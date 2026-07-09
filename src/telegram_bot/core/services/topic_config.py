"""TopicConfig — reads topic_config.json with mtime-based caching."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# prompts/ is scanned lazily so a new prompts/<mode>.md can be dropped at runtime
# and referenced from topic_config.json without a bot restart. Result is cached
# and re-scanned only when the directory's mtime changes — steady-state cost is
# one os.stat() per call.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# stream_mode controls how intermediate CC events are pushed to Telegram:
#   verbose — every status / tool_use as a separate message (legacy default)
#   live    — one editable "thinking" message batches status lines with a
#             timestamp; rotates pages at the 4096-char limit. Final results
#             still arrive as separate messages.
#   minimal — only the thinking placeholder and final results; no progress
#             noise. Useful in project topics where we care about outcomes.
#   live+   — like live (status lines batch into one editable buffer), PLUS the
#             model's intermediate text comments (the "let me check X" replies it
#             emits between tool calls) are pushed live as their own 💬 messages
#             instead of being swallowed and overwritten by the final result. The
#             optional `stream_thinking` flag additionally streams reasoning
#             (thinking) blocks as 🧠 messages.
StreamMode = Literal["verbose", "live", "minimal", "live+"]
_VALID_STREAM_MODES: set[str] = {"verbose", "live", "minimal", "live+"}
_DEFAULT_STREAM_MODE: StreamMode = "live+"

# exec_mode selects the execution channel for Claude Code:
#   subprocess — one-shot `claude -p` per message (default, zero warm-up cost).
#   tmux       — persistent tmux session; enables TeamCreate and survives
#                bot restarts, but pays a lazy-start cost on first use.
#   streaming  — persistent headless `claude --input-format stream-json` process
#                (no TUI). Messages sent mid-turn are picked up BETWEEN tool
#                calls (terminal-style steering). No tmux/popup friction.
ExecMode = Literal["subprocess", "tmux", "streaming"]
_VALID_EXEC_MODES: set[str] = {"subprocess", "tmux", "streaming"}
# Default for NEW / unconfigured topics: streaming (live process, mid-turn
# steering). Topics with an explicit exec_mode in topic_config.json keep
# theirs. Memory is bounded by the StreamingManager concurrency cap + idle
# reaper.
_DEFAULT_EXEC_MODE: ExecMode = "streaming"

Engine = Literal["claude", "codex"]
_VALID_ENGINES: set[str] = {"claude", "codex"}
_DEFAULT_ENGINE: Engine = "claude"

# Mirrors sender_attribution.VALID_ATTRIBUTION_MODES — duplicated here (not
# imported) to keep this low-level config module free of handler/service deps.
_VALID_ATTRIBUTION_MODES: set[str] = {"auto", "always", "never"}

# checkpoint_on_reset: when enabled, a /new, /clear, or "Новый чат" reset asks
# the engine to write a background checkpoint of the work just done before its
# context is dropped, then spawns the fresh session. Works in both exec modes —
# tmux parks the live TUI aside, subprocess fires a detached `--resume` run. The
# checkpoint is headless (no Telegram output). A topic value of None inherits
# the bot-wide Settings.checkpoint_on_reset default. The prompt is sent verbatim
# so it can be a slash command ("/чекпоинт") or a plain instruction.
_DEFAULT_CHECKPOINT_PROMPT = (
    "Session is ending. Save a checkpoint of this session before context is lost: "
    "briefly record in memory/CLAUDE.md what was done and what is next. "
    "This runs in the background — no chat reply is needed."
)
_MODEL_OVERRIDE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_CORE_PROMPT_MODES: set[str] = {"task", "free"}

_valid_modes_cache: tuple[int, set[str]] = (-1, set())


def _normalize_model(model: object) -> str | None:
    """Normalize a model override from topic_config or UI writes."""
    if not isinstance(model, str):
        return None
    normalized = model.strip()
    if not normalized:
        return None
    return normalized if _MODEL_OVERRIDE_RE.fullmatch(normalized) else None


def _valid_modes() -> set[str]:
    """A mode is valid if a matching prompts/<mode>.md currently exists on disk."""
    global _valid_modes_cache
    try:
        mtime = _PROMPTS_DIR.stat().st_mtime_ns
    except OSError:
        return _valid_modes_cache[1] | _CORE_PROMPT_MODES
    if mtime != _valid_modes_cache[0]:
        _valid_modes_cache = (mtime, {p.stem for p in _PROMPTS_DIR.glob("*.md")})
    return _valid_modes_cache[1] | _CORE_PROMPT_MODES


@dataclass
class TopicSettings:
    """Per-topic configuration."""

    name: str
    type: str  # "assistant" | "project"
    mode: str  # public default modes: "free" | "task"; custom prompt file stems also work
    cwd: str | None  # None → Settings.default_cwd
    mcp_config: str | None  # None → default (.mcp.bot.json)
    stream_mode: StreamMode = _DEFAULT_STREAM_MODE
    # Only meaningful under stream_mode == "live+": when True, reasoning
    # (thinking) blocks are streamed too. Ignored by every other mode.
    stream_thinking: bool = False
    exec_mode: ExecMode = _DEFAULT_EXEC_MODE
    engine: Engine = _DEFAULT_ENGINE
    model: str | None = None
    usage_pin: bool | None = None
    # None → inherit the bot-wide Settings.checkpoint_on_reset default.
    checkpoint_on_reset: bool | None = None
    checkpoint_prompt: str | None = None
    # None → inherit the bot-wide Settings.attribute_senders default.
    # "auto" | "always" | "never" — see sender_attribution.py.
    attribute_senders: str | None = None
    # Per-topic instructions appended to --append-system-prompt on top of the
    # base Telegram prompt AND the default_system_prompt persona (it adds to the
    # persona, it does not replace it). None → base + persona only. Takes effect
    # on a new session (CC restores the original system prompt on --resume), so
    # /new applies an edited prompt. Applies to both subprocess and tmux exec_mode.
    system_prompt: str | None = None


def resolve_checkpoint_prompt(
    settings: TopicSettings,
    *,
    global_enabled: bool = False,
    global_prompt: str = "",
) -> str | None:
    """Resolve checkpoint-on-reset for a topic into a prompt, or None if off.

    Enablement: the topic value wins when set (True/False); None inherits
    `global_enabled`. Prompt precedence: per-topic prompt, then the bot-wide
    `global_prompt`, then the built-in default. Returns the verbatim prompt to
    send to the engine, or None when checkpointing is disabled for this topic.
    """
    enabled = (
        settings.checkpoint_on_reset if settings.checkpoint_on_reset is not None else global_enabled
    )
    if not enabled:
        return None
    if settings.checkpoint_prompt and settings.checkpoint_prompt.strip():
        return settings.checkpoint_prompt.strip()
    if global_prompt and global_prompt.strip():
        return global_prompt.strip()
    return _DEFAULT_CHECKPOINT_PROMPT


def _default_topic() -> TopicSettings:
    return TopicSettings(
        name="",
        type="assistant",
        mode="free",
        cwd=None,
        mcp_config=None,
        stream_mode=_DEFAULT_STREAM_MODE,
        exec_mode=_DEFAULT_EXEC_MODE,
    )


class TopicConfig:
    """Reads and caches topic_config.json with mtime-based invalidation.

    Provides per-topic settings and notification routing.
    """

    def __init__(self, config_path: str, project_root: str) -> None:
        self._config_path = config_path
        self._project_root = project_root
        # Nanosecond mtime — coarse-grained st_mtime collapses two writes inside
        # the same second, leaving the cache stale.
        self._last_mtime: int = 0
        self._topics: dict[int, TopicSettings] = {}
        self._routing: dict[str, int] = {}
        # Top-level "default_system_prompt": persona applied to every topic that
        # has no own system_prompt, and to the default channel (thread_id None).
        self._default_system_prompt: str | None = None
        # Top-level "chat_prompts": per-chat custom prompt for DMs (and any chat
        # addressed without a topic), keyed by chat_id. Appended on top of the
        # persona, same as a topic's system_prompt — see ClaudeService._topic_system_prompt.
        self._chat_prompts: dict[int, str] = {}
        # Serializes external writes to topic_config.json. Concurrent with the
        # forum_topic handler's own lock — fine, both use atomic os.replace so
        # the worst case is one write clobbering another, not corruption.
        self._write_lock = asyncio.Lock()

    def _maybe_reload(self) -> None:
        """Check file mtime and reload if changed."""
        try:
            st = os.stat(self._config_path)
        except (FileNotFoundError, OSError):
            if self._last_mtime != 0:
                # File disappeared — keep last valid cache
                logger.warning(
                    "Topic config file not found: %s, keeping cached config", self._config_path
                )
            elif not self._topics:
                logger.warning("Topic config file not found: %s", self._config_path)
            return

        if st.st_mtime_ns == self._last_mtime:
            return

        try:
            with open(self._config_path, encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON in topic config: %s, keeping cached config", self._config_path
            )
            # Keep last valid cache; update mtime to avoid re-reading on every call
            self._last_mtime = st.st_mtime_ns
            return
        except OSError:
            logger.warning("Failed to read topic config: %s", self._config_path)
            return

        self._parse_config(raw)
        self._last_mtime = st.st_mtime_ns
        logger.info(
            "Loaded topic config: %d topics, %d routing rules",
            len(self._topics),
            len(self._routing),
        )

    def _parse_config(self, raw: dict[str, object]) -> None:
        """Parse raw JSON dict into typed internal structures."""
        topics: dict[int, TopicSettings] = {}
        routing: dict[str, int] = {}

        # Parse topics
        raw_topics = raw.get("topics", {})
        if isinstance(raw_topics, dict):
            for key, value in raw_topics.items():
                try:
                    thread_id = int(key)
                except (ValueError, TypeError):
                    logger.warning("Non-numeric thread_id key skipped: %r", key)
                    continue

                if not isinstance(value, dict):
                    logger.warning("Invalid topic config for thread_id %d, skipping", thread_id)
                    continue

                name = str(value.get("name", ""))
                topic_type = str(value.get("type", "assistant"))
                mode = str(value.get("mode", "free"))
                cwd = value.get("cwd")
                mcp_config = value.get("mcp_config")

                # Validate mode
                if mode not in _valid_modes():
                    logger.warning(
                        "Invalid mode %r for topic %d, falling back to 'free'", mode, thread_id
                    )
                    mode = "free"

                # Validate cwd
                if cwd is not None:
                    cwd = str(cwd)
                    if not os.path.isabs(cwd):
                        logger.warning(
                            "Relative cwd path %r for topic %d, falling back to None",
                            cwd,
                            thread_id,
                        )
                        cwd = None
                    elif not os.path.isdir(cwd):
                        logger.warning(
                            "Non-existent cwd directory %r for topic %d, falling back to None",
                            cwd,
                            thread_id,
                        )
                        cwd = None

                # Validate mcp_config
                if mcp_config is not None:
                    mcp_config = str(mcp_config)
                    if not os.path.isabs(mcp_config):
                        logger.warning(
                            "Relative mcp_config path %r for topic %d, falling back to None",
                            mcp_config,
                            thread_id,
                        )
                        mcp_config = None
                    elif not os.path.isfile(mcp_config):
                        logger.warning(
                            "Non-existent mcp_config file %r for topic %d, falling back to None",
                            mcp_config,
                            thread_id,
                        )
                        mcp_config = None

                # Validate stream_mode
                raw_stream_mode = value.get("stream_mode", _DEFAULT_STREAM_MODE)
                if raw_stream_mode not in _VALID_STREAM_MODES:
                    logger.warning(
                        "Invalid stream_mode %r for topic %d, falling back to %r",
                        raw_stream_mode,
                        thread_id,
                        _DEFAULT_STREAM_MODE,
                    )
                    stream_mode: StreamMode = _DEFAULT_STREAM_MODE
                else:
                    stream_mode = raw_stream_mode

                raw_stream_thinking = value.get("stream_thinking")
                stream_thinking: bool = (
                    raw_stream_thinking if isinstance(raw_stream_thinking, bool) else False
                )

                # Validate exec_mode.
                # isinstance(raw, str) gate must precede the `in _VALID_EXEC_MODES`
                # check — a list/dict/None value would otherwise raise
                # TypeError: unhashable type during set membership and crash the
                # entire config parse.
                raw_exec_mode = value.get("exec_mode", _DEFAULT_EXEC_MODE)
                if not isinstance(raw_exec_mode, str) or raw_exec_mode not in _VALID_EXEC_MODES:
                    logger.warning(
                        "Invalid exec_mode %r for topic %d, falling back to %r",
                        raw_exec_mode,
                        thread_id,
                        _DEFAULT_EXEC_MODE,
                    )
                    exec_mode: ExecMode = _DEFAULT_EXEC_MODE
                else:
                    exec_mode = raw_exec_mode  # type: ignore[assignment]

                raw_engine = value.get("engine", _DEFAULT_ENGINE)
                if not isinstance(raw_engine, str) or raw_engine not in _VALID_ENGINES:
                    logger.warning(
                        "Invalid engine %r for topic %d, falling back to %r",
                        raw_engine,
                        thread_id,
                        _DEFAULT_ENGINE,
                    )
                    engine: Engine = _DEFAULT_ENGINE
                else:
                    engine = raw_engine  # type: ignore[assignment]

                raw_model = value.get("model")
                model = _normalize_model(raw_model)
                if isinstance(raw_model, str) and raw_model.strip() and model is None:
                    logger.warning("Invalid model %r for topic %d, dropping", raw_model, thread_id)

                raw_usage_pin = value.get("usage_pin")
                usage_pin: bool | None = None
                if isinstance(raw_usage_pin, bool):
                    usage_pin = raw_usage_pin

                raw_ckpt = value.get("checkpoint_on_reset")
                checkpoint_on_reset: bool | None = raw_ckpt if isinstance(raw_ckpt, bool) else None

                raw_ckpt_prompt = value.get("checkpoint_prompt")
                checkpoint_prompt: str | None = None
                if isinstance(raw_ckpt_prompt, str) and raw_ckpt_prompt.strip():
                    checkpoint_prompt = raw_ckpt_prompt.strip()

                raw_attr = value.get("attribute_senders")
                attribute_senders: str | None = None
                if isinstance(raw_attr, str) and raw_attr in _VALID_ATTRIBUTION_MODES:
                    attribute_senders = raw_attr
                elif raw_attr is not None:
                    logger.warning(
                        "Invalid attribute_senders %r for topic %d, inheriting default",
                        raw_attr,
                        thread_id,
                    )

                raw_sysprompt = value.get("system_prompt")
                system_prompt: str | None = None
                if isinstance(raw_sysprompt, str) and raw_sysprompt.strip():
                    system_prompt = raw_sysprompt.strip()

                topics[thread_id] = TopicSettings(
                    name=name,
                    type=topic_type,
                    mode=mode,
                    cwd=cwd,
                    mcp_config=mcp_config,
                    stream_mode=stream_mode,
                    stream_thinking=stream_thinking,
                    exec_mode=exec_mode,
                    engine=engine,
                    model=model,
                    usage_pin=usage_pin,
                    checkpoint_on_reset=checkpoint_on_reset,
                    checkpoint_prompt=checkpoint_prompt,
                    attribute_senders=attribute_senders,
                    system_prompt=system_prompt,
                )

        # Parse routing
        raw_routing = raw.get("routing", {})
        if isinstance(raw_routing, dict):
            for key, value in raw_routing.items():
                try:
                    routing[str(key)] = int(value)
                except (ValueError, TypeError):
                    logger.warning("Invalid routing value for %r: %r, skipping", key, value)

        raw_default_prompt = raw.get("default_system_prompt")
        if isinstance(raw_default_prompt, str) and raw_default_prompt.strip():
            self._default_system_prompt = raw_default_prompt.strip()
        else:
            self._default_system_prompt = None

        # Parse chat_prompts (optional; absent in legacy configs).
        chat_prompts: dict[int, str] = {}
        raw_chat_prompts = raw.get("chat_prompts", {})
        if isinstance(raw_chat_prompts, dict):
            for key, value in raw_chat_prompts.items():
                try:
                    chat_id = int(key)
                except (ValueError, TypeError):
                    logger.warning("Non-numeric chat_prompts key skipped: %r", key)
                    continue
                if isinstance(value, str) and value.strip():
                    chat_prompts[chat_id] = value.strip()

        self._topics = topics
        self._routing = routing
        self._chat_prompts = chat_prompts

    def default_system_prompt(self) -> str | None:
        """Persona applied to topics without their own system_prompt (and DM)."""
        self._maybe_reload()
        return self._default_system_prompt

    def get_topic(self, thread_id: int | None) -> TopicSettings:
        """Return settings for a thread_id. Unknown/None returns defaults."""
        self._maybe_reload()
        if thread_id is None:
            return _default_topic()
        return self._topics.get(thread_id, _default_topic())

    def get_chat_prompt(self, chat_id: int | None) -> str | None:
        """Per-chat custom prompt for a DM/topicless chat, or None if unset."""
        self._maybe_reload()
        if chat_id is None:
            return None
        return self._chat_prompts.get(chat_id)

    def get_routing(self, notification_type: str) -> int | None:
        """Return thread_id for a notification type, or None if not configured."""
        self._maybe_reload()
        return self._routing.get(notification_type)

    async def _update_topic_field(
        self, *, thread_id: int, field_name: str, value: object, log_label: str
    ) -> bool:
        """Persist a single field on one topic atomically.

        Shared body of `update_stream_mode` and `update_exec_mode` — both
        differ only in (a) the input-validation set and (b) the key to
        set. Validation happens in the callers; this helper just handles
        the read-modify-write cycle under `self._write_lock`.

        `log_label` is used as the prefix for all warning/info records
        so the provenance (stream_mode vs exec_mode) survives in logs.
        """
        async with self._write_lock:
            try:
                with open(self._config_path, encoding="utf-8") as f:
                    data: Any = json.load(f)
            except FileNotFoundError:
                data = {"topics": {}}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("%s: cannot read config: %s", log_label, exc)
                return False

            if not isinstance(data, dict):
                logger.warning("%s: config top-level not an object", log_label)
                return False
            topics = data.setdefault("topics", {})
            if not isinstance(topics, dict):
                logger.warning("%s: topics is not an object", log_label)
                return False

            key = str(thread_id)
            topic = topics.setdefault(key, {})
            if not isinstance(topic, dict):
                logger.warning("%s: topic %s is not an object", log_label, key)
                return False
            topic[field_name] = value

            path = Path(self._config_path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            except OSError as exc:
                logger.warning("%s: write failed: %s", log_label, exc)
                return False

        logger.info("Set %s=%s for thread_id=%d", field_name, value, thread_id)
        return True

    async def _update_topic_fields(
        self, *, thread_id: int, values: dict[str, object], log_label: str
    ) -> bool:
        """Persist multiple fields on one topic with one atomic file replace."""
        async with self._write_lock:
            try:
                with open(self._config_path, encoding="utf-8") as f:
                    data: Any = json.load(f)
            except FileNotFoundError:
                data = {"topics": {}}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("%s: cannot read config: %s", log_label, exc)
                return False

            if not isinstance(data, dict):
                logger.warning("%s: config top-level not an object", log_label)
                return False
            topics = data.setdefault("topics", {})
            if not isinstance(topics, dict):
                logger.warning("%s: topics is not an object", log_label)
                return False

            key = str(thread_id)
            topic = topics.setdefault(key, {})
            if not isinstance(topic, dict):
                logger.warning("%s: topic %s is not an object", log_label, key)
                return False
            topic.update(values)

            path = Path(self._config_path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            except OSError as exc:
                logger.warning("%s: write failed: %s", log_label, exc)
                return False

        logger.info("Set %s for thread_id=%d", values, thread_id)
        return True

    async def update_stream_mode(self, thread_id: int, mode: StreamMode) -> bool:
        """Persist a new stream_mode for one topic. Returns False on bad input."""
        if mode not in _VALID_STREAM_MODES:
            logger.warning("update_stream_mode: invalid mode %r", mode)
            return False
        return await self._update_topic_field(
            thread_id=thread_id,
            field_name="stream_mode",
            value=mode,
            log_label="update_stream_mode",
        )

    async def update_stream_thinking(self, thread_id: int, enabled: bool) -> bool:
        """Persist the stream_thinking toggle for one topic (live+ only)."""
        return await self._update_topic_field(
            thread_id=thread_id,
            field_name="stream_thinking",
            value=bool(enabled),
            log_label="update_stream_thinking",
        )

    async def update_exec_mode(self, thread_id: int, mode: str) -> bool:
        """Persist a new exec_mode for one topic. Returns False on bad input."""
        if mode not in _VALID_EXEC_MODES:
            logger.warning("update_exec_mode: invalid mode %r", mode)
            return False
        return await self._update_topic_field(
            thread_id=thread_id,
            field_name="exec_mode",
            value=mode,
            log_label="update_exec_mode",
        )

    async def update_engine(self, thread_id: int, engine: Engine) -> bool:
        """Persist a new engine for one topic. Returns False on bad input."""
        if engine not in _VALID_ENGINES:
            logger.warning("update_engine: invalid engine %r", engine)
            return False
        return await self._update_topic_field(
            thread_id=thread_id,
            field_name="engine",
            value=engine,
            log_label="update_engine",
        )

    async def update_model(self, thread_id: int, model: str | None) -> bool:
        """Persist model for one topic. None is written as JSON null."""
        normalized = _normalize_model(model)
        if isinstance(model, str) and model.strip() and normalized is None:
            logger.warning("update_model: invalid model %r", model)
            return False
        return await self._update_topic_field(
            thread_id=thread_id,
            field_name="model",
            value=normalized,
            log_label="update_model",
        )

    async def update_system_prompt(self, thread_id: int, value: str | None) -> bool:
        """Persist a topic's custom system_prompt. Empty/None resets to JSON null
        (the topic then falls back to the global default_system_prompt)."""
        normalized = value.strip() if isinstance(value, str) and value.strip() else None
        return await self._update_topic_field(
            thread_id=thread_id,
            field_name="system_prompt",
            value=normalized,
            log_label="update_system_prompt",
        )

    async def update_chat_prompt(self, chat_id: int, value: str | None) -> bool:
        """Persist a per-chat custom prompt under top-level "chat_prompts".

        Empty/None removes the key (chat falls back to default_system_prompt).
        Atomic read-modify-write under `self._write_lock`, mirroring
        `_update_topic_field` but on the top-level chat_prompts map.
        """
        normalized = value.strip() if isinstance(value, str) and value.strip() else None
        log_label = "update_chat_prompt"
        async with self._write_lock:
            try:
                with open(self._config_path, encoding="utf-8") as f:
                    data: Any = json.load(f)
            except FileNotFoundError:
                data = {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("%s: cannot read config: %s", log_label, exc)
                return False

            if not isinstance(data, dict):
                logger.warning("%s: config top-level not an object", log_label)
                return False
            chat_prompts = data.setdefault("chat_prompts", {})
            if not isinstance(chat_prompts, dict):
                logger.warning("%s: chat_prompts is not an object", log_label)
                return False

            key = str(chat_id)
            if normalized is None:
                chat_prompts.pop(key, None)
            else:
                chat_prompts[key] = normalized

            if not self._atomic_write_config(data, log_label=log_label):
                return False

        logger.info("Set chat_prompt for chat_id=%d (cleared=%s)", chat_id, normalized is None)
        return True

    def _atomic_write_config(self, data: object, *, log_label: str) -> bool:
        """Serialize `data` to the config path via a unique tmp-file + os.replace.

        Call under `self._write_lock`. A per-write unique temp name (not a shared
        ``.tmp``) avoids a cross-process race with the forum_topic handler, which
        writes the same config under a different lock: a shared temp path could
        make one writer's os.replace hit FileNotFoundError when the other already
        renamed it. Returns False (and logs) on OSError.
        """
        path = Path(self._config_path)
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_name, path)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            logger.warning("%s: write failed: %s", log_label, exc)
            return False
        return True

    async def update_engine_model(self, thread_id: int, engine: Engine, model: str | None) -> bool:
        """Persist engine and model together with one atomic config write."""
        if engine not in _VALID_ENGINES:
            logger.warning("update_engine_model: invalid engine %r", engine)
            return False
        normalized = _normalize_model(model)
        if isinstance(model, str) and model.strip() and normalized is None:
            logger.warning("update_engine_model: invalid model %r", model)
            return False
        return await self._update_topic_fields(
            thread_id=thread_id,
            values={"engine": engine, "model": normalized},
            log_label="update_engine_model",
        )

    async def update_engine_model_exec_mode(
        self,
        thread_id: int,
        engine: Engine,
        model: str | None,
        exec_mode: str,
    ) -> bool:
        """Persist engine/model/exec_mode together with one atomic config write."""
        if engine not in _VALID_ENGINES:
            logger.warning("update_engine_model_exec_mode: invalid engine %r", engine)
            return False
        if exec_mode not in _VALID_EXEC_MODES:
            logger.warning("update_engine_model_exec_mode: invalid exec_mode %r", exec_mode)
            return False
        normalized = _normalize_model(model)
        if isinstance(model, str) and model.strip() and normalized is None:
            logger.warning("update_engine_model_exec_mode: invalid model %r", model)
            return False
        return await self._update_topic_fields(
            thread_id=thread_id,
            values={"engine": engine, "model": normalized, "exec_mode": exec_mode},
            log_label="update_engine_model_exec_mode",
        )
