from __future__ import annotations

import argparse
import json
import os
import sys
from html import escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.freelance_leads_bot.config import Settings, load_dotenv
from src.freelance_leads_bot.integrations.config import IntegrationSettings
from src.freelance_leads_bot.integrations.olga_manual_tasks import (
    DEFAULT_OLGA_MANUAL_TASKS_PATH,
    due_olga_manual_tasks,
    format_olga_manual_task_card,
    olga_manual_task_keyboard,
    remember_olga_manual_task_delivery,
)
from src.freelance_leads_bot.telegram import TelegramBot


def _admin_chat_id(settings: IntegrationSettings) -> str:
    return (
        os.getenv("OLGA_MANUAL_TASKS_CHAT_ID", "").strip()
        or settings.handoff_notify_chat_id
        or Settings.from_env().telegram_chat_id
    )


def send_olga_task_reminders_once(
    *,
    bot: TelegramBot,
    chat_id: str,
    path: Path | str = DEFAULT_OLGA_MANUAL_TASKS_PATH,
    now: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    rows = due_olga_manual_tasks(path, now=now, seed=True)
    sent = 0
    errors: list[dict[str, str]] = []
    for row in rows[: max(0, int(limit or 0))]:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        try:
            response = bot.send_message(
                chat_id,
                escape(format_olga_manual_task_card(row, reminder=True)),
                reply_markup=olga_manual_task_keyboard(task_id),
            )
        except Exception as exc:
            errors.append({"task_id": task_id, "error": repr(exc)})
            continue
        message_id = str((response.get("result") or {}).get("message_id") or "")
        if not message_id:
            errors.append({"task_id": task_id, "error": "telegram_message_not_delivered"})
            continue
        remember_olga_manual_task_delivery(
            path=path,
            task_id=task_id,
            telegram_chat_id=chat_id,
            telegram_message_id=message_id,
            reminded=True,
            now=now,
        )
        sent += 1
    return {"ok": not errors, "due": len(rows), "sent": sent, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description="Send Olga manual task reminders every 3 hours per open task.")
    parser.add_argument("--path", type=Path, default=DEFAULT_OLGA_MANUAL_TASKS_PATH)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    settings = IntegrationSettings.from_env()
    chat_id = _admin_chat_id(settings)
    if not chat_id:
        print(json.dumps({"ok": False, "error": "OLGA_MANUAL_TASKS_CHAT_ID/HANDOFF_NOTIFY_CHAT_ID/TELEGRAM_CHAT_ID is empty"}, ensure_ascii=False), flush=True)
        return
    result = send_olga_task_reminders_once(
        bot=TelegramBot(settings.telegram_admin_bot_token or Settings.from_env().telegram_bot_token),
        chat_id=chat_id,
        path=args.path,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
