from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .config import ROOT
from .codex_runner import (
    CODEX_CHAT_HISTORY_LIMIT,
    analyze_with_codex,
    chat_with_codex,
    codex_auth_status,
    codex_logout_reset,
    start_codex_device_login,
    telegram_markdown_to_html,
)
from .media_recognition import RecognizedMedia, recognize_message_media
from .integrations.admin_codex import CodexTelegramAdminService
from .integrations.admin import AdminResult
from .integrations.agent_tools import AutomationToolbox, JsonKnowledgeStore
from .integrations.agent_trace import JsonlAgentTraceLogger
from .integrations.avito_identity import CLIENT_NAME_CACHE_PATH, clean_client_name, dialog_ref, legacy_hash_ref, load_client_name_cache
from .integrations.avito_read import avito_read_client_from_settings
from .integrations.avito_sender import avito_image_sender_from_settings, avito_sender_from_settings
from .integrations.avito_history import prepare_avito_outgoing_text, remember_avito_outgoing, sent_successfully
from .integrations.care_crm import (
    CareLearningService,
    CareCrmStore,
    ClientMemoryService,
    FollowupBrainService,
    VisitConfirmationService,
    format_visit_confirmation_card,
    parse_visit_confirmation_callback,
    visit_confirmation_keyboard,
    visit_details_update_result_text,
    visit_confirmation_result_text,
)
from .integrations.config import IntegrationSettings
from .integrations.expert_rag import APPROVED, ExpertRagStore
from .integrations.expert_rag_admin import (
    ExpertRagAdminService,
    format_rag_admin_plan,
    parse_rag_admin_callback,
    rag_admin_plan_keyboard,
)
from .integrations.rag_admin_intent import RagAdminIntentParser
from .integrations.handoff_refs import (
    DEFAULT_HANDOFF_REFS_PATH,
    find_telegram_handoff_ref,
    find_telegram_handoff_ref_by_text,
    find_telegram_handoff_ref_in_logs,
    find_telegram_handoff_ref_near_message_id,
    load_telegram_handoff_refs,
    open_handoff_refs,
    remember_telegram_handoff_ref,
    update_handoff_status,
)
from .integrations.handoff_notify import handoff_notifier_from_settings
from .integrations.codex_review import sanitize_consultation_language
from .integrations.roles import telegram_role_for_user
from .integrations.runtime import booking_from_settings
from .integrations.service_catalog import ServiceCatalogStore
from .integrations.telegram_client_bot import CareFollowupDeliveryService
from .integrations.telegram_admin_bot import (
    _booking_from_settings,
    _download_telegram_photo,
    _history_assistant_content,
    _history_user_content,
    _largest_telegram_photo,
)
from .mfa import delete_totp_secret, mfa_code_text, mfa_status, save_totp_secret
from .miniapp import start_miniapp_server
from .scanner import scan
from .storage import LeadStore
from .telegram import (
    TelegramBot,
    render_lead,
    render_leads_digest,
    send_leads,
)
from .tts import synthesize_voice, tts_enabled


HELP = """Команды:
/codex - включить чат с Codex CLI
/exit - выйти из Codex-чата
/codex_clear - очистить историю Codex-чата
/codex_auth - проверить вход Codex
/codex_login - получить код входа Codex
/codex_logout - сбросить токен Codex
/mfa - показать текущий MFA-код
/mfa_status - статус MFA
/mfa_set <secret или otpauth://...> - сохранить MFA
/mfa_delete - удалить MFA
/menu - пульт Codex
/flags - флаги интеграций и кнопки включения/выключения
/full_live_on - включить все live-флаги
/full_live_off - выключить все live-флаги
/olga_history - последние handoff-карточки для Ольги/админа
/open_cards - незакрытые handoff-карточки Ольги/админа
/visit_confirmations - карточки проверки сегодняшних визитов для допродаж
/care_followups - карточки due-задач отдела заботы
/client <телефон|имя> - карточка клиента локальной CRM
/learning - последние уроки отдела заботы
/bot_restart - применить env-флаги рестартом Telegram-бота
/terminal - открыть Mini App терминала
/help - помощь
"""

PAGE_SIZE = 8
MEDIA_GROUP_SETTLE_SECONDS = 1.2


OFFSET_PATH = Path("data/telegram_update_offset.txt")
RUNTIME_LOG_PATH = Path("data/bot_runtime.log")
RESTART_REQUEST_PATH = Path("data/restart_requested")
ATTACHMENT_DIRECTIVE_RE = re.compile(r"(?m)^\s*\[\[(send_file|send_photo):(.+?)\]\]\s*$")
AVITO_CHAT_ID_RE = re.compile(r"\b(u2i-[A-Za-z0-9_~\\-]+)\b")
AVITO_POLLER_SCRIPT = ROOT / "scripts" / "avito_missed_message_poller.py"
AVITO_UNANSWERED_MONITOR_SCRIPT = ROOT / "scripts" / "avito_unanswered_monitor.py"
AVITO_POLLER_SUPERVISOR_LOG_PATH = Path("data/avito_poller_supervisor.log")
AVITO_UNANSWERED_SUPERVISOR_LOG_PATH = Path("data/avito_unanswered_supervisor.log")
AVITO_WEBHOOK_APP = "src.freelance_leads_bot.integrations.avito_webhook:app"
AVITO_WEBHOOK_SUPERVISOR_LOG_PATH = Path("data/avito_webhook_supervisor.log")
AVITO_WEBHOOK_LOG_PATH = Path("data/avito_webhook.log")
HANDOFF_OUTBOX_PATH = Path("data/handoff_outbox.jsonl")
AVITO_POLLER_LOG_PATH = Path("data/avito_poller.log")
AVITO_DRAFTS_PATH = Path("data/avito_client_drafts.json")
TELEGRAM_HANDOFF_REFS_PATH = DEFAULT_HANDOFF_REFS_PATH
CODEX_TIMEOUT_ANSWER = "Codex не успел ответить за отведенное время."
CODEX_TIMEOUT_RESTART_MESSAGE = (
    "Codex упёрся в таймаут, но я применяю запрошенный рестарт после завершения активных задач. "
    "Перезапускаюсь через пару секунд."
)


@dataclass
class PendingMediaGroup:
    messages: list[dict] = field(default_factory=list)
    update_ids: list[int] = field(default_factory=list)
    business_connection_id: str | None = None
    timer: threading.Timer | None = None


@dataclass
class CodexTaskRegistry:
    active: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def try_start(self, context_key: str) -> bool:
        with self.lock:
            if context_key in self.active:
                return False
            self.active[context_key] = time.monotonic()
            return True

    def finish(self, context_key: str) -> None:
        with self.lock:
            self.active.pop(context_key, None)

    def active_count(self) -> int:
        with self.lock:
            return len(self.active)

    def has_active(self) -> bool:
        return self.active_count() > 0

    def active_age_seconds(self, context_key: str) -> float | None:
        with self.lock:
            started_at = self.active.get(context_key)
        if started_at is None:
            return None
        return max(0.0, time.monotonic() - started_at)


@dataclass(frozen=True)
class FeatureFlag:
    name: str
    description: str
    title: str = ""
    default: bool = False
    restart_required: bool = True

    @property
    def command(self) -> str:
        return "/" + self.name

    @property
    def short_command(self) -> str:
        return "/" + self.name.lower()

    @property
    def label(self) -> str:
        return self.title or self.name


FEATURE_FLAGS: tuple[FeatureFlag, ...] = (
    FeatureFlag("AVITO_WEBHOOK_AUTOSTART", "автозапуск Avito webhook-сервера на 127.0.0.1:8030", "Avito webhook"),
    FeatureFlag("AVITO_POLLER_AUTOSTART", "автозапуск Avito missed-message poller вместе с Telegram-ботом", "Avito poller"),
    FeatureFlag("AVITO_CODEX_ENABLED", "Codex отвечает клиентам Avito через webhook", "Avito Codex"),
    FeatureFlag("AVITO_SEND_ENABLED", "реальная отправка текстовых ответов клиентам Avito", "Avito text send"),
    FeatureFlag("AVITO_IMAGE_SEND_ENABLED", "реальная отправка фото клиентам Avito", "Avito image send", default=True),
    FeatureFlag("HANDOFF_NOTIFY_ENABLED", "уведомления Ольге/админу о ручных консультациях", "Handoff notify"),
    FeatureFlag("TELEGRAM_ADMIN_CODEX_ENABLED", "Codex-агент в админском Telegram-контуре", "Admin Codex", default=True),
    FeatureFlag("TELEGRAM_ADMIN_LIVE_DRAFTS_ENABLED", "live-черновики Codex в Telegram", "Live drafts", default=True),
    FeatureFlag("TELEGRAM_ADMIN_HISTORY_ENABLED", "память Telegram/Avito-диалогов в SQLite", "History", default=True),
    FeatureFlag("YCLIENTS_ALLOW_MUTATIONS", "реальные изменения записей YCLIENTS", "YCLIENTS write"),
    FeatureFlag("VK_CODEX_ENABLED", "Codex отвечает клиентам VK", "VK Codex"),
    FeatureFlag("VK_SEND_ENABLED", "реальная отправка ответов VK", "VK send"),
    FeatureFlag("TELEGRAM_ACCOUNT_LISTENER_ENABLED", "слушатель Telegram-аккаунта, если сборка его поддерживает", "TG listener"),
)

FEATURE_FLAG_BY_COMMAND = {flag.name.casefold(): flag for flag in FEATURE_FLAGS}
TRUE_VALUES = {"1", "true", "yes", "on", "вкл", "включить", "enable", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "выкл", "выключить", "disable", "disabled"}


def format_active_age(age: float | None) -> str:
    return "unknown" if age is None else str(int(age))


def runtime_log(message: str) -> None:
    RUNTIME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S%z")
    with RUNTIME_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def is_codex_timeout_answer(answer: str) -> bool:
    return str(answer or "").strip() == CODEX_TIMEOUT_ANSWER


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "")
    if value == "":
        return default
    return value.strip().casefold() in TRUE_VALUES


def feature_flag_state(flag: FeatureFlag) -> str:
    return "вкл" if env_bool(flag.name, flag.default) else "выкл"


def feature_flags_text() -> str:
    lines = ["<b>Флаги</b>"]
    lines.append("Общие режимы: <code>/full_live_on</code> и <code>/full_live_off</code>.")
    lines.append("")
    for flag in FEATURE_FLAGS:
        restart = " · нужен рестарт" if flag.restart_required else ""
        lines.append(f"<code>{flag.command}</code> - вкл/выкл - сейчас <b>{feature_flag_state(flag)}</b> - {escape(flag.description)}{restart}")
    lines.append("")
    lines.append("Пример: <code>/AVITO_POLLER_AUTOSTART вкл</code> или <code>/AVITO_POLLER_AUTOSTART выкл</code>.")
    lines.append("После env-флагов: <code>/bot_restart</code>, чтобы применить без ssh.")
    return "\n".join(lines)


def feature_flags_keyboard() -> dict:
    rows: list[list[dict[str, str]]] = [
        [
            {"text": "Полный запуск", "callback_data": "preset:live:ask"},
            {"text": "Полное отключение", "callback_data": "preset:off:ask"},
        ],
        [
            {"text": "История Ольги", "callback_data": "olga_history"},
            {"text": "Открытые карточки", "callback_data": "open_cards"},
        ],
        [
            {"text": "Обновить /flags", "callback_data": "flags"},
        ],
    ]
    for flag in FEATURE_FLAGS:
        state = feature_flag_state(flag)
        rows.append(
            [
                {"text": f"{flag.label}: {state}", "callback_data": f"flag:{flag.name}:status"},
                {"text": "Вкл", "callback_data": f"flag:{flag.name}:on"},
                {"text": "Выкл", "callback_data": f"flag:{flag.name}:off"},
            ]
        )
    rows.append(
        [
            {"text": "Codex чат вкл", "callback_data": "codexchat:on"},
            {"text": "Codex чат выкл", "callback_data": "codexchat:off"},
        ]
    )
    return {"inline_keyboard": rows}


def olga_history_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Обновить", "callback_data": "olga_history"},
                {"text": "Открытые", "callback_data": "open_cards"},
                {"text": "Меню", "callback_data": "menu"},
            ]
        ]
    }


def open_cards_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Обновить", "callback_data": "open_cards"},
                {"text": "История", "callback_data": "olga_history"},
                {"text": "Меню", "callback_data": "menu"},
            ]
        ]
    }


def feature_preset_confirm_keyboard(mode: str) -> dict:
    if mode == "live":
        return {
            "inline_keyboard": [
                [{"text": "Да, включить всё", "callback_data": "preset:live:confirm"}],
                [{"text": "Отмена", "callback_data": "flags"}],
            ]
        }
    return {
        "inline_keyboard": [
            [{"text": "Да, отключить всё", "callback_data": "preset:off:confirm"}],
            [{"text": "Отмена", "callback_data": "flags"}],
        ]
    }


def format_olga_history(
    *,
    limit: int = 12,
    webhook_log_path: Path | str = AVITO_WEBHOOK_LOG_PATH,
    handoff_outbox_path: Path | str = HANDOFF_OUTBOX_PATH,
    client_name_cache_path: Path | str = CLIENT_NAME_CACHE_PATH,
    poller_log_path: Path | str = AVITO_POLLER_LOG_PATH,
) -> str:
    entries = _olga_history_entries(
        Path(webhook_log_path),
        Path(handoff_outbox_path),
        client_name_cache_path=Path(client_name_cache_path),
        poller_log_path=Path(poller_log_path),
    )
    if not entries:
        return (
            "<b>История Ольги</b>\n"
            "Пока не нашёл handoff-карточек. Для новых обращений здесь будут видны карточки, "
            "которые бот передал Ольге/админу."
        )

    lines = ["<b>История Ольги</b>", "Последние handoff-карточки и проверки доставки:"]
    for entry in entries[:limit]:
        sent = entry.get("sent")
        sent_label = "да" if sent is True else "нет" if sent is False else "не логировалось"
        telegram_id = entry.get("telegram_message_id") or "-"
        lines.append(
            "\n".join(
                [
                    "",
                    f"<b>{escape(str(entry.get('time') or '-'))}</b>",
                    _history_client_line(str(entry.get("chat_id") or ""), str(entry.get("client_name") or "")),
                    f"Причина: <code>{escape(str(entry.get('reason') or '-'))}</code>",
                    f"Telegram: <code>{escape(str(telegram_id))}</code>, отправлено: <b>{sent_label}</b>",
                ]
            )
        )
        handoff_text = _short_history_text(
            _humanize_handoff_text(str(entry.get("handoff_text") or ""), str(entry.get("chat_id") or ""), str(entry.get("client_name") or "")),
            520,
        )
        if handoff_text:
            lines.append("Карточка Ольге:\n" + escape(handoff_text))
        client_reply = _short_history_text(str(entry.get("client_reply") or ""), 300)
        if client_reply:
            lines.append("Ответ клиенту:\n" + escape(client_reply))

    text = "\n".join(lines)
    if len(text) > 3900:
        return text[:3800].rstrip() + "\n\n..."
    return text


