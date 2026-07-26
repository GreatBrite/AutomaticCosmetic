from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .city_utils import explicit_external_city, explicit_supported_city, fixed_cities_reply
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
INDIVIDUAL_EXPERT_RE = re.compile(
    r"(?iu)(подойд[её]т|оценит|оцените|посмотрит|посмотрите|что\s+лучше|"
    r"сколько\s+нужно|сколько\s+надо|какой\s+будет\s+результат|асимметр|форма|"
    r"состояние|морщин|выпад[ае]т|волос|кож[аи]|симптом|можно\s+ли\s+после|можно\s+ли\s+при)"
)
VISUAL_RE = re.compile(r"(?iu)(фото|сним|визуальн|посмотрит|посмотрите|оценит|оцените|асимметр|форма|морщин|состояние)")
CONSULTATION_DETAIL_RE = re.compile(
    r"(?iu)(хочу|интересует|нужно|надо|цель|эффект|результат|исправ|убрать|увелич|"
    r"зона|губ|груд|ягод|поп|ботокс|морщин|асимметр|объ[её]м|мл|делал[аи]?|было|раньше)"
)
CONSULTATION_GOAL_RE = re.compile(r"(?iu)(цель|эффект|результат|исправ|убрать|увелич|беспоко|асимметр|как\s+будет)")
CONSULTATION_HISTORY_RE = re.compile(r"(?iu)(раньше|было|была|был|делал[аи]?|не\s+делал[аи]?|первый\s+раз|уже\s+ставил[аи]?)")
PRICE_RE = re.compile(r"(?iu)(цен|стоим|прайс|сколько\s+стоит|руб|₽)")


def route_client_message(
    message: InboundMessage,
    *,
    retrieved_expert_answers: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    autoanswer_threshold: float = 0.82,
    cities: tuple[str, ...] = DEFAULT_CITIES,
    service_aliases: tuple[str, ...] = (),
) -> ClientRoute:
    text = str(message.text or "")
    lowered = text.casefold().replace("ё", "е")
    city = _explicit_city(text, conversation_history, cities)
    unsupported_city = explicit_external_city(text, cities=cities)
    service_key = _service_key_from_text(text)
    has_service_hint = bool(_has_service_hint(lowered, service_aliases) or _history_service_hint(conversation_history, service_aliases))

    if RISK_RE.search(lowered):
        return ClientRoute(
            route="risk_handoff",
            city=city,
            service_key=service_key,
            risk_flags=("risk_case",),
            handoff_reason=HandoffReason.COMPLAINT_OR_RISK.value,
            block_autoanswer_reason="risk_case",
        )

    if _booking_critical(lowered, conversation_history, service_aliases):
        return ClientRoute(
            route="booking_critical_handoff",
            city=city,
            service_key=service_key,
            risk_flags=("urgent", "booking_control"),
            handoff_reason=HandoffReason.BOOKING_CRITICAL.value,
            block_autoanswer_reason="booking_critical",
            metadata={"urgent": True, "sla": "booking_critical"},
        )

    if unsupported_city and _unsupported_city_request(lowered):
        return ClientRoute(
            route="unsupported_city",
            city=unsupported_city,
            service_key=service_key,
            block_autoanswer_reason="unsupported_city",
            metadata={"city": unsupported_city, "reply": fixed_cities_reply(cities)},
        )

    if _aesthetic_expectation_question(lowered, conversation_history):
        if _needs_consultation_details(message, lowered, conversation_history):
            return ClientRoute(
                route="ask_consultation_details",
                city=city,
                service_key=service_key,
                block_autoanswer_reason="consultation_details_required",
                metadata={"reason": "сначала нужно понять, что клиент хочет оценить"},
            )
        return ClientRoute(
            route="expert_expectation_handoff",
            city=city,
            service_key=service_key,
            risk_flags=("expert_expectation",),
            handoff_reason=HandoffReason.EXPERT_EXPECTATION.value,
            block_autoanswer_reason="aesthetic_expectation_guard",
            metadata={"guard": "aesthetic_expectation_guard", "reason": "нельзя автообещать результат по мл"},
        )

    if _individual_expert_question(lowered) and not _has_media(message):
        return ClientRoute(
            route="ask_consultation_details",
            city=city,
            service_key=service_key,
            block_autoanswer_reason="consultation_details_required",
            metadata={"reason": "индивидуальную оценку нельзя дать без фото/вводных"},
        )

    if _has_media(message):
        if _needs_consultation_details(message, lowered, conversation_history):
            return ClientRoute(
                route="ask_consultation_details",
                city=city,
                service_key=service_key,
                block_autoanswer_reason="consultation_details_required",
                metadata={"reason": "клиент прислал вложение без понятного запроса"},
            )
        return ClientRoute(
            route="media_handoff",
            city=city,
            service_key=service_key,
            handoff_reason=HandoffReason.PHOTO_CONSULTATION.value,
            block_autoanswer_reason="media_needs_review",
        )

    if _booking_without_service(lowered, conversation_history, service_aliases):
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


