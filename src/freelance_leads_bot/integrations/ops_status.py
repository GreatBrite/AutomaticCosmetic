from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import IntegrationSettings


DEFAULT_UNANSWERED_REPORT_PATH = Path("data/avito_unanswered_report.json")
DEFAULT_UNANSWERED_STATE_PATH = Path("data/avito_unanswered_monitor_state.json")
DEFAULT_DATA_PATH = Path("data")
DEFAULT_DATA_WARNING_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_DATA_ENTRY_WARNING_BYTES = 512 * 1024 * 1024
DEFAULT_DISK_FREE_WARNING_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_DISK_FREE_WARNING_RATIO = 0.10

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
    data_path: Path = DEFAULT_DATA_PATH,
    data_warning_bytes: int = DEFAULT_DATA_WARNING_BYTES,
    data_entry_warning_bytes: int = DEFAULT_DATA_ENTRY_WARNING_BYTES,
    disk_free_warning_bytes: int = DEFAULT_DISK_FREE_WARNING_BYTES,
    disk_free_warning_ratio: float = DEFAULT_DISK_FREE_WARNING_RATIO,
    now: int | None = None,
) -> OpsStatusReport:
    settings = settings or IntegrationSettings.from_env()
    generated_at = int(time.time()) if now is None else int(now)
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

    unanswered = read_unanswered_status(
        unanswered_report_path,
        unanswered_state_path,
        now=generated_at,
        stale_after_seconds=_unanswered_report_stale_after_seconds(settings),
    )
    actionable = int(unanswered.get("actionable_count") or 0)
    failed = int(unanswered.get("failed_count") or 0)
    report_is_stale = bool(unanswered.get("report_is_stale"))
    report_age = int(unanswered.get("report_age_seconds") or 0)
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
            "avito_unanswered_report_fresh",
            not report_is_stale,
            "warning",
            "Delayed Avito report is fresh."
            if not report_is_stale
            else f"Delayed Avito report is stale or missing; age={report_age}s.",
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
    rag_approved = int(rag.get("approved_count") or 0)
    rag_needs_review = int(rag.get("needs_review_count") or 0)
    checks.append(
        OpsCheck(
            "expert_rag",
            bool(rag.get("exists")) and rag_approved > 0,
            "warning",
            "Expert RAG has approved answers." if rag_approved > 0 else "Expert RAG has no approved answers.",
            rag,
        )
    )
    checks.append(
        OpsCheck(
            "expert_rag_needs_review",
            rag_needs_review == 0,
            "warning",
            "No expert RAG items need review."
            if rag_needs_review == 0
            else (
                f"{rag_needs_review} expert RAG items need review. "
                "Run: python -m src.freelance_leads_bot.integrations.expert_rag_review export --output data/expert_rag_review.md"
            ),
            rag,
        )
    )

    data_footprint = read_data_footprint(
        data_path,
        warning_bytes=data_warning_bytes,
        entry_warning_bytes=data_entry_warning_bytes,
    )
    data_total = int(data_footprint.get("total_bytes") or 0)
    largest_entry = data_footprint.get("largest_entry") if isinstance(data_footprint.get("largest_entry"), dict) else {}
    largest_entry_size = int(largest_entry.get("size_bytes") or 0)
    data_ok = bool(data_footprint.get("ok"))
    checks.append(
        OpsCheck(
            "data_footprint",
            data_ok,
            "warning",
            "Data directory footprint is within warning thresholds."
            if data_ok
            else (
                f"Data footprint needs attention: total={_format_bytes(data_total)}, "
                f"largest={largest_entry.get('path') or '-'} {_format_bytes(largest_entry_size)}."
            ),
            data_footprint,
        )
    )
    disk = read_disk_status(
        data_path,
        free_warning_bytes=disk_free_warning_bytes,
        free_warning_ratio=disk_free_warning_ratio,
    )
    disk_ok = bool(disk.get("ok"))
    free_bytes = int(disk.get("free_bytes") or 0)
    free_ratio = float(disk.get("free_ratio") or 0.0)
    checks.append(
        OpsCheck(
            "disk_free_space",
            disk_ok,
            "warning",
            "Disk free space is within warning thresholds."
            if disk_ok
            else f"Disk free space is low: free={_format_bytes(free_bytes)} ({free_ratio:.1%}).",
            disk,
        )
    )

    flags = safe_live_flags(settings)
    summary = {
        "services_active": not inactive,
        "avito_actionable": actionable,
        "avito_autoreply_failed": failed,
        "avito_unanswered_report_age_seconds": report_age,
        "rag_approved": rag_approved,
        "rag_high_risk_approved": rag.get("approved_high_risk_count", 0),
        "rag_needs_review": rag_needs_review,
        "data_total_bytes": data_total,
        "data_largest_path": largest_entry.get("path", ""),
        "data_largest_bytes": largest_entry_size,
        "disk_free_bytes": free_bytes,
        "disk_total_bytes": int(disk.get("total_bytes") or 0),
        "disk_free_ratio": free_ratio,
    }
    ok = all(check.ok or check.severity != "error" for check in checks)
    return OpsStatusReport(ok=ok, generated_at=generated_at, checks=tuple(checks), flags=flags, summary=summary)


