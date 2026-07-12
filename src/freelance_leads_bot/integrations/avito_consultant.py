from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import count
from typing import Any, Awaitable, Callable, Protocol

from .agent_tools import AutomationToolbox
from .agent_trace import JsonlAgentTraceLogger
from .avito import avito_photo_handoff
from .booking_flow import AvitoBookingFlow, booking_request_from_message, extract_date, extract_time
from .client_handlers import HandoffComposer, RagAnswerService
from .client_router import ClientRoute, route_client_message
from .config import DEFAULT_CITIES
from .expert_rag import ExpertRagStore
from .rag_retrieval import RagRetrievalService
from .models import Handoff, HandoffReason, InboundMessage, Service, Slot
from .roles import CodexRole, RoleProfile, conversation_key, role_profile


PRICE_WORDS = ("цен", "стоим", "прайс", "сколько", "расчет", "рассчет", "скидк")
BOOKING_WORDS = ("запис", "свобод", "окош", "время", "слот")
ADDRESS_WORDS = ("адрес", "метро", "территориально", "где", "локац", "как пройти", "вход")
MEDICAL_WORDS = ("противопоказ", "беремен", "кормлен", "аллерг", "анестет", "препарат", "эффект")
RISK_WORDS = (
    "аллергия",
    "отек",
    "отёк",
    "задыха",
    "трудно дыш",
    "больно",
    "сильная боль",
    "ожог",
    "гной",
    "температура",
    "жалоба",
    "плохо после",
)


@dataclass(frozen=True)
class AvitoConsultantReply:
    action: str
    reply: str
    handoff: Handoff | None = None
    slots: list[Slot] = field(default_factory=list)
    appointment_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AvitoAgentContext:
    message: InboundMessage
    available_tools: tuple[str, ...]
    tool_schemas: tuple[dict[str, Any], ...] = ()
    knowledge_items: tuple[dict[str, Any], ...] = ()
    retrieved_expert_answers: tuple[dict[str, Any], ...] = ()
    conversation_history: tuple[dict[str, Any], ...] = ()
    role_profile: RoleProfile = field(default_factory=lambda: role_profile(CodexRole.AVITO_CLIENT))
    conversation_key: str = ""

    def to_codex_payload(self) -> dict[str, Any]:
        return {
            "role": self.role_profile.prompt_role,
            "role_name": self.role_profile.role.value,
            "conversation_key": self.conversation_key,
            "goal": self.role_profile.goal,
            "current_date": _message_date(self.message),
            "message": _message_payload(self.message),
            "conversation_history": list(self.conversation_history),
            "available_tools": list(self.available_tools),
            "tool_schemas": list(self.tool_schemas),
            "knowledge_items": list(self.knowledge_items),
            "retrieved_expert_answers": list(self.retrieved_expert_answers),
            "reply_rules": _codex_payload_reply_rules(self.role_profile),
        }


class AvitoAgentPlanner(Protocol):
    async def respond(self, context: AvitoAgentContext, toolbox: AutomationToolbox) -> AvitoConsultantReply | None:
        ...


class CodexAvitoPlanner:
    """Bridge for a Codex-powered planner; Codex decides which tools to call."""

    def __init__(
        self,
        runner: Callable[[dict[str, Any], AutomationToolbox], Awaitable[dict[str, Any] | None]],
    ) -> None:
        self.runner = runner

    async def respond(self, context: AvitoAgentContext, toolbox: AutomationToolbox) -> AvitoConsultantReply | None:
        planned = await self.runner(context.to_codex_payload(), toolbox)
        if not planned:
            return None
        return _reply_from_codex_step(
            context,
            planned,
            planner="codex",
            metadata={"planner": "codex", "raw": planned},
        )