def _needs_consultation_details(
    message: InboundMessage,
    lowered: str,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> bool:
    if _history_consultation_details(conversation_history):
        return False
    if not str(message.text or "").strip() or str(message.text or "").strip().casefold() in {"[фото]", "фото"}:
        return True
    if _has_media(message) and not _consultation_details_complete(lowered):
        return True
    if _individual_expert_question(lowered) and not _has_media(message) and VISUAL_RE.search(lowered):
        return True
    if _aesthetic_expectation_question(lowered, conversation_history):
        return not (_has_media(message) and _consultation_details_complete(lowered))
    return False


def _consultation_details_complete(lowered: str) -> bool:
    has_zone = bool(SERVICE_HINT_RE.search(lowered))
    has_goal = bool(CONSULTATION_GOAL_RE.search(lowered))
    return has_zone and has_goal


def _history_consultation_details(conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> bool:
    recent_user_text = " ".join(
        str(item.get("content") or "") for item in conversation_history[-8:] if str(item.get("role") or "") == "user"
    ).casefold().replace("ё", "е")
    if not recent_user_text:
        return False
    return bool(_consultation_details_complete(recent_user_text) and re.search(r"(?iu)(фото|сним|объ[её]м|мл|хочу|цель|результат)", recent_user_text))


def _aesthetic_expectation_question(lowered: str, conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> bool:
    history_text = " ".join(str(item.get("content") or "") for item in conversation_history[-8:]).casefold().replace("ё", "е")
    source = " ".join(part for part in (lowered, history_text) if part)
    has_volume = bool(AESTHETIC_VOLUME_RE.search(lowered))
    has_result = bool(AESTHETIC_RESULT_RE.search(lowered))
    has_body = bool(AESTHETIC_BODY_RE.search(source))
    if has_volume and has_result:
        return True
    return has_body and has_result and bool(re.search(r"(?iu)(фото|до\s*/?\s*после|как\s+будет|сколько\s+надо|сколько\s+нужно)", lowered))


def _booking_critical(
    lowered: str,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    service_aliases: tuple[str, ...] = (),
) -> bool:
    if not BOOKING_CRITICAL_RE.search(lowered):
        return False
    history_text = " ".join(str(item.get("content") or "") for item in conversation_history[-8:]).casefold().replace("ё", "е")
    booking_context = bool(BOOKING_RE.search(history_text) or ADDRESS_RE.search(history_text) or _has_service_hint(history_text, service_aliases))
    if re.search(r"оплат|предоплат|запись актуальн|актуальна ли запись|точно.*жд|сегодня.*приход|завтра.*приход|опозда", lowered):
        return True
    if ADDRESS_RE.search(lowered) and re.search(r"записан|записана|записаны|запись|я к вам", lowered):
        return True
    if ADDRESS_RE.search(lowered):
        return booking_context
    return booking_context


def _booking_without_service(
    lowered: str,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    service_aliases: tuple[str, ...] = (),
) -> bool:
    if not BOOKING_RE.search(lowered):
        return False
    if _has_service_hint(lowered, service_aliases):
        return False
    return not _history_service_hint(conversation_history, service_aliases)


def _history_service_hint(conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]], service_aliases: tuple[str, ...] = ()) -> bool:
    recent_user_text = " ".join(
        str(item.get("content") or "") for item in conversation_history[-8:] if str(item.get("role") or "") == "user"
    ).casefold()
    return _has_service_hint(recent_user_text, service_aliases)


def _explicit_city(text: str, conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]], cities: tuple[str, ...]) -> str:
    history_text = " ".join(str(item.get("content") or "") for item in conversation_history[-8:] if str(item.get("role") or "") == "user")
    return explicit_supported_city(text, cities=cities, extra_text=history_text)


def _unsupported_city_request(lowered: str) -> bool:
    if PRICE_RE.search(lowered) and not (BOOKING_RE.search(lowered) or ADDRESS_RE.search(lowered)):
        return False
    return bool(BOOKING_RE.search(lowered) or ADDRESS_RE.search(lowered) or re.search(r"(?iu)(при[её]м|принимаете|будете\s+ли|локац|где\s+вы)", lowered))


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


def _has_service_hint(lowered: str, service_aliases: tuple[str, ...] = ()) -> bool:
    if SERVICE_HINT_RE.search(lowered):
        return True
    source = str(lowered or "").casefold().replace("ё", "е")
    for alias in service_aliases:
        normalized = str(alias or "").casefold().replace("ё", "е").strip()
        if len(normalized) >= 4 and normalized in source:
            return True
    return False


def _individual_expert_question(lowered: str) -> bool:
    return bool(INDIVIDUAL_EXPERT_RE.search(lowered))


def _best_retrieved_answer(answers: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> dict[str, Any] | None:
    return answers[0] if answers else None
