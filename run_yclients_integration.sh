#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${YCLIENTS_INTEGRATION_HOST:-127.0.0.1}"
PORT="${YCLIENTS_INTEGRATION_PORT:-8020}"
if [[ -x .venv/bin/uvicorn ]]; then
  exec .venv/bin/uvicorn src.freelance_leads_bot.integrations.yclients_integration:app --host "$HOST" --port "$PORT"
fi
exec python3 -m uvicorn src.freelance_leads_bot.integrations.yclients_integration:app --host "$HOST" --port "$PORT"
