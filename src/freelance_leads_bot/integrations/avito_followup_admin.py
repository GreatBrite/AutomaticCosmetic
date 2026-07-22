from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_FOLLOWUP_AUDIT_LOG_PATH = Path("data/avito_followup_audit.jsonl")
DEFAULT_FOLLOWUP_SNOOZE_SECONDS = 2 * 60 * 60
FOLLOWUP_ACTIONS = {"done", "stale", "urgent", "later"}


def pending_followup_token(key: str) -> str:
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:12]


def pending_followup_keyboard(key: str) -> dict[str, list[list[dict[str, str]]]]:
    token = pending_followup_token(key)
    return {
        "inline_keyboard": [
            [
                {"text": "Закрыто", "callback_data": f"avfu:{token}:done"},
                {"text": "Не актуально", "callback_data": f"avfu:{token}:stale"},
            ],
            [
                {"text": "Срочно", "callback_data": f"avfu:{token}:urgent"},
                {"text": "Напомнить позже", "callback_data": f"avfu:{token}:later"},
            ],
        ]
    }


def parse_pending_followup_callback(data: str) -> tuple[str, str] | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "avfu":
        return None
    token, action = parts[1].strip(), parts[2].strip()
    if not token or action not in FOLLOWUP_ACTIONS:
        return None
    return token, action


def pending_followup_card_text(row: dict[str, Any]) -> str:
    lines = [
        "Avito: зависшее обещание бота",
        f"Статус: {row.get('business_status') or 'awaiting_olga'}",
    ]
    if row.get("severity") == "critical":
        lines.append("Важность: КРИТИЧНО")
    if row.get("client_name"):
        lines.append(f"Клиент: {row['client_name']}")
    listing = " | ".join(str(row.get(key) or "").strip() for key in ("listing_city", "listing_title") if row.get(key))
    if listing:
        lines.append(f"Объявление: {listing}")
    if row.get("age_seconds"):
        lines.append(f"Сколько висит: {int(row.get('age_seconds') or 0) // 60} мин")
    bot_promise = _text_preview(str(row.get("bot_promise") or ""), 260)
    if bot_promise:
        lines.append(f"Обещал бот: {bot_promise}")
    last_client = _text_preview(str(row.get("last_client_message") or ""), 260)
    if last_client:
        lines.append(f"Последнее от клиента: {last_client}")
    lines.append("Нужно сделать: дать клиенту финальный ответ или закрыть обещание как неактуальное.")
    lines.append(f"Avito chat_id: {row.get('chat_id') or '-'}")
    return "\n".join(lines)


def apply_pending_followup_action(
    *,
    state_path: Path | str,
    token: str,
    action: str,
    actor: str = "telegram_admin",
    now: int | None = None,
    audit_path: Path | str = DEFAULT_FOLLOWUP_AUDIT_LOG_PATH,
    snooze_seconds: int = DEFAULT_FOLLOWUP_SNOOZE_SECONDS,
) -> dict[str, Any]:
    parsed = parse_pending_followup_callback(f"avfu:{token}:{action}")
    if parsed is None:
        return {"ok": False, "reason": "invalid_callback"}
    token, action = parsed
    now_ts = int(time.time()) if now is None else int(now)
    state = _load_state(Path(state_path))
    pending = state.get("pending_followups") if isinstance(state.get("pending_followups"), dict) else {}
    key = _find_pending_key_by_token(pending, token)
    if not key:
        return {"ok": False, "reason": "followup_not_found", "token": token, "action": action}
    row = pending.get(key) if isinstance(pending.get(key), dict) else {}
    if action == "done":
        row.update(
            {
                "business_status": "manual_closed",
                "business_resolved": True,
                "closed_at": now_ts,
                "closed_at_iso": _iso(now_ts),
                "closed_by": actor,
                "close_reason": "manual_closed",
                "overdue": False,
            }
        )
    elif action == "stale":
        row.update(
            {
                "business_status": "not_relevant",
                "business_resolved": True,
                "closed_at": now_ts,
                "closed_at_iso": _iso(now_ts),
                "closed_by": actor,
                "close_reason": "not_relevant",
                "overdue": False,
            }
        )
    elif action == "urgent":
        row.update(
            {
                "business_status": "urgent",
                "severity": "critical",
                "urgent": True,
                "urgent_marked_at": now_ts,
                "urgent_marked_by": actor,
                "snoozed_until": 0,
            }
        )
    elif action == "later":
        row.update(
            {
                "business_status": "awaiting_olga",
                "snoozed_until": now_ts + max(60, int(snooze_seconds or DEFAULT_FOLLOWUP_SNOOZE_SECONDS)),
                "snoozed_at": now_ts,
                "snoozed_by": actor,
            }
        )
    pending[key] = row
    state["pending_followups"] = pending
    _save_state(Path(state_path), state)
    audit = {
        "ts": now_ts,
        "ts_iso": _iso(now_ts),
        "event": "pending_followup_action",
        "actor": actor,
        "action": action,
        "key": key,
        "token": token,
        "chat_id": row.get("chat_id"),
        "message_id": row.get("message_id"),
        "business_status": row.get("business_status"),
    }
    _append_jsonl(Path(audit_path), audit)
    return {"ok": True, "action": action, "key": key, "token": token, "row": row, "audit": audit}


def _find_pending_key_by_token(pending: dict[str, Any], token: str) -> str:
    for key in pending:
        if pending_followup_token(str(key)) == token:
            return str(key)
    return ""


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _text_preview(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "..."


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts or 0), timezone.utc).isoformat() if ts else ""
