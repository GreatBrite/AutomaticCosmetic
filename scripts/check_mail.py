#!/usr/bin/env python3
from __future__ import annotations

import email
import imaplib
import json
import re
import sys
from email.header import decode_header
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "mcp" / "email.mcp.json"
CODE_RE = re.compile(r"\b\d{4,8}\b")
URL_RE = re.compile(r"https?://[^\s<>\"]+")


def decode_value(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def body_text(msg: email.message.Message) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if payload:
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            chunks.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(chunks)


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    cfg = json.loads(CONFIG.read_text())["mcpServers"]["freelance-gmail"]["env"]
    imap = imaplib.IMAP4_SSL(cfg["MCP_EMAIL_SERVER_IMAP_HOST"], int(cfg["MCP_EMAIL_SERVER_IMAP_PORT"]))
    imap.login(cfg["MCP_EMAIL_SERVER_USER_NAME"], cfg["MCP_EMAIL_SERVER_PASSWORD"])
    imap.select("INBOX")
    status, data = imap.search(None, "ALL")
    ids = (data[0].split() if data and data[0] else [])[-limit:]
    for msg_id in reversed(ids):
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        sender = decode_value(msg.get("From"))
        subject = decode_value(msg.get("Subject"))
        text = body_text(msg)
        codes = CODE_RE.findall(text)[:5]
        urls = URL_RE.findall(text)[:5]
        print("=" * 72)
        print("From:", sender[:160])
        print("Subject:", subject[:160])
        if codes:
            print("Codes:", ", ".join(codes))
        if urls:
            print("Links:")
            for url in urls:
                print(" ", url[:220])
    imap.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

