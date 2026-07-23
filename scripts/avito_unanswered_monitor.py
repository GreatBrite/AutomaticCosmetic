from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from pyavitoapi.transport.errors import AvitoApiError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.freelance_leads_bot.integrations.avito_identity import client_name_from_chat, update_client_name_cache
from src.freelance_leads_bot.integrations.agent_tools import AutomationToolbox
from src.freelance_leads_bot.integrations.agent_trace import JsonlAgentTraceLogger
from src.freelance_leads_bot.integrations.avito_consultant import CodexToolLoopPlanner
from src.freelance_leads_bot.integrations.avito_media import AvitoApiPhotoResolver
from src.freelance_leads_bot.integrations.avito_read import AvitoReadClient, AvitoReadGateway
from src.freelance_leads_bot.integrations.avito_sender import avito_sender_from_settings
from src.freelance_leads_bot.integrations.avito_webhook import process_avito_message
from src.freelance_leads_bot.integrations.codex_planner import CodexPlannerRunner
from src.freelance_leads_bot.integrations.config import IntegrationSettings
from src.freelance_leads_bot.integrations.expert_rag import ExpertRagStore
from src.freelance_leads_bot.integrations.avito_followup_admin import (
    apply_pending_followup_action,
    pending_followup_card_text,
    pending_followup_keyboard,
    pending_followup_token,
)
from src.freelance_leads_bot.integrations.handoff_notify import handoff_notifier_from_settings, process_handoff_sla
from src.freelance_leads_bot.integrations.models import AvitoListingContext, Channel, InboundMessage
from src.freelance_leads_bot.integrations.roles import CodexRole, role_profile
from src.freelance_leads_bot.integrations.runtime import booking_from_settings
from src.freelance_leads_bot.storage import LeadStore


DEFAULT_STATE_PATH = Path("data/avito_unanswered_monitor_state.json")
DEFAULT_REPORT_PATH = Path("data/avito_unanswered_report.json")
DEFAULT_LOG_PATH = Path("data/avito_unanswered_monitor.log")
DELETED_MESSAGE_TEXTS = {"сообщение удалено", "message deleted"}
FINAL_ACK_RE = re.compile(
    r"(?iu)^\s*(хорошо|ок|окей|спасибо|спасибо большое|спасибо[, ]+не надо.*|не надо.*|не нужно.*|"
    r"благодарю|поняла|понял|да|нет|👍|🙏|🌸)[.!?\s🙏👍🌸]*$"
)
CLIENT_CRITICAL_RE = re.compile(
    r"(?iu)(жду|адрес|запиш|запись актуальн|актуальна ли запись|что делать|вы забыли|забыли|"
    r"долго|отзыв|жалоб|оплат|предоплат|время|точно.*жд|сегодня.*приход|завтра.*приход|опозда|"
    r"гелендж|моск|ростов|краснодар|питер|спб)"
)
CLIENT_ACTION_RE = re.compile(r"(?iu)(\?|как|где|когда|можно|сколько|подскаж|уточн|провер|гелендж|моск|ростов|краснодар|питер|спб)")
BOT_PROMISE_RE = re.compile(
    r"(?iu)(уточн(?:ю|им)|провер(?:ю|им)|верн[уеё]сь с ответом|верн[её]мся с ответом|"
    r"напишу точн(?:ый|ую)|подтверж(?:у|дим)|свер(?:ю|им)|передам|сейчас проверим)"
)
BOT_FINAL_RE = re.compile(r"(?iu)(подтвержден[ао]?|записал[аи]?|адрес[:\s]|принимаем по адресу|можете приходить|оплата|предоплата|стоимость)")


