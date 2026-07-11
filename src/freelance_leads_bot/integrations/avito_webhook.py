from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request

from .agent_tools import AutomationToolbox
from .agent_trace import JsonlAgentTraceLogger
from .avito import avito_inbound_message, is_avito_message_event
from .avito_consultant import AvitoAgentPlanner, AvitoConsultant, CodexToolLoopPlanner
from .avito_dedup import PersistentProcessedEventStore
from .avito_media import AvitoApiPhotoResolver, AvitoPhotoResolver, enrich_reply_handoff_photos
from .avito_history import prepare_avito_outgoing_text, remember_avito_outgoing
from .avito_sender import AvitoSender, avito_sender_from_settings
from .avito_turn_buffer import batch_to_inbound_message, enqueue_avito_turn_message, pop_due_avito_turn_batches
from .avito_voice import AvitoApiVoiceResolver, AvitoVoiceResolver
from .config import IntegrationSettings
from .codex_planner import CodexPlannerRunner
from .codex_review import AvitoDraftReviewer, CodexDraftReviewer
from .handoff_notify import HandoffNotifier, handoff_notifier_from_settings
from .mentor_memory import MentorMemoryService
from .expert_rag import ExpertRagStore
from .runtime import booking_from_settings, rag_retrieval_from_settings
from .roles import CodexRole, legacy_runtime_status, role_profile
from .yclients import YClientsGateway
from ..storage import LeadStore


processed_events = PersistentProcessedEventStore()
app = FastAPI(title="Automatic Cosmetic Avito Webhook")
DELETED_MESSAGE_TEXTS = {"сообщение удалено", "message deleted"}
WEBHOOK_LOG_PATH = Path("data/avito_webhook.log")
AVITO_DEBOUNCE_WORKER_INTERVAL_SECONDS = 2.0


def _log_webhook(row: dict[str, Any]) -> None:
    WEBHOOK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": int(time.time()), **row}
    with WEBHOOK_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def get_settings() -> IntegrationSettings:
    return IntegrationSettings.from_env()


def get_booking(settings: IntegrationSettings = Depends(get_settings)) -> YClientsGateway:
    return booking_from_settings(settings)


def get_sender(settings: IntegrationSettings = Depends(get_settings)) -> AvitoSender:
    return avito_sender_from_settings(settings)


def get_handoff_notifier(settings: IntegrationSettings = Depends(get_settings)) -> HandoffNotifier:
    return handoff_notifier_from_settings(settings)


def get_photo_resolver(settings: IntegrationSettings = Depends(get_settings)) -> AvitoPhotoResolver | None:
    if settings.avito_ready:
        return AvitoApiPhotoResolver(settings)
    return None


def get_voice_resolver(settings: IntegrationSettings = Depends(get_settings)) -> AvitoVoiceResolver | None:
    if settings.avito_ready:
        return AvitoApiVoiceResolver(settings)
    return None


def get_history_store(settings: IntegrationSettings = Depends(get_settings)) -> LeadStore:
    return LeadStore(settings.telegram_admin_history_db_path)


def get_toolbox(
    booking: YClientsGateway = Depends(get_booking),
    notifier: HandoffNotifier = Depends(get_handoff_notifier),
) -> AutomationToolbox:
    return AutomationToolbox(
        booking,
        role_profile=role_profile(CodexRole.AVITO_CLIENT),
        operations_notifier=notifier,
    )


def get_planner(settings: IntegrationSettings = Depends(get_settings)) -> AvitoAgentPlanner | None:
    if not settings.avito_codex_enabled:
        return None
    return CodexToolLoopPlanner(
        CodexPlannerRunner(timeout_seconds=settings.avito_codex_timeout_seconds),
        max_steps=settings.avito_codex_max_steps,
        trace_logger=JsonlAgentTraceLogger(),
    )


def get_reviewer(settings: IntegrationSettings = Depends(get_settings)) -> AvitoDraftReviewer | None:
    if not settings.avito_codex_enabled:
        return None
    return CodexDraftReviewer(timeout_seconds=settings.avito_codex_timeout_seconds)


def get_expert_rag(settings: IntegrationSettings = Depends(get_settings)) -> ExpertRagStore | None:
    if not settings.rag_retrieval_enabled:
        return None
    return ExpertRagStore(settings.rag_expert_db_path)


def get_mentor_memory(
    toolbox: AutomationToolbox = Depends(get_toolbox),
    expert_rag: ExpertRagStore | None = Depends(get_expert_rag),
) -> MentorMemoryService:
    return MentorMemoryService(toolbox.knowledge, expert_rag=expert_rag)


