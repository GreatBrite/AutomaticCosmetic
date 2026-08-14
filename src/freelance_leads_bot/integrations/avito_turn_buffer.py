from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterator

from .models import AvitoListingContext, Channel, InboundMessage


DEFAULT_AVITO_TURN_BUFFER_PATH = Path("data/avito_turn_buffer.json")


@contextmanager
def _locked_state(path: Path | str) -> Iterator[dict[str, Any]]:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        try:
            state = json.loads(handle.read() or "{}")
        except json.JSONDecodeError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        yield state
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def enqueue_avito_turn_message(
    message: InboundMessage,
    *,
    debounce_seconds: int = 60,
    max_wait_seconds: int = 120,
    max_messages: int = 10,
    path: Path | str = DEFAULT_AVITO_TURN_BUFFER_PATH,
) -> dict[str, Any]:
    now = time.time()
    chat_key = message.chat_id or message.client_id
    if not chat_key:
        return {"queued": False, "reason": "missing_chat_id"}
    with _locked_state(path) as state:
        batches = state.setdefault("batches", {})
        batch = batches.get(chat_key) if isinstance(batches.get(chat_key), dict) else {}
        first_seen_at = float(batch.get("first_seen_at") or now)
        messages = [item for item in batch.get("messages", []) if isinstance(item, dict)]
        if not any(str(item.get("message_id") or "") == message.message_id for item in messages):
            messages.append(_message_to_dict(message))
        if max_messages > 0:
            messages = messages[-max_messages:]
        batch = {
            "chat_key": chat_key,
            "chat_id": message.chat_id,
            "client_id": message.client_id,
            "account_id": message.metadata.get("account_id"),
            "first_seen_at": first_seen_at,
            "updated_at": now,
            "process_after": now + max(0, debounce_seconds),
            "max_process_after": first_seen_at + max(max_wait_seconds, debounce_seconds),
            "messages": messages,
        }
        batches[chat_key] = batch
    return {
        "queued": True,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "messages": len(messages),
        "process_after": int(batch["process_after"]),
    }


def pop_due_avito_turn_batches(
    *,
    now: float | None = None,
    path: Path | str = DEFAULT_AVITO_TURN_BUFFER_PATH,
    limit: int = 20,
    lease_seconds: int = 180,
) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    due: list[dict[str, Any]] = []
    with _locked_state(path) as state:
        batches = state.setdefault("batches", {})
        for chat_key, batch in list(batches.items()):
            if not isinstance(batch, dict):
                batches.pop(chat_key, None)
                continue
            process_after = float(batch.get("process_after") or 0)
            max_process_after = float(batch.get("max_process_after") or process_after)
            lease_until = float(batch.get("lease_until") or 0)
            if lease_until > now:
                continue
            if now >= process_after or now >= max_process_after:
                batch["lease_until"] = now + max(1, lease_seconds)
                batch["leased_at"] = now
                batch["attempts"] = int(batch.get("attempts") or 0)
                due.append(dict(batch))
                if len(due) >= limit:
                    break
    return due


def mark_avito_turn_batch_processed(
    batch: dict[str, Any],
    *,
    path: Path | str = DEFAULT_AVITO_TURN_BUFFER_PATH,
) -> None:
    chat_key = str(batch.get("chat_key") or batch.get("chat_id") or "").strip()
    if not chat_key:
        return
    with _locked_state(path) as state:
        batches = state.setdefault("batches", {})
        current = batches.get(chat_key)
        if isinstance(current, dict) and _same_batch(current, batch):
            batches.pop(chat_key, None)


def mark_avito_turn_batch_failed(
    batch: dict[str, Any],
    error: str,
    *,
    now: float | None = None,
    path: Path | str = DEFAULT_AVITO_TURN_BUFFER_PATH,
    max_backoff_seconds: int = 300,
) -> None:
    now = time.time() if now is None else now
    chat_key = str(batch.get("chat_key") or batch.get("chat_id") or "").strip()
    if not chat_key:
        return
    with _locked_state(path) as state:
        batches = state.setdefault("batches", {})
        current = batches.get(chat_key)
        if not isinstance(current, dict) or not _same_batch(current, batch):
            return
        attempts = int(current.get("attempts") or 0) + 1
        backoff = min(max_backoff_seconds, 30 * (2 ** min(attempts - 1, 4)))
        current["attempts"] = attempts
        current["last_error"] = str(error or "")[-500:]
        current["last_failed_at"] = now
        current["process_after"] = now + backoff
        current["max_process_after"] = current["process_after"]
        current["lease_until"] = 0
        current["leased_at"] = 0


