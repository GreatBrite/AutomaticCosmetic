from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from ..media_recognition import transcribe_audio_bytes
from .config import IntegrationSettings
from .models import InboundMessage


class AvitoVoiceResolver(Protocol):
    async def transcribe(self, message: InboundMessage) -> InboundMessage:
        ...


def avito_voice_id_from_message(message: InboundMessage) -> str:
    raw = message.metadata.get("voice_id")
    if raw:
        return str(raw)
    content = {}
    raw_event = message.metadata.get("raw")
    if isinstance(raw_event, dict):
        value = raw_event.get("payload", {}).get("value") if isinstance(raw_event.get("payload"), dict) else raw_event.get("message")
        if not isinstance(value, dict):
            value = raw_event
        content = value.get("content") if isinstance(value.get("content"), dict) else {}
    voice = content.get("voice") if isinstance(content, dict) else None
    if isinstance(voice, dict):
        return str(voice.get("voice_id") or voice.get("id") or "")
    if isinstance(voice, str):
        return voice
    return ""


class AvitoApiVoiceResolver:
    def __init__(self, settings: IntegrationSettings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or IntegrationSettings.from_env()
        self.http_client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def transcribe(self, message: InboundMessage) -> InboundMessage:
        if message.text.strip():
            return message
        voice_id = avito_voice_id_from_message(message)
        if not voice_id:
            return message
        voice_url = await self._voice_url(voice_id, account_id=_account_id(message, self.settings))
        response = await self.http_client.get(voice_url)
        response.raise_for_status()
        content_type = response.headers.get("content-type") or "audio/ogg"
        filename = _filename_from_url(voice_url, voice_id, content_type)
        text = transcribe_audio_bytes(filename, response.content, content_type)
        return replace(
            message,
            text=text,
            metadata={
                **message.metadata,
                "voice_id": voice_id,
                "voice_transcribed": True,
                "voice_content_type": content_type,
            },
        )

    async def _voice_url(self, voice_id: str, *, account_id: int) -> str:
        try:
            from pyavitoapi.client import AvitoAsyncClient
        except ImportError as exc:
            raise RuntimeError("pyavitoapi is not installed") from exc
        async with AvitoAsyncClient(client_id=self.settings.avito_client_id, client_secret=self.settings.avito_client_secret) as client:
            headers = await client.auth.auth_header()
            payload = await client._transport.request(
                method="GET",
                path_template="/messenger/v1/accounts/{user_id}/getVoiceFiles",
                path_params={"user_id": account_id},
                query={"voice_ids": voice_id},
                headers=headers,
            )
        urls = payload.get("voices_urls") if isinstance(payload, dict) else {}
        voice_url = urls.get(voice_id) if isinstance(urls, dict) else ""
        if not voice_url:
            raise RuntimeError(f"Avito did not return voice URL for voice_id={voice_id}")
        return str(voice_url)


def _account_id(message: InboundMessage, settings: IntegrationSettings) -> int:
    try:
        return int(message.metadata.get("account_id") or settings.avito_account_id)
    except (TypeError, ValueError):
        return settings.avito_account_id


def _filename_from_url(url: str, voice_id: str, content_type: str) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] if path else ""
    if "." in name:
        return name
    if "mpeg" in content_type or "mp3" in content_type:
        suffix = ".mp3"
    elif "wav" in content_type:
        suffix = ".wav"
    elif "m4a" in content_type or "mp4" in content_type:
        suffix = ".m4a"
    else:
        suffix = ".ogg"
    return f"avito-voice-{voice_id}{suffix}"
