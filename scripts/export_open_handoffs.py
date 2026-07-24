#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.freelance_leads_bot.integrations.handoff_refs import (  # noqa: E402
    DEFAULT_HANDOFF_REFS_PATH,
    handoff_ref_is_critical,
    read_open_handoff_refs,
)


DEFAULT_WEBHOOK_LOG_PATH = Path("data/avito_webhook.log")
DEFAULT_POLLER_LOG_PATH = Path("data/avito_poller.log")


def build_open_handoffs_export(
    *,
    refs_path: Path = DEFAULT_HANDOFF_REFS_PATH,
    webhook_log_path: Path = DEFAULT_WEBHOOK_LOG_PATH,
    poller_log_path: Path = DEFAULT_POLLER_LOG_PATH,
    now: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    rows = read_open_handoff_refs(refs_path)
    chats = {str(row.get("avito_chat_id") or "").strip() for row in rows if row.get("avito_chat_id")}
    evidence = _chat_evidence(chats, [webhook_log_path, poller_log_path])
    items = []
    for row in rows:
        chat_id = str(row.get("avito_chat_id") or "").strip()
        created_at = int(row.get("created_at") or row.get("updated_at") or now_ts)
        age_seconds = max(0, now_ts - created_at)
        critical = handoff_ref_is_critical(row)
        item = {
            "handoff_id": str(row.get("handoff_id") or ""),
            "status": str(row.get("status") or "open"),
            "critical": critical,
            "age_seconds": age_seconds,
            "age_hours": round(age_seconds / 3600, 1),
            "avito_chat_id": chat_id,
            "client_name": str(row.get("client_name") or ""),
            "reason": str(row.get("reason") or _reason_from_text(str(row.get("handoff_text") or "")) or ""),
            "client_waits_for": str(row.get("client_waits_for") or _waits_for_from_text(str(row.get("handoff_text") or ""))),
            "booking_date": str(row.get("booking_date") or ""),
            "booking_time": str(row.get("booking_time") or ""),
            "city": str(row.get("city") or ""),
            "service": str(row.get("service") or ""),
            "confirmation_needed": str(row.get("confirmation_needed") or ""),
            "telegram_chat_id": str(row.get("telegram_chat_id") or ""),
            "telegram_message_id": str(row.get("telegram_message_id") or ""),
            "telegram_message_thread_id": str(row.get("telegram_message_thread_id") or ""),
            "created_at": created_at,
            "created_at_iso": _iso(created_at),
            "handoff_text": str(row.get("handoff_text") or "").strip(),
            "last_incoming": evidence.get(chat_id, {}).get("last_incoming", {}),
            "last_outgoing": evidence.get(chat_id, {}).get("last_outgoing", {}),
        }
        items.append(item)
    items.sort(key=lambda item: (not item["critical"], -int(item["age_seconds"]), str(item["status"]) != "draft_pending"))
    limited = items[: max(1, int(limit or 1))]
    return {
        "ok": True,
        "generated_at": now_ts,
        "generated_at_iso": _iso(now_ts),
        "refs_path": str(refs_path),
        "open_count": len(items),
        "critical_count": sum(1 for item in items if item["critical"]),
        "draft_pending_count": sum(1 for item in items if item["status"] == "draft_pending"),
        "oldest_age_seconds": max((int(item["age_seconds"]) for item in items), default=0),
        "items": limited,
        "truncated": len(items) > len(limited),
    }


def format_open_handoffs_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Open Olga Handoffs",
        "",
        f"Generated: `{report.get('generated_at_iso')}`",
        f"Open: `{report.get('open_count', 0)}`, critical: `{report.get('critical_count', 0)}`, draft_pending: `{report.get('draft_pending_count', 0)}`",
        f"Oldest: `{round(int(report.get('oldest_age_seconds') or 0) / 3600, 1)}h`",
        "",
        "For each item check the latest Avito incoming/outgoing, then choose one result: final client reply sent, Olga handled manually, not relevant, or still needs action.",
    ]
    items = report.get("items") if isinstance(report.get("items"), list) else []
    if not items:
        lines.extend(["", "No open handoffs found."])
        return "\n".join(lines)
    for index, item in enumerate(items, start=1):
        flags = ["CRITICAL" if item.get("critical") else "ordinary", str(item.get("status") or "open")]
        lines.extend(
            [
                "",
                f"## {index}. {item.get('client_name') or 'Без имени'}",
                "",
                f"- Flags: `{', '.join(flags)}`",
                f"- Age: `{item.get('age_hours')}h`, created: `{item.get('created_at_iso')}`",
                f"- Avito chat_id: `{item.get('avito_chat_id') or '-'}`",
                f"- Telegram: chat `{item.get('telegram_chat_id') or '-'}`, message `{item.get('telegram_message_id') or '-'}`, topic `{item.get('telegram_message_thread_id') or '-'}`",
                f"- Reason: `{item.get('reason') or '-'}`",
                f"- Client waits for: `{item.get('client_waits_for') or '-'}`",
                f"- Booking: `{_join_nonempty(item.get('city'), item.get('service'), item.get('booking_date'), item.get('booking_time')) or '-'}`",
                f"- Confirmation needed: `{item.get('confirmation_needed') or '-'}`",
            ]
        )
        last_incoming = item.get("last_incoming") if isinstance(item.get("last_incoming"), dict) else {}
        last_outgoing = item.get("last_outgoing") if isinstance(item.get("last_outgoing"), dict) else {}
        if last_incoming:
            lines.append(f"- Last incoming: `{last_incoming.get('ts_iso') or '-'}` — {_quote_inline(last_incoming.get('text'))}")
        if last_outgoing:
            lines.append(f"- Last outgoing: `{last_outgoing.get('ts_iso') or '-'}` — {_quote_inline(last_outgoing.get('text'))}")
        lines.extend(
            [
                "",
                "Handoff card:",
                "",
                "> " + _quote_block(str(item.get("handoff_text") or "-")),
                "",
                "Checklist:",
                "",
                "- [ ] Avito opened and latest incoming/outgoing checked",
                "- [ ] Final client reply sent or Olga confirmed manual handling",
                "- [ ] Close reason written down",
            ]
        )
    if report.get("truncated"):
        lines.append("\nReport was truncated by --limit.")
    return "\n".join(lines)


