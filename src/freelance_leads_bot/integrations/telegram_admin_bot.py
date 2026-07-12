from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..codex_runner import codex_auth_status, codex_logout_reset, start_codex_device_login, telegram_markdown_to_html
from ..media_recognition import transcribe_voice
from ..mfa import delete_totp_secret, mfa_code_text, mfa_status, save_totp_secret
from ..storage import LeadStore
from ..telegram import TelegramBot
from .admin_codex import CodexTelegramAdminService, AdminCodexProgress
from .agent_tools import AutomationToolbox
from .agent_trace import JsonlAgentTraceLogger, redact_sensitive
from .admin import AdminResult, TelegramAdminService
from .avito_read import avito_read_client_from_settings
from .avito_sender import avito_image_sender_from_settings, avito_sender_from_settings
from .config import IntegrationSettings
from .handoff_notify import handoff_notifier_from_settings
from .mentor_memory import MentorMemoryService
from .expert_rag import ExpertRagStore
from .expert_rag_admin import (
    ExpertRagAdminService,
    format_rag_admin_plan,
    parse_rag_admin_callback,
    rag_admin_plan_from_dict,
    rag_admin_plan_keyboard,
)
from .rag_admin_intent import RagAdminIntentParser
from .roles import CodexRole, conversation_key, role_profile, telegram_role_for_user
from .service_catalog import ServiceCatalogStore
from .yclients import DryRunYClientsGateway, LiveReadDryRunYClientsGateway, YClientsGateway, YClientsHttpGateway


VoiceTranscriber = Callable[[TelegramBot, dict[str, Any]], Any]


