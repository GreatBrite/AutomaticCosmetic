from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ROOT
from .city_utils import explicit_supported_city
from .config import DEFAULT_CITIES
from .models import HandoffReason, InboundMessage


DEFAULT_SERVICE_INTAKE_PATH = ROOT / "data" / "service_intake_specs.json"

INTAKE_ROUTES = {"ask_consultation_details", "media_handoff", "expert_expectation_handoff"}
IMMEDIATE_ROUTES = {"risk_handoff", "booking_critical_handoff", "unsupported_city", "ask_service", "ask_city"}

CONTACT_RE = re.compile(
    r"(?iu)(?:@\w{3,}|t\.me/\w+|vk\.com/\w+|wa\.me/\d+|(?:\+?\d[\d\s().-]{8,}\d)|"
    r"(?:телеграм|telegram|tg|ватсап|whatsapp|вотсап|вк|instagram|инстаграм)\s*[:\-]?\s*[\w@.+-]{3,})"
)
CONSULTATION_RE = re.compile(r"(?iu)(консультац|проконсульт|онлайн[-\s]?разбор|онлайн[-\s]?конс)")
PRICE_RE = re.compile(r"(?iu)(\bцен[ауые]?\b|ценник|стоим|прайс|сколько\s+стоит|руб|₽)")
ADDRESS_RE = re.compile(r"(?iu)(адрес|метро|территориально|где|локац|как пройти|вход)")
BOOKING_RE = re.compile(r"(?iu)(запис|свобод|окош|время|слот|следующ|недел|при[её]м)")
VISUAL_RE = re.compile(r"(?iu)(фото|сним|визуальн|посмотрит|посмотрите|оценит|оцените|асимметр|форма|как\s+будет)")
GOAL_RE = re.compile(
    r"(?iu)(хочу|цель|эффект|результат|исправ|убрать|увелич|подтян|омолод|беспоко|асимметр|"
    r"морщин|объ[её]м|мл|форма|как\s+будет|достаточн|хватит|размер)"
)
BODY_EXPECTATION_RE = re.compile(
    r"(?iu)(?:\b\d{2,5}\s*(?:мл|милли?литр\w*)\b|(?:^|[^\d])(?:300|400|500|600|800|1000|1200)(?:[^\d]|$)|"
    r"объ[её]м|размер|хватит|достаточн|как\s+будет|сколько\s+надо|сколько\s+нужно|до\s*/?\s*после)"
)
RISK_RE = re.compile(r"(?iu)(беремен|кормлен|гв\b|аллерг|осложн|от[её]к|боль|температур|гной|инфекц|ожог|задыха|трудно дыш|жалоб)")
INTAKE_ASK_MARKER_RE = re.compile(r"(?iu)(чтобы\s+ольга|что\s+именно\s+хотите\s+оценить|какая\s+зона\s+интересует|оставьте.*мессендж)")


DEFAULT_INTAKE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "service_key": "guby",
        "title": "Губы",
        "aliases": ("губы", "губ", "увеличение губ", "контурная пластика губ"),
        "required_fields": ("zone", "goal"),
        "visual_photo_required": True,
    },
    {
        "service_key": "grud",
        "title": "Грудь",
        "aliases": ("грудь", "груди", "увеличение груди", "контурная пластика груди"),
        "required_fields": ("goal",),
        "immediate_expectation_handoff": True,
    },
    {
        "service_key": "yagodicy",
        "title": "Ягодицы",
        "aliases": ("ягодицы", "ягодиц", "попа", "попу", "увеличение ягодиц", "tesoro", "тесоро"),
        "required_fields": ("goal",),
        "immediate_expectation_handoff": True,
    },
    {
        "service_key": "face_contour",
        "title": "Контурная пластика лица",
        "aliases": ("контурная пластика лица", "лицо", "скулы", "углы", "подбородок", "носогубные складки", "носослезная борозда", "профиль джоли", "морщины"),
        "required_fields": ("zone", "goal"),
        "visual_photo_required": True,
    },
    {
        "service_key": "botoks",
        "title": "Ботокс / ботулинотерапия",
        "aliases": ("ботокс", "ботулинотерапия", "диспорт", "лоб", "межбровье", "глаза", "нефертити", "гипергидроз"),
        "required_fields": ("zone", "goal"),
    },
    {
        "service_key": "skin_quality",
        "title": "Биоревитализация / мезотерапия / качество кожи",
        "aliases": ("биоревитализация", "мезотерапия", "качество кожи", "пилинг", "купероз", "восстановление кожи", "колост", "новакутан", "белотеро гидро"),
        "required_fields": ("zone", "goal"),
    },
    {
        "service_key": "threads",
        "title": "Нити",
        "aliases": ("нити", "нитевой лифтинг", "мезонити", "мононити", "коги", "cog"),
        "required_fields": ("zone", "goal"),
        "visual_photo_required": True,
    },
    {
        "service_key": "lipolytics_body",
        "title": "Липолитики тело",
        "aliases": ("липолитик", "липолитики", "тело", "живот", "бока", "бедра", "бедер"),
        "required_fields": ("zone", "goal"),
    },
    {
        "service_key": "scalp",
        "title": "Кожа головы / волосы",
        "aliases": ("кожа головы", "волосы", "выпадение волос", "рост волос", "ломкость волос"),
        "required_fields": ("goal",),
    },
    {
        "service_key": "filler_removal",
        "title": "Выведение филлера",
        "aliases": ("выведение филлера", "убрать филлер", "растворить филлер", "лонгидаза"),
        "required_fields": ("zone", "goal"),
        "visual_photo_required": True,
    },
    {
        "service_key": "consultation",
        "title": "Консультация в мессенджере/соцсети",
        "aliases": ("консультация", "проконсультироваться", "онлайн консультация", "онлайн-консультация"),
        "required_fields": ("contact",),
    },
)


