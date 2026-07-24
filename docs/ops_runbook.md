# AutomaticCosmetic ops runbook

Короткий эксплуатационный чеклист для живого бота.

## Быстрая проверка

```bash
cd /root/AutomaticCosmetic
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status
```

Обычный режим возвращает exit `0`, если есть только warning. Это удобно для ручной проверки: warning может означать рабочий хвост, а не аварию.

Для cron/systemd alert используй strict-режим:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status --strict
```

`--strict` возвращает ненулевой exit-code при любом warning или error.

JSON для автоматизации:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status --json
```

## Как читать статус

- `systemd_services`: все runtime-сервисы должны быть `active`.
- `avito_health`: Avito webhook должен быть жив, Avito credentials и handoff-уведомления готовы.
- `yclients_integration_health`: YCLIENTS integration health endpoint должен отвечать `ok`.
- `avito_unanswered_queue`: delayed Avito monitor нашёл диалоги, где клиент ждёт ответа.
- `avito_unanswered_report_fresh`: monitor жив и обновляет отчёт.
- `avito_autoreply_failures`: failed delayed auto-reply попытки. Это error.
- `expert_rag`: есть ли approved RAG-знания.
- `expert_rag_needs_review`: есть знания, которые бот не должен использовать сам, пока их не подтвердили.
- `expert_rag_temporal_cleanup`: approved-знания с датами, временем, окнами, адресами, акциями или конкретными договорённостями без expiry.
- `data_footprint`: размер `data/` и крупнейшие файлы/директории.
- `disk_free_space`: свободное место на диске.

В строке `RAG` поле `high_risk_approved` показывает approved-знания с медицинским/рисковым контекстом. Поле `excluded_from_avito_autoanswer` должно совпадать с ним: такие знания не передаются в Avito autoanswer/planner context и остаются только для контролируемого review/аналитики.

Поля `temporal_without_expiry` и `temporal_needs_cleanup` нужны для старых дат/окон/адресов: такие знания должны быть либо `autoanswer_allowed=false`, либо иметь `expires_at`/`valid_until`. Новые временные ответы Ольги сохраняются как память, но автоматически блокируются от autoanswer без expiry.

Почистить старые временные approved-знания безопасно через dry-run:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review temporal-cleanup
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review temporal-cleanup --apply
```

## Текущий нормальный WARN

Если видишь:

```text
expert_rag_needs_review: N expert RAG items need review
```

это не авария. Это очередь экспертных знаний, которые нужно явно подтвердить или устарить.

Сформировать Markdown для проверки:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review export --output data/expert_rag_review.md
```

В export/list/show есть `Review suggestion`. Это только подсказка, не решение:

- `needs_edit` обычно означает, что в ответе есть цена, мл, срок эффекта, старый handoff-контекст или слишком частный случай; такой текст нельзя blindly approve.
- `approve as-is` ставь только если Ольга подтвердила, что формулировка безопасна и пригодна для повторного использования.
- `deprecate` ставь для устаревших цен, разовых ответов или знаний, которые не стоит превращать в память.

Открой `data/expert_rag_review.md` и отметь ровно один чекбокс у каждого знания, которое готово к решению:

```markdown
- [x] approve #158 as-is
- [ ] deprecate #158
- [ ] needs edited answer for #158
```

Потом обязательно проверь файл dry-run командой. Она ничего не меняет в базе:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review decisions data/expert_rag_review.md
```

Если dry-run показывает только правильные `approve/deprecate` решения и нет `Conflicts`, `Missing items` или `Needs edited answer`, можно применить:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review decisions data/expert_rag_review.md --apply --by olga
```

`needs edited answer` не применяется автоматически: сначала нужно вручную отредактировать клиентский ответ/знание, затем снова прогнать review.

Посмотреть список в консоли:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review list
```

Посмотреть конкретную запись:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review show 158
```

Перед реальным изменением сначала делай dry-run:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review approve 158 --by olga --dry-run
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review deprecate 158 --dry-run
```

Реальное подтверждение/устаревание:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review approve 158 --by olga
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review deprecate 158
```