class TelegramAdminBotTransport:
    """Telegram transport for the cosmetologist admin bridge."""

    def __init__(
        self,
        bot: TelegramBot,
        service: TelegramAdminService,
        settings: IntegrationSettings,
        *,
        transcriber: VoiceTranscriber = transcribe_voice,
        history_store: LeadStore | None = None,
    ) -> None:
        self.bot = bot
        self.service = service
        self.settings = settings
        self.transcriber = transcriber
        self.history_store = history_store

    async def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        message = update.get("message") or update.get("business_message") or {}
        if not isinstance(message, dict) or not message:
            return {"ok": True, "ignored": True, "reason": "not_message"}

        chat_id = str((message.get("chat") or {}).get("id") or "")
        delivery_params = telegram_delivery_params(message, str(update.get("business_connection_id") or "").strip())
        user_id = _user_id(message)
        if not self._is_allowed(user_id):
            if chat_id:
                await self._send(chat_id, "Доступ только для администратора.", delivery_params)
            return {"ok": False, "ignored": True, "reason": "forbidden", "user_id": user_id}
        role = telegram_role_for_user(
            user_id,
            admin_user_id=self.settings.telegram_admin_user_id,
            cosmetologist_user_id=self.settings.telegram_cosmetologist_user_id,
        )
        history_key = telegram_history_key(chat_id, delivery_params, role=role)

        if not _has_user_content(message):
            return {"ok": True, "ignored": True, "reason": "empty_service_message"}

        text = await self._message_text(message)
        attachments = await self._message_attachments(message)
        if not text:
            if attachments:
                text = "Косметолог прислала фото. Определи по контексту беседы, что с ним нужно сделать."
            else:
                await self._send(chat_id, "Пришлите текст, фото с подписью или голосовую команду.", delivery_params)
                return {"ok": False, "reason": "empty_text"}

        command_result = await self._handle_service_command(text, chat_id, message, delivery_params)
        if command_result is not None:
            return command_result

        reply_plan_id = _rag_plan_id_from_reply(message)
        if reply_plan_id:
            result = await self._handle_rag_plan_reply(reply_plan_id, text, chat_id, delivery_params)
            if result is not None:
                return result

        await self._send_chat_action(chat_id, delivery_params)
        live_drafts = TelegramLiveDraftStreamer(
            self.bot,
            chat_id,
            int(update.get("update_id") or 0),
            delivery_params,
            enabled=self.settings.telegram_admin_live_drafts_enabled,
            min_interval_seconds=self.settings.telegram_admin_live_draft_interval_seconds,
        )
        live_drafts.open()
        result_task = asyncio.create_task(self._handle_text(text, live_drafts.progress, message, history_key, attachments, role))
        try:
            result = await asyncio.wait_for(
                asyncio.shield(result_task),
                timeout=self._response_wait_seconds(),
            )
        except asyncio.TimeoutError:
            self._remember(history_key, "user", _history_user_content(text, attachments))
            result_task.add_done_callback(
                lambda done: asyncio.create_task(
                    self._send_deferred_result(done, chat_id, delivery_params, history_key)
                )
            )
            message_text = "Codex ещё работает. Как только закончит, отправлю результат следующим сообщением."
            await self._send(chat_id, message_text, delivery_params)
            return {
                "ok": True,
                "action": "codex_deferred",
                "message": message_text,
                "text": text,
                "delivery_params": delivery_params,
                "history_key": history_key,
                "role": role.value,
            }
        rag_plan = _pending_rag_plan_from_result(result)
        if rag_plan:
            await asyncio.to_thread(
                self.bot.send_message,
                chat_id,
                escape(format_rag_admin_plan(rag_plan, details=True)),
                reply_markup=rag_admin_plan_keyboard(rag_plan.id),
                **delivery_params,
            )
        else:
            await self._send(chat_id, result.message, delivery_params)
        self._remember(history_key, "user", _history_user_content(text, attachments))
        self._remember(history_key, "assistant", _history_assistant_content(result))
        return {
            "ok": result.ok,
            "action": result.action,
            "message": result.message,
            "appointment_id": result.appointment_id,
            "client_id": result.client_id,
            "text": text,
            "delivery_params": delivery_params,
            "history_key": history_key,
            "role": role.value,
        }

    async def handle_callback_update(self, update: dict[str, Any]) -> dict[str, Any]:
        callback = update.get("callback_query") or {}
        data = str(callback.get("data") or "")
        parsed = parse_rag_admin_callback(data)
        if not parsed:
            return {"ok": True, "ignored": True, "reason": "not_rag_plan_callback"}
        callback_id = str(callback.get("id") or "")
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        delivery_params = telegram_delivery_params(message, str(update.get("business_connection_id") or "").strip())
        plan_id, action = parsed
        user_id = _user_id(callback)
        if not self._is_allowed(user_id):
            if callback_id:
                await asyncio.to_thread(self.bot.answer_callback_query, callback_id, "Нет доступа")
            return {"ok": False, "ignored": True, "reason": "forbidden", "user_id": user_id}
        service = self._rag_admin_service()
        if not service:
            if callback_id:
                await asyncio.to_thread(self.bot.answer_callback_query, callback_id, "RAG недоступен")
            return {"ok": False, "action": "rag_plan_callback", "message": "Expert RAG admin is not configured"}
        try:
            if action == "apply":
                plan = service.apply_plan(plan_id, actor="olga")
                text = "Готово, применила изменения в RAG-памяти.\n\n" + format_rag_admin_plan(plan, details=True)
                await asyncio.to_thread(self.bot.answer_callback_query, callback_id, "Применено")
                await self._send(chat_id, text, delivery_params)
                return {"ok": True, "action": "rag_plan_apply", "plan_id": plan_id, "message": text}
            if action == "cancel":
                plan = service.cancel_plan(plan_id, actor="olga")
                await asyncio.to_thread(self.bot.answer_callback_query, callback_id, "Отменено")
                text = "Ок, ничего не меняю в RAG-памяти."
                await self._send(chat_id, text, delivery_params)
                return {"ok": True, "action": "rag_plan_cancel", "plan_id": plan_id, "message": text, "plan": plan.to_dict() if plan else None}
            if action == "details":
                plan = service.get_plan(plan_id)
                await asyncio.to_thread(self.bot.answer_callback_query, callback_id, "Подробнее")
                text = format_rag_admin_plan(plan, details=True) if plan else "План не найден."
                await self._send(chat_id, text, delivery_params)
                return {"ok": bool(plan), "action": "rag_plan_details", "plan_id": plan_id, "message": text}
            if action == "edit":
                await asyncio.to_thread(self.bot.answer_callback_query, callback_id, "Жду правку")
                text = "Ответьте на эту карточку сообщением с правкой — я пересоберу план и снова попрошу подтверждение."
                await self._send(chat_id, text, delivery_params)
                return {"ok": True, "action": "rag_plan_edit_prompt", "plan_id": plan_id, "message": text}
        except Exception as exc:
            await asyncio.to_thread(self.bot.answer_callback_query, callback_id, "Не удалось")
            text = f"Не удалось обработать RAG-план: {exc}"
            await self._send(chat_id, text, delivery_params)
            return {"ok": False, "action": "rag_plan_callback", "plan_id": plan_id, "message": text}
        await asyncio.to_thread(self.bot.answer_callback_query, callback_id, "Неизвестное действие")
        return {"ok": False, "action": "rag_plan_callback", "plan_id": plan_id, "message": "unknown action"}

    async def _handle_rag_plan_reply(
        self,
        plan_id: str,
        text: str,
        chat_id: str,
        delivery_params: dict[str, str],
    ) -> dict[str, Any] | None:
        service = self._rag_admin_service()
        if not service:
            return None
        plan = service.update_plan_from_text(plan_id, text, actor="olga")
        message = format_rag_admin_plan(plan, details=True)
        await asyncio.to_thread(self.bot.send_message, chat_id, escape(message), reply_markup=rag_admin_plan_keyboard(plan.id), **delivery_params)
        return {"ok": plan.status == "pending", "action": "rag_plan_revised", "plan_id": plan.id, "message": message}

    def _rag_admin_service(self) -> ExpertRagAdminService | None:
        if not isinstance(self.service, CodexTelegramAdminService):
            return None
        return self.service.toolbox.expert_rag_admin

    async def _send_deferred_result(
        self,
        result_task: asyncio.Future[Any],
        chat_id: str,
        delivery_params: dict[str, str],
        history_key: str,
    ) -> None:
        try:
            result = result_task.result()
        except Exception as exc:
            result = AdminResult(action="codex_failed", ok=False, message=f"Codex чат не удался: {exc}")
        await self._send(chat_id, result.message, delivery_params)
        self._remember(history_key, "assistant", _history_assistant_content(result))

    def _response_wait_seconds(self) -> int:
        value = getattr(self.settings, "telegram_admin_response_wait_seconds", 25)
        try:
            return max(int(value), 1)
        except (TypeError, ValueError):
            return 25

    async def _handle_text(
        self,
        text: str,
        progress_callback: AdminCodexProgress | None = None,
        message: dict[str, Any] | None = None,
        history_key: str = "telegram_admin:default",
        attachments: list[dict[str, Any]] | None = None,
        role: CodexRole = CodexRole.ADMIN,
    ) -> Any:
        if isinstance(self.service, CodexTelegramAdminService):
            return await self.service.handle_message(
                self._codex_message(text, message or {}, history_key, attachments or [], role),
                progress_callback=progress_callback,
            )
        return await self.service.handle_text(text)

    async def _handle_service_command(
        self,
        text: str,
        chat_id: str,
        message: dict[str, Any],
        delivery_params: dict[str, str],
    ) -> dict[str, Any] | None:
        command = text.strip()
        command_name = command.split(maxsplit=1)[0].split("@", 1)[0].casefold()
        if command_name == "/codex_auth":
            body = await asyncio.to_thread(codex_auth_status)
            await self._send_html(chat_id, "<b>Codex auth:</b>\n" + telegram_markdown_to_html(body), delivery_params)
            return {"ok": True, "action": "codex_auth", "message": body}
        if command_name == "/codex_login":
            body = await asyncio.to_thread(start_codex_device_login)
            await self._send_html(chat_id, "<b>Codex login:</b>\n" + telegram_markdown_to_html(body), delivery_params)
            return {"ok": True, "action": "codex_login", "message": body}
        if command_name == "/codex_logout":
            body = await asyncio.to_thread(codex_logout_reset)
            await self._send_html(chat_id, "<b>Codex logout:</b>\n" + telegram_markdown_to_html(body), delivery_params)
            return {"ok": True, "action": "codex_logout", "message": body}
        if command_name == "/mfa_status":
            body = await asyncio.to_thread(mfa_status)
            await self._send_html(chat_id, "<b>MFA:</b>\n" + telegram_markdown_to_html(body), delivery_params)
            return {"ok": True, "action": "mfa_status", "message": body}
        if command_name == "/mfa_set":
            secret_text = command.split(maxsplit=1)[1].strip() if len(command.split(maxsplit=1)) > 1 else ""
            if not secret_text:
                body = (
                    "Пришли так: <code>/mfa_set JBSWY3DPEHPK3PXP</code>\n"
                    "Можно вставить и полный <code>otpauth://totp/...</code> URI. "
                    "После установки удали своё сообщение с секретом."
                )
                await self._send_html(chat_id, body, delivery_params)
                return {"ok": False, "action": "mfa_set_missing_secret", "message": body}
            await self._delete_message(chat_id, str(message.get("message_id") or ""))
            try:
                body = await asyncio.to_thread(save_totp_secret, secret_text)
            except Exception as exc:
                body = "Не удалось сохранить MFA: " + str(exc)
                await self._send(chat_id, body, delivery_params)
                return {"ok": False, "action": "mfa_set", "message": body}
            await self._send_html(chat_id, "<b>MFA сохранена:</b>\n" + telegram_markdown_to_html(body), delivery_params)
            return {"ok": True, "action": "mfa_set", "message": body}
        if command_name == "/mfa_delete":
            body = await asyncio.to_thread(delete_totp_secret)
            await self._send_html(chat_id, "<b>MFA:</b>\n" + telegram_markdown_to_html(body), delivery_params)
            return {"ok": True, "action": "mfa_delete", "message": body}
        if command_name == "/mfa":
            try:
                body = await asyncio.to_thread(mfa_code_text)
            except Exception as exc:
                body = "MFA не готова: " + str(exc)
                await self._send(chat_id, body, delivery_params)
                return {"ok": False, "action": "mfa", "message": body}
            await self._send_html(chat_id, "<b>MFA:</b>\n" + telegram_markdown_to_html(body), delivery_params)
            return {"ok": True, "action": "mfa", "message": body}
        if _looks_like_rag_admin_command(command):
            service = self._rag_admin_service()
            if service:
                plan = service.plan_change(command, actor="olga")
                body = format_rag_admin_plan(plan, details=True)
                await asyncio.to_thread(
                    self.bot.send_message,
                    chat_id,
                    escape(body),
                    reply_markup=rag_admin_plan_keyboard(plan.id),
                    **delivery_params,
                )
                return {"ok": plan.status == "pending", "action": "rag_plan_created", "message": body, "plan_id": plan.id}
        return None

    def _codex_message(
        self,
        text: str,
        message: dict[str, Any],
        history_key: str,
        attachments: list[dict[str, Any]] | None = None,
        role: CodexRole = CodexRole.ADMIN,
    ) -> dict[str, Any]:
        profile = role_profile(role)
        return {
            "text": text,
            "channel": "telegram_admin",
            "codex_role": role.value,
            "role_profile": profile.prompt_role,
            "chat_id": str((message.get("chat") or {}).get("id") or ""),
            "message_id": str(message.get("message_id") or ""),
            "thread": telegram_topic_params(message),
            "history_key": history_key,
            "conversation_key": history_key,
            "attachments": attachments or [],
            "conversation_history": self._recent_history(history_key),
        }

    def _recent_history(self, history_key: str) -> list[dict[str, Any]]:
        if not self.history_store or not self.settings.telegram_admin_history_enabled:
            return []
        return self.history_store.recent_codex_chat(self.settings.telegram_admin_history_limit, history_key)

    def _remember(self, history_key: str, role: str, content: str) -> None:
        if not self.history_store or not self.settings.telegram_admin_history_enabled:
            return
        self.history_store.add_codex_chat_message(role, content, history_key)

    async def _message_text(self, message: dict[str, Any]) -> str:
        text = str(message.get("text") or message.get("caption") or "").strip()
        if text:
            return text
        if message.get("voice") or message.get("audio"):
            recognized = await asyncio.to_thread(self.transcriber, self.bot, message)
            return str(recognized.text or "").strip()
        return ""

    async def _message_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        photo = _largest_telegram_photo(message)
        if not photo:
            return []
        try:
            path = await asyncio.to_thread(_download_telegram_photo, self.bot, str(photo["file_id"]))
        except (OSError, RuntimeError, KeyError):
            return []
        return [
            {
                "type": "photo",
                "source": "telegram_admin",
                "image_path": str(path),
                "file_id": str(photo.get("file_id") or ""),
                "width": photo.get("width"),
                "height": photo.get("height"),
            }
        ]

    async def _send(self, chat_id: str, text: str, delivery_params: dict[str, str] | None = None) -> None:
        if not chat_id:
            return
        await asyncio.to_thread(self.bot.send_message, chat_id, escape(text), **(delivery_params or {}))

    async def _send_html(self, chat_id: str, html: str, delivery_params: dict[str, str] | None = None) -> None:
        if not chat_id:
            return
        await asyncio.to_thread(self.bot.send_message, chat_id, html, **(delivery_params or {}))

    async def _delete_message(self, chat_id: str, message_id: str) -> None:
        if not chat_id or not message_id:
            return
        try:
            await asyncio.to_thread(self.bot.api, "deleteMessage", {"chat_id": chat_id, "message_id": message_id}, 8)
        except (OSError, RuntimeError, AttributeError):
            return

    async def _send_chat_action(self, chat_id: str, delivery_params: dict[str, str]) -> None:
        if not chat_id:
            return
        try:
            await asyncio.to_thread(self.bot.send_chat_action, chat_id, "typing", **delivery_params)
        except (OSError, RuntimeError, AttributeError):
            return

    def _is_allowed(self, user_id: int) -> bool:
        allowed = {
            self.settings.telegram_admin_user_id,
            self.settings.telegram_cosmetologist_user_id,
            *self.settings.telegram_extra_admin_user_ids,
        }
        return bool(user_id and user_id in allowed)


