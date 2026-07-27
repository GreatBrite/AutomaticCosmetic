from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import ROOT


DEFAULT_OLGA_MANUAL_TASKS_PATH = ROOT / "data" / "olga_manual_tasks.json"
DEFAULT_REMINDER_INTERVAL_SECONDS = 3 * 60 * 60
OPEN_STATUSES = {"open", "needs_help"}
OLGA_TASK_ACTIONS = {"done", "stale", "help", "later"}


DEFAULT_OLGA_MANUAL_TASKS: tuple[dict[str, str], ...] = (
    {
        "task_id": "avito_clients",
        "topic": "avito_clients",
        "title": "Avito: зависшие клиенты",
        "message_text": (
            "Ольга, нужно вручную проверить зависшие диалоги в Avito.\n\n"
            "Зачем: если клиенту бот написал «уточню», «проверю», «передам Ольге», клиент ждёт финальный ответ. "
            "Просто закрыть карточку в Telegram недостаточно.\n\n"
            "Что сделать:\n"
            "- открыть Avito;\n"
            "- найти диалог по имени или chat_id;\n"
            "- проверить, получил ли клиент понятный финальный ответ;\n"
            "- если ответа не было — ответить клиенту;\n"
            "- после этого нажать «Готово»;\n"
            "- если клиент уже неактуален — нажать «Не актуально»."
        ),
    },
    {
        "task_id": "cities_schedule",
        "topic": "cities_schedule",
        "title": "Города и расписание",
        "message_text": (
            "Ольга, нужно подтвердить рабочие города и правило ответа по неподдерживаемым городам.\n\n"
            "Зачем: бот не должен обещать приём в городе, где ты сейчас не принимаешь.\n\n"
            "Нужно подтвердить:\n"
            "- где точно можно записывать клиентов;\n"
            "- какие города нельзя обещать;\n"
            "- что писать клиенту, если он спрашивает про другой город.\n\n"
            "Текущий безопасный вариант: «Сейчас приём ведём только в Ростове-на-Дону, Москве, "
            "Санкт-Петербурге, Краснодаре и Геленджике. Если один из них удобен, подскажу по записи.»"
        ),
    },
    {
        "task_id": "prices_services",
        "topic": "prices_services",
        "title": "Цены и услуги",
        "message_text": (
            "Ольга, нужно подтвердить, какие цены бот может говорить клиентам автоматически.\n\n"
            "Зачем: если цена старая или зависит от зоны, объёма или модели, бот не должен называть её как факт.\n\n"
            "Что нужно:\n"
            "- подтвердить актуальные цены;\n"
            "- отметить услуги, где цена всегда индивидуальная;\n"
            "- отдельно сказать, можно ли использовать цены из Avito-объявлений;\n"
            "- если цена неизвестна, бот будет писать: «Стоимость сверю и вернусь с ответом.»"
        ),
    },
    {
        "task_id": "photo_expert",
        "topic": "photo_expert",
        "title": "Фото и экспертные вопросы",
        "message_text": (
            "Ольга, нужно подтвердить правило по фото, объёмам и ожидаемому результату.\n\n"
            "Зачем: бот не должен обещать «300 мл даст +1 размер», «будет заметный результат» "
            "или оценивать эффект без тебя.\n\n"
            "Правило по умолчанию:\n"
            "- если клиент спрашивает про результат, объём, размер, до/после или присылает фото для оценки — бот передаёт тебе;\n"
            "- клиенту пишет только безопасно: «По объёму и ожидаемому результату лучше не обещать вслепую. "
            "Передам Ольге, она посмотрит и сориентирует точнее.»"
        ),
    },
    {
        "task_id": "old_knowledge",
        "topic": "old_knowledge",
        "title": "Старые знания бота",
        "message_text": (
            "Ольга, нужно проверить старые ответы, которые бот мог использовать как знания.\n\n"
            "Зачем: там могут быть старые даты, акции, адреса, цены и разовые договорённости. "
            "Если бот возьмёт это как актуальный факт, он может соврать клиенту.\n\n"
            "Что сделать:\n"
            "- проверить список старых знаний;\n"
            "- отметить: «можно использовать», «устарело», «только как пример стиля», «нужно переписать»;\n"
            "- всё с датами, адресами, окнами записи и акциями без срока действия по умолчанию запрещаем для автоответов."
        ),
    },
    {
        "task_id": "closure_rules",
        "topic": "closure_rules",
        "title": "Правила закрытия задач",
        "message_text": (
            "Ольга, нужно соблюдать правило закрытия карточек.\n\n"
            "Карточку можно закрывать как «Готово», только если:\n"
            "- клиент реально получил финальный ответ;\n"
            "- или вопрос решён вручную;\n"
            "- или стало понятно, что отвечать уже не нужно.\n\n"
            "Если просто нажать «Готово», но клиенту не ответили, система оставит это как риск: "
            "«закрыто без ответа клиенту»."
        ),
    },
)


