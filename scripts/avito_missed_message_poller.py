from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from pyavitoapi.transport.errors import AvitoApiError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.freelance_leads_bot.integrations.agent_tools import AutomationToolbox
from src.freelance_leads_bot.integrations.agent_trace import JsonlAgentTraceLogger
from src.freelance_leads_bot.integrations.avito_dedup import PersistentProcessedEventStore
from src.freelance_leads_bot.integrations.avito_read import AvitoReadClient
from src.freelance_leads_bot.integrations.avito_consultant import CodexToolLoopPlanner
from src.freelance_leads_bot.integrations.avito_identity import client_name_from_chat, update_client_name_cache
from src.freelance_leads_bot.integrations.avito_media import AvitoApiPhotoResolver
from src.freelance_leads_bot.integrations.avito_sender import avito_sender_from_settings
from src.freelance_leads_bot.integrations.avito_turn_buffer import enqueue_avito_turn_message
from src.freelance_leads_bot.integrations.avito_voice import AvitoApiVoiceResolver
from src.freelance_leads_bot.integrations.avito_webhook import _is_own_message, process_avito_message
from src.freelance_leads_bot.integrations.codex_planner import CodexPlannerRunner
from src.freelance_leads_bot.integrations.config import IntegrationSettings
from src.freelance_leads_bot.integrations.expert_rag import ExpertRagStore
from src.freelance_leads_bot.integrations.handoff_notify import handoff_notifier_from_settings
from src.freelance_leads_bot.integrations.models import AvitoListingContext, Channel, InboundMessage
from src.freelance_leads_bot.integrations.runtime import booking_from_settings
from src.freelance_leads_bot.integrations.roles import CodexRole, role_profile
from src.freelance_leads_bot.storage import LeadStore


