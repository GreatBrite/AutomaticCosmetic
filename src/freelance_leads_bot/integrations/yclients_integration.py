from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .config import IntegrationSettings


@dataclass(frozen=True)
class YClientsIntegrationEvent:
    event_type: str
    payload: dict[str, Any]
    headers: dict[str, str]
    query: dict[str, str]
    received_at: str


class YClientsIntegrationEventRepository:
    def __init__(self, path: Path | str = Path("data/yclients_integration/events.jsonl")) -> None:
        self.path = Path(path)

    async def append(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> YClientsIntegrationEvent:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = YClientsIntegrationEvent(
            event_type=event_type,
            payload=payload,
            headers=headers or {},
            query=query or {},
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) + "\n")
        return event


@dataclass(frozen=True)
class YClientsIntegrationService:
    settings: IntegrationSettings
    repository: YClientsIntegrationEventRepository

    @property
    def integration_urls(self) -> dict[str, str]:
        base_url = self.settings.public_base_url.rstrip("/")
        secret = self.settings.yclients_integration_secret.strip()
        suffix = f"?secret={secret}" if secret else ""
        return {
            "webhook_url": f"{base_url}/yclients/webhook{suffix}",
            "callback_url": f"{base_url}/yclients/callback{suffix}",
            "registration_redirect_url": f"{base_url}/yclients/register",
        }

    @property
    def redacted_integration_urls(self) -> dict[str, str]:
        return {key: _redact_url(value) for key, value in self.integration_urls.items()}

    def is_valid_secret(self, secret: str | None) -> bool:
        expected = self.settings.yclients_integration_secret.strip()
        return not expected or secret == expected

    async def handle_webhook(self, payload: dict[str, Any], headers: dict[str, str], query: dict[str, str]) -> YClientsIntegrationEvent:
        return await self.repository.append(event_type="webhook", payload=payload, headers=headers, query=query)

    async def handle_disconnect_callback(self, payload: dict[str, Any], headers: dict[str, str], query: dict[str, str]) -> YClientsIntegrationEvent:
        return await self.repository.append(event_type="disconnect_callback", payload=payload, headers=headers, query=query)

    async def handle_registration_redirect(self, payload: dict[str, Any], headers: dict[str, str], query: dict[str, str]) -> YClientsIntegrationEvent:
        return await self.repository.append(event_type="registration_redirect", payload=payload, headers=headers, query=query)


app = FastAPI(title="Automatic Cosmetic YCLIENTS Integration")


def get_settings() -> IntegrationSettings:
    return IntegrationSettings.from_env()


def get_repository() -> YClientsIntegrationEventRepository:
    return YClientsIntegrationEventRepository()


def get_integration_service(
    settings: IntegrationSettings = Depends(get_settings),
    repository: YClientsIntegrationEventRepository = Depends(get_repository),
) -> YClientsIntegrationService:
    return YClientsIntegrationService(settings=settings, repository=repository)


@app.get("/health")
async def health(service: YClientsIntegrationService = Depends(get_integration_service)) -> dict[str, Any]:
    return {
        "ok": True,
        "integration_urls": service.redacted_integration_urls,
        "secret_required": bool(service.settings.yclients_integration_secret.strip()),
    }


@app.get("/yclients/webhook")
async def yclients_webhook_probe() -> dict[str, bool]:
    return {"ok": True}


@app.head("/yclients/webhook")
async def yclients_webhook_probe_head() -> Response:
    return Response(status_code=200)


@app.post("/yclients/webhook")
async def yclients_webhook(
    request: Request,
    service: YClientsIntegrationService = Depends(get_integration_service),
) -> dict[str, Any]:
    _check_secret(service, request)
    event = await service.handle_webhook(
        payload=await _request_payload(request),
        headers=_headers(request),
        query=_query(request),
    )
    return {"ok": True, "received_at": event.received_at}


@app.get("/yclients/callback")
async def yclients_callback_probe() -> dict[str, bool]:
    return {"ok": True}


@app.head("/yclients/callback")
async def yclients_callback_probe_head() -> Response:
    return Response(status_code=200)


@app.post("/yclients/callback")
async def yclients_callback(
    request: Request,
    service: YClientsIntegrationService = Depends(get_integration_service),
) -> dict[str, Any]:
    _check_secret(service, request)
    event = await service.handle_disconnect_callback(
        payload=await _request_payload(request),
        headers=_headers(request),
        query=_query(request),
    )
    return {"ok": True, "received_at": event.received_at}


@app.get("/yclients/register", response_class=HTMLResponse)
async def yclients_register_get(
    request: Request,
    service: YClientsIntegrationService = Depends(get_integration_service),
) -> HTMLResponse:
    await service.handle_registration_redirect(payload={}, headers=_headers(request), query=_query(request))
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="ru">
          <head><meta charset="utf-8"><title>YCLIENTS Integration</title></head>
          <body>
            <h1>Интеграция YCLIENTS подключена</h1>
            <p>Можно закрыть это окно и вернуться в YCLIENTS.</p>
          </body>
        </html>
        """
    )


@app.post("/yclients/register")
async def yclients_register_post(
    request: Request,
    service: YClientsIntegrationService = Depends(get_integration_service),
) -> dict[str, Any]:
    event = await service.handle_registration_redirect(
        payload=await _request_payload(request),
        headers=_headers(request),
        query=_query(request),
    )
    return {"ok": True, "received_at": event.received_at}


async def _request_payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {"payload": payload}

    body = await request.body()
    text = body.decode("utf-8", errors="replace")
    if "application/x-www-form-urlencoded" in content_type:
        return dict(parse_qsl(text, keep_blank_values=True))
    return {"raw_body": text} if text else {}


def _headers(request: Request) -> dict[str, str]:
    return {key: value for key, value in request.headers.items()}


def _query(request: Request) -> dict[str, str]:
    return {key: value for key, value in request.query_params.items()}


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in {"secret", "token", "key", "access_token", "client_secret"}:
            query.append((key, "***"))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _check_secret(service: YClientsIntegrationService, request: Request) -> None:
    if not service.is_valid_secret(request.query_params.get("secret")):
        raise HTTPException(status_code=403, detail="forbidden")
