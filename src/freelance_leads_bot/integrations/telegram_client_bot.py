from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..telegram import TelegramBot
from ..storage import LeadStore
from .avito_consultant import AvitoAgentPlanner, AvitoConsultant
from .care_crm import CareCrmStore, CareLearningService, ClientMemoryService
from .config import IntegrationSettings
from .handoff_notify import HandoffNotifier, handoff_notifier_from_settings
from .models import Channel, Handoff, HandoffReason, InboundMessage
from .runtime import booking_from_settings, codex_planner_from_settings, rag_retrieval_from_settings, toolbox_from_settings
from .roles import CodexRole, conversation_key, role_profile


logger = logging.getLogger(__name__)


class TelegramClientCareBot:
    """Client-facing Telegram bot for care, consultation, booking and soft upsell."""

    def __init__(
        self,
        *,
        settings: IntegrationSettings | None = None,
        bot: TelegramBot | None = None,
        handoff_notifier: HandoffNotifier | None = None,
        planner: AvitoAgentPlanner | None = None,
        history_store: LeadStore | None = None,
        care_crm: CareCrmStore | None = None,
    ) -> None:
        self.settings = settings or IntegrationSettings.from_env()
        if not self.settings.telegram_client_bot_token and bot is None:
            raise ValueError("TELEGRAM_CLIENT_BOT_TOKEN is empty")
        self.profile = role_profile(CodexRole.TELEGRAM_CLIENT)
        self.bot = bot or TelegramBot(self.settings.telegram_client_bot_token)
        self.history_store = history_store or LeadStore(self.settings.telegram_admin_history_db_path)
        self.care_crm = care_crm or CareCrmStore()
        self.client_memory = ClientMemoryService(self.care_crm)
        self.learning = CareLearningService(self.care_crm)
        booking = booking_from_settings(self.settings)
        self.handoff_notifier = handoff_notifier or handoff_notifier_from_settings(self.settings)
        self.toolbox = toolbox_from_settings(
            self.settings,
            booking,
            self.profile,
            care_crm=self.care_crm,
            operations_notifier=self.handoff_notifier,
        )
        self.planner = planner or codex_planner_from_settings(
            self.settings,
            enabled=self.settings.telegram_client_codex_enabled,
        )
        self.consultant = AvitoConsultant(
            self.toolbox,
            cities=self.settings.cities,
            planner=self.planner,
            profile=self.profile,
            rag_retrieval=rag_retrieval_from_settings(self.settings),
            rag_autoanswer_threshold=self.settings.rag_autoanswer_threshold,
            rag_handoff_threshold=self.settings.rag_handoff_threshold,
        )
        self.followup_delivery = CareFollowupDeliveryService(self.care_crm, self.bot)
        self.processed: set[str] = set()

    async def run(self) -> None:
        offset: int | None = None
        last_followup_check = 0.0
        logger.info("Telegram client care bot started codex_enabled=%s", self.settings.telegram_client_codex_enabled)
        while True:
            try:
                updates = await asyncio.to_thread(self.bot.get_updates, offset)
                for update in updates:
                    offset = int(update.get("update_id") or 0) + 1
                    await self.handle_update(update)
                if self.settings.telegram_client_followup_send_enabled and time.monotonic() - last_followup_check >= 60:
                    last_followup_check = time.monotonic()
                    await self.followup_delivery.send_due()
            except Exception:
                logger.exception("Telegram client bot polling error")
                await asyncio.sleep(5)

    async def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        message_payload = update.get("message") or {}
        if not isinstance(message_payload, dict) or not message_payload:
            return {"ok": True, "ignored": True, "reason": "not_message"}
        if (message_payload.get("from") or {}).get("is_bot"):
            return {"ok": True, "ignored": True, "reason": "bot_message"}
        message = telegram_client_inbound_message(message_payload)
        if not message.text and not message.has_photo:
            return {"ok": True, "ignored": True, "reason": "empty_message"}
        client_id = self._client_id_for_message(message)
        dedup_key = f"{message.chat_id}:{message.message_id}" if message.message_id else ""
        if dedup_key and dedup_key in self.processed:
            return {"ok": True, "ignored": True, "reason": "duplicate", "message_id": message.message_id}
        if dedup_key:
            self.processed.add(dedup_key)

        history_key = telegram_client_history_key(message)
        if message.text.strip().casefold().startswith("/start"):
            text = (
                "Здравствуйте! Я отдел заботы Ольги: помогу с консультацией, записью и уходом после процедур.\n\n"
                "Вы уже были у Ольги на процедуре? Если да, пришлите, пожалуйста, телефон, по которому записывались. "
                "Если нет — напишите, какая процедура интересует и в каком городе удобно."
            )
            send_result = await asyncio.to_thread(self.bot.send_message, message.chat_id, text)
            self._remember(history_key, "user", _history_user_content(message))
            self._remember(history_key, "assistant", text)
            self._record_client_interaction(client_id, message, intent="start")
            return {"ok": True, "action": "start", "reply": text, "send": send_result, "conversation_key": history_key}

        self._record_client_interaction(client_id, message)
        if _client_requested_no_contact(message.text):
            self.care_crm.update_client_flags(client_id, do_not_contact=True, consent_status="denied")
            text = "Хорошо, больше не буду писать первой. Если понадобится помощь или запись, вы всегда можете написать сюда."
            send_result = await asyncio.to_thread(self.bot.send_message, message.chat_id, text)
            self._remember(history_key, "user", _history_user_content(message))
            self._remember(history_key, "assistant", text)
            return {"ok": True, "action": "do_not_contact", "reply": text, "send": send_result, "conversation_key": history_key}

        if _client_reported_risk(message.text):
            self.care_crm.update_client_flags(client_id, complaint_risk=True)
            self.learning.upsert_preference(
                client_id,
                preference_type="safety_context",
                value=_truncate_plain(message.text, 500),
                source="telegram_client_risk_fallback",
                confidence=0.8,
            )
            text = (
                "Понимаю, спасибо, что написали. По таким ощущениям лучше, чтобы Ольга посмотрела лично по контексту процедуры. "
                "Я передам ей сообщение, а пока не буду предлагать новые процедуры."
            )
            send_result = await asyncio.to_thread(self.bot.send_message, message.chat_id, text)
            handoff = Handoff(
                reason=HandoffReason.COMPLAINT_OR_RISK,
                message=message,
                summary="Telegram client reported possible post-procedure risk/symptom. Do not upsell until Olga reviews.",
            )
            handoff_result = await self.handoff_notifier.notify(handoff)
            self._remember(history_key, "user", _history_user_content(message))
            self._remember(history_key, "assistant", text)
            self.care_crm.add_interaction(
                client_id,
                channel="telegram_client",
                direction="outbound_bot",
                author="bot",
                body=text,
                intent="complaint_or_risk_support",
            )
            return {
                "ok": True,
                "action": "complaint_or_risk",
                "reply": text,
                "send": send_result,
                "handoff_notify": handoff_result,
                "conversation_key": history_key,
            }

        conversation_history = self._conversation_history(message, history_key)
        decision = await self.consultant.respond(message, conversation_history=conversation_history)
        send_result = None
        if decision.reply:
            send_result = await asyncio.to_thread(self.bot.send_message, message.chat_id, decision.reply)
        handoff_result = await self.handoff_notifier.notify(decision.handoff) if decision.handoff else None
        if decision.handoff and decision.handoff.reason.value == "complaint_or_risk":
            self.care_crm.update_client_flags(client_id, complaint_risk=True)
        self._remember(history_key, "user", _history_user_content(message))
        self._remember(history_key, "assistant", _history_assistant_content(decision))
        if decision.reply:
            self.care_crm.add_interaction(
                client_id,
                channel="telegram_client",
                direction="outbound_bot",
                author="bot",
                body=decision.reply,
                intent=str(decision.action or "bot_reply"),
            )
        self.learning.record_outcome(
            agent_role=self.profile.prompt_role,
            input_ref=history_key,
            decision={
                "action": str(decision.action or ""),
                "handoff": decision.handoff.reason.value if decision.handoff else "",
                "appointment_id": decision.appointment_id,
            },
            outcome="reply_sent" if decision.reply else "no_reply",
        )
        return {
            "ok": True,
            "action": decision.action,
            "reply": decision.reply,
            "appointment_id": decision.appointment_id,
            "handoff": decision.handoff.reason.value if decision.handoff else None,
            "send": send_result,
            "handoff_notify": handoff_result,
            "planner": decision.metadata.get("planner"),
            "dry_run": not self.settings.yclients_allow_mutations,
            "conversation_key": history_key,
        }

    def _client_id_for_message(self, message: InboundMessage) -> int:
        phone = _extract_phone(message.text)
        if phone:
            matches = self.care_crm.search_clients(phone, limit=3)
            if matches:
                client_id = int(matches[0]["id"])
                self.care_crm.link_client_channel(
                    client_id,
                    channel="telegram_client",
                    external_user_id=str(message.metadata.get("telegram_user_id") or message.client_id),
                    chat_id=message.chat_id,
                    username=str(message.metadata.get("username") or ""),
                    display_name=str(message.metadata.get("client_name") or ""),
                    verified=True,
                )
                return client_id
        linked = self.care_crm.find_client_by_link(
            channel="telegram_client",
            external_user_id=str(message.metadata.get("telegram_user_id") or message.client_id),
            chat_id=message.chat_id,
        )
        if linked:
            return int(linked["id"])
        return self.care_crm.ensure_telegram_client(
            telegram_user_id=str(message.metadata.get("telegram_user_id") or message.client_id),
            chat_id=message.chat_id,
            username=str(message.metadata.get("username") or ""),
            display_name=str(message.metadata.get("client_name") or ""),
        )

    def _record_client_interaction(self, client_id: int, message: InboundMessage, *, intent: str = "client_message") -> None:
        body = _history_user_content(message)
        if not body.strip():
            return
        self.care_crm.add_interaction(
            client_id,
            channel="telegram_client",
            direction="inbound_client",
            author=str(message.metadata.get("client_name") or message.client_id),
            body=body,
            intent=intent,
            metadata={
                "message_id": message.message_id,
                "telegram_user_id": message.metadata.get("telegram_user_id"),
                "username": message.metadata.get("username"),
                "photo_ids": message.metadata.get("photo_ids") or [],
            },
        )

    def _conversation_history(self, message: InboundMessage, history_key: str) -> list[dict[str, Any]]:
        history = self.history_store.recent_codex_chat(self.settings.telegram_admin_history_limit, history_key)
        crm_context = self._crm_context_for_message(message)
        if crm_context:
            history.append({"role": "system", "content": crm_context, "created_at": ""})
        return history

    def _crm_context_for_message(self, message: InboundMessage) -> str:
        linked = self.care_crm.find_client_by_link(
            channel="telegram_client",
            external_user_id=str(message.metadata.get("telegram_user_id") or message.client_id),
            chat_id=message.chat_id,
        )
        clients: list[dict[str, Any]] = [linked] if linked else []
        queries = _crm_identity_queries(message)
        for query in queries:
            matches = self.care_crm.search_clients(query, limit=3)
            if not matches:
                continue
            seen_ids = {int(client.get("id") or 0) for client in clients}
            clients.extend(match for match in matches if int(match.get("id") or 0) not in seen_ids)
            break
        base = self._crm_context_from_clients(clients[:3]) if clients else ""
        lesson_context = self._learning_context_for_message(message)
        return "\n".join(part for part in (base, lesson_context) if part)

    def _crm_context_from_clients(self, clients: list[dict[str, Any]]) -> str:
        lines = ["CRM context: possible returning client matches."]
        for client in clients[:3]:
            client_id = int(client.get("id") or 0)
            if client_id:
                lines.append(self.client_memory.summary(client_id, include_internal=False))
        return "\n".join(lines)

    def _learning_context_for_message(self, message: InboundMessage) -> str:
        queries = _crm_identity_queries(message)
        lessons: list[dict[str, Any]] = []
        for query in queries[:2]:
            lessons.extend(self.learning.lessons(query=query, limit=2))
        if not lessons:
            lessons = self.learning.lessons(tags=("followup",), limit=2)
        seen: set[int] = set()
        lines = ["Learning context: durable lessons for tone/safety/preferences."]
        for lesson in lessons[:4]:
            lesson_id = int(lesson.get("id") or 0)
            if lesson_id in seen:
                continue
            seen.add(lesson_id)
            lines.append(f"Lesson: {lesson.get('lesson')}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _remember(self, history_key: str, role: str, content: str) -> None:
        if not content.strip():
            return
        self.history_store.add_codex_chat_message(role, content, history_key)


class CareFollowupDeliveryService:
    """Sends due local care follow-up tasks to linked Telegram clients when enabled."""

    def __init__(self, care_crm: CareCrmStore, bot: TelegramBot) -> None:
        self.care_crm = care_crm
        self.bot = bot

    async def send_due(self, *, now: datetime | None = None, limit: int = 20) -> dict[str, Any]:
        due_before = (now or datetime.now(timezone.utc)).isoformat()
        tasks = self.care_crm.list_followup_tasks(status="planned", due_before=due_before, limit=limit)
        sent: list[int] = []
        blocked: list[int] = []
        waiting_link: list[int] = []
        for task in tasks:
            task_id = int(task.get("id") or 0)
            gate = self.care_crm.followup_send_gate(task_id)
            if gate["status"] == "needs_channel":
                self.care_crm.update_followup_task(
                    task_id,
                    requires_channel_resolution=True,
                    outcome="needs_channel_resolution",
                )
                waiting_link.append(task_id)
                continue
            if not gate.get("allowed"):
                self.care_crm.update_followup_task(
                    task_id,
                    status="blocked",
                    blocked_reason=str(gate.get("reason") or gate.get("status") or "blocked"),
                    risk_level="blocked" if "risk" in str(gate.get("reason") or "") else None,
                )
                blocked.append(task_id)
                continue
            chat_id = str(gate.get("chat_id") or "")
            text = str(task.get("message_draft") or "").strip()
            await asyncio.to_thread(self.bot.send_message, chat_id, text)
            sent_at = datetime.now(timezone.utc).isoformat()
            self.care_crm.mark_followup_task_status(task_id, status="sent", sent_at=sent_at)
            self.care_crm.mark_client_contacted(int(task["client_id"]), contacted_at=sent_at)
            self.care_crm.add_interaction(
                int(task["client_id"]),
                visit_id=int(task.get("visit_id") or 0) or None,
                channel="telegram_client",
                direction="outbound_bot",
                author="followup_delivery",
                body=text,
                intent=str(task.get("kind") or "followup"),
                metadata={"task_id": task_id},
            )
            sent.append(task_id)
        return {"sent": sent, "blocked": blocked, "waiting_link": waiting_link, "checked": len(tasks)}

    async def send_task(self, task_id: int) -> dict[str, Any]:
        task = self.care_crm.get_followup_task(task_id)
        if not task:
            return {"ok": False, "status": "missing", "task_id": task_id}
        gate = self.care_crm.followup_send_gate(task_id)
        if gate["status"] == "needs_channel":
            self.care_crm.update_followup_task(task_id, requires_channel_resolution=True, outcome="needs_channel_resolution")
            return {"ok": False, "status": "needs_channel", "task_id": task_id}
        if not gate.get("allowed"):
            self.care_crm.update_followup_task(
                task_id,
                status="blocked",
                blocked_reason=str(gate.get("reason") or gate.get("status") or "blocked"),
            )
            return {"ok": False, "status": "blocked", "reason": gate.get("reason"), "task_id": task_id}
        chat_id = str(gate.get("chat_id") or "")
        text = str(task.get("message_draft") or "").strip()
        await asyncio.to_thread(self.bot.send_message, chat_id, text)
        sent_at = datetime.now(timezone.utc).isoformat()
        self.care_crm.mark_followup_task_status(task_id, status="sent", sent_at=sent_at)
        self.care_crm.mark_client_contacted(int(task["client_id"]), contacted_at=sent_at)
        self.care_crm.add_interaction(
            int(task["client_id"]),
            visit_id=int(task.get("visit_id") or 0) or None,
            channel="telegram_client",
            direction="outbound_bot",
            author="followup_delivery",
            body=text,
            intent=str(task.get("kind") or "followup"),
            metadata={"task_id": task_id},
        )
        return {"ok": True, "status": "sent", "task_id": task_id, "chat_id": chat_id}

    def skip_task(self, task_id: int) -> dict[str, Any]:
        task = self.care_crm.mark_followup_task_status(task_id, status="skipped")
        return {"ok": bool(task), "status": "skipped" if task else "missing", "task_id": task_id}


def telegram_client_inbound_message(message: dict[str, Any]) -> InboundMessage:
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id = str(chat.get("id") or "")
    user_id = str(sender.get("id") or chat_id)
    text = str(message.get("text") or message.get("caption") or "").strip()
    photos = message.get("photo") if isinstance(message.get("photo"), list) else []
    return InboundMessage(
        channel=Channel.TELEGRAM_CLIENT,
        client_id=user_id,
        chat_id=chat_id,
        message_id=str(message.get("message_id") or ""),
        text=text,
        created_at=int(message.get("date") or 0),
        has_photo=bool(photos),
        metadata={
            "transport": "telegram_client",
            "telegram_user_id": user_id,
            "username": str(sender.get("username") or ""),
            "client_name": _telegram_display_name(sender),
            "photo_ids": [str(photo.get("file_id") or "") for photo in photos if isinstance(photo, dict) and photo.get("file_id")],
        },
    )


def telegram_client_history_key(message: InboundMessage) -> str:
    return conversation_key("telegram_client", CodexRole.TELEGRAM_CLIENT, message.chat_id or message.client_id)


def _history_user_content(message: InboundMessage) -> str:
    parts = []
    if message.message_id:
        parts.append(f"message_id: {message.message_id}")
    if message.created_at:
        parts.append(f"created_at: {message.created_at}")
    if message.metadata.get("client_name"):
        parts.append(f"client_name: {message.metadata['client_name']}")
    if message.metadata.get("username"):
        parts.append(f"username: @{message.metadata['username']}")
    if message.text:
        parts.append(message.text)
    if message.has_photo:
        parts.append("[photo]")
    return "\n".join(parts).strip() or "[empty]"


def _history_assistant_content(decision: Any) -> str:
    parts = [str(getattr(decision, "reply", "") or "").strip()]
    handoff = getattr(decision, "handoff", None)
    if handoff:
        parts.append(f"handoff: {handoff.reason.value}")
    return "\n".join(part for part in parts if part)[-4000:]


def _crm_identity_queries(message: InboundMessage) -> list[str]:
    raw_values = [
        str(message.metadata.get("client_name") or ""),
        str(message.metadata.get("username") or ""),
        message.text,
    ]
    queries: list[str] = []
    for value in raw_values:
        value = " ".join(str(value or "").replace("@", " ").split()).strip()
        if len(value) >= 3 and value not in queries:
            queries.append(value)
    phone = _extract_phone(message.text)
    if phone and phone not in queries:
        queries.insert(0, phone)
    return queries[:4]


def _extract_phone(text: str) -> str:
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    if len(digits) >= 10:
        return digits
    return ""


def _client_requested_no_contact(text: str) -> bool:
    lowered = " ".join(str(text or "").casefold().split())
    if not lowered:
        return False
    return any(
        phrase in lowered
        for phrase in (
            "не пишите",
            "не писать",
            "не надо писать",
            "отпишитесь",
            "стоп",
            "stop",
        )
    )


def _client_reported_risk(text: str) -> bool:
    lowered = " ".join(str(text or "").casefold().split())
    if not lowered:
        return False
    return any(
        phrase in lowered
        for phrase in (
            "отёк",
            "отек",
            "болит",
            "боль",
            "синяк",
            "аллерг",
            "температура",
            "гной",
            "кровит",
            "жалоба",
            "недовольна",
            "недоволен",
            "плохо после",
            "осложнение",
        )
    )


def _truncate_plain(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _telegram_display_name(sender: dict[str, Any]) -> str:
    name = " ".join(part for part in (str(sender.get("first_name") or ""), str(sender.get("last_name") or "")) if part).strip()
    username = str(sender.get("username") or "").strip()
    return name or (f"@{username}" if username else "")


async def run_telegram_client_polling(settings: IntegrationSettings | None = None) -> None:
    bot = TelegramClientCareBot(settings=settings)
    await bot.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    asyncio.run(run_telegram_client_polling())
