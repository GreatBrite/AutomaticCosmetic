#!/usr/bin/env bash
set -euo pipefail

APP_NAME="freelance-leads-bot"
APP_DIR="${APP_DIR:-/opt/freelance_leads_bot}"
SERVICE_NAME="${SERVICE_NAME:-freelance-leads-bot}"
RUN_USER="${RUN_USER:-root}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl python3 python3-venv python3-pip rsync

mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.env' \
  --exclude 'data/*' \
  --exclude 'dist/' \
  ./ "$APP_DIR/"

mkdir -p "$APP_DIR/data" "$APP_DIR/data/telegram_uploads" "$APP_DIR/data/tts"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip wheel
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

if [[ ! -x /root/.local/bin/codex && -x "$APP_DIR/tools/codex/bin/codex" ]]; then
  mkdir -p /root/.local/bin /root/.codex/packages/standalone
  rsync -a "$APP_DIR/tools/codex/" /root/.codex/packages/standalone/current/
  ln -sf /root/.codex/packages/standalone/current/bin/codex /root/.local/bin/codex
fi

if [[ ! -x /root/.local/bin/codegraph && -x "$APP_DIR/tools/codegraph/codegraph" ]]; then
  mkdir -p /root/.local/bin
  ln -sf "$APP_DIR/tools/codegraph/codegraph" /root/.local/bin/codegraph
fi

if [[ -d "$APP_DIR/tools/codex_skills" ]]; then
  mkdir -p /root/.codex/skills
  rsync -a "$APP_DIR/tools/codex_skills/" /root/.codex/skills/
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  python3 - "$APP_DIR/.env" <<'PY'
from pathlib import Path
import os
import sys

path = Path(sys.argv[1])
values = {
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
    "ALLOWED_TELEGRAM_USERNAMES": os.getenv("ALLOWED_TELEGRAM_USERNAMES", ""),
    "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
    "MINIAPP_PUBLIC_URL": os.getenv("MINIAPP_PUBLIC_URL", ""),
}
lines = []
for raw in path.read_text(encoding="utf-8").splitlines():
    if "=" not in raw or raw.startswith("#"):
        lines.append(raw)
        continue
    key = raw.split("=", 1)[0]
    if key in values and values[key]:
        lines.append(f"{key}={values[key]}")
    else:
        lines.append(raw)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  echo "Created $APP_DIR/.env."
fi

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Automatic Cosmetic operations Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment=PATH=/root/.local/bin:${APP_DIR}/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${APP_DIR}/.venv/bin/python -m src.freelance_leads_bot.main serve
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "Installed to $APP_DIR"
if grep -q '^TELEGRAM_BOT_TOKEN=put_bot_token_here$' "$APP_DIR/.env" || ! grep -q '^TELEGRAM_CHAT_ID=.' "$APP_DIR/.env"; then
  echo "Fill $APP_DIR/.env, then start the service: systemctl start $SERVICE_NAME"
else
  systemctl start "$SERVICE_NAME"
  echo "Service started: $SERVICE_NAME"
fi
