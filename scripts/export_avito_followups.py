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

from src.freelance_leads_bot.integrations.avito_followup_admin import (  # noqa: E402
    DEFAULT_FOLLOWUP_AUDIT_LOG_PATH,
    apply_pending_followup_action,
    pending_followup_card_text,
    pending_followup_token,
)


DEFAULT_REPORT_PATH = Path("data/avito_unanswered_report.json")
DEFAULT_STATE_PATH = Path("data/avito_unanswered_monitor_state.json")
DECISION_RE = re.compile(
    r"^\s*-\s*\[[xX]\]\s*"
    r"(?P<action>resolved|not_relevant|remind_later|still_needs_action)"
    r"\s+#(?P<token>[A-Za-z0-9_.:-]+)"
    r"(?:\s*:\s*(?P<note>.*))?\s*$"
)
ACTION_TO_FOLLOWUP_ACTION = {
    "resolved": "done",
    "not_relevant": "stale",
    "remind_later": "later",
}


def build_avito_followups_export(
    *,
    report_path: Path = DEFAULT_REPORT_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    now: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    report = _load_json(report_path)
    state = _load_json(state_path)
    rows = _pending_rows(report, state, now=now_ts)
    rows.sort(
        key=lambda row: (
            str(row.get("severity") or "") != "critical",
            not bool(row.get("overdue")),
            -int(row.get("age_seconds") or 0),
            str(row.get("client_name") or ""),
        )
    )
    limited = rows[: max(1, int(limit or 1))]
    return {
        "ok": True,
        "generated_at": now_ts,
        "generated_at_iso": _iso(now_ts),
        "report_path": str(report_path),
        "state_path": str(state_path),
        "pending_count": len(rows),
        "critical_count": sum(1 for row in rows if str(row.get("severity") or "") == "critical"),
        "overdue_count": sum(1 for row in rows if row.get("overdue")),
        "oldest_age_seconds": max((int(row.get("age_seconds") or 0) for row in rows), default=0),
        "items": limited,
        "truncated": len(rows) > len(limited),
    }


def format_avito_followups_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Avito Pending Promises",
        "",
        f"Generated: `{report.get('generated_at_iso')}`",
        (
            f"Pending: `{report.get('pending_count', 0)}`, critical: `{report.get('critical_count', 0)}`, "
            f"overdue: `{report.get('overdue_count', 0)}`, oldest: `{round(int(report.get('oldest_age_seconds') or 0) / 3600, 1)}h`"
        ),
        "",
        "For each item open Avito or the client topic, check the latest incoming/outgoing, then mark one decision.",
    ]
    items = report.get("items") if isinstance(report.get("items"), list) else []
    if not items:
        lines.extend(["", "No active Avito pending promises found."])
        return "\n".join(lines)
    for index, row in enumerate(items, start=1):
        token = str(row.get("token") or pending_followup_token(str(row.get("key") or "")))
        flags = [str(row.get("business_status") or "awaiting_olga")]
        if row.get("severity") == "critical":
            flags.append("CRITICAL")
        if row.get("overdue") and "overdue" not in {flag.casefold() for flag in flags}:
            flags.append("overdue")
        lines.extend(
            [
                "",
                f"## {index}. {row.get('client_name') or 'Без имени'}",
                "",
                f"- Token: `#{token}`",
                f"- Key: `{row.get('key') or '-'}`",
                f"- Flags: `{', '.join(flags)}`",
                f"- Age: `{round(int(row.get('age_seconds') or 0) / 3600, 1)}h`",
                f"- Avito chat_id: `{row.get('chat_id') or '-'}`",
                f"- Message id: `{row.get('message_id') or '-'}`",
                f"- Listing: `{_join_nonempty(row.get('listing_city'), row.get('listing_title')) or '-'}`",
                "",
                "Card:",
                "",
                "> " + _quote_block(pending_followup_card_text(row)),
                "",
                "Checklist:",
                "",
                "- [ ] Avito/client topic opened and latest incoming/outgoing checked",
                "- [ ] Client received a final reply, or Olga explicitly decided not to reply",
                "- [ ] Close reason written after the colon",
                "",
                "Decision, mark exactly one after manual review:",
                "",
                f"- [ ] resolved #{token}: client received final answer; note time/text",
                f"- [ ] not_relevant #{token}: no longer relevant; note why",
                f"- [ ] remind_later #{token}: keep open and snooze; note when/why",
                f"- [ ] still_needs_action #{token}: still needs Olga/client action; note next step",
            ]
        )
    if report.get("truncated"):
        lines.append("\nReport was truncated by --limit.")
    return "\n".join(lines)


