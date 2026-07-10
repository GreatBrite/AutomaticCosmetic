from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.freelance_leads_bot.integrations.config import IntegrationSettings
from src.freelance_leads_bot.integrations.expert_rag import ExpertRagStore
from src.freelance_leads_bot.integrations.mentor_memory import _looks_reusable_answer


DEFAULT_HISTORY_DB = Path("data/leads.sqlite3")


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _extract_olga_reply(content: str) -> str:
    match = re.search(
        r"(?s)Сообщение от Telegram-пользователя @dr_olgat:\s*(.+?)(?:\n\nОтвет на сообщение|\Z)",
        content,
    )
    if not match:
        return ""
    return _normalize_space(match.group(1))


def _extract_handoff_question(content: str) -> str:
    match = re.search(r"(?ms)^Сообщение:\s*(.+?)(?:\nКонтекст:|\nМедиа:|\Z)", content)
    if match:
        return _normalize_space(match.group(1))
    return ""


def _extract_handoff_id(content: str) -> str:
    match = re.search(r"(?m)^Handoff id:\s*([^\n]+)", content)
    return _normalize_space(match.group(1)) if match else ""


def _extract_avito_chat_id(content: str) -> str:
    match = re.search(r"(?m)^Avito chat_id:\s*([^\n]+)", content)
    if match:
        return _normalize_space(match.group(1))
    match = re.search(r"(?m)^Диалог:\s*([^\n]+)", content)
    return _normalize_space(match.group(1)) if match else ""


def _looks_like_instruction_to_bot(text: str) -> bool:
    lowered = text.casefold()
    if len(lowered) < 20:
        return True
    if lowered in {"да", "нет", "ок", "окей", "можно", "отправь", "перешли"}:
        return True
    instruction_starts = (
        "перешли",
        "отправь",
        "напиши",
        "спроси",
        "уточни",
        "попроси",
        "сделай",
        "не отправляй",
        "можно отправить",
    )
    return lowered.startswith(instruction_starts)


def _iter_history_rows(history_db: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(history_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, chat_key, role, content, created_at
            FROM codex_chat_history
            WHERE role = 'user'
              AND chat_key LIKE 'chat:%'
              AND content LIKE '%@dr_olgat%'
              AND content LIKE '%Нужна ручная консультация%'
              AND content LIKE '%Сообщение:%'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def import_history(*, history_db: Path, store: ExpertRagStore, dry_run: bool = False) -> dict[str, int]:
    stats = {
        "seen": 0,
        "imported": 0,
        "skipped_no_reply": 0,
        "skipped_no_question": 0,
        "skipped_instruction": 0,
        "skipped_not_reusable": 0,
    }
    for row in _iter_history_rows(history_db):
        stats["seen"] += 1
        content = str(row.get("content") or "")
        olga_reply = _extract_olga_reply(content)
        if not olga_reply:
            stats["skipped_no_reply"] += 1
            continue
        if _looks_like_instruction_to_bot(olga_reply):
            stats["skipped_instruction"] += 1
            continue
        if not _looks_reusable_answer(olga_reply):
            stats["skipped_not_reusable"] += 1
            continue
        question = _extract_handoff_question(content)
        if not question:
            stats["skipped_no_question"] += 1
            continue
        if not dry_run:
            store.upsert_from_handoff(
                question=question,
                answer_client=olga_reply,
                answer_internal=content[-2500:],
                source_chat_id=_extract_avito_chat_id(content),
                source_message_id=str(row.get("id") or ""),
                olga_reply_message_id=str(row.get("id") or ""),
                approved_by="olga",
                metadata={
                    "source": "telegram_olga_history_import",
                    "history_id": str(row.get("id") or ""),
                    "history_chat_key": str(row.get("chat_key") or ""),
                    "handoff_id": _extract_handoff_id(content),
                    "created_at": str(row.get("created_at") or ""),
                },
            )
        stats["imported"] += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Import reusable Olga Avito handoff replies from Telegram admin history into expert RAG.")
    parser.add_argument("--history-db", type=Path, default=DEFAULT_HISTORY_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = IntegrationSettings.from_env()
    store = ExpertRagStore(settings.rag_expert_db_path)
    print(import_history(history_db=args.history_db, store=store, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
