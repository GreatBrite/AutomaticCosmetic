from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)?[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)")
SENSITIVE_KEYS = {"phone", "token", "api_key", "client_secret", "authorization", "password", "session"}


@dataclass(frozen=True)
class JsonlAgentTraceLogger:
    """Append-only agent trace log with sensitive values redacted."""

    path: Path = Path("data/agent_trace.jsonl")

    def write(
        self,
        *,
        planner: str,
        payload: dict[str, Any],
        trace: list[dict[str, Any]],
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        record = {
            "id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "planner": planner,
            "chat_id": _nested(payload, ("message", "chat_id")),
            "message_id": _nested(payload, ("message", "message_id")),
            "payload": redact_sensitive(payload),
            "trace": redact_sensitive(trace),
            "outcome": redact_sensitive(outcome),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return {"trace_log_id": record["id"], "trace_log_path": str(self.path)}


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in SENSITIVE_KEYS:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return PHONE_RE.sub("[phone]", value)
    return value


def _nested(payload: dict[str, Any], path: tuple[str, ...]) -> str:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "")