class CodexToolLoopPlanner:
    """Codex planner loop: Codex proposes tool calls, toolbox executes, Codex writes final reply."""

    def __init__(
        self,
        runner: Callable[[dict[str, Any], list[dict[str, Any]]], Awaitable[dict[str, Any] | None]],
        *,
        max_steps: int = 0,
        trace_logger: JsonlAgentTraceLogger | None = None,
    ) -> None:
        self.runner = runner
        self.max_steps = max_steps
        self.trace_logger = trace_logger

    async def respond(self, context: AvitoAgentContext, toolbox: AutomationToolbox) -> AvitoConsultantReply | None:
        payload = context.to_codex_payload()
        trace: list[dict[str, Any]] = []
        step_indexes = count() if self.max_steps <= 0 else range(self.max_steps)
        for _ in step_indexes:
            step = await self.runner(payload, trace)
            if not step:
                self._write_trace(payload, trace, {"action": "empty_step"})
                return None
            tool_calls = _tool_calls_from_step(step)
            if tool_calls:
                for call in tool_calls:
                    name = str(call.get("name") or "")
                    arguments = dict(call.get("arguments") or {})
                    trace.append({"type": "tool_call", "tool": name, "arguments": arguments})
                    if name not in context.available_tools:
                        trace.append(
                            {
                                "type": "tool_result",
                                "tool": name,
                                "arguments": arguments,
                                "ok": False,
                                "data": {},
                                "error": f"tool {name} is not allowed for role {context.role_profile.role.value}",
                            }
                        )
                        continue
                    result = await toolbox.execute(name, arguments)
                    trace.append(
                        {
                            "type": "tool_result",
                            "tool": name,
                            "arguments": arguments,
                            "ok": result.ok,
                            "data": result.data,
                            "error": result.error,
                        }
                    )
                continue
            reply = str(step.get("reply") or "").strip()
            handoff_reason = str(step.get("handoff_reason") or "").strip()
            if reply or handoff_reason:
                metadata = {"planner": "codex_tool_loop", "trace": trace, "raw": step, "conversation_key": context.conversation_key}
                metadata.update(self._write_trace(payload, trace, step))
                return _reply_from_codex_step(
                    context,
                    step,
                    planner="codex_tool_loop",
                    metadata=metadata,
                )
            self._write_trace(payload, trace, {"action": "empty_reply", "raw": step})
            return None
        outcome = {"action": "codex_tool_loop_limit", "max_steps": self.max_steps}
        metadata = {"planner": "codex_tool_loop", "trace": trace, "max_steps": self.max_steps, "conversation_key": context.conversation_key}
        metadata.update(self._write_trace(payload, trace, outcome))
        return AvitoConsultantReply(
            action="codex_tool_loop_limit",
            reply="Сейчас уточню детали и вернусь с ответом.",
            metadata=metadata,
        )

    def _write_trace(self, payload: dict[str, Any], trace: list[dict[str, Any]], outcome: dict[str, Any]) -> dict[str, Any]:
        if not self.trace_logger:
            return {}
        try:
            return self.trace_logger.write(
                planner="codex_tool_loop",
                payload=payload,
                trace=trace,
                outcome=outcome,
            )
        except OSError as exc:
            return {"trace_log_error": type(exc).__name__}