class TelegramLiveDraftStreamer:
    def __init__(
        self,
        bot: TelegramBot,
        chat_id: str,
        update_id: int,
        delivery_params: dict[str, str],
        *,
        enabled: bool = True,
        min_interval_seconds: float = 1.2,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.enabled = bool(enabled and chat_id)
        self.min_interval_seconds = max(min_interval_seconds, 0.5)
        self.message_thread_id = delivery_params.get("message_thread_id")
        self.draft_id = ((int(time.time() * 1000) << 8) ^ int(update_id)) % 2147483647 or 1
        self.last_sent_at = 0.0
        self.failed = False

    def open(self) -> None:
        if not self.enabled:
            return
        try:
            self.bot.send_message_draft(self.chat_id, self.draft_id, "", message_thread_id=self.message_thread_id)
            self.last_sent_at = 0.0
        except (OSError, RuntimeError, AttributeError):
            self.failed = True

    def progress(self, text: str) -> None:
        if not self.enabled or self.failed:
            return
        now = time.monotonic()
        if now - self.last_sent_at < self.min_interval_seconds:
            return
        self.last_sent_at = now
        live_text = "<b>Codex live:</b>\n" + telegram_markdown_to_html(str(text).strip() or "Думаю...")
        try:
            self.bot.send_message_draft(
                self.chat_id,
                self.draft_id,
                live_text,
                message_thread_id=self.message_thread_id,
            )
        except (OSError, RuntimeError, AttributeError):
            self.failed = True


def build_telegram_admin_transport(
    settings: IntegrationSettings | None = None,
    booking: YClientsGateway | None = None,
) -> TelegramAdminBotTransport:
    settings = settings or IntegrationSettings.from_env()
    bot = TelegramBot(settings.telegram_admin_bot_token)
    booking = booking or _booking_from_settings(settings)
    history_store = LeadStore(settings.telegram_admin_history_db_path)
    if settings.telegram_admin_codex_enabled:
        expert_rag_admin = (
            ExpertRagAdminService(
                ExpertRagStore(settings.rag_expert_db_path),
                intent_parser=RagAdminIntentParser(enabled=settings.rag_dynamic_intent_enabled),
                service_catalog=ServiceCatalogStore(settings.rag_service_catalog_path),
            )
            if settings.rag_retrieval_enabled
            else None
        )
        toolbox = AutomationToolbox(
            booking,
            avito=avito_read_client_from_settings(settings),
            avito_sender=avito_sender_from_settings(settings),
            avito_image_sender=avito_image_sender_from_settings(settings),
            avito_account_id=settings.avito_account_id,
            enable_workspace_tools=True,
            history_store=history_store,
            operations_notifier=handoff_notifier_from_settings(settings),
            expert_rag_admin=expert_rag_admin,
        )
        service = CodexTelegramAdminService(
            toolbox,
            settings,
            trace_logger=JsonlAgentTraceLogger(),
            mentor_memory=MentorMemoryService(
                toolbox.knowledge,
                expert_rag=expert_rag_admin.store if expert_rag_admin else None,
            ),
        )
    else:
        service = TelegramAdminService(booking, cities=settings.cities)
    return TelegramAdminBotTransport(bot, service, settings, history_store=history_store)


async def run_telegram_admin_polling(settings: IntegrationSettings | None = None) -> None:
    transport = build_telegram_admin_transport(settings)
    offset = await _initial_admin_polling_offset(transport)
    while True:
        try:
            updates = await asyncio.to_thread(transport.bot.get_updates, offset)
        except (OSError, TimeoutError):
            await asyncio.sleep(3)
            continue
        for update in updates:
            offset = int(update.get("update_id") or 0) + 1
            if "callback_query" in update:
                await transport.handle_callback_update(update)
            else:
                await transport.handle_update(update)


async def _initial_admin_polling_offset(transport: TelegramAdminBotTransport) -> int | None:
    updates = await asyncio.to_thread(transport.bot.get_updates, None)
    if not updates:
        return None
    return max(int(update.get("update_id") or 0) for update in updates) + 1


def _booking_from_settings(settings: IntegrationSettings) -> YClientsGateway:
    if settings.yclients_ready:
        live_gateway = YClientsHttpGateway(settings)
        if settings.yclients_allow_mutations:
            return live_gateway
        return LiveReadDryRunYClientsGateway(live_gateway)
    return DryRunYClientsGateway()


def _user_id(message: dict[str, Any]) -> int:
    try:
        return int((message.get("from") or {}).get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _has_user_content(message: dict[str, Any]) -> bool:
    if str(message.get("text") or message.get("caption") or "").strip():
        return True
    return any(message.get(kind) for kind in ("photo", "voice", "audio", "document"))


def telegram_topic_params(message: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {}
    if message.get("message_thread_id"):
        params["message_thread_id"] = str(message["message_thread_id"])
    direct_topic = message.get("direct_messages_topic") or {}
    if isinstance(direct_topic, dict) and direct_topic.get("topic_id"):
        params["direct_messages_topic_id"] = str(direct_topic["topic_id"])
    return params


def telegram_delivery_params(message: dict[str, Any], business_connection_id: str | None = None) -> dict[str, str]:
    params = telegram_topic_params(message)
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    return params


def telegram_history_key(chat_id: str, delivery_params: dict[str, str], role: CodexRole | str = CodexRole.ADMIN) -> str:
    return conversation_key("telegram", role, chat_id, thread=delivery_params)


def _largest_telegram_photo(message: dict[str, Any]) -> dict[str, Any] | None:
    photos = message.get("photo") or []
    if not isinstance(photos, list) or not photos:
        return None
    candidates = [photo for photo in photos if isinstance(photo, dict) and photo.get("file_id")]
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("file_size") or (int(item.get("width") or 0) * int(item.get("height") or 0))))


def _download_telegram_photo(bot: TelegramBot, file_id: str) -> Path:
    info = bot.get_file(file_id)
    file_path = str(info["file_path"])
    content = bot.download_file(file_path)
    suffix = Path(file_path).suffix or ".jpg"
    media_dir = Path("data/telegram_admin_uploads")
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / f"{int(time.time())}-{uuid4().hex}{suffix}"
    target.write_bytes(content)
    return target


def _history_user_content(text: str, attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return text
    lines = [text, "", "Вложения:"]
    for attachment in attachments:
        if attachment.get("type") == "photo":
            lines.append(f"- photo image_path={attachment.get('image_path')}")
    return "\n".join(line for line in lines if line is not None)


def _history_assistant_content(result: Any) -> str:
    trace = result.metadata.get("trace") if getattr(result, "metadata", None) else None
    if not trace:
        return str(result.message)
    lines = [str(result.message), "", "Tool trace:"]
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "tool_call":
            args = json.dumps(redact_sensitive(entry.get("arguments") or {}), ensure_ascii=False, sort_keys=True)
            lines.append(f"- call {entry.get('tool')}: {args[:500]}")
        elif entry.get("type") == "tool_result":
            data = json.dumps(redact_sensitive(entry.get("data") or {}), ensure_ascii=False, sort_keys=True)
            status = "ok" if entry.get("ok") else f"error={entry.get('error') or ''}"
            lines.append(f"- result {entry.get('tool')}: {status}; data={data[:1200]}")
    return "\n".join(lines)


def _looks_like_rag_admin_command(text: str) -> bool:
    lowered = str(text or "").casefold()
    if re.search(r"(?iu)(подним\w*|увелич\w*)[^\n]{0,80}\d+(?:[.,]\d+)?\s*%", lowered):
        return True
    return any(
        marker in lowered
        for marker in (
            "это устарело",
            "цена устарела",
            "цены устарели",
            "больше не актуально",
            "не говори про очную",
            "не рекоменд",
            "не склон",
            "запомни вот так",
        )
    )


def _rag_plan_id_from_reply(message: dict[str, Any]) -> str:
    reply = message.get("reply_to_message") or {}
    markup = reply.get("reply_markup") or {}
    keyboard = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if isinstance(keyboard, list):
        for row in keyboard:
            if not isinstance(row, list):
                continue
            for button in row:
                data = str((button or {}).get("callback_data") or "")
                parsed = parse_rag_admin_callback(data)
                if parsed:
                    return parsed[0]
    text = str(reply.get("text") or reply.get("caption") or "")
    match = re.search(r"(?im)^План:\s*([a-f0-9]{8,32})\b", text)
    return match.group(1) if match else ""


def _pending_rag_plan_from_result(result: Any) -> Any:
    if str(getattr(result, "action", "") or "") == "rag_admin_plan":
        plan = rag_admin_plan_from_dict((getattr(result, "metadata", None) or {}).get("plan"))
        if plan and plan.status == "pending":
            return plan
    trace = (getattr(result, "metadata", None) or {}).get("trace") or []
    for entry in reversed(trace):
        if not isinstance(entry, dict) or entry.get("type") != "tool_result":
            continue
        if entry.get("tool") != "expert_rag.plan_change":
            continue
        plan = rag_admin_plan_from_dict((entry.get("data") or {}).get("plan"))
        if plan and plan.status == "pending":
            return plan
    return None


if __name__ == "__main__":
    asyncio.run(run_telegram_admin_polling())
