from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..config import ROOT


DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH = ROOT / "data" / "telegram_client_topics.json"
MAX_TOPIC_TITLE_LENGTH = 120


def client_topic_key(
    *,
    channel: str,
    external_chat_id: str,
    account_id: str | int = "",
    client_id: str = "",
) -> str:
    channel = _compact_key(channel or "unknown")
    account = _compact_key(str(account_id or ""))
    external = _compact_key(str(external_chat_id or client_id or ""))
    if account:
        return f"{channel}:{account}:{external}"
    return f"{channel}:{external}"


def avito_followup_topic_key(row: dict[str, Any]) -> str:
    return client_topic_key(
        channel="avito",
        account_id=str(row.get("account_id") or ""),
        external_chat_id=str(row.get("chat_id") or ""),
    )


def topic_title_for_client(
    *,
    client_name: str = "",
    channel: str = "",
    city: str = "",
    listing_title: str = "",
    external_chat_id: str = "",
) -> str:
    parts = []
    name = _clean_title_part(client_name)
    if name:
        parts.append(name)
    channel_label = _clean_title_part(_channel_title(channel))
    city_label = _clean_title_part(city)
    if channel_label or city_label:
        parts.append(" / ".join(part for part in (channel_label, city_label) if part))
    listing = _clean_title_part(listing_title)
    if listing:
        parts.append(listing)
    if not parts:
        fallback = _clean_title_part(external_chat_id)
        parts.append(fallback or "Клиент")
    return _truncate_title(" | ".join(parts), MAX_TOPIC_TITLE_LENGTH)


def load_client_topics(path: Path | str = DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_client_topics(rows: dict[str, dict[str, Any]], path: Path | str = DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def remember_client_topic(
    *,
    key: str,
    telegram_chat_id: str,
    message_thread_id: str | int,
    title: str,
    channel: str = "",
    external_chat_id: str = "",
    account_id: str | int = "",
    client_name: str = "",
    listing_title: str = "",
    city: str = "",
    path: Path | str = DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH,
) -> dict[str, Any]:
    key = str(key or "").strip()
    telegram_chat_id = str(telegram_chat_id or "").strip()
    message_thread_id = str(message_thread_id or "").strip()
    if not key or not telegram_chat_id or not message_thread_id:
        return {}
    now = int(time.time())
    rows = load_client_topics(path)
    existing = rows.get(key) if isinstance(rows.get(key), dict) else {}
    row = {
        "key": key,
        "telegram_chat_id": telegram_chat_id,
        "message_thread_id": message_thread_id,
        "title": _truncate_title(title or existing.get("title") or "Клиент", MAX_TOPIC_TITLE_LENGTH),
        "channel": str(channel or existing.get("channel") or "").strip(),
        "external_chat_id": str(external_chat_id or existing.get("external_chat_id") or "").strip(),
        "account_id": str(account_id or existing.get("account_id") or "").strip(),
        "client_name": str(client_name or existing.get("client_name") or "").strip(),
        "listing_title": str(listing_title or existing.get("listing_title") or "").strip(),
        "city": str(city or existing.get("city") or "").strip(),
        "created_at": int(existing.get("created_at") or now),
        "updated_at": now,
    }
    rows[key] = row
    save_client_topics(rows, path)
    return row


def find_client_topic(
    key: str,
    *,
    telegram_chat_id: str = "",
    path: Path | str = DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH,
) -> dict[str, Any] | None:
    row = load_client_topics(path).get(str(key or "").strip())
    if not isinstance(row, dict):
        return None
    if telegram_chat_id and str(row.get("telegram_chat_id") or "").strip() != str(telegram_chat_id).strip():
        return None
    return row


def topic_params_from_row(row: dict[str, Any] | None) -> dict[str, str]:
    thread_id = str((row or {}).get("message_thread_id") or "").strip()
    return {"message_thread_id": thread_id} if thread_id else {}


def get_or_create_client_topic(
    bot: Any,
    telegram_chat_id: str,
    *,
    key: str,
    title: str,
    channel: str = "",
    external_chat_id: str = "",
    account_id: str | int = "",
    client_name: str = "",
    listing_title: str = "",
    city: str = "",
    enabled: bool = True,
    path: Path | str = DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH,
) -> dict[str, Any]:
    if not enabled:
        return {"ok": False, "reason": "disabled", "topic_params": {}}
    telegram_chat_id = str(telegram_chat_id or "").strip()
    if not telegram_chat_id or not key:
        return {"ok": False, "reason": "missing_key_or_chat", "topic_params": {}}
    existing = find_client_topic(key, telegram_chat_id=telegram_chat_id, path=path)
    if existing:
        return {"ok": True, "created": False, "topic": existing, "topic_params": topic_params_from_row(existing)}
    try:
        created = bot.create_forum_topic(telegram_chat_id, title)
        result = created.get("result") if isinstance(created, dict) else {}
        thread_id = str((result or {}).get("message_thread_id") or "").strip()
        if not thread_id:
            return {"ok": False, "reason": "missing_message_thread_id", "telegram": created, "topic_params": {}}
    except Exception as exc:
        return {"ok": False, "reason": "create_failed", "error": repr(exc), "topic_params": {}}
    row = remember_client_topic(
        key=key,
        telegram_chat_id=telegram_chat_id,
        message_thread_id=thread_id,
        title=title,
        channel=channel,
        external_chat_id=external_chat_id,
        account_id=account_id,
        client_name=client_name,
        listing_title=listing_title,
        city=city,
        path=path,
    )
    return {"ok": True, "created": True, "topic": row, "telegram": created, "topic_params": topic_params_from_row(row)}


def topic_request_from_avito_followup(row: dict[str, Any]) -> dict[str, Any]:
    title = topic_title_for_client(
        client_name=str(row.get("client_name") or ""),
        channel="Avito",
        city=str(row.get("listing_city") or ""),
        listing_title=str(row.get("listing_title") or ""),
        external_chat_id=str(row.get("chat_id") or ""),
    )
    return {
        "key": avito_followup_topic_key(row),
        "title": title,
        "channel": "avito",
        "external_chat_id": str(row.get("chat_id") or ""),
        "account_id": str(row.get("account_id") or ""),
        "client_name": str(row.get("client_name") or ""),
        "listing_title": str(row.get("listing_title") or ""),
        "city": str(row.get("listing_city") or ""),
    }


def _compact_key(value: str) -> str:
    value = str(value or "").strip()
    return re.sub(r"[^a-zA-Z0-9_.~:-]+", "_", value)[:180]


def _clean_title_part(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").replace("\n", " ")).strip()
    value = value.replace("<", "").replace(">", "")
    return value[:80]


def _channel_title(channel: str) -> str:
    value = str(channel or "").strip()
    labels = {"avito": "Avito", "vk": "VK", "telegram_client": "Telegram"}
    return labels.get(value.casefold(), value)


def _truncate_title(value: str, limit: int) -> str:
    value = _clean_title_part(value)
    if len(value) <= limit:
        return value or "Клиент"
    return value[: max(1, limit - 1)].rstrip() + "…"
