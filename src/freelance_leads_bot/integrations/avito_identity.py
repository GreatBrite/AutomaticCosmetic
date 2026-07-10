from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


CLIENT_NAME_CACHE_PATH = Path("data/avito_client_names.json")
_DIALOG_EMOJIS = ("🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "⚫", "⚪", "🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "⬛", "⬜", "⭐", "✨", "💎", "⚡")


def dialog_ref(chat_id: str) -> str:
    if not chat_id:
        return "-"
    digest = hashlib.sha256(chat_id.encode("utf-8")).digest()
    return "".join(_DIALOG_EMOJIS[byte % len(_DIALOG_EMOJIS)] for byte in digest[:5])


def legacy_hash_ref(chat_id: str) -> str:
    if not chat_id:
        return "-"
    return "#" + hashlib.sha1(chat_id.encode("utf-8")).hexdigest()[:6].upper()


def clean_client_name(value: Any) -> str:
    name = " ".join(str(value or "").split()).strip()
    if not name or name.casefold() in {"none", "null", "undefined"}:
        return ""
    return name[:80]


def client_name_from_chat(chat: dict[str, Any], *, account_id: int = 0, author_id: str = "") -> str:
    users = chat.get("users")
    if not isinstance(users, list):
        return ""
    author_id = str(author_id or "").strip()
    if author_id:
        for user in users:
            if isinstance(user, dict) and str(user.get("id") or "") == author_id:
                return clean_client_name(user.get("name"))
    for user in users:
        if not isinstance(user, dict):
            continue
        if account_id and str(user.get("id") or "") == str(account_id):
            continue
        name = clean_client_name(user.get("name"))
        if name:
            return name
    return ""


def load_client_name_cache(path: Path | str = CLIENT_NAME_CACHE_PATH) -> dict[str, str]:
    cache_path = Path(path)
    if not cache_path.exists():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(chat_id): clean_client_name(name) for chat_id, name in raw.items() if clean_client_name(name)}


def update_client_name_cache(names: dict[str, str], path: Path | str = CLIENT_NAME_CACHE_PATH) -> None:
    cleaned = {str(chat_id): clean_client_name(name) for chat_id, name in names.items() if chat_id and clean_client_name(name)}
    if not cleaned:
        return
    cache_path = Path(path)
    cache = load_client_name_cache(cache_path)
    cache.update(cleaned)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def client_display_name(chat_id: str, client_name: str = "", *, cache_path: Path | str = CLIENT_NAME_CACHE_PATH) -> str:
    name = clean_client_name(client_name)
    if name:
        return name
    return load_client_name_cache(cache_path).get(chat_id, "")
