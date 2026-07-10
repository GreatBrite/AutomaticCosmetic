from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from ..config import ROOT
from .models import Appointment, ClientProfile
from .yclients import YClientsGateway


CARE_CRM_SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    yclients_client_id TEXT UNIQUE,
    phone TEXT DEFAULT '',
    name TEXT DEFAULT '',
    city TEXT DEFAULT '',
    source TEXT DEFAULT '',
    skin_type TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    consent_status TEXT DEFAULT 'unknown',
    do_not_contact INTEGER NOT NULL DEFAULT 0,
    complaint_risk INTEGER NOT NULL DEFAULT 0,
    last_visit_at TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    yclients_record_id TEXT UNIQUE,
    client_id INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    city TEXT DEFAULT '',
    booked_service_id INTEGER NOT NULL DEFAULT 0,
    booked_service_title TEXT DEFAULT '',
    booked_price INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'scheduled',
    confirmation_status TEXT NOT NULL DEFAULT 'pending',
    confirmation_chat_id TEXT DEFAULT '',
    confirmation_card_message_id TEXT DEFAULT '',
    confirmed_by TEXT DEFAULT '',
    confirmed_at TEXT DEFAULT '',
    source TEXT DEFAULT 'yclients',
    raw_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(client_id) REFERENCES crm_clients(id)
);

CREATE TABLE IF NOT EXISTS crm_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL UNIQUE,
    client_id INTEGER NOT NULL,
    actually_attended INTEGER NOT NULL,
    actual_service_id INTEGER NOT NULL DEFAULT 0,
    actual_service_title TEXT DEFAULT '',
    amount_ml TEXT DEFAULT '',
    units TEXT DEFAULT '',
    product_or_drug TEXT DEFAULT '',
    procedure_notes TEXT DEFAULT '',
    reaction TEXT DEFAULT '',
    aftercare_notes TEXT DEFAULT '',
    confirmed_by TEXT DEFAULT '',
    confirmed_at TEXT DEFAULT '',
    source_text TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(appointment_id) REFERENCES crm_appointments(id),
    FOREIGN KEY(client_id) REFERENCES crm_clients(id)
);

CREATE TABLE IF NOT EXISTS crm_followup_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    visit_id INTEGER,
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    channel TEXT DEFAULT '',
    message_draft TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    risk_level TEXT NOT NULL DEFAULT 'low',
    approved_by TEXT DEFAULT '',
    approved_at TEXT DEFAULT '',
    draft_source TEXT DEFAULT 'rule',
    outcome TEXT DEFAULT '',
    admin_chat_id TEXT DEFAULT '',
    admin_card_message_id TEXT DEFAULT '',
    sent_at TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(client_id) REFERENCES crm_clients(id),
    FOREIGN KEY(visit_id) REFERENCES crm_visits(id)
);

CREATE TABLE IF NOT EXISTS crm_client_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    external_user_id TEXT DEFAULT '',
    chat_id TEXT DEFAULT '',
    username TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(client_id) REFERENCES crm_clients(id)
);

CREATE TABLE IF NOT EXISTS crm_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    appointment_id INTEGER,
    visit_id INTEGER,
    channel TEXT NOT NULL DEFAULT 'telegram',
    direction TEXT NOT NULL DEFAULT 'internal_note',
    author TEXT DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    intent TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(client_id) REFERENCES crm_clients(id),
    FOREIGN KEY(appointment_id) REFERENCES crm_appointments(id),
    FOREIGN KEY(visit_id) REFERENCES crm_visits(id)
);

CREATE TABLE IF NOT EXISTS crm_learning_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson TEXT NOT NULL DEFAULT '',
    source TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    created_from_interaction_id INTEGER,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(created_from_interaction_id) REFERENCES crm_interactions(id)
);

CREATE TABLE IF NOT EXISTS crm_client_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    preference_type TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    source TEXT DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(client_id) REFERENCES crm_clients(id)
);

CREATE TABLE IF NOT EXISTS crm_agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_role TEXT NOT NULL DEFAULT '',
    input_ref TEXT DEFAULT '',
    decision_json TEXT DEFAULT '{}',
    tool_calls_json TEXT DEFAULT '[]',
    outcome TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_crm_clients_phone ON crm_clients(phone);
