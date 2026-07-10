# Cosmetology Automation Spec

## Context

The business is a cosmetologist working across four cities and advertising the same services through two Avito accounts. There was no previous structured client database. YCLIENTS is the system of record for appointments and client notes.

The automation has three product surfaces:

1. Avito consultant for inbound leads and booking.
2. Telegram administrator for the cosmetologist.
3. Care/upsell bot for post-visit product and service follow-up.

## Source Of Truth

- Client records, appointments, services, and visit history: YCLIENTS.
- Avito conversation style and FAQ knowledge: exported Avito conversations plus curated service/price knowledge.
- Runtime secrets and domain config: `/root/AutomaticCosmetic/.env`.
- Public domain: `https://olgatihcosmo.com`.

## Avito Consultant

The consultant serves both Avito accounts and answers with the same service catalog, tone, prices, and booking policy.

Operating principle:

- Codex is the thinking layer under the hood. There is no separate intent detector in the main path: the agent receives context plus full CRUD tools and decides what to do.
- Prefer tool use over handoff. The bot should check services, prices, slots, client context, and the editable knowledge base before saying that it will ask the cosmetologist.
- Handoff is reserved for photos, explicit human requests, complaints/risk, missing legal/medical authority, or cases where the tool data is insufficient.
- Do not dump full price lists into chat unless the client asks for the whole price list; answer the exact question first, then ask the next booking question.
- Treat unclear questions as a reason to inspect listing context and knowledge, not as a reason to stop.
- Use the exact Avito listing title and price when the client asks about the price of that listing.
- Use `knowledge.*` before escalation for common medical/product/service questions such as contraindications, pregnancy, breastfeeding, effect duration, calculation by ml/units, and unclear price calculations.

Required behavior:

- Identify the city before offering appointment slots.
- Answer common questions about services, prices, preparation, contraindications, duration, and aftercare.
- Check free time in YCLIENTS for the requested city/service.
- Create a YCLIENTS appointment after the client confirms the slot and contact details.
- Notify or hand off to the cosmetologist with the lead context and contact.
- If the client sends a photo, stop automated diagnosis and route to the cosmetologist for individual consultation.
- Deduplicate webhook events and avoid double replies in the same chat.

Inputs:

- Avito webhook events from two accounts.
- Avito chat/listing context.
- Exported historic Avito conversations.
- YCLIENTS services, staff schedule, available slots, clients, and appointments.

Outputs:

- Avito replies.
- YCLIENTS client records and appointments.
- Telegram handoff notification to the cosmetologist/admin chat.

## Telegram Administrator

The administrator is a bridge between the cosmetologist and YCLIENTS.

Required behavior:

- Accept text and voice commands in Telegram.
- Create appointments by date, time, city/service, and client identity.
- Move appointments to another date/time/city where valid.
- Delete or cancel appointments.
- Search and edit client records.
- Add client notes such as skin type, reaction, preferences, contraindications, product recommendations, and follow-up timing.
- Confirm risky changes before applying them.

Inputs:

- Telegram messages and voice transcription.
- YCLIENTS client database, schedule, services, and appointments.

Outputs:

- Mutations in YCLIENTS.
- Confirmation messages with the final appointment/client state.

## Care / Upsell Bot

The upsell bot acts as a care department, not a cold sales bot.

Required behavior:

- Read visit history and client notes from YCLIENTS.
- After a procedure, use the cosmetologist's notes, especially skin type, to recommend suitable cosmetics.
- Schedule follow-up recommendations, for example Wednesday after a Monday visit.
- Recommend next procedures based on the previous procedure and the recommended interval.
- If the client wants to buy or book, route to the cosmetologist or create a booking flow.
- Avoid messaging clients with missing consent, complaints, unresolved handoffs, or insufficient data.

Inputs:

- YCLIENTS appointments and visit history.
- Client notes from the cosmetologist.
- Upsell rules mapping procedure to product/service recommendations and timing.

Outputs:

- Telegram/client bot messages.
- Handoff notifications.
- Optional new YCLIENTS bookings.

## Integration Staging

Legacy integration code was ported or retired and then archived outside the active project tree at `/root/AutomaticCosmetic_archives/20260529_full_migration/`. It must not be treated as the active application.

