from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .expert_rag import ExpertAnswer, ExpertRagStore


RAG_REVIEW_ACTIONS = {"keep", "block", "edit"}


def rag_manual_review_keyboard(item_id: int) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {"text": "Оставить", "callback_data": f"ragreview:{int(item_id)}:keep"},
                {"text": "Убрать автоответ", "callback_data": f"ragreview:{int(item_id)}:block"},
            ],
            [
                {"text": "Нужна правка", "callback_data": f"ragreview:{int(item_id)}:edit"},
            ],
        ]
    }


def parse_rag_manual_review_callback(data: str) -> tuple[int, str] | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "ragreview":
        return None
    try:
        item_id = int(parts[1])
    except ValueError:
        return None
    action = parts[2].strip()
    if item_id <= 0 or action not in RAG_REVIEW_ACTIONS:
        return None
    return item_id, action


def format_rag_manual_review_card(item: ExpertAnswer) -> str:
    reason = _review_reason(item)
    return "\n".join(
        [
            f"RAG: нужна проверка #{item.id}",
            f"Причина: {reason}",
            f"Тема: {item.topic or '-'}",
            f"Услуга: {item.service or '-'}",
            "",
            "Что спросил клиент:",
            _short(item.question_canonical, 600),
            "",
            "Что сейчас может ответить бот:",
            _short(item.answer_client, 900),
            "",
            "Что выбрать:",
            "Оставить — если это точная стабильная формулировка.",
            "Убрать автоответ — если бот не должен писать это сам.",
            "Нужна правка — если мысль верная, но текст нужно переписать.",
        ]
    )


def apply_rag_manual_review_action(
    *,
    store: ExpertRagStore,
    item_id: int,
    action: str,
    actor: str = "telegram_admin",
) -> dict[str, Any]:
    parsed = parse_rag_manual_review_callback(f"ragreview:{item_id}:{action}")
    if parsed is None:
        return {"ok": False, "reason": "invalid_callback"}
    item_id, action = parsed
    item = store.get(item_id)
    if not item:
        return {"ok": False, "reason": "item_not_found", "item_id": item_id}
    now = datetime.now(timezone.utc).isoformat()
    if action == "keep":
        updated = store.update_metadata(
            item_id,
            {
                "autoanswer_allowed": True,
                "manual_review_decision": "keep_for_autoanswer",
                "manual_reviewed_by": actor,
                "manual_reviewed_at": now,
            },
        )
    elif action == "block":
        updated = store.update_metadata(
            item_id,
            {
                "autoanswer_allowed": False,
                "autoanswer_block_reason": "manual_review_blocked",
                "manual_review_decision": "block_autoanswer",
                "manual_reviewed_by": actor,
                "manual_reviewed_at": now,
                "kept_for_context_style": True,
            },
        )
    else:
        updated = store.update_metadata(
            item_id,
            {
                "autoanswer_allowed": False,
                "autoanswer_block_reason": "manual_review_needs_edit",
                "manual_review_decision": "needs_edit",
                "manual_reviewed_by": actor,
                "manual_reviewed_at": now,
                "needs_olga_rewrite": True,
                "kept_for_context_style": True,
            },
        )
    return {"ok": True, "item_id": item_id, "action": action, "item": updated.to_dict()}


def _review_reason(item: ExpertAnswer) -> str:
    text = "\n".join([item.question_canonical, item.answer_client]).casefold()
    if "номер" in text or "свяжется" in text or "рассроч" in text:
        return "можно оставить только если это стабильное правило сбора контакта для мессенджера/соцсети"
    return "оставшаяся автоответная RAG-запись после автоматической очистки"


def _short(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized or "-"
    return normalized[: max(1, limit - 1)].rstrip() + "..."
