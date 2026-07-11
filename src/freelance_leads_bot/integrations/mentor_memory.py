from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .agent_tools import JsonKnowledgeStore, KnowledgeItem
from .avito_consultant import AvitoConsultantReply
from .expert_rag import ExpertAnswer, ExpertRagStore
from .models import InboundMessage


@dataclass(frozen=True)
class MentorMemoryResult:
    created: list[KnowledgeItem] = field(default_factory=list)
    expert_answers: list[ExpertAnswer] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class MentorMemoryService:
    """Learns reusable, attributed notes from Olga/admin corrections and completed interactions."""

    def __init__(self, knowledge: JsonKnowledgeStore, expert_rag: ExpertRagStore | None = None) -> None:
        self.knowledge = knowledge
        self.expert_rag = expert_rag

    def observe_avito_send(
        self,
        *,
        chat_id: str,
        text: str,
        actor: str,
        source: str = "telegram_admin",
        context: dict[str, Any] | None = None,
    ) -> MentorMemoryResult:
        if actor not in {"olga", "admin"}:
            return MentorMemoryResult(skipped=["actor_not_authoritative"])
        context = context or {}
        lessons = _lessons_from_authoritative_reply(text, context or {})
        result = self._store_lessons(lessons, source=source, actor=actor, chat_id=chat_id)
        expert_answers: list[ExpertAnswer] = []
        if self.expert_rag and _looks_reusable_answer(text):
            question = _question_from_context(context) or str(context.get("question") or context.get("handoff_text") or "")
            if question:
                expert_answers.append(
                    self.expert_rag.upsert_from_handoff(
                        question=question,
                        answer_client=text,
                        answer_internal=str(context.get("handoff_text") or ""),
                        source_chat_id=chat_id,
                        source_message_id=str(context.get("source_message_id") or ""),
                        olga_reply_message_id=str(context.get("olga_reply_message_id") or ""),
                        approved_by=actor,
                        metadata={"source": source, "actor": actor, "autoanswer_allowed": True},
                    )
                )
        return MentorMemoryResult(created=result.created, expert_answers=expert_answers, skipped=result.skipped)

    def observe_client_decision(
        self,
        *,
        message: InboundMessage,
        decision: AvitoConsultantReply,
        send_result: dict[str, Any] | None = None,
    ) -> MentorMemoryResult:
        if decision.handoff:
            return MentorMemoryResult(skipped=["handoff_waits_for_human"])
        if not (send_result or {}).get("sent"):
            return MentorMemoryResult(skipped=["not_authoritative_send"])
        lessons = _lessons_from_successful_bot_reply(message, decision)
        return self._store_lessons(lessons, source="avito_bot_send", actor="codex", chat_id=message.chat_id)

    def _store_lessons(
        self,
        lessons: list[dict[str, Any]],
        *,
        source: str,
        actor: str,
        chat_id: str,
    ) -> MentorMemoryResult:
        created: list[KnowledgeItem] = []
        skipped: list[str] = []
        for lesson in lessons:
            content = str(lesson.get("content") or "").strip()
            title = str(lesson.get("title") or "").strip()
            if len(content) < 12 or not title:
                skipped.append("too_short")
                continue
            fingerprint = _fingerprint(title, content, actor)
            if _exists(self.knowledge, fingerprint):
                skipped.append("duplicate")
                continue
            item = self.knowledge.create(
                kind=str(lesson.get("kind") or "mentor_memory"),
                title=title,
                content=content,
                tags=tuple(lesson.get("tags") or ("mentor", "avito")),
                metadata={
                    "source": source,
                    "actor": actor,
                    "chat_id": chat_id,
                    "confidence": lesson.get("confidence", "medium"),
                    "status": "confirmed" if actor in {"olga", "admin"} else "observed",
                    "fingerprint": fingerprint,
                },
            )
            created.append(item)
        return MentorMemoryResult(created=created, skipped=skipped)


