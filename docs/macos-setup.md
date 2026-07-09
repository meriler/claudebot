# Claudebot — настройка для macOS

Пошаговая инструкция по запуску бота на маке (например, на постоянно включённом
Mac mini). Основная документация и Linux/VPS-путь — в [README](../README.md) и
[README.ru](../README.ru.md); здесь только macOS-специфика.

Бот превращает Telegram в удалённый интерфейс к Claude Code: ты пишешь в
Telegram, бот запускает Claude Code в рабочей папке, ответ и прогресс приходят
обратно в чат (в том числе с телефона, голосом).

## Требования

- macOS с Claude Code CLI 2.1.130+ (`claude --version`), залогиненным
  (подписка или API key). Запусти `claude` интерактивно один раз и выйди через
  `/exit` — стартовый preflight требует пройденный онбординг.
- Python 3.12+ и `uv` (`brew install uv`)
- `tmux` (`brew install tmux`)
- `ffmpeg` (`brew install ffmpeg`) — настоятельно рекомендуется для голосовых
  сообщений
- Telegram-бот от @BotFather

## Шаг 1: Клонирование

```bash
git clone https://github.com/meriler/claudebot.git
cd claudebot
uv sync
```

## Шаг 2: Создание бота в Telegram

1. Открой @BotFather → `/newbot` → дай имя
2. Скопируй токен
3. `/setprivacy` → выбери бота → `Disable` (чтобы бот видел все сообщения в группах)

## Шаг 3: .env

```bash
cp .env.example .env
chmod 600 .env
```

Отредактируй `.env`:

```env
TELEGRAM_BOT_TOKEN=твой-токен-от-botfather
ALLOWED_USER_IDS=[123456789]
BOT_LANG=ru
DEEPGRAM_API_KEY=ключ-от-deepgram-если-нужны-голосовые
PROJECT_ROOT=/абсолютный/путь/к/claudebot
DEFAULT_CWD=/абсолютный/путь/к/рабочей-папке
FILE_CACHE_DIR=/абсолютный/путь/к/claudebot/data
TOPIC_CONFIG_PATH=./topic_config.json
TMUX_SESSIONS_DIR=./tmux_sessions
CC_MAX_TURNS=100
CC_INACTIVITY_KILL_SEC=28800
VAULT=/абсолютный/путь/к/vault-если-есть
```

**ВАЖНО:**
- `PROJECT_ROOT` — абсолютный путь к папке бота (не `.`)
- `DEFAULT_CWD` — абсолютный путь к папке, из которой Claude будет работать
- `FILE_CACHE_DIR` — абсолютный путь (иначе фото не доходят до Claude)
- `ALLOWED_USER_IDS` — JSON-массив. Узнать свой ID: @userinfobot в Telegram
- `VAULT` — опционально: рабочая папка (например, Obsidian-vault), куда
  агентские скиллы складывают файлы. Пусто — просто не используется.

## Шаг 4: Telegram-группа с топиками

1. Создай группу в Telegram
2. Настройки группы → включи "Темы" (Topics)
3. Добавь бота в группу, сделай админом
4. Создай топики (каждый = отдельный рабочий контекст)
5. Напиши по сообщению в каждый топик — бот авторегистрирует их

## Шаг 5: topic_config.json

После авторегистрации отредактируй `topic_config.json`:

```json
{
  "topics": {
    "2": {
      "name": "Работа",
      "type": "project",
      "mode": "free",
      "cwd": "/абсолютный/путь/к/рабочей-папке",
      "mcp_config": null,
      "stream_mode": "live+",
      "exec_mode": "streaming",
      "engine": "claude",
      "model": null
    }
  }
}
```

- `exec_mode`: `"streaming"` (дефолт, рекомендуется) — постоянный headless-агент
  с живым прогрессом; `"tmux"` — постоянная терминальная сессия (когда нужен
  `/tui` и интерактивные диалоги); `"subprocess"` — разовые запуски
- `stream_mode`: `"live+"` (дефолт) — прогресс плюс промежуточные ответы
  агента 💬-блоками; ещё есть `"live"`, `"verbose"`, `"minimal"`
- `cwd` — абсолютный путь к папке проекта

## Шаг 6: Автозапуск (LaunchAgent)

Создай `~/Library/LaunchAgents/com.USER.claudebot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.USER.claudebot</string>
    <key>WorkingDirectory</key>
    <string>/путь/к/claudebot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/uv</string>
        <string>run</string>
        <string>python</string>
        <string>-m</string>
        <string>telegram_bot</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/путь/к/claudebot/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/путь/к/claudebot/logs/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/USER/.local/bin</string>
        <key>HOME</key>
        <string>/Users/USER</string>
    </dict>
</dict>
</plist>
```

```bash
mkdir -p logs
launchctl load ~/Library/LaunchAgents/com.USER.claudebot.plist
```

## Известные проблемы и фиксы

Первый шаг диагностики любой проблемы — команда `/health` в чате с ботом: она
прогоняет self-check (CLI на месте, онбординг пройден, пути записываемы) и
показывает, что именно не так.

### 1. Относительные пути ломают MCP config

**Симптом:** `CC TUI start timeout` — Claude крашится при старте.

**Причина:** `PROJECT_ROOT=.` в `.env` → `mcp.runtime.json` содержит относительные пути → Claude (работающий из другого `cwd`) не находит MCP сервер.

**Фикс:** Все пути в `.env` должны быть абсолютными: `PROJECT_ROOT`, `DEFAULT_CWD`, `FILE_CACHE_DIR`.

### 2. Старые session_id от subprocess мешают tmux

**Симптом:** `CC TUI start timeout` после переключения с subprocess на tmux mode.

**Причина:** `channel_sessions.json` хранит session_id от subprocess-режима. Бот пытается `--resume` их в tmux.

**Фикс:** Очистить `channel_sessions.json` и `session_mapping.json`:
```bash
echo '{}' > channel_sessions.json
echo '{}' > session_mapping.json
```

### 3. Конфликт с официальным Telegram-плагином Антропика

**Симптом:** `TelegramConflictError: terminated by other getUpdates request`.

**Причина:** Два polling-клиента на один токен.

**Фикс:** В Claude Code: `/plugin uninstall telegram@claude-plugins-official`.

## Управление

```bash
# Запустить
launchctl load ~/Library/LaunchAgents/com.USER.claudebot.plist

# Остановить
launchctl unload ~/Library/LaunchAgents/com.USER.claudebot.plist

# Логи
tail -f logs/stdout.log

# tmux-сессии
tmux ls | grep cc-

# Подключиться к сессии (смотреть, но не трогать!)
tmux attach -t cc-XXXXX-N
```

## Telegram-команды

Полный список — в README (раздел Commands). Самое ходовое:

- `/clear` — новая сессия
- `/kill` — убить tmux-сессию
- `/tui` — снапшот терминала + кнопки управления
- `/cancel` — отменить текущий запрос
- `/stream` — переключить live+/live/verbose/minimal
- `/mode` — переключить streaming/tmux/subprocess
- `/health` — самодиагностика бота