def format_open_cards(
    *,
    limit: int = 15,
    max_age_days: int | None = 14,
    webhook_log_path: Path | str = AVITO_WEBHOOK_LOG_PATH,
    handoff_outbox_path: Path | str = HANDOFF_OUTBOX_PATH,
    client_name_cache_path: Path | str = CLIENT_NAME_CACHE_PATH,
    poller_log_path: Path | str = AVITO_POLLER_LOG_PATH,
    drafts_path: Path | str = AVITO_DRAFTS_PATH,
) -> str:
    webhook_path = Path(webhook_log_path)
    entries = _olga_history_entries(
        webhook_path,
        Path(handoff_outbox_path),
        client_name_cache_path=Path(client_name_cache_path),
        poller_log_path=Path(poller_log_path),
    )
    min_ts = time.time() - max_age_days * 86400 if max_age_days is not None else 0
    closures = _open_cards_closures_by_chat(webhook_path, Path(drafts_path))
    open_entries: list[dict[str, object]] = []
    for entry in entries:
        chat_id = str(entry.get("chat_id") or "")
        ts = float(entry.get("ts") or 0)
        if not chat_id or ts < min_ts:
            continue
        closed = any(float(item.get("ts") or 0) > ts for item in closures.get(chat_id, {}).get("closed", []))
        if closed:
            continue
        pending = [
            item for item in closures.get(chat_id, {}).get("pending", [])
            if float(item.get("ts") or 0) > ts
        ]
        open_entry = dict(entry)
        if pending:
            open_entry["pending_draft"] = pending[-1]
        open_entries.append(open_entry)

    if not open_entries:
        suffix = f" за последние {max_age_days} дней" if max_age_days is not None else ""
        return f"<b>Открытые карточки</b>\nНе нашёл незакрытых handoff-карточек{suffix}."

    scope = f" за последние {max_age_days} дней" if max_age_days is not None else ""
    lines = ["<b>Открытые карточки</b>", f"Handoff-карточки{scope}, по которым не найден закрывающий ответ клиенту:"]
    for index, entry in enumerate(open_entries[:limit], start=1):
        telegram_id = entry.get("telegram_message_id") or "-"
        lines.append(
            "\n".join(
                [
                    "",
                    f"<b>{index}. {escape(str(entry.get('time') or '-'))}</b>",
                    _history_client_line(str(entry.get("chat_id") or ""), str(entry.get("client_name") or "")),
                    f"Причина: <code>{escape(str(entry.get('reason') or '-'))}</code>",
                    f"Telegram: <code>{escape(str(telegram_id))}</code>",
                ]
            )
        )
        pending = entry.get("pending_draft") if isinstance(entry.get("pending_draft"), dict) else {}
        if pending:
            lines.append("Статус: <b>черновик ждёт подтверждения</b>")
        handoff_text = _short_history_text(
            _humanize_handoff_text(str(entry.get("handoff_text") or ""), str(entry.get("chat_id") or ""), str(entry.get("client_name") or "")),
            520,
        )
        if handoff_text:
            lines.append("Карточка:\n" + escape(handoff_text))

    if len(open_entries) > limit:
        lines.append(f"\nПоказано {limit} из {len(open_entries)} открытых.")

    text = "\n".join(lines)
    if len(text) > 3900:
        return text[:3800].rstrip() + "\n\n..."
    return text


def reconcile_handoff_refs_from_drafts(
    *,
    drafts_path: Path | str = AVITO_DRAFTS_PATH,
    refs_path: Path | str = TELEGRAM_HANDOFF_REFS_PATH,
) -> None:
    drafts = load_avito_client_drafts(drafts_path)
    open_handoff_refs(refs_path)
    refs = load_telegram_handoff_refs(refs_path)
    drafts_changed = False
    ordered_drafts = sorted(
        drafts.items(),
        key=lambda item: int(item[1].get("created_at") or 0) if isinstance(item[1], dict) else 0,
    )
    for draft_id, draft in ordered_drafts:
        if not isinstance(draft, dict) or draft.get("handoff_id"):
            continue
        chat_id = str(draft.get("chat_id") or "")
        created_at = int(draft.get("created_at") or 0)
        candidates = [
            ref
            for ref in refs.values()
            if isinstance(ref, dict)
            and str(ref.get("avito_chat_id") or "") == chat_id
            and int(ref.get("created_at") or 0) <= created_at
        ]
        candidates.sort(key=lambda ref: int(ref.get("created_at") or 0), reverse=True)
        if not candidates:
            continue
        handoff_id = str(candidates[0].get("handoff_id") or "")
        if not handoff_id:
            continue
        draft["handoff_id"] = handoff_id
        drafts[draft_id] = draft
        drafts_changed = True
        status = str(draft.get("status") or "")
        if status == "sent" and sent_successfully(draft.get("send_result")):
            update_handoff_status(handoff_id, "closed", draft_id=draft_id, path=refs_path)
        elif status == "pending":
            update_handoff_status(handoff_id, "draft_pending", draft_id=draft_id, path=refs_path)
        elif status == "rejected":
            update_handoff_status(handoff_id, "rejected", draft_id=draft_id, path=refs_path)
    if drafts_changed:
        save_avito_client_drafts(drafts, drafts_path)


def send_open_handoff_cards(
    bot: TelegramBot,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
    *,
    limit: int = 15,
) -> int:
    reconcile_handoff_refs_from_drafts()
    rows = open_handoff_refs()
    if not rows:
        bot.send_message(telegram_chat_id, "<b>Открытые handoff-карточки</b>\nНезакрытых карточек нет.", **(topic_params or {}))
        return 0
    bot.send_message(
        telegram_chat_id,
        f"<b>Открытые handoff-карточки: {min(len(rows), limit)}</b>\nОтветьте на нужную карточку текстом, голосом или фото.",
        **(topic_params or {}),
    )
    for ref in rows[:limit]:
        status = str(ref.get("status") or "open")
        status_line = {
            "draft_pending": "Статус: черновик ждёт подтверждения",
            "rejected": "Статус: предыдущий черновик отклонён, карточка остаётся открытой",
        }.get(status, "Статус: ждёт ответа")
        text = f"{escape(str(ref.get('handoff_text') or '').strip())}\n\n<b>{escape(status_line)}</b>"
        response = bot.send_message(telegram_chat_id, text, **(topic_params or {}))
        message_id = str((response.get("result") or {}).get("message_id") or "")
        if message_id:
            remember_telegram_handoff_ref(
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=message_id,
                avito_chat_id=str(ref.get("avito_chat_id") or ""),
                client_name=str(ref.get("client_name") or ""),
                handoff_text=str(ref.get("handoff_text") or ""),
                handoff_id=str(ref.get("handoff_id") or ""),
                source_message_id=str(ref.get("source_message_id") or ""),
                status=status,
            )
    return min(len(rows), limit)


def _open_cards_closures_by_chat(webhook_log_path: Path, drafts_path: Path) -> dict[str, dict[str, list[dict[str, object]]]]:
    closures: dict[str, dict[str, list[dict[str, object]]]] = {}
    initial_reply_ids: set[str] = set()
    for row in _iter_jsonl(webhook_log_path):
        if row.get("event") == "processed" and row.get("handoff"):
            response = row.get("send") if isinstance(row.get("send"), dict) else {}
            response = response.get("response") if isinstance(response.get("response"), dict) else {}
            response_id = str(response.get("id") or "")
            if response_id:
                initial_reply_ids.add(response_id)

    for row in _iter_jsonl(webhook_log_path):
        if row.get("event") != "ignored" or row.get("reason") != "own_message":
            continue
        message_id = str(row.get("message_id") or "")
        if message_id in initial_reply_ids:
            continue
        chat_id = str(row.get("chat_id") or "")
        if not chat_id:
            continue
        closures.setdefault(chat_id, {"closed": [], "pending": []})["closed"].append(
            {"ts": _row_timestamp(row), "text": str(row.get("text_preview") or "")}
        )

    for draft in load_avito_client_drafts(drafts_path).values():
        if not isinstance(draft, dict):
            continue
        chat_id = str(draft.get("chat_id") or "")
        if not chat_id:
            continue
        status = str(draft.get("status") or "")
        ts = float(draft.get("updated_at") or draft.get("created_at") or 0)
        bucket = "closed" if status == "sent" else "pending" if status == "pending" else ""
        if bucket:
            closures.setdefault(chat_id, {"closed": [], "pending": []})[bucket].append(
                {"ts": ts, "draft_id": str(draft.get("id") or ""), "status": status}
            )
    return closures


