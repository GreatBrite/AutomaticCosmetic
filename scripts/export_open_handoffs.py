#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
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
    load_telegram_handoff_refs,
    handoff_ref_is_critical,
    read_open_handoff_refs,
    update_handoff_status,
)


DEFAULT_WEBHOOK_LOG_PATH = Path("data/avito_webhook.log")
DEFAULT_POLLER_LOG_PATH = Path("data/avito_poller.log")
DECISION_SOURCE = "open_handoffs_markdown"
DECISION_ACTION_TO_STATUS = {
    "resolved": "closed",
    "closed_manual": "closed_manual",
    "closed_manual_no_client_reply": "closed_manual_no_client_reply",
    "not_relevant": "not_relevant",
}
DECISION_RE = re.compile(
    r"^\s*-\s*\[[xX]\]\s*"
    r"(?P<action>resolved|closed_manual|closed_manual_no_client_reply|not_relevant|still_needs_action)"
    r"\s+#(?P<handoff_id>[A-Za-z0-9_.:-]+)"
    r"(?:\s*:\s*(?P<note>.*))?\s*$"
)


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
                f"- Handoff id: `#{item.get('handoff_id') or '-'}`",
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
                "",
                "Decision, mark exactly one after manual review:",
                "",
                f"- [ ] resolved #{item.get('handoff_id')}: client received a final Avito reply; note time/text",
                f"- [ ] closed_manual #{item.get('handoff_id')}: Olga handled it outside the bot; note what happened",
                f"- [ ] closed_manual_no_client_reply #{item.get('handoff_id')}: critical task reviewed, no client reply was sent; note why this is acceptable",
                f"- [ ] not_relevant #{item.get('handoff_id')}: no longer relevant; note why",
                f"- [ ] still_needs_action #{item.get('handoff_id')}: still needs Olga/client action; note next step",
            ]
        )
    if report.get("truncated"):
        lines.append("\nReport was truncated by --limit.")
    return "\n".join(lines)


def parse_open_handoff_decisions(markdown: str) -> list[dict[str, str]]:
    decisions: list[dict[str, str]] = []
    for line_no, line in enumerate(str(markdown or "").splitlines(), start=1):
        match = DECISION_RE.match(line)
        if not match:
            continue
        decisions.append(
            {
                "line": str(line_no),
                "action": match.group("action"),
                "handoff_id": match.group("handoff_id"),
                "note": str(match.group("note") or "").strip(),
            }
        )
    return decisions


def build_handoff_decision_review(
    *,
    decisions_path: Path,
    refs_path: Path = DEFAULT_HANDOFF_REFS_PATH,
    apply: bool = False,
    now: int | None = None,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    try:
        markdown = decisions_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "generated_at": now_ts,
            "generated_at_iso": _iso(now_ts),
            "decisions_path": str(decisions_path),
            "refs_path": str(refs_path),
            "error": f"cannot_read_decisions: {exc}",
            "items": [],
        }
    decisions = parse_open_handoff_decisions(markdown)
    refs = load_telegram_handoff_refs(refs_path)
    refs_by_handoff_id = {
        str(ref.get("handoff_id") or "").strip(): ref
        for ref in refs.values()
        if isinstance(ref, dict) and str(ref.get("handoff_id") or "").strip()
    }
    seen: dict[str, dict[str, str]] = {}
    items: list[dict[str, Any]] = []
    ok = True
    for decision in decisions:
        handoff_id = decision["handoff_id"]
        action = decision["action"]
        note = decision["note"]
        status = DECISION_ACTION_TO_STATUS.get(action, "")
        current = refs_by_handoff_id.get(handoff_id)
        item = {
            "line": int(decision["line"]),
            "handoff_id": handoff_id,
            "action": action,
            "target_status": status or "",
            "note": note,
            "applied": False,
            "error": "",
        }
        if handoff_id in seen:
            item["error"] = f"duplicate_decision:first_seen_line={seen[handoff_id]['line']}"
        elif not current:
            item["error"] = "handoff_not_found"
        elif action != "still_needs_action" and not note:
            item["error"] = "missing_close_reason"
        elif action == "still_needs_action":
            item["target_status"] = str(current.get("status") or "open")
        seen[handoff_id] = decision
        if item["error"]:
            ok = False
        items.append(item)
    if ok and apply:
        for item in items:
            if item["action"] == "still_needs_action":
                continue
            item["applied"] = update_handoff_status(
                str(item["handoff_id"]),
                str(item["target_status"]),
                resolution_note=str(item["note"]),
                resolution_source=DECISION_SOURCE,
                resolution_action=str(item["action"]),
                path=refs_path,
            )
            if not item["applied"]:
                item["error"] = "update_failed"
                ok = False
    return {
        "ok": ok,
        "apply": bool(apply),
        "generated_at": now_ts,
        "generated_at_iso": _iso(now_ts),
        "decisions_path": str(decisions_path),
        "refs_path": str(refs_path),
        "decision_count": len(decisions),
        "applied_count": sum(1 for item in items if item.get("applied")),
        "error_count": sum(1 for item in items if item.get("error")),
        "items": items,
    }


def format_handoff_decision_review(review: dict[str, Any]) -> str:
    lines = [
        "# Open Handoff Decision Review",
        "",
        f"Generated: `{review.get('generated_at_iso')}`",
        f"Mode: `{'apply' if review.get('apply') else 'dry-run'}`",
        f"Decisions: `{review.get('decision_count', 0)}`, applied: `{review.get('applied_count', 0)}`, errors: `{review.get('error_count', 0)}`",
    ]
    if review.get("error"):
        lines.extend(["", f"Error: `{review.get('error')}`"])
        return "\n".join(lines)
    items = review.get("items") if isinstance(review.get("items"), list) else []
    if not items:
        lines.extend(["", "No checked decision lines found."])
        return "\n".join(lines)
    lines.extend(["", "Items:"])
    for item in items:
        result = "applied" if item.get("applied") else "planned"
        if item.get("error"):
            result = f"ERROR {item.get('error')}"
        lines.append(
            f"- line `{item.get('line')}`: `{item.get('action')}` `#{item.get('handoff_id')}` -> `{item.get('target_status') or '-'}` ({result})"
        )
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
    parser.add_argument(
        "--decisions",
        type=Path,
        help="Read checked decision lines from a previously exported Markdown review file.",
    )
    parser.add_argument(
        "--apply-decisions",
        action="store_true",
        help="Apply checked decision lines from --decisions. Without this flag the command is a dry-run.",
    )
    args = parser.parse_args(argv)
    if args.decisions:
        review = build_handoff_decision_review(
            decisions_path=args.decisions,
            refs_path=args.refs,
            apply=args.apply_decisions,
        )
        content = json.dumps(review, ensure_ascii=False, indent=2) if args.json else format_handoff_decision_review(review)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content + "\n", encoding="utf-8")
            print(f"Reviewed {review.get('decision_count', 0)} handoff decisions from {args.decisions}")
        else:
            print(content)
        return 0 if review.get("ok") else 1
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
