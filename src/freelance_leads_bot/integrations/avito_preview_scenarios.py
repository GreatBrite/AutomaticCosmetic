from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from .config import IntegrationSettings


@dataclass(frozen=True)
class PreviewScenario:
    name: str
    event: dict[str, Any]
    expected_action: str = ""
    expected_handoff: str = ""


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    ok: bool
    status: int
    action: str = ""
    reply: str = ""
    planner: str | None = None
    send_reason: str = ""
    handoff: str | None = None
    error: str = ""


def default_scenarios() -> list[PreviewScenario]:
    created = int(time.time())
    run_id = str(created)
    photo_id = f"preview-{run_id}-photo"
    return [
        PreviewScenario(
            name="listing_price_cleaning",
            event=
            _message(
                f"preview-{run_id}-price-cleaning",
                "Здравствуйте, сколько стоит чистка лица?",
                item={"id": 101, "title": "Чистка лица", "price_string": "3500 ₽", "city": "Москва"},
                created=created,
            ),
        ),
        PreviewScenario(
            name="service_price_botox",
            event=
            _message(
                f"preview-{run_id}-price-botox",
                "Сколько стоит ботокс лоб?",
                item={"id": 102, "title": "Ботокс", "price_string": "от 3000 ₽", "city": "Ростов-на-Дону"},
                created=created + 1,
            ),
        ),
        PreviewScenario(
            name="booking_slots",
            event=_message(f"preview-{run_id}-booking-slots", "Ростов, хочу записаться на чистку лица завтра", created=created + 2),
        ),
        PreviewScenario(
            name="booking_with_phone",
            event=
            _message(
                f"preview-{run_id}-booking-phone",
                "Ростов, чистка лица 2026-06-01 в 11:00, телефон 8 999 123 45 67",
                created=created + 3,
            ),
        ),
        PreviewScenario(
            name="medical_contraindications",
            event=_message(f"preview-{run_id}-medical", "Какие противопоказания после пилинга?", created=created + 4),
        ),
        PreviewScenario(
            name="pregnancy_question",
            event=_message(f"preview-{run_id}-pregnancy", "Можно делать ботокс при беременности?", created=created + 5),
        ),
        PreviewScenario(
            name="calculation_question",
            event=
            _message(
                f"preview-{run_id}-calculation",
                "Сколько мл нужно для увеличения губ?",
                item={"id": 103, "title": "Увеличение губ", "price_string": "от 9000 ₽", "city": "Санкт-Петербург"},
                created=created + 6,
            ),
        ),
        PreviewScenario(
            name="listing_context",
            event=
            _message(
                f"preview-{run_id}-listing-context",
                "Актуально?",
                item={"id": 104, "title": "Биоревитализация", "price_string": "6500 ₽", "city": "Краснодар"},
                created=created + 7,
            ),
        ),
        PreviewScenario(
            name="photo_handoff",
            event=_message(photo_id, "Посмотрите фото", photo=True, created=created + 8),
            expected_action="handoff",
            expected_handoff="photo_consultation",
        ),
        PreviewScenario(
            name="complaint_or_risk",
            event=_message(f"preview-{run_id}-risk", "После процедуры сильная аллергия, что делать?", created=created + 9),
            expected_action="handoff",
            expected_handoff="complaint_or_risk",
        ),
        PreviewScenario(
            name="duplicate_check",
            event=_message(photo_id, "Посмотрите фото", photo=True, created=created + 10),
            expected_action="ignored",
        ),
    ]


def run_scenarios(base_url: str, token: str, timeout_seconds: int = 280) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for scenario in default_scenarios():
        results.append(_post_scenario(base_url, token, scenario, timeout_seconds))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Avito preview webhook scenarios without enabling live send.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8030", help="Local Avito webhook base URL.")
    parser.add_argument("--timeout", type=int, default=280, help="Per-scenario timeout in seconds.")
    args = parser.parse_args(argv)
    settings = IntegrationSettings.from_env()
    if not settings.avito_webhook_secret:
        print(json.dumps({"ok": False, "error": "AVITO_WEBHOOK_SECRET is empty"}, ensure_ascii=False))
        return 1
    results = run_scenarios(args.base_url.rstrip("/"), settings.avito_webhook_secret, args.timeout)
    payload = {
        "ok": all(result.ok for result in results),
        "total": len(results),
        "passed": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok),
        "live_send_enabled": settings.avito_send_enabled,
        "yclients_mutations_enabled": settings.yclients_allow_mutations,
        "results": [asdict(result) for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload["ok"] else 1


def _post_scenario(base_url: str, token: str, scenario: PreviewScenario, timeout_seconds: int) -> ScenarioResult:
    url = f"{base_url}/avito/webhook?{urllib.parse.urlencode({'token': token})}"
    request = urllib.request.Request(
        url,
        data=json.dumps(scenario.event, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = response.status
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return ScenarioResult(name=scenario.name, ok=False, status=exc.code, error=exc.read().decode("utf-8", errors="replace")[:500])
    except Exception as exc:
        return ScenarioResult(name=scenario.name, ok=False, status=0, error=f"{type(exc).__name__}: {exc}")

    ignored = bool(data.get("ignored"))
    send = data.get("send") if isinstance(data.get("send"), dict) else {}
    action = str(data.get("action") or ("ignored" if ignored else ""))
    handoff = data.get("handoff")
    safety_ok = status == 200 and (ignored or (send.get("sent") is False and send.get("reason") == "preview_only" and data.get("dry_run") is True))
    expectation_ok = True
    if scenario.expected_action:
        expectation_ok = action == scenario.expected_action
    if scenario.expected_handoff:
        expectation_ok = expectation_ok and handoff == scenario.expected_handoff
    ok = safety_ok and expectation_ok
    return ScenarioResult(
        name=scenario.name,
        ok=ok,
        status=status,
        action=action,
        reply=str(data.get("reply") or ""),
        planner=data.get("planner"),
        send_reason=str(send.get("reason") or ""),
        handoff=handoff,
        error="" if ok else json.dumps(data, ensure_ascii=False)[:500],
    )


def _message(
    message_id: str,
    text: str,
    *,
    item: dict[str, Any] | None = None,
    photo: bool = False,
    created: int = 0,
) -> dict[str, Any]:
    content: dict[str, Any] = {"text": text}
    if item:
        content["item"] = item
    if photo:
        content["photo"] = {"id": f"{message_id}-photo"}
    return {
        "payload": {
            "type": "message_created",
            "value": {
                "id": message_id,
                "chat_id": f"{message_id}-chat",
                "user_id": 123,
                "created": created,
                "content": content,
            },
        }
    }


if __name__ == "__main__":
    raise SystemExit(main())
