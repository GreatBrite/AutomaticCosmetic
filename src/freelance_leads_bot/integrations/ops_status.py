from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import IntegrationSettings


DEFAULT_UNANSWERED_REPORT_PATH = Path("data/avito_unanswered_report.json")
DEFAULT_UNANSWERED_STATE_PATH = Path("data/avito_unanswered_monitor_state.json")

DEFAULT_SERVICES = (
    "freelance-leads-bot.service",
    "yclients-avito-webhook.service",
    "yclients-avito-missed-poller.service",
    "yclients-avito-unanswered-monitor.service",
    "yclients-yclients-integration.service",
)


@dataclass(frozen=True)
class OpsCheck:
    name: str
    ok: bool
    severity: str
    detail: str
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class OpsStatusReport:
    ok: bool
    generated_at: int
    checks: tuple[OpsCheck, ...]
    flags: dict[str, Any]
    summary: dict[str, Any]


def build_ops_status_report(
    settings: IntegrationSettings | None = None,
    *,
    service_states: dict[str, str] | None = None,
    avito_health: dict[str, Any] | None = None,
    yclients_health: dict[str, Any] | None = None,
    unanswered_report_path: Path = DEFAULT_UNANSWERED_REPORT_PATH,
    unanswered_state_path: Path = DEFAULT_UNANSWERED_STATE_PATH,
    rag_db_path: Path | None = None,
) -> OpsStatusReport:
    settings = settings or IntegrationSettings.from_env()
    rag_db = rag_db_path or settings.rag_expert_db_path
    checks: list[OpsCheck] = []

    services = service_states if service_states is not None else read_systemd_service_states(DEFAULT_SERVICES)
    inactive = sorted(name for name, state in services.items() if state != "active")
    checks.append(
        OpsCheck(
            "systemd_services",
            not inactive,
            "error",
            "All runtime services are active." if not inactive else "Inactive services: " + ", ".join(inactive),
            {"services": services},
        )
    )

    avito_health = avito_health if avito_health is not None else fetch_json("http://127.0.0.1:8030/health")
    checks.append(_health_check("avito_health", avito_health, required_flags=("avito_ready", "handoff_notify_ready")))

    yclients_health = yclients_health if yclients_health is not None else fetch_json("http://127.0.0.1:8020/health")
    checks.append(_health_check("yclients_integration_health", yclients_health))

    unanswered = read_unanswered_status(unanswered_report_path, unanswered_state_path)
    actionable = int(unanswered.get("actionable_count") or 0)
    failed = int(unanswered.get("failed_count") or 0)
    checks.append(
        OpsCheck(
            "avito_unanswered_queue",
            actionable == 0,
            "warning",
            "No actionable delayed Avito replies." if actionable == 0 else f"{actionable} Avito chats need action.",
            unanswered,
        )
    )
    checks.append(
        OpsCheck(
            "avito_autoreply_failures",
            failed == 0,
            "error",
            "No failed delayed autoreplies." if failed == 0 else f"{failed} delayed autoreplies failed.",
            unanswered,
        )
    )

    rag = read_rag_status(rag_db)
    checks.append(
        OpsCheck(
            "expert_rag",
            bool(rag.get("exists")) and int(rag.get("approved_count") or 0) > 0,
            "warning",
            "Expert RAG has approved answers." if int(rag.get("approved_count") or 0) > 0 else "Expert RAG has no approved answers.",
            rag,
        )
    )

    flags = safe_live_flags(settings)
    summary = {
        "services_active": not inactive,
        "avito_actionable": actionable,
        "avito_autoreply_failed": failed,
        "rag_approved": rag.get("approved_count", 0),
        "rag_high_risk_approved": rag.get("approved_high_risk_count", 0),
    }
    ok = all(check.ok or check.severity != "error" for check in checks)
    return OpsStatusReport(ok=ok, generated_at=int(time.time()), checks=tuple(checks), flags=flags, summary=summary)


