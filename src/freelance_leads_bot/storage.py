from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    company TEXT,
    budget TEXT,
    score INTEGER NOT NULL,
    status TEXT DEFAULT 'new',
    estimate_json TEXT DEFAULT '{}',
    codex_review_path TEXT DEFAULT '',
    lead_type TEXT DEFAULT 'project',
    apply_channel TEXT DEFAULT 'unknown',
    last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codex_chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL DEFAULT 'default',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS telegram_scout_seen (
    post_key TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    url TEXT DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

COLUMNS = {
    "status": "TEXT DEFAULT 'new'",
    "estimate_json": "TEXT DEFAULT '{}'",
    "codex_review_path": "TEXT DEFAULT ''",
    "lead_type": "TEXT DEFAULT 'project'",
    "apply_channel": "TEXT DEFAULT 'unknown'",
    "last_seen_at": "TEXT DEFAULT ''",
}

CODEX_CHAT_HISTORY_COLUMNS = {
    "chat_key": "TEXT NOT NULL DEFAULT 'default'",
}


class LeadStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
            for column, ddl in COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {ddl}")
            existing_history = {row[1] for row in conn.execute("PRAGMA table_info(codex_chat_history)")}
            for column, ddl in CODEX_CHAT_HISTORY_COLUMNS.items():
                if column not in existing_history:
                    conn.execute(f"ALTER TABLE codex_chat_history ADD COLUMN {column} {ddl}")
            conn.execute(
                """
                UPDATE leads
                SET last_seen_at = COALESCE(NULLIF(last_seen_at, ''), created_at, CURRENT_TIMESTAMP)
                WHERE last_seen_at IS NULL OR last_seen_at = ''
                """
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def add_if_new(self, lead: dict) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO leads (id, source, title, url, company, budget, score, status, estimate_json, lead_type, apply_channel)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead["id"],
                    lead["source"],
                    lead["title"],
                    lead["url"],
                    lead.get("company", ""),
                    lead.get("budget", ""),
                    lead["score"],
                    lead.get("status", "new"),
                    lead.get("estimate_json", "{}"),
                    lead.get("lead_type", "project"),
                    lead.get("apply_channel", "unknown"),
                ),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    UPDATE leads
                    SET last_seen_at = CURRENT_TIMESTAMP, score = MAX(score, ?), estimate_json = ?, lead_type = ?, apply_channel = ?
                    WHERE id = ?
                    """,
                    (
                        lead["score"],
                        lead.get("estimate_json", "{}"),
                        lead.get("lead_type", "project"),
                        lead.get("apply_channel", "unknown"),
                        lead["id"],
                    ),
                )
                return False
            return True

    def get(self, lead_id: str) -> dict | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None

    def update_status(self, lead_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))

    def update_codex_review(self, lead_id: str, path: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE leads SET codex_review_path = ? WHERE id = ?", (path, lead_id))

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def radar_enabled(self) -> bool:
        return self.get_setting("radar_enabled", "0") == "1"

    def set_radar_enabled(self, enabled: bool) -> None:
        self.set_setting("radar_enabled", "1" if enabled else "0")

    def codex_chat_enabled(self) -> bool:
        return self.get_setting("codex_chat_enabled", "0") == "1"

    def set_codex_chat_enabled(self, enabled: bool) -> None:
        self.set_setting("codex_chat_enabled", "1" if enabled else "0")

    def add_codex_chat_message(self, role: str, content: str, chat_key: str = "default") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO codex_chat_history (chat_key, role, content) VALUES (?, ?, ?)",
                (chat_key, role, content[-4000:]),
            )

    def recent_codex_chat(self, limit: int = 10, chat_key: str = "default") -> list[dict]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            limit_clause = "" if limit <= 0 else "LIMIT ?"
            params: tuple[Any, ...] = (chat_key,) if limit <= 0 else (chat_key, limit)
            rows = conn.execute(
                f"""
                SELECT role, content, created_at
                FROM codex_chat_history
                WHERE chat_key = ?
                ORDER BY id DESC
                {limit_clause}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_codex_chat_by_prefix(self, chat_key_prefix: str, limit: int = 10) -> list[dict]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            limit_clause = "" if limit <= 0 else "LIMIT ?"
            params: tuple[Any, ...] = (
                (chat_key_prefix, f"{chat_key_prefix}:%")
                if limit <= 0
                else (chat_key_prefix, f"{chat_key_prefix}:%", limit)
            )
            rows = conn.execute(
                f"""
                SELECT role, content, created_at
                FROM codex_chat_history
                WHERE chat_key = ? OR chat_key LIKE ?
                ORDER BY id DESC
                {limit_clause}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_codex_chat(self, chat_key: str = "default") -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM codex_chat_history WHERE chat_key = ?", (chat_key,))

    def scout_seen(self, post_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM telegram_scout_seen WHERE post_key = ?", (post_key,)).fetchone()
        return row is not None

    def mark_scout_seen(
        self,
        post_key: str,
        channel: str,
        message_id: int,
        url: str = "",
        score: int = 0,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO telegram_scout_seen (post_key, channel, message_id, url, score)
                VALUES (?, ?, ?, ?, ?)
                """,
                (post_key, channel, message_id, url, score),
            )

    def list_leads(self, status: str = "active", limit: int = 8, offset: int = 0) -> list[dict]:
        where = ""
        params: list[object] = []
        if status == "active":
            where = "WHERE status IN ('new', 'do') AND lead_type = 'project' AND apply_channel != 'research_only'"
        elif status in {"new", "do", "skip", "job"}:
            where = "WHERE status = ?"
            params.append(status)
        query = f"""
            SELECT *
            FROM leads
            {where}
            ORDER BY
                CASE status WHEN 'do' THEN 0 WHEN 'new' THEN 1 ELSE 2 END,
                score DESC,
                datetime(last_seen_at) DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count_leads(self, status: str = "active") -> int:
        where = ""
        params: list[object] = []
        if status == "active":
            where = "WHERE status IN ('new', 'do') AND lead_type = 'project' AND apply_channel != 'research_only'"
        elif status in {"new", "do", "skip", "job"}:
            where = "WHERE status = ?"
            params.append(status)
        with self.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM leads {where}", params).fetchone()[0]

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE date(created_at) = date('now')"
            ).fetchone()[0]
            fresh = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE datetime(last_seen_at) >= datetime('now', '-24 hours')"
            ).fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE status IN ('new', 'do') AND lead_type = 'project' AND apply_channel != 'research_only'"
            ).fetchone()[0]
            do = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'do'").fetchone()[0]
            skip = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'skip'").fetchone()[0]
            jobs = conn.execute("SELECT COUNT(*) FROM leads WHERE lead_type = 'job' OR status = 'job'").fetchone()[0]
            research = conn.execute("SELECT COUNT(*) FROM leads WHERE apply_channel = 'research_only'").fetchone()[0]
        return {
            "total": total,
            "today": today,
            "fresh": fresh,
            "active": active,
            "do": do,
            "skip": skip,
            "jobs": jobs,
            "research": research,
        }
