from __future__ import annotations

import argparse
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .agent_tools import JsonKnowledgeStore, KnowledgeItem


DEFAULT_KEYWORDS = (
    "avito",
    "авито",
    "нужен ответ",
    "клиент avito",
    "объявление",
    "прайс",
    "цена",
    "стоимость",
    "ботокс",
    "губ",
    "ягод",
    "груд",
    "беремен",
    "кормлен",
    "противопоказ",
    "препарат",
    "мл",
    "расчет",
    "рассчет",
)

PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)?[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)")
AVITO_CHAT_RE = re.compile(r"\bu2u-[A-Za-z0-9_-]+\b")
LONG_ID_RE = re.compile(r"\b\d{7,}\b")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True)
class ExportMessage:
    sender: str
    text: str
    timestamp: str = ""
    message_id: str = ""


@dataclass(frozen=True)
class ImportSummary:
    messages: int
    candidates: int
    imported: int
    dry_run: bool


class TelegramHtmlExportParser(HTMLParser):
    """Extract text messages from Telegram's exported messages.html."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[ExportMessage] = []
        self._message_depth = 0
        self._message_id = ""
        self._sender = ""
        self._last_sender = ""
        self._timestamp = ""
        self._text_parts: list[str] = []
        self._capture: str | None = None
        self._capture_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name: value or "" for name, value in attrs}
        classes = set((attrs_map.get("class") or "").split())
        if tag == "div" and "message" in classes:
            self._start_message(attrs_map.get("id") or "")
            return
        if self._message_depth:
            if tag == "div":
                self._message_depth += 1
            if tag == "br" and self._capture == "text":
                self._capture_parts.append("\n")
            if tag == "div" and "from_name" in classes and self._capture is None:
                self._capture = "from"
                self._capture_parts = []
            elif tag == "div" and "text" in classes and self._capture is None:
                self._capture = "text"
                self._capture_parts = []
            if tag == "div" and "date" in classes and "details" in classes:
                self._timestamp = attrs_map.get("title") or self._timestamp

    def handle_endtag(self, tag: str) -> None:
        if not self._message_depth or tag != "div":
            return
        if self._capture:
            value = _normalize_text("".join(self._capture_parts))
            if self._capture == "from" and value:
                self._sender = value.split("\n", 1)[0].strip()
            elif self._capture == "text" and value:
                self._text_parts.append(value)
            self._capture = None
            self._capture_parts = []
        self._message_depth -= 1
        if self._message_depth == 0:
            self._finish_message()

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._capture_parts.append(data)

    def _start_message(self, message_id: str) -> None:
        self._message_depth = 1
        self._message_id = message_id
        self._sender = ""
        self._timestamp = ""
        self._text_parts = []
        self._capture = None
        self._capture_parts = []

    def _finish_message(self) -> None:
        text = _normalize_text("\n".join(self._text_parts))
        if not text:
            return
        sender = self._sender or self._last_sender or "unknown"
        self._last_sender = sender
        self.messages.append(
            ExportMessage(
                sender=sender,
                text=redact_sensitive_text(text),
                timestamp=self._timestamp,
                message_id=self._message_id,
            )
        )


def parse_telegram_html_export(html: str) -> list[ExportMessage]:
    parser = TelegramHtmlExportParser()
    parser.feed(html)
    parser.close()
    return parser.messages


def parse_telegram_zip_export(path: Path | str) -> list[ExportMessage]:
    archive_path = Path(path)
    with zipfile.ZipFile(archive_path) as archive:
        names = [name for name in archive.namelist() if name.endswith("messages.html")]
        if not names:
            raise FileNotFoundError(f"messages.html not found in {archive_path}")
        with archive.open(names[0]) as handle:
            return parse_telegram_html_export(handle.read().decode("utf-8", errors="replace"))


def build_knowledge_candidates(
    messages: list[ExportMessage],
    *,
    source: str,
    keywords: Iterable[str] = DEFAULT_KEYWORDS,
    before: int = 2,
    after: int = 1,
) -> list[dict[str, object]]:
    lowered_keywords = tuple(keyword.casefold() for keyword in keywords)
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, message in enumerate(messages):
        haystack = message.text.casefold()
        if not any(keyword in haystack for keyword in lowered_keywords):
            continue
        start = max(0, index - before)
        end = min(len(messages), index + after + 1)
        window = messages[start:end]
        content = _format_window(window)
        fingerprint = content.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append(
            {
                "kind": "avito_conversation_example",
                "title": _candidate_title(message),
                "content": content,
                "tags": _candidate_tags(content),
                "metadata": {
                    "source": source,
                    "message_id": message.message_id,
                    "timestamp": message.timestamp,
                    "window_start": start,
                    "window_end": end - 1,
                    "redacted": True,
                },
            }
        )
    return candidates


def import_telegram_zip_to_knowledge(
    path: Path | str,
    store: JsonKnowledgeStore,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> tuple[ImportSummary, list[KnowledgeItem]]:
    messages = parse_telegram_zip_export(path)
    candidates = build_knowledge_candidates(messages, source=str(path))
    if limit is not None:
        candidates = candidates[:limit]
    imported: list[KnowledgeItem] = []
    if not dry_run:
        existing = {item.content.casefold() for item in store.list(kind="avito_conversation_example")}
        for candidate in candidates:
            content = str(candidate["content"])
            if content.casefold() in existing:
                continue
            imported.append(
                store.create(
                    kind=str(candidate["kind"]),
                    title=str(candidate["title"]),
                    content=content,
                    tags=tuple(candidate["tags"]),  # type: ignore[arg-type]
                    metadata=dict(candidate["metadata"]),  # type: ignore[arg-type]
                )
            )
            existing.add(content.casefold())
    return ImportSummary(len(messages), len(candidates), len(imported), dry_run), imported


def redact_sensitive_text(text: str) -> str:
    text = PHONE_RE.sub("[phone]", text)
    text = AVITO_CHAT_RE.sub("[avito_chat]", text)
    text = LONG_ID_RE.sub("[id]", text)
    return text


def _format_window(messages: list[ExportMessage]) -> str:
    lines = []
    for message in messages:
        prefix = f"{message.sender}:".strip()
        timestamp = f" ({message.timestamp})" if message.timestamp else ""
        lines.append(f"{prefix}{timestamp}\n{message.text}")
    return "\n\n".join(lines)


def _candidate_title(message: ExportMessage) -> str:
    compact = _normalize_text(message.text.replace("\n", " "))
    return compact[:90] or "Avito conversation example"


def _candidate_tags(text: str) -> tuple[str, ...]:
    lowered = text.casefold()
    tags = ["avito", "history"]
    for tag, words in {
        "price": ("цен", "стоим", "прайс", "расчет", "рассчет"),
        "booking": ("запис", "слот", "окош", "yclients"),
        "medical": ("беремен", "кормлен", "противопоказ", "препарат", "аллерг"),
        "listing": ("объявление",),
        "bad_example": ("плох", "не понял", "лишнее", "нужен ответ"),
    }.items():
        if any(word in lowered for word in words):
            tags.append(tag)
    return tuple(tags)


def _normalize_text(text: str) -> str:
    lines = [SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    kept = [line for line in lines if line]
    return "\n".join(kept).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Telegram/Avito chat export snippets into bot knowledge.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--knowledge", type=Path, default=Path("data/bot_knowledge.json"))
    parser.add_argument("--apply", action="store_true", help="Write candidates to the knowledge store. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    store = JsonKnowledgeStore(args.knowledge)
    summary, _items = import_telegram_zip_to_knowledge(args.archive, store, dry_run=not args.apply, limit=args.limit)
    mode = "imported" if args.apply else "dry-run"
    print(
        f"{mode}: messages={summary.messages} candidates={summary.candidates} "
        f"written={summary.imported} knowledge={args.knowledge}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
