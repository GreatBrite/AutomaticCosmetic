from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from .config import IntegrationSettings
from .runtime import booking_from_settings


@dataclass(frozen=True)
class LaunchCheck:
    name: str
    ok: bool
    detail: str
    severity: str = "info"


@dataclass(frozen=True)
class PrelaunchReport:
    ok_for_preview: bool
    ok_for_live: bool
    ok_for_avito_live: bool
    ok_for_yclients_mutations: bool
    ok_for_vk_preview: bool
    checks: tuple[LaunchCheck, ...]
    flags: dict[str, Any]
    next_step: str


def build_prelaunch_report(settings: IntegrationSettings | None = None) -> PrelaunchReport:
    settings = settings or IntegrationSettings.from_env()
    checks = [
        LaunchCheck("yclients_credentials", settings.yclients_ready, "YCLIENTS live-read credentials are present.", "error"),
        LaunchCheck("avito_credentials", settings.avito_ready, "Avito client credentials and webhook secret are present.", "error"),
        LaunchCheck("vk_credentials", settings.vk_ready, "VK group token and group id are present.", "warning"),
        LaunchCheck(
            "telegram_admin_ids",
            bool(settings.telegram_admin_user_id or settings.telegram_cosmetologist_user_id or settings.telegram_extra_admin_user_ids),
            "At least one Telegram admin/cosmetologist user id is configured.",
            "warning",
        ),
        LaunchCheck(
            "handoff_chat",
            bool(settings.handoff_notify_chat_id),
            "HANDOFF_NOTIFY_CHAT_ID is configured before live handoff notifications.",
            "warning",
        ),
        LaunchCheck("avito_send_guard", not settings.avito_send_enabled, "AVITO_SEND_ENABLED stays false during preview.", "error"),
        LaunchCheck("yclients_mutation_guard", not settings.yclients_allow_mutations, "YCLIENTS_ALLOW_MUTATIONS stays false during preview.", "error"),
        LaunchCheck("vk_send_guard", not settings.vk_send_enabled, "VK_SEND_ENABLED stays false until VK preview is reviewed.", "warning"),
    ]
    flags = {
        "public_base_url": settings.public_base_url,
        "cities": settings.cities,
        "yclients_ready": settings.yclients_ready,
        "yclients_allow_mutations": settings.yclients_allow_mutations,
        "avito_ready": settings.avito_ready,
        "avito_send_enabled": settings.avito_send_enabled,
        "avito_codex_enabled": settings.avito_codex_enabled,
        "handoff_notify_ready": settings.handoff_notify_ready,
        "vk_ready": settings.vk_ready,
        "vk_group_id": settings.vk_group_id,
        "vk_send_enabled": settings.vk_send_enabled,
        "vk_codex_enabled": settings.vk_codex_enabled,
    }
    ok_for_preview = _required_ok(checks, "yclients_credentials", "avito_credentials", "avito_send_guard", "yclients_mutation_guard")
    ok_for_avito_live = settings.avito_ready and settings.avito_codex_enabled and settings.handoff_notify_ready
    ok_for_yclients_mutations = ok_for_avito_live and settings.avito_send_enabled and settings.yclients_ready
    ok_for_vk_preview = settings.vk_ready and not settings.vk_send_enabled
    ok_for_live = (
        settings.yclients_ready
        and settings.avito_ready
        and settings.vk_ready
        and settings.avito_codex_enabled
        and settings.vk_codex_enabled
        and settings.handoff_notify_ready
        and settings.avito_send_enabled
        and settings.vk_send_enabled
        and settings.yclients_allow_mutations
    )
    return PrelaunchReport(
        ok_for_preview=ok_for_preview,
        ok_for_live=ok_for_live,
        ok_for_avito_live=ok_for_avito_live,
        ok_for_yclients_mutations=ok_for_yclients_mutations,
        ok_for_vk_preview=ok_for_vk_preview,
        checks=tuple(checks),
        flags=flags,
        next_step=_next_step(settings, ok_for_preview, ok_for_live, ok_for_avito_live, ok_for_yclients_mutations),
    )


async def probe_yclients_read(settings: IntegrationSettings | None = None) -> dict[str, Any]:
    settings = settings or IntegrationSettings.from_env()
    if not settings.yclients_ready:
        return {"ok": False, "reason": "yclients_not_ready"}
    booking = booking_from_settings(settings)
    services = await booking.get_services(settings.cities[0] if settings.cities else "")
    appointments = await booking.list_appointments(date.today().isoformat())
    return {
        "ok": True,
        "service_count": len(services),
        "appointment_count_today": len(appointments),
        "mutations_enabled": settings.yclients_allow_mutations,
    }


def report_data(report: PrelaunchReport) -> dict[str, Any]:
    data = asdict(report)
    data["checks"] = [asdict(check) for check in report.checks]
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automatic Cosmetic prelaunch readiness check")
    parser.add_argument("--probe-yclients", action="store_true", help="Run read-only live YCLIENTS service/appointment probe.")
    args = parser.parse_args(argv)
    report = build_prelaunch_report()
    data = report_data(report)
    if args.probe_yclients:
        data["yclients_probe"] = asyncio.run(probe_yclients_read())
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    return 0 if report.ok_for_preview else 1


def _required_ok(checks: list[LaunchCheck], *names: str) -> bool:
    by_name = {check.name: check.ok for check in checks}
    return all(by_name.get(name, False) for name in names)


def _next_step(
    settings: IntegrationSettings,
    ok_for_preview: bool,
    ok_for_live: bool,
    ok_for_avito_live: bool,
    ok_for_yclients_mutations: bool,
) -> str:
    if ok_for_live:
        return "Live включён: мониторить реальные Avito/VK/YCLIENTS события, agent_trace и handoff-уведомления."
    if not ok_for_preview:
        return "Проверить credentials и staged/live-флаги: сейчас это не preview-режим."
    if not settings.avito_codex_enabled:
        return "Включить AVITO_CODEX_ENABLED=true и прогнать 10-20 Avito preview-диалогов."
    if not settings.handoff_notify_ready:
        if settings.handoff_notify_chat_id:
            return "Проверить admin chat и включить HANDOFF_NOTIFY_ENABLED=true для ручных уведомлений."
        return "Настроить HANDOFF_NOTIFY_CHAT_ID и включить HANDOFF_NOTIFY_ENABLED=true после проверки admin chat."
    if not settings.avito_send_enabled:
        return "После ревью preview включить AVITO_SEND_ENABLED=true."
    if not ok_for_yclients_mutations:
        return "Проверить тестовую запись/перенос/отмену перед YCLIENTS_ALLOW_MUTATIONS=true."
    if settings.vk_ready and not settings.vk_send_enabled:
        return "Прогнать VK preview через run_vk_bot.sh, затем отдельно решить про VK_SEND_ENABLED=true."
    if ok_for_avito_live:
        return "Продолжать мониторинг логов, outbox и реальных чатов первые сутки."
    return "Продолжать staged rollout."


if __name__ == "__main__":
    raise SystemExit(main())
