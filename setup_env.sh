#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

read -r -p "Telegram chat id [912405808]: " chat_id
chat_id="${chat_id:-912405808}"
read -r -s -p "Telegram bot token: " bot_token
printf "\n"

if [[ -z "$bot_token" ]]; then
  echo "Bot token is required" >&2
  exit 1
fi

cat > .env <<EOF
TELEGRAM_BOT_TOKEN=$bot_token
TELEGRAM_CHAT_ID=$chat_id
DB_PATH=data/leads.sqlite3
MAX_ITEMS_PER_RUN=12
MIN_SCORE=1
POLL_SECONDS=900
KEYWORDS=python,django,fastapi,flask,telegram bot,automation,scraping,parser,web scraping,backend,api,ai,openai,chatgpt,llm,data,sqlite,postgres
NEGATIVE_KEYWORDS=senior manager,unpaid,volunteer,internship,onsite only,clearance
EOF

chmod 600 .env
echo ".env written"