def _chat_evidence(chat_ids: set[str], paths: list[Path]) -> dict[str, dict[str, dict[str, Any]]]:
    evidence: dict[str, dict[str, dict[str, Any]]] = {chat_id: {} for chat_id in chat_ids if chat_id}
    if not evidence:
        return {}
    for path in paths:
        if not path.exists():
            continue
        for row in _iter_jsonl(path):
            chat_id = str(row.get("chat_id") or "")
            if chat_id not in evidence:
                continue
            incoming = _incoming_text(row)
            outgoing = _outgoing_text(row)
            ts = int(row.get("ts") or 0)
            if incoming:
                _remember_latest(evidence[chat_id], "last_incoming", ts, incoming, row)
            if outgoing:
                _remember_latest(evidence[chat_id], "last_outgoing", ts, outgoing, row)
    return evidence


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _incoming_text(row: dict[str, Any]) -> str:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    text = str(row.get("text") or row.get("text_preview") or message.get("text") or "").strip()
    if str(row.get("event") or "") in {"processed", "retryable_error", "queued"} and text:
        return text
    return ""


def _outgoing_text(row: dict[str, Any]) -> str:
    if str(row.get("event") or "") == "ignored" and str(row.get("reason") or "") in {"own_message", "not_incoming"}:
        return str(row.get("text_preview") or "").strip()
    send = row.get("send") if isinstance(row.get("send"), dict) else {}
    response = send.get("response") if isinstance(send.get("response"), dict) else {}
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    text = str(content.get("text") or response.get("text") or "").strip()
    return text if send.get("sent") and text else ""


def _remember_latest(target: dict[str, dict[str, Any]], key: str, ts: int, text: str, row: dict[str, Any]) -> None:
    current = target.get(key) or {}
    if ts < int(current.get("ts") or 0):
        return
    target[key] = {
        "ts": ts,
        "ts_iso": _iso(ts),
        "message_id": str(row.get("message_id") or ""),
        "event": str(row.get("event") or ""),
        "text": text,
    }


def _reason_from_text(text: str) -> str:
    for line in text.splitlines():
        label, _, value = line.partition(":")
        if label.strip().casefold() == "причина":
            return value.strip()
    return ""


def _waits_for_from_text(text: str) -> str:
    lowered = text.casefold().replace("ё", "е")
    if "адрес" in lowered or "в силе" in lowered or "запис" in lowered:
        return "подтверждение записи/адрес"
    if "фото" in lowered or "вложени" in lowered:
        return "оценка фото/вложения"
    if "стоим" in lowered or "цен" in lowered:
        return "стоимость/условия"
    return ""


def _iso(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def _join_nonempty(*parts: Any) -> str:
    return " | ".join(str(part).strip() for part in parts if str(part or "").strip())


def _quote_inline(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > 180:
        text = text[:179].rstrip() + "..."
    return "`" + text.replace("`", "'") + "`"


def _quote_block(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > 1200:
        text = text[:1199].rstrip() + "..."
    return text.replace("\n", "\n> ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export open Olga handoff tasks for manual Avito review.")
    parser.add_argument("--refs", type=Path, default=DEFAULT_HANDOFF_REFS_PATH)
    parser.add_argument("--webhook-log", type=Path, default=DEFAULT_WEBHOOK_LOG_PATH)
    parser.add_argument("--poller-log", type=Path, default=DEFAULT_POLLER_LOG_PATH)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_open_handoffs_export(
        refs_path=args.refs,
        webhook_log_path=args.webhook_log,
        poller_log_path=args.poller_log,
        limit=args.limit,
    )
    content = json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_open_handoffs_markdown(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
        print(f"Exported {len(report.get('items') or [])} open handoffs to {args.output}")
    else:
        print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
