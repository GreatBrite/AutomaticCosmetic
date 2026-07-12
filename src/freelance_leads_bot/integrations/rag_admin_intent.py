from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any


IntentLLM = Callable[[str], str]


@dataclass(frozen=True)
class RagAdminIntent:
    intent: str
    confidence: float = 0.0
    scope: dict[str, Any] = field(default_factory=dict)
    operation: dict[str, Any] = field(default_factory=dict)
    answer_text: str = ""
    requires_confirmation: bool = True
    clarification_question: str = ""
    risk_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["risk_flags"] = list(self.risk_flags)
        return data


class RagAdminIntentParser:
    """Structured intent parser for Olga free-form RAG/service commands.

    If an LLM callable is configured, it must return a JSON object matching
    RagAdminIntent. The deterministic fallback keeps the bot safe when model
    parsing is unavailable.
    """

    def __init__(self, llm: IntentLLM | None = None, *, enabled: bool = True) -> None:
        self.llm = llm
        self.enabled = enabled

    def parse(self, text: str, *, context: dict[str, Any] | None = None) -> RagAdminIntent:
        command = str(text or "").strip()
        if self.enabled and self.llm:
            parsed = _parse_llm_json(self.llm(_intent_prompt(command, context or {})))
            if parsed and parsed.confidence >= 0.55 and parsed.intent != "unknown":
                return parsed
        return fallback_rag_admin_intent(command)


def fallback_rag_admin_intent(command: str) -> RagAdminIntent:
    lowered = command.casefold().replace("ё", "е")
    service = _service_from_text(lowered)
    product = "Tesoro Body" if "tesoro" in lowered or "тесоро" in lowered else ""
    city = _city_from_text(lowered)

    percent = _percent_from_text(lowered)
    if percent is not None and any(term in lowered for term in ("дороже", "подним", "увелич", "индекс", "+", "процент")):
        return RagAdminIntent(
            intent="price_percent_change",
            confidence=0.78 if service else 0.62,
            scope=_scope(service=service, product=product, city=city),
            operation={"type": "increase_percent", "value": percent},
            risk_flags=("price",),
        )

    exact = _price_exact_from_text(lowered)
    if exact:
        return RagAdminIntent(
            intent="price_exact_replace",
            confidence=0.72,
            scope=_scope(service=service, product=product, city=city, volume_ml=exact.get("volume_ml")),
            operation={"type": "replace_price", **exact},
            risk_flags=("price",),
        )

    duration = _duration_from_text(lowered)
    if duration:
        return RagAdminIntent(
            intent="effect_duration_update",
            confidence=0.82 if product or service else 0.67,
            scope=_scope(service=service, product=product, city=city),
            operation={"type": "set_effect_duration", "value": duration},
            risk_flags=("effect_duration",),
        )

    remembered = _remember_text(command)
    if remembered:
        return RagAdminIntent(
            intent="remember_answer",
            confidence=0.86,
            scope=_scope(service=service, product=product, city=city),
            answer_text=remembered,
        )

    if any(marker in lowered for marker in ("добавь услугу", "новая услуга", "теперь делаем")):
        title = _service_title_from_command(command)
        return RagAdminIntent(
            intent="service_add",
            confidence=0.76 if title else 0.52,
            scope=_scope(service=service or title, product=product, city=city),
            operation={"type": "service_add", "title": title or service},
        )

    if any(marker in lowered for marker in ("переименуй", "название услуги", "теперь называется")):
        return RagAdminIntent(
            intent="service_update",
            confidence=0.68,
            scope=_scope(service=service, product=product, city=city),
            operation={"type": "service_update", "title": _new_title_from_command(command)},
        )

    if any(marker in lowered for marker in ("больше не делаем", "не делаем", "отключи услугу", "скрой услугу")):
        return RagAdminIntent(
            intent="service_disable",
            confidence=0.75 if service else 0.55,
            scope=_scope(service=service, product=product, city=city),
            operation={"type": "set_service_status", "status": "hidden"},
        )

    if any(marker in lowered for marker in ("удали услугу", "удалить услугу")):
        return RagAdminIntent(
            intent="service_delete",
            confidence=0.75 if service else 0.55,
            scope=_scope(service=service, product=product, city=city),
            operation={"type": "set_service_status", "status": "deleted"},
        )

    if "очн" in lowered and any(marker in lowered for marker in ("не говор", "не рекоменд", "не склон")):
        return RagAdminIntent(
            intent="policy_update",
            confidence=0.84,
            scope=_scope(service=service, product=product, city=city, topic="consultation_policy"),
            answer_text=(
                "Не рекомендовать очную консультацию как следующий шаг по умолчанию. "
                "Если нужна дополнительная оценка, один раз предложить онлайн-разбор с Ольгой и запросить недостающие данные/фото."
            ),
        )

    if any(marker in lowered for marker in ("устарел", "не актуаль", "не использ", "не отвечай так", "это не надо")):
        return RagAdminIntent(
            intent="deprecate_knowledge",
            confidence=0.7,
            scope=_scope(service=service, product=product, city=city),
            operation={"type": "deprecate"},
        )

    return RagAdminIntent(
        intent="unknown",
        confidence=0.0,
        clarification_question="Не поняла, какое знание или услугу нужно изменить. Напишите услугу и что сделать.",
    )


