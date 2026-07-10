#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
exec .venv/bin/python -m src.freelance_leads_bot.integrations.telegram_client_bot