def _same_batch(current: dict[str, Any], batch: dict[str, Any]) -> bool:
    current_ids = [str(item.get("message_id") or "") for item in current.get("messages", []) if isinstance(item, dict)]
    batch_ids = [str(item.get("message_id") or "") for item in batch.get("messages", []) if isinstance(item, dict)]
    return current_ids == batch_ids


def batch_to_inbound_message(batch: dict[str, Any]) -> InboundMessage:
    raw_messages = [item for item in batch.get("messages", []) if isinstance(item, dict)]
    messages = [_message_from_dict(item) for item in raw_messages]
    if not messages:
        return InboundMessage(channel=Channel.AVITO, client_id=str(batch.get("client_id") or ""), chat_id=str(batch.get("chat_id") or ""))
    if len(messages) == 1:
        return messages[0]
    base = messages[-1]
    text_parts = []
    for index, message in enumerate(messages, start=1):
        content = message.text.strip() or ("[медиа]" if message.has_photo else "[пустое сообщение]")
        text_parts.append(f"{index}. {content}")
    metadata = _merge_metadata(messages, batch)
    return replace(
        base,
        text="Клиент прислал несколько сообщений подряд:\n" + "\n".join(text_parts),
        message_id=",".join(message.message_id for message in messages if message.message_id) or base.message_id,
        created_at=max((message.created_at for message in messages), default=base.created_at),
        has_photo=any(message.has_photo for message in messages),
        listing=_latest_listing(messages),
        metadata=metadata,
    )


def _message_to_dict(message: InboundMessage) -> dict[str, Any]:
    data = asdict(message)
    data["channel"] = message.channel.value
    return data


def _message_from_dict(data: dict[str, Any]) -> InboundMessage:
    listing_data = data.get("listing") if isinstance(data.get("listing"), dict) else None
    listing = AvitoListingContext(**listing_data) if listing_data else None
    channel = Channel(str(data.get("channel") or Channel.AVITO.value))
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return InboundMessage(
        channel=channel,
        client_id=str(data.get("client_id") or ""),
        chat_id=str(data.get("chat_id") or ""),
        message_id=str(data.get("message_id") or ""),
        text=str(data.get("text") or ""),
        created_at=int(data.get("created_at") or 0),
        has_photo=bool(data.get("has_photo")),
        listing=listing,
        metadata=metadata,
    )


def _latest_listing(messages: list[InboundMessage]) -> AvitoListingContext | None:
    for message in reversed(messages):
        if message.listing and message.listing.has_listing:
            return message.listing
    return messages[-1].listing if messages else None


def _merge_metadata(messages: list[InboundMessage], batch: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(messages[-1].metadata if messages else {})
    photo_urls: list[str] = []
    photo_ids: list[str] = []
    media_urls: list[str] = []
    media_ids: list[str] = []
    media_types: list[str] = []
    raw_messages: list[Any] = []
    message_ids: list[str] = []
    for message in messages:
        message_ids.append(message.message_id)
        raw_messages.append(message.metadata.get("raw"))
        photo_urls.extend(str(url) for url in message.metadata.get("photo_urls", []) if str(url))
        photo_ids.extend(str(item) for item in message.metadata.get("photo_ids", []) if str(item))
        media_urls.extend(str(url) for url in message.metadata.get("media_urls", []) if str(url))
        media_ids.extend(str(item) for item in message.metadata.get("media_ids", []) if str(item))
        media_types.extend(str(item) for item in message.metadata.get("media_types", []) if str(item))
    metadata["batched"] = True
    metadata["batch_size"] = len(messages)
    metadata["batched_message_ids"] = [item for item in message_ids if item]
    metadata["raw_messages"] = raw_messages
    metadata["photo_urls"] = _unique(photo_urls)
    metadata["photo_ids"] = _unique(photo_ids)
    metadata["media_urls"] = _unique(media_urls)
    metadata["media_ids"] = _unique(media_ids)
    metadata["media_types"] = _unique(media_types)
    metadata["account_id"] = metadata.get("account_id") or batch.get("account_id")
    return metadata


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