class AvitoConsultant:
    """Tool-first Avito consultant with Codex planner support and deterministic fallback."""

    def __init__(
        self,
        toolbox: AutomationToolbox,
        cities: tuple[str, ...] = DEFAULT_CITIES,
        planner: AvitoAgentPlanner | None = None,
        profile: RoleProfile | None = None,
        expert_rag: ExpertRagStore | None = None,
        rag_retrieval: RagRetrievalService | None = None,
        rag_autoanswer_threshold: float = 0.82,
        rag_handoff_threshold: float = 0.65,
    ) -> None:
        self.toolbox = toolbox
        self.cities = cities
        self.planner = planner
        self.profile = profile or role_profile(CodexRole.AVITO_CLIENT)
        self.expert_rag = expert_rag
        self.rag_retrieval = rag_retrieval
        self.rag_autoanswer_threshold = rag_autoanswer_threshold
        self.rag_handoff_threshold = rag_handoff_threshold
        self.rag_answer_service = RagAnswerService(autoanswer_threshold=rag_autoanswer_threshold)
        self.handoff_composer = HandoffComposer()
        self.booking_flow = AvitoBookingFlow(toolbox.booking, cities=cities, allow_create=False)

    async def respond(
        self,
        message: InboundMessage,
        *,
        conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    ) -> AvitoConsultantReply:
        context = await self.build_context(message, conversation_history=conversation_history)
        routed = await self._router_reply(context)
        if routed:
            return routed
        preflight = self._preflight_reply(context)
        if preflight:
            return preflight
        if self.planner:
            planned = await self.planner.respond(context, self.toolbox)
            if planned:
                return planned
        return await self._fallback_response(context)

    async def _router_reply(self, context: AvitoAgentContext) -> AvitoConsultantReply | None:
        if context.role_profile.role not in {CodexRole.AVITO_CLIENT, CodexRole.TELEGRAM_CLIENT, CodexRole.VK_CLIENT}:
            return None
        route = route_client_message(
            context.message,
            retrieved_expert_answers=context.retrieved_expert_answers,
            conversation_history=context.conversation_history,
            autoanswer_threshold=self.rag_autoanswer_threshold,
        )
        if route.route == "rag_answer":
            draft = self.rag_answer_service.from_retrieved(context.retrieved_expert_answers)
            if not draft:
                return None
            return AvitoConsultantReply(
                action="expert_rag_answer",
                reply=_with_next_step(draft.answer, context),
                metadata={"planner": "client_router", "route": route.to_dict(), **draft.metadata},
            )
        if route.route == "ask_service":
            return AvitoConsultantReply(
                action="ask_procedure_for_booking",
                reply="Подскажите, пожалуйста, какая процедура интересует? Тогда уже посмотрю по ней ближайшие варианты.",
                metadata={"planner": "client_router", "route": route.to_dict()},
            )
        if route.route == "ask_city":
            return AvitoConsultantReply(
                action="ask_city",
                reply="Подскажите, пожалуйста, в каком городе вам удобно? После этого сориентирую по адресу или ближайшим вариантам.",
                metadata={"planner": "client_router", "route": route.to_dict()},
            )
        if route.route == "media_handoff":
            return self._route_handoff_reply(
                context,
                route,
                reply="Спасибо, фото передадим на оценку и вернёмся с ответом.",
                reason=HandoffReason.PHOTO_CONSULTATION,
            )
        if route.route == "risk_handoff":
            return self._route_handoff_reply(
                context,
                route,
                reply=(
                    "Если есть сильный отёк, затруднённое дыхание, резкое ухудшение или симптомы быстро усиливаются, "
                    "пожалуйста, срочно обратитесь за медицинской помощью по 112 или 103. "
                    "Напишите, какая процедура и когда была, что именно беспокоит, когда началось, есть ли боль или температура, "
                    "и приложите фото при хорошем освещении."
                ),
                reason=HandoffReason.COMPLAINT_OR_RISK,
            )
        if route.route in {"booking_read", "address"}:
            return await self._fallback_response(context)
        return None

    def _route_handoff_reply(
        self,
        context: AvitoAgentContext,
        route: ClientRoute,
        *,
        reply: str,
        reason: HandoffReason,
    ) -> AvitoConsultantReply:
        handoff = Handoff(
            reason=reason,
            message=context.message,
            summary=self.handoff_composer.compose(
                message=context.message,
                route=route,
                retrieved_answers=context.retrieved_expert_answers,
                client_replied=bool(reply),
            ),
        )
        return AvitoConsultantReply(
            action="handoff",
            reply=reply,
            handoff=handoff,
            metadata={"planner": "client_router", "route": route.to_dict()},
        )

    async def build_context(
        self,
        message: InboundMessage,
        *,
        conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    ) -> AvitoAgentContext:
        knowledge_items = await self._knowledge_items(message)
        retrieved_expert_answers = self._retrieved_expert_answers(message)
        return AvitoAgentContext(
            message=message,
            available_tools=tuple(name for name in self.toolbox.tool_names() if self.profile.allows_tool(name)),
            tool_schemas=tuple(schema for schema in self.toolbox.tool_schemas() if self.profile.allows_tool(str(schema.get("name") or ""))),
            knowledge_items=tuple(knowledge_items),
            retrieved_expert_answers=tuple(retrieved_expert_answers),
            conversation_history=tuple(conversation_history),
            role_profile=self.profile,
            conversation_key=_conversation_key_for_message(message, self.profile),
        )

    def _preflight_reply(self, context: AvitoAgentContext) -> AvitoConsultantReply | None:
        if context.role_profile.role != CodexRole.AVITO_CLIENT:
            return None
        message = context.message
        if _looks_like_strong_ambiguous_booking_without_service(message, context.conversation_history):
            return AvitoConsultantReply(
                action="ask_procedure_for_booking",
                reply="Подскажите, пожалуйста, какая процедура интересует? Тогда уже посмотрю по ней ближайшие варианты.",
                metadata={"planner": "preflight", "reason": "booking_without_service"},
            )
        return None

    async def _fallback_response(self, context: AvitoAgentContext) -> AvitoConsultantReply:
        message = context.message
        handoff = avito_photo_handoff(message)
        if handoff:
            return AvitoConsultantReply(
                action="handoff",
                reply="Спасибо, фото передадим на оценку и вернёмся с ответом.",
                handoff=handoff,
                metadata={"planner": "fallback", "reason": "photo"},
            )
        risk_handoff = risk_or_complaint_handoff(message)
        if risk_handoff:
            expert_reply = self._answer_from_expert_rag(context)
            if expert_reply:
                return expert_reply
            return AvitoConsultantReply(
                action="handoff",
                reply=(
                    "Если есть сильный отёк, затруднённое дыхание, резкое ухудшение или симптомы быстро усиливаются, "
                    "пожалуйста, срочно обратитесь за медицинской помощью по 112 или 103. "
                    "Для онлайн-консультации с Ольгой напишите, какая процедура и когда была, что именно беспокоит, "
                    "когда началось, есть ли боль или температура, и приложите фото при хорошем освещении."
                ),
                handoff=risk_handoff,
                metadata={"planner": "fallback", "reason": "complaint_or_risk"},
            )
        if _booking_tools_may_help(message):
            decision = await self.booking_flow.process(booking_request_from_message(message, self.cities))
            return AvitoConsultantReply(
                action=decision.action,
                reply=decision.reply,
                handoff=decision.handoff,
                slots=decision.slots,
                appointment_id=decision.appointment_id,
                metadata={"planner": "fallback", "tool": "booking_flow"},
            )

        if _address_tools_may_help(message):
            return await self._answer_address(message)

        expert_reply = self._answer_from_expert_rag(context)
        if expert_reply:
            return expert_reply

        knowledge_reply = self._answer_from_knowledge(context)
        if knowledge_reply:
            return knowledge_reply

        if _service_tools_may_help(message):
            return await self._answer_price(message)

        if _looks_like_booking_without_service(message, context.conversation_history):
            return AvitoConsultantReply(
                action="ask_procedure_for_booking",
                reply="Подскажите, пожалуйста, какая процедура интересует? Тогда уже посмотрю по ней ближайшие варианты.",
                metadata={"planner": "fallback", "reason": "booking_without_service"},
            )

        if message.listing and message.listing.has_listing:
            return AvitoConsultantReply(
                action="listing_context_answer",
                reply=(
                    f"Да, услуга «{message.listing.title}» актуальна. "
                    "Могу подсказать стоимость, подготовку, противопоказания и свободное время. В каком городе вам удобно?"
                ),
                metadata={"listing": message.listing.to_prompt_context()},
            )

        return AvitoConsultantReply(
            action="clarify",
            reply="Подскажите, какая процедура интересует и в каком городе удобно? Сразу посмотрю цену и свободное время.",
        )

    async def _knowledge_items(self, message: InboundMessage) -> list[dict[str, Any]]:
        queries = _knowledge_queries(message)
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for query in queries:
            result = await self.toolbox.execute("knowledge.list", {"query": query})
            if not result.ok:
                continue
            for item in result.data.get("items") or []:
                item_id = str(item.get("id") or "")
                if item_id in seen:
                    continue
                if _unsafe_knowledge_item(item):
                    continue
                seen.add(item_id)
                items.append({**item, "_query": query})
        return items

    def _retrieved_expert_answers(self, message: InboundMessage) -> list[dict[str, Any]]:
        if self.rag_retrieval:
            result = self.rag_retrieval.retrieve_for_message(
                message,
                min_score=max(0.0, self.rag_handoff_threshold * 0.75),
                limit=5,
            )
            return [
                {
                    **answer,
                    "_retrieval_safe_for_autoanswer": result.safe_for_autoanswer,
                    "_retrieval_handoff_reason": result.handoff_reason,
                    "_retrieval_conflicts": list(result.conflicts),
                }
                for answer in result.answers
            ]
        if not self.expert_rag:
            return []
        query = " ".join(part for part in (message.text, message.listing.title if message.listing else "") if part)
        matches = self.expert_rag.search(
            query,
            limit=5,
            min_score=max(0.0, self.rag_handoff_threshold * 0.75),
            exclude_risk_levels=("high",),
        )
        return [answer.to_dict(score=score) for answer, score in matches if _expert_answer_autoanswer_allowed(answer.to_dict())]

    def _answer_from_expert_rag(self, context: AvitoAgentContext) -> AvitoConsultantReply | None:
        if not context.retrieved_expert_answers:
            return None
        best = context.retrieved_expert_answers[0]
        score = float(best.get("score") or 0)
        answer = str(best.get("answer_client") or "").strip()
        risk_level = str(best.get("risk_level") or "").strip().lower()
        if risk_level == "high":
            return None
        if not _expert_answer_autoanswer_allowed(best):
            return None
        if not answer or score < self.rag_autoanswer_threshold:
            return None
        if best.get("_retrieval_safe_for_autoanswer") is False:
            return None
        return AvitoConsultantReply(
            action="expert_rag_answer",
            reply=_with_next_step(answer, context),
            metadata={
                "planner": "expert_rag",
                "expert_answer_id": best.get("id"),
                "score": score,
                "risk_level": best.get("risk_level"),
            },
        )

    def _answer_from_knowledge(self, context: AvitoAgentContext) -> AvitoConsultantReply | None:
        for item in context.knowledge_items:
            content = str(item.get("content") or "").strip()
            if content:
                return AvitoConsultantReply(
                    action="knowledge_answer",
                    reply=_with_next_step(content, context),
                    metadata={"knowledge_id": str(item.get("id") or ""), "query": str(item.get("_query") or "")},
                )
        return None

    async def _answer_price(self, message: InboundMessage) -> AvitoConsultantReply:
        if message.listing and message.listing.price_string:
            title = message.listing.title or "этой услуге"
            reply = f"Стоимость «{title}» — {message.listing.price_string}."
            if _asks_amount_or_calculation(message.text):
                reply += " Точный расчет зависит от объема и зоны, его лучше считать после уточнения пожеланий."
            return AvitoConsultantReply(
                action="listing_price_answer",
                reply=_with_next_step(reply, message),
                metadata={"listing": message.listing.to_prompt_context()},
            )

        city = _city_from_message(message, self.cities)
        if not city:
            return AvitoConsultantReply(
                action="ask_city",
                reply="Подскажите, пожалуйста, в каком городе вам удобно?",
            )

        service_result = await self.toolbox.execute("yclients.services.list", {"city": city})
        if not service_result.ok:
            return AvitoConsultantReply(
                action="price_unknown",
                reply="Сейчас не вижу цену в базе. Напишите, какая именно процедура интересует, и я сверю по услугам.",
            )
        services = [_service_from_data(row) for row in service_result.data.get("services") or []]
        matched = _match_service(message.text, services)
        if matched:
            return AvitoConsultantReply(
                action="service_price_answer",
                reply=_with_next_step(_format_service_price(matched), message),
                metadata={"service_id": matched.id},
            )
        preview = ", ".join(_format_service_price(service) for service in services[:5])
        return AvitoConsultantReply(
            action="price_list_preview",
            reply=f"По прайсу вижу: {preview}. Напишите конкретную процедуру, и я подскажу точнее.",
        )

    async def _answer_address(self, message: InboundMessage) -> AvitoConsultantReply:
        city = _city_from_message(message, self.cities)
        if not city:
            return AvitoConsultantReply(
                action="ask_city",
                reply="Подскажите, пожалуйста, в каком городе вам удобнее? Тогда сразу уточню адрес.",
            )
        result = await self.toolbox.execute("yclients.company.address", {"city": city})
        company = result.data.get("company") if result.ok else {}
        address = str((company or {}).get("address") or "").strip()
        company_city = str((company or {}).get("city") or city or "").strip()
        if address:
            city_part = f"{company_city}: " if company_city else ""
            return AvitoConsultantReply(
                action="yclients_address_answer",
                reply=f"Адрес по YCLIENTS: {city_part}{address}.",
                metadata={"tool": "yclients.company.address", "company": company},
            )
        return AvitoConsultantReply(
            action="address_unknown",
            reply="Сейчас уточню точный адрес и напишу вам.",
            handoff=Handoff(
                reason=HandoffReason.MISSING_DATA,
                message=message,
                summary="Клиент спрашивает адрес, но в YCLIENTS адрес для выбранного города не найден.",
            ),
            metadata={"tool": "yclients.company.address", "company": company if isinstance(company, dict) else {}},
        )