@dataclass(frozen=True)
class ServiceIntakeSpec:
    service_key: str
    title: str
    aliases: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    visual_photo_required: bool = False
    immediate_expectation_handoff: bool = False


@dataclass(frozen=True)
class ServiceIntakeDecision:
    action: str
    reply: str = ""
    reason: HandoffReason = HandoffReason.MISSING_DATA
    summary: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class ServiceIntakeEngine:
    def __init__(self, path: Path | str = DEFAULT_SERVICE_INTAKE_PATH) -> None:
        self.path = Path(path)
        self.specs = _load_specs(self.path)

    def evaluate(
        self,
        message: InboundMessage,
        *,
        route: str = "",
        conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        cities: tuple[str, ...] = DEFAULT_CITIES,
    ) -> ServiceIntakeDecision:
        text = str(message.text or "").strip()
        source = _source_text(message, conversation_history)
        lowered = _normalize(source)
        current = _normalize(text)
        if route in IMMEDIATE_ROUTES or RISK_RE.search(current):
            return ServiceIntakeDecision(action="continue")
        if _should_skip_intake_for_operational_question(current):
            return ServiceIntakeDecision(action="continue")

        spec = self.resolve(message, conversation_history=conversation_history)
        is_consultation = bool(CONSULTATION_RE.search(lowered))
        has_media = _has_media(message)
        is_expert_or_visual = route in INTAKE_ROUTES or has_media or bool(VISUAL_RE.search(current))
        if is_consultation and (not spec or spec.service_key != "consultation"):
            spec = self.spec_by_key("consultation")
        if not spec or not (is_consultation or is_expert_or_visual):
            return ServiceIntakeDecision(action="continue")

        fields = _extract_fields(message, conversation_history, spec=spec, cities=cities)
        if _body_expectation_requires_handoff(spec, current, lowered):
            return ServiceIntakeDecision(
                action="handoff",
                reply="По объёму и ожидаемому результату лучше не обещать вслепую. Передам Ольге, она посмотрит и сориентирует точнее.",
                reason=HandoffReason.EXPERT_EXPECTATION,
                summary=_compose_card(
                    message,
                    spec=spec,
                    fields=fields,
                    missing_fields=(),
                    forced_reason="Вопрос про объём/ожидаемый результат; нельзя автообещать результат по мл.",
                ),
                fields=fields,
                metadata={"service_key": spec.service_key, "intake": "immediate_expectation_handoff"},
            )

        missing = _missing_fields(spec, fields, message=message, is_consultation=is_consultation, current=current)
        already_asked = _history_has_intake_question(conversation_history)
        if missing and not already_asked:
            return ServiceIntakeDecision(
                action="ask_intake_details",
                reply=_clarifying_reply(spec, missing, has_media=has_media, is_consultation=is_consultation),
                fields=fields,
                missing_fields=tuple(missing),
                metadata={"service_key": spec.service_key, "intake": "ask_missing_fields"},
            )

        if route in INTAKE_ROUTES or has_media or is_consultation or already_asked:
            return ServiceIntakeDecision(
                action="handoff",
                reply=_client_handoff_reply(spec, is_consultation=is_consultation),
                reason=HandoffReason.PHOTO_CONSULTATION if has_media else HandoffReason.EXPERT_EXPECTATION,
                summary=_compose_card(message, spec=spec, fields=fields, missing_fields=missing),
                fields=fields,
                missing_fields=tuple(missing),
                metadata={"service_key": spec.service_key, "intake": "card_handoff"},
            )
        return ServiceIntakeDecision(action="continue")

    def resolve(
        self,
        message: InboundMessage,
        *,
        conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    ) -> ServiceIntakeSpec | None:
        source = _normalize(_source_text(message, conversation_history))
        if not source:
            return None
        scored: list[tuple[int, int, ServiceIntakeSpec]] = []
        for index, spec in enumerate(self.specs):
            score = 0
            for alias in (spec.title, *spec.aliases):
                normalized = _normalize(alias)
                if normalized and normalized in source:
                    score = max(score, len(normalized))
            if score:
                scored.append((score, -index, spec))
        if not scored:
            return None
        scored.sort(reverse=True)
        return scored[0][2]

    def spec_by_key(self, service_key: str) -> ServiceIntakeSpec | None:
        return next((spec for spec in self.specs if spec.service_key == service_key), None)


