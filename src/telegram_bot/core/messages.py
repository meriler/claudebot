"""Bot message localization — translate strings by key based on settings.bot_lang.

Two languages: "en" (default) and "ru". Keys group into:
- ui.*    — strings shown to the user (status, errors, button text)
- cc.*    — content prefixes injected into prompts for Claude Code
            (forwarded media labels, voice transcription markers, etc.)
- tool.*  — tool-status messages shown while CC executes a tool

Use `t("ui.thinking")` for static strings or `t("ui.tmux_failed", exc=err)`
for templates with placeholders. Missing keys fall back to English, then
to the key itself if not found in either language.

When the message file changes between languages on bot restart, the
lru_cache around _get_lang() keeps a single value for the process lifetime.
"""

from __future__ import annotations

import functools
from typing import Any

_DEFAULT_LANG = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # --- UI: user-facing status / error messages -------------------
        "ui.start_welcome": (
            "Hi! I'll forward your messages to Claude Code. Just send me anything."
        ),
        "ui.thinking": "⏳ Thinking...",
        "ui.recognizing_voice": "⏳ Transcribing voice...",
        "ui.voice_recognition_failed": (
            "❌ Could not transcribe the voice message — please try again or type it."
        ),
        "ui.processing_forwards": "⏳ Processing forwarded messages...",
        "ui.processing_files": "⏳ Processing files...",
        "ui.new_session": "New session",
        "ui.context_cleared": "🧹 Context cleared, tmux session alive",
        "ui.reset_failed": "❌ Couldn't reset the context. Try again in a moment.",
        "ui.topic_welcome": (
            "👋 Topic ready. Tell me what it's for — which project to run in "
            "(path or name), and whether you want a custom prompt for this topic."
        ),
        "ui.cancelled": "❌ Cancelled",
        "ui.cancelled_queue_pending": "❌ Cancelled. Processing {count} queued message(s)…",
        "ui.nothing_to_cancel": "Nothing to cancel",
        "ui.language_current": "Language: <b>{lang}</b>. Use /language ru or /language en.",
        "ui.language_changed": "Language switched to <b>{lang}</b>.",
        "ui.language_invalid": "Unknown language. Use /language ru or /language en.",
        "ui.already_finished": "Process already finished",
        "ui.tmux_failed": "❌ Failed to start tmux: {exc}",
        "ui.tmux_killed": "🗑 Tmux session killed",
        "ui.tmux_not_active": "No active tmux session in this topic",
        "ui.engine_starting": (
            "🔄 {engine} is starting up — please wait a few seconds before sending."
        ),
        "ui.engine_ready": "✅ {engine} is ready.",
        "ui.engine_start_failed": "⚠️ {engine} failed to start: {exc}",
        # --- UI: tail / tui feature strings (Wave 3 tmux-tui-mode) ----
        "ui.tail_unavailable": (
            "⚠️ No active tmux session — /tui is unavailable."
            " Switch to tmux mode or start the session first."
        ),
        "ui.tail_snapshot_header": "TUI snapshot (last lines):",
        "ui.tail_keyboard_stale": ("Keyboard is outdated — session restarted. Call /tui again."),
        "ui.modal_blocked_header": (
            "⚠️ Message NOT sent — CC is waiting on a modal dialog.\n"
            "Your message: <code>{prompt}</code>\n"
            "Dismiss the modal (Esc / pick an option), then resend."
        ),
        "ui.modal_idle_detected": (
            "⚠️ CC is waiting on a modal dialog.\n"
            "Dismiss it (Esc / pick an option) so CC can continue."
        ),
        "ui.tui_start_timeout": (
            "❌ Claude Code TUI didn't become ready in 30 s — tmux killed."
            " Try again or /mode → regular."
        ),
        "ui.tui_session_missing": (
            "Session unavailable (created before migration) — keep writing to the current session."
        ),
        # --- UI: exec-mode picker -------------------------------------
        "ui.exec_mode_picker_caption": (
            "Execution mode: <b>{current}</b>\n\n"
            "⚡ regular — each message spawns a fresh Claude subprocess with"
            " no context from previous messages. Task runs, session dies, no"
            " resource footprint. Good for one-off tasks, notes, etc.\n\n"
            "🌊 streaming — a live Claude process kept alive across turns. A"
            " message sent while it works is picked up between tool calls (like"
            " typing in the terminal while Claude thinks). No tmux/popup"
            " friction. /kill closes the live process.\n\n"
            "🖥 tmux — persistent tmux session that keeps running and"
            " remembers context across messages. Full Claude Code with agent"
            " team support, but constantly consumes resources. Better for"
            " development and big tasks. First start takes ~1-2s. To kill the"
            " session and free resources — use /kill."
        ),
        "ui.exec_mode_changed": "Mode: {mode}",
        "ui.exec_mode_already": "Already: {mode}",
        "ui.exec_mode_busy": "Processing in progress, hit /cancel first",
        "ui.exec_mode_invalid": "Invalid mode",
        "ui.exec_mode_not_in_forum": "⚠️ /mode works only in forum topics",
        "ui.exec_mode_write_failed": "Failed to save mode, try again",
        "ui.exec_mode_label_subprocess": "⚡ regular",
        "ui.exec_mode_label_streaming": "🌊 streaming",
        "ui.exec_mode_label_tmux": "🖥 tmux",
        "ui.engine_picker_caption": (
            "Engine: <b>{engine}</b>\n\n"
            "This does not change /mode: regular and tmux remain transport settings."
        ),
        "ui.engine_changed": "Engine: {engine}",
        "ui.engine_changed_new_session": (
            "Engine: {engine}.\n\nActive session was reset. The next message"
            " will start a new session with {engine}."
        ),
        "ui.engine_already": "Already selected",
        "ui.engine_invalid": "Invalid engine",
        "ui.engine_not_in_forum": "⚠️ /engine works only in forum topics",
        "ui.engine_write_failed": "Failed to save engine settings",
        "ui.tmux_started_engine": "🖥 New tmux session started with {engine}",
        "ui.reply_engine_switched": (
            "↪️ Reply target belongs to {engine}; switching this topic before resume."
        ),
        "ui.stream_mode_picker_caption": (
            "Stream mode for this topic: <b>{current}</b>\n\n"
            "• <b>verbose</b> — each event as its own message\n"
            "• <b>live</b> — one editable buffer with progress updates\n"
            "• <b>minimal</b> — only final answers\n"
            "• <b>live+</b> — live buffer + intermediate 💬 comments; 🧠 adds reasoning"
        ),
        "ui.stream_mode_changed": "Mode: {mode}",
        "ui.stream_thinking_changed": "Reasoning streaming: {state}",
        "ui.stream_mode_invalid": "Unknown mode",
        "ui.stream_mode_not_in_forum": "⚠️ /stream works only in forum topics.",
        "ui.stream_mode_write_failed": "Failed to save config",
        "ui.state_on": "on",
        "ui.state_off": "off",
        "ui.thinking_toggle": "🧠 thoughts: {state}",
        "ui.page_of": "Page {page} of {total}",
        "ui.busy_wait": "Wait for the current request to finish.",
        # --- UI: model picker -------------------------------------------
        "ui.model_not_in_forum": "⚠️ /model works only in forum topics",
        "ui.model_picker_caption": "Current model: <b>{label}</b>\nPick a model:",
        "ui.model_invalid": "Unknown model.",
        "ui.model_already": "Already selected.",
        "ui.model_write_failed": "Failed to save.",
        "ui.model_changed": "Model: {label}",
        "ui.model_changed_note": "Model: <b>{label}</b>. {tail}",
        "ui.model_tail_continuity": (
            "Same conversation — the next reply is already on the new model."
        ),
        "ui.model_tail_new_session": "A new session starts with your next message.",
        # --- UI: /sysprompt ---------------------------------------------
        "ui.sysprompt_scope_topic": "topic",
        "ui.sysprompt_scope_chat": "chat",
        "ui.sysprompt_show_current": (
            "Current {scope} prompt (appended to the bot persona):\n\n"
            "<blockquote>{preview}</blockquote>\n\n"
            "To change it — send text after /sysprompt (or reply to a message). "
            "To reset — /sysprompt reset."
        ),
        "ui.sysprompt_truncated": "…(truncated)",
        "ui.sysprompt_not_set": (
            "No {scope} prompt is set — the default bot persona is used.\n\n"
            "To set one — send text after /sysprompt (or reply to a message)."
        ),
        "ui.sysprompt_save_failed": "Failed to save the prompt.",
        "ui.sysprompt_reset_done": (
            "The {scope} prompt was reset — back to the default bot persona."
        ),
        "ui.sysprompt_saved": "The {scope} prompt was saved (appended to the bot persona).",
        "ui.sysprompt_applies_next_session": "Takes effect from the next new session.",
        "ui.sysprompt_codex_warning": (
            "⚠️ This topic runs on the Codex engine — the custom prompt currently "
            "applies only to Claude."
        ),
        "ui.sysprompt_apply_btn": "🔄 Apply now (/new)",
        "ui.sysprompt_applied_toast": "Applied.",
        "ui.sysprompt_applied": (
            "Prompt applied. A new session will start with your next message."
        ),
        # --- UI: unsupported message types --------------------------------
        "ui.unsupported_video": (
            "Video isn't supported yet. Send it as a file (📎) or describe it in text"
        ),
        "ui.unsupported_sticker": "Stickers aren't supported. Copy the emoji as text",
        "ui.unsupported_contact": "Contacts aren't supported. Send the number as text",
        "ui.unsupported_location": "Location isn't supported. Send the address as text",
        "ui.unsupported_audio": "Audio files aren't supported yet. Send as a document (📎)",
        "ui.unsupported_animation": "GIFs aren't supported. Send as a file or a link",
        "ui.unsupported_generic": "This message type isn't supported yet",
        # --- UI: usage pin ------------------------------------------------
        "ui.usage_finished": "✅ Finished",
        "ui.usage_context_empty": "🧠 Context: —",
        "ui.usage_started": "Started",
        "ui.usage_now": "now",
        "ui.usage_hours_minutes": "{hours}h{minutes}m",
        "ui.usage_minutes": "{minutes}m",
        "ui.file_too_large_preview": "File is too large for inline preview",
        "ui.codex_transcript_missing": (
            "Codex accepted the message, but the bot could not find the transcript "
            "to stream the reply. The session is left alive; open /tui or send the "
            "next message once the work finishes."
        ),
        "ui.session_switched": "🔄 session: {sid}",
        "ui.session_switched_engine": "🔄 {engine} session: {sid}",
        "ui.resume_picker_caption_hdr": "Sessions for <code>{cwd}</code>, page {page}/{total}",
        "ui.resume_no_sessions": "No saved sessions for this cwd",
        "ui.resume_not_in_forum": "⚠️ /resume works only in forum topics",
        "ui.resume_subprocess_unsupported": "This command works only in tmux mode (/mode)",
        "ui.resume_already_on_it": "Already on this session",
        "ui.resume_target_missing": "Transcript disappeared; open /resume again",
        "ui.resume_invalid_id": "Invalid session ID",
        "ui.resume_starting": "Resuming...",
        "ui.resume_current_marker": "current",
        "ui.resume_switched": "🔄 session: <code>{sid}</code>",
        "ui.resume_started": "🆕 tmux started with resume <code>{sid}</code>",
        "ui.resume_engine_switched": "↪️ Switching engine to <code>{engine}</code>",
        "ui.resume_picker_stale": "List is stale, open /resume again",
        "ui.resume_spawn_failed": "Failed to start tmux. The next message will start fresh.",
        "ui.resume_spawn_failed_engine_changed": (
            "Failed to start tmux. Engine was switched to {engine};"
            " the next message will start fresh."
        ),
        "ui.resume_config_write_failed": "Failed to update engine; nothing changed",
        "ui.resume_cancelled": "Cancelled",
        "ui.error_generic": "An error occurred while processing the request. Try again.",
        "ui.cc_not_found": "Claude Code not found. Make sure it is installed and on PATH.",
        "ui.agent_cli_not_found": (
            "I couldn't find Claude Code or Codex. Install at least one of them for the "
            "same Linux user that runs this bot, make sure it is available on PATH, "
            "then restart the bot."
        ),
        "ui.compacting": "⏳ Compacting context...",
        "ui.compact_done": "✅ Compacted: {pre:,} → {post:,} tokens",
        "ui.running_command": "⚙️ Running {command}...",
        "ui.inactivity_kill": "Hung up, try again",
        "ui.voice_too_large": "Voice message is too large (max 100 MB).",
        "ui.voice_not_recognized": "Could not transcribe voice message",
        "ui.download_error": "Couldn't download the file, try again",
        "ui.file_too_large": "File is too large (max {size} MB)",
        "ui.forward_error": "Failed to process messages, try again",
        # --- UI: keyboard buttons -------------------------------------
        "ui.btn_new_chat": "New chat",
        "ui.btn_cancel": "Cancel ❌",
        "ui.btn_tui": "TUI 🖥",
        "ui.btn_checkpoint": "📌",
        "ui.checkpoint_prompt": (
            "Update the checkpoint in CLAUDE.md — briefly record"
            " what was done in this session and what's next."
        ),
        # --- CC content prefixes (injected into prompts) --------------
        "cc.voice_label": "Voice",
        "cc.videomessage_label": "Video message",
        "cc.voice_transcript_short": "[Voice, transcription]:",
        "cc.voice_failed_full": "[Voice message: failed to transcribe]",
        "cc.voice_too_large": "[Voice message: file too large]",
        "cc.voice_empty": "[Voice message: empty transcription]",
        "cc.transcription_failed": "[{label}, not transcribed]",
        "cc.photo": "[User sent a photo. Read the image file before responding]",
        "cc.photo_with_caption": (
            "[User sent a photo with caption]: {caption}\n[Read the image file before responding]"
        ),
        "cc.photo_failed": "[Photo: failed to download]",
        "cc.video": "[Video]",
        "cc.videomessage": "[Video message]",
        "cc.document": "[Document]",
        "cc.document_named": "[Document: {name}]",
        "cc.document_full": "[Document: {name}, {mime}]",
        "cc.document_failed": "[Document: {name}, failed to download]",
        "cc.sticker": "[Sticker: {emoji}]",
        "cc.audio": "[Audio: {title}]",
        "cc.audio_untitled": "untitled",
        "cc.empty_message": "[Empty message]",
        "cc.unknown_sender": "Unknown sender",
        "cc.unknown_channel": "Unknown channel",
        "cc.unknown_chat": "Unknown chat",
        "cc.file_default": "file",
        "cc.file_path": "[Photo saved at {path} — Read this file to see the image]",
        "cc.file_caption": (
            "[Photo caption]: {caption} [Photo saved at {path} — Read this file to see the image]"
        ),
        "cc.batch_during_processing": ("[Messages received during processing ({count} total)]:"),
        "cc.reply_context": ("[Message the user replied to]:\n{context}\n\n[User reply]:\n{reply}"),
        "cc.message_truncated": "\n[...message truncated]",
        "cc.sender_attribution": "[Message from: {name}]",
        "cc.forward_batch": "[Forwarded messages, {count} total]:",
        "cc.forward_message_header": "--- Message {index} ---",
        "cc.forward_from": "From: {name}",
        "cc.forward_post_link": "Post link: {link}",
        "cc.forward_date": "Date: {date}",
        "cc.attached_file": "Attached file (MUST Read before responding): {path}",
        "cc.caption": "Caption: {caption}",
        "cc.user_comment": "User comment: {comment}",
        "cc.photo_error": "[Photo: {error}]",
        "cc.document_error": "[Document: {name}, {mime}: {error}]",
        "cc.files_batch": "[Files, {count} total]:",
        "cc.unknown_file_type": "[Unknown file type]",
        "cc.error_generic_label": "error",
        "cc.topic_theme": (
            "This Telegram topic is named “{name}” — that is the subject of the "
            "conversation here. Stay focused on it and interpret messages in that "
            "context. If you have memory or note-taking tools available with prior "
            "work on this subject, lean on them."
        ),
        # --- Queue messages -------------------------------------------
        "ui.queue_added_batch": "Added to batch, #{position} in queue",
        "ui.queue_added": "Added to queue (#{position})",
        "ui.queue_session_suffix": ", session: {sid}",
        "ui.queue_remove_btn": "❌ Remove from queue",
        "ui.queue_removed": "❌ Removed from queue",
        "ui.queue_in_flight": "Already running — press ⛔ Stop",
        "ui.streaming_injected": "↳ delivered to the current turn",
        "ui.streaming_died": (
            "⚠️ The live session dropped. Send your message again — "
            "context is restored from the last checkpoint."
        ),
        # --- Tool status (shown while CC runs a tool) -----------------
        "tool.read": "📖 Reading file",
        "tool.grep": "🔍 Searching",
        "tool.glob": "🔍 Finding files",
        "tool.bash": "⚙️ Running",
        "tool.bash_with_cmd": "⚙️ Running: {cmd}",
        "tool.write": "✏️ Writing file",
        "tool.edit": "✏️ Editing",
        "tool.skill": "📋 Loading skill",
        "tool.tool_search": "🔍 Looking for a tool...",
        "tool.agent": "🤖 Launching subagent",
        "tool.agent_done": "✅ Subagent finished",
        "tool.agent_done_with_desc": "✅ Subagent finished: {desc}",
        "tool.send_message": "💬 Sending message...",
        "tool.send_image": "🖼 Sending image...",
        "tool.send_document": "📎 Sending document...",
        "tool.fetch_url": "🌐 Fetching URL",
        "tool.run_tests": "🧪 Running tests",
        "tool.calc_time": "🧮 Calculating time",
        "tool.check_time": "🕐 Checking time",
        "tool.read_memory": "🧠 Reading memory",
        "tool.write_memory": "🧠 Updating memory",
        "tool.read_skill": "📋 Reading skill",
        "tool.write_skill": "📋 Updating skill",
    },
    "ru": {
        # --- UI: user-facing status / error messages -------------------
        "ui.compacting": "⏳ Сжимаю контекст...",
        "ui.compact_done": "✅ Сжато: {pre:,} → {post:,} токенов",
        "ui.running_command": "⚙️ Выполняю {command}...",
        "ui.start_welcome": (
            "Привет! Я перешлю твои сообщения в Claude Code. Просто отправь мне сообщение."
        ),
        "ui.thinking": "⏳ Думаю...",
        "ui.recognizing_voice": "⏳ Распознаю голосовое...",
        "ui.voice_recognition_failed": (
            "❌ Не смог распознать голосовое — попробуй ещё раз или напиши текстом."
        ),
        "ui.processing_forwards": "⏳ Обрабатываю пересланные сообщения...",
        "ui.processing_files": "⏳ Обрабатываю файлы...",
        "ui.new_session": "Новая сессия",
        "ui.context_cleared": "🧹 Контекст очищен, tmux-сессия жива",
        "ui.reset_failed": "❌ Не удалось обновить контекст. Попробуй ещё раз через пару секунд.",
        "ui.topic_welcome": (
            "👋 Тема готова. Скажи, для чего она — какой проект (путь или название) и "
            "нужен ли кастомный промпт."
        ),
        "ui.cancelled": "❌ Отменено",
        "ui.cancelled_queue_pending": "❌ Отменено. Обрабатываю {count} сообщ. из очереди…",
        "ui.nothing_to_cancel": "Нечего отменять",
        "ui.language_current": "Язык: <b>{lang}</b>. Используй /language ru или /language en.",
        "ui.language_changed": "Язык переключён на <b>{lang}</b>.",
        "ui.language_invalid": "Неизвестный язык. Используй /language ru или /language en.",
        "ui.already_finished": "Процесс уже завершён",
        "ui.tmux_failed": "❌ Не удалось запустить tmux: {exc}",
        "ui.tmux_killed": "🗑 Tmux-сессия убита",
        "ui.tmux_not_active": "В этом топике нет активной tmux-сессии",
        "ui.engine_starting": (
            "🔄 {engine} запускается — подожди несколько секунд перед отправкой."
        ),
        "ui.engine_ready": "✅ {engine} готов к работе.",
        "ui.engine_start_failed": "⚠️ {engine} не запустился: {exc}",
        # --- UI: tail / tui feature strings (Wave 3 tmux-tui-mode) ----
        "ui.tail_unavailable": (
            "⚠️ Нет активной tmux-сессии — /tui недоступен."
            " Сначала переключись в tmux или запусти сессию."
        ),
        "ui.tail_snapshot_header": "Снимок TUI (последние строки):",
        "ui.tail_keyboard_stale": (
            "Клавиатура устарела — сессия перезапущена. Вызови /tui заново."
        ),
        "ui.modal_blocked_header": (
            "⚠️ Сообщение НЕ отправлено — CC ждёт действие в модальном диалоге.\n"
            "Твоё сообщение: <code>{prompt}</code>\n"
            "Закрой диалог (Esc / выбери пункт) и отправь заново."
        ),
        "ui.modal_idle_detected": (
            "⚠️ CC ждёт действие в модальном диалоге.\n"
            "Закрой его (Esc / выбери пункт), чтобы CC продолжил работу."
        ),
        "ui.tui_start_timeout": (
            "❌ Claude Code TUI не поднялся за 30 с — tmux убит."
            " Попробуй ещё раз или переключись /mode → обычный."
        ),
        "ui.tui_session_missing": (
            "Эта сессия недоступна (создана до миграции) — пиши в текущую сессию дальше."
        ),
        # --- UI: exec-mode picker -------------------------------------
        "ui.exec_mode_picker_caption": (
            "Режим выполнения: <b>{current}</b>\n\n"
            "⚡ обычный — каждое сообщение запускает сабпроцесс Claude без"
            " контекста от прошлых сообщений. Задача выполняется, сессия"
            " умирает, ресурсы не жрёт. Подходит для разовых задач, заметок"
            " и т.п.\n\n"
            "🌊 поток — живой процесс Claude держится между ходами. Сообщение,"
            " присланное во время работы, подхватывается между вызовами"
            " инструментов (как печатать в терминале, пока Claude думает). Без"
            " tmux и его всплывашек. /kill закрывает живой процесс.\n\n"
            "🖥 tmux — персистентная tmux-сессия, которая постоянно живёт и"
            " помнит контекст между сообщениями. Это полноценный Claude Code,"
            " поддерживает agent team, но постоянно жрёт ресурсы. Лучше для"
            " разработки и больших задач. Первый запуск ~1-2 сек. Чтобы убить"
            " сессию и освободить ресурсы — команда /kill."
        ),
        "ui.exec_mode_changed": "Режим: {mode}",
        "ui.exec_mode_already": "Уже: {mode}",
        "ui.exec_mode_busy": "Сейчас идёт обработка, нажми /cancel и повтори",
        "ui.exec_mode_invalid": "Неизвестный режим",
        "ui.exec_mode_not_in_forum": "⚠️ /mode работает только в форум-топиках",
        "ui.exec_mode_write_failed": "Не удалось сохранить режим, попробуй ещё раз",
        "ui.exec_mode_label_subprocess": "⚡ обычный",
        "ui.exec_mode_label_streaming": "🌊 поток",
        "ui.exec_mode_label_tmux": "🖥 tmux",
        "ui.engine_picker_caption": (
            "Движок: <b>{engine}</b>\n\n/mode остаётся режимом транспорта: обычный или tmux."
        ),
        "ui.engine_changed": "Движок: {engine}",
        "ui.engine_changed_new_session": (
            "Движок — {engine}.\n\nАктивная сессия сброшена. Следующее сообщение"
            " запустит новую сессию с {engine}."
        ),
        "ui.engine_already": "Уже выбрано",
        "ui.engine_invalid": "Неизвестный движок",
        "ui.engine_not_in_forum": "⚠️ /engine работает только внутри форум-топиков",
        "ui.engine_write_failed": "Не удалось сохранить настройки движка",
        "ui.tmux_started_engine": "🖥 Создана новая tmux-сессия с {engine}",
        "ui.reply_engine_switched": ("↪️ Ответ ведёт в {engine}; переключаю топик перед resume."),
        "ui.stream_mode_picker_caption": (
            "Режим трансляции этого топика: <b>{current}</b>\n\n"
            "• <b>verbose</b> — каждое событие отдельным сообщением\n"
            "• <b>live</b> — один редактируемый буфер с прогрессом\n"
            "• <b>minimal</b> — только финальные ответы\n"
            "• <b>live+</b> — буфер прогресса + промежуточные 💬-реплики; 🧠 добавляет рассуждения"
        ),
        "ui.stream_mode_changed": "Режим: {mode}",
        "ui.stream_thinking_changed": "Трансляция рассуждений: {state}",
        "ui.stream_mode_invalid": "Неизвестный режим",
        "ui.stream_mode_not_in_forum": "⚠️ /stream работает только внутри форум-топиков.",
        "ui.stream_mode_write_failed": "Не удалось записать конфиг",
        "ui.state_on": "вкл",
        "ui.state_off": "выкл",
        "ui.thinking_toggle": "🧠 мысли: {state}",
        "ui.page_of": "Стр. {page} из {total}",
        "ui.busy_wait": "Дождись завершения текущего запроса.",
        # --- UI: model picker -------------------------------------------
        "ui.model_not_in_forum": "⚠️ /model работает только в топиках форума",
        "ui.model_picker_caption": "Текущая модель: <b>{label}</b>\nВыбери модель:",
        "ui.model_invalid": "Неизвестная модель.",
        "ui.model_already": "Уже выбрана.",
        "ui.model_write_failed": "Не удалось сохранить.",
        "ui.model_changed": "Модель: {label}",
        "ui.model_changed_note": "Модель: <b>{label}</b>. {tail}",
        "ui.model_tail_continuity": "Та же беседа — следующий ответ уже на новой модели.",
        "ui.model_tail_new_session": "Новая сессия при следующем сообщении.",
        # --- UI: /sysprompt ---------------------------------------------
        "ui.sysprompt_scope_topic": "топика",
        "ui.sysprompt_scope_chat": "лички",
        "ui.sysprompt_show_current": (
            "Текущий промт {scope} (дополняет персону бота):\n\n"
            "<blockquote>{preview}</blockquote>\n\n"
            "Изменить — пришли текст после /sysprompt (или ответом на сообщение). "
            "Сбросить — /sysprompt reset."
        ),
        "ui.sysprompt_truncated": "…(обрезано)",
        "ui.sysprompt_not_set": (
            "Промт {scope} не задан — используется дефолтная персона бота.\n\n"
            "Задать — пришли текст после /sysprompt (или ответом на сообщение)."
        ),
        "ui.sysprompt_save_failed": "Не удалось сохранить промт.",
        "ui.sysprompt_reset_done": "Промт {scope} сброшен — снова дефолтная персона бота.",
        "ui.sysprompt_saved": "Промт {scope} сохранён (дополняет персону бота).",
        "ui.sysprompt_applies_next_session": "Применится со следующей новой сессии.",
        "ui.sysprompt_codex_warning": (
            "⚠️ Этот топик на движке Codex — кастомный промт пока применяется только к Claude."
        ),
        "ui.sysprompt_apply_btn": "🔄 Применить сейчас (/new)",
        "ui.sysprompt_applied_toast": "Применено.",
        "ui.sysprompt_applied": (
            "Промт применён. Новая сессия запустится при следующем сообщении."
        ),
        # --- UI: unsupported message types --------------------------------
        "ui.unsupported_video": (
            "Видео пока не поддерживается. Отправь как файл (📎) или опиши текстом"
        ),
        "ui.unsupported_sticker": "Стикеры не поддерживаются. Скопируй эмодзи текстом",
        "ui.unsupported_contact": "Контакты не поддерживаются. Отправь номер текстом",
        "ui.unsupported_location": "Геолокация не поддерживается. Скинь адрес текстом",
        "ui.unsupported_audio": "Аудиофайлы пока не поддерживаются. Отправь как документ (📎)",
        "ui.unsupported_animation": "GIF не поддерживается. Отправь как файл или ссылку",
        "ui.unsupported_generic": "Этот тип сообщения пока не поддерживается",
        # --- UI: usage pin ------------------------------------------------
        "ui.usage_finished": "✅ Завершена",
        "ui.usage_context_empty": "🧠 Контекст: —",
        "ui.usage_started": "Начало",
        "ui.usage_now": "сейчас",
        "ui.usage_hours_minutes": "{hours}ч{minutes}м",
        "ui.usage_minutes": "{minutes}м",
        "ui.file_too_large_preview": "Файл слишком большой для preview",
        "ui.codex_transcript_missing": (
            "Codex принял сообщение, но бот не смог найти transcript "
            "для стриминга ответа. Сессия оставлена живой; открой /tui "
            "или отправь следующее сообщение после завершения работы."
        ),
        "ui.session_switched": "🔄 сессия: {sid}",
        "ui.session_switched_engine": "🔄 сессия {engine}: {sid}",
        "ui.resume_picker_caption_hdr": "Сессии для <code>{cwd}</code>, страница {page}/{total}",
        "ui.resume_no_sessions": "Сохранённых сессий для этого cwd нет",
        "ui.resume_not_in_forum": "⚠️ /resume работает только внутри форум-топиков",
        "ui.resume_subprocess_unsupported": "Команда работает только в tmux-режиме (/mode)",
        "ui.resume_already_on_it": "Уже на этой сессии",
        "ui.resume_target_missing": "Транскрипт пропал; открой /resume заново",
        "ui.resume_invalid_id": "Некорректный ID сессии",
        "ui.resume_starting": "Возобновляю...",
        "ui.resume_current_marker": "текущая",
        "ui.resume_switched": "🔄 сессия: <code>{sid}</code>",
        "ui.resume_started": "🆕 tmux поднят с resume <code>{sid}</code>",
        "ui.resume_engine_switched": "↪️ Переключаю движок на <code>{engine}</code>",
        "ui.resume_picker_stale": "Список устарел, открой /resume заново",
        "ui.resume_spawn_failed": (
            "Не удалось поднять tmux. Следующее сообщение начнёт fresh-сессию"
        ),
        "ui.resume_spawn_failed_engine_changed": (
            "Не удалось поднять tmux. Движок уже переключён на {engine}; "
            "следующее сообщение начнёт fresh-сессию."
        ),
        "ui.resume_config_write_failed": "Не удалось обновить engine; ничего не изменено",
        "ui.resume_cancelled": "Отменено",
        "ui.error_generic": "Произошла ошибка при обработке запроса. Попробуй ещё раз.",
        "ui.cc_not_found": (
            "Claude Code не найден. Убедитесь, что он установлен и доступен в PATH."
        ),
        "ui.agent_cli_not_found": (
            "Я не нашёл ни Claude Code, ни Codex. Установи хотя бы один из них "
            "для того же Linux-пользователя, который запускает бота, проверь PATH "
            "и перезапусти бота."
        ),
        "ui.inactivity_kill": "Зависло, попробуй ещё раз",
        "ui.voice_too_large": "Голосовое сообщение слишком большое (максимум 100 МБ).",
        "ui.voice_not_recognized": "Не удалось распознать голосовое сообщение",
        "ui.download_error": "Не удалось скачать файл, попробуй ещё раз",
        "ui.file_too_large": "Файл слишком большой (максимум {size} МБ)",
        "ui.forward_error": "Не удалось обработать сообщения, попробуй ещё раз",
        # --- UI: keyboard buttons -------------------------------------
        "ui.btn_new_chat": "Новый чат",
        "ui.btn_cancel": "Отменить ❌",
        "ui.btn_tui": "TUI 🖥",
        "ui.btn_checkpoint": "📌",
        "ui.checkpoint_prompt": (
            "Обнови чекпоинт в CLAUDE.md — кратко зафиксируй"
            " что было сделано в этой сессии и что дальше."
        ),
        # CC content prefixes are intentionally English-only — see MESSAGES["en"].
        # Exception: the auto-theme topic prompt reads better in the bot's UI
        # language, so it is localized.
        "cc.topic_theme": (
            "Этот топик в Telegram называется «{name}» — это тема разговора "
            "здесь. Держи фокус на ней и трактуй сообщения в этом контексте. "
            "Если среди доступных инструментов есть память или заметки с "
            "наработками по теме — опирайся на них."
        ),
        # --- Queue messages -------------------------------------------
        "ui.queue_added_batch": "Добавлено в батч, он №{position} в очереди",
        "ui.queue_added": "Добавлено в очередь (№{position})",
        "ui.queue_session_suffix": ", сессия: {sid}",
        "ui.queue_remove_btn": "❌ Убрать из очереди",
        "ui.queue_removed": "❌ Убрано из очереди",
        "ui.queue_in_flight": "Уже выполняется — нажми ⛔ Стоп",
        "ui.streaming_injected": "↳ передано в текущий ход",
        "ui.streaming_died": (
            "⚠️ Живая сессия отвалилась. Пришли сообщение ещё раз — "
            "контекст восстановится с последней точки."
        ),
        # --- Tool status ----------------------------------------------
        "tool.read": "📖 Читаю файл",
        "tool.grep": "🔍 Ищу",
        "tool.glob": "🔍 Ищу файлы",
        "tool.bash": "⚙️ Выполняю",
        "tool.bash_with_cmd": "⚙️ Выполняю: {cmd}",
        "tool.write": "✏️ Пишу файл",
        "tool.edit": "✏️ Редактирую",
        "tool.skill": "📋 Загружаю Skill",
        "tool.tool_search": "🔍 Ищу инструмент...",
        "tool.agent": "🤖 Запускаю субагента",
        "tool.agent_done": "✅ Субагент завершил работу",
        "tool.agent_done_with_desc": "✅ Субагент завершил работу: {desc}",
        "tool.send_message": "💬 Отправляю сообщение...",
        "tool.send_image": "🖼 Отправляю картинку...",
        "tool.send_document": "📎 Отправляю документ...",
        "tool.fetch_url": "🌐 Загружаю URL",
        "tool.run_tests": "🧪 Запускаю тесты",
        "tool.calc_time": "🧮 Считаю время",
        "tool.check_time": "🕐 Проверяю время",
        "tool.read_memory": "🧠 Читаю память",
        "tool.write_memory": "🧠 Обновляю память",
        "tool.read_skill": "📋 Читаю скилл",
        "tool.write_skill": "📋 Обновляю скилл",
    },
}


