from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import ROOT, load_dotenv, split_csv, split_int_csv


DEFAULT_CITIES = ("Ростов-на-Дону", "Москва", "Санкт-Петербург", "Краснодар", "Геленджик")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int = 0) -> int:
    value = _env(name)
    if not value:
        return default
    return int(value)


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float = 0.0) -> float:
    value = _env(name)
    if not value:
        return default
    return float(value)


@dataclass(frozen=True)
class IntegrationSettings:
    public_base_url: str
    cities: tuple[str, ...]
    telegram_admin_user_id: int
    telegram_cosmetologist_user_id: int
    telegram_extra_admin_user_ids: tuple[int, ...]
    telegram_admin_bot_token: str
    telegram_client_bot_token: str
    telegram_client_codex_enabled: bool
    telegram_client_followup_send_enabled: bool
    telegram_admin_codex_enabled: bool
    telegram_admin_codex_timeout_seconds: int
    telegram_admin_codex_max_steps: int
    telegram_admin_live_drafts_enabled: bool
    telegram_admin_live_draft_interval_seconds: float
    telegram_admin_history_enabled: bool
    telegram_admin_history_limit: int
    telegram_admin_history_db_path: Path
    openrouter_api_key: str
    default_model: str
    avito_codex_enabled: bool
    avito_codex_timeout_seconds: int
    avito_codex_max_steps: int
    avito_turn_debounce_seconds: int
    avito_turn_max_wait_seconds: int
    avito_turn_batch_max_messages: int
    avito_unanswered_autostart: bool
    avito_unanswered_autoreply_enabled: bool
    avito_unanswered_min_age_seconds: int
    avito_unanswered_interval_seconds: int
    avito_unanswered_lookback_seconds: int
    rag_retrieval_enabled: bool
    rag_autoanswer_threshold: float
    rag_handoff_threshold: float
    rag_expert_db_path: Path
    yclients_api_key: str
    yclients_user_token: str
    yclients_company_id: int
    yclients_city_company_ids: dict[str, int]
    yclients_city_staff_ids: dict[str, int]
    yclients_partner_id: int
    yclients_form_id: int
    yclients_integration_secret: str
    yclients_allow_mutations: bool
    avito_account_id: int
    avito_account_ids: tuple[int, ...]
    avito_client_id: str
    avito_client_secret: str
    avito_webhook_secret: str
    avito_send_enabled: bool
    avito_image_send_enabled: bool
    handoff_notify_enabled: bool
    handoff_notify_chat_id: str
    vk_group_id: int
    vk_group_token: str
    vk_api_version: str
    vk_send_enabled: bool
    vk_codex_enabled: bool
    telegram_admin_response_wait_seconds: int = 60
    telegram_client_topics_enabled: bool = True
    telegram_client_topics_path: Path = ROOT / "data" / "telegram_client_topics.json"
    rag_dynamic_intent_enabled: bool = True
    rag_service_catalog_enabled: bool = True
    rag_shared_retrieval_enabled: bool = True
    rag_service_catalog_path: Path = ROOT / "data" / "service_catalog.json"
    rag_intent_llm_timeout_seconds: int = 20

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "IntegrationSettings":
        load_dotenv(env_path or ROOT / ".env")
        avito_test_mode = _env_bool("AVITO_TEST_MODE")
        return cls(
            public_base_url=_env("YCLIENTS_PUBLIC_BASE_URL", "https://olgatihcosmo.com"),
            cities=tuple(split_csv(_env("BUSINESS_CITIES", ",".join(DEFAULT_CITIES)))) or DEFAULT_CITIES,
            telegram_admin_user_id=_env_int("TELEGRAM_ADMIN_USER_ID"),
            telegram_cosmetologist_user_id=_env_int("TELEGRAM_COSMETOLOGIST_USER_ID"),
            telegram_extra_admin_user_ids=tuple(split_int_csv(_env("TELEGRAM_EXTRA_ADMIN_USER_IDS"))),
            telegram_admin_bot_token=_env("TELEGRAM_ADMIN_BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN"),
            telegram_client_bot_token=_env("TELEGRAM_CLIENT_BOT_TOKEN"),
            telegram_client_codex_enabled=_env_bool("TELEGRAM_CLIENT_CODEX_ENABLED"),
            telegram_client_followup_send_enabled=_env_bool("TELEGRAM_CLIENT_FOLLOWUP_SEND_ENABLED"),
            telegram_admin_codex_enabled=_env_bool("TELEGRAM_ADMIN_CODEX_ENABLED", True),
            telegram_admin_codex_timeout_seconds=_env_int("TELEGRAM_ADMIN_CODEX_TIMEOUT_SECONDS", 1800),
            telegram_admin_codex_max_steps=_env_int("TELEGRAM_ADMIN_CODEX_MAX_STEPS", 0),
            telegram_admin_live_drafts_enabled=_env_bool("TELEGRAM_ADMIN_LIVE_DRAFTS_ENABLED", True),
            telegram_admin_live_draft_interval_seconds=_env_float("TELEGRAM_ADMIN_LIVE_DRAFT_INTERVAL_SECONDS", 1.2),
            telegram_admin_history_enabled=_env_bool("TELEGRAM_ADMIN_HISTORY_ENABLED", True),
            telegram_admin_history_limit=_env_int("TELEGRAM_ADMIN_HISTORY_LIMIT", 0),
            telegram_admin_history_db_path=Path(_env("TELEGRAM_ADMIN_HISTORY_DB_PATH", str(ROOT / "data" / "leads.sqlite3"))),
            telegram_client_topics_enabled=_env_bool("TELEGRAM_CLIENT_TOPICS_ENABLED", True),
            telegram_client_topics_path=Path(_env("TELEGRAM_CLIENT_TOPICS_PATH", str(ROOT / "data" / "telegram_client_topics.json"))),
            openrouter_api_key=_env("OPENROUTER_API_KEY"),
            default_model=_env("DEFAULT_MODEL", "anthropic/claude-sonnet-4.5"),
            avito_codex_enabled=True if avito_test_mode else _env_bool("AVITO_CODEX_ENABLED"),
            avito_codex_timeout_seconds=_env_int("AVITO_CODEX_TIMEOUT_SECONDS", 180),
            avito_codex_max_steps=_env_int("AVITO_CODEX_MAX_STEPS", 4),
            avito_turn_debounce_seconds=_env_int("AVITO_TURN_DEBOUNCE_SECONDS", 60),
            avito_turn_max_wait_seconds=_env_int("AVITO_TURN_MAX_WAIT_SECONDS", 120),
            avito_turn_batch_max_messages=_env_int("AVITO_TURN_BATCH_MAX_MESSAGES", 10),
            avito_unanswered_autostart=_env_bool("AVITO_UNANSWERED_AUTOSTART"),
            avito_unanswered_autoreply_enabled=_env_bool("AVITO_UNANSWERED_AUTOREPLY_ENABLED"),
            avito_unanswered_min_age_seconds=_env_int("AVITO_UNANSWERED_MIN_AGE_SECONDS", 1200),
            avito_unanswered_interval_seconds=_env_int("AVITO_UNANSWERED_INTERVAL_SECONDS", 300),
            avito_unanswered_lookback_seconds=_env_int("AVITO_UNANSWERED_LOOKBACK_SECONDS", 86400),
            rag_retrieval_enabled=_env_bool("RAG_RETRIEVAL_ENABLED", True),
            rag_dynamic_intent_enabled=_env_bool("RAG_DYNAMIC_INTENT_ENABLED", True),
            rag_service_catalog_enabled=_env_bool("RAG_SERVICE_CATALOG_ENABLED", True),
            rag_shared_retrieval_enabled=_env_bool("RAG_SHARED_RETRIEVAL_ENABLED", True),
            rag_autoanswer_threshold=_env_float("RAG_AUTOANSWER_THRESHOLD", 0.82),
            rag_handoff_threshold=_env_float("RAG_HANDOFF_THRESHOLD", 0.65),
            rag_expert_db_path=Path(_env("RAG_EXPERT_DB_PATH", str(ROOT / "data" / "expert_rag.sqlite3"))),
            rag_service_catalog_path=Path(_env("RAG_SERVICE_CATALOG_PATH", str(ROOT / "data" / "service_catalog.json"))),
            rag_intent_llm_timeout_seconds=_env_int("RAG_INTENT_LLM_TIMEOUT_SECONDS", 20),
            yclients_api_key=_env("YCLIENTS_API_KEY"),
            yclients_user_token=_env("YCLIENTS_USER_TOKEN"),
            yclients_company_id=_env_int("YCLIENTS_COMPANY_ID"),
            yclients_city_company_ids=_yclients_city_company_ids(),
            yclients_city_staff_ids=_yclients_city_staff_ids(),
            yclients_partner_id=_env_int("YCLIENTS_PARTNER_ID"),
            yclients_form_id=_env_int("YCLIENTS_FORM_ID"),
            yclients_integration_secret=_env("YCLIENTS_INTEGRATION_SECRET"),
            yclients_allow_mutations=_env_bool("YCLIENTS_ALLOW_MUTATIONS"),
            avito_account_id=_env_int("AVITO_ACCOUNT_ID"),
            avito_account_ids=tuple(
                int(account_id)
                for account_id in split_csv(_env("AVITO_ACCOUNT_IDS"))
                if account_id.isdigit()
            ),
            avito_client_id=_env("AVITO_CLIENT_ID"),
            avito_client_secret=_env("AVITO_CLIENT_SECRET"),
            avito_webhook_secret=_env("AVITO_WEBHOOK_SECRET"),
            avito_send_enabled=False if avito_test_mode else _env_bool("AVITO_SEND_ENABLED"),
            avito_image_send_enabled=_env_bool("AVITO_IMAGE_SEND_ENABLED", True),
            handoff_notify_enabled=False if avito_test_mode else _env_bool("HANDOFF_NOTIFY_ENABLED"),
            handoff_notify_chat_id=_env("HANDOFF_NOTIFY_CHAT_ID"),
            vk_group_id=_env_int("VK_GROUP_ID"),
            vk_group_token=_env("VK_GROUP_TOKEN"),
            vk_api_version=_env("VK_API_VERSION", "5.199"),
            vk_send_enabled=_env_bool("VK_SEND_ENABLED"),
            vk_codex_enabled=_env_bool("VK_CODEX_ENABLED"),
            telegram_admin_response_wait_seconds=_env_int("TELEGRAM_ADMIN_RESPONSE_WAIT_SECONDS", 60),
        )

    @property
    def yclients_ready(self) -> bool:
        return bool(self.yclients_api_key and self.yclients_user_token and self.yclients_company_id)

    @property
    def avito_ready(self) -> bool:
        return bool(self.avito_client_id and self.avito_client_secret and self.avito_webhook_secret)

    @property
    def handoff_notify_ready(self) -> bool:
        return bool(self.handoff_notify_enabled and self.telegram_admin_bot_token and self.handoff_notify_chat_id)

    @property
    def vk_ready(self) -> bool:
        return bool(self.vk_group_token and self.vk_group_id)


