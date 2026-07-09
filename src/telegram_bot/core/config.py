"""Core configuration loading from environment variables.

Core settings cover generic bot functionality: token, auth, Claude Code,
voice transcription, sessions, tmux, and topics. Downstream projects can layer
their own settings on top of these generic core settings.
"""

import functools
import logging

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Voice message size cap enforced at every ingestion point (incoming
# voice, forwarded voice, media-content router). Telegram itself caps
# voice files, but an up-front byte check avoids a pointless download
# when a relay or future bot API change lets a large payload through.
MAX_VOICE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB

_VALID_ATTRIBUTE_SENDERS = {"auto", "always", "never"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    allowed_user_ids: list[int] = []
    # Reply sent to users who are NOT in allowed_user_ids. Empty string (default)
    # keeps the silent-ignore behaviour. Set via UNAUTHORIZED_REPLY in .env to
    # let the bot answer strangers with a canned line instead of staying mute.
    unauthorized_reply: str = ""
    bot_lang: str = "en"
    # Sender attribution: prefix each prompt with "[Message from: <name>]" so
    # the engine can tell whitelisted people apart in a shared bot/topic.
    #   auto   — attribute only when allowed_user_ids has >1 user (default).
    #            A single-user bot stays unprefixed (prompts unchanged).
    #   always — attribute every message regardless of whitelist size.
    #   never  — never attribute.
    # Set via ATTRIBUTE_SENDERS in .env. A per-topic `attribute_senders` in
    # topic_config.json overrides this.
    attribute_senders: str = "auto"
    # Optional. Workspace/vault directory used by skills that save files
    # (e.g. deep-research, scraping). Empty (default) — those skills just have
    # no save target; everything else works. If set, preflight verifies the
    # path exists and is writable at startup. Set via VAULT env var.
    vault: str = ""
    # Directory where handlers download media before forwarding to CC.
    # Core-owned generic bot feature. Override via `FILE_CACHE_DIR` in .env.
    file_cache_dir: str = "/tmp/telegram-bot-cache"
    project_root: str = "."
    default_cwd: str = "."
    session_timeout_sec: int = 86400
    session_cleanup_interval_sec: int = 300
    cc_query_timeout_sec: int = 600
    deepgram_api_key: str = ""
    fluidaudio_cli_path: str = ""
    fluidaudio_model_dir: str = ""
    yandex_folder_id: str = ""
    yandex_sa_key_json: str = ""
    cc_wait_timeout_sec: int = 10
    cc_inactivity_kill_sec: float = 180
    cc_preempt_idle_sec: float = 60
    cc_agent_progress_throttle_sec: float = 10
    cc_max_turns: int = 100
    session_mapping_path: str = "./session_mapping.json"
    session_mapping_max_size: int = 5000  # each interaction records multiple response chunks
    shutdown_timeout_sec: int = 7  # Gives the service manager time to stop cleanly.
    topic_config_path: str = "./topic_config.json"
    notification_chat_id: int | None = None
    tmux_sessions_dir: str = "./tmux_sessions"
    usage_pin_enabled: bool = True
    usage_pin_update_interval_sec: int = 30
    # Auto-checkpoint on session reset. When True, /new, /clear, or the
    # "Новый чат" button first asks the engine to write a background checkpoint
    # of the session before its context is dropped — so a forgotten manual
    # checkpoint never loses the work. Set via CHECKPOINT_ON_RESET in .env. A
    # per-topic `checkpoint_on_reset` in topic_config.json overrides this.
    # checkpoint_prompt is sent verbatim to the engine, so it may be a slash
    # command ("/чекпоинт") or plain instruction; empty uses the built-in text.
    checkpoint_on_reset: bool = False
    checkpoint_prompt: str = ""
    startup_batch_window_sec: float = 3.0
    # Cooldown between canned replies to the same unauthorized user (seconds).
    unauthorized_reply_cooldown_sec: float = 600.0
    # Dead-man heartbeat receiver (e.g. a healthchecks.io ping URL). Empty —
    # heartbeat loop idles. Secret travels in the X-Heartbeat-Secret header.
    heartbeat_url: str = ""
    heartbeat_secret: str = ""
    # Rotating log file. Empty (default) keeps plain stdout logging; set via
    # LOG_FILE in .env to write to a self-rotating file instead (10 MB x 3).
    log_file: str = ""

    @field_validator("attribute_senders", mode="after")
    @classmethod
    def _validate_attribute_senders(cls, value: str) -> str:
        """Warn-and-fall-back on a bad ATTRIBUTE_SENDERS, matching topic_config.

        The bot tolerates invalid config rather than failing startup (same as
        an unknown per-topic value or engine), so a typo logs a warning and
        degrades to the safe default instead of crashing the bot.
        """
        if value not in _VALID_ATTRIBUTE_SENDERS:
            logger.warning("Invalid ATTRIBUTE_SENDERS %r, falling back to 'auto'", value)
            return "auto"
        return value


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
