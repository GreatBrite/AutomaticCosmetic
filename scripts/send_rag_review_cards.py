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
from src.freelance_leads_bot.integrations.expert_rag import APPROVED, ExpertRagStore
from src.freelance_leads_bot.integrations.rag_manual_review import format_rag_manual_review_card, rag_manual_review_keyboard
from src.freelance_leads_bot.telegram import TelegramBot


def _admin_chat_id(settings: IntegrationSettings) -> str:
    return os.getenv("RAG_REVIEW_CHAT_ID", "").strip() or str(settings.telegram_admin_user_id or "") or Settings.from_env().telegram_chat_id


def remaining_autoanswer_items(store: ExpertRagStore, *, limit: int) -> list[Any]:
    rows = []
    for item in store.list_answers(status=APPROVED, limit=1000):
        metadata = item.metadata or {}
        if metadata.get("autoanswer_allowed") is False:
            continue
        rows.append(item)
    return rows[: max(1, int(limit or 1))]


def send_rag_review_cards_once(
    *,
    bot: TelegramBot,
    chat_id: str,
    store: ExpertRagStore,
    limit: int = 20,
) -> dict[str, Any]:
    rows = remaining_autoanswer_items(store, limit=limit)
    if not rows:
        bot.send_message(chat_id, "RAG: автоответных записей, требующих ручного решения, сейчас нет.")
        return {"ok": True, "sent": 0, "empty": True}
    bot.send_message(
        chat_id,
        (
            f"<b>RAG: осталось проверить {len(rows)} автоответных записей</b>\n"
            "Я уже убрал из автоответов даты, старые цены, экспертные оценки, медицину и консультации не по телефону. "
            "Ниже остались только записи, где нужен твой выбор."
        ),
    )
    sent = 0
    for item in rows:
        bot.send_message(
            chat_id,
            escape(format_rag_manual_review_card(item)),
            reply_markup=rag_manual_review_keyboard(item.id),
        )
        sent += 1
    return {"ok": True, "sent": sent, "empty": False, "item_ids": [item.id for item in rows]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Send remaining RAG autoanswer review cards to Telegram admin.")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    settings = IntegrationSettings.from_env()
    chat_id = _admin_chat_id(settings)
    if not chat_id:
        print(json.dumps({"ok": False, "error": "RAG_REVIEW_CHAT_ID/TELEGRAM_ADMIN_USER_ID/TELEGRAM_CHAT_ID is empty"}, ensure_ascii=False), flush=True)
        return
    result = send_rag_review_cards_once(
        bot=TelegramBot(settings.telegram_admin_bot_token or Settings.from_env().telegram_bot_token),
        chat_id=chat_id,
        store=ExpertRagStore(settings.rag_expert_db_path),
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
