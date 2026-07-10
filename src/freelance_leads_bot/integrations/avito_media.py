from __future__ import annotations

from dataclasses import replace
import re
from typing import Any, Protocol

from .avito_consultant import AvitoConsultantReply
from .config import IntegrationSettings
from .models import Handoff, InboundMessage


class AvitoPhotoResolver(Protocol):
    async def photo_urls(self, account_id: int, chat_id: str, message_id: str = "") -> list[str]:
        ...


class AvitoApiPhotoResolver:
    def __init__(self, settings: IntegrationSettings | None = None, client: Any | None = None) -> None:
        self.settings = settings or IntegrationSettings.from_env()
        self.client = client or self._build_client()

    def _build_client(self) -> Any:
        try:
            from pyavitoapi.client import AvitoAsyncClient
        except ImportError as exc:
            raise RuntimeError("pyavitoapi is not installed") from exc
        return AvitoAsyncClient(
            client_id=self.settings.avito_client_id,
            client_secret=self.settings.avito_client_secret,
        )

    async def photo_urls(self, account_id: int, chat_id: str, message_id: str = "") -> list[str]:
        if not account_id or not chat_id:
            return []
        async with self.client as client:
            headers = await client.auth.auth_header()
            payload = await client._transport.request(
                method="GET",
                path_template="/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
                path_params={"user_id": account_id, "chat_id": chat_id},
                headers=headers,
            )
        messages = _messages_from_payload(payload)
        if message_id:
            wanted = {item for item in str(message_id).split(",") if item}
            matched = [message for message in messages if str(message.get("id") or "") in wanted]
            messages = matched or messages
        return _unique(_photo_urls_from_any(messages))


async def enrich_reply_handoff_photos(
    reply: AvitoConsultantReply,
    *,
    resolver: AvitoPhotoResolver | None,
    account_id: int,
) -> AvitoConsultantReply:
    if not reply.handoff or not resolver:
        return reply
    message = reply.handoff.message
    if not message.has_photo or message.metadata.get("photo_urls"):
        return reply
    try:
        photo_urls = await resolver.photo_urls(account_id, message.chat_id, message.message_id)
    except Exception as exc:
        photo_urls = []
        error = type(exc).__name__
    else:
        error = ""
    metadata = {**message.metadata}
    if photo_urls:
        metadata["photo_urls"] = photo_urls
    if error:
        metadata["photo_resolve_error"] = error
    enriched_message = replace(message, metadata=metadata)
    enriched_handoff = replace(reply.handoff, message=enriched_message)
    return replace(reply, handoff=enriched_handoff)


def _messages_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "items", "result", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _messages_from_payload(value)
            if nested:
                return nested
    return [payload] if payload.get("id") or payload.get("content") else []


def _photo_urls_from_any(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    urls: list[str] = []
    if isinstance(value, dict):
        best_size_url = _best_size_url(value.get("sizes"))
        if best_size_url:
            urls.append(best_size_url)
        for key, raw in value.items():
            if key == "sizes" and best_size_url:
                continue
            if key in {"url", "src", "link", "image_url", "photo_url"} and isinstance(raw, str):
                if raw.startswith(("http://", "https://")):
                    urls.append(raw)
            else:
                urls.extend(_photo_urls_from_any(raw))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_photo_urls_from_any(item))
    return urls


def _best_size_url(sizes: Any) -> str:
    candidates: list[tuple[int, str]] = []
    if isinstance(sizes, dict):
        for key, value in sizes.items():
            for url in _photo_urls_from_any(value):
                candidates.append((_size_score(str(key)), url))
    elif isinstance(sizes, list):
        for index, value in enumerate(sizes):
            for url in _photo_urls_from_any(value):
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
