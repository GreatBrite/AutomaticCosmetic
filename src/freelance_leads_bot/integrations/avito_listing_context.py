from __future__ import annotations

import fcntl
import json
import re
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from .models import AvitoListingContext, InboundMessage


DEFAULT_AVITO_LISTING_CONTEXTS_PATH = Path("data/avito_listing_contexts.json")
LISTING_HISTORY_RE = re.compile(r"(?im)^Объявление:\s*(.+)$")


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
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def remember_avito_listing_context(
    chat_id: str,
    listing: AvitoListingContext | None,
    *,
    path: Path | str = DEFAULT_AVITO_LISTING_CONTEXTS_PATH,
) -> None:
    chat_key = str(chat_id or "").strip()
    if not chat_key or not listing or not listing.has_listing:
        return
    with _locked_state(path) as state:
        state[chat_key] = {
            "chat_id": chat_key,
            "item_id": listing.item_id,
            "title": listing.title,
            "url": listing.url,
            "price_string": listing.price_string,
            "city": listing.city,
            "updated_at": int(time.time()),
        }


def cached_avito_listing_context(
    chat_id: str,
    *,
    path: Path | str = DEFAULT_AVITO_LISTING_CONTEXTS_PATH,
) -> AvitoListingContext | None:
    chat_key = str(chat_id or "").strip()
    if not chat_key:
        return None
    state_path = Path(path)
    if not state_path.exists():
        return None
    with _locked_state(state_path) as state:
        row = state.get(chat_key)
    return _listing_from_row(row)


def restore_avito_listing_context(
    message: InboundMessage,
    *,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    path: Path | str = DEFAULT_AVITO_LISTING_CONTEXTS_PATH,
) -> InboundMessage:
    if message.listing and message.listing.has_listing:
        remember_avito_listing_context(message.chat_id, message.listing, path=path)
        return message
    listing = cached_avito_listing_context(message.chat_id, path=path) or listing_context_from_history(conversation_history)
    if not listing:
        return message
    return replace(message, listing=listing, metadata={**message.metadata, "listing_restored": True})


def listing_context_from_history(
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> AvitoListingContext | None:
    for item in reversed(list(conversation_history)[-12:]):
        content = str(item.get("content") or "")
        match = LISTING_HISTORY_RE.search(content)
        if not match:
            continue
        return _listing_from_history_line(match.group(1))
    return None


def _listing_from_history_line(line: str) -> AvitoListingContext | None:
    parts = [part.strip() for part in str(line or "").split("|")]
    title = parts[0] if parts else ""
    price = ""
    city = ""
    for part in parts[1:]:
        if "₽" in part or "руб" in part.casefold() or "бесплат" in part.casefold():
            price = part
        elif part:
            city = part
    listing = AvitoListingContext(title=title, price_string=price, city=city)
    return listing if listing.has_listing else None


def _listing_from_row(row: Any) -> AvitoListingContext | None:
    if not isinstance(row, dict):
        return None
    listing = AvitoListingContext(
        item_id=_int_or_none(row.get("item_id")),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        price_string=str(row.get("price_string") or ""),
        city=str(row.get("city") or ""),
    )
    return listing if listing.has_listing else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
