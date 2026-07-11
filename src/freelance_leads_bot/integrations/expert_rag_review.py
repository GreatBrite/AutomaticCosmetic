from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .expert_rag import DEFAULT_EXPERT_RAG_DB_PATH, NEEDS_REVIEW, ExpertAnswer, ExpertRagStore


DEFAULT_AUDIT_LOG_PATH = Path("data/expert_rag_review_audit.jsonl")
DECISION_ACTION_APPROVE = "approve"
DECISION_ACTION_DEPRECATE = "deprecate"
DECISION_ACTION_NEEDS_EDIT = "needs_edit"
_DECISION_CHECKBOX_RE = re.compile(
    r"^\s*-\s*\[[xX]\]\s*(approve|deprecate|needs\s+edited\s+answer)(?:\s+for)?\s+#(\d+)\b",
    re.IGNORECASE,
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

    return parser


def run_review_command(argv: list[str] | None = None) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = ExpertRagStore(args.db)
    audit_log_path = resolve_audit_log_path(args.db, args.audit_log)

    if args.command == "list":
        items = store.list_answers(status=args.status, limit=args.limit)
        return 0, _json_or_text(args.json, {"items": [item.to_dict() for item in items]}, _format_list(items))

    if args.command == "show":
        item = store.get(args.id)
        if not item:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
        return 0, _json_or_text(args.json, item.to_dict(), _format_item(item))

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
        payload = {"status": args.status, "count": len(items), "items": [item.to_dict() for item in items]}
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

    return 2, "Unsupported command."


def _json_or_text(as_json: bool, payload: dict[str, Any], text: str) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) if as_json else text


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
        lines.append(f"- #{item.id} [{item.status}] {labels}")
        lines.append(f"  Q: {question}")
        lines.append(f"  A: {answer}")
    return "\n".join(lines)


def _format_item(item: ExpertAnswer) -> str:
    metadata = json.dumps(item.metadata or {}, ensure_ascii=False, sort_keys=True)
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
