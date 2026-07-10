#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${AVITO_WEBHOOK_HOST:-127.0.0.1}"
PORT="${AVITO_WEBHOOK_PORT:-8030}"
if [[ -x .venv/bin/uvicorn ]]; then
  exec .venv/bin/uvicorn src.freelance_leads_bot.integrations.avito_webhook:app --host "$HOST" --port "$PORT"
fi
exec python3 -m uvicorn src.freelance_leads_bot.integrations.avito_webhook:app --host "$HOST" --port "$PORT"