@app.get("/health")
async def health(settings: IntegrationSettings = Depends(get_settings)) -> dict[str, Any]:
    return {
        "ok": True,
        "public_base_url": settings.public_base_url,
        "yclients_ready": settings.yclients_ready,
        "yclients_allow_mutations": settings.yclients_allow_mutations,
        "avito_ready": settings.avito_ready,
        "avito_send_enabled": settings.avito_send_enabled,
        "avito_codex_enabled": settings.avito_codex_enabled,
        "avito_turn_debounce_seconds": settings.avito_turn_debounce_seconds,
        "avito_unanswered_autoreply_enabled": settings.avito_unanswered_autoreply_enabled,
        "rag_retrieval_enabled": settings.rag_retrieval_enabled,
        "rag_autoanswer_threshold": settings.rag_autoanswer_threshold,
        "rag_handoff_threshold": settings.rag_handoff_threshold,
        "handoff_notify_ready": settings.handoff_notify_ready,
        "vk_ready": settings.vk_ready,
        "vk_send_enabled": settings.vk_send_enabled,
        "vk_codex_enabled": settings.vk_codex_enabled,
        "codex_roles": [role.value for role in CodexRole],
        "legacy_runtime": legacy_runtime_status(),
    }


@app.on_event("startup")
async def start_avito_turn_debounce_worker() -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    settings = IntegrationSettings.from_env()
    if settings.avito_turn_debounce_seconds <= 0:
        return
    app.state.avito_turn_debounce_task = asyncio.create_task(_avito_turn_debounce_worker())
    _log_webhook({"event": "debounce_worker_started", "seconds": settings.avito_turn_debounce_seconds})


