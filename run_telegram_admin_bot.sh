#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

python -m src.freelance_leads_bot.integrations.telegram_admin_bot