@functools.lru_cache(maxsize=1)
def _get_lang() -> str:
    """Read BOT_LANG from environment once per process.

    Reads os.environ directly (not Settings) so that t() works at module
    import time before .env validation runs — handlers use t() in router
    filters, which fire while the dispatcher is being built.
    """
    import os

    lang = os.environ.get("BOT_LANG", _DEFAULT_LANG)
    if lang not in MESSAGES:
        return _DEFAULT_LANG
    return lang


def t(key: str, **kwargs: Any) -> str:
    """Translate a key using the configured bot language.

    Falls back to English if the key is missing in the active language,
    then to the key itself if missing in English too. Format placeholders
    via kwargs.
    """
    lang = _get_lang()
    template = MESSAGES.get(lang, MESSAGES[_DEFAULT_LANG]).get(key)
    if template is None:
        template = MESSAGES[_DEFAULT_LANG].get(key, key)
    if kwargs:
        return template.format(**kwargs)
    return template


def all_translations(key: str) -> frozenset[str]:
    """All known translations of *key* across every language table.

    Reply-keyboard button filters use this instead of `F.text == t(key)`: the
    latter freezes the current-language label at import time, so after a runtime
    `/language` switch the button on the keyboard (new language, or a stale
    keyboard still showing the old one) no longer matches the filter and the
    press falls through to Claude as a plain prompt. Matching against ALL
    languages' labels keeps the button working regardless. (H5, audit 2026-07-02.)
    """
    return frozenset(table[key] for table in MESSAGES.values() if key in table)


def reset_lang_cache() -> None:
    """Clear cached language — for tests that change settings between runs."""
    _get_lang.cache_clear()
