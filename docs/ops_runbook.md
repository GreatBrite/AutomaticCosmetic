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
- `data_footprint`: размер `data/` и крупнейшие файлы/директории.
- `disk_free_space`: свободное место на диске.

В строке `RAG` поле `high_risk_approved` показывает approved-знания с медицинским/рисковым контекстом. Поле `excluded_from_avito_autoanswer` должно совпадать с ним: такие знания не передаются в Avito autoanswer/planner context и остаются только для контролируемого review/аналитики.

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

### `avito_pending_followups`

Это зависшие обещания бота после фраз вроде “уточню”, “проверю”, “подтвержу”.

Если обещание просрочено дольше `AVITO_OVERDUE_PROMISE_ERROR_AFTER_SECONDS`, `ops_status --strict` возвращает error. По умолчанию SLA — 3 часа.

Проверить хвосты:

```bash
.venv/bin/python -m src.freelance_leads_bot.integrations.ops_status --json
journalctl -u yclients-avito-unanswered-monitor.service -n 200 --no-pager
```

После ручного ответа в Avito монитор должен запомнить исходящее сообщение и закрыть открытую задачу, если ответ похож на финальный.

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
