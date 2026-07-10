from __future__ import annotations

import re
from typing import Any

from .avito_identity import clean_client_name, client_name_from_chat
from .models import AvitoListingContext, Channel, Handoff, HandoffReason, InboundMessage


def avito_event_type(event: dict[str, Any]) -> str:
    if event.get("type"):
        return str(event.get("type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(payload.get("type") or "")


def is_avito_message_event(event: dict[str, Any]) -> bool:
    event_type = avito_event_type(event)
    return event_type == "message" or event_type.startswith("message") or event_type == "messenger"


def avito_value(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("value"), dict):
        return payload["value"]
    if isinstance(event.get("message"), dict):
        return event["message"]
    return event


def avito_inbound_message(event: dict[str, Any]) -> InboundMessage:
    value = avito_value(event)
    content = value.get("content") if isinstance(value.get("content"), dict) else {}
    listing = _listing_from_payload(event) or _listing_from_content(content)
    author_id = str(value.get("author_id") or "")
    account_id = value.get("account_id") or event.get("account_id") or event.get("user_id") or 0
    return InboundMessage(
        channel=Channel.AVITO,
        client_id=str(value.get("user_id") or value.get("author_id") or ""),
        chat_id=str(value.get("chat_id") or event.get("chat_id") or ""),
        message_id=str(value.get("id") or value.get("message_id") or ""),
        text=_text_from_content(value, content),
        created_at=_created_at(value),
        has_photo=_has_photo(value, content),
        listing=listing,
        metadata={
            "account_id": account_id,
            "author_id": author_id,
            "client_name": _client_name_from_event(event, value=value, account_id=_int_or_none(account_id) or 0, author_id=author_id),
            "direction": str(value.get("direction") or event.get("direction") or ""),
            "message_type": str(value.get("type") or event.get("type") or ""),
            "photo_urls": avito_photo_urls(event),
            "photo_ids": avito_photo_ids(event),
            "media_urls": avito_media_urls(event),
            "media_ids": avito_media_ids(event),
            "media_types": avito_media_types(event),
            "voice_id": avito_voice_id(event),
            "raw": event,
        },
    )


def avito_photo_handoff(message: InboundMessage) -> Handoff | None:
    if not message.has_photo:
        return None
    return Handoff(
        reason=HandoffReason.PHOTO_CONSULTATION,
        message=message,
        summary="Клиент отправил фото, нужна индивидуальная консультация косметолога.",
    )


def _text_from_content(value: dict[str, Any], content: dict[str, Any]) -> str:
    if isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(content.get("text"), dict):
        return str(content["text"].get("text") or "")
    return str(value.get("text") or "")


def _has_photo(value: dict[str, Any], content: dict[str, Any]) -> bool:
    for key in ("image", "photo", "images", "photos", "video", "videos", "file", "files"):
        if content.get(key) or value.get(key):
            return True
    attachments = value.get("attachments") or content.get("attachments") or []
    return any(isinstance(item, dict) and item.get("type") in {"image", "photo", "video", "file"} for item in attachments)


def avito_photo_urls(event: dict[str, Any]) -> list[str]:
    value = avito_value(event)
    content = value.get("content") if isinstance(value.get("content"), dict) else {}
    urls: list[str] = []
    for candidate in _photo_candidates(value, content):
        urls.extend(_urls_from_photo(candidate))
    return _unique(urls)


def avito_photo_ids(event: dict[str, Any]) -> list[str]:
    value = avito_value(event)
    content = value.get("content") if isinstance(value.get("content"), dict) else {}
    ids: list[str] = []
    for candidate in _photo_candidates(value, content):
        if isinstance(candidate, dict):
            raw_id = candidate.get("id") or candidate.get("image_id") or candidate.get("photo_id")
            if raw_id:
                ids.append(str(raw_id))
    return _unique(ids)


def avito_media_urls(event: dict[str, Any]) -> list[str]:
    value = avito_value(event)
    content = value.get("content") if isinstance(value.get("content"), dict) else {}
    urls: list[str] = []
    for candidate in _media_candidates(value, content):
        urls.extend(_urls_from_photo(candidate))
    return _unique(urls)


def avito_media_ids(event: dict[str, Any]) -> list[str]:
    value = avito_value(event)
    content = value.get("content") if isinstance(value.get("content"), dict) else {}
    ids: list[str] = []
    for candidate in _media_candidates(value, content):
        if isinstance(candidate, dict):
            raw_id = candidate.get("id") or candidate.get("image_id") or candidate.get("photo_id") or candidate.get("video_id") or candidate.get("file_id")
            if raw_id:
                ids.append(str(raw_id))
    return _unique(ids)


def avito_media_types(event: dict[str, Any]) -> list[str]:
    value = avito_value(event)
    content = value.get("content") if isinstance(value.get("content"), dict) else {}
    types: list[str] = []
    for key in ("image", "photo", "images", "photos"):
        if content.get(key) or value.get(key):
            types.append("photo")
    for key in ("video", "videos"):
        if content.get(key) or value.get(key):
            types.append("video")
    for key in ("file", "files"):
        if content.get(key) or value.get(key):
            types.append("file")
    for attachment in value.get("attachments") or content.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("type") in {"image", "photo", "video", "file"}:
            raw_type = str(attachment.get("type") or "")
            types.append("photo" if raw_type in {"image", "photo"} else raw_type)
    return _unique(types)


def avito_voice_id(event: dict[str, Any]) -> str:
    value = avito_value(event)
    content = value.get("content") if isinstance(value.get("content"), dict) else {}
    for candidate in (content.get("voice"), value.get("voice")):
        if isinstance(candidate, dict):
            voice_id = candidate.get("voice_id") or candidate.get("id")
            if voice_id:
                return str(voice_id)
        if isinstance(candidate, str):
            return candidate
    return ""


def _created_at(value: dict[str, Any]) -> int:
    raw = value.get("created") or value.get("created_at") or value.get("timestamp") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _photo_candidates(value: dict[str, Any], content: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = []
    for key in ("image", "photo", "images", "photos"):
        raw = content.get(key) or value.get(key)
        if isinstance(raw, list):
            candidates.extend(raw)
        elif raw:
            candidates.append(raw)
    for attachment in value.get("attachments") or content.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("type") in {"image", "photo"}:
            candidates.append(attachment.get("image") or attachment.get("photo") or attachment)
    return candidates


def _media_candidates(value: dict[str, Any], content: dict[str, Any]) -> list[Any]:
    candidates = list(_photo_candidates(value, content))
    for key in ("video", "videos", "file", "files"):
        raw = content.get(key) or value.get(key)
        if isinstance(raw, list):
            candidates.extend(raw)
        elif raw:
            candidates.append(raw)
    for attachment in value.get("attachments") or content.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("type") in {"video", "file"}:
            candidates.append(attachment.get("video") or attachment.get("file") or attachment)
    return candidates


def _urls_from_photo(photo: Any) -> list[str]:
    if isinstance(photo, str):
        return [photo] if photo.startswith(("http://", "https://")) else []
    if not isinstance(photo, dict):
        return []
    urls: list[str] = []
    for key in ("url", "src", "link", "image_url", "photo_url"):
        raw = photo.get(key)
        if isinstance(raw, str) and raw.startswith(("http://", "https://")):
            urls.append(raw)
    sizes = photo.get("sizes")
    best_size_url = _best_size_url(sizes)
    if best_size_url:
        return [best_size_url]
    if isinstance(sizes, dict):
        for value in sizes.values():
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
            elif isinstance(value, dict):
                urls.extend(_urls_from_photo(value))
    if isinstance(sizes, list):
        for value in sizes:
            urls.extend(_urls_from_photo(value))
    return urls


def _best_size_url(sizes: Any) -> str:
    candidates: list[tuple[int, str]] = []
    if isinstance(sizes, dict):
        for key, value in sizes.items():
            urls = [value] if isinstance(value, str) else _urls_from_photo(value)
            for url in urls:
                if url.startswith(("http://", "https://")):
                    candidates.append((_size_score(str(key)), url))
    elif isinstance(sizes, list):
        for index, value in enumerate(sizes):
            urls = [value] if isinstance(value, str) else _urls_from_photo(value)
            for url in urls:
                if url.startswith(("http://", "https://")):
                    candidates.append((index + 1, url))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _size_score(label: str) -> int:
    match = re.search(r"(\d+)\D+(\d+)", label)
    if not match:
        return 0
    return int(match.group(1)) * int(match.group(2))


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _listing_from_payload(event: dict[str, Any]) -> AvitoListingContext | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    item = payload.get("item") or payload.get("listing")
    return _listing_from_dict(item) if isinstance(item, dict) else None


def _listing_from_content(content: dict[str, Any]) -> AvitoListingContext | None:
    item = content.get("item") or content.get("listing")
    return _listing_from_dict(item) if isinstance(item, dict) else None


def _listing_from_dict(item: dict[str, Any]) -> AvitoListingContext:
    return AvitoListingContext(
        item_id=_int_or_none(item.get("id") or item.get("item_id")),
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        price_string=str(item.get("price_string") or item.get("price") or ""),
        city=str(item.get("city") or item.get("location") or ""),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _client_name_from_event(event: dict[str, Any], *, value: dict[str, Any], account_id: int, author_id: str) -> str:
    for key in ("author_name", "user_name", "name"):
        name = clean_client_name(value.get(key) or event.get(key))
        if name:
            return name
    for container in (value.get("user"), value.get("author"), event.get("user"), event.get("author")):
        if isinstance(container, dict):
            name = clean_client_name(container.get("name") or container.get("display_name"))
            if name:
                return name
    return client_name_from_chat(event, account_id=account_id, author_id=author_id)