Все реальные `approve/deprecate` пишутся в audit-log:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.expert_rag_review audit --limit 20
```

## Dynamic RAG / каталог услуг

Dynamic RAG включается флагами:

```bash
RAG_DYNAMIC_INTENT_ENABLED=true
RAG_SERVICE_CATALOG_ENABLED=true
RAG_SHARED_RETRIEVAL_ENABLED=true
```

Быстрый rollback без отката кода — выставить любой из этих флагов в `false`.

Первичное заполнение каталога услуг:

```bash
.venv/bin/python scripts/service_catalog_admin.py seed
```

Dry-run миграции существующих approved RAG-знаний к `service_key`:

```bash
.venv/bin/python scripts/service_catalog_admin.py migration-plan > data/service_catalog_migration_plan.json
```

Применять только после просмотра JSON-плана:

```bash
.venv/bin/python scripts/service_catalog_admin.py migration-apply --plan data/service_catalog_migration_plan.json
```

LLM-понимание свободных команд Ольги использует `OPENROUTER_API_KEY`, `DEFAULT_MODEL` и `RAG_INTENT_LLM_TIMEOUT_SECONDS`. Если LLM недоступен или вернул невалидный JSON, бот автоматически использует безопасный fallback parser и пишет `parser_source=fallback` в metadata плана.

## Что делать при Avito warning/error

### `avito_unanswered_queue`

Есть диалоги, где клиент ждёт ответа.

Проверить JSON:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status --json
```

Смотреть `checks[].name == "avito_unanswered_queue"` и `data.actionable_count`.

В Telegram открыть рабочий список:

```text
/avito_followups
```

Если `TELEGRAM_CLIENT_TOPICS_ENABLED=true`, бот создаёт отдельную Telegram-тему под каждый Avito-диалог и сохраняет связь в `TELEGRAM_CLIENT_TOPICS_PATH` (`data/telegram_client_topics.json` по умолчанию). Карточка и фото клиента отправляются в эту тему; если Telegram не дал создать тему, бот молча откатывается к обычной отправке в основной чат.

По каждой карточке нужно выбрать действие:

- `Закрыто` — клиент уже получил финальный ответ.
- `Не актуально` — обещание больше не требует ответа клиенту.
- `Напомнить позже` — временно убрать из повторных уведомлений.

Если клиент пишет про запись, адрес, оплату, перенос или “я записана, всё в силе?”, это считается критичным: задачу нельзя закрывать обещанием “уточню”, нужен финальный ответ клиенту или явное решение Ольги.

### `telegram_open_handoffs`

`ops_status` отдельно проверяет открытые Telegram-карточки Ольги из `data/telegram_handoff_refs.json`. Проверка read-only: запуск статуса не чинит и не перезаписывает этот файл.

Нерешённые статусы: `open`, `in_progress`, `draft_pending`, `rejected`, `expired_critical`.

Критичными считаются карточки про запись, перенос, отмену, адрес, подтверждение даты/времени, вопрос “в силе?”, фото/вложения, voice, жалобу, негативный отзыв или медицинский вопрос. Если critical handoff старше 1 часа, это warning; старше 3 часов — error. Обычный handoff старше 24 часов — warning, старше 48 часов — error. `expired_critical` всегда error.

Если `ops_status` пишет `Immediate action required: review open Olga handoffs.`, нужно открыть `/open_cards` или тему клиента и дать клиенту финальный ответ/закрыть карточку с понятной причиной. Массово закрывать старые карточки без проверки последнего входящего и исходящего Avito нельзя.

Для спокойного ручного разбора текущих хвостов можно сделать read-only экспорт. Команда не чинит и не переписывает `data/telegram_handoff_refs.json`, а собирает список открытых карточек с критичностью, возрастом, Avito chat_id и последними найденными входящими/исходящими из логов:

```bash
.venv/bin/python scripts/export_open_handoffs.py --output data/open_handoffs_review.md
```

Дальше по каждому пункту в `data/open_handoffs_review.md` нужно открыть Avito/тему клиента, проверить последний входящий и исходящий, затем закрыть карточку только с понятным результатом: клиент получил финальный ответ, Ольга обработала вручную, вопрос явно неактуален или задача всё ещё требует действия.

### `avito_pending_followups`

Это зависшие обещания бота после фраз вроде “уточню”, “проверю”, “подтвержу”.

