#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_open_handoffs import build_open_handoffs_export  # noqa: E402
from scripts.verify_logrotate_config import verify_logrotate_config  # noqa: E402
from scripts.verify_runtime_backup import verify_runtime_backup  # noqa: E402
from src.freelance_leads_bot.integrations.expert_rag_review import run_review_command  # noqa: E402
from src.freelance_leads_bot.integrations.handoff_refs import DEFAULT_HANDOFF_REFS_PATH  # noqa: E402
from src.freelance_leads_bot.integrations.ops_status import (  # noqa: E402
    DEFAULT_UNANSWERED_REPORT_PATH,
    DEFAULT_UNANSWERED_STATE_PATH,
    build_ops_status_report,
    format_ops_status_report,
    ops_status_exit_code,
    report_data,
)


def build_production_readiness_report(
    *,
    unanswered_report_path: Path = DEFAULT_UNANSWERED_REPORT_PATH,
    unanswered_state_path: Path = DEFAULT_UNANSWERED_STATE_PATH,
    handoff_refs_path: Path | None = None,
    care_crm_path: Path | None = None,
    rag_db_path: Path | None = None,
    data_path: Path | None = None,
    backup_dir: Path = Path("backups"),
    logrotate_path: Path = Path("deploy/logrotate/automaticcosmetic"),
    service_states: dict[str, str] | None = None,
    avito_health: dict[str, Any] | None = None,
    yclients_health: dict[str, Any] | None = None,
    now: int | None = None,
    include_backup_verify: bool = True,
    handoff_limit: int = 50,
    temporal_limit: int = 200,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now is None else int(now)
    blockers: list[str] = []
    manual_actions: list[str] = []
    resolved_handoff_refs_path = handoff_refs_path or (
        DEFAULT_HANDOFF_REFS_PATH
        if Path(unanswered_report_path) == DEFAULT_UNANSWERED_REPORT_PATH
        else Path(unanswered_report_path).parent / DEFAULT_HANDOFF_REFS_PATH.name
    )

    ops_report = build_ops_status_report(
        service_states=service_states,
        avito_health=avito_health,
        yclients_health=yclients_health,
        unanswered_report_path=unanswered_report_path,
        unanswered_state_path=unanswered_state_path,
        handoff_refs_path=resolved_handoff_refs_path,
        care_crm_path=care_crm_path or Path("data/care_crm.sqlite3"),
        rag_db_path=rag_db_path,
        data_path=data_path or Path("data"),
        now=now_ts,
    )
    ops_payload = report_data(ops_report)
    if ops_status_exit_code(ops_report, strict=True) != 0:
        blockers.append("ops_status --strict is not green")
    ops_summary = ops_payload.get("summary") if isinstance(ops_payload.get("summary"), dict) else {}
    handoff_manual_no_reply = int(ops_summary.get("handoff_manual_closed_without_client_reply") or 0)
    avito_manual_no_reply = int(ops_summary.get("avito_manual_closed_without_client_reply") or 0)
    avito_pending_promises = int(ops_summary.get("avito_pending_followups") or 0)
    avito_critical_promises = int(ops_summary.get("avito_critical_followups") or 0)
    avito_overdue_promises = int(ops_summary.get("avito_overdue_followups") or 0)
    poller_last_chats = int(ops_summary.get("avito_poller_last_chats") or 0)
    poller_expected_chats = int(ops_summary.get("avito_poller_expected_chats") or 0)
    poller_age_seconds = int(ops_summary.get("avito_poller_age_seconds") or 0)
    if poller_expected_chats and poller_last_chats < poller_expected_chats:
        manual_actions.append(
            f"Fix Avito missed-poller coverage: latest summary scanned {poller_last_chats}/{poller_expected_chats} chats; check .env/systemd and restart the poller."
        )
    if handoff_manual_no_reply > 0:
        manual_actions.append(
            f"Review {handoff_manual_no_reply} Telegram handoff closures marked closed_manual_no_client_reply in Avito audit history."
        )
    if avito_manual_no_reply > 0:
        manual_actions.append(
            f"Review {avito_manual_no_reply} Avito pending promises closed manually without a confirmed client reply."
        )
    if avito_critical_promises > 0 or avito_overdue_promises > 0:
        blockers.append(
            f"{max(avito_critical_promises, avito_overdue_promises)} critical/overdue Avito bot promises need final reply"
        )
        manual_actions.append(
            f"Review /avito_followups: pending={avito_pending_promises}, critical={avito_critical_promises}, overdue={avito_overdue_promises}; close only after a final client reply or explicit not-relevant decision."
        )

    handoffs = build_open_handoffs_export(
        refs_path=resolved_handoff_refs_path,
        now=now_ts,
        limit=handoff_limit,
    )
    if int(handoffs.get("open_count") or 0) > 0:
        blockers.append(f"{handoffs.get('open_count')} open Olga handoffs need manual review")
        manual_actions.append("Review open Olga handoffs in Avito/Telegram topics and close only with a clear result.")

    temporal = _temporal_cleanup_status(rag_db_path=rag_db_path, limit=temporal_limit)
    if int(temporal.get("planned_count") or 0) > 0:
        blockers.append(f"{temporal.get('planned_count')} temporal RAG autoanswer items need cleanup decision")
        manual_actions.append(
            "Review temporal RAG cleanup export, mark per-item decisions, dry-run --decisions, then apply only checked block_autoanswer decisions."
        )

    backup = _backup_status(backup_dir, enabled=include_backup_verify)
    if include_backup_verify and not backup.get("ok"):
        blockers.append("runtime backup restore verification failed or no backup was found")

    logrotate = verify_logrotate_config(logrotate_path)
    if not logrotate.get("ok"):
        blockers.append("logrotate config is unsafe or incomplete")

    ok = not blockers
    return {
        "ok": ok,
        "generated_at": now_ts,
        "generated_at_iso": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "blockers": blockers,
        "manual_actions": manual_actions,
        "ops_status": ops_payload,
        "ops_status_text": format_ops_status_report(ops_report),
        "open_handoffs": {
            **{
                key: value
                for key, value in handoffs.items()
                if key not in {"items"}
            },
            "export_command": "python scripts/export_open_handoffs.py --output data/open_handoffs_review.md",
            "dry_run_decisions_command": "python scripts/export_open_handoffs.py --decisions data/open_handoffs_review.md",
            "apply_decisions_command": "python scripts/export_open_handoffs.py --decisions data/open_handoffs_review.md --apply-decisions",
        },
        "manual_closure_audit": {
            "handoff_manual_closed_without_client_reply": handoff_manual_no_reply,
            "avito_manual_closed_without_client_reply": avito_manual_no_reply,
        },
        "poller_coverage": {
            "last_chats": poller_last_chats,
            "expected_chats": poller_expected_chats,
            "age_seconds": poller_age_seconds,
        },
        "avito_promises": {
            "pending": avito_pending_promises,
            "critical": avito_critical_promises,
            "overdue": avito_overdue_promises,
            "manual_closed_without_client_reply": avito_manual_no_reply,
            "export_command": "python scripts/export_avito_followups.py --output data/avito_followups_review.md",
            "dry_run_decisions_command": "python scripts/export_avito_followups.py --decisions data/avito_followups_review.md",
            "apply_decisions_command": "python scripts/export_avito_followups.py --decisions data/avito_followups_review.md --apply-decisions",
        },
        "open_handoff_samples": handoffs.get("items", [])[: min(10, handoff_limit)],
        "temporal_rag_cleanup": {
            "ok": temporal.get("ok"),
            "dry_run": temporal.get("dry_run"),
            "planned_count": temporal.get("planned_count", 0),
            "sample_ids": [item.get("id") for item in temporal.get("planned", [])[:10] if isinstance(item, dict)],
            "export_command": "python -m src.freelance_leads_bot.integrations.expert_rag_review temporal-cleanup --output data/expert_rag_temporal_cleanup.md",
            "dry_run_decisions_command": "python -m src.freelance_leads_bot.integrations.expert_rag_review temporal-cleanup --decisions data/expert_rag_temporal_cleanup.md",
            "apply_decisions_command": "python -m src.freelance_leads_bot.integrations.expert_rag_review temporal-cleanup --decisions data/expert_rag_temporal_cleanup.md --apply",
        },
        "backup_restore_verify": backup,
        "logrotate": logrotate,
    }


def format_production_readiness_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# AutomaticCosmetic Production Readiness",
        "",
        f"Generated: `{report.get('generated_at_iso')}`",
        f"Status: `{'OK' if report.get('ok') else 'BLOCKED'}`",
        "",
        "## Blockers",
        "",
    ]
    blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
    if blockers:
        lines.extend(f"- {blocker}" for blocker in blockers)
    else:
        lines.append("- No blockers detected.")
    manual_actions = report.get("manual_actions") if isinstance(report.get("manual_actions"), list) else []
    if manual_actions:
        lines.extend(["", "## Manual Actions", ""])
        lines.extend(f"- {action}" for action in manual_actions)
    lines.extend(
        [
            "",
            "## Ops Status",
            "",
            "```text",
            str(report.get("ops_status_text") or ""),
            "```",
            "",
            "## Open Handoffs",
            "",
        ]
    )
    handoffs = report.get("open_handoffs") if isinstance(report.get("open_handoffs"), dict) else {}
    manual_closure_audit = report.get("manual_closure_audit") if isinstance(report.get("manual_closure_audit"), dict) else {}
    lines.append(
        f"Open: `{handoffs.get('open_count', 0)}`, critical: `{handoffs.get('critical_count', 0)}`, "
        f"draft_pending: `{handoffs.get('draft_pending_count', 0)}`, oldest: `{round(int(handoffs.get('oldest_age_seconds') or 0) / 3600, 1)}h`"
    )
    lines.append(
        "Manual closures without client reply: "
        f"`handoff={manual_closure_audit.get('handoff_manual_closed_without_client_reply', 0)}`, "
        f"`avito_promises={manual_closure_audit.get('avito_manual_closed_without_client_reply', 0)}`"
    )
    samples = report.get("open_handoff_samples") if isinstance(report.get("open_handoff_samples"), list) else []
    for item in samples[:5]:
        lines.append(f"- `{item.get('avito_chat_id') or '-'}` {item.get('client_name') or 'Без имени'}: {item.get('age_hours')}h, critical={item.get('critical')}")
    lines.extend(
        [
            "",
            "```bash",
            str(handoffs.get("export_command") or ""),
            str(handoffs.get("dry_run_decisions_command") or ""),
            str(handoffs.get("apply_decisions_command") or ""),
            "```",
        ]
    )
    temporal = report.get("temporal_rag_cleanup") if isinstance(report.get("temporal_rag_cleanup"), dict) else {}
    poller = report.get("poller_coverage") if isinstance(report.get("poller_coverage"), dict) else {}
    avito_promises = report.get("avito_promises") if isinstance(report.get("avito_promises"), dict) else {}
    backup = report.get("backup_restore_verify") if isinstance(report.get("backup_restore_verify"), dict) else {}
    logrotate = report.get("logrotate") if isinstance(report.get("logrotate"), dict) else {}
    lines.extend(
        [
            "",
            "## Avito Missed Poller",
            "",
            f"Latest chats: `{poller.get('last_chats', 0)}/{poller.get('expected_chats', 0)}`, age: `{poller.get('age_seconds', 0)}s`",
            "",
            "## Avito Pending Promises",
            "",
            f"Pending: `{avito_promises.get('pending', 0)}`, critical: `{avito_promises.get('critical', 0)}`, overdue: `{avito_promises.get('overdue', 0)}`, manual_no_client_reply: `{avito_promises.get('manual_closed_without_client_reply', 0)}`",
            "",
            "```bash",
            str(avito_promises.get("export_command") or ""),
            str(avito_promises.get("dry_run_decisions_command") or ""),
            str(avito_promises.get("apply_decisions_command") or ""),
            "```",
            "",
            "## RAG Temporal Cleanup",
            "",
            f"Planned: `{temporal.get('planned_count', 0)}`, sample ids: `{', '.join(str(item) for item in temporal.get('sample_ids', [])) or '-'}`",
            "",
            "```bash",
            str(temporal.get("export_command") or ""),
            str(temporal.get("dry_run_decisions_command") or ""),
            str(temporal.get("apply_decisions_command") or ""),
            "```",
            "",
            "## Backup Restore Verify",
            "",
            f"OK: `{backup.get('ok')}`, stamp: `{backup.get('stamp', '-')}`, sensitive archive: `{backup.get('contains_sensitive_runtime_secrets', '-')}`",
            "",
            "## Logrotate",
            "",
            f"OK: `{logrotate.get('ok')}`, forbidden matches: `{', '.join(str(item) for item in logrotate.get('forbidden_matches', [])) or '-'}`",
        ]
    )
    return "\n".join(lines)


