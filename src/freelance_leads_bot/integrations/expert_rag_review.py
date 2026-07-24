from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .expert_rag import APPROVED, DEFAULT_EXPERT_RAG_DB_PATH, NEEDS_REVIEW, ExpertAnswer, ExpertRagStore


DEFAULT_AUDIT_LOG_PATH = Path("data/expert_rag_review_audit.jsonl")
DECISION_ACTION_APPROVE = "approve"
DECISION_ACTION_DEPRECATE = "deprecate"
DECISION_ACTION_NEEDS_EDIT = "needs_edit"
TEMPORAL_DECISION_BLOCK_AUTOANSWER = "block_autoanswer"
TEMPORAL_DECISION_KEEP_AUTOANSWER = "keep_for_autoanswer"
TEMPORAL_DECISION_NEEDS_EDIT = "needs_edit"
_DECISION_CHECKBOX_RE = re.compile(
    r"^\s*-\s*\[[xX]\]\s*(approve|deprecate|needs\s+edited\s+answer)(?:\s+for)?\s+#(\d+)\b",
    re.IGNORECASE,
)
_TEMPORAL_DECISION_RE = re.compile(
    r"^\s*-\s*\[[xX]\]\s*"
    r"(block_autoanswer|keep_for_autoanswer|needs_edit)"
    r"\s+#(?P<id>\d+)"
    r"(?:\s*:\s*(?P<note>.*))?\s*$",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(r"(?iu)(?:\b\d[\d\s]{2,}\b\s*(?:₽|руб|р\b)?|стоимост|цена|прайс|как\s+модель|как\s+пациент)")
_VOLUME_RE = re.compile(r"(?iu)\b\d+(?:[.,-]\d+)?\s*мл\b")
_CASE_SPECIFIC_RE = re.compile(r"(?iu)(клиент прислал несколько сообщений|после родов|подвисает|жду|буду ждать|еду в питер|мурманск|фото-пример)")
_EFFECT_RE = re.compile(r"(?iu)(эффект|держится|сохраняется)")
_TEMPORAL_RE = re.compile(
    r"(?iu)\b("
    r"сегодня|завтра|послезавтра|вчера|"
    r"понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресень[ея]|"
    r"\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|"
    r"\d{1,2}:\d{2}|"
    r"окн[оа]|слот|запис[ьи]|адрес|акци[яи]|скидк|договор[её]н"
    r")\b"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review AutomaticCosmetic expert RAG items")
    parser.add_argument("--db", type=Path, default=DEFAULT_EXPERT_RAG_DB_PATH, help="Path to expert RAG SQLite database.")
    parser.add_argument(
        "--audit-log",
        type=Path,
        help="Path to append/read review mutation audit JSONL. Defaults to data/ for the live DB, or next to a custom --db.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List expert answers by status.")
    list_parser.add_argument("--status", default=NEEDS_REVIEW, help="Status to list; default: needs_review.")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum number of items.")

    show_parser = subparsers.add_parser("show", help="Show a single expert answer.")
    show_parser.add_argument("id", type=int)

    approve_parser = subparsers.add_parser("approve", help="Approve an expert answer for retrieval.")
    approve_parser.add_argument("id", type=int)
    approve_parser.add_argument("--by", default="olga", help="Approver name.")
    approve_parser.add_argument("--dry-run", action="store_true", help="Show what would be approved without changing the database.")

    deprecate_parser = subparsers.add_parser("deprecate", help="Deprecate an expert answer.")
    deprecate_parser.add_argument("id", type=int)
    deprecate_parser.add_argument("--dry-run", action="store_true", help="Show what would be deprecated without changing the database.")

    export_parser = subparsers.add_parser("export", help="Export review backlog for Olga approval.")
    export_parser.add_argument("--status", default=NEEDS_REVIEW, help="Status to export; default: needs_review.")
    export_parser.add_argument("--limit", type=int, default=50, help="Maximum number of items.")
    export_parser.add_argument("--output", type=Path, help="Write export to this file instead of stdout.")

    audit_parser = subparsers.add_parser("audit", help="Show recent expert RAG review mutations.")
    audit_parser.add_argument("--limit", type=int, default=20, help="Maximum number of audit events.")

    decisions_parser = subparsers.add_parser("decisions", help="Parse checked decisions from an exported review Markdown file.")
    decisions_parser.add_argument("path", type=Path, help="Markdown file produced by the export command and marked with [x].")
    decisions_parser.add_argument("--apply", action="store_true", help="Apply safe approve/deprecate decisions. Default is dry-run.")
    decisions_parser.add_argument("--by", default="olga", help="Approver name for applied approve decisions.")

    temporal_parser = subparsers.add_parser(
        "temporal-cleanup",
        help="Disable autoanswer for approved temporal facts without expiry.",
    )
    temporal_parser.add_argument("--limit", type=int, default=200, help="Maximum approved items to scan.")
    temporal_parser.add_argument("--apply", action="store_true", help="Apply metadata changes. Default is dry-run.")
    temporal_parser.add_argument("--output", type=Path, help="Write dry-run/apply review report to this file.")
    temporal_parser.add_argument(
        "--decisions",
        type=Path,
        help="Read checked temporal cleanup decisions from a Markdown report. Default is dry-run unless --apply is also set.",
    )

    return parser


def run_review_command(argv: list[str] | None = None) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = ExpertRagStore(args.db)
    audit_log_path = resolve_audit_log_path(args.db, args.audit_log)

    if args.command == "list":
        items = store.list_answers(status=args.status, limit=args.limit)
        return 0, _json_or_text(args.json, {"items": [_item_payload(item) for item in items]}, _format_list(items))

    if args.command == "show":
        item = store.get(args.id)
        if not item:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
        return 0, _json_or_text(args.json, _item_payload(item), _format_item(item))

    if args.command == "approve":
        if args.dry_run:
            item = store.get(args.id)
            if not item:
                return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
            payload = {"ok": True, "dry_run": True, "action": "approve", "approved_by": args.by, "item": item.to_dict()}
            return 0, _json_or_text(args.json, payload, _format_dry_run(item, action="approve", approved_by=args.by))
        previous = store.get(args.id)
        if not previous:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
        try:
            item = store.approve(args.id, approved_by=args.by)
        except KeyError:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
        _append_audit_log(audit_log_path, action="approve", previous=previous, current=item, approved_by=args.by)
        return 0, _json_or_text(args.json, {"ok": True, "item": item.to_dict()}, f"Approved expert RAG item {item.id} by {item.approved_by}.")

    if args.command == "deprecate":
        if args.dry_run:
            item = store.get(args.id)
            if not item:
                return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
            payload = {"ok": True, "dry_run": True, "action": "deprecate", "item": item.to_dict()}
            return 0, _json_or_text(args.json, payload, _format_dry_run(item, action="deprecate"))
        previous = store.get(args.id)
        if not previous:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
        changed = store.deprecate(args.id)
        if not changed:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
        current = store.get(args.id) or previous
        _append_audit_log(audit_log_path, action="deprecate", previous=previous, current=current)
        return 0, _json_or_text(args.json, {"ok": True, "id": args.id, "status": "deprecated"}, f"Deprecated expert RAG item {args.id}.")

    if args.command == "export":
        items = store.list_answers(status=args.status, limit=args.limit)
        payload = {"status": args.status, "count": len(items), "items": [_item_payload(item) for item in items]}
        content = json.dumps(payload, ensure_ascii=False, indent=2) if args.json else _format_export_markdown(items, status=args.status)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content + "\n", encoding="utf-8")
            return 0, _json_or_text(
                args.json,
                {"ok": True, "path": str(args.output), "count": len(items)},
                f"Exported {len(items)} expert RAG items to {args.output}.",
            )
        return 0, content

    if args.command == "audit":
        events = _read_audit_log(audit_log_path, limit=args.limit)
        return 0, _json_or_text(args.json, {"events": events}, _format_audit_events(events))

    if args.command == "decisions":
        return _run_decisions_command(
            store,
            audit_log_path=audit_log_path,
            path=args.path,
            apply=args.apply,
            approved_by=args.by,
            as_json=args.json,
        )

    if args.command == "temporal-cleanup":
        if args.decisions:
            return _run_temporal_cleanup_decisions_command(
                store,
                audit_log_path=audit_log_path,
                decisions_path=args.decisions,
                limit=args.limit,
                apply=args.apply,
                as_json=args.json,
                output_path=args.output,
            )
        return _run_temporal_cleanup_command(
            store,
            audit_log_path=audit_log_path,
            limit=args.limit,
            apply=args.apply,
            as_json=args.json,
            output_path=args.output,
        )

    return 2, "Unsupported command."


def _json_or_text(as_json: bool, payload: dict[str, Any], text: str) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) if as_json else text


