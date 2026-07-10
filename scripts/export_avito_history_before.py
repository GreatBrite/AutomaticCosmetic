from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.freelance_leads_bot.integrations.avito_read import AvitoReadClient
from src.freelance_leads_bot.integrations.config import IntegrationSettings


DEFAULT_OUTPUT_DIR = Path("data/avito_history_exports")
DEFAULT_CUTOFF = "2026-05-15"
DEFAULT_TZ = "Europe/Moscow"


def _items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    direct = payload.get(key)
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get(key), list):
        return [item for item in result[key] if isinstance(item, dict)]
    return []


def _has_more(payload: dict[str, Any], count: int, limit: int) -> bool:
    meta = payload.get("meta")
    if isinstance(meta, dict) and "has_more" in meta:
        return bool(meta.get("has_more"))
    return count >= limit


def _chat_title(chat: dict[str, Any]) -> str:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    value = context.get("value") if isinstance(context.get("value"), dict) else {}
    return str(value.get("title") or "")


def _chat_city(chat: dict[str, Any]) -> str:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    value = context.get("value") if isinstance(context.get("value"), dict) else {}
    location = value.get("location") if isinstance(value.get("location"), dict) else {}
    return str(location.get("title") or "")


def _chat_price(chat: dict[str, Any]) -> str:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    value = context.get("value") if isinstance(context.get("value"), dict) else {}
    return str(value.get("price_string") or "")


def _chat_updated(chat: dict[str, Any]) -> int:
    for key in ("updated", "created"):
        try:
            value = int(chat.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") if isinstance(message.get("content"), dict) else {}
    return str(content.get("text") or "").strip()


def _message_created(message: dict[str, Any]) -> int:
    try:
        return int(message.get("created") or 0)
    except (TypeError, ValueError):
        return 0


def _message_direction(message: dict[str, Any], account_id: int) -> str:
    direction = str(message.get("direction") or "").lower()
    if direction:
        return direction
    return "out" if str(message.get("author_id") or "") == str(account_id) else "in"


def _iso(ts: int, tz_name: str) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo(tz_name)).isoformat()


def _cutoff_ts(value: str, tz_name: str) -> int:
    date_value = datetime.strptime(value, "%Y-%m-%d").date()
    dt = datetime.combine(date_value, time.min, tzinfo=ZoneInfo(tz_name))
    return int(dt.timestamp())


def _dialogue_examples(
    *,
    account_id: int,
    chat: dict[str, Any],
    messages: list[dict[str, Any]],
    cutoff_ts: int,
    tz_name: str,
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    pending_client: list[str] = []
    for message in sorted(messages, key=_message_created):
        created = _message_created(message)
        if not created or created >= cutoff_ts:
            continue
        text = _message_text(message)
        if not text:
            continue
        direction = _message_direction(message, account_id)
        if direction == "in":
            pending_client.append(text)
            pending_client = pending_client[-4:]
            continue
        if direction != "out" or not pending_client:
            continue
        examples.append(
            {
                "account_id": account_id,
                "chat_id": str(chat.get("id") or ""),
                "created": created,
                "created_at": _iso(created, tz_name),
                "listing_title": _chat_title(chat),
                "listing_price": _chat_price(chat),
                "city": _chat_city(chat),
                "client_context": "\n".join(pending_client),
                "olga_answer": text,
            }
        )
        pending_client = []
    return examples


async def _fetch_all_messages(
    reader: AvitoReadClient,
    account_id: int,
    chat_id: str,
    *,
    limit: int,
    max_offsets: int,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max_offsets):
        payload = await reader.get_chat_messages(account_id, chat_id, limit=limit, offset=offset)
        batch = _items(payload, "messages")
        messages.extend(batch)
        if not _has_more(payload, len(batch), limit):
            break
        offset += limit
    return messages


async def export_history(args: argparse.Namespace) -> dict[str, Any]:
    settings = IntegrationSettings.from_env()
    reader = AvitoReadClient(settings)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cutoff_ts = _cutoff_ts(args.before, args.timezone)
    accounts = settings.avito_account_ids or (settings.avito_account_id,)

    raw_path = output_dir / f"avito_messages_before_{args.before}.jsonl"
    examples_path = output_dir / f"avito_olga_examples_before_{args.before}.jsonl"
    summary_path = output_dir / f"summary_before_{args.before}.json"

    summary = {
        "before": args.before,
        "timezone": args.timezone,
        "cutoff_ts": cutoff_ts,
        "accounts": list(accounts),
        "chats_seen": 0,
        "chats_with_before_cutoff_messages": 0,
        "messages_exported": 0,
        "examples_exported": 0,
        "chat_errors": [],
        "raw_path": str(raw_path),
        "examples_path": str(examples_path),
    }

    with raw_path.open("w", encoding="utf-8") as raw_handle, examples_path.open("w", encoding="utf-8") as examples_handle:
        for account_id in accounts:
            offset = 0
            for _ in range(args.max_chat_pages):
                payload = await reader.list_chats(account_id, limit=args.chat_limit, offset=offset)
                chats = _items(payload, "chats")
                if not chats:
                    break
                for chat in chats:
                    summary["chats_seen"] += 1
                    chat_id = str(chat.get("id") or "")
                    if not chat_id:
                        continue
                    if args.only_chat_updated_before and _chat_updated(chat) >= cutoff_ts:
                        continue
                    try:
                        messages = await _fetch_all_messages(
                            reader,
                            account_id,
                            chat_id,
                            limit=args.message_limit,
                            max_offsets=args.max_message_pages,
                        )
                    except Exception as exc:
                        summary["chat_errors"].append(
                            {
                                "account_id": account_id,
                                "chat_id": chat_id,
                                "error": str(exc),
                            }
                        )
                        continue
                    before_messages = [msg for msg in messages if 0 < _message_created(msg) < cutoff_ts]
                    if not before_messages:
                        continue
                    summary["chats_with_before_cutoff_messages"] += 1
                    for message in sorted(before_messages, key=_message_created):
                        row = {
                            "account_id": account_id,
                            "chat": chat,
                            "message": message,
                            "created_at": _iso(_message_created(message), args.timezone),
                        }
                        raw_handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                        summary["messages_exported"] += 1
                    for example in _dialogue_examples(
                        account_id=account_id,
                        chat=chat,
                        messages=messages,
                        cutoff_ts=cutoff_ts,
                        tz_name=args.timezone,
                    ):
                        examples_handle.write(json.dumps(example, ensure_ascii=False, default=str) + "\n")
                        summary["examples_exported"] += 1
                if not _has_more(payload, len(chats), args.chat_limit):
                    break
                offset += args.chat_limit

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Export read-only Avito chat history before a cutoff date.")
    parser.add_argument("--before", default=DEFAULT_CUTOFF, help="Cutoff date, YYYY-MM-DD, exclusive.")
    parser.add_argument("--timezone", default=DEFAULT_TZ)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--chat-limit", type=int, default=50)
    parser.add_argument("--message-limit", type=int, default=100)
    parser.add_argument("--max-chat-pages", type=int, default=200)
    parser.add_argument("--max-message-pages", type=int, default=20)
    parser.add_argument(
        "--only-chat-updated-before",
        action="store_true",
        help="Fetch messages only for chats whose Avito chat timestamp is before the cutoff.",
    )
    args = parser.parse_args()
    summary = asyncio.run(export_history(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