def _olga_history_entries(
    webhook_log_path: Path,
    handoff_outbox_path: Path,
    *,
    client_name_cache_path: Path = CLIENT_NAME_CACHE_PATH,
    poller_log_path: Path = AVITO_POLLER_LOG_PATH,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    client_names = _client_name_lookup(client_name_cache_path, poller_log_path)

    for row in _iter_jsonl(webhook_log_path):
        if row.get("event") not in {"processed", "debounce_batch_processed"} or not row.get("handoff"):
            continue
        chat_id = str(row.get("chat_id") or "")
        if chat_id.startswith("chat-"):
            continue
        message_id = str(row.get("message_id") or "")
        key = (chat_id, message_id)
        if key in seen:
            continue
        seen.add(key)
        handoff_notify = row.get("handoff_notify") if isinstance(row.get("handoff_notify"), dict) else {}
        ts = _row_timestamp(row)
        entries.append(
            {
                "ts": ts,
                "time": _format_history_time(ts),
                "chat_id": chat_id,
                "message_id": message_id,
                "client_name": client_names.get(chat_id, ""),
                "reason": row.get("handoff"),
                "sent": handoff_notify.get("sent") if isinstance(handoff_notify, dict) else None,
                "telegram_message_id": _telegram_message_id(handoff_notify),
                "handoff_text": handoff_notify.get("text") if isinstance(handoff_notify, dict) else "",
                "client_reply": _client_reply_text(row.get("send")),
            }
        )

    for row in _iter_jsonl(handoff_outbox_path):
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        chat_id = str(message.get("chat_id") or row.get("chat_id") or "")
        message_id = str(message.get("message_id") or row.get("message_id") or "")
        key = (chat_id, message_id)
        if key in seen:
            continue
        seen.add(key)
        ts = _row_timestamp(row)
        entries.append(
            {
                "ts": ts,
                "time": _format_history_time(ts),
                "chat_id": chat_id,
                "message_id": message_id,
                "client_name": clean_client_name(metadata.get("client_name")) or client_names.get(chat_id, ""),
                "reason": row.get("reason"),
                "sent": False,
                "telegram_message_id": "-",
                "handoff_text": row.get("text") or row.get("summary") or "",
                "client_reply": "",
            }
        )

    entries.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return entries


def _iter_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_timestamp(row: dict[str, object]) -> float:
    raw_ts = row.get("ts")
    if isinstance(raw_ts, (int, float)):
        return float(raw_ts)
    created_at = str(row.get("created_at") or "")
    if created_at:
        try:
            return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _format_history_time(ts: float) -> str:
    if ts <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _telegram_message_id(handoff_notify: object) -> object:
    if not isinstance(handoff_notify, dict):
        return ""
    telegram = handoff_notify.get("telegram")
    if not isinstance(telegram, dict):
        return ""
    result = telegram.get("result")
    if isinstance(result, dict):
        return result.get("message_id") or ""
    return ""


def _client_reply_text(send: object) -> str:
    if not isinstance(send, dict):
        return ""
    response = send.get("response")
    if not isinstance(response, dict):
        return ""
    content = response.get("content")
    if not isinstance(content, dict):
        return ""
    return str(content.get("text") or "")


def _short_history_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _history_client_line(chat_id: str, client_name: str) -> str:
    name = clean_client_name(client_name)
    if name:
        return f"Клиент Avito: <b>{escape(name)}</b>"
    return f"Диалог Avito: <code>{escape(dialog_ref(chat_id))}</code>"


def _humanize_handoff_text(text: str, chat_id: str, client_name: str = "") -> str:
    if not text or not chat_id:
        return text
    name = clean_client_name(client_name)
    replacement = f"Клиент: {name}" if name else f"Диалог: {dialog_ref(chat_id)}"
    text = text.replace(f"Чат: {chat_id}", replacement).replace(f"Диалог: {legacy_hash_ref(chat_id)}", replacement).replace(
        f"Диалог: {dialog_ref(chat_id)}", replacement
    )
    if name:
        text = re.sub(r"Диалог:\s*\S+", replacement, text)
    return text


def _client_name_lookup(client_name_cache_path: Path, poller_log_path: Path) -> dict[str, str]:
    names = load_client_name_cache(client_name_cache_path)
    if not poller_log_path.exists():
        return names
    for row in _iter_jsonl(poller_log_path):
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        chat_id = str(row.get("chat_id") or message.get("chat_id") or "")
        name = clean_client_name(metadata.get("client_name"))
        if not name:
            raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
            chat = raw.get("chat") if isinstance(raw.get("chat"), dict) else {}
            users = chat.get("users") if isinstance(chat.get("users"), list) else []
            account_id = str(metadata.get("account_id") or "")
            author_id = str(metadata.get("author_id") or "")
            for user in users:
                if not isinstance(user, dict):
                    continue
                if author_id and str(user.get("id") or "") != author_id:
                    continue
                name = clean_client_name(user.get("name"))
                if name:
                    break
            if not name:
                for user in users:
                    if not isinstance(user, dict) or str(user.get("id") or "") == account_id:
                        continue
                    name = clean_client_name(user.get("name"))
                    if name:
                        break
        if chat_id and name:
            names[chat_id] = name
    return names


def load_avito_client_drafts(path: Path | str = AVITO_DRAFTS_PATH) -> dict[str, dict]:
    draft_path = Path(path)
    if not draft_path.exists():
        return {}
    try:
        raw = json.loads(draft_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_avito_client_drafts(drafts: dict[str, dict], path: Path | str = AVITO_DRAFTS_PATH) -> None:
    draft_path = Path(path)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(json.dumps(drafts, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")


def avito_draft_from_result(result: AdminResult) -> dict[str, str]:
    outcome = result.metadata.get("outcome") if isinstance(result.metadata, dict) else {}
    if result.action != "avito_client_draft" or not isinstance(outcome, dict):
        return {}
    chat_id = str(outcome.get("chat_id") or outcome.get("avito_chat_id") or "").strip()
    draft_text = str(outcome.get("draft_text") or outcome.get("client_text") or outcome.get("text") or "").strip()
    draft_text, _changed = sanitize_consultation_language(draft_text)
    if not chat_id or not draft_text:
        return {}
    return {"chat_id": chat_id, "draft_text": draft_text}


def avito_draft_keyboard(draft_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Да, отправить", "callback_data": f"avdraft:{draft_id}:send"},
                {"text": "Нет", "callback_data": f"avdraft:{draft_id}:reject"},
            ],
            [
                {"text": "Запомнить", "callback_data": f"avdraft:{draft_id}:remember"},
                {"text": "Не запоминать", "callback_data": f"avdraft:{draft_id}:forget"},
            ]
        ]
    }


def format_avito_client_draft_card(draft: dict) -> str:
    client_label = clean_client_name(draft.get("client_name")) or dialog_ref(str(draft.get("chat_id") or ""))
    draft_text, _changed = sanitize_consultation_language(str(draft.get("draft_text") or ""))
    return (
        "<b>Черновик ответа клиенту Avito</b>\n"
        f"Клиент/диалог: <b>{escape(client_label)}</b>\n\n"
        f"{escape(draft_text)}\n\n"
        "Можно нажать <b>Да</b>, чтобы отправить, <b>Нет</b>, чтобы не отправлять, "
        "<b>Запомнить</b>, чтобы сохранить как проверенное знание для похожих вопросов, "
        "или ответить на это сообщение своей правкой — Codex сам решит, нужен новый черновик или отправка."
    )


def send_avito_client_draft_card(
    bot: TelegramBot,
    *,
    telegram_chat_id: str,
    history_key: str,
    chat_id: str,
    draft_text: str,
    handoff_id: str = "",
    topic_params: dict[str, str] | None = None,
) -> dict:
    draft_text, _changed = sanitize_consultation_language(draft_text)
    draft_id = uuid4().hex[:12]
    drafts = load_avito_client_drafts()
    draft = {
        "id": draft_id,
        "status": "pending",
        "chat_id": chat_id,
        "draft_text": draft_text,
        "client_name": load_client_name_cache().get(chat_id, ""),
        "history_key": history_key,
        "handoff_id": str(handoff_id or ""),
        "created_at": int(time.time()),
        "telegram_chat_id": str(telegram_chat_id),
        "telegram_message_id": "",
    }
    response = bot.send_message(
        telegram_chat_id,
        format_avito_client_draft_card(draft),
        reply_markup=avito_draft_keyboard(draft_id),
        **(topic_params or {}),
    )
    message_id = str((response.get("result") or {}).get("message_id") or "")
    draft["telegram_message_id"] = message_id
    drafts[draft_id] = draft
    save_avito_client_drafts(drafts)
    if handoff_id:
        update_handoff_status(handoff_id, "draft_pending", draft_id=draft_id)
    return draft


def find_avito_draft_for_reply(message: dict) -> dict | None:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return None
    reply_message_id = str(reply.get("message_id") or "")
    chat_id = str((message.get("chat") or {}).get("id") or "")
    if not reply_message_id or not chat_id:
        return None
    for draft in load_avito_client_drafts().values():
        if str(draft.get("telegram_chat_id") or "") == chat_id and str(draft.get("telegram_message_id") or "") == reply_message_id:
            return draft
    return None


def avito_draft_revision_prompt(draft: dict, olga_text: str) -> str:
    return (
        "Ольга/админ ответила на карточку черновика для клиента Avito.\n"
        f"Avito chat_id: {draft.get('chat_id')}\n"
        f"Текущий черновик клиенту:\n{draft.get('draft_text')}\n\n"
        f"Ответ Ольги/админа на черновик:\n{olga_text}\n\n"
        "Сам оцени её сообщение. Если это явное подтверждение отправки — отправь черновик или аккуратно обновлённый текст через avito.messages.send. "
        "Если это правка или новое экспертное уточнение — подготовь новый клиентский черновик и верни action='avito_client_draft' с chat_id и draft_text. "
        "Голосовые сообщения часто распознаются как короткий фрагмент без слов «да» или «отправь»: если в таком ответе есть профессиональная оценка, цифры, запрет, разрешение или рекомендация по сути текущего черновика, считай это экспертным уточнением и готовь новый черновик, а не проси ещё одно подтверждение. "
        "Если она отказала или данных не хватает — не отправляй клиенту, коротко ответь Ольге что нужно."
    )


def _handoff_ref_for_draft(draft: dict) -> dict:
    handoff_id = str(draft.get("handoff_id") or "").strip()
    if not handoff_id:
        return {}
    for ref in load_telegram_handoff_refs().values():
        if isinstance(ref, dict) and str(ref.get("handoff_id") or "") == handoff_id:
            return ref
    return {}


def _client_question_from_handoff_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = re.search(r"(?ms)^Сообщение:\s*(.+?)(?:\nКонтекст:|\nМедиа:|\Z)", raw)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return raw[:800]


def remember_avito_draft_expert_answer(draft: dict, settings: IntegrationSettings) -> tuple[bool, str]:
    ref = _handoff_ref_for_draft(draft)
    handoff_text = str(ref.get("handoff_text") or draft.get("handoff_text") or "").strip()
    question = _client_question_from_handoff_text(handoff_text)
    answer = str(draft.get("sent_text") or draft.get("draft_text") or "").strip()
    if not question:
        return False, "Не нашёл исходный вопрос клиента для RAG."
    if not answer:
        return False, "Не нашёл текст клиентского ответа для RAG."
    store = ExpertRagStore(settings.rag_expert_db_path)
    item = store.upsert_from_handoff(
        question=question,
        answer_client=answer,
        answer_internal=handoff_text,
        source_chat_id=str(draft.get("chat_id") or ref.get("avito_chat_id") or ""),
        source_message_id=str(ref.get("source_message_id") or ""),
        olga_reply_message_id=str(draft.get("telegram_message_id") or ""),
        approved_by="olga",
        status=APPROVED,
        metadata={
            "source": "telegram_avito_draft_button",
            "draft_id": str(draft.get("id") or ""),
            "handoff_id": str(draft.get("handoff_id") or ""),
            "autoanswer_allowed": True,
        },
    )
    return True, f"Запомнила как approved knowledge #{item.id}."


def handle_avito_draft_callback(
    *,
    bot: TelegramBot,
    callback_id: str,
    data: str,
    settings: IntegrationSettings,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
) -> bool:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "avdraft":
        return False
    draft_id, action = parts[1], parts[2]
    drafts = load_avito_client_drafts()
    draft = drafts.get(draft_id)
    if not isinstance(draft, dict):
        bot.answer_callback_query(callback_id, "Черновик не найден")
        return True
    if action == "reject":
        draft["status"] = "rejected"
        draft["updated_at"] = int(time.time())
        drafts[draft_id] = draft
        save_avito_client_drafts(drafts)
        update_handoff_status(str(draft.get("handoff_id") or ""), "rejected", draft_id=draft_id)
        bot.answer_callback_query(callback_id, "Не отправляю")
        bot.send_message(
            telegram_chat_id,
            "Ок, не отправляю. Можно ответить на карточку черновика правкой, если нужно подготовить новый вариант.",
            **(topic_params or {}),
        )
        return True
    if action == "forget":
        draft["remember_status"] = "skipped"
        draft["remembered_at"] = int(time.time())
        drafts[draft_id] = draft
        save_avito_client_drafts(drafts)
        bot.answer_callback_query(callback_id, "Не запоминаю")
        bot.send_message(
            telegram_chat_id,
            "Ок, этот черновик не добавляю в RAG-память.",
            **(topic_params or {}),
        )
        return True
    if action == "remember":
        ok, message = remember_avito_draft_expert_answer(draft, settings)
        draft["remember_status"] = "approved" if ok else "failed"
        draft["remembered_at"] = int(time.time())
        draft["remember_message"] = message
        drafts[draft_id] = draft
        save_avito_client_drafts(drafts)
        bot.answer_callback_query(callback_id, "Запомнила" if ok else "Не запомнила")
        bot.send_message(telegram_chat_id, escape(message), **(topic_params or {}))
        return True
    if action != "send":
        bot.answer_callback_query(callback_id, "Неизвестное действие")
        return True
    try:
        history_store = LeadStore(settings.telegram_admin_history_db_path)
        draft_text, changed = sanitize_consultation_language(str(draft.get("draft_text") or ""))
        if changed:
            draft["draft_text"] = draft_text
        outgoing_text = prepare_avito_outgoing_text(history_store, str(draft.get("chat_id") or ""), draft_text)
        result = asyncio.run(
            avito_sender_from_settings(settings).send_message(
                settings.avito_account_id,
                str(draft.get("chat_id") or ""),
                outgoing_text,
            )
        )
    except Exception as exc:
        bot.answer_callback_query(callback_id, "Не отправилось")
        bot.send_message(
            telegram_chat_id,
            "Не удалось отправить черновик в Avito: " + escape(str(exc)),
            **(topic_params or {}),
        )
        return True
    draft["status"] = "sent" if result.get("sent") else "send_failed"
    draft["updated_at"] = int(time.time())
    draft["send_result"] = result
    draft["sent_text"] = outgoing_text if "outgoing_text" in locals() else str(draft.get("draft_text") or "")
    drafts[draft_id] = draft
    save_avito_client_drafts(drafts)
    if sent_successfully(result):
        remember_avito_outgoing(history_store, str(draft.get("chat_id") or ""), outgoing_text)
        update_handoff_status(str(draft.get("handoff_id") or ""), "closed", draft_id=draft_id)
    bot.answer_callback_query(callback_id, "Отправлено" if result.get("sent") else "Не отправлено")
    bot.send_message(
        telegram_chat_id,
        "Черновик отправлен клиенту." if result.get("sent") else "Черновик не отправлен: " + escape(str(result)),
        **(topic_params or {}),
    )
    return True


def handle_rag_plan_callback(
    *,
    bot: TelegramBot,
    callback_id: str,
    data: str,
    service: CodexTelegramAdminService | None,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
) -> bool:
    parsed = parse_rag_admin_callback(data)
    if not parsed:
        return False
    if not service or not service.toolbox.expert_rag_admin:
        bot.answer_callback_query(callback_id, "RAG недоступен")
        bot.send_message(telegram_chat_id, "RAG-память сейчас недоступна.", **(topic_params or {}))
        return True
    plan_id, action = parsed
    admin = service.toolbox.expert_rag_admin
    try:
        if action == "apply":
            plan = admin.apply_plan(plan_id, actor="olga")
            bot.answer_callback_query(callback_id, "Применено")
            bot.send_message(
                telegram_chat_id,
                "Готово, применила изменения в RAG-памяти.\n\n" + escape(format_rag_admin_plan(plan, details=True)),
                **(topic_params or {}),
            )
            return True
        if action == "cancel":
            admin.cancel_plan(plan_id, actor="olga")
            bot.answer_callback_query(callback_id, "Отменено")
            bot.send_message(telegram_chat_id, "Ок, ничего не меняю в RAG-памяти.", **(topic_params or {}))
            return True
        if action == "details":
            plan = admin.get_plan(plan_id)
            bot.answer_callback_query(callback_id, "Подробнее")
            bot.send_message(
                telegram_chat_id,
                escape(format_rag_admin_plan(plan, details=True) if plan else "План не найден."),
                **(topic_params or {}),
            )
            return True
        if action == "edit":
            bot.answer_callback_query(callback_id, "Жду правку")
            bot.send_message(
                telegram_chat_id,
                "Ответьте на карточку RAG-плана своей правкой — я пересоберу план и снова попрошу подтверждение.",
                **(topic_params or {}),
            )
            return True
    except Exception as exc:
        bot.answer_callback_query(callback_id, "Не удалось")
        bot.send_message(telegram_chat_id, "Не удалось обработать RAG-план: " + escape(str(exc)), **(topic_params or {}))
        return True
    bot.answer_callback_query(callback_id, "Неизвестное действие")
    return True


def find_rag_plan_id_for_reply(message: dict) -> str:
    reply = message.get("reply_to_message") or {}
    markup = reply.get("reply_markup") or {}
    keyboard = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if isinstance(keyboard, list):
        for row in keyboard:
            if not isinstance(row, list):
                continue
            for button in row:
                parsed = parse_rag_admin_callback(str((button or {}).get("callback_data") or ""))
                if parsed:
                    return parsed[0]
    text = str(reply.get("text") or reply.get("caption") or "")
    match = re.search(r"(?im)^План:\s*([a-f0-9]{8,32})\b", text)
    return match.group(1) if match else ""


def handle_rag_plan_text_reply(
    *,
    bot: TelegramBot,
    message: dict,
    text: str,
    service: CodexTelegramAdminService | None,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
) -> bool:
    plan_id = find_rag_plan_id_for_reply(message)
    if not plan_id or not service or not service.toolbox.expert_rag_admin:
        return False
    plan = service.toolbox.expert_rag_admin.update_plan_from_text(plan_id, text, actor="olga")
    bot.send_message(
        telegram_chat_id,
        escape(format_rag_admin_plan(plan, details=True)),
        reply_markup=rag_admin_plan_keyboard(plan.id),
        **(topic_params or {}),
    )
    return True


def looks_like_rag_admin_command(text: str) -> bool:
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
            "tesoro теперь",
            "тесоро теперь",
        )
    )


def handle_rag_admin_freeform_command(
    *,
    bot: TelegramBot,
    text: str,
    service: CodexTelegramAdminService | None,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
) -> bool:
    if not looks_like_rag_admin_command(text) or not service or not service.toolbox.expert_rag_admin:
        return False
    plan = service.toolbox.expert_rag_admin.plan_change(text, actor="olga")
    bot.send_message(
        telegram_chat_id,
        escape(format_rag_admin_plan(plan, details=True)),
        reply_markup=rag_admin_plan_keyboard(plan.id),
        **(topic_params or {}),
    )
    return True


def parse_visit_confirmation_command_date(raw_text: str) -> str:
    parts = str(raw_text or "").split(maxsplit=1)
    if len(parts) < 2:
        return datetime.now().date().isoformat()
    raw_date = parts[1].strip()
    if not raw_date:
        return datetime.now().date().isoformat()
    iso_match = re.search(r"\b(20\d{2}-\d{1,2}-\d{1,2})\b", raw_date)
    if iso_match:
        return datetime.fromisoformat(iso_match.group(1)).date().isoformat()
    dmy_match = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", raw_date)
    if dmy_match:
        year = int(dmy_match.group(3) or datetime.now().year)
        if year < 100:
            year += 2000
        return datetime(year, int(dmy_match.group(2)), int(dmy_match.group(1))).date().isoformat()
    return datetime.now().date().isoformat()


def visit_confirmation_rows_for_day(settings: IntegrationSettings, day: str) -> list[dict]:
    async def load_rows() -> list[dict]:
        booking = booking_from_settings(settings)
        try:
            service = VisitConfirmationService(CareCrmStore(), booking)
            return await service.sync_day(day)
        finally:
            aclose = getattr(booking, "aclose", None)
            if callable(aclose):
                await aclose()

    return asyncio.run(load_rows())


def send_visit_confirmation_cards(
    bot: TelegramBot,
    chat_id: str,
    integration_settings: IntegrationSettings,
    command_text: str,
    topic_params: dict[str, str] | None = None,
) -> None:
    day = parse_visit_confirmation_command_date(command_text)
    store = CareCrmStore()
    try:
        rows = visit_confirmation_rows_for_day(integration_settings, day)
    except Exception as exc:
        bot.send_message(chat_id, "Не смогла получить записи YCLIENTS: " + escape(str(exc)), **(topic_params or {}))
        return
    if not rows:
        bot.send_message(chat_id, f"На {escape(day)} не нашла записей для проверки визитов.", **(topic_params or {}))
        return
    bot.send_message(
        chat_id,
        f"<b>Проверка визитов за {escape(day)}</b>\nКарточек: {len(rows)}.",
        **(topic_params or {}),
    )
    for row in rows:
        response = bot.send_message(
            chat_id,
            format_visit_confirmation_card(row),
            reply_markup=visit_confirmation_keyboard(int(row["id"])),
            **(topic_params or {}),
        )
        message_id = str((response.get("result") or {}).get("message_id") or "")
        if message_id:
            store.remember_confirmation_card(int(row["id"]), chat_id=chat_id, message_id=message_id)


