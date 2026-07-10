from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..config import ROOT


CITY_ALIASES = {
    "москва": "Москва",
    "москве": "Москва",
    "москву": "Москва",
    "мск": "Москва",
    "ростов": "Ростов-на-Дону",
    "ростов-на-дону": "Ростов-на-Дону",
    "ростов на дону": "Ростов-на-Дону",
    "ростове": "Ростов-на-Дону",
    "ростове-на-дону": "Ростов-на-Дону",
    "санкт-петербург": "Санкт-Петербург",
    "санкт петербург": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "питер": "Санкт-Петербург",
    "краснодар": "Краснодар",
    "краснодаре": "Краснодар",
    "геленджик": "Геленджик",
    "геленджике": "Геленджик",
    "гелик": "Геленджик",
}


class CityScheduleStore:
    """Local date -> city schedule for one cosmetologist working across cities."""

    def __init__(self, path: Path | str = ROOT / "data" / "city_schedule.json") -> None:
        self.path = Path(path)

    def normalize_city(self, raw: str) -> str:
        key = re.sub(r"\s+", " ", (raw or "").strip().casefold()).replace("ё", "е")
        return CITY_ALIASES.get(key) or CITY_ALIASES.get(key.replace(" ", "-")) or raw.strip()

    def normalize_cities(self, raw: str) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for part in re.split(r"\s*(?:,|;|/|\||\s+и\s+)\s*", raw or ""):
            normalized = self.normalize_city(part)
            if normalized and normalized not in seen:
                seen.add(normalized)
                values.append(normalized)
        return values

    def city_matches(self, schedule_city: str, requested_city: str) -> bool:
        requested = self.normalize_city(requested_city)
        if not requested:
            return False
        return requested in self.normalize_cities(schedule_city)

    def parse_dates(self, text: str, *, base: date | None = None) -> list[str]:
        base = base or date.today()
        values: list[str] = []
        seen: set[str] = set()

        for year, month, day in re.findall(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text or ""):
            value = date(int(year), int(month), int(day)).isoformat()
            if value not in seen:
                seen.add(value)
                values.append(value)

        for day, month, year_raw in re.findall(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", text or ""):
            year = int(year_raw or base.year)
            if year < 100:
                year += 2000
            value = date(year, int(month), int(day)).isoformat()
            if value not in seen:
                seen.add(value)
                values.append(value)

        without_full_dates = re.sub(r"\b20\d{2}-\d{1,2}-\d{1,2}\b", " ", text or "")
        without_full_dates = re.sub(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b", " ", without_full_dates)
        for token in re.findall(r"\b([1-9]|[12]\d|3[01])\b", without_full_dates):
            value = date(base.year, base.month, int(token)).isoformat()
            if value not in seen:
                seen.add(value)
                values.append(value)

        return values

    def get_city(self, schedule_date: str) -> str:
        rows = self._load()
        row = rows.get(schedule_date[:10]) or {}
        return str(row.get("city") or "")

    def set_dates(self, city: str, dates: list[str]) -> dict[str, Any]:
        normalized_city = ", ".join(self.normalize_cities(city)) or self.normalize_city(city)
        rows = self._load()
        now = datetime.now(timezone.utc).isoformat()
        for item in dates:
            rows[item[:10]] = {"city": normalized_city, "updated_at": now}
        self._save(rows)
        return {"city": normalized_city, "dates": [item[:10] for item in dates], "updated_count": len(dates)}

    def delete_dates(self, dates: list[str]) -> dict[str, Any]:
        rows = self._load()
        count = 0
        for item in dates:
            if rows.pop(item[:10], None) is not None:
                count += 1
        self._save(rows)
        return {"dates": [item[:10] for item in dates], "deleted_count": count}

    def list(self, *, from_date: str = "", city: str = "") -> list[dict[str, str]]:
        start = date.fromisoformat(from_date[:10]) if from_date else date.today()
        normalized_city = self.normalize_city(city) if city else ""
        rows = []
        for raw_date, row in sorted(self._load().items()):
            row_city = str(row.get("city") or "")
            if raw_date < start.isoformat():
                continue
            if normalized_city and not self.city_matches(row_city, normalized_city):
                continue
            rows.append({"date": raw_date, "city": row_city})
        return rows

    def format(self, *, from_date: str = "", city: str = "") -> str:
        rows = self.list(from_date=from_date, city=city)
        if not rows:
            return "График пока пуст."
        by_city: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            parsed = date.fromisoformat(row["date"])
            by_city[row["city"]].append(parsed.strftime("%d.%m"))
        return "\n".join(f"{city}: {', '.join(days)}" for city, days in by_city.items())

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return {str(key): dict(value) for key, value in dict(payload).items() if isinstance(value, dict)}

    def _save(self, rows: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
