# Automatic Cosmetic

Операционная система для косметолога: Avito-консультант, Telegram-администратор и контур заботы/допродаж поверх YCLIENTS.

## Зачем нужен бот

Он снимает рутину с трех мест:

- **Avito**: отвечает клиентам, уточняет город, услугу и время, смотрит YCLIENTS и готовит запись.
- **Telegram для косметолога**: можно голосом или текстом добавлять, переносить, отменять записи и вести заметки по клиентам.
- **Забота и допродажи**: после визитов бот сможет предлагать уход, косметику или следующую услугу по истории и заметкам в YCLIENTS.

## Как устроен

Под капотом логика строится вокруг Codex и инструментов. Агент получает контекст и набор tools:

- посмотреть услуги YCLIENTS;
- посмотреть свободные слоты;
- создать, перенести или отменить запись;
- найти клиента;
- обновить заметки клиента;
- читать, создавать и редактировать базу знаний;
- планировать задачи заботы и допродаж.

Codex должен сам решить, какие инструменты вызвать. Старые эвристики оставлены как fallback, если Codex выключен.

## Что уже сделано

- Единый `.env` для ключей и секретов.
- YCLIENTS gateway с live-read и защищенными mutations.
- Avito webhook.
- Avito sender с preview-режимом, чтобы не отправлять ответы вживую случайно.
- Telegram admin bot transport с текстом и голосом.
- Общий `AutomationToolbox` для CRUD со схемами tools, required-полями и guardrails для внешних мутаций.
- База знаний `JsonKnowledgeStore`.
- Care/upsell planner.
- Codex tool loop planner с trace событий `tool_call` / `tool_result` и редактированным JSONL-журналом `data/agent_trace.jsonl`.
- Импортер выгрузки чатов в knowledge с редактированием телефонов/chat_id.

## Текущее состояние

Основной Telegram-бот запущен, через него можно логиниться в Codex командой `/codex_login`.

Avito и YCLIENTS live-мутации стоят на предохранителях: ответы в Avito и изменения в YCLIENTS не надо включать до контрольного теста на реальных сценариях.

Telegram account/MTProto listener намеренно отключен в portable bot-only сборке.

## Запуск

```bash
cd /root/AutomaticCosmetic
./setup_env.sh
./run_bot.sh
```

Проверка:

```bash
cd /root/AutomaticCosmetic
.venv/bin/python -m compileall -q src scripts
.venv/bin/python -m pytest -q
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status
```

## Команды Telegram

- `/menu` - открыть панель управления;
- `/status` - состояние и статистика;
- `/help` - список команд;
- `/codex_login` - вход в Codex через Telegram-бота.

## Важные документы

- [docs/cosmetology_automation_spec.md](/root/AutomaticCosmetic/docs/cosmetology_automation_spec.md) - продуктовая спецификация.
- [DEVELOPMENT.md](/root/AutomaticCosmetic/DEVELOPMENT.md) - заметки по текущей разработке.
- [docs/parity_report_2026-05-29.md](/root/AutomaticCosmetic/docs/parity_report_2026-05-29.md) - отчет по переносу legacy-интеграций в новый Codex runtime.

## systemd

```bash
sudo cp /root/AutomaticCosmetic/freelance-leads-bot.service.example /etc/systemd/system/freelance-leads-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now freelance-leads-bot
sudo journalctl -u freelance-leads-bot -f
```

Боевые Avito/YCLIENTS сервисы проверяются одной командой:

```bash
cd /root/AutomaticCosmetic
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status
```

В отчёте важны поля:

- `ok`: общий эксплуатационный статус;
- `summary.avito_actionable`: сколько Avito-диалогов реально ждут действия;
- `summary.avito_autoreply_failed`: сколько delayed auto-reply попыток упали;
- `checks.systemd_services`: живы ли runtime-сервисы;
- `checks.expert_rag`: есть ли approved RAG-знания.