def handle_visit_confirmation_callback(
    *,
    bot: TelegramBot,
    callback_id: str,
    data: str,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
) -> bool:
    action = parse_visit_confirmation_callback(data)
    if action is None:
        return False
    store = CareCrmStore()
    try:
        if action.action == "yes":
            row = store.mark_visit(action.appointment_id, attended=True, confirmed_by="telegram_button")
        elif action.action == "no":
            row = store.mark_visit(action.appointment_id, attended=False, confirmed_by="telegram_button")
        else:
            row = store.mark_needs_details(action.appointment_id, confirmed_by="telegram_button")
    except KeyError:
        bot.answer_callback_query(callback_id, "Запись не найдена")
        return True
    bot.answer_callback_query(callback_id, "Отмечено")
    bot.send_message(telegram_chat_id, visit_confirmation_result_text(row, action.action), **(topic_params or {}))
    return True


def visit_confirmation_reply_appointment(message: dict) -> dict | None:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return None
    reply_message_id = str(reply.get("message_id") or "").strip()
    chat_id = str((message.get("chat") or {}).get("id") or "").strip()
    if not chat_id or not reply_message_id:
        return None
    return CareCrmStore().find_appointment_by_confirmation_card(chat_id=chat_id, message_id=reply_message_id)


def handle_visit_confirmation_text_reply(
    bot: TelegramBot,
    *,
    message: dict,
    text: str,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
) -> bool:
    appointment = visit_confirmation_reply_appointment(message)
    if appointment is None:
        return False
    if not str(text or "").strip():
        return False
    store = CareCrmStore()
    try:
        row = store.apply_visit_details_from_text(
            int(appointment["id"]),
            text,
            confirmed_by="telegram_reply",
        )
    except KeyError:
        bot.send_message(telegram_chat_id, "Не нашла запись для этой карточки визита.", **(topic_params or {}))
        return True
    bot.send_message(telegram_chat_id, visit_details_update_result_text(row, text), **(topic_params or {}))
    return True


def care_followup_reply_task(message: dict) -> dict | None:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return None
    reply_message_id = str(reply.get("message_id") or "").strip()
    chat_id = str((message.get("chat") or {}).get("id") or "").strip()
    if not chat_id or not reply_message_id:
        return None
    return CareCrmStore().find_followup_by_card(chat_id=chat_id, message_id=reply_message_id)


def handle_care_followup_text_reply(
    bot: TelegramBot,
    *,
    message: dict,
    text: str,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
) -> bool:
    task = care_followup_reply_task(message)
    if task is None:
        return False
    draft = str(text or "").strip()
    if not draft:
        return False
    updated = FollowupBrainService(CareCrmStore()).rewrite_from_olga(int(task["id"]), text=draft, author="telegram_reply")
    if not updated:
        bot.send_message(telegram_chat_id, "Не нашла follow-up задачу для этой карточки.", **(topic_params or {}))
        return True
    bot.send_message(
        telegram_chat_id,
        "Запомнила новый черновик и сохранила правку как урок для отдела заботы.\n\n"
        f"Новый текст:\n<blockquote>{escape(str(updated.get('message_draft') or ''))}</blockquote>",
        **(topic_params or {}),
    )
    return True


def format_crm_client_card(client: dict, memory: dict) -> str:
    flags = []
    if client.get("do_not_contact"):
        flags.append("не писать")
    if client.get("complaint_risk"):
        flags.append("риск/жалоба")
    flag_line = f"\nФлаги: <b>{escape(', '.join(flags))}</b>" if flags else ""
    phone = str(client.get("phone") or "")
    phone_line = f"\nТелефон: <code>{escape(phone)}</code>" if phone else ""
    lines = [
        "<b>CRM-карточка клиента</b>",
        f"Клиент: <b>{escape(str(client.get('name') or 'без имени'))}</b>{phone_line}",
        f"Город: <b>{escape(str(client.get('city') or '-'))}</b>",
        f"Последний визит: <b>{escape(str(client.get('last_visit_at') or '-'))}</b>{flag_line}",
    ]
    visits = memory.get("visits") if isinstance(memory.get("visits"), list) else []
    if visits:
        lines.append("\n<b>Последние визиты</b>")
        for visit in visits[:5]:
            service = visit.get("actual_service_title") or visit.get("booked_service_title") or "-"
            lines.append(f"- {escape(str(visit.get('scheduled_at') or '-'))}: {escape(str(service))}")
    prefs = memory.get("preferences") if isinstance(memory.get("preferences"), list) else []
    if prefs:
        lines.append("\n<b>Предпочтения</b>")
        for pref in prefs[:5]:
            lines.append(f"- {escape(str(pref.get('preference_type') or '-'))}: {escape(str(pref.get('value') or '-'))}")
    links = memory.get("links") if isinstance(memory.get("links"), list) else []
    if links:
        lines.append("\n<b>Каналы</b>")
        for link in links[:5]:
            verified = "да" if link.get("verified") else "нет"
            lines.append(f"- {escape(str(link.get('channel') or '-'))}, verified: {verified}")
    return "\n".join(lines)


def send_crm_client_card(bot: TelegramBot, chat_id: str, command_text: str, topic_params: dict[str, str] | None = None) -> None:
    query = command_text.split(maxsplit=1)[1].strip() if len(command_text.split(maxsplit=1)) > 1 else ""
    if not query:
        bot.send_message(chat_id, "Пришлите так: <code>/client телефон или имя</code>.", **(topic_params or {}))
        return
    store = CareCrmStore()
    matches = store.search_clients(query, limit=5)
    if not matches:
        bot.send_message(chat_id, "Не нашла клиента в локальной CRM.", **(topic_params or {}))
        return
    if len(matches) > 1:
        names = "\n".join(f"- #{row['id']} {escape(str(row.get('name') or '-'))} {escape(str(row.get('phone') or ''))}" for row in matches)
        bot.send_message(chat_id, "Нашла несколько похожих клиентов:\n" + names + "\n\nУточните телефоном или более точным именем.", **(topic_params or {}))
        return
    client = matches[0]
    memory = ClientMemoryService(store).memory(int(client["id"]), include_internal=True)
    bot.send_message(chat_id, format_crm_client_card(client, memory), **(topic_params or {}))


def format_learning_lessons(rows: list[dict]) -> str:
    if not rows:
        return "<b>Уроки отдела заботы</b>\nПока уроков нет."
    lines = ["<b>Уроки отдела заботы</b>"]
    for row in rows[:15]:
        tags = str(row.get("tags") or "")
        tags_line = f" · {escape(tags)}" if tags else ""
        lines.append(f"\n#{row.get('id')} · {escape(str(row.get('source') or '-'))}{tags_line}\n{escape(str(row.get('lesson') or ''))}")
    return "\n".join(lines)


def send_learning_lessons(bot: TelegramBot, chat_id: str, command_text: str, topic_params: dict[str, str] | None = None) -> None:
    query = command_text.split(maxsplit=1)[1].strip() if len(command_text.split(maxsplit=1)) > 1 else ""
    rows = CareLearningService(CareCrmStore()).lessons(query=query, limit=15)
    bot.send_message(chat_id, format_learning_lessons(rows), **(topic_params or {}))


def care_followup_keyboard(task_id: int) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {"text": "Отправить", "callback_data": f"carefu:{task_id}:send"},
                {"text": "Пропустить", "callback_data": f"carefu:{task_id}:skip"},
            ],
            [
                {"text": "Переписать", "callback_data": f"carefu:{task_id}:rewrite"},
                {"text": "Спросить Ольгу", "callback_data": f"carefu:{task_id}:ask"},
            ],
            [{"text": "Не писать клиенту", "callback_data": f"carefu:{task_id}:no_contact"}],
        ]
    }


def parse_care_followup_callback(data: str) -> tuple[int, str] | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "carefu":
        return None
    try:
        task_id = int(parts[1])
    except ValueError:
        return None
    if parts[2] not in {"send", "skip", "rewrite", "ask", "no_contact"}:
        return None
    return task_id, parts[2]


def format_care_followup_card(task: dict) -> str:
    client = escape(str(task.get("client_name") or "клиент"))
    service = escape(str(task.get("actual_service_title") or "визит"))
    due_at = escape(str(task.get("due_at") or "-"))
    city = escape(str(task.get("city") or ""))
    draft = escape(str(task.get("message_draft") or ""))
    reason = escape(str(task.get("reason") or "Причина не записана."))
    confidence = escape(str(round(float(task.get("confidence") or 0), 2)))
    risk_level = escape(str(task.get("risk_level") or "unknown"))
    links = CareCrmStore().list_client_links(int(task.get("client_id") or 0), channel="telegram_client") if task.get("client_id") else []
    telegram_link = "есть" if any(str(link.get("chat_id") or "") for link in links) else "нет"
    status_bits = []
    if task.get("do_not_contact"):
        status_bits.append("не писать")
    if task.get("complaint_risk"):
        status_bits.append("риск/жалоба")
    status_line = "\nСтоп-флаги: <b>" + escape(", ".join(status_bits)) + "</b>" if status_bits else ""
    city_line = f"\nГород визита: <b>{city}</b>" if city else ""
    return (
        "<b>Задача отдела заботы</b>\n"
        f"Клиент: <b>{client}</b>\n"
        f"После визита: <b>{service}</b>{city_line}\n"
        f"Срок: <b>{due_at}</b>{status_line}\n"
        f"Риск: <b>{risk_level}</b>, уверенность: <b>{confidence}</b>, Telegram: <b>{telegram_link}</b>\n"
        f"Причина: {reason}\n\n"
        f"Черновик клиенту:\n<blockquote>{draft}</blockquote>"
    )


def send_care_followup_cards(
    bot: TelegramBot,
    chat_id: str,
    integration_settings: IntegrationSettings,
    topic_params: dict[str, str] | None = None,
) -> None:
    store = CareCrmStore()
    tasks = store.list_followup_tasks(status="planned", due_before=datetime.now().isoformat(), limit=20)
    if not tasks:
        bot.send_message(chat_id, "Due-задач отдела заботы сейчас нет.", **(topic_params or {}))
        return
    bot.send_message(chat_id, f"<b>Due-задачи отдела заботы</b>\nКарточек: {len(tasks)}.", **(topic_params or {}))
    for task in tasks:
        task_id = int(task["id"])
        enriched = FollowupBrainService(store).enrich_task(task_id) or task
        response = bot.send_message(
            chat_id,
            format_care_followup_card(enriched),
            reply_markup=care_followup_keyboard(task_id),
            **(topic_params or {}),
        )
        message_id = str((response.get("result") or {}).get("message_id") or "")
        if message_id:
            store.remember_followup_card(task_id, chat_id=chat_id, message_id=message_id)


def handle_care_followup_callback(
    *,
    bot: TelegramBot,
    callback_id: str,
    data: str,
    settings: IntegrationSettings,
    telegram_chat_id: str,
    topic_params: dict[str, str] | None = None,
) -> bool:
    parsed = parse_care_followup_callback(data)
    if parsed is None:
        return False
    task_id, action = parsed
    delivery = CareFollowupDeliveryService(CareCrmStore(), TelegramBot(settings.telegram_client_bot_token)) if settings.telegram_client_bot_token else None
    store = CareCrmStore()
    if action == "skip":
        result = CareFollowupDeliveryService(store, bot).skip_task(task_id)
        bot.answer_callback_query(callback_id, "Пропущено" if result.get("ok") else "Задача не найдена")
        bot.send_message(telegram_chat_id, "Задача отдела заботы пропущена." if result.get("ok") else "Не нашла задачу.", **(topic_params or {}))
        return True
    if action == "rewrite":
        store.update_followup_task(task_id, outcome="rewrite_requested")
        bot.answer_callback_query(callback_id, "Жду правку")
        bot.send_message(
            telegram_chat_id,
            "Ответьте текстом на эту карточку follow-up: я заменю черновик и сохраню правку как урок для агента.",
            **(topic_params or {}),
        )
        return True
    if action == "ask":
        store.update_followup_task(task_id, outcome="ask_olga")
        bot.answer_callback_query(callback_id, "Оставила как вопрос")
        bot.send_message(telegram_chat_id, "Ок, эту задачу оставила как требующую решения Ольги.", **(topic_params or {}))
        return True
    if action == "no_contact":
        task = store.get_followup_task(task_id)
        if task:
            store.update_client_flags(int(task["client_id"]), do_not_contact=True, consent_status="denied")
            store.update_followup_task(task_id, status="blocked", outcome="do_not_contact_by_olga")
        bot.answer_callback_query(callback_id, "Не писать")
        bot.send_message(telegram_chat_id, "Отметила клиента как «не писать» и заблокировала задачу.", **(topic_params or {}))
        return True
    if delivery is None:
        bot.answer_callback_query(callback_id, "Нет client bot token")
        bot.send_message(telegram_chat_id, "Не настроен TELEGRAM_CLIENT_BOT_TOKEN, отправить клиенту не могу.", **(topic_params or {}))
        return True
    result = asyncio.run(delivery.send_task(task_id))
    if result.get("ok"):
        bot.answer_callback_query(callback_id, "Отправлено")
        bot.send_message(telegram_chat_id, "Сообщение отдела заботы отправлено клиенту.", **(topic_params or {}))
        return True
    bot.answer_callback_query(callback_id, str(result.get("status") or "Не отправлено"))
    bot.send_message(telegram_chat_id, "Не отправила follow-up: " + escape(str(result)), **(topic_params or {}))
    return True


def parse_feature_flag_command(raw_text: str) -> tuple[FeatureFlag | None, str]:
    parts = raw_text.strip().split()
    if not parts:
        return None, ""
    command = parts[0].lstrip("/").split("@", 1)[0].casefold()
    flag = FEATURE_FLAG_BY_COMMAND.get(command)
    if flag is None:
        return None, ""
    action = parts[1].casefold() if len(parts) > 1 else "status"
    return flag, action


def set_feature_flag(
    flag: FeatureFlag,
    action: str,
    *,
    env_path: Path = ROOT / ".env",
) -> tuple[bool, str]:
    action = action.strip().casefold() or "status"
    current = env_bool(flag.name, flag.default)
    if action in {"status", "статус", "show", "показать"}:
        return current, _feature_flag_result_text(flag, current, changed=False)
    if action in TRUE_VALUES:
        wanted = True
    elif action in FALSE_VALUES:
        wanted = False
    else:
        raise ValueError("Используй вкл/выкл/status.")
    _write_env_value(env_path, flag.name, "true" if wanted else "false")
    os.environ[flag.name] = "true" if wanted else "false"
    runtime_log(f"feature_flag set {flag.name}={wanted}")
    return wanted, _feature_flag_result_text(flag, wanted, changed=(wanted != current))


