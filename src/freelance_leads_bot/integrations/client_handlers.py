from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client_router import ClientRoute
from .models import InboundMessage


@dataclass(frozen=True)
class RagAnswerDraft:
    answer: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class RagAnswerService:
    def __init__(self, *, autoanswer_threshold: float) -> None:
        self.autoanswer_threshold = float(autoanswer_threshold)

    def from_retrieved(self, answers: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> RagAnswerDraft | None:
        if not answers:
            return None
        best = answers[0]
        score = float(best.get("score") or 0)
        answer = str(best.get("answer_client") or "").strip()
        risk_level = str(best.get("risk_level") or "").strip().lower()
        if risk_level == "high":
            return None
        if not answer or score < self.autoanswer_threshold:
            return None
        if best.get("_retrieval_safe_for_autoanswer") is False:
            return None
        metadata = {
            "expert_answer_id": best.get("id"),
            "score": score,
            "risk_level": best.get("risk_level"),
        }
        return RagAnswerDraft(answer=answer, score=score, metadata=metadata)


class HandoffComposer:
    def compose(
        self,
        *,
        message: InboundMessage,
        route: ClientRoute,
        retrieved_answers: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        client_replied: bool = False,
    ) -> str:
        lines = [
            "Нужно: проверить клиентский вопрос и дать безопасный ответ.",
            f"Канал: {message.channel.value if hasattr(message.channel, 'value') else message.channel}",
            f"Причина: {route.handoff_reason or route.block_autoanswer_reason or route.route}",
        ]
        if message.text:
            lines.append(f"Сообщение клиента: {message.text}")
        if route.service_key:
            lines.append(f"Услуга: {route.service_key}")
        if route.city:
            lines.append(f"Город: {route.city}")
        if retrieved_answers:
            similar = str(retrieved_answers[0].get("answer_client") or "").strip()
            if similar:
                lines.append(f"Похожее подтверждённое знание: {similar[:500]}")
        lines.append("Клиенту уже ответили." if client_replied else "Клиенту пока ничего не писали.")
        return "\n".join(lines)