def _booking_tools_may_help(message: InboundMessage) -> bool:
    lowered = message.text.casefold()
    if not _has_service_or_procedure_hint(message):
        return False
    if extract_date(message.text) or extract_time(message.text):
        return not _service_tools_may_help(message)
    if AvitoBookingFlow(_NoopBooking()).extract_phone(message.text):
        return not _service_tools_may_help(message)
    return any(word in lowered for word in BOOKING_WORDS) and not _service_tools_may_help(message)


def _looks_like_booking_without_service(message: InboundMessage, conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = ()) -> bool:
    lowered = message.text.casefold()
    has_booking_signal = (
        bool(extract_date(message.text))
        or bool(extract_time(message.text))
        or bool(AvitoBookingFlow(_NoopBooking()).extract_phone(message.text))
        or any(word in lowered for word in (*BOOKING_WORDS, "следующ", "недел", "прием", "приём", "встреч", "личн"))
    )
    return has_booking_signal and not _has_explicit_service_or_procedure_hint(message, conversation_history)


def _looks_like_strong_ambiguous_booking_without_service(
    message: InboundMessage,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> bool:
    lowered = message.text.casefold()
    strong_booking_signal = (
        bool(extract_date(message.text))
        or bool(extract_time(message.text))
        or bool(AvitoBookingFlow(_NoopBooking()).extract_phone(message.text))
        or any(word in lowered for word in ("следующ", "недел", "прием", "приём", "встреч", "личн"))
    )
    return strong_booking_signal and not _has_explicit_service_or_procedure_hint(message, conversation_history)


def _has_service_or_procedure_hint(message: InboundMessage) -> bool:
    text = message.text.casefold()
    listing_title = (message.listing.title if message.listing else "").casefold()
    source = f"{text} {listing_title}"
    if any(word in source for word in ("процедур", "услуг", "губ", "ягод", "груд", "ботокс", "диспорт", "филлер", "носогуб", "скул", "подбород", "биоревитал", "мезотерап", "чистк", "пилинг", "волос", "кожа головы", "тесоро", "tesoro")):
        return True
    return False


def _has_explicit_service_or_procedure_hint(
    message: InboundMessage,
    conversation_history: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> bool:
    source = " ".join(
        part
        for part in (
            message.text,
            " ".join(str(item.get("content") or "") for item in conversation_history[-8:] if str(item.get("role") or "") == "user"),
        )
        if part
    ).casefold()
    if any(word in source for word in ("процедур", "услуг", "губ", "ягод", "груд", "ботокс", "диспорт", "филлер", "носогуб", "скул", "подбород", "биоревитал", "мезотерап", "чистк", "пилинг", "волос", "кожа головы", "тесоро", "tesoro")):
        return True
    return False


def _address_tools_may_help(message: InboundMessage) -> bool:
    lowered = message.text.casefold()
    return any(word in lowered for word in ADDRESS_WORDS)


def risk_or_complaint_handoff(message: InboundMessage) -> Handoff | None:
    lowered = message.text.casefold()
    if not any(word in lowered for word in RISK_WORDS):
        return None
    return Handoff(
        reason=HandoffReason.COMPLAINT_OR_RISK,
        message=message,
        summary="Клиент описывает жалобу, риск или возможную нежелательную реакцию после процедуры.",
    )


def _service_tools_may_help(message: InboundMessage) -> bool:
    lowered = message.text.casefold()
    return bool(message.listing and message.listing.has_listing and any(word in lowered for word in PRICE_WORDS)) or any(
        word in lowered for word in PRICE_WORDS
    )


def _looks_like_price_question(lowered: str) -> bool:
    return any(word in lowered for word in PRICE_WORDS)


def _knowledge_queries(message: InboundMessage) -> list[str]:
    text = message.text.casefold()
    queries: list[str] = []
    if message.listing:
        queries.extend(part for part in (message.listing.title, message.listing.price_string) if part)
    if any(word in text for word in MEDICAL_WORDS):
        queries.extend(["противопоказания", "беременность", "препарат", "эффект"])
    if _asks_amount_or_calculation(text):
        queries.extend(["расчет", "мл", "объем"])
    words = re.findall(r"[а-яёa-z0-9]{4,}", text, flags=re.IGNORECASE)
    queries.extend(words[:5])
    return [query for query in queries if query]


def _unsafe_knowledge_item(item: dict[str, Any]) -> bool:
    if str(item.get("kind") or "") == "location_policy":
        return True
    tags = {str(tag).casefold() for tag in item.get("tags") or []}
    if "bad_example" in tags:
        return True
    text = f"{item.get('title') or ''}\n{item.get('content') or ''}".casefold()
    blocked = (
        "handoff:",
        "handoff contexts",
        "ежедневный quality digest",
        "качество агента",
        "нужен ответ по avito",
        "клиенту отправлено:",
        "проверка:",
        "можно ответить обычным сообщением",
        "активные контексты:",
        "как будто цены",
        "цены в этом прайсе",
        "ну вот цены в боте",
    )
    return any(marker in text for marker in blocked)


def _asks_amount_or_calculation(text: str) -> bool:
    lowered = text.casefold()
    return any(word in lowered for word in ("мл", "объем", "объём", "расчет", "рассчет", "ямок", "сколько нужно"))


def _city_from_message(message: InboundMessage, cities: tuple[str, ...]) -> str:
    flow = AvitoBookingFlow(_NoopBooking(), cities=cities)
    return flow.extract_city(message.text)


def _match_service(text: str, services: list[Service]) -> Service | None:
    return AvitoBookingFlow(_NoopBooking()).match_service(text, services)


def _format_service_price(service: Service) -> str:
    price = f"{service.price} ₽" if service.price > 1 else "цену уточню по услуге"
    return f"{service.title} - {price}"


def _with_next_step(reply: str, message_or_context: InboundMessage | AvitoAgentContext) -> str:
    message = message_or_context.message if isinstance(message_or_context, AvitoAgentContext) else message_or_context
    if "город" in reply.casefold() or _booking_tools_may_help(message):
        return reply
    return f"{reply} В каком городе вам удобно?"


def _message_date(message: InboundMessage) -> str:
    if message.created_at:
        try:
            return datetime.fromtimestamp(int(message.created_at), timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            pass
    return datetime.now(timezone.utc).date().isoformat()


def _codex_payload_reply_rules(profile: RoleProfile) -> list[str]:
    if profile.role in {CodexRole.AVITO_CLIENT, CodexRole.TELEGRAM_CLIENT, CodexRole.VK_CLIENT}:
        return [
            "Отвечай коротко, по-человечески и только по вопросу клиента.",
            "conversation_history — память именно этого клиента/чата; используй её для коротких сообщений вроде 'да', 'завтра', 'на 30-е'.",
            "Router уже отфильтровал простые случаи: high-confidence RAG, фото/видео, риск, личную встречу без процедуры и запрос города.",
            "Если пришёл сюда, сначала используй доступные read-only tools/knowledge/историю; если опоры всё ещё нет — сделай внутренний handoff.",
            "Клиентские роли не делают live-мутации: не создают, не переносят, не отменяют YCLIENTS-записи и не пишут notes.",
            "Не раскрывай клиенту tools, trace, RAG ids, source, внутренние причины или слова handoff/эскалация.",
            "Не предлагай очную консультацию как стандартный шаг; если реально нужна оценка, один раз предложи онлайн-разбор и собери недостающие данные.",
            "Точный адрес называй только из yclients.company.address; цену — только из подтверждённого источника или YCLIENTS price_status='known'.",
            "Если schedule_status='unknown', график неизвестен: не говори, что мест нет.",
        ]
    return [
        *profile.reply_rules,
        "conversation_history — память именно этого клиента/чата; используй её для коротких ответов вроде 'да', 'завтра', 'на 30-е'.",
        "Сначала используй инструменты и знания, потом отвечай.",
        "Если сомневаешься, собери контекст через доступные tools/knowledge/историю; если всё ещё нет опоры — сделай внутренний handoff.",
        "Не показывай клиенту/Ольге внутренние trace/tool details без необходимости.",
    ]


def _message_payload(message: InboundMessage) -> dict[str, Any]:
    payload = asdict(message)
    metadata = dict(message.metadata or {})
    safe_metadata = {
        "account_id": metadata.get("account_id"),
        "author_role": metadata.get("author_role") or "client",
        "author_id": metadata.get("author_id"),
        "direction": metadata.get("direction") or "",
        "is_own_account": bool(metadata.get("is_own_account")),
        "message_type": metadata.get("message_type") or "",
        "photo_ids": metadata.get("photo_ids") or [],
        "photo_urls": metadata.get("photo_urls") or [],
        "source": metadata.get("source") or "",
    }
    raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
    raw_message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    raw_chat = raw.get("chat") if isinstance(raw.get("chat"), dict) else {}
    last_message = raw_chat.get("last_message") if isinstance(raw_chat.get("last_message"), dict) else {}
    if raw_message:
        safe_metadata["raw_message"] = _compact_raw_message(raw_message)
    if last_message and str(last_message.get("id") or "") == message.message_id:
        safe_metadata["chat_last_message"] = _compact_raw_message(last_message)
    payload["metadata"] = safe_metadata
    return payload


def _compact_raw_message(raw_message: dict[str, Any]) -> dict[str, Any]:
    content = raw_message.get("content") if isinstance(raw_message.get("content"), dict) else {}
    return {
        "id": str(raw_message.get("id") or ""),
        "author_id": str(raw_message.get("author_id") or ""),
        "created": raw_message.get("created") or 0,
        "direction": str(raw_message.get("direction") or ""),
        "type": str(raw_message.get("type") or ""),
        "text": str(content.get("text") or "")[:1000],
    }


def _service_from_data(row: dict[str, Any]) -> Service:
    return Service(
        id=int(row.get("id") or 0),
        title=str(row.get("title") or ""),
        price=int(row.get("price") or 0),
        duration_minutes=int(row.get("duration_minutes") or 0),
        city=str(row.get("city") or ""),
    )


def _reply_from_codex_step(
    context: AvitoAgentContext,
    step: dict[str, Any],
    *,
    planner: str,
    metadata: dict[str, Any],
) -> AvitoConsultantReply:
    del planner
    handoff = _handoff_from_codex_step(context, step)
    return AvitoConsultantReply(
        action=str(step.get("action") or ("handoff" if handoff else "codex_reply")),
        reply=str(step.get("reply") or ""),
        handoff=handoff,
        appointment_id=step.get("appointment_id"),
        metadata=metadata,
    )


def _handoff_from_codex_step(context: AvitoAgentContext, step: dict[str, Any]) -> Handoff | None:
    raw_reason = str(step.get("handoff_reason") or "").strip()
    if not raw_reason:
        return None
    try:
        reason = HandoffReason(raw_reason)
    except ValueError:
        return None
    return Handoff(
        reason=reason,
        message=context.message,
        summary=_normalize_handoff_summary(str(step.get("handoff_summary") or step.get("summary") or "")),
    )


def _normalize_handoff_summary(summary: str) -> str:
    text = str(summary or "").strip()
    replacements = {
        "Нужно у Ольги:": "Нужно:",
        "Нужно от Ольги:": "Нужно:",
        "Нужно спросить у Ольги:": "Нужно:",
        "Нужно уточнить у Ольги:": "Нужно уточнить:",
        "Нужно у специалиста:": "Нужно:",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _expert_answer_autoanswer_allowed(answer: dict[str, Any]) -> bool:
    metadata = answer.get("metadata") if isinstance(answer.get("metadata"), dict) else {}
    return metadata.get("autoanswer_allowed") is not False


class _NoopBooking:
    async def get_services(self, city: str = "") -> list[Service]:
        return []

    async def get_free_slots(self, city: str, service_id: int, date: str) -> list[Slot]:
        return []

    async def create_appointment(self, appointment: Any) -> int:
        return 0


def _conversation_key_for_message(message: InboundMessage, profile: RoleProfile) -> str:
    if profile.role == CodexRole.VK_CLIENT:
        channel = "vk"
    elif profile.role == CodexRole.TELEGRAM_CLIENT:
        channel = "telegram_client"
    else:
        channel = "avito"
    identifier = message.chat_id or message.client_id
    return conversation_key(channel, profile.role, identifier)


def _tool_calls_from_step(step: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = step.get("tool_calls") or step.get("tools") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    return [call for call in raw_calls if isinstance(call, dict) and call.get("name")]
