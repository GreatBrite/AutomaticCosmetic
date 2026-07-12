from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .expert_rag import APPROVED, ExpertAnswer, ExpertRagStore


DEFAULT_SERVICE_CATALOG_PATH = Path("data/service_catalog.json")
ACTIVE = "active"
HIDDEN = "hidden"
DEPRECATED = "deprecated"
DELETED = "deleted"
DEFAULT_CHANNEL_VISIBILITY = ("avito", "telegram_client", "vk")
DEFAULT_SERVICE_CATALOG_SEED: tuple[dict[str, Any], ...] = (
    {
        "service_key": "guby",
        "title": "Увеличение губ",
        "aliases": ("губы", "увеличение губ", "контурная пластика губ"),
        "products": ("Корея", "Juvederm", "Stylage", "Revolax"),
    },
    {
        "service_key": "grud",
        "title": "Коррекция/увеличение груди филлером",
        "aliases": ("грудь", "увеличение груди", "коррекция груди", "асимметрия груди"),
    },
    {
        "service_key": "yagodicy",
        "title": "Увеличение ягодиц",
        "aliases": ("ягодицы", "попа", "попе", "попу", "увеличение ягодиц"),
        "products": ("Tesoro Body", "Тесоро Body"),
    },
    {
        "service_key": "botoks",
        "title": "Ботокс / ботулинотерапия",
        "aliases": ("ботокс", "ботулинотерапия", "диспорт"),
    },
    {
        "service_key": "kozha_golovy",
        "title": "Процедуры для кожи головы",
        "aliases": ("кожа головы", "волосы", "выпадение волос", "ломкость волос"),
    },
    {
        "service_key": "online_consultation",
        "title": "Онлайн-консультация с Ольгой",
        "aliases": ("онлайн консультация", "онлайн-консультация", "онлайн разбор", "онлайн-разбор"),
    },
)