def _temporal_cleanup_status(*, rag_db_path: Path | None, limit: int) -> dict[str, Any]:
    args = ["--json"]
    if rag_db_path:
        args.extend(["--db", str(rag_db_path)])
    args.extend(["temporal-cleanup", "--limit", str(limit)])
    code, output = run_review_command(args)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {"ok": False, "error": output, "code": code, "planned_count": 0, "planned": []}
    payload["ok"] = bool(payload.get("ok")) and code == 0
    return payload


def _backup_status(backup_dir: Path, *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"ok": True, "skipped": True}
    try:
        return verify_runtime_backup(backup_dir=backup_dir)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "backup_dir": str(backup_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build read-only AutomaticCosmetic production readiness report.")
    parser.add_argument("--unanswered-report", type=Path, default=DEFAULT_UNANSWERED_REPORT_PATH)
    parser.add_argument("--unanswered-state", type=Path, default=DEFAULT_UNANSWERED_STATE_PATH)
    parser.add_argument("--handoff-refs", type=Path)
    parser.add_argument("--care-crm", type=Path)
    parser.add_argument("--rag-db", type=Path)
    parser.add_argument("--data-path", type=Path, default=Path("data"))
    parser.add_argument("--backup-dir", type=Path, default=Path("backups"))
    parser.add_argument("--logrotate", type=Path, default=Path("deploy/logrotate/automaticcosmetic"))
    parser.add_argument("--skip-backup-verify", action="store_true")
    parser.add_argument("--handoff-limit", type=int, default=50)
    parser.add_argument("--temporal-limit", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_production_readiness_report(
        unanswered_report_path=args.unanswered_report,
        unanswered_state_path=args.unanswered_state,
        handoff_refs_path=args.handoff_refs,
        care_crm_path=args.care_crm,
        rag_db_path=args.rag_db,
        data_path=args.data_path,
        backup_dir=args.backup_dir,
        logrotate_path=args.logrotate,
        include_backup_verify=not args.skip_backup_verify,
        handoff_limit=args.handoff_limit,
        temporal_limit=args.temporal_limit,
    )
    content = json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_production_readiness_markdown(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
        print(f"Exported production readiness report to {args.output}. Status={'OK' if report.get('ok') else 'BLOCKED'}")
    else:
        print(content)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
