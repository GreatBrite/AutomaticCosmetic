from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Channel, InboundMessage


@dataclass
class VKLongPollServer:
    server: str
    key: str
    ts: str


def is_vk_message_new(update: dict[str, Any]) -> bool:
    return update.get("type") == "message_new" and isinstance((update.get("object") or {}).get("message"), dict)


def vk_inbound_message(update: dict[str, Any]) -> InboundMessage:
    message = (update.get("object") or {}).get("message") or {}
    peer_id = str(message.get("peer_id") or message.get("from_id") or "")
    from_id = str(message.get("from_id") or peer_id)
    message_id = str(message.get("id") or message.get("conversation_message_id") or "")
    return InboundMessage(
        channel=Channel.VK,
        client_id=from_id,
        chat_id=peer_id,
        message_id=message_id,
        text=str(message.get("text") or "").strip(),
        created_at=_int(message.get("date")),
        has_photo=_has_photo(message),
        metadata={"peer_id": peer_id, "from_id": from_id, "raw": update},
    )


def _has_photo(message: dict[str, Any]) -> bool:
    return any(isinstance(item, dict) and item.get("type") == "photo" for item in message.get("attachments") or [])


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