def _intent_prompt(command: str, context: dict[str, Any]) -> str:
    return (
        "Extract a strict JSON object for Olga's RAG/service admin command. "
        "Do not perform actions. Return only JSON with keys: intent, confidence, scope, operation, "
        "answer_text, requires_confirmation, clarification_question, risk_flags.\n"
        "Allowed intents: price_percent_change, price_exact_replace, effect_duration_update, "
        "policy_update, remember_answer, deprecate_knowledge, service_add, service_update, "
        "service_disable, service_delete, unknown.\n"
        f"Context: {json.dumps(context, ensure_ascii=False)}\n"
        f"Command: {command}"
    )


def _parse_llm_json(raw: str) -> RagAdminIntent | None:
    try:
        payload = json.loads(str(raw or "").strip())
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", str(raw or ""), flags=re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return RagAdminIntent(
        intent=str(payload.get("intent") or "unknown"),
        confidence=float(payload.get("confidence") or 0),
        scope=dict(payload.get("scope") or {}),
        operation=dict(payload.get("operation") or {}),
        answer_text=str(payload.get("answer_text") or ""),
        requires_confirmation=bool(payload.get("requires_confirmation", True)),
        clarification_question=str(payload.get("clarification_question") or ""),
        risk_flags=tuple(payload.get("risk_flags") or ()),
    )


def _scope(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in ("", None)}


def _service_from_text(text: str) -> str:
    if any(term in text for term in ("ягод", "поп", "tesoro", "тесоро")):
        return "ягодицы"
    if "груд" in text:
        return "грудь"
    if "губ" in text:
        return "губы"
    if "ботокс" in text or "ботулин" in text:
        return "ботокс"
    if "волос" in text or "голов" in text:
        return "кожа головы"
    return ""


def _city_from_text(text: str) -> str:
    for raw, city in (("спб", "Санкт-Петербург"), ("питер", "Санкт-Петербург"), ("ростов", "Ростов-на-Дону"), ("краснодар", "Краснодар"), ("москва", "Москва")):
        if raw in text:
            return city
    return ""


def _percent_from_text(text: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text) or re.search(r"процент(?:ов)?\s+на\s+(\d+(?:[.,]\d+)?)", text) or re.search(r"на\s+(\d+(?:[.,]\d+)?)\s+процент", text)
    if not match:
        words = {"пять": 5.0, "десять": 10.0, "пятнадцать": 15.0, "двадцать": 20.0}
        for word, value in words.items():
            if re.search(rf"(?:процент(?:ов)?\s+на\s+{word}|на\s+{word}\s+процент)", text):
                return value
        return None
    return float(match.group(1).replace(",", "."))


def _duration_from_text(text: str) -> str:
    match = re.search(r"\bдо\s+(\d+(?:[.,]\d+)?)\s*(лет|года|год|месяц(?:ев|а)?|мес)\b", text)
    if not match:
        return ""
    value = float(match.group(1).replace(",", "."))
    rendered = str(int(value)) if value.is_integer() else str(value).replace(".", ",")
    return f"{rendered} месяцев" if match.group(2).startswith("мес") else f"{rendered} лет"


def _price_exact_from_text(text: str) -> dict[str, Any]:
    price_match = None
    for match in re.finditer(r"(?<!\d)(\d[\d\s]{2,})(?:\s*(?:₽|руб|р\b))?", text):
        if re.match(r"\s*мл\b", text[match.end() : match.end() + 8]):
            continue
        value = int(match.group(1).replace(" ", ""))
        if value >= 1000:
            price_match = match
            break
    if not price_match:
        return {}
    volume_match = re.search(r"\b(\d{2,4})\s*мл\b", text)
    return {
        "new_value": int(price_match.group(1).replace(" ", "")),
        "volume_ml": int(volume_match.group(1)) if volume_match else None,
    }


def _remember_text(command: str) -> str:
    match = re.search(r"(?isu)\bзапомни(?:\s+вот\s+так)?[:：]?\s*(.+)$", command)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _service_title_from_command(command: str) -> str:
    match = re.search(r"(?isu)(?:добавь услугу|новая услуга|теперь делаем)[:：]?\s*(.+)$", command)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _new_title_from_command(command: str) -> str:
    match = re.search(r"(?isu)(?:теперь называется|переименуй.+?в)[:：]?\s*(.+)$", command)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""