def parse_avito_followup_decisions(markdown: str) -> list[dict[str, str]]:
    decisions: list[dict[str, str]] = []
    for line_no, line in enumerate(str(markdown or "").splitlines(), start=1):
        match = DECISION_RE.match(line)
        if not match:
            continue
        decisions.append(
            {
                "line": str(line_no),
                "action": match.group("action"),
                "token": match.group("token"),
                "note": str(match.group("note") or "").strip(),
            }
        )
    return decisions


def build_avito_followup_decision_review(
    *,
    decisions_path: Path,
    state_path: Path = DEFAULT_STATE_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
    audit_path: Path = DEFAULT_FOLLOWUP_AUDIT_LOG_PATH,
    apply: bool = False,
    actor: str = "markdown_review",
    now: int | None = None,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    try:
        markdown = decisions_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _decision_error(now_ts, decisions_path, state_path, f"cannot_read_decisions: {exc}")
    decisions = parse_avito_followup_decisions(markdown)
    state = _load_json(state_path)
    pending = state.get("pending_followups") if isinstance(state.get("pending_followups"), dict) else {}
    pending_tokens = {pending_followup_token(str(key)): str(key) for key in pending}
    seen: dict[str, dict[str, str]] = {}
    items: list[dict[str, Any]] = []
    ok = True
    for decision in decisions:
        token = decision["token"]
        action = decision["action"]
        note = decision["note"]
        row = {
            "line": int(decision["line"]),
            "token": token,
            "action": action,
            "note": note,
            "key": pending_tokens.get(token, ""),
            "exists": token in pending_tokens,
            "apply_action": ACTION_TO_FOLLOWUP_ACTION.get(action, ""),
            "would_apply": False,
            "applied": False,
            "errors": [],
        }
        if token in seen:
            row["errors"].append(f"duplicate decision; first seen on line {seen[token]['line']}")
        else:
            seen[token] = decision
        if action != "still_needs_action" and not note:
            row["errors"].append("decision requires a reason after ':'")
        if not row["exists"]:
            row["errors"].append("pending followup token not found")
        if row["errors"]:
            ok = False
        elif row["apply_action"]:
            row["would_apply"] = True
        items.append(row)
    if not decisions:
        ok = False
    applied_count = 0
    if apply and ok:
        for item in items:
            if not item["would_apply"]:
                continue
            result = apply_pending_followup_action(
                state_path=state_path,
                audit_path=audit_path,
                token=item["token"],
                action=item["apply_action"],
                actor=actor,
                now=now_ts,
                client_answer_confirmed=item["action"] == "resolved",
                resolution_note=item["note"],
            )
            item["result"] = result
            item["applied"] = bool(result.get("ok"))
            if item["applied"]:
                applied_count += 1
                _sync_report_after_action(
                    report_path=report_path,
                    key=str(result.get("key") or ""),
                    row=result.get("row") if isinstance(result.get("row"), dict) else {},
                )
            else:
                item["errors"].append(str(result.get("reason") or "apply_failed"))
                ok = False
    return {
        "ok": ok,
        "apply": bool(apply),
        "generated_at": now_ts,
        "generated_at_iso": _iso(now_ts),
        "decisions_path": str(decisions_path),
        "state_path": str(state_path),
        "report_path": str(report_path),
        "audit_path": str(audit_path),
        "decision_count": len(decisions),
        "applied_count": applied_count,
        "items": items,
        "errors": [error for item in items for error in item.get("errors", [])],
    }


def format_decision_review_markdown(review: dict[str, Any]) -> str:
    lines = [
        "# Avito Pending Promise Decision Review",
        "",
        f"Generated: `{review.get('generated_at_iso')}`",
        f"Mode: `{'apply' if review.get('apply') else 'dry-run'}`",
        f"OK: `{review.get('ok')}`, decisions: `{review.get('decision_count', 0)}`, applied: `{review.get('applied_count', 0)}`",
        "",
    ]
    for item in review.get("items", []):
        lines.append(
            f"- line {item.get('line')}: `{item.get('action')}` #{item.get('token')} "
            f"key=`{item.get('key') or '-'}` would_apply=`{item.get('would_apply')}` applied=`{item.get('applied')}`"
        )
        for error in item.get("errors", []):
            lines.append(f"  - error: {error}")
    return "\n".join(lines)


def _pending_rows(report: dict[str, Any], state: dict[str, Any], *, now: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in report.get("pending_followups") or []:
        if isinstance(row, dict) and not row.get("business_resolved"):
            rows.append(dict(row))
    if rows:
        return [_normalize_row(row, now=now) for row in rows]
    pending = state.get("pending_followups") if isinstance(state.get("pending_followups"), dict) else {}
    for key, row in pending.items():
        if not isinstance(row, dict) or row.get("business_resolved"):
            continue
        rows.append(dict(row, key=str(key)))
    return [_normalize_row(row, now=now) for row in rows]


def _normalize_row(row: dict[str, Any], *, now: int) -> dict[str, Any]:
    key = str(row.get("key") or _key_from_row(row))
    created_at = int(row.get("created_at") or row.get("bot_message_ts") or row.get("last_client_ts") or now)
    normalized = dict(row)
    normalized["key"] = key
    normalized["token"] = pending_followup_token(key) if key else ""
    normalized["age_seconds"] = int(row.get("age_seconds") or max(0, now - created_at))
    normalized["overdue"] = bool(row.get("overdue")) or str(row.get("business_status") or "") == "overdue"
    return normalized


def _sync_report_after_action(*, report_path: Path, key: str, row: dict[str, Any]) -> None:
    if not key or not row:
        return
    report = _load_json(report_path)
    rows = report.get("pending_followups") if isinstance(report.get("pending_followups"), list) else []
    changed = False
    for index, existing in enumerate(rows):
        if isinstance(existing, dict) and str(existing.get("key") or "") == key:
            rows[index] = {**existing, **row, "key": key}
            changed = True
            break
    if not changed:
        return
    report["pending_followups"] = rows
    active = [item for item in rows if isinstance(item, dict) and not item.get("business_resolved")]
    report["pending_followup_count"] = len(active)
    report["overdue_followup_count"] = sum(1 for item in active if item.get("overdue") or item.get("business_status") == "overdue")
    report["critical_followup_count"] = sum(1 for item in active if item.get("severity") == "critical")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _key_from_row(row: dict[str, Any]) -> str:
    return ":".join(str(row.get(key) or "").strip() for key in ("account_id", "chat_id", "message_id") if row.get(key))


def _decision_error(now_ts: int, decisions_path: Path, state_path: Path, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "apply": False,
        "generated_at": now_ts,
        "generated_at_iso": _iso(now_ts),
        "decisions_path": str(decisions_path),
        "state_path": str(state_path),
        "error": error,
        "items": [],
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _quote_block(text: str) -> str:
    return str(text or "-").replace("\n", "\n> ")


def _join_nonempty(*parts: Any) -> str:
    return " | ".join(str(part).strip() for part in parts if str(part or "").strip())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts or 0), timezone.utc).isoformat() if ts else ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Export and review active Avito pending promises.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_FOLLOWUP_AUDIT_LOG_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--apply-decisions", action="store_true")
    parser.add_argument("--actor", default="markdown_review")
    args = parser.parse_args()

    if args.decisions:
        review = build_avito_followup_decision_review(
            decisions_path=args.decisions,
            state_path=args.state,
            report_path=args.report,
            audit_path=args.audit_log,
            apply=args.apply_decisions,
            actor=args.actor,
        )
        content = json.dumps(review, ensure_ascii=False, indent=2) if args.json else format_decision_review_markdown(review)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content + "\n", encoding="utf-8")
        print(content)
        raise SystemExit(0 if review.get("ok") else 1)

    report = build_avito_followups_export(report_path=args.report, state_path=args.state, limit=args.limit)
    content = json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_avito_followups_markdown(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
        print(f"Exported {report.get('pending_count', 0)} Avito pending promises to {args.output}")
    else:
        print(content)


if __name__ == "__main__":
    main()