@dataclass(frozen=True)
class UnansweredChat:
    account_id: int
    chat_id: str
    client_name: str
    message_id: str
    message_type: str
    text: str
    created: int
    age_seconds: int
    listing_title: str = ""
    listing_city: str = ""
    raw_chat: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    raw_message: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    needs_action: bool = True
    severity: str = "action"
    reason: str = "latest_client_message"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "client_name": self.client_name,
            "message_id": self.message_id,
            "message_type": self.message_type,
            "text": self.text,
            "created": self.created,
            "created_at": datetime.fromtimestamp(self.created, timezone.utc).isoformat() if self.created else "",
            "age_seconds": self.age_seconds,
            "age_minutes": round(self.age_seconds / 60, 1),
            "listing_title": self.listing_title,
            "listing_city": self.listing_city,
            "needs_action": self.needs_action,
            "severity": self.severity,
            "reason": self.reason,
        }


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _items(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _content(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    return content if isinstance(content, dict) else {}


def _message_text(message: dict[str, Any]) -> str:
    content = _content(message)
    text = str(content.get("text") or "").strip()
    if text:
        return text
    message_type = str(message.get("type") or "").strip()
    if message_type in {"image", "photo"} or content.get("image") or content.get("photo"):
        return "[фото]"
    if message_type == "video" or content.get("video"):
        return "[видео]"
    if message_type == "voice" or content.get("voice"):
        return "[голосовое]"
    if message_type == "file" or content.get("file"):
        return "[файл]"
    return "[пустое сообщение]"


def _is_relevant_incoming(message: dict[str, Any], *, account_id: int) -> bool:
    if str(message.get("direction") or "") != "in":
        return False
    message_type = str(message.get("type") or "")
    if message_type == "system":
        return False
    author_id = str(message.get("author_id") or "")
    if author_id and author_id == str(account_id):
        return False
    text = str(_content(message).get("text") or "").strip().casefold()
    if message_type == "deleted" or text in DELETED_MESSAGE_TEXTS:
        return False
    return True


def _is_outgoing_reply(message: dict[str, Any], *, account_id: int) -> bool:
    direction = str(message.get("direction") or "")
    author_id = str(message.get("author_id") or "")
    if direction == "out":
        return True
    return bool(author_id and author_id == str(account_id))


def _listing(chat: dict[str, Any]) -> tuple[str, str]:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    value = context.get("value") if isinstance(context.get("value"), dict) else {}
    location = value.get("location") if isinstance(value.get("location"), dict) else {}
    return str(value.get("title") or ""), str(location.get("title") or value.get("city") or "")


def _find_unanswered(
    *,
    account_id: int,
    chat: dict[str, Any],
    messages: list[dict[str, Any]],
    now: int,
    min_age_seconds: int,
    lookback_seconds: int,
) -> UnansweredChat | None:
    ordered = sorted(messages, key=lambda item: int(item.get("created") or 0))
    latest_incoming: dict[str, Any] | None = None
    latest_outgoing_after_incoming: dict[str, Any] | None = None
    cutoff = now - lookback_seconds if lookback_seconds > 0 else 0

    for message in ordered:
        created = int(message.get("created") or 0)
        if _is_relevant_incoming(message, account_id=account_id):
            latest_incoming = message
            latest_outgoing_after_incoming = None
        elif latest_incoming is not None and _is_outgoing_reply(message, account_id=account_id):
            latest_outgoing_after_incoming = message

    if latest_incoming is None or latest_outgoing_after_incoming is not None:
        return None

    created = int(latest_incoming.get("created") or 0)
    age_seconds = max(0, now - created)
    if created < cutoff or age_seconds < min_age_seconds:
        return None

    listing_title, listing_city = _listing(chat)
    classification = _classify_client_message(_message_text(latest_incoming))
    return UnansweredChat(
        account_id=account_id,
        chat_id=str(chat.get("id") or ""),
        client_name=client_name_from_chat(chat, account_id=account_id, author_id=latest_incoming.get("author_id")),
        message_id=str(latest_incoming.get("id") or ""),
        message_type=str(latest_incoming.get("type") or ""),
        text=_message_text(latest_incoming),
        created=created,
        age_seconds=age_seconds,
        listing_title=listing_title,
        listing_city=listing_city,
        raw_chat=chat,
        raw_message=latest_incoming,
        needs_action=classification["needs_action"],
        severity=classification["severity"],
        reason=classification["reason"],
    )


def _classify_client_message(text: str) -> dict[str, Any]:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return {"needs_action": True, "severity": "action", "reason": "empty_or_media_message"}
    if FINAL_ACK_RE.fullmatch(normalized):
        return {"needs_action": False, "severity": "low", "reason": "final_ack"}
    if CLIENT_CRITICAL_RE.search(normalized):
        return {"needs_action": True, "severity": "critical", "reason": "critical_client_message"}
    if CLIENT_ACTION_RE.search(normalized):
        return {"needs_action": True, "severity": "action", "reason": "question_or_action_request"}
    return {"needs_action": True, "severity": "action", "reason": "latest_client_message"}


def _looks_like_bot_promise(text: str) -> bool:
    return bool(BOT_PROMISE_RE.search(" ".join(str(text or "").split())))


def _looks_like_bot_final_answer(text: str) -> bool:
    normalized = " ".join(str(text or "").split())
    return bool(normalized and not _looks_like_bot_promise(normalized))


def _followup_key(*, account_id: int, chat_id: str, message_id: str) -> str:
    return f"{account_id}:{chat_id}:{message_id}"


def _message_id(message: dict[str, Any]) -> str:
    return str(message.get("id") or message.get("message_id") or "").strip()


def _latest_client_message_before(
    messages: list[dict[str, Any]], *, account_id: int, created_before: int
) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for message in messages:
        created = int(message.get("created") or 0)
        if created > created_before:
            continue
        if _is_relevant_incoming(message, account_id=account_id):
            latest = message
    return latest


def _active_pending_followups(state: dict[str, Any]) -> dict[str, Any]:
    pending = state.setdefault("pending_followups", {})
    if not isinstance(pending, dict):
        pending = {}
        state["pending_followups"] = pending
    return pending


def sync_pending_followups(
    *,
    account_id: int,
    chat: dict[str, Any],
    messages: list[dict[str, Any]],
    state: dict[str, Any],
    now: int,
    reminder_seconds: int = 3600,
    escalation_seconds: int = 10800,
) -> None:
    ordered = sorted(messages, key=lambda item: int(item.get("created") or 0))
    pending = _active_pending_followups(state)
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return
    listing_title, listing_city = _listing(chat)
    active_keys = {
        key
        for key, row in pending.items()
        if isinstance(row, dict)
        and int(row.get("account_id") or 0) == int(account_id)
        and str(row.get("chat_id") or "") == chat_id
        and not row.get("business_resolved")
    }

    for message in ordered:
        created = int(message.get("created") or 0)
        text = _message_text(message)
        if _is_relevant_incoming(message, account_id=account_id):
            for key in active_keys:
                row = pending.get(key)
                if not isinstance(row, dict) or row.get("business_resolved"):
                    continue
                promised_at = int(row.get("promised_at") or 0)
                if created >= promised_at:
                    row["client_ack_after_promise"] = True
                    row["last_client_message"] = text
                    row["last_client_message_at"] = created
                    row["last_client_photo_urls"] = _photo_urls(message)
                    row["last_client_media_urls"] = _photo_urls(message)
            continue

        if not _is_outgoing_reply(message, account_id=account_id):
            continue

        message_id = _message_id(message) or str(created)
        if _looks_like_bot_promise(text):
            for key in list(active_keys):
                row = pending.get(key)
                if not isinstance(row, dict) or row.get("business_resolved"):
                    continue
                if created >= int(row.get("promised_at") or 0):
                    row.update(
                        {
                            "business_status": "superseded_by_new_promise",
                            "business_resolved": True,
                            "closed_at": created,
                            "closed_at_iso": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else "",
                            "final_answer": text,
                            "overdue": False,
                        }
                    )
                    active_keys.discard(key)
            client_message = _latest_client_message_before(ordered, account_id=account_id, created_before=created)
            client_text = _message_text(client_message) if client_message else ""
            client_photo_urls = _photo_urls(client_message) if client_message else []
            classification = _classify_client_message(client_text)
            key = _followup_key(account_id=account_id, chat_id=chat_id, message_id=message_id)
            row = pending.get(key) if isinstance(pending.get(key), dict) else {}
            row.update(
                {
                    "account_id": account_id,
                    "chat_id": chat_id,
                    "client_name": client_name_from_chat(chat, account_id=account_id),
                    "message_id": message_id,
                    "bot_promise": text,
                    "promised_at": created,
                    "promised_at_iso": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else "",
                    "deadline_at": created + reminder_seconds,
                    "escalation_at": created + escalation_seconds,
                    "business_status": "awaiting_olga",
                    "business_resolved": False,
                    "client_replied": True,
                    "client_replied_at": created,
                    "client_ack_after_promise": bool(client_message and int(client_message.get("created") or 0) >= created),
                    "last_client_message": client_text,
                    "last_client_message_at": int(client_message.get("created") or 0) if client_message else 0,
                    "last_client_photo_urls": client_photo_urls,
                    "last_client_media_urls": client_photo_urls,
                    "listing_title": listing_title,
                    "listing_city": listing_city,
                    "severity": "critical" if classification["severity"] == "critical" or CLIENT_CRITICAL_RE.search(text) else "action",
                    "reason": "bot_promised_followup",
                }
            )
            pending[key] = row
            active_keys.add(key)
            continue

        if _looks_like_bot_final_answer(text):
            for key in list(active_keys):
                row = pending.get(key)
                if not isinstance(row, dict) or row.get("business_resolved"):
                    continue
                if created >= int(row.get("promised_at") or 0):
                    row.update(
                        {
                            "business_status": "business_resolved",
                            "business_resolved": True,
                            "closed_at": created,
                            "closed_at_iso": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else "",
                            "final_answer": text,
                            "overdue": False,
                        }
                    )
                    active_keys.discard(key)

    for key in list(active_keys):
        row = pending.get(key)
        if not isinstance(row, dict) or row.get("business_resolved"):
            continue
        promised_at = int(row.get("promised_at") or 0)
        deadline_at = int(row.get("deadline_at") or 0)
        escalation_at = int(row.get("escalation_at") or 0)
        row["age_seconds"] = max(0, now - promised_at)
        row["age_minutes"] = round(row["age_seconds"] / 60, 1)
        row["overdue"] = bool(deadline_at and now >= deadline_at)
        row["urgent"] = bool(row.get("urgent")) or bool(escalation_at and now >= escalation_at)
        row["business_status"] = "urgent" if row.get("urgent") else ("overdue" if row["overdue"] else "awaiting_olga")


def pending_followup_rows(state: dict[str, Any], *, now: int, include_resolved: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, row in _active_pending_followups(state).items():
        if not isinstance(row, dict):
            continue
        if row.get("business_resolved") and not include_resolved:
            continue
        item = dict(row)
        item["key"] = key
        promised_at = int(item.get("promised_at") or 0)
        if promised_at:
            item["age_seconds"] = max(0, now - promised_at)
            item["age_minutes"] = round(item["age_seconds"] / 60, 1)
        deadline_at = int(item.get("deadline_at") or 0)
        escalation_at = int(item.get("escalation_at") or 0)
        item["overdue"] = bool(deadline_at and now >= deadline_at and not item.get("business_resolved"))
        item["urgent"] = bool(item.get("urgent")) or bool(escalation_at and now >= escalation_at and not item.get("business_resolved"))
        if not item.get("business_resolved"):
            item["business_status"] = "urgent" if item.get("urgent") else ("overdue" if item["overdue"] else "awaiting_olga")
        rows.append(item)
    rows.sort(key=lambda item: (0 if item.get("business_status") == "overdue" else 1, -int(item.get("urgent") is True), int(item.get("promised_at") or 0)))
    return rows


def _state_key(item: UnansweredChat) -> str:
    return f"{item.account_id}:{item.chat_id}:{item.message_id}"


def _report_item(item: UnansweredChat, state: dict[str, Any]) -> dict[str, Any]:
    key = _state_key(item)
    data = item.to_dict()
    handled = state.get("handled") if isinstance(state.get("handled"), dict) else {}
    failed = state.get("failed") if isinstance(state.get("failed"), dict) else {}
    handled_row = handled.get(key) if isinstance(handled, dict) else None
    failed_row = failed.get(key) if isinstance(failed, dict) else None
    if isinstance(handled_row, dict):
        result = handled_row.get("result") if isinstance(handled_row.get("result"), dict) else {}
        stale_ack = item.needs_action and result.get("ignored") and result.get("reason") == "client_ack_after_pending_reply"
        data["autoreply_state"] = "pending" if stale_ack else "handled"
        data["handled_at"] = handled_row.get("handled_at")
        data["handled_action"] = result.get("action")
        data["ignored_reason"] = result.get("reason") if result.get("ignored") else ""
        data["needs_action"] = item.needs_action if stale_ack else False
    elif isinstance(failed_row, dict):
        data["autoreply_state"] = "failed"
        data["last_attempt_at"] = failed_row.get("last_attempt_at")
        data["needs_action"] = True
    else:
        data["autoreply_state"] = "pending"
        data["needs_action"] = item.needs_action
    return data


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"alerts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"alerts": {}}
    return data if isinstance(data, dict) else {"alerts": {}}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_log(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": int(time.time()), **row}, ensure_ascii=False, default=str) + "\n")


def _safe_avito_error(exc: AvitoApiError) -> dict[str, Any]:
    payload = exc.payload if isinstance(exc.payload, dict) else {"payload": exc.payload}
    return {"ok": False, "error": repr(exc), "status_code": exc.status_code, "payload": payload}


def _format_alert(items: list[UnansweredChat], *, max_items: int) -> str:
    critical = sum(1 for item in items if item.severity == "critical")
    action = sum(1 for item in items if item.needs_action)
    lines = [f"Avito: есть неотвеченные входящие | требуют действия: {action}, критичных: {critical}"]
    for index, item in enumerate(items[:max_items], start=1):
        age_min = int(item.age_seconds / 60)
        label = item.client_name or item.chat_id
        text = item.text.replace("\n", " ").strip()
        if len(text) > 180:
            text = text[:177] + "..."
        marker = "КРИТИЧНО" if item.severity == "critical" else ("нужно действие" if item.needs_action else "похоже на финальное спасибо/ок")
        lines.append(f"{index}. {label} — {age_min} мин без ответа ({marker})")
        if item.listing_city or item.listing_title:
            lines.append(f"   Объявление: {item.listing_city} {item.listing_title}".strip())
        lines.append(f"   Сообщение: {text}")
        lines.append(f"   chat_id: {item.chat_id}")
    if len(items) > max_items:
        lines.append(f"…и ещё {len(items) - max_items}")
    return "\n".join(lines)


def _format_followup_alert(rows: list[dict[str, Any]], *, max_items: int) -> str:
    overdue = sum(1 for row in rows if row.get("overdue") or row.get("business_status") == "overdue")
    critical = sum(1 for row in rows if row.get("severity") == "critical")
    lines = [f"Avito: зависшие обещания бота | просрочено: {overdue}, критичных: {critical}"]
    for index, row in enumerate(rows[:max_items], start=1):
        age_min = int(row.get("age_seconds") or 0) // 60
        text = " ".join(str(row.get("bot_promise") or "").split())
        if len(text) > 150:
            text = text[:147] + "..."
        lines.append(f"{index}. {row.get('client_name') or row.get('chat_id')} — {age_min} мин, {row.get('business_status')}")
        listing = " | ".join(str(row.get(key) or "").strip() for key in ("listing_city", "listing_title") if row.get(key))
        if listing:
            lines.append(f"   Объявление: {listing}")
        lines.append(f"   Обещал бот: {text}")
        last_client = " ".join(str(row.get("last_client_message") or "").split())
        if last_client:
            lines.append(f"   Последнее от клиента: {last_client[:150]}")
        lines.append("   Нужно сделать: дать клиенту финальный ответ или закрыть обещание как неактуальное.")
        lines.append(f"   chat_id: {row.get('chat_id')}")
    if len(rows) > max_items:
        lines.append(f"…и ещё {len(rows) - max_items}")
    return "\n".join(lines)


def _inbound_from_unanswered(item: UnansweredChat) -> InboundMessage:
    raw_message = item.raw_message
    content = _content(raw_message)
    message_type = str(raw_message.get("type") or item.message_type or "")
    has_media = message_type in {"image", "photo", "video", "file"} or any(content.get(key) for key in ("image", "photo", "video", "file"))
    return InboundMessage(
        channel=Channel.AVITO,
        client_id=str(raw_message.get("author_id") or ""),
        chat_id=item.chat_id,
        message_id=item.message_id,
        text=str(content.get("text") or ""),
        created_at=int(raw_message.get("created") or item.created or 0),
        has_photo=has_media,
        listing=AvitoListingContext(title=item.listing_title, city=item.listing_city),
        metadata={
            "account_id": item.account_id,
            "author_id": str(raw_message.get("author_id") or ""),
            "author_role": "client",
            "client_name": item.client_name,
            "direction": str(raw_message.get("direction") or "in"),
            "is_own_account": False,
            "message_type": message_type,
            "photo_urls": _photo_urls(raw_message),
            "photo_ids": _media_ids(raw_message) if message_type in {"image", "photo"} else [],
            "media_urls": _photo_urls(raw_message),
            "media_ids": _media_ids(raw_message),
            "media_types": _media_types(raw_message),
            "source": "avito_unanswered_monitor",
            "delayed_autoreply": True,
            "raw": {"chat": item.raw_chat, "message": raw_message},
        },
    )


def _photo_urls(message: dict[str, Any]) -> list[str]:
    content = _content(message)
    result: list[str] = []
    for key in ("image", "photo", "video", "file"):
        value = content.get(key) or message.get(key)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            result.extend(_urls_from_any(candidate))
    return list(dict.fromkeys(result))


def _urls_from_any(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, dict):
        urls: list[str] = []
        for nested in value.values():
            urls.extend(_urls_from_any(nested))
        return urls
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(_urls_from_any(item))
        return urls
    return []


def _followup_media_urls(row: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("last_client_photo_urls", "last_client_media_urls", "photo_urls", "media_urls"):
        value = row.get(key) if isinstance(row, dict) else None
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            url = str(candidate or "").strip()
            if url.startswith(("http://", "https://")) and url not in result:
                result.append(url)
    return result


def _media_ids(message: dict[str, Any]) -> list[str]:
    content = _content(message)
    result: list[str] = []
    for key in ("image", "photo", "video", "file", "voice"):
        value = content.get(key) or message.get(key)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            if isinstance(candidate, dict):
                item = candidate.get("id") or candidate.get("image_id") or candidate.get("video_id") or candidate.get("file_id") or candidate.get("voice_id")
                if item:
                    result.append(str(item))
            elif isinstance(candidate, str) and not candidate.startswith(("http://", "https://")):
                result.append(candidate)
    return list(dict.fromkeys(result))


def _media_types(message: dict[str, Any]) -> list[str]:
    content = _content(message)
    result: list[str] = []
    message_type = str(message.get("type") or "")
    if message_type in {"image", "photo", "video", "file", "voice"}:
        result.append("photo" if message_type == "image" else message_type)
    for key in ("image", "photo", "video", "file", "voice"):
        if content.get(key) or message.get(key):
            result.append("photo" if key == "image" else key)
    return list(dict.fromkeys(result))


async def autoreply_once(
    *,
    settings: IntegrationSettings,
    items: list[UnansweredChat],
    state: dict[str, Any],
    state_path: Path,
    avito_reader: AvitoReadGateway | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    if not state.get("activated_at"):
        state["activated_at"] = now
        state.setdefault("handled", {})
        state.setdefault("failed", {})
        _save_state(state_path, state)
        return {"attempted": 0, "sent": 0, "handoff": 0, "ignored": 0, "failed": 0, "activated_at": now}
    activated_at = int(state.get("activated_at") or now)
    handled = state.setdefault("handled", {})
    failed = state.setdefault("failed", {})
    notifier = handoff_notifier_from_settings(settings)
    toolbox = AutomationToolbox(
        booking_from_settings(settings),
        role_profile=role_profile(CodexRole.AVITO_CLIENT),
        operations_notifier=notifier,
    )
    planner = (
        CodexToolLoopPlanner(
            CodexPlannerRunner(timeout_seconds=settings.avito_codex_timeout_seconds),
            max_steps=settings.avito_codex_max_steps,
            trace_logger=JsonlAgentTraceLogger(),
        )
        if settings.avito_codex_enabled
        else None
    )
    sender = avito_sender_from_settings(settings)
    photo_resolver = AvitoApiPhotoResolver(settings) if settings.avito_ready else None
    history_store = LeadStore(settings.telegram_admin_history_db_path)
    expert_rag = ExpertRagStore(settings.rag_expert_db_path) if settings.rag_retrieval_enabled else None
    stats = {"attempted": 0, "sent": 0, "handoff": 0, "ignored": 0, "failed": 0, "activated_at": activated_at}
    for item in items:
        key = _state_key(item)
        if item.created < activated_at or key in handled:
            continue
        stats["attempted"] += 1
        try:
            result = await process_avito_message(
                message=_inbound_from_unanswered(item),
                settings=settings,
                toolbox=toolbox,
                planner=planner,
                sender=sender,
                handoff_notifier=notifier,
                photo_resolver=photo_resolver,
                history_store=history_store,
                expert_rag=expert_rag,
                avito_reader=avito_reader,
                force_unanswered_autoreply=True,
            )
        except Exception as exc:
            failed[key] = {"last_attempt_at": now, "error": repr(exc)}
            stats["failed"] += 1
            continue
        handled[key] = {"handled_at": now, "chat_id": item.chat_id, "message_id": item.message_id, "result": _compact_result(result)}
        if result.get("ignored"):
            stats["ignored"] += 1
        elif result.get("handoff"):
            stats["handoff"] += 1
        elif (result.get("send") or {}).get("sent") or (result.get("send") or {}).get("reason") == "preview_only":
            stats["sent"] += 1
    _save_state(state_path, state)
    return stats


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "ignored": result.get("ignored"),
        "reason": result.get("reason"),
        "action": result.get("action"),
        "handoff": result.get("handoff"),
        "send": result.get("send"),
        "mark_read": result.get("mark_read"),
    }


async def audit_once(
    *,
    settings: IntegrationSettings,
    chat_limit: int,
    messages_per_chat: int,
    min_age_seconds: int,
    lookback_seconds: int,
    state: dict[str, Any] | None = None,
    reader: AvitoReadGateway | None = None,
) -> list[UnansweredChat]:
    reader = reader or AvitoReadClient(settings)
    account_ids = list(dict.fromkeys([settings.avito_account_id, *settings.avito_account_ids]))
    account_ids = [account_id for account_id in account_ids if account_id]
    now = int(time.time())
    result: list[UnansweredChat] = []

    for account_id in account_ids:
        chats: list[dict[str, Any]] = []
        offset = 0
        remaining = max(0, chat_limit)
        while remaining > 0:
            page_limit = min(remaining, 100)
            chats_payload = await reader.list_chats(account_id, limit=page_limit, offset=offset)
            page = _items(chats_payload, "chats", "items")
            chats.extend(page)
            if len(page) < page_limit:
                break
            offset += page_limit
            remaining -= page_limit
        update_client_name_cache({str(chat.get("id") or ""): client_name_from_chat(chat, account_id=account_id) for chat in chats})
        for chat in chats:
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                continue
            messages_payload = await reader.get_chat_messages(account_id, chat_id, limit=messages_per_chat)
            messages = _items(messages_payload, "messages", "items")
            if state is not None:
                sync_pending_followups(account_id=account_id, chat=chat, messages=messages, state=state, now=now)
            item = _find_unanswered(
                account_id=account_id,
                chat=chat,
                messages=messages,
                now=now,
                min_age_seconds=min_age_seconds,
                lookback_seconds=lookback_seconds,
            )
            if item:
                result.append(item)

    result.sort(key=lambda item: item.created)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Avito chats where the latest client message has no outgoing reply.")
    parser.add_argument("--once", action="store_true", help="Run one audit and exit.")
    parser.add_argument("--notify", action="store_true", help="Send Telegram notification for newly detected unanswered chats.")
    parser.add_argument("--autoreply", action="store_true", help="Run delayed Avito autoreply for new unanswered chats.")
    parser.add_argument("--chat-limit", type=int, default=None)
    parser.add_argument("--messages-per-chat", type=int, default=None)
    parser.add_argument("--min-age-seconds", type=int, default=None)
    parser.add_argument("--lookback-seconds", type=int, default=None)
    parser.add_argument("--interval-seconds", type=int, default=None)
    parser.add_argument("--repeat-alert-seconds", type=int, default=None)
    parser.add_argument("--max-alert-items", type=int, default=None)
    parser.add_argument("--followup-token", default="", help="Apply an admin action to a pending followup by token, or pass the full state key.")
    parser.add_argument("--followup-action", choices=("done", "stale", "urgent", "later"), default="done")
    parser.add_argument("--followup-actor", default="cli")
    parser.add_argument("--state-path", type=Path, default=Path(os.getenv("AVITO_UNANSWERED_STATE_PATH", str(DEFAULT_STATE_PATH))))
    parser.add_argument("--report-path", type=Path, default=Path(os.getenv("AVITO_UNANSWERED_REPORT_PATH", str(DEFAULT_REPORT_PATH))))
    parser.add_argument("--log-path", type=Path, default=Path(os.getenv("AVITO_UNANSWERED_LOG_PATH", str(DEFAULT_LOG_PATH))))
    args = parser.parse_args()

    settings = IntegrationSettings.from_env()
    chat_limit = args.chat_limit if args.chat_limit is not None else _env_int("AVITO_UNANSWERED_CHAT_LIMIT", 50)
    messages_per_chat = args.messages_per_chat if args.messages_per_chat is not None else _env_int("AVITO_UNANSWERED_MESSAGES_PER_CHAT", 50)
    min_age_seconds = args.min_age_seconds if args.min_age_seconds is not None else settings.avito_unanswered_min_age_seconds
    lookback_seconds = args.lookback_seconds if args.lookback_seconds is not None else settings.avito_unanswered_lookback_seconds
    interval_seconds = args.interval_seconds if args.interval_seconds is not None else settings.avito_unanswered_interval_seconds
    repeat_alert_seconds = args.repeat_alert_seconds if args.repeat_alert_seconds is not None else _env_int("AVITO_UNANSWERED_REPEAT_ALERT_SECONDS", 21600)
    max_alert_items = args.max_alert_items if args.max_alert_items is not None else _env_int("AVITO_UNANSWERED_MAX_ALERT_ITEMS", 10)
    notify_enabled = args.notify or _env_bool("AVITO_UNANSWERED_NOTIFY_ENABLED")
    autoreply_enabled = args.autoreply or _env_bool("AVITO_UNANSWERED_AUTOREPLY_ENABLED")
    notifier = handoff_notifier_from_settings(settings) if notify_enabled else None
    if args.followup_token:
        token = pending_followup_token(args.followup_token) if ":" in args.followup_token else args.followup_token
        result = apply_pending_followup_action(
            state_path=args.state_path,
            token=token,
            action=args.followup_action,
            actor=args.followup_actor,
        )
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        return

    while True:
        try:
            state = _load_state(args.state_path)
            items = await audit_once(
                settings=settings,
                chat_limit=chat_limit,
                messages_per_chat=messages_per_chat,
                min_age_seconds=min_age_seconds,
                lookback_seconds=lookback_seconds,
                state=state,
            )
            now = int(time.time())
            followups = pending_followup_rows(state, now=now)
            overdue_followups = [row for row in followups if row.get("overdue")]
            critical_followups = [row for row in followups if row.get("severity") == "critical"]
            report = {
                "ok": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "count": len(items),
                "actionable_count": 0,
                "critical_unanswered_count": 0,
                "final_ack_count": 0,
                "pending_followup_count": len(followups),
                "overdue_followup_count": len(overdue_followups),
                "critical_followup_count": len(critical_followups),
                "items": [],
                "pending_followups": followups,
            }
            report["items"] = [_report_item(item, state) for item in items]
            report["actionable_count"] = sum(1 for item in report["items"] if item.get("needs_action"))
            report["critical_unanswered_count"] = sum(
                1 for item in report["items"] if item.get("needs_action") and item.get("severity") == "critical"
            )
            report["final_ack_count"] = sum(1 for item in report["items"] if item.get("reason") == "final_ack")
            args.report_path.parent.mkdir(parents=True, exist_ok=True)
            args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            notified = 0
            followup_notified = 0
            autoreply = {}
            actionable_items = [item for item in items if item.needs_action]
            if notifier and actionable_items:
                alerts = state.setdefault("alerts", {})
                fresh: list[UnansweredChat] = []
                for item in actionable_items:
                    key = _state_key(item)
                    previous = int((alerts.get(key) or {}).get("last_alerted_at") or 0) if isinstance(alerts.get(key), dict) else 0
                    if now - previous >= repeat_alert_seconds:
                        fresh.append(item)
                        alerts[key] = {"last_alerted_at": now, "chat_id": item.chat_id, "message_id": item.message_id}
                if fresh:
                    await notifier.notify_text(_format_alert(fresh, max_items=max_alert_items))
                    notified = len(fresh)

            followup_alert_rows = [row for row in followups if row.get("overdue") or row.get("severity") == "critical"]
            followup_alert_rows = [row for row in followup_alert_rows if int(row.get("snoozed_until") or 0) <= now]
            if notifier and followup_alert_rows:
                alerts = state.setdefault("followup_alerts", {})
                fresh_rows: list[dict[str, Any]] = []
                for row in followup_alert_rows:
                    key = str(row.get("key") or "")
                    if not key:
                        continue
                    previous = int((alerts.get(key) or {}).get("last_alerted_at") or 0) if isinstance(alerts.get(key), dict) else 0
                    if now - previous >= repeat_alert_seconds:
                        fresh_rows.append(row)
                        alerts[key] = {"last_alerted_at": now, "chat_id": row.get("chat_id"), "message_id": row.get("message_id")}
                if fresh_rows:
                    for row in fresh_rows[:max_alert_items]:
                        key = str(row.get("key") or "")
                        await notifier.notify_text(pending_followup_card_text(row), reply_markup=pending_followup_keyboard(key))
                        for index, url in enumerate(_followup_media_urls(row)[:5], start=1):
                            await notifier.notify_photo_url(
                                url,
                                caption=f"Фото клиента из Avito для зависшего обещания ({index})",
                            )
                    followup_notified = min(len(fresh_rows), max_alert_items)

            if autoreply_enabled and settings.avito_unanswered_autoreply_enabled:
                avito_reader = AvitoReadClient(settings) if settings.avito_ready and settings.avito_send_enabled else None
                autoreply = await autoreply_once(
                    settings=settings,
                    items=actionable_items,
                    state=state,
                    state_path=args.state_path,
                    avito_reader=avito_reader,
                )
            else:
                _save_state(args.state_path, state)

            handoff_sla = {}
            if settings.handoff_notify_enabled:
                sla_notifier = notifier or handoff_notifier_from_settings(settings)
                handoff_sla = await process_handoff_sla(sla_notifier)

            summary = {
                "ok": True,
                "count": len(items),
                "actionable_count": report["actionable_count"],
                "critical_unanswered_count": report["critical_unanswered_count"],
                "pending_followup_count": len(followups),
                "overdue_followup_count": len(overdue_followups),
                "notified": notified,
                "followup_notified": followup_notified,
                "autoreply": autoreply,
                "handoff_sla": handoff_sla,
                "report_path": str(args.report_path),
            }
            _append_log(args.log_path, {"event": "summary", **summary})
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        except AvitoApiError as exc:
            row = _safe_avito_error(exc)
            _append_log(args.log_path, {"event": "avito_api_error", **row})
            print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
        except Exception as exc:
            row = {"ok": False, "error": repr(exc)}
            _append_log(args.log_path, {"event": "error", **row})
            print(json.dumps(row, ensure_ascii=False), flush=True)

        if args.once:
            return
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