Useful legacy pieces:

- Avito webhook app and context resolver.
- YCLIENTS booking provider and integration event app.
- Conversation orchestrator and client consultant agent.
- Memory/event deduplication services.
- VK bot code as an extra channel reference.

## Immediate Engineering Plan

1. Build shared configuration and typed domain models in the main package. Done: `src/freelance_leads_bot/integrations`.
2. Port a minimal YCLIENTS client wrapper with dry-run friendly interfaces. Started: `DryRunYClientsGateway`.
3. Port Avito webhook handling behind a service boundary.
4. Add Telegram admin command core and confirmation flow. Started: `TelegramAdminService`.
5. Add shared tool layer so every bot can use CRUD operations instead of being trapped in prompts. Started: `AutomationToolbox`.
6. Add care/upsell rule storage and a scheduler.
7. Add tests around parsing, booking decisions, deduplication, tool calls, and handoff rules.

## Implemented Foundation

- `IntegrationSettings` reads the shared `.env`, including `YCLIENTS_PUBLIC_BASE_URL=https://olgatihcosmo.com`.
- `DryRunYClientsGateway` provides a safe in-memory YCLIENTS-shaped gateway for development.
- `YClientsHttpGateway` can read live YCLIENTS services, available times, clients, and records through the official API shape.
- Live YCLIENTS mutations are guarded by `YCLIENTS_ALLOW_MUTATIONS=false` by default.
- `AvitoBookingFlow` covers the first booking slice:
  - photo messages become human handoff;
  - missing city asks for city;
  - selected city/service/date returns available slots;
  - selected slot plus phone creates a dry-run appointment and returns a handoff-friendly confirmation.
- `src.freelance_leads_bot.integrations.avito_webhook:app` exposes:
  - `GET /health`;
  - `POST /avito/webhook?token=<AVITO_WEBHOOK_SECRET>`.
- `AvitoConsultant` is now the response layer in front of the webhook:
  - prepares `AvitoAgentContext` for a Codex-powered planner;
  - gives Codex the full message/listing context, all available CRUD tools, editable knowledge, and tool schemas;
  - does not use a separate intent detector, preselected candidate tools, or pre-Codex handoff gate; when Codex is enabled, Codex decides replies, tool calls, and handoffs from the full toolbox/context;
  - supports `CodexToolLoopPlanner`, where Codex can request tool calls, inspect tool results, call more tools, and only then return the final client reply;
  - materializes Codex handoff decisions when Codex returns `handoff_reason` (`photo_consultation`, `complaint_or_risk`, `human_requested`, `booking_ambiguous`, or `missing_data`);
  - writes redacted planner/tool-loop audit records to `data/agent_trace.jsonl` when Codex planning is enabled;
  - can be enabled in the webhook with `AVITO_CODEX_ENABLED=true`;
  - uses `CodexPlannerRunner` to call the local Codex CLI through the project `chat_with_codex` helper and parse JSON tool-loop steps;
  - asks Codex to route photos and complaint/risk cases to human handoff; deterministic routing is only a fallback when Codex planning is disabled;
  - sends booking-shaped messages into `AvitoBookingFlow`;
  - answers listing price questions from Avito listing context;
  - checks editable knowledge before saying that the cosmetologist must answer;
  - keeps replies focused on the client's exact question instead of dumping the whole service list.
- `run_avito_webhook.sh` starts the Avito webhook locally on `127.0.0.1:8030` by default.
- The webhook validates the token, ignores non-message events, deduplicates by `chat_id:message_id`, parses city/date/time/phone, and returns the reply that will later be sent to Avito.
- `PreviewAvitoSender` writes every outgoing Avito reply to `data/avito_outbox.jsonl` while `AVITO_SEND_ENABLED=false`.
- `PreviewHandoffNotifier` writes photo/risk/manual handoffs to `data/handoff_outbox.jsonl` while `HANDOFF_NOTIFY_ENABLED=false`.
- `TelegramHandoffNotifier` can notify the cosmetologist/admin chat when `HANDOFF_NOTIFY_ENABLED=true`, `HANDOFF_NOTIFY_CHAT_ID` is set, and the admin bot token is present.
- `JsonlAgentTraceLogger` writes Codex planner payloads, tool traces, and outcomes to `data/agent_trace.jsonl` with phone numbers and secret-like fields redacted.
- `AvitoSdkSender` uses Avito messenger endpoint `POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/messages` when `AVITO_SEND_ENABLED=true`.
- `TelegramAdminService` provides the first Telegram administrator core:
  - parses text commands for adding, moving, cancelling appointments, and updating client notes;
  - creates appointments only after matching city, service, date, time, phone, and a free YCLIENTS slot;
  - moves/cancels appointments through the same `YClientsGateway`;
  - writes skin type and cosmetologist notes through `update_client_notes`.
