from __future__ import annotations

import argparse
import asyncio
import json
import os
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
from src.freelance_leads_bot.integrations.avito_read import AvitoReadClient
from src.freelance_leads_bot.integrations.avito_sender import avito_sender_from_settings
from src.freelance_leads_bot.integrations.avito_webhook import process_avito_message
from src.freelance_leads_bot.integrations.codex_planner import CodexPlannerRunner
from src.freelance_leads_bot.integrations.config import IntegrationSettings
from src.freelance_leads_bot.integrations.expert_rag import ExpertRagStore
from src.freelance_leads_bot.integrations.handoff_notify import handoff_notifier_from_settings
from src.freelance_leads_bot.integrations.models import AvitoListingContext, Channel, InboundMessage
from src.freelance_leads_bot.integrations.roles import CodexRole, role_profile
from src.freelance_leads_bot.integrations.runtime import booking_from_settings
from src.freelance_leads_bot.storage import LeadStore


DEFAULT_STATE_PATH = Path("data/avito_unanswered_monitor_state.json")
DEFAULT_REPORT_PATH = Path("data/avito_unanswered_report.json")
DEFAULT_LOG_PATH = Path("data/avito_unanswered_monitor.log")
DELETED_MESSAGE_TEXTS = {"сообщение удалено", "message deleted"}


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
    )


def _state_key(item: UnansweredChat) -> str:
    return f"{item.account_id}:{item.chat_id}:{item.message_id}"


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
    lines = ["Avito: есть неотвеченные входящие"]
    for index, item in enumerate(items[:max_items], start=1):
        age_min = int(item.age_seconds / 60)
        label = item.client_name or item.chat_id
        text = item.text.replace("\n", " ").strip()
        if len(text) > 180:
            text = text[:177] + "..."
        lines.append(f"{index}. {label} — {age_min} мин без ответа")
        if item.listing_city or item.listing_title:
            lines.append(f"   Объявление: {item.listing_city} {item.listing_title}".strip())
        lines.append(f"   Сообщение: {text}")
        lines.append(f"   chat_id: {item.chat_id}")
    if len(items) > max_items:
        lines.append(f"…и ещё {len(items) - max_items}")
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
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                result.append(candidate)
            elif isinstance(candidate, dict):
                for nested in candidate.values():
                    if isinstance(nested, str) and nested.startswith(("http://", "https://")):
                        result.append(nested)
    return list(dict.fromkeys(result))


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
    }


async def audit_once(
    *,
    settings: IntegrationSettings,
    chat_limit: int,
    messages_per_chat: int,
    min_age_seconds: int,
    lookback_seconds: int,
) -> list[UnansweredChat]:
    reader = AvitoReadClient(settings)
    account_ids = list(dict.fromkeys([settings.avito_account_id, *settings.avito_account_ids]))
    account_ids = [account_id for account_id in account_ids if account_id]
    now = int(time.time())
    result: list[UnansweredChat] = []

    for account_id in account_ids:
        chats_payload = await reader.list_chats(account_id, limit=chat_limit)
        chats = _items(chats_payload, "chats", "items")
        update_client_name_cache({str(chat.get("id") or ""): client_name_from_chat(chat, account_id=account_id) for chat in chats})
        for chat in chats:
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                continue
            messages_payload = await reader.get_chat_messages(account_id, chat_id, limit=messages_per_chat)
            messages = _items(messages_payload, "messages", "items")
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
    parser.add_argument("--chat-limit", type=int, default=_env_int("AVITO_UNANSWERED_CHAT_LIMIT", 50))
    parser.add_argument("--messages-per-chat", type=int, default=_env_int("AVITO_UNANSWERED_MESSAGES_PER_CHAT", 50))
    parser.add_argument("--min-age-seconds", type=int, default=_env_int("AVITO_UNANSWERED_MIN_AGE_SECONDS", 900))
    parser.add_argument("--lookback-seconds", type=int, default=_env_int("AVITO_UNANSWERED_LOOKBACK_SECONDS", 172800))
    parser.add_argument("--interval-seconds", type=int, default=_env_int("AVITO_UNANSWERED_INTERVAL_SECONDS", 300))
    parser.add_argument("--repeat-alert-seconds", type=int, default=_env_int("AVITO_UNANSWERED_REPEAT_ALERT_SECONDS", 21600))
    parser.add_argument("--max-alert-items", type=int, default=_env_int("AVITO_UNANSWERED_MAX_ALERT_ITEMS", 10))
    parser.add_argument("--state-path", type=Path, default=Path(os.getenv("AVITO_UNANSWERED_STATE_PATH", str(DEFAULT_STATE_PATH))))
    parser.add_argument("--report-path", type=Path, default=Path(os.getenv("AVITO_UNANSWERED_REPORT_PATH", str(DEFAULT_REPORT_PATH))))
    parser.add_argument("--log-path", type=Path, default=Path(os.getenv("AVITO_UNANSWERED_LOG_PATH", str(DEFAULT_LOG_PATH))))
    args = parser.parse_args()

    settings = IntegrationSettings.from_env()
    notify_enabled = args.notify or _env_bool("AVITO_UNANSWERED_NOTIFY_ENABLED")
    autoreply_enabled = args.autoreply or _env_bool("AVITO_UNANSWERED_AUTOREPLY_ENABLED")
    notifier = handoff_notifier_from_settings(settings) if notify_enabled else None

    while True:
        try:
            items = await audit_once(
                settings=settings,
                chat_limit=args.chat_limit,
                messages_per_chat=args.messages_per_chat,
                min_age_seconds=args.min_age_seconds,
                lookback_seconds=args.lookback_seconds,
            )
            now = int(time.time())
            report = {
                "ok": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "count": len(items),
                "items": [item.to_dict() for item in items],
            }
            args.report_path.parent.mkdir(parents=True, exist_ok=True)
            args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            notified = 0
            autoreply = {}
            if notifier and items:
                state = _load_state(args.state_path)
                alerts = state.setdefault("alerts", {})
                fresh: list[UnansweredChat] = []
                for item in items:
                    key = _state_key(item)
                    previous = int((alerts.get(key) or {}).get("last_alerted_at") or 0) if isinstance(alerts.get(key), dict) else 0
                    if now - previous >= args.repeat_alert_seconds:
                        fresh.append(item)
                        alerts[key] = {"last_alerted_at": now, "chat_id": item.chat_id, "message_id": item.message_id}
                if fresh:
                    await notifier.notify_text(_format_alert(fresh, max_items=args.max_alert_items))
                    notified = len(fresh)
                _save_state(args.state_path, state)

            if autoreply_enabled and settings.avito_unanswered_autoreply_enabled:
                state = _load_state(args.state_path)
                autoreply = await autoreply_once(settings=settings, items=items, state=state, state_path=args.state_path)

            summary = {"ok": True, "count": len(items), "notified": notified, "autoreply": autoreply, "report_path": str(args.report_path)}
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
        await asyncio.sleep(args.interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
