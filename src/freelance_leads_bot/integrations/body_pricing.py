from __future__ import annotations

import re
from typing import Any


MODEL_BODY_PRICES: dict[int, int] = {
    300: 50_000,
    400: 70_000,
    500: 85_000,
    600: 100_000,
}

STANDARD_BODY_PRICES: dict[int, int] = {
    200: 55_000,
    300: 80_000,
    400: 110_000,
    500: 130_000,
    600: 145_000,
}

BODY_PRICE_SCOPE_RE = re.compile(r"(?iu)(груд|ягод|поп|тесоро|tesoro|body|контурн\w+\s+(?:пластик\w+|коррекц\w+)\s+тел)")
FACE_SCOPE_RE = re.compile(
    r"(?iu)(контурн\w*\s+пластик\w*\s+лиц|лиц[аоеу]?|скул|угл[ыо]?\s+нижн\w+\s+челюст|"
    r"челюст|подбород|нососл[её]з|носогуб|нефертити|овал|брыл|морщ)"
)
PRICE_QUESTION_RE = re.compile(
    r"(?iu)(цен|стоим|прайс|сколько\s+(?:стоит|будет|по\s+цене)|какая\s+цена|"
    r"за\s+\d|руб|₽|\d+\s*(?:тыс|тысяч|000))"
)
SHORT_PRICE_QUESTION_RE = re.compile(r"(?iu)(сколько\??|получается|итого)")
VOLUME_RE = re.compile(r"(?iu)(?<!\d)(200|300|400|500|600)\s*(?:мл|ml|милли?литр\w*)?")
NON_MODEL_RE = re.compile(r"(?iu)(не\s+как\s+модель|не\s+для\s+модел\w+|не\s+модель|обычн\w+|стандартн\w+|как\s+пациент|пациент)")
MODEL_RE = re.compile(r"(?iu)(как\s+модель|модель|модел)")
SIDE_OR_TOTAL_RE = re.compile(r"(?iu)(обе|оба|две|два|кажд\w+|сторон\w+|ягодиц\w+\s+вместе|вместе|140\s*(?:тыс|000)?)")