def _item_payload(item: ExpertAnswer) -> dict[str, Any]:
    return {**item.to_dict(), "review_suggestion": review_suggestion(item)}


def review_suggestion(item: ExpertAnswer) -> dict[str, Any]:
    """Return non-mutating review hints for an expert RAG item."""

    text = " ".join(
        part
        for part in (
            item.question_canonical,
            item.answer_client,
            item.answer_internal,
            item.topic,
            item.service,
            item.city,
        )
        if part
    )
    metadata = item.metadata or {}
    reasons: list[str] = []
    if _MONEY_RE.search(text):
        reasons.append("contains_price_or_commercial_terms")
    if _VOLUME_RE.search(text):
        reasons.append("contains_volume_ml")
    if _EFFECT_RE.search(text):
        reasons.append("contains_effect_duration_claim")
    if _CASE_SPECIFIC_RE.search(text):
        reasons.append("case_specific_context")
    if str(metadata.get("source") or "") == "telegram_olga_history_import":
        reasons.append("imported_from_telegram_history")
    if "Нужна ручная консультация" in item.answer_internal:
        reasons.append("legacy_handoff_card_context")
    if not item.service:
        reasons.append("missing_service_metadata")
    if not item.city:
        reasons.append("missing_city_metadata")

    if "case_specific_context" in reasons or "legacy_handoff_card_context" in reasons:
        action = DECISION_ACTION_NEEDS_EDIT
    elif any(reason in reasons for reason in ("contains_price_or_commercial_terms", "contains_volume_ml", "contains_effect_duration_claim")):
        action = DECISION_ACTION_NEEDS_EDIT
    elif item.status == NEEDS_REVIEW:
        action = DECISION_ACTION_NEEDS_EDIT
    else:
        action = DECISION_ACTION_APPROVE

    return {
        "suggested_action": action,
        "confidence": "medium" if reasons else "low",
        "reasons": reasons,
        "note": _review_suggestion_note(action, reasons),
    }


