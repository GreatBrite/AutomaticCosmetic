from __future__ import annotations

import json
import hashlib
import re
import time
from pathlib import Path
from typing import Any


DEFAULT_HANDOFF_REFS_PATH = Path("data/telegram_handoff_refs.json")
MAX_HANDOFF_REFS = 1000
UNRESOLVED_HANDOFF_STATUSES = {"open", "in_progress", "draft_pending", "rejected", "expired_critical"}
CLOSED_HANDOFF_STATUSES = {
    "answered",
    "resolved",
    "closed",
    "closed_manual",
    "closed_manual_no_client_reply",
    "expired",
}


def telegram_handoff_ref_key(telegram_chat_id: str, telegram_message_id: str | int) -> str:
    return f"{str(telegram_chat_id).strip()}:{str(telegram_message_id).strip()}"


def handoff_id_for(avito_chat_id: str, source_message_id: str = "", created_at: int = 0) -> str:
    raw = f"{str(avito_chat_id).strip()}|{str(source_message_id).strip()}|{int(created_at or 0)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_telegram_handoff_refs(path: Path | str = DEFAULT_HANDOFF_REFS_PATH) -> dict[str, dict]:
    ref_path = Path(path)
    try:
        raw = json.loads(ref_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_telegram_handoff_refs(refs: dict[str, dict], path: Path | str = DEFAULT_HANDOFF_REFS_PATH) -> None:
    ref_path = Path(path)
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        (ref for ref in refs.values() if isinstance(ref, dict)),
        key=lambda ref: int(ref.get("updated_at") or ref.get("created_at") or 0),
        reverse=True,
    )[:MAX_HANDOFF_REFS]
    compact = {
        telegram_handoff_ref_key(str(ref.get("telegram_chat_id") or ""), str(ref.get("telegram_message_id") or "")): ref
        for ref in rows
        if ref.get("telegram_chat_id") and ref.get("telegram_message_id")
    }
    ref_path.write_text(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def remember_telegram_handoff_ref(
    *,
    telegram_chat_id: str,
    telegram_message_id: str | int,
    avito_chat_id: str,
    telegram_message_thread_id: str | int = "",
    client_name: str = "",
    handoff_text: str = "",
    handoff_id: str = "",
    source_message_id: str = "",
    status: str = "open",
    urgency: str = "",
    deadline_at: int = 0,
    escalation_at: int = 0,
    phone: str = "",
    city: str = "",
    service: str = "",
    booking_date: str = "",
    booking_time: str = "",
    confirmation_needed: str = "",
    assignee: str = "",
    path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
) -> dict:
    telegram_chat_id = str(telegram_chat_id).strip()
    telegram_message_id = str(telegram_message_id).strip()
    avito_chat_id = str(avito_chat_id).strip()
    if not telegram_chat_id or not telegram_message_id or not avito_chat_id:
        return {}
    now = int(time.time())
    key = telegram_handoff_ref_key(telegram_chat_id, telegram_message_id)
    refs = load_telegram_handoff_refs(path)
    existing = refs.get(key) if isinstance(refs.get(key), dict) else {}
    stable_handoff_id = str(handoff_id or existing.get("handoff_id") or "").strip()
    if not stable_handoff_id:
        stable_handoff_id = handoff_id_for(avito_chat_id, source_message_id, int(existing.get("created_at") or now))
    ref = {
        "handoff_id": stable_handoff_id,
        "telegram_chat_id": telegram_chat_id,
        "telegram_message_id": telegram_message_id,
        "telegram_message_thread_id": str(telegram_message_thread_id or existing.get("telegram_message_thread_id") or "").strip(),
        "avito_chat_id": avito_chat_id,
        "source_message_id": str(source_message_id or existing.get("source_message_id") or "").strip(),
        "client_name": str(client_name or "").strip(),
        "handoff_text": str(handoff_text or "").strip()[-3000:],
        "status": str(status or existing.get("status") or "open"),
        "urgency": str(urgency or existing.get("urgency") or "").strip(),
        "deadline_at": int(deadline_at or existing.get("deadline_at") or 0),
        "escalation_at": int(escalation_at or existing.get("escalation_at") or 0),
        "phone": str(phone or existing.get("phone") or "").strip(),
        "city": str(city or existing.get("city") or "").strip(),
        "service": str(service or existing.get("service") or "").strip(),
        "booking_date": str(booking_date or existing.get("booking_date") or "").strip(),
        "booking_time": str(booking_time or existing.get("booking_time") or "").strip(),
        "confirmation_needed": str(confirmation_needed or existing.get("confirmation_needed") or "").strip(),
        "assignee": str(assignee or existing.get("assignee") or "").strip(),
        "reminder_sent_at": int(existing.get("reminder_sent_at") or 0),
        "escalation_sent_at": int(existing.get("escalation_sent_at") or 0),
        "draft_id": str(existing.get("draft_id") or ""),
        "closed_at": int(existing.get("closed_at") or 0),
        "created_at": int(existing.get("created_at") or now),
        "updated_at": now,
    }
    refs[key] = ref
    save_telegram_handoff_refs(refs, path)
    return ref


def update_handoff_status(
    handoff_id: str,
    status: str,
    *,
    draft_id: str = "",
    path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
) -> bool:
    handoff_id = str(handoff_id or "").strip()
    if not handoff_id:
        return False
    refs = load_telegram_handoff_refs(path)
    now = int(time.time())
    changed = False
    for ref in refs.values():
        if not isinstance(ref, dict) or str(ref.get("handoff_id") or "") != handoff_id:
            continue
        ref["status"] = status
        ref["draft_id"] = str(draft_id or ref.get("draft_id") or "")
        ref["closed_at"] = now if status in CLOSED_HANDOFF_STATUSES else 0
        ref["updated_at"] = now
        changed = True
    if changed:
        save_telegram_handoff_refs(refs, path)
    return changed


def update_latest_handoff_for_chat(
    avito_chat_id: str,
    status: str,
    *,
    draft_id: str = "",
    path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
) -> str:
    candidates = [
        ref
        for ref in load_telegram_handoff_refs(path).values()
        if isinstance(ref, dict)
        and str(ref.get("avito_chat_id") or "") == str(avito_chat_id or "")
        and str(ref.get("status") or "open") in UNRESOLVED_HANDOFF_STATUSES
    ]
    candidates.sort(key=lambda ref: int(ref.get("created_at") or 0), reverse=True)
    if not candidates:
        return ""
    handoff_id = str(candidates[0].get("handoff_id") or "")
    for ref in candidates:
        update_handoff_status(str(ref.get("handoff_id") or ""), status, draft_id=draft_id, path=path)
    return handoff_id


def latest_handoff_ref_for_chat(
    avito_chat_id: str,
    *,
    telegram_chat_id: str = "",
    path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
) -> dict | None:
    avito_chat_id = str(avito_chat_id or "").strip()
    telegram_chat_id = str(telegram_chat_id or "").strip()
    if not avito_chat_id:
        return None
    candidates = [
        ref
        for ref in load_telegram_handoff_refs(path).values()
        if isinstance(ref, dict)
        and str(ref.get("avito_chat_id") or "").strip() == avito_chat_id
        and (not telegram_chat_id or str(ref.get("telegram_chat_id") or "").strip() == telegram_chat_id)
    ]
    candidates.sort(key=_handoff_ref_recency_key, reverse=True)
    return candidates[0] if candidates else None


def latest_unresolved_handoff_ref_for_chat(
    avito_chat_id: str,
    *,
    telegram_chat_id: str = "",
    path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
) -> dict | None:
    avito_chat_id = str(avito_chat_id or "").strip()
    telegram_chat_id = str(telegram_chat_id or "").strip()
    if not avito_chat_id:
        return None
    latest = latest_handoff_ref_for_chat(avito_chat_id, telegram_chat_id=telegram_chat_id, path=path)
    if latest and str(latest.get("status") or "open") in UNRESOLVED_HANDOFF_STATUSES:
        return latest
    return None


def open_handoff_refs(path: Path | str = DEFAULT_HANDOFF_REFS_PATH) -> list[dict]:
    refs = load_telegram_handoff_refs(path)
    canonical: dict[str, dict] = {}
    changed = False
    for key, ref in refs.items():
        if not isinstance(ref, dict):
            continue
        handoff_id = str(ref.get("handoff_id") or "").strip()
        if not handoff_id:
            handoff_id = handoff_id_for(
                str(ref.get("avito_chat_id") or ""),
                str(ref.get("source_message_id") or ""),
                int(ref.get("created_at") or 0),
            )
            ref["handoff_id"] = handoff_id
            ref.setdefault("status", "open")
            refs[key] = ref
            changed = True
        current = canonical.get(handoff_id)
        if current is None or int(ref.get("updated_at") or 0) > int(current.get("updated_at") or 0):
            canonical[handoff_id] = ref
    if changed:
        save_telegram_handoff_refs(refs, path)
    by_chat: dict[str, dict] = {}
    for ref in canonical.values():
        chat_key = str(ref.get("avito_chat_id") or "").strip() or str(ref.get("handoff_id") or "").strip()
        current = by_chat.get(chat_key)
        if current is None or _handoff_ref_recency_key(ref) > _handoff_ref_recency_key(current):
            by_chat[chat_key] = ref
    rows = [dict(ref) for ref in by_chat.values() if str(ref.get("status") or "open") in UNRESOLVED_HANDOFF_STATUSES]
    rows.sort(key=_handoff_ref_recency_key, reverse=True)
    return rows


def find_telegram_handoff_ref(
    telegram_chat_id: str,
    telegram_message_id: str | int,
    path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
) -> dict | None:
    key = telegram_handoff_ref_key(telegram_chat_id, telegram_message_id)
    ref = load_telegram_handoff_refs(path).get(key)
    return ref if isinstance(ref, dict) else None


def find_telegram_handoff_ref_by_text(
    telegram_chat_id: str,
    text: str,
    path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
) -> dict | None:
    telegram_chat_id = str(telegram_chat_id).strip()
    needle = _normalize_handoff_text(text)
    if not telegram_chat_id or len(needle) < 24:
        return None
    refs = [
        ref
        for ref in load_telegram_handoff_refs(path).values()
        if isinstance(ref, dict) and str(ref.get("telegram_chat_id") or "").strip() == telegram_chat_id
    ]
    refs.sort(key=lambda ref: int(ref.get("updated_at") or ref.get("created_at") or 0), reverse=True)
    for ref in refs:
        haystack = _normalize_handoff_text(ref.get("handoff_text"))
        if not haystack:
            continue
        if needle in haystack or haystack in needle:
            return ref
        if _handoff_text_token_overlap(needle, haystack) >= 0.72:
            return ref
    return None


def find_telegram_handoff_ref_near_message_id(
    telegram_chat_id: str,
    telegram_message_id: str | int,
    path: Path | str = DEFAULT_HANDOFF_REFS_PATH,
    *,
    max_distance: int = 8,
) -> dict | None:
    telegram_chat_id = str(telegram_chat_id).strip()
    try:
        target_id = int(telegram_message_id)
    except (TypeError, ValueError):
        return None
    if not telegram_chat_id or target_id <= 0:
        return None
    candidates: list[tuple[int, int, dict]] = []
    for ref in load_telegram_handoff_refs(path).values():
        if not isinstance(ref, dict) or str(ref.get("telegram_chat_id") or "").strip() != telegram_chat_id:
            continue
        try:
            ref_message_id = int(ref.get("telegram_message_id") or 0)
        except (TypeError, ValueError):
            continue
        distance = abs(ref_message_id - target_id)
        if distance <= max_distance:
            updated_at = int(ref.get("updated_at") or ref.get("created_at") or 0)
            candidates.append((distance, -updated_at, ref))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2] if candidates else None


