from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DEFAULT_TELEGRAM_SCOUT_CHANNELS = (
    "GitHubRadar,"
    "githubtrending,"
    "github_repos,"
    "code_stars,"
    "linux_do_channel"
)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def split_int_csv(value: str) -> list[int]:
    result: list[int] = []
    for part in split_csv(value):
        normalized = part.strip().strip("[]'\" ")
        if normalized.isdigit():
            result.append(int(normalized))
    return result


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: Path
    keywords: list[str]
    negative_keywords: list[str]
    max_items_per_run: int
    min_score: int
    poll_seconds: int
    allowed_telegram_usernames: list[str]
    allowed_telegram_user_ids: list[int]
    miniapp_public_url: str
    miniapp_host: str
    miniapp_port: int
    miniapp_default_cwd: Path
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_string: str
    telegram_account_listener_enabled: bool
    telegram_scout_enabled: bool
    telegram_scout_channels: list[str]
    telegram_scout_target_username: str
    telegram_scout_interval_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        keywords = split_csv(
            os.getenv(
                "KEYWORDS",
                "python, django, fastapi, flask, telegram bot, automation, scraping, parser,"
                " web scraping, backend, api, ai, openai, chatgpt, llm, data, sqlite, postgres",
            )
        )
        negative_keywords = split_csv(
            os.getenv(
                "NEGATIVE_KEYWORDS",
                "senior manager, unpaid, volunteer, internship, onsite only, clearance",
            )
        )
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            db_path=Path(os.getenv("DB_PATH", DATA_DIR / "leads.sqlite3")),
            keywords=keywords,
            negative_keywords=negative_keywords,
            max_items_per_run=int(os.getenv("MAX_ITEMS_PER_RUN", "12")),
            min_score=int(os.getenv("MIN_SCORE", "1")),
            poll_seconds=int(os.getenv("POLL_SECONDS", "900")),
            allowed_telegram_usernames=[
                username.lower().lstrip("@")
                for username in split_csv(
                    os.getenv(
                        "ALLOWED_TELEGRAM_USERNAMES",
                        "gr8brite,yosekit,somemedic,mastermebm,r18_37,bogdanuck,onesector,zavoz_vpn_i",
                    )
                )
            ],
            allowed_telegram_user_ids=split_int_csv(
                ",".join(
                    value
                    for value in (
                        os.getenv("ALLOWED_TELEGRAM_USER_IDS", ""),
                        os.getenv("TELEGRAM_ADMIN_USER_ID", ""),
                        os.getenv("TELEGRAM_COSMETOLOGIST_USER_ID", ""),
                        os.getenv("TELEGRAM_EXTRA_ADMIN_USER_IDS", ""),
                    )
                    if value
                )
            ),
            miniapp_public_url=os.getenv("MINIAPP_PUBLIC_URL", ""),
            miniapp_host=os.getenv("MINIAPP_HOST", "127.0.0.1"),
            miniapp_port=int(os.getenv("MINIAPP_PORT", "8045")),
            miniapp_default_cwd=Path(os.getenv("MINIAPP_DEFAULT_CWD", "/root")),
            telegram_api_id=int(os.getenv("TELEGRAM_API_ID", "0") or "0"),
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            telegram_session_string=os.getenv("TELEGRAM_SESSION_STRING", ""),
            telegram_account_listener_enabled=os.getenv("TELEGRAM_ACCOUNT_LISTENER_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"},
            telegram_scout_enabled=os.getenv("TELEGRAM_SCOUT_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"},
            telegram_scout_channels=[
                channel.lstrip("@")
                for channel in split_csv(os.getenv("TELEGRAM_SCOUT_CHANNELS", DEFAULT_TELEGRAM_SCOUT_CHANNELS))
            ],
            telegram_scout_target_username=os.getenv("TELEGRAM_SCOUT_TARGET_USERNAME", "gr8brite").strip().lstrip("@"),
            telegram_scout_interval_seconds=int(os.getenv("TELEGRAM_SCOUT_INTERVAL_SECONDS", "3600")),
        )
