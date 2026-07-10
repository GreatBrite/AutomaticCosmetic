from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_DEDUP_TTL_SECONDS = 7 * 24 * 60 * 60


@dataclass
class PersistentProcessedEventStore:
    path: Path = Path("data/avito_processed_events.json")
    ttl_seconds: int = DEFAULT_DEDUP_TTL_SECONDS
    seen: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.seen.update(self._load())

    def mark_once(self, key: str) -> bool:
        if not key:
            return True
        self._prune(self.seen)
        if key in self.seen:
            self._save(self.seen)
            return False
        self.seen[key] = time.time()
        self._save(self.seen)
        return True

    def contains(self, key: str) -> bool:
        if not key:
            return False
        self._prune(self.seen)
        exists = key in self.seen
        if exists:
            self._save(self.seen)
        return exists

    def _load(self) -> dict[str, float]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, float] = {}
        for key, value in payload.items():
            try:
                result[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    def _save(self, seen: dict[str, float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(seen, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)

    def _prune(self, seen: dict[str, float]) -> None:
        now = time.time()
        for key, created_at in list(seen.items()):
            if now - created_at > self.ttl_seconds:
                seen.pop(key, None)