def _load_specs(path: Path) -> tuple[ServiceIntakeSpec, ...]:
    rows: Any = DEFAULT_INTAKE_SPECS
    try:
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        rows = DEFAULT_INTAKE_SPECS
    specs: list[ServiceIntakeSpec] = []
    iterable = rows if isinstance(rows, (list, tuple)) else []
    for row in iterable:
        if not isinstance(row, dict):
            continue
        key = str(row.get("service_key") or "").strip()
        title = str(row.get("title") or key).strip()
        if not key or not title:
            continue
        specs.append(
            ServiceIntakeSpec(
                service_key=key,
                title=title,
                aliases=tuple(str(item).strip() for item in row.get("aliases") or () if str(item).strip()),
                required_fields=tuple(str(item).strip() for item in row.get("required_fields") or () if str(item).strip()),
                visual_photo_required=bool(row.get("visual_photo_required")),
                immediate_expectation_handoff=bool(row.get("immediate_expectation_handoff")),
            )
        )
    return tuple(specs) or tuple(ServiceIntakeSpec(**row) for row in DEFAULT_INTAKE_SPECS)


def _source_text(message: InboundMessage, conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    parts = [str(message.text or "")]
    if message.listing:
        parts.extend([message.listing.title, message.listing.price_string, message.listing.city])
    parts.extend(str(item.get("content") or "") for item in conversation_history[-8:] if str(item.get("role") or "") == "user")
    return " ".join(part for part in parts if part)


def _normalize(value: str) -> str:
    return str(value or "").casefold().replace("ё", "е")


def _has_media(message: InboundMessage) -> bool:
    metadata = message.metadata or {}
    return bool(
        message.has_photo
        or metadata.get("has_photo")
        or metadata.get("has_video")
        or metadata.get("has_file")
        or metadata.get("media_urls")
        or metadata.get("photo_urls")
        or metadata.get("voice_id")
        or metadata.get("voice_transcription_error")
    )


def _should_skip_intake_for_operational_question(current: str) -> bool:
    if PRICE_RE.search(current):
        return True
    if ADDRESS_RE.search(current):
        return True
    return bool(BOOKING_RE.search(current) and not CONSULTATION_RE.search(current))


def _extract_fields(
    message: InboundMessage,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    spec: ServiceIntakeSpec,
    cities: tuple[str, ...],
) -> dict[str, str]:
    source = _source_text(message, conversation_history)
    dialog_source = " ".join(
        part
        for part in (
            str(message.text or ""),
            " ".join(str(item.get("content") or "") for item in conversation_history[-8:] if str(item.get("role") or "") == "user"),
        )
        if part
    )
    current_and_history = _normalize(source)
    dialog_normalized = _normalize(dialog_source)
    city = explicit_supported_city(str(message.text or ""), cities=cities, extra_text=" ".join(str(item.get("content") or "") for item in conversation_history[-8:]))
    fields = {
        "service": spec.title,
        "question": str(message.text or "").strip() or "[медиа без текста]",
        "zone": _extract_zone(dialog_normalized, spec) or _extract_zone(current_and_history, spec),
        "goal": "указана в диалоге" if GOAL_RE.search(dialog_normalized) else "",
        "city": city,
        "contact": _extract_contact(source),
        "photo": "есть" if _has_media(message) else "",
    }
    return fields


def _extract_zone(source: str, spec: ServiceIntakeSpec) -> str:
    zone_patterns = (
        ("губ", "губы"),
        ("груд", "грудь"),
        ("ягод|поп", "ягодицы"),
        ("скул", "скулы"),
        ("подбород", "подбородок"),
        ("угл", "углы нижней челюсти"),
        ("носогуб", "носогубные складки"),
        ("нососл", "носослезная борозда"),
        ("лоб", "лоб"),
        ("межбров", "межбровье"),
        ("глаз", "зона вокруг глаз"),
        ("кожа головы|волос", "кожа головы/волосы"),
        ("живот", "живот"),
        ("бок", "бока"),
        ("бедр", "бедра"),
    )
    for pattern, label in zone_patterns:
        if re.search(pattern, source, flags=re.IGNORECASE):
            return label
    if spec.service_key in {"guby", "grud", "yagodicy", "scalp"}:
        return spec.title.lower()
    return ""


def _extract_contact(source: str) -> str:
    match = CONTACT_RE.search(source)
    return match.group(0).strip() if match else ""


def _missing_fields(
    spec: ServiceIntakeSpec,
    fields: dict[str, str],
    *,
    message: InboundMessage,
    is_consultation: bool,
    current: str,
) -> list[str]:
    required = list(spec.required_fields)
    if spec.visual_photo_required and VISUAL_RE.search(current) and not _has_media(message):
        required.append("photo")
    if is_consultation and "contact" not in required:
        required.append("contact")
    seen: set[str] = set()
    missing: list[str] = []
    for field_name in required:
        if field_name in seen:
            continue
        seen.add(field_name)
        if not fields.get(field_name):
            missing.append(field_name)
    return missing


def _body_expectation_requires_handoff(spec: ServiceIntakeSpec, current: str, source: str) -> bool:
    if not spec.immediate_expectation_handoff:
        return False
    body_service = spec.service_key in {"grud", "yagodicy"}
    return body_service and bool(BODY_EXPECTATION_RE.search(current) or BODY_EXPECTATION_RE.search(source))


def _history_has_intake_question(conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("role") or "") == "assistant" and INTAKE_ASK_MARKER_RE.search(str(item.get("content") or ""))
        for item in conversation_history[-8:]
    )


