#!/bin/bash
# Bot MCP Server launcher
# Reads TELEGRAM_BOT_TOKEN from PROJECT_DIR/.env, exposes it as BOT_TOKEN.

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
PROJECT_DIR="${PROJECT_DIR:-$SCRIPT_DIR/../..}"

# Extract ONLY the token. The previous `export $(... | xargs)` broke the
# moment .env gained values with spaces/newlines (YANDEX_SA_KEY_JSON PEM,
# UNAUTHORIZED_REPLY text, paths with "Application Support"): xargs split
# them into invalid identifiers, `export` exited non-zero, TELEGRAM_BOT_TOKEN
# was never set → start.sh hit `exit 1` and the bot MCP server never started,
# leaving the agent without send_image/send_document/send_message.
if [ -f "$PROJECT_DIR/.env" ]; then
  TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$PROJECT_DIR/.env" | head -n1 | sed 's/^TELEGRAM_BOT_TOKEN=//')"
  export TELEGRAM_BOT_TOKEN
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "Error: TELEGRAM_BOT_TOKEN not set in $PROJECT_DIR/.env" >&2
  exit 1
fi

export BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
exec "$PROJECT_DIR/.venv/bin/python" "$SCRIPT_DIR/server.py"
