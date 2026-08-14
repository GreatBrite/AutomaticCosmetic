from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .config import IntegrationSettings


class AvitoReadGateway(Protocol):
    async def list_chats(self, account_id: int, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        ...

    async def get_chat_messages(self, account_id: int, chat_id: str, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        ...

    async def mark_chat_read(self, account_id: int, chat_id: str) -> dict[str, Any]:
        ...


class AvitoReadClient:
    def __init__(self, settings: IntegrationSettings | None = None, client: Any | None = None) -> None:
        self.settings = settings or IntegrationSettings.from_env()
        self.client = client or self._build_client()

    def _build_client(self) -> Any:
        if not self.settings.avito_client_id or not self.settings.avito_client_secret:
            raise RuntimeError("AVITO_CLIENT_ID/AVITO_CLIENT_SECRET are required")
        try:
            from pyavitoapi.client import AvitoAsyncClient
        except ImportError as exc:
            raise RuntimeError("pyavitoapi is not installed") from exc
        return AvitoAsyncClient(client_id=self.settings.avito_client_id, client_secret=self.settings.avito_client_secret)

    async def list_chats(self, account_id: int, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        async with self.client as client:
            headers = await client.auth.auth_header()
            return await client._transport.request(
                method="GET",
                path_template="/messenger/v2/accounts/{user_id}/chats",
                path_params={"user_id": account_id},
                query={"limit": limit, "offset": offset},
                headers=headers,
            )

    async def get_chat_messages(self, account_id: int, chat_id: str, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        async with self.client as client:
            headers = await client.auth.auth_header()
            return await client._transport.request(
                method="GET",
                path_template="/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
                path_params={"user_id": account_id, "chat_id": chat_id},
                query={"limit": limit, "offset": offset},
                headers=headers,
            )

    async def mark_chat_read(self, account_id: int, chat_id: str) -> dict[str, Any]:
        async with self.client as client:
            headers = await client.auth.auth_header()
            payload = await client._transport.request(
                method="POST",
                path_template="/messenger/v1/accounts/{user_id}/chats/{chat_id}/read",
                path_params={"user_id": account_id, "chat_id": chat_id},
                headers=headers,
            )
            return payload if isinstance(payload, dict) else {"ok": True}


@dataclass(frozen=True)
class NullAvitoReadClient:
    reason: str = "avito_read_not_configured"

    async def list_chats(self, account_id: int, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return {"ok": False, "reason": self.reason, "chats": []}

    async def get_chat_messages(self, account_id: int, chat_id: str, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        return {"ok": False, "reason": self.reason, "messages": []}

    async def mark_chat_read(self, account_id: int, chat_id: str) -> dict[str, Any]:
        return {"ok": False, "reason": self.reason, "account_id": account_id, "chat_id": chat_id}


def avito_read_client_from_settings(settings: IntegrationSettings) -> AvitoReadGateway:
    if not settings.avito_ready:
        return NullAvitoReadClient("avito_credentials_missing")
    try:
        return AvitoReadClient(settings)
    except RuntimeError as exc:
        return NullAvitoReadClient(str(exc))
