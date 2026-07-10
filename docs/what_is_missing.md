# What Is Missing

У проекта уже есть рабочая основа операционной системы для косметолога:

- основной Telegram-бот;
- Telegram admin transport с текстом и голосом;
- Avito webhook;
- Avito sender в preview-режиме;
- YCLIENTS gateway с live-read и защищенными mutations;
- общий `AutomationToolbox` со схемами tools, required-полями и guardrails;
- база знаний `JsonKnowledgeStore`;
- care/upsell planner;
- Codex tool loop planner с trace вызовов, результатов tools и редактированным JSONL-журналом;
- Codex-first принятие решений: при включенном Codex ни фото, ни риски, ни записи не решаются до Codex; приложение только исполняет выбранные Codex tools/handoff;
- импортер Avito/Telegram-истории в knowledge;
- тестовый контур, который сейчас проходит.

## Не доделано до production

- Провести контрольный тест на реальных Avito-сценариях перед включением `AVITO_SEND_ENABLED=true`.
- Проверить UX подтверждений перед включением `YCLIENTS_ALLOW_MUTATIONS=true`.
- Прогнать `python -m src.freelance_leads_bot.integrations.prelaunch` перед каждым переключением live-флагов.
- Прогнать VK в preview через `run_vk_bot.sh`; live-отправку включать только отдельным `VK_SEND_ENABLED=true`.
- Довести care/upsell до полноценного фонового цикла отправки клиентам с учетом согласий, жалоб и ручных стоп-флагов.
- Наполнить базу знаний реальными услугами, ценами, противопоказаниями, подготовкой и aftercare.
- Проверить голосовые команды Telegram-администратора на реальных формулировках косметолога.
- Описать регламент ручного handoff: фото, жалобы, медицинские риски, нестандартные вопросы, спорные записи.
- Подготовить production-настройки токенов, allowed user ids, webhook URL и мониторинг логов.

## Ближайший практичный шаг

1. Прогнать 10-20 реальных Avito-диалогов в preview-режиме.
2. Сверить ответы с косметологом и поправить knowledge.
3. Прогнать создание/перенос/отмену записей в YCLIENTS сначала в dry-run, потом на тестовой записи.
4. Только после этого включать live-отправку в Avito и live-mutations в YCLIENTS.
5. После стабилизации Avito/YCLIENTS включать VK: сначала preview-outbox, затем `VK_SEND_ENABLED=true`.
