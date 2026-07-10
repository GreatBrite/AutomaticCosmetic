from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Protocol

from ..telegram import TelegramBot
from .handoff_refs import DEFAULT_HANDOFF_REFS_PATH, latest_unresolved_handoff_ref_for_chat, remember_telegram_handoff_ref
from .avito_identity import client_display_name, dialog_ref
from .config import IntegrationSettings
from .models import Handoff


class HandoffNotifier(Protocol):
    async def notify(self, handoff: Handoff) -> dict[str, Any]:
        ...

    async def notify_text(self, text: str) -> dict[str, Any]:
        ...


class PreviewHandoffNotifier:
    """Writes human handoffs to a local outbox until live Telegram notifications are enabled."""

    def __init__(self, path: Path | str = Path("data/handoff_outbox.jsonl")) -> None:
        self.path = Path(path)

    async def notify(self, handoff: Handoff) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        photo_urls = _handoff_photo_urls(handoff)
        photo_ids = _handoff_photo_ids(handoff)
        media_urls = _handoff_media_urls(handoff)
        media_ids = _handoff_media_ids(handoff)
        handoff_text = format_handoff_message(handoff)
        row = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": handoff.reason.value,
            "summary": handoff.summary,
            "message": _message_data(handoff),
            "photo_urls": photo_urls,
            "photo_ids": photo_ids,
            "media_urls": media_urls,
            "media_ids": media_ids,
            "text": handoff_text,
        }
        await asyncio.to_thread(_append_jsonl, self.path, row)
        return {
            "sent": False,
            "reason": "preview_only",
            "outbox": str(self.path),
            "photo_count": len(photo_urls),
            "photo_id_count": len(photo_ids),
            "media_count": len(media_urls),
            "media_id_count": len(media_ids),
            "text": handoff_text,
        }

    async def notify_text(self, text: str) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"created_at": datetime.now(timezone.utc).isoformat(), "type": "operations_notification", "text": text}
        await asyncio.to_thread(_append_jsonl, self.path, row)
        return {"sent": False, "reason": "preview_only", "outbox": str(self.path), "text": text}


