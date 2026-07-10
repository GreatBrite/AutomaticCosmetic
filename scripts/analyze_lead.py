#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.freelance_leads_bot.codex_runner import analyze_with_codex
from src.freelance_leads_bot.config import Settings
from src.freelance_leads_bot.storage import LeadStore


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: scripts/analyze_lead.py <lead_id>", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    store = LeadStore(settings.db_path)
    lead = store.get(sys.argv[1])
    if not lead:
        print("Lead not found", file=sys.stderr)
        return 1
    try:
        lead["estimate"] = json.loads(lead.get("estimate_json") or "{}")
    except json.JSONDecodeError:
        lead["estimate"] = {}
    text, path = analyze_with_codex(lead)
    store.update_codex_review(lead["id"], str(path))
    print(text)
    print(f"\nSaved to: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