def safe_live_flags(settings: IntegrationSettings) -> dict[str, Any]:
    return {
        "avito_ready": settings.avito_ready,
        "avito_send_enabled": settings.avito_send_enabled,
        "avito_codex_enabled": settings.avito_codex_enabled,
        "avito_unanswered_autoreply_enabled": settings.avito_unanswered_autoreply_enabled,
        "avito_unanswered_interval_seconds": settings.avito_unanswered_interval_seconds,
        "yclients_ready": settings.yclients_ready,
        "yclients_allow_mutations": settings.yclients_allow_mutations,
        "handoff_notify_ready": settings.handoff_notify_ready,
        "rag_retrieval_enabled": settings.rag_retrieval_enabled,
        "rag_autoanswer_threshold": settings.rag_autoanswer_threshold,
        "rag_handoff_threshold": settings.rag_handoff_threshold,
        "vk_ready": settings.vk_ready,
        "vk_send_enabled": settings.vk_send_enabled,
    }


def read_unanswered_status(
    report_path: Path,
    state_path: Path,
    *,
    now: int | None = None,
    stale_after_seconds: int = 900,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    report = _read_json(report_path)
    state = _read_json(state_path)
    items = report.get("items") if isinstance(report.get("items"), list) else []
    actionable_count = int(report.get("actionable_count") or sum(1 for item in items if isinstance(item, dict) and item.get("needs_action")))
    failed = state.get("failed") if isinstance(state.get("failed"), dict) else {}
    handled = state.get("handled") if isinstance(state.get("handled"), dict) else {}
    report_created_at = str(report.get("created_at") or "")
    report_created_ts = _parse_iso_timestamp(report_created_at)
    report_age = max(0, now_ts - report_created_ts) if report_created_ts > 0 else 0
    stale_after = max(1, int(stale_after_seconds or 1))
    report_is_stale = not report_path.exists() or report_created_ts <= 0 or report_age > stale_after
    return {
        "report_exists": report_path.exists(),
        "state_exists": state_path.exists(),
        "report_created_at": report_created_at,
        "report_age_seconds": report_age,
        "report_stale_after_seconds": stale_after,
        "report_is_stale": report_is_stale,
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


def _unanswered_report_stale_after_seconds(settings: IntegrationSettings) -> int:
    interval = max(0, int(settings.avito_unanswered_interval_seconds or 0))
    return max(interval * 2, 900)


def _parse_iso_timestamp(value: str) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


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


def ops_status_exit_code(report: OpsStatusReport, *, strict: bool = False) -> int:
    if strict:
        return 0 if all(check.ok for check in report.checks) else 1
    return 0 if report.ok else 1


def format_ops_status_report(report: OpsStatusReport) -> str:
    failing_errors = [check for check in report.checks if not check.ok and check.severity == "error"]
    failing_warnings = [check for check in report.checks if not check.ok and check.severity == "warning"]
    status = "ERROR" if failing_errors else "WARN" if failing_warnings else "OK"
    summary = report.summary
    flags = report.flags
    lines = [
        f"AutomaticCosmetic ops: {status}",
        (
            "Services: "
            + ("active" if summary.get("services_active") else "attention needed")
            + f" | Avito actionable={summary.get('avito_actionable', 0)}"
            + f" failed_autoreplies={summary.get('avito_autoreply_failed', 0)}"
            + f" report_age={summary.get('avito_unanswered_report_age_seconds', 0)}s"
        ),
        (
            f"RAG: approved={summary.get('rag_approved', 0)}"
            f" high_risk_approved={summary.get('rag_high_risk_approved', 0)}"
            f" needs_review={summary.get('rag_needs_review', 0)}"
        ),
        (
            f"Data: total={_format_bytes(int(summary.get('data_total_bytes') or 0))}"
            f" largest={summary.get('data_largest_path') or '-'}"
            f" {_format_bytes(int(summary.get('data_largest_bytes') or 0))}"
            f" | disk_free={_format_bytes(int(summary.get('disk_free_bytes') or 0))}"
            f" ({float(summary.get('disk_free_ratio') or 0.0):.1%})"
        ),
        (
            "Live flags: "
            f"avito_send={_on_off(flags.get('avito_send_enabled'))}, "
            f"avito_codex={_on_off(flags.get('avito_codex_enabled'))}, "
            f"avito_autoreply={_on_off(flags.get('avito_unanswered_autoreply_enabled'))}, "
            f"yclients_mutations={_on_off(flags.get('yclients_allow_mutations'))}"
        ),
    ]
    if failing_errors:
        lines.append("Errors:")
        lines.extend(f"- {check.name}: {check.detail}" for check in failing_errors)
    if failing_warnings:
        lines.append("Warnings:")
        lines.extend(f"- {check.name}: {check.detail}" for check in failing_warnings)
    if not failing_errors and not failing_warnings:
        lines.append("No immediate action required.")
    return "\n".join(lines)


def _health_check(name: str, payload: dict[str, Any], *, required_flags: tuple[str, ...] = ()) -> OpsCheck:
    missing = [flag for flag in required_flags if not payload.get(flag)]
    ok = bool(payload.get("ok")) and not missing
    detail = "Health endpoint is OK." if ok else "Health endpoint failed or required flags are false."
    return OpsCheck(name, ok, "error", detail, {"missing_flags": missing, "ok": payload.get("ok"), **{flag: payload.get(flag) for flag in required_flags}})


def _on_off(value: Any) -> str:
    return "on" if bool(value) else "off"


def read_data_footprint(
    path: Path,
    *,
    warning_bytes: int = DEFAULT_DATA_WARNING_BYTES,
    entry_warning_bytes: int = DEFAULT_DATA_ENTRY_WARNING_BYTES,
    top_limit: int = 8,
) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "ok": True,
            "total_bytes": 0,
            "warning_bytes": warning_bytes,
            "entry_warning_bytes": entry_warning_bytes,
            "top_entries": [],
            "largest_entry": {},
        }
    entries: list[dict[str, Any]] = []
    total = 0
    children = list(path.iterdir()) if path.is_dir() else [path]
    for child in children:
        size = _path_size(child)
        total += size
        entries.append({"path": str(child), "size_bytes": size, "size": _format_bytes(size), "is_dir": child.is_dir()})
    entries.sort(key=lambda item: int(item.get("size_bytes") or 0), reverse=True)
    largest_entry = entries[0] if entries else {}
    largest_size = int(largest_entry.get("size_bytes") or 0)
    ok = total <= warning_bytes and largest_size <= entry_warning_bytes
    return {
        "exists": True,
        "path": str(path),
        "ok": ok,
        "total_bytes": total,
        "total": _format_bytes(total),
        "warning_bytes": warning_bytes,
        "entry_warning_bytes": entry_warning_bytes,
        "top_entries": entries[: max(1, int(top_limit or 1))],
        "largest_entry": largest_entry,
    }


def read_disk_status(
    path: Path,
    *,
    free_warning_bytes: int = DEFAULT_DISK_FREE_WARNING_BYTES,
    free_warning_ratio: float = DEFAULT_DISK_FREE_WARNING_RATIO,
) -> dict[str, Any]:
    target = Path(path)
    probe = target if target.exists() else target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return {
            "ok": False,
            "path": str(path),
            "probe_path": str(probe),
            "error": type(exc).__name__,
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "free_ratio": 0.0,
            "free_warning_bytes": free_warning_bytes,
            "free_warning_ratio": free_warning_ratio,
        }
    total = int(usage.total)
    free = int(usage.free)
    used = int(usage.used)
    ratio = free / total if total > 0 else 0.0
    ok = free >= int(free_warning_bytes) and ratio >= float(free_warning_ratio)
    return {
        "ok": ok,
        "path": str(path),
        "probe_path": str(probe),
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "free_ratio": ratio,
        "total": _format_bytes(total),
        "used": _format_bytes(used),
        "free": _format_bytes(free),
        "free_warning_bytes": int(free_warning_bytes),
        "free_warning_ratio": float(free_warning_ratio),
    }


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if not path.is_dir():
            return 0
    except OSError:
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += int(child.stat().st_size)
        except OSError:
            continue
    return total


def _format_bytes(value: int) -> str:
    size = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{unit}"
        size /= 1024


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
    parser.add_argument("--strict", action="store_true", help="Return non-zero for warnings as well as errors.")
    args = parser.parse_args(argv)
    report = build_ops_status_report()
    data = report_data(report)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(format_ops_status_report(report))
    return ops_status_exit_code(report, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
