from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from typing import Any, Protocol

from ..codex_runner import chat_with_codex
from .avito_consultant import AvitoConsultantReply
from .codex_planner import parse_codex_step
from .models import Handoff, HandoffReason, InboundMessage


OFFLINE_CONSULTATION_RE = re.compile(
    r"(?is)(?:^|(?<=[.!?…]))\s*[^.!?…]*(?:очн\w*\s+консультац\w*|"
    r"прийти\s+на\s+консультац\w*|приходите\s+на\s+консультац\w*|"
    r"приехать\s+на\s+консультац\w*)[^.!?…]*[.!?…]?"
)
FACTUAL_CONSULTATION_PRICE_RE = re.compile(r"(?is)(?:платн|бесплатн|\d+\s*(?:₽|руб)|стоимост|цена)")
CONSULTATION_NUDGE_RE = re.compile(r"(?is)(?:лучше|нужн|обязательн|требу|приход|приезж|запис|подойд)")
FINAL_CONSULTATION_HEDGE_RE = re.compile(
    r"(?is)(?:^|(?<=[.!?…]))\s*[^.!?…]*(?:(?:окончательно[^.!?…]*(?:подбира\w+|"
    r"подбер\w+|реша\w+|определя\w+))|(?:(?:подбира\w+|подбер\w+|реша\w+|определя\w+)"
    r"[^.!?…]*на\s+консультац\w*))[^.!?…]*[.!?…]?"
)
REDUNDANT_EXPERT_TAIL_RE = re.compile(
    r"(?is)(?:^|(?<=[.!?…]))\s*"
    r"(?:Точн\w+|Итогов\w+|Окончательн\w+)[^.!?…]*"
    r"(?:объ[её]м|стоимост|вариант|коррекц)[^.!?…]*"
    r"(?:подтверд\w+|подбира\w+|подбер\w+|завис\w+|можно\s+сказать|сориентир\w+)[^.!?…]*"
    r"(?:после\s+(?:уточнен\w+|оценк\w+|осмотр\w+|консультац\w+)|"
    r"по\s+(?:форм\w+|исходн\w+|желаем\w+)|"
    r"на\s+(?:консультац\w+|осмотр\w+))[^.!?…]*[.!?…]?"
)
EXPERT_ESTIMATE_CUE_RE = re.compile(
    r"(?is)(?:фото\s+посмотр|по\s+фото|визуальн\w+\s+оценк|ориентир|ориентировочн|"
    r"\b\d{2,5}\s*(?:мл|₽|руб)|стоимост|как\s+модель|потребу\w+|можно\s+ориентироваться)"
)


