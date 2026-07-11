from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .expert_rag import DEFAULT_EXPERT_RAG_DB_PATH, NEEDS_REVIEW, ExpertAnswer, ExpertRagStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review AutomaticCosmetic expert RAG items")
    parser.add_argument("--db", type=Path, default=DEFAULT_EXPERT_RAG_DB_PATH, help="Path to expert RAG SQLite database.")
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

    deprecate_parser = subparsers.add_parser("deprecate", help="Deprecate an expert answer.")
    deprecate_parser.add_argument("id", type=int)

    export_parser = subparsers.add_parser("export", help="Export review backlog for Olga approval.")
    export_parser.add_argument("--status", default=NEEDS_REVIEW, help="Status to export; default: needs_review.")
    export_parser.add_argument("--limit", type=int, default=50, help="Maximum number of items.")
    export_parser.add_argument("--output", type=Path, help="Write export to this file instead of stdout.")

    return parser


def run_review_command(argv: list[str] | None = None) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = ExpertRagStore(args.db)

    if args.command == "list":
        items = store.list_answers(status=args.status, limit=args.limit)
        return 0, _json_or_text(args.json, {"items": [item.to_dict() for item in items]}, _format_list(items))

    if args.command == "show":
        item = store.get(args.id)
        if not item:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
        return 0, _json_or_text(args.json, item.to_dict(), _format_item(item))

    if args.command == "approve":
        try:
            item = store.approve(args.id, approved_by=args.by)
        except KeyError:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
        return 0, _json_or_text(args.json, {"ok": True, "item": item.to_dict()}, f"Approved expert RAG item {item.id} by {item.approved_by}.")

    if args.command == "deprecate":
        changed = store.deprecate(args.id)
        if not changed:
            return 1, _json_or_text(args.json, {"ok": False, "error": "not_found", "id": args.id}, f"Expert RAG item {args.id} not found.")
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

    return 2, "Unsupported command."


def _json_or_text(as_json: bool, payload: dict[str, Any], text: str) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) if as_json else text


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
                "Decision commands:",
                "",
                "```bash",
                f"python -m src.freelance_leads_bot.integrations.expert_rag_review approve {item.id} --by olga",
                f"python -m src.freelance_leads_bot.integrations.expert_rag_review deprecate {item.id}",
                "```",
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