LOG_PATH = Path("data/avito_poller.log")
DELETED_MESSAGE_TEXTS = {"сообщение удалено", "message deleted"}
DEFAULT_ACCESS_ERROR_BACKOFF_SECONDS = 1800
DEFAULT_POLLER_CHAT_LIMIT = 150
DEFAULT_POLLER_MESSAGES_PER_CHAT = 50
DEFAULT_POLLER_PAGE_SIZE = 100


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _log(row: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": int(time.time()), **row}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _is_messenger_access_error(exc: AvitoApiError) -> bool:
    if exc.status_code != 402:
        return False
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    message = str(payload.get("message") or exc).casefold()
    return "api мессенджера" in message or "доступ к чатам" in message or "подписк" in message


def _safe_error_payload(exc: AvitoApiError) -> dict[str, Any]:
    return exc.payload if isinstance(exc.payload, dict) else {"payload": exc.payload}


def _items(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _listing_from_chat(chat: dict[str, Any]) -> AvitoListingContext | None:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    value = context.get("value") if isinstance(context.get("value"), dict) else {}
    if not value:
        return None
    location = value.get("location") if isinstance(value.get("location"), dict) else {}
    return AvitoListingContext(
        item_id=_int_or_none(value.get("id")),
        title=str(value.get("title") or ""),
        url=str(value.get("url") or ""),
        price_string=str(value.get("price_string") or ""),
        city=str(location.get("title") or value.get("city") or ""),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _photo_urls(message: dict[str, Any]) -> list[str]:
    content = message.get("content") if isinstance(message.get("content"), dict) else {}
    result: list[str] = []
    image = content.get("image") or content.get("photo")
    candidates = image if isinstance(image, list) else [image]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            result.append(candidate)
        if isinstance(candidate, dict):
            for value in candidate.values():
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    result.append(value)
    return list(dict.fromkeys(result))


def _media_ids(message: dict[str, Any]) -> list[str]:
    content = message.get("content") if isinstance(message.get("content"), dict) else {}
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
    content = message.get("content") if isinstance(message.get("content"), dict) else {}
    result: list[str] = []
    message_type = str(message.get("type") or "")
    if message_type in {"image", "photo", "video", "file", "voice"}:
        result.append("photo" if message_type == "image" else message_type)
    for key in ("image", "photo", "video", "file", "voice"):
        if content.get(key) or message.get(key):
            result.append("photo" if key == "image" else key)
    return list(dict.fromkeys(result))


def _voice_id(message: dict[str, Any]) -> str:
    content = message.get("content") if isinstance(message.get("content"), dict) else {}
    voice = content.get("voice") or message.get("voice")
    if isinstance(voice, dict):
        return str(voice.get("voice_id") or voice.get("id") or "")
    if isinstance(voice, str):
        return voice
    return ""


def _inbound_from_message(
    *,
    account_id: int,
    chat: dict[str, Any],
    raw_message: dict[str, Any],
) -> InboundMessage:
    content = raw_message.get("content") if isinstance(raw_message.get("content"), dict) else {}
    text = content.get("text")
    message_type = str(raw_message.get("type") or "")
    author_id = str(raw_message.get("author_id") or "")
    has_photo = message_type in {"image", "photo", "video", "file"} or bool(content.get("image") or content.get("photo") or content.get("video") or content.get("file"))
    is_own_account = author_id == str(account_id)
    client_name = client_name_from_chat(chat, account_id=account_id, author_id=author_id)
    return InboundMessage(
        channel=Channel.AVITO,
        client_id=author_id,
        chat_id=str(chat.get("id") or ""),
        message_id=str(raw_message.get("id") or ""),
        text=str(text or ""),
        created_at=int(raw_message.get("created") or 0),
        has_photo=has_photo,
        listing=_listing_from_chat(chat),
        metadata={
            "account_id": account_id,
            "author_id": author_id,
            "author_role": "own_account" if is_own_account else "client",
            "client_name": client_name,
            "direction": str(raw_message.get("direction") or ""),
            "is_own_account": is_own_account,
            "message_type": message_type,
            "photo_urls": _photo_urls(raw_message),
            "photo_ids": [],
            "media_urls": _photo_urls(raw_message),
            "media_ids": _media_ids(raw_message),
            "media_types": _media_types(raw_message),
            "voice_id": _voice_id(raw_message),
            "raw": {"chat": chat, "message": raw_message},
            "source": "avito_poller",
        },
    )


def _should_process(raw_message: dict[str, Any], *, since_ts: int, settings: IntegrationSettings) -> tuple[bool, str]:
    message_id = str(raw_message.get("id") or "")
    if not message_id:
        return False, "missing_message_id"
    created = int(raw_message.get("created") or 0)
    if created < since_ts:
        return False, "too_old"
    if str(raw_message.get("direction") or "") != "in":
        return False, "not_incoming"
    message_type = str(raw_message.get("type") or "")
    if message_type == "system":
        return False, "system"
    content = raw_message.get("content") if isinstance(raw_message.get("content"), dict) else {}
    text = str(content.get("text") or "").strip().lower()
    if message_type == "deleted" or text in DELETED_MESSAGE_TEXTS:
        return False, "deleted_message"
    if _is_own_message(raw_message.get("author_id"), settings):
        return False, "own_message"
    if not content.get("text") and not content.get("image") and not content.get("photo") and not content.get("voice") and message_type not in {"image", "photo", "video", "file", "voice"} and not content.get("video") and not content.get("file"):
        return False, "empty"
    return True, ""


async def run_once(settings: IntegrationSettings, *, lookback_seconds: int, chat_limit: int, messages_per_chat: int) -> dict[str, Any]:
    account_id = settings.avito_account_id
    reader = AvitoReadClient(settings)
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
    voice_resolver = AvitoApiVoiceResolver(settings) if settings.avito_ready else None
    dedup = PersistentProcessedEventStore()
    history_store = LeadStore(settings.telegram_admin_history_db_path)
    expert_rag = ExpertRagStore(settings.rag_expert_db_path) if settings.rag_retrieval_enabled else None

    since_ts = int(time.time()) - lookback_seconds
    chats = await _list_recent_chats(reader, account_id, chat_limit=chat_limit)
    update_client_name_cache({str(chat.get("id") or ""): client_name_from_chat(chat, account_id=account_id) for chat in chats})
    processed = 0
    skipped = 0
    errors = 0
    skip_reasons: Counter[str] = Counter()

    for chat in chats:
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            skipped += 1
            skip_reasons["missing_chat_id"] += 1
            continue
        messages_payload = await reader.get_chat_messages(account_id, chat_id, limit=messages_per_chat)
        candidates: list[dict[str, Any]] = []
        for raw_message in reversed(_items(messages_payload, "messages", "items")):
            should_process, reason = _should_process(raw_message, since_ts=since_ts, settings=settings)
            if not should_process:
                skipped += 1
                skip_reasons[reason or "filtered"] += 1
                continue
            candidates.append(raw_message)
        if not candidates:
            continue
        for raw_message in candidates:
            key = f"{chat_id}:{raw_message.get('id') or ''}"
            if dedup.contains(key):
                skipped += 1
                skip_reasons["duplicate"] += 1
                continue
            message = _inbound_from_message(account_id=account_id, chat=chat, raw_message=raw_message)
            if str(message.metadata.get("message_type") or "") == "voice" and voice_resolver:
                try:
                    message = await voice_resolver.transcribe(message)
                except Exception as exc:
                    metadata = dict(message.metadata)
                    metadata["voice_transcription_error"] = repr(exc)
                    message = replace(message, metadata=metadata)
                    _log(
                        {
                            "event": "voice_transcription_error",
                            "chat_id": chat_id,
                            "message_id": message.message_id,
                            "error": repr(exc),
                        }
                    )
            if settings.avito_turn_debounce_seconds > 0:
                queued = enqueue_avito_turn_message(
                    message,
                    debounce_seconds=settings.avito_turn_debounce_seconds,
                    max_wait_seconds=settings.avito_turn_max_wait_seconds,
                    max_messages=settings.avito_turn_batch_max_messages,
                )
                dedup.mark_once(key)
                skipped += 1
                skip_reasons["turn_debounce_queued"] += 1
                _log(
                    {
                        "event": "queued",
                        "reason": "turn_debounce",
                        "chat_id": chat_id,
                        "message_id": message.message_id,
                        "message": asdict(message),
                        "queue": queued,
                    }
                )
                continue
            try:
                result = await process_avito_message(
                    message=message,
                    settings=settings,
                    toolbox=toolbox,
                    planner=planner,
                    sender=sender,
                    handoff_notifier=notifier,
                    photo_resolver=photo_resolver,
                    history_store=history_store,
                    expert_rag=expert_rag,
                )
            except Exception as exc:
                errors += 1
                _log({"event": "process_error", "chat_id": chat_id, "message_id": raw_message.get("id"), "error": repr(exc)})
                continue
            if _dedup_allowed(result):
                dedup.mark_once(key)
            if result.get("ignored"):
                skipped += 1
                skip_reasons[str(result.get("reason") or "ignored")] += 1
                _log(
                    {
                        "event": "ignored",
                        "chat_id": chat_id,
                        "message_id": message.message_id,
                        "reason": result.get("reason"),
                        "result": result,
                    }
                )
                continue
            if not result.get("ok"):
                errors += 1
                skip_reasons[str(result.get("reason") or "retryable_error")] += 1
                _log(
                    {
                        "event": "retryable_error",
                        "chat_id": chat_id,
                        "message_id": message.message_id,
                        "message": asdict(message),
                        "result": result,
                    }
                )
                continue
            processed += 1
            _log(
                {
                    "event": "processed",
                    "chat_id": chat_id,
                    "message_id": message.message_id,
                    "message": asdict(message),
                    "result": result,
                }
            )

    summary = {"processed": processed, "skipped": skipped, "errors": errors, "chats": len(chats), "skip_reasons": dict(skip_reasons)}
    _log({"event": "summary", **summary})
    return summary


def _dedup_allowed(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is not True:
        return False
    return str(result.get("processing_status") or "processed") in {"processed", "queued", "ignored"}


async def _list_recent_chats(reader: AvitoReadClient, account_id: int, *, chat_limit: int) -> list[dict[str, Any]]:
    target = max(0, int(chat_limit or 0))
    if target <= 0:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    page_size = min(DEFAULT_POLLER_PAGE_SIZE, target)
    while len(rows) < target:
        limit = min(page_size, target - len(rows))
        payload = await reader.list_chats(account_id, limit=limit, offset=offset)
        page = _items(payload, "chats", "items")
        if not page:
            break
        rows.extend(page)
        if len(page) < limit:
            break
        offset += len(page)
    return rows[:target]


async def main() -> None:
    settings = IntegrationSettings.from_env()
    interval = _env_int("AVITO_POLLER_INTERVAL_SECONDS", 60)
    access_error_backoff = _env_int("AVITO_POLLER_ACCESS_ERROR_BACKOFF_SECONDS", DEFAULT_ACCESS_ERROR_BACKOFF_SECONDS)
    lookback = _env_int("AVITO_POLLER_LOOKBACK_SECONDS", 3600)
    chat_limit = _env_int("AVITO_POLLER_CHAT_LIMIT", _env_int("AVITO_UNANSWERED_CHAT_LIMIT", DEFAULT_POLLER_CHAT_LIMIT))
    messages_per_chat = _env_int(
        "AVITO_POLLER_MESSAGES_PER_CHAT",
        _env_int("AVITO_UNANSWERED_MESSAGES_PER_CHAT", DEFAULT_POLLER_MESSAGES_PER_CHAT),
    )
    once = os.getenv("AVITO_POLLER_ONCE", "").lower() in {"1", "true", "yes", "on"}

    while True:
        try:
            summary = await run_once(
                settings,
                lookback_seconds=lookback,
                chat_limit=chat_limit,
                messages_per_chat=messages_per_chat,
            )
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        except AvitoApiError as exc:
            if _is_messenger_access_error(exc):
                row = {
                    "event": "access_error",
                    "status_code": exc.status_code,
                    "error": repr(exc),
                    "payload": _safe_error_payload(exc),
                    "backoff_seconds": access_error_backoff,
                }
                _log(row)
                print(json.dumps({"ok": False, **row}, ensure_ascii=False, default=str), flush=True)
                if once:
                    return
                await asyncio.sleep(access_error_backoff)
                continue
            _log({"event": "fatal_error", "error": repr(exc), "status_code": exc.status_code, "payload": _safe_error_payload(exc)})
            print(json.dumps({"ok": False, "error": repr(exc)}, ensure_ascii=False), flush=True)
        except Exception as exc:
            _log({"event": "fatal_error", "error": repr(exc)})
            print(json.dumps({"ok": False, "error": repr(exc)}, ensure_ascii=False), flush=True)
        if once:
            return
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