def load_olga_manual_tasks(path: Path | str = DEFAULT_OLGA_MANUAL_TASKS_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tasks": {}}
    if not isinstance(raw, dict):
        return {"tasks": {}}
    raw.setdefault("tasks", {})
    if not isinstance(raw["tasks"], dict):
        raw["tasks"] = {}
    return raw


def save_olga_manual_tasks(state: dict[str, Any], path: Path | str = DEFAULT_OLGA_MANUAL_TASKS_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ensure_default_olga_manual_tasks(
    path: Path | str = DEFAULT_OLGA_MANUAL_TASKS_PATH,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    state = load_olga_manual_tasks(path)
    tasks = state.setdefault("tasks", {})
    created: list[str] = []
    existing_topics = {
        str(row.get("topic") or "")
        for row in tasks.values()
        if isinstance(row, dict) and str(row.get("topic") or "")
    }
    for definition in DEFAULT_OLGA_MANUAL_TASKS:
        topic = definition["topic"]
        task_id = definition["task_id"]
        if topic in existing_topics or isinstance(tasks.get(task_id), dict):
            continue
        tasks[task_id] = {
            **definition,
            "status": "open",
            "created_at": now_ts,
            "created_at_iso": _iso(now_ts),
            "last_reminded_at": 0,
            "last_reminded_at_iso": "",
            "reminder_interval_seconds": DEFAULT_REMINDER_INTERVAL_SECONDS,
            "telegram_chat_id": "",
            "telegram_message_id": "",
            "telegram_message_thread_id": "",
            "closed_at": 0,
            "closed_at_iso": "",
            "closed_by": "",
            "close_reason": "",
            "olga_comment": "",
        }
        created.append(task_id)
        existing_topics.add(topic)
    if created:
        save_olga_manual_tasks(state, path)
    return {"ok": True, "created": created, "state": state}


def open_olga_manual_tasks(
    path: Path | str = DEFAULT_OLGA_MANUAL_TASKS_PATH,
    *,
    seed: bool = True,
) -> list[dict[str, Any]]:
    if seed:
        state = ensure_default_olga_manual_tasks(path)["state"]
    else:
        state = load_olga_manual_tasks(path)
    rows = [
        dict(row)
        for row in state.get("tasks", {}).values()
        if isinstance(row, dict) and str(row.get("status") or "open") in OPEN_STATUSES
    ]
    rows.sort(key=lambda row: (0 if row.get("status") == "needs_help" else 1, int(row.get("created_at") or 0), str(row.get("task_id") or "")))
    return rows


def due_olga_manual_tasks(
    path: Path | str = DEFAULT_OLGA_MANUAL_TASKS_PATH,
    *,
    now: int | None = None,
    seed: bool = True,
) -> list[dict[str, Any]]:
    now_ts = int(time.time()) if now is None else int(now)
    rows = open_olga_manual_tasks(path, seed=seed)
    due: list[dict[str, Any]] = []
    for row in rows:
        last = int(row.get("last_reminded_at") or 0)
        interval = int(row.get("reminder_interval_seconds") or DEFAULT_REMINDER_INTERVAL_SECONDS)
        if not last or now_ts - last >= max(60, interval):
            due.append(row)
    return due


def olga_manual_task_keyboard(task_id: str) -> dict[str, list[list[dict[str, str]]]]:
    task_id = str(task_id or "").strip()
    return {
        "inline_keyboard": [
            [
                {"text": "Готово", "callback_data": f"olgatask:{task_id}:done"},
                {"text": "Не актуально", "callback_data": f"olgatask:{task_id}:stale"},
            ],
            [
                {"text": "Нужна помощь", "callback_data": f"olgatask:{task_id}:help"},
                {"text": "Напомнить позже", "callback_data": f"olgatask:{task_id}:later"},
            ],
        ]
    }


def parse_olga_manual_task_callback(data: str) -> tuple[str, str] | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "olgatask":
        return None
    task_id = parts[1].strip()
    action = parts[2].strip()
    if not task_id or action not in OLGA_TASK_ACTIONS:
        return None
    return task_id, action


def format_olga_manual_task_card(row: dict[str, Any], *, reminder: bool = False) -> str:
    prefix = "Напоминание: задача для Ольги ещё открыта\n\n" if reminder else ""
    status = str(row.get("status") or "open")
    status_line = "Нужна помощь" if status == "needs_help" else "Открыта"
    return (
        f"{prefix}{row.get('title') or 'Задача для Ольги'}\n"
        f"Статус: {status_line}\n\n"
        f"{str(row.get('message_text') or '').strip()}\n\n"
        "Кнопки ниже нужны только для статуса задачи: закрыть, отметить неактуальной, попросить помощь или перенести напоминание."
    )


def apply_olga_manual_task_action(
    *,
    path: Path | str = DEFAULT_OLGA_MANUAL_TASKS_PATH,
    task_id: str,
    action: str,
    actor: str = "telegram_admin",
    now: int | None = None,
    olga_comment: str = "",
) -> dict[str, Any]:
    parsed = parse_olga_manual_task_callback(f"olgatask:{task_id}:{action}")
    if parsed is None:
        return {"ok": False, "reason": "invalid_callback"}
    task_id, action = parsed
    now_ts = int(time.time()) if now is None else int(now)
    state = load_olga_manual_tasks(path)
    tasks = state.setdefault("tasks", {})
    row = tasks.get(task_id) if isinstance(tasks.get(task_id), dict) else None
    if row is None:
        return {"ok": False, "reason": "task_not_found", "task_id": task_id}
    if action == "done":
        row.update(
            {
                "status": "done",
                "closed_at": now_ts,
                "closed_at_iso": _iso(now_ts),
                "closed_by": actor,
                "close_reason": "done",
            }
        )
    elif action == "stale":
        row.update(
            {
                "status": "not_relevant",
                "closed_at": now_ts,
                "closed_at_iso": _iso(now_ts),
                "closed_by": actor,
                "close_reason": "not_relevant",
            }
        )
    elif action == "help":
        row.update(
            {
                "status": "needs_help",
                "needs_help_at": now_ts,
                "needs_help_at_iso": _iso(now_ts),
                "last_reminded_at": now_ts,
                "last_reminded_at_iso": _iso(now_ts),
            }
        )
    elif action == "later":
        row.update(
            {
                "status": "open",
                "last_reminded_at": now_ts,
                "last_reminded_at_iso": _iso(now_ts),
                "snoozed_at": now_ts,
                "snoozed_at_iso": _iso(now_ts),
            }
        )
    if olga_comment:
        row["olga_comment"] = str(olga_comment).strip()[:1000]
    row["updated_at"] = now_ts
    row["updated_at_iso"] = _iso(now_ts)
    tasks[task_id] = row
    save_olga_manual_tasks(state, path)
    return {"ok": True, "task_id": task_id, "action": action, "row": row}


def remember_olga_manual_task_delivery(
    *,
    path: Path | str = DEFAULT_OLGA_MANUAL_TASKS_PATH,
    task_id: str,
    telegram_chat_id: str,
    telegram_message_id: str | int,
    telegram_message_thread_id: str | int = "",
    reminded: bool = True,
    now: int | None = None,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    state = load_olga_manual_tasks(path)
    tasks = state.setdefault("tasks", {})
    row = tasks.get(task_id) if isinstance(tasks.get(task_id), dict) else None
    if row is None:
        return {"ok": False, "reason": "task_not_found", "task_id": task_id}
    row["telegram_chat_id"] = str(telegram_chat_id or "")
    row["telegram_message_id"] = str(telegram_message_id or "")
    row["telegram_message_thread_id"] = str(telegram_message_thread_id or "")
    row["updated_at"] = now_ts
    row["updated_at_iso"] = _iso(now_ts)
    if reminded:
        row["last_reminded_at"] = now_ts
        row["last_reminded_at_iso"] = _iso(now_ts)
    tasks[task_id] = row
    save_olga_manual_tasks(state, path)
    return {"ok": True, "task_id": task_id, "row": row}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts or 0), timezone.utc).isoformat() if ts else ""
