# Parity Report 2026-05-29

## Scope

The audit checks that the new role-based Codex runtime keeps the active duties that were previously split between the main project and `.legacy_runtime/yclients_avito_tg`.

## Local Documentation Checked

- `docs/api_references/telegram/bot_api.html`
  - Confirms `message_thread_id` for forum/topic routing.
  - Confirms `direct_messages_topic_id` and `sendMessageDraft`.
  - Confirms `parse_mode=HTML` and Telegram-supported HTML tags for streamed/live responses.
- `docs/api_references/yclients/developers_ru.html`
  - Confirms YCLIENTS API shape, Bearer authorization examples, and endpoint catalog.
  - The legacy `/yclients/webhook`, `/yclients/callback`, and `/yclients/register` contract is a local integration adapter contract, not a direct public YCLIENTS resource method.
- `docs/api_references/avito/api_catalog.html`
  - Confirms the Avito API catalog is present locally. Messenger details are embedded in the portal bundle, so code-level parity is verified through current sender/webhook tests.
- `docs/api_references/vk/schema_repo/`
  - Confirms `messages.send`, message attachments, and photo upload/save methods for future VK media parity.

## Implemented Parity

- Role model exists in the new runtime:
  - `admin`
  - `olga_boss`
  - `avito_client`
  - `vk_client`
  - `yclients_upsell_stub`
- Conversation keys are separated by role/channel, including Telegram topics.
- `/health` in the Avito webhook app reports readiness, live flags, roles, and legacy runtime status.
- New YCLIENTS integration adapter now exists at `src/freelance_leads_bot/integrations/yclients_integration.py`.
- The new YCLIENTS adapter exposes the legacy-compatible routes:
  - `GET /health`
  - `GET/HEAD/POST /yclients/webhook`
  - `GET/HEAD/POST /yclients/callback`
  - `GET/POST /yclients/register`
- The new YCLIENTS adapter writes received events to `data/yclients_integration/events.jsonl`, matching the active legacy behavior.
- `run_yclients_integration.sh` and `yclients-integration.service.example` were added for safe deployment of the new adapter.

## Live Runtime Status

- `freelance-leads-bot.service` is active from the new project.
- `yclients-avito-webhook.service` is active from the new project and exposes role/legacy status in local `/health`.
- `yclients-yclients-integration.service` now runs `run_yclients_integration.sh` from the new project.
- `yclients-tg-client.service` is disabled and inactive because it used the old non-Codex Telegram client runtime.

The previous `yclients-yclients-integration.service` unit was backed up before switching.

Public YCLIENTS probes passed:

- `https://olgatihcosmo.com/yclients/webhook`
- `https://olgatihcosmo.com/yclients/callback`
- `https://olgatihcosmo.com/yclients/register`

## Remaining Before Legacy Switch-Off

- Keep monitoring `data/agent_trace.jsonl`, Avito webhook logs, and YCLIENTS integration logs after the switch.
- Decide whether a separate client-facing Telegram role is needed. The old `yclients-tg-client.service` is intentionally off because it bypassed Codex.
- Legacy trees were moved out of the active project to `/root/AutomaticCosmetic_archives/20260529_full_migration`.
