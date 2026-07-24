from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULT_CITIES
from .models import HandoffReason, InboundMessage


@dataclass(frozen=True)
class ClientRoute:
    route: str
    service_key: str = ""
    city: str = ""
    risk_flags: tuple[str, ...] = ()
    handoff_reason: str = ""
    block_autoanswer_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "service_key": self.service_key,
            "city": self.city,
            "risk_flags": list(self.risk_flags),
            "handoff_reason": self.handoff_reason,
            "block_autoanswer_reason": self.block_autoanswer_reason,
            "metadata": self.metadata,
        }


SERVICE_HINT_RE = re.compile(
    r"(?iu)(процедур|услуг|губ|груд|ягод|поп|ботокс|диспорт|филлер|носогуб|скул|подбород|биоревитал|мезотерап|чистк|пилинг|волос|кожа головы|тесоро|tesoro)"
)
BOOKING_RE = re.compile(r"(?iu)(запис|свобод|окош|время|слот|следующ|недел|при[её]м|встреч|личн|очно)")
ADDRESS_RE = re.compile(r"(?iu)(адрес|метро|территориально|где|локац|как пройти|вход)")
RISK_RE = re.compile(r"(?iu)(беремен|кормлен|гв\\b|аллерг|осложн|от[её]к|боль|температур|гной|инфекц|ожог|задыха|трудно дыш|плохо после|жалоб)")
BOOKING_CRITICAL_RE = re.compile(
    r"(?iu)(адрес|оплат|предоплат|запись актуальн|актуальна ли запись|точно.*жд|"
    r"жд[уеё]т|я жду|вы забыли|забыли|не ответил|долго|что делать|отзыв|жалоб|"
    r"сегодня.*приход|завтра.*приход|можно.*опозда|опозда)"
)
AESTHETIC_VOLUME_RE = re.compile(r"(?iu)(?:\b\d{2,5}\s*(?:мл|милли?литр\w*)\b|(?:^|[^\d])(?:300|400|1200)(?:[^\d]|$)|объ[её]м)")
AESTHETIC_RESULT_RE = re.compile(
    r"(?iu)(размер|\+\s*1|плюс\s+один|заметн\w+|ярк\w+|выраженн\w+|результат|"
    r"увелич\w+|хватит|достаточн\w+|как\s+будет|до\s*/?\s*после|сколько\s+надо|сколько\s+нужно)"
)
AESTHETIC_BODY_RE = re.compile(r"(?iu)(груд|ягод|поп|тесоро|tesoro)")


def route_client_message(
    message: InboundMessage,
    *,
    retrieved_expert_answers: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    autoanswer_threshold: float = 0.82,
) -> ClientRoute:
    text = str(message.text or "")
    lowered = text.casefold().replace("ё", "е")
    city = _explicit_city(text, conversation_history)
    service_key = _service_key_from_text(text)
    has_service_hint = bool(SERVICE_HINT_RE.search(lowered) or _history_service_hint(conversation_history))

    if _aesthetic_expectation_question(lowered, conversation_history):
        return ClientRoute(
            route="expert_expectation_handoff",
            city=city,
            service_key=service_key,
            risk_flags=("expert_expectation",),
            handoff_reason=HandoffReason.MISSING_DATA.value,
            block_autoanswer_reason="aesthetic_expectation_guard",
            metadata={"guard": "aesthetic_expectation_guard", "reason": "нельзя автообещать результат по мл"},
        )

    if _booking_critical(lowered, conversation_history):
        return ClientRoute(
            route="booking_critical_handoff",
            city=city,
            service_key=service_key,
            risk_flags=("urgent", "booking_control"),
            handoff_reason=HandoffReason.BOOKING_CRITICAL.value,
            block_autoanswer_reason="booking_critical",
            metadata={"urgent": True, "sla": "booking_critical"},
        )

    if _has_media(message):
        return ClientRoute(
            route="media_handoff",
            city=city,
            service_key=service_key,
            handoff_reason=HandoffReason.PHOTO_CONSULTATION.value,
            block_autoanswer_reason="media_needs_review",
        )

    if RISK_RE.search(lowered):
        return ClientRoute(
            route="risk_handoff",
            city=city,
            service_key=service_key,
            risk_flags=("risk_case",),
            handoff_reason=HandoffReason.COMPLAINT_OR_RISK.value,
            block_autoanswer_reason="risk_case",
        )

    if _booking_without_service(lowered, conversation_history):
        return ClientRoute(
            route="ask_service",
            city=city,
            block_autoanswer_reason="booking_without_service",
            handoff_reason=HandoffReason.BOOKING_AMBIGUOUS.value,
        )

    if ADDRESS_RE.search(lowered) and not city:
        return ClientRoute(route="ask_city", service_key=service_key, block_autoanswer_reason="city_required")

    if BOOKING_RE.search(lowered) and has_service_hint and not city:
        return ClientRoute(route="ask_city", service_key=service_key, block_autoanswer_reason="city_required")

    best = _best_retrieved_answer(retrieved_expert_answers)
    if best:
        score = float(best.get("score") or 0)
        if (
            score >= autoanswer_threshold
            and best.get("_retrieval_safe_for_autoanswer") is not False
            and str(best.get("answer_client") or "").strip()
        ):
            return ClientRoute(route="rag_answer", city=city, service_key=service_key, metadata={"score": score})
        if best.get("_retrieval_safe_for_autoanswer") is False:
            return ClientRoute(
                route="codex_planner",
                city=city,
                service_key=service_key,
                block_autoanswer_reason=str(best.get("_retrieval_handoff_reason") or "rag_not_safe"),
                metadata={"score": score, "conflicts": list(best.get("_retrieval_conflicts") or [])},
            )

    if BOOKING_RE.search(lowered):
        return ClientRoute(route="booking_read", city=city, service_key=service_key)

    if ADDRESS_RE.search(lowered):
        return ClientRoute(route="address", city=city, service_key=service_key)

    return ClientRoute(route="codex_planner", city=city, service_key=service_key)