@dataclass(frozen=True)
class ServiceCatalogItem:
    service_key: str
    title: str
    aliases: tuple[str, ...] = ()
    status: str = ACTIVE
    cities: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    yclients_service_ids: dict[str, int] = field(default_factory=dict)
    visibility: tuple[str, ...] = ("avito", "telegram_client", "vk")
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ServiceCatalogStore:
    def __init__(self, path: Path | str = DEFAULT_SERVICE_CATALOG_PATH) -> None:
        self.path = Path(path)

    def list(self, *, include_deleted: bool = False) -> list[ServiceCatalogItem]:
        items = self._load()
        if not include_deleted:
            items = [item for item in items if item.status != DELETED]
        return items

    def get(self, service_key: str) -> ServiceCatalogItem | None:
        key = normalize_service_key(service_key)
        return next((item for item in self._load() if item.service_key == key), None)

    def resolve(self, text: str) -> ServiceCatalogItem | None:
        lowered = _normalize(text)
        if not lowered:
            return None
        best: ServiceCatalogItem | None = None
        best_score = 0
        for item in self._load():
            names = (item.service_key, item.title, *item.aliases, *item.products)
            score = max((_match_score(lowered, candidate) for candidate in names), default=0)
            if score > best_score:
                best = item
                best_score = score
        return best if best_score > 0 else None

    def upsert(
        self,
        *,
        service_key: str,
        title: str,
        aliases: tuple[str, ...] = (),
        status: str = ACTIVE,
        cities: tuple[str, ...] = (),
        products: tuple[str, ...] = (),
        yclients_service_ids: dict[str, int] | None = None,
        visibility: tuple[str, ...] = ("avito", "telegram_client", "vk"),
        metadata: dict[str, Any] | None = None,
    ) -> ServiceCatalogItem:
        now = _now()
        key = normalize_service_key(service_key or title)
        items = self._load()
        for index, item in enumerate(items):
            if item.service_key == key:
                updated = replace(
                    item,
                    title=title or item.title,
                    aliases=_dedupe((*item.aliases, *aliases)),
                    status=status or item.status,
                    cities=_dedupe(cities or item.cities),
                    products=_dedupe((*item.products, *products)),
                    yclients_service_ids=yclients_service_ids or item.yclients_service_ids,
                    visibility=_dedupe(visibility or item.visibility),
                    metadata={**item.metadata, **(metadata or {})},
                    updated_at=now,
                )
                items[index] = updated
                self._save(items)
                return updated
        created = ServiceCatalogItem(
            service_key=key,
            title=title or key,
            aliases=_dedupe(aliases),
            status=status,
            cities=_dedupe(cities),
            products=_dedupe(products),
            yclients_service_ids=yclients_service_ids or {},
            visibility=_dedupe(visibility),
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        items.append(created)
        self._save(items)
        return created

    def set_status(self, service_key: str, status: str, *, metadata: dict[str, Any] | None = None) -> ServiceCatalogItem:
        item = self.get(service_key)
        if not item:
            raise KeyError(f"service {service_key!r} not found")
        items = self._load()
        updated = replace(item, status=status, metadata={**item.metadata, **(metadata or {})}, updated_at=_now())
        self._save([updated if row.service_key == item.service_key else row for row in items])
        return updated

    def seed_defaults(self, *, overwrite: bool = False) -> list[ServiceCatalogItem]:
        seeded: list[ServiceCatalogItem] = []
        for row in DEFAULT_SERVICE_CATALOG_SEED:
            service_key = str(row["service_key"])
            if self.get(service_key) and not overwrite:
                continue
            seeded.append(
                self.upsert(
                    service_key=service_key,
                    title=str(row["title"]),
                    aliases=tuple(row.get("aliases") or ()),
                    products=tuple(row.get("products") or ()),
                    status=ACTIVE,
                    visibility=DEFAULT_CHANNEL_VISIBILITY,
                    metadata={"source": "default_seed"},
                )
            )
        return seeded

    def plan_expert_rag_service_key_migration(
        self,
        store: ExpertRagStore,
        *,
        status: str = APPROVED,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        for item in store.list_answers(status=status, limit=limit):
            current = service_catalog_from_rag_metadata(item.metadata or {})
            resolved = self.resolve(_answer_service_text(item))
            if not resolved or current == resolved.service_key:
                continue
            plan.append(
                {
                    "id": item.id,
                    "question": item.question_canonical,
                    "answer": item.answer_client,
                    "old_service_key": current,
                    "new_service_key": resolved.service_key,
                    "service_title": resolved.title,
                }
            )
        return plan

    def apply_expert_rag_service_key_migration(self, store: ExpertRagStore, plan: list[dict[str, Any]]) -> list[int]:
        updated_ids: list[int] = []
        for row in plan:
            item_id = int(row.get("id") or 0)
            service_key = str(row.get("new_service_key") or "").strip()
            if not item_id or not service_key:
                continue
            store.update_metadata(item_id, {"service_key": service_key, "service_catalog_migrated_at": _now()})
            updated_ids.append(item_id)
        return updated_ids

    def _load(self) -> list[ServiceCatalogItem]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        items: list[ServiceCatalogItem] = []
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            items.append(
                ServiceCatalogItem(
                    service_key=str(row.get("service_key") or ""),
                    title=str(row.get("title") or ""),
                    aliases=tuple(row.get("aliases") or ()),
                    status=str(row.get("status") or ACTIVE),
                    cities=tuple(row.get("cities") or ()),
                    products=tuple(row.get("products") or ()),
                    yclients_service_ids={str(k): int(v) for k, v in dict(row.get("yclients_service_ids") or {}).items()},
                    visibility=tuple(row.get("visibility") or ("avito", "telegram_client", "vk")),
                    metadata=dict(row.get("metadata") or {}),
                    created_at=str(row.get("created_at") or ""),
                    updated_at=str(row.get("updated_at") or ""),
                )
            )
        return items

    def _save(self, items: list[ServiceCatalogItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def normalize_service_key(text: str) -> str:
    value = _normalize(text)
    value = re.sub(r"[^a-zа-я0-9]+", "_", value).strip("_")
    return value or "service"


def service_catalog_from_rag_metadata(metadata: dict[str, Any]) -> str:
    return str(metadata.get("service_key") or "").strip()


def _answer_service_text(item: ExpertAnswer) -> str:
    metadata = item.metadata or {}
    return " ".join(
        str(part or "")
        for part in (
            metadata.get("service_key"),
            item.service,
            item.topic,
            item.question_canonical,
            item.answer_client,
        )
    )


def _match_score(haystack: str, candidate: str) -> int:
    needle = _normalize(candidate)
    if not needle:
        return 0
    if haystack == needle:
        return 4
    if needle in haystack:
        return 3
    tokens = set(re.findall(r"[a-zа-я0-9]+", needle))
    if tokens and tokens.intersection(re.findall(r"[a-zа-я0-9]+", haystack)):
        return 1
    return 0


def _normalize(text: str) -> str:
    lowered = str(text or "").casefold().replace("ё", "е")
    replacements = {"попа": "ягодицы", "попе": "ягодицы", "попу": "ягодицы", "сись": "грудь"}
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    return re.sub(r"\s+", " ", lowered).strip()


def _dedupe(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return tuple(result)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
