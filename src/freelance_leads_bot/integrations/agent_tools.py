from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import ROOT
from ..storage import LeadStore
from .avito_history import prepare_avito_outgoing_text, remember_avito_outgoing, sent_successfully
from .care_crm import CareCrmStore, CareLearningService, ClientIdentityService, ClientMemoryService, VisitFactService
from .models import Appointment, ClientProfile, Service, Slot, UpsellRule
from .roles import RoleProfile
from .handoff_notify import HandoffNotifier
from .upsell import CareUpsellPlanner, CareUpsellService, care_task_data
from .yclients import YClientsGateway
from .avito_read import AvitoReadGateway
from .avito_sender import AvitoSender
from .handoff_refs import update_latest_handoff_for_chat
from .city_schedule import CityScheduleStore


@dataclass(frozen=True)
class KnowledgeItem:
    id: str
    kind: str
    title: str
    content: str
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required: tuple[str, ...] = ()
    properties: dict[str, str] = field(default_factory=dict)
    mutates: bool = False
    external: bool = False
    guardrail: str = ""

    def to_prompt_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required": list(self.required),
            "properties": dict(self.properties),
            "mutates": self.mutates,
            "external": self.external,
            "guardrail": self.guardrail,
        }


class JsonKnowledgeStore:
    """Small CRUD store for bot-owned knowledge, examples, FAQ, and service notes."""

    def __init__(self, path: Path | str = Path("data/bot_knowledge.json")) -> None:
        self.path = Path(path)

    def create(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeItem:
        now = _now()
        item = KnowledgeItem(
            id=str(uuid4()),
            kind=kind,
            title=title,
            content=content,
            tags=tuple(tag.strip() for tag in tags if tag.strip()),
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        items = self._load()
        items.append(item)
        self._save(items)
        return item

    def list(self, *, query: str = "", kind: str = "", tags: tuple[str, ...] = ()) -> list[KnowledgeItem]:
        items = self._load()
        if kind:
            items = [item for item in items if item.kind == kind]
        if tags:
            wanted = {tag.casefold() for tag in tags}
            items = [item for item in items if wanted.intersection(tag.casefold() for tag in item.tags)]
        if query:
            needle = query.casefold()
            items = [
                item
                for item in items
                if needle in item.title.casefold()
                or needle in item.content.casefold()
                or any(needle in tag.casefold() for tag in item.tags)
            ]
        return items

    def get(self, item_id: str) -> KnowledgeItem | None:
        return next((item for item in self._load() if item.id == item_id), None)

    def update(self, item_id: str, **changes: Any) -> KnowledgeItem:
        items = self._load()
        for index, item in enumerate(items):
            if item.id == item_id:
                allowed = {key: value for key, value in changes.items() if key in {"kind", "title", "content", "tags", "metadata"}}
                if "tags" in allowed:
                    allowed["tags"] = tuple(allowed["tags"])
                updated = replace(item, **allowed, updated_at=_now())
                items[index] = updated
                self._save(items)
                return updated
        raise KeyError(f"knowledge item {item_id} not found")

    def delete(self, item_id: str) -> bool:
        items = self._load()
        kept = [item for item in items if item.id != item_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    def _load(self) -> list[KnowledgeItem]:
        if not self.path.exists():
            return []
        raw_items = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            KnowledgeItem(
                id=str(row.get("id") or ""),
                kind=str(row.get("kind") or "note"),
                title=str(row.get("title") or ""),
                content=str(row.get("content") or ""),
                tags=tuple(row.get("tags") or ()),
                metadata=dict(row.get("metadata") or {}),
                created_at=str(row.get("created_at") or ""),
                updated_at=str(row.get("updated_at") or ""),
            )
            for row in raw_items
            if isinstance(row, dict)
        ]

    def _save(self, items: list[KnowledgeItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(item) for item in items]
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


class AutomationToolbox:
    """CRUD tools shared by Avito, Telegram admin, and care/upsell agents."""

    def __init__(
        self,
        booking: YClientsGateway,
        knowledge: JsonKnowledgeStore | None = None,
        avito: AvitoReadGateway | None = None,
        avito_sender: AvitoSender | None = None,
        avito_image_sender: AvitoSender | None = None,
        avito_account_id: int = 0,
        enable_workspace_tools: bool = False,
        city_schedule: CityScheduleStore | None = None,
        care_crm: CareCrmStore | None = None,
        role_profile: RoleProfile | None = None,
        history_store: LeadStore | None = None,
        operations_notifier: HandoffNotifier | None = None,
    ) -> None:
        self.booking = booking
        self.knowledge = knowledge or JsonKnowledgeStore()
        self.avito = avito
        self.avito_sender = avito_sender
        self.avito_image_sender = avito_image_sender
        self.avito_account_id = avito_account_id
        self.city_schedule = city_schedule or CityScheduleStore()
        self.care_crm = care_crm
        self.role_profile = role_profile
        self.history_store = history_store
        self.operations_notifier = operations_notifier
        self._tool_specs = _tool_specs(enable_workspace_tools=enable_workspace_tools or bool(role_profile and role_profile.allow_workspace_tools))
        if role_profile:
            self._tool_specs = {name: spec for name, spec in self._tool_specs.items() if role_profile.allows_tool(name)}

    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._tool_specs)

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tool_specs.values())

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [spec.to_prompt_schema() for spec in self.tool_specs()]

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        args = arguments or {}
        validation_error = self.validate_call(name, args)
        if validation_error:
            return ToolResult(ok=False, error=validation_error)
        try:
            if name == "yclients.company.address":
                address = await self.booking.get_company_address(str(args.get("city") or ""))
                return ToolResult(ok=True, data={"company": address})
            if name == "yclients.services.list":
                services = await self.booking.get_services(str(args.get("city") or ""))
                return ToolResult(ok=True, data={"services": [_service_data(item) for item in services]})
            if name == "yclients.slots.list":
                date_value = str(args.get("date") or "")[:10]
                schedule_city = self.city_schedule.get_city(date_value)
                requested_city = self.city_schedule.normalize_city(str(args.get("city") or ""))
                if not schedule_city:
                    return ToolResult(
                        ok=True,
                        data={
                            "slots": [],
                            "schedule_status": "unknown",
                            "schedule_missing": True,
                            "requested_city": requested_city,
                            "date": date_value,
                            "can_state_no_slots": False,
                            "handoff_recommended": True,
                            "message": "График Ольги на эту дату не задан; нельзя делать вывод, что мест нет.",
                        },
                    )
                if schedule_city and requested_city and not self.city_schedule.city_matches(schedule_city, requested_city):
                    return ToolResult(
                        ok=True,
                        data={
                            "slots": [],
                            "schedule_status": "known_wrong_city",
                            "blocked_by_city_schedule": True,
                            "requested_city": requested_city,
                            "schedule_city": schedule_city,
                            "date": date_value,
                            "can_state_no_slots": False,
                            "handoff_recommended": False,
                        },
                    )
                slots = await self.booking.get_free_slots(
                    requested_city or str(args.get("city") or ""),
                    int(args.get("service_id") or 0),
                    str(args.get("date") or ""),
                )
                return ToolResult(
                    ok=True,
                    data={
                        "slots": [_slot_data(item) for item in slots],
                        "schedule_status": "known",
                        "requested_city": requested_city,
                        "schedule_city": schedule_city,
                        "date": date_value,
                        "can_state_no_slots": not bool(slots),
                        "handoff_recommended": False,
                    },
                )
            if name == "yclients.appointments.create":
                appointment, service_error = await self._appointment_from_args_checked(args)
                if service_error:
                    return ToolResult(ok=False, error=service_error)
                appointment_id = await self.booking.create_appointment(appointment)
                return ToolResult(ok=True, data={"appointment_id": appointment_id})
            if name == "yclients.appointments.list":
                appointments = await self.booking.list_appointments(
                    str(args.get("date") or ""),
                    str(args.get("client_id") or ""),
                    str(args.get("city") or ""),
                )
                return ToolResult(ok=True, data={"appointments": [_appointment_data(item) for item in appointments]})
            if name == "yclients.appointments.move":
                appointment = await self.booking.move_appointment(
                    int(args["appointment_id"]),
                    _slot_from_args(args),
                    str(args.get("city") or ""),
                )
                return ToolResult(ok=True, data={"appointment": _appointment_data(appointment)})
            if name == "yclients.appointments.cancel":
                appointment = await self.booking.cancel_appointment(
                    int(args["appointment_id"]),
                    str(args.get("city") or ""),
                )
                if not appointment:
                    return ToolResult(ok=False, error="YCLIENTS appointment was not found or was not cancelled")
                appointment_data = _appointment_data(appointment)
                notification = {}
                if self.operations_notifier:
                    notification = await self.operations_notifier.notify_text(_cancellation_notification(appointment))
                return ToolResult(
                    ok=True,
                    data={
                        "cancelled": True,
                        "appointment": appointment_data,
                        "client_message": _cancellation_client_message(appointment),
                        "olga_notification": notification,
                    },
                )
            if name == "yclients.clients.search":
                clients = await self.booking.search_clients(
                    str(args.get("query") or ""),
                    str(args.get("city") or ""),
                )
                duplicate_names = _duplicate_client_names(clients)
                return ToolResult(
                    ok=True,
                    data={
                        "clients": [_client_data(item) for item in clients],
                        "ambiguous": bool(duplicate_names),
                        "duplicate_names": sorted(duplicate_names),
                        "selection_rule": (
                            "Есть одноимённые клиенты. Перед изменением данных выбери конкретный client_id по телефону и городу."
                            if duplicate_names
                            else "Используй конкретный client_id из результата для изменений."
                        ),
                    },
                )
            if name == "yclients.clients.notes.update":
                await self.booking.update_client_notes(
                    str(args["client_id"]),
                    str(args.get("notes") or ""),
                    str(args.get("skin_type") or ""),
                    str(args.get("city") or ""),
                )
                return ToolResult(ok=True, data={"client_id": str(args["client_id"]), "city": str(args.get("city") or "")})
            if name == "care.tasks.plan":
                planner = CareUpsellPlanner(_upsell_rules_from_args(args.get("rules") or []))
                service = CareUpsellService(self.booking, planner)
                tasks = await service.tasks_between(
                    str(args.get("start_date") or args.get("date") or ""),
                    str(args.get("end_date") or args.get("date") or ""),
                    now=_optional_datetime_arg(args.get("now")),
                )
                return ToolResult(ok=True, data={"tasks": [care_task_data(task) for task in tasks]})
            if name.startswith("care.learning."):
                return self._execute_care_learning(name, args)
            if name.startswith("care.crm."):
                return self._execute_care_crm(name, args)
            if name == "avito.chats.list":
                return await self._execute_avito_chats_list(args)
            if name == "avito.messages.list":
                return await self._execute_avito_messages_list(args)
            if name == "avito.messages.send":
                return await self._execute_avito_messages_send(args)
            if name == "avito.messages.send_phone":
                return await self._execute_avito_messages_send_phone(args)
            if name == "avito.messages.send_image":
                return await self._execute_avito_messages_send_image(args)
            if name == "avito.messages.send_file":
                return await self._execute_avito_messages_send_file(args)
            if name.startswith("schedule."):
                return await self._execute_schedule(name, args)
            if name.startswith("workspace."):
                return await self._execute_workspace(name, args)
            if name.startswith("knowledge."):
                return self._execute_knowledge(name, args)
            return ToolResult(ok=False, error=f"unknown tool: {name}")
        except Exception as exc:
            return ToolResult(ok=False, error=str(exc))

    def validate_call(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        spec = self._tool_specs.get(name)
        if spec is None:
            return f"unknown tool: {name}"
        if self.role_profile and not self.role_profile.live_actions_enabled and spec.mutates:
            return f"tool {name} is disabled for role {self.role_profile.role.value}"
        args = arguments or {}
        missing = [key for key in spec.required if _is_missing(args.get(key))]
        if missing:
            return f"missing required arguments for {name}: {', '.join(missing)}"
        return ""

    async def _appointment_from_args_checked(self, args: dict[str, Any]) -> tuple[Appointment, str]:
        appointment = _appointment_from_args(args)
        city = str(args.get("city") or "")
        service_id = int(args.get("service_id") or 0)
        services = await self.booking.get_services(city)
        selected = next((service for service in services if int(service.id or 0) == service_id), None)
        if selected is None:
            return appointment, f"YCLIENTS service_id {service_id} not found for city={city!r}; call yclients.services.list and choose an exact service."
        hint = _appointment_service_hint(args)
        mismatch = _service_hint_mismatch(hint, selected.title)
        if mismatch:
            return appointment, (
                f"Refusing to create appointment: service_id {service_id} is {selected.title!r}, "
                f"but request/notes mention {mismatch}. Call yclients.services.list and use the matching service_id."
            )
        prepared_args = {
            **args,
            "service_title": selected.title,
            "service_price": selected.price,
            "duration_minutes": selected.duration_minutes or args.get("duration_minutes") or 60,
        }
        return _appointment_from_args(prepared_args), ""

    def _execute_care_crm(self, name: str, args: dict[str, Any]) -> ToolResult:
        care_crm = self.care_crm or CareCrmStore()
        if name == "care.crm.client.memory.get":
            memory = ClientMemoryService(care_crm).memory(
                int(args["client_id"]),
                include_internal=bool(args.get("include_internal") or False),
                limit=_bounded_int(args.get("limit"), default=6, minimum=1, maximum=15),
            )
            return ToolResult(ok=True, data={"memory": memory})
        if name == "care.crm.clients.search":
            clients = care_crm.search_clients(
                str(args.get("query") or ""),
                limit=_bounded_int(args.get("limit"), default=10, minimum=1, maximum=25),
            )
            return ToolResult(ok=True, data={"clients": [_crm_client_data(row) for row in clients]})
        if name == "care.crm.client.get":
            client = care_crm.get_client(int(args["client_id"]))
            return ToolResult(ok=client is not None, data={"client": _crm_client_data(client) if client else None})
        if name == "care.crm.visits.list":
            visits = care_crm.list_client_visits(
                int(args["client_id"]),
                limit=_bounded_int(args.get("limit"), default=10, minimum=1, maximum=25),
            )
            return ToolResult(ok=True, data={"visits": [_crm_visit_data(row) for row in visits]})
        if name == "care.crm.interactions.list":
            interactions = care_crm.list_client_interactions(
                int(args["client_id"]),
                limit=_bounded_int(args.get("limit"), default=10, minimum=1, maximum=25),
            )
            return ToolResult(ok=True, data={"interactions": [_crm_interaction_data(row) for row in interactions]})
        if name == "care.crm.interactions.create":
            interaction_id = care_crm.add_interaction(
                int(args["client_id"]),
                appointment_id=int(args["appointment_id"]) if args.get("appointment_id") else None,
                visit_id=int(args["visit_id"]) if args.get("visit_id") else None,
                channel=str(args.get("channel") or "telegram_client"),
                direction=str(args.get("direction") or "inbound_client"),
                author=str(args.get("author") or ""),
                body=str(args.get("body") or ""),
                intent=str(args.get("intent") or "client_message"),
                metadata=dict(args.get("metadata") or {}),
            )
            return ToolResult(ok=True, data={"interaction_id": interaction_id})
        if name == "care.crm.client.flags.update":
            client = care_crm.update_client_flags(
                int(args["client_id"]),
                consent_status=str(args["consent_status"]) if args.get("consent_status") is not None else None,
                do_not_contact=bool(args["do_not_contact"]) if args.get("do_not_contact") is not None else None,
                complaint_risk=bool(args["complaint_risk"]) if args.get("complaint_risk") is not None else None,
            )
            return ToolResult(ok=True, data={"client": _crm_client_data(client)})
        if name == "care.crm.followups.list":
            tasks = care_crm.list_followup_tasks(
                status=str(args.get("status") or "planned"),
                due_before=str(args.get("due_before") or ""),
                client_id=int(args["client_id"]) if args.get("client_id") else None,
                limit=_bounded_int(args.get("limit"), default=25, minimum=1, maximum=100),
            )
            return ToolResult(ok=True, data={"tasks": [_crm_followup_task_data(row) for row in tasks]})
        if name == "care.crm.appointments.match":
            rows = VisitFactService(care_crm).match(
                query=str(args.get("query") or ""),
                day=str(args.get("day") or ""),
                limit=_bounded_int(args.get("limit"), default=10, minimum=1, maximum=25),
            )
            return ToolResult(ok=True, data={"appointments": [_crm_appointment_data(row) for row in rows]})
        if name == "care.crm.visit.fact.upsert":
            row = VisitFactService(care_crm).upsert_fact(
                int(args["appointment_id"]),
                attended=bool(args.get("attended", True)),
                actual_service_title=str(args.get("actual_service_title") or ""),
                amount_ml=str(args.get("amount_ml") or ""),
                units=str(args.get("units") or ""),
                product_or_drug=str(args.get("product_or_drug") or ""),
                procedure_notes=str(args.get("procedure_notes") or ""),
                reaction=str(args.get("reaction") or ""),
                aftercare_notes=str(args.get("aftercare_notes") or ""),
                confirmed_by=str(args.get("confirmed_by") or "agent_tool"),
                source_text=str(args.get("source_text") or ""),
            )
            return ToolResult(ok=True, data={"appointment": _crm_appointment_data(row)})
        if name == "care.crm.client.link":
            result = ClientIdentityService(care_crm).link(
                int(args["client_id"]),
                channel=str(args["channel"]),
                external_user_id=str(args.get("external_user_id") or ""),
                chat_id=str(args.get("chat_id") or ""),
                username=str(args.get("username") or ""),
                display_name=str(args.get("display_name") or ""),
                verified=bool(args.get("verified") or False),
            )
            return ToolResult(ok=True, data={"result": result})
        if name == "care.crm.client.merge.suggest":
            suggestions = ClientIdentityService(care_crm).suggest_merges(
                query=str(args.get("query") or ""),
                limit=_bounded_int(args.get("limit"), default=10, minimum=1, maximum=25),
            )
            return ToolResult(ok=True, data={"suggestions": suggestions})
        if name == "care.crm.client.merge.apply":
            result = ClientIdentityService(care_crm).apply_merge(
                source_client_id=int(args["source_client_id"]),
                target_client_id=int(args["target_client_id"]),
                merged_by=str(args.get("merged_by") or "agent_tool"),
            )
            return ToolResult(ok=True, data={"result": result})
        return ToolResult(ok=False, error=f"unknown tool: {name}")

    def _execute_care_learning(self, name: str, args: dict[str, Any]) -> ToolResult:
        care_crm = self.care_crm or CareCrmStore()
        learning = CareLearningService(care_crm)
        if name == "care.learning.lesson.create":
            item = learning.create_lesson(
                lesson=str(args["lesson"]),
                source=str(args.get("source") or ""),
                tags=tuple(str(tag) for tag in (args.get("tags") or ())),
                confidence=float(args.get("confidence") or 0.5),
                created_from_interaction_id=int(args["created_from_interaction_id"]) if args.get("created_from_interaction_id") else None,
                metadata=dict(args.get("metadata") or {}),
            )
            return ToolResult(ok=True, data={"lesson": _crm_lesson_data(item)})
        if name == "care.learning.lessons.list":
            items = learning.lessons(
                query=str(args.get("query") or ""),
                tags=tuple(str(tag) for tag in (args.get("tags") or ())),
                limit=_bounded_int(args.get("limit"), default=20, minimum=1, maximum=100),
            )
            return ToolResult(ok=True, data={"lessons": [_crm_lesson_data(item) for item in items]})
        if name == "care.learning.preference.upsert":
            item = learning.upsert_preference(
                int(args["client_id"]),
                preference_type=str(args["preference_type"]),
                value=str(args["value"]),
                source=str(args.get("source") or ""),
                confidence=float(args.get("confidence") or 0.5),
                metadata=dict(args.get("metadata") or {}),
            )
            return ToolResult(ok=True, data={"preference": _crm_preference_data(item)})
        if name == "care.learning.outcome.record":
            decision_id = learning.record_outcome(
                agent_role=str(args.get("agent_role") or ""),
                input_ref=str(args.get("input_ref") or ""),
                decision=dict(args.get("decision") or {}),
                tool_calls=list(args.get("tool_calls") or []),
                outcome=str(args.get("outcome") or ""),
            )
            return ToolResult(ok=True, data={"decision_id": decision_id})
        return ToolResult(ok=False, error=f"unknown tool: {name}")

    async def _execute_schedule(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "schedule.city.list":
            rows = self.city_schedule.list(
                from_date=str(args.get("from_date") or ""),
                city=str(args.get("city") or ""),
            )
            audit = await self._audit_city_schedule(rows)
            return ToolResult(
                ok=True,
                data={
                    "schedule": rows,
                    "yclients_audit": audit,
                    "text": self.city_schedule.format(
                        from_date=str(args.get("from_date") or ""),
                        city=str(args.get("city") or ""),
                    ),
                },
            )
        if name == "schedule.city.set":
            city = str(args.get("city") or "")
            raw_dates = args.get("dates") or args.get("text") or ""
            dates = [str(item) for item in raw_dates] if isinstance(raw_dates, list) else self.city_schedule.parse_dates(str(raw_dates))
            if not city.strip() or not dates:
                return ToolResult(ok=False, error="city and dates are required")
            cities = self.city_schedule.normalize_cities(city)
            if not cities:
                return ToolResult(ok=False, error="a supported city is required")
            slots = _schedule_slots_from_args(args)
            previous_by_date = {
                schedule_date[:10]: self.city_schedule.normalize_cities(self.city_schedule.get_city(schedule_date))
                for schedule_date in dates
            }
            yclients_updates = [
                await self.booking.set_staff_schedule(schedule_city, dates, slots)
                for schedule_city in cities
            ]
            deletes_by_city: dict[str, list[str]] = {}
            for schedule_date, previous_cities in previous_by_date.items():
                for previous_city in previous_cities:
                    if previous_city not in cities:
                        deletes_by_city.setdefault(previous_city, []).append(schedule_date)
            yclients_deletes = [
                await self.booking.delete_staff_schedule(previous_city, previous_dates)
                for previous_city, previous_dates in deletes_by_city.items()
            ]
            result = self.city_schedule.set_dates(city, dates)
            return ToolResult(
                ok=True,
                data={
                    **result,
                    "synchronized": True,
                    "yclients_updates": yclients_updates,
                    "yclients_deletes": yclients_deletes,
                    "text": self.city_schedule.format(),
                },
            )
        if name == "schedule.city.delete":
            raw_dates = args.get("dates") or args.get("text") or ""
            dates = [str(item) for item in raw_dates] if isinstance(raw_dates, list) else self.city_schedule.parse_dates(str(raw_dates))
            if not dates:
                return ToolResult(ok=False, error="dates are required")
            cities_by_date = {
                schedule_date[:10]: self.city_schedule.normalize_cities(self.city_schedule.get_city(schedule_date))
                for schedule_date in dates
            }
            missing_dates = [schedule_date for schedule_date, cities in cities_by_date.items() if not cities]
            if missing_dates:
                return ToolResult(
                    ok=False,
                    error=f"city schedule is missing for dates: {', '.join(missing_dates)}",
                )
            deletes_by_city: dict[str, list[str]] = {}
            for schedule_date, cities in cities_by_date.items():
                for schedule_city in cities:
                    deletes_by_city.setdefault(schedule_city, []).append(schedule_date)
            yclients_deletes = [
                await self.booking.delete_staff_schedule(schedule_city, schedule_dates)
                for schedule_city, schedule_dates in deletes_by_city.items()
            ]
            result = self.city_schedule.delete_dates(dates)
            return ToolResult(
                ok=True,
                data={
                    **result,
                    "synchronized": True,
                    "yclients_deletes": yclients_deletes,
                    "text": self.city_schedule.format(),
                },
            )
        return ToolResult(ok=False, error=f"unknown tool: {name}")

    async def _audit_city_schedule(self, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
        audited = [dict(row) for row in rows]
        indexes_by_city: dict[str, list[int]] = {}
        for index, row in enumerate(audited):
            for city in self.city_schedule.normalize_cities(str(row.get("city") or "")):
                indexes_by_city.setdefault(city, []).append(index)
        for city, indexes in indexes_by_city.items():
            dates = [str(audited[index]["date"]) for index in indexes]
            try:
                schedule_rows = await self.booking.get_staff_schedule(city, min(dates), max(dates))
                active_dates = {
                    str(item.get("date") or "")[:10]
                    for item in schedule_rows
                    if isinstance(item, dict) and item.get("slots")
                }
                for index in indexes:
                    status = "synced" if audited[index]["date"] in active_dates else "missing_in_yclients"
                    previous = str(audited[index].get("yclients_status") or "")
                    audited[index]["yclients_status"] = (
                        "missing_in_yclients"
                        if previous == "missing_in_yclients" or status == "missing_in_yclients"
                        else status
                    )
            except Exception as exc:
                for index in indexes:
                    audited[index]["yclients_status"] = "unavailable"
                    audited[index]["yclients_error"] = str(exc)
        return audited

    async def _execute_workspace(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "workspace.files.list":
            path = _workspace_path(str(args.get("path") or "."))
            pattern = str(args.get("pattern") or "*")
            max_results = _bounded_int(args.get("max_results"), default=50, minimum=1, maximum=200)
            if _is_sensitive_path(path):
                return ToolResult(ok=False, error="refusing to list sensitive path")
            if not path.exists():
                return ToolResult(ok=False, error=f"path does not exist: {_display_path(path)}")
            if not path.is_dir():
                return ToolResult(ok=False, error=f"path is not a directory: {_display_path(path)}")
            files: list[dict[str, Any]] = []
            for item in path.rglob(pattern):
                if len(files) >= max_results:
                    break
                if _is_sensitive_path(item):
                    continue
                try:
                    stat = item.stat()
                except OSError:
                    continue
                files.append({"path": _display_path(item), "type": "dir" if item.is_dir() else "file", "size": stat.st_size})
            return ToolResult(ok=True, data={"files": files, "truncated": len(files) >= max_results})
        if name == "workspace.files.read":
            path = _workspace_path(str(args.get("path") or ""))
            max_chars = _bounded_int(args.get("max_chars"), default=12000, minimum=1, maximum=40000)
            if _is_sensitive_path(path):
                return ToolResult(ok=False, error="refusing to read sensitive path")
            if not path.exists():
                return ToolResult(ok=False, error=f"path does not exist: {_display_path(path)}")
            if not path.is_file():
                return ToolResult(ok=False, error=f"path is not a file: {_display_path(path)}")
            text = path.read_text(encoding="utf-8", errors="replace")
            return ToolResult(ok=True, data={"path": _display_path(path), "content": text[:max_chars], "truncated": len(text) > max_chars})
        if name == "workspace.logs.tail":
            path = _workspace_path(str(args.get("path") or "data/agent_trace.jsonl"))
            lines = _bounded_int(args.get("lines"), default=80, minimum=1, maximum=300)
            if _is_sensitive_path(path):
                return ToolResult(ok=False, error="refusing to read sensitive log path")
            if not path.exists():
                return ToolResult(ok=False, error=f"log file does not exist: {_display_path(path)}")
            if not path.is_file():
                return ToolResult(ok=False, error=f"path is not a file: {_display_path(path)}")
            text = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
            return ToolResult(ok=True, data={"path": _display_path(path), "content": _truncate_text(text, 40000)})
        if name == "workspace.command.run":
            return await _run_workspace_command(str(args.get("command") or ""), _bounded_int(args.get("timeout_seconds"), default=10, minimum=1, maximum=20))
        if name == "workspace.python.run":
            return await _run_workspace_python(str(args.get("code") or ""), _bounded_int(args.get("timeout_seconds"), default=10, minimum=1, maximum=20))
        return ToolResult(ok=False, error=f"unknown tool: {name}")

    def _execute_knowledge(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "knowledge.create":
            item = self.knowledge.create(
                kind=str(args.get("kind") or "note"),
                title=str(args.get("title") or ""),
                content=str(args.get("content") or ""),
                tags=tuple(args.get("tags") or ()),
                metadata=dict(args.get("metadata") or {}),
            )
            return ToolResult(ok=True, data={"item": _knowledge_data(item)})
        if name == "knowledge.list":
            items = self.knowledge.list(
                query=str(args.get("query") or ""),
                kind=str(args.get("kind") or ""),
                tags=tuple(args.get("tags") or ()),
            )
            return ToolResult(ok=True, data={"items": [_knowledge_data(item) for item in items]})
        if name == "knowledge.get":
            item = self.knowledge.get(str(args["id"]))
            return ToolResult(ok=item is not None, data={"item": _knowledge_data(item) if item else None})
        if name == "knowledge.update":
            item_id = str(args.pop("id"))
            item = self.knowledge.update(item_id, **args)
            return ToolResult(ok=True, data={"item": _knowledge_data(item)})
        if name == "knowledge.delete":
            deleted = self.knowledge.delete(str(args["id"]))
            return ToolResult(ok=deleted, data={"deleted": deleted})
        return ToolResult(ok=False, error=f"unknown tool: {name}")

    async def _execute_avito_chats_list(self, args: dict[str, Any]) -> ToolResult:
        if not self.avito:
            return ToolResult(ok=False, error="Avito read gateway is not configured")
        account_id = int(args.get("account_id") or self.avito_account_id or 0)
        if not account_id:
            return ToolResult(ok=False, error="Avito account_id is missing")
        payload = await self.avito.list_chats(
            account_id,
            limit=_bounded_int(args.get("limit"), default=10, minimum=1, maximum=50),
            offset=max(0, int(args.get("offset") or 0)),
        )
        return ToolResult(ok=True, data={"account_id": account_id, "chats": [_chat_data(chat) for chat in _items(payload, "chats", "items")]})

    async def _execute_avito_messages_list(self, args: dict[str, Any]) -> ToolResult:
        if not self.avito:
            return ToolResult(ok=False, error="Avito read gateway is not configured")
        account_id = int(args.get("account_id") or self.avito_account_id or 0)
        chat_id = str(args.get("chat_id") or "")
        if not account_id:
            return ToolResult(ok=False, error="Avito account_id is missing")
        if not chat_id:
            return ToolResult(ok=False, error="chat_id is missing")
        payload = await self.avito.get_chat_messages(
            account_id,
            chat_id,
            limit=_bounded_int(args.get("limit"), default=30, minimum=1, maximum=100),
            offset=max(0, int(args.get("offset") or 0)),
        )
        return ToolResult(ok=True, data={"account_id": account_id, "chat_id": chat_id, "messages": [_message_data(message) for message in _items(payload, "messages", "items")]})

    async def _execute_avito_messages_send(self, args: dict[str, Any]) -> ToolResult:
        if not self.avito_sender:
            return ToolResult(ok=False, error="Avito sender is not configured")
        account_id = int(args.get("account_id") or self.avito_account_id or 0)
        chat_id = str(args.get("chat_id") or "")
        text = str(args.get("text") or "")
        if not account_id:
            return ToolResult(ok=False, error="Avito account_id is missing")
        if not chat_id:
            return ToolResult(ok=False, error="chat_id is missing")
        if not text.strip():
            return ToolResult(ok=False, error="text is missing")
        outgoing_text = prepare_avito_outgoing_text(self.history_store, chat_id, text)
        result = await self.avito_sender.send_message(account_id, chat_id, outgoing_text)
        if sent_successfully(result):
            remember_avito_outgoing(self.history_store, chat_id, outgoing_text)
            update_latest_handoff_for_chat(chat_id, "closed")
        return ToolResult(ok=True, data={"account_id": account_id, "chat_id": chat_id, "text": outgoing_text, "send_result": result})

    async def _execute_avito_messages_send_phone(self, args: dict[str, Any]) -> ToolResult:
        if not self.avito_sender:
            return ToolResult(ok=False, error="Avito sender is not configured")
        account_id = int(args.get("account_id") or self.avito_account_id or 0)
        chat_id = str(args.get("chat_id") or "")
        phone = str(args.get("phone") or "").strip()
        text = str(args.get("text") or "").strip()
        if not account_id:
            return ToolResult(ok=False, error="Avito account_id is missing")
        if not chat_id:
            return ToolResult(ok=False, error="chat_id is missing")
        if not phone:
            return ToolResult(ok=False, error="phone is missing")
        message = text or f"Телефон для связи: {phone}"
        if phone not in message:
            message = f"{message}\n{phone}"
        outgoing_text = prepare_avito_outgoing_text(self.history_store, chat_id, message)
        result = await self.avito_sender.send_message(account_id, chat_id, outgoing_text)
        if sent_successfully(result):
            remember_avito_outgoing(self.history_store, chat_id, outgoing_text)
            update_latest_handoff_for_chat(chat_id, "closed")
        return ToolResult(ok=True, data={"account_id": account_id, "chat_id": chat_id, "phone": phone, "text": outgoing_text, "send_result": result})

    async def _execute_avito_messages_send_image(self, args: dict[str, Any]) -> ToolResult:
        if not self.avito_image_sender:
            return ToolResult(ok=False, error="Avito image sender is not configured")
        account_id = int(args.get("account_id") or self.avito_account_id or 0)
        chat_id = str(args.get("chat_id") or "")
        image_path = str(args.get("image_path") or args.get("path") or "")
        if not account_id:
            return ToolResult(ok=False, error="Avito account_id is missing")
        if not chat_id:
            return ToolResult(ok=False, error="chat_id is missing")
        if not image_path:
            return ToolResult(ok=False, error="image_path is missing")
        result = await self.avito_image_sender.send_image(account_id, chat_id, image_path)
        return ToolResult(ok=True, data={"account_id": account_id, "chat_id": chat_id, "image_path": image_path, "send_result": result})

    async def _execute_avito_messages_send_file(self, args: dict[str, Any]) -> ToolResult:
        sender = self.avito_image_sender or self.avito_sender
        if not sender:
            return ToolResult(ok=False, error="Avito sender is not configured")
        account_id = int(args.get("account_id") or self.avito_account_id or 0)
        chat_id = str(args.get("chat_id") or "")
        file_path = str(args.get("file_path") or args.get("path") or "")
        caption = str(args.get("caption") or "")
        if not account_id:
            return ToolResult(ok=False, error="Avito account_id is missing")
        if not chat_id:
            return ToolResult(ok=False, error="chat_id is missing")
        if not file_path:
            return ToolResult(ok=False, error="file_path is missing")
        if not hasattr(sender, "send_file"):
            return ToolResult(ok=False, error="Avito sender does not support generic files")
        result = await sender.send_file(account_id, chat_id, file_path, caption)
        return ToolResult(ok=True, data={"account_id": account_id, "chat_id": chat_id, "file_path": file_path, "caption": caption, "send_result": result})


def _tool_specs(*, enable_workspace_tools: bool = False) -> dict[str, ToolSpec]:
    external_guard = "Live YCLIENTS mutations stay blocked unless the configured gateway explicitly allows them."
    specs = {
        "yclients.company.address": ToolSpec(
            name="yclients.company.address",
            description=(
                "Read the company/branch address from YCLIENTS. This is the only allowed source for exact client-facing addresses and metro/location details."
            ),
            properties={"city": "City name, optional but preferred. Uses the configured company for that city."},
            external=True,
        ),
        "yclients.services.list": ToolSpec(
            name="yclients.services.list",
            description="List YCLIENTS services for a city. Prices marked placeholder must not be quoted as real client prices.",
            properties={"city": "City name, optional but preferred."},
            external=True,
        ),
        "yclients.slots.list": ToolSpec(
            name="yclients.slots.list",
            description=(
                "List free YCLIENTS slots for city and date, respecting the local city schedule. "
                "service_id is optional and is only echoed on returned slots; YCLIENTS returns book_times "
                "for the staff/date without filtering by service. If schedule_status is unknown, "
                "availability is unknown; ask Olga/handoff instead of saying there are no slots."
            ),
            required=("city", "date"),
            properties={"city": "City name.", "service_id": "YCLIENTS service id, optional.", "date": "Date in YYYY-MM-DD."},
            external=True,
        ),
        "yclients.appointments.create": ToolSpec(
            name="yclients.appointments.create",
            description="Create a YCLIENTS appointment after client confirmed slot and contact details.",
            required=("city", "service_id", "datetime", "phone"),
            properties={
                "city": "City name.",
                "service_id": "YCLIENTS service id.",
                "service_title": "Exact service title returned by yclients.services.list; include it when known for validation.",
                "datetime": "Appointment datetime in ISO format.",
                "phone": "Client phone.",
                "client_name": "Client name, optional.",
                "notes": "Operational notes, optional.",
                "staff_id": "YCLIENTS staff id, optional. Prefer the staff_id returned by yclients.slots.list for the selected slot.",
            },
            mutates=True,
            external=True,
            guardrail=external_guard,
        ),
        "yclients.appointments.list": ToolSpec(
            name="yclients.appointments.list",
            description="List appointments by date and optionally client id.",
            required=("date",),
            properties={"date": "Date in YYYY-MM-DD.", "client_id": "YCLIENTS client id, optional.", "city": "City/branch filter, optional."},
            external=True,
        ),
        "yclients.appointments.move": ToolSpec(
            name="yclients.appointments.move",
            description="Move an existing YCLIENTS appointment to another slot.",
            required=("appointment_id", "datetime", "city"),
            properties={
                "appointment_id": "YCLIENTS appointment id.",
                "datetime": "New appointment datetime in ISO format.",
                "city": "Current city/branch of the appointment.",
                "target_city": "Target city, optional. Cross-company moves are rejected; create and cancel instead.",
                "staff_id": "YCLIENTS staff id, optional.",
                "service_id": "YCLIENTS service id, optional.",
            },
            mutates=True,
            external=True,
            guardrail=external_guard,
        ),
        "yclients.appointments.cancel": ToolSpec(
            name="yclients.appointments.cancel",
            description="Cancel an existing YCLIENTS appointment.",
            required=("appointment_id", "city"),
            properties={"appointment_id": "YCLIENTS appointment id.", "city": "City/branch of the appointment."},
            mutates=True,
            external=True,
            guardrail=external_guard,
        ),
        "yclients.clients.search": ToolSpec(
            name="yclients.clients.search",
            description="Search YCLIENTS clients by phone, name or external id.",
            required=("query",),
            properties={"query": "Phone, name, or external client id.", "city": "City/branch filter, optional. Without it searches all configured companies."},
            external=True,
        ),
        "yclients.clients.notes.update": ToolSpec(
            name="yclients.clients.notes.update",
            description="Update YCLIENTS client notes and optional skin type.",
            required=("client_id", "notes", "city"),
            properties={
                "client_id": "Exact YCLIENTS client id returned by clients.search.",
                "city": "City/branch containing this client.",
                "notes": "Client notes.",
                "skin_type": "Skin type, optional.",
            },
            mutates=True,
            external=True,
            guardrail=external_guard,
        ),
        "care.tasks.plan": ToolSpec(
            name="care.tasks.plan",
            description="Plan care and upsell tasks from appointments and configured rules.",
            required=("start_date", "end_date"),
            properties={"start_date": "Start date in YYYY-MM-DD.", "end_date": "End date in YYYY-MM-DD.", "rules": "Rule list."},
            external=True,
        ),
        "care.crm.clients.search": ToolSpec(
            name="care.crm.clients.search",
            description=(
                "Search the local care CRM by client name, phone, or YCLIENTS client id. "
                "Use this before assuming whether a Telegram client is new or returning."
            ),
            required=("query",),
            properties={"query": "Client name, phone, Telegram-provided name, or YCLIENTS id.", "limit": "Max clients, 1-25."},
            guardrail="Read-only local CRM. Do not expose internal identifiers or notes to clients.",
        ),
        "care.crm.client.memory.get": ToolSpec(
            name="care.crm.client.memory.get",
            description=(
                "Build an agent-friendly memory card for a local CRM client: safe client facts, visits, preferences, links and follow-ups."
            ),
            required=("client_id",),
            properties={
                "client_id": "Local CRM client id.",
                "include_internal": "Boolean. Use true only for Olga/admin/internal roles.",
                "limit": "Max rows per memory section, 1-15.",
            },
            guardrail="Do not quote internal ids, Olga notes or raw tool data to clients.",
        ),
        "care.crm.client.get": ToolSpec(
            name="care.crm.client.get",
            description="Read one local care CRM client card by local client_id.",
            required=("client_id",),
            properties={"client_id": "Local CRM client id."},
            guardrail="Read-only local CRM. Do not expose internal identifiers or notes to clients.",
        ),
        "care.crm.visits.list": ToolSpec(
            name="care.crm.visits.list",
            description=(
                "List confirmed factual visits for a local CRM client. Prefer actual_service_title over booked_service_title."
            ),
            required=("client_id",),
            properties={"client_id": "Local CRM client id.", "limit": "Max visits, 1-25."},
            guardrail="Read-only local CRM facts. Use for personalization and care; do not reveal internal statuses to clients.",
        ),
        "care.crm.interactions.list": ToolSpec(
            name="care.crm.interactions.list",
            description="List internal care CRM interactions/notes for a local client. Internal planning only.",
            required=("client_id",),
            properties={"client_id": "Local CRM client id.", "limit": "Max interactions, 1-25."},
            guardrail="Internal read-only notes. Never quote or reveal to clients.",
        ),
        "care.crm.interactions.create": ToolSpec(
            name="care.crm.interactions.create",
            description="Create a local CRM interaction/note from the current client conversation.",
            required=("client_id", "body"),
            properties={
                "client_id": "Local CRM client id.",
                "body": "Client message or summarized fact.",
                "intent": "Intent label, e.g. client_message, complaint_or_risk, no_contact_request, preference.",
                "channel": "Channel, default telegram_client.",
                "direction": "Direction, default inbound_client.",
                "appointment_id": "Related local appointment id, optional.",
                "visit_id": "Related local visit id, optional.",
                "metadata": "Structured metadata, optional.",
            },
            mutates=True,
            guardrail="Local CRM only. Do not invent medical facts; record what the client said or a clear summary.",
        ),
        "care.crm.client.flags.update": ToolSpec(
            name="care.crm.client.flags.update",
            description="Update local CRM safety/contact flags such as consent_status, do_not_contact, or complaint_risk.",
            required=("client_id",),
            properties={
                "client_id": "Local CRM client id.",
                "consent_status": "unknown, asked, granted, denied.",
                "do_not_contact": "Boolean. Set true if client asks not to be contacted.",
                "complaint_risk": "Boolean. Set true for complaints, adverse reactions, risky symptoms, or reputation risk.",
            },
            mutates=True,
            guardrail="Local CRM only. If in doubt on medical risk, set complaint_risk=true and hand off.",
        ),
        "care.crm.followups.list": ToolSpec(
            name="care.crm.followups.list",
            description="List planned local CRM follow-up tasks for care and soft upsell. Does not send messages.",
            properties={
                "status": "Task status, default planned.",
                "due_before": "Only tasks due at or before this ISO datetime/date, optional.",
                "client_id": "Local CRM client id, optional.",
                "limit": "Max tasks, 1-100.",
            },
            guardrail="Read-only planned tasks. Do not send messages from this tool.",
        ),
        "care.crm.appointments.match": ToolSpec(
            name="care.crm.appointments.match",
            description=(
                "Find local CRM appointments by day and natural query. Use for Olga visit fact updates before deciding which appointment she means."
            ),
            properties={"query": "Client name, phone, service title, or YCLIENTS record id.", "day": "Date YYYY-MM-DD, optional.", "limit": "Max rows, 1-25."},
            guardrail="Read-only match. If more than one plausible row remains, ask Olga one short clarification.",
        ),
        "care.crm.visit.fact.upsert": ToolSpec(
            name="care.crm.visit.fact.upsert",
            description="Write the factual visit outcome to local CRM: attended/no-show, actual service, amount, product, reaction and notes.",
            required=("appointment_id",),
            properties={
                "appointment_id": "Local CRM appointment id.",
                "attended": "Boolean, default true.",
                "actual_service_title": "What was actually done; prefer factual service over booked title.",
                "amount_ml": "Volume in ml if relevant.",
                "units": "Units if relevant.",
                "product_or_drug": "Product/drug/preparation if Olga said it.",
                "procedure_notes": "Short factual procedure notes.",
                "reaction": "Client reaction/symptoms/result if Olga said it.",
                "aftercare_notes": "Aftercare notes if Olga said it.",
                "confirmed_by": "Who confirmed the fact.",
                "source_text": "Original Olga/admin phrase.",
            },
            mutates=True,
            guardrail="Local CRM only. Do not write paid/spent or financial YCLIENTS fields.",
        ),
        "care.crm.client.link": ToolSpec(
            name="care.crm.client.link",
            description="Link a local CRM client to a transport identity such as telegram_client, avito, vk or yclients.",
            required=("client_id", "channel"),
            properties={
                "client_id": "Local CRM client id.",
                "channel": "telegram_client, avito, vk, yclients, or other source label.",
                "external_user_id": "External user id, optional.",
                "chat_id": "External chat/dialog id, optional.",
                "username": "Username, optional.",
                "display_name": "Display name, optional.",
                "verified": "Boolean. True only when phone/Oльга/client confirmation makes it reliable.",
            },
            mutates=True,
            guardrail="Never merge/link uncertain people silently; use merge suggestions or ask Olga when identity is ambiguous.",
        ),
        "care.crm.client.merge.suggest": ToolSpec(
            name="care.crm.client.merge.suggest",
            description="Suggest possible duplicate local CRM clients. This does not merge anything.",
            properties={"query": "Optional search query.", "limit": "Max suggestions, 1-25."},
            guardrail="Suggestions only; if confidence is low, ask Olga before applying merge.",
        ),
        "care.crm.client.merge.apply": ToolSpec(
            name="care.crm.client.merge.apply",
            description="Merge one local CRM client into another after Olga/admin confirmation.",
            required=("source_client_id", "target_client_id"),
            properties={"source_client_id": "Duplicate client id to remove.", "target_client_id": "Canonical client id to keep.", "merged_by": "Who confirmed merge."},
            mutates=True,
            guardrail="Internal only. Do not expose merge mechanics to clients.",
        ),
        "care.learning.lesson.create": ToolSpec(
            name="care.learning.lesson.create",
            description="Save a durable lesson for future agent behavior: Olga wording preferences, follow-up style, service nuance, or safety rule.",
            required=("lesson",),
            properties={"lesson": "Plain-language lesson.", "source": "Where it came from.", "tags": "List of tags.", "confidence": "0-1.", "created_from_interaction_id": "Optional interaction id.", "metadata": "Optional structured context."},
            mutates=True,
            guardrail="Store behavioral lessons, not secrets or unsupported medical claims.",
        ),
        "care.learning.lessons.list": ToolSpec(
            name="care.learning.lessons.list",
            description="Read durable care/upsell lessons relevant to the current client, service, risk or follow-up.",
            properties={"query": "Text query.", "tags": "List of tags.", "limit": "Max rows, 1-100."},
            guardrail="Use lessons as internal guidance; do not quote them as policy to clients.",
        ),
        "care.learning.preference.upsert": ToolSpec(
            name="care.learning.preference.upsert",
            description="Save or update a client preference learned from conversation or Olga: channel, timing, tone, service interest, sensitivity.",
            required=("client_id", "preference_type", "value"),
            properties={"client_id": "Local CRM client id.", "preference_type": "Preference key.", "value": "Preference value.", "source": "Where it came from.", "confidence": "0-1.", "metadata": "Optional context."},
            mutates=True,
            guardrail="Record only useful care preferences; never invent private facts.",
        ),
        "care.learning.outcome.record": ToolSpec(
            name="care.learning.outcome.record",
            description="Record an agent decision outcome for later review and self-improvement.",
            properties={"agent_role": "Role name.", "input_ref": "Reference to task/message.", "decision": "Structured decision.", "tool_calls": "Tool calls used.", "outcome": "Outcome label."},
            mutates=True,
            guardrail="Internal telemetry only.",
        ),
        "schedule.city.list": ToolSpec(
            name="schedule.city.list",
            description="List Olga's city schedule and audit whether each date has a matching work schedule in YCLIENTS.",
            properties={"from_date": "Start date in YYYY-MM-DD, optional.", "city": "City filter, optional."},
        ),
        "schedule.city.set": ToolSpec(
            name="schedule.city.set",
            description="Set Olga's working city and synchronize her employee work schedule in YCLIENTS.",
            required=("city", "dates"),
            properties={
                "city": "City name.",
                "dates": "Date list or text with dates, e.g. ['2026-06-01'] or '1, 2, 5 июня'.",
                "from_time": "Optional workday start in HH:MM.",
                "to_time": "Optional workday end in HH:MM.",
            },
            mutates=True,
            guardrail="Updates YCLIENTS first and writes the local schedule only after successful synchronization. Use explicit hours when Olga gives them; otherwise city defaults apply.",
        ),
        "schedule.city.delete": ToolSpec(
            name="schedule.city.delete",
            description="Delete Olga's work schedule dates from YCLIENTS and then clear the matching local city schedule.",
            required=("dates",),
            properties={"dates": "Date list or text with dates to clear."},
            mutates=True,
            guardrail="Requires an existing local city for every date so the correct YCLIENTS company and employee are changed.",
        ),
        "avito.chats.list": ToolSpec(
            name="avito.chats.list",
            description="List latest Avito chats for the configured account. Read-only.",
            properties={"limit": "Max chats, 1-50.", "offset": "Pagination offset.", "account_id": "Optional Avito account id override."},
            external=True,
        ),
        "avito.messages.list": ToolSpec(
            name="avito.messages.list",
            description="List messages from an Avito chat by chat_id. Read-only.",
            required=("chat_id",),
            properties={"chat_id": "Avito chat id.", "limit": "Max messages, 1-100.", "offset": "Pagination offset.", "account_id": "Optional Avito account id override."},
            external=True,
        ),
        "avito.messages.send": ToolSpec(
            name="avito.messages.send",
            description="Send a text message to an Avito chat. Uses preview outbox unless AVITO_SEND_ENABLED=true.",
            required=("chat_id", "text"),
            properties={"chat_id": "Avito chat id.", "text": "Message text to send.", "account_id": "Optional Avito account id override."},
            mutates=True,
            external=True,
            guardrail="Avito live sends stay in preview outbox unless AVITO_SEND_ENABLED=true.",
        ),
        "avito.messages.send_phone": ToolSpec(
            name="avito.messages.send_phone",
            description="Send a confirmed phone/contact text to an Avito chat. Use only with a phone number from Olga/admin/knowledge.",
            required=("chat_id", "phone"),
            properties={"chat_id": "Avito chat id.", "phone": "Confirmed phone number.", "text": "Optional client-facing text.", "account_id": "Optional Avito account id override."},
            mutates=True,
            external=True,
            guardrail="Do not invent phone numbers. Avito live sends stay in preview outbox unless AVITO_SEND_ENABLED=true.",
        ),
        "avito.messages.send_image": ToolSpec(
            name="avito.messages.send_image",
            description="Send an image file to an Avito chat. Use image_path from Telegram admin attachments.",
            required=("chat_id", "image_path"),
            properties={"chat_id": "Avito chat id.", "image_path": "Local image file path previously downloaded from Telegram.", "account_id": "Optional Avito account id override."},
            mutates=True,
            external=True,
            guardrail="Sends a real Avito image when AVITO_IMAGE_SEND_ENABLED=true.",
        ),
        "avito.messages.send_file": ToolSpec(
            name="avito.messages.send_file",
            description="Send a local media file or attachment to an Avito chat. Images are supported live; videos/documents may be preview-only or unsupported by Avito API.",
            required=("chat_id", "file_path"),
            properties={"chat_id": "Avito chat id.", "file_path": "Local file path.", "caption": "Optional caption.", "account_id": "Optional Avito account id override."},
            mutates=True,
            external=True,
            guardrail="Use only confirmed local files/assets. Non-image Avito file support depends on API capability.",
        ),
        "knowledge.create": ToolSpec(
            name="knowledge.create",
            description="Create a bot-owned knowledge item.",
            required=("title", "content"),
            properties={"kind": "Knowledge kind.", "title": "Title.", "content": "Reusable answer or note.", "tags": "Tag list."},
            mutates=True,
        ),
        "knowledge.list": ToolSpec(
            name="knowledge.list",
            description="Search bot-owned knowledge.",
            properties={"query": "Search query.", "kind": "Knowledge kind.", "tags": "Tag list."},
        ),
        "knowledge.get": ToolSpec(
            name="knowledge.get",
            description="Read a bot-owned knowledge item by id.",
            required=("id",),
            properties={"id": "Knowledge item id."},
        ),
        "knowledge.update": ToolSpec(
            name="knowledge.update",
            description="Update a bot-owned knowledge item.",
            required=("id",),
            properties={"id": "Knowledge item id.", "title": "New title.", "content": "New content.", "tags": "Tag list."},
            mutates=True,
        ),
        "knowledge.delete": ToolSpec(
            name="knowledge.delete",
            description="Delete a bot-owned knowledge item.",
            required=("id",),
            properties={"id": "Knowledge item id."},
            mutates=True,
        ),
    }
    if enable_workspace_tools:
        specs.update(
            {
                "workspace.files.list": ToolSpec(
                    name="workspace.files.list",
                    description="List files inside the project workspace for diagnostics. Read-only.",
                    properties={"path": "Workspace-relative directory, default '.'.", "pattern": "Glob pattern, default '*'.", "max_results": "1-200."},
                    guardrail="Read-only diagnostics. Sensitive paths such as .env, auth, token, secret, key and MFA files are hidden.",
                ),
                "workspace.files.read": ToolSpec(
                    name="workspace.files.read",
                    description="Read a UTF-8 text file from the project workspace for diagnostics. Read-only.",
                    required=("path",),
                    properties={"path": "Workspace-relative file path.", "max_chars": "Maximum characters to return, 1-40000."},
                    guardrail="Read-only diagnostics. Refuses sensitive paths such as .env, auth, token, secret, key and MFA files.",
                ),
                "workspace.logs.tail": ToolSpec(
                    name="workspace.logs.tail",
                    description="Read the last lines of a project log file, default data/agent_trace.jsonl. Read-only.",
                    properties={"path": "Workspace-relative log path, default data/agent_trace.jsonl.", "lines": "Line count, 1-300."},
                    guardrail="Read-only diagnostics. Refuses sensitive log paths.",
                ),
                "workspace.command.run": ToolSpec(
                    name="workspace.command.run",
                    description="Run a short allowlisted read-only shell-style command in the project workspace.",
                    required=("command",),
                    properties={"command": "Allowed commands: pwd, ls, find, rg, sed, head, tail, cat, wc, date, stat, du.", "timeout_seconds": "1-20."},
                    guardrail="Read-only diagnostics only. No shell expansion, no writes, no secrets, no git state changes.",
                ),
                "workspace.python.run": ToolSpec(
                    name="workspace.python.run",
                    description="Run a short Python snippet for read-only project diagnostics.",
                    required=("code",),
                    properties={"code": "Python code. Keep it short and read-only.", "timeout_seconds": "1-20."},
                    guardrail="Read-only diagnostics only. Blocks imports/operations commonly used for writes, networking, subprocesses, and secrets.",
                ),
            }
        )
    return specs


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


SERVICE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("увеличение губ", ("губ",)),
    ("Корея", ("коре",)),
    ("ягодицы", ("ягод",)),
    ("грудь", ("груд",)),
    ("ботокс", ("ботокс",)),
    ("нити", ("нит", "cog", "ког")),
    ("носогубные складки", ("носогуб",)),
    ("кисетные морщины", ("кисет",)),
    ("скулы", ("скул",)),
    ("подбородок", ("подбород",)),
)


def _appointment_service_hint(args: dict[str, Any]) -> str:
    values = [
        args.get("service_title"),
        args.get("service_name"),
        args.get("service_query"),
        args.get("procedure"),
        args.get("notes"),
    ]
    return " ".join(str(value or "") for value in values).casefold().replace("ё", "е")


def _service_hint_mismatch(hint: str, selected_title: str) -> str:
    if not hint.strip():
        return ""
    title = str(selected_title or "").casefold().replace("ё", "е")
    mismatches: list[str] = []
    for label, needles in SERVICE_HINTS:
        if any(needle in hint for needle in needles) and not any(needle in title for needle in needles):
            mismatches.append(label)
    return ", ".join(mismatches)


def _appointment_from_args(args: dict[str, Any]) -> Appointment:
    service = Service(
        id=int(args.get("service_id") or 0),
        title=str(args.get("service_title") or ""),
        price=int(args.get("service_price") or 0),
        duration_minutes=int(args.get("duration_minutes") or 60),
    )
    client = ClientProfile(
        name=str(args.get("client_name") or "Клиент"),
        phone=str(args.get("phone") or ""),
        external_id=str(args.get("client_id") or ""),
        skin_type=str(args.get("skin_type") or ""),
        notes=str(args.get("client_notes") or ""),
        city=str(args.get("city") or ""),
    )
    raw = dict(args.get("raw") or {})
    if args.get("staff_id"):
        raw["staff_id"] = int(args.get("staff_id") or 0)
    return Appointment(
        client=client,
        service=service,
        city=str(args.get("city") or ""),
        starts_at=_parse_datetime_arg(args.get("datetime") or args.get("starts_at")),
        notes=str(args.get("notes") or ""),
        raw=raw,
    )


def _slot_from_args(args: dict[str, Any]) -> Slot:
    return Slot(
        city=str(args.get("target_city") or args.get("city") or ""),
        starts_at=_parse_datetime_arg(args.get("datetime") or args.get("starts_at")),
        staff_id=int(args.get("staff_id") or 0),
        service_id=int(args.get("service_id") or 0),
    )


def _parse_datetime_arg(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    raise ValueError("datetime is required")


def _optional_datetime_arg(value: Any) -> datetime | None:
    if not value:
        return None
    return _parse_datetime_arg(value)


def _upsell_rules_from_args(rows: list[dict[str, Any]]) -> list[UpsellRule]:
    rules: list[UpsellRule] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rules.append(
            UpsellRule(
                source_service=str(row.get("source_service") or ""),
                delay_days=int(row.get("delay_days") or 0),
                recommendation=str(row.get("recommendation") or ""),
                target_service=str(row.get("target_service") or ""),
                product_hint=str(row.get("product_hint") or ""),
                requires_skin_type=bool(row.get("requires_skin_type") or False),
            )
        )
    return rules


def _service_data(service: Service) -> dict[str, Any]:
    data = asdict(service)
    data["price_status"] = "placeholder" if 0 < int(service.price or 0) <= 1 else ("known" if service.price > 1 else "unknown")
    if data["price_status"] == "placeholder":
        data["client_price_hint"] = "Цена в YCLIENTS выглядит технической заглушкой; не называй ее клиенту как стоимость."
    return data


def _client_data(client: ClientProfile) -> dict[str, Any]:
    return asdict(client)


def _duplicate_client_names(clients: list[ClientProfile]) -> set[str]:
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for client in clients:
        normalized = " ".join(client.name.casefold().split())
        if not normalized:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
        display.setdefault(normalized, client.name)
    return {display[name] for name, count in counts.items() if count > 1}


def _crm_client_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "client_id": int(row.get("id") or 0),
        "yclients_client_id": str(row.get("yclients_client_id") or ""),
        "name": str(row.get("name") or ""),
        "phone": str(row.get("phone") or ""),
        "city": str(row.get("city") or ""),
        "skin_type": str(row.get("skin_type") or ""),
        "last_visit_at": str(row.get("last_visit_at") or ""),
        "consent_status": str(row.get("consent_status") or "unknown"),
        "do_not_contact": bool(row.get("do_not_contact") or False),
        "complaint_risk": bool(row.get("complaint_risk") or False),
    }


def _crm_visit_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "visit_id": int(row.get("id") or 0),
        "appointment_id": int(row.get("appointment_id") or 0),
        "scheduled_at": str(row.get("scheduled_at") or ""),
        "city": str(row.get("city") or ""),
        "actually_attended": bool(row.get("actually_attended") or False),
        "booked_service_title": str(row.get("booked_service_title") or ""),
        "actual_service_title": str(row.get("actual_service_title") or ""),
        "amount_ml": str(row.get("amount_ml") or ""),
        "units": str(row.get("units") or ""),
        "product_or_drug": str(row.get("product_or_drug") or ""),
        "procedure_notes": str(row.get("procedure_notes") or ""),
        "reaction": str(row.get("reaction") or ""),
        "aftercare_notes": str(row.get("aftercare_notes") or ""),
        "confirmed_at": str(row.get("confirmed_at") or ""),
    }


def _crm_appointment_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "appointment_id": int(row.get("id") or 0),
        "yclients_record_id": str(row.get("yclients_record_id") or ""),
        "client_id": int(row.get("client_id") or 0),
        "client_name": str(row.get("client_name") or ""),
        "client_phone": str(row.get("client_phone") or ""),
        "scheduled_at": str(row.get("scheduled_at") or ""),
        "city": str(row.get("city") or ""),
        "booked_service_id": int(row.get("booked_service_id") or 0),
        "booked_service_title": str(row.get("booked_service_title") or ""),
        "status": str(row.get("status") or ""),
        "confirmation_status": str(row.get("confirmation_status") or ""),
        "do_not_contact": bool(row.get("client_do_not_contact") or False),
        "complaint_risk": bool(row.get("client_complaint_risk") or False),
    }


def _crm_interaction_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "interaction_id": int(row.get("id") or 0),
        "appointment_id": int(row.get("appointment_id") or 0),
        "visit_id": int(row.get("visit_id") or 0),
        "channel": str(row.get("channel") or ""),
        "direction": str(row.get("direction") or ""),
        "author": str(row.get("author") or ""),
        "body": _truncate_text(str(row.get("body") or ""), 1200),
        "intent": str(row.get("intent") or ""),
        "created_at": str(row.get("created_at") or ""),
    }


def _crm_followup_task_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": int(row.get("id") or 0),
        "client_id": int(row.get("client_id") or 0),
        "visit_id": int(row.get("visit_id") or 0),
        "kind": str(row.get("kind") or ""),
        "due_at": str(row.get("due_at") or ""),
        "status": str(row.get("status") or ""),
        "channel": str(row.get("channel") or ""),
        "message_draft": str(row.get("message_draft") or ""),
        "reason": str(row.get("reason") or ""),
        "confidence": float(row.get("confidence") or 0.0),
        "risk_level": str(row.get("risk_level") or ""),
        "approved_by": str(row.get("approved_by") or ""),
        "approved_at": str(row.get("approved_at") or ""),
        "draft_source": str(row.get("draft_source") or ""),
        "outcome": str(row.get("outcome") or ""),
        "client_name": str(row.get("client_name") or ""),
        "client_phone": str(row.get("client_phone") or ""),
        "do_not_contact": bool(row.get("do_not_contact") or False),
        "complaint_risk": bool(row.get("complaint_risk") or False),
        "actual_service_title": str(row.get("actual_service_title") or ""),
        "visit_scheduled_at": str(row.get("scheduled_at") or ""),
        "city": str(row.get("city") or ""),
    }


def _crm_lesson_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "lesson_id": int(row.get("id") or 0),
        "lesson": str(row.get("lesson") or ""),
        "source": str(row.get("source") or ""),
        "tags": [tag for tag in str(row.get("tags") or "").split(",") if tag],
        "confidence": float(row.get("confidence") or 0.0),
        "created_from_interaction_id": int(row.get("created_from_interaction_id") or 0),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _crm_preference_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "preference_id": int(row.get("id") or 0),
        "client_id": int(row.get("client_id") or 0),
        "preference_type": str(row.get("preference_type") or ""),
        "value": str(row.get("value") or ""),
        "source": str(row.get("source") or ""),
        "confidence": float(row.get("confidence") or 0.0),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _slot_data(slot: Slot) -> dict[str, Any]:
    return {
        "city": slot.city,
        "starts_at": slot.starts_at.isoformat(),
        "staff_id": slot.staff_id,
        "service_id": slot.service_id,
    }


def _appointment_data(appointment: Appointment) -> dict[str, Any]:
    data = asdict(appointment)
    data["starts_at"] = appointment.starts_at.isoformat() if appointment.starts_at else None
    return data


def _cancellation_client_message(appointment: Appointment) -> str:
    details = []
    if appointment.service.title:
        details.append(appointment.service.title)
    if appointment.city:
        details.append(appointment.city)
    if appointment.starts_at:
        details.append(appointment.starts_at.strftime("%d.%m.%Y в %H:%M"))
    suffix = ": " + ", ".join(details) if details else ""
    return f"Запись{suffix} отменена."


def _cancellation_notification(appointment: Appointment) -> str:
    client = appointment.client
    lines = ["Запись в YCLIENTS отменена"]
    if appointment.id:
        lines.append(f"ID записи: {appointment.id}")
    if appointment.service.title:
        lines.append(f"Услуга: {appointment.service.title}")
    if appointment.city:
        lines.append(f"Город: {appointment.city}")
    if appointment.starts_at:
        lines.append(f"Дата и время: {appointment.starts_at.strftime('%d.%m.%Y %H:%M')}")
    if client.name:
        lines.append(f"Клиент: {client.name}")
    if client.phone:
        lines.append(f"Телефон: {client.phone}")
    if client.external_id:
        lines.append(f"YCLIENTS client_id: {client.external_id}")
    return "\n".join(lines)


def _knowledge_data(item: KnowledgeItem) -> dict[str, Any]:
    return asdict(item)


def _items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    result = payload.get("result")
    if isinstance(result, dict):
        return _items(result, *keys)
    return []


def _chat_data(chat: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(chat.get("id") or chat.get("chat_id") or chat.get("chatId") or ""),
        "updated_at": chat.get("updated") or chat.get("updated_at") or chat.get("last_message_created_at") or "",
        "created_at": chat.get("created") or chat.get("created_at") or "",
        "last_message": _message_data(chat.get("last_message") or {}) if isinstance(chat.get("last_message"), dict) else {},
        "users": chat.get("users") or [],
        "context": chat.get("context") or {},
        "raw": chat,
    }


def _message_data(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content") if isinstance(message.get("content"), dict) else {}
    text = content.get("text") if isinstance(content, dict) else ""
    if isinstance(text, dict):
        text = text.get("text")
    return {
        "id": str(message.get("id") or message.get("message_id") or ""),
        "author_id": str(message.get("author_id") or message.get("user_id") or ""),
        "created": message.get("created") or message.get("created_at") or message.get("timestamp") or "",
        "type": message.get("type") or message.get("direction") or "",
        "text": str(text or message.get("text") or "").strip(),
        "content": content,
    }


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _schedule_slots_from_args(args: dict[str, Any]) -> list[dict[str, str]] | None:
    starts_at = str(args.get("from_time") or "").strip()
    ends_at = str(args.get("to_time") or "").strip()
    if not starts_at and not ends_at:
        return None
    if not starts_at or not ends_at:
        raise ValueError("from_time and to_time must be provided together")
    try:
        start_time = datetime.strptime(starts_at, "%H:%M").time()
        end_time = datetime.strptime(ends_at, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("from_time and to_time must use HH:MM format") from exc
    if start_time >= end_time:
        raise ValueError("to_time must be later than from_time")
    return [{"from": start_time.strftime("%H:%M"), "to": end_time.strftime("%H:%M")}]


_ALLOWED_WORKSPACE_COMMANDS = frozenset({"pwd", "ls", "find", "rg", "sed", "head", "tail", "cat", "wc", "date", "stat", "du"})
_SENSITIVE_PATH_MARKERS = (
    ".env",
    ".codex",
    "auth.json",
    "mfa_totp",
    "token",
    "secret",
    "client_secret",
    "private",
    "credential",
    "key",
)
_BLOCKED_PYTHON_MARKERS = (
    "__import__",
    "eval(",
    "exec(",
    "compile(",
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "http.client",
    "ftplib",
    "shutil",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.rename",
    "os.replace",
    "pathlib.Path.write_text",
    "pathlib.Path.write_bytes",
    ".write_text(",
    ".write_bytes(",
    ".unlink(",
    ".rename(",
    ".replace(",
    ".mkdir(",
    "open(",
    ".env",
    "token",
    "secret",
    "mfa",
)


def _workspace_path(value: str) -> Path:
    if not value.strip():
        raise ValueError("path is required")
    raw_path = Path(value).expanduser()
    path = raw_path if raw_path.is_absolute() else ROOT / raw_path
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path must stay inside project workspace")
    return resolved


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _is_sensitive_path(path: Path) -> bool:
    lowered_parts = [part.casefold() for part in path.parts]
    lowered = str(path).casefold()
    return any(marker in lowered_parts or marker in lowered for marker in _SENSITIVE_PATH_MARKERS)


def _truncate_text(text: str, max_chars: int = 40000) -> str:
    return text[:max_chars]


async def _run_workspace_command(command: str, timeout_seconds: int) -> ToolResult:
    if not command.strip():
        return ToolResult(ok=False, error="command is required")
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return ToolResult(ok=False, error=f"cannot parse command: {exc}")
    if not args:
        return ToolResult(ok=False, error="command is required")
    executable = Path(args[0]).name
    if executable not in _ALLOWED_WORKSPACE_COMMANDS:
        return ToolResult(ok=False, error=f"command is not allowlisted: {executable}")
    for arg in args[1:]:
        if arg.startswith("-"):
            continue
        if any(marker in arg.casefold() for marker in _SENSITIVE_PATH_MARKERS):
            return ToolResult(ok=False, error="refusing command with sensitive path or token-like argument")
        if "/" in arg or arg.startswith("."):
            try:
                path = _workspace_path(arg)
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            if _is_sensitive_path(path):
                return ToolResult(ok=False, error="refusing command with sensitive path")
    completed = await _run_subprocess(args, timeout_seconds)
    return ToolResult(ok=completed["returncode"] == 0, data=completed, error=completed.get("stderr", "") if completed["returncode"] else "")


async def _run_workspace_python(code: str, timeout_seconds: int) -> ToolResult:
    if not code.strip():
        return ToolResult(ok=False, error="code is required")
    lowered = code.casefold()
    blocked = [marker for marker in _BLOCKED_PYTHON_MARKERS if marker.casefold() in lowered]
    if blocked:
        return ToolResult(ok=False, error=f"python snippet contains blocked read/write/security marker: {blocked[0]}")
    completed = await _run_subprocess([sys.executable, "-I", "-c", code], timeout_seconds)
    return ToolResult(ok=completed["returncode"] == 0, data=completed, error=completed.get("stderr", "") if completed["returncode"] else "")


async def _run_subprocess(args: list[str], timeout_seconds: int) -> dict[str, Any]:
    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )

    try:
        completed = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        return {"args": args, "returncode": 124, "stdout": "", "stderr": f"command timed out after {timeout_seconds}s"}
    return {
        "args": args,
        "returncode": completed.returncode,
        "stdout": _truncate_text(completed.stdout),
        "stderr": _truncate_text(completed.stderr),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
