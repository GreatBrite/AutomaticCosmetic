from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from .expert_rag import APPROVED, ExpertRagStore, infer_metadata
from .models import InboundMessage
from .service_catalog import ACTIVE, HIDDEN, ServiceCatalogStore, service_catalog_from_rag_metadata

TEMPORAL_FACT_RE = re.compile(
    r"(?iu)(?:\b(?:сегодня|завтра|послезавтра)\b|"
    r"\b(?:понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресень[еия])\b|"
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|"
    r"\b\d{1,2}\s*(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b|"
    r"\b\d{1,2}:\d{2}\b|есть\s+окн|свободн|можно\s+запис|адрес)"
)


@dataclass(frozen=True)
class RagRetrievalRequest:
    channel: str
    text: str
    city: str = ""
    service_key: str = ""
    service_hint: str = ""
    client_context: dict[str, Any] = field(default_factory=dict)
    risk_policy: str = "client_autoanswer"
    limit: int = 5
    min_score: float = 0.0


@dataclass(frozen=True)
class RagRetrievalResult:
    answers: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0
    safe_for_autoanswer: bool = False
    handoff_reason: str = ""
    conflicts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "answers": list(self.answers),
            "confidence": self.confidence,
            "safe_for_autoanswer": self.safe_for_autoanswer,
            "handoff_reason": self.handoff_reason,
            "conflicts": list(self.conflicts),
        }


class RagRetrievalService:
    def __init__(self, store: ExpertRagStore, service_catalog: ServiceCatalogStore | None = None) -> None:
        self.store = store
        self.service_catalog = service_catalog or ServiceCatalogStore()

    def retrieve(self, request: RagRetrievalRequest) -> RagRetrievalResult:
        text = _strip_batch_system_text(request.text)
        request = replace(request, text=text)
        query = " ".join(part for part in (text, request.service_hint, request.service_key) if part)
        service_filter = self._service_filter(request)
        request_blocker = _request_autoanswer_blocker(request, service_filter)
        matches = self.store.search(
            query,
            status=APPROVED,
            limit=max(request.limit * 3, request.limit),
            min_score=request.min_score,
            city=request.city,
            service=service_filter,
            exclude_risk_levels=("high",),
        )
        answers: list[dict[str, Any]] = []
        conflicts: list[str] = []
        for answer, score in matches:
            payload = answer.to_dict(score=score)
            if not _autoanswer_allowed(payload):
                continue
            if not self._channel_allowed(payload, request.channel):
                continue
            if not self._service_allowed(payload, request):
                continue
            answers.append(payload)
            if len(answers) >= request.limit:
                break
        confidence = float(answers[0].get("score") or 0) if answers else 0.0
        if _price_conflict(answers[:3]):
            conflicts.append("price_conflict")
        if request_blocker:
            conflicts.append(request_blocker)
        return RagRetrievalResult(
            answers=tuple(answers),
            confidence=confidence,
            safe_for_autoanswer=bool(answers and not conflicts),
            handoff_reason=_handoff_reason(conflicts, answers),
            conflicts=tuple(conflicts),
        )

    def retrieve_for_message(self, message: InboundMessage, *, min_score: float, limit: int = 5) -> RagRetrievalResult:
        listing_title = message.listing.title if message.listing else ""
        text = " ".join(part for part in (message.text, listing_title) if part)
        return self.retrieve(
            RagRetrievalRequest(
                channel=message.channel.value if hasattr(message.channel, "value") else str(message.channel),
                text=text,
                service_hint=listing_title,
                limit=limit,
                min_score=min_score,
            )
        )

    def _channel_allowed(self, answer: dict[str, Any], channel: str) -> bool:
        metadata = answer.get("metadata") if isinstance(answer.get("metadata"), dict) else {}
        visibility = metadata.get("visibility") or metadata.get("channels") or ()
        if isinstance(visibility, str):
            visibility = [visibility]
        if visibility and channel not in visibility:
            return False
        service_key = service_catalog_from_rag_metadata(metadata)
        service = self.service_catalog.get(service_key) if service_key else None
        return not service or not service.visibility or channel in service.visibility

    def _service_allowed(self, answer: dict[str, Any], request: RagRetrievalRequest) -> bool:
        metadata = answer.get("metadata") if isinstance(answer.get("metadata"), dict) else {}
        service_key = service_catalog_from_rag_metadata(metadata)
        service = self.service_catalog.get(service_key) if service_key else None
        if not service and request.service_hint:
            service = self.service_catalog.resolve(request.service_hint)
        if not service:
            return True
        if service.status not in {ACTIVE, HIDDEN}:
            return False
        if service.status == HIDDEN:
            return False
        return True

    def _service_filter(self, request: RagRetrievalRequest) -> str:
        if request.service_key:
            return request.service_key
        for source in (request.service_hint, request.text):
            service = infer_metadata(source).get("service") if source else ""
            if service:
                return service
        resolved = self.service_catalog.resolve(request.service_hint or request.text)
        return resolved.service_key if resolved else ""


def _autoanswer_allowed(answer: dict[str, Any]) -> bool:
    metadata = answer.get("metadata") if isinstance(answer.get("metadata"), dict) else {}
    if answer.get("status") != APPROVED or metadata.get("autoanswer_allowed") is False:
        return False
    if answer.get("expires_at") or metadata.get("valid_until") or metadata.get("expires_at"):
        return True
    text = "\n".join(str(answer.get(key) or "") for key in ("question_canonical", "answer_client", "answer_internal", "topic"))
    return not TEMPORAL_FACT_RE.search(text)


def _strip_batch_system_text(text: str) -> str:
    return re.sub(r"(?iu)\bклиент прислал несколько сообщений подряд:?\s*", " ", str(text or "")).strip()


def _price_conflict(answers: list[dict[str, Any]]) -> bool:
    prices: set[str] = set()
    for answer in answers:
        text = str(answer.get("answer_client") or "")
        found = tuple(sorted(re.findall(r"\b\d[\d\s]{3,}\b", text)))
        if found:
            prices.add("|".join(found))
    return len(prices) > 1


def _request_autoanswer_blocker(request: RagRetrievalRequest, service_filter: str) -> str:
    text = str(request.text or "").casefold().replace("ё", "е")
    if re.search(r"беремен|кормлен|аллерг|осложн|отек|боль|температур|гной|инфекц|операц|онколог|эпилеп|диабет", text):
        return "risk_case"
    if re.search(r"\b(встреч|прием|приём|личн|очно|очная)\b", text) and not service_filter:
        return "personal_meeting_without_service"
    if request.city == "" and re.search(r"\b(адрес|слот|запис|окн|время|когда|город)\b", text):
        return "city_required"
    return ""


def _handoff_reason(conflicts: list[str], answers: list[dict[str, Any]]) -> str:
    if not answers:
        return "no_approved_knowledge"
    if not conflicts:
        return ""
    if "risk_case" in conflicts:
        return "risk_case"
    if "personal_meeting_without_service" in conflicts:
        return "booking_ambiguous"
    if "city_required" in conflicts:
        return "city_required"
    return "conflict"