Если есть critical followup, `ops_status` даёт минимум warning даже до просрочки. Если critical/overdue обещание старше `AVITO_OVERDUE_PROMISE_ERROR_AFTER_SECONDS`, `ops_status --strict` возвращает error. По умолчанию SLA — 3 часа.

Проверить хвосты:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status --json
journalctl -u yclients-avito-unanswered-monitor.service -n 200 --no-pager
```

После ручного ответа в Avito монитор должен запомнить исходящее сообщение и закрыть открытую задачу, если ответ похож на финальный.

Если нажали `Закрыто` по критичной карточке, а монитор не видит исходящего ответа клиенту после создания задачи, карточка получает статус `closed_manual_no_client_reply`. Это не дёргает Ольгу повторно, но остаётся warning в `ops_status`: такой случай нужно отдельно проверить в Avito.

Фото и вложения из Avito бот пересылает в тему клиента. Статусы хранятся в `data/telegram_handoff_refs.json`: `received`, `downloaded`, `sent_to_olga`, `download_failed`, `manual_avito_check_required`. Если фото/файл не удалось переслать после retry, Ольга получает текстовую карточку “открыть Avito и проверить вложение вручную”.

Голосовые сообщения расшифровываются webhook/missed-poller. Если расшифровка упала, сообщение не считается обработанным молча: создаётся handoff Ольге, а при падении handoff сообщение остаётся retryable.

Missed-poller должен проверять минимум 150 последних чатов:

```env
AVITO_POLLER_CHAT_LIMIT=150
AVITO_POLLER_MESSAGES_PER_CHAT=50
```

Если эти переменные не заданы, poller использует такие же production-defaults и проходит чаты страницами через `offset`.

### `avito_unanswered_report_fresh`

Monitor может зависнуть или перестать обновлять отчёт.

Проверить сервис:

```bash
systemctl status yclients-avito-unanswered-monitor.service --no-pager
journalctl -u yclients-avito-unanswered-monitor.service -n 100 --no-pager
```

### `avito_autoreply_failures`

Это error: delayed auto-reply попытки падали.

Проверить:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status --json
journalctl -u yclients-avito-unanswered-monitor.service -n 200 --no-pager
```

## Проверка визитов и допродажи

Вечерние карточки визитов отправляет timer:

```bash
systemctl status yclients-visit-confirmations.timer --no-pager
systemctl list-timers --all yclients-visit-confirmations.timer --no-pager
```

Ручной запуск:

```bash
.venv/bin/python scripts/send_visit_confirmations.py --date 2026-07-22 --force --no-quiet-empty
```

В Telegram можно запросить карточки командой:

```text
/visit_confirmations
/care_followups
```

Follow-up клиентам отправляется только если включён `TELEGRAM_CLIENT_FOLLOWUP_SEND_ENABLED`. До проверки черновиков держать этот флаг выключенным.

Правила отдела заботы:

- После подтверждённого визита follow-up можно готовить даже при `consent_status=unknown`, если нет явного запрета.
- `do_not_contact` или `consent_status=denied` полностью блокируют отправку клиенту.
- `complaint_risk`, risk-level `high/blocked` или спорный риск — полный стоп: клиенту не пишем, оставляем задачу Ольге.
- Если у клиента нет verified Telegram-связки, `/care_followups` показывает задачу найти канал связи или не писать, а не готовую отправку.
- Ответ Ольги текстом на care-карточку заменяет черновик и сохраняется как урок тона для будущих follow-up.

Полезные ops-сигналы:

- `care_visit_details` — есть визиты в `needs_details` дольше 24 часов.
- `care_followup_channels` — due-задачи есть, но нет verified Telegram-канала.
- `care_followup_risk_gate` — live-отправка включена, а в очереди есть риск-блокированные задачи.

## YCLIENTS webhook secret

В production `YCLIENTS_INTEGRATION_SECRET` должен быть непустым. `/health` обязан показывать:

```json
{"secret_required": true}
```

POST на `/yclients/webhook` и `/yclients/callback` без `X-YCLIENTS-Signature`/`X-YClients-Webhook-Secret` должен возвращать `403`. GET/HEAD probe остаётся открытым для health-check.

Если `ops_status` показывает `yclients_webhook_secret` как error:

