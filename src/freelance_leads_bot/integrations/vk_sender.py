from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import IntegrationSettings
from .vk import VKLongPollServer


class VKApiError(RuntimeError):
    pass


class VKSender(Protocol):
    async def send_message(self, peer_id: int, text: str) -> dict[str, Any]:
        ...


@dataclass
class PreviewVKSender:
    outbox_path: Path = Path("data/vk_outbox.jsonl")

    async def send_message(self, peer_id: int, text: str) -> dict[str, Any]:
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": int(time.time()),
            "peer_id": peer_id,
            "text": text,
            "sent": False,
            "reason": "preview_only",
        }
        with self.outbox_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"sent": False, "reason": "preview_only", "outbox_path": str(self.outbox_path)}


class VKClient:
    API_BASE = "https://api.vk.com/method"

    def __init__(self, settings: IntegrationSettings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or IntegrationSettings.from_env()
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        if not self.settings.vk_group_token:
            raise VKApiError("VK_GROUP_TOKEN is empty")
        response = await self.client.post(
            f"{self.API_BASE}/{method}",
            data={
                **params,
                "access_token": self.settings.vk_group_token,
                "v": self.settings.vk_api_version,
            },
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            error = data["error"] or {}
            raise VKApiError(f"VK {method}: {error.get('error_code')} {error.get('error_msg')}")
        return data.get("response")

    async def resolve_group_id(self) -> int:
        if self.settings.vk_group_id:
            return self.settings.vk_group_id
        response = await self.call("groups.getById")
        if isinstance(response, list) and response:
            return abs(int(response[0]["id"]))
        if isinstance(response, dict) and response.get("groups"):
            return abs(int(response["groups"][0]["id"]))
        raise VKApiError("VK_GROUP_ID is empty and groups.getById did not return group id")

    async def get_long_poll_server(self) -> VKLongPollServer:
        response = await self.call("groups.getLongPollServer", group_id=await self.resolve_group_id())
        return VKLongPollServer(server=str(response["server"]), key=str(response["key"]), ts=str(response["ts"]))

    async def poll(self, server: VKLongPollServer, *, wait: int = 25) -> dict[str, Any]:
        response = await self.client.get(
            server.server,
            params={"act": "a_check", "key": server.key, "ts": server.ts, "wait": wait},
            timeout=wait + 10,
        )
        response.raise_for_status()
        return response.json()

    async def send_message(self, peer_id: int, text: str) -> dict[str, Any]:
        response = await self.call(
            "messages.send",
            peer_id=peer_id,
            message=text,
            random_id=random.randint(1, 2_147_483_647),
        )
        return {"sent": True, "response": response}


def vk_sender_from_settings(settings: IntegrationSettings) -> VKSender:
    if settings.vk_send_enabled:
        return VKClient(settings)
    return PreviewVKSender()