def _yclients_city_company_ids() -> dict[str, int]:
    rows = {
        "Ростов-на-Дону": _env_int("YCLIENTS_ROSTOV_COMPANY_ID"),
        "Санкт-Петербург": _env_int("YCLIENTS_SPB_COMPANY_ID") or _env_int("YCLIENTS_SAINT_PETERSBURG_COMPANY_ID"),
        "Москва": _env_int("YCLIENTS_MOSCOW_COMPANY_ID"),
        "Краснодар": _env_int("YCLIENTS_KRASNODAR_COMPANY_ID"),
        "Геленджик": _env_int("YCLIENTS_GELENDZHIK_COMPANY_ID") or _env_int("YCLIENTS_GELENDJIK_COMPANY_ID"),
    }
    return {city: company_id for city, company_id in rows.items() if company_id}


def _yclients_city_staff_ids() -> dict[str, int]:
    rows = {
        "Ростов-на-Дону": _env_int("YCLIENTS_ROSTOV_STAFF_ID"),
        "Москва": _env_int("YCLIENTS_MOSCOW_STAFF_ID"),
        "Краснодар": _env_int("YCLIENTS_KRASNODAR_STAFF_ID"),
        "Геленджик": _env_int("YCLIENTS_GELENDZHIK_STAFF_ID") or _env_int("YCLIENTS_GELENDJIK_STAFF_ID"),
    }
    return {city: staff_id for city, staff_id in rows.items() if staff_id}