def _has_media(message: InboundMessage) -> bool:
    metadata = message.metadata or {}
    has_unresolved_voice = bool(metadata.get("voice_id") and not metadata.get("voice_transcribed"))
    return bool(
        message.has_photo
        or metadata.get("has_photo")
        or metadata.get("has_video")
        or metadata.get("has_file")
        or metadata.get("media_urls")
        or has_unresolved_voice
        or metadata.get("voice_transcription_error")
    )


def _aesthetic_expectation_question(lowered: str, conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> bool:
    history_text = " ".join(str(item.get("content") or "") for item in conversation_history[-8:]).casefold().replace("ё", "е")
    source = " ".join(part for part in (lowered, history_text) if part)
    has_volume = bool(AESTHETIC_VOLUME_RE.search(lowered))
    has_result = bool(AESTHETIC_RESULT_RE.search(lowered))
    has_body = bool(AESTHETIC_BODY_RE.search(source))
    if has_volume and has_result:
        return True
    return has_body and has_result and bool(re.search(r"(?iu)(фото|до\s*/?\s*после|как\s+будет|сколько\s+надо|сколько\s+нужно)", lowered))


def _booking_critical(lowered: str, conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> bool:
    if not BOOKING_CRITICAL_RE.search(lowered):
        return False
    history_text = " ".join(str(item.get("content") or "") for item in conversation_history[-8:]).casefold().replace("ё", "е")
    booking_context = bool(BOOKING_RE.search(history_text) or ADDRESS_RE.search(history_text) or SERVICE_HINT_RE.search(history_text))
    if re.search(r"оплат|предоплат|запись актуальн|актуальна ли запись|точно.*жд|сегодня.*приход|завтра.*приход|опозда", lowered):
        return True
    if ADDRESS_RE.search(lowered) and re.search(r"записан|записана|записаны|запись|я к вам", lowered):
        return True
    if ADDRESS_RE.search(lowered):
        return booking_context
    return booking_context


def _booking_without_service(lowered: str, conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> bool:
    if not BOOKING_RE.search(lowered):
        return False
    if SERVICE_HINT_RE.search(lowered):
        return False
    return not _history_service_hint(conversation_history)


def _history_service_hint(conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> bool:
    recent_user_text = " ".join(
        str(item.get("content") or "") for item in conversation_history[-8:] if str(item.get("role") or "") == "user"
    ).casefold()
    return bool(SERVICE_HINT_RE.search(recent_user_text))


def _explicit_city(text: str, conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    source = " ".join(
        part
        for part in (
            text,
            " ".join(str(item.get("content") or "") for item in conversation_history[-8:] if str(item.get("role") or "") == "user"),
        )
        if part
    ).casefold().replace("ё", "е")
    aliases = {
        "спб": "Санкт-Петербург",
        "питер": "Санкт-Петербург",
        "москве": "Москва",
        "москву": "Москва",
        "ростове": "Ростов-на-Дону",
        "ростов": "Ростов-на-Дону",
        "абинске": "Абинск",
        "абинск": "Абинск",
    }
    for raw, city in aliases.items():
        if re.search(rf"(?<![а-яa-z]){re.escape(raw)}(?![а-яa-z])", source):
            return city
    for city in DEFAULT_CITIES:
        normalized = city.casefold().replace("ё", "е")
        if normalized and normalized in source:
            return city
        first = normalized.split("-")[0]
        if len(first) >= 5 and first in source:
            return city
    return ""


def _service_key_from_text(text: str) -> str:
    lowered = str(text or "").casefold().replace("ё", "е")
    if re.search(r"губ", lowered):
        return "guby"
    if re.search(r"груд", lowered):
        return "grud"
    if re.search(r"ягод|поп|tesoro|тесоро", lowered):
        return "yagodicy"
    if re.search(r"ботокс|ботулин|диспорт", lowered):
        return "botoks"
    if re.search(r"волос|кожа головы", lowered):
        return "kozha_golovy"
    return ""


def _best_retrieved_answer(answers: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> dict[str, Any] | None:
    return answers[0] if answers else None
