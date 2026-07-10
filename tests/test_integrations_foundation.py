from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
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
from src.freelance_leads_bot.integrations.booking_flow import AvitoBookingFlow, BookingRequest
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
    pop_due_avito_turn_batches,
)
from src.freelance_leads_bot.integrations.avito_webhook import (
    annotate_avito_message_actor,
    app as avito_app,
    get_booking,
    get_handoff_notifier,
    get_history_store,
    get_photo_resolver,
    get_planner,
    get_reviewer,
    get_sender,
    get_settings,
    get_voice_resolver,
    process_avito_message,
    processed_events,
)
from src.freelance_leads_bot.integrations.avito_media import enrich_reply_handoff_photos
from src.freelance_leads_bot.integrations.codex_planner import build_codex_planner_prompt, parse_codex_step
from src.freelance_leads_bot.integrations.codex_review import (
    apply_review_outcome,
    build_codex_review_prompt,
    sanitize_consultation_language,
)
from src.freelance_leads_bot.integrations.avito_history_import import import_telegram_zip_to_knowledge, parse_telegram_html_export
from src.freelance_leads_bot.integrations.handoff_notify import PreviewHandoffNotifier, format_handoff_message
from src.freelance_leads_bot.integrations.handoff_refs import (
    find_telegram_handoff_ref,
    latest_unresolved_handoff_ref_for_chat,
    open_handoff_refs,
    remember_telegram_handoff_ref,
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
from src.freelance_leads_bot.integrations.prelaunch import build_prelaunch_report
import src.freelance_leads_bot.integrations.roles as roles_module
from src.freelance_leads_bot.integrations.roles import CodexRole, conversation_key, legacy_runtime_status, role_profile
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
from scripts.avito_unanswered_monitor import (
    _find_unanswered as find_unanswered_avito_chat,
    _report_item as report_unanswered_item,
    autoreply_once as autoreply_unanswered_once,
)
from scripts.avito_missed_message_poller import _should_process as should_process_missed_avito_message
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
    telegram_handoff_ref_context,
)


def test_prepare_avito_outgoing_text_removes_second_greeting_today(tmp_path) -> None:
    store = LeadStore(tmp_path / "history.sqlite3")
    remember_avito_outgoing(store, "chat-1", "Здравствуйте! Первый ответ.")

    text = prepare_avito_outgoing_text(store, "chat-1", "Добрый день! Продолжаем обсуждение.")

    assert text == "Продолжаем обсуждение."


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


@pytest.fixture(autouse=True)
def isolate_avito_processed_events(tmp_path):
    old_path = processed_events.path
    old_seen = processed_events.seen
    old_history_override = avito_app.dependency_overrides.get(get_history_store)
    processed_events.path = tmp_path / "avito_processed_events.json"
    processed_events.seen = {}
    avito_app.dependency_overrides[get_history_store] = lambda: LeadStore(tmp_path / "avito_history.sqlite3")
    try:
        yield
    finally:
        processed_events.path = old_path
        processed_events.seen = old_seen
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
    assert interaction["intent"] == "visit_fact_update"
    assert "Juvederm" in interaction["body"]


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
    assert profile.allows_tool("yclients.appointments.create")
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

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return {"ok": True, "result": {"message_id": len(self.messages)}}

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

    assert reply.action == "knowledge_answer"
    assert reply.handoff is None
    assert "беременности" in reply.reply
    assert "передам" not in reply.reply.lower()


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
    assert "knowledge.create" in seen_payload["available_tools"]
    assert any(tool["name"] == "knowledge.create" for tool in seen_payload["tool_schemas"])
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
async def test_avito_consultant_does_not_bypass_codex_for_photo_handoff() -> None:
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

    assert reply.metadata["planner"] == "codex_tool_loop"
    assert reply.action == "handoff"
    assert reply.handoff is not None
    assert reply.handoff.reason == "photo_consultation"
    assert "Codex решил" in reply.handoff.summary


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
    assert reply.reply == ""
    assert reply.handoff is not None
    assert reply.handoff.reason == "missing_data"
    assert "Клиенту пока ничего не писали" in reply.handoff.summary
    assert "Нужно у Ольги" not in reply.handoff.summary
    assert "Нужно: фото до/после" in reply.handoff.summary


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

    consultant = AvitoConsultant(toolbox, planner=CodexToolLoopPlanner(fake_codex_loop))
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
    assert knowledge.list(query="ботокс")
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
    message = avito_inbound_message({"type": "message", "chat_id": "chat-unlimited", "content": {"text": "Проверь слоты"}})

    reply = await consultant.respond(message)

    assert reply.action == "codex_reply"
    assert reply.reply == "Готово после расширенной проверки."
    assert calls == 8


