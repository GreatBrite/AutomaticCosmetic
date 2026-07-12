from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_EXPERT_RAG_DB_PATH = Path("data/expert_rag.sqlite3")
APPROVED = "approved"
DRAFT = "draft"
NEEDS_REVIEW = "needs_review"
DEPRECATED = "deprecated"

RISK_TERMS = (
    "беремен",
    "кормлен",
    "аллерг",
    "противопоказ",
    "осложн",
    "отек",
    "отёк",
    "боль",
    "температур",
    "воспален",
    "гной",
    "инфекц",
    "безопас",
    "операц",
    "врач",
    "диагноз",
    "хронич",
    "онколог",
    "эпилеп",
    "диабет",
    "давлен",
    "антибиот",
    "лекарств",
    "принима",
)
PRICE_TERMS = ("цен", "стоим", "руб", "₽", "прайс", "сколько")


@dataclass(frozen=True)
class ExpertAnswer:
    id: int
    question_canonical: str
    answer_client: str
    answer_internal: str = ""
    topic: str = ""
    service: str = ""
    city: str = ""
    risk_level: str = "low"
    source_chat_id: str = ""
    source_message_id: str = ""
    olga_reply_message_id: str = ""
    status: str = APPROVED
    approved_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    metadata: dict[str, Any] | None = None

    def to_dict(self, *, score: float | None = None) -> dict[str, Any]:
        data = {
            "id": self.id,
            "question_canonical": self.question_canonical,
            "answer_client": self.answer_client,
            "answer_internal": self.answer_internal,
            "topic": self.topic,
            "service": self.service,
            "city": self.city,
            "risk_level": self.risk_level,
            "source_chat_id": self.source_chat_id,
            "source_message_id": self.source_message_id,
            "olga_reply_message_id": self.olga_reply_message_id,
            "status": self.status,
            "approved_by": self.approved_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "metadata": self.metadata or {},
        }
        if score is not None:
            data["score"] = round(score, 4)
        return data


