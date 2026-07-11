from __future__ import annotations

import asyncio
import logging
from typing import Any

from .avito_consultant import AvitoAgentPlanner, AvitoConsultant
from .config import IntegrationSettings
from .handoff_notify import HandoffNotifier, handoff_notifier_from_settings
from .runtime import booking_from_settings, codex_planner_from_settings, rag_retrieval_from_settings, toolbox_from_settings
from .roles import CodexRole, conversation_key, role_profile
from .vk import VKLongPollServer, is_vk_message_new, vk_inbound_message
from .vk_sender import VKClient, VKSender, vk_sender_from_settings


logger = logging.getLogger(__name__)


class VKBot:
    """VK Long Poll bot that reuses the same tool-first client consultant as Avito."""

    def __init__(
        self,
        *,
        settings: IntegrationSettings | None = None,
        vk: VKClient | None = None,
        sender: VKSender | None = None,
        handoff_notifier: HandoffNotifier | None = None,
        planner: AvitoAgentPlanner | None = None,
    ) -> None:
        self.settings = settings or IntegrationSettings.from_env()
        booking = booking_from_settings(self.settings)
        self.profile = role_profile(CodexRole.VK_CLIENT)
        self.vk = vk or VKClient(self.settings)
        self.sender = sender or vk_sender_from_settings(self.settings)
        self.handoff_notifier = handoff_notifier or handoff_notifier_from_settings(self.settings)
        self.toolbox = toolbox_from_settings(
            self.settings,
            booking,
            self.profile,
            operations_notifier=self.handoff_notifier,
        )
        self.planner = planner or codex_planner_from_settings(self.settings, enabled=self.settings.vk_codex_enabled)
        self.consultant = AvitoConsultant(
            self.toolbox,
            cities=self.settings.cities,
            planner=self.planner,
            profile=self.profile,
            rag_retrieval=rag_retrieval_from_settings(self.settings),
            rag_autoanswer_threshold=self.settings.rag_autoanswer_threshold,
            rag_handoff_threshold=self.settings.rag_handoff_threshold,
        )
        self.processed: set[str] = set()

    async def run(self) -> None:
        server = await self.vk.get_long_poll_server()
        logger.info("VK bot started group_id=%s send_enabled=%s", self.settings.vk_group_id, self.settings.vk_send_enabled)
        while True:
            try:
                server = await self.poll_once(server)
            except Exception:
                logger.exception("VK polling error")
                await asyncio.sleep(5)
                server = await self.vk.get_long_poll_server()

    async def poll_once(self, server: VKLongPollServer) -> VKLongPollServer:
        payload = await self.vk.poll(server)
        if payload.get("failed"):
            return await self.vk.get_long_poll_server()
        server.ts = str(payload.get("ts") or server.ts)
        for update in payload.get("updates") or []:
            await self.handle_update(update)
        return server

    async def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        if not is_vk_message_new(update):
            return {"ok": True, "ignored": True, "reason": "not_message_new"}
        message = vk_inbound_message(update)
        if not message.text and not message.has_photo:
            return {"ok": True, "ignored": True, "reason": "empty_message"}
        dedup_key = f"{message.chat_id}:{message.message_id}" if message.message_id else ""
        if dedup_key and dedup_key in self.processed:
            return {"ok": True, "ignored": True, "reason": "duplicate", "message_id": message.message_id}
        if dedup_key:
            self.processed.add(dedup_key)

        decision = await self.consultant.respond(message)
        send_result = await self.sender.send_message(_peer_id(message.chat_id), decision.reply)
        handoff_result = await self.handoff_notifier.notify(decision.handoff) if decision.handoff else None
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
            "conversation_key": conversation_key("vk", CodexRole.VK_CLIENT, message.chat_id or message.client_id),
        }


async def run_vk_polling(settings: IntegrationSettings | None = None) -> None:
    bot = VKBot(settings=settings)
    try:
        await bot.run()
    finally:
        await bot.vk.aclose()
        if hasattr(bot.sender, "aclose"):
            await bot.sender.aclose()  # type: ignore[attr-defined]


def _peer_id(chat_id: str) -> int:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    asyncio.run(run_vk_polling())