@pytest.mark.anyio
async def test_codex_tool_loop_default_disables_step_cap(tmp_path) -> None:
    toolbox = AutomationToolbox(DryRunYClientsGateway(), JsonKnowledgeStore(tmp_path / "knowledge.json"))
    calls = 0

    async def fake_codex_loop(payload, trace):
        nonlocal calls
        calls += 1
        if calls <= 7:
            return {"tool_calls": [{"name": "yclients.services.list", "arguments": {}}]}
        return {"action": "codex_reply", "reply": "Готово без явного лимита."}

    consultant = AvitoConsultant(toolbox, planner=CodexToolLoopPlanner(fake_codex_loop))
    message = avito_inbound_message({"type": "message", "chat_id": "chat-default-unlimited", "content": {"text": "Проверь слоты"}})

    reply = await consultant.respond(message)

    assert reply.action == "codex_reply"
    assert reply.reply == "Готово без явного лимита."
    assert calls == 8


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
            "content": {"text": "Запишите меня, телефон +7 999 123-45-67"},
        }
    )

    reply = await consultant.respond(message)
    content = log_path.read_text(encoding="utf-8")

    assert reply.metadata["trace_log_path"] == str(log_path)
    assert "chat-redact" in content
    assert "[phone]" in content
    assert "+7 999 123-45-67" not in content
    assert '"phone": "[redacted]"' in content


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
        telegram_admin_history_db_path=Path("data/leads.sqlite3"),
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
        rag_expert_db_path=Path("data/expert_rag.sqlite3"),
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
        assert first.json()["action"] == "created"
        assert first.json()["dry_run"] is True
        assert first.json()["send"]["reason"] == "preview_only"
        assert len(gateway.appointments) == 1
        assert second.json()["ignored"] is True
        assert second.json()["reason"] == "duplicate"
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
        assert response.json()["action"] == "codex_reply"
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
        assert health.json()["integration_urls"]["webhook_url"] == "https://olgatihcosmo.com/yclients/webhook?secret=secret123"
        assert health.json()["integration_urls"]["callback_url"] == "https://olgatihcosmo.com/yclients/callback?secret=secret123"
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
    assert "Нужна ручная консультация" in text
    assert "Диалог: " in text
    assert "#" not in text
    assert "chat-photo" not in text
    assert "Консультация" in text

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
            self.photos = []

        def send_message(self, chat_id, text):
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

    assert result["photos_sent"] == 2
    assert result["photos_failed"] == 1
    assert len(result["photo_errors"]) == 1
    assert len(bot.photos) == 2


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
    assert "админским сообщением от Ольги" in prompt
    assert "персональный ассистент владельца бизнеса" in prompt
    assert "живой ассистент записи" in prompt
    assert "Город из объявления Avito" in prompt
    assert "без объяснения внутреннего маршрута" in prompt
    assert "reply=''" in prompt
    assert "фото до/после" in prompt
    assert "тихую задачу" in prompt
    assert "экспертной правке Ольги" in prompt
    assert "Не спамь онлайн-консультацией" in prompt
    assert "Если в conversation_history/trace/knowledge уже есть оценка Ольги" in prompt
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
    assert "фото посмотрят индивидуально" in outbox.read_text(encoding="utf-8")
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