def body_price_reply_for_message(
    message: Any,
    *,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> str:
    listing_title = str(getattr(getattr(message, "listing", None), "title", "") or "")
    return body_price_reply(str(getattr(message, "text", "") or ""), listing_title=listing_title, conversation_history=conversation_history)


def body_price_reply(
    text: str,
    *,
    listing_title: str = "",
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> str:
    current = str(text or "")
    history_text = " ".join(
        str(item.get("content") or "") for item in list(conversation_history)[-6:] if str(item.get("role") or "") == "user"
    )
    history_volume_text = " ".join(str(item.get("content") or "") for item in list(conversation_history)[-6:])
    scope_source = f"{current}\n{listing_title}\n{history_text}"
    if FACE_SCOPE_RE.search(listing_title) and not BODY_PRICE_SCOPE_RE.search(f"{current}\n{history_text}"):
        return ""
    volumes = _requested_volumes(current) or _requested_volumes(history_text) or _requested_volumes(history_volume_text)
    side_or_total_question = bool(SIDE_OR_TOTAL_RE.search(f"{current}\n{history_text}"))
    if not BODY_PRICE_SCOPE_RE.search(scope_source):
        return ""
    price_source = f"{current}\n{history_text}"
    if not PRICE_QUESTION_RE.search(price_source) and not (
        volumes and (SHORT_PRICE_QUESTION_RE.search(price_source) or side_or_total_question)
    ):
        return ""

    price_kind = _requested_price_kind(f"{current}\n{listing_title}\n{history_text}")
    if price_kind == "both":
        price_kind = _requested_history_price_kind(history_volume_text)
    service = _body_service_name(scope_source)

    if volumes:
        return _volume_price_reply(service, volumes, price_kind, side_or_total_question=side_or_total_question)
    return _full_price_reply(service, price_kind=price_kind, side_or_total_question=side_or_total_question)


def body_price_guard_reply(
    reply: str,
    message: Any,
    *,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> str:
    canonical = body_price_reply_for_message(message, conversation_history=conversation_history)
    if not canonical:
        return ""
    reply_text = str(reply or "")
    if not reply_text.strip():
        return canonical
    if PRICE_QUESTION_RE.search(reply_text) or re.search(r"(?iu)\d[\d\s]{2,}\s*(?:₽|руб)?", reply_text):
        return canonical
    return ""


def canonical_body_price_table_text() -> str:
    return (
        "Прайс грудь/ягодицы: "
        "как модель: 300 мл — 50 000 ₽, 400 мл — 70 000 ₽, 500 мл — 85 000 ₽, 600 мл — 100 000 ₽; "
        "не как модель: 200 мл — 55 000 ₽, 300 мл — 80 000 ₽, 400 мл — 110 000 ₽, 500 мл — 130 000 ₽, 600 мл — 145 000 ₽. "
        "Цена за объём препарата, не за сторону/ягодицу; не умножай на две стороны."
    )


def _requested_volumes(text: str) -> list[int]:
    seen: set[int] = set()
    volumes: list[int] = []
    for match in VOLUME_RE.finditer(str(text or "")):
        value = int(match.group(1))
        if value not in seen:
            seen.add(value)
            volumes.append(value)
    return volumes


def _requested_price_kind(text: str) -> str:
    source = str(text or "")
    if NON_MODEL_RE.search(source):
        return "standard"
    if MODEL_RE.search(source):
        return "model"
    return "both"


def _requested_history_price_kind(text: str) -> str:
    source = str(text or "")
    has_standard = bool(NON_MODEL_RE.search(source))
    has_model = bool(MODEL_RE.search(source))
    if has_standard and not has_model:
        return "standard"
    if has_model and not has_standard:
        return "model"
    return "both"


def _body_service_name(text: str) -> str:
    source = str(text or "").casefold().replace("ё", "е")
    has_breast = "груд" in source
    has_buttocks = bool(re.search(r"ягод|поп|тесоро|tesoro", source))
    if has_breast and not has_buttocks:
        return "увеличению груди"
    if has_buttocks and not has_breast:
        return "увеличению ягодиц"
    return "увеличению груди/ягодиц"


def _volume_price_reply(service: str, volumes: list[int], price_kind: str, *, side_or_total_question: bool = False) -> str:
    lines = [f"Стоимость по {service}:"]
    for volume in volumes:
        if price_kind == "model":
            if volume in MODEL_BODY_PRICES:
                lines.append(f"{volume} мл как модель — {_money(MODEL_BODY_PRICES[volume])}.")
            else:
                lines.append(f"{volume} мл как модель в фиксированном прайсе не указан.")
            continue
        if price_kind == "standard":
            if volume in STANDARD_BODY_PRICES:
                lines.append(f"{volume} мл не как модель — {_money(STANDARD_BODY_PRICES[volume])}.")
            else:
                lines.append(f"{volume} мл не как модель в фиксированном прайсе не указан.")
            continue
        parts: list[str] = []
        if volume in MODEL_BODY_PRICES:
            parts.append(f"как модель — {_money(MODEL_BODY_PRICES[volume])}")
        if volume in STANDARD_BODY_PRICES:
            parts.append(f"не как модель — {_money(STANDARD_BODY_PRICES[volume])}")
        if parts:
            lines.append(f"{volume} мл: {', '.join(parts)}.")
        else:
            lines.append(f"{volume} мл в фиксированном прайсе не указан.")
    if side_or_total_question:
        lines.append("Стоимость считается по объёму препарата, не умножается отдельно на стороны или ягодицы.")
    return " ".join(lines)


def _full_price_reply(service: str, *, price_kind: str, side_or_total_question: bool = False) -> str:
    model = ", ".join(f"{volume} мл — {_money(price)}" for volume, price in MODEL_BODY_PRICES.items())
    standard = ", ".join(f"{volume} мл — {_money(price)}" for volume, price in STANDARD_BODY_PRICES.items())
    suffix = " Стоимость считается по объёму препарата, не умножается отдельно на стороны или ягодицы." if side_or_total_question else ""
    if price_kind == "model":
        return f"Стоимость по {service} как модель: {model}.{suffix}"
    if price_kind == "standard":
        return f"Стоимость по {service} не как модель: {standard}.{suffix}"
    return f"Стоимость по {service}: как модель: {model}; не как модель: {standard}.{suffix}"


def _money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₽"
