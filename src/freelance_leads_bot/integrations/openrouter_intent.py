from __future__ import annotations

import json
from typing import Any

import httpx


class OpenRouterIntentClient:
    """Small synchronous OpenRouter adapter for structured intent extraction.

    The caller provides the full extraction prompt. This client only asks the
    model to return a JSON object and returns the raw model text to the parser.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 20.0,
        base_url: str = "https://openrouter.ai/api/v1",
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.timeout_seconds = max(float(timeout_seconds or 20.0), 1.0)
        self.base_url = base_url.rstrip("/")
        self.client = client

    def __call__(self, prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("OpenRouter API key is not configured")
        if not self.model:
            raise RuntimeError("OpenRouter model is not configured")
        owns_client = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout_seconds)
        try:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/GreatBrite/AutomaticCosmetic",
                    "X-Title": "AutomaticCosmetic RAG Intent",
                },
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Return only one valid JSON object. "
                                "Do not include markdown, commentary or tool calls."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            return _extract_message_text(response.json())
        finally:
            if owns_client:
                client.close()


def _extract_message_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenRouter returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise RuntimeError("OpenRouter returned no message")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return json.dumps(message, ensure_ascii=False)