def _lessons_from_authoritative_reply(text: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = _normalize_space(text)
    lowered = normalized.casefold()
    lessons: list[dict[str, Any]] = []
    if not normalized:
        return lessons

    if re.search(r"(\+7|8)\D*\d{3}\D*\d{3}\D*\d{2}\D*\d{2}", normalized):
        lessons.append(
            {
                "kind": "contact_policy",
                "title": "Телефон для клиентов Avito",
                "content": f"Если клиент просит телефон или связь вне Avito, использовать подтвержденный ответ: {normalized}",
                "tags": ("mentor", "avito", "phone", "contact"),
                "confidence": "high",
            }
        )

    if any(word in lowered for word in ("стоимость", "стоит", "цена", "₽", "руб")) and re.search(r"\d[\d\s]*(₽|руб)", lowered):
        lessons.append(
            {
                "kind": "price_policy",
                "title": _title_from_context(context, "Подтвержденная цена из ответа Ольги"),
                "content": f"Подтвержденный ответ по цене: {normalized}",
                "tags": ("mentor", "avito", "price"),
                "confidence": "high",
            }
        )

    if any(word in lowered for word in ("адрес", "метро", "м.", "территориально", "находимся")):
        lessons.append(
            {
                "kind": "location_policy",
                "title": "Подтвержденный ответ по адресу",
                "content": f"Адрес/локацию клиенту формулировать только так, если контекст совпадает: {normalized}",
                "tags": ("mentor", "avito", "address", "location"),
                "confidence": "high",
            }
        )

    if any(word in lowered for word in ("беремен", "гв", "кормлен", "противопоказ")):
        lessons.append(
            {
                "kind": "safety_policy",
                "title": "Противопоказания и ГВ",
                "content": f"Медицинский ответ от Ольги: {normalized}",
                "tags": ("mentor", "avito", "safety", "contraindications"),
                "confidence": "high",
            }
        )

    if not lessons and _looks_reusable_answer(normalized):
        lessons.append(
            {
                "kind": "answer_pattern",
                "title": _title_from_context(context, "Пример ответа Ольги клиенту"),
                "content": f"Когда клиент спрашивает похожее, можно отвечать в стиле Ольги: {normalized}",
                "tags": ("mentor", "avito", "olga_style"),
                "confidence": "medium",
            }
        )
    return lessons


def _lessons_from_successful_bot_reply(message: InboundMessage, decision: AvitoConsultantReply) -> list[dict[str, Any]]:
    del decision
    if message.listing and message.listing.title and message.listing.price_string:
        return [
            {
                "kind": "listing_context",
                "title": f"Avito объявление: {message.listing.title}",
                "content": (
                    f"В объявлении Avito «{message.listing.title}» указана цена {message.listing.price_string}. "
                    "Эту цену можно называть только в контексте этого объявления."
                ),
                "tags": ("mentor", "avito", "listing", "price"),
                "confidence": "medium",
            }
        ]
    return []


def _exists(knowledge: JsonKnowledgeStore, fingerprint: str) -> bool:
    return any(item.metadata.get("fingerprint") == fingerprint for item in knowledge.list(tags=("mentor",)))


def _fingerprint(title: str, content: str, actor: str) -> str:
    raw = f"{actor}\n{_normalize_space(title).casefold()}\n{_normalize_space(content).casefold()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _title_from_context(context: dict[str, Any], fallback: str) -> str:
    listing = context.get("listing") if isinstance(context.get("listing"), dict) else {}
    title = str(listing.get("title") or "").strip()
    return f"{fallback}: {title}" if title else fallback


def _question_from_context(context: dict[str, Any]) -> str:
    for key in ("client_message", "question", "source_text"):
        value = str(context.get(key) or "").strip()
        if value:
            return value
    text = str(context.get("handoff_text") or "").strip()
    match = re.search(r"(?m)^Сообщение:\s*(.+?)(?:\nКонтекст:|\Z)", text, flags=re.S)
    if match:
        return _normalize_space(match.group(1))
    return ""


def _looks_reusable_answer(text: str) -> bool:
    lowered = text.casefold()
    if len(text) < 20 or len(text) > 700:
        return False
    if any(marker in lowered for marker in ("codex", "tool", "handoff", "trace", "эскалац")):
        return False
    return any(
        word in lowered
        for word in (
            "можно",
            "нельзя",
            "стоимость",
            "запис",
            "после",
            " до ",
            "процедур",
            "ольга",
            "используем",
            "препарат",
            "филлер",
            "tesoro",
            "объем",
            "объём",
        )
    )