class ExpertRagStore:
    """Small local expert-answer memory.

    v1 deliberately keeps the backend simple: SQLite persistence plus lexical
    retrieval. The class boundary lets us swap retrieval for embeddings/Qdrant
    later without changing Avito orchestration.
    """

    def __init__(self, path: Path | str = DEFAULT_EXPERT_RAG_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def upsert_from_handoff(
        self,
        *,
        question: str,
        answer_client: str,
        answer_internal: str = "",
        source_chat_id: str = "",
        source_message_id: str = "",
        olga_reply_message_id: str = "",
        approved_by: str = "",
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ExpertAnswer:
        canonical = canonicalize_question(question)
        answer = _clean_answer(answer_client)
        inferred = infer_metadata(" ".join([canonical, answer, answer_internal]))
        final_status = status or _default_status(answer)
        now = _now()
        fingerprint = _fingerprint(canonical, answer)
        metadata = {**(metadata or {}), "fingerprint": fingerprint, "tokens": sorted(_tokens(canonical))}
        with self._connect() as conn:
            existing = None
            for candidate in conn.execute("SELECT id, metadata FROM expert_answers").fetchall():
                try:
                    candidate_metadata = json.loads(str(candidate["metadata"] or "{}"))
                except json.JSONDecodeError:
                    candidate_metadata = {}
                if isinstance(candidate_metadata, dict) and candidate_metadata.get("fingerprint") == fingerprint:
                    existing = candidate
                    break
            if existing:
                conn.execute(
                    """
                    UPDATE expert_answers
                    SET question_canonical = ?, answer_client = ?, answer_internal = ?, topic = ?,
                        service = ?, city = ?, risk_level = ?, source_chat_id = ?,
                        source_message_id = ?, olga_reply_message_id = ?, status = ?,
                        approved_by = ?, updated_at = ?, metadata = ?
                    WHERE id = ?
                    """,
                    (
                        canonical,
                        answer,
                        answer_internal,
                        inferred["topic"],
                        inferred["service"],
                        inferred["city"],
                        inferred["risk_level"],
                        source_chat_id,
                        source_message_id,
                        olga_reply_message_id,
                        final_status,
                        approved_by,
                        now,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        int(existing["id"]),
                    ),
                )
                row_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO expert_answers (
                        question_canonical, answer_client, answer_internal, topic, service, city,
                        risk_level, source_chat_id, source_message_id, olga_reply_message_id,
                        status, approved_by, created_at, updated_at, expires_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical,
                        answer,
                        answer_internal,
                        inferred["topic"],
                        inferred["service"],
                        inferred["city"],
                        inferred["risk_level"],
                        source_chat_id,
                        source_message_id,
                        olga_reply_message_id,
                        final_status,
                        approved_by,
                        now,
                        now,
                        "",
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                row_id = int(cursor.lastrowid)
        found = self.get(row_id)
        if not found:
            raise RuntimeError("expert answer was not persisted")
        return found

    def approve(self, item_id: int, *, approved_by: str = "olga") -> ExpertAnswer:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE expert_answers SET status = ?, approved_by = ?, updated_at = ? WHERE id = ?",
                (APPROVED, approved_by, now, item_id),
            )
        found = self.get(item_id)
        if not found:
            raise KeyError(f"expert answer {item_id} not found")
        return found

    def deprecate(self, item_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE expert_answers SET status = ?, updated_at = ? WHERE id = ?",
                (DEPRECATED, _now(), item_id),
            )
        return cursor.rowcount > 0

    def update_metadata(self, item_id: int, metadata: dict[str, Any]) -> ExpertAnswer:
        existing = self.get(item_id)
        if not existing:
            raise KeyError(f"expert answer {item_id} not found")
        merged = {**(existing.metadata or {}), **metadata}
        with self._connect() as conn:
            conn.execute(
                "UPDATE expert_answers SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False, sort_keys=True), _now(), item_id),
            )
        updated = self.get(item_id)
        if not updated:
            raise KeyError(f"expert answer {item_id} not found after metadata update")
        return updated

    def get(self, item_id: int) -> ExpertAnswer | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM expert_answers WHERE id = ?", (item_id,)).fetchone()
        return _answer_from_row(row) if row else None

    def list_answers(self, *, status: str = "", limit: int = 20) -> list[ExpertAnswer]:
        sql = "SELECT * FROM expert_answers"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit or 1)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [answer for row in rows if (answer := _answer_from_row(row))]

    def search(
        self,
        query: str,
        *,
        status: str = APPROVED,
        limit: int = 5,
        min_score: float = 0.0,
        city: str = "",
        service: str = "",
        exclude_risk_levels: tuple[str, ...] = (),
    ) -> list[tuple[ExpertAnswer, float]]:
        canonical = canonicalize_question(query)
        query_tokens = _tokens(canonical)
        if not query_tokens:
            return []
        query_metadata = infer_metadata(canonical)
        sql = "SELECT * FROM expert_answers WHERE status = ?"
        params: list[Any] = [status]
        if city:
            sql += " AND (city = '' OR lower(city) = lower(?))"
            params.append(city)
        if service:
            sql += " AND (service = '' OR lower(service) = lower(?))"
            params.append(service)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        scored: list[tuple[ExpertAnswer, float]] = []
        excluded_risks = {risk.strip().casefold() for risk in exclude_risk_levels if risk.strip()}
        for row in rows:
            answer = _answer_from_row(row)
            if not answer:
                continue
            if excluded_risks and answer.risk_level.strip().casefold() in excluded_risks:
                continue
            if answer.expires_at and answer.expires_at < _now():
                continue
            score = max(
                _score(query_tokens, _tokens(answer.question_canonical)),
                _score(query_tokens, _tokens(answer.question_canonical + " " + answer.answer_client)),
            )
            if query_metadata["service"] and query_metadata["service"] == answer.service:
                score += 0.08
            if query_metadata["topic"] != "general" and query_metadata["topic"] == answer.topic:
                score += 0.05
            if query_metadata["city"] and query_metadata["city"] == answer.city:
                score += 0.05
            score = min(score, 1.0)
            if score >= min_score:
                scored.append((answer, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS expert_answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_canonical TEXT NOT NULL,
                    answer_client TEXT NOT NULL,
                    answer_internal TEXT DEFAULT '',
                    topic TEXT DEFAULT '',
                    service TEXT DEFAULT '',
                    city TEXT DEFAULT '',
                    risk_level TEXT DEFAULT 'low',
                    source_chat_id TEXT DEFAULT '',
                    source_message_id TEXT DEFAULT '',
                    olga_reply_message_id TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    approved_by TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_expert_answers_status ON expert_answers(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_expert_answers_topic ON expert_answers(topic)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_expert_answers_city ON expert_answers(city)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def canonicalize_question(text: str) -> str:
    text = str(text or "").casefold().replace("ё", "е")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\+?\d[\d\s()\-]{6,}\d", " [phone] ", text)
    text = re.sub(r"[^a-zа-я0-9₽]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def infer_metadata(text: str) -> dict[str, str]:
    lowered = canonicalize_question(text)
    service = ""
    if "губ" in lowered:
        service = "губы"
    elif "ягод" in lowered or "поп" in lowered:
        service = "ягодицы"
    elif "груд" in lowered:
        service = "грудь"
    elif "ботокс" in lowered or "ботулин" in lowered:
        service = "ботокс"
    elif "волос" in lowered or "голов" in lowered:
        service = "кожа головы"
    city = ""
    for candidate in ("москва", "краснодар", "ростов", "санкт петербург", "спб", "геленджик"):
        if candidate in lowered:
            city = "Санкт-Петербург" if candidate == "спб" else candidate.title()
            break
    risk_level = "high" if any(term in lowered for term in RISK_TERMS) else "low"
    topic = "price" if any(term in lowered for term in PRICE_TERMS) else service or "general"
    return {"topic": topic, "service": service, "city": city, "risk_level": risk_level}


def _answer_from_row(row: sqlite3.Row | None) -> ExpertAnswer | None:
    if row is None:
        return None
    try:
        metadata = json.loads(str(row["metadata"] or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    return ExpertAnswer(
        id=int(row["id"]),
        question_canonical=str(row["question_canonical"] or ""),
        answer_client=str(row["answer_client"] or ""),
        answer_internal=str(row["answer_internal"] or ""),
        topic=str(row["topic"] or ""),
        service=str(row["service"] or ""),
        city=str(row["city"] or ""),
        risk_level=str(row["risk_level"] or "low"),
        source_chat_id=str(row["source_chat_id"] or ""),
        source_message_id=str(row["source_message_id"] or ""),
        olga_reply_message_id=str(row["olga_reply_message_id"] or ""),
        status=str(row["status"] or DRAFT),
        approved_by=str(row["approved_by"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        expires_at=str(row["expires_at"] or ""),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-zа-я0-9₽]{3,}", canonicalize_question(text)))
    return {token for token in tokens if token not in {"здравствуйте", "скажите", "пожалуйста", "можно", "хочу", "будет"}}


def _score(query_tokens: set[str], doc_tokens: set[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    overlap = len(query_tokens & doc_tokens)
    recall = overlap / max(1, len(query_tokens))
    precision = overlap / max(1, len(doc_tokens))
    return (0.75 * recall) + (0.25 * precision)


def _default_status(answer: str) -> str:
    lowered = canonicalize_question(answer)
    if any(term in lowered for term in RISK_TERMS):
        return NEEDS_REVIEW
    if any(term in lowered for term in PRICE_TERMS) and re.search(r"\d", lowered):
        return NEEDS_REVIEW
    return APPROVED


def _clean_answer(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:4000]


def _fingerprint(question: str, answer: str) -> str:
    import hashlib

    return hashlib.sha256(f"{question}\n{answer}".encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
