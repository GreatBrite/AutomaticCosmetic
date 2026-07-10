from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from .models import Appointment, ClientProfile, UpsellRule
from .yclients import YClientsGateway


BUY_WORDS = ("куп", "закаж", "беру", "хочу", "интересно", "сколько", "как оплат")
BOOK_WORDS = ("запис", "окош", "время", "когда можно", "хочу на")


@dataclass(frozen=True)
class CareTask:
    kind: str
    client: ClientProfile
    appointment: Appointment
    rule: UpsellRule
    due_at: datetime
    message: str
    handoff_to_cosmetologist: bool = False
    reason: str = ""


def due_upsell_rules(
    appointment: Appointment,
    rules: list[UpsellRule],
    *,
    now: datetime | None = None,
) -> list[UpsellRule]:
    if not appointment.starts_at:
        return []
    current = now or datetime.now(tz=appointment.starts_at.tzinfo)
    due: list[UpsellRule] = []
    service_title = appointment.service.title.casefold()
    skin_type = appointment.client.skin_type.strip()
    for rule in rules:
        if rule.source_service.casefold() not in service_title:
            continue
        if rule.requires_skin_type and not skin_type:
            continue
        if current >= appointment.starts_at + timedelta(days=rule.delay_days):
            due.append(rule)
    return due


class CareUpsellPlanner:
    """Plans post-visit care and repeat-service recommendations from YCLIENTS-shaped data."""

    def __init__(self, rules: list[UpsellRule]) -> None:
        self.rules = rules

    def tasks_for_appointment(self, appointment: Appointment, *, now: datetime | None = None) -> list[CareTask]:
        due_rules = due_upsell_rules(appointment, self.rules, now=now)
        return [self._task_from_rule(appointment, rule) for rule in due_rules]

    def tasks_for_appointments(self, appointments: list[Appointment], *, now: datetime | None = None) -> list[CareTask]:
        tasks: list[CareTask] = []
        for appointment in appointments:
            tasks.extend(self.tasks_for_appointment(appointment, now=now))
        return sorted(tasks, key=lambda task: task.due_at)

    def route_client_reply(self, task: CareTask, reply_text: str) -> CareTask:
        lowered = reply_text.casefold()
        wants_product = task.kind == "product_recommendation" and any(word in lowered for word in BUY_WORDS)
        wants_service = task.kind == "service_recommendation" and any(word in lowered for word in BUY_WORDS + BOOK_WORDS)
        if wants_product or wants_service:
            return CareTask(
                kind=task.kind,
                client=task.client,
                appointment=task.appointment,
                rule=task.rule,
                due_at=task.due_at,
                message=task.message,
                handoff_to_cosmetologist=True,
                reason="client_interested",
            )
        return task

    def _task_from_rule(self, appointment: Appointment, rule: UpsellRule) -> CareTask:
        due_at = _due_at(appointment, rule)
        kind = "product_recommendation" if rule.product_hint else "service_recommendation"
        return CareTask(
            kind=kind,
            client=appointment.client,
            appointment=appointment,
            rule=rule,
            due_at=due_at,
            message=_care_message(appointment, rule),
            reason="rule_due",
        )


class CareUpsellService:
    """Reads YCLIENTS appointments and builds due care tasks for a date range."""

    def __init__(self, booking: YClientsGateway, planner: CareUpsellPlanner) -> None:
        self.booking = booking
        self.planner = planner

    async def tasks_between(self, start_date: str, end_date: str, *, now: datetime | None = None) -> list[CareTask]:
        appointments: list[Appointment] = []
        for day in _date_range(start_date, end_date):
            appointments.extend(await self.booking.list_appointments(day.isoformat()))
        return self.planner.tasks_for_appointments(appointments, now=now)


def care_task_data(task: CareTask) -> dict:
    data = asdict(task)
    data["due_at"] = task.due_at.isoformat()
    data["appointment"]["starts_at"] = task.appointment.starts_at.isoformat() if task.appointment.starts_at else None
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def _due_at(appointment: Appointment, rule: UpsellRule) -> datetime:
    if not appointment.starts_at:
        raise ValueError("appointment.starts_at is required for care task")
    return appointment.starts_at + timedelta(days=rule.delay_days)


def _date_range(start_date: str, end_date: str) -> list[date]:
    start = date.fromisoformat(start_date[:10])
    end = date.fromisoformat(end_date[:10])
    if end < start:
        start, end = end, start
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _care_message(appointment: Appointment, rule: UpsellRule) -> str:
    name = appointment.client.name.strip()
    greeting = f"{name}, " if name else ""
    skin = appointment.client.skin_type.strip()
    if rule.product_hint:
        skin_part = f" с учетом типа кожи: {skin}" if skin else ""
        return f"{greeting}Ольга рекомендовала уход после процедуры «{appointment.service.title}»{skin_part}. {rule.recommendation}"
    target = rule.target_service or rule.recommendation
    return (
        f"{greeting}после процедуры «{appointment.service.title}» уже можно планировать следующий этап: "
        f"{target}. {rule.recommendation}"
    )
