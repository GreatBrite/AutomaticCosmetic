# Development Notes

## Current State

- The portable cosmetology operations bot is unpacked in `/root/AutomaticCosmetic`.
- All real runtime keys and secrets live in `.env` and are ignored by git.
- `.env.example` is only a non-secret reference for deploy/install tooling.
- CodeGraph is initialized in `.codegraph/`; use it for structural code lookup.
- Python dependencies are installed in `.venv`.
- Legacy Avito/YCLIENTS/VK integration code was archived outside the active project at `/root/AutomaticCosmetic_archives/20260529_full_migration/`.

## Runtime Secrets

`.env` is the single source of truth for real local configuration. It contains:

- Telegram bot/admin/client/cosmetologist keys from `/root/YclientsAvitoTg/.env`
- YCLIENTS credentials from `/root/YclientsAvitoTg/.env`
- Avito credentials from `/root/YclientsAvitoTg/.env`
- VK group credentials from `/root/YclientsAvitoTg/.env`
- OpenRouter key from `/root/YclientsAvitoTg_fixed/.env`
- Codex runtime settings from `/root/YclientsAvitoTg/.env`

Some legacy keys were already empty in the source env and remain empty here. Do not put real secrets into `.env.example`.

## Useful Commands

```bash
cd /root/AutomaticCosmetic
.venv/bin/python -m compileall -q src scripts
.venv/bin/python -m src.freelance_leads_bot.main --help
./run_bot.sh
```

## Notes For Next Development

- Current code reads Telegram, Mini App, transcription, TTS, Avito, YCLIENTS, VK, knowledge, and care/upsell settings.
- Avito and YCLIENTS are live in the new runtime. VK is ready, but live sends are controlled by `VK_SEND_ENABLED`.
- The legacy Telegram client service is masked; do not unmask it unless intentionally restoring the archived runtime.
- Keep `.env`, `.env.*`, archives, SQLite databases, MCP credential files, and generated runtime data out of git.
