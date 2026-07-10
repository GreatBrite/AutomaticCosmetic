from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from pyavitoapi.transport.errors import AvitoApiError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.freelance_leads_bot.integrations.avito_dedup import PersistentProcessedEventStore
from src.freelance_leads_bot.integrations.avito_read import AvitoReadClient
from src.freelance_leads_bot.integrations.config import IntegrationSettings
from src.freelance_leads_bot.telegram import TelegramBot


LOG_PATH = Path("data/avito_live_telegram_relay.log")
SEEN_PATH = Path("data/avito_live_telegram_seen.json")
PREVIEW_SEEN_PATH = Path("data/avito_live_telegram_preview_seen.json")
HANDOFF_SEEN_PATH = Path("data/avito_live_telegram_handoff_seen.json")
CARDS_PATH = Path("data/avito_live_telegram_cards.json")
PREVIEW_OUTBOX_PATH = Path("data/avito_outbox.jsonl")
HANDOFF_OUTBOX_PATH = Path("data/handoff_outbox.jsonl")
DEFAULT_TELEGRAM_CHAT_ID = "-1003784160049"
DEFAULT_ACCESS_ERROR_BACKOFF_SECONDS = 1800


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


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


def load_cards(path: Path = CARDS_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def save_cards(cards: dict[str, dict[str, Any]], path: Path = CARDS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cards, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def card_chat_snapshot(chat: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(chat.get("id") or ""),
        "context": chat.get("context") if isinstance(chat.get("context"), dict) else {},
        "users": chat.get("users") if isinstance(chat.get("users"), list) else [],
    }


def _items(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _content(raw_message: dict[str, Any]) -> dict[str, Any]:
    return raw_message.get("content") if isinstance(raw_message.get("content"), dict) else {}


def _message_text(raw_message: dict[str, Any]) -> str:
    content = _content(raw_message)
    text = content.get("text")
    if isinstance(text, str):
        return text.strip()
    if isinstance(text, dict):
        return str(text.get("text") or "").strip()
    return str(raw_message.get("text") or "").strip()


def _message_type(raw_message: dict[str, Any]) -> str:
    return str(raw_message.get("type") or "").lower()


def _message_direction(raw_message: dict[str, Any], own_account_ids: set[int]) -> str:
    direction = str(raw_message.get("direction") or "").lower()
    if direction in {"in", "out"}:
        return direction
    return "out" if _is_own_author(raw_message, own_account_ids) else "in"


def _author_id(raw_message: dict[str, Any]) -> int:
    try:
        return int(raw_message.get("author_id") or 0)
    except (TypeError, ValueError):
        return 0


def _is_own_author(raw_message: dict[str, Any], own_account_ids: set[int]) -> bool:
    author_id = _author_id(raw_message)
    return bool(author_id and author_id in own_account_ids)


def _photo_urls(raw_message: dict[str, Any]) -> list[str]:
    content = _content(raw_message)
    result: list[str] = []
    for key in ("image", "photo", "images", "photos"):
        raw = content.get(key) or raw_message.get(key)
        candidates = raw if isinstance(raw, list) else [raw]
        for candidate in candidates:
            result.extend(_urls_from_photo(candidate))
    for attachment in raw_message.get("attachments") or content.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("type") in {"image", "photo"}:
            result.extend(_urls_from_photo(attachment.get("image") or attachment.get("photo") or attachment))
    return list(dict.fromkeys(url for url in result if url))


def _urls_from_photo(photo: Any) -> list[str]:
    if isinstance(photo, str):
        return [photo] if photo.startswith(("http://", "https://")) else []
    if not isinstance(photo, dict):
        return []
    urls: list[str] = []
    for key in ("url", "src", "link", "image_url", "photo_url"):
        value = photo.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            urls.append(value)
    sizes = photo.get("sizes")
    if isinstance(sizes, dict):
        for value in sizes.values():
            urls.extend(_urls_from_photo(value))
    if isinstance(sizes, list):
        for value in sizes:
            urls.extend(_urls_from_photo(value))
    return urls


def _listing_title(chat: dict[str, Any]) -> str:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    value = context.get("value") if isinstance(context.get("value"), dict) else {}
    parts = [str(value.get("title") or "").strip(), str(value.get("price_string") or "").strip()]
    return " | ".join(part for part in parts if part)


def _chat_user_name(chat: dict[str, Any], raw_message: dict[str, Any]) -> str:
    author_id = _author_id(raw_message)
    for user in chat.get("users") or []:
        if isinstance(user, dict) and _safe_int(user.get("id")) == author_id:
            return str(user.get("name") or "").strip()
    return ""


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _timestamp_from_iso(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _format_time(raw_message: dict[str, Any]) -> str:
    created = _safe_int(raw_message.get("created"))
    if not created:
        return ""
    return datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def should_relay(raw_message: dict[str, Any], *, since_ts: int) -> tuple[bool, str]:
    message_id = str(raw_message.get("id") or "")
    if not message_id:
        return False, "missing_message_id"
    created = _safe_int(raw_message.get("created"))
    if created and created < since_ts:
        return False, "too_old"
    message_type = _message_type(raw_message)
    text = _message_text(raw_message).casefold()
    if message_type == "system":
        return False, "system"
    if message_type == "deleted" or text in {"сообщение удалено", "message deleted"}:
        return False, "deleted_message"
    if not text and not _photo_urls(raw_message):
        return False, "empty"
    return True, ""


def format_telegram_message(
    *,
    account_id: int,
    chat: dict[str, Any],
    raw_message: dict[str, Any],
    own_account_ids: set[int],
) -> str:
    direction = _message_direction(raw_message, own_account_ids)
    is_own = _is_own_author(raw_message, own_account_ids) or direction == "out"
    actor = "🤖 Бот/аккаунт" if is_own else "👤 Клиент"
    chat_id = str(chat.get("id") or "")
    message_id = str(raw_message.get("id") or "")
    author = _chat_user_name(chat, raw_message)
    text = _message_text(raw_message)
    photos = _photo_urls(raw_message)
    listing = _listing_title(chat)
    lines = [
        f"<b>Avito live · {actor}</b>",
        f"<b>chat_id:</b> <code>{escape(chat_id)}</code>",
        f"<b>message_id:</b> <code>{escape(message_id)}</code>",
        f"<b>account_id:</b> <code>{account_id}</code>",
    ]
    timestamp = _format_time(raw_message)
    if timestamp:
        lines.append(f"<b>time:</b> {escape(timestamp)}")
    if author:
        lines.append(f"<b>author:</b> {escape(author)}")
    if listing:
        lines.append(f"<b>listing:</b> {escape(listing)}")
    if text:
        lines.extend(["", escape(text)])
    if photos:
        lines.append("")
        lines.append("<b>photo:</b> " + " ".join(f'<a href="{escape(url)}">link</a>' for url in photos[:5]))
    return "\n".join(lines)


def telegram_message_id(send_response: dict[str, Any]) -> int:
    result = send_response.get("result") if isinstance(send_response.get("result"), dict) else {}
    return _safe_int(result.get("message_id") or send_response.get("message_id"))


def compact_relay_event(
    *,
    chat: dict[str, Any],
    raw_message: dict[str, Any],
    own_account_ids: set[int],
) -> dict[str, Any]:
    direction = _message_direction(raw_message, own_account_ids)
    is_own = _is_own_author(raw_message, own_account_ids) or direction == "out"
    return {
        "id": str(raw_message.get("id") or ""),
        "created": _safe_int(raw_message.get("created")),
        "direction": "out" if is_own else "in",
        "actor": "Бот/аккаунт" if is_own else "Клиент",
        "author": _chat_user_name(chat, raw_message),
        "type": _message_type(raw_message) or "message",
        "text": _message_text(raw_message),
        "photos": _photo_urls(raw_message)[:5],
    }


def compact_preview_event(row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("text") or "").strip()
    image_path = str(row.get("image_path") or "").strip()
    preview_type = str(row.get("type") or ("image" if image_path else "text"))
    return {
        "id": preview_event_key(row),
        "created": _safe_int(row.get("ts")) or int(time.time()),
        "direction": "out",
        "actor": "Codex preview",
        "author": "бот не отправил клиенту",
        "type": preview_type,
        "text": text or (f"[image preview] {image_path}" if image_path else ""),
        "photos": [],
    }


def preview_event_key(row: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "ts": row.get("ts"),
            "account_id": row.get("account_id"),
            "chat_id": row.get("chat_id"),
            "text": row.get("text"),
            "image_path": row.get("image_path"),
            "type": row.get("type"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "preview:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def iter_preview_outbox(path: Path, *, since_ts: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if _safe_int(row.get("ts")) < since_ts:
            continue
        if row.get("sent") is not False or row.get("reason") != "preview_only":
            continue
        if not row.get("chat_id"):
            continue
        if not str(row.get("text") or row.get("image_path") or "").strip():
            continue
        rows.append(row)
    return rows


def handoff_event_key(row: dict[str, Any]) -> str:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    raw = json.dumps(
        {
            "created_at": row.get("created_at"),
            "reason": row.get("reason"),
            "chat_id": message.get("chat_id"),
            "message_id": message.get("message_id"),
            "summary": row.get("summary"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "handoff:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def iter_handoff_outbox(path: Path, *, since_ts: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        created = _timestamp_from_iso(row.get("created_at"))
        if created and created < since_ts:
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if not message.get("chat_id"):
            continue
        if not str(row.get("summary") or row.get("text") or "").strip():
            continue
        rows.append(row)
    return rows


def compact_handoff_event(row: dict[str, Any]) -> dict[str, Any]:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    reason = str(row.get("reason") or "").strip()
    summary = str(row.get("summary") or "").strip()
    client_text = str(message.get("text") or "").strip()
    parts = []
    if reason:
        parts.append(f"Причина: {reason}")
    if client_text:
        parts.append(f"Сообщение клиента: {client_text}")
    if summary:
        parts.append(f"Вопрос/контекст: {summary}")
    return {
        "id": handoff_event_key(row),
        "created": _timestamp_from_iso(row.get("created_at")) or int(time.time()),
        "direction": "out",
        "actor": "Codex question",
        "author": "вопрос в live, Ольге лично не отправлен",
        "type": "handoff",
        "text": "\n".join(parts) or str(row.get("text") or "").strip(),
        "photos": [],
    }


def merge_events(old_events: list[dict[str, Any]], new_events: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for event in [*old_events, *new_events]:
        event_id = str(event.get("id") or "")
        if event_id:
            by_id[event_id] = event
    events = list(by_id.values())
    events.sort(key=lambda item: (_safe_int(item.get("created")), str(item.get("id") or "")))
    return events[-limit:]


def format_telegram_card(
    *,
    account_id: int,
    chat: dict[str, Any],
    events: list[dict[str, Any]],
    max_visible_events: int,
) -> str:
    chat_id = str(chat.get("id") or "")
    listing = _listing_title(chat)
    lines = [
        "<b>Avito live</b>",
        f"<b>chat_id:</b> <code>{escape(chat_id)}</code>",
        f"<b>account_id:</b> <code>{account_id}</code>",
    ]
    if listing:
        lines.append(f"<b>listing:</b> {escape(listing)}")
    if events:
        updated = _safe_int(events[-1].get("created"))
        if updated:
            lines.append(f"<b>updated:</b> {escape(datetime.fromtimestamp(updated, timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}")
    lines.append("")
    lines.append("<b>Переписка:</b>")

    hidden = max(0, len(events) - max_visible_events)
    if hidden:
        lines.append(f"<i>Скрыто старых сообщений: {hidden}</i>")
    event_text_limit = max(160, min(700, 2600 // max(1, max_visible_events)))
    for event in events[-max_visible_events:]:
        created = _safe_int(event.get("created"))
        stamp = datetime.fromtimestamp(created, timezone.utc).strftime("%m-%d %H:%M") if created else "time?"
        actor = str(event.get("actor") or "Сообщение")
        prefix = "🤖" if event.get("direction") == "out" else "👤"
        author = str(event.get("author") or "").strip()
        title = f"{prefix} {actor}"
        if author:
            title += f" · {author}"
        text = str(event.get("text") or "").strip()
        if len(text) > event_text_limit:
            text = text[: event_text_limit - 1].rstrip() + "…"
        photos = [str(url) for url in event.get("photos") or [] if str(url).startswith(("http://", "https://"))]
        body = escape(text) if text else f"<i>{escape(str(event.get('type') or 'message'))}</i>"
        if photos:
            links = f'<a href="{escape(photos[0])}">photo</a>'
            if len(photos) > 1:
                links += f" +{len(photos) - 1}"
            body = f"{body}\n{links}" if body else links
        lines.append("")
        lines.append(f"<b>{escape(stamp)} · {escape(title)}</b>")
        lines.append(body)
    return "\n".join(lines)


def _card_messages(card: dict[str, Any]) -> list[dict[str, Any]]:
    messages = card.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def _card_telegram_message_id(card: dict[str, Any]) -> int:
    return _safe_int(card.get("telegram_message_id"))


def send_or_edit_card(
    *,
    bot: TelegramBot,
    telegram_chat_id: str,
    cards: dict[str, dict[str, Any]],
    account_id: int,
    chat: dict[str, Any],
    new_events: list[dict[str, Any]],
    history_per_card: int,
    visible_per_card: int,
) -> int:
    chat_id = str(chat.get("id") or "")
    card = cards.get(chat_id, {})
    events = merge_events(_card_messages(card), new_events, limit=history_per_card)
    text = format_telegram_card(account_id=account_id, chat=chat, events=events, max_visible_events=visible_per_card)
    message_id = _card_telegram_message_id(card)
    if message_id:
        try:
            bot.edit_message_text(telegram_chat_id, message_id, text)
        except Exception as exc:
            _log({"event": "edit_failed_sending_new_card", "chat_id": chat_id, "message_id": message_id, "error": repr(exc)})
            response = bot.send_message(telegram_chat_id, text)
            message_id = telegram_message_id(response)
    else:
        response = bot.send_message(telegram_chat_id, text)
        message_id = telegram_message_id(response)
    if not message_id:
        raise RuntimeError("Telegram response did not include message_id")
    cards[chat_id] = {
        "telegram_message_id": message_id,
        "messages": events,
        "chat": card_chat_snapshot(chat),
        "updated_at": int(time.time()),
    }
    save_cards(cards)
    return message_id


async def relay_once(
    settings: IntegrationSettings,
    *,
    telegram_chat_id: str,
    lookback_seconds: int,
    chat_limit: int,
    messages_per_chat: int,
    history_per_card: int,
    visible_per_card: int,
    preview_outbox_path: Path,
    preview_lookback_seconds: int,
    handoff_outbox_path: Path,
    handoff_lookback_seconds: int,
    seen: PersistentProcessedEventStore,
    preview_seen: PersistentProcessedEventStore,
    handoff_seen: PersistentProcessedEventStore,
    cards: dict[str, dict[str, Any]],
    bot: TelegramBot,
) -> dict[str, Any]:
    account_id = settings.avito_account_id
    if not account_id:
        raise RuntimeError("AVITO_ACCOUNT_ID is required")
    reader = AvitoReadClient(settings)
    own_account_ids = set(settings.avito_account_ids)
    own_account_ids.add(account_id)
    since_ts = int(time.time()) - lookback_seconds
    chats_payload = await reader.list_chats(account_id, limit=chat_limit)
    chats = _items(chats_payload, "chats", "items")
    relayed = 0
    relayed_preview = 0
    relayed_handoff = 0
    updated_cards = 0
    skipped = 0
    errors = 0

    for chat in chats:
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            skipped += 1
            continue
        messages_payload = await reader.get_chat_messages(account_id, chat_id, limit=messages_per_chat)
        new_events: list[dict[str, Any]] = []
        new_seen_keys: list[str] = []
        for raw_message in reversed(_items(messages_payload, "messages", "items")):
            ok, reason = should_relay(raw_message, since_ts=since_ts)
            message_id = str(raw_message.get("id") or "")
            key = f"{chat_id}:{message_id}"
            if not ok:
                skipped += 1
                continue
            if key in seen.seen:
                skipped += 1
                continue
            new_events.append(compact_relay_event(chat=chat, raw_message=raw_message, own_account_ids=own_account_ids))
            new_seen_keys.append(key)

        if not new_events:
            continue
        try:
            message_id = send_or_edit_card(
                bot=bot,
                telegram_chat_id=telegram_chat_id,
                cards=cards,
                account_id=account_id,
                chat=chat,
                new_events=new_events,
                history_per_card=history_per_card,
                visible_per_card=visible_per_card,
            )
            for key in new_seen_keys:
                seen.mark_once(key)
        except Exception as exc:
            errors += 1
            _log({"event": "relay_error", "chat_id": chat_id, "message_ids": [event.get("id") for event in new_events], "error": repr(exc)})
            continue
        updated_cards += 1
        relayed += len(new_events)
        _log({"event": "card_updated", "chat_id": chat_id, "telegram_message_id": message_id, "messages": len(new_events)})

    preview_since_ts = int(time.time()) - preview_lookback_seconds
    preview_by_chat: dict[str, list[dict[str, Any]]] = {}
    for row in iter_preview_outbox(preview_outbox_path, since_ts=preview_since_ts):
        key = preview_event_key(row)
        if key in preview_seen.seen:
            skipped += 1
            continue
        preview_by_chat.setdefault(str(row.get("chat_id") or ""), []).append(row)
    for chat_id, rows in preview_by_chat.items():
        new_events = [compact_preview_event(row) for row in rows]
        card = cards.get(chat_id, {})
        chat = card.get("chat") if isinstance(card.get("chat"), dict) else {"id": chat_id}
        account = _safe_int(rows[-1].get("account_id")) or account_id
        try:
            message_id = send_or_edit_card(
                bot=bot,
                telegram_chat_id=telegram_chat_id,
                cards=cards,
                account_id=account,
                chat=chat,
                new_events=new_events,
                history_per_card=history_per_card,
                visible_per_card=visible_per_card,
            )
            for row in rows:
                preview_seen.mark_once(preview_event_key(row))
        except Exception as exc:
            errors += 1
            _log({"event": "preview_relay_error", "chat_id": chat_id, "error": repr(exc)})
            continue
        updated_cards += 1
        relayed_preview += len(new_events)
        _log({"event": "preview_card_updated", "chat_id": chat_id, "telegram_message_id": message_id, "messages": len(new_events)})

    handoff_since_ts = int(time.time()) - handoff_lookback_seconds
    handoff_by_chat: dict[str, list[dict[str, Any]]] = {}
    for row in iter_handoff_outbox(handoff_outbox_path, since_ts=handoff_since_ts):
        key = handoff_event_key(row)
        if key in handoff_seen.seen:
            skipped += 1
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        handoff_by_chat.setdefault(str(message.get("chat_id") or ""), []).append(row)
    for chat_id, rows in handoff_by_chat.items():
        new_events = [compact_handoff_event(row) for row in rows]
        card = cards.get(chat_id, {})
        chat = card.get("chat") if isinstance(card.get("chat"), dict) else {"id": chat_id}
        account = account_id
        try:
            message_id = send_or_edit_card(
                bot=bot,
                telegram_chat_id=telegram_chat_id,
                cards=cards,
                account_id=account,
                chat=chat,
                new_events=new_events,
                history_per_card=history_per_card,
                visible_per_card=visible_per_card,
            )
            for row in rows:
                handoff_seen.mark_once(handoff_event_key(row))
        except Exception as exc:
            errors += 1
            _log({"event": "handoff_relay_error", "chat_id": chat_id, "error": repr(exc)})
            continue
        updated_cards += 1
        relayed_handoff += len(new_events)
        _log({"event": "handoff_card_updated", "chat_id": chat_id, "telegram_message_id": message_id, "messages": len(new_events)})

    summary = {
        "relayed": relayed,
        "relayed_preview": relayed_preview,
        "relayed_handoff": relayed_handoff,
        "updated_cards": updated_cards,
        "skipped": skipped,
        "errors": errors,
        "chats": len(chats),
    }
    _log({"event": "summary", **summary})
    return summary


async def main() -> None:
    settings = IntegrationSettings.from_env()
    telegram_token = os.getenv("AVITO_LIVE_TELEGRAM_BOT_TOKEN", "").strip() or settings.telegram_admin_bot_token
    telegram_chat_id = os.getenv("AVITO_LIVE_TELEGRAM_CHAT_ID", "").strip() or DEFAULT_TELEGRAM_CHAT_ID
    interval = _env_float("AVITO_LIVE_RELAY_INTERVAL_SECONDS", 15.0)
    access_error_backoff = _env_int("AVITO_LIVE_RELAY_ACCESS_ERROR_BACKOFF_SECONDS", DEFAULT_ACCESS_ERROR_BACKOFF_SECONDS)
    lookback = _env_int("AVITO_LIVE_RELAY_LOOKBACK_SECONDS", 1800)
    chat_limit = _env_int("AVITO_LIVE_RELAY_CHAT_LIMIT", 30)
    messages_per_chat = _env_int("AVITO_LIVE_RELAY_MESSAGES_PER_CHAT", 20)
    history_per_card = _env_int("AVITO_LIVE_RELAY_HISTORY_PER_CARD", 40)
    visible_per_card = _env_int("AVITO_LIVE_RELAY_VISIBLE_PER_CARD", 16)
    preview_lookback = _env_int("AVITO_LIVE_RELAY_PREVIEW_LOOKBACK_SECONDS", 3600)
    preview_outbox_path = Path(os.getenv("AVITO_LIVE_RELAY_PREVIEW_OUTBOX", "").strip() or PREVIEW_OUTBOX_PATH)
    handoff_lookback = _env_int("AVITO_LIVE_RELAY_HANDOFF_LOOKBACK_SECONDS", 3600)
    handoff_outbox_path = Path(os.getenv("AVITO_LIVE_RELAY_HANDOFF_OUTBOX", "").strip() or HANDOFF_OUTBOX_PATH)
    once = _env_bool("AVITO_LIVE_RELAY_ONCE")
    if not telegram_token:
        raise RuntimeError("TELEGRAM_ADMIN_BOT_TOKEN or AVITO_LIVE_TELEGRAM_BOT_TOKEN is required")
    bot = TelegramBot(telegram_token)
    seen = PersistentProcessedEventStore(SEEN_PATH)
    preview_seen = PersistentProcessedEventStore(PREVIEW_SEEN_PATH)
    handoff_seen = PersistentProcessedEventStore(HANDOFF_SEEN_PATH)
    cards = load_cards()

    while True:
        try:
            summary = await relay_once(
                settings,
                telegram_chat_id=telegram_chat_id,
                lookback_seconds=lookback,
                chat_limit=chat_limit,
                messages_per_chat=messages_per_chat,
                history_per_card=history_per_card,
                visible_per_card=visible_per_card,
                preview_outbox_path=preview_outbox_path,
                preview_lookback_seconds=preview_lookback,
                handoff_outbox_path=handoff_outbox_path,
                handoff_lookback_seconds=handoff_lookback,
                seen=seen,
                preview_seen=preview_seen,
                handoff_seen=handoff_seen,
                cards=cards,
                bot=bot,
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