CREATE INDEX IF NOT EXISTS idx_crm_appointments_scheduled_at ON crm_appointments(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_crm_appointments_confirmation ON crm_appointments(confirmation_status, scheduled_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_crm_followup_visit_kind ON crm_followup_tasks(visit_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS idx_crm_client_links_external ON crm_client_links(channel, external_user_id);
CREATE INDEX IF NOT EXISTS idx_crm_client_links_chat ON crm_client_links(channel, chat_id);
CREATE INDEX IF NOT EXISTS idx_crm_interactions_client ON crm_interactions(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_crm_learning_tags ON crm_learning_lessons(tags);
CREATE UNIQUE INDEX IF NOT EXISTS idx_crm_client_preferences_key ON crm_client_preferences(client_id, preference_type);
CREATE INDEX IF NOT EXISTS idx_crm_agent_decisions_role ON crm_agent_decisions(agent_role, created_at);
"""


@dataclass(frozen=True)
class VisitConfirmationAction:
    appointment_id: int
    action: str


@dataclass(frozen=True)
class FollowupRule:
    kind: str
    delay_days: int
    message_draft: str
    channel: str = "telegram_client"


DEFAULT_FOLLOWUP_RULES: tuple[FollowupRule, ...] = (
    FollowupRule(
        kind="care_checkin_1d",
        delay_days=1,
        message_draft="Здравствуйте! Как вы себя чувствуете после процедуры? Всё ли комфортно?",
    ),
    FollowupRule(
        kind="result_checkin_14d",
        delay_days=14,
        message_draft="Здравствуйте! Как сейчас результат после процедуры? Если хотите, можно посмотреть, всё ли идёт комфортно.",
    ),
    FollowupRule(
        kind="care_relationship_45d",
        delay_days=45,
        message_draft="Здравствуйте! Хотела аккуратно напомнить о себе: если понадобится уход, коррекция или консультация, можно написать сюда.",
    ),
)


class CareCrmStore:
    """Local factual CRM for post-visit care and upsell logic."""

    def __init__(self, path: Path | str = ROOT / "data" / "care_crm.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(CARE_CRM_SCHEMA)
            _ensure_schema_migrations(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert_appointment(self, appointment: Appointment) -> dict[str, Any]:
        client_id = self.upsert_client(appointment.client, city=appointment.city)
        record_id = str(appointment.id or appointment.raw.get("record_id") or "").strip()
        scheduled_at = appointment.starts_at.isoformat() if appointment.starts_at else ""
        if not scheduled_at:
            raise ValueError("appointment.starts_at is required")
        payload = {
            "yclients_record_id": record_id,
            "client_id": client_id,
            "scheduled_at": scheduled_at,
            "city": appointment.city,
            "booked_service_id": int(appointment.service.id or 0),
            "booked_service_title": appointment.service.title,
            "booked_price": int(appointment.service.price or 0),
            "raw_json": json.dumps(appointment.raw, ensure_ascii=False, default=str),
        }
        with self.connect() as conn:
            if record_id:
                conn.execute(
                    """
                    INSERT INTO crm_appointments (
                        yclients_record_id, client_id, scheduled_at, city, booked_service_id,
                        booked_service_title, booked_price, raw_json
                    )
                    VALUES (:yclients_record_id, :client_id, :scheduled_at, :city, :booked_service_id,
                        :booked_service_title, :booked_price, :raw_json)
                    ON CONFLICT(yclients_record_id) DO UPDATE SET
                        client_id = excluded.client_id,
                        scheduled_at = excluded.scheduled_at,
                        city = excluded.city,
                        booked_service_id = excluded.booked_service_id,
                        booked_service_title = excluded.booked_service_title,
                        booked_price = excluded.booked_price,
                        raw_json = excluded.raw_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    payload,
                )
                row = conn.execute("SELECT * FROM crm_appointments WHERE yclients_record_id = ?", (record_id,)).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT *
                    FROM crm_appointments
                    WHERE client_id = ? AND scheduled_at = ? AND booked_service_title = ?
                    """,
                    (client_id, scheduled_at, appointment.service.title),
                ).fetchone()
                if row is None:
                    cur = conn.execute(
                        """
                        INSERT INTO crm_appointments (
                            client_id, scheduled_at, city, booked_service_id, booked_service_title, booked_price, raw_json
                        )
                        VALUES (:client_id, :scheduled_at, :city, :booked_service_id, :booked_service_title, :booked_price, :raw_json)
                        """,
                        payload,
                    )
                    row = conn.execute("SELECT * FROM crm_appointments WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def upsert_client(self, client: ClientProfile, *, city: str = "") -> int:
        external_id = str(client.external_id or "").strip()
        phone = _normalize_phone(client.phone)
        with self.connect() as conn:
            row = None
            if external_id:
                row = conn.execute("SELECT * FROM crm_clients WHERE yclients_client_id = ?", (external_id,)).fetchone()
            if row is None and phone:
                row = conn.execute("SELECT * FROM crm_clients WHERE phone = ? ORDER BY id LIMIT 1", (phone,)).fetchone()
            if row is None:
                cur = conn.execute(
                    """
                    INSERT INTO crm_clients (yclients_client_id, phone, name, city, skin_type, notes, source)
                    VALUES (?, ?, ?, ?, ?, ?, 'yclients')
                    """,
                    (external_id or None, phone, client.name, city or client.city, client.skin_type, client.notes),
                )
                return int(cur.lastrowid)
            client_id = int(row["id"])
            conn.execute(
                """
                UPDATE crm_clients
                SET yclients_client_id = COALESCE(NULLIF(?, ''), yclients_client_id),
                    phone = COALESCE(NULLIF(?, ''), phone),
                    name = COALESCE(NULLIF(?, ''), name),
                    city = COALESCE(NULLIF(?, ''), city),
                    skin_type = COALESCE(NULLIF(?, ''), skin_type),
                    notes = COALESCE(NULLIF(?, ''), notes),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (external_id, phone, client.name, city or client.city, client.skin_type, client.notes, client_id),
            )
            return client_id

    def search_clients(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        needle = str(query or "").strip()
        if not needle:
            return []
        phone = _normalize_phone(needle)
        like = f"%{needle.casefold()}%"
        raw_like = f"%{needle}%"
        phone_like = f"%{phone}%" if phone else like
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM crm_clients
                WHERE lower(name) LIKE ?
                   OR name LIKE ?
                   OR phone LIKE ?
                   OR yclients_client_id = ?
                ORDER BY last_visit_at DESC, updated_at DESC, id DESC
                LIMIT ?
                """,
                (like, raw_like, phone_like, needle, max(1, min(int(limit or 10), 25))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_client(self, client_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM crm_clients WHERE id = ?", (client_id,)).fetchone()
        return dict(row) if row else None

    def list_client_appointments(self, client_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    a.*,
                    c.name AS client_name,
                    c.phone AS client_phone,
                    c.do_not_contact AS client_do_not_contact,
                    c.complaint_risk AS client_complaint_risk
                FROM crm_appointments a
                JOIN crm_clients c ON c.id = a.client_id
                WHERE a.client_id = ?
                ORDER BY a.scheduled_at DESC, a.id DESC
                LIMIT ?
                """,
                (client_id, max(1, min(int(limit or 10), 25))),
            ).fetchall()
        return [dict(row) for row in rows]

    def match_appointments(
        self,
        *,
        query: str = "",
        day: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if day:
            clauses.append("substr(a.scheduled_at, 1, 10) = ?")
            params.append(str(day)[:10])
        needle = str(query or "").strip()
        phone = _normalize_phone(needle)
        if needle:
            clauses.append(
                "(lower(c.name) LIKE ? OR c.name LIKE ? OR c.phone LIKE ? OR a.booked_service_title LIKE ? OR a.yclients_record_id = ?)"
            )
            params.extend((f"%{needle.casefold()}%", f"%{needle}%", f"%{phone or needle}%", f"%{needle}%", needle))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit or 10), 25)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    a.*,
                    c.name AS client_name,
                    c.phone AS client_phone,
                    c.do_not_contact AS client_do_not_contact,
                    c.complaint_risk AS client_complaint_risk
                FROM crm_appointments a
                JOIN crm_clients c ON c.id = a.client_id
                {where}
                ORDER BY a.scheduled_at DESC, a.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def find_client_by_link(
        self,
        *,
        channel: str,
        external_user_id: str = "",
        chat_id: str = "",
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, l.channel, l.external_user_id, l.chat_id, l.username, l.display_name, l.verified
                FROM crm_client_links l
                JOIN crm_clients c ON c.id = l.client_id
                WHERE l.channel = ?
                  AND (
                    (l.external_user_id != '' AND l.external_user_id = ?)
                    OR (l.chat_id != '' AND l.chat_id = ?)
                  )
                ORDER BY l.verified DESC, l.updated_at DESC, l.id DESC
                LIMIT 1
                """,
                (channel, str(external_user_id), str(chat_id)),
            ).fetchone()
        return dict(row) if row else None

    def ensure_telegram_client(
        self,
        *,
        telegram_user_id: str,
        chat_id: str,
        username: str = "",
        display_name: str = "",
    ) -> int:
        linked = self.find_client_by_link(channel="telegram_client", external_user_id=telegram_user_id, chat_id=chat_id)
        if linked:
            return int(linked["id"])
        client_id = self.upsert_client(
            ClientProfile(name=display_name or username or "Telegram клиент", external_id="", notes="", city=""),
            city="",
        )
        self.link_client_channel(
            client_id,
            channel="telegram_client",
            external_user_id=telegram_user_id,
            chat_id=chat_id,
            username=username,
            display_name=display_name,
            verified=False,
        )
        return client_id

    def link_client_channel(
        self,
        client_id: int,
        *,
        channel: str,
        external_user_id: str = "",
        chat_id: str = "",
        username: str = "",
        display_name: str = "",
        verified: bool = False,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crm_client_links (
                    client_id, channel, external_user_id, chat_id, username, display_name, verified
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, external_user_id) DO UPDATE SET
                    client_id = excluded.client_id,
                    chat_id = excluded.chat_id,
                    username = excluded.username,
                    display_name = excluded.display_name,
                    verified = excluded.verified,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (client_id, channel, external_user_id, chat_id, username, display_name, int(verified)),
            )

    def list_client_links(self, client_id: int, *, channel: str = "") -> list[dict[str, Any]]:
        clauses = ["client_id = ?"]
        params: list[Any] = [client_id]
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM crm_client_links
                WHERE {' AND '.join(clauses)}
                ORDER BY verified DESC, updated_at DESC, id DESC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_client_flags(
        self,
        client_id: int,
        *,
        consent_status: str | None = None,
        do_not_contact: bool | None = None,
        complaint_risk: bool | None = None,
    ) -> dict[str, Any]:
        current = self.get_client(client_id)
        if not current:
            raise KeyError(f"client {client_id} not found")
        consent = current.get("consent_status") if consent_status is None else consent_status
        do_not = current.get("do_not_contact") if do_not_contact is None else int(do_not_contact)
        risk = current.get("complaint_risk") if complaint_risk is None else int(complaint_risk)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crm_clients
                SET consent_status = ?,
                    do_not_contact = ?,
                    complaint_risk = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (consent, int(do_not or 0), int(risk or 0), client_id),
            )
        updated = self.get_client(client_id)
        return updated or current

    def list_client_visits(self, client_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    v.*,
                    a.scheduled_at,
                    a.city,
                    a.booked_service_title,
                    a.booked_price,
                    a.yclients_record_id
                FROM crm_visits v
                JOIN crm_appointments a ON a.id = v.appointment_id
                WHERE v.client_id = ?
                ORDER BY a.scheduled_at DESC, v.id DESC
                LIMIT ?
                """,
                (client_id, max(1, min(int(limit or 10), 25))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_client_interactions(self, client_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM crm_interactions
                WHERE client_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (client_id, max(1, min(int(limit or 10), 25))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_followup_tasks(
        self,
        *,
        status: str = "planned",
        due_before: str = "",
        client_id: int | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("t.status = ?")
            params.append(status)
        if due_before:
            clauses.append("t.due_at <= ?")
            params.append(due_before)
        if client_id:
            clauses.append("t.client_id = ?")
            params.append(client_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit or 25), 100)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    t.*,
                    c.name AS client_name,
                    c.phone AS client_phone,
                    c.do_not_contact,
                    c.complaint_risk,
                    v.actual_service_title,
                    a.scheduled_at,
                    a.city
                FROM crm_followup_tasks t
                JOIN crm_clients c ON c.id = t.client_id
                LEFT JOIN crm_visits v ON v.id = t.visit_id
                LEFT JOIN crm_appointments a ON a.id = v.appointment_id
                {where}
                ORDER BY t.due_at, t.id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_followup_task_status(self, task_id: int, *, status: str, sent_at: str = "") -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crm_followup_tasks
                SET status = ?,
                    sent_at = COALESCE(NULLIF(?, ''), sent_at),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, sent_at, task_id),
            )
            row = conn.execute("SELECT * FROM crm_followup_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def update_followup_task(
        self,
        task_id: int,
        *,
        message_draft: str | None = None,
        reason: str | None = None,
        confidence: float | None = None,
        risk_level: str | None = None,
        approved_by: str | None = None,
        draft_source: str | None = None,
        outcome: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_followup_task(task_id)
        if not current:
            return None
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crm_followup_tasks
                SET message_draft = ?,
                    reason = ?,
                    confidence = ?,
                    risk_level = ?,
                    approved_by = ?,
                    approved_at = CASE WHEN ? != '' THEN ? ELSE approved_at END,
                    draft_source = ?,
                    outcome = ?,
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    current.get("message_draft") if message_draft is None else message_draft,
                    current.get("reason") if reason is None else reason,
                    float(current.get("confidence") or 0.5) if confidence is None else float(confidence),
                    current.get("risk_level") if risk_level is None else risk_level,
                    current.get("approved_by") if approved_by is None else approved_by,
                    approved_by or "",
                    datetime.now(timezone.utc).isoformat(),
                    current.get("draft_source") if draft_source is None else draft_source,
                    current.get("outcome") if outcome is None else outcome,
                    current.get("status") if status is None else status,
                    task_id,
                ),
            )
        return self.get_followup_task(task_id)

    def remember_followup_card(self, task_id: int, *, chat_id: str, message_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crm_followup_tasks
                SET admin_chat_id = ?,
                    admin_card_message_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (chat_id, message_id, task_id),
            )

    def find_followup_by_card(self, *, chat_id: str, message_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM crm_followup_tasks
                WHERE admin_chat_id = ?
                  AND admin_card_message_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(chat_id), str(message_id)),
            ).fetchone()
        return self.get_followup_task(int(row["id"])) if row else None

    def get_followup_task(self, task_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    t.*,
                    c.name AS client_name,
                    c.phone AS client_phone,
                    c.do_not_contact,
                    c.complaint_risk,
                    v.actual_service_title,
                    a.scheduled_at,
                    a.city
                FROM crm_followup_tasks t
                JOIN crm_clients c ON c.id = t.client_id
                LEFT JOIN crm_visits v ON v.id = t.visit_id
                LEFT JOIN crm_appointments a ON a.id = v.appointment_id
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_appointments_for_confirmation(self, day: str | date) -> list[dict[str, Any]]:
        day_value = day.isoformat() if isinstance(day, date) else str(day)[:10]
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    a.*,
                    c.name AS client_name,
                    c.phone AS client_phone,
                    c.skin_type AS client_skin_type,
                    c.consent_status AS client_consent_status,
                    c.do_not_contact AS client_do_not_contact,
                    c.complaint_risk AS client_complaint_risk
                FROM crm_appointments a
                JOIN crm_clients c ON c.id = a.client_id
                WHERE substr(a.scheduled_at, 1, 10) = ?
                  AND a.confirmation_status IN ('pending', 'sent', 'needs_details')
                ORDER BY a.scheduled_at, a.id
                """,
                (day_value,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_appointment(self, appointment_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    a.*,
                    c.name AS client_name,
                    c.phone AS client_phone,
                    c.skin_type AS client_skin_type,
                    c.consent_status AS client_consent_status,
                    c.do_not_contact AS client_do_not_contact,
                    c.complaint_risk AS client_complaint_risk
                FROM crm_appointments a
                JOIN crm_clients c ON c.id = a.client_id
                WHERE a.id = ?
                """,
                (appointment_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_appointment_by_confirmation_card(self, *, chat_id: str, message_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, c.name AS client_name, c.phone AS client_phone, c.skin_type AS client_skin_type
                FROM crm_appointments a
                JOIN crm_clients c ON c.id = a.client_id
                WHERE a.confirmation_chat_id = ?
                  AND a.confirmation_card_message_id = ?
                ORDER BY a.id DESC
                LIMIT 1
                """,
                (str(chat_id), str(message_id)),
            ).fetchone()
        return dict(row) if row else None

    def remember_confirmation_card(self, appointment_id: int, *, chat_id: str, message_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crm_appointments
                SET confirmation_status = 'sent',
                    confirmation_chat_id = ?,
                    confirmation_card_message_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (chat_id, message_id, appointment_id),
            )

    def mark_visit(
        self,
        appointment_id: int,
        *,
        attended: bool,
        actual_service_title: str = "",
        amount_ml: str = "",
        units: str = "",
        product_or_drug: str = "",
        procedure_notes: str = "",
        reaction: str = "",
        aftercare_notes: str = "",
        confirmed_by: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        appointment = self.get_appointment(appointment_id)
        if not appointment:
            raise KeyError(f"appointment {appointment_id} not found")
        now = datetime.now(timezone.utc).isoformat()
        service_title = actual_service_title.strip() or str(appointment.get("booked_service_title") or "")
        status = "attended" if attended else "no_show"
        confirmation_status = "confirmed" if attended else "no_show"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crm_visits (
                    appointment_id, client_id, actually_attended, actual_service_title, amount_ml, units,
                    product_or_drug, procedure_notes, reaction, aftercare_notes, confirmed_by, confirmed_at, source_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(appointment_id) DO UPDATE SET
                    actually_attended = excluded.actually_attended,
                    actual_service_title = excluded.actual_service_title,
                    amount_ml = COALESCE(NULLIF(excluded.amount_ml, ''), crm_visits.amount_ml),
                    units = COALESCE(NULLIF(excluded.units, ''), crm_visits.units),
                    product_or_drug = COALESCE(NULLIF(excluded.product_or_drug, ''), crm_visits.product_or_drug),
                    procedure_notes = COALESCE(NULLIF(excluded.procedure_notes, ''), crm_visits.procedure_notes),
                    reaction = COALESCE(NULLIF(excluded.reaction, ''), crm_visits.reaction),
                    aftercare_notes = COALESCE(NULLIF(excluded.aftercare_notes, ''), crm_visits.aftercare_notes),
                    confirmed_by = excluded.confirmed_by,
                    confirmed_at = excluded.confirmed_at,
                    source_text = excluded.source_text,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    appointment_id,
                    int(appointment["client_id"]),
                    int(attended),
                    service_title,
                    amount_ml,
                    units,
                    product_or_drug,
                    procedure_notes,
                    reaction,
                    aftercare_notes,
                    confirmed_by,
                    now,
                    source_text,
                ),
            )
            conn.execute(
                """
                UPDATE crm_appointments
                SET status = ?,
                    confirmation_status = ?,
                    confirmed_by = ?,
                    confirmed_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, confirmation_status, confirmed_by, now, appointment_id),
            )
            if attended:
                conn.execute(
                    """
                    UPDATE crm_clients
                    SET last_visit_at = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (str(appointment["scheduled_at"]), int(appointment["client_id"])),
                )
                visit = conn.execute("SELECT * FROM crm_visits WHERE appointment_id = ?", (appointment_id,)).fetchone()
                if visit:
                    self._plan_followup_tasks(conn, appointment=appointment, visit=dict(visit))
        updated = self.get_appointment(appointment_id)
        return updated or appointment

    def _plan_followup_tasks(self, conn: sqlite3.Connection, *, appointment: dict[str, Any], visit: dict[str, Any]) -> None:
        if bool(appointment.get("client_do_not_contact") or False) or bool(appointment.get("client_complaint_risk") or False):
            return
        service_title = str(visit.get("actual_service_title") or appointment.get("booked_service_title") or "")
        scheduled_at = _parse_iso_datetime(str(appointment.get("scheduled_at") or "")) or datetime.now()
        client_id = int(appointment["client_id"])
        visit_id = int(visit["id"])
        for rule in DEFAULT_FOLLOWUP_RULES:
            if not _followup_rule_matches(rule, service_title):
                continue
            due_at = (scheduled_at + timedelta(days=rule.delay_days)).isoformat()
            reason = _followup_reason(rule, service_title, scheduled_at)
            risk_level = "blocked" if bool(appointment.get("client_complaint_risk") or False) else "low"
            conn.execute(
                """
                INSERT INTO crm_followup_tasks (
                    client_id, visit_id, kind, due_at, channel, message_draft, reason, confidence, risk_level, draft_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(visit_id, kind) DO UPDATE SET
                    due_at = excluded.due_at,
                    channel = excluded.channel,
                    message_draft = excluded.message_draft,
                    reason = excluded.reason,
                    confidence = excluded.confidence,
                    risk_level = excluded.risk_level,
                    draft_source = excluded.draft_source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (client_id, visit_id, rule.kind, due_at, rule.channel, rule.message_draft, reason, 0.72, risk_level, "rule"),
            )

    def apply_visit_details_from_text(
        self,
        appointment_id: int,
        text: str,
        *,
        confirmed_by: str = "",
    ) -> dict[str, Any]:
        appointment = self.get_appointment(appointment_id)
        if not appointment:
            raise KeyError(f"appointment {appointment_id} not found")
        details = parse_actual_visit_details(text, booked_service_title=str(appointment.get("booked_service_title") or ""))
        self.add_interaction(
            int(appointment["client_id"]),
            appointment_id=appointment_id,
            channel="telegram",
            direction="internal_note",
            author=confirmed_by,
            body=text,
            intent="visit_fact_update",
            metadata=details,
        )
        if not details["understood"]:
            return self.mark_needs_details(appointment_id, confirmed_by=confirmed_by)
        return self.mark_visit(
            appointment_id,
            attended=True,
            actual_service_title=str(details["actual_service_title"]),
            amount_ml=str(details.get("amount_ml") or ""),
            confirmed_by=confirmed_by,
            source_text=text,
        )

    def mark_needs_details(self, appointment_id: int, *, confirmed_by: str = "") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crm_appointments
                SET confirmation_status = 'needs_details',
                    confirmed_by = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (confirmed_by, appointment_id),
            )
        row = self.get_appointment(appointment_id)
        if not row:
            raise KeyError(f"appointment {appointment_id} not found")
        return row

    def add_interaction(
        self,
        client_id: int,
        *,
        appointment_id: int | None = None,
        visit_id: int | None = None,
        channel: str = "telegram",
        direction: str = "internal_note",
        author: str = "",
        body: str = "",
        intent: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO crm_interactions (
                    client_id, appointment_id, visit_id, channel, direction, author, body, intent, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    appointment_id,
                    visit_id,
                    channel,
                    direction,
                    author,
                    body,
                    intent,
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            return int(cur.lastrowid)

    def create_learning_lesson(
        self,
        *,
        lesson: str,
        source: str = "",
        tags: tuple[str, ...] = (),
        confidence: float = 0.5,
        created_from_interaction_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO crm_learning_lessons (
                    lesson, source, tags, confidence, created_from_interaction_id, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lesson,
                    source,
                    ",".join(tag.strip() for tag in tags if tag.strip()),
                    float(confidence),
                    created_from_interaction_id,
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            row = conn.execute("SELECT * FROM crm_learning_lessons WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def list_learning_lessons(self, *, query: str = "", tags: tuple[str, ...] = (), limit: int = 20) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("(lower(lesson) LIKE ? OR lower(source) LIKE ?)")
            params.extend((f"%{query.casefold()}%", f"%{query.casefold()}%"))
        for tag in tags:
            if tag:
                clauses.append("tags LIKE ?")
                params.append(f"%{tag}%")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit or 20), 100)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM crm_learning_lessons
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_client_preference(
        self,
        client_id: int,
        *,
        preference_type: str,
        value: str,
        source: str = "",
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.get_client(client_id):
            raise KeyError(f"client {client_id} not found")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crm_client_preferences (
                    client_id, preference_type, value, source, confidence, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id, preference_type) DO UPDATE SET
                    value = excluded.value,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    client_id,
                    preference_type,
                    value,
                    source,
                    float(confidence),
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            row = conn.execute(
                "SELECT * FROM crm_client_preferences WHERE client_id = ? AND preference_type = ?",
                (client_id, preference_type),
            ).fetchone()
        return dict(row)

    def list_client_preferences(self, client_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM crm_client_preferences
                WHERE client_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (client_id, max(1, min(int(limit or 20), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_agent_decision(
        self,
        *,
        agent_role: str,
        input_ref: str = "",
        decision: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        outcome: str = "",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO crm_agent_decisions (agent_role, input_ref, decision_json, tool_calls_json, outcome)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    agent_role,
                    input_ref,
                    json.dumps(decision or {}, ensure_ascii=False, default=str),
                    json.dumps(tool_calls or [], ensure_ascii=False, default=str),
                    outcome,
                ),
            )
            return int(cur.lastrowid)

    def list_agent_decisions(self, *, agent_role: str = "", limit: int = 20) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if agent_role:
            clauses.append("agent_role = ?")
            params.append(agent_role)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit or 20), 100)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM crm_agent_decisions
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def suggest_client_merges(self, *, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        clients = self.search_clients(query, limit=limit) if query else []
        suggestions: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id AS source_id, b.id AS target_id, a.phone, a.name AS source_name, b.name AS target_name
                FROM crm_clients a
                JOIN crm_clients b ON b.id > a.id AND a.phone != '' AND a.phone = b.phone
                ORDER BY a.updated_at DESC, b.updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 10), 25)),),
            ).fetchall()
        for row in rows:
            pair = (int(row["source_id"]), int(row["target_id"]))
            seen.add(pair)
            suggestions.append({**dict(row), "reason": "same_phone", "confidence": 0.95})
        if clients:
            for index, left in enumerate(clients):
                for right in clients[index + 1 :]:
                    pair = tuple(sorted((int(left["id"]), int(right["id"]))))
                    if pair in seen:
                        continue
                    if left.get("name") and right.get("name") and str(left["name"]).casefold() == str(right["name"]).casefold():
                        suggestions.append(
                            {
                                "source_id": pair[0],
                                "target_id": pair[1],
                                "source_name": left.get("name"),
                                "target_name": right.get("name"),
                                "reason": "same_name_search_match",
                                "confidence": 0.55,
                            }
                        )
        return suggestions[: max(1, min(int(limit or 10), 25))]

    def merge_clients(self, *, source_client_id: int, target_client_id: int, merged_by: str = "") -> dict[str, Any]:
        if source_client_id == target_client_id:
            raise ValueError("source and target client ids must differ")
        source = self.get_client(source_client_id)
        target = self.get_client(target_client_id)
        if not source or not target:
            raise KeyError("source or target client not found")
        with self.connect() as conn:
            for table in ("crm_appointments", "crm_visits", "crm_followup_tasks", "crm_interactions", "crm_client_preferences"):
                conn.execute(f"UPDATE {table} SET client_id = ? WHERE client_id = ?", (target_client_id, source_client_id))
            for link in conn.execute("SELECT * FROM crm_client_links WHERE client_id = ?", (source_client_id,)).fetchall():
                try:
                    conn.execute(
                        "UPDATE crm_client_links SET client_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (target_client_id, int(link["id"])),
                    )
                except sqlite3.IntegrityError:
                    conn.execute("DELETE FROM crm_client_links WHERE id = ?", (int(link["id"]),))
            conn.execute(
                """
                UPDATE crm_clients
                SET phone = COALESCE(NULLIF(phone, ''), ?),
                    name = COALESCE(NULLIF(name, ''), ?),
                    city = COALESCE(NULLIF(city, ''), ?),
                    skin_type = COALESCE(NULLIF(skin_type, ''), ?),
                    notes = trim(COALESCE(notes, '') || CASE WHEN ? != '' THEN '\nMerged note: ' || ? ELSE '' END),
                    do_not_contact = max(do_not_contact, ?),
                    complaint_risk = max(complaint_risk, ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    source.get("phone") or "",
                    source.get("name") or "",
                    source.get("city") or "",
                    source.get("skin_type") or "",
                    source.get("notes") or "",
                    source.get("notes") or "",
                    int(source.get("do_not_contact") or 0),
                    int(source.get("complaint_risk") or 0),
                    target_client_id,
                ),
            )
            conn.execute("DELETE FROM crm_clients WHERE id = ?", (source_client_id,))
        self.record_agent_decision(
            agent_role="client_identity",
            input_ref=f"merge:{source_client_id}->{target_client_id}",
            decision={"source_client_id": source_client_id, "target_client_id": target_client_id, "merged_by": merged_by},
            outcome="merged",
        )
        return {"source_client_id": source_client_id, "target_client_id": target_client_id, "client": self.get_client(target_client_id)}


class VisitConfirmationService:
    def __init__(self, store: CareCrmStore, booking: YClientsGateway) -> None:
        self.store = store
        self.booking = booking

    async def sync_day(self, day: str | date) -> list[dict[str, Any]]:
        day_value = day.isoformat() if isinstance(day, date) else str(day)[:10]
        for appointment in await self.booking.list_appointments(day_value):
            self.store.upsert_appointment(appointment)
        return self.store.list_appointments_for_confirmation(day_value)


class ClientMemoryService:
    """Builds client memory objects for agents without exposing raw storage shape."""

    def __init__(self, store: CareCrmStore) -> None:
        self.store = store

    def memory(self, client_id: int, *, include_internal: bool = False, limit: int = 6) -> dict[str, Any]:
        client = self.store.get_client(client_id)
        if not client:
            raise KeyError(f"client {client_id} not found")
        visits = self.store.list_client_visits(client_id, limit=limit)
        links = self.store.list_client_links(client_id)
        preferences = self.store.list_client_preferences(client_id, limit=limit)
        followups = self.store.list_followup_tasks(client_id=client_id, status="", limit=limit)
        memory = {
            "client": _client_public_memory(client, include_internal=include_internal),
            "links": [_link_memory(row, include_internal=include_internal) for row in links],
            "visits": [_visit_memory(row) for row in visits],
            "preferences": [_preference_memory(row) for row in preferences],
            "followups": [_followup_memory(row) for row in followups],
            "summary": self.summary(client_id, include_internal=include_internal),
        }
        if include_internal:
            memory["interactions"] = [_interaction_memory(row) for row in self.store.list_client_interactions(client_id, limit=limit)]
        return memory

    def summary(self, client_id: int, *, include_internal: bool = False) -> str:
        client = self.store.get_client(client_id)
        if not client:
            return ""
        visits = self.store.list_client_visits(client_id, limit=3)
        prefs = self.store.list_client_preferences(client_id, limit=5)
        lines = [
            "ClientMemory:",
            f"name={client.get('name') or '-'}, city={client.get('city') or '-'}, last_visit_at={client.get('last_visit_at') or '-'}",
            f"do_not_contact={bool(client.get('do_not_contact') or False)}, complaint_risk={bool(client.get('complaint_risk') or False)}",
        ]
        if include_internal:
            lines.append(f"phone={client.get('phone') or '-'}, yclients_client_id={client.get('yclients_client_id') or '-'}")
        for visit in visits:
            lines.append(
                "Visit: "
                f"{visit.get('scheduled_at') or '-'}, "
                f"actual_service={visit.get('actual_service_title') or visit.get('booked_service_title') or '-'}, "
                f"attended={bool(visit.get('actually_attended') or False)}, city={visit.get('city') or '-'}"
            )
        for pref in prefs:
            lines.append(f"Preference: {pref.get('preference_type') or '-'}={pref.get('value') or '-'}")
        return "\n".join(lines)


class VisitFactService:
    """Agent-facing service for matching appointments and writing factual visit outcomes."""

    def __init__(self, store: CareCrmStore) -> None:
        self.store = store

    def match(self, *, query: str = "", day: str = "", limit: int = 10) -> list[dict[str, Any]]:
        return self.store.match_appointments(query=query, day=day, limit=limit)

    def upsert_fact(
        self,
        appointment_id: int,
        *,
        attended: bool = True,
        actual_service_title: str = "",
        amount_ml: str = "",
        units: str = "",
        product_or_drug: str = "",
        procedure_notes: str = "",
        reaction: str = "",
        aftercare_notes: str = "",
        confirmed_by: str = "agent",
        source_text: str = "",
    ) -> dict[str, Any]:
        row = self.store.mark_visit(
            appointment_id,
            attended=attended,
            actual_service_title=actual_service_title,
            amount_ml=amount_ml,
            units=units,
            product_or_drug=product_or_drug,
            procedure_notes=procedure_notes,
            reaction=reaction,
            aftercare_notes=aftercare_notes,
            confirmed_by=confirmed_by,
            source_text=source_text,
        )
        if source_text:
            self.store.add_interaction(
                int(row["client_id"]),
                appointment_id=appointment_id,
                channel="telegram_admin",
                direction="internal_note",
                author=confirmed_by,
                body=source_text,
                intent="visit_fact_update",
                metadata={
                    "actual_service_title": actual_service_title,
                    "amount_ml": amount_ml,
                    "product_or_drug": product_or_drug,
                    "reaction": reaction,
                },
            )
        return row


class ClientIdentityService:
    """Safe identity linking and duplicate suggestions."""

    def __init__(self, store: CareCrmStore) -> None:
        self.store = store

    def link(
        self,
        client_id: int,
        *,
        channel: str,
        external_user_id: str = "",
        chat_id: str = "",
        username: str = "",
        display_name: str = "",
        verified: bool = False,
    ) -> dict[str, Any]:
        self.store.link_client_channel(
            client_id,
            channel=channel,
            external_user_id=external_user_id,
            chat_id=chat_id,
            username=username,
            display_name=display_name,
            verified=verified,
        )
        return {"client": self.store.get_client(client_id), "links": self.store.list_client_links(client_id)}

    def suggest_merges(self, *, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        return self.store.suggest_client_merges(query=query, limit=limit)

    def apply_merge(self, *, source_client_id: int, target_client_id: int, merged_by: str = "") -> dict[str, Any]:
        return self.store.merge_clients(source_client_id=source_client_id, target_client_id=target_client_id, merged_by=merged_by)


class CareLearningService:
    """Durable learning memory: lessons, preferences and outcomes, not model fine-tuning."""

    def __init__(self, store: CareCrmStore) -> None:
        self.store = store

    def create_lesson(
        self,
        *,
        lesson: str,
        source: str = "",
        tags: tuple[str, ...] = (),
        confidence: float = 0.5,
        created_from_interaction_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.store.create_learning_lesson(
            lesson=lesson,
            source=source,
            tags=tags,
            confidence=confidence,
            created_from_interaction_id=created_from_interaction_id,
            metadata=metadata,
        )

    def lessons(self, *, query: str = "", tags: tuple[str, ...] = (), limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_learning_lessons(query=query, tags=tags, limit=limit)

    def upsert_preference(
        self,
        client_id: int,
        *,
        preference_type: str,
        value: str,
        source: str = "",
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.store.upsert_client_preference(
            client_id,
            preference_type=preference_type,
            value=value,
            source=source,
            confidence=confidence,
            metadata=metadata,
        )

    def record_outcome(
        self,
        *,
        agent_role: str,
        input_ref: str = "",
        decision: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        outcome: str = "",
    ) -> int:
        return self.store.record_agent_decision(
            agent_role=agent_role,
            input_ref=input_ref,
            decision=decision,
            tool_calls=tool_calls,
            outcome=outcome,
        )


class FollowupBrainService:
    """Creates and revises follow-up task text from factual memory and learning."""

    def __init__(self, store: CareCrmStore) -> None:
        self.store = store
        self.memory = ClientMemoryService(store)

    def enrich_task(self, task_id: int, *, draft_hint: str = "", source: str = "agent_rule") -> dict[str, Any] | None:
        task = self.store.get_followup_task(task_id)
        if not task:
            return None
        risk_level = self._risk_level(task)
        service = str(task.get("actual_service_title") or "визита")
        lesson = self._lesson_hint(service)
        draft = draft_hint.strip() or str(task.get("message_draft") or "").strip()
        if not draft:
            draft = f"Здравствуйте! Как вы себя чувствуете после {service}? Всё ли комфортно?"
        if lesson and lesson not in draft:
            reason = f"{task.get('reason') or ''} Учитываем урок: {lesson}".strip()
        else:
            reason = str(task.get("reason") or f"Мягкое сервисное касание после подтверждённого визита: {service}.")
        confidence = 0.0 if risk_level in {"blocked", "high"} else max(float(task.get("confidence") or 0.62), 0.7)
        return self.store.update_followup_task(
            task_id,
            message_draft=draft,
            reason=reason,
            confidence=confidence,
            risk_level=risk_level,
            draft_source=source,
        )

    def rewrite_from_olga(self, task_id: int, *, text: str, author: str = "olga") -> dict[str, Any] | None:
        task = self.store.update_followup_task(
            task_id,
            message_draft=text.strip(),
            approved_by=author,
            draft_source="olga_revision",
            outcome="draft_rewritten",
        )
        if task:
            self.store.create_learning_lesson(
                lesson="Ольга исправила follow-up черновик; учитывать такой тон/формулировку в похожих сообщениях.",
                source="followup_revision",
                tags=("followup", "olga_style", str(task.get("kind") or "")),
                confidence=0.75,
                metadata={"task_id": task_id, "text": text.strip()},
            )
        return task

    def _risk_level(self, task: dict[str, Any]) -> str:
        if bool(task.get("do_not_contact") or False):
            return "blocked"
        if bool(task.get("complaint_risk") or False):
            return "high"
        return str(task.get("risk_level") or "low")

    def _lesson_hint(self, service: str) -> str:
        lessons = self.store.list_learning_lessons(query=service, limit=1)
        if not lessons:
            lessons = self.store.list_learning_lessons(tags=("followup",), limit=1)
        return str(lessons[0].get("lesson") or "")[:280] if lessons else ""


def visit_confirmation_keyboard(appointment_id: int) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {"text": "Да", "callback_data": f"visitconfirm:{appointment_id}:yes"},
                {"text": "Нет", "callback_data": f"visitconfirm:{appointment_id}:no"},
            ],
            [{"text": "Другая услуга", "callback_data": f"visitconfirm:{appointment_id}:other"}],
        ]
    }


def parse_visit_confirmation_callback(data: str) -> VisitConfirmationAction | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "visitconfirm":
        return None
    try:
        appointment_id = int(parts[1])
    except ValueError:
        return None
    if parts[2] not in {"yes", "no", "other"}:
        return None
    return VisitConfirmationAction(appointment_id=appointment_id, action=parts[2])


def format_visit_confirmation_card(row: dict[str, Any]) -> str:
    client = str(row.get("client_name") or "Клиент").strip()
    phone = str(row.get("client_phone") or "").strip()
    scheduled_at = _format_scheduled_at(str(row.get("scheduled_at") or ""))
    service = str(row.get("booked_service_title") or "услуга не указана").strip()
    city = str(row.get("city") or "").strip()
    city_line = f"\nГород: <b>{escape(city)}</b>" if city else ""
    phone_line = f"\nТелефон: <code>{escape(phone)}</code>" if phone else ""
    return (
        "<b>Проверка визита</b>\n"
        f"Клиент: <b>{escape(client)}</b>{phone_line}\n"
        f"Была запись: <b>{escape(scheduled_at)}</b>{city_line}\n"
        f"Процедура по записи: <b>{escape(service)}</b>\n\n"
        "Клиент был и услуга оказана?"
    )


def visit_confirmation_result_text(row: dict[str, Any], action: str) -> str:
    client = escape(str(row.get("client_name") or "клиент"))
    if action == "yes":
        return f"Отметила: <b>{client}</b> был(а), услуга подтверждена."
    if action == "no":
        return f"Отметила: <b>{client}</b> не был(а). Для допродаж этот визит использовать не будем."
    return (
        f"Ок, по <b>{client}</b> нужна фактическая процедура. "
        "Ответьте текстом на карточку: что реально сделали, объём/препарат и важные заметки."
    )


def visit_details_update_result_text(row: dict[str, Any], source_text: str) -> str:
    details = parse_actual_visit_details(source_text, booked_service_title=str(row.get("booked_service_title") or ""))
    client = escape(str(row.get("client_name") or "клиент"))
    if not details["understood"]:
        return (
            f"По <b>{client}</b> не уверена, что поняла фактическую услугу. "
            "Напишите, например: <code>губы 1 мл, препарат Juvederm</code>."
        )
    service = escape(str(details["actual_service_title"]))
    amount = escape(str(details.get("amount_ml") or ""))
    amount_line = f"\nОбъём: <b>{amount} мл</b>" if amount else ""
    return f"Запомнила фактический визит по <b>{client}</b>:\nУслуга: <b>{service}</b>{amount_line}"


def parse_actual_visit_details(text: str, *, booked_service_title: str = "") -> dict[str, Any]:
    raw = str(text or "").strip()
    amount_match = re_search_amount_ml(raw)
    service_title = _extract_actual_service_title(raw, booked_service_title=booked_service_title)
    understood = bool(service_title and len(service_title) >= 3)
    return {
        "understood": understood,
        "actual_service_title": service_title,
        "amount_ml": amount_match or "",
    }


def re_search_amount_ml(text: str) -> str:
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:мл|ml)\b", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).replace(",", ".")


def _extract_actual_service_title(text: str, *, booked_service_title: str = "") -> str:
    cleaned = " ".join(str(text or "").replace("\n", " ").split()).strip(" .,!?:;-")
    if not cleaned:
        return ""
    lowered = cleaned.casefold()
    if lowered in {"да", "был", "была", "пришла", "пришел", "ок", "нет"}:
        return ""
    prefixes = (
        "на самом деле",
        "по факту",
        "фактически",
        "сделали",
        "делали",
        "была",
        "был",
        "пришла",
        "пришел",
        "оказали",
    )
    for prefix in prefixes:
        if lowered.startswith(prefix + " "):
            cleaned = cleaned[len(prefix) :].strip(" .,!?:;-")
            lowered = cleaned.casefold()
            break
    correction_match = re.search(r"\bа\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if correction_match and ("не " in lowered or "вместо" in lowered):
        cleaned = correction_match.group(1).strip(" .,!?:;-")
    without_amount = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:мл|ml)\b", "", cleaned, flags=re.IGNORECASE)
    service_part = without_amount.split(",", 1)[0].strip(" .,!?:;-")
    service_part = " ".join(service_part.split()).strip(" .,!?:;-")
    return service_part


def _followup_rule_matches(rule: FollowupRule, service_title: str) -> bool:
    return True


def _followup_reason(rule: FollowupRule, service_title: str, scheduled_at: datetime) -> str:
    service = service_title or "подтверждённого визита"
    if rule.kind == "care_checkin_1d":
        return f"Проверка самочувствия через 1 день после фактической услуги: {service}."
    if rule.kind == "result_checkin_14d":
        return f"Проверка результата через 14 дней после фактической услуги: {service}."
    return f"Мягкое сервисное касание после визита {scheduled_at.date().isoformat()}: {service}."


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _format_scheduled_at(value: str) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d.%m.%Y %H:%M")


def _normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    followup_columns = {
        "reason": "TEXT DEFAULT ''",
        "confidence": "REAL NOT NULL DEFAULT 0.5",
        "risk_level": "TEXT NOT NULL DEFAULT 'low'",
        "approved_by": "TEXT DEFAULT ''",
        "approved_at": "TEXT DEFAULT ''",
        "draft_source": "TEXT DEFAULT 'rule'",
        "outcome": "TEXT DEFAULT ''",
        "admin_chat_id": "TEXT DEFAULT ''",
        "admin_card_message_id": "TEXT DEFAULT ''",
    }
    for column, definition in followup_columns.items():
        _ensure_column(conn, "crm_followup_tasks", column, definition)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _client_public_memory(row: dict[str, Any], *, include_internal: bool = False) -> dict[str, Any]:
    data = {
        "name": str(row.get("name") or ""),
        "city": str(row.get("city") or ""),
        "last_visit_at": str(row.get("last_visit_at") or ""),
        "consent_status": str(row.get("consent_status") or "unknown"),
        "do_not_contact": bool(row.get("do_not_contact") or False),
        "complaint_risk": bool(row.get("complaint_risk") or False),
    }
    if include_internal:
        data.update(
            {
                "client_id": int(row.get("id") or 0),
                "yclients_client_id": str(row.get("yclients_client_id") or ""),
                "phone": str(row.get("phone") or ""),
                "skin_type": str(row.get("skin_type") or ""),
                "notes": str(row.get("notes") or ""),
            }
        )
    return data


def _link_memory(row: dict[str, Any], *, include_internal: bool = False) -> dict[str, Any]:
    data = {
        "channel": str(row.get("channel") or ""),
        "verified": bool(row.get("verified") or False),
        "display_name": str(row.get("display_name") or ""),
        "username": str(row.get("username") or ""),
    }
    if include_internal:
        data.update(
            {
                "external_user_id": str(row.get("external_user_id") or ""),
                "chat_id": str(row.get("chat_id") or ""),
            }
        )
    return data


def _visit_memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scheduled_at": str(row.get("scheduled_at") or ""),
        "city": str(row.get("city") or ""),
        "booked_service_title": str(row.get("booked_service_title") or ""),
        "actual_service_title": str(row.get("actual_service_title") or ""),
        "actually_attended": bool(row.get("actually_attended") or False),
        "amount_ml": str(row.get("amount_ml") or ""),
        "units": str(row.get("units") or ""),
        "product_or_drug": str(row.get("product_or_drug") or ""),
        "reaction": str(row.get("reaction") or ""),
        "aftercare_notes": str(row.get("aftercare_notes") or ""),
    }


def _preference_memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "preference_type": str(row.get("preference_type") or ""),
        "value": str(row.get("value") or ""),
        "source": str(row.get("source") or ""),
        "confidence": float(row.get("confidence") or 0.0),
    }


def _followup_memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(row.get("kind") or ""),
        "due_at": str(row.get("due_at") or ""),
        "status": str(row.get("status") or ""),
        "reason": str(row.get("reason") or ""),
        "confidence": float(row.get("confidence") or 0.0),
        "risk_level": str(row.get("risk_level") or ""),
        "outcome": str(row.get("outcome") or ""),
    }


def _interaction_memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "channel": str(row.get("channel") or ""),
        "direction": str(row.get("direction") or ""),
        "author": str(row.get("author") or ""),
        "body": str(row.get("body") or "")[:1200],
        "intent": str(row.get("intent") or ""),
        "created_at": str(row.get("created_at") or ""),
    }
