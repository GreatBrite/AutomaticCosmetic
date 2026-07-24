from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from src.freelance_leads_bot.integrations.avito import (
    avito_inbound_message,
    avito_media_urls,
    avito_photo_handoff,
    avito_photo_urls,
    is_avito_message_event,
)
from src.freelance_leads_bot.integrations.agent_tools import AutomationToolbox, JsonKnowledgeStore
from src.freelance_leads_bot.integrations.agent_trace import JsonlAgentTraceLogger, redact_sensitive
from src.freelance_leads_bot.integrations.admin import AdminResult, TelegramAdminService, parse_admin_command
import src.freelance_leads_bot.integrations.admin_codex as admin_codex_module
from src.freelance_leads_bot.integrations.admin_codex import CodexTelegramAdminRunner, CodexTelegramAdminService, build_admin_codex_prompt
from src.freelance_leads_bot.integrations.city_schedule import CityScheduleStore
from src.freelance_leads_bot.integrations.avito_consultant import (
    AvitoConsultant,
    AvitoConsultantReply,
    CodexAvitoPlanner,
    CodexToolLoopPlanner,
)
from src.freelance_leads_bot.integrations.client_handlers import RagAnswerService
from src.freelance_leads_bot.integrations.client_router import route_client_message
from src.freelance_leads_bot.integrations.booking_flow import AvitoBookingFlow, BookingRequest, extract_date
from src.freelance_leads_bot.integrations.avito_sender import AvitoSdkSender, PreviewAvitoSender
from src.freelance_leads_bot.integrations.avito_history import prepare_avito_outgoing_text, remember_avito_outgoing
from src.freelance_leads_bot.integrations.avito_turn_buffer import batch_to_inbound_message
from src.freelance_leads_bot.integrations.care_crm import (
    CareLearningService,
    CareCrmStore,
    ClientIdentityService,
    ClientMemoryService,
    FollowupBrainService,
    VisitConfirmationService,
    VisitFactService,
    format_visit_confirmation_card,
    parse_actual_visit_details,
    parse_visit_confirmation_callback,
    visit_confirmation_keyboard,
)
from src.freelance_leads_bot.integrations.avito_turn_buffer import (
    batch_to_inbound_message,
    enqueue_avito_turn_message,
    mark_avito_turn_batch_failed,
    mark_avito_turn_batch_processed,
    pop_due_avito_turn_batches,
)
import src.freelance_leads_bot.integrations.avito_webhook as avito_webhook_module
from src.freelance_leads_bot.integrations.avito_webhook import (
    annotate_avito_message_actor,
    app as avito_app,
    get_booking,
    get_handoff_notifier,
    get_history_store,
    get_avito_reader,
    get_photo_resolver,
    get_planner,
    get_reviewer,
    get_sender,
    get_settings,
    get_voice_resolver,
    process_avito_message,
    processing_outcome_from_result,
    processed_events,
)
from src.freelance_leads_bot.integrations.avito_media import enrich_reply_handoff_photos
from src.freelance_leads_bot.integrations.avito_followup_admin import (
    apply_pending_followup_action,
    parse_pending_followup_callback,
    pending_followup_keyboard,
    pending_followup_token,
)
from src.freelance_leads_bot.integrations.codex_planner import build_codex_planner_prompt, parse_codex_step
from src.freelance_leads_bot.integrations.codex_review import (
    apply_review_outcome,
    build_codex_review_prompt,
    sanitize_consultation_language,
)
from src.freelance_leads_bot.integrations.avito_history_import import import_telegram_zip_to_knowledge, parse_telegram_html_export
from src.freelance_leads_bot.integrations.handoff_notify import PreviewHandoffNotifier, format_handoff_message, process_handoff_sla
from src.freelance_leads_bot.integrations.handoff_refs import (
    find_telegram_handoff_ref,
    handoff_ref_is_critical,
    latest_unresolved_handoff_ref_for_chat,
    load_telegram_handoff_refs,
    open_handoff_refs,
    read_open_handoff_refs,
    remember_telegram_handoff_ref,
    save_telegram_handoff_refs,
    update_handoff_status,
)
from src.freelance_leads_bot.integrations.mentor_memory import MentorMemoryService
from src.freelance_leads_bot.integrations.models import (
    Appointment,
    Channel,
    ClientProfile,
    Handoff,
    HandoffReason,
    InboundMessage,
    Service,
    Slot,
    UpsellRule,
)
from src.freelance_leads_bot.integrations.ops_status import (
    build_ops_status_report,
    format_ops_status_report,
    ops_status_exit_code,
    read_avito_poller_status,
    read_data_footprint,
    read_disk_status,
    read_telegram_handoff_status,
    report_data,
)
from src.freelance_leads_bot.integrations.prelaunch import build_prelaunch_report
from src.freelance_leads_bot.integrations.expert_rag_review import DEFAULT_AUDIT_LOG_PATH, review_suggestion, run_review_command, resolve_audit_log_path
import src.freelance_leads_bot.integrations.roles as roles_module
from src.freelance_leads_bot.integrations.roles import CodexRole, conversation_key, legacy_runtime_status, role_profile, role_safety_report
import src.freelance_leads_bot.integrations.telegram_admin_bot as telegram_admin_bot_module
from src.freelance_leads_bot.integrations.telegram_admin_bot import TelegramAdminBotTransport, telegram_delivery_params, telegram_history_key
from src.freelance_leads_bot.integrations.telegram_client_bot import (
    CareFollowupDeliveryService,
    TelegramClientCareBot,
    telegram_client_history_key,
    telegram_client_inbound_message,
)
from src.freelance_leads_bot.storage import LeadStore
from src.freelance_leads_bot.integrations.upsell import CareUpsellPlanner, CareUpsellService, due_upsell_rules
from src.freelance_leads_bot.integrations.vk import is_vk_message_new, vk_inbound_message
from src.freelance_leads_bot.integrations.vk_bot import VKBot
from src.freelance_leads_bot.integrations.vk_sender import PreviewVKSender
from src.freelance_leads_bot.integrations.yclients_integration import (
    YClientsIntegrationEventRepository,
    app as yclients_integration_app,
    get_integration_service as get_yclients_integration_service,
    get_repository as get_yclients_integration_repository,
    get_settings as get_yclients_integration_settings,
)
from src.freelance_leads_bot.integrations.yclients import (
    DryRunYClientsGateway,
    LiveReadDryRunYClientsGateway,
    YClientsHttpGateway,
    YClientsMutationDisabled,
)
from src.freelance_leads_bot.integrations.config import IntegrationSettings
from src.freelance_leads_bot.integrations.expert_rag import APPROVED, NEEDS_REVIEW, ExpertRagStore
from src.freelance_leads_bot.integrations.expert_rag_admin import ExpertRagAdminService, parse_rag_admin_callback
from src.freelance_leads_bot.integrations.openrouter_intent import OpenRouterIntentClient
from src.freelance_leads_bot.integrations.rag_admin_intent import RagAdminIntentParser
from src.freelance_leads_bot.integrations.rag_retrieval import RagRetrievalRequest, RagRetrievalService
from src.freelance_leads_bot.integrations.service_catalog import ACTIVE, DELETED, HIDDEN, ServiceCatalogStore
from scripts.avito_unanswered_monitor import (
    _find_unanswered as find_unanswered_avito_chat,
    _format_alert as format_unanswered_alert,
    _format_followup_alert as format_pending_followup_alert,
    _report_item as report_unanswered_item,
    audit_once as audit_unanswered_once,
    autoreply_once as autoreply_unanswered_once,
    pending_followup_rows,
    sync_pending_followups,
)
from scripts.avito_missed_message_poller import _list_recent_chats as list_recent_missed_avito_chats
from scripts.avito_missed_message_poller import _dedup_allowed as missed_poller_dedup_allowed
from scripts.avito_missed_message_poller import _should_process as should_process_missed_avito_message
from scripts.backup_runtime_data import backup_runtime_data
from scripts.export_open_handoffs import (
    build_handoff_decision_review,
    build_open_handoffs_export,
    format_open_handoffs_markdown,
    parse_open_handoff_decisions,
)
from scripts.export_avito_followups import (
    build_avito_followup_decision_review,
    build_avito_followups_export,
    format_avito_followups_markdown,
    parse_avito_followup_decisions,
)
from scripts.production_readiness_report import build_production_readiness_report, format_production_readiness_markdown
from scripts.verify_runtime_backup import verify_runtime_backup
from scripts.verify_logrotate_config import verify_logrotate_config
from scripts.avito_live_telegram_relay import (
    compact_handoff_event,
    compact_relay_event,
    compact_preview_event,
    format_telegram_card as format_avito_live_telegram_card,
    handoff_event_key as avito_live_handoff_event_key,
    iter_handoff_outbox as iter_avito_live_handoff_outbox,
    iter_preview_outbox as iter_avito_live_preview_outbox,
    merge_events as merge_avito_live_events,
    preview_event_key as avito_live_preview_event_key,
    should_relay as should_relay_avito_live_message,
    telegram_message_id as avito_live_telegram_message_id,
)
import src.freelance_leads_bot.codex_runner as codex_runner_module
import src.freelance_leads_bot.main as main_module
from src.freelance_leads_bot.codex_runner import build_chat_prompt, codex_chat_timeout_seconds
from src.freelance_leads_bot.main import (
    FEATURE_FLAGS,
    FEATURE_FLAG_BY_COMMAND,
    annotate_sender_for_codex,
    avito_context_hint_from_history,
    codex_tool_conversation_history,
    codex_tool_cross_topic_context,
    codex_history_prefix,
    avito_draft_from_result,
    avito_draft_revision_prompt,
    care_followup_keyboard,
    feature_flags_text,
    feature_flags_keyboard,
    format_care_followup_card,
    format_avito_client_draft_card,
    format_olga_history,
    format_open_cards,
    menu_text,
    parse_care_followup_callback,
    parse_feature_flag_command,
    set_all_feature_flags,
    set_feature_flag,
    send_open_handoff_cards,
    telegram_callback_delivery_target,
    telegram_embedded_message_context,
    telegram_handoff_preview_looks_like_card,
    telegram_handoff_ref_context,
)


def test_prepare_avito_outgoing_text_removes_second_greeting_today(tmp_path) -> None:
    store = LeadStore(tmp_path / "history.sqlite3")
    remember_avito_outgoing(store, "chat-1", "Здравствуйте! Первый ответ.")

    text = prepare_avito_outgoing_text(store, "chat-1", "Добрый день! Продолжаем обсуждение.")

    assert text == "Продолжаем обсуждение."


def test_prepare_avito_outgoing_text_masks_client_phone_echo(tmp_path) -> None:
    store = LeadStore(tmp_path / "history.sqlite3")

    text = prepare_avito_outgoing_text(store, "chat-1", "Записала ваш номер +7 999 123-45-67, сейчас проверю.")

    assert "+7 999 123-45-67" not in text
    assert "[телефон]" in text


def test_telegram_handoff_preview_detects_new_and_legacy_card_headers() -> None:
    assert telegram_handoff_preview_looks_like_card("Нужна ручная проверка\nПричина: missing_data")
    assert telegram_handoff_preview_looks_like_card("Нужна ручная консультация\nПричина: missing_data")
    assert telegram_handoff_preview_looks_like_card("Причина: missing_data\nКанал: avito")
    assert not telegram_handoff_preview_looks_like_card("Обычное сообщение клиента")


def test_handoff_status_is_shared_by_reissued_cards(tmp_path) -> None:
    path = tmp_path / "refs.json"
    first = remember_telegram_handoff_ref(
        telegram_chat_id="olga",
        telegram_message_id="10",
        avito_chat_id="avito-chat",
        source_message_id="client-message",
        handoff_text="Нужна ручная консультация",
        path=path,
    )
    remember_telegram_handoff_ref(
        telegram_chat_id="topic",
        telegram_message_id="20",
        avito_chat_id="avito-chat",
        handoff_id=first["handoff_id"],
        source_message_id="client-message",
        handoff_text="Нужна ручная консультация",
        path=path,
    )

    update_handoff_status(first["handoff_id"], "closed", path=path)

    assert open_handoff_refs(path) == []


def test_latest_unresolved_handoff_ref_for_chat_uses_updated_card(tmp_path) -> None:
    path = tmp_path / "refs.json"
    first = remember_telegram_handoff_ref(
        telegram_chat_id="olga",
        telegram_message_id="10",
        avito_chat_id="avito-chat",
        source_message_id="m1",
        handoff_text="Старый вопрос",
        path=path,
    )
    second = remember_telegram_handoff_ref(
        telegram_chat_id="olga",
        telegram_message_id="11",
        avito_chat_id="avito-chat",
        source_message_id="m2",
        handoff_text="Новый вопрос",
        path=path,
    )

    ref = latest_unresolved_handoff_ref_for_chat("avito-chat", telegram_chat_id="olga", path=path)
    update_handoff_status(second["handoff_id"], "closed", path=path)
    fallback = latest_unresolved_handoff_ref_for_chat("avito-chat", telegram_chat_id="olga", path=path)

    assert ref is not None
    assert ref["telegram_message_id"] == "11"
    assert fallback is None


def test_open_handoff_refs_keeps_only_latest_card_per_avito_chat(tmp_path) -> None:
    path = tmp_path / "refs.json"
    first = remember_telegram_handoff_ref(
        telegram_chat_id="olga",
        telegram_message_id="10",
        avito_chat_id="avito-chat",
        source_message_id="m1",
        handoff_text="Старый вопрос",
        path=path,
    )
    second = remember_telegram_handoff_ref(
        telegram_chat_id="olga",
        telegram_message_id="11",
        avito_chat_id="avito-chat",
        source_message_id="m2",
        handoff_text="Новый вопрос",
        path=path,
    )

    rows = open_handoff_refs(path)
    update_handoff_status(second["handoff_id"], "closed", path=path)
    after_close = open_handoff_refs(path)

    assert len(rows) == 1
    assert rows[0]["handoff_id"] == second["handoff_id"]
    assert rows[0]["handoff_id"] != first["handoff_id"]
    assert after_close == []


def test_read_open_handoff_refs_is_read_only_and_classifies_business_critical(tmp_path) -> None:
    path = tmp_path / "refs.json"
    path.write_text(
        json.dumps(
            {
                "admin:10": {
                    "telegram_chat_id": "admin",
                    "telegram_message_id": "10",
                    "avito_chat_id": "chat-address",
                    "handoff_text": "Клиент спрашивает: запись на 28 июля у нас в силе? Адрес не напишите?",
                    "status": "open",
                    "created_at": 100,
                    "updated_at": 100,
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    rows = read_open_handoff_refs(path)
    after = path.read_text(encoding="utf-8")

    assert rows
    assert before == after
    assert rows[0]["handoff_id"]
    assert handoff_ref_is_critical(rows[0]) is True


def test_export_open_handoffs_is_read_only_and_includes_avito_evidence(tmp_path) -> None:
    refs_path = tmp_path / "telegram_handoff_refs.json"
    webhook_log = tmp_path / "avito_webhook.log"
    poller_log = tmp_path / "avito_poller.log"
    refs_path.write_text(
        json.dumps(
            {
                "admin:10": {
                    "telegram_chat_id": "admin",
                    "telegram_message_id": "10",
                    "telegram_message_thread_id": "77",
                    "avito_chat_id": "chat-critical",
                    "client_name": "Милена",
                    "handoff_text": "Клиент спрашивает: запись на 28 июля у нас в силе? Адрес не напишите?",
                    "status": "open",
                    "created_at": 100,
                    "updated_at": 100,
                },
                "admin:11": {
                    "telegram_chat_id": "admin",
                    "telegram_message_id": "11",
                    "avito_chat_id": "chat-ordinary",
                    "client_name": "Олеся",
                    "handoff_text": "Нужна ручная консультация по уходу",
                    "status": "open",
                    "created_at": 500,
                    "updated_at": 500,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    webhook_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "processed",
                        "chat_id": "chat-critical",
                        "message_id": "in-1",
                        "ts": 650,
                        "message": {"text": "Запись в силе? Адрес напишите"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "event": "ignored",
                        "reason": "own_message",
                        "chat_id": "chat-critical",
                        "message_id": "out-1",
                        "ts": 700,
                        "text_preview": "Да, запись подтверждена, адрес отправили.",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    poller_log.write_text("", encoding="utf-8")
    before = refs_path.read_text(encoding="utf-8")

    report = build_open_handoffs_export(
        refs_path=refs_path,
        webhook_log_path=webhook_log,
        poller_log_path=poller_log,
        now=1000,
    )
    rendered = format_open_handoffs_markdown(report)
    after = refs_path.read_text(encoding="utf-8")

    assert before == after
    assert report["open_count"] == 2
    assert report["critical_count"] == 1
    assert report["items"][0]["avito_chat_id"] == "chat-critical"
    assert report["items"][0]["critical"] is True
    assert report["items"][0]["last_incoming"]["text"] == "Запись в силе? Адрес напишите"
    assert report["items"][0]["last_outgoing"]["text"] == "Да, запись подтверждена, адрес отправили."
    assert "CRITICAL" in rendered
    assert "Avito opened and latest incoming/outgoing checked" in rendered
    assert f"resolved #{report['items'][0]['handoff_id']}" in rendered


def test_open_handoff_decision_review_dry_run_and_apply_with_reasons(tmp_path) -> None:
    refs_path = tmp_path / "telegram_handoff_refs.json"
    first = remember_telegram_handoff_ref(
        telegram_chat_id="admin",
        telegram_message_id="10",
        avito_chat_id="chat-critical",
        source_message_id="in-1",
        client_name="Милена",
        handoff_text="Клиент спрашивает: запись на 28 июля у нас в силе? Адрес не напишите?",
        status="open",
        path=refs_path,
    )
    second = remember_telegram_handoff_ref(
        telegram_chat_id="admin",
        telegram_message_id="11",
        avito_chat_id="chat-old",
        source_message_id="in-2",
        client_name="Олеся",
        handoff_text="Нужна ручная консультация",
        status="draft_pending",
        path=refs_path,
    )
    decisions_path = tmp_path / "review.md"
    decisions_path.write_text(
        "\n".join(
            [
                f"- [x] resolved #{first['handoff_id']}: ответ в Avito отправлен в 12:10",
                f"- [x] not_relevant #{second['handoff_id']}: клиент уже отменил вопрос",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    before = json.loads(refs_path.read_text(encoding="utf-8"))

    decisions = parse_open_handoff_decisions(decisions_path.read_text(encoding="utf-8"))
    dry_run = build_handoff_decision_review(decisions_path=decisions_path, refs_path=refs_path, apply=False)
    after_dry_run = json.loads(refs_path.read_text(encoding="utf-8"))

    assert len(decisions) == 2
    assert dry_run["ok"] is True
    assert dry_run["applied_count"] == 0
    assert before == after_dry_run

    applied = build_handoff_decision_review(decisions_path=decisions_path, refs_path=refs_path, apply=True)
    refs = load_telegram_handoff_refs(refs_path)
    first_ref = next(ref for ref in refs.values() if ref["handoff_id"] == first["handoff_id"])
    second_ref = next(ref for ref in refs.values() if ref["handoff_id"] == second["handoff_id"])

    assert applied["ok"] is True
    assert applied["applied_count"] == 2
    assert first_ref["status"] == "closed"
    assert first_ref["closed_at"] > 0
    assert first_ref["resolution_note"] == "ответ в Avito отправлен в 12:10"
    assert first_ref["resolution_source"] == "open_handoffs_markdown"
    assert first_ref["resolution_action"] == "resolved"
    assert second_ref["status"] == "not_relevant"
    assert second_ref["closed_at"] > 0


def test_open_handoff_decision_review_requires_close_reason(tmp_path) -> None:
    refs_path = tmp_path / "telegram_handoff_refs.json"
    ref = remember_telegram_handoff_ref(
        telegram_chat_id="admin",
        telegram_message_id="10",
        avito_chat_id="chat-critical",
        source_message_id="in-1",
        handoff_text="Клиент спрашивает: запись в силе?",
        status="open",
        path=refs_path,
    )
    decisions_path = tmp_path / "review.md"
    decisions_path.write_text(f"- [x] closed_manual #{ref['handoff_id']}\n", encoding="utf-8")

    review = build_handoff_decision_review(decisions_path=decisions_path, refs_path=refs_path, apply=True)
    refs = load_telegram_handoff_refs(refs_path)
    current = next(row for row in refs.values() if row["handoff_id"] == ref["handoff_id"])

    assert review["ok"] is False
    assert review["items"][0]["error"] == "missing_close_reason"
    assert review["applied_count"] == 0
    assert current["status"] == "open"
    assert current["closed_at"] == 0


def test_open_handoff_decision_review_does_not_partially_apply_invalid_file(tmp_path) -> None:
    refs_path = tmp_path / "telegram_handoff_refs.json"
    first = remember_telegram_handoff_ref(
        telegram_chat_id="admin",
        telegram_message_id="10",
        avito_chat_id="chat-one",
        source_message_id="in-1",
        handoff_text="Клиент получил ответ",
        status="open",
        path=refs_path,
    )
    second = remember_telegram_handoff_ref(
        telegram_chat_id="admin",
        telegram_message_id="11",
        avito_chat_id="chat-two",
        source_message_id="in-2",
        handoff_text="Клиент спрашивает адрес",
        status="open",
        path=refs_path,
    )
    decisions_path = tmp_path / "review.md"
    decisions_path.write_text(
        "\n".join(
            [
                f"- [x] resolved #{first['handoff_id']}: ответ отправлен",
                f"- [x] closed_manual #{second['handoff_id']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    review = build_handoff_decision_review(decisions_path=decisions_path, refs_path=refs_path, apply=True)
    refs = load_telegram_handoff_refs(refs_path)
    first_ref = next(row for row in refs.values() if row["handoff_id"] == first["handoff_id"])
    second_ref = next(row for row in refs.values() if row["handoff_id"] == second["handoff_id"])

    assert review["ok"] is False
    assert review["applied_count"] == 0
    assert first_ref["status"] == "open"
    assert second_ref["status"] == "open"


def test_send_open_handoff_cards_reissues_each_handoff_in_current_topic(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    remember_telegram_handoff_ref(
        telegram_chat_id="olga",
        telegram_message_id="10",
        avito_chat_id="avito-chat",
        source_message_id="client-message",
        handoff_text="Нужна ручная консультация\nПричина: missing_data",
    )

    class FakeBot:
        def __init__(self) -> None:
            self.calls = []

        def send_message(self, chat_id, text, **kwargs):
            self.calls.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": 100 + len(self.calls)}}

    bot = FakeBot()
    count = send_open_handoff_cards(bot, "target-chat", {"message_thread_id": "77"})

    assert count == 1
    assert len(bot.calls) == 2
    assert all(call[0] == "target-chat" for call in bot.calls)
    assert all(call[2]["message_thread_id"] == "77" for call in bot.calls)
    assert "Нужна ручная консультация" in bot.calls[1][1]
    assert "SLA: ordinary" in bot.calls[1][1]


def test_send_open_handoff_cards_marks_critical_sla(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    remember_telegram_handoff_ref(
        telegram_chat_id="olga",
        telegram_message_id="10",
        avito_chat_id="avito-chat",
        source_message_id="client-message",
        handoff_text="Клиент спрашивает: запись на 28 июля у нас в силе? Адрес напишите.",
    )

    class FakeBot:
        def __init__(self) -> None:
            self.calls = []

        def send_message(self, chat_id, text, **kwargs):
            self.calls.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": 100 + len(self.calls)}}

    bot = FakeBot()

    count = send_open_handoff_cards(bot, "target-chat")

    assert count == 1
    assert "SLA: critical" in bot.calls[1][1]


@pytest.fixture(autouse=True)
def isolate_avito_processed_events(tmp_path, monkeypatch):
    old_path = processed_events.path
    old_seen = processed_events.seen
    old_webhook_log_path = avito_webhook_module.WEBHOOK_LOG_PATH
    old_history_override = avito_app.dependency_overrides.get(get_history_store)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("AUTOMATICCOSMETIC_TEST_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("TELEGRAM_ADMIN_HISTORY_DB_PATH", str(runtime_dir / "leads.sqlite3"))
    monkeypatch.setenv("RAG_EXPERT_DB_PATH", str(runtime_dir / "expert_rag.sqlite3"))
    processed_events.path = tmp_path / "avito_processed_events.json"
    processed_events.seen = {}
    avito_webhook_module.WEBHOOK_LOG_PATH = tmp_path / "avito_webhook.log"
    avito_app.dependency_overrides[get_history_store] = lambda: LeadStore(tmp_path / "avito_history.sqlite3")
    try:
        yield
    finally:
        processed_events.path = old_path
        processed_events.seen = old_seen
        avito_webhook_module.WEBHOOK_LOG_PATH = old_webhook_log_path
        if old_history_override is None:
            avito_app.dependency_overrides.pop(get_history_store, None)
        else:
            avito_app.dependency_overrides[get_history_store] = old_history_override


def test_avito_event_is_converted_to_inbound_message() -> None:
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "m1",
                "user_id": 123,
                "author_id": 456,
                "chat_id": "chat-1",
                "created": 1710000000,
                "content": {
                    "text": "Здравствуйте, сколько стоит чистка?",
                    "item": {
                        "id": 99,
                        "title": "Чистка лица",
                        "price_string": "3500 ₽",
                        "city": "Москва",
                    },
                },
            },
        }
    }

    assert is_avito_message_event(event)
    message = avito_inbound_message(event)

    assert message.chat_id == "chat-1"
    assert message.text == "Здравствуйте, сколько стоит чистка?"
    assert message.listing is not None
    assert message.listing.city == "Москва"


def test_avito_webhook_log_is_isolated_from_production_data(tmp_path) -> None:
    assert avito_webhook_module.WEBHOOK_LOG_PATH.parent == tmp_path

    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json={"type": "ping"})

        assert response.status_code == 200
        assert avito_webhook_module.WEBHOOK_LOG_PATH.exists()
        rows = [json.loads(line) for line in avito_webhook_module.WEBHOOK_LOG_PATH.read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["event"] == "ignored"
        assert rows[-1]["reason"] == "not_message_event"
        assert not str(avito_webhook_module.WEBHOOK_LOG_PATH).startswith("data/")
    finally:
        avito_app.dependency_overrides.clear()


def test_booking_request_parser_extracts_date_time_city_and_phone() -> None:
    from src.freelance_leads_bot.integrations.booking_flow import booking_request_from_message

    message = avito_inbound_message(
        {
            "type": "message",
            "text": "Ростов, чистка лица 2026-06-01 в 14:30, телефон 8 999 123 45 67",
        }
    )

    request = booking_request_from_message(message)

    assert request.city == "Ростов-на-Дону"
    assert request.preferred_date == "2026-06-01"
    assert request.preferred_time == "14:30"
    assert request.phone == "+79991234567"


def test_care_crm_upserts_appointments_and_marks_visit(tmp_path) -> None:
    store = CareCrmStore(tmp_path / "care.sqlite3")
    appointment = Appointment(
        id=777,
        client=ClientProfile(name="Анна", phone="8 999 000 00 00", external_id="5", skin_type="сухая"),
        service=Service(id=7, title="Увеличение ягодиц", price=18000, duration_minutes=60),
        city="Москва",
        starts_at=datetime(2026, 6, 2, 17, 30),
    )

    row = store.upsert_appointment(appointment)
    rows = store.list_appointments_for_confirmation("2026-06-02")
    updated = store.mark_visit(
        int(row["id"]),
        attended=True,
        actual_service_title="Увеличение губ 1 мл",
        confirmed_by="test",
        source_text="по факту губы 1 мл",
    )

    assert len(rows) == 1
    assert rows[0]["client_name"] == "Анна"
    assert rows[0]["client_phone"] == "79990000000"
    assert updated["status"] == "attended"
    assert updated["confirmation_status"] == "confirmed"
    with store.connect() as conn:
        visit = conn.execute("SELECT * FROM crm_visits WHERE appointment_id = ?", (row["id"],)).fetchone()
    assert visit["actually_attended"] == 1
    assert visit["actual_service_title"] == "Увеличение губ 1 мл"
    assert visit["actual_service_category"] == "lips"
    assert visit["actual_amount_value"] == "1"
    assert visit["actual_amount_unit"] == "мл"
    assert visit["source_text"] == "по факту губы 1 мл"


def test_care_crm_plans_followup_tasks_from_confirmed_visit(tmp_path) -> None:
    store = CareCrmStore(tmp_path / "care.sqlite3")
    row = store.upsert_appointment(
        Appointment(
            id=776,
            client=ClientProfile(name="Анна", phone="+7 900 000 00 00", external_id="9"),
            service=Service(id=7, title="Увеличение ягодиц", price=18000, duration_minutes=60),
            city="Москва",
            starts_at=datetime(2026, 6, 2, 17, 30),
        )
    )

    store.mark_visit(int(row["id"]), attended=True, actual_service_title="Губы 1 мл", confirmed_by="test")
    store.mark_visit(int(row["id"]), attended=True, actual_service_title="Губы 1 мл", confirmed_by="test")
    tasks = store.list_followup_tasks(client_id=int(row["client_id"]))

    kinds = {task["kind"] for task in tasks}
    assert len(tasks) == len(kinds)
    assert {"care_checkin_1d", "result_checkin_14d", "care_relationship_45d"}.issubset(kinds)
    checkin = next(task for task in tasks if task["kind"] == "care_checkin_1d")
    assert checkin["due_at"].startswith("2026-06-03")
    relationship = next(task for task in tasks if task["kind"] == "care_relationship_45d")
    assert "аккуратно напомнить" in relationship["message_draft"]


def test_care_crm_does_not_plan_followups_for_do_not_contact_client(tmp_path) -> None:
    store = CareCrmStore(tmp_path / "care.sqlite3")
    row = store.upsert_appointment(
        Appointment(
            id=775,
            client=ClientProfile(name="Анна", phone="+7 900 000 00 01", external_id="10"),
            service=Service(id=7, title="Чистка лица", price=5000, duration_minutes=60),
            city="Москва",
            starts_at=datetime(2026, 6, 2, 17, 30),
        )
    )
    store.update_client_flags(int(row["client_id"]), do_not_contact=True)

    store.mark_visit(int(row["id"]), attended=True, actual_service_title="Чистка лица", confirmed_by="test")

    assert store.list_followup_tasks(client_id=int(row["client_id"])) == []


def test_care_crm_applies_olga_text_reply_and_logs_interaction(tmp_path) -> None:
    store = CareCrmStore(tmp_path / "care.sqlite3")
    row = store.upsert_appointment(
        Appointment(
            id=778,
            client=ClientProfile(name="Елена", phone="+7 900 111 22 33", external_id="6"),
            service=Service(id=8, title="Увеличение ягодиц", price=18000, duration_minutes=60),
            city="Москва",
            starts_at=datetime(2026, 6, 2, 18, 30),
        )
    )
    store.remember_confirmation_card(int(row["id"]), chat_id="10", message_id="555")

    matched = store.find_appointment_by_confirmation_card(chat_id="10", message_id="555")
    updated = store.apply_visit_details_from_text(
        int(row["id"]),
        "не ягодицы, а губы 1 мл, Juvederm",
        confirmed_by="telegram_reply",
    )

    assert matched is not None
    assert matched["client_name"] == "Елена"
    assert updated["confirmation_status"] == "confirmed"
    with store.connect() as conn:
        visit = conn.execute("SELECT * FROM crm_visits WHERE appointment_id = ?", (row["id"],)).fetchone()
        interaction = conn.execute("SELECT * FROM crm_interactions WHERE appointment_id = ?", (row["id"],)).fetchone()
    assert visit["actual_service_title"] == "губы"
    assert visit["actual_amount_value"] == "1"
    assert visit["actual_amount_unit"] == "мл"
    assert visit["product_or_drug"] == "Juvederm"
    assert interaction["intent"] == "visit_fact_update"
    assert "Juvederm" in interaction["body"]


def test_care_crm_unclear_visit_details_need_more_info_and_do_not_plan_followups(tmp_path) -> None:
    store = CareCrmStore(tmp_path / "care.sqlite3")
    row = store.upsert_appointment(
        Appointment(
            id=7791,
            client=ClientProfile(name="Елена", phone="+7 900 111 22 34", external_id="61"),
            service=Service(id=8, title="Увеличение ягодиц", price=18000, duration_minutes=60),
            city="Москва",
            starts_at=datetime(2026, 6, 2, 18, 30),
        )
    )

    updated = store.apply_visit_details_from_text(int(row["id"]), "да", confirmed_by="telegram_reply")

    assert updated["confirmation_status"] == "needs_details"
    assert store.list_followup_tasks(client_id=int(row["client_id"]), status="") == []


def test_care_crm_no_show_blocks_existing_followups(tmp_path) -> None:
    store = CareCrmStore(tmp_path / "care.sqlite3")
    row = store.upsert_appointment(
        Appointment(
            id=7792,
            client=ClientProfile(name="Елена", phone="+7 900 111 22 35", external_id="62"),
            service=Service(id=8, title="Увеличение губ", price=12000, duration_minutes=60),
            city="Москва",
            starts_at=datetime(2026, 6, 2, 18, 30),
        )
    )
    store.mark_visit(int(row["id"]), attended=True, actual_service_title="Губы 1 мл", confirmed_by="test")

    updated = store.mark_visit(int(row["id"]), attended=False, confirmed_by="telegram_button")
    tasks = store.list_followup_tasks(client_id=int(row["client_id"]), status="")

    assert updated["confirmation_status"] == "no_show"
    assert tasks
    assert {task["status"] for task in tasks} == {"blocked"}
    assert {task["blocked_reason"] for task in tasks} == {"no_show"}


@pytest.mark.anyio
async def test_daily_visit_confirmation_sender_sends_cards_and_remembers_message(monkeypatch) -> None:
    import scripts.send_visit_confirmations as sender

    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": len(self.messages) + 100}}

    class FakeStore:
        remembered = []

        def remember_confirmation_card(self, appointment_id, *, chat_id, message_id):
            self.remembered.append((appointment_id, chat_id, message_id))

    async def fake_rows(settings, day):
        return [
            {
                "id": 42,
                "client_name": "Анна",
                "client_phone": "79990000000",
                "scheduled_at": f"{day}T17:30:00",
                "city": "Москва",
                "booked_service_title": "Губы 1 мл",
            }
        ]

    monkeypatch.setattr(sender, "_visit_confirmation_rows", fake_rows)
    monkeypatch.setattr(sender, "CareCrmStore", FakeStore)
    bot = FakeBot()

    result = await sender.send_visit_confirmations_once(
        settings=object(),
        bot=bot,
        chat_id="admin-chat",
        day="2026-07-22",
    )

    assert result == {"ok": True, "day": "2026-07-22", "sent": 1, "empty": False}
    assert len(bot.messages) == 2
    assert "Проверка визитов за 2026-07-22" in bot.messages[0][1]
    assert "Клиент: <b>Анна</b>" in bot.messages[1][1]
    assert bot.messages[1][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "visitconfirm:42:yes"
    assert FakeStore.remembered == [(42, "admin-chat", "102")]


@pytest.mark.anyio
async def test_daily_visit_confirmation_sender_is_quiet_when_empty(monkeypatch) -> None:
    import scripts.send_visit_confirmations as sender

    class FakeBot:
        messages = []

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True}

    async def fake_rows(settings, day):
        return []

    monkeypatch.setattr(sender, "_visit_confirmation_rows", fake_rows)
    bot = FakeBot()

    result = await sender.send_visit_confirmations_once(
        settings=object(),
        bot=bot,
        chat_id="admin-chat",
        day="2026-07-22",
        quiet_empty=True,
    )

    assert result == {"ok": True, "day": "2026-07-22", "sent": 0, "empty": True}
    assert bot.messages == []


def test_actual_visit_details_parser_asks_for_clarification_when_unclear() -> None:
    assert parse_actual_visit_details("1 мл", booked_service_title="Увеличение ягодиц")["understood"] is False
    details = parse_actual_visit_details("по факту губы 0,7 мл")
    assert details["understood"] is True
    assert details["actual_service_title"] == "губы"
    assert details["amount_ml"] == "0.7"


def test_care_memory_learning_identity_and_visit_fact_services(tmp_path) -> None:
    store = CareCrmStore(tmp_path / "care.sqlite3")
    row = store.upsert_appointment(
        Appointment(
            id=784,
            client=ClientProfile(name="Анна Петрова", phone="+7 900 123 45 67", external_id="101"),
            service=Service(id=9, title="Увеличение ягодиц", price=18000, duration_minutes=60),
            city="Москва",
            starts_at=datetime(2026, 6, 2, 12, 0),
        )
    )
    updated = VisitFactService(store).upsert_fact(
        int(row["id"]),
        actual_service_title="Губы",
        amount_ml="1",
        product_or_drug="Juvederm",
        confirmed_by="test",
        source_text="не ягодицы, а губы 1 мл Juvederm",
    )
    CareLearningService(store).upsert_preference(
        int(row["client_id"]),
        preference_type="tone",
        value="писать мягко и коротко",
        source="olga",
        confidence=0.8,
    )
    lesson = CareLearningService(store).create_lesson(
        lesson="После губ сначала спрашивать самочувствие, не продавать сразу.",
        source="olga_revision",
        tags=("followup", "губы"),
        confidence=0.9,
    )
    ClientIdentityService(store).link(
        int(row["client_id"]),
        channel="telegram_client",
        external_user_id="2002",
        chat_id="1001",
        verified=True,
    )
    memory = ClientMemoryService(store).memory(int(row["client_id"]), include_internal=True)
    matches = VisitFactService(store).match(query="Анна", day="2026-06-02")
    suggestions = ClientIdentityService(store).suggest_merges(query="Анна")

    assert updated["confirmation_status"] == "confirmed"
    assert memory["client"]["phone"] == "79001234567"
    assert memory["visits"][0]["actual_service_title"] == "Губы"
    assert memory["visits"][0]["amount_ml"] == "1"
    assert memory["preferences"][0]["value"] == "писать мягко и коротко"
    assert lesson["lesson"].startswith("После губ")
    assert matches[0]["booked_service_title"] == "Увеличение ягодиц"
    assert suggestions == []


@pytest.mark.anyio
async def test_care_crm_tools_expose_returning_client_facts_without_internal_notes(tmp_path) -> None:
    crm = CareCrmStore(tmp_path / "care.sqlite3")
    row = crm.upsert_appointment(
        Appointment(
            id=779,
            client=ClientProfile(name="Анна Петрова", phone="+7 900 111 22 33", external_id="77"),
            service=Service(id=9, title="Увеличение губ", price=12000, duration_minutes=60),
            city="Москва",
            starts_at=datetime(2026, 6, 2, 18, 30),
        )
    )
    crm.mark_visit(int(row["id"]), attended=True, actual_service_title="Губы 1 мл", confirmed_by="test")
    crm.add_interaction(int(row["client_id"]), appointment_id=int(row["id"]), body="Внутренняя заметка Ольги", intent="note")
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        care_crm=crm,
        role_profile=role_profile(CodexRole.TELEGRAM_CLIENT),
    )

    search = await toolbox.execute("care.crm.clients.search", {"query": "Анна"})
    visits = await toolbox.execute("care.crm.visits.list", {"client_id": row["client_id"]})
    internal_notes = await toolbox.execute("care.crm.interactions.list", {"client_id": row["client_id"]})

    assert search.ok is True
    assert search.data["clients"][0]["name"] == "Анна Петрова"
    assert search.data["clients"][0]["do_not_contact"] is False
    assert visits.ok is True
    assert visits.data["visits"][0]["actual_service_title"] == "Губы 1 мл"
    assert visits.data["visits"][0]["booked_service_title"] == "Увеличение губ"
    assert internal_notes.ok is False
    assert "unknown tool" in internal_notes.error


@pytest.mark.anyio
async def test_new_care_crm_and_learning_tools_support_agent_memory(tmp_path) -> None:
    crm = CareCrmStore(tmp_path / "care.sqlite3")
    row = crm.upsert_appointment(
        Appointment(
            id=785,
            client=ClientProfile(name="Мария", phone="+7 911 111 22 33", external_id="102"),
            service=Service(id=9, title="Чистка лица", price=5000, duration_minutes=60),
            city="Санкт-Петербург",
            starts_at=datetime(2026, 6, 2, 12, 0),
        )
    )
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        care_crm=crm,
        role_profile=role_profile(CodexRole.OLGA_BOSS),
    )

    fact = await toolbox.execute(
        "care.crm.visit.fact.upsert",
        {"appointment_id": row["id"], "actual_service_title": "Чистка лица", "source_text": "была чистка"},
    )
    pref = await toolbox.execute(
        "care.learning.preference.upsert",
        {"client_id": row["client_id"], "preference_type": "channel", "value": "telegram"},
    )
    lesson = await toolbox.execute(
        "care.learning.lesson.create",
        {"lesson": "Для чистки лица follow-up начинать с вопроса о коже.", "tags": ["followup", "чистка"]},
    )
    memory = await toolbox.execute("care.crm.client.memory.get", {"client_id": row["client_id"], "include_internal": True})
    appointments = await toolbox.execute("care.crm.appointments.match", {"query": "Мария", "day": "2026-06-02"})

    assert fact.ok is True
    assert pref.ok is True
    assert lesson.ok is True
    assert memory.data["memory"]["visits"][0]["actual_service_title"] == "Чистка лица"
    assert memory.data["memory"]["preferences"][0]["value"] == "telegram"
    assert appointments.data["appointments"][0]["client_name"] == "Мария"


@pytest.mark.anyio
async def test_internal_care_crm_interaction_tool_is_available_to_upsell_planner(tmp_path) -> None:
    crm = CareCrmStore(tmp_path / "care.sqlite3")
    client_id = crm.upsert_client(ClientProfile(name="Мария", phone="+7 911 111 22 33"), city="Санкт-Петербург")
    crm.add_interaction(client_id, body="Попросила мягко написать через 2 недели", intent="followup_hint")
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        care_crm=crm,
        role_profile=role_profile(CodexRole.YCLIENTS_UPSELL_STUB),
    )

    result = await toolbox.execute("care.crm.interactions.list", {"client_id": client_id})

    assert result.ok is True
    assert result.data["interactions"][0]["intent"] == "followup_hint"
    assert "мягко написать" in result.data["interactions"][0]["body"]


@pytest.mark.anyio
async def test_internal_followup_task_tool_lists_planned_care_queue(tmp_path) -> None:
    crm = CareCrmStore(tmp_path / "care.sqlite3")
    row = crm.upsert_appointment(
        Appointment(
            id=781,
            client=ClientProfile(name="Елена", phone="+7 922 111 22 33", external_id="91"),
            service=Service(id=9, title="Чистка лица", price=5000, duration_minutes=60),
            city="Краснодар",
            starts_at=datetime(2026, 6, 2, 12, 0),
        )
    )
    crm.mark_visit(int(row["id"]), attended=True, actual_service_title="Чистка лица", confirmed_by="test")
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        care_crm=crm,
        role_profile=role_profile(CodexRole.YCLIENTS_UPSELL_STUB),
    )

    result = await toolbox.execute("care.crm.followups.list", {"client_id": row["client_id"]})

    assert result.ok is True
    kinds = {task["kind"] for task in result.data["tasks"]}
    assert {"care_checkin_1d", "result_checkin_14d", "care_relationship_45d"}.issubset(kinds)
    assert result.data["tasks"][0]["client_name"] == "Елена"


@pytest.mark.anyio
async def test_followup_delivery_sends_due_task_to_linked_telegram_client(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": 1}}

    crm = CareCrmStore(tmp_path / "care.sqlite3")
    row = crm.upsert_appointment(
        Appointment(
            id=782,
            client=ClientProfile(name="Елена", phone="+7 922 111 22 33", external_id="92"),
            service=Service(id=9, title="Чистка лица", price=5000, duration_minutes=60),
            city="Краснодар",
            starts_at=datetime(2026, 6, 2, 12, 0),
        )
    )
    crm.mark_visit(int(row["id"]), attended=True, actual_service_title="Чистка лица", confirmed_by="test")
    crm.link_client_channel(int(row["client_id"]), channel="telegram_client", external_user_id="2002", chat_id="1001", verified=True)
    service = CareFollowupDeliveryService(crm, FakeBot())

    result = await service.send_due(now=datetime(2026, 6, 3, 13, 0), limit=10)
    tasks = crm.list_followup_tasks(client_id=int(row["client_id"]), status="")
    interactions = crm.list_client_interactions(int(row["client_id"]), limit=5)

    assert result["sent"]
    assert result["blocked"] == []
    assert any(task["status"] == "sent" and task["kind"] == "care_checkin_1d" for task in tasks)
    assert any(item["direction"] == "outbound_bot" and item["intent"] == "care_checkin_1d" for item in interactions)
    assert crm.get_client(int(row["client_id"]))["last_contacted_at"]


@pytest.mark.anyio
async def test_followup_delivery_requires_verified_telegram_channel(tmp_path) -> None:
    class FakeBot:
        def send_message(self, chat_id, text, **kwargs):
            raise AssertionError("should not send without verified link")

    crm = CareCrmStore(tmp_path / "care.sqlite3")
    row = crm.upsert_appointment(
        Appointment(
            id=7821,
            client=ClientProfile(name="Елена", phone="+7 922 111 22 34", external_id="921"),
            service=Service(id=9, title="Чистка лица", price=5000, duration_minutes=60),
            city="Краснодар",
            starts_at=datetime(2026, 6, 2, 12, 0),
        )
    )
    crm.mark_visit(int(row["id"]), attended=True, actual_service_title="Чистка лица", confirmed_by="test")
    task_id = int(crm.list_followup_tasks(client_id=int(row["client_id"]))[0]["id"])

    gate = crm.followup_send_gate(task_id)
    result = await CareFollowupDeliveryService(crm, FakeBot()).send_task(task_id)

    assert gate["status"] == "needs_channel"
    assert result["status"] == "needs_channel"
    assert crm.get_followup_task(task_id)["requires_channel_resolution"] == 1


@pytest.mark.anyio
async def test_followup_delivery_blocks_complaint_risk(tmp_path) -> None:
    class FakeBot:
        def send_message(self, chat_id, text, **kwargs):
            raise AssertionError("should not send risk task")

    crm = CareCrmStore(tmp_path / "care.sqlite3")
    row = crm.upsert_appointment(
        Appointment(
            id=7822,
            client=ClientProfile(name="Елена", phone="+7 922 111 22 35", external_id="922"),
            service=Service(id=9, title="Чистка лица", price=5000, duration_minutes=60),
            city="Краснодар",
            starts_at=datetime(2026, 6, 2, 12, 0),
        )
    )
    crm.mark_visit(int(row["id"]), attended=True, actual_service_title="Чистка лица", confirmed_by="test")
    crm.link_client_channel(int(row["client_id"]), channel="telegram_client", external_user_id="2002", chat_id="1001", verified=True)
    task_id = int(crm.list_followup_tasks(client_id=int(row["client_id"]))[0]["id"])
    crm.update_client_flags(int(row["client_id"]), complaint_risk=True, risk_reason="клиент жаловался")

    gate = crm.followup_send_gate(task_id)
    result = await CareFollowupDeliveryService(crm, FakeBot()).send_task(task_id)

    assert gate["status"] == "blocked"
    assert "жаловался" in gate["reason"]
    assert result["status"] != "sent"
    assert crm.get_followup_task(task_id)["status"] == "blocked"


def test_care_followup_admin_card_helpers() -> None:
    task = {
        "id": 55,
        "client_name": "Анна",
        "actual_service_title": "Губы 1 мл",
        "city": "Краснодар",
        "due_at": "2026-06-03T17:30:00",
        "message_draft": "Анна, здравствуйте! Как самочувствие после процедуры?",
        "reason": "Проверка самочувствия после подтверждённого визита.",
        "confidence": 0.72,
        "risk_level": "high",
        "complaint_risk": 1,
    }

    keyboard = care_followup_keyboard(55)
    card = format_care_followup_card(task)

    assert parse_care_followup_callback("carefu:55:send") == (55, "send")
    assert parse_care_followup_callback("carefu:55:skip") == (55, "skip")
    assert parse_care_followup_callback("carefu:55:rewrite") == (55, "rewrite")
    assert parse_care_followup_callback("carefu:55:ask") == (55, "ask")
    assert parse_care_followup_callback("carefu:55:no_contact") == (55, "no_contact")
    assert parse_care_followup_callback("carefu:nope:send") is None
    assert keyboard["inline_keyboard"][0][0]["callback_data"] == "carefu:55:send"
    assert keyboard["inline_keyboard"][0][1]["callback_data"] == "carefu:55:skip"
    assert keyboard["inline_keyboard"][1][0]["callback_data"] == "carefu:55:rewrite"
    assert keyboard["inline_keyboard"][2][0]["callback_data"] == "carefu:55:no_contact"
    assert "Задача отдела заботы" in card
    assert "Анна" in card
    assert "Губы 1 мл" in card
    assert "Краснодар" in card
    assert "риск/жалоба" in card
    assert "Проверка самочувствия" in card


@pytest.mark.anyio
async def test_visit_confirmation_service_syncs_yclients_day_and_formats_card(tmp_path) -> None:
    gateway = DryRunYClientsGateway()
    await gateway.create_appointment(
        Appointment(
            client=ClientProfile(name="Мария", phone="+7 911 111 22 33", external_id="12"),
            service=Service(id=4, title="Биоревитализация", price=9000, duration_minutes=45),
            city="Санкт-Петербург",
            starts_at=datetime(2026, 6, 2, 12, 0),
        )
    )
    store = CareCrmStore(tmp_path / "care.sqlite3")
    service = VisitConfirmationService(store, gateway)

    rows = await service.sync_day("2026-06-02")
    action = parse_visit_confirmation_callback(f"visitconfirm:{rows[0]['id']}:yes")
    keyboard = visit_confirmation_keyboard(int(rows[0]["id"]))
    card = format_visit_confirmation_card(rows[0])

    assert len(rows) == 1
    assert rows[0]["client_name"] == "Мария"
    assert action is not None
    assert action.action == "yes"
    assert keyboard["inline_keyboard"][0][0]["callback_data"] == f"visitconfirm:{rows[0]['id']}:yes"
    assert "Проверка визита" in card
    assert "Биоревитализация" in card


@pytest.mark.anyio
async def test_booking_flow_exposes_explicit_state_for_slot_lifecycle() -> None:
    service = Service(id=4, title="Губы", price=9000, duration_minutes=45)
    message = InboundMessage(channel=Channel.AVITO, client_id="client-1", chat_id="chat-1", text="Москва губы 02.06 в 12:00 +7 999 000 00 00")
    slots = [Slot(city="Москва", starts_at=datetime(2026, 6, 2, 12, 0), service_id=4)]

    created = await AvitoBookingFlow(DryRunYClientsGateway(services=[service], slots=slots), allow_create=True).process(
        BookingRequest(message=message, city="Москва", service_query="губы", preferred_date="2026-06-02", preferred_time="12:00", phone="+79990000000")
    )
    offered = await AvitoBookingFlow(DryRunYClientsGateway(services=[service], slots=slots), allow_create=True).process(
        BookingRequest(message=message, city="Москва", service_query="губы", preferred_date="2026-06-02")
    )
    awaiting = await AvitoBookingFlow(DryRunYClientsGateway(services=[service], slots=slots), allow_create=False).process(
        BookingRequest(message=message, city="Москва", service_query="губы", preferred_date="2026-06-02", preferred_time="12:00", phone="+79990000000")
    )

    assert created.action == "created"
    assert created.state == "confirmed"
    assert offered.action == "offer_slots"
    assert offered.state == "offered_slot"
    assert awaiting.action == "booking_confirmation_required"
    assert awaiting.state == "awaiting_olga"


def test_codex_logout_reset_moves_auth_json_to_backup(tmp_path, monkeypatch) -> None:
    auth_dir = tmp_path / ".codex"
    auth_path = auth_dir / "auth.json"
    backup_dir = auth_dir / "auth-backups"
    auth_dir.mkdir()
    auth_path.write_text('{"token":"secret"}', encoding="utf-8")

    subprocess_calls: list[list[str]] = []
    monkeypatch.setattr(codex_runner_module, "CODEX_AUTH_PATH", auth_path)
    monkeypatch.setattr(codex_runner_module, "CODEX_AUTH_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(codex_runner_module, "_kill_pending_codex_login", lambda: None)
    monkeypatch.setattr(codex_runner_module.subprocess, "run", lambda *args, **kwargs: subprocess_calls.append(list(args[0])))

    result = codex_runner_module.codex_logout_reset()

    backups = list(backup_dir.glob("auth.json*.bak"))
    assert not auth_path.exists()
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"token":"secret"}'
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert "moved:" in result
    assert "`auth_removed`" in result
    assert subprocess_calls == []


def test_generic_codex_chat_prompt_does_not_define_avito_send_directive() -> None:
    prompt = build_chat_prompt("Ответь клиенту: можно завтра в 14:00", [])

    assert "avito_send" not in prompt
    assert "[[avito_send:" not in prompt


def test_avito_context_hint_can_use_parent_telegram_chat_history(tmp_path) -> None:
    store = LeadStore(tmp_path / "leads.sqlite3")
    store.add_codex_chat_message(
        "codex",
        "Вопрос из Avito\nЧат: u2i-QzEmnqjpOLEsiR6vYTBenA\nКлиент просит фото.",
        "chat:5993376751",
    )
    store.add_codex_chat_message("user", "Ответь клиенту: да, можно завтра", "chat:5993376751:thread:55841")

    parent_history = store.recent_codex_chat_by_prefix(codex_history_prefix("chat:5993376751:thread:55841"), 10)
    hint = avito_context_hint_from_history(parent_history)

    assert codex_history_prefix("chat:5993376751:thread:55841") == "chat:5993376751"
    assert "u2i-QzEmnqjpOLEsiR6vYTBenA" in hint
    assert "avito.messages.send" in hint


def test_telegram_topic_conversation_history_stays_isolated(tmp_path) -> None:
    store = LeadStore(tmp_path / "leads.sqlite3")
    store.add_codex_chat_message("user", "Ольгин контекст из другой темы", "chat:5993376751:thread:1")
    store.add_codex_chat_message(
        "assistant",
        "Avito card: Чат: u2i-QzEmnqjpOLEsiR6vYTBenA",
        "chat:5993376751",
    )
    store.add_codex_chat_message("user", "Привет от админа", "chat:5993376751:thread:2")

    history = codex_tool_conversation_history(store, "chat:5993376751:thread:2", 10)
    cross_topic_context = codex_tool_cross_topic_context(store, "chat:5993376751:thread:2", 10)

    assert [item["content"] for item in history] == ["Привет от админа"]
    assert "Ольгин контекст" not in str(history)
    assert "u2i-QzEmnqjpOLEsiR6vYTBenA" in cross_topic_context


def test_codex_topic_history_limit_zero_loads_whole_topic(tmp_path) -> None:
    store = LeadStore(tmp_path / "leads.sqlite3")
    store.add_codex_chat_message("user", "Первое", "chat:5993376751:thread:2")
    store.add_codex_chat_message("assistant", "Второе", "chat:5993376751:thread:2")
    store.add_codex_chat_message("user", "Третье", "chat:5993376751:thread:2")
    store.add_codex_chat_message("user", "Другая тема", "chat:5993376751:thread:1")

    limited = codex_tool_conversation_history(store, "chat:5993376751:thread:2", 2)
    full = codex_tool_conversation_history(store, "chat:5993376751:thread:2", 0)

    assert [item["content"] for item in limited] == ["Второе", "Третье"]
    assert [item["content"] for item in full] == ["Первое", "Второе", "Третье"]


def test_menu_exposes_feature_flag_commands(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AVITO_POLLER_AUTOSTART", raising=False)
    store = LeadStore(tmp_path / "leads.sqlite3")

    text = menu_text(store)
    flags_text = feature_flags_text()
    flag, action = parse_feature_flag_command("/AVITO_POLLER_AUTOSTART вкл")

    assert "/AVITO_POLLER_AUTOSTART" in text
    assert "вкл/выкл" in text
    assert "/flags" in text
    assert "AVITO_POLLER_AUTOSTART" in flags_text
    assert flag == FEATURE_FLAG_BY_COMMAND["avito_poller_autostart"]
    assert action == "вкл"


def test_feature_flags_keyboard_exposes_full_live_presets() -> None:
    keyboard = feature_flags_keyboard()
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]

    assert any(button["callback_data"] == "preset:live:ask" for button in buttons)
    assert any(button["callback_data"] == "preset:off:ask" for button in buttons)
    assert any(button["callback_data"] == "olga_history" for button in buttons)


def test_format_olga_history_reads_webhook_handoff_delivery(tmp_path) -> None:
    webhook_log = tmp_path / "avito_webhook.log"
    webhook_log.write_text(
        json.dumps(
            {
                "ts": 1780285012,
                "event": "processed",
                "chat_id": "u2i-test",
                "message_id": "m-test",
                "action": "handoff",
                "handoff": "missing_data",
                "send": {"sent": True, "response": {"content": {"text": "Минутку, уточню."}}},
                "handoff_notify": {
                    "sent": True,
                    "telegram": {"ok": True, "result": {"message_id": 42}},
                    "text": "Нужна ручная консультация\nПричина: missing_data\nЧат: u2i-test",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    text = format_olga_history(
        webhook_log_path=webhook_log,
        handoff_outbox_path=tmp_path / "missing.jsonl",
        client_name_cache_path=tmp_path / "missing_names.json",
        poller_log_path=tmp_path / "missing_poller.log",
    )

    assert "Диалог Avito" in text
    assert "u2i-test" not in text
    assert "#36068D" not in text
    assert "missing_data" in text
    assert "42" in text
    assert "Минутку, уточню." in text


def test_format_olga_history_prefers_avito_client_name(tmp_path) -> None:
    webhook_log = tmp_path / "avito_webhook.log"
    webhook_log.write_text(
        json.dumps(
            {
                "ts": 1780285012,
                "event": "processed",
                "chat_id": "u2i-test",
                "message_id": "m-test",
                "action": "handoff",
                "handoff": "missing_data",
                "send": {"sent": True, "response": {"content": {"text": "Минутку, уточню."}}},
                "handoff_notify": {
                    "sent": True,
                    "telegram": {"ok": True, "result": {"message_id": 42}},
                    "text": "Нужна ручная консультация\nПричина: missing_data\nДиалог: 🔴⚫🔴💎🟩",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    cache = tmp_path / "names.json"
    cache.write_text(json.dumps({"u2i-test": "Анна"}, ensure_ascii=False), encoding="utf-8")

    text = format_olga_history(
        webhook_log_path=webhook_log,
        handoff_outbox_path=tmp_path / "missing.jsonl",
        client_name_cache_path=cache,
        poller_log_path=tmp_path / "missing_poller.log",
    )

    assert "Клиент Avito" in text
    assert "Анна" in text
    assert "u2i-test" not in text
    assert "🔴⚫🔴💎🟩" not in text


def test_format_olga_history_reads_debounce_batch_handoffs(tmp_path) -> None:
    webhook_log = tmp_path / "avito_webhook.log"
    webhook_log.write_text(
        json.dumps(
            {
                "ts": 1780285012,
                "event": "debounce_batch_processed",
                "chat_id": "u2i-batch",
                "message_id": "m1,m2",
                "action": "handoff",
                "handoff": "missing_data",
                "send": {"sent": True, "response": {"id": "hold-1", "content": {"text": "Уточню."}}},
                "handoff_notify": {
                    "sent": True,
                    "telegram": {"ok": True, "result": {"message_id": 77}},
                    "text": "Нужна ручная консультация\nПричина: missing_data\nЧат: u2i-batch",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    text = format_olga_history(
        webhook_log_path=webhook_log,
        handoff_outbox_path=tmp_path / "missing.jsonl",
        client_name_cache_path=tmp_path / "missing_names.json",
        poller_log_path=tmp_path / "missing_poller.log",
    )

    assert "missing_data" in text
    assert "77" in text


def test_format_open_cards_keeps_handoff_open_after_initial_hold_reply(tmp_path) -> None:
    webhook_log = tmp_path / "avito_webhook.log"
    webhook_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": 1780285012,
                        "event": "processed",
                        "chat_id": "u2i-open",
                        "message_id": "m-open",
                        "action": "handoff",
                        "handoff": "missing_data",
                        "send": {"sent": True, "response": {"id": "hold-1", "content": {"text": "Минутку, уточню."}}},
                        "handoff_notify": {
                            "sent": True,
                            "telegram": {"ok": True, "result": {"message_id": 42}},
                            "text": "Нужна ручная консультация\nПричина: missing_data\nЧат: u2i-open",
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "ts": 1780285013,
                        "event": "ignored",
                        "reason": "own_message",
                        "chat_id": "u2i-open",
                        "message_id": "hold-1",
                        "text_preview": "Минутку, уточню.",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    text = format_open_cards(
        max_age_days=None,
        webhook_log_path=webhook_log,
        handoff_outbox_path=tmp_path / "missing.jsonl",
        client_name_cache_path=tmp_path / "missing_names.json",
        poller_log_path=tmp_path / "missing_poller.log",
        drafts_path=tmp_path / "missing_drafts.json",
    )

    assert "Открытые карточки" in text
    assert "missing_data" in text
    assert "42" in text
    assert "SLA: ordinary" in text


def test_format_open_cards_closes_handoff_after_later_client_reply(tmp_path) -> None:
    webhook_log = tmp_path / "avito_webhook.log"
    webhook_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": 1780285012,
                        "event": "processed",
                        "chat_id": "u2i-closed",
                        "message_id": "m-closed",
                        "action": "handoff",
                        "handoff": "missing_data",
                        "send": {"sent": True, "response": {"id": "hold-1", "content": {"text": "Минутку, уточню."}}},
                        "handoff_notify": {
                            "sent": True,
                            "telegram": {"ok": True, "result": {"message_id": 42}},
                            "text": "Нужна ручная консультация\nПричина: missing_data\nЧат: u2i-closed",
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "ts": 1780285112,
                        "event": "ignored",
                        "reason": "own_message",
                        "chat_id": "u2i-closed",
                        "message_id": "final-1",
                        "text_preview": "Здравствуйте! Отвечаем по сути.",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    text = format_open_cards(
        max_age_days=None,
        webhook_log_path=webhook_log,
        handoff_outbox_path=tmp_path / "missing.jsonl",
        client_name_cache_path=tmp_path / "missing_names.json",
        poller_log_path=tmp_path / "missing_poller.log",
        drafts_path=tmp_path / "missing_drafts.json",
    )

    assert "Не нашёл незакрытых" in text


def test_set_all_feature_flags_updates_env_and_codex_chat(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("AVITO_SEND_ENABLED=false\n", encoding="utf-8")
    store = LeadStore(tmp_path / "leads.sqlite3")
    store.set_codex_chat_enabled(False)
    for flag in FEATURE_FLAGS:
        monkeypatch.setenv(flag.name, "false")

    result = set_all_feature_flags(True, store=store, env_path=env_path)
    env_text = env_path.read_text(encoding="utf-8")

    assert "Полный запуск" in result
    assert store.codex_chat_enabled() is True
    for flag in FEATURE_FLAGS:
        assert f"{flag.name}=true" in env_text
        assert os.environ[flag.name] == "true"


def test_feature_flag_command_updates_env_file(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("AVITO_SEND_ENABLED=false\nOTHER=value\n", encoding="utf-8")
    flag = FEATURE_FLAG_BY_COMMAND["avito_send_enabled"]
    monkeypatch.setenv("AVITO_SEND_ENABLED", "false")

    value, message = set_feature_flag(flag, "вкл", env_path=env_path)

    assert value is True
    assert "AVITO_SEND_ENABLED=true" in env_path.read_text(encoding="utf-8")
    assert "OTHER=value" in env_path.read_text(encoding="utf-8")
    assert "bot_restart" in message


def test_avito_photo_routes_to_human_handoff() -> None:
    event = {
        "type": "message",
        "content": {"text": "Что с кожей?", "image": {"id": "photo-1"}},
        "chat_id": "chat-2",
    }

    handoff = avito_photo_handoff(avito_inbound_message(event))

    assert handoff is not None
    assert handoff.reason == "photo_consultation"
    assert "оценить вложение" in handoff.summary
    assert "индивидуальная консультация" not in handoff.summary.casefold()


def test_avito_photo_urls_are_extracted_from_image_sizes() -> None:
    event = {
        "type": "message",
        "content": {
            "text": "Фото",
            "image": {"id": "img-1", "sizes": {"140x105": "https://img.example/small.jpg", "1280x960": "https://img.example/big.jpg"}},
        },
        "chat_id": "chat-photo",
    }

    message = avito_inbound_message(event)

    assert avito_photo_urls(event) == ["https://img.example/big.jpg"]
    assert message.metadata["photo_urls"] == ["https://img.example/big.jpg"]
    assert message.metadata["photo_ids"] == ["img-1"]


def test_avito_video_attachment_routes_to_handoff_media() -> None:
    event = {
        "type": "message",
        "chat_id": "chat-video",
        "content": {
            "text": "Прислала)",
            "video": {"id": "video-1", "url": "https://video.example/file.mp4"},
        },
    }

    message = avito_inbound_message(event)
    handoff = avito_photo_handoff(message)
    assert handoff is not None

    text = format_handoff_message(handoff)

    assert message.has_photo is True
    assert avito_media_urls(event) == ["https://video.example/file.mp4"]
    assert message.metadata["media_urls"] == ["https://video.example/file.mp4"]
    assert message.metadata["media_ids"] == ["video-1"]
    assert message.metadata["media_types"] == ["video"]
    assert "Медиа: видео/файл будет переслано" in text


def test_avito_turn_buffer_preserves_batched_media_metadata() -> None:
    first = avito_inbound_message(
        {
            "type": "message",
            "id": "msg-1",
            "chat_id": "chat-video",
            "content": {
                "text": "Прислала)",
                "video": {"id": "video-1", "url": "https://video.example/file.mp4"},
            },
        }
    )
    second = avito_inbound_message(
        {
            "type": "message",
            "id": "msg-2",
            "chat_id": "chat-video",
            "content": {"text": "Вот ещё вопрос"},
        }
    )

    merged = batch_to_inbound_message({"messages": [first.__dict__ | {"channel": first.channel.value}, second.__dict__ | {"channel": second.channel.value}]})
    handoff = avito_photo_handoff(merged)
    assert handoff is not None

    assert merged.has_photo is True
    assert merged.metadata["media_urls"] == ["https://video.example/file.mp4"]
    assert merged.metadata["media_ids"] == ["video-1"]
    assert merged.metadata["media_types"] == ["video"]
    assert "Медиа: видео/файл будет переслано" in format_handoff_message(handoff)


def test_avito_voice_id_is_extracted_from_message_content() -> None:
    message = avito_inbound_message(
        {
            "payload": {
                "type": "message_created",
                "value": {
                    "id": "voice-message-1",
                    "chat_id": "chat-voice",
                    "type": "voice",
                    "content": {"voice": {"voice_id": "voice-1"}},
                },
            }
        }
    )

    assert message.text == ""
    assert message.metadata["message_type"] == "voice"
    assert message.metadata["voice_id"] == "voice-1"


def test_avito_message_metadata_marks_actor_for_codex() -> None:
    message = avito_inbound_message(
        {
            "payload": {
                "type": "message_created",
                "value": {
                    "id": "own-1",
                    "author_id": 1,
                    "chat_id": "chat-own",
                    "direction": "out",
                    "type": "text",
                    "content": {"text": "Наш исходящий ответ"},
                },
            }
        }
    )

    annotated = annotate_avito_message_actor(message, _settings())

    assert annotated.metadata["direction"] == "out"
    assert annotated.metadata["is_own_account"] is True
    assert annotated.metadata["author_role"] == "own_account"


@pytest.mark.anyio
async def test_consultant_routes_complaints_and_risks_to_handoff() -> None:
    consultant = AvitoConsultant(AutomationToolbox(DryRunYClientsGateway()))
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "risk-chat",
            "content": {"text": "После процедуры сильная аллергия и отёк, что делать?"},
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "handoff"
    assert reply.handoff is not None
    assert reply.handoff.reason == "complaint_or_risk"
    assert "112" in reply.reply


def test_due_upsell_rules_respect_delay_and_skin_type() -> None:
    appointment = Appointment(
        client=ClientProfile(skin_type="сухая"),
        service=Service(title="Пилинг А"),
        starts_at=datetime.now() - timedelta(days=31),
    )
    rules = [
        UpsellRule(source_service="Пилинг", delay_days=30, recommendation="Увлажняющий крем", requires_skin_type=True),
        UpsellRule(source_service="Ботокс", delay_days=30, recommendation="Контрольный визит"),
    ]

    due = due_upsell_rules(appointment, rules)

    assert len(due) == 1
    assert due[0].recommendation == "Увлажняющий крем"


def test_care_upsell_planner_recommends_cosmetics_by_skin_type_after_visit() -> None:
    appointment = Appointment(
        client=ClientProfile(name="Анна", skin_type="сухая"),
        service=Service(title="Чистка лица"),
        starts_at=datetime(2026, 5, 25, 12, 0),
    )
    planner = CareUpsellPlanner(
        [
            UpsellRule(
                source_service="Чистка",
                delay_days=2,
                recommendation="Подойдет мягкий увлажняющий крем и SPF утром.",
                product_hint="увлажняющий крем",
                requires_skin_type=True,
            )
        ]
    )

    tasks = planner.tasks_for_appointment(appointment, now=datetime(2026, 5, 27, 12, 1))

    assert len(tasks) == 1
    assert tasks[0].kind == "product_recommendation"
    assert tasks[0].due_at == datetime(2026, 5, 27, 12, 0)
    assert "сухая" in tasks[0].message
    assert "увлажняющий крем" in tasks[0].message


def test_care_upsell_planner_recommends_next_service_after_interval_and_handoff_on_interest() -> None:
    appointment = Appointment(
        client=ClientProfile(name="Мария", phone="+79990000000"),
        service=Service(title="Пилинг А"),
        starts_at=datetime(2026, 1, 1, 10, 0),
    )
    planner = CareUpsellPlanner(
        [
            UpsellRule(
                source_service="Пилинг",
                delay_days=31,
                recommendation="Можно подобрать время на уходовую процедуру.",
                target_service="Биоревитализация",
            )
        ]
    )

    tasks = planner.tasks_for_appointments([appointment], now=datetime(2026, 2, 1, 10, 0))
    routed = planner.route_client_reply(tasks[0], "Да, хочу записаться")

    assert tasks[0].kind == "service_recommendation"
    assert "Биоревитализация" in tasks[0].message
    assert routed.handoff_to_cosmetologist is True
    assert routed.reason == "client_interested"


@pytest.mark.anyio
async def test_care_upsell_service_reads_yclients_appointments_for_date_range() -> None:
    gateway = DryRunYClientsGateway()
    await gateway.create_appointment(
        Appointment(
            client=ClientProfile(name="Мария", skin_type="комбинированная"),
            service=Service(title="Пилинг А"),
            starts_at=datetime(2026, 1, 1, 10, 0),
        )
    )
    planner = CareUpsellPlanner(
        [UpsellRule(source_service="Пилинг", delay_days=31, recommendation="Пора на следующий уход.", target_service="Биоревитализация")]
    )
    service = CareUpsellService(gateway, planner)

    tasks = await service.tasks_between("2026-01-01", "2026-01-02", now=datetime(2026, 2, 1, 10, 0))

    assert len(tasks) == 1
    assert tasks[0].client.name == "Мария"
    assert tasks[0].kind == "service_recommendation"


@pytest.mark.anyio
async def test_booking_flow_asks_for_city_first() -> None:
    gateway = DryRunYClientsGateway()
    flow = AvitoBookingFlow(gateway)
    message = avito_inbound_message({"type": "message", "text": "Хочу записаться на чистку"})

    decision = await flow.process(BookingRequest(message=message))

    assert decision.action == "ask_city"
    assert "город" in decision.reply.lower()


@pytest.mark.anyio
async def test_avito_consultant_asks_city_even_when_listing_has_city() -> None:
    gateway = DryRunYClientsGateway()
    consultant = AvitoConsultant(AutomationToolbox(gateway))
    message = avito_inbound_message(
        {
            "type": "message",
            "text": "Хочу записаться на чистку",
            "content": {
                "text": "Хочу записаться на чистку",
                "item": {"title": "Чистка лица", "city": "Москва"},
            },
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "ask_city"
    assert "город" in reply.reply.casefold()


@pytest.mark.anyio
async def test_booking_flow_offers_slots_after_city_service_and_date() -> None:
    gateway = DryRunYClientsGateway()
    flow = AvitoBookingFlow(gateway)
    slot = gateway.slots[0]
    message = avito_inbound_message({"type": "message", "text": "Ростов, чистка лица"})

    decision = await flow.process(
        BookingRequest(
            message=message,
            city=slot.city,
            service_query="чистка лица",
            preferred_date=slot.starts_at.date().isoformat(),
        )
    )

    assert decision.action == "offer_slots"
    assert decision.slots
    assert slot.starts_at.strftime("%H:%M") in decision.reply


@pytest.mark.anyio
async def test_booking_flow_creates_dry_run_appointment() -> None:
    gateway = DryRunYClientsGateway()
    flow = AvitoBookingFlow(gateway)
    slot = gateway.slots[0]
    message = avito_inbound_message(
        {
            "type": "message",
            "text": f"{slot.city}, чистка лица, телефон 8 999 123 45 67",
            "chat_id": "avito-chat",
        }
    )

    decision = await flow.process(
        BookingRequest(
            message=message,
            city=slot.city,
            service_query="чистка лица",
            preferred_date=slot.starts_at.date().isoformat(),
            preferred_time=slot.starts_at.strftime("%H:%M"),
        )
    )

    assert decision.action == "created"
    assert decision.appointment_id == 1
    assert len(gateway.appointments) == 1
    assert gateway.appointments[0].client.phone == "+79991234567"


@pytest.mark.anyio
async def test_booking_flow_routes_photo_to_handoff() -> None:
    gateway = DryRunYClientsGateway()
    flow = AvitoBookingFlow(gateway)
    message = avito_inbound_message(
        {"type": "message", "content": {"text": "Оцените кожу", "photo": {"id": "p1"}}, "chat_id": "chat"}
    )

    decision = await flow.process(BookingRequest(message=message))

    assert decision.action == "handoff"
    assert decision.handoff is not None


def test_telegram_admin_parser_understands_create_command() -> None:
    command = parse_admin_command(
        "Добавь запись клиент Анна, Ростов, чистка лица 2026-06-01 в 14:30, телефон 8 999 123 45 67"
    )

    assert command.action == "create_appointment"
    assert command.city == "Ростов-на-Дону"
    assert command.client_name == "Анна"
    assert command.phone == "+79991234567"
    assert command.preferred_date == "2026-06-01"
    assert command.preferred_time == "14:30"


@pytest.mark.anyio
async def test_telegram_admin_service_creates_moves_cancels_and_updates_notes() -> None:
    gateway = DryRunYClientsGateway(clients=[ClientProfile(name="Анна", phone="+79990000000", external_id="5")])
    service = TelegramAdminService(gateway)
    slot = gateway.slots[0]

    created = await service.handle_text(
        (
            f"Добавь запись клиент Анна, {slot.city}, чистка лица "
            f"{slot.starts_at.date().isoformat()} в {slot.starts_at.strftime('%H:%M')}, "
            "телефон 8 999 123 45 67"
        )
    )

    assert created.ok is True
    assert created.appointment_id == 1
    assert gateway.appointments[0].client.name == "Анна"

    moved = await service.handle_text("Перенеси запись 1 на 2026-06-02 в 15:00")

    assert moved.ok is True
    assert gateway.appointments[0].starts_at == datetime(2026, 6, 2, 15, 0)
    assert gateway.appointments[0].city == slot.city

    notes = await service.handle_text("Клиент 5 кожа сухая, заметка любит легкие кремы")

    assert notes.ok is True
    assert gateway.client_notes["5"] == ("любит легкие кремы", "сухая")

    cancelled = await service.handle_text("Отмени запись 1")

    assert cancelled.ok is True
    assert gateway.appointments == []


@pytest.mark.anyio
async def test_telegram_admin_transport_handles_text_voice_and_rejects_unknown_user() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []
            self.actions = []

        def send_message(self, chat_id, text, **delivery_params):
            self.messages.append((chat_id, text, delivery_params))

        def send_chat_action(self, chat_id, action="typing", **delivery_params):
            self.actions.append((chat_id, action, delivery_params))

    class FakeVoice:
        text = "Клиент 5 кожа сухая, заметка любит SPF"

    def fake_transcriber(bot, message):
        assert bot is fake_bot
        assert message["voice"]["file_id"] == "voice-1"
        return FakeVoice()

    fake_bot = FakeBot()
    gateway = DryRunYClientsGateway(clients=[ClientProfile(name="Анна", phone="+79990000000", external_id="5")])
    transport = TelegramAdminBotTransport(
        fake_bot,
        TelegramAdminService(gateway),
        _settings(),
        transcriber=fake_transcriber,
    )
    slot = gateway.slots[0]

    text_result = await transport.handle_update(
        {
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_thread_id": 42,
                "text": (
                    f"Добавь запись клиент Анна, {slot.city}, чистка лица "
                    f"{slot.starts_at.date().isoformat()} в {slot.starts_at.strftime('%H:%M')}, "
                    "телефон 8 999 123 45 67"
                ),
            }
        }
    )
    voice_result = await transport.handle_update(
        {
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "voice": {"file_id": "voice-1"},
            }
        }
    )
    forbidden = await transport.handle_update(
        {"message": {"chat": {"id": "stranger-chat"}, "from": {"id": 999}, "text": "Отмени запись 1"}}
    )
    empty_service = await transport.handle_update(
        {
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_thread_id": 43,
                "forum_topic_created": {"name": "Новая тема"},
            }
        }
    )

    assert text_result["ok"] is True
    assert text_result["appointment_id"] == 1
    assert voice_result["ok"] is True
    assert empty_service == {"ok": True, "ignored": True, "reason": "empty_service_message"}
    assert text_result["delivery_params"] == {"message_thread_id": "42"}
    assert fake_bot.actions[0] == ("admin-chat", "typing", {"message_thread_id": "42"})
    assert fake_bot.messages[0][2] == {"message_thread_id": "42"}
    assert gateway.client_notes["5"] == ("любит SPF", "сухая")
    assert forbidden["reason"] == "forbidden"
    assert fake_bot.messages[-1][1] == "Доступ только для администратора."


def test_telegram_delivery_params_preserve_threaded_ai_topics_and_business_connection() -> None:
    message = {
        "message_thread_id": 7,
        "direct_messages_topic": {"topic_id": 99},
    }

    assert telegram_delivery_params(message, "biz-1") == {
        "message_thread_id": "7",
        "direct_messages_topic_id": "99",
        "business_connection_id": "biz-1",
    }


def test_empty_topic_service_reply_is_not_codex_context() -> None:
    message = {
        "from": {"username": "gr8brite"},
        "reply_to_message": {
            "message_id": 10,
            "forum_topic_created": {"name": "Новая тема"},
        },
    }

    assert telegram_embedded_message_context("Ответ на сообщение", message["reply_to_message"]) == []
    assert annotate_sender_for_codex(message, "") == ""

    text_reply = dict(message)
    text_reply["text"] = "А глянь голосовое"

    assert "[без текста; тип: неизвестно]" not in annotate_sender_for_codex(text_reply, text_reply["text"])
    assert "А глянь голосовое" in annotate_sender_for_codex(text_reply, text_reply["text"])


@pytest.mark.anyio
async def test_telegram_admin_polling_starts_after_pending_updates() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.offsets = []

        def get_updates(self, offset=None):
            self.offsets.append(offset)
            if offset is None:
                return [{"update_id": 10}, {"update_id": 12}]
            return []

    class FakeTransport:
        def __init__(self) -> None:
            self.bot = FakeBot()

    transport = FakeTransport()

    offset = await telegram_admin_bot_module._initial_admin_polling_offset(transport)

    assert offset == 13
    assert transport.bot.offsets == [None]


@pytest.mark.anyio
async def test_telegram_admin_transport_handles_auth_and_mfa_commands(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []
            self.deleted = []

        def send_message(self, chat_id, text, **delivery_params):
            self.messages.append((chat_id, text, delivery_params))

        def api(self, method, payload=None, timeout=30):
            self.deleted.append((method, payload, timeout))
            return {"ok": True}

    monkeypatch.setattr(telegram_admin_bot_module, "codex_auth_status", lambda: "status **ok**")
    monkeypatch.setattr(telegram_admin_bot_module, "start_codex_device_login", lambda: "login `1234`")
    monkeypatch.setattr(telegram_admin_bot_module, "codex_logout_reset", lambda: "logged out")
    monkeypatch.setattr(telegram_admin_bot_module, "mfa_status", lambda: "MFA настроена")
    monkeypatch.setattr(telegram_admin_bot_module, "mfa_code_text", lambda: "Текущий MFA-код: `123456`")
    monkeypatch.setattr(telegram_admin_bot_module, "save_totp_secret", lambda secret: f"saved {secret[-4:]}")
    monkeypatch.setattr(telegram_admin_bot_module, "delete_totp_secret", lambda: "deleted")

    fake_bot = FakeBot()
    transport = TelegramAdminBotTransport(fake_bot, TelegramAdminService(DryRunYClientsGateway()), _settings())

    auth = await transport.handle_update({"message": {"chat": {"id": "admin-chat"}, "from": {"id": 1}, "text": "/codex_auth"}})
    mfa = await transport.handle_update({"message": {"chat": {"id": "admin-chat"}, "from": {"id": 1}, "text": "/mfa"}})
    mfa_set = await transport.handle_update(
        {
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_id": 77,
                "text": "/mfa_set JBSWY3DPEHPK3PXP",
            }
        }
    )

    assert auth["action"] == "codex_auth"
    assert mfa["action"] == "mfa"
    assert mfa_set["action"] == "mfa_set"
    assert fake_bot.messages[0][1].startswith("<b>Codex auth:</b>")
    assert "<b>ok</b>" in fake_bot.messages[0][1]
    assert "123456" in fake_bot.messages[1][1]
    assert fake_bot.deleted == [("deleteMessage", {"chat_id": "admin-chat", "message_id": "77"}, 8)]


@pytest.mark.anyio
async def test_telegram_admin_transport_streams_codex_live_drafts(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []
            self.drafts = []
            self.actions = []

        def send_message(self, chat_id, text, **delivery_params):
            self.messages.append((chat_id, text, delivery_params))

        def send_message_draft(self, chat_id, draft_id, text=None, message_thread_id=None):
            self.drafts.append((chat_id, draft_id, text, message_thread_id))

        def send_chat_action(self, chat_id, action="typing", **delivery_params):
            self.actions.append((chat_id, action, delivery_params))

    async def fake_runner(payload, trace, progress_callback=None):
        if progress_callback:
            progress_callback("Понимаю команду\nПроверяю tools")
        return {"action": "codex_admin_reply", "ok": True, "reply": "Готово."}

    settings = replace(_settings(), telegram_admin_live_draft_interval_seconds=0)
    service = CodexTelegramAdminService(
        AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json")),
        settings,
        runner=fake_runner,
    )
    fake_bot = FakeBot()
    transport = TelegramAdminBotTransport(fake_bot, service, settings)

    result = await transport.handle_update(
        {
            "update_id": 123,
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_thread_id": 42,
                "text": "Проверь запись",
            },
        }
    )

    assert result["ok"] is True
    assert fake_bot.drafts[0][2] == ""
    assert fake_bot.drafts[0][3] == "42"
    assert fake_bot.drafts[1][2].startswith("<b>Codex live:</b>")
    assert "Проверяю tools" in fake_bot.drafts[1][2]
    assert "<b>" in fake_bot.drafts[1][2]
    assert fake_bot.drafts[1][3] == "42"
    assert fake_bot.messages[-1] == ("admin-chat", "Готово.", {"message_thread_id": "42"})


@pytest.mark.anyio
async def test_telegram_admin_transport_defers_slow_codex_result(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        def send_message(self, chat_id, text, **delivery_params):
            self.messages.append((chat_id, text, delivery_params))

        def send_message_draft(self, chat_id, draft_id, text=None, message_thread_id=None):
            return None

        def send_chat_action(self, chat_id, action="typing", **delivery_params):
            return None

    async def fake_runner(payload, trace, progress_callback=None):
        await asyncio.sleep(0.05)
        return {"action": "codex_admin_reply", "ok": True, "reply": "Финальный ответ."}

    settings = replace(
        _settings(),
        telegram_admin_live_drafts_enabled=False,
        telegram_admin_response_wait_seconds=1,
        telegram_admin_history_db_path=tmp_path / "admin.sqlite3",
    )
    service = CodexTelegramAdminService(
        AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json")),
        settings,
        runner=fake_runner,
    )
    fake_bot = FakeBot()
    store = LeadStore(settings.telegram_admin_history_db_path)
    transport = TelegramAdminBotTransport(fake_bot, service, settings, history_store=store)
    transport._response_wait_seconds = lambda: 0.01  # type: ignore[method-assign]

    result = await transport.handle_update(
        {
            "update_id": 123,
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_thread_id": 42,
                "text": "Долгая команда",
            },
        }
    )
    await asyncio.sleep(0.08)

    assert result["action"] == "codex_deferred"
    assert fake_bot.messages[0] == (
        "admin-chat",
        "Codex ещё работает. Как только закончит, отправлю результат следующим сообщением.",
        {"message_thread_id": "42"},
    )
    assert fake_bot.messages[-1] == ("admin-chat", "Финальный ответ.", {"message_thread_id": "42"})
    history = store.recent_codex_chat(0, result["history_key"])
    assert [row["role"] for row in history] == ["user", "assistant"]


@pytest.mark.anyio
async def test_telegram_admin_context_is_persisted_per_thread(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        def send_message(self, chat_id, text, **delivery_params):
            self.messages.append((chat_id, text, delivery_params))

        def send_message_draft(self, chat_id, draft_id, text=None, message_thread_id=None):
            return None

        def send_chat_action(self, chat_id, action="typing", **delivery_params):
            return None

    payloads = []

    async def fake_runner(payload, trace, progress_callback=None):
        payloads.append(payload)
        return {"action": "codex_admin_reply", "ok": True, "reply": f"Ответ {len(payloads)}"}

    settings = replace(_settings(), telegram_admin_live_drafts_enabled=False, telegram_admin_history_db_path=tmp_path / "admin.sqlite3")
    service = CodexTelegramAdminService(
        AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json")),
        settings,
        runner=fake_runner,
    )
    store = LeadStore(settings.telegram_admin_history_db_path)
    transport = TelegramAdminBotTransport(FakeBot(), service, settings, history_store=store)

    first = await transport.handle_update(
        {
            "update_id": 1,
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_id": 10,
                "message_thread_id": 42,
                "text": "Клиентка Анна хочет чистку",
            },
        }
    )
    second = await transport.handle_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_id": 11,
                "message_thread_id": 42,
                "text": "Запиши ее завтра",
            },
        }
    )

    assert first["history_key"] == "telegram:admin:admin-chat:thread:42"
    assert second["history_key"] == "telegram:admin:admin-chat:thread:42"
    assert payloads[0]["message"]["conversation_history"] == []
    assert [item["role"] for item in payloads[1]["message"]["conversation_history"]] == ["user", "assistant"]
    assert "Клиентка Анна" in payloads[1]["message"]["conversation_history"][0]["content"]
    assert payloads[1]["message"]["history_key"] == "telegram:admin:admin-chat:thread:42"
    assert payloads[1]["conversation_key"] == "telegram:admin:admin-chat:thread:42"


@pytest.mark.anyio
async def test_telegram_admin_history_is_capped_with_summary_even_when_config_unlimited(tmp_path) -> None:
    class FakeBot:
        def send_message(self, chat_id, text, **delivery_params):
            return None

        def send_message_draft(self, chat_id, draft_id, text=None, message_thread_id=None):
            return None

        def send_chat_action(self, chat_id, action="typing", **delivery_params):
            return None

    payloads = []

    async def fake_runner(payload, trace, progress_callback=None):
        payloads.append(payload)
        return {"action": "codex_admin_reply", "ok": True, "reply": "Готово."}

    settings = replace(
        _settings(),
        telegram_admin_live_drafts_enabled=False,
        telegram_admin_history_limit=0,
        telegram_admin_history_db_path=tmp_path / "admin.sqlite3",
    )
    service = CodexTelegramAdminService(
        AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json")),
        settings,
        runner=fake_runner,
    )
    store = LeadStore(settings.telegram_admin_history_db_path)
    history_key = "telegram:admin:admin-chat:thread:42"
    for index in range(25):
        store.add_codex_chat_message("user" if index % 2 == 0 else "assistant", f"old message {index}", history_key)
    transport = TelegramAdminBotTransport(FakeBot(), service, settings, history_store=store)

    await transport.handle_update(
        {
            "update_id": 7,
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_id": 17,
                "message_thread_id": 42,
                "text": "Продолжи задачу",
            },
        }
    )

    history = payloads[0]["message"]["conversation_history"]
    assert len(history) == 21
    assert history[0]["role"] == "system"
    assert "Краткое резюме памяти" in history[0]["content"]
    assert "old message 4" not in json.dumps(history, ensure_ascii=False)
    assert "old message 24" in json.dumps(history, ensure_ascii=False)


@pytest.mark.anyio
async def test_telegram_admin_history_keeps_tool_trace_for_non_linear_followups(tmp_path) -> None:
    class FakeBot:
        def send_message(self, chat_id, text, **delivery_params):
            return None

        def send_message_draft(self, chat_id, draft_id, text=None, message_thread_id=None):
            return None

        def send_chat_action(self, chat_id, action="typing", **delivery_params):
            return None

    calls = 0

    async def fake_runner(payload, trace, progress_callback=None):
        nonlocal calls
        calls += 1
        if not trace:
            return {"tool_calls": [{"name": "yclients.services.list", "arguments": {"city": "Ростов-на-Дону"}}]}
        return {"action": "codex_admin_reply", "ok": True, "reply": "Проверила услуги."}

    settings = replace(_settings(), telegram_admin_live_drafts_enabled=False, telegram_admin_history_db_path=tmp_path / "admin.sqlite3")
    service = CodexTelegramAdminService(
        AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json")),
        settings,
        runner=fake_runner,
    )
    store = LeadStore(settings.telegram_admin_history_db_path)
    transport = TelegramAdminBotTransport(FakeBot(), service, settings, history_store=store)

    result = await transport.handle_update(
        {
            "update_id": 4,
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_id": 13,
                "message_thread_id": 42,
                "text": "Какие услуги есть в Ростове?",
            },
        }
    )

    assert result["ok"] is True
    history = store.recent_codex_chat(2, result["history_key"])
    assert "Tool trace:" in history[1]["content"]
    assert "yclients.services.list" in history[1]["content"]
    assert "Ростов-на-Дону" in history[1]["content"]


@pytest.mark.anyio
async def test_telegram_admin_photo_is_downloaded_and_available_to_codex(tmp_path, monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        def send_message(self, chat_id, text, **delivery_params):
            self.messages.append((chat_id, text, delivery_params))

        def send_message_draft(self, chat_id, draft_id, text=None, message_thread_id=None):
            return None

        def send_chat_action(self, chat_id, action="typing", **delivery_params):
            return None

    payloads = []

    async def fake_runner(payload, trace, progress_callback=None):
        payloads.append(payload)
        return {"action": "codex_admin_reply", "ok": True, "reply": "Фото отправлено."}

    downloaded = tmp_path / "telegram-photo.jpg"
    downloaded.write_bytes(b"image")
    monkeypatch.setattr(telegram_admin_bot_module, "_download_telegram_photo", lambda bot, file_id: downloaded)

    settings = replace(_settings(), telegram_admin_live_drafts_enabled=False, telegram_admin_history_db_path=tmp_path / "admin.sqlite3")
    service = CodexTelegramAdminService(
        AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json")),
        settings,
        runner=fake_runner,
    )
    store = LeadStore(settings.telegram_admin_history_db_path)
    transport = TelegramAdminBotTransport(FakeBot(), service, settings, history_store=store)

    result = await transport.handle_update(
        {
            "update_id": 3,
            "message": {
                "chat": {"id": "admin-chat"},
                "from": {"id": 1},
                "message_id": 12,
                "photo": [{"file_id": "small", "width": 10, "height": 10}, {"file_id": "big", "width": 100, "height": 100}],
                "caption": "Отправь это фото клиенту в Авито chat-1",
            },
        }
    )

    assert result["ok"] is True
    assert payloads[0]["message"]["attachments"][0]["image_path"] == str(downloaded)
    assert payloads[0]["message"]["attachments"][0]["file_id"] == "big"
    history = store.recent_codex_chat(2, result["history_key"])
    assert "image_path=" in history[0]["content"]


def test_telegram_history_key_is_scoped_by_topic() -> None:
    assert telegram_history_key("chat-1", {"message_thread_id": "42"}) == "telegram:admin:chat-1:thread:42"
    assert telegram_history_key("chat-1", {"direct_messages_topic_id": "7"}) == "telegram:admin:chat-1:direct:7"
    assert telegram_history_key("chat-1", {}) == "telegram:admin:chat-1"
    assert telegram_history_key("chat-1", {"message_thread_id": "42"}, role=CodexRole.OLGA_BOSS) == "telegram:olga_boss:chat-1:thread:42"


def test_conversation_key_covers_all_role_based_channels() -> None:
    assert conversation_key("avito", CodexRole.AVITO_CLIENT, "chat-1") == "avito:client:chat-1"
    assert conversation_key("telegram_client", CodexRole.TELEGRAM_CLIENT, "chat-1") == "telegram_client:client:chat-1"
    assert conversation_key("vk", CodexRole.VK_CLIENT, "peer-1") == "vk:client:peer-1"
    assert conversation_key("yclients", CodexRole.YCLIENTS_UPSELL_STUB, "appointment-1") == "yclients:upsell:appointment-1"


def test_olga_and_upsell_roles_include_crm_interaction_rules() -> None:
    olga_rules = "\n".join(role_profile(CodexRole.OLGA_BOSS).reply_rules)
    upsell_profile = role_profile(CodexRole.YCLIENTS_UPSELL_STUB)
    upsell_rules = "\n".join(upsell_profile.reply_rules)

    assert "внутренний CRM-факт" in olga_rules
    assert "заполнять анкету" in olga_rules
    assert "paid/spent" in olga_rules
    assert upsell_profile.prompt_role == "yclients_upsell_planner"
    assert "подтверждённым фактам" in upsell_rules
    assert "фактическая услуга" in upsell_rules


def test_telegram_client_role_is_care_consultant() -> None:
    profile = role_profile(CodexRole.TELEGRAM_CLIENT)
    rules = "\n".join(profile.reply_rules)

    assert profile.prompt_role == "telegram_client_care_consultant"
    assert profile.allows_tool("care.crm.clients.search")
    assert profile.allows_tool("care.crm.visits.list")
    assert not profile.allows_tool("care.crm.interactions.list")
    assert not profile.allows_tool("yclients.appointments.create")
    assert not profile.allows_tool("yclients.appointments.move")
    assert not profile.allows_tool("yclients.appointments.cancel")
    assert not profile.allows_tool("yclients.clients.notes.update")
    assert not profile.allows_tool("avito.messages.send")
    assert "отдел заботы" in profile.goal
    assert "новым, повторным" in rules
    assert "CRM-факты" in rules
    assert "внутренние данные CRM" in rules


def test_avito_client_role_hides_live_yclients_mutations() -> None:
    profile = role_profile(CodexRole.AVITO_CLIENT)
    rules = "\n".join(profile.reply_rules)

    assert profile.allows_tool("yclients.services.list")
    assert profile.allows_tool("yclients.slots.list")
    assert not profile.allows_tool("yclients.appointments.create")
    assert not profile.allows_tool("yclients.appointments.move")
    assert not profile.allows_tool("yclients.appointments.cancel")
    assert not profile.allows_tool("yclients.clients.notes.update")
    assert "не превращай слова" in rules.casefold()


def test_client_message_router_blocks_ambiguous_booking_before_llm() -> None:
    message = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-route",
        chat_id="chat-route",
        text="На следующую неделю, 950-025-01-15 имя Галина, хочу личную встречу",
    )

    route = route_client_message(message)

    assert route.route == "ask_service"
    assert route.block_autoanswer_reason == "booking_without_service"


def test_client_message_router_routes_high_confidence_rag_answer() -> None:
    message = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-route-rag",
        chat_id="chat-route-rag",
        text="Сколько ягодицы 400 мл?",
    )

    route = route_client_message(
        message,
        retrieved_expert_answers=[
            {
                "answer_client": "400 мл Tesoro Body стоит 110 000.",
                "score": 0.91,
                "_retrieval_safe_for_autoanswer": True,
            }
        ],
        autoanswer_threshold=0.82,
    )

    assert route.route == "rag_answer"
    assert route.service_key == "yagodicy"


def test_client_message_router_handoffs_aesthetic_volume_expectations() -> None:
    route = route_client_message(
        InboundMessage(
            channel=Channel.AVITO,
            client_id="client-expectation",
            chat_id="chat-expectation",
            text="300 мл хватит на грудь, будет плюс один размер?",
        )
    )

    assert route.route == "expert_expectation_handoff"
    assert route.handoff_reason == HandoffReason.EXPERT_EXPECTATION.value
    assert route.block_autoanswer_reason == "aesthetic_expectation_guard"
    assert route.metadata["reason"] == "нельзя автообещать результат по мл"


def test_client_message_router_allows_price_only_volume_rag_answer() -> None:
    message = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-price-volume",
        chat_id="chat-price-volume",
        text="Сколько стоит 300 мл ягодицы?",
    )

    route = route_client_message(
        message,
        retrieved_expert_answers=[
            {
                "answer_client": "300 мл Tesoro Body стоит 75 000.",
                "score": 0.91,
                "_retrieval_safe_for_autoanswer": True,
            }
        ],
        autoanswer_threshold=0.82,
    )

    assert route.route == "rag_answer"
    assert route.service_key == "yagodicy"


def test_client_message_router_blocks_risk_address_and_media() -> None:
    risk = route_client_message(
        InboundMessage(
            channel=Channel.AVITO,
            client_id="client-risk-route",
            chat_id="chat-risk-route",
            text="После процедуры отёк и температура",
        )
    )
    address = route_client_message(
        InboundMessage(channel=Channel.AVITO, client_id="client-address-route", chat_id="chat-address-route", text="Какой адрес?")
    )
    media = route_client_message(
        InboundMessage(
            channel=Channel.AVITO,
            client_id="client-media-route",
            chat_id="chat-media-route",
            text="Посмотрите фото",
            has_photo=True,
        )
    )

    assert risk.route == "risk_handoff"
    assert risk.handoff_reason == HandoffReason.COMPLAINT_OR_RISK.value
    assert address.route == "ask_city"
    assert media.route == "media_handoff"


def test_client_message_router_routes_booking_critical_context_to_urgent_handoff() -> None:
    route = route_client_message(
        InboundMessage(
            channel=Channel.AVITO,
            client_id="client-booking-critical",
            chat_id="chat-booking-critical",
            text="Вы про меня забыли? Мне завтра приходить? Адрес?",
        ),
        conversation_history=(
            {"role": "user", "content": "Хочу записаться на грудь в Краснодаре"},
            {"role": "assistant", "content": "Подберем время записи."},
        ),
    )

    assert route.route == "booking_critical_handoff"
    assert route.handoff_reason == HandoffReason.BOOKING_CRITICAL.value
    assert route.metadata["urgent"] is True
    assert route.metadata["sla"] == "booking_critical"


def test_client_message_router_treats_booked_address_without_history_as_critical() -> None:
    route = route_client_message(
        InboundMessage(
            channel=Channel.AVITO,
            client_id="client-booked-address",
            chat_id="chat-booked-address",
            text="Я к вам записана, адрес не напишите?",
        ),
        conversation_history=(),
    )

    assert route.route == "booking_critical_handoff"
    assert route.handoff_reason == HandoffReason.BOOKING_CRITICAL.value
    assert route.metadata["urgent"] is True


@pytest.mark.anyio
async def test_avito_consultant_creates_booking_critical_handoff(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    consultant = AvitoConsultant(toolbox)
    message = avito_inbound_message(
        {"type": "message", "chat_id": "chat-critical", "content": {"text": "Вы про меня забыли? Что делать, адрес?"}}
    )

    reply = await consultant.respond(
        message,
        conversation_history=({"role": "user", "content": "Хочу записаться на грудь в Краснодаре"},),
    )

    assert reply.action == "handoff"
    assert reply.handoff is not None
    assert reply.handoff.reason == HandoffReason.BOOKING_CRITICAL
    assert "подтверж" in reply.reply.casefold()


@pytest.mark.anyio
async def test_avito_consultant_handoffs_aesthetic_volume_expectation(tmp_path) -> None:
    consultant = AvitoConsultant(AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json")))
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-expectation",
            "content": {"text": "300 мл хватит на грудь? Будет заметный результат?"},
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "handoff"
    assert reply.handoff is not None
    assert reply.handoff.reason == HandoffReason.EXPERT_EXPECTATION
    assert reply.reply == "По объёму и ожидаемому результату лучше не обещать вслепую. Передам Ольге, она посмотрит и сориентирует точнее."
    assert "нельзя автообещать результат по мл" in reply.handoff.summary


@pytest.mark.anyio
async def test_aesthetic_expectation_handoff_ref_is_critical_for_sla(tmp_path) -> None:
    from src.freelance_leads_bot.integrations.handoff_notify import TelegramHandoffNotifier

    class FakeTelegramBot:
        def __init__(self) -> None:
            self.messages = []

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": len(self.messages)}}

    message = avito_inbound_message(
        {
            "type": "message",
            "id": "m-expert-1",
            "chat_id": "chat-expert",
            "content": {"text": "300 мл хватит на грудь, будет плюс один размер?"},
        }
    )
    handoff = Handoff(
        reason=HandoffReason.EXPERT_EXPECTATION,
        message=message,
        summary="Нельзя автообещать результат по мл. Проверьте вопрос клиента.",
    )
    ref_path = tmp_path / "handoff_refs.json"
    notifier = TelegramHandoffNotifier(FakeTelegramBot(), "admin-chat", ref_path=ref_path, topics_enabled=False)

    result = await notifier.notify(handoff)
    refs = load_telegram_handoff_refs(ref_path)
    ref = next(iter(refs.values()))

    assert result["telegram_handoff_ref"]["reason"] == HandoffReason.EXPERT_EXPECTATION.value
    assert ref["urgency"] == "critical"
    assert ref["sla"] == "critical"
    assert ref["deadline_at"] > 0
    assert ref["escalation_at"] > 0
    assert "объёму/ожидаемому результату" in ref["client_waits_for"]
    assert handoff_ref_is_critical(ref) is True


@pytest.mark.anyio
async def test_elena_acceptance_flow_keeps_booking_critical_control(tmp_path, monkeypatch) -> None:
    from src.freelance_leads_bot.integrations.handoff_notify import TelegramHandoffNotifier
    import src.freelance_leads_bot.integrations.handoff_notify as handoff_notify

    class FakeTelegramBot:
        def __init__(self) -> None:
            self.messages = []
            self.edits = []
            self.photos = []

        def send_message(self, chat_id, text):
            message_id = len(self.messages) + 1
            self.messages.append((chat_id, text))
            return {"ok": True, "result": {"message_id": message_id}}

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))
            return {"ok": True, "result": {"message_id": message_id}}

        def send_photo(self, chat_id, path, caption=None):
            self.photos.append((chat_id, str(path), caption))
            return {"ok": True, "result": {"message_id": 100 + len(self.photos)}}

    async def direct_retry(func, *args, **kwargs):
        return func(*args)

    def download_photo(url, media_dir):
        path = tmp_path / "elena-photo.jpg"
        path.write_bytes(b"image")
        return path

    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    knowledge.create(
        kind="service_note",
        title="Объем для увеличения груди",
        content="Объём подбирается индивидуально по фото и желаемому результату.",
        tags=("объем", "грудь"),
    )
    consultant = AvitoConsultant(AutomationToolbox(DryRunYClientsGateway(), knowledge))
    monkeypatch.setattr(handoff_notify, "_to_thread_retry", direct_retry)
    monkeypatch.setattr(handoff_notify, "_download_photo_url", download_photo)
    ref_path = tmp_path / "handoff_refs.json"
    bot = FakeTelegramBot()
    notifier = TelegramHandoffNotifier(bot, "admin-chat", ref_path=ref_path)
    history: list[dict[str, str]] = []

    events = [
        {"id": "elena-1", "text": "Можно записаться на увеличение груди 15 июля в 10:00?", "item": {"title": "Увеличение груди", "city": "Краснодар"}},
        {"id": "elena-2", "text": "Фото отправила", "image": {"url": "https://img.example/elena.jpg"}},
        {"id": "elena-3", "text": "Какой объем нужен?"},
        {"id": "elena-4", "text": "Как оплатить?"},
        {"id": "elena-5", "text": "Какой адрес?"},
        {"id": "elena-6", "text": "Вы про меня забыли?"},
        {"id": "elena-7", "text": "Что делать?"},
    ]
    replies: list[AvitoConsultantReply] = []
    for event in events:
        content = {"text": event["text"]}
        if event.get("item"):
            content["item"] = event["item"]
        if event.get("image"):
            content["image"] = event["image"]
        message = avito_inbound_message({"type": "message", "id": event["id"], "chat_id": "chat-elena", "content": content})
        reply = await consultant.respond(message, conversation_history=tuple(history))
        replies.append(reply)
        history.append({"role": "user", "content": event["text"]})
        if reply.reply:
            history.append({"role": "assistant", "content": reply.reply})
        if reply.handoff:
            await notifier.notify(reply.handoff)

    refs = load_telegram_handoff_refs(ref_path)
    urgent_refs = [ref for ref in refs.values() if ref.get("urgency") == "critical"]
    unsafe_reply_text = "\n".join(reply.reply for reply in replies if reply.reply).casefold()
    now = max(int(ref.get("created_at") or 0) for ref in refs.values()) + 4 * 60 * 60
    for ref in refs.values():
        ref["created_at"] = now - 4 * 60 * 60
    save_telegram_handoff_refs(refs, ref_path)
    sla = await process_handoff_sla(notifier, ref_path=ref_path, now=now, reminder_after_seconds=30 * 60)

    assert len(urgent_refs) >= 4
    assert len(bot.edits) <= 1
    assert all("СРОЧНО" in ref["handoff_text"] for ref in urgent_refs)
    assert "приходите" not in unsafe_reply_text
    assert "запись актуальна" not in unsafe_reply_text
    assert "можем предложить" not in unsafe_reply_text
    assert all(ref.get("deadline_at") for ref in urgent_refs)
    assert sla["deduped"] >= len(refs) - 1
    assert sla["reminders"] == 1
    assert sla["escalations"] == 1


def test_rag_answer_service_filters_unsafe_retrieval() -> None:
    service = RagAnswerService(autoanswer_threshold=0.82)

    safe = service.from_retrieved(
        [
            {
                "id": 123,
                "answer_client": "Tesoro Body держится до 4 лет.",
                "score": 0.9,
                "risk_level": "low",
                "_retrieval_safe_for_autoanswer": True,
            }
        ]
    )
    low = service.from_retrieved([{"answer_client": "Ответ", "score": 0.4, "_retrieval_safe_for_autoanswer": True}])
    high_risk = service.from_retrieved(
        [{"answer_client": "Ответ", "score": 0.9, "risk_level": "high", "_retrieval_safe_for_autoanswer": True}]
    )
    unsafe = service.from_retrieved([{"answer_client": "Ответ", "score": 0.9, "_retrieval_safe_for_autoanswer": False}])

    assert safe is not None
    assert safe.answer == "Tesoro Body держится до 4 лет."
    assert low is None
    assert high_risk is None
    assert unsafe is None


def test_client_roles_share_readonly_tools_and_keep_rag_admin_private() -> None:
    dangerous = {
        "yclients.appointments.create",
        "yclients.appointments.move",
        "yclients.appointments.cancel",
        "yclients.clients.notes.update",
        "knowledge.create",
        "knowledge.update",
        "knowledge.delete",
        "expert_rag.plan_change",
        "expert_rag.apply_plan",
    }
    for role in (CodexRole.AVITO_CLIENT, CodexRole.TELEGRAM_CLIENT, CodexRole.VK_CLIENT):
        profile = role_profile(role)
        assert profile.allows_tool("yclients.services.list")
        assert profile.allows_tool("yclients.slots.list")
        assert profile.allows_tool("care.crm.interactions.create")
        assert all(not profile.allows_tool(tool) for tool in dangerous)

    for role in (CodexRole.ADMIN, CodexRole.OLGA_BOSS):
        profile = role_profile(role)
        assert profile.allows_tool("yclients.appointments.create")
        assert profile.allows_tool("yclients.appointments.move")
        assert profile.allows_tool("yclients.appointments.cancel")
        assert profile.allows_tool("yclients.clients.notes.update")
        assert profile.allows_tool("expert_rag.plan_change")
        assert profile.allows_tool("expert_rag.apply_plan")


def test_telegram_client_inbound_message_extracts_identity_and_photos() -> None:
    message = telegram_client_inbound_message(
        {
            "message_id": 42,
            "date": 1_720_000_000,
            "chat": {"id": 1001},
            "from": {"id": 2002, "first_name": "Анна", "last_name": "Петрова", "username": "anna_p"},
            "caption": "Здравствуйте, хочу записаться повторно",
            "photo": [{"file_id": "small"}, {"file_id": "big"}],
        }
    )

    assert message.channel == Channel.TELEGRAM_CLIENT
    assert message.client_id == "2002"
    assert message.chat_id == "1001"
    assert message.message_id == "42"
    assert message.text == "Здравствуйте, хочу записаться повторно"
    assert message.has_photo is True
    assert message.metadata["client_name"] == "Анна Петрова"
    assert message.metadata["photo_ids"] == ["small", "big"]


@pytest.mark.anyio
async def test_telegram_client_bot_persists_history_and_injects_crm_context(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": 99}}

    class FakePlanner:
        def __init__(self) -> None:
            self.contexts = []

        async def respond(self, context, toolbox):
            self.contexts.append(context)
            return AvitoConsultantReply(action="codex_reply", reply="Рада снова видеть вас. Как самочувствие после губ?")

    crm = CareCrmStore(tmp_path / "care.sqlite3")
    row = crm.upsert_appointment(
        Appointment(
            id=780,
            client=ClientProfile(name="Анна Петрова", phone="+7 900 111 22 33", external_id="88"),
            service=Service(id=9, title="Увеличение губ", price=12000, duration_minutes=60),
            city="Москва",
            starts_at=datetime(2026, 6, 2, 18, 30),
        )
    )
    crm.mark_visit(int(row["id"]), attended=True, actual_service_title="Губы 1 мл", confirmed_by="test")
    history = LeadStore(tmp_path / "history.sqlite3")
    planner = FakePlanner()
    bot = TelegramClientCareBot(
        settings=replace(
            _settings(),
            telegram_admin_history_db_path=tmp_path / "history.sqlite3",
            telegram_client_codex_enabled=True,
        ),
        bot=FakeBot(),
        planner=planner,
        history_store=history,
        care_crm=crm,
    )

    result = await bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 42,
                "date": 1_720_000_000,
                "chat": {"id": 1001},
                "from": {"id": 2002, "first_name": "Анна", "last_name": "Петрова"},
                "text": "Здравствуйте, я повторно после губ",
            },
        }
    )

    assert result["ok"] is True
    assert result["conversation_key"] == "telegram_client:client:1001"
    assert planner.contexts
    injected_history = planner.contexts[0].conversation_history
    assert any("CRM context" in item["content"] and "Губы 1 мл" in item["content"] for item in injected_history)
    stored = history.recent_codex_chat(10, telegram_client_history_key(planner.contexts[0].message))
    assert stored[0]["role"] == "user"
    assert "повторно после губ" in stored[0]["content"]
    assert stored[1]["role"] == "assistant"
    assert "Рада снова видеть" in stored[1]["content"]


@pytest.mark.anyio
async def test_telegram_client_start_and_phone_binding_flow(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []
            self.photos = []

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": len(self.messages)}}

        def send_photo_url(self, chat_id, photo_url, caption=None, **kwargs):
            self.photos.append((chat_id, photo_url, caption, kwargs))
            return {"ok": True, "result": {"message_id": 100 + len(self.photos)}}

    class FakePlanner:
        async def respond(self, context, toolbox):
            return AvitoConsultantReply(action="codex_reply", reply="Нашла ваш прошлый визит, помогу с уходом.")

    crm = CareCrmStore(tmp_path / "care.sqlite3")
    client_id = crm.upsert_client(ClientProfile(name="Анна Петрова", phone="+79001112233"), city="Москва")
    bot_api = FakeBot()
    bot = TelegramClientCareBot(
        settings=replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3"),
        bot=bot_api,
        planner=FakePlanner(),
        history_store=LeadStore(tmp_path / "history.sqlite3"),
        care_crm=crm,
    )

    start = await bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_720_000_000,
                "chat": {"id": 1001},
                "from": {"id": 2002, "first_name": "Анна"},
                "text": "/start",
            },
        }
    )
    reply = await bot.handle_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 2,
                "date": 1_720_000_100,
                "chat": {"id": 1001},
                "from": {"id": 2002, "first_name": "Анна"},
                "text": "Да, мой телефон +7 900 111 22 33",
            },
        }
    )

    linked = crm.find_client_by_link(channel="telegram_client", external_user_id="2002", chat_id="1001")
    interactions = crm.list_client_interactions(client_id, limit=10)
    assert start["action"] == "start"
    assert "Вы уже были у Ольги" in start["reply"]
    assert reply["reply"] == "Нашла ваш прошлый визит, помогу с уходом."
    assert linked is not None
    assert int(linked["id"]) == client_id
    assert any("мой телефон" in item["body"] for item in interactions)


@pytest.mark.anyio
async def test_telegram_client_can_set_do_not_contact_from_client_request(tmp_path) -> None:
    class FakeBot:
        def send_message(self, chat_id, text, **kwargs):
            return {"ok": True, "result": {"message_id": 1}}

    crm = CareCrmStore(tmp_path / "care.sqlite3")
    bot = TelegramClientCareBot(
        settings=replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3"),
        bot=FakeBot(),
        planner=None,
        history_store=LeadStore(tmp_path / "history.sqlite3"),
        care_crm=crm,
    )

    result = await bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_720_000_000,
                "chat": {"id": 1001},
                "from": {"id": 2002, "first_name": "Анна"},
                "text": "Пожалуйста, не пишите мне больше",
            },
        }
    )

    linked = crm.find_client_by_link(channel="telegram_client", external_user_id="2002", chat_id="1001")
    assert result["action"] == "do_not_contact"
    assert linked is not None
    assert linked["do_not_contact"] == 1


@pytest.mark.anyio
async def test_telegram_client_risk_message_sets_complaint_risk_and_handoff(tmp_path) -> None:
    class FakeBot:
        def send_message(self, chat_id, text, **kwargs):
            return {"ok": True, "result": {"message_id": 1}}

    class FakeHandoffNotifier:
        def __init__(self) -> None:
            self.handoffs = []

        async def notify(self, handoff):
            self.handoffs.append(handoff)
            return {"sent": True}

    crm = CareCrmStore(tmp_path / "care.sqlite3")
    notifier = FakeHandoffNotifier()
    bot = TelegramClientCareBot(
        settings=replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3"),
        bot=FakeBot(),
        handoff_notifier=notifier,
        planner=None,
        history_store=LeadStore(tmp_path / "history.sqlite3"),
        care_crm=crm,
    )

    result = await bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_720_000_000,
                "chat": {"id": 1001},
                "from": {"id": 2002, "first_name": "Анна"},
                "text": "После процедуры сильный отёк, что делать?",
            },
        }
    )

    linked = crm.find_client_by_link(channel="telegram_client", external_user_id="2002", chat_id="1001")
    assert result["action"] == "complaint_or_risk"
    assert linked is not None
    assert linked["complaint_risk"] == 1
    assert notifier.handoffs
    assert notifier.handoffs[0].reason.value == "complaint_or_risk"


def test_legacy_runtime_status_uses_service_target_not_only_service_name(monkeypatch) -> None:
    class Result:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0

    def fake_run(args, **kwargs):
        if args[:2] == ["systemctl", "show"]:
            service = args[2]
            if service == "yclients-yclients-integration.service":
                return Result(
                    "ActiveState=active\n"
                    "SubState=running\n"
                    "WorkingDirectory=/root/AutomaticCosmetic\n"
                    "ExecStart={ path=/root/AutomaticCosmetic/run_yclients_integration.sh }\n"
                )
            return Result(
                "ActiveState=inactive\n"
                "SubState=dead\n"
                "WorkingDirectory=/root/AutomaticCosmetic/.legacy_runtime/yclients_avito_tg\n"
                "ExecStart={ path=/bin/bash ; argv[]=python -m src.presentation.telegram.client_bot }\n"
            )
        if args[:3] == ["ps", "-eo", "args="]:
            return Result("")
        raise AssertionError(args)

    monkeypatch.setattr(roles_module.subprocess, "run", fake_run)

    status = legacy_runtime_status()

    assert status["active"] is False
    assert status["services"][0]["active"] is False
    assert status["services"][1]["active"] is True
    assert status["services"][1]["legacy_runtime"] is False


@pytest.mark.anyio
async def test_role_profile_filters_tools_and_blocks_upsell_mutations(tmp_path) -> None:
    admin_toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "admin-knowledge.json"),
        enable_workspace_tools=True,
        role_profile=role_profile(CodexRole.ADMIN),
    )
    olga_toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "olga-knowledge.json"),
        enable_workspace_tools=True,
        role_profile=role_profile(CodexRole.OLGA_BOSS),
    )
    upsell_toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "upsell-knowledge.json"),
        role_profile=role_profile(CodexRole.YCLIENTS_UPSELL_STUB),
    )

    assert "workspace.logs.tail" in admin_toolbox.tool_names()
    assert "workspace.command.run" not in admin_toolbox.tool_names()
    assert "workspace.python.run" not in admin_toolbox.tool_names()
    assert "workspace.logs.tail" not in olga_toolbox.tool_names()
    assert "schedule.city.set" in olga_toolbox.tool_names()
    assert "avito.messages.send_phone" in olga_toolbox.tool_names()
    assert "yclients.appointments.create" not in upsell_toolbox.tool_names()

    client_toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "client-knowledge.json"),
        role_profile=role_profile(CodexRole.AVITO_CLIENT),
    )
    assert "avito.messages.send" not in client_toolbox.tool_names()
    assert "avito.messages.send_image" not in client_toolbox.tool_names()

    blocked = await upsell_toolbox.execute(
        "knowledge.create",
        {"title": "draft", "content": "must not mutate from upsell"},
    )
    assert blocked.ok is False
    assert "unknown tool" in blocked.error


def test_role_safety_report_enforces_production_tool_matrix() -> None:
    report = role_safety_report()
    roles = report["roles"]

    assert report["ok"] is True
    assert roles["admin"]["workspace_tools"] == ["workspace.files.list", "workspace.files.read", "workspace.logs.tail"]
    assert roles["admin"]["forbidden_admin_workspace_execution_tools"] == []
    assert roles["olga_boss"]["forbidden_olga_workspace_tools"] == []
    assert roles["olga_boss"]["allow_workspace_tools"] is False
    assert roles["yclients_upsell_stub"]["live_actions_enabled"] is False
    assert roles["yclients_upsell_stub"]["forbidden_upsell_tools"] == []
    for role in ("avito_client", "telegram_client", "vk_client"):
        assert roles[role]["workspace_tools"] == []
        assert roles[role]["forbidden_client_tools"] == []
        assert "avito.messages.send" not in role_profile(CodexRole(role)).allowed_tools
        assert "yclients.appointments.create" not in role_profile(CodexRole(role)).allowed_tools


@pytest.mark.anyio
async def test_telegram_cosmetologist_gets_olga_role_and_separate_context(tmp_path) -> None:
    class FakeBot:
        def send_message(self, chat_id, text, **delivery_params):
            return None

        def send_message_draft(self, chat_id, draft_id, text=None, message_thread_id=None):
            return None

        def send_chat_action(self, chat_id, action="typing", **delivery_params):
            return None

    payloads = []

    async def fake_runner(payload, trace, progress_callback=None):
        payloads.append(payload)
        return {"action": "codex_admin_reply", "ok": True, "reply": "Готово."}

    settings = replace(
        _settings(),
        telegram_admin_user_id=1,
        telegram_cosmetologist_user_id=2,
        telegram_admin_live_drafts_enabled=False,
        telegram_admin_history_db_path=tmp_path / "admin.sqlite3",
    )
    service = CodexTelegramAdminService(
        AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"), enable_workspace_tools=True),
        settings,
        runner=fake_runner,
    )
    transport = TelegramAdminBotTransport(FakeBot(), service, settings, history_store=LeadStore(settings.telegram_admin_history_db_path))

    result = await transport.handle_update(
        {
            "update_id": 5,
            "message": {
                "chat": {"id": "boss-chat"},
                "from": {"id": 2},
                "message_id": 14,
                "message_thread_id": 77,
                "text": "Покажи график",
            },
        }
    )

    assert result["role"] == "olga_boss"
    assert result["history_key"] == "telegram:olga_boss:boss-chat:thread:77"
    assert payloads[0]["role_name"] == "olga_boss"
    assert "workspace.logs.tail" not in payloads[0]["available_tools"]
    assert "schedule.city.set" in payloads[0]["available_tools"]


@pytest.mark.anyio
async def test_automation_toolbox_exposes_avito_read_tools(tmp_path) -> None:
    class FakeAvito:
        async def list_chats(self, account_id, *, limit=20, offset=0):
            assert account_id == 355
            assert limit == 2
            return {"chats": [{"id": "chat-1", "last_message": {"id": "m1", "content": {"text": "Здравствуйте"}}}]}

        async def get_chat_messages(self, account_id, chat_id, *, limit=30, offset=0):
            assert account_id == 355
            assert chat_id == "chat-1"
            return {"messages": [{"id": "m1", "author_id": "client", "created": 123, "content": {"text": "Здравствуйте"}}]}

    class FakeSender:
        async def send_message(self, account_id, chat_id, text):
            return {"sent": False, "reason": "preview_only", "account_id": account_id, "chat_id": chat_id, "text": text}

        async def send_image(self, account_id, chat_id, image_path):
            return {"sent": True, "account_id": account_id, "chat_id": chat_id, "image_path": str(image_path)}

        async def send_file(self, account_id, chat_id, file_path, caption=""):
            return {"sent": False, "reason": "preview_only", "account_id": account_id, "chat_id": chat_id, "file_path": str(file_path), "caption": caption}

    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image")
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        avito=FakeAvito(),
        avito_sender=FakeSender(),
        avito_image_sender=FakeSender(),
        avito_account_id=355,
    )

    assert "avito.chats.list" in toolbox.tool_names()
    assert "avito.messages.send" in toolbox.tool_names()
    assert "avito.messages.send_image" in toolbox.tool_names()
    chats = await toolbox.execute("avito.chats.list", {"limit": 2})
    messages = await toolbox.execute("avito.messages.list", {"chat_id": "chat-1"})
    sent = await toolbox.execute("avito.messages.send", {"chat_id": "chat-1", "text": "Здравствуйте"})
    phone_sent = await toolbox.execute("avito.messages.send_phone", {"chat_id": "chat-1", "phone": "+79991234567"})
    image_sent = await toolbox.execute("avito.messages.send_image", {"chat_id": "chat-1", "image_path": str(image_path)})
    file_sent = await toolbox.execute("avito.messages.send_file", {"chat_id": "chat-1", "file_path": str(image_path), "caption": "Фото до/после"})

    assert chats.ok is True
    assert chats.data["chats"][0]["id"] == "chat-1"
    assert chats.data["chats"][0]["last_message"]["text"] == "Здравствуйте"
    assert messages.ok is True
    assert messages.data["messages"][0]["text"] == "Здравствуйте"
    assert sent.ok is True
    assert sent.data["send_result"]["reason"] == "preview_only"
    assert phone_sent.ok is True
    assert "+79991234567" in phone_sent.data["send_result"]["text"]
    assert image_sent.ok is True
    assert image_sent.data["send_result"]["sent"] is True
    assert file_sent.ok is True
    assert file_sent.data["send_result"]["caption"] == "Фото до/после"


@pytest.mark.anyio
async def test_automation_toolbox_exposes_yclients_crud_and_knowledge_crud(tmp_path) -> None:
    gateway = DryRunYClientsGateway(clients=[ClientProfile(name="Анна", phone="+79990000000", external_id="5")])
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / "knowledge.json"))
    slot = gateway.slots[0]

    assert "yclients.appointments.create" in toolbox.tool_names()
    assert "yclients.appointments.list" in toolbox.tool_names()
    assert "care.tasks.plan" in toolbox.tool_names()
    assert "knowledge.update" in toolbox.tool_names()

    services = await toolbox.execute("yclients.services.list", {"city": slot.city})
    service = services.data["services"][0]

    created = await toolbox.execute(
        "yclients.appointments.create",
        {
            "client_name": "Анна",
            "phone": "+79990000000",
            "city": slot.city,
            "service_id": service["id"],
            "service_title": service["title"],
            "service_price": service["price"],
            "duration_minutes": service["duration_minutes"],
            "datetime": slot.starts_at.isoformat(),
            "notes": "Создано инструментом",
        },
    )

    assert created.ok is True
    assert created.data["appointment_id"] == 1

    moved = await toolbox.execute(
        "yclients.appointments.move",
        {"appointment_id": 1, "datetime": "2026-06-02T12:00:00", "city": slot.city},
    )

    assert moved.ok is True
    assert gateway.appointments[0].starts_at == datetime(2026, 6, 2, 12, 0)

    note = await toolbox.execute(
        "knowledge.create",
        {
            "kind": "faq",
            "title": "Цена ботокса",
            "content": "Цена зависит от зоны и количества единиц.",
            "tags": ["avito", "price"],
        },
    )
    item_id = note.data["item"]["id"]

    listed = await toolbox.execute("knowledge.list", {"query": "ботокс"})
    updated = await toolbox.execute("knowledge.update", {"id": item_id, "content": "Расчет по зонам и единицам."})
    deleted = await toolbox.execute("knowledge.delete", {"id": item_id})

    assert listed.ok is True
    assert listed.data["items"][0]["id"] == item_id
    assert updated.data["item"]["content"] == "Расчет по зонам и единицам."
    assert deleted.ok is True


@pytest.mark.anyio
async def test_client_role_knowledge_list_filters_internal_avito_examples(tmp_path) -> None:
    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    knowledge.create(
        kind="avito_conversation_example",
        title="Avito webhook debug",
        content="handoff contexts, webhook secret123, клиенту отправлено: технический ответ",
        tags=("avito", "internal"),
    )
    safe = knowledge.create(
        kind="faq",
        title="Ботокс",
        content="Ботокс рассчитывается по зонам и единицам.",
        tags=("avito", "ботокс"),
    )
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        knowledge,
        role_profile=roles_module.role_profile(roles_module.CodexRole.AVITO_CLIENT),
    )

    result = await toolbox.execute("knowledge.list", {"query": "ботокс"})

    assert result.ok is True
    assert [item["id"] for item in result.data["items"]] == [safe.id]
    assert "secret123" not in json.dumps(result.data, ensure_ascii=False)


@pytest.mark.anyio
async def test_yclients_client_search_marks_same_name_as_ambiguous(tmp_path) -> None:
    gateway = DryRunYClientsGateway(
        clients=[
            ClientProfile(name="Анна", phone="+79990000001", external_id="5", city="Москва"),
            ClientProfile(name="Анна", phone="+79990000002", external_id="6", city="Москва"),
        ]
    )
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / "knowledge.json"))

    result = await toolbox.execute("yclients.clients.search", {"query": "Анна", "city": "Москва"})

    assert result.ok is True
    assert result.data["ambiguous"] is True
    assert result.data["duplicate_names"] == ["Анна"]
    assert {item["external_id"] for item in result.data["clients"]} == {"5", "6"}


@pytest.mark.anyio
async def test_yclients_cancel_returns_client_message_and_notifies_olga(tmp_path) -> None:
    class Notifier:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def notify_text(self, text: str) -> dict:
            self.messages.append(text)
            return {"sent": True}

    gateway = DryRunYClientsGateway()
    appointment = Appointment(
        id=41,
        client=ClientProfile(name="Анна", phone="+79990000000", external_id="5"),
        service=Service(id=7, title="Чистка лица", price=3500, duration_minutes=90),
        city="Москва",
        starts_at=datetime(2026, 6, 2, 12, 0),
    )
    gateway.appointments.append(appointment)
    notifier = Notifier()
    toolbox = AutomationToolbox(
        gateway,
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        operations_notifier=notifier,  # type: ignore[arg-type]
    )

    result = await toolbox.execute("yclients.appointments.cancel", {"appointment_id": 41, "city": "Москва"})

    assert result.ok is True
    assert result.data["client_message"] == "Запись: Чистка лица, Москва, 02.06.2026 в 12:00 отменена."
    assert "Клиент: Анна" in notifier.messages[0]
    assert "Телефон: +79990000000" in notifier.messages[0]
    assert gateway.appointments == []


@pytest.mark.anyio
async def test_automation_toolbox_lists_appointments_and_plans_care_tasks(tmp_path) -> None:
    gateway = DryRunYClientsGateway()
    await gateway.create_appointment(
        Appointment(
            client=ClientProfile(name="Анна", skin_type="сухая"),
            service=Service(id=1, title="Чистка лица", price=3500, duration_minutes=90),
            city="Ростов-на-Дону",
            starts_at=datetime(2026, 5, 25, 12, 0),
        )
    )
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / "knowledge.json"))

    listed = await toolbox.execute("yclients.appointments.list", {"date": "2026-05-25"})
    planned = await toolbox.execute(
        "care.tasks.plan",
        {
            "start_date": "2026-05-25",
            "end_date": "2026-05-25",
            "now": "2026-05-27T12:00:00",
            "rules": [
                {
                    "source_service": "Чистка",
                    "delay_days": 2,
                    "recommendation": "Мягкий крем и SPF утром.",
                    "product_hint": "крем",
                    "requires_skin_type": True,
                }
            ],
        },
    )

    assert listed.ok is True
    assert listed.data["appointments"][0]["client"]["name"] == "Анна"
    assert planned.ok is True
    assert planned.data["tasks"][0]["kind"] == "product_recommendation"
    assert "сухая" in planned.data["tasks"][0]["message"]


@pytest.mark.anyio
async def test_avito_consultant_answers_listing_price_without_handoff(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    consultant = AvitoConsultant(toolbox)
    message = avito_inbound_message(
        {
            "payload": {
                "type": "message_created",
                "value": {
                    "id": "price-1",
                    "chat_id": "chat-price",
                    "content": {
                        "text": "Фото и скидка? И какая цена?",
                        "item": {
                            "id": 10,
                            "title": "Увеличение ягодиц",
                            "price_string": "от 18 000 ₽",
                            "city": "Ростов-на-Дону",
                        },
                    },
                },
            }
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "listing_price_answer"
    assert reply.handoff is None
    assert "от 18 000 ₽" in reply.reply
    assert "передам" not in reply.reply.lower()


@pytest.mark.anyio
async def test_avito_consultant_does_not_turn_phone_and_week_into_personal_meeting(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    consultant = AvitoConsultant(toolbox)
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-no-procedure",
            "content": {
                "text": "На следующий неделю, 950-025-01-15 имя Ганжина..",
                "item": {
                    "id": 10,
                    "title": "Косметолог Санкт-Петербург",
                    "city": "Санкт-Петербург",
                },
            },
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "ask_procedure_for_booking"
    assert reply.handoff is None
    assert "какая процедура" in reply.reply.lower()
    assert "встреч" not in reply.reply.lower()
    assert "приём" not in reply.reply.lower()


@pytest.mark.anyio
async def test_avito_preflight_blocks_ambiguous_booking_before_planner(tmp_path) -> None:
    class ExplodingPlanner:
        async def respond(self, context, toolbox):
            raise AssertionError("ambiguous booking without explicit service must not reach planner")

    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    consultant = AvitoConsultant(toolbox, planner=ExplodingPlanner())
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-no-procedure-planner",
            "content": {
                "text": "На следующую неделю, 950-025-01-15 имя Ганжина",
                "item": {
                    "id": 10,
                    "title": "Модель на Увеличение груди Увеличение ягодиц Акция",
                    "city": "Санкт-Петербург",
                },
            },
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "ask_procedure_for_booking"
    assert reply.handoff is None
    assert "какая процедура" in reply.reply.lower()


@pytest.mark.anyio
async def test_avito_preflight_uses_history_service_before_booking(tmp_path) -> None:
    class Planner:
        def __init__(self) -> None:
            self.called = False

        async def respond(self, context, toolbox):
            self.called = True
            return AvitoConsultantReply(action="planned", reply="Планирую по истории.")

    planner = Planner()
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    consultant = AvitoConsultant(toolbox, planner=planner)
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-history-procedure",
            "content": {"text": "на следующую неделю, телефон 950-025-01-15"},
        }
    )

    reply = await consultant.respond(message, conversation_history=[{"role": "user", "content": "Интересует увеличение губ"}])

    assert planner.called is False
    assert reply.action in {"ask_city", "slots", "booking_options", "booking_handoff"}


@pytest.mark.anyio
async def test_avito_consultant_uses_knowledge_for_medical_questions_instead_of_empty_handoff(tmp_path) -> None:
    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    knowledge.create(
        kind="faq",
        title="Беременность и филлеры",
        content="Во время беременности и грудного вскармливания инъекционные процедуры не проводим.",
        tags=("беременность", "противопоказания", "эффект"),
    )
    toolbox = AutomationToolbox(DryRunYClientsGateway(), knowledge)
    consultant = AvitoConsultant(toolbox)
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-med",
            "content": {"text": "А если не беременна, но забеременею, как повлияет?"},
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "handoff"
    assert reply.handoff is not None
    assert reply.handoff.reason == HandoffReason.COMPLAINT_OR_RISK


@pytest.mark.anyio
async def test_avito_consultant_answers_amount_calculation_from_knowledge(tmp_path) -> None:
    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    knowledge.create(
        kind="service_note",
        title="Расчет объема для ямок",
        content="Объем считается индивидуально по выраженности ямок; обычно сначала уточняем фото и желаемый результат.",
        tags=("расчет", "мл", "ямок"),
    )
    toolbox = AutomationToolbox(DryRunYClientsGateway(), knowledge)
    consultant = AvitoConsultant(toolbox)
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-amount",
            "content": {
                "text": "За сколько мл?",
                "item": {
                    "id": 10,
                    "title": "Увеличение ягодиц",
                    "price_string": "от 18 000 ₽",
                    "city": "Ростов-на-Дону",
                },
            },
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "knowledge_answer"
    assert "индивидуально" in reply.reply
    assert "передам" not in reply.reply.lower()


@pytest.mark.anyio
async def test_avito_consultant_answers_address_only_from_yclients(tmp_path) -> None:
    class AddressGateway(DryRunYClientsGateway):
        async def get_company_address(self, city: str = ""):
            assert city == "Москва"
            return {"city": "Москва", "address": "Москва, Малый Гнездниковский переулок, 12", "source": "yclients.company"}

    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    knowledge.create(
        kind="location_policy",
        title="Старый адрес из памяти",
        content="м. Тверская, не использовать",
        tags=("mentor", "address"),
    )
    toolbox = AutomationToolbox(AddressGateway(), knowledge)
    consultant = AvitoConsultant(toolbox)
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-address",
            "content": {
                "text": "Подскажите адрес в Москве",
                "item": {"title": "Губы", "city": "Москва"},
            },
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "yclients_address_answer"
    assert "Малый Гнездниковский" in reply.reply
    assert "Тверская" not in reply.reply
    assert reply.metadata["tool"] == "yclients.company.address"


@pytest.mark.anyio
async def test_avito_consultant_can_delegate_decision_to_codex_planner(tmp_path) -> None:
    seen_payload = {}

    async def fake_codex_runner(payload, toolbox):
        nonlocal seen_payload
        seen_payload = payload
        result = await toolbox.execute("knowledge.list", {"query": "ботокс"})
        assert result.ok is True
        return {"action": "codex_reply", "reply": "По ботоксу подскажу по зонам. В каком городе удобно?"}

    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    knowledge.create(kind="faq", title="Ботокс", content="Цена зависит от зоны.", tags=("ботокс",))
    toolbox = AutomationToolbox(DryRunYClientsGateway(), knowledge)
    consultant = AvitoConsultant(toolbox, planner=CodexAvitoPlanner(fake_codex_runner))
    message = avito_inbound_message({"type": "message", "chat_id": "chat-codex", "content": {"text": "Интересует акция ботокс"}})

    reply = await consultant.respond(message)

    assert reply.action == "codex_reply"
    assert reply.metadata["planner"] == "codex"
    assert "yclients.services.list" in seen_payload["available_tools"]
    assert "knowledge.create" not in seen_payload["available_tools"]
    assert all(tool["name"] != "knowledge.create" for tool in seen_payload["tool_schemas"])
    assert seen_payload["role_name"] == "avito_client"
    assert seen_payload["conversation_key"] == "avito:client:chat-codex"
    assert "Самостоятельно помочь клиенту Avito" in seen_payload["goal"]
    assert "candidate_tools" not in seen_payload


@pytest.mark.anyio
async def test_avito_consultant_payload_uses_client_history_and_sanitizes_stale_raw_last_message(tmp_path) -> None:
    seen_payload = {}

    async def fake_codex_runner(payload, toolbox):
        del toolbox
        nonlocal seen_payload
        seen_payload = payload
        return {"action": "codex_reply", "reply": "По истории вижу, что речь про 30 мая."}

    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    consultant = AvitoConsultant(toolbox, planner=CodexAvitoPlanner(fake_codex_runner))
    message = avito_inbound_message(
        {
            "payload": {
                "type": "message_created",
                "value": {
                    "id": "m-current",
                    "user_id": 123,
                    "author_id": 456,
                    "chat_id": "chat-history",
                    "created": 1780049409,
                    "content": {"text": "да"},
                    "chat": {
                        "id": "chat-history",
                        "last_message": {
                            "id": "m-stale",
                            "author_id": 456,
                            "created": 1780049759,
                            "direction": "in",
                            "content": {"text": "можно на 30 июня"},
                        },
                    },
                },
            }
        }
    )

    await consultant.respond(message, conversation_history=[{"role": "user", "content": "Можно на 30 мая?", "created_at": "x"}])

    assert seen_payload["conversation_history"][0]["content"] == "Можно на 30 мая?"
    assert seen_payload["current_date"] == "2026-05-29"
    assert seen_payload["message"]["text"] == "да"
    assert "raw" not in seen_payload["message"]["metadata"]
    assert "chat_last_message" not in seen_payload["message"]["metadata"]


@pytest.mark.anyio
async def test_avito_consultant_payload_exposes_own_message_actor_metadata(tmp_path) -> None:
    seen_payload = {}

    async def fake_codex_runner(payload, toolbox):
        del toolbox
        nonlocal seen_payload
        seen_payload = payload
        return {"action": "codex_reply", "reply": "ignored"}

    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    consultant = AvitoConsultant(toolbox, planner=CodexAvitoPlanner(fake_codex_runner))
    message = annotate_avito_message_actor(
        avito_inbound_message(
            {
                "payload": {
                    "type": "message_created",
                    "value": {
                        "id": "own-2",
                        "author_id": 1,
                        "chat_id": "chat-own-payload",
                        "direction": "out",
                        "type": "text",
                        "content": {"text": "Наш исходящий ответ"},
                    },
                }
            }
        ),
        _settings(),
    )

    await consultant.respond(message)

    metadata = seen_payload["message"]["metadata"]
    assert metadata["direction"] == "out"
    assert metadata["is_own_account"] is True
    assert metadata["author_role"] == "own_account"


@pytest.mark.anyio
async def test_client_codex_payload_is_compact_and_readonly(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    consultant = AvitoConsultant(toolbox)
    message = avito_inbound_message(
        {
            "type": "message",
            "message_id": "m-compact",
            "chat_id": "chat-compact",
            "content": {"text": "Здравствуйте, сколько стоит увеличение ягодиц и когда можно?"},
        }
    )

    context = await consultant.build_context(message)
    payload = context.to_codex_payload()
    prompt = build_codex_planner_prompt(payload, [])

    assert len(prompt) < 18_000
    assert len(payload["reply_rules"]) <= 10
    assert "yclients.appointments.create" not in payload["available_tools"]
    assert "expert_rag.plan_change" not in payload["available_tools"]


@pytest.mark.anyio
async def test_avito_consultant_filters_internal_handoff_knowledge_from_codex_payload(tmp_path) -> None:
    seen_payload = {}

    async def fake_codex_runner(payload, toolbox):
        del toolbox
        nonlocal seen_payload
        seen_payload = payload
        return {"action": "codex_reply", "reply": "Чистка лица стоит 3500 ₽."}

    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    knowledge.create(
        kind="avito_conversation_example",
        title="Ежедневный quality digest Handoff",
        content="Нужен ответ по Avito\nHandoff: 17\nКлиенту отправлено: Да, передам косметологу.",
        tags=("avito", "history", "bad_example"),
    )
    knowledge.create(kind="faq", title="Чистка лица", content="Чистка лица стоит 3500 ₽.", tags=("чистка",))
    toolbox = AutomationToolbox(DryRunYClientsGateway(), knowledge)
    consultant = AvitoConsultant(toolbox, planner=CodexAvitoPlanner(fake_codex_runner))
    message = avito_inbound_message({"type": "message", "chat_id": "chat-clean", "content": {"text": "Сколько чистка лица?"}})

    await consultant.respond(message)

    payload_text = str(seen_payload["knowledge_items"]).casefold()
    assert "чистка лица стоит" in payload_text
    assert "handoff" not in payload_text
    assert "нужен ответ по avito" not in payload_text


@pytest.mark.anyio
async def test_avito_consultant_filters_unverified_imported_price_tables(tmp_path) -> None:
    seen_payload = {}

    async def fake_codex_runner(payload, toolbox):
        del toolbox
        nonlocal seen_payload
        seen_payload = payload
        return {"action": "codex_reply", "reply": "Стоимость по телу уточню у Ольги."}

    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    knowledge.create(
        kind="avito_conversation_example",
        title="Как будто цены в этом прайсе чуть завышены",
        content="ну вот цены в боте\nКонтурная пластика тела: 120 мл — 30000 ₽, 180 мл — 45000 ₽\nКак будто цены в этом прайсе чуть завышены",
        tags=("avito", "history", "price"),
    )
    knowledge.create(kind="faq", title="Тело", content="Стоимость по телу рассчитывает Ольга после уточнения зоны.", tags=("тело", "грудь"))
    toolbox = AutomationToolbox(DryRunYClientsGateway(), knowledge)
    consultant = AvitoConsultant(toolbox, planner=CodexAvitoPlanner(fake_codex_runner))
    message = avito_inbound_message({"type": "message", "chat_id": "chat-body-price", "content": {"text": "Сколько стоит грудь?"}})

    await consultant.respond(message)

    payload_text = str(seen_payload["knowledge_items"]).casefold()
    assert "120 мл" not in payload_text
    assert "30000" not in payload_text
    assert "рассчитывает ольга" in payload_text


@pytest.mark.anyio
async def test_avito_consultant_routes_photo_handoff_before_codex() -> None:
    async def fake_codex_loop(payload, trace):
        assert payload["message"]["has_photo"] is True
        assert trace == []
        return {
            "action": "handoff",
            "handoff_reason": "photo_consultation",
            "handoff_summary": "Codex решил передать фото косметологу.",
            "reply": "Спасибо, фото передам Ольге для индивидуальной консультации.",
        }

    consultant = AvitoConsultant(
        AutomationToolbox(DryRunYClientsGateway()),
        planner=CodexToolLoopPlanner(fake_codex_loop),
    )
    message = avito_inbound_message(
        {"type": "message", "chat_id": "photo-codex", "content": {"text": "Посмотрите фото", "photo": {"id": "p1"}}}
    )

    reply = await consultant.respond(message)

    assert reply.metadata["planner"] == "client_router"
    assert reply.action == "handoff"
    assert reply.handoff is not None
    assert reply.handoff.reason == HandoffReason.PHOTO_CONSULTATION
    assert "Клиенту уже ответили" in reply.handoff.summary


@pytest.mark.anyio
async def test_codex_tool_loop_allows_silent_handoff_for_before_after_assets() -> None:
    async def fake_codex_loop(payload, trace):
        assert trace == []
        assert "фото до/после" in str(payload).casefold()
        return {
            "action": "handoff",
            "handoff_reason": "missing_data",
            "handoff_summary": "Клиенту пока ничего не писали. Нужно у Ольги: фото до/после на 300 мл или решение, что фото не будет.",
            "reply": "",
        }

    consultant = AvitoConsultant(
        AutomationToolbox(DryRunYClientsGateway()),
        planner=CodexToolLoopPlanner(fake_codex_loop),
    )
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-volume",
            "content": {"text": "За 50 тысяч 300 мл хватит? Можно фото до/после?"},
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "handoff"
    assert reply.reply == "По объёму и ожидаемому результату лучше не обещать вслепую. Передам Ольге, она посмотрит и сориентирует точнее."
    assert reply.handoff is not None
    assert reply.handoff.reason == HandoffReason.EXPERT_EXPECTATION
    assert "Клиенту уже ответили" in reply.handoff.summary
    assert "Нужно у Ольги" not in reply.handoff.summary
    assert "нельзя автообещать результат по мл" in reply.handoff.summary


@pytest.mark.anyio
async def test_codex_tool_loop_planner_executes_multiple_tools_before_final_reply(tmp_path) -> None:
    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    gateway = DryRunYClientsGateway(services=[Service(id=7, title="Ботокс", price=3000, duration_minutes=30)])
    toolbox = AutomationToolbox(gateway, knowledge)
    steps = []

    async def fake_codex_loop(payload, trace):
        steps.append({"payload": payload, "trace": list(trace)})
        if len(trace) == 0:
            return {"tool_calls": [{"name": "yclients.services.list", "arguments": {"city": "Ростов-на-Дону"}}]}
        if len([event for event in trace if event["type"] == "tool_result"]) == 1:
            return {
                "tool_calls": [
                    {
                        "name": "knowledge.create",
                        "arguments": {
                            "kind": "conversation_lesson",
                            "title": "Ботокс цена Авито",
                            "content": "На вопрос про акцию ботокса отвечать по зонам и не отправлять к косметологу.",
                            "tags": ["avito", "ботокс"],
                        },
                    }
                ]
            }
        return {"action": "codex_reply", "reply": "Ботокс сейчас от 3000 ₽, зависит от зоны. В каком городе удобно?"}

    consultant = AvitoConsultant(toolbox, planner=CodexToolLoopPlanner(fake_codex_loop, max_steps=4))
    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-loop",
            "content": {"text": "Здравствуйте, интересует акция ботокс"},
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "codex_reply"
    assert reply.metadata["planner"] == "codex_tool_loop"
    assert reply.metadata["trace"][0]["type"] == "tool_call"
    assert reply.metadata["trace"][0]["tool"] == "yclients.services.list"
    assert reply.metadata["trace"][1]["type"] == "tool_result"
    assert reply.metadata["trace"][2]["tool"] == "knowledge.create"
    assert reply.metadata["trace"][3]["ok"] is False
    assert "not allowed" in reply.metadata["trace"][3]["error"]
    assert not knowledge.list(query="ботокс")
    assert len(steps) == 3


@pytest.mark.anyio
async def test_codex_tool_loop_zero_max_steps_disables_step_cap(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    calls = 0

    async def fake_codex_loop(payload, trace):
        nonlocal calls
        calls += 1
        if calls <= 7:
            return {"tool_calls": [{"name": "yclients.services.list", "arguments": {}}]}
        return {"action": "codex_reply", "reply": "Готово после расширенной проверки."}

    consultant = AvitoConsultant(toolbox, planner=CodexToolLoopPlanner(fake_codex_loop, max_steps=0))
    message = avito_inbound_message({"type": "message", "chat_id": "chat-unlimited", "content": {"text": "Расскажите про уход после ботокса"}})

    reply = await consultant.respond(message)

    assert reply.action == "codex_reply"
    assert reply.reply == "Готово после расширенной проверки."
    assert calls == 8


@pytest.mark.anyio
async def test_codex_tool_loop_default_step_cap_handoffs_safely(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    calls = 0

    async def fake_codex_loop(payload, trace):
        nonlocal calls
        calls += 1
        if calls <= 7:
            return {"tool_calls": [{"name": "yclients.services.list", "arguments": {}}]}
        return {"action": "codex_reply", "reply": "Готово без явного лимита."}

    consultant = AvitoConsultant(toolbox, planner=CodexToolLoopPlanner(fake_codex_loop, max_steps=4))
    message = avito_inbound_message({"type": "message", "chat_id": "chat-default-unlimited", "content": {"text": "Расскажите про уход после ботокса"}})

    reply = await consultant.respond(message)

    assert reply.action == "handoff"
    assert "уточню" in reply.reply.casefold()
    assert reply.handoff is not None
    assert "достиг лимита шагов" in reply.handoff.summary
    assert calls == 4


def test_integration_settings_default_avito_codex_max_steps_is_four(monkeypatch) -> None:
    monkeypatch.delenv("AVITO_CODEX_MAX_STEPS", raising=False)

    settings = IntegrationSettings.from_env()

    assert settings.avito_codex_max_steps == 4


@pytest.mark.anyio
async def test_automation_toolbox_exposes_strict_tool_schemas_and_validates_calls(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))

    schemas = toolbox.tool_schemas()
    create_appointment = next(tool for tool in schemas if tool["name"] == "yclients.appointments.create")

    assert create_appointment["mutates"] is True
    assert create_appointment["external"] is True
    assert "phone" in create_appointment["required"]
    assert "Live YCLIENTS mutations" in create_appointment["guardrail"]
    assert all(not tool["name"].startswith("workspace.") for tool in schemas)

    missing = await toolbox.execute("yclients.appointments.create", {"city": "Ростов-на-Дону"})
    unknown = await toolbox.execute("missing.tool", {})

    assert missing.ok is False
    assert "missing required arguments" in missing.error
    assert unknown.ok is False
    assert "unknown tool" in unknown.error


@pytest.mark.anyio
async def test_automation_toolbox_rejects_create_when_service_id_conflicts_with_notes(tmp_path) -> None:
    gateway = DryRunYClientsGateway(
        services=[
            Service(id=27204198, title="Кисетные морщины", price=3500, duration_minutes=60),
            Service(id=27204117, title="Увеличение губ - Корея", price=8000, duration_minutes=60),
        ]
    )
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / "knowledge.json"))

    result = await toolbox.execute(
        "yclients.appointments.create",
        {
            "city": "Ростов-на-Дону",
            "service_id": 27204198,
            "datetime": gateway.slots[0].starts_at.isoformat(),
            "phone": "+79885801199",
            "client_name": "Zara",
            "notes": "Губы Корея",
        },
    )

    assert result.ok is False
    assert "Refusing to create appointment" in result.error
    assert "увеличение губ" in result.error
    assert gateway.appointments == []


@pytest.mark.anyio
async def test_automation_toolbox_workspace_diagnostics_are_read_only(tmp_path) -> None:
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        enable_workspace_tools=True,
    )

    workspace_read = next(tool for tool in toolbox.tool_schemas() if tool["name"] == "workspace.files.read")

    listed = await toolbox.execute("workspace.files.list", {"path": "src/freelance_leads_bot/integrations", "pattern": "agent_tools.py"})
    read = await toolbox.execute("workspace.files.read", {"path": "src/freelance_leads_bot/integrations/agent_tools.py", "max_chars": 9000})
    command = await toolbox.execute("workspace.command.run", {"command": "pwd"})
    python = await toolbox.execute("workspace.python.run", {"code": "print(2 + 2)"})
    secret_file = await toolbox.execute("workspace.files.read", {"path": ".env"})
    secret_command = await toolbox.execute("workspace.command.run", {"command": "cat .env"})
    dangerous_python = await toolbox.execute("workspace.python.run", {"code": "open('.env').read()"})

    assert workspace_read["mutates"] is False
    assert "sensitive paths" in workspace_read["guardrail"]
    assert listed.ok is True
    assert listed.data["files"][0]["path"].endswith("agent_tools.py")
    assert read.ok is True
    assert "class AutomationToolbox" in read.data["content"]
    assert command.ok is True
    assert command.data["stdout"].strip().endswith("AutomaticCosmetic")
    assert python.ok is True
    assert python.data["stdout"].strip() == "4"
    assert secret_file.ok is False
    assert "sensitive" in secret_file.error
    assert secret_command.ok is False
    assert "sensitive" in secret_command.error
    assert dangerous_python.ok is False
    assert "blocked" in dangerous_python.error


@pytest.mark.anyio
async def test_automation_toolbox_city_schedule_blocks_wrong_city_slots(tmp_path) -> None:
    schedule = CityScheduleStore(tmp_path / "city_schedule.json")
    gateway = DryRunYClientsGateway()
    toolbox = AutomationToolbox(
        gateway,
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        city_schedule=schedule,
    )
    slot = gateway.slots[0]
    schedule_date = slot.starts_at.date().isoformat()

    updated = await toolbox.execute("schedule.city.set", {"city": "Москва", "dates": [schedule_date]})
    wrong_city = await toolbox.execute(
        "yclients.slots.list",
        {"city": "Краснодар", "service_id": slot.service_id, "date": schedule_date},
    )
    right_city = await toolbox.execute(
        "yclients.slots.list",
        {"city": "Москва", "service_id": slot.service_id, "date": schedule_date},
    )
    listed = await toolbox.execute("schedule.city.list", {"from_date": schedule_date})

    assert updated.ok is True
    assert wrong_city.ok is True
    assert wrong_city.data["slots"] == []
    assert wrong_city.data["blocked_by_city_schedule"] is True
    assert wrong_city.data["schedule_city"] == "Москва"
    assert right_city.ok is True
    assert right_city.data["slots"]
    assert listed.data["schedule"] == [{"date": schedule_date, "city": "Москва"}]
    assert listed.data["yclients_audit"] == [
        {"date": schedule_date, "city": "Москва", "yclients_status": "synced"}
    ]


@pytest.mark.anyio
async def test_city_schedule_is_not_written_when_yclients_sync_fails(tmp_path) -> None:
    class FailingGateway(DryRunYClientsGateway):
        async def set_staff_schedule(self, city, dates, slots=None):
            raise RuntimeError("YCLIENTS unavailable")

    schedule = CityScheduleStore(tmp_path / "city_schedule.json")
    toolbox = AutomationToolbox(
        FailingGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        city_schedule=schedule,
    )

    result = await toolbox.execute(
        "schedule.city.set",
        {"city": "Краснодар", "dates": ["2026-06-24"]},
    )

    assert result.ok is False
    assert "YCLIENTS unavailable" in result.error
    assert schedule.get_city("2026-06-24") == ""


@pytest.mark.anyio
async def test_city_schedule_change_removes_previous_city_from_yclients(tmp_path) -> None:
    schedule = CityScheduleStore(tmp_path / "city_schedule.json")
    gateway = DryRunYClientsGateway()
    schedule.set_dates("Краснодар", ["2026-06-24"])
    await gateway.set_staff_schedule("Краснодар", ["2026-06-24"])
    toolbox = AutomationToolbox(
        gateway,
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        city_schedule=schedule,
    )

    result = await toolbox.execute(
        "schedule.city.set",
        {
            "city": "Геленджик",
            "dates": ["2026-06-24"],
            "from_time": "12:00",
            "to_time": "19:00",
        },
    )

    assert result.ok is True
    assert schedule.get_city("2026-06-24") == "Геленджик"
    assert await gateway.get_staff_schedule("Краснодар", "2026-06-24", "2026-06-24") == []
    assert await gateway.get_staff_schedule("Геленджик", "2026-06-24", "2026-06-24") == [
        {
            "staff_id": 1,
            "date": "2026-06-24",
            "slots": [{"from": "12:00", "to": "19:00"}],
        }
    ]


@pytest.mark.anyio
async def test_automation_toolbox_lists_slots_without_service_id_once(tmp_path) -> None:
    class RecordingGateway(DryRunYClientsGateway):
        def __init__(self) -> None:
            super().__init__()
            self.free_slot_calls: list[tuple[str, int, str]] = []

        async def get_free_slots(self, city: str, service_id: int, date: str):
            self.free_slot_calls.append((city, service_id, date))
            return await super().get_free_slots(city, service_id, date)

    schedule = CityScheduleStore(tmp_path / "city_schedule.json")
    gateway = RecordingGateway()
    toolbox = AutomationToolbox(
        gateway,
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        city_schedule=schedule,
    )
    slot = gateway.slots[0]
    schedule_date = slot.starts_at.date().isoformat()
    schedule.set_dates(slot.city, [schedule_date])

    result = await toolbox.execute("yclients.slots.list", {"city": slot.city, "date": schedule_date})
    schema = next(spec for spec in toolbox.tool_schemas() if spec["name"] == "yclients.slots.list")

    assert result.ok is True
    assert result.data["slots"]
    assert gateway.free_slot_calls == [(slot.city, 0, schedule_date)]
    assert schema["required"] == ["city", "date"]


def test_city_schedule_normalizes_gelendzhik_and_rostov(tmp_path) -> None:
    schedule = CityScheduleStore(tmp_path / "city_schedule.json")

    assert schedule.normalize_city("Геленджике") == "Геленджик"
    assert schedule.normalize_city("гелик") == "Геленджик"
    assert schedule.normalize_city("Ростове-на-Дону") == "Ростов-на-Дону"


def test_city_schedule_matches_multi_city_dates(tmp_path) -> None:
    schedule = CityScheduleStore(tmp_path / "city_schedule.json")

    result = schedule.set_dates("Краснодар, Геленджик", ["2026-06-08"])

    assert result["city"] == "Краснодар, Геленджик"
    assert schedule.city_matches(schedule.get_city("2026-06-08"), "Краснодар") is True
    assert schedule.city_matches(schedule.get_city("2026-06-08"), "Геленджике") is True
    assert schedule.city_matches(schedule.get_city("2026-06-08"), "Москва") is False
    assert schedule.list(from_date="2026-06-08", city="Геленджик") == [
        {"date": "2026-06-08", "city": "Краснодар, Геленджик"}
    ]


@pytest.mark.anyio
async def test_automation_toolbox_marks_unknown_schedule_before_slot_lookup(tmp_path) -> None:
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        city_schedule=CityScheduleStore(tmp_path / "city_schedule.json"),
    )

    result = await toolbox.execute("yclients.slots.list", {"city": "Москва", "service_id": 1, "date": "2026-06-30"})

    assert result.ok is True
    assert result.data["slots"] == []
    assert result.data["schedule_status"] == "unknown"
    assert result.data["schedule_missing"] is True
    assert result.data["can_state_no_slots"] is False
    assert result.data["handoff_recommended"] is True


@pytest.mark.anyio
async def test_yclients_service_placeholder_prices_are_marked_for_codex(tmp_path) -> None:
    gateway = DryRunYClientsGateway(services=[Service(id=7, title="Увеличение губ", price=1, duration_minutes=60)])
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / "knowledge.json"))

    result = await toolbox.execute("yclients.services.list", {"city": "Москва"})

    service = result.data["services"][0]
    assert service["price"] == 1
    assert service["price_status"] == "placeholder"
    assert "не называй" in service["client_price_hint"]


@pytest.mark.anyio
async def test_telegram_admin_codex_service_executes_tools_before_reply(tmp_path) -> None:
    gateway = DryRunYClientsGateway(services=[Service(id=7, title="Чистка лица", price=3500, duration_minutes=60)])
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / "knowledge.json"))
    settings = _settings()
    calls = []

    async def fake_runner(payload, trace, progress_callback=None):
        calls.append({"payload": payload, "trace": list(trace)})
        if not trace:
            return {
                "tool_calls": [
                    {
                        "name": "yclients.appointments.create",
                        "arguments": {
                            "city": "Ростов-на-Дону",
                            "service_id": 7,
                            "datetime": gateway.slots[0].starts_at.isoformat(),
                            "phone": "+79991234567",
                            "client_name": "Анна",
                        },
                    }
                ]
            }
        return {"action": "codex_admin_reply", "ok": True, "reply": "Готово, запись создана.", "appointment_id": 1}

    service = CodexTelegramAdminService(toolbox, settings, runner=fake_runner)

    result = await service.handle_text("Запиши Анну на чистку")

    assert result.ok is True
    assert result.appointment_id == 1
    assert result.message == "Готово, запись создана."
    assert calls[0]["payload"]["role"] == "telegram_admin"
    assert calls[0]["payload"]["role_name"] == "admin"
    assert calls[1]["trace"][0]["tool"] == "yclients.appointments.create"


@pytest.mark.anyio
async def test_telegram_admin_runner_returns_plain_text_fallback(monkeypatch) -> None:
    def fake_chat_with_codex(message, history=None, timeout_seconds=300, progress_callback=None, raw_prompt=False):
        return "Сделал. Нужно перезапустить процесс.", Path("data/codex_chat/fake.txt")

    monkeypatch.setattr(admin_codex_module, "chat_with_codex", fake_chat_with_codex)

    step = await CodexTelegramAdminRunner(timeout_seconds=1800)(
        {"message": {"text": "Сделай"}, "available_tools": [], "tool_schemas": []},
        [],
    )

    assert step == {
        "action": "codex_admin_reply",
        "ok": True,
        "reply": "Сделал. Нужно перезапустить процесс.",
    }


@pytest.mark.anyio
async def test_telegram_admin_runner_marks_cli_timeout_fallback_not_ok(monkeypatch) -> None:
    def fake_chat_with_codex(message, history=None, timeout_seconds=300, progress_callback=None, raw_prompt=False):
        return "Codex не успел ответить за отведенное время.", Path("data/codex_chat/fake.txt")

    monkeypatch.setattr(admin_codex_module, "chat_with_codex", fake_chat_with_codex)

    step = await CodexTelegramAdminRunner(timeout_seconds=1800)(
        {"message": {"text": "Долгая задача"}, "available_tools": [], "tool_schemas": []},
        [],
    )

    assert step is not None
    assert step["ok"] is False
    assert step["reply"] == "Codex не успел ответить за отведенное время."


@pytest.mark.anyio
async def test_telegram_admin_zero_max_steps_disables_step_cap(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    settings = replace(_settings(), telegram_admin_codex_max_steps=0)
    calls = 0

    async def fake_runner(payload, trace, progress_callback=None):
        nonlocal calls
        calls += 1
        if calls <= 7:
            return {"tool_calls": [{"name": "yclients.services.list", "arguments": {}}]}
        return {"action": "codex_admin_reply", "ok": True, "reply": "Готово после расширенной проверки."}

    service = CodexTelegramAdminService(toolbox, settings, runner=fake_runner)

    result = await service.handle_text("Проверь без лимита шагов")

    assert result.ok is True
    assert result.message == "Готово после расширенной проверки."
    assert calls == 8


@pytest.mark.anyio
async def test_telegram_admin_codex_service_sends_avito_via_tool_call(tmp_path) -> None:
    class FakeSender:
        def __init__(self) -> None:
            self.calls = []

        async def send_message(self, account_id, chat_id, text):
            self.calls.append((account_id, chat_id, text))
            return {"sent": True}

        async def send_image(self, account_id, chat_id, image_path):
            return {"sent": True, "image_path": str(image_path)}

    sender = FakeSender()
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        avito_sender=sender,
        avito_account_id=355,
    )
    settings = _settings()

    async def fake_runner(payload, trace, progress_callback=None):
        if not trace:
            return {
                "tool_calls": [
                    {
                        "name": "avito.messages.send",
                        "arguments": {
                            "chat_id": "u2i-QzEmnqjpOLEsiR6vYTBenA",
                            "text": "Здравствуйте! Завтра в 14:00 свободно.",
                        },
                    }
                ]
            }
        return {"action": "codex_admin_reply", "ok": True, "reply": "Отправила клиенту в Avito."}

    service = CodexTelegramAdminService(toolbox, settings, runner=fake_runner)

    result = await service.handle_message(
        {
            "text": "Ответь клиенту, что завтра в 14:00 свободно",
            "channel": "telegram_admin",
            "codex_role": "olga_boss",
            "conversation_history": [
                {
                    "role": "assistant",
                    "content": "Вопрос из Avito\nЧат: u2i-QzEmnqjpOLEsiR6vYTBenA",
                }
            ],
        }
    )

    assert result.ok is True
    assert result.message == "Отправила клиенту в Avito."
    assert sender.calls == [(355, "u2i-QzEmnqjpOLEsiR6vYTBenA", "Здравствуйте! Завтра в 14:00 свободно.")]
    assert result.metadata["trace"][0]["tool"] == "avito.messages.send"


@pytest.mark.anyio
async def test_telegram_admin_codex_can_return_avito_client_draft(tmp_path) -> None:
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        JsonKnowledgeStore(tmp_path / "knowledge.json"),
        avito_account_id=355,
    )
    settings = _settings()

    async def fake_runner(payload, trace, progress_callback=None):
        return {
            "action": "avito_client_draft",
            "ok": True,
            "chat_id": "u2i-QzEmnqjpOLEsiR6vYTBenA",
            "draft_text": "К сожалению, на завтра записать не получится. Могу посмотреть другое ближайшее время.",
            "reply": "Подготовила черновик.",
        }

    service = CodexTelegramAdminService(toolbox, settings, runner=fake_runner)

    result = await service.handle_message(
        {
            "text": "Нет, завтра нельзя",
            "channel": "telegram_admin",
            "codex_role": "olga_boss",
            "conversation_history": [
                {
                    "role": "assistant",
                    "content": "Нужна ручная консультация\nКлиент: Анна\nЧат: u2i-QzEmnqjpOLEsiR6vYTBenA\nСообщение: Можно завтра?",
                }
            ],
        }
    )

    assert result.ok is True
    assert result.action == "avito_client_draft"
    assert result.message == "Подготовила черновик."
    draft = avito_draft_from_result(result)
    assert draft == {
        "chat_id": "u2i-QzEmnqjpOLEsiR6vYTBenA",
        "draft_text": "К сожалению, на завтра записать не получится. Могу посмотреть другое ближайшее время.",
    }
    card = format_avito_client_draft_card({**draft, "client_name": "Анна"})
    assert "Черновик ответа клиенту Avito" in card
    assert "Анна" in card


def test_avito_draft_from_result_sanitizes_redundant_expert_tail() -> None:
    result = AdminResult(
        ok=True,
        action="avito_client_draft",
        message="Подготовила черновик.",
        metadata={
            "outcome": {
                "chat_id": "u2i-test",
                "draft_text": (
                    "Елена, фото посмотрели. По визуальной оценке можно ориентироваться на увеличение примерно на +1 размер, "
                    "ориентировочный объём — около 400 мл препарата. "
                    "Точный объём и итоговую стоимость лучше подтвердить после уточнения желаемого результата и исходных данных."
                ),
            }
        },
    )

    draft = avito_draft_from_result(result)
    card = format_avito_client_draft_card({**draft, "client_name": "Елена"})

    assert draft["draft_text"] == (
        "Елена, фото посмотрели. По визуальной оценке можно ориентироваться на увеличение примерно на +1 размер, "
        "ориентировочный объём — около 400 мл препарата."
    )
    assert "Точный объём" not in card


def test_admin_codex_prompt_tells_agent_to_preserve_olga_prices_and_terms() -> None:
    prompt = build_admin_codex_prompt(
        {
            "role_name": "olga_boss",
            "message": {
                "text": (
                    "Как модель\n300мл 50 000\n400мл 70 000\n"
                    "Тесоро боди\nДо 4ех лет\nОчная консультация платная\nОнлайн консультация бесплатная"
                )
            },
            "available_tools": [],
        },
        [],
    )

    assert "обязательно сохрани все эти факты" in prompt
    assert "цены" in prompt
    assert "платная/бесплатная консультация" in prompt


def test_avito_draft_revision_prompt_lets_codex_choose_send_or_new_draft() -> None:
    prompt = avito_draft_revision_prompt(
        {"chat_id": "u2i-QzEmnqjpOLEsiR6vYTBenA", "draft_text": "К сожалению, завтра нельзя."},
        "Лучше предложи пятницу",
    )

    assert "сам оцени" in prompt.casefold()
    assert "avito.messages.send" in prompt
    assert "avito_client_draft" in prompt
    assert "Лучше предложи пятницу" in prompt
    assert "Голосовые сообщения" in prompt
    assert "экспертным уточнением" in prompt


def test_codex_chat_timeout_uses_long_default_and_env_floor(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_TIMEOUT_SECONDS", raising=False)
    assert codex_chat_timeout_seconds() == 1800

    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "15")
    assert codex_chat_timeout_seconds() == 60

    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "2400")
    assert codex_chat_timeout_seconds() == 2400


def test_avito_draft_reply_text_includes_voice_transcription(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "recognize_message_media",
        lambda bot, message: main_module.RecognizedMedia(
            "voice",
            "Голос принят.",
            "Пользователь отправил голосовое сообщение. Расшифровка:\n600-800 мл",
        ),
    )

    text = main_module.avito_draft_reply_text_for_codex(object(), {"voice": {"file_id": "voice-1"}}, "")

    assert "Пользователь отправил голосовое сообщение" in text
    assert "600-800 мл" in text


def test_telegram_callback_delivery_target_uses_callback_message_chat_and_thread() -> None:
    callback = {
        "message": {
            "chat": {"id": 5993376751},
            "message_thread_id": 56628,
        }
    }

    chat_id, params = telegram_callback_delivery_target(callback, "912405808")

    assert chat_id == "5993376751"
    assert params == {"message_thread_id": "56628"}


def test_avito_draft_reject_confirmation_stays_in_callback_chat_thread(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.answers: list[tuple[str, str]] = []
            self.messages: list[tuple[str, str, dict]] = []

        def answer_callback_query(self, callback_id, text):
            self.answers.append((callback_id, text))

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((str(chat_id), text, kwargs))
            return {"ok": True, "result": {"message_id": 1}}

    drafts = {
        "draft1": {
            "id": "draft1",
            "status": "pending",
            "chat_id": "u2i-QzEmnqjpOLEsiR6vYTBenA",
            "draft_text": "Здравствуйте, пришлите фото до/после.",
            "telegram_chat_id": "5993376751",
            "telegram_message_id": "123",
        }
    }

    monkeypatch.setattr(main_module, "load_avito_client_drafts", lambda: drafts.copy())
    monkeypatch.setattr(main_module, "save_avito_client_drafts", lambda updated: drafts.update(updated))
    bot = FakeBot()

    handled = main_module.handle_avito_draft_callback(
        bot=bot,
        callback_id="cb-1",
        data="avdraft:draft1:reject",
        settings=_settings(),
        telegram_chat_id="5993376751",
        topic_params={"message_thread_id": "56628"},
    )

    assert handled is True
    assert bot.answers == [("cb-1", "Не отправляю")]
    assert bot.messages == [
        (
            "5993376751",
            "Ок, не отправляю. Можно ответить на карточку черновика правкой, если нужно подготовить новый вариант.",
            {"message_thread_id": "56628"},
        )
    ]
    assert drafts["draft1"]["status"] == "rejected"


def test_avito_draft_remember_button_stores_approved_rag_item(monkeypatch, tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.answers: list[tuple[str, str]] = []
            self.messages: list[tuple[str, str, dict]] = []

        def answer_callback_query(self, callback_id, text):
            self.answers.append((callback_id, text))

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((str(chat_id), text, kwargs))
            return {"ok": True, "result": {"message_id": 1}}

    drafts = {
        "draft1": {
            "id": "draft1",
            "status": "pending",
            "chat_id": "avito-chat-1",
            "draft_text": "Для ягодиц используем препарат Tesoro Body.",
            "telegram_chat_id": "5993376751",
            "telegram_message_id": "123",
            "handoff_id": "handoff-1",
        }
    }
    refs = {
        "5993376751:100": {
            "handoff_id": "handoff-1",
            "avito_chat_id": "avito-chat-1",
            "source_message_id": "m-client-1",
            "handoff_text": (
                "Нужна ручная консультация\n"
                "Причина: missing_data\n"
                "Канал: avito\n"
                "Клиент: Анна\n"
                "Сообщение: Какой филлер используется для ягодиц?\n"
                "Контекст: Уточнить препарат."
            ),
        }
    }

    monkeypatch.setattr(main_module, "load_avito_client_drafts", lambda: drafts.copy())
    monkeypatch.setattr(main_module, "save_avito_client_drafts", lambda updated: drafts.update(updated))
    monkeypatch.setattr(main_module, "load_telegram_handoff_refs", lambda: refs)
    settings = replace(_settings(), rag_expert_db_path=tmp_path / "expert.sqlite3")
    bot = FakeBot()

    handled = main_module.handle_avito_draft_callback(
        bot=bot,
        callback_id="cb-remember",
        data="avdraft:draft1:remember",
        settings=settings,
        telegram_chat_id="5993376751",
        topic_params={"message_thread_id": "56628"},
    )

    assert handled is True
    assert bot.answers == [("cb-remember", "Запомнила")]
    assert drafts["draft1"]["remember_status"] == "approved"
    matches = ExpertRagStore(tmp_path / "expert.sqlite3").search("какой препарат для увеличения ягодиц", min_score=0.1)
    assert matches
    assert matches[0][0].status == APPROVED
    assert "Tesoro Body" in matches[0][0].answer_client


def test_avito_draft_forget_button_marks_memory_skipped(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.answers: list[tuple[str, str]] = []
            self.messages: list[tuple[str, str, dict]] = []

        def answer_callback_query(self, callback_id, text):
            self.answers.append((callback_id, text))

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((str(chat_id), text, kwargs))
            return {"ok": True, "result": {"message_id": 1}}

    drafts = {
        "draft1": {
            "id": "draft1",
            "status": "pending",
            "chat_id": "avito-chat-1",
            "draft_text": "Ответ клиенту.",
            "telegram_chat_id": "5993376751",
            "telegram_message_id": "123",
        }
    }

    monkeypatch.setattr(main_module, "load_avito_client_drafts", lambda: drafts.copy())
    monkeypatch.setattr(main_module, "save_avito_client_drafts", lambda updated: drafts.update(updated))
    bot = FakeBot()

    handled = main_module.handle_avito_draft_callback(
        bot=bot,
        callback_id="cb-forget",
        data="avdraft:draft1:forget",
        settings=_settings(),
        telegram_chat_id="5993376751",
    )

    assert handled is True
    assert bot.answers == [("cb-forget", "Не запоминаю")]
    assert drafts["draft1"]["remember_status"] == "skipped"


@pytest.mark.anyio
async def test_telegram_olga_avito_send_teaches_mentor_memory(tmp_path) -> None:
    class FakeSender:
        async def send_message(self, account_id, chat_id, text):
            return {"sent": True, "account_id": account_id, "chat_id": chat_id, "text": text}

        async def send_image(self, account_id, chat_id, image_path):
            return {"sent": True}

        async def send_file(self, account_id, chat_id, file_path, caption=""):
            return {"sent": True}

    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    toolbox = AutomationToolbox(
        DryRunYClientsGateway(),
        knowledge,
        avito_sender=FakeSender(),
        avito_account_id=355,
    )
    settings = _settings()

    async def fake_runner(payload, trace, progress_callback=None):
        if not trace:
            return {
                "tool_calls": [
                    {
                        "name": "avito.messages.send",
                        "arguments": {"chat_id": "chat-price", "text": "Стоимость Anti-Dark 1 фл 8000 руб."},
                    }
                ]
            }
        return {"action": "codex_admin_reply", "ok": True, "reply": "Отправила и запомнила."}

    service = CodexTelegramAdminService(
        toolbox,
        settings,
        runner=fake_runner,
        mentor_memory=MentorMemoryService(knowledge),
    )

    result = await service.handle_message({"text": "Ответь клиенту", "codex_role": "olga_boss"})

    memories = knowledge.list(tags=("mentor",))
    assert result.ok is True
    assert any(item.kind == "price_policy" and "Anti-Dark" in item.content for item in memories)
    assert memories[0].metadata["actor"] == "olga"


def test_admin_codex_prompt_states_no_separate_parser() -> None:
    prompt = build_admin_codex_prompt({"message": {"text": "Запиши Анну"}, "available_tools": []}, [])

    assert "Нет отдельного парсера команд" in prompt
    assert "Не копируй её дословно" in prompt
    assert "tool_calls" in prompt
    assert "Работай нелинейно" in prompt
    assert "conversation_history" in prompt
    assert "Ольга — косметолог и владелец экспертного контекста" in prompt
    assert "role_name='olga_boss'" in prompt
    assert "role_name='admin'" in prompt
    assert "не называй его Ольгой" in prompt
    assert "не отвечай клиентской рамкой 'Я помощник Ольги...'" in prompt
    assert "не начинай клиентский текст обращением 'Ольга, ...'" in prompt


def test_admin_codex_services_compact_keeps_relevant_thread_services_visible() -> None:
    services = [{"id": index, "title": f"Service {index}", "price": 1000} for index in range(20)]
    services.append({"id": 27204264, "title": "Нити коги (нити Cog)", "price": 3500})

    compact = admin_codex_module._compact_tool_data("yclients.services.list", {"services": services})

    assert compact["services_count"] == 21
    assert all(item["id"] != 27204264 for item in compact["services_sample"])
    assert {"id": 27204264, "title": "Нити коги (нити Cog)", "price": 3500} in compact["services_relevant"]


@pytest.mark.anyio
async def test_codex_tool_loop_planner_writes_redacted_trace_log(tmp_path) -> None:
    log_path = tmp_path / "agent_trace.jsonl"
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))

    async def fake_codex_loop(payload, trace):
        if not trace:
            return {
                "tool_calls": [
                    {
                        "name": "yclients.appointments.create",
                        "arguments": {
                            "city": "Москва",
                            "service_id": 7,
                            "datetime": "2026-06-01T10:00:00",
                            "phone": "+7 999 123-45-67",
                        },
                    }
                ]
            }
        return {"action": "codex_reply", "reply": "Запись создана."}

    consultant = AvitoConsultant(
        toolbox,
        planner=CodexToolLoopPlanner(fake_codex_loop, trace_logger=JsonlAgentTraceLogger(log_path)),
    )
    message = avito_inbound_message(
        {
            "type": "message",
            "message_id": "m-redact",
            "chat_id": "chat-redact",
            "content": {"text": "Запишите меня на чистку лица, телефон +7 999 123-45-67"},
        }
    )

    reply = await consultant.respond(message)
    content = log_path.read_text(encoding="utf-8")

    assert reply.metadata["trace_log_path"] == str(log_path)
    assert "chat-redact" in content
    assert "[phone]" in content
    assert "+7 999 123-45-67" not in content
    assert '"phone": "[redacted]"' in content


@pytest.mark.anyio
async def test_avito_tool_loop_rejects_tools_outside_role_even_with_unfiltered_toolbox(tmp_path) -> None:
    class RecordingGateway(DryRunYClientsGateway):
        def __init__(self) -> None:
            super().__init__()
            self.create_calls = 0

        async def create_appointment(self, appointment):
            self.create_calls += 1
            return await super().create_appointment(appointment)

    gateway = RecordingGateway()
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / "knowledge.json"))

    async def fake_codex_loop(payload, trace):
        if not trace:
            return {
                "tool_calls": [
                    {
                        "name": "yclients.appointments.create",
                        "arguments": {
                            "city": "Москва",
                            "service_id": 7,
                            "datetime": "2026-06-01T10:00:00",
                            "phone": "+7 999 123-45-67",
                        },
                    }
                ]
            }
        assert trace[-1]["ok"] is False
        assert "not allowed" in trace[-1]["error"]
        return {"action": "codex_reply", "reply": "Не буду создавать запись без подтверждения администратора."}

    consultant = AvitoConsultant(toolbox, planner=CodexToolLoopPlanner(fake_codex_loop))
    message = avito_inbound_message(
        {
            "type": "message",
            "message_id": "m-forbidden-tool",
            "chat_id": "chat-forbidden-tool",
            "content": {"text": "Здравствуйте, хочу уточнить детали ухода"},
        }
    )

    reply = await consultant.respond(message)

    assert reply.action == "codex_reply"
    assert gateway.create_calls == 0
    assert reply.metadata["trace"][1]["ok"] is False
    assert "not allowed" in reply.metadata["trace"][1]["error"]


@pytest.mark.anyio
@pytest.mark.parametrize("role", [CodexRole.TELEGRAM_CLIENT, CodexRole.VK_CLIENT])
async def test_telegram_and_vk_client_tool_loop_reject_yclients_mutations(tmp_path, role) -> None:
    class RecordingGateway(DryRunYClientsGateway):
        def __init__(self) -> None:
            super().__init__()
            self.create_calls = 0

        async def create_appointment(self, appointment):
            self.create_calls += 1
            return await super().create_appointment(appointment)

    gateway = RecordingGateway()
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / f"{role.value}.json"))

    async def fake_codex_loop(payload, trace):
        if not trace:
            assert "yclients.appointments.create" not in payload["available_tools"]
            return {
                "tool_calls": [
                    {
                        "name": "yclients.appointments.create",
                        "arguments": {
                            "city": "Москва",
                            "service_id": 7,
                            "datetime": "2026-06-01T10:00:00",
                            "phone": "+7 999 123-45-67",
                        },
                    }
                ]
            }
        assert trace[-1]["ok"] is False
        assert "not allowed" in trace[-1]["error"]
        return {"action": "codex_reply", "reply": "Передам на подтверждение, live-запись не создаю."}

    consultant = AvitoConsultant(
        toolbox,
        planner=CodexToolLoopPlanner(fake_codex_loop),
        profile=role_profile(role),
    )
    message = InboundMessage(
        channel=Channel.TELEGRAM_CLIENT if role == CodexRole.TELEGRAM_CLIENT else Channel.VK,
        client_id=f"client-{role.value}",
        chat_id=f"chat-{role.value}",
        text="Здравствуйте, хочу уточнить детали ухода",
    )

    reply = await consultant.respond(message)

    assert reply.action == "codex_reply"
    assert gateway.create_calls == 0
    assert reply.metadata["trace"][1]["ok"] is False
    assert "not allowed" in reply.metadata["trace"][1]["error"]


def test_redact_sensitive_masks_nested_values() -> None:
    payload = {"message": "телефон 8 999 123-45-67", "auth": {"token": "secret"}, "items": [{"phone": "79991234567"}]}

    redacted = redact_sensitive(payload)

    assert redacted["message"] == "телефон [phone]"
    assert redacted["auth"]["token"] == "[redacted]"
    assert redacted["items"][0]["phone"] == "[redacted]"


def test_avito_test_mode_forces_preview_sender_and_codex(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_ADMIN_HISTORY_LIMIT", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "AVITO_SEND_ENABLED=true",
                "AVITO_CODEX_ENABLED=false",
                "HANDOFF_NOTIFY_ENABLED=true",
                "AVITO_ACCOUNT_ID=1",
                "AVITO_CLIENT_ID=client",
                "AVITO_CLIENT_SECRET=secret",
                "AVITO_WEBHOOK_SECRET=webhook",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AVITO_TEST_MODE", "true")

    settings = IntegrationSettings.from_env(env_path)

    assert settings.avito_send_enabled is False
    assert settings.avito_codex_enabled is True
    assert settings.handoff_notify_enabled is False
    assert settings.telegram_admin_history_limit == 0


def test_telegram_admin_codex_timeout_defaults_are_longer(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_ADMIN_CODEX_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_RESPONSE_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("AVITO_TEST_MODE", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")

    settings = IntegrationSettings.from_env(env_path)

    assert settings.telegram_admin_codex_timeout_seconds == 1800
    assert settings.telegram_admin_response_wait_seconds == 60


def _settings(allow_mutations: bool = False) -> IntegrationSettings:
    runtime_dir = Path(os.getenv("AUTOMATICCOSMETIC_TEST_RUNTIME_DIR", ""))
    history_db_path = (runtime_dir / "leads.sqlite3") if runtime_dir else Path("data/leads.sqlite3")
    expert_db_path = (runtime_dir / "expert_rag.sqlite3") if runtime_dir else Path("data/expert_rag.sqlite3")
    return IntegrationSettings(
        public_base_url="https://olgatihcosmo.com",
        cities=("Ростов-на-Дону", "Москва"),
        telegram_admin_user_id=1,
        telegram_cosmetologist_user_id=2,
        telegram_extra_admin_user_ids=(),
        telegram_admin_bot_token="admin-token",
        telegram_client_bot_token="client-token",
        telegram_client_codex_enabled=False,
        telegram_client_followup_send_enabled=False,
        telegram_admin_codex_enabled=True,
        telegram_admin_codex_timeout_seconds=180,
        telegram_admin_codex_max_steps=6,
        telegram_admin_live_drafts_enabled=True,
        telegram_admin_live_draft_interval_seconds=1.2,
        telegram_admin_history_enabled=True,
        telegram_admin_history_limit=8,
        telegram_admin_history_db_path=history_db_path,
        openrouter_api_key="openrouter",
        default_model="model",
        avito_codex_enabled=False,
        avito_codex_timeout_seconds=180,
        avito_codex_max_steps=6,
        avito_turn_debounce_seconds=0,
        avito_turn_max_wait_seconds=120,
        avito_turn_batch_max_messages=10,
        avito_unanswered_autostart=False,
        avito_unanswered_autoreply_enabled=False,
        avito_unanswered_min_age_seconds=1200,
        avito_unanswered_interval_seconds=300,
        avito_unanswered_lookback_seconds=86400,
        rag_retrieval_enabled=True,
        rag_autoanswer_threshold=0.82,
        rag_handoff_threshold=0.65,
        rag_expert_db_path=expert_db_path,
        yclients_api_key="api-key",
        yclients_user_token="user-token",
        yclients_company_id=123,
        yclients_city_company_ids={},
        yclients_city_staff_ids={},
        yclients_partner_id=0,
        yclients_form_id=0,
        yclients_integration_secret="",
        yclients_allow_mutations=allow_mutations,
        avito_account_id=1,
        avito_account_ids=(1, 2),
        avito_client_id="avito-client",
        avito_client_secret="avito-secret",
        avito_webhook_secret="webhook",
        avito_send_enabled=False,
        avito_image_send_enabled=True,
        handoff_notify_enabled=False,
        handoff_notify_chat_id="",
        vk_group_id=0,
        vk_group_token="",
        vk_api_version="5.199",
        vk_send_enabled=False,
        vk_codex_enabled=False,
    )


def _settings_with_city_company_ids(city_company_ids: dict[str, int]) -> IntegrationSettings:
    return replace(_settings(), yclients_city_company_ids=city_company_ids)


def test_default_test_settings_use_isolated_runtime_paths(tmp_path) -> None:
    settings = _settings()

    assert not str(settings.telegram_admin_history_db_path).startswith("data/")
    assert not str(settings.rag_expert_db_path).startswith("data/")
    assert Path(os.environ["AUTOMATICCOSMETIC_TEST_RUNTIME_DIR"]) in settings.telegram_admin_history_db_path.parents
    assert Path(os.environ["AUTOMATICCOSMETIC_TEST_RUNTIME_DIR"]) in settings.rag_expert_db_path.parents


@pytest.mark.anyio
async def test_yclients_http_gateway_maps_services_slots_and_clients() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/book_services/123":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "services": [
                            {"id": 7, "title": "Чистка лица", "price_min": 3500, "seance_length": 5400}
                        ]
                    },
                },
            )
        if request.url.path == "/api/v1/book_staff/123":
            return httpx.Response(200, json={"success": True, "data": [{"id": 9, "name": "Ростов-на-Дону"}]})
        if request.url.path == "/api/v1/book_times/123/9/2026-06-01":
            return httpx.Response(200, json={"success": True, "data": ["11:00", {"time": "14:30"}]})
        if request.url.path == "/api/v1/company/123/clients/search":
            return httpx.Response(
                200,
                json={"success": True, "data": {"clients": [{"id": 5, "name": "Анна", "phone": "79990000000"}]}},
            )
        if request.url.path == "/api/v1/company/123":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "title": "4 города",
                        "city": "Москва",
                        "address": "Москва, Малый Гнездниковский переулок, 12",
                        "coordinate_lat": 55.762435,
                        "coordinate_lon": 37.606628,
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler))
    gateway = YClientsHttpGateway(_settings(), client=client)

    services = await gateway.get_services("Ростов-на-Дону")
    slots = await gateway.get_free_slots("Ростов-на-Дону", services[0].id, "2026-06-01")
    clients = await gateway.search_clients("Анна")
    address = await gateway.get_company_address("Москва")

    assert services[0].title == "Чистка лица"
    assert services[0].duration_minutes == 90
    assert [slot.starts_at.strftime("%H:%M") for slot in slots] == ["11:00", "14:30"]
    assert [slot.staff_id for slot in slots] == [9, 9]
    assert clients[0].external_id == "5"
    assert address["address"] == "Москва, Малый Гнездниковский переулок, 12"
    assert [request.method for request in requests] == ["GET", "GET", "GET", "POST", "GET"]


@pytest.mark.anyio
async def test_yclients_http_gateway_uses_city_specific_company_id_for_slots() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/book_staff/777":
            return httpx.Response(200, json={"success": True, "data": [{"id": 17, "name": "Ростов-на-Дону"}]})
        if request.url.path == "/api/v1/book_times/777/17/2026-06-01":
            return httpx.Response(200, json={"success": True, "data": ["12:00"]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler))
    gateway = YClientsHttpGateway(_settings_with_city_company_ids({"Ростов-на-Дону": 777}), client=client)

    slots = await gateway.get_free_slots("Ростов", 7, "2026-06-01")

    assert [slot.starts_at.strftime("%H:%M") for slot in slots] == ["12:00"]
    assert slots[0].staff_id == 17
    assert [request.url.path for request in requests] == [
        "/api/v1/book_staff/777",
        "/api/v1/book_times/777/17/2026-06-01",
    ]


@pytest.mark.anyio
async def test_yclients_http_gateway_uses_city_company_for_move() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "PUT"
        assert request.url.path == "/api/v1/record/777/41"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "id": 41,
                    "datetime": "2026-06-02 12:00:00",
                    "client": {"id": 5, "name": "Анна"},
                    "services": [{"id": 7, "title": "Чистка лица", "price": 3500}],
                },
            },
        )

    gateway = YClientsHttpGateway(
        replace(_settings_with_city_company_ids({"Ростов-на-Дону": 777}), yclients_allow_mutations=True),
        client=httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler)),
    )

    moved = await gateway.move_appointment(
        41,
        Slot(city="Ростов-на-Дону", starts_at=datetime(2026, 6, 2, 12, 0), staff_id=17),
        "Ростов-на-Дону",
    )

    assert moved.id == 41
    assert moved.city == "Ростов-на-Дону"
    assert len(requests) == 1


@pytest.mark.anyio
async def test_yclients_http_gateway_sets_and_deletes_city_staff_schedule() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "PUT"
        assert request.url.path == "/api/v1/company/777/staff/schedule"
        return httpx.Response(200, json={"success": True, "data": []})

    settings = replace(
        _settings_with_city_company_ids({"Ростов-на-Дону": 777}),
        yclients_city_staff_ids={"Ростов-на-Дону": 17},
        yclients_allow_mutations=True,
    )
    gateway = YClientsHttpGateway(
        settings,
        client=httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler)),
    )

    updated = await gateway.set_staff_schedule("Ростов", ["2026-06-25"])
    deleted = await gateway.delete_staff_schedule("Ростов", ["2026-06-25"])

    assert updated["slots"] == [{"from": "11:00", "to": "20:00"}]
    assert deleted["deleted_count"] == 1
    assert json.loads(requests[0].content) == {
        "schedules_to_set": [
            {
                "staff_id": 17,
                "dates": ["2026-06-25"],
                "slots": [{"from": "11:00", "to": "20:00"}],
            }
        ],
        "schedules_to_delete": [],
    }
    assert json.loads(requests[1].content) == {
        "schedules_to_set": [],
        "schedules_to_delete": [{"staff_id": 17, "dates": ["2026-06-25"]}],
    }


@pytest.mark.anyio
async def test_yclients_http_gateway_updates_notes_in_selected_city_company() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/company/777/clients/search":
            return httpx.Response(
                200,
                json={"success": True, "data": {"clients": [{"id": 5, "name": "Анна", "phone": "79990000000"}]}},
            )
        if request.url.path == "/api/v1/company/777/clients/5/comments":
            return httpx.Response(200, json={"success": True, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    gateway = YClientsHttpGateway(
        replace(_settings_with_city_company_ids({"Ростов-на-Дону": 777}), yclients_allow_mutations=True),
        client=httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler)),
    )

    await gateway.update_client_notes("5", "Аллергии не сообщала", city="Ростов-на-Дону")

    assert [request.url.path for request in requests] == [
        "/api/v1/company/777/clients/search",
        "/api/v1/company/777/clients/5/comments",
    ]


@pytest.mark.anyio
async def test_yclients_slots_tool_without_service_id_makes_one_book_times_request(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/book_staff/123":
            return httpx.Response(200, json={"success": True, "data": [{"id": 9, "name": "Ростов-на-Дону"}]})
        if request.url.path == "/api/v1/book_times/123/9/2026-06-01":
            return httpx.Response(200, json={"success": True, "data": ["11:00"]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    schedule = CityScheduleStore(tmp_path / "city_schedule.json")
    schedule.set_dates("Ростов-на-Дону", ["2026-06-01"])
    client = httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler))
    gateway = YClientsHttpGateway(_settings(), client=client)
    toolbox = AutomationToolbox(gateway, JsonKnowledgeStore(tmp_path / "knowledge.json"), city_schedule=schedule)

    result = await toolbox.execute("yclients.slots.list", {"city": "Ростов-на-Дону", "date": "2026-06-01"})

    assert result.ok is True
    assert result.data["slots"][0]["service_id"] == 0
    assert [request.url.path for request in requests] == [
        "/api/v1/book_staff/123",
        "/api/v1/book_times/123/9/2026-06-01",
    ]


@pytest.mark.anyio
async def test_yclients_http_gateway_does_not_use_aggregate_slots_when_city_staff_missing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/book_staff/123":
            return httpx.Response(200, json={"success": True, "data": [{"id": 9, "name": "Москва"}]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler))
    gateway = YClientsHttpGateway(_settings(), client=client)

    slots = await gateway.get_free_slots("Геленджик", 7, "2026-06-01")

    assert slots == []
    assert [request.url.path for request in requests] == ["/api/v1/book_staff/123"]


@pytest.mark.anyio
async def test_yclients_http_gateway_blocks_mutations_by_default() -> None:
    gateway = YClientsHttpGateway(
        _settings(allow_mutations=False),
        client=httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )
    appointment = Appointment(
        client=ClientProfile(name="Анна", phone="+79990000000"),
        service=Service(id=7, title="Чистка лица", price=3500, duration_minutes=90),
        city="Ростов-на-Дону",
        starts_at=datetime(2026, 6, 1, 11, 0),
    )

    with pytest.raises(YClientsMutationDisabled):
        await gateway.create_appointment(appointment)


@pytest.mark.anyio
async def test_yclients_http_gateway_can_create_when_mutations_allowed() -> None:
    seen_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        assert request.method == "POST"
        assert request.url.path == "/api/v1/records/123"
        seen_payload = dict(__import__("json").loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"success": True, "data": {"record_id": 777}})

    gateway = YClientsHttpGateway(
        _settings(allow_mutations=True),
        client=httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler)),
    )
    appointment = Appointment(
        client=ClientProfile(name="Анна", phone="8 999 000 00 00"),
        service=Service(id=7, title="Чистка лица", price=3500, duration_minutes=90),
        city="Ростов-на-Дону",
        starts_at=datetime(2026, 6, 1, 11, 0),
        notes="Источник: Avito",
        raw={"staff_id": 17},
    )

    record_id = await gateway.create_appointment(appointment)

    assert record_id == 777
    assert seen_payload["staff_id"] == 17
    assert seen_payload["client"]["phone"] == "79990000000"
    assert seen_payload["services"][0]["id"] == 7
    assert seen_payload["save_if_busy"] is False


@pytest.mark.anyio
async def test_yclients_http_gateway_resolves_staff_id_for_create_when_missing() -> None:
    requests: list[httpx.Request] = []
    seen_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v1/book_staff/123":
            return httpx.Response(200, json={"success": True, "data": [{"id": 42, "name": "Москва"}]})
        if request.method == "POST" and request.url.path == "/api/v1/records/123":
            seen_payload = dict(json.loads(request.content.decode("utf-8")))
            return httpx.Response(201, json={"success": True, "data": {"id": 778}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    gateway = YClientsHttpGateway(
        _settings(allow_mutations=True),
        client=httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler)),
    )

    record_id = await gateway.create_appointment(
        Appointment(
            client=ClientProfile(name="Анна", phone="+79990000000"),
            service=Service(id=7, title="Чистка лица", price=3500, duration_minutes=90),
            city="Москва",
            starts_at=datetime(2026, 6, 1, 11, 0),
        )
    )

    assert record_id == 778
    assert seen_payload["staff_id"] == 42
    assert [request.url.path for request in requests] == ["/api/v1/book_staff/123", "/api/v1/records/123"]


@pytest.mark.anyio
async def test_yclients_http_gateway_treats_delete_204_as_cancelled() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/record/123/777"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": 777,
                        "datetime": "2026-06-01 11:00:00",
                        "client": {"id": 5, "name": "Анна", "phone": "79990000000"},
                        "services": [{"id": 7, "title": "Чистка лица", "price": 3500, "seance_length": 5400}],
                    },
                },
            )
        assert request.method == "DELETE"
        return httpx.Response(204)

    gateway = YClientsHttpGateway(
        _settings(allow_mutations=True),
        client=httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler)),
    )

    cancelled = await gateway.cancel_appointment(777, "Москва")

    assert cancelled is not None
    assert cancelled.id == 777
    assert cancelled.client.name == "Анна"
    assert cancelled.city == "Москва"
    assert len(requests) == 2


@pytest.mark.anyio
async def test_yclients_http_gateway_error_includes_status_and_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/records/123"
        return httpx.Response(400, json={"success": False, "data": None, "meta": {"message": "Произошла ошибка"}})

    gateway = YClientsHttpGateway(
        _settings(allow_mutations=True),
        client=httpx.AsyncClient(base_url="https://api.yclients.test", transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError) as exc:
        await gateway.create_appointment(
            Appointment(
                client=ClientProfile(name="Анна", phone="+79990000000"),
                service=Service(id=7, title="Чистка лица", price=3500, duration_minutes=90),
                city="Москва",
                starts_at=datetime(2026, 6, 1, 11, 0),
                raw={"staff_id": 42},
            )
        )

    assert "status=400" in str(exc.value)
    assert "Произошла ошибка" in str(exc.value)


@pytest.mark.anyio
async def test_live_read_dry_run_gateway_reads_live_and_writes_memory() -> None:
    read_gateway = DryRunYClientsGateway(services=[Service(id=9, title="Пилинг", price=3000, duration_minutes=60)])
    gateway = LiveReadDryRunYClientsGateway(read_gateway)

    services = await gateway.get_services("Москва")
    appointment_id = await gateway.create_appointment(
        Appointment(
            client=ClientProfile(name="Анна", phone="+79990000000"),
            service=services[0],
            city="Москва",
            starts_at=read_gateway.slots[3].starts_at,
        )
    )

    assert services[0].id == 9
    assert appointment_id == 1
    assert len(gateway.dry_run_gateway.appointments) == 1


def test_avito_webhook_rejects_bad_token() -> None:
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=bad", json={"type": "message", "text": "Ростов"})
        assert response.status_code == 403
    finally:
        avito_app.dependency_overrides.clear()


def test_processing_outcome_controls_dedup_for_webhook_and_missed_poller() -> None:
    processed = processing_outcome_from_result({"ok": True, "processing_status": "processed"})
    queued = processing_outcome_from_result({"ok": True, "processing_status": "queued"})
    safe_ignore = processing_outcome_from_result({"ok": True, "processing_status": "ignored", "ignored": True, "reason": "own_message"})
    unsafe_ignore = processing_outcome_from_result({"ok": True, "processing_status": "ignored", "ignored": True, "reason": "unknown_silent_skip"})
    retryable = processing_outcome_from_result({"ok": False, "processing_status": "retryable_error", "reason": "telegram_down"})

    assert processed.safe_to_dedup is True
    assert queued.safe_to_dedup is True
    assert safe_ignore.safe_to_dedup is True
    assert unsafe_ignore.safe_to_dedup is False
    assert unsafe_ignore.ok is False
    assert retryable.safe_to_dedup is False
    assert missed_poller_dedup_allowed({"ok": False, "processing_status": "retryable_error"}) is False
    assert missed_poller_dedup_allowed({"ok": True, "processing_status": "ignored", "ignored": True, "reason": "not_message_event"}) is True
    assert missed_poller_dedup_allowed({"ok": True, "processing_status": "ignored", "ignored": True, "reason": "unknown_silent_skip"}) is False


def test_avito_webhook_processes_booking_decision_and_deduplicates() -> None:
    processed_events.seen.clear()
    settings = _settings()
    gateway = DryRunYClientsGateway()
    slot = gateway.slots[0]
    avito_app.dependency_overrides[get_settings] = lambda: settings
    avito_app.dependency_overrides[get_booking] = lambda: gateway
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "m-webhook-1",
                "chat_id": "chat-webhook",
                "user_id": 123,
                "content": {
                    "text": (
                        f"{slot.city}, чистка лица {slot.starts_at.date().isoformat()} "
                        f"в {slot.starts_at.strftime('%H:%M')}, телефон 8 999 123 45 67"
                    )
                },
            },
        }
    }
    try:
        client = TestClient(avito_app)
        first = client.post("/avito/webhook?token=webhook", json=event)
        second = client.post("/avito/webhook?token=webhook", json=event)

        assert first.status_code == 200
        assert first.json()["action"] == "booking_confirmation_required"
        assert first.json()["dry_run"] is True
        assert first.json()["send"]["reason"] == "preview_only"
        assert first.json()["handoff"] == "booking_ambiguous"
        assert len(gateway.appointments) == 0
        assert second.json()["ignored"] is True
        assert second.json()["reason"] == "duplicate"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_marks_chat_read_after_handling() -> None:
    class FakeAvitoReader:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        async def list_chats(self, account_id: int, *, limit: int = 20, offset: int = 0) -> dict[str, object]:
            return {"chats": []}

        async def get_chat_messages(self, account_id: int, chat_id: str, *, limit: int = 30, offset: int = 0) -> dict[str, object]:
            return {"messages": []}

        async def mark_chat_read(self, account_id: int, chat_id: str) -> dict[str, object]:
            self.calls.append((account_id, chat_id))
            return {"ok": True, "account_id": account_id, "chat_id": chat_id}

    processed_events.seen.clear()
    settings = _settings()
    reader = FakeAvitoReader()
    avito_app.dependency_overrides[get_settings] = lambda: settings
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_avito_reader] = lambda: reader
    try:
        client = TestClient(avito_app)
        response = client.post(
            "/avito/webhook?token=webhook",
            json={"type": "message", "message_id": "read-1", "chat_id": "chat-read", "user_id": 1, "author_id": 123, "text": "Здравствуйте"},
        )

        assert response.status_code == 200
        assert response.json()["mark_read"]["ok"] is True
        assert reader.calls == [(1, "chat-read")]
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_ignores_own_messages() -> None:
    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "own-1",
                "chat_id": "chat-webhook",
                "user_id": 1,
                "author_id": 1,
                "content": {"text": "Наш исходящий ответ"},
            },
        }
    }
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json=event)

        assert response.status_code == 200
        assert response.json()["ignored"] is True
        assert response.json()["reason"] == "own_message"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_ignores_outgoing_messages_without_author_id() -> None:
    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "out-1",
                "chat_id": "chat-webhook",
                "direction": "out",
                "content": {"text": "Наш исходящий ответ"},
            },
        }
    }
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json=event)

        assert response.status_code == 200
        assert response.json()["ignored"] is True
        assert response.json()["reason"] == "not_incoming"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_outgoing_final_answer_closes_open_handoff(tmp_path, monkeypatch) -> None:
    import src.freelance_leads_bot.integrations.avito_webhook as avito_webhook_module

    closed: list[tuple[str, str]] = []

    def fake_update_latest_handoff_for_chat(chat_id: str, status: str) -> str:
        closed.append((chat_id, status))
        return "handoff-1"

    history_store = LeadStore(tmp_path / "history.sqlite3")
    monkeypatch.setattr(avito_webhook_module, "update_latest_handoff_for_chat", fake_update_latest_handoff_for_chat)
    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_history_store] = lambda: history_store
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "out-final-1",
                "chat_id": "chat-handoff",
                "direction": "out",
                "content": {"text": "Ближайшее время есть на 29, 30 и 31 июля. Адрес отправлю после записи."},
            },
        }
    }
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json=event)

        assert response.status_code == 200
        assert response.json()["ignored"] is True
        assert response.json()["reason"] == "not_incoming"
        assert response.json()["manual_outgoing"]["remembered"] is True
        assert response.json()["manual_outgoing"]["closed_handoff"] is True
        assert closed == [("chat-handoff", "closed")]
        history = history_store.recent_codex_chat(5, "avito:client:chat-handoff")
        assert history[-1]["role"] == "assistant"
        assert "29, 30 и 31 июля" in history[-1]["content"]
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_outgoing_promise_does_not_close_handoff(tmp_path, monkeypatch) -> None:
    import src.freelance_leads_bot.integrations.avito_webhook as avito_webhook_module

    closed: list[tuple[str, str]] = []
    history_store = LeadStore(tmp_path / "history.sqlite3")
    monkeypatch.setattr(avito_webhook_module, "update_latest_handoff_for_chat", lambda chat_id, status: closed.append((chat_id, status)) or "handoff-1")
    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_history_store] = lambda: history_store
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "out-promise-1",
                "chat_id": "chat-handoff",
                "direction": "out",
                "content": {"text": "Уточню свободное время и напишу вам."},
            },
        }
    }
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json=event)

        assert response.status_code == 200
        assert response.json()["manual_outgoing"]["remembered"] is True
        assert response.json()["manual_outgoing"]["closed_handoff"] is False
        assert closed == []
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_ignores_deleted_messages() -> None:
    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "deleted-webhook-1",
                "chat_id": "chat-webhook",
                "direction": "in",
                "type": "text",
                "author_id": 123,
                "content": {"text": "Сообщение удалено"},
            },
        }
    }
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json=event)

        assert response.status_code == 200
        assert response.json()["ignored"] is True
        assert response.json()["reason"] == "deleted_message"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_poller_skips_deleted_messages() -> None:
    should_process, reason = should_process_missed_avito_message(
        {
            "id": "deleted-1",
            "created": 1779984778,
            "direction": "in",
            "type": "text",
            "author_id": 123,
            "content": {"text": "Сообщение удалено"},
        },
        since_ts=1779980000,
        settings=_settings(),
    )

    assert should_process is False
    assert reason == "deleted_message"


def test_avito_poller_accepts_voice_messages_for_transcription() -> None:
    should_process, reason = should_process_missed_avito_message(
        {
            "id": "voice-1",
            "created": 1779984778,
            "direction": "in",
            "type": "voice",
            "author_id": 123,
            "content": {"voice": {"voice_id": "voice-file-1"}},
        },
        since_ts=1779980000,
        settings=_settings(),
    )

    assert should_process is True
    assert reason == ""


@pytest.mark.anyio
async def test_avito_poller_lists_recent_chats_with_pagination() -> None:
    class FakeReader:
        def __init__(self) -> None:
            self.calls = []

        async def list_chats(self, account_id, *, limit=20, offset=0):
            self.calls.append((limit, offset))
            return {"chats": [{"id": f"chat-{index}"} for index in range(offset, min(offset + limit, 150))]}

    reader = FakeReader()

    chats = await list_recent_missed_avito_chats(reader, 123, chat_limit=150)

    assert len(chats) == 150
    assert reader.calls == [(100, 0), (50, 100)]


def test_expert_rag_approved_answer_is_retrieved_and_deprecated_ignored(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    approved = store.upsert_from_handoff(
        question="Какой филлер используется для ягодиц?",
        answer_client="Для ягодиц используем препарат Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    deprecated = store.upsert_from_handoff(
        question="Сколько стоят губы?",
        answer_client="Старая цена 1 ₽.",
        status=APPROVED,
        approved_by="olga",
    )
    store.deprecate(deprecated.id)

    matches = store.search("Здравствуйте, какой препарат для увеличения ягодиц?", min_score=0.2)

    assert matches
    assert matches[0][0].id == approved.id
    assert all(item.id != deprecated.id for item, _score in matches)


def test_expert_rag_search_can_exclude_high_risk_answers(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    high_risk = store.upsert_from_handoff(
        question="Можно делать процедуру после операции?",
        answer_client="После операции процедуру можно делать только после разрешения врача.",
        status=APPROVED,
        approved_by="olga",
    )

    assert high_risk.risk_level == "high"
    assert store.search("Можно делать процедуру после операции?", min_score=0.1)
    assert store.search("Можно делать процедуру после операции?", min_score=0.1, exclude_risk_levels=("high",)) == []


def test_expert_rag_admin_plans_price_increase_without_mutation_until_apply(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    original = store.upsert_from_handoff(
        question="Сколько стоит увеличение ягодиц 600 мл?",
        answer_client="600 мл как модель 100 000, стандартная стоимость 145 000.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )
    service = ExpertRagAdminService(
        store,
        plans_path=tmp_path / "plans.json",
        audit_path=tmp_path / "audit.jsonl",
    )

    plan = service.plan_change("Подними цены на ягодицы на 10%", actor="olga")

    assert plan.status == "pending"
    assert plan.changes[0].source_id == original.id
    assert "110 000" in plan.changes[0].new_answer
    assert "159 500" in plan.changes[0].new_answer
    assert store.get(original.id).status == APPROVED  # type: ignore[union-attr]

    applied = service.apply_plan(plan.id, actor="olga")
    applied_again = service.apply_plan(plan.id, actor="olga")

    assert applied.status == "applied"
    assert applied_again.metadata["created_ids"] == applied.metadata["created_ids"]
    assert store.get(original.id).status == "deprecated"  # type: ignore[union-attr]
    created = store.get(applied.metadata["created_ids"][0])
    assert created is not None
    assert created.status == APPROVED
    assert "110 000" in created.answer_client
    assert created.metadata["replaces_id"] == original.id
    assert (tmp_path / "audit.jsonl").exists()


def test_expert_rag_admin_policy_plan_creates_non_autoanswer_rule(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    service = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl")

    plan = service.plan_change("Не говори про очную консультацию, максимум онлайн с Ольгой", actor="olga")
    applied = service.apply_plan(plan.id, actor="olga")
    created = store.get(applied.metadata["created_ids"][0])

    assert created is not None
    assert created.status == APPROVED
    assert created.metadata["autoanswer_allowed"] is False
    assert "онлайн-разбор" in created.answer_client


def test_expert_rag_admin_updates_tesoro_effect_duration(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    original = store.upsert_from_handoff(
        question="Сколько держится Tesoro Body?",
        answer_client="Для ягодиц используем Tesoro Body, эффект может сохраняться до 4 лет.",
        status=APPROVED,
        approved_by="olga",
    )
    service = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl")

    plan = service.plan_change("Tesoro теперь до 3 лет", actor="olga")
    applied = service.apply_plan(plan.id, actor="olga")

    assert plan.status == "pending"
    assert store.get(original.id).status == "deprecated"  # type: ignore[union-attr]
    created = store.get(applied.metadata["created_ids"][0])
    assert created is not None
    assert "до 3 лет" in created.answer_client
    assert created.metadata["autoanswer_allowed"] is True


def test_expert_rag_admin_remember_freeform_creates_reusable_answer(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    service = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl")

    plan = service.plan_change("Запомни вот так: онлайн-консультация с Ольгой бесплатная", actor="olga")
    applied = service.apply_plan(plan.id, actor="olga")
    created = store.get(applied.metadata["created_ids"][0])

    assert created is not None
    assert created.status == APPROVED
    assert created.metadata["autoanswer_allowed"] is True
    assert "онлайн-консультация" in created.answer_client


def test_expert_rag_admin_temporal_freeform_is_not_autoanswer(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    service = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl")

    plan = service.plan_change("Запомни вот так: завтра окно на губы в 15:00, адрес Ленина 1", actor="olga")
    applied = service.apply_plan(plan.id, actor="olga")
    created = store.get(applied.metadata["created_ids"][0])

    assert created is not None
    assert created.status == APPROVED
    assert created.metadata["autoanswer_allowed"] is False
    assert created.metadata["temporal_fact"] is True
    assert created.metadata["autoanswer_block_reason"] == "temporal_without_expiry"


def test_expert_rag_admin_ambiguous_price_increase_needs_clarification(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    store.upsert_from_handoff(
        question="Сколько стоит увеличение ягодиц?",
        answer_client="Ягодицы 600 мл 100 000.",
        status=APPROVED,
        approved_by="olga",
    )
    store.upsert_from_handoff(
        question="Сколько стоит увеличение груди?",
        answer_client="Грудь 700 мл 115 000.",
        status=APPROVED,
        approved_by="olga",
    )
    service = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl")

    plan = service.plan_change("Подними цены на 10%", actor="olga")

    assert plan.status == "needs_clarification"
    assert plan.changes == []


def test_rag_admin_intent_parser_understands_freeform_price_language() -> None:
    intent = RagAdminIntentParser().parse("Слушай, по попе теперь чуть дороже сделай, процентов на десять")

    assert intent.intent == "price_percent_change"
    assert intent.scope["service"] == "ягодицы"
    assert intent.operation["value"] == 10
    assert intent.parser_source == "fallback"


def test_rag_admin_intent_parser_uses_llm_json_and_falls_back_on_timeout() -> None:
    def llm(prompt: str) -> str:
        assert "structured intent" in prompt or "structured intent" in prompt.lower()
        return json.dumps(
            {
                "intent": "effect_duration_update",
                "confidence": 0.91,
                "scope": {"product": "Tesoro Body"},
                "operation": {"type": "set_effect_duration", "value": "3 лет"},
                "risk_flags": ["effect_duration"],
            },
            ensure_ascii=False,
        )

    parsed = RagAdminIntentParser(llm=llm).parse("Tesoro теперь до 3 лет")

    assert parsed.intent == "effect_duration_update"
    assert parsed.operation["value"] == "3 лет"
    assert parsed.parser_source == "llm"

    def timeout(_prompt: str) -> str:
        raise TimeoutError("slow")

    fallback = RagAdminIntentParser(llm=timeout).parse("Tesoro теперь до 3 лет")

    assert fallback.intent == "effect_duration_update"
    assert fallback.parser_source == "fallback"


def test_openrouter_intent_client_extracts_message_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"intent": "policy_update", "confidence": 0.9}, ensure_ascii=False)
                        }
                    }
                ]
            },
        )

    client = OpenRouterIntentClient(
        api_key="key",
        model="model",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://openrouter.test"),
    )

    raw = client("Команда")

    assert json.loads(raw)["intent"] == "policy_update"
    assert requests
    assert json.loads(requests[0].content)["response_format"] == {"type": "json_object"}


def test_expert_rag_admin_exact_price_and_service_lifecycle(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    original = store.upsert_from_handoff(
        question="Сколько стоит увеличение ягодиц 600 мл?",
        answer_client="600 мл 100 000.",
        status=APPROVED,
        approved_by="olga",
        metadata={"service_key": "ягодицы", "autoanswer_allowed": True},
    )
    service = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl", service_catalog=catalog)

    add_plan = service.plan_change("Добавь услугу ягодицы", actor="olga")
    service.apply_plan(add_plan.id, actor="olga")
    assert catalog.resolve("по попе") is not None
    price_plan = service.plan_change("Для ягодиц 600 мл теперь 120 000", actor="olga")
    applied = service.apply_plan(price_plan.id, actor="olga")
    created = store.get(applied.metadata["created_ids"][0])
    assert created is not None
    assert "120 000" in created.answer_client
    assert store.get(original.id).status == "deprecated"  # type: ignore[union-attr]

    disable_plan = service.plan_change("Больше не делаем ягодицы", actor="olga")
    service.apply_plan(disable_plan.id, actor="olga")
    assert catalog.get("ягодицы").status == HIDDEN  # type: ignore[union-attr]


def test_service_delete_soft_deletes_and_excludes_from_shared_retrieval(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    catalog.upsert(service_key="ягодицы", title="Ягодицы", aliases=("ягодицы", "попа"), status=ACTIVE)
    answer = store.upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Для ягодиц используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
        metadata={"service_key": "ягодицы", "autoanswer_allowed": True},
    )
    service = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl", service_catalog=catalog)

    delete_plan = service.plan_change("Удали услугу ягодицы", actor="olga")
    service.apply_plan(delete_plan.id, actor="olga")
    result = RagRetrievalService(store, catalog).retrieve(RagRetrievalRequest(channel="avito", text="Какой препарат для ягодиц?", min_score=0.1))

    assert catalog.get("ягодицы").status == DELETED  # type: ignore[union-attr]
    assert store.get(answer.id).status == "deprecated"  # type: ignore[union-attr]
    assert result.answers == ()


def test_service_catalog_seed_and_rag_service_key_migration(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    catalog.seed_defaults()
    answer = store.upsert_from_handoff(
        question="Сколько держится Tesoro Body для попы?",
        answer_client="Tesoro Body для ягодиц держится до 4 лет.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )

    plan = catalog.plan_expert_rag_service_key_migration(store, limit=100)
    updated_ids = catalog.apply_expert_rag_service_key_migration(store, plan)
    updated = store.get(answer.id)

    assert catalog.resolve("по попе").service_key == "yagodicy"  # type: ignore[union-attr]
    assert updated_ids == [answer.id]
    assert updated is not None
    assert updated.metadata["service_key"] == "yagodicy"


def test_shared_rag_retrieval_filters_by_channel_visibility_and_policy(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    catalog.upsert(service_key="guby", title="Губы", aliases=("губы",), visibility=("avito", "telegram_client"))
    store.upsert_from_handoff(
        question="Какой препарат для губ?",
        answer_client="Для губ используем препарат test.",
        status=APPROVED,
        approved_by="olga",
        metadata={"service_key": "guby", "autoanswer_allowed": True},
    )
    retrieval = RagRetrievalService(store, catalog)

    avito = retrieval.retrieve(RagRetrievalRequest(channel="avito", text="Какой препарат для губ?", min_score=0.1))
    telegram = retrieval.retrieve(RagRetrievalRequest(channel="telegram_client", text="Какой препарат для губ?", min_score=0.1))
    vk = retrieval.retrieve(RagRetrievalRequest(channel="vk", text="Какой препарат для губ?", min_score=0.1))

    assert avito.answers
    assert telegram.answers
    assert vk.answers == ()


def test_shared_rag_retrieval_can_feed_avito_vk_and_telegram_clients(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    catalog.upsert(
        service_key="yagodicy",
        title="Ягодицы",
        aliases=("ягодицы", "попа", "tesoro"),
        visibility=("avito", "telegram_client", "vk"),
    )
    store.upsert_from_handoff(
        question="Сколько держится Tesoro Body для ягодиц?",
        answer_client="Tesoro Body для ягодиц держится до 4 лет.",
        status=APPROVED,
        approved_by="olga",
        metadata={"service_key": "yagodicy", "autoanswer_allowed": True},
    )
    retrieval = RagRetrievalService(store, catalog)

    results = [
        retrieval.retrieve(RagRetrievalRequest(channel=channel, text="Сколько держится Tesoro Body для ягодиц?", min_score=0.1))
        for channel in ("avito", "telegram_client", "vk")
    ]

    assert all(result.answers for result in results)
    assert {result.answers[0]["answer_client"] for result in results} == {"Tesoro Body для ягодиц держится до 4 лет."}
    assert all(result.safe_for_autoanswer for result in results)


def test_shared_rag_retrieval_blocks_unsafe_autoanswer_but_keeps_similar_answers(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    catalog.upsert(service_key="guby", title="Губы", aliases=("губы",), visibility=("avito",))
    store.upsert_from_handoff(
        question="Сколько стоят губы?",
        answer_client="Губы стоят 20 000.",
        status=APPROVED,
        approved_by="olga",
        metadata={"service_key": "guby", "autoanswer_allowed": True},
    )

    result = RagRetrievalService(store, catalog).retrieve(
        RagRetrievalRequest(channel="avito", text="После губ аллергия, сколько стоят губы?", min_score=0.0)
    )

    assert result.safe_for_autoanswer is False
    assert result.handoff_reason == "risk_case"
    assert "risk_case" in result.conflicts
    assert result.answers


def test_shared_rag_retrieval_blocks_temporal_autoanswer_without_expiry(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    catalog.upsert(service_key="guby", title="Губы", aliases=("губы",), visibility=("avito",))
    store.upsert_from_handoff(
        question="Есть окно на губы завтра?",
        answer_client="Завтра есть окно на 15:00.",
        status=APPROVED,
        approved_by="olga",
        metadata={"service_key": "guby", "autoanswer_allowed": True},
    )

    result = RagRetrievalService(store, catalog).retrieve(RagRetrievalRequest(channel="avito", text="Есть окно завтра на губы?", min_score=0.0))

    assert result.answers == ()
    assert result.safe_for_autoanswer is False
    assert result.handoff_reason == "no_approved_knowledge"


def test_shared_rag_retrieval_blocks_aesthetic_volume_result_promise(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    catalog.upsert(service_key="grud", title="Грудь", aliases=("грудь",), visibility=("avito",))
    store.upsert_from_handoff(
        question="300 мл даст плюс один размер груди?",
        answer_client="300 мл по груди даст заметный результат и примерно +1 размер.",
        status=APPROVED,
        approved_by="olga",
        metadata={"service_key": "grud", "autoanswer_allowed": True},
    )

    result = RagRetrievalService(store, catalog).retrieve(
        RagRetrievalRequest(channel="avito", text="300 мл даст плюс один размер груди?", min_score=0.0)
    )

    assert result.answers == ()
    assert result.safe_for_autoanswer is False
    assert result.handoff_reason == "no_approved_knowledge"


def test_shared_rag_retrieval_allows_price_only_volume_answer(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    catalog = ServiceCatalogStore(tmp_path / "services.json")
    catalog.upsert(service_key="yagodicy", title="Ягодицы", aliases=("ягодицы",), visibility=("avito",))
    store.upsert_from_handoff(
        question="Сколько стоит 300 мл ягодицы?",
        answer_client="300 мл Tesoro Body стоит 75 000.",
        status=APPROVED,
        approved_by="olga",
        metadata={"service_key": "yagodicy", "autoanswer_allowed": True},
    )

    result = RagRetrievalService(store, catalog).retrieve(
        RagRetrievalRequest(channel="avito", text="Сколько стоит 300 мл ягодицы?", min_score=0.0)
    )

    assert result.answers
    assert result.safe_for_autoanswer is True


@pytest.mark.anyio
async def test_telegram_rag_plan_cancel_and_details_callbacks(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []
            self.callbacks = []

        def send_message(self, chat_id, text, **delivery_params):
            self.messages.append((chat_id, text, delivery_params))

        def answer_callback_query(self, callback_id, text):
            self.callbacks.append((callback_id, text))

    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    store.upsert_from_handoff(
        question="Сколько стоит увеличение ягодиц 600 мл?",
        answer_client="600 мл 100 000.",
        status=APPROVED,
        approved_by="olga",
    )
    rag_admin = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl")
    plan = rag_admin.plan_change("подними цены на ягодицы на 10%")
    settings = replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3")
    toolbox = AutomationToolbox(DryRunYClientsGateway(), expert_rag_admin=rag_admin)
    transport = TelegramAdminBotTransport(FakeBot(), CodexTelegramAdminService(toolbox, settings), settings)
    update = {
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 1},
            "data": f"ragplan:{plan.id}:details",
            "message": {"chat": {"id": "10"}},
        }
    }

    details = await transport.handle_callback_update(update)
    update["callback_query"]["id"] = "cb-2"
    update["callback_query"]["data"] = f"ragplan:{plan.id}:cancel"
    cancelled = await transport.handle_callback_update(update)

    assert details["ok"] is True
    assert cancelled["ok"] is True
    assert rag_admin.get_plan(plan.id).status == "cancelled"  # type: ignore[union-attr]
    assert store.list_answers(status=APPROVED, limit=10)[0].answer_client == "600 мл 100 000."


@pytest.mark.anyio
async def test_automation_toolbox_expert_rag_plan_and_apply(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    store.upsert_from_handoff(
        question="Сколько стоит увеличение ягодиц 600 мл?",
        answer_client="600 мл 100 000.",
        status=APPROVED,
        approved_by="olga",
    )
    rag_admin = ExpertRagAdminService(store, plans_path=tmp_path / "plans.json", audit_path=tmp_path / "audit.jsonl")
    toolbox = AutomationToolbox(DryRunYClientsGateway(), expert_rag_admin=rag_admin)

    planned = await toolbox.execute("expert_rag.plan_change", {"command": "подними цены на ягодицы на 10%"})

    assert planned.ok
    plan_id = planned.data["plan"]["id"]
    assert parse_rag_admin_callback(f"ragplan:{plan_id}:apply") == (plan_id, "apply")
    applied = await toolbox.execute("expert_rag.apply_plan", {"plan_id": plan_id})
    assert applied.ok
    assert applied.data["plan"]["status"] == "applied"


@pytest.mark.anyio
async def test_avito_consultant_excludes_autoanswer_disabled_expert_rag(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    store.upsert_from_handoff(
        question="Что говорить про очную консультацию?",
        answer_client="Не рекомендовать очную консультацию по умолчанию.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": False},
    )
    consultant = AvitoConsultant(
        AutomationToolbox(DryRunYClientsGateway()),
        expert_rag=store,
        rag_autoanswer_threshold=0.1,
        rag_handoff_threshold=0.1,
    )
    message = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-policy-rag",
        chat_id="chat-policy-rag",
        text="Нужна очная консультация?",
    )

    reply = await consultant.respond(message)

    assert reply.action != "expert_rag_answer"


@pytest.mark.anyio
async def test_avito_consultant_answers_from_high_confidence_expert_rag(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    store.upsert_from_handoff(
        question="Какой филлер используется для ягодиц?",
        answer_client="Для ягодиц используем препарат Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    consultant = AvitoConsultant(
        AutomationToolbox(DryRunYClientsGateway()),
        expert_rag=store,
        rag_autoanswer_threshold=0.2,
        rag_handoff_threshold=0.1,
    )
    message = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-rag",
        chat_id="chat-rag",
        text="Какой препарат для увеличения ягодиц?",
    )

    reply = await consultant.respond(message)

    assert reply.action == "expert_rag_answer"
    assert "Tesoro Body" in reply.reply
    assert reply.handoff is None


@pytest.mark.anyio
async def test_high_confidence_rag_bypasses_codex_planner(tmp_path) -> None:
    class ExplodingPlanner:
        async def respond(self, context, toolbox):
            raise AssertionError("planner should not be called for safe high-confidence RAG")

    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    store.upsert_from_handoff(
        question="Сколько держится Tesoro Body для ягодиц?",
        answer_client="Tesoro Body для ягодиц держится до 4 лет.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True, "service_key": "yagodicy"},
    )
    consultant = AvitoConsultant(
        AutomationToolbox(DryRunYClientsGateway()),
        planner=ExplodingPlanner(),
        expert_rag=store,
        rag_autoanswer_threshold=0.2,
        rag_handoff_threshold=0.1,
    )

    reply = await consultant.respond(
        InboundMessage(
            channel=Channel.AVITO,
            client_id="client-rag-bypass",
            chat_id="chat-rag-bypass",
            text="Сколько держится Tesoro Body для ягодиц?",
        )
    )

    assert reply.action == "expert_rag_answer"
    assert "до 4 лет" in reply.reply
    assert reply.metadata["planner"] == "client_router"


@pytest.mark.anyio
async def test_avito_consultant_does_not_autoanswer_high_risk_expert_rag(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    store.upsert_from_handoff(
        question="Можно делать процедуру после операции?",
        answer_client="После операции процедуру можно делать только после очного разрешения врача.",
        status=APPROVED,
        approved_by="olga",
    )
    consultant = AvitoConsultant(
        AutomationToolbox(DryRunYClientsGateway()),
        expert_rag=store,
        rag_autoanswer_threshold=0.2,
        rag_handoff_threshold=0.1,
    )
    message = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-risk-rag",
        chat_id="chat-risk-rag",
        text="Можно делать процедуру после операции?",
    )

    reply = await consultant.respond(message)

    assert reply.action != "expert_rag_answer"


@pytest.mark.anyio
async def test_avito_consultant_does_not_send_high_risk_expert_rag_to_planner(tmp_path) -> None:
    seen_payload: dict[str, object] = {}

    async def fake_codex_runner(payload, toolbox):
        del toolbox
        seen_payload.update(payload)
        return None

    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    store.upsert_from_handoff(
        question="Можно делать процедуру после операции?",
        answer_client="После операции процедуру можно делать только после разрешения врача.",
        status=APPROVED,
        approved_by="olga",
    )
    consultant = AvitoConsultant(
        AutomationToolbox(DryRunYClientsGateway()),
        planner=CodexAvitoPlanner(fake_codex_runner),
        expert_rag=store,
        rag_autoanswer_threshold=0.2,
        rag_handoff_threshold=0.1,
    )
    message = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-risk-planner",
        chat_id="chat-risk-planner",
        text="Можно делать процедуру после операции?",
    )

    await consultant.respond(message)

    assert seen_payload["retrieved_expert_answers"] == []


def test_expert_rag_price_or_medical_answer_requires_review(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    item = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        approved_by="olga",
    )

    assert item.status == NEEDS_REVIEW
    assert store.search("сколько стоит увеличение губ") == []
    approved = store.approve(item.id, approved_by="olga")
    assert approved.status == APPROVED
    assert store.search("сколько стоит увеличение губ", min_score=0.1)


def test_expert_rag_review_cli_lists_shows_and_updates_items(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    store = ExpertRagStore(db_path)
    review_item = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        status=NEEDS_REVIEW,
    )
    stale_item = store.upsert_from_handoff(
        question="Старая цена на ягодицы?",
        answer_client="Старая цена больше не актуальна.",
        status=NEEDS_REVIEW,
    )

    code, output = run_review_command(["--db", str(db_path), "list"])
    assert code == 0
    assert f"#{review_item.id}" in output
    assert "Сколько стоит" in output or "сколько стоит" in output

    code, output = run_review_command(["--db", str(db_path), "show", str(review_item.id)])
    assert code == 0
    assert "Client answer:" in output
    assert "10000" in output

    code, output = run_review_command(
        ["--db", str(db_path), "--audit-log", str(audit_path), "--json", "approve", str(review_item.id), "--by", "olga"]
    )
    payload = json.loads(output)
    assert code == 0
    assert payload["ok"] is True
    assert payload["item"]["status"] == APPROVED
    assert payload["item"]["approved_by"] == "olga"

    code, output = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "deprecate", str(stale_item.id)])
    assert code == 0
    assert "Deprecated" in output
    assert store.get(stale_item.id).status == "deprecated"  # type: ignore[union-attr]


def test_expert_rag_review_cli_dry_run_does_not_mutate_items(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    store = ExpertRagStore(db_path)
    approve_item = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        status=NEEDS_REVIEW,
    )
    deprecate_item = store.upsert_from_handoff(
        question="Старая цена?",
        answer_client="Старая цена не актуальна.",
        status=NEEDS_REVIEW,
    )

    code, output = run_review_command(["--db", str(db_path), "approve", str(approve_item.id), "--by", "olga", "--dry-run"])
    assert code == 0
    assert "DRY RUN" in output
    assert "would become approved" in output
    assert store.get(approve_item.id).status == NEEDS_REVIEW  # type: ignore[union-attr]

    code, output = run_review_command(["--db", str(db_path), "--json", "deprecate", str(deprecate_item.id), "--dry-run"])
    payload = json.loads(output)
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["action"] == "deprecate"
    assert store.get(deprecate_item.id).status == NEEDS_REVIEW  # type: ignore[union-attr]


def test_expert_rag_review_cli_writes_audit_log_for_mutations_only(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    store = ExpertRagStore(db_path)
    approve_item = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        status=NEEDS_REVIEW,
    )
    deprecate_item = store.upsert_from_handoff(
        question="Старая цена?",
        answer_client="Старая цена не актуальна.",
        status=NEEDS_REVIEW,
    )

    code, _ = run_review_command(
        ["--db", str(db_path), "--audit-log", str(audit_path), "approve", str(approve_item.id), "--by", "olga", "--dry-run"]
    )
    assert code == 0
    assert not audit_path.exists()

    code, _ = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "approve", str(approve_item.id), "--by", "olga"])
    assert code == 0
    code, _ = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "deprecate", str(deprecate_item.id)])
    assert code == 0

    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [event["action"] for event in events] == ["approve", "deprecate"]
    assert events[0]["item_id"] == approve_item.id
    assert events[0]["approved_by"] == "olga"
    assert events[0]["previous"]["status"] == NEEDS_REVIEW
    assert events[0]["current"]["status"] == APPROVED
    assert events[1]["item_id"] == deprecate_item.id
    assert events[1]["previous"]["status"] == NEEDS_REVIEW
    assert events[1]["current"]["status"] == "deprecated"


def test_expert_rag_review_cli_custom_db_defaults_audit_log_next_to_db(tmp_path) -> None:
    db_path = tmp_path / "nested" / "expert.sqlite3"
    store = ExpertRagStore(db_path)
    item = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        status=NEEDS_REVIEW,
    )

    assert resolve_audit_log_path(db_path) == db_path.parent / DEFAULT_AUDIT_LOG_PATH.name

    code, _ = run_review_command(["--db", str(db_path), "approve", str(item.id), "--by", "olga"])

    assert code == 0
    audit_path = db_path.parent / DEFAULT_AUDIT_LOG_PATH.name
    assert audit_path.exists()
    event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["item_id"] == item.id
    assert event["action"] == "approve"


def test_expert_rag_review_cli_reads_audit_log(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    store = ExpertRagStore(db_path)
    first = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        status=NEEDS_REVIEW,
    )
    second = store.upsert_from_handoff(
        question="Старая цена?",
        answer_client="Старая цена не актуальна.",
        status=NEEDS_REVIEW,
    )

    code, _ = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "approve", str(first.id), "--by", "olga"])
    assert code == 0
    audit_path.write_text(audit_path.read_text(encoding="utf-8") + "{bad json\n", encoding="utf-8")
    code, _ = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "deprecate", str(second.id)])
    assert code == 0

    code, output = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "audit", "--limit", "2"])
    assert code == 0
    assert "Expert RAG audit events: 2" in output
    assert f"#{second.id} deprecate: needs_review -> deprecated" in output
    assert f"#{first.id} approve: needs_review -> approved by olga" in output
    assert output.index(f"#{second.id}") < output.index(f"#{first.id}")

    code, output = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "--json", "audit", "--limit", "1"])
    payload = json.loads(output)
    assert code == 0
    assert len(payload["events"]) == 1
    assert payload["events"][0]["item_id"] == second.id


def test_expert_rag_review_cli_exports_backlog_for_approval(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    export_path = tmp_path / "review.md"
    store = ExpertRagStore(db_path)
    item = store.upsert_from_handoff(
        question="Сколько держится эффект?",
        answer_client="Эффект сохраняется до 4 лет.",
        status=NEEDS_REVIEW,
        source_chat_id="chat-1",
        source_message_id="msg-1",
        olga_reply_message_id="olga-1",
    )

    code, output = run_review_command(["--db", str(db_path), "export"])
    assert code == 0
    assert "# Expert RAG review backlog: needs_review" in output
    assert f"## #{item.id}" in output
    assert "Candidate client answer:" in output
    assert "Review suggestion:" in output
    assert "Suggested action: `needs_edit`" in output
    assert "contains_effect_duration_claim" in output
    assert "Decision checklist:" in output
    assert f"- [ ] approve #{item.id} as-is" in output
    assert f"- [ ] deprecate #{item.id}" in output
    assert f"- [ ] needs edited answer for #{item.id}" in output
    assert "Edited client answer, if needed:" in output
    assert f"approve {item.id} --by olga --dry-run" in output
    assert f"approve {item.id} --by olga" in output
    assert f"deprecate {item.id} --dry-run" in output
    assert f"deprecate {item.id}" in output

    code, output = run_review_command(["--db", str(db_path), "--json", "export"])
    payload = json.loads(output)
    assert code == 0
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == item.id
    assert payload["items"][0]["review_suggestion"]["suggested_action"] == "needs_edit"
    assert "contains_effect_duration_claim" in payload["items"][0]["review_suggestion"]["reasons"]

    code, output = run_review_command(["--db", str(db_path), "export", "--output", str(export_path)])
    assert code == 0
    assert "Exported 1 expert RAG items" in output
    assert f"## #{item.id}" in export_path.read_text(encoding="utf-8")


def test_expert_rag_review_suggestion_flags_context_sensitive_price_items(tmp_path) -> None:
    store = ExpertRagStore(tmp_path / "expert.sqlite3")
    item = store.upsert_from_handoff(
        question="Клиент прислал несколько сообщений подряд: какая будет цена на 700 мл?",
        answer_client="700мл 115 000 как модель, 160 000 как пациент.",
        answer_internal="Нужна ручная консультация\nКонтекст: клиент спрашивал по фото-примеру.",
        status=NEEDS_REVIEW,
        metadata={"source": "telegram_olga_history_import"},
    )

    suggestion = review_suggestion(item)
    code, list_output = run_review_command(["--db", str(store.path), "list"])
    code_show, show_output = run_review_command(["--db", str(store.path), "show", str(item.id)])

    assert suggestion["suggested_action"] == "needs_edit"
    assert "contains_price_or_commercial_terms" in suggestion["reasons"]
    assert "contains_volume_ml" in suggestion["reasons"]
    assert "case_specific_context" in suggestion["reasons"]
    assert "legacy_handoff_card_context" in suggestion["reasons"]
    assert code == 0
    assert "Suggestion: needs_edit" in list_output
    assert code_show == 0
    assert "Review suggestion:" in show_output
    assert "suggested_action=needs_edit" in show_output


def test_expert_rag_review_cli_dry_runs_markdown_decisions(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    review_path = tmp_path / "review.md"
    store = ExpertRagStore(db_path)
    approve_item = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        status=NEEDS_REVIEW,
    )
    deprecate_item = store.upsert_from_handoff(
        question="Старая цена?",
        answer_client="Старая цена не актуальна.",
        status=NEEDS_REVIEW,
    )
    review_path.write_text(
        "\n".join(
            [
                f"- [x] approve #{approve_item.id} as-is",
                f"- [X] deprecate #{deprecate_item.id}",
            ]
        ),
        encoding="utf-8",
    )

    code, output = run_review_command(["--db", str(db_path), "--json", "decisions", str(review_path)])
    payload = json.loads(output)

    assert code == 0
    assert payload["dry_run"] is True
    assert payload["applied"] is False
    assert [entry["action"] for entry in payload["planned"]] == ["approve", "deprecate"]
    assert store.get(approve_item.id).status == NEEDS_REVIEW  # type: ignore[union-attr]
    assert store.get(deprecate_item.id).status == NEEDS_REVIEW  # type: ignore[union-attr]


def test_expert_rag_review_cli_applies_markdown_decisions_with_audit(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    review_path = tmp_path / "review.md"
    store = ExpertRagStore(db_path)
    approve_item = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        status=NEEDS_REVIEW,
    )
    deprecate_item = store.upsert_from_handoff(
        question="Старая цена?",
        answer_client="Старая цена не актуальна.",
        status=NEEDS_REVIEW,
    )
    review_path.write_text(
        f"- [x] approve #{approve_item.id} as-is\n- [x] deprecate #{deprecate_item.id}\n",
        encoding="utf-8",
    )

    code, output = run_review_command(
        ["--db", str(db_path), "--audit-log", str(audit_path), "decisions", str(review_path), "--apply", "--by", "olga"]
    )

    assert code == 0
    assert "APPLY" in output
    assert "applied" in output
    assert store.get(approve_item.id).status == APPROVED  # type: ignore[union-attr]
    assert store.get(deprecate_item.id).status == "deprecated"  # type: ignore[union-attr]
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [event["action"] for event in events] == ["approve", "deprecate"]
    assert events[0]["approved_by"] == "olga"


def test_expert_rag_review_cli_rejects_conflicting_markdown_decisions(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    review_path = tmp_path / "review.md"
    store = ExpertRagStore(db_path)
    item = store.upsert_from_handoff(
        question="Сколько стоит увеличение губ?",
        answer_client="Увеличение губ стоит 10000 ₽.",
        status=NEEDS_REVIEW,
    )
    edit_item = store.upsert_from_handoff(
        question="Какой объём нужен?",
        answer_client="Нужно смотреть фото.",
        status=NEEDS_REVIEW,
    )
    review_path.write_text(
        "\n".join(
            [
                f"- [x] approve #{item.id} as-is",
                f"- [x] deprecate #{item.id}",
                f"- [x] needs edited answer for #{edit_item.id}",
            ]
        ),
        encoding="utf-8",
    )

    code, output = run_review_command(
        ["--db", str(db_path), "--audit-log", str(audit_path), "decisions", str(review_path), "--apply"]
    )

    assert code == 1
    assert "Conflicts: 1" in output
    assert "Needs edited answer: 1" in output
    assert "No changes were applied" in output
    assert store.get(item.id).status == NEEDS_REVIEW  # type: ignore[union-attr]
    assert store.get(edit_item.id).status == NEEDS_REVIEW  # type: ignore[union-attr]
    assert not audit_path.exists()


def test_expert_rag_review_temporal_cleanup_dry_run_and_apply(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    store = ExpertRagStore(db_path)
    stale = store.upsert_from_handoff(
        question="Можно завтра записаться?",
        answer_client="Завтра есть окно на 15:00.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )
    blocked = store.upsert_from_handoff(
        question="Адрес завтра?",
        answer_client="Завтра адрес уточняем отдельно.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": False},
    )

    dry_code, dry_output = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "temporal-cleanup"])
    apply_code, apply_output = run_review_command(["--db", str(db_path), "--audit-log", str(audit_path), "temporal-cleanup", "--apply"])
    updated_stale = store.get(stale.id)
    updated_blocked = store.get(blocked.id)

    assert dry_code == 0
    assert "DRY RUN" in dry_output
    assert f"#{stale.id}" in dry_output
    assert apply_code == 0
    assert "APPLY" in apply_output
    assert updated_stale is not None
    assert updated_stale.metadata["autoanswer_allowed"] is False
    assert updated_stale.metadata["temporal_fact"] is True
    assert updated_stale.metadata["autoanswer_block_reason"] == "temporal_without_expiry"
    assert updated_blocked is not None
    assert updated_blocked.metadata["autoanswer_allowed"] is False
    assert audit_path.exists()
    assert "temporal_cleanup" in audit_path.read_text(encoding="utf-8")


def test_expert_rag_review_temporal_cleanup_exports_markdown_without_mutation(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    output_path = tmp_path / "temporal_cleanup.md"
    store = ExpertRagStore(db_path)
    item = store.upsert_from_handoff(
        question="Когда есть окно на губы в Ростове?",
        answer_client="Завтра есть окно на 15:00 в Ростове.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )

    code, output = run_review_command(["--db", str(db_path), "temporal-cleanup", "--output", str(output_path)])
    markdown = output_path.read_text(encoding="utf-8")
    unchanged = store.get(item.id)

    assert code == 0
    assert "Exported temporal RAG cleanup report" in output
    assert "# Expert RAG Temporal Cleanup" in markdown
    assert f"#{item.id}" in markdown
    assert "Checklist before `--apply`" in markdown
    assert "Завтра есть окно на 15:00 в Ростове." in markdown
    assert unchanged is not None
    assert unchanged.metadata["autoanswer_allowed"] is True


def test_expert_rag_review_temporal_cleanup_markdown_decisions_dry_run_and_apply(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    decisions_path = tmp_path / "temporal_cleanup.md"
    store = ExpertRagStore(db_path)
    item = store.upsert_from_handoff(
        question="Когда есть окно на губы?",
        answer_client="Завтра есть окно на 15:00.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )
    decisions_path.write_text(
        f"- [x] block_autoanswer #{item.id}: разовая запись на завтра\n",
        encoding="utf-8",
    )

    dry_code, dry_output = run_review_command(
        ["--db", str(db_path), "--audit-log", str(audit_path), "temporal-cleanup", "--decisions", str(decisions_path)]
    )
    unchanged = store.get(item.id)
    apply_code, apply_output = run_review_command(
        ["--db", str(db_path), "--audit-log", str(audit_path), "temporal-cleanup", "--decisions", str(decisions_path), "--apply"]
    )
    updated = store.get(item.id)

    assert dry_code == 0
    assert "DRY RUN" in dry_output
    assert "sets autoanswer_allowed=false" in dry_output
    assert unchanged is not None
    assert unchanged.metadata["autoanswer_allowed"] is True
    assert apply_code == 0
    assert "APPLY" in apply_output
    assert updated is not None
    assert updated.metadata["autoanswer_allowed"] is False
    assert updated.metadata["temporal_cleanup_decision"] == "block_autoanswer"
    assert updated.metadata["temporal_cleanup_decision_note"] == "разовая запись на завтра"
    assert "temporal_cleanup_decision" in audit_path.read_text(encoding="utf-8")


def test_expert_rag_review_temporal_cleanup_decisions_reject_invalid_without_partial_apply(tmp_path) -> None:
    db_path = tmp_path / "expert.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    decisions_path = tmp_path / "temporal_cleanup.md"
    store = ExpertRagStore(db_path)
    first = store.upsert_from_handoff(
        question="Когда есть окно?",
        answer_client="Завтра есть окно на 15:00.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )
    second = store.upsert_from_handoff(
        question="Адрес завтра?",
        answer_client="Адрес завтра уточняем отдельно.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )
    decisions_path.write_text(
        "\n".join(
            [
                f"- [x] block_autoanswer #{first.id}: разовая запись",
                f"- [x] block_autoanswer #{second.id}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    code, output = run_review_command(
        ["--db", str(db_path), "--audit-log", str(audit_path), "temporal-cleanup", "--decisions", str(decisions_path), "--apply"]
    )
    first_after = store.get(first.id)
    second_after = store.get(second.id)

    assert code == 1
    assert "Missing reasons: 1" in output
    assert "No changes were applied" in output
    assert first_after is not None
    assert second_after is not None
    assert first_after.metadata["autoanswer_allowed"] is True
    assert second_after.metadata["autoanswer_allowed"] is True
    assert not audit_path.exists()


def test_mentor_memory_stores_olga_handoff_answer_in_expert_rag(tmp_path) -> None:
    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    expert = ExpertRagStore(tmp_path / "expert.sqlite3")
    memory = MentorMemoryService(knowledge, expert_rag=expert)

    result = memory.observe_avito_send(
        chat_id="chat-1",
        text="Для ягодиц используем препарат Tesoro Body.",
        actor="olga",
        context={"client_message": "Какой филлер используется для ягодиц?"},
    )

    assert result.expert_answers
    matches = expert.search("какой препарат для увеличения ягодиц", min_score=0.1)
    assert matches
    assert "Tesoro Body" in matches[0][0].answer_client


def test_mentor_memory_blocks_temporal_olga_answer_from_autoanswer(tmp_path) -> None:
    knowledge = JsonKnowledgeStore(tmp_path / "knowledge.json")
    expert = ExpertRagStore(tmp_path / "expert.sqlite3")
    memory = MentorMemoryService(knowledge, expert_rag=expert)

    result = memory.observe_avito_send(
        chat_id="chat-1",
        text="Да, запись завтра есть на 15:00, адрес Ленина 1.",
        actor="olga",
        context={"client_message": "Можно записаться завтра?"},
    )

    assert result.expert_answers
    answer = result.expert_answers[0]
    assert answer.metadata["autoanswer_allowed"] is False
    assert answer.metadata["temporal_fact"] is True
    assert answer.metadata["autoanswer_block_reason"] == "temporal_without_expiry"


@pytest.mark.anyio
async def test_unanswered_monitor_skips_messages_before_activation(tmp_path) -> None:
    chat = {"id": "chat-1", "users": [{"id": 10, "name": "Анна"}]}
    message = {"id": "m1", "author_id": 10, "direction": "in", "type": "text", "created": 100, "content": {"text": "Здравствуйте"}}
    item = find_unanswered_avito_chat(
        account_id=1,
        chat=chat,
        messages=[message],
        now=2000,
        min_age_seconds=1200,
        lookback_seconds=5000,
    )

    assert item is not None
    assert item.created == 100
    state: dict[str, object] = {}
    stats = await autoreply_unanswered_once(settings=_settings(), items=[item], state=state, state_path=tmp_path / "state.json")

    assert stats["attempted"] == 0
    assert "activated_at" in state


def test_unanswered_monitor_report_marks_handled_items() -> None:
    chat = {"id": "chat-1", "users": [{"id": 10, "name": "Анна"}]}
    message = {"id": "m1", "author_id": 10, "direction": "in", "type": "text", "created": 100, "content": {"text": "Ок"}}
    item = find_unanswered_avito_chat(
        account_id=1,
        chat=chat,
        messages=[message],
        now=2000,
        min_age_seconds=1200,
        lookback_seconds=5000,
    )
    assert item is not None
    state = {
        "handled": {
            "1:chat-1:m1": {
                "handled_at": 1500,
                "result": {"ok": True, "ignored": True, "reason": "client_ack_after_pending_reply"},
            }
        }
    }

    row = report_unanswered_item(item, state)

    assert row["autoreply_state"] == "handled"
    assert row["needs_action"] is False
    assert row["ignored_reason"] == "client_ack_after_pending_reply"


def test_unanswered_monitor_report_reopens_stale_ack_for_actionable_text() -> None:
    chat = {"id": "chat-wait-address", "users": [{"id": 10, "name": "Анна"}]}
    message = {"id": "m1", "author_id": 10, "direction": "in", "type": "text", "created": 100, "content": {"text": "Жду адрес"}}
    item = find_unanswered_avito_chat(
        account_id=1,
        chat=chat,
        messages=[message],
        now=2000,
        min_age_seconds=1200,
        lookback_seconds=5000,
    )
    assert item is not None
    state = {
        "handled": {
            "1:chat-wait-address:m1": {
                "handled_at": 1500,
                "result": {"ok": True, "ignored": True, "reason": "client_ack_after_pending_reply"},
            }
        }
    }

    row = report_unanswered_item(item, state)

    assert row["autoreply_state"] == "pending"
    assert row["needs_action"] is True
    assert row["severity"] == "critical"


def test_unanswered_monitor_classifies_final_ack_as_not_actionable() -> None:
    chat = {"id": "chat-ack", "users": [{"id": 10, "name": "Анна"}]}
    for text in ("Спасибо большое", "Хорошо 🌸", "Спасибо не надо я хотела первый раз попробовать"):
        message = {"id": f"m-ack-{text}", "author_id": 10, "direction": "in", "type": "text", "created": 100, "content": {"text": text}}

        item = find_unanswered_avito_chat(
            account_id=1,
            chat=chat,
            messages=[message],
            now=2000,
            min_age_seconds=1200,
            lookback_seconds=5000,
        )

        assert item is not None
        assert item.needs_action is False
        assert item.severity == "low"
        assert item.reason == "final_ack"


def test_unanswered_monitor_flags_named_live_critical_cases() -> None:
    cases = {
        "u2i-GZU5PbnrTmNYotE_UZq1Cw": "Запишите, пожалуйста, на 15.00. Одного часа нам хватит? По какому адресу Вы принимаете?",
        "u2i-xVYkKKUHGevcp6wMOLrE2Q": "Жду адрес",
        "u2i-~gs~jkteogGr8oUFac9TEQ": "Жду",
        "u2i-CbskqJzftz74sTOVCg9atA": "Спасибо большое вы же в Геленджике?",
    }

    for chat_id, text in cases.items():
        item = find_unanswered_avito_chat(
            account_id=1,
            chat={"id": chat_id, "users": [{"id": 10, "name": "Клиент"}]},
            messages=[{"id": f"{chat_id}:m1", "author_id": 10, "direction": "in", "type": "text", "created": 100, "content": {"text": text}}],
            now=2000,
            min_age_seconds=1200,
            lookback_seconds=5000,
        )

        assert item is not None
        assert item.needs_action is True
        assert item.severity == "critical"


def test_unanswered_alert_mentions_actionable_and_critical_counts() -> None:
    chat = {"id": "chat-critical", "users": [{"id": 10, "name": "Анна"}]}
    message = {"id": "m-critical", "author_id": 10, "direction": "in", "type": "text", "created": 100, "content": {"text": "Жду адрес"}}
    item = find_unanswered_avito_chat(
        account_id=1,
        chat=chat,
        messages=[message],
        now=2000,
        min_age_seconds=1200,
        lookback_seconds=5000,
    )
    assert item is not None

    alert = format_unanswered_alert([item], max_items=10)

    assert "требуют действия: 1" in alert
    assert "критичных: 1" in alert
    assert "КРИТИЧНО" in alert


@pytest.mark.anyio
async def test_unanswered_monitor_paginates_chat_list_over_avito_limit() -> None:
    class FakeAvitoReader:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        async def list_chats(self, account_id: int, *, limit: int = 20, offset: int = 0) -> dict[str, object]:
            self.calls.append((limit, offset))
            chats = [{"id": f"chat-{index}", "users": [{"id": 10, "name": "Анна"}]} for index in range(offset, min(offset + limit, 150))]
            return {"chats": chats}

        async def get_chat_messages(self, account_id: int, chat_id: str, *, limit: int = 30, offset: int = 0) -> dict[str, object]:
            created = int(time.time()) - 2000
            return {
                "messages": [
                    {
                        "id": f"{chat_id}:m1",
                        "author_id": 10,
                        "direction": "in",
                        "type": "text",
                        "created": created,
                        "content": {"text": "Жду адрес"},
                    }
                ]
            }

        async def mark_chat_read(self, account_id: int, chat_id: str) -> dict[str, object]:
            return {"ok": True}

    reader = FakeAvitoReader()

    items = await audit_unanswered_once(
        settings=replace(_settings(), avito_account_ids=()),
        chat_limit=150,
        messages_per_chat=50,
        min_age_seconds=1200,
        lookback_seconds=5000,
        reader=reader,
    )

    assert reader.calls == [(100, 0), (50, 100)]
    assert len(items) == 150


def test_pending_followup_stays_open_after_client_ack_and_closes_on_final_answer() -> None:
    chat = {"id": "chat-followup", "users": [{"id": 10, "name": "Анна"}]}
    state: dict[str, object] = {}
    base_messages = [
        {"id": "m-client-1", "author_id": 10, "direction": "in", "type": "text", "created": 100, "content": {"text": "По какому адресу вы принимаете?"}},
        {"id": "m-bot-1", "author_id": 1, "direction": "out", "type": "text", "created": 160, "content": {"text": "Уточню точный адрес и вернусь с ответом."}},
        {"id": "m-client-2", "author_id": 10, "direction": "in", "type": "text", "created": 220, "content": {"text": "Хорошо, спасибо"}},
    ]

    sync_pending_followups(account_id=1, chat=chat, messages=base_messages, state=state, now=4000, reminder_seconds=1800, escalation_seconds=7200)
    rows = pending_followup_rows(state, now=4000)

    assert len(rows) == 1
    assert rows[0]["business_status"] == "overdue"
    assert rows[0]["business_resolved"] is False
    assert rows[0]["client_replied"] is True
    assert rows[0]["client_ack_after_promise"] is True
    assert rows[0]["last_client_message"] == "Хорошо, спасибо"

    resolved_messages = [
        *base_messages,
        {"id": "m-bot-2", "author_id": 1, "direction": "out", "type": "text", "created": 5000, "content": {"text": "Адрес: Геленджик, ул. Морская, 10. Можете приходить к 15:00."}},
    ]
    sync_pending_followups(account_id=1, chat=chat, messages=resolved_messages, state=state, now=5100, reminder_seconds=1800, escalation_seconds=7200)

    assert pending_followup_rows(state, now=5100) == []
    all_rows = pending_followup_rows(state, now=5100, include_resolved=True)
    assert all_rows[0]["business_status"] == "business_resolved"
    assert all_rows[0]["final_answer"].startswith("Адрес:")


def test_pending_followup_keeps_client_photo_urls_for_olga_card() -> None:
    chat = {"id": "chat-followup", "users": [{"id": 10, "name": "Анна"}]}
    state: dict[str, object] = {}
    messages = [
        {
            "id": "m-client-photo",
            "author_id": 10,
            "direction": "in",
            "type": "image",
            "created": 100,
            "content": {"text": "[фото]", "image": {"sizes": {"1280x960": "https://img.example/big.jpg"}}},
        },
        {
            "id": "m-bot-1",
            "author_id": 1,
            "direction": "out",
            "type": "text",
            "created": 160,
            "content": {"text": "Спасибо, фото получили. Уточним по зонам и стоимости, затем вернёмся с ответом."},
        },
    ]

    sync_pending_followups(account_id=1, chat=chat, messages=messages, state=state, now=4000, reminder_seconds=1800, escalation_seconds=7200)
    rows = pending_followup_rows(state, now=4000)

    assert rows[0]["last_client_message"] == "[фото]"
    assert rows[0]["last_client_photo_urls"] == ["https://img.example/big.jpg"]


def test_pending_followup_not_relevant_does_not_reopen_same_promise() -> None:
    chat = {"id": "chat-followup", "users": [{"id": 10, "name": "Анна"}]}
    messages = [
        {"id": "m-client-1", "author_id": 10, "direction": "in", "type": "text", "created": 100, "content": {"text": "Жду адрес"}},
        {"id": "m-bot-1", "author_id": 1, "direction": "out", "type": "text", "created": 160, "content": {"text": "Уточню точный адрес и вернусь с ответом."}},
    ]
    state: dict[str, object] = {}
    sync_pending_followups(account_id=1, chat=chat, messages=messages, state=state, now=4000, reminder_seconds=1800, escalation_seconds=7200)
    key = next(iter(state["pending_followups"]))  # type: ignore[index]
    state["pending_followups"][key].update(  # type: ignore[index]
        {
            "business_status": "not_relevant",
            "business_resolved": True,
            "close_reason": "not_relevant",
            "closed_by": "olga",
        }
    )

    sync_pending_followups(account_id=1, chat=chat, messages=messages, state=state, now=5000, reminder_seconds=1800, escalation_seconds=7200)

    assert pending_followup_rows(state, now=5000) == []
    all_rows = pending_followup_rows(state, now=5000, include_resolved=True)
    assert all_rows[0]["business_status"] == "not_relevant"
    assert all_rows[0]["business_resolved"] is True


def test_pending_followup_alert_includes_business_context_and_action() -> None:
    text = format_pending_followup_alert(
        [
            {
                "chat_id": "chat-followup",
                "client_name": "Анна",
                "business_status": "overdue",
                "severity": "critical",
                "age_seconds": 5400,
                "listing_city": "Геленджик",
                "listing_title": "Увеличение губ",
                "bot_promise": "Уточню точный адрес и напишу вам.",
                "last_client_message": "Жду адрес",
            }
        ],
        max_items=10,
    )

    assert "Анна" in text
    assert "Геленджик | Увеличение губ" in text
    assert "Обещал бот: Уточню точный адрес" in text
    assert "Последнее от клиента: Жду адрес" in text
    assert "Нужно сделать: дать клиенту финальный ответ" in text
    assert "chat_id: chat-followup" in text


def test_pending_followup_admin_action_closes_state_and_writes_audit(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    audit_path = tmp_path / "audit.jsonl"
    key = "1:chat-followup:m-bot-1"
    state_path.write_text(
        json.dumps(
            {
                "pending_followups": {
                    key: {
                        "account_id": 1,
                        "chat_id": "chat-followup",
                        "message_id": "m-bot-1",
                        "client_name": "Анна",
                        "business_status": "overdue",
                        "business_resolved": False,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    token = pending_followup_token(key)

    result = apply_pending_followup_action(
        state_path=state_path,
        token=token,
        action="stale",
        actor="olga",
        now=1780000000,
        audit_path=audit_path,
    )
    updated = json.loads(state_path.read_text(encoding="utf-8"))
    audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert result["ok"] is True
    assert updated["pending_followups"][key]["business_resolved"] is True
    assert updated["pending_followups"][key]["business_status"] == "not_relevant"
    assert updated["pending_followups"][key]["closed_by"] == "olga"
    assert audit[0]["action"] == "stale"
    assert audit[0]["chat_id"] == "chat-followup"
    assert parse_pending_followup_callback(f"avfu:{token}:done") == (token, "done")
    assert pending_followup_keyboard(key)["inline_keyboard"][0][0]["callback_data"] == f"avfu:{token}:done"
    button_texts = [
        button["text"]
        for row in pending_followup_keyboard(key)["inline_keyboard"]
        for button in row
    ]
    assert "Срочно" not in button_texts


def test_export_avito_followups_markdown_is_read_only_and_includes_decisions(tmp_path) -> None:
    report_path = tmp_path / "report.json"
    state_path = tmp_path / "state.json"
    key = "1:chat-followup:m-bot-1"
    report_path.write_text(
        json.dumps(
            {
                "pending_followups": [
                    {
                        "key": key,
                        "account_id": 1,
                        "chat_id": "chat-followup",
                        "message_id": "m-bot-1",
                        "client_name": "Анна",
                        "business_status": "overdue",
                        "business_resolved": False,
                        "overdue": True,
                        "severity": "critical",
                        "age_seconds": 7200,
                        "listing_city": "Геленджик",
                        "listing_title": "Увеличение губ",
                        "bot_promise": "Уточню адрес и напишу.",
                        "last_client_message": "Жду адрес",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"pending_followups": {key: {"chat_id": "chat-followup"}}}, ensure_ascii=False), encoding="utf-8")
    before = state_path.read_text(encoding="utf-8")

    report = build_avito_followups_export(report_path=report_path, state_path=state_path, now=1780000000)
    markdown = format_avito_followups_markdown(report)

    assert state_path.read_text(encoding="utf-8") == before
    assert report["pending_count"] == 1
    assert report["critical_count"] == 1
    assert report["overdue_count"] == 1
    assert "Жду адрес" in markdown
    assert f"resolved #{pending_followup_token(key)}" in markdown
    assert f"not_relevant #{pending_followup_token(key)}" in markdown
    assert parse_avito_followup_decisions(markdown) == []


def test_avito_followup_decisions_dry_run_and_apply_resolved_with_reason(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    report_path = tmp_path / "report.json"
    decisions_path = tmp_path / "avito_followups.md"
    audit_path = tmp_path / "audit.jsonl"
    key = "1:chat-followup:m-bot-1"
    token = pending_followup_token(key)
    state_path.write_text(
        json.dumps(
            {
                "pending_followups": {
                    key: {
                        "account_id": 1,
                        "chat_id": "chat-followup",
                        "message_id": "m-bot-1",
                        "client_name": "Анна",
                        "business_status": "overdue",
                        "business_resolved": False,
                        "overdue": True,
                        "severity": "critical",
                        "bot_promise": "Уточню адрес и подтвержу запись.",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(
            {
                "pending_followup_count": 1,
                "overdue_followup_count": 1,
                "critical_followup_count": 1,
                "pending_followups": [
                    {
                        "key": key,
                        "chat_id": "chat-followup",
                        "business_status": "overdue",
                        "business_resolved": False,
                        "overdue": True,
                        "severity": "critical",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    decisions_path.write_text(f"- [x] resolved #{token}: ответили в Avito в 12:10\n", encoding="utf-8")
    before = state_path.read_text(encoding="utf-8")
    report_before = report_path.read_text(encoding="utf-8")

    dry_run = build_avito_followup_decision_review(
        decisions_path=decisions_path,
        state_path=state_path,
        report_path=report_path,
        audit_path=audit_path,
        apply=False,
        now=1780000000,
    )
    after_dry_run = state_path.read_text(encoding="utf-8")
    report_after_dry_run = report_path.read_text(encoding="utf-8")
    applied = build_avito_followup_decision_review(
        decisions_path=decisions_path,
        state_path=state_path,
        report_path=report_path,
        audit_path=audit_path,
        apply=True,
        actor="olga",
        now=1780000100,
    )
    row = json.loads(state_path.read_text(encoding="utf-8"))["pending_followups"][key]
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert dry_run["ok"] is True
    assert dry_run["items"][0]["would_apply"] is True
    assert before == after_dry_run
    assert report_before == report_after_dry_run
    assert applied["ok"] is True
    assert applied["applied_count"] == 1
    assert row["business_resolved"] is True
    assert row["business_status"] == "manual_closed"
    assert row["client_answer_confirmed"] is True
    assert row["resolution_note"] == "ответили в Avito в 12:10"
    assert report["pending_followup_count"] == 0
    assert report["overdue_followup_count"] == 0
    assert report["critical_followup_count"] == 0
    assert report["pending_followups"][0]["business_resolved"] is True
    assert report["pending_followups"][0]["resolution_note"] == "ответили в Avito в 12:10"
    assert audit_path.exists()
    assert "ответили в Avito в 12:10" in audit_path.read_text(encoding="utf-8")


def test_avito_followup_decisions_reject_invalid_without_partial_apply(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    decisions_path = tmp_path / "avito_followups.md"
    first_key = "1:chat-followup:m-bot-1"
    second_key = "1:chat-other:m-bot-2"
    first_token = pending_followup_token(first_key)
    second_token = pending_followup_token(second_key)
    state = {
        "pending_followups": {
            first_key: {"chat_id": "chat-followup", "business_status": "overdue", "business_resolved": False},
            second_key: {"chat_id": "chat-other", "business_status": "overdue", "business_resolved": False},
        }
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    decisions_path.write_text(
        "\n".join(
            [
                f"- [x] not_relevant #{first_token}: клиент отказался",
                f"- [x] resolved #{second_token}",
            ]
        ),
        encoding="utf-8",
    )

    review = build_avito_followup_decision_review(
        decisions_path=decisions_path,
        state_path=state_path,
        audit_path=tmp_path / "audit.jsonl",
        apply=True,
        now=1780000000,
    )
    unchanged = json.loads(state_path.read_text(encoding="utf-8"))

    assert review["ok"] is False
    assert review["applied_count"] == 0
    assert any("requires a reason" in error for error in review["errors"])
    assert unchanged == state


def test_pending_followup_admin_keeps_old_urgent_callback_compatible_and_snoozes(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    key = "1:chat-followup:m-bot-1"
    state_path.write_text(
        json.dumps({"pending_followups": {key: {"chat_id": "chat-followup", "business_status": "overdue", "business_resolved": False}}}),
        encoding="utf-8",
    )
    token = pending_followup_token(key)

    urgent = apply_pending_followup_action(state_path=state_path, token=token, action="urgent", now=1780000000, audit_path=tmp_path / "audit.jsonl")
    later = apply_pending_followup_action(state_path=state_path, token=token, action="later", now=1780000100, audit_path=tmp_path / "audit.jsonl")
    updated = json.loads(state_path.read_text(encoding="utf-8"))["pending_followups"][key]

    assert urgent["ok"] is True
    assert later["ok"] is True
    assert updated["severity"] == "critical"
    assert updated["snoozed_until"] == 1780000100 + 2 * 60 * 60


def test_avito_followup_cards_include_inline_actions(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages = []
            self.photos = []
            self.topics = []

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": len(self.messages)}}

        def send_photo_url(self, chat_id, photo_url, caption=None, **kwargs):
            self.photos.append((chat_id, photo_url, caption, kwargs))
            return {"ok": True, "result": {"message_id": 100 + len(self.photos)}}

        def create_forum_topic(self, chat_id, name):
            self.topics.append((chat_id, name))
            return {"ok": True, "result": {"message_thread_id": 77}}

    report_path = tmp_path / "report.json"
    key = "1:chat-followup:m-bot-1"
    report_path.write_text(
        json.dumps(
            {
                "pending_followups": [
                    {
                        "key": key,
                        "account_id": 1,
                        "chat_id": "chat-followup",
                        "client_name": "Анна",
                        "business_status": "overdue",
                        "severity": "critical",
                        "age_seconds": 3600,
                        "listing_city": "Геленджик",
                        "listing_title": "Увеличение губ",
                        "bot_promise": "Уточню адрес и напишу.",
                        "last_client_message": "Жду адрес",
                        "last_client_photo_urls": ["https://img.example/client.jpg"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bot = FakeBot()

    main_module.send_avito_followup_cards(bot, "admin-chat", report_path=report_path, topics_path=tmp_path / "topics.json")

    assert len(bot.messages) == 2
    assert "Зависшие Avito-обещания" in bot.messages[0][1]
    assert "Жду адрес" in bot.messages[1][1]
    assert bot.topics == [("admin-chat", "Анна | Avito / Геленджик | Увеличение губ")]
    assert bot.messages[1][2]["message_thread_id"] == "77"
    assert bot.messages[1][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"avfu:{pending_followup_token(key)}:done"
    assert bot.photos == [("admin-chat", "https://img.example/client.jpg", "Фото клиента из Avito (1/1)", {"message_thread_id": "77"})]


def test_avito_followup_cards_reuse_existing_client_topic(tmp_path) -> None:
    from src.freelance_leads_bot.integrations.telegram_client_topics import remember_client_topic

    class FakeBot:
        def __init__(self) -> None:
            self.messages = []
            self.photos = []
            self.topics = []

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": len(self.messages)}}

        def send_photo_url(self, chat_id, photo_url, caption=None, **kwargs):
            self.photos.append((chat_id, photo_url, caption, kwargs))
            return {"ok": True, "result": {"message_id": 100 + len(self.photos)}}

        def create_forum_topic(self, chat_id, name):
            self.topics.append((chat_id, name))
            return {"ok": True, "result": {"message_thread_id": 99}}

    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "pending_followups": [
                    {
                        "key": "1:chat-followup:m-bot-1",
                        "account_id": 1,
                        "chat_id": "chat-followup",
                        "client_name": "Анна",
                        "business_status": "overdue",
                        "severity": "critical",
                        "age_seconds": 3600,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    topic_path = tmp_path / "topics.json"
    remember_client_topic(
        key="avito:1:chat-followup",
        telegram_chat_id="admin-chat",
        message_thread_id="55",
        title="Анна",
        path=topic_path,
    )
    bot = FakeBot()

    main_module.send_avito_followup_cards(bot, "admin-chat", report_path=report_path, topics_path=topic_path)

    assert bot.topics == []
    assert bot.messages[1][2]["message_thread_id"] == "55"


def test_avito_followup_media_downloads_and_uploads_when_telegram_cannot_fetch_url(tmp_path, monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.photos = []

        def send_photo_url(self, chat_id, photo_url, caption=None, **kwargs):
            raise RuntimeError("Telegram cannot fetch URL")

        def send_photo(self, chat_id, path, caption=None, **kwargs):
            self.photos.append((chat_id, str(path), caption, kwargs))
            return {"ok": True}

    downloaded = tmp_path / "downloaded.jpg"
    downloaded.write_bytes(b"jpg")
    monkeypatch.setattr(main_module, "_download_photo_url", lambda url, media_dir: downloaded)
    bot = FakeBot()

    sent = main_module.send_avito_followup_media(
        bot,
        "admin-chat",
        {"last_client_photo_urls": ["https://img.example/client.jpg"]},
    )

    assert sent == 1
    assert bot.photos == [("admin-chat", str(downloaded), "Фото клиента из Avito (1/1)", {})]


def test_avito_followup_callback_updates_report_immediately(tmp_path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.answers = []
            self.messages = []

        def answer_callback_query(self, callback_id, text):
            self.answers.append((callback_id, text))

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True}

    key = "1:chat-followup:m-bot-1"
    row = {
        "key": key,
        "account_id": 1,
        "chat_id": "chat-followup",
        "message_id": "m-bot-1",
        "client_name": "Анна",
        "business_status": "overdue",
        "business_resolved": False,
        "overdue": True,
        "severity": "critical",
    }
    state_path = tmp_path / "state.json"
    report_path = tmp_path / "report.json"
    state_path.write_text(json.dumps({"pending_followups": {key: dict(row, key=None)}}, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(
        json.dumps(
            {
                "pending_followup_count": 1,
                "overdue_followup_count": 1,
                "critical_followup_count": 1,
                "pending_followups": [row],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bot = FakeBot()

    handled = main_module.handle_avito_followup_callback(
        bot=bot,
        callback_id="cb-1",
        data=f"avfu:{pending_followup_token(key)}:done",
        telegram_chat_id="admin-chat",
        state_path=state_path,
        report_path=report_path,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert handled is True
    assert bot.answers == [("cb-1", "Закрыто")]
    assert report["pending_followup_count"] == 0
    assert report["overdue_followup_count"] == 0
    assert report["critical_followup_count"] == 0
    assert report["pending_followups"][0]["business_status"] == "closed_manual_no_client_reply"
    assert report["pending_followups"][0]["business_resolved"] is True


def test_pending_followup_done_keeps_noncritical_manual_close(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    audit_path = tmp_path / "audit.jsonl"
    key = "1:chat-followup:m-bot-1"
    state_path.write_text(
        json.dumps(
            {
                "pending_followups": {
                    key: {
                        "chat_id": "chat-followup",
                        "business_status": "overdue",
                        "business_resolved": False,
                        "bot_promise": "Передам Ольге пожелание по тону.",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = apply_pending_followup_action(
        state_path=state_path,
        token=pending_followup_token(key),
        action="done",
        now=1780000000,
        audit_path=audit_path,
    )
    row = json.loads(state_path.read_text(encoding="utf-8"))["pending_followups"][key]

    assert result["ok"] is True
    assert row["business_status"] == "manual_closed"
    assert row["client_answer_confirmed"] is True


def test_pending_followup_done_marks_critical_without_client_reply(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    audit_path = tmp_path / "audit.jsonl"
    key = "1:chat-followup:m-bot-1"
    state_path.write_text(
        json.dumps(
            {
                "pending_followups": {
                    key: {
                        "chat_id": "chat-followup",
                        "business_status": "overdue",
                        "business_resolved": False,
                        "severity": "critical",
                        "bot_promise": "Уточню адрес и подтвержу запись.",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = apply_pending_followup_action(
        state_path=state_path,
        token=pending_followup_token(key),
        action="done",
        now=1780000000,
        audit_path=audit_path,
    )
    row = json.loads(state_path.read_text(encoding="utf-8"))["pending_followups"][key]

    assert result["ok"] is True
    assert row["business_status"] == "closed_manual_no_client_reply"
    assert row["client_answer_confirmed"] is False


def test_care_followup_send_is_blocked_by_feature_flag(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.answers = []
            self.messages = []

        def answer_callback_query(self, callback_id, text):
            self.answers.append((callback_id, text))

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True}

    class FakeStore:
        def __init__(self) -> None:
            self.updates = []

        def update_followup_task(self, task_id, **kwargs):
            self.updates.append((task_id, kwargs))
            return {"id": task_id, **kwargs}

        def followup_send_gate(self, task_id):
            return {"status": "allowed", "allowed": True, "task_id": task_id, "chat_id": "1001"}

    stores = []

    def fake_store_factory():
        store = FakeStore()
        stores.append(store)
        return store

    monkeypatch.setattr(main_module, "CareCrmStore", fake_store_factory)
    bot = FakeBot()

    handled = main_module.handle_care_followup_callback(
        bot=bot,
        callback_id="cb-care",
        data="carefu:42:send",
        settings=_settings(),
        telegram_chat_id="admin-chat",
    )

    assert handled is True
    assert bot.answers == [("cb-care", "Отправка выключена")]
    assert "TELEGRAM_CLIENT_FOLLOWUP_SEND_ENABLED" in bot.messages[0][1]
    assert stores[0].updates == [(42, {"outcome": "send_blocked_by_feature_flag"})]


def test_avito_live_telegram_relay_builds_one_card_with_client_and_bot_messages() -> None:
    chat = {
        "id": "chat-live",
        "context": {"value": {"title": "ГУБЫ", "price_string": "5000 ₽"}},
        "users": [{"id": 123, "name": "Анна"}, {"id": 355539652, "name": "Аккаунт"}],
    }
    own_account_ids = {355539652}
    client_message = {
        "id": "m-client",
        "author_id": 123,
        "created": 1780000000,
        "direction": "in",
        "type": "text",
        "content": {"text": "ГУБЫ)"},
    }
    bot_message = {
        "id": "m-bot",
        "author_id": 355539652,
        "created": 1780000060,
        "direction": "out",
        "type": "text",
        "content": {"text": "Здравствуйте! Подскажите город?"},
    }

    events = [
        compact_relay_event(chat=chat, raw_message=client_message, own_account_ids=own_account_ids),
        compact_relay_event(chat=chat, raw_message=bot_message, own_account_ids=own_account_ids),
    ]
    card = format_avito_live_telegram_card(account_id=355539652, chat=chat, events=events, max_visible_events=10)

    assert "<code>chat-live</code>" in card
    assert "ГУБЫ | 5000 ₽" in card
    assert "👤 Клиент · Анна" in card
    assert "ГУБЫ)" in card
    assert "🤖 Бот/аккаунт · Аккаунт" in card
    assert "Подскажите город" in card


def test_avito_live_telegram_relay_merges_events_and_filters_empty() -> None:
    old = [{"id": "m1", "created": 20, "text": "old"}, {"id": "m2", "created": 30, "text": "old-duplicate"}]
    new = [{"id": "m2", "created": 10, "text": "new-duplicate"}, {"id": "m3", "created": 40, "text": "new"}]

    assert [event["id"] for event in merge_avito_live_events(old, new, limit=2)] == ["m1", "m3"]
    assert should_relay_avito_live_message({"id": "m1", "created": 100, "type": "text", "content": {"text": "Привет"}}, since_ts=90) == (True, "")
    assert should_relay_avito_live_message({"id": "m2", "created": 100, "type": "system", "content": {"text": "x"}}, since_ts=90) == (False, "system")
    assert should_relay_avito_live_message({"id": "m3", "created": 100, "type": "text", "content": {"text": ""}}, since_ts=90) == (False, "empty")
    assert avito_live_telegram_message_id({"ok": True, "result": {"message_id": 777}}) == 777


def test_avito_live_telegram_relay_reads_codex_preview_outbox(tmp_path) -> None:
    outbox = tmp_path / "avito_outbox.jsonl"
    fresh = {
        "ts": 1780000000,
        "account_id": 355539652,
        "chat_id": "chat-preview",
        "text": "Здравствуйте! Подскажите город?",
        "sent": False,
        "reason": "preview_only",
    }
    old = {**fresh, "ts": 10, "chat_id": "old-chat"}
    sent = {**fresh, "chat_id": "sent-chat", "sent": True, "reason": "sent"}
    outbox.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in [old, fresh, sent]), encoding="utf-8")

    rows = iter_avito_live_preview_outbox(outbox, since_ts=100)
    event = compact_preview_event(rows[0])
    card = format_avito_live_telegram_card(
        account_id=355539652,
        chat={"id": "chat-preview"},
        events=[event],
        max_visible_events=10,
    )

    assert rows == [fresh]
    assert avito_live_preview_event_key(fresh).startswith("preview:")
    assert event["actor"] == "Codex preview"
    assert event["direction"] == "out"
    assert "бот не отправил клиенту" in card
    assert "Подскажите город" in card


def test_avito_live_telegram_relay_reads_handoff_outbox_as_live_question(tmp_path) -> None:
    outbox = tmp_path / "handoff_outbox.jsonl"
    fresh = {
        "created_at": "2026-05-29T13:21:28+00:00",
        "reason": "missing_data",
        "summary": "Нужен точный адрес в Москве.",
        "message": {
            "channel": "avito",
            "chat_id": "chat-handoff",
            "message_id": "m-handoff",
            "text": "И где территориально?",
        },
        "text": "Нужна ручная консультация",
    }
    old = {**fresh, "created_at": "2026-05-20T13:21:28+00:00", "message": {**fresh["message"], "chat_id": "old-chat"}}
    outbox.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in [old, fresh]), encoding="utf-8")

    rows = iter_avito_live_handoff_outbox(outbox, since_ts=1780000000)
    event = compact_handoff_event(rows[0])
    card = format_avito_live_telegram_card(
        account_id=355539652,
        chat={"id": "chat-handoff"},
        events=[event],
        max_visible_events=10,
    )

    assert rows == [fresh]
    assert avito_live_handoff_event_key(fresh).startswith("handoff:")
    assert event["actor"] == "Codex question"
    assert event["direction"] == "out"
    assert "Ольге лично не отправлен" in card
    assert "Нужен точный адрес" in card


def test_avito_webhook_photo_returns_handoff(tmp_path) -> None:
    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_photo_resolver] = lambda: None
    outbox = tmp_path / "handoff_outbox.jsonl"
    avito_app.dependency_overrides[get_handoff_notifier] = lambda: PreviewHandoffNotifier(outbox)
    event = {
        "type": "message",
        "message_id": "photo-1",
        "chat_id": "chat-photo",
        "content": {"text": "Посмотрите фото", "photo": {"id": "p1"}},
    }
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json=event)

        assert response.status_code == 200
        assert response.json()["action"] == "handoff"
        assert response.json()["handoff"] == "photo_consultation"
        assert response.json()["handoff_notify"]["reason"] == "preview_only"
        assert "photo_consultation" in outbox.read_text(encoding="utf-8")
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_turn_buffer_batches_multiple_messages_for_one_codex_turn(tmp_path) -> None:
    buffer_path = tmp_path / "turn_buffer.json"
    first = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-1",
        chat_id="chat-batch",
        message_id="m1",
        text="Здравствуйте",
        created_at=1780000001,
        metadata={"account_id": 1},
    )
    second = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-1",
        chat_id="chat-batch",
        message_id="m2",
        text="А губы сколько?",
        created_at=1780000002,
        metadata={"account_id": 1},
    )

    queued_first = enqueue_avito_turn_message(first, debounce_seconds=60, max_wait_seconds=120, path=buffer_path)
    queued_second = enqueue_avito_turn_message(second, debounce_seconds=60, max_wait_seconds=120, path=buffer_path)
    early = pop_due_avito_turn_batches(now=queued_second["process_after"] - 1, path=buffer_path)
    due = pop_due_avito_turn_batches(now=queued_second["process_after"] + 1, path=buffer_path)
    batched = batch_to_inbound_message(due[0])

    assert queued_first["queued"] is True
    assert early == []
    assert len(due) == 1
    assert batched.chat_id == "chat-batch"
    assert batched.metadata["batched"] is True
    assert batched.metadata["batch_size"] == 2
    assert "1. Здравствуйте" in batched.text
    assert "2. А губы сколько?" in batched.text
    assert batched.message_id == "m1,m2"


def test_avito_turn_buffer_retries_failed_batch_before_removing_it(tmp_path) -> None:
    buffer_path = tmp_path / "turn_buffer.json"
    message = InboundMessage(
        channel=Channel.AVITO,
        client_id="client-retry",
        chat_id="chat-retry",
        message_id="m-retry",
        text="Нужна запись",
        created_at=1780000001,
        metadata={"account_id": 1},
    )

    queued = enqueue_avito_turn_message(message, debounce_seconds=0, max_wait_seconds=0, path=buffer_path)
    due = pop_due_avito_turn_batches(now=queued["process_after"] + 1, path=buffer_path, lease_seconds=60)
    leased_again = pop_due_avito_turn_batches(now=queued["process_after"] + 2, path=buffer_path, lease_seconds=60)
    mark_avito_turn_batch_failed(due[0], "temporary failure", now=queued["process_after"] + 3, path=buffer_path)
    before_retry = pop_due_avito_turn_batches(now=queued["process_after"] + 10, path=buffer_path, lease_seconds=60)
    retry = pop_due_avito_turn_batches(now=queued["process_after"] + 40, path=buffer_path, lease_seconds=60)
    mark_avito_turn_batch_processed(retry[0], path=buffer_path)
    after_processed = pop_due_avito_turn_batches(now=queued["process_after"] + 500, path=buffer_path, lease_seconds=60)

    assert len(due) == 1
    assert leased_again == []
    assert before_retry == []
    assert len(retry) == 1
    assert retry[0]["attempts"] == 1
    assert after_processed == []


def test_extract_date_ignores_invalid_dates() -> None:
    assert extract_date("Запишите на 15.99", today=date(2026, 7, 1)) == ""
    assert extract_date("Запишите на 2026-99-15", today=date(2026, 7, 1)) == ""


def test_avito_webhook_greets_empty_created_chat() -> None:
    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "empty-chat-1",
                "chat_id": "chat-empty",
                "type": "system",
                "content": {"text": "Пользователь создал чат, но пока ничего не написал"},
            },
        }
    }
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json=event)

        assert response.status_code == 200
        assert response.json()["action"] == "empty_chat_greeting"
        assert "какой у вас вопрос" in response.json()["reply"]
        assert response.json()["handoff"] is None
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_uses_planner_when_dependency_is_enabled() -> None:
    class FakePlanner:
        async def respond(self, context, toolbox):
            del toolbox
            return AvitoConsultantReply(
                action="codex_reply",
                reply=f"Codex отвечает по чату {context.message.chat_id}",
                metadata={"planner": "fake_codex"},
            )

    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_planner] = lambda: FakePlanner()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    try:
        client = TestClient(avito_app)
        response = client.post(
            "/avito/webhook?token=webhook",
            json={"type": "message", "message_id": "codex-1", "chat_id": "chat-codex", "text": "Интересует ботокс"},
        )

        assert response.status_code == 200
        assert response.json()["action"] == "codex_reply"
        assert response.json()["planner"] == "fake_codex"
        assert "chat-codex" in response.json()["reply"]
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_transcribes_voice_before_processing() -> None:
    class FakeVoiceResolver:
        async def transcribe(self, message):
            assert message.metadata["voice_id"] == "voice-1"
            return replace(
                message,
                text="Здравствуйте, хочу записаться на увеличение груди",
                metadata={**message.metadata, "voice_transcribed": True},
            )

    class FakePlanner:
        async def respond(self, context, toolbox):
            del toolbox
            assert context.message.text == "Здравствуйте, хочу записаться на увеличение груди"
            assert context.message.metadata["voice_transcribed"] is True
            return AvitoConsultantReply(
                action="codex_reply",
                reply="Здравствуйте! Подскажите, пожалуйста, в каком городе хотите записаться?",
                metadata={"planner": "fake_codex"},
            )

    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_planner] = lambda: FakePlanner()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_voice_resolver] = lambda: FakeVoiceResolver()
    event = {
        "payload": {
            "type": "message_created",
            "value": {
                "id": "voice-message-1",
                "chat_id": "chat-voice",
                "direction": "in",
                "type": "voice",
                "content": {"voice": {"voice_id": "voice-1"}},
            },
        }
    }
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json=event)

        assert response.status_code == 200
        assert response.json()["action"] == "ask_city"
        assert response.json()["send"]["reason"] == "preview_only"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_reviewer_revises_before_send() -> None:
    class FakePlanner:
        async def respond(self, context, toolbox):
            del context, toolbox
            return AvitoConsultantReply(action="codex_reply", reply="Мы у метро Тверская.")

    class FakeReviewer:
        async def review(self, *, message, decision, conversation_history=()):
            del message, conversation_history
            return replace(decision, reply="Точный адрес уточню у Ольги и напишу.", metadata={**decision.metadata, "draft_review": {"action": "revise"}})

    processed_events.seen.clear()
    avito_app.dependency_overrides[get_settings] = lambda: _settings()
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_planner] = lambda: FakePlanner()
    avito_app.dependency_overrides[get_reviewer] = lambda: FakeReviewer()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    try:
        client = TestClient(avito_app)
        response = client.post("/avito/webhook?token=webhook", json={"type": "message", "message_id": "review-1", "chat_id": "chat-review", "text": "Где вы?"})

        assert response.status_code == 200
        assert response.json()["reply"] == "Точный адрес уточню у Ольги и напишу."
        assert response.json()["draft_review"]["action"] == "revise"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_keeps_memory_per_avito_client(tmp_path) -> None:
    histories = []

    class FakePlanner:
        async def respond(self, context, toolbox):
            del toolbox
            histories.append(list(context.conversation_history))
            return AvitoConsultantReply(
                action="codex_reply",
                reply="Ответ с учётом памяти.",
                metadata={"planner": "fake_codex", "conversation_key": context.conversation_key},
            )

    processed_events.seen.clear()
    settings = replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3")
    store = LeadStore(settings.telegram_admin_history_db_path)
    avito_app.dependency_overrides[get_settings] = lambda: settings
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_planner] = lambda: FakePlanner()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_history_store] = lambda: store
    try:
        client = TestClient(avito_app)
        first = client.post("/avito/webhook?token=webhook", json={"type": "message", "message_id": "m1", "chat_id": "chat-memory", "text": "Можно на 30 мая?"})
        second = client.post("/avito/webhook?token=webhook", json={"type": "message", "message_id": "m2", "chat_id": "chat-memory", "text": "да"})

        assert first.status_code == 200
        assert second.status_code == 200
        assert histories[0] == []
        assert any("Можно на 30 мая" in item["content"] for item in histories[1])
        saved = store.recent_codex_chat(10, "avito:client:chat-memory")
        assert [item["role"] for item in saved] == ["user", "assistant", "user", "assistant"]
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_skips_own_echo_from_history(tmp_path) -> None:
    class FakePlanner:
        async def respond(self, context, toolbox):
            raise AssertionError("own Avito echo must not reach planner")

    processed_events.seen.clear()
    settings = replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3")
    store = LeadStore(settings.telegram_admin_history_db_path)
    store.add_codex_chat_message("assistant", "Здравствуйте! Подскажите город?", "avito:client:chat-echo")
    avito_app.dependency_overrides[get_settings] = lambda: settings
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_planner] = lambda: FakePlanner()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_history_store] = lambda: store
    try:
        client = TestClient(avito_app)
        response = client.post(
            "/avito/webhook?token=webhook",
            json={"type": "message", "message_id": "echo-1", "chat_id": "chat-echo", "text": "Здравствуйте! Подскажите город?"},
        )

        assert response.status_code == 200
        assert response.json()["ignored"] is True
        assert response.json()["reason"] == "assistant_echo"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_skips_duplicate_from_client_history(tmp_path) -> None:
    class FakePlanner:
        async def respond(self, context, toolbox):
            raise AssertionError("duplicate messages must not reach planner")

    processed_events.seen.clear()
    settings = replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3")
    store = LeadStore(settings.telegram_admin_history_db_path)
    store.add_codex_chat_message("user", "message_id: duplicate-1\nГУБЫ)", "avito:client:chat-duplicate")
    avito_app.dependency_overrides[get_settings] = lambda: settings
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_planner] = lambda: FakePlanner()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_history_store] = lambda: store
    try:
        client = TestClient(avito_app)
        response = client.post(
            "/avito/webhook?token=webhook",
            json={"type": "message", "message_id": "duplicate-1", "chat_id": "chat-duplicate", "text": "ГУБЫ)"},
        )

        assert response.status_code == 200
        assert response.json()["ignored"] is True
        assert response.json()["reason"] == "duplicate_history"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_skips_waiting_ack_after_pending_reply(tmp_path) -> None:
    class FakePlanner:
        async def respond(self, context, toolbox):
            raise AssertionError("ack after pending reply must not reach planner")

    processed_events.seen.clear()
    settings = replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3")
    store = LeadStore(settings.telegram_admin_history_db_path)
    store.add_codex_chat_message("assistant", "Проверим вручную и напишем вам.", "avito:client:chat-wait")
    avito_app.dependency_overrides[get_settings] = lambda: settings
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_planner] = lambda: FakePlanner()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_history_store] = lambda: store
    try:
        client = TestClient(avito_app)
        response = client.post(
            "/avito/webhook?token=webhook",
            json={"type": "message", "message_id": "wait-ack-1", "chat_id": "chat-wait", "text": "Ок"},
        )

        assert response.status_code == 200
        assert response.json()["ignored"] is True
        assert response.json()["reason"] == "client_ack_after_pending_reply"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_webhook_does_not_skip_waiting_address_after_pending_reply(tmp_path) -> None:
    processed_events.seen.clear()
    settings = replace(_settings(), telegram_admin_history_db_path=tmp_path / "history.sqlite3")
    store = LeadStore(settings.telegram_admin_history_db_path)
    store.add_codex_chat_message("assistant", "Уточню точный адрес и напишу вам.", "avito:client:chat-address-wait")
    avito_app.dependency_overrides[get_settings] = lambda: settings
    avito_app.dependency_overrides[get_booking] = lambda: DryRunYClientsGateway()
    avito_app.dependency_overrides[get_sender] = lambda: PreviewAvitoSender()
    avito_app.dependency_overrides[get_history_store] = lambda: store
    try:
        client = TestClient(avito_app)
        response = client.post(
            "/avito/webhook?token=webhook",
            json={"type": "message", "message_id": "wait-address-1", "chat_id": "chat-address-wait", "text": "Жду адрес"},
        )

        assert response.status_code == 200
        assert response.json().get("ignored") is not True
        assert response.json().get("reason") != "client_ack_after_pending_reply"
    finally:
        avito_app.dependency_overrides.clear()
        processed_events.seen.clear()


def test_avito_health_exposes_vk_launch_flags() -> None:
    settings = replace(_settings(), vk_group_id=225170792, vk_group_token="vk-token", vk_send_enabled=False, vk_codex_enabled=False)
    avito_app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(avito_app)
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["vk_ready"] is True
        assert response.json()["vk_send_enabled"] is False
        assert response.json()["vk_codex_enabled"] is False
    finally:
        avito_app.dependency_overrides.clear()


def test_new_yclients_integration_endpoints_match_legacy_contract(tmp_path) -> None:
    settings = replace(_settings(), public_base_url="https://olgatihcosmo.com", yclients_integration_secret="secret123")
    repository = YClientsIntegrationEventRepository(tmp_path / "events.jsonl")
    yclients_integration_app.dependency_overrides[get_yclients_integration_settings] = lambda: settings
    yclients_integration_app.dependency_overrides[get_yclients_integration_repository] = lambda: repository
    try:
        client = TestClient(yclients_integration_app)

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["integration_urls"]["webhook_url"] == "https://olgatihcosmo.com/yclients/webhook?secret=%2A%2A%2A"
        assert health.json()["integration_urls"]["callback_url"] == "https://olgatihcosmo.com/yclients/callback?secret=%2A%2A%2A"
        assert health.json()["integration_urls"]["registration_redirect_url"] == "https://olgatihcosmo.com/yclients/register"

        assert client.get("/yclients/webhook").json() == {"ok": True}
        assert client.request("HEAD", "/yclients/webhook").status_code == 200
        assert client.post("/yclients/webhook", json={"event": "record"}, params={"secret": "bad"}).status_code == 403

        webhook = client.post("/yclients/webhook", json={"event": "record"}, params={"secret": "secret123"})
        callback = client.post("/yclients/callback", data={"company_id": "1"}, params={"secret": "secret123"})
        register_get = client.get("/yclients/register?company_id=1")
        register_post = client.post("/yclients/register", json={"company_id": 1})

        assert webhook.status_code == 200
        assert callback.status_code == 200
        assert register_get.status_code == 200
        assert "Интеграция YCLIENTS подключена" in register_get.text
        assert register_post.status_code == 200

        rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [row["event_type"] for row in rows] == ["webhook", "disconnect_callback", "registration_redirect", "registration_redirect"]
        assert rows[0]["payload"] == {"event": "record"}
        assert rows[1]["payload"]["company_id"] == "1"
        assert rows[2]["query"]["company_id"] == "1"
    finally:
        yclients_integration_app.dependency_overrides.clear()


def test_format_handoff_message_includes_avito_context() -> None:
    handoff = avito_photo_handoff(
        avito_inbound_message(
            {
                "type": "message",
                "chat_id": "chat-photo",
                "content": {
                    "text": "Что делать с кожей?",
                    "item": {"title": "Консультация", "price_string": "0 ₽", "city": "Москва"},
                    "photo": {"id": "p1"},
                },
            }
        )
    )

    assert handoff is not None
    text = format_handoff_message(handoff)
    assert "Нужна ручная проверка" in text
    assert "Диалог: " in text
    assert "#" not in text
    assert "chat-photo" not in text
    assert "Консультация" in text
    assert "Нужна ручная консультация" not in text
    assert "очная консультация" not in text.casefold()

    named_handoff = replace(handoff, message=replace(handoff.message, metadata={**handoff.message.metadata, "client_name": "Анна"}))
    named_text = format_handoff_message(named_handoff)
    assert "Клиент: Анна" in named_text
    assert "Диалог: " not in named_text


def test_telegram_handoff_ref_context_restores_hidden_avito_chat_id(tmp_path, monkeypatch) -> None:
    ref_path = tmp_path / "handoff_refs.json"
    monkeypatch.setattr(main_module, "TELEGRAM_HANDOFF_REFS_PATH", ref_path)
    remember_telegram_handoff_ref(
        telegram_chat_id="5993376751",
        telegram_message_id=1691,
        avito_chat_id="u2i-3mq1guQUx_p8BlFOazMtRA",
        client_name="VSergeeva",
        handoff_text=(
            "Нужна ручная консультация\n"
            "Клиент: VSergeeva\n"
            "Сообщение: 300мл в каждую 50000?\n"
            "Контекст: Уточнить, 300 мл за 50 000 ₽ — это объём на каждую ягодицу или общий объём за процедуру."
        ),
        path=ref_path,
    )
    message = {
        "chat": {"id": 5993376751},
        "from": {"username": "dr_olgat"},
        "text": "Общий объем конечно",
        "reply_to_message": {
            "message_id": 1691,
            "from": {"username": "YclientsAvitoTg_bot"},
            "text": "Нужна ручная консультация\nКлиент: VSergeeva\nСообщение: 300мл в каждую 50000?",
        },
    }

    annotated = annotate_sender_for_codex(message, message["text"])

    assert "Avito chat_id: u2i-3mq1guQUx_p8BlFOazMtRA" in annotated
    assert "Клиент Avito: VSergeeva" in annotated
    assert "Общий объем конечно" in annotated


def test_telegram_handoff_ref_context_restores_avito_chat_id_from_quoted_card_text(tmp_path, monkeypatch) -> None:
    ref_path = tmp_path / "handoff_refs.json"
    monkeypatch.setattr(main_module, "TELEGRAM_HANDOFF_REFS_PATH", ref_path)
    remember_telegram_handoff_ref(
        telegram_chat_id="5993376751",
        telegram_message_id=2319,
        avito_chat_id="u2i-oqvw6390gBKgVh_skaw6eQ",
        client_name="Arseniy",
        handoff_text=(
            "Нужна ручная консультация\n"
            "Причина: missing_data\n"
            "Канал: avito\n"
            "Клиент: Arseniy\n"
            "Сообщение: На сегодня к сожалению не могу\n"
            "Контекст: Нужно уточнить ближайшие доступные даты для модели на увеличение губ "
            "в Санкт-Петербурге после 16.06, так как в графике будущих дат нет."
        ),
        path=ref_path,
    )
    message = {
        "chat": {"id": 5993376751},
        "from": {"username": "dr_olgat"},
        "text": "Пришли фото",
        "reply_to_message": {
            "message_id": 999999,
            "from": {"username": "YclientsAvitoTg_bot"},
            "text": (
                "Нужна ручная консультация\n"
                "Причина: missing_data\n"
                "Канал: avito\n"
                "Клиент: Arseniy\n"
                "Сообщение: На сегодня к сожалению не могу\n"
                "Контекст: Нужно уточнить ближайшие доступные даты для модели на увеличение губ "
                "в Санкт-Петербурге после 16.06, так как в графике будущих дат нет."
            ),
        },
    }

    annotated = annotate_sender_for_codex(message, message["text"])

    assert "Avito chat_id: u2i-oqvw6390gBKgVh_skaw6eQ" in annotated
    assert "Клиент Avito: Arseniy" in annotated
    assert "Пришли фото" in annotated


def test_telegram_handoff_ref_context_restores_avito_chat_id_from_nearby_preview(tmp_path, monkeypatch) -> None:
    ref_path = tmp_path / "handoff_refs.json"
    monkeypatch.setattr(main_module, "TELEGRAM_HANDOFF_REFS_PATH", ref_path)
    remember_telegram_handoff_ref(
        telegram_chat_id="5993376751",
        telegram_message_id=2346,
        avito_chat_id="u2i-TLp5D1HRj6WxDm4lyxr8hA",
        client_name="Диана",
        handoff_text=(
            "Нужна ручная консультация\n"
            "Причина: missing_data\n"
            "Канал: avito\n"
            "Клиент: Диана\n"
            "Сообщение: Я про актуальность увеличения губ бесплатно\n"
            "Контекст: Уточнить, есть ли сейчас бесплатная акция/места для моделей на увеличение губ."
        ),
        path=ref_path,
    )
    message = {
        "chat": {"id": 5993376751},
        "from": {"username": "dr_olgat"},
        "text": "Пришли фото",
        "reply_to_message": {
            "message_id": 2342,
            "from": {"username": "YclientsAvitoTg_bot"},
            "text": "Нужна ручная консультация Причина: m...",
        },
    }

    annotated = annotate_sender_for_codex(message, message["text"])

    assert "Avito chat_id: u2i-TLp5D1HRj6WxDm4lyxr8hA" in annotated
    assert "Клиент Avito: Диана" in annotated
    assert "Пришли фото" in annotated


def test_telegram_handoff_ref_context_can_backfill_from_webhook_log(tmp_path) -> None:
    webhook_log = tmp_path / "avito_webhook.log"
    webhook_log.write_text(
        json.dumps(
            {
                "ts": 1780308658,
                "event": "processed",
                "chat_id": "u2i-3mq1guQUx_p8BlFOazMtRA",
                "action": "handoff",
                "handoff_notify": {
                    "sent": True,
                    "telegram": {"ok": True, "result": {"message_id": 1691, "chat": {"id": 5993376751}}},
                    "text": "Нужна ручная консультация\nКлиент: VSergeeva\nСообщение: 300мл в каждую 50000?",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    message = {"chat": {"id": 5993376751}, "reply_to_message": {"message_id": 1691}}

    context = telegram_handoff_ref_context(message, ref_path=tmp_path / "missing.json", log_paths=[webhook_log])

    assert "Avito chat_id: u2i-3mq1guQUx_p8BlFOazMtRA" in context
    assert "300мл в каждую" in context


@pytest.mark.anyio
async def test_telegram_handoff_notifier_downloads_and_sends_avito_photo_urls(tmp_path, monkeypatch) -> None:
    from src.freelance_leads_bot.integrations.handoff_notify import TelegramHandoffNotifier
    import src.freelance_leads_bot.integrations.handoff_notify as handoff_notify

    class FakeTelegramBot:
        def __init__(self) -> None:
            self.messages = []
            self.photos = []

        def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))
            return {"ok": True, "result": {"message_id": 1}}

        def send_photo(self, chat_id, path, caption=None):
            self.photos.append((chat_id, str(path), caption))
            return {"ok": True, "result": {"message_id": 2}}

    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-photo",
            "content": {
                "text": "Посмотрите фото",
                "image": {"id": "img-1", "sizes": {"1280x960": "https://img.example/big.jpg"}},
            },
        }
    )
    handoff = avito_photo_handoff(message)
    assert handoff is not None
    downloaded = tmp_path / "downloaded.jpg"
    downloaded.write_bytes(b"fake-image")
    monkeypatch.setattr(handoff_notify, "_download_photo_url", lambda url, media_dir: downloaded)
    bot = FakeTelegramBot()
    ref_path = tmp_path / "handoff_refs.json"
    notifier = TelegramHandoffNotifier(bot, "admin-chat", ref_path=ref_path)

    result = await notifier.notify(handoff)

    assert result["photos_sent"] == 1
    assert len(bot.photos) == 1
    assert bot.photos[0][0] == "admin-chat"
    assert bot.photos[0][1] == str(downloaded)
    assert "Фото из avito, диалог" in bot.photos[0][2]
    assert "chat-photo" not in bot.photos[0][2]
    assert result["photo_results"][0]["path"] == str(downloaded)
    assert "будет переслано" in bot.messages[0][1]
    ref = find_telegram_handoff_ref("admin-chat", 1, ref_path)
    assert ref is not None
    assert ref["avito_chat_id"] == "chat-photo"


@pytest.mark.anyio
async def test_telegram_handoff_notifier_merges_repeated_avito_chat_card(tmp_path, monkeypatch) -> None:
    from src.freelance_leads_bot.integrations.handoff_notify import TelegramHandoffNotifier
    import src.freelance_leads_bot.integrations.handoff_notify as handoff_notify

    class FakeTelegramBot:
        def __init__(self) -> None:
            self.messages = []
            self.edits = []

        def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))
            return {"ok": True, "result": {"message_id": 1}}

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))
            return {"ok": True, "result": {"message_id": message_id}}

    async def direct_retry(func, *args, **kwargs):
        return func(*args)

    monkeypatch.setattr(handoff_notify, "_to_thread_retry", direct_retry)
    bot = FakeTelegramBot()
    ref_path = tmp_path / "handoff_refs.json"
    notifier = TelegramHandoffNotifier(bot, "admin-chat", ref_path=ref_path)
    first = Handoff(
        reason=HandoffReason.MISSING_DATA,
        message=avito_inbound_message({"type": "message", "id": "m1", "chat_id": "chat-repeat", "text": "Первый вопрос"}),
        summary="Нужно уточнить метод.",
    )
    second = Handoff(
        reason=HandoffReason.MISSING_DATA,
        message=avito_inbound_message({"type": "message", "id": "m2", "chat_id": "chat-repeat", "text": "Второй вопрос"}),
        summary="Нужно уточнить объём.",
    )

    first_result = await notifier.notify(first)
    second_result = await notifier.notify(second)
    ref = find_telegram_handoff_ref("admin-chat", 1, ref_path)

    assert first_result["sent"] is True
    assert second_result["merged"] is True
    assert second_result["reason"] == "merged_existing_handoff"
    assert len(bot.messages) == 1
    assert len(bot.edits) == 1
    assert bot.edits[0][1] == 1
    assert "Второй вопрос" in bot.edits[0][2]
    assert ref is not None
    assert ref["source_message_id"] == "m2"
    assert "Второй вопрос" in ref["handoff_text"]


@pytest.mark.anyio
async def test_telegram_handoff_notifier_does_not_merge_booking_critical_card(tmp_path, monkeypatch) -> None:
    from src.freelance_leads_bot.integrations.handoff_notify import TelegramHandoffNotifier
    import src.freelance_leads_bot.integrations.handoff_notify as handoff_notify

    class FakeTelegramBot:
        def __init__(self) -> None:
            self.messages = []
            self.edits = []

        def send_message(self, chat_id, text):
            message_id = len(self.messages) + 1
            self.messages.append((chat_id, text))
            return {"ok": True, "result": {"message_id": message_id}}

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))
            return {"ok": True, "result": {"message_id": message_id}}

    async def direct_retry(func, *args, **kwargs):
        return func(*args)

    monkeypatch.setattr(handoff_notify, "_to_thread_retry", direct_retry)
    bot = FakeTelegramBot()
    ref_path = tmp_path / "handoff_refs.json"
    notifier = TelegramHandoffNotifier(bot, "admin-chat", ref_path=ref_path)
    first = Handoff(
        reason=HandoffReason.MISSING_DATA,
        message=avito_inbound_message({"type": "message", "id": "m1", "chat_id": "chat-critical", "text": "Нужен метод"}),
        summary="Обычная открытая задача.",
    )
    critical = Handoff(
        reason=HandoffReason.BOOKING_CRITICAL,
        message=avito_inbound_message({"type": "message", "id": "m2", "chat_id": "chat-critical", "text": "Мне завтра приходить? Адрес?"}),
        summary="Клиент ждёт подтверждение записи и адрес.",
    )

    await notifier.notify(first)
    result = await notifier.notify(critical)
    ref = find_telegram_handoff_ref("admin-chat", 2, ref_path)

    assert result["sent"] is True
    assert "merged" not in result
    assert len(bot.messages) == 2
    assert bot.edits == []
    assert "СРОЧНО" in bot.messages[1][1]
    assert ref is not None
    assert ref["urgency"] == "critical"
    assert int(ref["deadline_at"]) > 0
    assert int(ref["escalation_at"]) > int(ref["deadline_at"])
    assert ref["city"] == "Краснодар" or ref["city"] == ""
    assert ref["confirmation_needed"] == "точный адрес и подтверждение записи"
    assert ref["assignee"] == "Ольга/админ"


@pytest.mark.anyio
async def test_handoff_sla_sends_reminders_escalates_and_expires_old_refs(tmp_path) -> None:
    class FakeNotifier:
        def __init__(self) -> None:
            self.texts = []

        async def notify(self, handoff):
            raise AssertionError("SLA processing sends text notifications only")

        async def notify_text(self, text):
            self.texts.append(text)
            return {"sent": True, "text": text}

    ref_path = tmp_path / "handoff_refs.json"
    now = 1780000000
    reminder = remember_telegram_handoff_ref(
        telegram_chat_id="admin-chat",
        telegram_message_id=1,
        avito_chat_id="chat-reminder",
        handoff_text="Нужна ручная проверка",
        path=ref_path,
    )
    critical = remember_telegram_handoff_ref(
        telegram_chat_id="admin-chat",
        telegram_message_id=2,
        avito_chat_id="chat-critical",
        client_name="Алена",
        handoff_text=(
            "СРОЧНО: клиент ждёт подтверждение записи/адрес\n"
            "Причина: booking_critical\n"
            "Клиент: Алена\n"
            "Объявление: Увеличение губ | Краснодар\n"
            "Сообщение: Я не получила ответ по записи\n"
            "Контекст: Нужно проверить наличие записи клиента, дату/время и адрес, затем ответить с точным подтверждением."
        ),
        urgency="critical",
        city="Краснодар",
        service="Увеличение губ",
        confirmation_needed="актуальность записи и время прихода",
        assignee="Ольга/админ",
        path=ref_path,
    )
    stale = remember_telegram_handoff_ref(
        telegram_chat_id="admin-chat",
        telegram_message_id=3,
        avito_chat_id="chat-stale",
        handoff_text="Старая карточка",
        path=ref_path,
    )
    refs = load_telegram_handoff_refs(ref_path)
    refs[f"admin-chat:1"]["created_at"] = now - 61 * 60
    refs[f"admin-chat:2"]["created_at"] = now - 4 * 60 * 60
    refs[f"admin-chat:3"]["created_at"] = now - 8 * 24 * 60 * 60
    save_telegram_handoff_refs(refs, ref_path)
    notifier = FakeNotifier()

    result = await process_handoff_sla(notifier, ref_path=ref_path, now=now, reminder_after_seconds=30 * 60)
    updated = load_telegram_handoff_refs(ref_path)

    assert result["reminders"] == 2
    assert result["escalations"] == 1
    assert result["expired"] == 1
    assert updated[f"admin-chat:1"]["reminder_sent_at"] == now
    assert updated[f"admin-chat:2"]["escalation_sent_at"] == now
    assert updated[f"admin-chat:3"]["status"] == "expired"
    assert any("Напоминание" in text for text in notifier.texts)
    assert any("Критично" in text for text in notifier.texts)
    assert any("Клиент: Алена" in text for text in notifier.texts)
    assert any("Объявление: Увеличение губ | Краснодар" in text for text in notifier.texts)
    assert any("Последнее от клиента: Я не получила ответ по записи" in text for text in notifier.texts)
    assert any("Контекст: Нужно проверить наличие записи клиента" in text for text in notifier.texts)
    assert any("Детали записи: Увеличение губ | Краснодар" in text for text in notifier.texts)


@pytest.mark.anyio
async def test_handoff_sla_repeats_reminders_after_cooldown_without_new_handoff(tmp_path) -> None:
    class FakeNotifier:
        def __init__(self) -> None:
            self.texts = []

        async def notify_text(self, text):
            self.texts.append(text)
            return {"sent": True, "text": text}

    ref_path = tmp_path / "handoff_refs.json"
    now = 1780000000
    ref = remember_telegram_handoff_ref(
        telegram_chat_id="admin-chat",
        telegram_message_id=1,
        avito_chat_id="chat-critical",
        handoff_text="Причина: booking_critical\nСообщение: запись на 28 июля в силе?",
        urgency="critical",
        reason="booking_critical",
        path=ref_path,
    )
    refs = load_telegram_handoff_refs(ref_path)
    refs["admin-chat:1"]["created_at"] = now - 5 * 60 * 60
    refs["admin-chat:1"]["reminder_sent_at"] = now - 7 * 60 * 60
    refs["admin-chat:1"]["escalation_sent_at"] = now - 3 * 60 * 60
    save_telegram_handoff_refs(refs, ref_path)
    notifier = FakeNotifier()

    result = await process_handoff_sla(
        notifier,
        ref_path=ref_path,
        now=now,
        reminder_after_seconds=60 * 60,
        escalation_after_seconds=3 * 60 * 60,
        reminder_repeat_seconds=6 * 60 * 60,
        escalation_repeat_seconds=2 * 60 * 60,
    )
    updated = load_telegram_handoff_refs(ref_path)["admin-chat:1"]

    assert result["reminders"] == 1
    assert result["escalations"] == 1
    assert updated["handoff_id"] == ref["handoff_id"]
    assert updated["reminder_count"] == 1
    assert updated["escalation_count"] == 1
    assert len(load_telegram_handoff_refs(ref_path)) == 1


@pytest.mark.anyio
async def test_handoff_sla_deduplicates_repeated_open_cards_for_same_avito_chat(tmp_path) -> None:
    class FakeNotifier:
        def __init__(self) -> None:
            self.texts = []

        async def notify_text(self, text):
            self.texts.append(text)
            return {"sent": True, "text": text}

    ref_path = tmp_path / "handoff_refs.json"
    now = 1780000000
    first = remember_telegram_handoff_ref(
        telegram_chat_id="admin-chat",
        telegram_message_id=1,
        avito_chat_id="chat-same",
        handoff_text="Причина: booking_critical\nСообщение: запись на 28 июля в силе?",
        urgency="critical",
        reason="booking_critical",
        path=ref_path,
    )
    second = remember_telegram_handoff_ref(
        telegram_chat_id="admin-chat",
        telegram_message_id=2,
        avito_chat_id="chat-same",
        handoff_text="Причина: booking_critical\nСообщение: адрес напишите",
        urgency="critical",
        reason="booking_critical",
        path=ref_path,
    )
    refs = load_telegram_handoff_refs(ref_path)
    refs["admin-chat:1"]["created_at"] = now - 4 * 60 * 60
    refs["admin-chat:1"]["reminder_sent_at"] = now - 7 * 60 * 60
    refs["admin-chat:1"]["escalation_sent_at"] = now - 3 * 60 * 60
    refs["admin-chat:2"]["created_at"] = now - 2 * 60 * 60
    refs["admin-chat:2"]["reminder_sent_at"] = now - 7 * 60 * 60
    refs["admin-chat:2"]["escalation_sent_at"] = now - 3 * 60 * 60
    save_telegram_handoff_refs(refs, ref_path)

    notifier = FakeNotifier()
    result = await process_handoff_sla(
        notifier,
        ref_path=ref_path,
        now=now,
        reminder_after_seconds=60 * 60,
        escalation_after_seconds=3 * 60 * 60,
        reminder_repeat_seconds=6 * 60 * 60,
        escalation_repeat_seconds=2 * 60 * 60,
    )
    updated = load_telegram_handoff_refs(ref_path)

    assert result["deduped"] == 1
    assert result["reminders"] == 1
    assert result["escalations"] == 1
    assert len(notifier.texts) == 2
    assert updated["admin-chat:1"]["reminder_count"] == 1
    assert updated["admin-chat:1"]["escalation_count"] == 1
    assert updated["admin-chat:2"].get("reminder_count", 0) == 0
    assert updated["admin-chat:2"].get("escalation_count", 0) == 0


@pytest.mark.anyio
async def test_handoff_sla_marks_critical_expired_separately(tmp_path) -> None:
    class FakeNotifier:
        async def notify_text(self, text):
            return {"sent": True, "text": text}

    ref_path = tmp_path / "handoff_refs.json"
    now = 1780000000
    remember_telegram_handoff_ref(
        telegram_chat_id="admin-chat",
        telegram_message_id=1,
        avito_chat_id="chat-critical",
        handoff_text="СРОЧНО: клиент ждёт подтверждение записи/адрес\nПричина: booking_critical",
        urgency="critical",
        path=ref_path,
    )
    refs = load_telegram_handoff_refs(ref_path)
    refs["admin-chat:1"]["created_at"] = now - 8 * 24 * 60 * 60
    save_telegram_handoff_refs(refs, ref_path)

    result = await process_handoff_sla(FakeNotifier(), ref_path=ref_path, now=now)
    updated = load_telegram_handoff_refs(ref_path)["admin-chat:1"]

    assert result["expired"] == 0
    assert result["expired_critical"] == 1
    assert updated["status"] == "expired_critical"
    assert updated.get("closed_at", 0) == 0
    assert updated["expired_at"] == now


@pytest.mark.anyio
async def test_telegram_handoff_notifier_sends_photos_when_merging_existing_card(tmp_path, monkeypatch) -> None:
    from src.freelance_leads_bot.integrations.handoff_notify import TelegramHandoffNotifier
    import src.freelance_leads_bot.integrations.handoff_notify as handoff_notify

    class FakeTelegramBot:
        def __init__(self) -> None:
            self.messages = []
            self.edits = []
            self.photos = []

        def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))
            return {"ok": True, "result": {"message_id": 1}}

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))
            return {"ok": True, "result": {"message_id": message_id}}

        def send_photo(self, chat_id, path, caption=None):
            self.photos.append((chat_id, str(path), caption))
            return {"ok": True, "result": {"message_id": 10 + len(self.photos)}}

    async def direct_retry(func, *args, **kwargs):
        return func(*args)

    def download_photo(url, media_dir):
        path = tmp_path / ("photo-" + url.rsplit("/", 1)[-1])
        path.write_bytes(b"image")
        return path

    monkeypatch.setattr(handoff_notify, "_to_thread_retry", direct_retry)
    monkeypatch.setattr(handoff_notify, "_download_photo_url", download_photo)
    bot = FakeTelegramBot()
    ref_path = tmp_path / "handoff_refs.json"
    notifier = TelegramHandoffNotifier(bot, "admin-chat", ref_path=ref_path)
    first = Handoff(
        reason=HandoffReason.MISSING_DATA,
        message=avito_inbound_message({"type": "message", "id": "m1", "chat_id": "chat-repeat", "text": "Нужен ориентир"}),
        summary="Открытая текстовая задача.",
    )
    photo_message = avito_inbound_message(
        {
            "type": "message",
            "id": "m2",
            "chat_id": "chat-repeat",
            "content": {"text": "[фото]", "image": {"url": "https://img.example/one.jpg"}},
        }
    )
    second = Handoff(reason=HandoffReason.PHOTO_CONSULTATION, message=photo_message, summary="Клиент прислал фото.")

    await notifier.notify(first)
    second_result = await notifier.notify(second)

    assert second_result["merged"] is True
    assert second_result["photos_sent"] == 1
    assert second_result["photos_failed"] == 0
    assert len(bot.messages) == 1
    assert len(bot.edits) == 1
    assert len(bot.photos) == 1
    assert "Фото из avito" in bot.photos[0][2]


@pytest.mark.anyio
async def test_telegram_handoff_notifier_keeps_sending_after_one_photo_fails(tmp_path, monkeypatch) -> None:
    from src.freelance_leads_bot.integrations.handoff_notify import TelegramHandoffNotifier
    import src.freelance_leads_bot.integrations.handoff_notify as handoff_notify

    class FakeTelegramBot:
        def __init__(self) -> None:
            self.messages = []
            self.photos = []

        def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))
            return {"ok": True, "result": {"message_id": 1}}

        def send_photo(self, chat_id, path, caption=None):
            self.photos.append((chat_id, str(path), caption))
            return {"ok": True, "result": {"message_id": 10 + len(self.photos)}}

    async def direct_retry(func, *args, **kwargs):
        return func(*args)

    def download_photo(url, media_dir):
        if "bad" in url:
            raise TimeoutError("download timed out")
        path = tmp_path / ("ok-" + url.rsplit("/", 1)[-1])
        path.write_bytes(b"image")
        return path

    message = avito_inbound_message(
        {
            "type": "message",
            "chat_id": "chat-photo",
            "content": {
                "text": "Посмотрите фото",
                "images": [
                    {"url": "https://img.example/good-1.jpg"},
                    {"url": "https://img.example/bad.jpg"},
                    {"url": "https://img.example/good-2.jpg"},
                ],
            },
        }
    )
    handoff = avito_photo_handoff(message)
    assert handoff is not None
    monkeypatch.setattr(handoff_notify, "_to_thread_retry", direct_retry)
    monkeypatch.setattr(handoff_notify, "_download_photo_url", download_photo)
    bot = FakeTelegramBot()
    notifier = TelegramHandoffNotifier(bot, "admin-chat", ref_path=tmp_path / "refs.json")

    result = await notifier.notify(handoff)
    ref = find_telegram_handoff_ref("admin-chat", 1, tmp_path / "refs.json")

    assert result["photos_sent"] == 2
    assert result["photos_failed"] == 1
    assert len(result["photo_errors"]) == 1
    assert [status["status"] for status in result["media_statuses"]] == [
        "sent_to_olga",
        "manual_avito_check_required",
        "sent_to_olga",
    ]
    assert ref is not None
    assert [status["status"] for status in ref["media_statuses"]] == [
        "sent_to_olga",
        "manual_avito_check_required",
        "sent_to_olga",
    ]
    assert result["media_failure_notify"]["sent"] is True
    assert "не удалось переслать вложение" in result["media_failure_notify"]["text"]
    assert len(bot.photos) == 2
    assert len(bot.messages) == 2


@pytest.mark.anyio
async def test_avito_handoff_photo_resolver_fills_missing_photo_urls() -> None:
    class FakeResolver:
        async def photo_urls(self, account_id, chat_id, message_id=""):
            assert account_id == 1
            assert chat_id == "chat-photo"
            assert message_id == "m-photo"
            return ["https://img.example/from-api.jpg"]

    message = avito_inbound_message(
        {
            "type": "message",
            "id": "m-photo",
            "chat_id": "chat-photo",
            "content": {"text": "Фото", "photo": {"id": "p1"}},
        }
    )
    handoff = avito_photo_handoff(message)
    assert handoff is not None
    reply = AvitoConsultantReply(action="handoff", reply="Передам фото", handoff=handoff)

    enriched = await enrich_reply_handoff_photos(reply, resolver=FakeResolver(), account_id=1)

    assert enriched.handoff is not None
    assert enriched.handoff.message.metadata["photo_urls"] == ["https://img.example/from-api.jpg"]


@pytest.mark.anyio
async def test_avito_api_photo_resolver_reads_dict_message_payload() -> None:
    from src.freelance_leads_bot.integrations.avito_media import AvitoApiPhotoResolver

    class FakeAuth:
        async def auth_header(self):
            return {"Authorization": "Bearer test"}

    class FakeTransport:
        async def request(self, **kwargs):
            return {
                "messages": [
                    {"id": "other", "content": {"image": {"url": "https://img.example/other.jpg"}}},
                    {"id": "m-photo", "content": {"image": {"sizes": {"1280x960": "https://img.example/from-dict.jpg"}}}},
                ]
            }

    class FakeClient:
        def __init__(self):
            self.auth = FakeAuth()
            self._transport = FakeTransport()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    resolver = AvitoApiPhotoResolver(_settings(), client=FakeClient())

    urls = await resolver.photo_urls(1, "chat-photo", "m-photo")

    assert urls == ["https://img.example/from-dict.jpg"]


def test_codex_planner_prompt_and_json_parser() -> None:
    payload = {"available_tools": ["knowledge.list"], "message": {"text": "ботокс"}}
    prompt = build_codex_planner_prompt(payload, [{"tool": "knowledge.list", "ok": True}])
    parsed = parse_codex_step('```json\n{"tool_calls":[{"name":"knowledge.list","arguments":{"query":"ботокс"}}]}\n```')

    assert "Верни строго один JSON-объект" in prompt
    assert "knowledge.list" in prompt
    assert "handoff_reason" in prompt
    assert "Ольга — косметолог и владелец экспертного контекста" in prompt
    assert "живой ассистент записи" in prompt
    assert "Router уже обработал простые safety/RAG/media/city/procedure случаи" in prompt
    assert "Слово handoff — только внутреннее поле JSON" in prompt
    assert "reply=''" in prompt
    assert "фото до/после" in prompt
    assert "тихую задачу" in prompt
    assert "подтверждённое решение" in prompt
    assert "Не предлагай очную консультацию" in prompt
    assert "Если уже есть оценка Ольги/подтверждённое решение" in prompt
    assert "Нужно у Ольги:" not in prompt
    assert "Нужно: фото до/после" in prompt
    assert parsed is not None
    assert parsed["tool_calls"][0]["name"] == "knowledge.list"


def test_codex_review_can_revise_or_handoff_bad_draft() -> None:
    message = avito_inbound_message({"type": "message", "chat_id": "chat-review", "text": "Где вы?"})
    decision = AvitoConsultantReply(action="codex_reply", reply="Мы у метро Тверская, приходите завтра.")

    revised = apply_review_outcome(
        message,
        decision,
        {"action": "revise", "reply": "Точный адрес уточню у Ольги и напишу.", "notes": "адрес не подтвержден"},
    )
    handoff = apply_review_outcome(
        message,
        decision,
        {"action": "handoff", "handoff_reason": "missing_data", "handoff_summary": "Нужен точный адрес", "reply": "Уточню адрес у Ольги."},
    )

    assert revised.reply == "Точный адрес уточню у Ольги и напишу."
    assert revised.metadata["draft_review"]["action"] == "revise"
    assert handoff.action == "handoff"
    assert handoff.handoff is not None
    assert handoff.handoff.reason == "missing_data"


def test_codex_review_guard_handoffs_aesthetic_volume_promise() -> None:
    message = avito_inbound_message({"type": "message", "chat_id": "chat-review", "text": "300 мл хватит на грудь?"})
    decision = AvitoConsultantReply(action="codex_reply", reply="300 мл по груди даст заметный результат и примерно плюс один размер.")

    reviewed = apply_review_outcome(message, decision, {"action": "approve", "notes": "ok"})

    assert reviewed.action == "handoff"
    assert reviewed.handoff is not None
    assert reviewed.handoff.reason == HandoffReason.EXPERT_EXPECTATION
    assert reviewed.reply == "По объёму и ожидаемому результату лучше не обещать вслепую. Передам Ольге, она посмотрит и сориентирует точнее."
    assert reviewed.metadata["aesthetic_expectation_guard"]["reason"] == "aesthetic_expectation_guard"


def test_codex_review_guard_allows_explicit_olga_aesthetic_formula() -> None:
    message = avito_inbound_message({"type": "message", "chat_id": "chat-review", "text": "Что даст 300 мл?"})
    decision = AvitoConsultantReply(
        action="codex_reply",
        reply="300 мл для ягодиц — минимальный объём: можно скорректировать небольшие дефекты, выраженный результат по фото и ожиданиям оценивает Ольга.",
        metadata={"olga_approved_aesthetic_formula": True},
    )

    reviewed = apply_review_outcome(message, decision, {"action": "approve", "notes": "approved Olga wording"})

    assert reviewed.action == "codex_reply"
    assert "минимальный объём" in reviewed.reply
    assert "aesthetic_expectation_guard" not in reviewed.metadata


def test_consultation_guard_removes_offline_and_final_hedge() -> None:
    reply = (
        "Доброе утро! Фото посмотрели. Визуальную коррекцию асимметрии без операции сделать можно. "
        "Если цель именно скорректировать форму без увеличения объёма груди, ориентировочно понадобится 200 мл препарата. "
        "Точный объём и вариант коррекции окончательно подбираются индивидуально по исходной форме и желаемому результату. "
        "Приходите на очную консультацию, там всё решим."
    )

    sanitized, changed = sanitize_consultation_language(reply)

    assert changed is True
    assert "Визуальную коррекцию асимметрии" in sanitized
    assert "200 мл" in sanitized
    assert "очная консультация" not in sanitized.casefold()
    assert "приходите" not in sanitized.casefold()
    assert "консультац" not in sanitized.casefold()
    assert "окончательно" not in sanitized.casefold()


def test_consultation_guard_removes_redundant_expert_confirmation_tail() -> None:
    reply = (
        "Елена, фото посмотрели. По визуальной оценке можно ориентироваться на увеличение примерно на +1 размер, "
        "ориентировочный объём — около 400 мл препарата. "
        "Точный объём и итоговую стоимость лучше подтвердить после уточнения желаемого результата и исходных данных."
    )

    sanitized, changed = sanitize_consultation_language(reply)

    assert changed is True
    assert sanitized == (
        "Елена, фото посмотрели. По визуальной оценке можно ориентироваться на увеличение примерно на +1 размер, "
        "ориентировочный объём — около 400 мл препарата."
    )


def test_consultation_guard_preserves_factual_consultation_price_terms() -> None:
    reply = (
        "Здравствуйте! Увеличение ягодиц выполняем препаратом Tesoro Body, эффект может сохраняться до 4 лет.\n\n"
        "Очная консультация платная. Онлайн консультация бесплатная.\n"
        "Приходите на очную консультацию, там всё решим."
    )

    sanitized, changed = sanitize_consultation_language(reply)

    assert changed is True
    assert "Очная консультация платная" in sanitized
    assert "Онлайн консультация бесплатная" in sanitized
    assert "Приходите" not in sanitized
    assert "там всё решим" not in sanitized


def test_codex_review_sanitizes_approved_draft() -> None:
    message = avito_inbound_message({"type": "message", "chat_id": "chat-review", "text": "Можно по фото?"})
    decision = AvitoConsultantReply(
        action="codex_reply",
        reply="Можно сделать коррекцию. Точный вариант окончательно подберём индивидуально на консультации.",
    )

    reviewed = apply_review_outcome(message, decision, {"action": "approve", "notes": "ok"})

    assert reviewed.reply == "Можно сделать коррекцию."
    assert reviewed.metadata["consultation_guard"]["changed"] is True


def test_codex_review_prompt_checks_internals_offtopic_and_unconfirmed_facts() -> None:
    message = avito_inbound_message({"type": "message", "chat_id": "chat-review", "text": "Как устроен Codex?"})
    decision = AvitoConsultantReply(action="codex_reply", reply="Сейчас расскажу про tools.")

    prompt = build_codex_review_prompt(message, decision, [])

    assert "выдуманный адрес" in prompt
    assert "Codex/tool/handoff" in prompt
    assert "оффтопик" in prompt
    assert "Не спамь онлайн-консультацией" in prompt
    assert "оценка Ольги" in prompt


def test_codex_chat_prompt_explains_olga_business_context() -> None:
    prompt = build_chat_prompt("Что ты ответил бы Ольге косметологу на вопрос: что ты умеешь?")

    assert "Ольга — косметолог и владелец экспертного контекста" in prompt
    assert "Если сообщение пришло из админского канала или от самой Ольги" in prompt
    assert "общайся с Ольгой как её персональный ассистент" in prompt
    assert "не начинай ответ рамкой «Я помощник Ольги...»" in prompt
    assert "не начинай клиентский ответ обращением «Ольга, ...»" in prompt
    assert "Я помощник Ольги, косметолога" in prompt


def test_prelaunch_report_keeps_live_guards_off_before_preview() -> None:
    report = build_prelaunch_report(replace(_settings(), vk_group_id=225170792, vk_group_token="vk-token"))

    assert report.ok_for_preview is True
    assert report.ok_for_vk_preview is True
    assert report.flags["avito_send_enabled"] is False
    assert report.flags["yclients_allow_mutations"] is False
    assert "AVITO_CODEX_ENABLED" in report.next_step


def test_ops_status_reports_actionable_avito_queue(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps({"ok": True, "count": 1, "actionable_count": 1, "items": [{"needs_action": True}]}),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
    )

    queue_check = next(check for check in report.checks if check.name == "avito_unanswered_queue")
    assert report.ok is True
    assert queue_check.ok is False
    assert queue_check.severity == "warning"
    assert report.summary["avito_actionable"] == 1


def test_ops_status_failed_autoreply_is_error(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "count": 0,
                "actionable_count": 0,
                "items": [],
                "pending_followup_count": 1,
                "critical_followup_count": 1,
                "overdue_followup_count": 1,
                "pending_followups": [
                    {
                        "business_status": "overdue",
                        "severity": "critical",
                        "overdue": True,
                        "age_seconds": 7200,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {"chat:msg": {"error": "boom"}}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
    )

    failed_check = next(check for check in report.checks if check.name == "avito_autoreply_failures")
    assert report.ok is False
    assert failed_check.ok is False
    assert failed_check.severity == "error"
    assert report.summary["avito_autoreply_failed"] == 1


def test_ops_status_warns_on_stale_unanswered_report(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:01:40+00:00",
                "count": 0,
                "actionable_count": 0,
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=2000,
    )

    freshness_check = next(check for check in report.checks if check.name == "avito_unanswered_report_fresh")
    assert report.ok is True
    assert freshness_check.ok is False
    assert freshness_check.severity == "warning"
    assert freshness_check.data is not None
    assert freshness_check.data["report_age_seconds"] == 1900
    assert report.summary["avito_unanswered_report_age_seconds"] == 1900


def test_ops_status_human_summary_highlights_warnings(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:01:40+00:00",
                "count": 1,
                "actionable_count": 1,
                "items": [{"needs_action": True}],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=2000,
    )

    text = format_ops_status_report(report)

    assert text.startswith("AutomaticCosmetic ops: WARN")
    assert "Avito actionable=1" in text
    assert "report_age=1900s" in text
    assert "Warnings:" in text
    assert "avito_unanswered_queue" in text
    assert "avito_unanswered_report_fresh" in text
    assert "No immediate action required" not in text


def test_ops_status_warns_on_overdue_avito_promises(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:03:20+00:00",
                "count": 0,
                "actionable_count": 0,
                "critical_unanswered_count": 0,
                "pending_followup_count": 2,
                "overdue_followup_count": 2,
                "items": [],
                "pending_followups": [{"business_status": "overdue"}, {"business_status": "overdue"}],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=200,
    )
    check = next(check for check in report.checks if check.name == "avito_pending_followups")
    text = format_ops_status_report(report)

    assert check.ok is False
    assert check.severity == "warning"
    assert report.summary["avito_pending_followups"] == 2
    assert report.summary["avito_overdue_followups"] == 2
    assert "overdue_promises=2" in text


def test_ops_status_errors_when_overdue_avito_promises_exceed_sla(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:03:20+00:00",
                "count": 0,
                "actionable_count": 0,
                "pending_followup_count": 1,
                "overdue_followup_count": 1,
                "items": [],
                "pending_followups": [{"business_status": "overdue", "overdue": True, "age_seconds": 7200}],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        overdue_followup_error_after_seconds=3600,
        now=200,
    )
    check = next(check for check in report.checks if check.name == "avito_pending_followups")

    assert report.ok is False
    assert check.ok is False
    assert check.severity == "error"
    assert report.summary["avito_max_overdue_followup_age_seconds"] == 7200
    assert ops_status_exit_code(report, strict=True) == 1


def test_ops_status_warns_on_critical_pending_avito_promises_before_overdue(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:03:20+00:00",
                "count": 0,
                "actionable_count": 0,
                "pending_followup_count": 1,
                "critical_followup_count": 1,
                "overdue_followup_count": 0,
                "items": [],
                "pending_followups": [{"severity": "critical", "age_seconds": 1200}],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=200,
    )
    check = next(check for check in report.checks if check.name == "avito_pending_followups")
    text = format_ops_status_report(report)

    assert check.ok is False
    assert check.severity == "warning"
    assert report.summary["avito_critical_followups"] == 1
    assert "critical_promises=1" in text
    assert "No immediate action required" not in text


def test_ops_status_errors_when_avito_poller_scans_too_few_chats(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    poller_log = tmp_path / "avito_poller.log"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    monkeypatch.setenv("AVITO_POLLER_CHAT_LIMIT", "150")
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "count": 0,
                "actionable_count": 0,
                "items": [],
                "pending_followup_count": 1,
                "critical_followup_count": 1,
                "overdue_followup_count": 1,
                "pending_followups": [
                    {
                        "business_status": "overdue",
                        "severity": "critical",
                        "overdue": True,
                        "age_seconds": 7200,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")
    poller_log.write_text(
        json.dumps(
            {
                "ts": 1780000000,
                "event": "summary",
                "processed": 0,
                "skipped": 259,
                "errors": 0,
                "chats": 20,
                "skip_reasons": {"too_old": 259},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    status = read_avito_poller_status(poller_log, expected_chat_limit=150, stale_after_seconds=300, now=1780000100)
    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        avito_poller_log_path=poller_log,
        rag_db_path=rag_path,
        now=1780000100,
    )
    check = next(check for check in report.checks if check.name == "avito_missed_poller_coverage")
    text = format_ops_status_report(report)

    assert status["recent"] is True
    assert status["chats_ok"] is False
    assert check.ok is False
    assert check.severity == "error"
    assert report.summary["avito_poller_last_chats"] == 20
    assert report.summary["avito_poller_expected_chats"] == 150
    assert "Poller: chats=20/150" in text
    assert ops_status_exit_code(report, strict=True) == 1


def test_ops_status_accepts_recent_avito_poller_full_coverage(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    poller_log = tmp_path / "avito_poller.log"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    monkeypatch.setenv("AVITO_POLLER_CHAT_LIMIT", "150")
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "count": 0,
                "actionable_count": 0,
                "items": [],
                "pending_followup_count": 1,
                "critical_followup_count": 1,
                "overdue_followup_count": 1,
                "pending_followups": [
                    {
                        "business_status": "overdue",
                        "severity": "critical",
                        "overdue": True,
                        "age_seconds": 7200,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")
    poller_log.write_text(
        json.dumps({"ts": 1780000000, "event": "summary", "processed": 0, "skipped": 1, "errors": 0, "chats": 150})
        + "\n",
        encoding="utf-8",
    )

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        avito_poller_log_path=poller_log,
        rag_db_path=rag_path,
        now=1780000100,
    )
    check = next(check for check in report.checks if check.name == "avito_missed_poller_coverage")
    text = format_ops_status_report(report)

    assert check.ok is True
    assert report.summary["avito_poller_last_chats"] == 150
    assert "Poller: chats=150/150" in text


def test_ops_status_reports_open_telegram_handoffs_without_mutating_refs(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    handoff_path = tmp_path / "telegram_handoff_refs.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(json.dumps({"ok": True, "count": 0, "actionable_count": 0, "items": []}), encoding="utf-8")
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")
    handoff_path.write_text(
        json.dumps(
            {
                "admin:10": {
                    "telegram_chat_id": "admin",
                    "telegram_message_id": "10",
                    "avito_chat_id": "chat-booking",
                    "handoff_text": "Сообщение: Запись на 28 июля у нас в силе? Адрес напишите.",
                    "status": "open",
                    "created_at": 1000,
                    "updated_at": 1000,
                },
                "admin:11": {
                    "telegram_chat_id": "admin",
                    "telegram_message_id": "11",
                    "avito_chat_id": "chat-draft",
                    "handoff_text": "Нужна ручная проверка",
                    "status": "draft_pending",
                    "created_at": 1000,
                    "updated_at": 1000,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    before = handoff_path.read_text(encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        handoff_refs_path=handoff_path,
        rag_db_path=rag_path,
        now=1000 + 4 * 60 * 60,
    )
    after = handoff_path.read_text(encoding="utf-8")
    check = next(check for check in report.checks if check.name == "telegram_open_handoffs")
    status = read_telegram_handoff_status(handoff_path, now=1000 + 4 * 60 * 60)
    text = format_ops_status_report(report)

    assert before == after
    assert check.ok is False
    assert check.severity == "error"
    assert status["open_count"] == 2
    assert status["critical_count"] == 1
    assert status["draft_pending_count"] == 1
    assert report.summary["handoff_open"] == 2
    assert "Handoff: open=2 critical=1 draft_pending=1 manual_no_client_reply=0 oldest=4h" in text
    assert "Immediate action required: review open Olga handoffs." in text
    assert ops_status_exit_code(report, strict=True) == 1


def test_ops_status_reports_telegram_manual_closed_without_client_reply(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    handoff_path = tmp_path / "telegram_handoff_refs.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(json.dumps({"ok": True, "count": 0, "actionable_count": 0, "items": []}), encoding="utf-8")
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")
    handoff_path.write_text(
        json.dumps(
            {
                "admin:10": {
                    "handoff_id": "handoff-no-reply",
                    "telegram_chat_id": "admin",
                    "telegram_message_id": "10",
                    "avito_chat_id": "chat-booking",
                    "handoff_text": "Сообщение: Запись на 28 июля у нас в силе? Адрес напишите.",
                    "status": "closed_manual_no_client_reply",
                    "resolution_note": "Ольга проверила, клиенту не писали из-за неактуальности",
                    "created_at": 1000,
                    "updated_at": 1000,
                    "closed_at": 1100,
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    before = handoff_path.read_text(encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        handoff_refs_path=handoff_path,
        rag_db_path=rag_path,
        now=2000,
    )
    after = handoff_path.read_text(encoding="utf-8")
    open_check = next(check for check in report.checks if check.name == "telegram_open_handoffs")
    manual_check = next(check for check in report.checks if check.name == "telegram_manual_closure_without_client_reply")
    status = read_telegram_handoff_status(handoff_path, now=2000)

    assert before == after
    assert open_check.ok is True
    assert manual_check.ok is False
    assert manual_check.severity == "warning"
    assert status["open_count"] == 0
    assert status["manual_closed_without_client_reply_count"] == 1
    assert report.summary["handoff_manual_closed_without_client_reply"] == 1
    assert "manual_no_client_reply=1" in format_ops_status_report(report)
    assert ops_status_exit_code(report, strict=False) == 0
    assert ops_status_exit_code(report, strict=True) == 1


def test_ops_status_json_redacts_secrets_and_keeps_secret_required_flag(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(json.dumps({"ok": True, "count": 0, "actionable_count": 0, "items": []}), encoding="utf-8")
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        replace(_settings(), yclients_integration_secret="real-secret"),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True, "debug_url": "https://x.test/hook?token=abc123"},
        yclients_health={
            "ok": True,
            "secret_required": True,
            "integration_urls": {"webhook_url": "https://x.test/yclients/webhook?secret=real-secret"},
        },
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=200,
    )

    payload = report_data(report)
    rendered = json.dumps(payload, ensure_ascii=False)

    assert "real-secret" not in rendered
    assert "abc123" not in rendered
    assert payload["flags"]["yclients_integration_secret_required"] is True


def test_ops_status_reports_role_tool_matrix(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для губ?",
        answer_client="Используем сертифицированные препараты.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(json.dumps({"ok": True, "count": 0, "actionable_count": 0, "items": []}), encoding="utf-8")
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=200,
    )
    check = next(check for check in report.checks if check.name == "role_tool_matrix")

    assert check.ok is True
    assert check.severity == "error"
    assert check.data is not None
    assert check.data["roles"]["admin"]["workspace_execution_tools"] == []
    assert check.data["roles"]["olga_boss"]["workspace_tools"] == []
    assert check.data["roles"]["avito_client"]["forbidden_client_tools"] == []


def test_webhook_runners_disable_uvicorn_access_logs() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "run_avito_webhook.sh",
        root / "run_yclients_integration.sh",
        root / "deploy/systemd/yclients-avito-webhook.service",
        root / "deploy/systemd/yclients-yclients-integration.service",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "--no-access-log" in text or "run_yclients_integration.sh" in text


def test_missed_poller_systemd_unit_sets_production_limits() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/yclients-avito-missed-poller.service").read_text(encoding="utf-8")

    assert "Environment=AVITO_POLLER_CHAT_LIMIT=150" in unit
    assert "Environment=AVITO_POLLER_MESSAGES_PER_CHAT=50" in unit


def test_ops_status_reports_temporal_rag_cleanup_separately(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    store = ExpertRagStore(rag_path)
    store.upsert_from_handoff(
        question="Когда можно на губы?",
        answer_client="Завтра есть окно на 15:00.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )
    store.upsert_from_handoff(
        question="Какой адрес завтра?",
        answer_client="Адрес завтра уточняем отдельно.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": False},
    )
    report_path.write_text(json.dumps({"ok": True, "count": 0, "actionable_count": 0, "items": []}), encoding="utf-8")
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=200,
    )
    check = next(check for check in report.checks if check.name == "expert_rag_temporal_cleanup")
    text = format_ops_status_report(report)

    assert check.ok is False
    assert check.severity == "warning"
    assert report.summary["rag_approved_temporal_without_expiry"] == 2
    assert report.summary["rag_temporal_blocked_from_autoanswer"] == 1
    assert report.summary["rag_temporal_needs_cleanup"] == 1
    assert "temporal_without_expiry=2" in text
    assert "temporal_needs_cleanup=1" in text


def test_ops_status_human_summary_marks_high_risk_rag_as_excluded_from_avito_autoanswer(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Можно делать процедуру после операции?",
        answer_client="После операции процедуру можно делать только после разрешения врача.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:03:20+00:00",
                "count": 0,
                "actionable_count": 0,
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=200,
    )
    text = format_ops_status_report(report)

    assert report.summary["rag_high_risk_approved"] == 1
    assert report.summary["rag_high_risk_excluded_from_avito_autoanswer"] == 1
    assert "high_risk_approved=1 excluded_from_avito_autoanswer=1" in text


def test_ops_status_exit_code_can_be_strict_for_warnings(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:01:40+00:00",
                "count": 1,
                "actionable_count": 1,
                "items": [{"needs_action": True}],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=2000,
    )

    assert report.ok is True
    assert ops_status_exit_code(report) == 0
    assert ops_status_exit_code(report, strict=True) == 1


def test_ops_status_reports_data_footprint_warning(tmp_path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    (data_path / "small.log").write_bytes(b"a" * 10)
    large_dir = data_path / "codex_chat"
    large_dir.mkdir()
    (large_dir / "trace.txt").write_bytes(b"b" * 80)

    footprint = read_data_footprint(data_path, warning_bytes=200, entry_warning_bytes=50)

    assert footprint["ok"] is False
    assert footprint["total_bytes"] == 90
    assert footprint["largest_entry"]["path"].endswith("codex_chat")
    assert footprint["largest_entry"]["size_bytes"] == 80


def test_ops_status_reports_disk_free_warning(tmp_path) -> None:
    disk = read_disk_status(tmp_path, free_warning_bytes=10**18, free_warning_ratio=0.99)

    assert disk["ok"] is False
    assert disk["total_bytes"] > 0
    assert disk["free_bytes"] > 0
    assert 0 < disk["free_ratio"] <= 1


def test_ops_status_human_summary_includes_data_footprint(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    data_path = tmp_path / "data"
    data_path.mkdir()
    (data_path / "agent_trace.jsonl").write_bytes(b"x" * 80)
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:03:20+00:00",
                "count": 0,
                "actionable_count": 0,
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        data_path=data_path,
        data_warning_bytes=200,
        data_entry_warning_bytes=50,
        disk_free_warning_bytes=1,
        disk_free_warning_ratio=0.0,
        now=200,
    )
    text = format_ops_status_report(report)

    data_check = next(check for check in report.checks if check.name == "data_footprint")
    assert report.ok is True
    assert data_check.ok is False
    assert data_check.severity == "warning"
    assert report.summary["data_total_bytes"] == 80
    assert "Data: total=80B" in text
    assert "disk_free=" in text
    assert "data_footprint" in text


def test_backup_runtime_data_copies_sqlite_and_archives_json_env(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "care.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO sample (name) VALUES ('olga')")
    (data_dir / "state.json").write_text('{"ok": true}', encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("TOKEN=secret\n", encoding="utf-8")

    result = backup_runtime_data(
        data_dir=data_dir,
        output_dir=tmp_path / "backups",
        env_path=env_path,
        now=1780000000,
    )

    assert result["ok"] is True
    copied_db = Path(result["sqlite_files"][0])
    assert copied_db.exists()
    with sqlite3.connect(copied_db) as conn:
        assert conn.execute("SELECT name FROM sample").fetchone()[0] == "olga"
    with tarfile.open(result["archive_path"], "r:gz") as archive:
        names = archive.getnames()
    assert "data/state.json" in names
    assert ".env" in names


def test_verify_runtime_backup_restores_to_isolated_dir_and_checks_integrity(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "care.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO sample (name) VALUES ('olga')")
    (data_dir / "state.json").write_text('{"ok": true}', encoding="utf-8")
    (data_dir / "mfa_totp.json").write_text('{"secret": "totp"}', encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("TOKEN=secret\n", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_runtime_data(data_dir=data_dir, output_dir=backup_dir, env_path=env_path, now=1780000000)

    result = verify_runtime_backup(
        backup_dir=backup_dir,
        restore_dir=tmp_path / "restore-check",
        stamp="20260528T202640Z",
    )

    assert result["ok"] is True
    assert result["restore_dir_persistent"] is True
    assert result["contains_env"] is True
    assert result["contains_mfa_totp"] is True
    assert result["contains_sensitive_runtime_secrets"] is True
    assert result["sqlite"][0]["integrity_check"] == "ok"
    restored_db = Path(result["restore_dir"]) / "sqlite" / "care.sqlite3"
    restored_state = Path(result["restore_dir"]) / "runtime" / "data" / "state.json"
    assert restored_db.exists()
    assert restored_state.read_text(encoding="utf-8") == '{"ok": true}'


def test_logrotate_config_covers_debug_logs_without_runtime_state() -> None:
    root = Path(__file__).resolve().parents[1]
    result = verify_logrotate_config(root / "deploy" / "logrotate" / "automaticcosmetic")

    assert result["ok"] is True
    assert "/root/AutomaticCosmetic/data/codex_chat/*.debug.log" in result["patterns"]
    assert "/root/AutomaticCosmetic/data/*.jsonl" in result["patterns"]
    assert "copytruncate" in result["directives"]
    assert result["forbidden_matches"] == []


def test_logrotate_config_rejects_sqlite_and_state_json(tmp_path) -> None:
    path = tmp_path / "automaticcosmetic"
    path.write_text(
        """
/root/AutomaticCosmetic/data/*.log
/root/AutomaticCosmetic/data/leads.sqlite3
/root/AutomaticCosmetic/data/telegram_handoff_refs.json {
    daily
    rotate 30
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
""",
        encoding="utf-8",
    )

    result = verify_logrotate_config(path)

    assert result["ok"] is False
    assert "/root/AutomaticCosmetic/data/leads.sqlite3" in result["forbidden_matches"]
    assert "/root/AutomaticCosmetic/data/telegram_handoff_refs.json" in result["forbidden_matches"]


def test_production_readiness_report_aggregates_manual_blockers(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    care_path = data_dir / "care_crm.sqlite3"
    CareCrmStore(care_path)
    report_path = tmp_path / "avito_unanswered_report.json"
    state_path = tmp_path / "avito_unanswered_monitor_state.json"
    poller_log = tmp_path / "avito_poller.log"
    handoff_path = tmp_path / "telegram_handoff_refs.json"
    rag_path = tmp_path / "expert.sqlite3"
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "count": 0,
                "actionable_count": 0,
                "items": [],
                "pending_followup_count": 1,
                "critical_followup_count": 1,
                "overdue_followup_count": 1,
                "pending_followups": [
                    {
                        "business_status": "overdue",
                        "severity": "critical",
                        "overdue": True,
                        "age_seconds": 7200,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")
    poller_log.write_text(json.dumps({"ts": 1000 + 4 * 60 * 60, "event": "summary", "chats": 20}) + "\n", encoding="utf-8")
    handoff_path.write_text(
        json.dumps(
            {
                "admin:10": {
                    "telegram_chat_id": "admin",
                    "telegram_message_id": "10",
                    "avito_chat_id": "chat-booking",
                    "handoff_text": "Сообщение: Запись на 28 июля у нас в силе? Адрес напишите.",
                    "status": "open",
                    "created_at": 100,
                    "updated_at": 100,
                },
                "admin:11": {
                    "handoff_id": "handoff-no-reply",
                    "telegram_chat_id": "admin",
                    "telegram_message_id": "11",
                    "avito_chat_id": "chat-manual",
                    "handoff_text": "Сообщение: Клиент ждал подтверждение, но Ольга закрыла вручную.",
                    "status": "closed_manual_no_client_reply",
                    "resolution_note": "закрыто вручную после проверки Авито",
                    "created_at": 120,
                    "updated_at": 130,
                    "closed_at": 140,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ExpertRagStore(rag_path).upsert_from_handoff(
        question="Когда есть окно?",
        answer_client="Завтра есть окно на 15:00.",
        status=APPROVED,
        approved_by="olga",
        metadata={"autoanswer_allowed": True},
    )
    backup_data = tmp_path / "backup-data"
    backup_data.mkdir()
    with sqlite3.connect(backup_data / "leads.sqlite3") as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    (backup_data / "state.json").write_text("{}", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("TOKEN=secret\n", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_runtime_data(data_dir=backup_data, output_dir=backup_dir, env_path=env_path, now=1780000000)

    report = build_production_readiness_report(
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        handoff_refs_path=handoff_path,
        care_crm_path=care_path,
        rag_db_path=rag_path,
        data_path=data_dir,
        backup_dir=backup_dir,
        logrotate_path=Path(__file__).resolve().parents[1] / "deploy" / "logrotate" / "automaticcosmetic",
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        now=1000 + 4 * 60 * 60,
    )
    markdown = format_production_readiness_markdown(report)

    assert report["ok"] is False
    assert "ops_status --strict is not green" in report["blockers"]
    assert "1 critical/overdue Avito bot promises need final reply" in report["blockers"]
    assert "1 open Olga handoffs need manual review" in report["blockers"]
    assert "1 temporal RAG autoanswer items need cleanup decision" in report["blockers"]
    assert report["poller_coverage"]["last_chats"] == 20
    assert report["poller_coverage"]["expected_chats"] == 150
    assert report["avito_promises"]["pending"] == 1
    assert report["avito_promises"]["critical"] == 1
    assert report["avito_promises"]["overdue"] == 1
    assert report["avito_promises"]["export_command"] == "python scripts/export_avito_followups.py --output data/avito_followups_review.md"
    assert "--decisions data/avito_followups_review.md" in report["avito_promises"]["dry_run_decisions_command"]
    assert report["open_handoffs"]["export_command"] == "python scripts/export_open_handoffs.py --output data/open_handoffs_review.md"
    assert "--decisions data/open_handoffs_review.md" in report["open_handoffs"]["dry_run_decisions_command"]
    assert report["manual_closure_audit"]["handoff_manual_closed_without_client_reply"] == 1
    assert "--decisions data/expert_rag_temporal_cleanup.md" in report["temporal_rag_cleanup"]["dry_run_decisions_command"]
    assert report["backup_restore_verify"]["ok"] is True
    assert report["logrotate"]["ok"] is True
    assert "Status: `BLOCKED`" in markdown
    assert "Review open Olga handoffs" in markdown
    assert "Review /avito_followups: pending=1, critical=1, overdue=1" in "\n".join(report["manual_actions"])
    assert "Pending: `1`, critical: `1`, overdue: `1`" in markdown
    assert "python scripts/export_avito_followups.py --output data/avito_followups_review.md" in markdown
    assert "python scripts/export_avito_followups.py --decisions data/avito_followups_review.md --apply-decisions" in markdown
    assert "python scripts/export_open_handoffs.py --output data/open_handoffs_review.md" in markdown
    assert "python scripts/export_open_handoffs.py --decisions data/open_handoffs_review.md --apply-decisions" in markdown
    assert "Fix Avito missed-poller coverage: latest summary scanned 20/150 chats" in "\n".join(report["manual_actions"])
    assert "mark per-item decisions" in "\n".join(report["manual_actions"])
    assert "Latest chats: `20/150`" in markdown
    assert "temporal-cleanup --decisions data/expert_rag_temporal_cleanup.md" in markdown
    assert "temporal-cleanup --apply" not in markdown
    assert "Review 1 Telegram handoff closures marked closed_manual_no_client_reply" in "\n".join(report["manual_actions"])
    assert "Manual closures without client reply: `handoff=1`, `avito_promises=0`" in markdown


def test_ops_status_warns_when_expert_rag_has_items_needing_review(tmp_path) -> None:
    report_path = tmp_path / "unanswered_report.json"
    state_path = tmp_path / "unanswered_state.json"
    rag_path = tmp_path / "expert.sqlite3"
    store = ExpertRagStore(rag_path)
    store.upsert_from_handoff(
        question="Какой препарат для ягодиц?",
        answer_client="Используем Tesoro Body.",
        status=APPROVED,
        approved_by="olga",
    )
    review_item = store.upsert_from_handoff(
        question="Можно ли после операции?",
        answer_client="Нужно уточнить у Ольги по анамнезу.",
        status=NEEDS_REVIEW,
    )
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "1970-01-01T00:03:20+00:00",
                "count": 0,
                "actionable_count": 0,
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"handled": {}, "failed": {}, "activated_at": 100}), encoding="utf-8")

    report = build_ops_status_report(
        _settings(),
        service_states={"freelance-leads-bot.service": "active"},
        avito_health={"ok": True, "avito_ready": True, "handoff_notify_ready": True},
        yclients_health={"ok": True, "secret_required": True},
        unanswered_report_path=report_path,
        unanswered_state_path=state_path,
        rag_db_path=rag_path,
        now=200,
    )
    text = format_ops_status_report(report)

    review_check = next(check for check in report.checks if check.name == "expert_rag_needs_review")
    assert report.ok is True
    assert review_check.ok is False
    assert review_check.severity == "warning"
    assert report.summary["rag_needs_review"] == 1
    assert report.summary["rag_needs_review_ids"] == [review_item.id]
    assert review_check.data is not None
    assert review_check.data["needs_review_ids"] == [review_item.id]
    assert "AutomaticCosmetic ops: WARN" in text
    assert "needs_review=1" in text
    assert f"ids: {review_item.id}" in text
    assert "expert_rag_needs_review" in text
    assert "expert_rag_review export --output data/expert_rag_review.md" in text
    assert "mark [x] decisions" in text
    assert "expert_rag_review decisions data/expert_rag_review.md" in text
    assert "add --apply only after the dry-run is correct" in text


def test_vk_update_is_converted_to_shared_inbound_message() -> None:
    update = {
        "type": "message_new",
        "object": {
            "message": {
                "id": 10,
                "peer_id": 123,
                "from_id": 456,
                "date": 1710000000,
                "text": "Сколько стоит чистка?",
                "attachments": [{"type": "photo"}],
            }
        },
    }

    assert is_vk_message_new(update)
    message = vk_inbound_message(update)
    assert message.channel == "vk"
    assert message.chat_id == "123"
    assert message.client_id == "456"
    assert message.has_photo is True


@pytest.mark.anyio
async def test_vk_bot_uses_preview_sender_and_shared_handoff(tmp_path) -> None:
    settings = replace(_settings(), vk_group_id=225170792, vk_group_token="vk-token")
    outbox = tmp_path / "vk_outbox.jsonl"
    bot = VKBot(
        settings=settings,
        sender=PreviewVKSender(outbox),
        handoff_notifier=PreviewHandoffNotifier(tmp_path / "handoff.jsonl"),
    )
    update = {
        "type": "message_new",
        "object": {
            "message": {
                "id": 11,
                "peer_id": 123,
                "from_id": 456,
                "text": "Посмотрите фото",
                "attachments": [{"type": "photo"}],
            }
        },
    }

    result = await bot.handle_update(update)
    duplicate = await bot.handle_update(update)

    assert result["action"] == "handoff"
    assert result["send"]["reason"] == "preview_only"
    assert result["handoff"] == "photo_consultation"
    outbox_text = outbox.read_text(encoding="utf-8")
    assert "фото передадим на оценку" in outbox_text
    assert "консультац" not in outbox_text.casefold()
    assert duplicate["reason"] == "duplicate"


def test_avito_history_import_redacts_and_writes_knowledge(tmp_path) -> None:
    html = """
    <div class="message default clearfix" id="message1">
      <div class="body">
        <div class="pull_right date details" title="25.05.2026 10:00:00 UTC+03:00">10:00</div>
        <div class="from_name">Ольга</div>
        <div class="text">Клиент Avito: 355539652<br>Чат: u2u-secretChat<br>Телефон +7 999 123-45-67</div>
      </div>
    </div>
    <div class="message default clearfix joined" id="message2">
      <div class="body">
        <div class="pull_right date details" title="25.05.2026 10:01:00 UTC+03:00">10:01</div>
        <div class="text">Нужен ответ по Avito: клиент спрашивает стоимость ботокс</div>
      </div>
    </div>
    """
    archive = tmp_path / "ChatExport.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("ChatExport/messages.html", html)

    messages = parse_telegram_html_export(html)
    assert messages[0].sender == "Ольга"
    assert "[phone]" in messages[0].text
    assert "[avito_chat]" in messages[0].text
    assert "355539652" not in messages[0].text

    store = JsonKnowledgeStore(tmp_path / "knowledge.json")
    summary, imported = import_telegram_zip_to_knowledge(archive, store)
    assert summary.dry_run is False
    assert summary.imported == 1
    assert imported[0].kind == "avito_conversation_example"
    assert "bad_example" in imported[0].tags
    assert "[phone]" in imported[0].content
    assert store.list(query="ботокс")


@pytest.mark.anyio
async def test_preview_avito_sender_writes_outbox(tmp_path) -> None:
    outbox = tmp_path / "avito_outbox.jsonl"
    sender = PreviewAvitoSender(outbox)

    result = await sender.send_message(123, "chat-1", "Ответ клиенту")

    assert result["sent"] is False
    assert result["reason"] == "preview_only"
    assert "Ответ клиенту" in outbox.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_process_avito_message_send_failure_is_retryable(tmp_path) -> None:
    class Planner:
        async def respond(self, context, toolbox):
            return AvitoConsultantReply(action="codex_reply", reply="Ответ клиенту.", metadata={"planner": "test"})

    class FailingSender:
        async def send_message(self, account_id, chat_id, text):
            return {"sent": False, "error": "boom"}

    message = avito_inbound_message({"type": "message", "id": "m1", "chat_id": "chat-fail", "content": {"text": "Нестандартный вопрос"}})

    result = await process_avito_message(
        message=message,
        settings=_settings(),
        toolbox=AutomationToolbox(DryRunYClientsGateway()),
        planner=Planner(),
        sender=FailingSender(),
        handoff_notifier=PreviewHandoffNotifier(tmp_path / "handoff.jsonl"),
        photo_resolver=None,
        history_store=LeadStore(tmp_path / "history.sqlite3"),
    )

    assert result["ok"] is False
    assert result["processing_status"] == "retryable_error"
    assert result["error"] == "avito_send_failed:boom"
    assert result["mark_read"]["reason"] == "not_marked_read_delivery_failed"


@pytest.mark.anyio
async def test_process_avito_message_handoff_failure_is_retryable_when_no_client_reply(tmp_path) -> None:
    class Planner:
        async def respond(self, context, toolbox):
            return AvitoConsultantReply(
                action="handoff",
                reply="",
                handoff=Handoff(reason=HandoffReason.MISSING_DATA, message=context.message, summary="Нужно уточнить у Ольги."),
                metadata={"planner": "test"},
            )

    class Sender:
        async def send_message(self, account_id, chat_id, text):
            raise AssertionError("empty reply must not be sent")

    class FailingNotifier:
        async def notify(self, handoff):
            return {"sent": False, "error": "telegram down"}

    message = avito_inbound_message({"type": "message", "id": "m1", "chat_id": "chat-handoff-fail", "content": {"text": "Нестандартный вопрос"}})

    result = await process_avito_message(
        message=message,
        settings=_settings(),
        toolbox=AutomationToolbox(DryRunYClientsGateway()),
        planner=Planner(),
        sender=Sender(),
        handoff_notifier=FailingNotifier(),
        photo_resolver=None,
        history_store=LeadStore(tmp_path / "history.sqlite3"),
    )

    assert result["ok"] is False
    assert result["processing_status"] == "retryable_error"
    assert result["error"] == "telegram_handoff_failed:telegram down"
    assert result["mark_read"]["reason"] == "not_marked_read_delivery_failed"


@pytest.mark.anyio
async def test_process_avito_message_successful_handoff_counts_processed(tmp_path) -> None:
    class Planner:
        async def respond(self, context, toolbox):
            return AvitoConsultantReply(
                action="handoff",
                reply="",
                handoff=Handoff(reason=HandoffReason.MISSING_DATA, message=context.message, summary="Нужно уточнить у Ольги."),
                metadata={"planner": "test"},
            )

    class Sender:
        async def send_message(self, account_id, chat_id, text):
            raise AssertionError("empty reply must not be sent")

    class Notifier:
        async def notify(self, handoff):
            return {"sent": True, "telegram": {"ok": True, "result": {"message_id": 1}}}

    message = avito_inbound_message({"type": "message", "id": "m1", "chat_id": "chat-handoff-ok", "content": {"text": "Нестандартный вопрос"}})

    result = await process_avito_message(
        message=message,
        settings=_settings(),
        toolbox=AutomationToolbox(DryRunYClientsGateway()),
        planner=Planner(),
        sender=Sender(),
        handoff_notifier=Notifier(),
        photo_resolver=None,
        history_store=LeadStore(tmp_path / "history.sqlite3"),
    )

    assert result["ok"] is True
    assert result["processing_status"] == "processed"
    assert result["handoff"] == "missing_data"


@pytest.mark.anyio
async def test_avito_sdk_sender_uses_messenger_endpoint() -> None:
    class FakeAuth:
        async def auth_header(self):
            return {"Authorization": "Bearer test"}

    class FakeTransport:
        def __init__(self):
            self.calls = []

        async def request(self, **kwargs):
            self.calls.append(kwargs)
            return {"id": "msg-1"}

    class FakeClient:
        def __init__(self):
            self.auth = FakeAuth()
            self._transport = FakeTransport()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    fake_client = FakeClient()
    sender = AvitoSdkSender(_settings(), client=fake_client)

    result = await sender.send_message(123, "chat-1", "Здравствуйте")

    assert result["sent"] is True
    call = fake_client._transport.calls[0]
    assert call["method"] == "POST"
    assert call["path_template"] == "/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages"
    assert call["path_params"] == {"user_id": 123, "chat_id": "chat-1"}
    assert call["json_body"]["message"]["text"] == "Здравствуйте"


@pytest.mark.anyio
async def test_avito_sdk_sender_uploads_and_sends_image(tmp_path) -> None:
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"abc-image-id": {"url": "https://example.test/image.jpg"}}

    class FakeHttpClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, headers=None, files=None):
            self.posts.append({"url": url, "headers": headers, "files": files})
            return FakeResponse()

    class FakeAuth:
        async def auth_header(self):
            return {"Authorization": "Bearer test"}

    class FakeTransport:
        def __init__(self):
            self._client = FakeHttpClient()
            self.calls = []

        def _build_url(self, path_template, path_params):
            return path_template.format(user_id=path_params["user_id"])

        async def request(self, **kwargs):
            self.calls.append(kwargs)
            return {"id": "img-msg-1"}

    class FakeClient:
        def __init__(self):
            self.auth = FakeAuth()
            self._transport = FakeTransport()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image-bytes")
    fake_client = FakeClient()
    sender = AvitoSdkSender(_settings(), client=fake_client)

    result = await sender.send_image(123, "chat-1", image_path)

    assert result["sent"] is True
    assert result["image_id"] == "abc-image-id"
    assert fake_client._transport._client.posts[0]["files"]["uploadfile[]"][0] == "photo.jpg"
    call = fake_client._transport.calls[0]
    assert call["path_template"] == "/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages/image"
    assert call["json_body"] == {"image_id": "abc-image-id"}