@app.on_event("shutdown")
async def stop_avito_turn_debounce_worker() -> None:
    task = getattr(app.state, "avito_turn_debounce_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _avito_turn_debounce_worker() -> None:
    while True:
        try:
            settings = IntegrationSettings.from_env()
            if settings.avito_turn_debounce_seconds > 0:
                await process_due_avito_turn_batches(settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_webhook({"event": "debounce_worker_error", "error": repr(exc)})
        await asyncio.sleep(AVITO_DEBOUNCE_WORKER_INTERVAL_SECONDS)


async def process_due_avito_turn_batches(settings: IntegrationSettings | None = None) -> list[dict[str, Any]]:
    settings = settings or IntegrationSettings.from_env()
    due_batches = pop_due_avito_turn_batches()
    results: list[dict[str, Any]] = []
    if not due_batches:
        return results
    handoff_notifier = handoff_notifier_from_settings(settings)
    toolbox = AutomationToolbox(
        booking_from_settings(settings),
        role_profile=role_profile(CodexRole.AVITO_CLIENT),
        operations_notifier=handoff_notifier,
    )
    planner = get_planner(settings)
    sender = avito_sender_from_settings(settings)
    photo_resolver = AvitoApiPhotoResolver(settings) if settings.avito_ready else None
    history_store = LeadStore(settings.telegram_admin_history_db_path)
    reviewer = get_reviewer(settings)
    expert_rag = ExpertRagStore(settings.rag_expert_db_path) if settings.rag_retrieval_enabled else None
    mentor_memory = MentorMemoryService(toolbox.knowledge, expert_rag=expert_rag)
    for batch in due_batches:
        message = batch_to_inbound_message(batch)
        try:
            result = await process_avito_message(
                message=message,
                settings=settings,
                toolbox=toolbox,
                planner=planner,
                sender=sender,
                handoff_notifier=handoff_notifier,
                photo_resolver=photo_resolver,
                history_store=history_store,
                reviewer=reviewer,
                mentor_memory=mentor_memory,
                expert_rag=expert_rag,
            )
        except Exception as exc:
            result = {"ok": False, "error": repr(exc), "chat_id": message.chat_id, "message_id": message.message_id}
            _log_webhook({"event": "debounce_batch_error", **result})
        else:
            _log_webhook(
                {
                    "event": "debounce_batch_processed" if not result.get("ignored") else "debounce_batch_ignored",
                    "reason": result.get("reason"),
                    "chat_id": message.chat_id,
                    "message_id": message.message_id,
                    "batch_size": message.metadata.get("batch_size", 1),
                    "action": result.get("action"),
                    "send": result.get("send"),
                    "handoff": result.get("handoff"),
                    "handoff_notify": result.get("handoff_notify"),
                }
            )
        results.append(result)
    return results


@app.post("/avito/webhook")
async def avito_webhook(
    request: Request,
    settings: IntegrationSettings = Depends(get_settings),
    toolbox: AutomationToolbox = Depends(get_toolbox),
    planner: AvitoAgentPlanner | None = Depends(get_planner),
    sender: AvitoSender = Depends(get_sender),
    handoff_notifier: HandoffNotifier = Depends(get_handoff_notifier),
    photo_resolver: AvitoPhotoResolver | None = Depends(get_photo_resolver),
    voice_resolver: AvitoVoiceResolver | None = Depends(get_voice_resolver),
    history_store: LeadStore = Depends(get_history_store),
    reviewer: AvitoDraftReviewer | None = Depends(get_reviewer),
    mentor_memory: MentorMemoryService | None = Depends(get_mentor_memory),
    expert_rag: ExpertRagStore | None = Depends(get_expert_rag),
) -> dict[str, Any]:
    if request.query_params.get("token") != settings.avito_webhook_secret:
        raise HTTPException(status_code=403, detail="forbidden")

    event = await request.json()
    if not isinstance(event, dict) or not is_avito_message_event(event):
        _log_webhook({"event": "ignored", "reason": "not_message_event"})
        return {"ok": True, "ignored": True, "reason": "not_message_event"}

    message = annotate_avito_message_actor(avito_inbound_message(event), settings)
    message = await transcribe_avito_voice_message(message, voice_resolver=voice_resolver)
    ignore_reason = _ignore_reason(event, message, settings)
    if ignore_reason:
        _log_webhook(
            {
                "event": "ignored",
                "reason": ignore_reason,
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "message_type": message.metadata.get("message_type"),
                "text_preview": _text_preview(message.text),
                "voice_id": message.metadata.get("voice_id"),
                "voice_transcription_error": message.metadata.get("voice_transcription_error"),
            }
        )
        return {"ok": True, "ignored": True, "reason": ignore_reason, "message_id": message.message_id}

    dedup_key = f"{message.chat_id}:{message.message_id}" if message.message_id else ""
    if not processed_events.mark_once(dedup_key):
        _log_webhook({"event": "ignored", "reason": "duplicate", "chat_id": message.chat_id, "message_id": message.message_id})
        return {"ok": True, "ignored": True, "reason": "duplicate", "message_id": message.message_id}

    if settings.avito_turn_debounce_seconds > 0:
        queued = enqueue_avito_turn_message(
            message,
            debounce_seconds=settings.avito_turn_debounce_seconds,
            max_wait_seconds=settings.avito_turn_max_wait_seconds,
            max_messages=settings.avito_turn_batch_max_messages,
        )
        _log_webhook(
            {
                "event": "queued",
                "reason": "turn_debounce",
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "queue": queued,
            }
        )
        return {"ok": True, "queued": True, "reason": "turn_debounce", "message_id": message.message_id, "queue": queued}

    result = await process_avito_message(
        message=message,
        settings=settings,
        toolbox=toolbox,
        planner=planner,
        sender=sender,
        handoff_notifier=handoff_notifier,
        photo_resolver=photo_resolver,
        history_store=history_store,
        reviewer=reviewer,
        mentor_memory=mentor_memory,
        expert_rag=expert_rag,
    )
    _log_webhook(
        {
            "event": "processed" if not result.get("ignored") else "ignored",
            "reason": result.get("reason"),
            "chat_id": message.chat_id,
            "message_id": message.message_id,
            "action": result.get("action"),
            "send": result.get("send"),
            "handoff": result.get("handoff"),
            "handoff_notify": result.get("handoff_notify"),
        }
    )
    return result


async def transcribe_avito_voice_message(message: Any, *, voice_resolver: AvitoVoiceResolver | None) -> Any:
    if not voice_resolver:
        return message
    if str(message.metadata.get("message_type") or "") != "voice":
        return message
    if message.text.strip():
        return message
    try:
        return await voice_resolver.transcribe(message)
    except Exception as exc:
        return replace(
            message,
            metadata={
                **message.metadata,
                "voice_transcription_error": repr(exc),
            },
        )


async def process_avito_message(
    *,
    message: Any,
    settings: IntegrationSettings,
    toolbox: AutomationToolbox,
    planner: AvitoAgentPlanner | None,
    sender: AvitoSender,
    handoff_notifier: HandoffNotifier,
    photo_resolver: AvitoPhotoResolver | None,
    history_store: LeadStore | None = None,
    reviewer: AvitoDraftReviewer | None = None,
    mentor_memory: MentorMemoryService | None = None,
    expert_rag: ExpertRagStore | None = None,
    force_unanswered_autoreply: bool = False,
) -> dict[str, Any]:
    consultant = AvitoConsultant(
        toolbox,
        cities=settings.cities,
        planner=planner,
        profile=role_profile(CodexRole.AVITO_CLIENT),
        expert_rag=expert_rag,
        rag_retrieval=rag_retrieval_from_settings(settings),
        rag_autoanswer_threshold=settings.rag_autoanswer_threshold,
        rag_handoff_threshold=settings.rag_handoff_threshold,
    )
    conversation_key = f"avito:client:{message.chat_id or message.client_id}"
    conversation_history = history_store.recent_codex_chat(settings.telegram_admin_history_limit, conversation_key) if history_store else []
    if history_store and _history_has_message_id(conversation_history, message.message_id) and not force_unanswered_autoreply:
        return {
            "ok": True,
            "ignored": True,
            "reason": "duplicate_history",
            "message_id": message.message_id,
            "conversation_key": conversation_key,
        }
    if _is_ack_after_pending_reply(message, conversation_history):
        if history_store:
            history_store.add_codex_chat_message("user", _history_user_content(message), conversation_key)
        return {
            "ok": True,
            "ignored": True,
            "reason": "client_ack_after_pending_reply",
            "message_id": message.message_id,
            "conversation_key": conversation_key,
        }
    if _looks_like_own_echo(conversation_history, message):
        return {
            "ok": True,
            "ignored": True,
            "reason": "assistant_echo",
            "message_id": message.message_id,
            "conversation_key": conversation_key,
        }
    if _is_empty_chat_prompt(message):
        reply = prepare_avito_outgoing_text(history_store, message.chat_id, "Здравствуйте! Подскажите, пожалуйста, какой у вас вопрос?")
        send_result = await sender.send_message(_account_id(message.metadata.get("account_id"), settings), message.chat_id, reply)
        if history_store:
            history_store.add_codex_chat_message("user", _history_user_content(message), conversation_key)
            remember_avito_outgoing(history_store, message.chat_id, reply)
        return {
            "ok": True,
            "action": "empty_chat_greeting",
            "reply": reply,
            "appointment_id": None,
            "handoff": None,
            "slots": [],
            "dry_run": not settings.yclients_allow_mutations,
            "send": send_result,
            "handoff_notify": None,
            "mentor_memory": {},
            "planner": None,
            "draft_review": None,
            "conversation_key": conversation_key,
        }
    decision = await consultant.respond(message, conversation_history=conversation_history)
    account_id = _account_id(message.metadata.get("account_id"), settings)
    decision = await enrich_reply_handoff_photos(decision, resolver=photo_resolver, account_id=account_id)
    if reviewer:
        decision = await reviewer.review(message=message, decision=decision, conversation_history=conversation_history)
    outgoing_reply = prepare_avito_outgoing_text(history_store, message.chat_id, decision.reply)
    send_result = await sender.send_message(account_id, message.chat_id, outgoing_reply) if outgoing_reply else {"sent": False, "reason": "empty_reply"}
    handoff_result = await handoff_notifier.notify(decision.handoff) if decision.handoff else None
    memory_result = mentor_memory.observe_client_decision(message=message, decision=decision, send_result=send_result) if mentor_memory else None
    if history_store:
        history_store.add_codex_chat_message("user", _history_user_content(message), conversation_key)
        remember_avito_outgoing(history_store, message.chat_id, outgoing_reply or decision.reply)
    return {
        "ok": True,
        "action": decision.action,
        "reply": outgoing_reply,
        "appointment_id": decision.appointment_id,
        "handoff": decision.handoff.reason.value if decision.handoff else None,
        "slots": [slot.starts_at.isoformat() for slot in decision.slots],
        "dry_run": not settings.yclients_allow_mutations,
        "send": send_result,
        "handoff_notify": handoff_result,
        "mentor_memory": _memory_result_data(memory_result),
        "planner": decision.metadata.get("planner"),
        "draft_review": decision.metadata.get("draft_review"),
        "conversation_key": decision.metadata.get("conversation_key") or conversation_key,
    }


def _history_user_content(message: Any) -> str:
    parts = []
    if message.message_id:
        parts.append(f"message_id: {message.message_id}")
    if message.created_at:
        parts.append(f"created_at: {message.created_at}")
    if message.text:
        parts.append(str(message.text))
    if message.has_photo:
        parts.append("[photo]")
    if message.listing and message.listing.has_listing:
        listing = message.listing
        listing_parts = [part for part in (listing.title, listing.price_string, listing.city) if part]
        if listing_parts:
            parts.append("Объявление: " + " | ".join(listing_parts))
    return "\n".join(parts).strip() or "[empty]"


def _history_has_message_id(history: list[dict[str, Any]], message_id: str) -> bool:
    if not message_id:
        return False
    needle = f"message_id: {message_id}"
    return any(str(item.get("role") or "") == "user" and needle in str(item.get("content") or "") for item in history)


def _looks_like_own_echo(history: list[dict[str, Any]], message: Any) -> bool:
    direction = str(message.metadata.get("direction") or "").lower()
    author_id = str(message.metadata.get("author_id") or "").strip()
    if direction == "in" or author_id:
        return False
    text = _normalize_echo_text(message.text)
    if not text:
        return False
    for item in history[-8:]:
        if str(item.get("role") or "") != "assistant":
            continue
        if _normalize_echo_text(str(item.get("content") or "")).startswith(text):
            return True
    return False


def _history_assistant_content(decision: Any) -> str:
    return str(decision.reply or "").strip()[-4000:]


def _normalize_echo_text(text: str) -> str:
    return " ".join(str(text or "").casefold().split())[:1000]


def _memory_result_data(result: Any) -> dict[str, Any]:
    if not result:
        return {}
    return {
        "created": [item.id for item in getattr(result, "created", [])],
        "skipped": list(getattr(result, "skipped", [])),
    }


def _text_preview(text: str, limit: int = 180) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _account_id(raw: Any, settings: IntegrationSettings) -> int:
    try:
        parsed = int(raw or 0)
    except (TypeError, ValueError):
        parsed = 0
    return parsed or settings.avito_account_id


def _is_own_message(raw_author_id: Any, settings: IntegrationSettings) -> bool:
    try:
        author_id = int(raw_author_id or 0)
    except (TypeError, ValueError):
        return False
    own_account_ids = set(settings.avito_account_ids)
    if settings.avito_account_id:
        own_account_ids.add(settings.avito_account_id)
    return bool(author_id and author_id in own_account_ids)


def annotate_avito_message_actor(message: Any, settings: IntegrationSettings) -> Any:
    metadata = dict(message.metadata or {})
    is_own = _is_own_message(metadata.get("author_id"), settings)
    direction = str(metadata.get("direction") or "").lower()
    metadata["is_own_account"] = is_own
    metadata["author_role"] = "own_account" if is_own else "client"
    metadata["direction"] = direction
    return replace(message, metadata=metadata)


def _ignore_reason(event: dict[str, Any], message: Any, settings: IntegrationSettings) -> str:
    value = event.get("payload", {}).get("value") if isinstance(event.get("payload"), dict) else event
    if not isinstance(value, dict):
        value = event

    direction = str(message.metadata.get("direction") or value.get("direction") or event.get("direction") or "").lower()
    if direction and direction != "in":
        return "not_incoming"

    message_type = str(value.get("type") or "").lower()
    if message_type == "system" and not _is_empty_chat_prompt(message):
        return "system"

    text = str(message.text or "").strip().lower()
    if message_type == "deleted" or text in DELETED_MESSAGE_TEXTS:
        return "deleted_message"

    raw_author = message.metadata.get("author_id") or value.get("author_id") or value.get("user_id")
    if bool(message.metadata.get("is_own_account")) or _is_own_message(raw_author, settings):
        return "own_message"

    if message.metadata.get("voice_transcription_error"):
        return "voice_transcription_error"

    if not message.text and not message.has_photo:
        return "empty"

    return ""


def _is_empty_chat_prompt(message: Any) -> bool:
    text = str(message.text or "").casefold()
    return "создал чат" in text and "пока ничего не написал" in text


def _is_ack_after_pending_reply(message: Any, history: list[dict[str, Any]]) -> bool:
    text = _normalize_ack_text(message.text)
    if not text or not any(ack == text or text.startswith(ack + " ") for ack in ("хорошо", "жду", "ок", "окей", "ладно", "спасибо", "спасибо жду")):
        return False
    for item in history[-6:]:
        if str(item.get("role") or "") != "assistant":
            continue
        content = _normalize_echo_text(str(item.get("content") or ""))
        if any(marker in content for marker in ("уточню", "проверю", "проверим", "напишу вам", "передам", "вернёмся", "вернемся")):
            return True
    return False


def _normalize_ack_text(text: str) -> str:
    normalized = _normalize_echo_text(text)
    for char in ",.!?;:…":
        normalized = normalized.replace(char, "")
    return " ".join(normalized.split())