```bash
grep -n '^YCLIENTS_INTEGRATION_SECRET=' .env
systemctl restart yclients-yclients-integration.service
curl -s http://127.0.0.1:8020/health
```

Секрет не писать в логи, PR и скриншоты.

Webhook/YCLIENTS uvicorn-сервисы должны запускаться с `--no-access-log`, потому что Avito token и YCLIENTS secret приходят в query params и иначе могут попасть в journal. `/health` отдаёт только redacted integration URLs, а `ops_status --json` дополнительно маскирует поля `secret`, `token`, `key`, `access_token`.

## Backup и restore

Ежедневный backup:

```bash
sudo cp deploy/systemd/automaticcosmetic-backup.service /etc/systemd/system/
sudo cp deploy/systemd/automaticcosmetic-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now automaticcosmetic-backup.timer
```

Ручной запуск:

```bash
.venv/bin/python scripts/backup_runtime_data.py --data-dir data --output-dir backups --env-path .env --retention-days 14
```

SQLite копируется через безопасный sqlite backup API в `backups/sqlite-*/`. JSON-state и `.env` архивируются отдельно в `backups/runtime-json-env-*.tar.gz` с правами `0600`.

Проверка restore без записи в live `data/`:

```bash
.venv/bin/python scripts/verify_runtime_backup.py --backup-dir backups --restore-dir /tmp/automaticcosmetic-restore-check
```

Команда распаковывает `runtime-json-env-*.tar.gz` в отдельную папку, копирует SQLite из `sqlite-*` туда же и прогоняет `PRAGMA integrity_check`. Если нужно проверить конкретную дату, укажи stamp:

```bash
.venv/bin/python scripts/verify_runtime_backup.py --backup-dir backups --stamp YYYYMMDDTHHMMSSZ --restore-dir /tmp/automaticcosmetic-restore-check
```

Backup содержит `.env` и может содержать `data/mfa_totp.json`, то есть внутри есть реальные секреты. Такой архив нельзя отправлять подрядчикам, в GitHub issue или в чат без шифрования/очистки.

Реальный restore в production делать только после успешной проверки выше:

```bash
systemctl stop freelance-leads-bot.service \
  yclients-avito-webhook.service \
  yclients-avito-missed-poller.service \
  yclients-avito-unanswered-monitor.service \
  yclients-yclients-integration.service \
  yclients-visit-confirmations.timer \
  yclients-visit-confirmations.service
cp backups/sqlite-YYYYMMDDTHHMMSSZ/*.sqlite3 data/
tar -xzf backups/runtime-json-env-YYYYMMDDTHHMMSSZ.tar.gz -C /root/AutomaticCosmetic
systemctl start yclients-yclients-integration.service \
  yclients-avito-webhook.service \
  yclients-avito-missed-poller.service \
  yclients-avito-unanswered-monitor.service \
  freelance-leads-bot.service
systemctl start yclients-visit-confirmations.timer
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status
```

Перед restore проверить, что архив именно нужной даты, и сохранить копию текущего `data/`/`.env` отдельно, если есть риск отката не туда.

## Log rotation

Шаблон лежит в `deploy/logrotate/automaticcosmetic`.

```bash
sudo cp deploy/logrotate/automaticcosmetic /etc/logrotate.d/automaticcosmetic
sudo logrotate -d /etc/logrotate.d/automaticcosmetic
```

Ротация касается только `*.log`, `*.jsonl`, trace/audit/outbox. Она не трогает SQLite, client topics, handoff refs и state JSON.

## Что делать при data/disk warning

Проверить крупнейшие элементы:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status --json
du -h data/* 2>/dev/null | sort -h | tail -20
df -h .
```

Не удаляй данные вслепую. Особенно аккуратно с:

- `data/leads.sqlite3`
- `data/expert_rag.sqlite3`
- `data/telegram_handoff_refs.json`
- `data/telegram_client_topics.json`
- `data/avito_processed_events.json`
- `data/avito_unanswered_monitor_state.json`

Логи и trace обычно безопаснее архивировать/ротировать, чем удалять базы.

## Полная проверка после изменений

```bash
cd /root/AutomaticCosmetic
.venv/bin/python -m pytest -q
scripts/live_smoke_check.sh
```