- `TelegramAdminBotTransport` wires the admin core to Telegram:
  - accepts text commands from allowed admin/cosmetologist user IDs;
  - transcribes Telegram voice/audio through the existing media recognition layer; by default this uses local `faster-whisper` (`TRANSCRIBE_PROVIDER=faster-whisper`);
  - sends the final YCLIENTS operation result back to the admin chat.
- `run_telegram_admin_bot.sh` starts the admin bot polling loop.
- `prelaunch` provides a non-mutating readiness report before each launch stage:
  - `python -m src.freelance_leads_bot.integrations.prelaunch`;
  - optional read-only YCLIENTS probe: `python -m src.freelance_leads_bot.integrations.prelaunch --probe-yclients`.
- VK is wired as the next staged channel:
  - `VK_GROUP_TOKEN` and `VK_GROUP_ID=225170792` identify the group;
  - incoming VK Long Poll events are converted to the shared `InboundMessage` model;
  - VK replies use the same `AvitoConsultant`/`AutomationToolbox`/Codex tool-loop path as Avito when `VK_CODEX_ENABLED=true`;
  - `PreviewVKSender` writes to `data/vk_outbox.jsonl` while `VK_SEND_ENABLED=false`;
  - `run_vk_bot.sh` starts the VK Long Poll preview/live worker depending on `VK_SEND_ENABLED`.
- `AutomationToolbox` exposes shared tools for all bot surfaces:
  - publishes tool schemas with descriptions, required arguments, mutation flags, external-system flags, and guardrail notes;
  - validates unknown tools and missing required arguments before execution;
  - `yclients.services.list`;
  - `yclients.slots.list`;
  - `yclients.appointments.create`;
  - `yclients.appointments.list`;
  - `yclients.appointments.move`;
  - `yclients.appointments.cancel`;
  - `yclients.clients.search`;
  - `yclients.clients.notes.update`;
  - `care.tasks.plan`;
  - `knowledge.create/list/get/update/delete`.
- `JsonKnowledgeStore` gives the bots CRUD storage for FAQs, successful answer examples, service notes, pricing clarifications, contraindication snippets, and conversation lessons.
- `avito_history_import` imports Telegram/Avito-style HTML exports into `JsonKnowledgeStore` with phone numbers, Avito chat ids, and long numeric identifiers redacted.
- `ChatExport_2026-05-26.zip` has been imported into `data/bot_knowledge.json`: 198 messages produced 26 knowledge examples for the Avito consultant.
- `due_upsell_rules` evaluates post-visit product/service recommendations by procedure, delay, and skin type.
- `CareUpsellPlanner` turns due rules into care tasks:
  - product recommendations after a visit, using the client's skin type from YCLIENTS notes;
  - next-service recommendations after the configured interval;
  - handoff flags when the client replies that they want to buy or book.
- `CareUpsellService` reads YCLIENTS appointments over a date range and returns due care tasks, so the scheduler/client bot can work from actual visit history instead of manual inputs.

The current booking flow is wired to a local Avito webhook endpoint. Sending replies back into Avito is guarded by `AVITO_SEND_ENABLED=false`; while disabled, replies are written to preview outbox instead of sent. Codex planning is controlled by `AVITO_CODEX_ENABLED`; preview launch should keep Codex enabled and Avito sending disabled. Live YCLIENTS mutation methods exist behind an explicit safety flag and should stay disabled until the confirmation UX is reviewed. Telegram admin transport exists for text and voice. VK is ready for staged preview but should stay on `VK_SEND_ENABLED=false` until Avito/YCLIENTS are stable.
