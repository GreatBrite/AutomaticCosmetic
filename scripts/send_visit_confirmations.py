from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.freelance_leads_bot.config import Settings, load_dotenv
from src.freelance_leads_bot.integrations.care_crm import (
    CareCrmStore,
    VisitConfirmationService,
    format_visit_confirmation_card,
    visit_confirmation_keyboard,
)
from src.freelance_leads_bot.integrations.config import IntegrationSettings
from src.freelance_leads_bot.integrations.runtime import booking_from_settings
from src.freelance_leads_bot.telegram import TelegramBot


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _admin_chat_id(settings: IntegrationSettings) -> str:
    return (
        os.getenv("VISIT_CONFIRMATIONS_CHAT_ID", "").strip()
        or settings.handoff_notify_chat_id
        or Settings.from_env().telegram_chat_id
    )


async def _visit_confirmation_rows(settings: IntegrationSettings, day: str) -> list[dict[str, Any]]:
    booking = booking_from_settings(settings)
    try:
        service = VisitConfirmationService(CareCrmStore(), booking)
        return await service.sync_day(day)
    finally:
        aclose = getattr(booking, "aclose", None)
        if callable(aclose):
            await aclose()


async def send_visit_confirmations_once(
    *,
    settings: IntegrationSettings,
    bot: TelegramBot,
    chat_id: str,
    day: str,
    quiet_empty: bool = True,
) -> dict[str, Any]:
    rows = await _visit_confirmation_rows(settings, day)
    if not rows:
        if not quiet_empty:
            bot.send_message(chat_id, f"На {day} не нашла записей для проверки визитов.")
        return {"ok": True, "day": day, "sent": 0, "empty": True}

    store = CareCrmStore()
    bot.send_message(chat_id, f"<b>Проверка визитов за {day}</b>\nКарточек: {len(rows)}.")
    sent = 0
    for row in rows:
        response = bot.send_message(
            chat_id,
            format_visit_confirmation_card(row),
            reply_markup=visit_confirmation_keyboard(int(row["id"])),
        )
        message_id = str((response.get("result") or {}).get("message_id") or "")
        if message_id:
            store.remember_confirmation_card(int(row["id"]), chat_id=chat_id, message_id=message_id)
        sent += 1
    return {"ok": True, "day": day, "sent": sent, "empty": False}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Send Olga daily visit confirmation cards from YCLIENTS appointments.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Day to sync in YYYY-MM-DD format. Defaults to today.")
    parser.add_argument("--force", action="store_true", help="Run even when VISIT_CONFIRMATIONS_AUTOSEND_ENABLED is disabled.")
    parser.add_argument("--quiet-empty", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not args.force and not _env_bool("VISIT_CONFIRMATIONS_AUTOSEND_ENABLED"):
        print(json.dumps({"ok": True, "skipped": True, "reason": "autosend_disabled"}, ensure_ascii=False), flush=True)
        return

    settings = IntegrationSettings.from_env()
    chat_id = _admin_chat_id(settings)
    if not chat_id:
        print(json.dumps({"ok": False, "error": "VISIT_CONFIRMATIONS_CHAT_ID/HANDOFF_NOTIFY_CHAT_ID/TELEGRAM_CHAT_ID is empty"}, ensure_ascii=False), flush=True)
        return
    result = await send_visit_confirmations_once(
        settings=settings,
        bot=TelegramBot(settings.telegram_admin_bot_token or Settings.from_env().telegram_bot_token),
        chat_id=chat_id,
        day=str(args.date)[:10],
        quiet_empty=bool(args.quiet_empty),
    )
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
