from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Protocol

from ..telegram import TelegramBot
from .booking_flow import extract_date, extract_time
from .handoff_refs import (
    DEFAULT_HANDOFF_REFS_PATH,
    UNRESOLVED_HANDOFF_STATUSES,
    handoff_ref_is_critical,
    latest_unresolved_handoff_ref_for_chat,
    load_telegram_handoff_refs,
    remember_telegram_handoff_ref,
    save_telegram_handoff_refs,
    update_handoff_status,
)
from .avito_identity import client_display_name, dialog_ref
from .config import IntegrationSettings
from .models import Handoff
from .telegram_client_topics import (
    DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH,
    client_topic_key,
    get_or_create_client_topic,
    load_client_topics,
    topic_params_from_row,
    topic_request_from_avito_followup,
    topic_title_for_client,
)


class HandoffNotifier(Protocol):
    async def notify(self, handoff: Handoff) -> dict[str, Any]:
        ...

    async def notify_text(
        self,
        text: str,
        *,
        reply_markup: dict | None = None,
        topic_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        ...

    async def notify_photo_url(
        self,
        photo_url: str,
        *,
        caption: str = "",
        topic_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        ...

    async def notify_avito_followup(self, row: dict[str, Any], text: str, *, reply_markup: dict | None = None) -> dict[str, Any]:
        ...


HANDOFF_FOLLOWUP_ACTIONS = {"done", "stale", "later"}


def handoff_followup_token(handoff_id: str) -> str:
    return hashlib.sha256(str(handoff_id or "").encode("utf-8")).hexdigest()[:12]


def handoff_followup_keyboard(ref: dict[str, Any]) -> dict[str, list[list[dict[str, str]]]]:
    token = handoff_followup_token(str(ref.get("handoff_id") or ""))
    return {
        "inline_keyboard": [
            [
                {"text": "Закрыто", "callback_data": f"hfu:{token}:done"},
                {"text": "Не актуально", "callback_data": f"hfu:{token}:stale"},
            ],
            [
                {"text": "Напомнить позже", "callback_data": f"hfu:{token}:later"},
            ],
        ]
    }


def parse_handoff_followup_callback(data: str) -> tuple[str, str] | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "hfu":
        return None
    token, action = parts[1].strip(), parts[2].strip()
    if not token or action not in HANDOFF_FOLLOWUP_ACTIONS:
        return None
    return token, action


def apply_handoff_followup_action(
    *,
    ref_path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
    token: str,
    action: str,
    actor: str = "telegram_admin",
    now: int | None = None,
) -> dict[str, Any]:
    parsed = parse_handoff_followup_callback(f"hfu:{token}:{action}")
    if parsed is None:
        return {"ok": False, "reason": "invalid_callback"}
    token, action = parsed
    now_ts = int(time.time()) if now is None else int(now)
    refs = load_telegram_handoff_refs(ref_path)
    ref = _find_handoff_ref_by_followup_token(refs, token)
    if not ref:
        return {"ok": False, "reason": "handoff_not_found", "token": token, "action": action}
    handoff_id = str(ref.get("handoff_id") or "").strip()
    if action == "done":
        ok = update_handoff_status(
            handoff_id,
            "closed_manual",
            resolution_note=f"Закрыто кнопкой Telegram reminder ({actor})",
            resolution_source="telegram_handoff_followup",
            resolution_action="done",
            path=ref_path,
        )
    elif action == "stale":
        ok = update_handoff_status(
            handoff_id,
            "not_relevant",
            resolution_note=f"Не актуально по кнопке Telegram reminder ({actor})",
            resolution_source="telegram_handoff_followup",
            resolution_action="stale",
            path=ref_path,
        )
    else:
        ok = _snooze_handoff_followup(refs, handoff_id, now=now_ts, ref_path=ref_path, actor=actor)
    updated = _find_handoff_ref_by_followup_token(load_telegram_handoff_refs(ref_path), token) if ok else ref
    return {"ok": bool(ok), "action": action, "token": token, "handoff_id": handoff_id, "ref": updated or ref}


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

    async def notify_text(
        self,
        text: str,
        *,
        reply_markup: dict | None = None,
        topic_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "type": "operations_notification",
            "text": text,
            "reply_markup": reply_markup or {},
            "topic_params": topic_params or {},
        }
        await asyncio.to_thread(_append_jsonl, self.path, row)
        return {
            "sent": False,
            "reason": "preview_only",
            "outbox": str(self.path),
            "text": text,
            "reply_markup": reply_markup or {},
            "topic_params": topic_params or {},
        }

    async def notify_photo_url(
        self,
        photo_url: str,
        *,
        caption: str = "",
        topic_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "type": "operations_photo",
            "photo_url": photo_url,
            "caption": caption,
            "topic_params": topic_params or {},
        }
        await asyncio.to_thread(_append_jsonl, self.path, row)
        return {
            "sent": False,
            "reason": "preview_only",
            "outbox": str(self.path),
            "photo_url": photo_url,
            "caption": caption,
            "topic_params": topic_params or {},
        }

    async def notify_avito_followup(self, row: dict[str, Any], text: str, *, reply_markup: dict | None = None) -> dict[str, Any]:
        return await self.notify_text(text, reply_markup=reply_markup)


class TelegramHandoffNotifier:
    def __init__(
        self,
        bot: TelegramBot,
        chat_id: str,
        media_dir: Path | str = Path("data/avito_photos"),
        ref_path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
        topics_enabled: bool = True,
        topics_path: Path | str = DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.media_dir = Path(media_dir)
        self.ref_path = Path(ref_path)
        self.topics_enabled = topics_enabled
        self.topics_path = Path(topics_path)

    async def notify(self, handoff: Handoff) -> dict[str, Any]:
        handoff_text = format_handoff_message(handoff)
        text = escape(handoff_text)
        urgent = _is_booking_critical(handoff)
        topic_result = await self._topic_for_handoff(handoff)
        topic_params = dict(topic_result.get("topic_params") or {})
        if not urgent:
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

        result, topic_params, topic_fallback = await _send_message_with_topic_fallback(
            self.bot,
            self.chat_id,
            text,
            topic_params=topic_params,
        )
        notify_error = "" if _telegram_delivery_ok(result) else _telegram_send_error(result, "telegram_message_not_delivered")
        telegram_message_id = _telegram_message_id(result) if isinstance(result, dict) else ""
        ref = {}
        if telegram_message_id:
            task_fields = _booking_task_fields(handoff) if urgent else _handoff_task_fields(handoff)
            reason = str(getattr(handoff.reason, "value", handoff.reason))
            is_critical = _handoff_is_critical(handoff)
            ref = await asyncio.to_thread(
                remember_telegram_handoff_ref,
                telegram_chat_id=self.chat_id,
                telegram_message_id=telegram_message_id,
                avito_chat_id=handoff.message.chat_id,
                telegram_message_thread_id=topic_params.get("message_thread_id", ""),
                account_id=str((handoff.message.metadata or {}).get("account_id") or ""),
                client_name=str((handoff.message.metadata or {}).get("client_name") or ""),
                handoff_text=handoff_text,
                source_message_id=str(handoff.message.message_id or ""),
                reason=reason,
                urgency="critical" if is_critical else "",
                sla="critical" if is_critical else "",
                client_waits_for=str(task_fields.get("client_waits_for") or ""),
                deadline_at=int(time.time()) + 60 * 60 if is_critical else 0,
                escalation_at=int(time.time()) + 3 * 60 * 60 if is_critical else 0,
                phone=str(task_fields.get("phone") or ""),
                city=str(task_fields.get("city") or ""),
                service=str(task_fields.get("service") or ""),
                booking_date=str(task_fields.get("booking_date") or ""),
                booking_time=str(task_fields.get("booking_time") or ""),
                confirmation_needed=str(task_fields.get("confirmation_needed") or ""),
                assignee=str(task_fields.get("assignee") or ""),
                path=self.ref_path,
            )
        photo_results, photo_errors, photo_statuses = await self._send_handoff_photos(handoff, topic_params=topic_params)
        media_results, media_errors, media_statuses = await self._send_handoff_media(handoff, topic_params=topic_params)
        attachment_statuses = photo_statuses + media_statuses
        if telegram_message_id and attachment_statuses:
            await asyncio.to_thread(_store_ref_media_statuses, self.chat_id, telegram_message_id, attachment_statuses, self.ref_path)
        failure_notify = await self._notify_media_failures(handoff, photo_errors + media_errors)
        return {
            "sent": bool(telegram_message_id) and not notify_error,
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
            "media_statuses": attachment_statuses,
            "media_failure_notify": failure_notify,
            "topic": topic_result,
            "topic_params": topic_params,
            "topic_fallback": topic_fallback,
            "text": handoff_text,
        }

    async def _send_handoff_photos(
        self,
        handoff: Handoff,
        *,
        topic_params: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        photo_results: list[dict[str, Any]] = []
        photo_errors: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        for index, photo_url in enumerate(_handoff_photo_urls(handoff), start=1):
            status = {"kind": "photo", "index": index, "url_hash": _url_hash(photo_url), "status": "received"}
            try:
                photo_path = await _to_thread_retry(_download_photo_url, photo_url, self.media_dir)
                status.update({"status": "downloaded", "path": str(photo_path)})
                dialog_line = _dialog_line(handoff.message)
                caption = escape(f"Фото из {handoff.message.channel.value}, {dialog_line[:1].lower() + dialog_line[1:]}") if index == 1 else None
                send_result = await _to_thread_retry(lambda: self.bot.send_photo(self.chat_id, photo_path, caption, **(topic_params or {})))
            except Exception as exc:
                status.update({"status": "manual_avito_check_required", "download_failed": True, "error": repr(exc)})
                photo_errors.append({"kind": "photo", "url_hash": _url_hash(photo_url), "status": "manual_avito_check_required", "error": repr(exc), "index": index})
                statuses.append(status)
                continue
            status.update({"status": "sent_to_olga"})
            statuses.append(status)
            photo_results.append({"path": str(photo_path), "telegram": send_result, "index": index})
        return photo_results, photo_errors, statuses

    async def _send_handoff_media(
        self,
        handoff: Handoff,
        *,
        topic_params: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        media_results: list[dict[str, Any]] = []
        media_errors: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        photo_urls = set(_handoff_photo_urls(handoff))
        for index, media_url in enumerate([url for url in _handoff_media_urls(handoff) if url not in photo_urls], start=1):
            status = {"kind": "media", "index": index, "url_hash": _url_hash(media_url), "status": "received"}
            try:
                media_path = await _to_thread_retry(_download_photo_url, media_url, self.media_dir)
                status.update({"status": "downloaded", "path": str(media_path)})
                dialog_line = _dialog_line(handoff.message)
                caption = escape(f"Вложение из {handoff.message.channel.value}, {dialog_line[:1].lower() + dialog_line[1:]}") if index == 1 else None
                send_result = await _to_thread_retry(lambda: self.bot.send_document(self.chat_id, media_path, caption, **(topic_params or {})))
            except Exception as exc:
                status.update({"status": "manual_avito_check_required", "download_failed": True, "error": repr(exc)})
                media_errors.append({"kind": "media", "url_hash": _url_hash(media_url), "status": "manual_avito_check_required", "error": repr(exc), "index": index})
                statuses.append(status)
                continue
            status.update({"status": "sent_to_olga"})
            statuses.append(status)
            media_results.append({"path": str(media_path), "telegram": send_result, "index": index})
        return media_results, media_errors, statuses

    async def _notify_media_failures(self, handoff: Handoff, errors: list[dict[str, Any]]) -> dict[str, Any]:
        if not errors:
            return {}
        text = (
            "СРОЧНО: не удалось переслать вложение из Avito после повторных попыток\n"
            f"{_dialog_line(handoff.message)}\n"
            f"Ошибок: {len(errors)}. Нужно открыть диалог вручную и проверить вложение."
        )
        return await self.notify_text(text)

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
        if not _telegram_delivery_ok(edit_result):
            return {"merged": False, "reason": "edit_not_delivered", "error": _telegram_send_error(edit_result, "telegram_edit_not_delivered")}
        is_critical = _handoff_is_critical(handoff)
        task_fields = _handoff_task_fields(handoff)
        ref = await asyncio.to_thread(
            remember_telegram_handoff_ref,
            telegram_chat_id=self.chat_id,
            telegram_message_id=telegram_message_id,
            avito_chat_id=handoff.message.chat_id,
            telegram_message_thread_id=str(existing_ref.get("telegram_message_thread_id") or ""),
            account_id=str((handoff.message.metadata or {}).get("account_id") or existing_ref.get("account_id") or ""),
            client_name=str((handoff.message.metadata or {}).get("client_name") or ""),
            handoff_text=handoff_text,
            handoff_id=handoff_id,
            source_message_id=str(handoff.message.message_id or ""),
            status=str(existing_ref.get("status") or "open"),
            reason=str(getattr(handoff.reason, "value", handoff.reason)),
            urgency="critical" if is_critical else str(existing_ref.get("urgency") or ""),
            sla="critical" if is_critical else str(existing_ref.get("sla") or ""),
            client_waits_for=str(task_fields.get("client_waits_for") or existing_ref.get("client_waits_for") or ""),
            deadline_at=int(time.time()) + 60 * 60 if is_critical else int(existing_ref.get("deadline_at") or 0),
            escalation_at=int(time.time()) + 3 * 60 * 60 if is_critical else int(existing_ref.get("escalation_at") or 0),
            path=self.ref_path,
        )
        topic_params = {"message_thread_id": str(existing_ref.get("telegram_message_thread_id") or "")} if existing_ref.get("telegram_message_thread_id") else {}
        if not topic_params:
            topic_result = await self._topic_for_handoff(handoff)
            topic_params = dict(topic_result.get("topic_params") or {})
        photo_results, photo_errors, photo_statuses = await self._send_handoff_photos(handoff, topic_params=topic_params)
        media_results, media_errors, media_statuses = await self._send_handoff_media(handoff, topic_params=topic_params)
        attachment_statuses = photo_statuses + media_statuses
        if attachment_statuses:
            await asyncio.to_thread(_store_ref_media_statuses, self.chat_id, telegram_message_id, attachment_statuses, self.ref_path)
        failure_notify = await self._notify_media_failures(handoff, photo_errors + media_errors)
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
            "media_statuses": attachment_statuses,
            "media_failure_notify": failure_notify,
            "topic_params": topic_params,
            "text": handoff_text,
        }

    async def notify_text(
        self,
        text: str,
        *,
        reply_markup: dict | None = None,
        topic_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        result, used_topic_params, topic_fallback = await _send_message_with_topic_fallback(
            self.bot,
            self.chat_id,
            escape(text),
            topic_params=topic_params,
            reply_markup=reply_markup,
        )
        sent = _telegram_delivery_ok(result)
        return {
            "sent": sent,
            "error": "" if sent else _telegram_send_error(result, "telegram_message_not_delivered"),
            "telegram": result,
            "text": text,
            "reply_markup": reply_markup or {},
            "topic_params": used_topic_params,
            "topic_fallback": topic_fallback,
        }

    async def notify_photo_url(
        self,
        photo_url: str,
        *,
        caption: str = "",
        topic_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = await _to_thread_retry(
                lambda: self.bot.send_photo_url(self.chat_id, photo_url, escape(caption) if caption else None, **(topic_params or {}))
            )
            sent = _telegram_delivery_ok(result)
            return {
                "sent": sent,
                "error": "" if sent else _telegram_send_error(result, "telegram_photo_not_delivered"),
                "telegram": result,
                "photo_url": photo_url,
                "caption": caption,
                "topic_params": topic_params or {},
            }
        except Exception as url_exc:
            try:
                path = await asyncio.to_thread(_download_photo_url, photo_url, self.media_dir)
                result = await _to_thread_retry(
                    lambda: self.bot.send_photo(self.chat_id, path, escape(caption) if caption else None, **(topic_params or {}))
                )
                sent = _telegram_delivery_ok(result)
                return {
                    "sent": sent,
                    "error": "" if sent else _telegram_send_error(result, "telegram_photo_not_delivered"),
                    "telegram": result,
                    "photo_url": photo_url,
                    "caption": caption,
                    "topic_params": topic_params or {},
                    "fallback": "download_upload",
                    "url_error": repr(url_exc),
                }
            except Exception as exc:
                return {
                    "sent": False,
                    "error": repr(exc),
                    "url_error": repr(url_exc),
                    "photo_url": photo_url,
                    "caption": caption,
                    "topic_params": topic_params or {},
                }

    async def notify_avito_followup(self, row: dict[str, Any], text: str, *, reply_markup: dict | None = None) -> dict[str, Any]:
        topic_result = await self._topic_for_avito_followup(row)
        topic_params = dict(topic_result.get("topic_params") or {})
        if not topic_params:
            return {
                "sent": False,
                "reason": "missing_valid_topic",
                "text": text,
                "reply_markup": reply_markup or {},
                "topic": topic_result,
                "topic_params": {},
            }
        result = await self.notify_text(text, reply_markup=reply_markup, topic_params=topic_params)
        result["topic"] = topic_result
        return result

    async def _topic_for_handoff(self, handoff: Handoff) -> dict[str, Any]:
        message = handoff.message
        listing = message.listing
        metadata = message.metadata or {}
        client_name = str(metadata.get("client_name") or metadata.get("name") or message.client_id or "").strip()
        external_chat_id = str(message.chat_id or message.client_id or "").strip()
        key = client_topic_key(
            channel=message.channel.value,
            account_id=str(metadata.get("account_id") or ""),
            external_chat_id=external_chat_id,
            client_id=message.client_id,
        )
        title = topic_title_for_client(
            client_name=client_name,
            channel=message.channel.value,
            city=str((listing.city if listing else "") or metadata.get("city") or ""),
            listing_title=str((listing.title if listing else "") or metadata.get("listing_title") or ""),
            external_chat_id=external_chat_id,
        )
        return await asyncio.to_thread(
            get_or_create_client_topic,
            self.bot,
            self.chat_id,
            key=key,
            title=title,
            channel=message.channel.value,
            external_chat_id=external_chat_id,
            account_id=str(metadata.get("account_id") or ""),
            client_name=client_name,
            listing_title=str((listing.title if listing else "") or metadata.get("listing_title") or ""),
            city=str((listing.city if listing else "") or metadata.get("city") or ""),
            enabled=self.topics_enabled,
            path=self.topics_path,
        )

    async def _topic_for_avito_followup(self, row: dict[str, Any]) -> dict[str, Any]:
        request = topic_request_from_avito_followup(row)
        return await asyncio.to_thread(
            get_or_create_client_topic,
            self.bot,
            self.chat_id,
            enabled=self.topics_enabled,
            path=self.topics_path,
            **request,
        )

    async def topic_for_handoff_ref(self, ref: dict[str, Any]) -> dict[str, Any]:
        invalid_thread_ids = _invalid_handoff_thread_ids(ref)
        topic_params = _handoff_sla_topic_params(ref)
        if topic_params:
            return {"ok": True, "created": False, "reason": "handoff_ref_thread", "topic_params": topic_params}
        existing = await asyncio.to_thread(
            _find_client_topic_for_handoff_ref,
            ref,
            self.chat_id,
            self.topics_path,
            invalid_thread_ids,
        )
        if existing:
            return {
                "ok": True,
                "created": False,
                "reason": "existing_client_topic",
                "topic": existing,
                "topic_params": topic_params_from_row(existing),
            }
        request = _topic_request_from_handoff_ref(ref)
        if not request:
            return {"ok": False, "reason": "missing_handoff_topic_key", "topic_params": {}}
        return await asyncio.to_thread(
            get_or_create_client_topic,
            self.bot,
            self.chat_id,
            enabled=self.topics_enabled,
            path=self.topics_path,
            invalid_thread_ids=invalid_thread_ids,
            **request,
        )


async def process_handoff_sla(
    notifier: HandoffNotifier,
    *,
    ref_path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
    now: int | None = None,
    reminder_after_seconds: int = 6 * 60 * 60,
    escalation_after_seconds: int = 12 * 60 * 60,
    expire_after_seconds: int = 7 * 24 * 60 * 60,
    reminder_repeat_seconds: int = 6 * 60 * 60,
    escalation_repeat_seconds: int = 6 * 60 * 60,
    max_notifications: int | None = None,
) -> dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    refs = await asyncio.to_thread(load_telegram_handoff_refs, ref_path)
    reminders = 0
    escalations = 0
    expired = 0
    expired_critical = 0
    deduped = 0
    notifications: list[dict[str, Any]] = []
    changed = False
    delivered_notifications = 0
    candidates, deduped = _canonical_handoff_sla_refs(refs, now=now)
    for ref in candidates:
        if max_notifications is not None and delivered_notifications >= max(0, int(max_notifications)):
            break
        if not isinstance(ref, dict):
            continue
        status = str(ref.get("status") or "open")
        if status not in UNRESOLVED_HANDOFF_STATUSES:
            continue
        created_at = int(ref.get("created_at") or ref.get("updated_at") or now)
        age = max(0, now - created_at)
        if age >= expire_after_seconds:
            if handoff_ref_is_critical(ref):
                ref["status"] = "expired_critical"
                ref["expired_at"] = now
                expired_critical += 1
            else:
                ref["status"] = "expired"
                ref["closed_at"] = now
                expired += 1
            ref["updated_at"] = now
            changed = True
            continue
        escalation_sent_at = int(ref.get("escalation_sent_at") or 0)
        can_send_escalation = (
            handoff_ref_is_critical(ref)
            and age >= escalation_after_seconds
            and (not escalation_sent_at or now - escalation_sent_at >= max(1, int(escalation_repeat_seconds or 1)))
        )
        if can_send_escalation:
            result, invalidated = await _send_handoff_sla_notification(notifier, ref, event="escalation", now=now)
            notifications.append(result)
            changed = changed or invalidated
            if _notification_delivered(result):
                delivered_notifications += 1
                used_topic_params = result.get("topic_params") if isinstance(result, dict) else {}
                ref["telegram_message_thread_id"] = str((used_topic_params or {}).get("message_thread_id") or "")
                ref["escalation_sent_at"] = now
                ref["escalation_count"] = int(ref.get("escalation_count") or 0) + 1
                ref["updated_at"] = now
                escalations += 1
                changed = True
            continue
        reminder_sent_at = int(ref.get("reminder_sent_at") or 0)
        can_send_reminder = age >= reminder_after_seconds and (
            not reminder_sent_at or now - reminder_sent_at >= max(1, int(reminder_repeat_seconds or 1))
        )
        if can_send_reminder:
            result, invalidated = await _send_handoff_sla_notification(notifier, ref, event="reminder", now=now)
            notifications.append(result)
            changed = changed or invalidated
            if _notification_delivered(result):
                delivered_notifications += 1
                used_topic_params = result.get("topic_params") if isinstance(result, dict) else {}
                ref["telegram_message_thread_id"] = str((used_topic_params or {}).get("message_thread_id") or "")
                ref["reminder_sent_at"] = now
                ref["reminder_count"] = int(ref.get("reminder_count") or 0) + 1
                ref["updated_at"] = now
                reminders += 1
                changed = True
    if changed:
        await asyncio.to_thread(save_telegram_handoff_refs, refs, ref_path)
    return {
        "ok": True,
        "reminders": reminders,
        "escalations": escalations,
        "expired": expired,
        "expired_critical": expired_critical,
        "deduped": deduped,
        "notifications": notifications,
    }


def _notification_delivered(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("sent") is True:
        return True
    if _telegram_delivery_ok(result.get("telegram") or result):
        return True
    return result.get("reason") == "preview_only" and bool(result.get("outbox") or result.get("outbox_path"))


async def _send_handoff_sla_notification(
    notifier: HandoffNotifier,
    ref: dict[str, Any],
    *,
    event: str,
    now: int,
) -> tuple[dict[str, Any], bool]:
    text = _format_handoff_sla_notification(ref, event=event)
    reply_markup = handoff_followup_keyboard(ref)
    topic_result = await _handoff_sla_topic_result(notifier, ref)
    topic_params = dict(topic_result.get("topic_params") or {})
    if not topic_params:
        return (
            {
                "sent": False,
                "reason": "missing_valid_topic",
                "text": text,
                "reply_markup": reply_markup,
                "topic": topic_result,
                "topic_params": {},
            },
            False,
        )
    result = await notifier.notify_text(text, reply_markup=reply_markup, topic_params=topic_params)
    if isinstance(result, dict) and "topic" not in result:
        result["topic"] = topic_result
    if not _notification_thread_not_found(result):
        return result if isinstance(result, dict) else {"sent": False, "telegram": result}, False

    invalidated = _remember_invalid_handoff_thread(ref, str(topic_params.get("message_thread_id") or ""), now=now)
    retry_topic_result = await _handoff_sla_topic_result(notifier, ref)
    retry_topic_params = dict(retry_topic_result.get("topic_params") or {})
    if not retry_topic_params or retry_topic_params == topic_params:
        if isinstance(result, dict):
            result["topic_retry"] = retry_topic_result
        return result if isinstance(result, dict) else {"sent": False, "telegram": result}, invalidated

    retry_result = await notifier.notify_text(text, reply_markup=reply_markup, topic_params=retry_topic_params)
    if isinstance(retry_result, dict):
        retry_result["topic"] = retry_topic_result
        retry_result["topic_retry_from"] = topic_result
    return retry_result if isinstance(retry_result, dict) else {"sent": False, "telegram": retry_result}, invalidated


def _notification_thread_not_found(result: Any) -> bool:
    if _message_thread_not_found(result):
        return True
    if isinstance(result, dict):
        if _message_thread_not_found(result.get("telegram")):
            return True
        fallback = result.get("topic_fallback")
        if isinstance(fallback, dict) and _message_thread_not_found(fallback.get("original")):
            return True
    return False


def _handoff_sla_topic_params(ref: dict[str, Any]) -> dict[str, str]:
    thread_id = str(ref.get("telegram_message_thread_id") or "").strip()
    if thread_id and thread_id in _invalid_handoff_thread_ids(ref):
        return {}
    return {"message_thread_id": thread_id} if thread_id else {}


async def _handoff_sla_topic_result(notifier: HandoffNotifier, ref: dict[str, Any]) -> dict[str, Any]:
    resolver = getattr(notifier, "topic_for_handoff_ref", None)
    if callable(resolver):
        try:
            result = await resolver(ref)
            if isinstance(result, dict) and result.get("topic_params"):
                return result
        except Exception as exc:
            return {"ok": False, "reason": "topic_resolver_failed", "error": repr(exc), "topic_params": _handoff_sla_topic_params(ref)}
    return {"ok": bool(_handoff_sla_topic_params(ref)), "reason": "handoff_ref_thread", "topic_params": _handoff_sla_topic_params(ref)}


def _find_client_topic_for_handoff_ref(
    ref: dict[str, Any],
    telegram_chat_id: str,
    topics_path: Path | str = DEFAULT_TELEGRAM_CLIENT_TOPICS_PATH,
    invalid_thread_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    external_chat_id = str(ref.get("avito_chat_id") or "").strip()
    if not external_chat_id:
        return None
    account_id = str(ref.get("account_id") or "").strip()
    invalid_thread_ids = invalid_thread_ids or set()
    candidates = [
        row
        for row in load_client_topics(topics_path).values()
        if isinstance(row, dict)
        and str(row.get("telegram_chat_id") or "").strip() == str(telegram_chat_id or "").strip()
        and str(row.get("channel") or "").casefold() == "avito"
        and str(row.get("external_chat_id") or "").strip() == external_chat_id
        and str(row.get("message_thread_id") or "").strip()
        and str(row.get("message_thread_id") or "").strip() not in invalid_thread_ids
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            1 if account_id and str(row.get("account_id") or "").strip() == account_id else 0,
            int(row.get("updated_at") or row.get("created_at") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def _invalid_handoff_thread_ids(ref: dict[str, Any]) -> set[str]:
    raw = ref.get("invalid_telegram_message_thread_ids") if isinstance(ref, dict) else []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _remember_invalid_handoff_thread(ref: dict[str, Any], thread_id: str, *, now: int) -> bool:
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return False
    invalid = sorted(_invalid_handoff_thread_ids(ref) | {thread_id})
    ref["invalid_telegram_message_thread_ids"] = invalid
    if str(ref.get("telegram_message_thread_id") or "").strip() == thread_id:
        ref["telegram_message_thread_id"] = ""
    ref["topic_invalidated_at"] = now
    ref["updated_at"] = now
    return True


def _topic_request_from_handoff_ref(ref: dict[str, Any]) -> dict[str, Any]:
    external_chat_id = str(ref.get("avito_chat_id") or "").strip()
    if not external_chat_id:
        return {}
    account_id = str(ref.get("account_id") or "").strip()
    client_name = str(ref.get("client_name") or _handoff_ref_text_value(ref, "Клиент") or "").strip()
    listing_title, listing_city = _listing_parts_from_handoff_ref(ref)
    city = str(ref.get("city") or listing_city or "").strip()
    listing_title = str(listing_title or ref.get("service") or "").strip()
    title = topic_title_for_client(
        client_name=client_name,
        channel="avito",
        city=city,
        listing_title=listing_title,
        external_chat_id=external_chat_id,
    )
    return {
        "key": client_topic_key(channel="avito", account_id=account_id, external_chat_id=external_chat_id),
        "title": title,
        "channel": "avito",
        "external_chat_id": external_chat_id,
        "account_id": account_id,
        "client_name": client_name,
        "listing_title": listing_title,
        "city": city,
    }


def _handoff_ref_text_value(ref: dict[str, Any], label: str) -> str:
    prefix = f"{label}:"
    for line in str(ref.get("handoff_text") or "").splitlines():
        if line.strip().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def _listing_parts_from_handoff_ref(ref: dict[str, Any]) -> tuple[str, str]:
    listing = _handoff_ref_text_value(ref, "Объявление")
    if not listing:
        return "", ""
    parts = [part.strip() for part in listing.split("|") if part.strip()]
    if not parts:
        return "", ""
    city = parts[-1] if len(parts) > 1 else ""
    title = " | ".join(parts[:-1]) if city else parts[0]
    return title, city


def _find_handoff_ref_by_followup_token(refs: dict[str, dict], token: str) -> dict[str, Any] | None:
    for ref in refs.values():
        if not isinstance(ref, dict):
            continue
        handoff_id = str(ref.get("handoff_id") or "").strip()
        if handoff_id and handoff_followup_token(handoff_id) == token:
            return ref
    return None


def _snooze_handoff_followup(
    refs: dict[str, dict],
    handoff_id: str,
    *,
    now: int,
    ref_path: Path | str,
    actor: str,
) -> bool:
    handoff_id = str(handoff_id or "").strip()
    if not handoff_id:
        return False
    changed = False
    for ref in refs.values():
        if not isinstance(ref, dict) or str(ref.get("handoff_id") or "") != handoff_id:
            continue
        ref["reminder_sent_at"] = now
        ref["escalation_sent_at"] = now
        ref["snoozed_at"] = now
        ref["snoozed_by"] = actor
        ref["updated_at"] = now
        changed = True
    if changed:
        save_telegram_handoff_refs(refs, ref_path)
    return changed


def _canonical_handoff_sla_refs(refs: dict[str, dict], *, now: int) -> tuple[list[dict], int]:
    grouped: dict[str, dict] = {}
    unresolved = 0
    for fallback_key, ref in refs.items():
        if not isinstance(ref, dict):
            continue
        if str(ref.get("status") or "open") not in UNRESOLVED_HANDOFF_STATUSES:
            continue
        unresolved += 1
        group_key = _handoff_sla_group_key(ref, fallback_key=str(fallback_key))
        current = grouped.get(group_key)
        if current is None or _handoff_sla_priority(ref, now=now) > _handoff_sla_priority(current, now=now):
            grouped[group_key] = ref
    rows = list(grouped.values())
    rows.sort(key=lambda ref: _handoff_sla_priority(ref, now=now), reverse=True)
    return rows, max(0, unresolved - len(rows))


def _handoff_sla_group_key(ref: dict[str, Any], *, fallback_key: str) -> str:
    avito_chat_id = str(ref.get("avito_chat_id") or "").strip()
    if avito_chat_id:
        return f"chat:{avito_chat_id}"
    handoff_id = str(ref.get("handoff_id") or "").strip()
    if handoff_id:
        return f"handoff:{handoff_id}"
    return f"ref:{fallback_key}"


def _handoff_sla_priority(ref: dict[str, Any], *, now: int) -> tuple[int, int, int, int]:
    status = str(ref.get("status") or "open")
    created_at = int(ref.get("created_at") or ref.get("updated_at") or now)
    updated_at = int(ref.get("updated_at") or created_at)
    age = max(0, now - created_at)
    return (
        1 if status == "expired_critical" else 0,
        1 if handoff_ref_is_critical(ref) else 0,
        age,
        updated_at,
    )


def handoff_notifier_from_settings(settings: IntegrationSettings) -> HandoffNotifier:
    if settings.handoff_notify_ready:
        return TelegramHandoffNotifier(
            TelegramBot(settings.telegram_admin_bot_token),
            settings.handoff_notify_chat_id,
            topics_enabled=settings.telegram_client_topics_enabled,
            topics_path=settings.telegram_client_topics_path,
        )
    return PreviewHandoffNotifier()


def format_handoff_message(handoff: Handoff) -> str:
    message = handoff.message
    listing = message.listing
    urgent = _handoff_is_critical(handoff)
    heading = "Нужна ручная проверка"
    if urgent:
        heading = "СРОЧНО: клиент ждёт подтверждение записи/адрес" if _is_booking_critical(handoff) else "СРОЧНО: Нужна ручная проверка"
    lines = [
        heading,
        f"Причина: {handoff.reason.value}",
        "Статус: open",
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


def _is_booking_critical(handoff: Handoff) -> bool:
    return str(getattr(handoff.reason, "value", handoff.reason)) == "booking_critical"


def _handoff_is_critical(handoff: Handoff) -> bool:
    return str(getattr(handoff.reason, "value", handoff.reason)) in {
        "booking_critical",
        "booking_ambiguous",
        "photo_consultation",
        "complaint_or_risk",
        "expert_expectation",
        "medical_question",
        "voice_transcription_failed",
    }


def _ref_is_critical(ref: dict[str, Any]) -> bool:
    return handoff_ref_is_critical(ref)


def _format_handoff_sla_notification(ref: dict[str, Any], *, event: str) -> str:
    heading = (
        "Критично, клиент ждёт, возможен негативный отзыв"
        if event == "escalation"
        else "Напоминание: handoff всё ещё открыт"
    )
    context = _handoff_ref_context(ref)
    lines = [
        heading,
        f"Статус: {ref.get('status') or 'open'}",
    ]
    if context.get("client_name"):
        lines.append(f"Клиент: {context['client_name']}")
    if context.get("listing"):
        lines.append(f"Объявление: {context['listing']}")
    details = [part for part in (ref.get("service"), ref.get("city"), ref.get("booking_date"), ref.get("booking_time")) if part]
    if details:
        lines.append(f"Детали записи: {' | '.join(str(part) for part in details)}")
    if context.get("client_message"):
        lines.append(f"Последнее от клиента: {_text_preview(context['client_message'], 260)}")
    if context.get("summary"):
        lines.append(f"Контекст: {_text_preview(context['summary'], 320)}")
    if ref.get("confirmation_needed"):
        lines.append(f"Нужно подтвердить: {ref['confirmation_needed']}")
    else:
        lines.append("Нужно сделать: открыть диалог/карточку и дать клиенту финальный ответ.")
    if ref.get("deadline_at"):
        lines.append(f"Дедлайн: {ref['deadline_at']}")
    if ref.get("assignee"):
        lines.append(f"Ответственный: {ref['assignee']}")
    lines.append(f"Avito chat_id: {ref.get('avito_chat_id') or '-'}")
    return "\n".join(lines)


def _handoff_ref_context(ref: dict[str, Any]) -> dict[str, str]:
    result = {
        "client_name": str(ref.get("client_name") or "").strip(),
        "listing": "",
        "client_message": "",
        "summary": "",
    }
    for line in str(ref.get("handoff_text") or "").splitlines():
        label, _, value = line.partition(":")
        value = value.strip()
        if not value:
            continue
        normalized = label.strip().casefold()
        if normalized == "клиент" and not result["client_name"]:
            result["client_name"] = value
        elif normalized == "объявление":
            result["listing"] = value
        elif normalized == "сообщение":
            result["client_message"] = value
        elif normalized == "контекст":
            result["summary"] = value
    return result


def _text_preview(text: str, limit: int = 180) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "..."


def _store_ref_media_statuses(
    telegram_chat_id: str,
    telegram_message_id: str | int,
    media_statuses: list[dict[str, Any]],
    ref_path: Path | str,
) -> None:
    refs = load_telegram_handoff_refs(ref_path)
    key = f"{str(telegram_chat_id).strip()}:{str(telegram_message_id).strip()}"
    ref = refs.get(key) if isinstance(refs.get(key), dict) else None
    if not ref:
        return
    ref["media_statuses"] = media_statuses
    ref["updated_at"] = int(time.time())
    save_telegram_handoff_refs(refs, ref_path)


def _booking_task_fields(handoff: Handoff) -> dict[str, str]:
    message = handoff.message
    metadata = message.metadata or {}
    text = str(message.text or "")
    listing = message.listing
    return {
        "phone": str(metadata.get("phone") or _phone_from_text(text)),
        "city": str(metadata.get("city") or (listing.city if listing else "") or _city_from_text(text)),
        "service": str(metadata.get("service") or metadata.get("service_title") or (listing.title if listing else "")),
        "booking_date": str(metadata.get("booking_date") or extract_date(text)),
        "booking_time": str(metadata.get("booking_time") or extract_time(text)),
        "confirmation_needed": _confirmation_needed(text),
        "assignee": str(metadata.get("assignee") or "Ольга/админ"),
    }


def _handoff_task_fields(handoff: Handoff) -> dict[str, str]:
    message = handoff.message
    metadata = message.metadata or {}
    text = str(message.text or "")
    listing = message.listing
    reason = str(getattr(handoff.reason, "value", handoff.reason))
    waits_for = ""
    if reason == "photo_consultation" or message.has_photo:
        waits_for = "оценка фото/вложения"
    elif reason == "expert_expectation":
        waits_for = "экспертная оценка Ольги по объёму/ожидаемому результату"
    elif reason in {"complaint_or_risk", "medical_question"}:
        waits_for = "экспертный ответ Ольги"
    elif reason == "voice_transcription_failed":
        waits_for = "ручная проверка голосового сообщения"
    elif "передам" in text.casefold() or "уточ" in text.casefold():
        waits_for = "финальный ответ после обещания уточнить"
    return {
        "client_waits_for": waits_for,
        "phone": str(metadata.get("phone") or _phone_from_text(text)),
        "city": str(metadata.get("city") or (listing.city if listing else "") or _city_from_text(text)),
        "service": str(metadata.get("service") or metadata.get("service_title") or (listing.title if listing else "")),
        "booking_date": str(metadata.get("booking_date") or extract_date(text)),
        "booking_time": str(metadata.get("booking_time") or extract_time(text)),
        "confirmation_needed": _confirmation_needed(text) if reason in {"booking_ambiguous", "booking_critical"} else "",
        "assignee": str(metadata.get("assignee") or "Ольга/админ"),
    }


def _confirmation_needed(text: str) -> str:
    lowered = text.casefold().replace("ё", "е")
    if "адрес" in lowered or "где" in lowered:
        return "точный адрес и подтверждение записи"
    if "оплат" in lowered or "предоплат" in lowered:
        return "условия оплаты"
    if "опозда" in lowered:
        return "можно ли клиенту опоздать"
    if "актуальн" in lowered or "приход" in lowered or "жд" in lowered:
        return "актуальность записи и время прихода"
    return "что именно ответить клиенту по записи"


def _phone_from_text(text: str) -> str:
    match = re.search(r"(?<!\d)(?:\+7|8)?[\s().-]*9\d{2}(?:[\s().-]*\d){7}(?!\d)", text)
    return match.group(0).strip() if match else ""


def _city_from_text(text: str) -> str:
    lowered = text.casefold().replace("ё", "е")
    aliases = {
        "моск": "Москва",
        "краснодар": "Краснодар",
        "ростов": "Ростов-на-Дону",
        "питер": "Санкт-Петербург",
        "спб": "Санкт-Петербург",
        "гелендж": "Геленджик",
    }
    for needle, city in aliases.items():
        if needle in lowered:
            return city
    return ""


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


def _telegram_delivery_ok(send_response: Any) -> bool:
    return isinstance(send_response, dict) and send_response.get("ok") is not False and bool(_telegram_message_id(send_response))


def _telegram_send_error(send_response: Any, fallback: str) -> str:
    if not isinstance(send_response, dict):
        return fallback
    return str(send_response.get("error") or send_response.get("description") or fallback)


async def _send_message_with_topic_fallback(
    bot: Any,
    chat_id: str,
    text: str,
    *,
    topic_params: dict[str, str] | None = None,
    reply_markup: dict | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    topic_params = dict(topic_params or {})
    kwargs = dict(topic_params)
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    try:
        result = await _to_thread_retry(lambda: bot.send_message(chat_id, text, **kwargs))
    except Exception as exc:
        result = {"ok": False, "error": repr(exc)}
    if _telegram_delivery_ok(result) or not topic_params or not _message_thread_not_found(result):
        return result, topic_params, {}
    return result, topic_params, {
        "reason": "message_thread_not_found",
        "original": result,
        "topic_params": topic_params,
        "general_fallback_sent": False,
    }


def _message_thread_not_found(send_response: Any) -> bool:
    error = _telegram_send_error(send_response, "")
    return "message thread not found" in error.casefold()


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
