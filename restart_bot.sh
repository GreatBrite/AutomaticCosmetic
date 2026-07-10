#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
service="${BOT_SYSTEMD_SERVICE:-freelance-leads-bot.service}"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is not available; cannot restart $service" >&2
  exit 1
fi

if ! systemctl cat "$service" >/dev/null 2>&1; then
  echo "systemd service not found: $service" >&2
  exit 1
fi

echo "Restarting $service via systemd..."
exec systemctl restart "$service"