def set_all_feature_flags(
    enabled: bool,
    *,
    store: LeadStore | None = None,
    env_path: Path = ROOT / ".env",
) -> str:
    changed: list[str] = []
    for flag in FEATURE_FLAGS:
        current = env_bool(flag.name, flag.default)
        wanted = enabled
        _write_env_value(env_path, flag.name, "true" if wanted else "false")
        os.environ[flag.name] = "true" if wanted else "false"
        if current != wanted:
            changed.append(flag.name)
    if store is not None:
        store.set_codex_chat_enabled(enabled)
    runtime_log(f"feature_preset {'live_on' if enabled else 'live_off'} changed={len(changed)}")
    state = "включены" if enabled else "выключены"
    title = "Полный запуск" if enabled else "Полное отключение"
    changed_text = ", ".join(changed) if changed else "изменений не было"
    return (
        f"<b>{title}</b>\n"
        f"Флаги {state}. Codex-чат тоже {'включён' if enabled else 'выключен'}.\n"
        f"Изменено: {escape(changed_text)}\n\n"
        "Применится после рестарта сервиса. Команда: /bot_restart"
    )


def _feature_flag_result_text(flag: FeatureFlag, value: bool, *, changed: bool) -> str:
    state = "включён" if value else "выключен"
    prefix = "Изменил" if changed else "Сейчас"
    restart = "\nПрименится после рестарта сервиса. Команда: /bot_restart" if flag.restart_required else ""
    return f"{prefix} <code>{flag.name}</code>: <b>{state}</b>\n{escape(flag.description)}{restart}"


def _write_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
            updated.append(f"{key}={value}")
            found = True
        else:
            updated.append(line)
    if not found:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(f"{key}={value}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def load_offset() -> int | None:
    try:
        value = OFFSET_PATH.read_text(encoding="utf-8").strip()
        return int(value) if value else None
    except (FileNotFoundError, ValueError):
        return None


def save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset), encoding="utf-8")


def consume_restart_request() -> bool:
    if not RESTART_REQUEST_PATH.exists():
        return False
    RESTART_REQUEST_PATH.unlink(missing_ok=True)
    return True


