# Portable Deploy

One-command deploy after uploading the archive:

```bash
tar -xzf freelance-leads-bot-portable.tar.gz && cd freelance-leads-bot-portable && sudo ./deploy/install.sh
```

One-command deploy with secrets passed through environment:

```bash
TELEGRAM_BOT_TOKEN='...' TELEGRAM_CHAT_ID='...' OPENAI_API_KEY='...' sudo -E ./deploy/install.sh
```

The installer creates `/opt/freelance_leads_bot`, installs Python dependencies, installs bundled Codex tools/skills when missing, and creates a systemd service. It starts the service only when the Telegram token and chat id are already present.

Before real use, fill `/opt/freelance_leads_bot/.env`:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OPENAI_API_KEY`
- `MINIAPP_PUBLIC_URL` only after the new domain is known

Not included:

- Telegram account MTProto session and account-listener functionality
- current `.env`
- real MCP configs with credentials; only examples are included
- SQLite database and Codex chat history
- Telegram uploads, generated voice files, screenshots, runtime logs
- Codex auth/cache/model cache