def _review_suggestion_note(action: str, reasons: list[str]) -> str:
    if action == DECISION_ACTION_NEEDS_EDIT:
        if "contains_price_or_commercial_terms" in reasons or "contains_volume_ml" in reasons:
            return "Needs Olga-approved reusable wording before approval; raw price/volume answers are too context-sensitive."
        return "Needs a reusable client-safe wording before approval."
    if action == DECISION_ACTION_DEPRECATE:
        return "Likely not safe to reuse."
    return "Looks reusable, but still requires human approval."


def resolve_audit_log_path(db_path: Path, explicit_audit_log: Path | None = None) -> Path:
    if explicit_audit_log is not None:
        return explicit_audit_log
    if Path(db_path) == DEFAULT_EXPERT_RAG_DB_PATH:
        return DEFAULT_AUDIT_LOG_PATH
    return Path(db_path).parent / DEFAULT_AUDIT_LOG_PATH.name


def _append_audit_log(
    path: Path,
    *,
    action: str,
    previous: ExpertAnswer,
    current: ExpertAnswer,
    approved_by: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "item_id": current.id,
        "approved_by": approved_by,
        "previous": previous.to_dict(),
        "current": current.to_dict(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _read_audit_log(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events[-max(1, int(limit or 1)) :][::-1]


def _run_decisions_command(
    store: ExpertRagStore,
    *,
    audit_log_path: Path,
    path: Path,
    apply: bool,
    approved_by: str,
    as_json: bool,
) -> tuple[int, str]:
    if not path.exists():
        return 1, _json_or_text(
            as_json,
            {"ok": False, "error": "file_not_found", "path": str(path)},
            f"Decision file not found: {path}",
        )
    parsed = _parse_markdown_decisions(path.read_text(encoding="utf-8"))
    plan = _build_decision_plan(store, parsed)
    ok = not plan["conflicts"] and not plan["missing"] and not plan["needs_edit"]
    if apply and ok:
        previous_by_id: dict[int, ExpertAnswer] = {}
        for entry in plan["planned"]:
            item_id = int(entry["id"])
            previous = store.get(item_id)
            if not previous:
                plan["missing"].append({"id": item_id})
                ok = False
                break
            previous_by_id[item_id] = previous
    if apply and ok:
        for entry in plan["planned"]:
            item_id = int(entry["id"])
            previous = previous_by_id[item_id]
            if entry["action"] == DECISION_ACTION_APPROVE:
                current = store.approve(item_id, approved_by=approved_by)
                _append_audit_log(
                    audit_log_path,
                    action="approve",
                    previous=previous,
                    current=current,
                    approved_by=approved_by,
                )
                entry["applied"] = True
            elif entry["action"] == DECISION_ACTION_DEPRECATE:
                changed = store.deprecate(item_id)
                if not changed:
                    plan["missing"].append({"id": item_id})
                    ok = False
                    break
                current = store.get(item_id) or previous
                _append_audit_log(audit_log_path, action="deprecate", previous=previous, current=current)
                entry["applied"] = True
    payload = {
        "ok": ok,
        "dry_run": not apply,
        "applied": bool(apply and ok and plan["planned"]),
        "approved_by": approved_by,
        **plan,
    }
    code = 0 if ok else 1
    return code, _json_or_text(as_json, payload, _format_decision_plan(payload))


def _run_temporal_cleanup_command(
    store: ExpertRagStore,
    *,
    audit_log_path: Path,
    limit: int,
    apply: bool,
    as_json: bool,
    output_path: Path | None = None,
) -> tuple[int, str]:
    planned = _temporal_cleanup_plan(store, limit=limit)
    applied: list[dict[str, Any]] = []
    if apply:
        for entry in planned:
            item = store.get(int(entry["id"]))
            if not item:
                continue
            updated = store.update_metadata(
                item.id,
                {
                    "autoanswer_allowed": False,
                    "temporal_fact": True,
                    "autoanswer_block_reason": "temporal_without_expiry",
                    "temporal_cleanup_applied_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _append_audit_log(audit_log_path, action="temporal_cleanup", previous=item, current=updated)
            applied.append({**entry, "applied": True})
    payload = {
        "ok": True,
        "dry_run": not apply,
        "planned_count": len(planned),
        "applied_count": len(applied),
        "planned": applied if apply else planned,
    }
    if output_path:
        content = json.dumps(payload, ensure_ascii=False, indent=2) if as_json else _format_temporal_cleanup_markdown(payload)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content + "\n", encoding="utf-8")
        return 0, _json_or_text(
            as_json,
            {"ok": True, "path": str(output_path), "planned_count": len(planned), "applied_count": len(applied), "dry_run": not apply},
            f"Exported temporal RAG cleanup report to {output_path}. Planned={len(planned)}, applied={len(applied)}.",
        )
    return 0, _json_or_text(as_json, payload, _format_temporal_cleanup(payload))


def _run_temporal_cleanup_decisions_command(
    store: ExpertRagStore,
    *,
    audit_log_path: Path,
    decisions_path: Path,
    limit: int,
    apply: bool,
    as_json: bool,
    output_path: Path | None = None,
) -> tuple[int, str]:
    if not decisions_path.exists():
        payload = {
            "ok": False,
            "dry_run": not apply,
            "error": "file_not_found",
            "path": str(decisions_path),
            "planned": [],
            "conflicts": [],
            "missing": [],
            "not_candidates": [],
            "missing_reason": [],
        }
        return 1, _json_or_text(as_json, payload, _format_temporal_decision_plan(payload))
    parsed = _parse_temporal_cleanup_decisions(decisions_path.read_text(encoding="utf-8"))
    plan = _build_temporal_cleanup_decision_plan(store, parsed, limit=limit)
    ok = not plan["conflicts"] and not plan["missing"] and not plan["not_candidates"] and not plan["missing_reason"]
    if ok and apply:
        for entry in plan["planned"]:
            if entry["action"] != TEMPORAL_DECISION_BLOCK_AUTOANSWER:
                continue
            item = store.get(int(entry["id"]))
            if not item:
                entry["error"] = "not_found"
                ok = False
                break
            updated = store.update_metadata(
                item.id,
                {
                    "autoanswer_allowed": False,
                    "temporal_fact": True,
                    "autoanswer_block_reason": "temporal_without_expiry",
                    "temporal_cleanup_decision": TEMPORAL_DECISION_BLOCK_AUTOANSWER,
                    "temporal_cleanup_decision_note": str(entry.get("note") or "")[:500],
                    "temporal_cleanup_applied_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _append_audit_log(
                audit_log_path,
                action="temporal_cleanup_decision",
                previous=item,
                current=updated,
            )
            entry["applied"] = True
    payload = {
        "ok": ok,
        "dry_run": not apply,
        "decision_count": sum(len(actions) for actions in parsed.values()),
        "planned_count": len(plan["planned"]),
        "applied_count": sum(1 for entry in plan["planned"] if entry.get("applied")),
        **plan,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2) if as_json else _format_temporal_decision_plan(payload)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content + "\n", encoding="utf-8")
        return (0 if ok else 1), _json_or_text(
            as_json,
            {
                "ok": ok,
                "path": str(output_path),
                "decision_count": payload["decision_count"],
                "applied_count": payload["applied_count"],
                "dry_run": not apply,
            },
            f"Reviewed temporal RAG cleanup decisions from {decisions_path}. Applied={payload['applied_count']}.",
        )
    return (0 if ok else 1), content


def _temporal_cleanup_plan(store: ExpertRagStore, *, limit: int) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for item in store.list_answers(status=APPROVED, limit=max(1, int(limit or 1))):
        metadata = item.metadata or {}
        if item.expires_at or metadata.get("valid_until") or metadata.get("expires_at"):
            continue
        if metadata.get("autoanswer_allowed") is False:
            continue
        text = "\n".join([item.question_canonical, item.answer_client, item.answer_internal, item.topic, item.service, item.city])
        if not _TEMPORAL_RE.search(text):
            continue
        planned.append(
            {
                "id": item.id,
                "status": item.status,
                "question": item.question_canonical,
                "answer_client": item.answer_client,
                "answer_internal": item.answer_internal,
                "topic": item.topic,
                "service": item.service,
                "city": item.city,
                "risk_level": item.risk_level,
                "reason": "temporal_without_expiry",
            }
        )
    return planned


def _parse_temporal_cleanup_decisions(markdown: str) -> dict[int, list[dict[str, str]]]:
    decisions: dict[int, list[dict[str, str]]] = {}
    for line_no, line in enumerate(str(markdown or "").splitlines(), start=1):
        match = _TEMPORAL_DECISION_RE.match(line)
        if not match:
            continue
        item_id = int(match.group("id"))
        decisions.setdefault(item_id, []).append(
            {
                "line": str(line_no),
                "action": match.group(1).casefold(),
                "note": str(match.group("note") or "").strip(),
            }
        )
    return decisions


def _build_temporal_cleanup_decision_plan(
    store: ExpertRagStore,
    parsed: dict[int, list[dict[str, str]]],
    *,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    candidate_ids = {int(entry["id"]) for entry in _temporal_cleanup_plan(store, limit=limit)}
    planned: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    not_candidates: list[dict[str, Any]] = []
    missing_reason: list[dict[str, Any]] = []
    for item_id, decisions in sorted(parsed.items()):
        actions = sorted({decision["action"] for decision in decisions})
        if len(actions) > 1:
            conflicts.append({"id": item_id, "actions": actions, "lines": [int(decision["line"]) for decision in decisions]})
            continue
        decision = decisions[0]
        item = store.get(item_id)
        if not item:
            missing.append({"id": item_id, "action": decision["action"], "line": int(decision["line"])})
            continue
        if item_id not in candidate_ids:
            not_candidates.append({"id": item_id, "action": decision["action"], "status": item.status})
            continue
        if not decision["note"]:
            missing_reason.append({"id": item_id, "action": decision["action"], "line": int(decision["line"])})
            continue
        planned.append(
            {
                "id": item.id,
                "action": decision["action"],
                "note": decision["note"],
                "status": item.status,
                "question": item.question_canonical,
                "answer_client": item.answer_client,
                "applied": False,
            }
        )
    return {
        "planned": planned,
        "conflicts": conflicts,
        "missing": missing,
        "not_candidates": not_candidates,
        "missing_reason": missing_reason,
    }


def _parse_markdown_decisions(markdown: str) -> dict[int, list[str]]:
    decisions: dict[int, list[str]] = {}
    for line in markdown.splitlines():
        match = _DECISION_CHECKBOX_RE.match(line)
        if not match:
            continue
        raw_action, raw_id = match.groups()
        action = _normalize_decision_action(raw_action)
        decisions.setdefault(int(raw_id), []).append(action)
    return decisions


def _normalize_decision_action(raw_action: str) -> str:
    action = " ".join(raw_action.lower().split())
    if action == "approve":
        return DECISION_ACTION_APPROVE
    if action == "deprecate":
        return DECISION_ACTION_DEPRECATE
    return DECISION_ACTION_NEEDS_EDIT


def _build_decision_plan(store: ExpertRagStore, parsed: dict[int, list[str]]) -> dict[str, list[dict[str, Any]]]:
    planned: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    needs_edit: list[dict[str, Any]] = []
    for item_id, actions in sorted(parsed.items()):
        unique_actions = sorted(set(actions))
        if len(unique_actions) > 1:
            conflicts.append({"id": item_id, "actions": unique_actions})
            continue
        action = unique_actions[0]
        item = store.get(item_id)
        if not item:
            missing.append({"id": item_id, "action": action})
            continue
        if action == DECISION_ACTION_NEEDS_EDIT:
            needs_edit.append({"id": item_id, "status": item.status, "action": action})
            continue
        planned.append(
            {
                "id": item.id,
                "action": action,
                "from_status": item.status,
                "question": item.question_canonical,
                "answer_client": item.answer_client,
            }
        )
    return {"planned": planned, "conflicts": conflicts, "missing": missing, "needs_edit": needs_edit}


def _format_audit_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No expert RAG review audit events found."
    lines = [f"Expert RAG audit events: {len(events)}"]
    for event in events:
        previous = event.get("previous") if isinstance(event.get("previous"), dict) else {}
        current = event.get("current") if isinstance(event.get("current"), dict) else {}
        item_id = event.get("item_id") or current.get("id") or previous.get("id") or "-"
        before = previous.get("status") or "-"
        after = current.get("status") or "-"
        action = event.get("action") or "-"
        approved_by = event.get("approved_by") or "-"
        lines.append(
            f"- {event.get('created_at') or '-'} #{item_id} {action}: {before} -> {after}"
            + (f" by {approved_by}" if approved_by != "-" else "")
        )
        answer = str(current.get("answer_client") or previous.get("answer_client") or "")
        if answer:
            lines.append(f"  A: {_short(answer, 100)}")
    return "\n".join(lines)


def _format_decision_plan(payload: dict[str, Any]) -> str:
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    conflicts = payload.get("conflicts") if isinstance(payload.get("conflicts"), list) else []
    missing = payload.get("missing") if isinstance(payload.get("missing"), list) else []
    needs_edit = payload.get("needs_edit") if isinstance(payload.get("needs_edit"), list) else []
    mode = "DRY RUN" if payload.get("dry_run") else "APPLY"
    lines = [f"{mode}: expert RAG review decisions"]
    if planned:
        lines.append(f"Planned safe decisions: {len(planned)}")
        for entry in planned:
            applied = " applied" if entry.get("applied") else ""
            lines.append(
                f"- #{entry['id']} {entry['action']}: "
                f"{entry['from_status']} -> {_decision_target_status(entry['action'])}{applied}"
            )
            if entry.get("question"):
                lines.append(f"  Q: {_short(str(entry['question']), 90)}")
    else:
        lines.append("Planned safe decisions: 0")
    if conflicts:
        lines.append(f"Conflicts: {len(conflicts)}")
        for entry in conflicts:
            lines.append(f"- #{entry.get('id')} has multiple checked actions: {', '.join(entry.get('actions') or [])}")
    if missing:
        lines.append(f"Missing items: {len(missing)}")
        for entry in missing:
            lines.append(f"- #{entry.get('id')} not found")
    if needs_edit:
        lines.append(f"Needs edited answer: {len(needs_edit)}")
        for entry in needs_edit:
            lines.append(f"- #{entry.get('id')} requires manual edited answer before applying")
    if not payload.get("ok"):
        lines.append("No changes were applied because the decision file needs attention.")
    elif payload.get("dry_run"):
        lines.append("No changes were applied. Re-run with --apply to mutate approve/deprecate decisions.")
    return "\n".join(lines)


def _format_temporal_cleanup(payload: dict[str, Any]) -> str:
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    mode = "DRY RUN" if payload.get("dry_run") else "APPLY"
    lines = [f"{mode}: expert RAG temporal cleanup"]
    lines.append(f"Temporal autoanswer blocks planned: {payload.get('planned_count', 0)}")
    if not planned:
        lines.append("No approved temporal autoanswer items need cleanup.")
    for entry in planned[:50]:
        applied = " applied" if entry.get("applied") else ""
        lines.append(f"- #{entry.get('id')} {entry.get('reason')}{applied}")
        if entry.get("question"):
            lines.append(f"  Q: {_short(str(entry['question']), 90)}")
        if entry.get("answer_client"):
            lines.append(f"  A: {_short(str(entry['answer_client']), 120)}")
    if payload.get("dry_run") and planned:
        lines.append("No changes were applied. Re-run with --apply to set autoanswer_allowed=false.")
    return "\n".join(lines)


def _format_temporal_decision_plan(payload: dict[str, Any]) -> str:
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    conflicts = payload.get("conflicts") if isinstance(payload.get("conflicts"), list) else []
    missing = payload.get("missing") if isinstance(payload.get("missing"), list) else []
    not_candidates = payload.get("not_candidates") if isinstance(payload.get("not_candidates"), list) else []
    missing_reason = payload.get("missing_reason") if isinstance(payload.get("missing_reason"), list) else []
    mode = "DRY RUN" if payload.get("dry_run") else "APPLY"
    lines = [f"{mode}: expert RAG temporal cleanup decisions"]
    if payload.get("error"):
        lines.append(f"Error: {payload.get('error')}")
        return "\n".join(lines)
    lines.append(f"Checked decisions: {payload.get('decision_count', 0)}")
    lines.append(f"Planned decisions: {len(planned)}, applied: {payload.get('applied_count', 0)}")
    for entry in planned:
        applied = " applied" if entry.get("applied") else ""
        mutation = "sets autoanswer_allowed=false" if entry.get("action") == TEMPORAL_DECISION_BLOCK_AUTOANSWER else "no mutation"
        lines.append(f"- #{entry.get('id')} {entry.get('action')}: {mutation}{applied}")
        if entry.get("question"):
            lines.append(f"  Q: {_short(str(entry['question']), 90)}")
    if conflicts:
        lines.append(f"Conflicts: {len(conflicts)}")
        for entry in conflicts:
            lines.append(f"- #{entry.get('id')} has multiple checked actions: {', '.join(entry.get('actions') or [])}")
    if missing:
        lines.append(f"Missing items: {len(missing)}")
        for entry in missing:
            lines.append(f"- #{entry.get('id')} not found")
    if not_candidates:
        lines.append(f"Not temporal cleanup candidates: {len(not_candidates)}")
        for entry in not_candidates:
            lines.append(f"- #{entry.get('id')} status={entry.get('status')}")
    if missing_reason:
        lines.append(f"Missing reasons: {len(missing_reason)}")
        for entry in missing_reason:
            lines.append(f"- #{entry.get('id')} {entry.get('action')} needs a reason after ':'")
    if not payload.get("ok"):
        lines.append("No changes were applied because the decision file needs attention.")
    elif payload.get("dry_run"):
        lines.append("No changes were applied. Re-run with --apply to apply block_autoanswer decisions.")
    return "\n".join(lines)


def _format_temporal_cleanup_markdown(payload: dict[str, Any]) -> str:
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    mode = "DRY RUN" if payload.get("dry_run") else "APPLY"
    lines = [
        "# Expert RAG Temporal Cleanup",
        "",
        f"Mode: `{mode}`",
        f"Planned: `{payload.get('planned_count', 0)}`, applied: `{payload.get('applied_count', 0)}`",
        "",
        "These approved RAG items contain dates, time windows, address/slot wording, promos, or one-off agreements without expiry. They should not be used as factual Avito autoanswers until Olga explicitly approves a stable formulation.",
        "",
        "Checklist before `--apply`:",
        "",
        "- [ ] Checked that items below are time-specific or one-off",
        "- [ ] Confirmed they should remain available as context/style, not direct autoanswer",
        "- [ ] If an item is actually a stable rule, edit it separately with an expiry or reusable wording before cleanup",
        "",
        "For each item, mark at most one decision after manual review. Closing decisions require a reason after `:`.",
    ]
    if not planned:
        lines.extend(["", "No approved temporal autoanswer items need cleanup."])
        return "\n".join(lines)
    for index, entry in enumerate(planned, start=1):
        labels = ", ".join(
            str(value)
            for value in (
                entry.get("topic"),
                entry.get("service"),
                entry.get("city"),
                entry.get("risk_level"),
            )
            if value
        )
        lines.extend(
            [
                "",
                f"## {index}. #{entry.get('id')} `{entry.get('reason') or 'temporal_without_expiry'}`",
                "",
                f"- Status: `{entry.get('status') or '-'}`",
                f"- Labels: `{labels or '-'}`",
                f"- Applied: `{bool(entry.get('applied'))}`",
                "",
                "Question:",
                "",
                f"> {_quote_markdown(str(entry.get('question') or '-'))}",
                "",
                "Client answer:",
                "",
                f"> {_quote_markdown(str(entry.get('answer_client') or '-'))}",
            ]
        )
        if entry.get("answer_internal"):
            lines.extend(["", "Internal note:", "", f"> {_quote_markdown(str(entry.get('answer_internal') or '-'))}"])
        lines.extend(
            [
                "",
                "Decision:",
                "",
                f"- [ ] block_autoanswer #{entry.get('id')}: temporal/one-off fact; keep only as context/style",
                f"- [ ] keep_for_autoanswer #{entry.get('id')}: stable reusable rule with expiry/rewrite handled separately",
                f"- [ ] needs_edit #{entry.get('id')}: rewrite or add expiry before changing metadata",
            ]
        )
    return "\n".join(lines)


def _decision_target_status(action: str) -> str:
    return "approved" if action == DECISION_ACTION_APPROVE else "deprecated"


def _format_list(items: list[ExpertAnswer]) -> str:
    if not items:
        return "No expert RAG items found."
    lines = [f"Expert RAG items: {len(items)}"]
    for item in items:
        question = _short(item.question_canonical, 90)
        answer = _short(item.answer_client, 90)
        labels = ", ".join(part for part in (item.topic, item.service, item.city, item.risk_level) if part)
        suggestion = review_suggestion(item)
        lines.append(f"- #{item.id} [{item.status}] {labels}")
        lines.append(f"  Suggestion: {suggestion['suggested_action']} ({', '.join(suggestion['reasons']) or 'no review flags'})")
        lines.append(f"  Q: {question}")
        lines.append(f"  A: {answer}")
    return "\n".join(lines)


def _format_item(item: ExpertAnswer) -> str:
    metadata = json.dumps(item.metadata or {}, ensure_ascii=False, sort_keys=True)
    suggestion = review_suggestion(item)
    return "\n".join(
        [
            f"Expert RAG item #{item.id}",
            f"status={item.status} approved_by={item.approved_by or '-'} risk={item.risk_level}",
            f"topic={item.topic or '-'} service={item.service or '-'} city={item.city or '-'}",
            f"source_chat_id={item.source_chat_id or '-'} source_message_id={item.source_message_id or '-'} olga_reply_message_id={item.olga_reply_message_id or '-'}",
            f"created_at={item.created_at or '-'} updated_at={item.updated_at or '-'} expires_at={item.expires_at or '-'}",
            "",
            "Question:",
            item.question_canonical or "-",
            "",
            "Client answer:",
            item.answer_client or "-",
            "",
            "Internal answer/context:",
            item.answer_internal or "-",
            "",
            "Metadata:",
            metadata,
            "",
            "Review suggestion:",
            f"- suggested_action={suggestion['suggested_action']} confidence={suggestion['confidence']}",
            f"- reasons={', '.join(suggestion['reasons']) or '-'}",
            f"- note={suggestion['note']}",
        ]
    )


def _format_export_markdown(items: list[ExpertAnswer], *, status: str) -> str:
    lines = [
        f"# Expert RAG review backlog: {status}",
        "",
        f"Items: {len(items)}",
        "",
        "Approve only facts that are still актуальные and safe for the bot to reuse. "
        "Deprecate outdated prices, ambiguous medical claims, or overly case-specific answers.",
    ]
    if not items:
        lines.extend(["", "No items found."])
        return "\n".join(lines)
    for item in items:
        labels = ", ".join(part for part in (item.topic, item.service, item.city, item.risk_level) if part) or "-"
        suggestion = review_suggestion(item)
        lines.extend(
            [
                "",
                f"## #{item.id} — {labels}",
                "",
                f"- Status: `{item.status}`",
                f"- Source: chat `{item.source_chat_id or '-'}`, message `{item.source_message_id or '-'}`, Olga reply `{item.olga_reply_message_id or '-'}`",
                f"- Updated: `{item.updated_at or '-'}`",
                "",
                "Client question:",
                "",
                f"> {_quote_markdown(item.question_canonical or '-')}",
                "",
                "Candidate client answer:",
                "",
                f"> {_quote_markdown(item.answer_client or '-')}",
                "",
                "Review suggestion:",
                "",
                f"- Suggested action: `{suggestion['suggested_action']}`",
                f"- Reasons: {', '.join(suggestion['reasons']) or '-'}",
                f"- Note: {suggestion['note']}",
                "",
                "Decision checklist:",
                "",
                f"- [ ] approve #{item.id} as-is",
                f"- [ ] deprecate #{item.id}",
                f"- [ ] needs edited answer for #{item.id}",
                "",
                "Edited client answer, if needed:",
                "",
                "> ",
                "",
                "Decision commands:",
                "",
                "```bash",
                f"python -m src.freelance_leads_bot.integrations.expert_rag_review approve {item.id} --by olga --dry-run",
                f"python -m src.freelance_leads_bot.integrations.expert_rag_review approve {item.id} --by olga",
                f"python -m src.freelance_leads_bot.integrations.expert_rag_review deprecate {item.id} --dry-run",
                f"python -m src.freelance_leads_bot.integrations.expert_rag_review deprecate {item.id}",
                "```",
            ]
        )
    return "\n".join(lines)


def _format_dry_run(item: ExpertAnswer, *, action: str, approved_by: str = "") -> str:
    target = "approved" if action == "approve" else "deprecated"
    lines = [
        f"DRY RUN: expert RAG item #{item.id} would become {target}.",
        f"Current status: {item.status}",
    ]
    if approved_by:
        lines.append(f"Approved by would be: {approved_by}")
    lines.extend(
        [
            "",
            "Question:",
            item.question_canonical or "-",
            "",
            "Client answer:",
            item.answer_client or "-",
        ]
    )
    return "\n".join(lines)


def _quote_markdown(value: str) -> str:
    return str(value or "").replace("\n", "\n> ")


def _short(value: str, limit: int) -> str:
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def main(argv: list[str] | None = None) -> int:
    code, output = run_review_command(argv)
    print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