def find_telegram_handoff_ref_in_logs(
    telegram_chat_id: str,
    telegram_message_id: str | int,
    log_paths: list[Path | str],
) -> dict | None:
    telegram_message_id = str(telegram_message_id).strip()
    if not telegram_message_id:
        return None
    for path in log_paths:
        ref = _find_ref_in_log(Path(path), telegram_chat_id, telegram_message_id)
        if ref:
            return ref
    return None


def _find_ref_in_log(path: Path, telegram_chat_id: str, telegram_message_id: str) -> dict | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ref = _ref_from_log_row(row, telegram_chat_id, telegram_message_id)
        if ref:
            return ref
    return None


def _ref_from_log_row(row: dict[str, Any], telegram_chat_id: str, telegram_message_id: str) -> dict | None:
    handoff_notify = row.get("handoff_notify") if isinstance(row.get("handoff_notify"), dict) else {}
    telegram = handoff_notify.get("telegram") if isinstance(handoff_notify.get("telegram"), dict) else {}
    result = telegram.get("result") if isinstance(telegram.get("result"), dict) else {}
    if str(result.get("message_id") or "") != telegram_message_id:
        return None
    result_chat = result.get("chat") if isinstance(result.get("chat"), dict) else {}
    result_chat_id = str(result_chat.get("id") or telegram_chat_id or "").strip()
    if telegram_chat_id and result_chat_id and str(telegram_chat_id) != result_chat_id:
        return None
    avito_chat_id = str(row.get("chat_id") or "").strip()
    if not avito_chat_id:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        avito_chat_id = str(message.get("chat_id") or "").strip()
    if not avito_chat_id:
        return None
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return {
        "handoff_id": handoff_id_for(avito_chat_id, str(row.get("message_id") or ""), int(row.get("ts") or 0)),
        "telegram_chat_id": result_chat_id or str(telegram_chat_id).strip(),
        "telegram_message_id": telegram_message_id,
        "avito_chat_id": avito_chat_id,
        "client_name": str(metadata.get("client_name") or "").strip(),
        "handoff_text": str(handoff_notify.get("text") or "").strip()[-3000:],
        "status": "open",
        "created_at": int(row.get("ts") or 0),
        "updated_at": int(row.get("ts") or 0),
    }


def _normalize_handoff_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\wа-яё]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _handoff_ref_recency_key(ref: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(ref.get("updated_at") or ref.get("created_at") or 0),
        _safe_int(ref.get("telegram_message_id")),
        int(ref.get("created_at") or 0),
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _handoff_text_token_overlap(needle: str, haystack: str) -> float:
    needle_tokens = {token for token in needle.split() if len(token) > 2}
    if len(needle_tokens) < 4:
        return 0.0
    haystack_tokens = {token for token in haystack.split() if len(token) > 2}
    return len(needle_tokens & haystack_tokens) / len(needle_tokens)