class AvitoDraftReviewer(Protocol):
    async def review(
        self,
        *,
        message: InboundMessage,
        decision: AvitoConsultantReply,
        conversation_history: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> AvitoConsultantReply:
        ...


class CodexDraftReviewer:
    """Second Codex pass that reviews a client-facing draft before Avito send."""

    def __init__(self, timeout_seconds: int = 120) -> None:
        self.timeout_seconds = timeout_seconds

    async def review(
        self,
        *,
        message: InboundMessage,
        decision: AvitoConsultantReply,
        conversation_history: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> AvitoConsultantReply:
        prompt = build_codex_review_prompt(message, decision, conversation_history)
        text, _path = await asyncio.to_thread(chat_with_codex, prompt, None, self.timeout_seconds, None, True)
        outcome = parse_codex_step(text)
        return apply_review_outcome(message, decision, outcome)


def apply_review_outcome(
    message: InboundMessage,
    decision: AvitoConsultantReply,
    outcome: dict[str, Any] | None,
) -> AvitoConsultantReply:
    metadata = dict(decision.metadata or {})
    if not outcome:
        metadata["draft_review"] = {"action": "unavailable"}
        reply, changed = sanitize_consultation_language(decision.reply)
        if changed:
            metadata["consultation_guard"] = {"changed": True, "reason": "review_unavailable"}
        return replace(decision, reply=reply, metadata=metadata)

    action = str(outcome.get("action") or "approve").strip().casefold()
    metadata["draft_review"] = {
        "action": action,
        "notes": str(outcome.get("notes") or outcome.get("reason") or "")[:1200],
        "raw": outcome,
    }
    if action == "approve":
        reply, changed = sanitize_consultation_language(decision.reply)
        if changed:
            metadata["consultation_guard"] = {"changed": True, "reason": "approved_draft"}
        return replace(decision, reply=reply, metadata=metadata)

    reply = str(outcome.get("reply") or decision.reply or "").strip()
    reply, changed = sanitize_consultation_language(reply)
    if changed:
        metadata["consultation_guard"] = {"changed": True, "reason": action}
    if action == "handoff":
        handoff = _handoff_from_review(message, outcome) or decision.handoff
        return replace(
            decision,
            action="handoff",
            reply=reply or "Сейчас уточню этот момент и вернусь с ответом.",
            handoff=handoff,
            metadata=metadata,
        )

    if action in {"revise", "correct", "rewrite"} and reply:
        return replace(decision, action=str(outcome.get("decision_action") or decision.action), reply=reply, metadata=metadata)

    metadata["draft_review"]["action"] = "ignored_invalid"
    return replace(decision, metadata=metadata)


def build_codex_review_prompt(
    message: InboundMessage,
    decision: AvitoConsultantReply,
    conversation_history: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> str:
    payload = {
        "message": _message_data(message),
        "conversation_history": list(conversation_history)[-8:],
        "draft_decision": _decision_data(decision),
    }
    return (
        "Ты второй Codex-агент проверки ответа косметологического Avito-ассистента.\n"
        "Цель не ограничить автора черновика, а дать ему ещё один внимательный взгляд перед отправкой клиенту.\n"
        "Проверь только клиентский текст: он должен быть полезным, коротким и честным.\n"
        "Особенно ищи ошибки из живых карточек: неподтверждённая цена, выдуманный адрес/метро, уверенный ответ без опоры, "
        "перепутанная дата/город, медицинская самоуверенность, раскрытие внутренних слов вроде Codex/tool/handoff, "
        "ответ на оффтопик или попытку узнать устройство бота.\n"
        "Если всё хорошо — approve. Если можно исправить без специалиста — revise и дай готовый reply.\n"
        "Если нужна личная экспертная оценка/фото/точная цена/адрес/медицинский риск — handoff и дай безопасный короткий reply клиенту без объяснения внутреннего маршрута.\n"
        "Не склоняй клиента к очной консультации. Если для оценки реально не хватает данных, один раз предложи онлайн-разбор с Ольгой и запроси только недостающие данные/фото.\n"
        "Если в истории/trace/draft уже есть оценка Ольги или подтверждённое решение специалиста, не добавляй новый призыв к консультации и убирай шаблонные хвосты вроде 'окончательно подбирается индивидуально/на консультации'.\n"
        "Не спамь онлайн-консультацией: обычный ответ про цену, адрес, запись или уже разобранное фото должен завершаться по сути вопроса, без консультационного CTA.\n"
        "Не запрещай ответ только потому, что он не идеален; исправляй по смыслу.\n\n"
        "Верни строго JSON без markdown:\n"
        '{"action":"approve","notes":"коротко"}\n'
        '{"action":"revise","reply":"исправленный текст","notes":"что исправлено"}\n'
        '{"action":"handoff","handoff_reason":"missing_data","handoff_summary":"что уточнить внутри команды","reply":"текст клиенту"}\n\n'
        "PAYLOAD:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n"
    )


def sanitize_consultation_language(reply: str) -> tuple[str, bool]:
    """Remove unsafe/default consultation nudges from client-facing Avito drafts."""

    original = str(reply or "")
    sanitized = _remove_offline_consultation_nudges(original)
    sanitized = FINAL_CONSULTATION_HEDGE_RE.sub(" ", sanitized)
    sanitized = _remove_redundant_expert_tail(sanitized)
    sanitized = _normalize_reply_spacing(sanitized)
    if original.strip() and not sanitized:
        sanitized = "Сейчас уточню этот момент и вернусь с ответом."
    return sanitized, sanitized != original.strip()


def _remove_redundant_expert_tail(text: str) -> str:
    if not EXPERT_ESTIMATE_CUE_RE.search(text):
        return text
    return REDUNDANT_EXPERT_TAIL_RE.sub(" ", text)


def _remove_offline_consultation_nudges(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        sentence = match.group(0)
        if FACTUAL_CONSULTATION_PRICE_RE.search(sentence) and not CONSULTATION_NUDGE_RE.search(sentence):
            return sentence
        return " "

    return OFFLINE_CONSULTATION_RE.sub(replace, text)


def _normalize_reply_spacing(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _handoff_from_review(message: InboundMessage, outcome: dict[str, Any]) -> Handoff | None:
    raw_reason = str(outcome.get("handoff_reason") or "missing_data").strip()
    try:
        reason = HandoffReason(raw_reason)
    except ValueError:
        reason = HandoffReason.MISSING_DATA
    return Handoff(reason=reason, message=message, summary=str(outcome.get("handoff_summary") or outcome.get("notes") or ""))


def _message_data(message: InboundMessage) -> dict[str, Any]:
    listing = message.listing.to_prompt_context() if message.listing else {}
    return {
        "channel": message.channel.value,
        "client_id": message.client_id,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "text": message.text,
        "created_at": message.created_at,
        "has_photo": message.has_photo,
        "listing": listing,
        "metadata": {
            "author_role": message.metadata.get("author_role"),
            "direction": message.metadata.get("direction"),
            "message_type": message.metadata.get("message_type"),
        },
    }


def _decision_data(decision: AvitoConsultantReply) -> dict[str, Any]:
    return {
        "action": decision.action,
        "reply": decision.reply,
        "handoff_reason": decision.handoff.reason.value if decision.handoff else "",
        "handoff_summary": decision.handoff.summary if decision.handoff else "",
        "appointment_id": decision.appointment_id,
        "metadata": {
            "planner": decision.metadata.get("planner") if isinstance(decision.metadata, dict) else "",
            "trace_tail": (decision.metadata.get("trace") or [])[-6:] if isinstance(decision.metadata, dict) else [],
        },
    }