def _clarifying_reply(spec: ServiceIntakeSpec, missing: list[str], *, has_media: bool, is_consultation: bool) -> str:
    if is_consultation:
        return (
            "Чтобы Ольга могла ответить в переписке, напишите, пожалуйста, по какой процедуре вопрос "
            "и оставьте номер или аккаунт удобного мессенджера/соцсети."
        )
    if has_media and ("goal" in missing or set(missing) >= {"zone", "goal"}):
        return "Что именно хотите оценить по фото: какая зона интересует и какой результат хотите получить?"
    labels = {
        "zone": "какая зона интересует",
        "goal": "что хотите получить в результате",
        "photo": "приложите фото при хорошем освещении",
        "city": "в каком городе вам удобно",
        "contact": "оставьте номер или аккаунт удобного мессенджера/соцсети",
    }
    questions = [labels[field_name] for field_name in missing if field_name in labels]
    if not questions:
        return "Уточните, пожалуйста, детали запроса, и я передам Ольге понятную карточку."
    joined = ", ".join(questions[:3])
    return f"Чтобы Ольга точнее сориентировала по услуге «{spec.title}», уточните, пожалуйста: {joined}."


def _client_handoff_reply(spec: ServiceIntakeSpec, *, is_consultation: bool) -> str:
    if is_consultation:
        return "Передам контакт и вопрос Ольге, она сориентирует вас в переписке."
    return f"Передам Ольге данные по услуге «{spec.title}», она посмотрит и сориентирует точнее."


def _compose_card(
    message: InboundMessage,
    *,
    spec: ServiceIntakeSpec,
    fields: dict[str, str],
    missing_fields: list[str] | tuple[str, ...],
    forced_reason: str = "",
) -> str:
    missing_text = ", ".join(_field_label(item) for item in missing_fields) if missing_fields else "нет"
    clarified = ", ".join(
        label
        for key, label in (
            ("zone", "зона"),
            ("goal", "цель"),
            ("city", "город"),
            ("contact", "контакт"),
            ("photo", "фото/медиа"),
        )
        if fields.get(key)
    ) or "нет"
    lines = [
        "Нужна консультация Ольги",
        f"Услуга: {fields.get('service') or spec.title}",
        f"Вопрос клиента: {fields.get('question') or '[не указан]'}",
        f"Цель: {fields.get('goal') or 'не указана'}",
        f"Зона: {fields.get('zone') or 'не указана'}",
        f"Город: {fields.get('city') or 'не указан'}",
        f"Контакт: {fields.get('contact') or 'не указан'}",
        f"Фото/медиа: {fields.get('photo') or 'не приложено'}",
        f"Что уже уточнил бот: {clarified}",
        f"Чего не хватает: {missing_text}",
        f"Что нужно решить Ольге: дать безопасный ответ клиенту без обещаний результата.",
    ]
    if forced_reason:
        lines.append(f"Причина передачи без доп. уточнений: {forced_reason}")
    if message.listing and message.listing.has_listing:
        listing_parts = [part for part in (message.listing.title, message.listing.price_string, message.listing.city) if part]
        if listing_parts:
            lines.append(f"Объявление: {' | '.join(listing_parts)}")
    return "\n".join(lines)


def _field_label(field_name: str) -> str:
    return {
        "zone": "зона",
        "goal": "цель",
        "photo": "фото",
        "city": "город",
        "contact": "контакт мессенджера/соцсети",
    }.get(field_name, field_name)
