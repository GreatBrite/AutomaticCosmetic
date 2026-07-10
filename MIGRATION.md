# YclientsAvitoTg migration instructions

This archive is meant to be unpacked by an agent on a new Linux server.
It contains the project code, `.env`, `whisper.env`, `data/` SQLite state,
and deployment notes. It intentionally does not contain `.venv`, `.git`,
runtime logs, PID files, Python caches, or local terminal/editor state.

Important: `.env`, `whisper.env`, and `data/yclients_memory.db` contain
production secrets/client data. Keep the archive private and delete it after
migration if it is no longer needed.

## 1. Unpack

```bash
mkdir -p /root/YclientsAvitoTg
tar -xzf yclients_avito_tg_migration_*.tar.gz -C /root/YclientsAvitoTg --strip-components=1
cd /root/YclientsAvitoTg
```

Check that these files exist:

```bash
ls -la .env whisper.env pyproject.toml uv.lock data/yclients_memory.db
```

## 2. Install system dependencies

Ubuntu/Debian example:

```bash
apt update
apt install -y python3.12 python3.12-venv curl ca-certificates nginx
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

If the server does not have Python 3.12 packages, install Python 3.12 using
the server's normal package source, then re-run the commands below.

## 3. Install Python dependencies

The start scripts use `uv run`, so sync dependencies with uv:

```bash
cd /root/YclientsAvitoTg
uv sync
```

Quick import/compile check:

```bash
uv run python -m compileall -q src scripts
uv run python scripts/regression_dialogue_smoke.py
```

The regression script may touch real local memory. If a real external API is
temporarily unavailable, at least run compileall before starting services.

## 4. Restore executable bits

The archive should preserve them, but verify:

```bash
chmod +x start_*.sh stop_*.sh
```

## 5. Start services

Before starting Telegram polling on the new server, stop the old server's
Telegram bots. Telegram allows only one active polling consumer per bot token.

Recommended startup order:

```bash
./start_avito_webhook.sh start
./start_yclients_integration.sh start
./start_bot.sh start
./start_client_bot.sh start
./start_vk_bot.sh start
```

Optional ASR/Whisper service:

```bash
docker compose -f docker-compose.asr.yml up -d
```

Status checks:

```bash
./start_avito_webhook.sh status
./start_yclients_integration.sh status
./start_bot.sh status
./start_client_bot.sh status
./start_vk_bot.sh status
```

Logs:

```bash
tail -f bot.log client_bot.log avito_webhook.log vk_bot.log yclients_integration.log
```

## 6. Avito webhook and nginx

The app listens locally on:

```text
127.0.0.1:8030 -> src.presentation.avito.webhook_app:app
```

Public Avito URL:

```text
https://olgatihcosmo.com/avito/webhook?token=<AVITO_WEBHOOK_SECRET from .env>
```

If this new server will receive public traffic for `olgatihcosmo.com`,
configure DNS to the new IP and apply nginx config from:

```text
deployment/system-configs/olgatihcosmo.com.nginx.conf
deployment/system-configs/nginx-stream-snippet.conf
deployment/system-configs/xray-reality-notes.md
```

Typical nginx commands after adapting paths/certificates:

```bash
cp deployment/system-configs/olgatihcosmo.com.nginx.conf /etc/nginx/sites-available/olgatihcosmo.com
ln -sf /etc/nginx/sites-available/olgatihcosmo.com /etc/nginx/sites-enabled/olgatihcosmo.com
nginx -t
systemctl reload nginx
```

If Xray Reality is also on this machine and must keep working on public 443,
nginx should own public 443 and forward Reality traffic to Xray on
`127.0.0.1:10443` with PROXY protocol. Do not overwrite an existing VPN config
without first checking it.

## 7. Smoke checks after startup

Local Avito endpoint:

```bash
curl -i "http://127.0.0.1:8030/avito/webhook?token=$(grep '^AVITO_WEBHOOK_SECRET=' .env | cut -d= -f2-)"
```

Expected for GET may be `405 Method Not Allowed`; that is fine because the
webhook endpoint is POST-only. The important part is that the service responds.

Public TLS/proxy check:

```bash
curl -I https://olgatihcosmo.com/avito/webhook
```

Expected without token: `403 Forbidden`. That means nginx reaches the Avito
webhook app.

## 8. Cutover checklist

1. Stop old Telegram bot processes before starting new ones.
2. Move DNS or reverse proxy for `olgatihcosmo.com` to the new server.
3. Confirm Avito webhook returns `403` without token and accepts POST with token.
4. Watch logs for 10-15 minutes:
   `tail -f avito_webhook.log client_bot.log vk_bot.log bot.log`.
5. Keep the old server available until new logs show inbound messages are being
   processed normally.
