"""SessionManager — Claude Code subprocess lifecycle management."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import re
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from telegram_bot.core.config import Settings
from telegram_bot.core.messages import t
from telegram_bot.core.services import cc_events as _cc_events
from telegram_bot.core.services.bot_mcp_runtime import (
    default_bot_mcp_config,
    ensure_bot_runtime_mcp_config,
)
from telegram_bot.core.services.cc_events import (
    _EXTRA_BASH_RULES,  # noqa: F401 — re-export for tests/fixtures
    _EXTRA_FILE_PATH_RULES,  # noqa: F401 — re-export for tests/fixtures
    _EXTRA_TOOL_STATUS,  # noqa: F401 — re-export for tests/fixtures
    TOOL_STATUS_MAP,
    StreamEvent,
    _agent_done_status,
    _tool_status,
    parse_cc_event,
)
from telegram_bot.core.services.cc_modes import (
    _MODE_TOOLS,
    DEFAULT_MODE,
    FREE_MODE_PROMPT,
    FREE_MODE_TOOLS,
    TASK_MODE_PROMPT,
    TASK_MODE_TOOLS,
    Mode,
    _get_mode_prompt,
)
from telegram_bot.core.services.codex_mcp import build_codex_mcp_config_args
from telegram_bot.core.services.providers import CODEX_ADAPTER, ExecCommand, choose_available_engine
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config
from telegram_bot.core.types import ChannelKey

if TYPE_CHECKING:
    from telegram_bot.core.services.topic_config import TopicConfig

logger = logging.getLogger(__name__)

_TELEGRAM_SYSTEM_PROMPT = (
    "You are running inside a Telegram bot. The user communicates through "
    "Telegram and can ONLY see messages you send back via the chat. "
    "To send files to the user, call mcp__bot__send_document(file_path=...). "
    "To send images inline, call mcp__bot__send_image(file_path=...). "
    "NEVER tell the user 'the file is on disk' or 'open it yourself' — "
    "they cannot access the filesystem. Always send files via the bot MCP tools."
)

# Re-exports — public API surface of this module. Downstream imports
# (tmux_manager, streaming, handlers/*) use these names; listing them
# in __all__ makes the re-export explicit for mypy.
__all__ = [
    "DEFAULT_MODE",
    "FREE_MODE_PROMPT",
    "FREE_MODE_TOOLS",
    "TASK_MODE_PROMPT",
    "TASK_MODE_TOOLS",
    "TOOL_STATUS_MAP",
    "CCInactivityError",
    "CCNotFoundError",
    "CCProcessError",
    "CCTimeoutError",
    "Mode",
    "ReplySessionRef",
    "SessionData",
    "SessionManager",
    "StreamEvent",
    "_agent_done_status",
    "_tool_status",
    "parse_cc_event",
]

_POLL_SEC = 30.0  # readline poll interval for inactivity check (not user-facing)
_MODEL_OVERRIDE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")

# Topics whose name is NOT a subject — free-for-all threads where forcing an
# on-topic focus would be wrong. Matched case-insensitively against topic.name.
# A topic can still opt in/out further via its own system_prompt.
_FREEFORM_TOPIC_NAMES = frozenset(
    {
        "рандом",
        "random",
        "general",
        "общее",
        "разное",
        "прочее",
        "misc",
        "офтоп",
        "оффтоп",
        "off-topic",
        "offtopic",
        "болталка",
        "флуд",
        "разговоры",
        "чат",
        "чатик",
        "дебаг",
        "debug",
    }
)


def _valid_model_override(model: object) -> str | None:
    if not isinstance(model, str):
        return None
    normalized = model.strip()
    return normalized if _MODEL_OVERRIDE_RE.fullmatch(normalized) else None


@dataclass
class SessionData:
    session_id: str | None = None
    process: asyncio.subprocess.Process | None = None
    process_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_activity: float = 0.0
    last_stream_event: float = 0.0
    mode: Mode = "free"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancelled: bool = False
    cwd: str = ""
    mcp_config: str = ""
    chat_id: int = 0
    thread_id: int | None = None
    engine: str = "claude"
    model: str | None = None


@dataclass(frozen=True)
class ReplySessionRef:
    """Provider-aware reply target resolved from a Telegram message id."""

    session_id: str
    provider: str = "claude"
    model: str | None = None
    exec_mode: str | None = None


def _provider_from_session_id(session_id: str) -> str:
    """Infer provider from a session_id when the stored mapping omits it.

    Codex emits UUIDv7 session ids (version=7); claude emits UUIDv4 (version=4).
    Used by ``resolve_reply_reference`` to handle legacy plain-string entries
    written before ``record_message`` persisted the provider — otherwise
    reply-to-resume on a codex message routes the topic to claude and breaks
    the live session. Defaults to ``"claude"`` for malformed or non-UUID ids
    (the legacy ``record_message`` default before this fix).
    """
    try:
        return "codex" if uuid.UUID(session_id).version == 7 else "claude"
    except ValueError:
        return "claude"


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        topic_config: TopicConfig | None = None,
    ) -> None:
        self._settings = settings
        self._topic_config = topic_config
        self._sessions: dict[ChannelKey, SessionData] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._msg_sessions: collections.OrderedDict[int, object] = collections.OrderedDict()
        self._mapping_path = Path(settings.session_mapping_path)
        # Persisted channel_key → session_id: lets bot resume CC session after restart
        self._channel_sessions: dict[str, object] = {}
        self._channel_sessions_path = self._mapping_path.with_name("channel_sessions.json")
        # Channels where next message should ignore reply-to-resume (set after kill/reset)
        self._fresh_channels: set[str] = set()
        # Copy of the module-level _MODE_TOOLS so instance-scoped extensions
        # (extend_mode_tools) don't leak into other SessionManager instances.
        self._mode_tools: dict[str, str] = dict(_MODE_TOOLS)
        # Strong refs to detached checkpoint-on-reset processes (see
        # spawn_background_checkpoint) so their watchdog tasks aren't GC'd.
        self._checkpoint_tasks: set[asyncio.Task[None]] = set()

    def extend_mode_tools(self, extensions: dict[str, list[str]]) -> None:
        """Append tool names to the allowedTools list of one or more modes.

        Used by private bot entry points to attach assistant-specific MCP
        tools that must not live in the public core.
        """
        for mode, tools in extensions.items():
            if mode not in self._mode_tools:
                raise ValueError(f"Unknown mode: {mode!r}")
            if not tools:
                continue
            current = self._mode_tools[mode]
            addition = ",".join(tools)
            self._mode_tools[mode] = f"{current},{addition}" if current else addition

    @staticmethod
    def extend_tool_status_map(extensions: dict[str, str]) -> None:
        """Register extra tool → status labels.

        Overwrites existing keys, so callers can override core defaults.
        Module-level state lives in `cc_events` — this wrapper mutates
        that registry so `_tool_status_map()` picks up the extensions.
        """
        _cc_events._EXTRA_TOOL_STATUS.update(extensions)

    @staticmethod
    def extend_file_path_rules(rules: list[tuple[str, str, str]]) -> None:
        """Register extra (path_substring, read_status, write_status) rules.

        Appended after the core generic rules — first match wins, so core
        patterns (memory/, .claude/skills/) still take precedence.
        """
        _cc_events._EXTRA_FILE_PATH_RULES.extend(rules)

    @staticmethod
    def extend_bash_rules(rules: list[tuple[str, str]]) -> None:
        """Register extra (bash_substring, status) rules.

        Prepended before core rules so private substrings win over generic
        ones before generic rules such as raw `git`.
        """
        _cc_events._EXTRA_BASH_RULES.extend(rules)

    @property
    def file_cache_dir(self) -> str:
        """Media cache path exposed to handlers as an absolute path.

        Agents run with per-topic cwd, which can be different from the bot
        repo. Relative media paths would then be resolved against the wrong
        project when the agent uses Read.
        """
        configured = Path(self._settings.file_cache_dir)
        if not configured.is_absolute():
            configured = Path(self._settings.project_root) / configured
        return str(configured.resolve())

    def default_mcp_config_path(self) -> str:
        """Default MCP config path used by bot-launched sessions."""
        return str(default_bot_mcp_config(self._settings.project_root))

    def prepare_runtime_mcp_config(
        self, channel_key: ChannelKey, base_mcp_config: str
    ) -> tuple[str, Path]:
        """Generate a runtime MCP config that merges the standard bot server
        (send_image/send_document/send_message, scoped to this channel) into the
        base config, and write it to a temp file.

        Mirrors what the one-shot `_run_streaming_subprocess` path does inline
        (see ~`session.mcp_config = ensure_bot_runtime_mcp_config(...)`). The
        persistent streaming path needs the SAME wiring — without it the default
        base path (`.mcp.bot.json`) does not exist, `_base_command_argv` drops
        `--mcp-config`, and the agent has no bot tools.

        Returns ``(config_path, runtime_file_path)``. The caller owns the
        runtime file's lifetime (unlink it when the process dies)."""
        chat_id, thread_id = channel_key
        runtime_path = (
            Path(self.file_cache_dir)
            / "mcp-runtime"
            / f"{chat_id}-{thread_id}-{time.time_ns()}.json"
        )
        config = ensure_bot_runtime_mcp_config(
            base_mcp_config=base_mcp_config or self.default_mcp_config_path(),
            channel_key=channel_key,
            runtime_path=runtime_path,
            project_root=self._settings.project_root,
        )
        return config, runtime_path

    def _default_cwd(self) -> Path:
        """Default agent working directory, resolved relative to project_root."""
        configured = Path(self._settings.default_cwd)
        if configured.is_absolute():
            return configured
        return Path(self._settings.project_root) / configured

    @staticmethod
    def _ch_key(channel_key: ChannelKey) -> str:
        """Serialize ChannelKey to a JSON-safe string key."""
        return f"{channel_key[0]}:{channel_key[1]}"

    @staticmethod
    def _atomic_write_json(path: Path, data: object) -> None:
        """Write JSON via tmp + os.replace so a kill mid-write can't truncate the file.

        Guards against SIGTERM/kill during write; power loss is best-effort
        (fsync on the tmp file, no directory fsync). Tmp is created 0600 so
        the data is never world-readable, even transiently.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)

    @staticmethod
    def _session_ref(provider: str, session_id: str, model: str | None = None) -> object:
        """Persist legacy Claude as a string; use typed refs when needed."""
        if provider == "claude" and model is None:
            return session_id
        return {
            "provider": provider,
            "session_id": session_id,
            "model": model,
        }

    def _apply_topic_config(self, session: SessionData, channel_key: ChannelKey) -> None:
        """Refresh session.cwd / mode / mcp_config from current topic_config.

        Called on every session lookup so that after CC (or anyone) edits
        topic_config.json the next message in that thread picks up the new
        cwd immediately — without forcing the user to press "New chat".
        TopicConfig has its own mtime cache, so this is cheap.

        No-op when topic_config is not wired in (classic mode): in that case
        session.cwd / mode / mcp_config is owned by whoever instantiated the
        session and should not be overwritten.
        """
        thread_id = channel_key[1]
        if self._topic_config is None or thread_id is None:
            return

        topic = self._topic_config.get_topic(thread_id)
        runtime = resolve_topic_runtime_config(
            topic,
            BotDefaults(
                cwd=self._default_cwd(),
                mcp_config=Path(self._settings.project_root) / ".mcp.bot.json",
            ),
        )
        session.mode = runtime.mode
        session.cwd = str(runtime.cwd)
        session.mcp_config = runtime.mcp_config or ""
        session.engine = choose_available_engine(runtime.engine) or runtime.engine
        session.model = runtime.model

    def _get_session(self, channel_key: ChannelKey) -> SessionData:
        if channel_key not in self._sessions:
            # Classic-mode defaults — may be overwritten below by _apply_topic_config
            # when a topic_config is wired in.
            session = SessionData(
                mode="free",
                cwd=str(self._default_cwd()),
                mcp_config=str(Path(self._settings.project_root) / ".mcp.bot.json"),
                chat_id=channel_key[0],
                thread_id=channel_key[1],
            )
            # Restore last session_id so CC can resume conversation after restart
            saved = self._channel_sessions.get(self._ch_key(channel_key))
            saved_sid: str | None = None
            saved_provider = "claude"
            saved_model: str | None = None
            if isinstance(saved, str):
                saved_sid = saved
            elif isinstance(saved, dict):
                sid = saved.get("session_id")
                provider = saved.get("provider")
                model = saved.get("model")
                if isinstance(sid, str):
                    saved_sid = sid
                if isinstance(provider, str):
                    saved_provider = provider
                if isinstance(model, str):
                    saved_model = model
            if saved_sid:
                session.session_id = saved_sid
                session.engine = saved_provider
                session.model = saved_model
                logger.info(
                    "Restoring provider=%s session_id=%s for channel %s",
                    saved_provider,
                    saved_sid,
                    channel_key,
                )
            self._sessions[channel_key] = session
        # Apply on every lookup — picks up live edits to topic_config.json.
        self._apply_topic_config(self._sessions[channel_key], channel_key)
        return self._sessions[channel_key]

    async def _kill_process(self, process: asyncio.subprocess.Process) -> None:
        """Kill a CC subprocess and its process group immediately via SIGKILL."""
        if process.returncode is not None:
            return
        pid = process.pid
        if pid is None:
            return
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
            logger.info("Sent SIGKILL to process group %d", pgid)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                logger.warning("process.wait() timed out 5s after SIGKILL for pgid %d", pgid)
        except OSError:
            # Orphaned CC process is a real operational signal — INFO, not
            # DEBUG. "Already exited" is the benign common case; a persistent
            # "no permission" storm in journalctl is the red flag.
            logger.info(
                "Process %d not killable (already exited or no permission)",
                pid,
                exc_info=True,
            )

    async def _kill_session(self, session: SessionData) -> None:
        """Kill session's process if alive, reset session state."""
        if session.process is not None:
            await self._kill_process(session.process)
            session.process = None
        session.session_id = None
        session.last_activity = 0.0
        session.cancelled = False

    def _base_command_argv(
        self,
        mode: Mode = "free",
        mcp_config: str = "",
        chat_id: int = 0,
        thread_id: int | None = None,
    ) -> list[str]:
        """Shared base argv (flags, tools, system prompt, mcp) for one-shot AND
        streaming commands. The two diverge only in how the prompt is delivered
        (`-p <prompt>` argv vs `--input-format stream-json` over stdin)."""
        mcp_path = mcp_config or str(Path(self._settings.project_root) / ".mcp.bot.json")
        base = [
            "claude",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            self._mode_tools[mode],
            "--disallowedTools",
            # TeamCreate blocked: CC subprocess bug #29293
            "AskUserQuestion,EnterPlanMode,TeamCreate",
            "--max-turns",
            str(self._settings.cc_max_turns),
            "--append-system-prompt",
            self._topic_system_prompt(thread_id, chat_id),
        ]
        # Only attach an MCP config when the file exists. CC fails fast with
        # "Invalid MCP configuration" if --mcp-config points at a missing path,
        # which would block the bot for any user without an .mcp.bot.json.
        if Path(mcp_path).exists():
            base.extend(["--mcp-config", mcp_path])
        return base

    def _build_command(
        self,
        prompt: str,
        session_id: str | None,
        mode: Mode = "free",
        mcp_config: str = "",
        chat_id: int = 0,
        thread_id: int | None = None,
        model: str | None = None,
    ) -> tuple[list[str], str]:
        """Build the claude CLI argv and the prompt to feed via stdin.

        Returns ``(argv, stdin_text)``. The prompt goes to stdin (`claude -p`
        with no prompt argument reads it from stdin), NOT into argv — argv is
        world-readable via `ps`/procfs, so an argv prompt would leak the content
        of replies/forwards to any local user. Codex and streaming already use
        stdin; this brings the subprocess path in line. (S7, audit 2026-07-02.)
        """
        base = self._base_command_argv(mode, mcp_config, chat_id, thread_id)
        # Pin the topic's model override (from the /model picker) so the
        # subprocess path honours it like the tmux/streaming paths. On --resume
        # CC keeps the transcript context but runs the turn on this model.
        if model:
            base = [*base, "--model", model]

        if session_id:
            # CC --resume reuses the session's original system prompt.
            return [*base, "--resume", session_id, "-p"], prompt

        tg_context = self._build_tg_context(
            chat_id, thread_id, cwd_configured=self._is_cwd_configured(thread_id)
        )
        full_prompt = _get_mode_prompt(mode) + tg_context + prompt
        return [*base, "-p"], full_prompt

    def build_streaming_argv(
        self,
        session_id: str | None,
        mode: Mode = "free",
        mcp_config: str = "",
        chat_id: int = 0,
        thread_id: int | None = None,
        model: str | None = None,
    ) -> list[str]:
        """Argv for a persistent `claude --input-format stream-json` process.

        No prompt arg — user messages are fed via stdin as NDJSON, so the live
        process stays open across turns and picks up mid-turn messages between
        tool calls. `--resume <sid>` restores a prior session's context; a fresh
        session gets its mode prompt + tg context prepended to the FIRST stdin
        message by the caller (`_build_full_prompt`, which StreamingManager
        reuses). `--input-format stream-json` requires `--print` (`-p`).

        `model` (the topic's model override) is pinned with `--model` so the
        streaming path honours the `/model` picker like the tmux/one-shot paths.
        On `--resume` CC keeps the transcript's context but runs the new turn on
        this model, so switching model + resuming continues the same conversation.
        """
        argv = [*self._base_command_argv(mode, mcp_config, chat_id, thread_id)]
        argv += ["--input-format", "stream-json"]
        if session_id:
            argv += ["--resume", session_id]
        if model:
            argv += ["--model", model]
        argv.append("-p")
        return argv

    def _build_full_prompt(
        self,
        prompt: str,
        session_id: str | None,
        mode: Mode,
        chat_id: int,
        thread_id: int | None,
    ) -> str:
        """Build the actual prompt text sent to an engine."""
        if session_id:
            return prompt
        tg_context = self._build_tg_context(
            chat_id, thread_id, cwd_configured=self._is_cwd_configured(thread_id)
        )
        return _get_mode_prompt(mode) + tg_context + prompt

    def _build_exec_command(self, prompt: str, session: SessionData) -> ExecCommand:
        """Provider-aware subprocess command.

        Claude keeps the historical argv-prompt contract. Codex receives the
        prompt via stdin and writes the final answer to a unique temp file.
        """
        cwd = session.cwd or str(self._default_cwd())
        if session.engine != "codex":
            argv, stdin_text = self._build_command(
                prompt,
                session.session_id,
                session.mode,
                session.mcp_config,
                session.chat_id,
                session.thread_id,
                session.model,
            )
            return ExecCommand(argv=argv, cwd=cwd, stdin_text=stdin_text)

        output_dir = Path(self.file_cache_dir) / "codex-last-message"
        output_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            output_dir.chmod(0o700)
        output_path = output_dir / f"{session.chat_id}-{session.thread_id}-{time.time_ns()}.txt"
        with contextlib.suppress(FileNotFoundError):
            output_path.unlink()

        if session.session_id:
            argv = [
                CODEX_ADAPTER.binary(),
                "exec",
                "resume",
                *build_codex_mcp_config_args(session.mcp_config),
                session.session_id,
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-o",
                str(output_path),
                "-",
            ]
        else:
            argv = [
                CODEX_ADAPTER.binary(),
                "exec",
                *build_codex_mcp_config_args(session.mcp_config),
                "--json",
                "--cd",
                cwd,
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-o",
                str(output_path),
                "-",
            ]
        if session.model:
            # Insert before trailing "-" so the stdin marker remains last.
            argv[-1:-1] = ["--model", session.model]
        return ExecCommand(
            argv=argv,
            cwd=cwd,
            stdin_text=self._build_full_prompt(
                prompt,
                session.session_id,
                session.mode,
                session.chat_id,
                session.thread_id,
            ),
            output_last_message_path=output_path,
        )

    def build_tmux_startup_args(
        self,
        mode: Mode = "free",
        mcp_config: str = "",
        *,
        session_id_new: str | None = None,
        resume_session_id: str | None = None,
        model: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> list[str]:
        """Build CC TUI startup args for persistent tmux session.

        Exactly one of `session_id_new` or `resume_session_id` must be
        supplied:

        - `session_id_new` — start a fresh session and pin its transcript
          to the given UUID4. Emits `claude --session-id <uuid>`. Use this
          when there is no transcript on disk yet (initial `start_session`
          or `clear_context` respawn with a freshly generated UUID).
        - `resume_session_id` — continue an existing transcript. Emits
          `claude --resume <uuid>`. Use this when the caller has verified
          that `~/.claude/projects/<slug>/<uuid>.jsonl` exists on disk:
          `switch_session` for reply-to-resume; `restore_all` resurrect
          path after bot restart; `start_session` lazy-resume path (caller
          ran `peek_saved_session` first).

        Why two flags: CC 2.1.114 rejects `--session-id X` when X.jsonl
        already exists ("Session ID is already in use") — the process exits
        immediately and the tmux window dies. Earlier code assumed
        `--session-id` worked for both semantics; that assumption was wrong
        and broke `switch_session` and `restore_all` resurrect.

        `--dangerously-skip-permissions` is mandatory for TUI mode so that
        no hardcoded `.claude/` permission dialog can trap the CC process
        behind a confirmation prompt — relevant for agent-team subagents and
        any MCP tool that touches protected paths.
        """
        # Match the guard to the downstream selector: both use `is None`
        # semantics, so an accidental empty-string argument can't slip past
        # the XOR and emit `--session-id ""`.
        if (session_id_new is None) == (resume_session_id is None):
            raise ValueError(
                "build_tmux_startup_args: pass exactly one of "
                "session_id_new (new session) or resume_session_id (existing transcript)"
            )
        mcp_path = mcp_config or str(Path(self._settings.project_root) / ".mcp.bot.json")
        if session_id_new is not None:
            session_flag = ["--session-id", session_id_new]
        else:
            assert resume_session_id is not None  # narrowing for mypy
            session_flag = ["--resume", resume_session_id]
        cmd = [
            "claude",
            *session_flag,
            "--dangerously-skip-permissions",
            "--allowedTools",
            self._mode_tools[mode],
            "--disallowedTools",
            # tmux mode: TeamCreate enabled for agent teams
            "AskUserQuestion,EnterPlanMode",
            "--max-turns",
            str(self._settings.cc_max_turns),
        ]
        if Path(mcp_path).exists():
            cmd.extend(["--mcp-config", mcp_path])
        if model:
            cmd.extend(["--model", model])
        # CC applies --append-system-prompt on new sessions; on --resume it
        # restores the original system prompt from the transcript (which already
        # contains this text from the initial start). Carry the per-context custom
        # prompt (topic.system_prompt or per-chat prompt) so tmux matches subprocess.
        cmd.extend(["--append-system-prompt", self._topic_system_prompt(thread_id, chat_id)])
        return cmd

    def _topic_system_prompt(self, thread_id: int | None, chat_id: int | None = None) -> str:
        """Compose --append-system-prompt: base Telegram prompt + Klava persona +
        the context's own custom prompt (appended, NOT replacing the persona).

        Layers (each added only when non-empty):
          1. base Telegram prompt (always)
          2. default_system_prompt — the Klava persona
          3. custom prompt of this context:
             - topic (thread_id set): topic.system_prompt
             - DM / topicless (thread_id None): per-chat prompt for chat_id

        Applied on a NEW session only; CC restores the original system prompt on
        --resume, so an edited prompt takes effect after /new.
        """
        parts = [_TELEGRAM_SYSTEM_PROMPT]
        if self._topic_config is None:
            return _TELEGRAM_SYSTEM_PROMPT
        persona = self._topic_config.default_system_prompt()
        if persona:
            parts.append(persona)
        if thread_id is not None:
            topic = self._topic_config.get_topic(thread_id)
            # Auto-theme: a named topic gets its name as the conversation
            # subject, so Klava stays on-topic per thread even without a custom
            # system_prompt. Covers all current and future topics from the name
            # alone; a custom prompt (below) refines it further. Free-for-all
            # threads (random/general/etc.) are skipped — forcing a focus there
            # would be wrong.
            if topic.name and topic.name.strip().lower() not in _FREEFORM_TOPIC_NAMES:
                parts.append(t("cc.topic_theme", name=topic.name))
            custom = topic.system_prompt
        else:
            custom = self._topic_config.get_chat_prompt(chat_id)
        if custom:
            parts.append(custom)
        return "\n\n".join(parts)

    def _is_cwd_configured(self, thread_id: int | None) -> bool:
        """True iff the topic doesn't need setup: assistant type (no cwd needed) or cwd is set."""
        if thread_id is None or self._topic_config is None:
            return True  # no topic concept → no setup hint
        topic = self._topic_config.get_topic(thread_id)
        if topic.type == "assistant":
            return True  # assistant topics intentionally have no cwd
        return topic.cwd is not None

    def _get_topic_name(self, thread_id: int | None) -> str | None:
        if thread_id is None or self._topic_config is None:
            return None
        topic = self._topic_config.get_topic(thread_id)
        return topic.name or None

    def _build_tg_context(
        self,
        chat_id: int,
        thread_id: int | None = None,
        cwd_configured: bool = True,
    ) -> str:
        """Build <telegram-context> block with chat_id, thread_id, and optional setup hint.

        When cwd_configured is False (the topic has no cwd in topic_config.json yet),
        an explicit setup instruction is appended so CC always sees it on the first
        message in an unconfigured thread — no reliance on skill auto-discovery.
        """
        if not chat_id:
            return ""
        lines = [f"chat_id: {chat_id}"]
        if thread_id is not None:
            lines.append(f"thread_id: {thread_id}")
        topic_name = self._get_topic_name(thread_id)
        if topic_name:
            lines.append(f"topic: {topic_name}")
        if not cwd_configured:
            lines.extend(
                [
                    "",
                    "This topic is not yet configured (cwd is null in topic_config.json).",
                    "Use the repository `topic-setup` skill/instructions to figure out "
                    "which project to link, verify the path, and update topic_config.json.",
                ]
            )
        lines.append("")
        lines.append(
            "To send files/images to the user, use mcp__bot__send_document or "
            "mcp__bot__send_image. Never say 'file is on disk' — the user "
            "can only see what you send through Telegram."
        )
        return "\n<telegram-context>\n" + "\n".join(lines) + "\n</telegram-context>\n\n"

    async def _run_cc_stream(
        self,
        prompt: str,
        session: SessionData,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> str:
        """Run a CC subprocess, stream events via on_event, return final result."""
        session_id = session.session_id

        if session_id is None and session.process is not None:
            await self._kill_process(session.process)
            session.process = None
            session.session_id = None

        original_mcp_config = session.mcp_config
        runtime_mcp_path: Path | None = None
        try:
            runtime_mcp_path = (
                Path(self.file_cache_dir)
                / "mcp-runtime"
                / f"{session.chat_id}-{session.thread_id}-{time.time_ns()}.json"
            )
            session.mcp_config = ensure_bot_runtime_mcp_config(
                base_mcp_config=original_mcp_config or self.default_mcp_config_path(),
                channel_key=(session.chat_id, session.thread_id),
                runtime_path=runtime_mcp_path,
                project_root=self._settings.project_root,
            )
            exec_cmd = self._build_exec_command(prompt, session)
        except Exception:
            session.mcp_config = original_mcp_config
            if runtime_mcp_path is not None:
                with contextlib.suppress(OSError):
                    runtime_mcp_path.unlink()
            raise
        finally:
            session.mcp_config = original_mcp_config
        cmd = exec_cmd.argv
        cwd = exec_cmd.cwd

        def cleanup_output_last_message() -> None:
            if exec_cmd.output_last_message_path is not None:
                with contextlib.suppress(OSError):
                    exec_cmd.output_last_message_path.unlink()

        def cleanup_runtime_mcp_config() -> None:
            if runtime_mcp_path is not None:
                with contextlib.suppress(OSError):
                    runtime_mcp_path.unlink()

        logger.info(
            "Running agent stream: provider=%s resume=%s, mode=%s, cwd=%s, session_id=%s",
            session.engine,
            session_id is not None,
            session.mode,
            cwd,
            session_id,
        )

        # Stop pressed in the window between send_stream's `cancelled = False`
        # reset and the spawn below. In that window cancel() sees `process is
        # None` and returns without killing, and `cancelled` is otherwise only
        # honored in the error handler — so without this guard the process would
        # still spawn. Abort before spawning; reset so the next run starts clean.
        if session.cancelled:
            session.cancelled = False
            cleanup_runtime_mcp_config()
            cleanup_output_last_message()
            logger.info(
                "CC spawn aborted before start: cancelled for channel (%s, %s)",
                session.chat_id,
                session.thread_id,
            )
            return ""

        for attempt in range(1, 4):
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE if exec_cmd.stdin_text is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    cwd=cwd,
                    limit=10
                    * 1024
                    * 1024,  # 10MB line buffer (CC embeds base64 PDFs in stream JSON)
                )
                if attempt > 1:
                    logger.info(
                        "CC subprocess spawned on attempt %d/3 (pid=%d)", attempt, process.pid
                    )
                break
            except FileNotFoundError:
                if attempt < 3:
                    logger.warning(
                        "Claude Code binary not found (attempt %d/3), retrying in 2s", attempt
                    )
                    await asyncio.sleep(2)
                else:
                    logger.error("Claude Code binary not found after 3 attempts")
                    cleanup_runtime_mcp_config()
                    raise CCNotFoundError from None
            except Exception:
                cleanup_runtime_mcp_config()
                raise

        async with session.process_lock:
            if session.cancelled:
                # Stop was pressed in the spawn window — after the pre-spawn
                # guard passed but before we assigned session.process. cancel()
                # set `cancelled` and saw process is None, so it returned False
                # without killing. Honor it now under the same lock so the turn
                # is not delivered. (M6, audit 2026-07-02.)
                session.cancelled = False
                aborted = True
            else:
                session.process = process
                aborted = False
        if aborted:
            await self._kill_process(process)
            cleanup_output_last_message()
            cleanup_runtime_mcp_config()
            logger.info(
                "CC spawn aborted post-spawn: cancelled during spawn window for (%s, %s)",
                session.chat_id,
                session.thread_id,
            )
            return ""
        session.last_stream_event = time.monotonic()
        if exec_cmd.stdin_text is not None:
            try:
                assert process.stdin is not None
                process.stdin.write(exec_cmd.stdin_text.encode())
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionError):
                await self._kill_process(process)
                async with session.process_lock:
                    if session.process is process:
                        session.process = None
                cleanup_output_last_message()
                cleanup_runtime_mcp_config()
                raise CCProcessError(-1) from None
        result_text = ""
        new_session_id: str | None = None
        force_killed = False
        stderr_buffer: collections.deque[str] = collections.deque(maxlen=256)
        stderr_task: asyncio.Task[None] | None = None

        async def drain_stderr() -> None:
            if process.stderr is None:
                return
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                stderr_buffer.append(chunk.decode(errors="replace"))

        stderr_task = asyncio.create_task(drain_stderr())

        try:
            result_text, new_session_id = await asyncio.wait_for(
                self._read_stream(process, on_event, provider=session.engine, session=session),
                timeout=self._settings.cc_query_timeout_sec,
            )
        except TimeoutError:
            logger.warning("CC stream timed out after %ds", self._settings.cc_query_timeout_sec)
            await self._kill_process(process)
            async with session.process_lock:
                if session.process is process:
                    session.process = None
            if stderr_task is not None:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            cleanup_output_last_message()
            cleanup_runtime_mcp_config()
            raise CCTimeoutError from None
        except Exception:
            # Mirror the TimeoutError branch above: any stream error (e.g. an
            # AttributeError from a malformed CC event) must also kill the
            # subprocess and detach it from the session. Without this the
            # process is orphaned and `session.process` keeps pointing at a
            # dead/hung session, poisoning the next turn. (H2, audit 2026-07-02.)
            await self._kill_process(process)
            async with session.process_lock:
                if session.process is process:
                    session.process = None
            if stderr_task is not None:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            cleanup_output_last_message()
            cleanup_runtime_mcp_config()
            raise

        # Wait for process to finish (with timeout to prevent infinite hang)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._settings.cc_wait_timeout_sec)
        except TimeoutError:
            logger.warning(
                "process.wait() timed out after %ds, killing process",
                self._settings.cc_wait_timeout_sec,
            )
            await self._kill_process(process)
            async with session.process_lock:
                if session.process is process:
                    session.process = None
            force_killed = True

        if stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

        # Always log stderr for diagnostics (skill loading, MCP init, etc.).
        stderr_text = "".join(stderr_buffer)
        if stderr_text:
            logger.info("CC stderr:\n%s", stderr_text[-2000:])

        if exec_cmd.output_last_message_path is not None and not force_killed:
            try:
                file_text = exec_cmd.output_last_message_path.read_text(encoding="utf-8")
            except OSError:
                file_text = ""
            if file_text:
                result_text = file_text
            elif process.returncode == 0:
                logger.warning("Codex output-last-message file empty or missing")
                cleanup_output_last_message()
                cleanup_runtime_mcp_config()
                raise CCProcessError(process.returncode or -1)
        cleanup_output_last_message()
        cleanup_runtime_mcp_config()

        if process.returncode and process.returncode != 0 and not result_text:
            logger.warning(
                "CC stream exited with code %d, stderr: %s",
                process.returncode,
                stderr_text[-500:] or "(empty)",
            )
            raise CCProcessError(process.returncode)

        # Don't update session_id from force-killed process output (may be stale)
        if force_killed:
            session.session_id = None
        elif new_session_id:
            session.session_id = new_session_id

        session.last_activity = time.monotonic()
        async with session.process_lock:
            if session.process is process:
                session.process = None
        logger.info(
            "CC stream done, session_id=%s rc=%s result_len=%d",
            session.session_id,
            process.returncode,
            len(result_text),
        )

        return result_text

    async def _read_stream(
        self,
        process: asyncio.subprocess.Process,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
        provider: str = "claude",
        session: SessionData | None = None,
    ) -> tuple[str, str | None]:
        """Read stream-json lines from process stdout, dispatch events.

        Tracks subagent lifecycle via system events (task_started/task_progress/task_notification).
        Kills process after cc_inactivity_kill_sec of silence (safety net).

        Returns (result_text, session_id).
        """
        result_text = ""
        session_id: str | None = None
        kill_sec = self._settings.cc_inactivity_kill_sec
        throttle_sec = self._settings.cc_agent_progress_throttle_sec
        active_agents: dict[str, str] = {}  # tool_use_id → description
        agent_last_progress: dict[str, float] = {}  # tool_use_id → last progress timestamp

        async def dispatch(event: StreamEvent) -> None:
            ret = on_event(event)
            if asyncio.iscoroutine(ret):
                await ret

        if process.stdout is None:
            raise RuntimeError("stdout pipe not available")

        idle_start: float | None = None

        while True:
            try:
                raw_line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=_POLL_SEC,
                )
            except TimeoutError:
                now = time.monotonic()
                if idle_start is None:
                    idle_start = now
                elapsed = now - idle_start
                if elapsed >= kill_sec:
                    logger.warning("CC inactivity kill after %.0fs of silence", elapsed)
                    await dispatch(StreamEvent("status", t("ui.inactivity_kill")))
                    await self._kill_process(process)
                    raise CCInactivityError(elapsed) from None
                continue

            if not raw_line:
                break

            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue

            if provider == "codex":
                parsed = CODEX_ADAPTER.parse_exec_event(line)
                events = parsed.events
                new_sid = parsed.session_id
                event_type = "codex"
            else:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = data.get("type")
                events, new_sid = parse_cc_event(
                    data, active_agents, agent_last_progress, throttle_sec
                )
            if new_sid:
                session_id = new_sid

            for event in events:
                if event.type == "result":
                    result_text = event.content
                else:
                    await dispatch(event)

            if provider == "codex":
                idle_start = None
                continue

            if event_type in ("system", "assistant", "result", "user", "rate_limit_event"):
                idle_start = None
                if session is not None:
                    session.last_stream_event = time.monotonic()
                continue

            # Unknown event types — don't reset idle timer
            logger.debug("CC unknown event type: %s", event_type)

        return result_text, session_id

    async def send_stream(
        self,
        channel_key: ChannelKey,
        prompt: str,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> str:
        """Send a prompt to CC with streaming events. Returns final response text."""
        session = self._get_session(channel_key)

        async with session.lock:
            thread_id = channel_key[1]
            requested_engine = session.engine
            if self._topic_config is not None and thread_id is not None:
                requested_engine = self._topic_config.get_topic(thread_id).engine
            available_engine = choose_available_engine(requested_engine)
            if available_engine is None:
                logger.warning(
                    "No supported agent CLI found for channel %s; install Claude Code or Codex",
                    channel_key,
                )
                return t("ui.agent_cli_not_found")
            if available_engine != requested_engine:
                logger.warning(
                    "Engine %s unavailable for channel %s; falling back to %s",
                    requested_engine,
                    channel_key,
                    available_engine,
                )
                session.engine = available_engine
                session.model = None
                if self._topic_config is not None and thread_id is not None:
                    update_engine_model = getattr(self._topic_config, "update_engine_model", None)
                    if update_engine_model is not None:
                        ok = await update_engine_model(thread_id, available_engine, None)
                        if not ok:
                            logger.warning(
                                "Failed to persist fallback engine=%s for thread_id=%s",
                                available_engine,
                                thread_id,
                            )
            session.cancelled = False
            last_error: Exception | None = None

            for attempt in range(2):
                try:
                    result = await self._run_cc_stream(prompt, session, on_event)
                    if session.session_id:
                        self._channel_sessions[self._ch_key(channel_key)] = self._session_ref(
                            session.engine,
                            session.session_id,
                            session.model,
                        )
                        self._save_channel_sessions()
                    return result
                except CCNotFoundError:
                    # Misconfiguration — the `claude` binary is missing from
                    # PATH. Used to be silent; log WARNING so `journalctl -p
                    # warning` surfaces it to the operator.
                    logger.warning(
                        "CCNotFoundError: `claude` binary not on PATH for channel %s",
                        channel_key,
                    )
                    return t("ui.cc_not_found")
                except (CCTimeoutError, CCProcessError, CCInactivityError) as exc:
                    last_error = exc
                    if attempt == 0:
                        # User pressed Stop — don't retry, preserve session
                        if session.cancelled:
                            logger.info(
                                "CC cancelled by user, session_id preserved: %s",
                                session.session_id,
                            )
                            session.process = None
                            session.cancelled = False
                            return ""

                        logger.info("Retrying CC stream after error: %s", exc)
                        # Kill old process before retry to prevent zombie processes
                        if session.process is not None:
                            await self._kill_process(session.process)
                        # SIGTERM without cancel — preserve session for retry
                        if isinstance(exc, CCProcessError) and exc.exit_code == 143:
                            logger.info(
                                "SIGTERM, preserving session_id=%s for retry",
                                session.session_id,
                            )
                        else:
                            session.session_id = None
                        session.process = None
                        continue

            logger.error("CC stream failed after retry: %s", last_error)
            session.session_id = None  # Don't resume from failed session
            return t("ui.error_generic")

    async def send(
        self,
        channel_key: ChannelKey,
        prompt: str,
    ) -> str:
        """Send a prompt to CC and return the response text (non-streaming).

        Uses the mode already set on the session (default: "free").

        """
        # Delegate to send_stream with a no-op callback
        return await self.send_stream(channel_key, prompt, lambda _: None)

    def idle_seconds(self, channel_key: ChannelKey) -> float | None:
        """Seconds since last stream event for an active CC process.

        Returns None if no active process for the channel.
        """
        session = self._sessions.get(channel_key)
        if session is None or session.process is None or session.process.returncode is not None:
            return None
        if session.last_stream_event <= 0:
            return None
        return time.monotonic() - session.last_stream_event

    async def cancel(self, channel_key: ChannelKey) -> bool:
        """Cancel a running CC process but preserve session_id for --resume.

        Returns True if a process was killed, False if nothing to cancel.
        """
        if channel_key not in self._sessions:
            return False
        session = self._sessions[channel_key]
        session.cancelled = True
        async with session.process_lock:
            proc = session.process
            if proc is None or proc.returncode is not None:
                return False
            session.process = None
        await self._kill_process(proc)
        logger.info(
            "Cancelled CC process for channel %s, session_id preserved: %s",
            channel_key,
            session.session_id,
        )
        return True

    async def spawn_background_checkpoint(self, channel_key: ChannelKey, prompt: str) -> bool:
        """Fire a detached `--resume` checkpoint on the channel's session.

        Subprocess-mode counterpart of the tmux park-and-checkpoint: before a
        reset wipes the session mapping, resume the *current* session_id once
        with `prompt`, headless. Output is discarded — nothing reaches
        Telegram, and the process self-terminates when CC finishes (bounded by
        `--max-turns`). A watchdog kills it after `cc_query_timeout_sec` so a
        wedged run can't linger. Returns True if a process was spawned.

        Must be called before `kill_session`, which clears `session_id`.
        """
        session = self._sessions.get(channel_key)
        if session is None or not session.session_id:
            logger.info("checkpoint-on-reset: no live session for %s; skipping", channel_key)
            return False

        exec_cmd = self._build_exec_command(prompt, session)
        try:
            process = await asyncio.create_subprocess_exec(
                *exec_cmd.argv,
                stdin=asyncio.subprocess.PIPE
                if exec_cmd.stdin_text is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=exec_cmd.cwd,
                start_new_session=True,
            )
        except OSError:
            logger.warning(
                "checkpoint-on-reset: failed to spawn for %s", channel_key, exc_info=True
            )
            return False

        if exec_cmd.stdin_text is not None and process.stdin is not None:
            with contextlib.suppress(OSError, BrokenPipeError):
                process.stdin.write(exec_cmd.stdin_text.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()

        logger.info(
            "checkpoint-on-reset: spawned background checkpoint for %s (pid=%d, session=%s)",
            channel_key,
            process.pid,
            session.session_id,
        )

        task: asyncio.Task[None] = asyncio.create_task(self._await_checkpoint(process, channel_key))
        self._checkpoint_tasks.add(task)
        task.add_done_callback(self._checkpoint_tasks.discard)
        return True

    async def _await_checkpoint(
        self, process: asyncio.subprocess.Process, channel_key: ChannelKey
    ) -> None:
        """Reap a background checkpoint process, force-killing it on timeout."""
        try:
            await asyncio.wait_for(process.wait(), timeout=self._settings.cc_query_timeout_sec)
            logger.info(
                "checkpoint-on-reset: finished for %s (rc=%s)", channel_key, process.returncode
            )
        except TimeoutError:
            logger.warning(
                "checkpoint-on-reset: timed out for %s after %ss; killing",
                channel_key,
                self._settings.cc_query_timeout_sec,
            )
            await self._kill_process(process)

    async def kill_session(self, channel_key: ChannelKey) -> None:
        """Force-kill a channel's CC session.

        Intended for logical resets (/new, /clear, "Новый чат"): also wipes
        the persistent channel→session mapping so the old session can't be
        resumed on bot restart.
        """
        if channel_key in self._sessions:
            session = self._sessions[channel_key]
            async with session.lock:
                await self._kill_session(session)
        # Remove from persistent mapping so session isn't restored after bot restart
        ch_key = self._ch_key(channel_key)
        self._channel_sessions.pop(ch_key, None)
        self._fresh_channels.add(ch_key)
        self._save_channel_sessions()
        logger.info("Killed session for channel %s", channel_key)

    def consume_fresh_start(self, channel_key: ChannelKey) -> bool:
        """Return True and clear flag if channel was just reset (reset).

        Used by process_queue_item to skip reply-to-resume on the first message
        after a user-initiated reset, preventing Mac Telegram's sticky reply mode
        from accidentally resuming the old session.
        """
        ch_key = self._ch_key(channel_key)
        if ch_key in self._fresh_channels:
            self._fresh_channels.discard(ch_key)
            # Persist the consumption so the flag doesn't resurrect after restart
            self._save_channel_sessions()
            return True
        return False

    def get_mode(self, channel_key: ChannelKey) -> Mode:
        """Get the current mode for a channel."""
        if channel_key not in self._sessions:
            return "free"
        return self._sessions[channel_key].mode

    async def _cleanup_expired_sessions(self) -> None:
        """Run one cleanup pass: kill zombie processes and remove idle session entries."""
        for channel_key, session in list(self._sessions.items()):
            try:
                if session.process is not None and session.process.returncode is not None:
                    async with session.lock:
                        if session.process is not None and session.process.returncode is not None:
                            logger.info("Cleaning up zombie process for channel %s", channel_key)
                            session.process = None
                # Remove idle session data (no process, inactive beyond threshold)
                elif (
                    session.process is None
                    and not session.lock.locked()
                    and session.last_activity > 0
                    and (time.monotonic() - session.last_activity)
                    > self._settings.session_timeout_sec
                ):
                    logger.info("Removing idle session for channel %s", channel_key)
                    del self._sessions[channel_key]
            except Exception:
                logger.exception("Error during cleanup of session for channel %s", channel_key)

    async def _cleanup_loop(self) -> None:
        """Background task: periodically kill expired sessions."""
        while True:
            await asyncio.sleep(self._settings.session_cleanup_interval_sec)
            await self._cleanup_expired_sessions()

    def start_cleanup(self) -> None:
        """Start the background cleanup loop."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Session cleanup task started")

    async def override_session(self, channel_key: ChannelKey, session_id: str) -> None:
        """Override session_id for a channel (used by reply-to-resume)."""
        session = self._get_session(channel_key)
        async with session.lock:
            session.session_id = session_id
        logger.info("Override session for channel %s: session_id=%s", channel_key, session_id)

    async def persist_session_id(
        self,
        channel_key: ChannelKey,
        session_id: str,
        *,
        provider: str = "claude",
        model: str | None = None,
    ) -> None:
        """Set AND persist a channel's session_id (resume across bot restart).

        Public helper so StreamingManager (and other channels) don't reach into
        `_channel_sessions`/`_save_channel_sessions` directly. Mirrors what the
        one-shot path does inline after a successful run.
        """
        await self.override_session(channel_key, session_id)
        self._channel_sessions[self._ch_key(channel_key)] = self._session_ref(
            provider, session_id, model
        )
        self._save_channel_sessions()

    def get_current_session_id(self, channel_key: ChannelKey) -> str | None:
        """Get the current session_id for a channel, or None if no session."""
        if channel_key not in self._sessions:
            return None
        return self._sessions[channel_key].session_id

    async def clear_provider_session(
        self, channel_key: ChannelKey, *, mark_fresh: bool = True
    ) -> None:
        """Clear in-memory and persisted subprocess session for engine/model changes."""
        session = self._get_session(channel_key)
        async with session.lock:
            if session.process is not None:
                await self._kill_process(session.process)
                session.process = None
            session.session_id = None
            session.cancelled = False
        ch_key = self._ch_key(channel_key)
        self._channel_sessions.pop(ch_key, None)
        if mark_fresh:
            self._fresh_channels.add(ch_key)
        self._save_channel_sessions()

    # --- Message → session_id mapping (reply-to-resume) ---

    def record_message(
        self,
        message_id: int,
        session_id: str,
        channel_key: ChannelKey | None = None,
        *,
        provider: str | None = None,
        model: str | None = None,
        exec_mode: str | None = None,
    ) -> None:
        """Record a message_id → (session_id, channel_key) mapping for reply-to-resume.

        Callers in the tmux flow MUST pass ``provider`` (and ``model`` when set)
        explicitly — the live tmux state is the only authoritative source for
        which engine produced the answer. Without an explicit provider, the
        fallback uses ``_get_session`` so ``_apply_topic_config`` runs and the
        engine reflects the current ``topic_config.json`` rather than a stale
        ``_sessions[key].engine`` left over from a reply-driven engine switch.

        New writes always use typed dict so reply resolution is unambiguous.
        Legacy plain-string entries already on disk (29 of them in production
        before this fix) are NOT rewritten by this call — the read path in
        ``resolve_reply_reference`` infers their provider via UUID version
        and rewrites them to typed dict on first resolve.
        """
        ch_str = f"{channel_key[0]}:{channel_key[1]}" if channel_key else ""
        if provider is None:
            # No explicit provider: tmux callers always pass one, so this
            # branch is the subprocess / non-tmux path. _get_session triggers
            # _apply_topic_config so engine reflects the current topic config
            # rather than a stale cached value. We only call _get_session
            # when a session already exists for the channel — otherwise
            # `record_message` would silently materialize session state as a
            # side effect of a recording op. In practice record_message is
            # always called after a successful stream, so the session exists.
            if channel_key is not None and channel_key in self._sessions:
                session = self._get_session(channel_key)
                resolved_provider = session.engine
                # Fallback inherits the model from topic config; explicit-provider
                # branch (below) trusts the caller verbatim because tmux owns its
                # own model and any mismatch with topic config is intentional.
                resolved_model = session.model if model is None else model
            else:
                resolved_provider = "claude"
                resolved_model = model
        else:
            resolved_provider = provider
            resolved_model = model
        resolved_exec_mode = exec_mode if exec_mode in {"subprocess", "tmux", "streaming"} else None
        if (
            resolved_exec_mode is None
            and channel_key is not None
            and self._topic_config is not None
        ):
            try:
                topic_settings = self._topic_config.get_topic(channel_key[1])
                resolved_exec_mode = topic_settings.exec_mode
            except Exception:
                logger.debug("record_message: failed to resolve exec_mode", exc_info=True)
        self._msg_sessions[message_id] = {
            "provider": resolved_provider,
            "session_id": session_id,
            "channel_key": ch_str,
            "model": resolved_model,
            "exec_mode": resolved_exec_mode,
        }
        # Evict oldest entries when exceeding max size
        max_size = self._settings.session_mapping_max_size
        while len(self._msg_sessions) > max_size:
            self._msg_sessions.popitem(last=False)

    def resolve_reply_reference(
        self, message_id: int, channel_key: ChannelKey | None = None
    ) -> ReplySessionRef | None:
        """Look up provider-aware reply target for a message_id.

        Cross-channel replies are ignored to prevent session contamination between topics.
        Cross-provider replies are allowed: the caller can switch topic engine before resume.
        """
        value = self._msg_sessions.get(message_id)
        if value is None:
            return None

        if isinstance(value, dict):
            session_id = value.get("session_id")
            ch_str = value.get("channel_key")
            provider = value.get("provider", "claude")
            model = _valid_model_override(value.get("model"))
            raw_exec_mode = value.get("exec_mode")
            exec_mode = (
                raw_exec_mode if raw_exec_mode in {"subprocess", "tmux", "streaming"} else None
            )
            if not isinstance(session_id, str):
                return None
            if isinstance(ch_str, str) and ch_str and channel_key is not None:
                expected = f"{channel_key[0]}:{channel_key[1]}"
                if ch_str != expected:
                    return None
            return ReplySessionRef(
                session_id=session_id,
                provider=provider if isinstance(provider, str) else "claude",
                model=model,
                exec_mode=exec_mode,
            )

        # Parse stored value: "session_id|chat_id:thread_id" or legacy "session_id".
        # Lazy upgrade: rewrite the entry as typed dict so the next save_mapping
        # drains it from the on-disk legacy pool. Cross-channel guard runs before
        # the rewrite so we never persist an entry under the wrong owner.
        if isinstance(value, str) and "|" in value:
            session_id, ch_str = value.split("|", 1)
            if ch_str and channel_key is not None:
                # Validate channel match
                expected = f"{channel_key[0]}:{channel_key[1]}"
                if ch_str != expected:
                    logger.debug(
                        "Ignoring cross-channel reply: msg %d from %s, current %s",
                        message_id,
                        ch_str,
                        expected,
                    )
                    return None
            inferred = _provider_from_session_id(session_id)
            self._msg_sessions[message_id] = {
                "provider": inferred,
                "session_id": session_id,
                "channel_key": ch_str,
                "model": None,
                "exec_mode": None,
            }
            return ReplySessionRef(session_id=session_id, provider=inferred)
        elif isinstance(value, str):
            # Legacy format: plain session_id without channel info
            inferred = _provider_from_session_id(value)
            self._msg_sessions[message_id] = {
                "provider": inferred,
                "session_id": value,
                "channel_key": "",
                "model": None,
                "exec_mode": None,
            }
            return ReplySessionRef(session_id=value, provider=inferred)
        return None

    def resolve_reply_session(
        self, message_id: int, channel_key: ChannelKey | None = None
    ) -> str | None:
        """Look up session_id for a message_id. Returns None if not found or wrong channel."""
        ref = self.resolve_reply_reference(message_id, channel_key)
        if ref is None:
            return None
        if channel_key is not None:
            current = self._get_session(channel_key)
            if ref.provider != current.engine:
                logger.info(
                    "Ignoring cross-provider reply in session-only resolver: "
                    "msg %d from %s, current %s",
                    message_id,
                    ref.provider,
                    current.engine,
                )
                return None
        return ref.session_id

    def reply_requires_provider_switch(self, message_id: int, channel_key: ChannelKey) -> bool:
        """Return True when a reply target cannot be handled by the current provider/model."""
        ref = self.resolve_reply_reference(message_id, channel_key)
        if ref is None:
            return False
        current = self._get_session(channel_key)
        return ref.provider != current.engine or ref.model != current.model

    def is_cross_provider_reply(self, message_id: int, channel_key: ChannelKey) -> bool:
        """True when message_id resolves in this channel but belongs to another provider."""
        ref = self.resolve_reply_reference(message_id, channel_key)
        if ref is None:
            return False
        current = self._get_session(channel_key)
        return ref.provider != current.engine

    def load_mapping(self) -> None:
        """Load message→session mapping from JSON file.

        Backwards compatible: old dict format {"s": sid, "p": project} converted to string.
        """
        # NB: no early return if the message-mapping file is missing — the
        # channel→session mapping below must still load (latent bug until 2026-06).
        try:
            data = (
                json.loads(self._mapping_path.read_text()) if self._mapping_path.exists() else None
            )
            if isinstance(data, dict):
                migrated = 0
                for k, v in data.items():
                    if isinstance(v, str):
                        self._msg_sessions[int(k)] = v
                    elif isinstance(v, dict) and "session_id" in v:
                        self._msg_sessions[int(k)] = {
                            "provider": str(v.get("provider", "claude")),
                            "session_id": str(v["session_id"]),
                            "channel_key": str(v.get("channel_key", "")),
                            "model": v.get("model") if isinstance(v.get("model"), str) else None,
                        }
                    elif isinstance(v, dict) and "s" in v:
                        # Old dict format: extract session_id
                        self._msg_sessions[int(k)] = str(v["s"])
                        migrated += 1
                    else:
                        logger.debug("Skipping invalid mapping entry: %s -> %s", k, v)
                logger.info(
                    "Loaded %d message→session mappings (%d migrated from old format)",
                    len(self._msg_sessions),
                    migrated,
                )
        except (json.JSONDecodeError, ValueError, OSError):
            logger.warning("Failed to load session mapping from %s", self._mapping_path)

        # Trim to max_size after loading
        max_size = self._settings.session_mapping_max_size
        while len(self._msg_sessions) > max_size:
            self._msg_sessions.popitem(last=False)

        # Load channel→session mapping for post-restart resume
        if self._channel_sessions_path.exists():
            try:
                data = json.loads(self._channel_sessions_path.read_text())
                if isinstance(data, dict):
                    fresh = data.pop("__fresh__", None)
                    if isinstance(fresh, list):
                        self._fresh_channels = {x for x in fresh if isinstance(x, str)}
                    self._channel_sessions = {}
                    for k, v in data.items():
                        if not isinstance(k, str):
                            continue
                        if isinstance(v, str):
                            self._channel_sessions[k] = v
                        elif isinstance(v, dict) and isinstance(v.get("session_id"), str):
                            self._channel_sessions[k] = {
                                "provider": str(v.get("provider", "claude")),
                                "session_id": str(v["session_id"]),
                                "model": (
                                    v.get("model") if isinstance(v.get("model"), str) else None
                                ),
                            }
                    logger.info("Loaded %d channel→session mappings", len(self._channel_sessions))
            except (json.JSONDecodeError, OSError):
                logger.warning(
                    "Failed to load channel sessions from %s", self._channel_sessions_path
                )

    def _save_channel_sessions(self) -> None:
        """Write channel→session mapping to disk (called after each stream and on shutdown).

        Fresh-start flags ride along under the reserved "__fresh__" key so a
        user-initiated reset survives a bot restart (older readers skip the
        list value silently).
        """
        try:
            payload: dict[str, object] = dict(self._channel_sessions)
            if self._fresh_channels:
                payload["__fresh__"] = sorted(self._fresh_channels)
            self._atomic_write_json(self._channel_sessions_path, payload)
        except OSError:
            logger.warning(
                "Failed to save channel sessions to %s",
                self._channel_sessions_path,
                exc_info=True,
            )

    def save_mapping(self) -> None:
        """Save message→session and channel→session mappings to JSON files."""
        try:
            data = {str(k): v for k, v in self._msg_sessions.items()}
            self._atomic_write_json(self._mapping_path, data)
            logger.info("Saved %d message→session mappings", len(data))
        except OSError:
            logger.warning(
                "Failed to save session mapping to %s", self._mapping_path, exc_info=True
            )
        # Also persist in-memory session_ids from active sessions
        for channel_key, session in self._sessions.items():
            if session.session_id:
                self._channel_sessions[self._ch_key(channel_key)] = self._session_ref(
                    session.engine,
                    session.session_id,
                    session.model,
                )
        self._save_channel_sessions()

    async def _shutdown_sessions(self) -> None:
        """Kill all active sessions. Called during shutdown with timeout protection.

        Acquires session.lock for each session to avoid concurrent modification.
        """
        for channel_key, session in list(self._sessions.items()):
            if session.process is not None:
                logger.info("Shutting down session for channel %s", channel_key)
                try:
                    async with asyncio.timeout(5):
                        async with session.lock:
                            await self._kill_session(session)
                except TimeoutError:
                    logger.warning(
                        "Lock timeout during shutdown for channel %s, force-killing",
                        channel_key,
                    )
                    await self._kill_session(session)

    async def shutdown(self) -> None:
        """Kill all active sessions and stop cleanup with timeout protection."""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

        try:
            await asyncio.wait_for(
                self._shutdown_sessions(),
                timeout=self._settings.shutdown_timeout_sec,
            )
        except TimeoutError:
            # Count sessions that weren't cleaned up
            remaining = sum(1 for s in self._sessions.values() if s.process is not None)
            logger.warning(
                "Shutdown timeout after %ds, %d session(s) not cleaned up",
                self._settings.shutdown_timeout_sec,
                remaining,
            )

        self._sessions.clear()
        logger.info("All sessions shut down")


class CCNotFoundError(Exception):
    pass


class CCTimeoutError(Exception):
    pass


class CCProcessError(Exception):
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        super().__init__(f"CC process exited with code {exit_code}")


class CCInactivityError(Exception):
    def __init__(self, idle_seconds: float) -> None:
        self.idle_seconds = idle_seconds
        super().__init__(f"CC process inactive for {idle_seconds:.0f}s")
