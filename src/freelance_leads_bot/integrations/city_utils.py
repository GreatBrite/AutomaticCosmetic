from __future__ import annotations

import re

from .config import DEFAULT_CITIES


CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "Москва": ("москва", "москве", "москву", "москвы", "мск"),
    "Ростов-на-Дону": ("ростов-на-дону", "ростов на дону", "ростове-на-дону", "ростов", "ростове", "ростова", "ростову"),
    "Санкт-Петербург": ("санкт-петербург", "санкт петербург", "петербург", "петербурге", "питер", "питере", "спб"),
    "Краснодар": ("краснодар", "краснодаре", "краснодара", "краснодару"),
    "Геленджик": ("геленджик", "геленджике", "гелик"),
}

KNOWN_EXTERNAL_CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "Абинск": ("абинск", "абинске"),
    "Анапа": ("анапа", "анапе", "анапу"),
    "Ейск": ("ейск", "ейске"),
    "Новороссийск": ("новороссийск", "новороссийске"),
    "Павловская": ("павловская", "павловской"),
    "Сочи": ("сочи",),
    "Туапсе": ("туапсе",),
    "Казань": ("казань", "казани"),
    "Воронеж": ("воронеж", "воронеже"),
    "Самара": ("самара", "самаре"),
    "Екатеринбург": ("екатеринбург", "екатеринбурге"),
}

NON_CITY_PLACE_WORDS = {
    "метро",
    "улица",
    "улице",
    "район",
    "районе",
    "центр",
    "центре",
    "салон",
    "салоне",
}


def cities_text(cities: tuple[str, ...] = DEFAULT_CITIES) -> str:
    return ", ".join(city for city in cities if str(city).strip()) or ", ".join(DEFAULT_CITIES)


def fixed_cities_reply(cities: tuple[str, ...] = DEFAULT_CITIES) -> str:
    return f"Сейчас прием ведем только в фиксированных городах: {cities_text(cities)}. Если один из них удобен, подскажу по записи."


def normalize_city(raw: str, *, cities: tuple[str, ...] = DEFAULT_CITIES) -> str:
    source = _normalize(raw)
    if not source:
        return ""
    for city in cities:
        if _normalize(city) == source:
            return city
    for city, aliases in CITY_ALIASES.items():
        if city in cities and source in {_normalize(alias) for alias in aliases}:
            return city
    return raw.strip()


def city_matches(schedule_city: str, requested_city: str, *, cities: tuple[str, ...] = DEFAULT_CITIES) -> bool:
    requested = normalize_city(requested_city, cities=cities)
    if not requested:
        return False
    return requested in normalize_cities(schedule_city, cities=cities)


def normalize_cities(raw: str, *, cities: tuple[str, ...] = DEFAULT_CITIES) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"\s*(?:,|;|/|\||\s+и\s+)\s*", raw or ""):
        normalized = normalize_city(part, cities=cities)
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return values


def explicit_supported_city(
    text: str,
    *,
    cities: tuple[str, ...] = DEFAULT_CITIES,
    extra_text: str = "",
) -> str:
    source = _normalize(" ".join(part for part in (text, extra_text) if part))
    if not source:
        return ""
    for city in cities:
        normalized = _normalize(city)
        if normalized and _contains_wordish(source, normalized):
            return city
        first = normalized.split("-")[0].split(" ")[0]
        if len(first) >= 5 and _contains_wordish(source, first):
            return city
    for city, aliases in CITY_ALIASES.items():
        if city not in cities:
            continue
        if any(_contains_wordish(source, _normalize(alias)) for alias in aliases):
            return city
    return ""


def explicit_external_city(text: str, *, cities: tuple[str, ...] = DEFAULT_CITIES) -> str:
    source = _normalize(text)
    if not source:
        return ""
    if explicit_supported_city(text, cities=cities):
        return ""
    for city, aliases in KNOWN_EXTERNAL_CITY_ALIASES.items():
        if any(_contains_wordish(source, _normalize(alias)) for alias in aliases):
            return city
    capitalized = re.search(r"(?u)\b(?:в|во|из|город(?:е)?|г\.|принимаете\s+в)\s+([А-ЯЁ][а-яё-]{3,})\b", text or "")
    if capitalized:
        candidate = capitalized.group(1).strip()
        normalized = _normalize(candidate)
        if normalized not in NON_CITY_PLACE_WORDS and not explicit_supported_city(candidate, cities=cities):
            return candidate
    return ""


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold()).replace("ё", "е")


def _contains_wordish(source: str, needle: str) -> bool:
    if not needle:
        return False
    if " " in needle or "-" in needle:
        return needle in source
    return bool(re.search(rf"(?<![а-яa-z]){re.escape(needle)}(?![а-яa-z])", source))
