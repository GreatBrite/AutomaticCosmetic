from __future__ import annotations

import re
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..storage import LeadStore


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_AVITO_WEBHOOK_LOG_PATH = Path("data/avito_webhook.log")
GREETING_RE = re.compile(
    r"^\s*(?:(?:[А-ЯЁA-Z][^,\n]{0,40}),\s*)?"
    r"(?:здравствуйте|здравствуй|добрый\s+(?:день|вечер)|доброе\s+утро)"
    r"(?:\s*[!,.🤍🫶😊]*)\s*",
    re.IGNORECASE,
)
CLIENT_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)?[\s().-]*9\d{2}(?:[\s().-]*\d){7}(?!\d)")


def avito_history_key(chat_id: str) -> str:
    return f"avito:client:{str(chat_id or '').strip()}"


def prepare_avito_outgoing_text(store: LeadStore | None, chat_id: str, text: str, *, mask_client_phone_echo: bool = True) -> str:
    clean = str(text or "").strip()
    if mask_client_phone_echo:
        clean = CLIENT_PHONE_RE.sub("[телефон]", clean)
    if not clean or store is None:
        return clean
    history = store.recent_codex_chat(0, avito_history_key(chat_id))
    if _greeted_today(history) or _greeted_today_in_log(chat_id):
        clean = GREETING_RE.sub("", clean, count=1).lstrip(" ,.!-\n")
        if clean:
            clean = clean[:1].upper() + clean[1:]
    return clean


def remember_avito_outgoing(store: LeadStore | None, chat_id: str, text: str) -> None:
    if store is None or not str(text or "").strip():
        return
    store.add_codex_chat_message("assistant", str(text).strip()[-4000:], avito_history_key(chat_id))


def sent_successfully(result: Any) -> bool:
    if not isinstance(result, dict) or not result.get("sent"):
        return False
    caption_result = result.get("caption_result")
    if caption_result is not None:
        return sent_successfully(caption_result)
    return True


def _greeted_today(history: list[dict[str, Any]]) -> bool:
    today = datetime.now(MOSCOW_TZ).date()
    for item in history:
        if str(item.get("role") or "") != "assistant":
            continue
        created_at = _parse_created_at(str(item.get("created_at") or ""))
        if created_at is None or created_at.astimezone(MOSCOW_TZ).date() != today:
            continue
        if GREETING_RE.match(str(item.get("content") or "")):
            return True
    return False


def _greeted_today_in_log(chat_id: str, path: Path = DEFAULT_AVITO_WEBHOOK_LOG_PATH) -> bool:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    today = datetime.now(MOSCOW_TZ).date()
    for line in reversed(lines[-5000:]):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("chat_id") or "") != str(chat_id or ""):
            continue
        if row.get("event") != "ignored" or row.get("reason") != "own_message":
            continue
        try:
            created_at = datetime.fromtimestamp(float(row.get("ts") or 0), MOSCOW_TZ)
        except (TypeError, ValueError, OSError):
            continue
        if created_at.date() < today:
            break
        if created_at.date() == today and GREETING_RE.match(str(row.get("text_preview") or "")):
            return True
    return False


def _parse_created_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed
