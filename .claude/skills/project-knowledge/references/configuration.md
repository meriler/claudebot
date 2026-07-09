# Configuration

Environment is loaded by `telegram_bot.core.config.Settings`.

Required:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_IDS`

Common optional settings:

- `BOT_LANG`: `en` or `ru`.
- `DEFAULT_CWD`: default working directory for new topics; public default is `.`.
- `FILE_CACHE_DIR`: downloaded media cache.
- `TOPIC_CONFIG_PATH`: defaults to `./topic_config.json`.
- `TMUX_SESSIONS_DIR`: defaults to `./tmux_sessions`.
- `DEEPGRAM_API_KEY`: enables voice transcription (`ffmpeg` strongly
  recommended alongside it).
- `VAULT`: optional workspace/vault directory for file-saving agent skills.

`topic_config.example.json` is public-safe and can be copied to
`topic_config.json`. The real `topic_config.json` is runtime config and must not
be committed.

Topic fields:

- `name`: human-readable topic label.
- `type`: `assistant` or `project`.
- `mode`: public prompt mode. `free` is the standard project/general prompt.
  `task` is a replaceable example of a second prompt mode.
- `cwd`: absolute project path or `null` for `DEFAULT_CWD`.
- `mcp_config`: absolute MCP config path or `null` for bot-generated config.
- `stream_mode`: `live+` (default), `live`, `verbose`, or `minimal`.
- `exec_mode`: `streaming` (default), `subprocess`, or `tmux`.
- `engine`: `claude` or `codex`.
- `model`: optional model override.

Claude Code CLI 2.1.130+ (installed, authenticated, onboarded) is always
required by the startup preflight, even for Codex-only topic setups. Codex CLI
is optional. Runtime prefers Claude Code when both engines are available.

Voice transcription requires a Deepgram API key in `DEEPGRAM_API_KEY`; leave it
empty to disable voice messages.

Never commit `.env`, `.mcp*.json`, session JSON files, `tmux_sessions/`,
`data/`, virtual environments, Python caches, or test/lint/typecheck caches.

For end-user installation and service setup, use `bot-setup`. For forum topic
creation and project wiring, use `topic-setup`. Both skills must exist in
`.claude/skills/` and `.codex/skills/`.