class TelegramHandoffNotifier:
    def __init__(
        self,
        bot: TelegramBot,
        chat_id: str,
        media_dir: Path | str = Path("data/avito_photos"),
        ref_path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.media_dir = Path(media_dir)
        self.ref_path = Path(ref_path)

    async def notify(self, handoff: Handoff) -> dict[str, Any]:
        handoff_text = format_handoff_message(handoff)
        text = escape(handoff_text)
        existing_ref = await asyncio.to_thread(
            latest_unresolved_handoff_ref_for_chat,
            handoff.message.chat_id,
            telegram_chat_id=self.chat_id,
            path=self.ref_path,
        )
        if existing_ref:
            merged = await self._merge_into_existing_handoff(handoff, handoff_text, text, existing_ref)
            if merged.get("merged"):
                return merged

        notify_error = ""
        try:
            result = await _to_thread_retry(self.bot.send_message, self.chat_id, text)
        except Exception as exc:
            result = {"ok": False, "error": repr(exc)}
            notify_error = repr(exc)
        telegram_message_id = _telegram_message_id(result) if isinstance(result, dict) else ""
        ref = {}
        if telegram_message_id:
            ref = await asyncio.to_thread(
                remember_telegram_handoff_ref,
                telegram_chat_id=self.chat_id,
                telegram_message_id=telegram_message_id,
                avito_chat_id=handoff.message.chat_id,
                client_name=str((handoff.message.metadata or {}).get("client_name") or ""),
                handoff_text=handoff_text,
                source_message_id=str(handoff.message.message_id or ""),
                path=self.ref_path,
            )
        photo_results, photo_errors = await self._send_handoff_photos(handoff)
        media_results, media_errors = await self._send_handoff_media(handoff)
        return {
            "sent": not notify_error,
            "error": notify_error,
            "telegram": result,
            "telegram_handoff_ref": ref,
            "photos_sent": len(photo_results),
            "photos_failed": len(photo_errors),
            "media_sent": len(media_results),
            "media_failed": len(media_errors),
            "photo_results": photo_results,
            "photo_errors": photo_errors,
            "media_results": media_results,
            "media_errors": media_errors,
            "text": handoff_text,
        }

    async def _send_handoff_photos(self, handoff: Handoff) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        photo_results: list[dict[str, Any]] = []
        photo_errors: list[dict[str, Any]] = []
        for index, photo_url in enumerate(_handoff_photo_urls(handoff), start=1):
            try:
                photo_path = await _to_thread_retry(_download_photo_url, photo_url, self.media_dir)
                dialog_line = _dialog_line(handoff.message)
                caption = escape(f"Фото из {handoff.message.channel.value}, {dialog_line[:1].lower() + dialog_line[1:]}") if index == 1 else None
                send_result = await _to_thread_retry(self.bot.send_photo, self.chat_id, photo_path, caption)
            except Exception as exc:
                photo_errors.append({"url_hash": _url_hash(photo_url), "error": repr(exc), "index": index})
                continue
            photo_results.append({"path": str(photo_path), "telegram": send_result, "index": index})
        return photo_results, photo_errors

    async def _send_handoff_media(self, handoff: Handoff) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        media_results: list[dict[str, Any]] = []
        media_errors: list[dict[str, Any]] = []
        photo_urls = set(_handoff_photo_urls(handoff))
        for index, media_url in enumerate([url for url in _handoff_media_urls(handoff) if url not in photo_urls], start=1):
            try:
                media_path = await _to_thread_retry(_download_photo_url, media_url, self.media_dir)
                dialog_line = _dialog_line(handoff.message)
                caption = escape(f"Вложение из {handoff.message.channel.value}, {dialog_line[:1].lower() + dialog_line[1:]}") if index == 1 else None
                send_result = await _to_thread_retry(self.bot.send_document, self.chat_id, media_path, caption)
            except Exception as exc:
                media_errors.append({"url_hash": _url_hash(media_url), "error": repr(exc), "index": index})
                continue
            media_results.append({"path": str(media_path), "telegram": send_result, "index": index})
        return media_results, media_errors

    async def _merge_into_existing_handoff(
        self,
        handoff: Handoff,
        handoff_text: str,
        escaped_text: str,
        existing_ref: dict[str, Any],
    ) -> dict[str, Any]:
        telegram_message_id = str(existing_ref.get("telegram_message_id") or "").strip()
        handoff_id = str(existing_ref.get("handoff_id") or "").strip()
        if not telegram_message_id or not handoff_id:
            return {"merged": False, "reason": "missing_existing_ref"}
        try:
            parsed_message_id = int(telegram_message_id)
        except ValueError:
            return {"merged": False, "reason": "invalid_existing_message_id"}
        try:
            edit_result = await _to_thread_retry(self.bot.edit_message_text, self.chat_id, parsed_message_id, escaped_text)
        except Exception as exc:
            return {"merged": False, "reason": "edit_failed", "error": repr(exc)}
        ref = await asyncio.to_thread(
            remember_telegram_handoff_ref,
            telegram_chat_id=self.chat_id,
            telegram_message_id=telegram_message_id,
            avito_chat_id=handoff.message.chat_id,
            client_name=str((handoff.message.metadata or {}).get("client_name") or ""),
            handoff_text=handoff_text,
            handoff_id=handoff_id,
            source_message_id=str(handoff.message.message_id or ""),
            status=str(existing_ref.get("status") or "open"),
            path=self.ref_path,
        )
        photo_results, photo_errors = await self._send_handoff_photos(handoff)
        media_results, media_errors = await self._send_handoff_media(handoff)
        return {
            "sent": False,
            "merged": True,
            "reason": "merged_existing_handoff",
            "telegram": edit_result,
            "telegram_handoff_ref": ref,
            "photos_sent": len(photo_results),
            "photos_failed": len(photo_errors),
            "media_sent": len(media_results),
            "media_failed": len(media_errors),
            "photo_results": photo_results,
            "photo_errors": photo_errors,
            "media_results": media_results,
            "media_errors": media_errors,
            "text": handoff_text,
        }

    async def notify_text(self, text: str) -> dict[str, Any]:
        try:
            result = await _to_thread_retry(self.bot.send_message, self.chat_id, escape(text))
            return {"sent": True, "telegram": result, "text": text}
        except Exception as exc:
            return {"sent": False, "error": repr(exc), "text": text}


def handoff_notifier_from_settings(settings: IntegrationSettings) -> HandoffNotifier:
    if settings.handoff_notify_ready:
        return TelegramHandoffNotifier(TelegramBot(settings.telegram_admin_bot_token), settings.handoff_notify_chat_id)
    return PreviewHandoffNotifier()


def format_handoff_message(handoff: Handoff) -> str:
    message = handoff.message
    listing = message.listing
    lines = [
        "Нужна ручная консультация",
        f"Причина: {handoff.reason.value}",
        f"Канал: {message.channel.value}",
        _dialog_line(message),
    ]
    if listing and listing.has_listing:
        listing_parts = [part for part in (listing.title, listing.price_string, listing.city) if part]
        lines.append(f"Объявление: {' | '.join(listing_parts)}")
    if message.text:
        lines.append(f"Сообщение: {message.text}")
    if handoff.summary:
        lines.append(f"Контекст: {handoff.summary}")
    photo_urls = _handoff_photo_urls(handoff)
    photo_ids = _handoff_photo_ids(handoff)
    media_urls = [url for url in _handoff_media_urls(handoff) if url not in set(photo_urls)]
    media_ids = _handoff_media_ids(handoff)
    media_types = _handoff_media_types(handoff)
    if photo_urls:
        lines.append(f"Фото: будет переслано в Telegram ({len(photo_urls)} шт.)")
    if media_urls:
        media_label = "видео/файл" if any(item in {"video", "file"} for item in media_types) else "вложение"
        lines.append(f"Медиа: {media_label} будет переслано в Telegram ({len(media_urls)} шт.)")
    elif message.has_photo and not photo_urls:
        ids = media_ids or photo_ids
        suffix = f" id: {', '.join(ids)}" if ids else ""
        media_label = "видео/файл" if any(item in {"video", "file"} for item in media_types) else "фото/вложение"
        lines.append(f"Медиа: клиент прислал {media_label}, но URL для пересылки не пришёл в webhook.{suffix}")
    return "\n".join(lines)


def _dialog_line(message: Any) -> str:
    name = client_display_name(message.chat_id, str((message.metadata or {}).get("client_name") or ""))
    if name:
        return f"Клиент: {name}"
    return f"Диалог: {dialog_ref(message.chat_id)}"


def _message_data(handoff: Handoff) -> dict[str, Any]:
    data = asdict(handoff.message)
    if handoff.message.listing:
        data["listing"] = asdict(handoff.message.listing)
    return data


def _telegram_message_id(send_response: dict[str, Any]) -> str:
    result = send_response.get("result") if isinstance(send_response.get("result"), dict) else {}
    return str(result.get("message_id") or send_response.get("message_id") or "").strip()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _handoff_photo_urls(handoff: Handoff) -> list[str]:
    raw = handoff.message.metadata.get("photo_urls") or []
    return [str(url) for url in raw if str(url).startswith(("http://", "https://"))]


def _handoff_photo_ids(handoff: Handoff) -> list[str]:
    raw = handoff.message.metadata.get("photo_ids") or []
    return [str(item) for item in raw if str(item)]


def _handoff_media_urls(handoff: Handoff) -> list[str]:
    raw = handoff.message.metadata.get("media_urls") or []
    return [str(url) for url in raw if str(url).startswith(("http://", "https://"))]


def _handoff_media_ids(handoff: Handoff) -> list[str]:
    raw = handoff.message.metadata.get("media_ids") or []
    return [str(item) for item in raw if str(item)]


def _handoff_media_types(handoff: Handoff) -> list[str]:
    raw = handoff.message.metadata.get("media_types") or []
    return [str(item) for item in raw if str(item)]


def _download_photo_url(photo_url: str, media_dir: Path) -> Path:
    media_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:16]
    request = urllib.request.Request(photo_url, headers={"User-Agent": "AutomaticCosmetic/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
        content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0].strip()
    suffix = mimetypes.guess_extension(content_type) or ".jpg"
    path = media_dir / f"avito_{digest}{suffix}"
    path.write_bytes(content)
    return path


async def _to_thread_retry(func: Any, *args: Any, attempts: int = 3, delay_seconds: float = 1.0) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            await asyncio.to_thread(time.sleep, delay_seconds * attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError("retry failed without exception")


def _url_hash(photo_url: str) -> str:
    return hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:16]
