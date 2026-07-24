#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_PATTERNS = {
    "/root/AutomaticCosmetic/data/*.log",
    "/root/AutomaticCosmetic/data/*.jsonl",
    "/root/AutomaticCosmetic/data/codex_chat/*.debug.log",
    "/root/AutomaticCosmetic/data/traces/*.jsonl",
    "/root/AutomaticCosmetic/data/audit/*.jsonl",
    "/root/AutomaticCosmetic/data/outbox/*.jsonl",
}
REQUIRED_DIRECTIVES = {"daily", "rotate 30", "missingok", "notifempty", "compress", "delaycompress", "copytruncate"}
FORBIDDEN_SUBSTRINGS = {
    ".sqlite",
    ".sqlite3",
    ".db",
    "telegram_handoff_refs.json",
    "telegram_client_topics.json",
    "avito_processed_events.json",
    "avito_unanswered_monitor_state.json",
    "expert_rag.sqlite3",
    "leads.sqlite3",
    "care_crm.sqlite3",
}


def verify_logrotate_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    patterns, directives = _parse_logrotate(text)
    missing_patterns = sorted(REQUIRED_PATTERNS - set(patterns))
    missing_directives = sorted(REQUIRED_DIRECTIVES - set(directives))
    forbidden_matches = sorted(
        value
        for value in patterns
        for forbidden in FORBIDDEN_SUBSTRINGS
        if forbidden in value
    )
    ok = not missing_patterns and not missing_directives and not forbidden_matches
    return {
        "ok": ok,
        "path": str(path),
        "patterns": patterns,
        "directives": directives,
        "missing_patterns": missing_patterns,
        "missing_directives": missing_directives,
        "forbidden_matches": forbidden_matches,
    }


def _parse_logrotate(text: str) -> tuple[list[str], list[str]]:
    patterns: list[str] = []
    directives: list[str] = []
    in_block = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("{"):
            prefix = line[:-1].strip()
            if prefix:
                patterns.extend(part for part in prefix.split() if part)
            in_block = True
            continue
        if line == "}":
            in_block = False
            continue
        if in_block:
            directives.append(line)
        else:
            patterns.extend(part for part in line.split() if part)
    return patterns, directives


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify AutomaticCosmetic logrotate config scope.")
    parser.add_argument("path", nargs="?", type=Path, default=Path("deploy/logrotate/automaticcosmetic"))
    args = parser.parse_args()
    result = verify_logrotate_config(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
