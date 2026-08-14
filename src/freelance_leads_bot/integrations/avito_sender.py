from __future__ import annotations

import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import IntegrationSettings


class AvitoSender(Protocol):
    async def send_message(self, account_id: int, chat_id: str, text: str) -> dict[str, Any]:
        ...

    async def send_image(self, account_id: int, chat_id: str, image_path: str | Path) -> dict[str, Any]:
        ...

    async def send_file(self, account_id: int, chat_id: str, file_path: str | Path, caption: str = "") -> dict[str, Any]:
        ...


@dataclass
class PreviewAvitoSender:
    outbox_path: Path = Path("data/avito_outbox.jsonl")

    async def send_message(self, account_id: int, chat_id: str, text: str) -> dict[str, Any]:
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": int(time.time()),
            "account_id": account_id,
            "chat_id": chat_id,
            "text": text,
            "sent": False,
            "reason": "preview_only",
        }
        with self.outbox_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"sent": False, "reason": "preview_only", "outbox_path": str(self.outbox_path)}

    async def send_image(self, account_id: int, chat_id: str, image_path: str | Path) -> dict[str, Any]:
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": int(time.time()),
            "account_id": account_id,
            "chat_id": chat_id,
            "image_path": str(image_path),
            "sent": False,
            "reason": "preview_only",
            "type": "image",
        }
        with self.outbox_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"sent": False, "reason": "preview_only", "outbox_path": str(self.outbox_path)}

    async def send_file(self, account_id: int, chat_id: str, file_path: str | Path, caption: str = "") -> dict[str, Any]:
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        path = Path(file_path)
        event = {
            "ts": int(time.time()),
            "account_id": account_id,
            "chat_id": chat_id,
            "file_path": str(path),
            "caption": caption,
            "sent": False,
            "reason": "preview_only",
            "type": "file",
            "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
        with self.outbox_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"sent": False, "reason": "preview_only", "outbox_path": str(self.outbox_path)}


class AvitoSdkSender:
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
        return AvitoAsyncClient(
            client_id=self.settings.avito_client_id,
            client_secret=self.settings.avito_client_secret,
        )

    async def send_message(self, account_id: int, chat_id: str, text: str) -> dict[str, Any]:
        if not account_id or not chat_id:
            return {"sent": False, "reason": "missing_account_or_chat_id"}
        async with self.client as client:
            headers = await client.auth.auth_header()
            response = await client._transport.request(
                method="POST",
                path_template="/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages",
                path_params={"user_id": account_id, "chat_id": chat_id},
                json_body={"message": {"text": text}, "type": "text"},
                headers=headers,
            )
        return {"sent": True, "response": response}

    async def send_image(self, account_id: int, chat_id: str, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        if not account_id or not chat_id:
            return {"sent": False, "reason": "missing_account_or_chat_id"}
        if not path.exists() or not path.is_file():
            return {"sent": False, "reason": "image_file_not_found", "image_path": str(path)}
        image_bytes = path.read_bytes()
        if not image_bytes:
            return {"sent": False, "reason": "empty_image", "image_path": str(path)}
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        async with self.client as client:
            headers = await client.auth.auth_header()
            transport = client._transport
            if transport._client is None:
                return {"sent": False, "reason": "transport_not_initialized"}
            upload_url = transport._build_url(
                "/messenger/v1/accounts/{user_id}/uploadImages",
                {"user_id": account_id},
            )
            upload_response = await transport._client.post(
                upload_url,
                headers=headers,
                files={"uploadfile[]": (path.name, image_bytes, mime_type)},
            )
            if upload_response.status_code >= 400:
                return {
                    "sent": False,
                    "reason": "upload_failed",
                    "status_code": upload_response.status_code,
                    "response": _safe_json(upload_response),
                }
            upload_payload = _safe_json(upload_response)
            image_id = _image_id_from_upload(upload_payload)
            if not image_id:
                return {"sent": False, "reason": "missing_image_id", "response": upload_payload}
            response = await transport.request(
                method="POST",
                path_template="/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages/image",
                path_params={"user_id": account_id, "chat_id": chat_id},
                json_body={"image_id": image_id},
                headers=headers,
            )
        return {"sent": True, "image_id": image_id, "response": response}

    async def send_file(self, account_id: int, chat_id: str, file_path: str | Path, caption: str = "") -> dict[str, Any]:
        path = Path(file_path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if mime_type.startswith("image/"):
            result = await self.send_image(account_id, chat_id, path)
            if caption.strip() and result.get("sent"):
                caption_result = await self.send_message(account_id, chat_id, caption)
                if not caption_result.get("sent"):
                    return {**result, "sent": False, "reason": "caption_send_failed", "caption_result": caption_result}
                return {**result, "caption_result": caption_result}
            return result
        return {
            "sent": False,
            "reason": "unsupported_avito_file_type",
            "file_path": str(path),
            "media_type": mime_type,
            "caption": caption,
        }


def avito_sender_from_settings(settings: IntegrationSettings) -> AvitoSender:
    if settings.avito_send_enabled:
        return AvitoSdkSender(settings)
    return PreviewAvitoSender()


def avito_image_sender_from_settings(settings: IntegrationSettings) -> AvitoSender:
    if settings.avito_image_send_enabled:
        return AvitoSdkSender(settings)
    return PreviewAvitoSender()


def _image_id_from_upload(payload: Any) -> str:
    if not isinstance(payload, dict) or not payload:
        return ""
    if isinstance(payload.get("image_id"), str):
        return payload["image_id"]
    if isinstance(payload.get("id"), str):
        return payload["id"]
    return str(next(iter(payload.keys()), ""))


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return {"text": getattr(response, "text", "")[:500]}