def is_process_running_with_arg(needle: str) -> bool:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return False
    current_pid = os.getpid()
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit() or int(pid_dir.name) == current_pid:
            continue
        try:
            cmdline = pid_dir.joinpath("cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if needle in cmdline:
            return True
    return False


def start_avito_poller_if_needed() -> None:
    if os.getenv("AVITO_POLLER_AUTOSTART", "false").lower() not in {"1", "true", "yes", "on"}:
        runtime_log("avito_poller skipped autostart_disabled")
        return
    if not AVITO_POLLER_SCRIPT.exists():
        runtime_log("avito_poller unavailable script_missing")
        return
    script_arg = str(AVITO_POLLER_SCRIPT)
    if is_process_running_with_arg(script_arg) or is_process_running_with_arg("scripts/avito_missed_message_poller.py"):
        runtime_log("avito_poller already_running")
        return
    AVITO_POLLER_SUPERVISOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = AVITO_POLLER_SUPERVISOR_LOG_PATH.open("ab")
    process = subprocess.Popen(
        [sys.executable, script_arg],
        cwd=str(ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runtime_log(f"avito_poller started pid={process.pid}")


def start_avito_unanswered_monitor_if_needed() -> None:
    if os.getenv("AVITO_UNANSWERED_AUTOSTART", "false").lower() not in {"1", "true", "yes", "on"}:
        runtime_log("avito_unanswered skipped autostart_disabled")
        return
    if not AVITO_UNANSWERED_MONITOR_SCRIPT.exists():
        runtime_log("avito_unanswered unavailable script_missing")
        return
    script_arg = str(AVITO_UNANSWERED_MONITOR_SCRIPT)
    if is_process_running_with_arg(script_arg) or is_process_running_with_arg("scripts/avito_unanswered_monitor.py"):
        runtime_log("avito_unanswered already_running")
        return
    command = [sys.executable, script_arg]
    if os.getenv("AVITO_UNANSWERED_AUTOREPLY_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
        command.append("--autoreply")
    AVITO_UNANSWERED_SUPERVISOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = AVITO_UNANSWERED_SUPERVISOR_LOG_PATH.open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runtime_log(f"avito_unanswered started pid={process.pid} autoreply={'--autoreply' in command}")


def start_avito_webhook_if_needed() -> None:
    if os.getenv("AVITO_WEBHOOK_AUTOSTART", "false").lower() not in {"1", "true", "yes", "on"}:
        runtime_log("avito_webhook skipped autostart_disabled")
        return
    host = os.getenv("AVITO_WEBHOOK_HOST", "127.0.0.1")
    port = os.getenv("AVITO_WEBHOOK_PORT", "8030")
    if is_process_running_with_arg(AVITO_WEBHOOK_APP) or is_process_running_with_arg(f"--port {port}"):
        runtime_log("avito_webhook already_running")
        return
    uvicorn_path = ROOT / ".venv" / "bin" / "uvicorn"
    command = [
        str(uvicorn_path) if uvicorn_path.exists() else "uvicorn",
        AVITO_WEBHOOK_APP,
        "--host",
        host,
        "--port",
        port,
    ]
    AVITO_WEBHOOK_SUPERVISOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = AVITO_WEBHOOK_SUPERVISOR_LOG_PATH.open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runtime_log(f"avito_webhook started pid={process.pid} host={host} port={port}")


def maybe_schedule_restart(
    bot: TelegramBot,
    chat_id: str,
    topic_params: dict[str, str] | None = None,
    active_codex_tasks: int = 0,
    restart_message: str | None = None,
) -> bool:
    if not RESTART_REQUEST_PATH.exists():
        return False
    if active_codex_tasks > 0:
        runtime_log(f"bot_restart deferred active_codex_tasks={active_codex_tasks}")
        return False
    if not consume_restart_request():
        return False
    bot.send_message(chat_id, restart_message or "Принял запрос на рестарт. Перезапускаюсь через пару секунд.", **(topic_params or {}))
    schedule_bot_restart()
    return True


def telegram_username(message: dict) -> str:
    sender = message.get("from") or {}
    username = str(sender.get("username") or "").strip().lower().lstrip("@")
    if username:
        return username
    chat = message.get("chat") or {}
    return str(chat.get("username") or "").strip().lower().lstrip("@")


def telegram_display_name(user: dict | None) -> str:
    if not isinstance(user, dict):
        return ""
    username = str(user.get("username") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    name = " ".join(
        str(user.get(key) or "").strip()
        for key in ("first_name", "last_name")
        if str(user.get(key) or "").strip()
    )
    return name


def telegram_chat_title(chat: dict | None) -> str:
    if not isinstance(chat, dict):
        return ""
    username = str(chat.get("username") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    return str(chat.get("title") or chat.get("first_name") or "").strip()


def is_allowed_telegram_message(message: dict, settings: Settings) -> bool:
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id == settings.telegram_chat_id:
        return True
    sender_id = str((message.get("from") or {}).get("id", "")).strip()
    if sender_id and sender_id.isdigit() and int(sender_id) in settings.allowed_telegram_user_ids:
        return True
    username = telegram_username(message)
    return bool(username and username in settings.allowed_telegram_usernames)


def telegram_message_visible_text(message: dict | None) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("text") or message.get("caption") or "").strip()


def telegram_message_media_kinds(message: dict | None) -> list[str]:
    if not isinstance(message, dict):
        return []
    return [
        kind
        for kind in ("photo", "voice", "audio", "document", "video", "sticker", "animation")
        if message.get(kind)
    ]


def media_group_key(message: dict) -> tuple[str, str, str] | None:
    media_group_id = str(message.get("media_group_id") or "").strip()
    if not media_group_id:
        return None
    chat_id = str((message.get("chat") or {}).get("id", ""))
    topic_params = telegram_topic_params(message)
    topic_id = topic_params.get("message_thread_id") or topic_params.get("direct_messages_topic_id") or ""
    return chat_id, topic_id, media_group_id


def media_group_sort_key(message: dict) -> int:
    try:
        return int(message.get("message_id") or 0)
    except (TypeError, ValueError):
        return 0


def recognize_media_messages(bot: TelegramBot, messages: list[dict]) -> RecognizedMedia | None:
    ordered_messages = sorted(messages, key=media_group_sort_key)
    if not ordered_messages:
        return None
    if len(ordered_messages) == 1:
        return recognize_message_media(bot, ordered_messages[0])

    recognized = []
    for message in ordered_messages:
        media = recognize_message_media(bot, message)
        if media is not None:
            recognized.append(media)
    if not recognized:
        return None

    kinds = {media.kind for media in recognized}
    kind = "album" if len(kinds) > 1 else f"{recognized[0].kind}_album"
    text = f"Альбом Telegram принят: {len(recognized)} медиа."
    prompt_lines = [f"Пользователь отправил Telegram-альбом: {len(recognized)} медиа."]
    for index, media in enumerate(recognized, start=1):
        prompt_lines.append(f"\n--- Медиа {index} из {len(recognized)} ({media.kind}) ---")
        prompt_lines.append(media.prompt_text)
    prompt_lines.append("\nПосмотри все медиа из альбома вместе и ответь на запрос пользователя одним сообщением.")
    return RecognizedMedia(kind, text, "\n".join(prompt_lines))


def avito_draft_reply_text_for_codex(bot: TelegramBot, message: dict, codex_user_text: str) -> str:
    parts = [codex_user_text.strip()] if codex_user_text.strip() else []
    media = recognize_message_media(bot, message)
    if media is not None and media.prompt_text.strip():
        parts.append(media.prompt_text.strip())
    return "\n\n".join(parts).strip()


def media_messages_context_for_codex(messages: list[dict]) -> str:
    contexts = []
    seen = set()
    for message in sorted(messages, key=media_group_sort_key):
        context = telegram_message_context_for_codex(message)
        if context and context not in seen:
            seen.add(context)
            contexts.append(context)
    return "\n\n".join(contexts).strip()


def avito_context_hint_from_history(history: list[dict], limit: int = 8) -> str:
    for item in reversed(history[-limit:]):
        content = str(item.get("content") or "")
        match = AVITO_CHAT_ID_RE.search(content)
        if match:
            return (
                "Активный Avito-контекст из истории этой Telegram-беседы:\n"
                f"Avito chat_id: {match.group(1)}\n"
                "Если Ольга просит ответить клиенту, используй этот chat_id в tool `avito.messages.send`."
            )
    return ""


def telegram_forward_origin_label(origin: dict | None) -> str:
    if not isinstance(origin, dict):
        return ""
    origin_type = str(origin.get("type") or "").strip()
    if origin_type == "user":
        name = telegram_display_name(origin.get("sender_user"))
        return f"переслано от пользователя {name}" if name else "переслано от пользователя"
    if origin_type == "hidden_user":
        name = str(origin.get("sender_user_name") or "").strip()
        return f"переслано от скрытого пользователя {name}" if name else "переслано от скрытого пользователя"
    if origin_type in {"chat", "channel"}:
        name = telegram_chat_title(origin.get("sender_chat") or origin.get("chat"))
        label = "канала" if origin_type == "channel" else "чата"
        return f"переслано из {label} {name}" if name else f"переслано из {label}"
    return "пересланное сообщение"


def telegram_embedded_message_context(label: str, message: dict | None) -> list[str]:
    if not isinstance(message, dict):
        return []
    text = telegram_message_visible_text(message)
    media_kinds = telegram_message_media_kinds(message)
    if not text and not media_kinds:
        return []
    lines: list[str] = []
    author = telegram_display_name(message.get("from")) or telegram_chat_title(message.get("sender_chat"))
    if author:
        lines.append(f"{label} от {author}:")
    else:
        lines.append(f"{label}:")
    if text:
        lines.append(text)
    else:
        lines.append(f"[без текста; тип: {', '.join(media_kinds)}]")
    return lines


def telegram_handoff_ref_for_message(
    message: dict,
    *,
    ref_path: Path | str | None = None,
    log_paths: list[Path | str] | None = None,
) -> dict | None:
    telegram_chat_id = str((message.get("chat") or {}).get("id") or "").strip()
    if not telegram_chat_id:
        return None
    ref = None
    reply = message.get("reply_to_message")
    if isinstance(reply, dict):
        reply_message_id = str(reply.get("message_id") or "").strip()
        if reply_message_id:
            ref = find_telegram_handoff_ref(telegram_chat_id, reply_message_id, ref_path or TELEGRAM_HANDOFF_REFS_PATH)
            if not ref:
                ref = find_telegram_handoff_ref_in_logs(
                    telegram_chat_id,
                    reply_message_id,
                    log_paths or [AVITO_WEBHOOK_LOG_PATH, AVITO_POLLER_LOG_PATH],
                )
            if not ref and telegram_handoff_preview_looks_like_card(telegram_handoff_embedded_text(message)):
                ref = find_telegram_handoff_ref_near_message_id(
                    telegram_chat_id,
                    reply_message_id,
                    ref_path or TELEGRAM_HANDOFF_REFS_PATH,
                )
    if not ref:
        ref = find_telegram_handoff_ref_by_text(
            telegram_chat_id,
            telegram_handoff_embedded_text(message),
            ref_path or TELEGRAM_HANDOFF_REFS_PATH,
        )
    if not ref:
        return None
    return ref


def telegram_handoff_ref_context(
    message: dict,
    *,
    ref_path: Path | str | None = None,
    log_paths: list[Path | str] | None = None,
) -> str:
    ref = telegram_handoff_ref_for_message(message, ref_path=ref_path, log_paths=log_paths)
    if not ref:
        return ""
    avito_chat_id = str(ref.get("avito_chat_id") or ref.get("chat_id") or "").strip()
    if not avito_chat_id:
        return ""
    lines = [
        "Служебная привязка ответа на Avito-карточку:",
        f"Avito chat_id: {avito_chat_id}",
        f"Handoff id: {ref.get('handoff_id') or ''}",
        "Если Ольга просит или даёт экспертный ответ для клиента, используй этот chat_id.",
    ]
    client_name = clean_client_name(ref.get("client_name"))
    if client_name:
        lines.append(f"Клиент Avito: {client_name}")
    handoff_text = str(ref.get("handoff_text") or "").strip()
    if handoff_text:
        lines.extend(["Оригинальная карточка:", handoff_text[-1200:]])
    return "\n".join(lines)


def telegram_handoff_preview_looks_like_card(text: str) -> bool:
    normalized = str(text or "").casefold()
    return (
        "нужна ручная проверка" in normalized
        or "нужна ручная консультация" in normalized
        or ("причина:" in normalized and "канал:" in normalized)
    )


def telegram_handoff_embedded_text(message: dict) -> str:
    snippets: list[str] = []
    for key in ("reply_to_message", "external_reply"):
        embedded = message.get(key)
        if isinstance(embedded, dict):
            text = telegram_message_visible_text(embedded)
            if text:
                snippets.append(text)
    quote = message.get("quote")
    if isinstance(quote, dict):
        quote_text = str(quote.get("text") or "").strip()
        if quote_text:
            snippets.append(quote_text)
    return "\n\n".join(snippets).strip()


def telegram_message_context_for_codex(message: dict) -> str:
    parts: list[str] = []
    main_text = telegram_message_visible_text(message)

    forward_label = telegram_forward_origin_label(message.get("forward_origin"))
    if forward_label:
        forwarded_lines = [f"Контекст Telegram: {forward_label}."]
        if main_text:
            forwarded_lines.extend(["Текст пересланного сообщения:", main_text])
        else:
            forwarded_lines.append("Текст пересланного сообщения не виден боту.")
        parts.insert(0, "\n".join(forwarded_lines))
    elif main_text:
        parts.append(main_text)

    reply_lines = telegram_embedded_message_context("Ответ на сообщение", message.get("reply_to_message"))
    if reply_lines:
        parts.append("\n".join(reply_lines))
    handoff_ref = telegram_handoff_ref_context(message)
    if handoff_ref:
        parts.append(handoff_ref)

    external_reply = message.get("external_reply")
    if isinstance(external_reply, dict) and external_reply:
        external_lines = telegram_embedded_message_context("Внешний ответ", external_reply)
        if external_lines:
            origin_label = telegram_forward_origin_label(external_reply.get("origin"))
            if origin_label:
                external_lines.insert(1, f"Источник: {origin_label}.")
            parts.append("\n".join(external_lines))

    quote = message.get("quote") or {}
    quote_text = str(quote.get("text") or "").strip() if isinstance(quote, dict) else ""
    if quote_text:
        parts.append("Цитата из Telegram:\n" + quote_text)

    return "\n\n".join(part for part in parts if part).strip()


def annotate_sender_for_codex(message: dict, text: str) -> str:
    username = telegram_username(message)
    text = telegram_message_context_for_codex(message) or text
    if not str(text or "").strip():
        return ""
    if not username:
        return text
    return f"Сообщение от Telegram-пользователя @{username}:\n{text}"


def telegram_topic_params(message: dict) -> dict[str, str]:
    params: dict[str, str] = {}
    if message.get("message_thread_id"):
        params["message_thread_id"] = str(message["message_thread_id"])
    direct_topic = message.get("direct_messages_topic") or {}
    if isinstance(direct_topic, dict) and direct_topic.get("topic_id"):
        params["direct_messages_topic_id"] = str(direct_topic["topic_id"])
    return params


def telegram_delivery_params(message: dict, business_connection_id: str | None = None) -> dict[str, str]:
    params = telegram_topic_params(message)
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    return params


def telegram_callback_delivery_target(
    callback: dict,
    fallback_chat_id: str,
    business_connection_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    message = callback.get("message")
    if not isinstance(message, dict):
        return fallback_chat_id, {}
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return fallback_chat_id, telegram_delivery_params(message, business_connection_id)
    return str(chat.get("id") or fallback_chat_id), telegram_delivery_params(message, business_connection_id)


def codex_history_key(message: dict) -> str:
    chat_id = str((message.get("chat") or {}).get("id", ""))
    topic_params = telegram_topic_params(message)
    if topic_params.get("message_thread_id"):
        return f"chat:{chat_id}:thread:{topic_params['message_thread_id']}"
    if topic_params.get("direct_messages_topic_id"):
        return f"chat:{chat_id}:direct:{topic_params['direct_messages_topic_id']}"
    return f"chat:{chat_id}"


def telegram_sender_key(message: dict) -> str:
    sender = message.get("from") or {}
    sender_id = str(sender.get("id") or "").strip()
    if sender_id:
        return f"user:{sender_id}"
    username = telegram_username(message)
    if username:
        return f"username:{username}"
    chat_id = str((message.get("chat") or {}).get("id", "")).strip()
    return f"chat:{chat_id}" if chat_id else "unknown"


def codex_history_prefix(history_key: str) -> str:
    for separator in (":thread:", ":direct:"):
        if separator in history_key:
            return history_key.split(separator, 1)[0]
    return history_key


def codex_busy_key(message: dict) -> str:
    return f"{codex_history_key(message)}:{telegram_sender_key(message)}"


def schedule_bot_restart() -> None:
    runtime_log("bot_restart scheduled")
    restart_script = ROOT / "restart_bot.sh"
    subprocess.Popen(
        ["bash", "-lc", f"sleep 2; {restart_script} >> {ROOT / 'data' / 'restart_bot.last.log'} 2>&1"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def hydrate_stored_lead(row: dict) -> dict:
    lead = dict(row)
    try:
        lead["estimate"] = json.loads(lead.get("estimate_json") or "{}")
    except json.JSONDecodeError:
        lead["estimate"] = {}
    return lead


def run_once(settings: Settings) -> None:
    store = LeadStore(settings.db_path)
    bot = TelegramBot(settings.telegram_bot_token)
    leads, errors = scan(settings, store)
    send_leads(bot, settings.telegram_chat_id, leads, errors)


def menu_text(store: LeadStore) -> str:
    codex_chat = "включён" if store.codex_chat_enabled() else "выключен"
    return (
        "<b>Codex пульт</b>\n"
        f"Codex чат: <b>{codex_chat}</b>\n\n"
        "Пиши задачу обычным сообщением после включения чата.\n\n"
        "Auth: /codex_auth, /codex_login, /codex_logout\n"
        "MFA: /mfa, /mfa_status, /mfa_set, /mfa_delete\n"
        "История Ольги: /olga_history\n"
        "Флаги: /flags, /full_live_on, /full_live_off или команды ниже\n\n"
        + "\n".join(
            f"<code>{flag.command}</code> - вкл/выкл - сейчас <b>{feature_flag_state(flag)}</b> - {escape(flag.description)}"
            for flag in FEATURE_FLAGS[:8]
        )
        + "\nЕщё: /flags"
    )


def status_text(store: LeadStore) -> str:
    stats = store.stats()
    radar = "включён" if store.radar_enabled() else "выключен"
    codex_chat = "включён" if store.codex_chat_enabled() else "выключен"
    return (
        f"<b>Статус</b>\n"
        f"Радар: {radar}\n"
        f"Codex чат: {codex_chat}\n"
        f"Всего лидов: {stats['total']}\n"
        f"Активных: {stats['active']}\n"
        f"Свежих за 24ч: {stats['fresh']}\n"
        f"Добавлено сегодня: {stats['today']}\n"
        f"Делаем: {stats['do']}\n"
        f"Не делаем: {stats['skip']}\n"
        f"Вакансий отложено: {stats['jobs']}\n"
        f"Research-only: {stats['research']}"
    )


def send_menu(bot: TelegramBot, chat_id: str, store: LeadStore, topic_params: dict[str, str] | None = None) -> None:
    bot.send_message(chat_id, menu_text(store), reply_markup=feature_flags_keyboard(), **(topic_params or {}))


def send_terminal_miniapp(bot: TelegramBot, chat_id: str, settings: Settings, topic_params: dict[str, str] | None = None) -> None:
    bot.send_web_app_button(
        chat_id,
        "<b>Server Mini App</b>\nТерминал и просмотр директорий доступны через кнопку ниже.",
        "Открыть терминал",
        settings.miniapp_public_url,
        **(topic_params or {}),
    )


def send_leads_page(bot: TelegramBot, chat_id: str, store: LeadStore, page: int) -> None:
    page = max(0, page)
    total = store.count_leads("active")
    leads = store.list_leads("active", PAGE_SIZE, page * PAGE_SIZE)
    if not leads:
        bot.send_message(chat_id, "Активных лидов пока нет.")
        return
    start = page * PAGE_SIZE + 1
    end = start + len(leads) - 1
    lines = [f"<b>Лиды {start}-{end} из {total}</b>"]
    for i, lead in enumerate(leads, start=start):
        estimate = (lead.get("estimate") or {}) if isinstance(lead.get("estimate"), dict) else {}
        try:
            estimate = json.loads(lead.get("estimate_json") or "{}")
        except json.JSONDecodeError:
            estimate = {}
        risk = estimate.get("risk", "?")
        channel = lead.get("apply_channel", "unknown")
        lines.append(
            f"{i}. {escape(lead['title'])} | {escape(str(channel))} | score {lead['score']} | риск {escape(str(risk))}"
        )
    bot.send_message(chat_id, "\n".join(lines))


def send_lead_card(bot: TelegramBot, chat_id: str, store: LeadStore, lead_id: str, page: int = 0) -> None:
    lead = store.get(lead_id)
    if not lead:
        bot.send_message(chat_id, "Лид не найден.")
        return
    bot.send_message(chat_id, render_lead(hydrate_stored_lead(lead)))


def resolve_project_file(path_text: str) -> Path:
    raw = path_text.strip().strip("\"'")
    if not raw:
        raise RuntimeError("Путь к файлу пустой.")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    resolved = path.resolve()
    root = ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Можно отправлять только файлы из проекта.") from exc
    if not resolved.is_file():
        raise RuntimeError(f"Файл не найден: {raw}")
    return resolved


def send_local_file(
    bot: TelegramBot,
    chat_id: str,
    path_text: str,
    as_photo: bool = False,
    topic_params: dict[str, str] | None = None,
) -> None:
    path = resolve_project_file(path_text)
    content_type = mimetypes.guess_type(path.name)[0] or ""
    topic_params = topic_params or {}
    if as_photo:
        if not content_type.startswith("image/"):
            raise RuntimeError("Для /sendphoto нужен файл изображения.")
        bot.send_photo(chat_id, path, caption=escape(path.name), **topic_params)
    else:
        bot.send_document(chat_id, path, caption=escape(path.name), **topic_params)


def send_codex_answer(bot: TelegramBot, chat_id: str, answer: str, topic_params: dict[str, str] | None = None) -> None:
    attachments: list[tuple[str, str]] = []
    topic_params = topic_params or {}

    def collect(match: re.Match[str]) -> str:
        attachments.append((match.group(1), match.group(2).strip()))
        return ""

    clean_answer = ATTACHMENT_DIRECTIVE_RE.sub(collect, answer).strip()
    bot.send_message(
        chat_id,
        "<b>Codex:</b>\n" + telegram_markdown_to_html(clean_answer or "Готово."),
        **topic_params,
    )
    for kind, path_text in attachments[:5]:
        try:
            send_local_file(bot, chat_id, path_text, as_photo=(kind == "send_photo"), topic_params=topic_params)
            runtime_log(f"codex_chat attachment_sent kind={kind} path={path_text}")
        except Exception as exc:
            runtime_log(f"codex_chat attachment_failed kind={kind} error={type(exc).__name__}")
            bot.send_message(chat_id, "Не удалось отправить файл: " + escape(str(exc)), **topic_params)
    if tts_enabled():
        try:
            voice_path = synthesize_voice(clean_answer or "Готово.")
            if voice_path:
                bot.send_voice(chat_id, voice_path, **topic_params)
                runtime_log(f"codex_chat voice_sent path={voice_path}")
        except Exception as exc:
            runtime_log(f"codex_chat voice_failed error={type(exc).__name__}")


def build_codex_tool_service(settings: IntegrationSettings) -> CodexTelegramAdminService:
    history_store = LeadStore(settings.telegram_admin_history_db_path)
    booking = _booking_from_settings(settings)
    toolbox = AutomationToolbox(
        booking,
        knowledge=JsonKnowledgeStore(),
        avito=avito_read_client_from_settings(settings),
        avito_sender=avito_sender_from_settings(settings),
        avito_image_sender=avito_image_sender_from_settings(settings),
        avito_account_id=settings.avito_account_id,
        enable_workspace_tools=True,
        history_store=history_store,
        operations_notifier=handoff_notifier_from_settings(settings),
        expert_rag_admin=(
            ExpertRagAdminService(
                ExpertRagStore(settings.rag_expert_db_path),
                intent_parser=RagAdminIntentParser(enabled=settings.rag_dynamic_intent_enabled),
                service_catalog=ServiceCatalogStore(settings.rag_service_catalog_path),
            )
            if settings.rag_retrieval_enabled
            else None
        ),
    )
    return CodexTelegramAdminService(toolbox, settings, trace_logger=JsonlAgentTraceLogger())


def telegram_user_id(message: dict | None) -> int:
    if not isinstance(message, dict):
        return 0
    try:
        return int((message.get("from") or {}).get("id") or 0)
    except (TypeError, ValueError):
        return 0


def codex_tool_role(message: dict | None, settings: IntegrationSettings):
    return telegram_role_for_user(
        telegram_user_id(message),
        admin_user_id=settings.telegram_admin_user_id,
        cosmetologist_user_id=settings.telegram_cosmetologist_user_id,
    )


def codex_tool_conversation_history(store: LeadStore, history_key: str, limit: int) -> list[dict]:
    return store.recent_codex_chat(limit, history_key)


def codex_tool_cross_topic_context(store: LeadStore, history_key: str, limit: int) -> str:
    parent_key = codex_history_prefix(history_key)
    if parent_key == history_key:
        return ""
    return avito_context_hint_from_history(store.recent_codex_chat_by_prefix(parent_key, limit))


def collect_telegram_photo_attachments(bot: TelegramBot, messages: list[dict]) -> list[dict]:
    attachments: list[dict] = []
    for message in sorted(messages, key=media_group_sort_key):
        photo = _largest_telegram_photo(message)
        if not photo:
            continue
        try:
            path = _download_telegram_photo(bot, str(photo["file_id"]))
        except (OSError, RuntimeError, KeyError):
            continue
        attachments.append(
            {
                "type": "photo",
                "source": "telegram_admin",
                "image_path": str(path),
                "file_id": str(photo.get("file_id") or ""),
                "width": photo.get("width"),
                "height": photo.get("height"),
            }
        )
    return attachments


def run_codex_tool_loop(
    service: CodexTelegramAdminService,
    integration_settings: IntegrationSettings,
    store: LeadStore,
    history_key: str,
    source_message: dict | None,
    text: str,
    attachments: list[dict],
    progress_callback: Callable[[str], None] | None = None,
) -> AdminResult:
    role = codex_tool_role(source_message, integration_settings)
    history_content = _history_user_content(text, attachments)
    store.add_codex_chat_message("user", history_content, history_key)
    message_payload = {
        "text": text,
        "channel": "telegram_admin",
        "codex_role": role.value,
        "chat_id": str(((source_message or {}).get("chat") or {}).get("id") or ""),
        "message_id": str((source_message or {}).get("message_id") or ""),
        "thread": telegram_topic_params(source_message or {}),
        "history_key": history_key,
        "conversation_key": history_key,
        "attachments": attachments,
        "conversation_history": codex_tool_conversation_history(store, history_key, CODEX_CHAT_HISTORY_LIMIT),
        "cross_topic_context": codex_tool_cross_topic_context(store, history_key, CODEX_CHAT_HISTORY_LIMIT),
    }
    result = asyncio.run(service.handle_message(message_payload, progress_callback=progress_callback))
    store.add_codex_chat_message("assistant", _history_assistant_content(result), history_key)
    return result


def start_telegram_account_listener(
    settings: Settings,
    store: LeadStore,
    codex_tasks: CodexTaskRegistry,
) -> None:
    runtime_log("account_listener unavailable in portable bot-only build")

def start_codex_chat_task(
    bot: TelegramBot,
    store: LeadStore,
    codex_tool_service: CodexTelegramAdminService | None,
    integration_settings: IntegrationSettings | None,
    chat_id: str,
    history_key: str,
    update_id: int,
    user_text: str | Callable[[], str],
    on_done: Callable[[], None],
    topic_params: dict[str, str] | None = None,
    initial_live_text: str = "Запустил задачу.",
    active_codex_tasks: Callable[[], int] | None = None,
    source_message: dict | None = None,
    attachments: list[dict] | Callable[[], list[dict]] | None = None,
    handoff_id: str = "",
) -> threading.Thread:
    draft_id = ((int(time.time() * 1000) << 8) ^ int(update_id)) % 2147483647 or 1
    topic_params = topic_params or {}
    draft_thread_id = topic_params.get("message_thread_id")
    business_connection_id = topic_params.get("business_connection_id")

    def worker() -> None:
        last_draft_at = 0.0
        draft_failed = False
        live_message_id: int | None = None
        runtime_log(f"codex_chat start update_id={update_id} draft_id={draft_id}")

        def render_live(progress: str) -> str:
            return "<b>Codex live:</b>\n" + telegram_markdown_to_html(progress)

        def stream_codex_progress(progress: str) -> None:
            nonlocal last_draft_at, draft_failed
            now = time.monotonic()
            live_text = render_live(progress)

            if not draft_failed and now - last_draft_at >= 1.2:
                last_draft_at = now
                try:
                    bot.send_message_draft(chat_id, draft_id, live_text, message_thread_id=draft_thread_id)
                    runtime_log(f"codex_chat draft_sent draft_id={draft_id} chars={len(progress)}")
                except RuntimeError:
                    draft_failed = True
                    runtime_log(f"codex_chat draft_failed draft_id={draft_id}")

        try:
            bot.send_chat_action(
                chat_id,
                message_thread_id=draft_thread_id,
                business_connection_id=business_connection_id,
            )
            live_response = bot.send_message(chat_id, f"<b>Codex live:</b>\n{escape(initial_live_text)}", **topic_params)
            live_message_id = int((live_response.get("result") or {}).get("message_id") or 0) or None
            if live_message_id is not None:
                runtime_log(f"codex_chat live_opened message_id={live_message_id}")
            bot.send_message_draft(chat_id, draft_id, "", message_thread_id=draft_thread_id)
            runtime_log(f"codex_chat draft_opened draft_id={draft_id}")
        except RuntimeError:
            draft_failed = True
            runtime_log(f"codex_chat draft_open_failed draft_id={draft_id}")

        try:
            if callable(user_text):
                resolved_user_text = user_text()
            else:
                resolved_user_text = user_text
            runtime_log(f"codex_chat prompt_ready update_id={update_id} chars={len(resolved_user_text)}")
            if callable(attachments):
                resolved_attachments = attachments()
            else:
                resolved_attachments = attachments or []
            codex_result: AdminResult | None = None
            if codex_tool_service is not None and integration_settings is not None:
                codex_result = run_codex_tool_loop(
                    codex_tool_service,
                    integration_settings,
                    store,
                    history_key,
                    source_message,
                    resolved_user_text,
                    resolved_attachments,
                    progress_callback=stream_codex_progress,
                )
                answer = codex_result.message
            else:
                store.add_codex_chat_message("user", resolved_user_text, history_key)
                history = store.recent_codex_chat(CODEX_CHAT_HISTORY_LIMIT, history_key)
                answer, _ = chat_with_codex(
                    resolved_user_text,
                    history,
                    progress_callback=stream_codex_progress,
                )
                store.add_codex_chat_message("codex", answer, history_key)
            runtime_log(f"codex_chat completed update_id={update_id} answer_chars={len(answer)}")
        except Exception as exc:
            answer = f"Codex чат не удался: {exc}"
            codex_result = None
            runtime_log(f"codex_chat failed update_id={update_id} error={type(exc).__name__}")
        try:
            final_sent = False
            draft = avito_draft_from_result(codex_result) if codex_result is not None else {}
            if draft:
                send_avito_client_draft_card(
                    bot,
                    telegram_chat_id=chat_id,
                    history_key=history_key,
                    chat_id=draft["chat_id"],
                    draft_text=draft["draft_text"],
                    handoff_id=handoff_id,
                    topic_params=topic_params,
                )
            else:
                send_codex_answer(bot, chat_id, answer, topic_params)
            final_sent = True
            runtime_log(f"codex_chat final_sent update_id={update_id}")
        finally:
            on_done()
            if final_sent:
                active_count = active_codex_tasks() if active_codex_tasks is not None else 0
                maybe_schedule_restart(
                    bot,
                    chat_id,
                    topic_params,
                    active_count,
                    restart_message=CODEX_TIMEOUT_RESTART_MESSAGE if is_codex_timeout_answer(answer) else None,
                )

    thread = threading.Thread(target=worker, name=f"codex-chat-{update_id}", daemon=True)
    thread.start()
    return thread


def start_media_recognition_task(
    bot: TelegramBot,
    chat_id: str,
    update_id: int,
    message: dict | list[dict],
    topic_params: dict[str, str] | None = None,
) -> None:
    topic_params = topic_params or {}
    draft_thread_id = topic_params.get("message_thread_id")
    business_connection_id = topic_params.get("business_connection_id")
    messages = message if isinstance(message, list) else [message]

    def worker() -> None:
        runtime_log(f"media_recognition start update_id={update_id}")
        try:
            bot.send_chat_action(
                chat_id,
                message_thread_id=draft_thread_id,
                business_connection_id=business_connection_id,
            )
            media = recognize_media_messages(bot, messages)
            if media is None:
                return
            titles = {
                "photo": "Фото принято",
                "photo_album": "Альбом фото принят",
                "album": "Альбом принят",
                "voice": "Голос распознан",
                "document": "Файл принят",
                "document_album": "Альбом файлов принят",
            }
            title = titles.get(media.kind, "Медиа принято")
            bot.send_message(chat_id, f"<b>{title}:</b>\n{escape(media.text)}", **topic_params)
            runtime_log(f"media_recognition completed update_id={update_id} kind={media.kind} chars={len(media.text)}")
        except Exception as exc:
            runtime_log(f"media_recognition failed update_id={update_id} error={type(exc).__name__}")
            bot.send_message(chat_id, "Не удалось распознать медиа: " + escape(str(exc)), **topic_params)

    threading.Thread(target=worker, name=f"media-recognition-{update_id}", daemon=True).start()


def serve(settings: Settings) -> None:
    store = LeadStore(settings.db_path)
    integration_settings = IntegrationSettings.from_env()
    codex_tool_service = build_codex_tool_service(integration_settings)
    bot = TelegramBot(settings.telegram_bot_token)
    start_miniapp_server(settings)
    start_avito_webhook_if_needed()
    start_avito_poller_if_needed()
    start_avito_unanswered_monitor_if_needed()
    bot.send_message(settings.telegram_chat_id, "Бот запущен. Пульт Codex: /menu")
    offset = load_offset()
    codex_tasks = CodexTaskRegistry()
    start_telegram_account_listener(settings, store, codex_tasks)
    media_lock = threading.Lock()
    pending_media_groups: dict[tuple[str, str, str], PendingMediaGroup] = {}

    def process_media_messages(
        update_id: int,
        messages: list[dict],
        business_connection_id: str | None = None,
    ) -> None:
        if not messages:
            return
        messages = sorted(messages, key=media_group_sort_key)
        message = messages[0]
        if not is_allowed_telegram_message(message, settings):
            return
        reply_chat_id = str((message.get("chat") or {}).get("id", ""))
        history_key = codex_history_key(message)
        busy_key = codex_busy_key(message)
        topic_params = telegram_delivery_params(message, business_connection_id)
        if store.codex_chat_enabled():
            if not codex_tasks.try_start(busy_key):
                runtime_log(
                    "codex_chat busy_denied "
                    f"update_id={update_id} key={busy_key} age_seconds={format_active_age(codex_tasks.active_age_seconds(busy_key))}"
                )
                bot.send_message(
                    reply_chat_id,
                    "Codex ещё работает над твоей прошлой задачей. Live-черновик должен обновляться.",
                    **topic_params,
                )
                return

            def resolve_media_prompt() -> str:
                media = recognize_media_messages(bot, messages)
                if media is None:
                    raise RuntimeError("Не нашёл фото, голос или файл в сообщении.")
                text_context = media_messages_context_for_codex(messages)
                if text_context and text_context not in media.prompt_text:
                    return text_context + "\n\n" + media.prompt_text
                return media.prompt_text

            initial_text = "Принимаю альбом, потом отдам задачу Codex." if len(messages) > 1 else "Принимаю медиа, потом отдам задачу Codex."
            start_codex_chat_task(
                bot,
                store,
                codex_tool_service,
                integration_settings,
                reply_chat_id,
                history_key,
                update_id,
                resolve_media_prompt,
                lambda busy_key=busy_key: codex_tasks.finish(busy_key),
                topic_params=topic_params,
                initial_live_text=initial_text,
                active_codex_tasks=codex_tasks.active_count,
                source_message=message,
                attachments=lambda messages=messages: collect_telegram_photo_attachments(bot, messages),
                handoff_id=str((telegram_handoff_ref_for_message(message) or {}).get("handoff_id") or ""),
            )
        else:
            start_media_recognition_task(bot, reply_chat_id, update_id, messages, topic_params)

    def flush_media_group(key: tuple[str, str, str]) -> None:
        with media_lock:
            pending = pending_media_groups.pop(key, None)
        if pending is None:
            return
        update_id = min(pending.update_ids) if pending.update_ids else 0
        runtime_log(f"media_group flush key={key[2]} count={len(pending.messages)} update_id={update_id}")
        process_media_messages(update_id, pending.messages, pending.business_connection_id)

    def queue_media_group_message(
        update_id: int,
        message: dict,
        business_connection_id: str | None = None,
    ) -> None:
        key = media_group_key(message)
        if key is None:
            process_media_messages(update_id, [message], business_connection_id)
            return
        with media_lock:
            pending = pending_media_groups.setdefault(key, PendingMediaGroup())
            pending.messages.append(message)
            pending.update_ids.append(update_id)
            pending.business_connection_id = pending.business_connection_id or business_connection_id
            if pending.timer is not None:
                pending.timer.cancel()
            pending.timer = threading.Timer(MEDIA_GROUP_SETTLE_SECONDS, flush_media_group, args=(key,))
            pending.timer.daemon = True
            pending.timer.start()
        runtime_log(f"media_group queued key={key[2]} count={len(pending.messages)} update_id={update_id}")

    while True:
        try:
            updates = bot.get_updates(offset)
        except (OSError, RuntimeError, TimeoutError) as exc:
            runtime_log(f"telegram polling transient_error={type(exc).__name__}: {exc}")
            time.sleep(3)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            save_offset(offset)
            if "callback_query" in update:
                callback = update["callback_query"]
                callback_id = callback["id"]
                data = callback.get("data", "")
                callback_chat_id, callback_topic_params = telegram_callback_delivery_target(
                    callback,
                    settings.telegram_chat_id,
                    str(update.get("business_connection_id") or callback.get("business_connection_id") or "").strip(),
                )
                if data == "menu":
                    bot.answer_callback_query(callback_id, "Меню")
                    send_menu(bot, callback_chat_id, store, callback_topic_params)
                    continue
                if data == "help":
                    bot.answer_callback_query(callback_id, "Помощь")
                    bot.send_message(callback_chat_id, HELP, **callback_topic_params)
                    continue
                if data == "flags":
                    bot.answer_callback_query(callback_id, "Флаги")
                    bot.send_message(callback_chat_id, feature_flags_text(), reply_markup=feature_flags_keyboard(), **callback_topic_params)
                    continue
                if data.startswith("avdraft:"):
                    callback_chat_id, callback_topic_params = telegram_callback_delivery_target(
                        callback,
                        settings.telegram_chat_id,
                        str(update.get("business_connection_id") or callback.get("business_connection_id") or "").strip(),
                    )
                    if handle_avito_draft_callback(
                        bot=bot,
                        callback_id=callback_id,
                        data=data,
                        settings=integration_settings,
                        telegram_chat_id=callback_chat_id,
                        topic_params=callback_topic_params,
                    ):
                        continue
                if data.startswith("ragplan:"):
                    if handle_rag_plan_callback(
                        bot=bot,
                        callback_id=callback_id,
                        data=data,
                        service=codex_tool_service,
                        telegram_chat_id=callback_chat_id,
                        topic_params=callback_topic_params,
                    ):
                        continue
                if data.startswith("visitconfirm:"):
                    callback_chat_id, callback_topic_params = telegram_callback_delivery_target(
                        callback,
                        settings.telegram_chat_id,
                        str(update.get("business_connection_id") or callback.get("business_connection_id") or "").strip(),
                    )
                    if handle_visit_confirmation_callback(
                        bot=bot,
                        callback_id=callback_id,
                        data=data,
                        telegram_chat_id=callback_chat_id,
                        topic_params=callback_topic_params,
                    ):
                        continue
                if data.startswith("carefu:"):
                    callback_chat_id, callback_topic_params = telegram_callback_delivery_target(
                        callback,
                        settings.telegram_chat_id,
                        str(update.get("business_connection_id") or callback.get("business_connection_id") or "").strip(),
                    )
                    if handle_care_followup_callback(
                        bot=bot,
                        callback_id=callback_id,
                        data=data,
                        settings=integration_settings,
                        telegram_chat_id=callback_chat_id,
                        topic_params=callback_topic_params,
                    ):
                        continue
                if data == "olga_history":
                    bot.answer_callback_query(callback_id, "История Ольги")
                    bot.send_message(callback_chat_id, format_olga_history(), reply_markup=olga_history_keyboard(), **callback_topic_params)
                    continue
                if data == "open_cards":
                    bot.answer_callback_query(callback_id, "Открытые карточки")
                    send_open_handoff_cards(bot, callback_chat_id, callback_topic_params)
                    continue
                if data == "preset:live:ask":
                    bot.answer_callback_query(callback_id, "Подтверждение")
                    bot.send_message(
                        callback_chat_id,
                        "<b>Полный запуск</b>\nВключить все live-флаги: Avito/VK ответы, отправку, YCLIENTS-запись, handoff, poller, историю и Codex-контуры?",
                        reply_markup=feature_preset_confirm_keyboard("live"),
                        **callback_topic_params,
                    )
                    continue
                if data == "preset:off:ask":
                    bot.answer_callback_query(callback_id, "Подтверждение")
                    bot.send_message(
                        callback_chat_id,
                        "<b>Полное отключение</b>\nВыключить все интеграционные флаги и Codex-чат? Сам Telegram-бот с /menu и /mfa останется включён.",
                        reply_markup=feature_preset_confirm_keyboard("off"),
                        **callback_topic_params,
                    )
                    continue
                if data == "preset:live:confirm":
                    result_text = set_all_feature_flags(True, store=store)
                    bot.answer_callback_query(callback_id, "Всё включено")
                    bot.send_message(callback_chat_id, result_text, reply_markup=feature_flags_keyboard(), **callback_topic_params)
                    continue
                if data == "preset:off:confirm":
                    result_text = set_all_feature_flags(False, store=store)
                    bot.answer_callback_query(callback_id, "Всё выключено")
                    bot.send_message(callback_chat_id, result_text, reply_markup=feature_flags_keyboard(), **callback_topic_params)
                    continue
                if data == "codexclear":
                    store.clear_codex_chat()
                    bot.answer_callback_query(callback_id, "История очищена")
                    bot.send_message(
                        callback_chat_id,
                        "История Codex-чата очищена.",
                        **callback_topic_params,
                    )
                    continue
                if data == "status":
                    bot.answer_callback_query(callback_id, "Фриланс-пульт убран")
                    send_menu(bot, callback_chat_id, store, callback_topic_params)
                    continue
                if data == "scan":
                    bot.answer_callback_query(callback_id, "Фриланс-пульт убран")
                    send_menu(bot, callback_chat_id, store, callback_topic_params)
                    continue
                if data.startswith("codexchat:"):
                    enabled = data.endswith(":on")
                    store.set_codex_chat_enabled(enabled)
                    bot.answer_callback_query(callback_id, "Codex чат включён" if enabled else "Codex чат выключен")
                    if enabled:
                        bot.send_message(
                            callback_chat_id,
                            "Codex чат включён. Режим: role-based tools + контекст беседы. /exit чтобы выйти.",
                            **callback_topic_params,
                        )
                    send_menu(bot, callback_chat_id, store, callback_topic_params)
                    continue
                if data.startswith("flag:"):
                    _prefix, _sep, rest = data.partition(":")
                    flag_name, _sep, action = rest.partition(":")
                    flag = FEATURE_FLAG_BY_COMMAND.get(flag_name.casefold())
                    if not flag:
                        bot.answer_callback_query(callback_id, "Флаг не найден")
                        continue
                    try:
                        _value, result_text = set_feature_flag(flag, action or "status")
                    except Exception as exc:
                        bot.answer_callback_query(callback_id, "Не удалось изменить флаг")
                        bot.send_message(callback_chat_id, "Флаг не изменён: " + escape(str(exc)), **callback_topic_params)
                        continue
                    bot.answer_callback_query(callback_id, f"{flag.name}: {feature_flag_state(flag)}")
                    bot.send_message(callback_chat_id, result_text, **callback_topic_params)
                    send_menu(bot, callback_chat_id, store, callback_topic_params)
                    continue
                if data.startswith("radar:"):
                    bot.answer_callback_query(callback_id, "Фриланс-пульт убран")
                    send_menu(bot, callback_chat_id, store, callback_topic_params)
                    continue
                if data.startswith("leads:"):
                    bot.answer_callback_query(callback_id, "Фриланс-пульт убран")
                    send_menu(bot, callback_chat_id, store, callback_topic_params)
                    continue
                if data.startswith("lead:"):
                    _, _, lead_id = data.partition(":")
                    bot.answer_callback_query(callback_id, "Карточка лида")
                    send_lead_card(bot, callback_chat_id, store, lead_id)
                    continue
                action, _, lead_id = data.partition(":")
                lead = store.get(lead_id)
                if not lead:
                    bot.answer_callback_query(callback_id, "Лид не найден")
                    continue
                if action == "do":
                    store.update_status(lead_id, "do")
                    bot.answer_callback_query(callback_id, "Отмечено: делаем")
                    bot.send_message(
                        callback_chat_id,
                        f"Лид отмечен как <b>делаем</b>:\n{escape(lead['title'])}",
                        **callback_topic_params,
                    )
                elif action == "skip":
                    store.update_status(lead_id, "skip")
                    bot.answer_callback_query(callback_id, "Отмечено: не делаем")
                    bot.send_message(
                        callback_chat_id,
                        f"Лид отмечен как <b>не делаем</b>:\n{escape(lead['title'])}",
                        **callback_topic_params,
                    )
                elif action == "codex":
                    bot.answer_callback_query(callback_id, "Запускаю Codex анализ")
                    bot.send_message(callback_chat_id, "Codex анализирует лид, это может занять пару минут.", **callback_topic_params)
                    try:
                        text, path = analyze_with_codex(hydrate_stored_lead(lead))
                    except Exception as exc:
                        text = f"Codex анализ не удался: {exc}"
                        path = None
                    if path:
                        store.update_codex_review(lead_id, str(path))
                    bot.send_message(
                        callback_chat_id,
                        "<b>Codex анализ:</b>\n" + escape(text),
                        **callback_topic_params,
                    )
                continue

            business_connection_id = str(update.get("business_connection_id") or "").strip()
            message = update.get("message") or update.get("business_message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            raw_text = (message.get("text") or "").strip()
            text = raw_text.lower()
            codex_user_text = annotate_sender_for_codex(message, message.get("text") or "")
            has_codex_text = bool(codex_user_text.strip())
            has_media = bool(message.get("photo") or message.get("voice") or message.get("audio") or message.get("document"))
            if not is_allowed_telegram_message(message, settings):
                continue
            reply_chat_id = chat_id
            is_extra_user = reply_chat_id != settings.telegram_chat_id
            history_key = codex_history_key(message)
            topic_params = telegram_delivery_params(message, business_connection_id)
            feature_flag, feature_flag_action = parse_feature_flag_command(raw_text)
            avito_draft = find_avito_draft_for_reply(message)
            if avito_draft and (has_codex_text or has_media):
                draft_reply_text = avito_draft_reply_text_for_codex(bot, message, codex_user_text)
                if not draft_reply_text:
                    bot.send_message(reply_chat_id, "Не смог разобрать ответ на черновик. Пришлите текстом или голосом ещё раз.", **topic_params)
                    continue
                draft_history_key = str(avito_draft.get("history_key") or history_key)
                busy_key = f"avito_draft:{avito_draft.get('id') or draft_history_key}"
                if not codex_tasks.try_start(busy_key):
                    bot.send_message(reply_chat_id, "Codex уже думает над этим черновиком.", **topic_params)
                    continue
                start_codex_chat_task(
                    bot,
                    store,
                    codex_tool_service,
                    integration_settings,
                    reply_chat_id,
                    draft_history_key,
                    update["update_id"],
                    avito_draft_revision_prompt(avito_draft, draft_reply_text),
                    lambda busy_key=busy_key: codex_tasks.finish(busy_key),
                    topic_params=topic_params,
                    initial_live_text="Думаю над правкой черновика.",
                    active_codex_tasks=codex_tasks.active_count,
                    source_message=message,
                    handoff_id=str(avito_draft.get("handoff_id") or ""),
                )
                continue
            if has_codex_text and handle_rag_plan_text_reply(
                bot=bot,
                message=message,
                text=raw_text,
                service=codex_tool_service,
                telegram_chat_id=reply_chat_id,
                topic_params=topic_params,
            ):
                continue
            if has_codex_text and handle_rag_admin_freeform_command(
                bot=bot,
                text=raw_text,
                service=codex_tool_service,
                telegram_chat_id=reply_chat_id,
                topic_params=topic_params,
            ):
                continue
            if has_codex_text and handle_visit_confirmation_text_reply(
                bot,
                message=message,
                text=raw_text,
                telegram_chat_id=reply_chat_id,
                topic_params=topic_params,
            ):
                continue
            if has_codex_text and handle_care_followup_text_reply(
                bot,
                message=message,
                text=raw_text,
                telegram_chat_id=reply_chat_id,
                topic_params=topic_params,
            ):
                continue
            if text.startswith("/exit"):
                store.set_codex_chat_enabled(False)
                bot.send_message(
                    reply_chat_id,
                    "Codex чат выключен.",
                    **topic_params,
                )
            elif text.startswith("/codex_clear"):
                store.clear_codex_chat(history_key)
                bot.send_message(
                    reply_chat_id,
                    "История Codex-чата очищена.",
                    **topic_params,
                )
            elif text.startswith("/codex_auth"):
                bot.send_message(
                    reply_chat_id,
                    "<b>Codex auth:</b>\n" + telegram_markdown_to_html(codex_auth_status()),
                    **topic_params,
                )
            elif text.startswith("/codex_login"):
                bot.send_message(
                    reply_chat_id,
                    "<b>Codex login:</b>\n" + telegram_markdown_to_html(start_codex_device_login()),
                    **topic_params,
                )
            elif text.startswith("/codex_logout"):
                bot.send_message(
                    reply_chat_id,
                    "<b>Codex logout:</b>\n" + telegram_markdown_to_html(codex_logout_reset()),
                    **topic_params,
                )
            elif text.startswith("/codex"):
                store.set_codex_chat_enabled(True)
                bot.send_message(
                    reply_chat_id,
                    "Codex чат включён. Режим: role-based tools + контекст беседы. Пиши обычным текстом. /exit выйти, /codex_clear очистить историю.",
                    **topic_params,
                )
            elif text.startswith("/flags") or text.startswith("/feature_flags"):
                bot.send_message(reply_chat_id, feature_flags_text(), reply_markup=feature_flags_keyboard(), **topic_params)
            elif text.startswith("/full_live_on"):
                bot.send_message(reply_chat_id, set_all_feature_flags(True, store=store), reply_markup=feature_flags_keyboard(), **topic_params)
            elif text.startswith("/full_live_off"):
                bot.send_message(reply_chat_id, set_all_feature_flags(False, store=store), reply_markup=feature_flags_keyboard(), **topic_params)
            elif text.startswith("/olga_history") or text.startswith("/olga"):
                bot.send_message(reply_chat_id, format_olga_history(), reply_markup=olga_history_keyboard(), **topic_params)
            elif text.startswith("/open_cards") or text.startswith("/open"):
                send_open_handoff_cards(bot, reply_chat_id, topic_params)
            elif text.startswith("/visit_confirmations") or text.startswith("/visits_today") or text.startswith("/visits"):
                send_visit_confirmation_cards(bot, reply_chat_id, integration_settings, raw_text, topic_params)
            elif text.startswith("/care_followups") or text.startswith("/followups"):
                send_care_followup_cards(bot, reply_chat_id, integration_settings, topic_params)
            elif text.startswith("/client"):
                send_crm_client_card(bot, reply_chat_id, raw_text, topic_params)
            elif text.startswith("/learning"):
                send_learning_lessons(bot, reply_chat_id, raw_text, topic_params)
            elif text.startswith("/bot_restart"):
                if codex_tasks.has_active():
                    RESTART_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
                    RESTART_REQUEST_PATH.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"), encoding="utf-8")
                    bot.send_message(reply_chat_id, "Рестарт поставлен в очередь после текущей задачи Codex.", **topic_params)
                else:
                    bot.send_message(reply_chat_id, "Перезапускаюсь через пару секунд.", **topic_params)
                    schedule_bot_restart()
            elif text.startswith("/mfa_status"):
                bot.send_message(
                    reply_chat_id,
                    "<b>MFA:</b>\n" + telegram_markdown_to_html(mfa_status()),
                    **topic_params,
                )
            elif text.startswith("/mfa_set"):
                secret_text = raw_text.split(maxsplit=1)[1].strip() if len(raw_text.split(maxsplit=1)) > 1 else ""
                if not secret_text:
                    bot.send_message(
                        reply_chat_id,
                        "Пришли так: <code>/mfa_set JBSWY3DPEHPK3PXP</code>\n"
                        "Можно вставить и полный <code>otpauth://totp/...</code> URI. "
                        "После установки удали своё сообщение с секретом.",
                        **topic_params,
                    )
                else:
                    try:
                        try:
                            bot.api(
                                "deleteMessage",
                                {"chat_id": reply_chat_id, "message_id": str(message.get("message_id", ""))},
                                timeout=8,
                            )
                        except RuntimeError:
                            pass
                        bot.send_message(
                            reply_chat_id,
                            "<b>MFA сохранена:</b>\n" + telegram_markdown_to_html(save_totp_secret(secret_text)),
                            **topic_params,
                        )
                    except Exception as exc:
                        bot.send_message(reply_chat_id, "Не удалось сохранить MFA: " + escape(str(exc)), **topic_params)
            elif text.startswith("/mfa_delete"):
                bot.send_message(
                    reply_chat_id,
                    "<b>MFA:</b>\n" + telegram_markdown_to_html(delete_totp_secret()),
                    **topic_params,
                )
            elif text.startswith("/mfa"):
                try:
                    bot.send_message(
                        reply_chat_id,
                        "<b>MFA:</b>\n" + telegram_markdown_to_html(mfa_code_text()),
                        **topic_params,
                    )
                except Exception as exc:
                    bot.send_message(reply_chat_id, "MFA не готова: " + escape(str(exc)), **topic_params)
            elif text.startswith("/menu") or text.startswith("/start"):
                send_menu(bot, reply_chat_id, store, topic_params)
            elif feature_flag:
                try:
                    _value, result_text = set_feature_flag(feature_flag, feature_flag_action)
                except Exception as exc:
                    bot.send_message(reply_chat_id, "Флаг не изменён: " + escape(str(exc)), **topic_params)
                else:
                    bot.send_message(reply_chat_id, result_text, reply_markup=feature_flags_keyboard(), **topic_params)
            elif text.startswith("/terminal") or text.startswith("/miniapp"):
                send_terminal_miniapp(bot, reply_chat_id, settings, topic_params)
            elif text.startswith("/run"):
                bot.send_message(reply_chat_id, "Фриланс-пульт убран.", **topic_params)
            elif text.startswith("/leads"):
                bot.send_message(reply_chat_id, "Фриланс-пульт убран.", **topic_params)
            elif text.startswith("/radar_on"):
                bot.send_message(reply_chat_id, "Фриланс-пульт убран.", **topic_params)
            elif text.startswith("/radar_off"):
                bot.send_message(reply_chat_id, "Фриланс-пульт убран.", **topic_params)
            elif text.startswith("/status"):
                send_menu(bot, reply_chat_id, store, topic_params)
            elif text.startswith("/help"):
                bot.send_message(reply_chat_id, HELP, **topic_params)
            elif text.startswith("/sendfile "):
                try:
                    send_local_file(bot, reply_chat_id, raw_text.split(maxsplit=1)[1], topic_params=topic_params)
                except Exception as exc:
                    bot.send_message(reply_chat_id, "Не удалось отправить файл: " + escape(str(exc)), **topic_params)
            elif text.startswith("/sendphoto "):
                try:
                    send_local_file(bot, reply_chat_id, raw_text.split(maxsplit=1)[1], as_photo=True, topic_params=topic_params)
                except Exception as exc:
                    bot.send_message(reply_chat_id, "Не удалось отправить фото: " + escape(str(exc)), **topic_params)
            elif has_media:
                queue_media_group_message(update["update_id"], message, business_connection_id)
            elif (store.codex_chat_enabled() or is_extra_user) and has_codex_text:
                user_text = codex_user_text
                busy_key = codex_busy_key(message)
                if not codex_tasks.try_start(busy_key):
                    runtime_log(
                        "codex_chat busy_denied "
                        f"update_id={update['update_id']} key={busy_key} "
                        f"age_seconds={format_active_age(codex_tasks.active_age_seconds(busy_key))}"
                    )
                    bot.send_message(reply_chat_id, "Codex ещё работает над твоей прошлой задачей. Live-черновик должен обновляться.", **topic_params)
                    continue
                start_codex_chat_task(
                    bot,
                    store,
                    codex_tool_service,
                    integration_settings,
                    reply_chat_id,
                    history_key,
                    update["update_id"],
                    user_text,
                    lambda busy_key=busy_key: codex_tasks.finish(busy_key),
                    topic_params=topic_params,
                    active_codex_tasks=codex_tasks.active_count,
                    source_message=message,
                    handoff_id=str((telegram_handoff_ref_for_message(message) or {}).get("handoff_id") or ""),
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["run-once", "serve"], nargs="?", default="serve")
    args = parser.parse_args()
    settings = Settings.from_env()
    if not settings.telegram_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID is empty")
    if args.mode == "run-once":
        run_once(settings)
    else:
        serve(settings)


if __name__ == "__main__":
    main()