def safe_live_flags(settings: IntegrationSettings) -> dict[str, Any]:
    return {
        "avito_ready": settings.avito_ready,
        "avito_send_enabled": settings.avito_send_enabled,
        "avito_codex_enabled": settings.avito_codex_enabled,
        "avito_unanswered_autoreply_enabled": settings.avito_unanswered_autoreply_enabled,
        "yclients_ready": settings.yclients_ready,
        "yclients_allow_mutations": settings.yclients_allow_mutations,
        "handoff_notify_ready": settings.handoff_notify_ready,
        "rag_retrieval_enabled": settings.rag_retrieval_enabled,
        "rag_autoanswer_threshold": settings.rag_autoanswer_threshold,
        "rag_handoff_threshold": settings.rag_handoff_threshold,
        "vk_ready": settings.vk_ready,
        "vk_send_enabled": settings.vk_send_enabled,
    }


def read_unanswered_status(report_path: Path, state_path: Path) -> dict[str, Any]:
    report = _read_json(report_path)
    state = _read_json(state_path)
    items = report.get("items") if isinstance(report.get("items"), list) else []
    actionable_count = int(report.get("actionable_count") or sum(1 for item in items if isinstance(item, dict) and item.get("needs_action")))
    failed = state.get("failed") if isinstance(state.get("failed"), dict) else {}
    handled = state.get("handled") if isinstance(state.get("handled"), dict) else {}
    return {
        "report_exists": report_path.exists(),
        "state_exists": state_path.exists(),
        "report_created_at": report.get("created_at", ""),
        "total_count": int(report.get("count") or len(items)),
        "actionable_count": actionable_count,
        "handled_count": len(handled),
        "failed_count": len(failed),
        "activated_at": int(state.get("activated_at") or 0),
    }


def read_rag_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "approved_count": 0, "approved_high_risk_count": 0, "needs_review_count": 0}
    result = {"exists": True, "path": str(path), "approved_count": 0, "approved_high_risk_count": 0, "needs_review_count": 0}
    try:
        with sqlite3.connect(path) as conn:
            for status, risk_level, count in conn.execute(
                "SELECT status, risk_level, COUNT(*) FROM expert_answers GROUP BY status, risk_level"
            ):
                count = int(count or 0)
                if status == "approved":
                    result["approved_count"] += count
                    if risk_level == "high":
                        result["approved_high_risk_count"] += count
                if status == "needs_review":
                    result["needs_review_count"] += count
    except sqlite3.Error as exc:
        result.update({"error": type(exc).__name__, "approved_count": 0})
    return result


def read_systemd_service_states(services: tuple[str, ...] = DEFAULT_SERVICES) -> dict[str, str]:
    result: dict[str, str] = {}
    for service in services:
        try:
            completed = subprocess.run(
                ["systemctl", "is-active", service],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )
            result[service] = (completed.stdout or completed.stderr or "unknown").strip() or "unknown"
        except (OSError, subprocess.TimeoutExpired):
            result[service] = "unknown"
    return result


def fetch_json(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {"ok": False, "reason": "non_object_json"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": type(exc).__name__}


def report_data(report: OpsStatusReport) -> dict[str, Any]:
    data = asdict(report)
    data["checks"] = [asdict(check) for check in report.checks]
    return data


def _health_check(name: str, payload: dict[str, Any], *, required_flags: tuple[str, ...] = ()) -> OpsCheck:
    missing = [flag for flag in required_flags if not payload.get(flag)]
    ok = bool(payload.get("ok")) and not missing
    detail = "Health endpoint is OK." if ok else "Health endpoint failed or required flags are false."
    return OpsCheck(name, ok, "error", detail, {"missing_flags": missing, "ok": payload.get("ok"), **{flag: payload.get(flag) for flag in required_flags}})


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AutomaticCosmetic operational status audit")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    args = parser.parse_args(argv)
    report = build_ops_status_report()
    data = report_data(report)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
