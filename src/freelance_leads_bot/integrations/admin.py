from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .booking_flow import AvitoBookingFlow, extract_date, extract_time
from .config import DEFAULT_CITIES
from .models import Appointment, ClientProfile, Service, Slot
from .yclients import YClientsGateway


APPOINTMENT_ID_RE = re.compile(r"(?:запис[ьиь]?|record|id|#)\s*#?(\d+)", re.IGNORECASE)
CLIENT_ID_RE = re.compile(r"(?:клиент(?:а|у)?|client|id)\s*#?(\d+)", re.IGNORECASE)
SKIN_TYPE_RE = re.compile(
    r"(?:тип\s+кожи|кожа)\s*[:\-]?\s*([а-яёa-z -]+?)(?=,|;|\.|$|\s+(?:заметка|пометка|комментарий))",
    re.IGNORECASE,
)
NOTES_RE = re.compile(r"(?:заметка|пометка|комментарий)\s*[:\-]?\s*(.+)$", re.IGNORECASE)
NAME_RE = re.compile(
    r"(?:клиент(?:ка|а)?|для|имя)\s+([а-яёa-z][а-яёa-z -]{1,60}?)(?=,|;|$|\s+(?:телефон|номер|на|в|ростов|москва|спб|питер|санкт|краснодар|чистка|пилинг|консультац))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AdminCommand:
    action: str
    raw_text: str
    city: str = ""
    service_query: str = ""
    preferred_date: str = ""
    preferred_time: str = ""
    client_name: str = ""
    phone: str = ""
    appointment_id: int | None = None
    client_id: str = ""
    client_query: str = ""
    notes: str = ""
    skin_type: str = ""


@dataclass(frozen=True)
class AdminResult:
    action: str
    ok: bool
    message: str
    appointment_id: int | None = None
    appointment: Appointment | None = None
    client_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TelegramAdminService:
    """Text-command core for the cosmetologist admin bot."""

    def __init__(self, booking: YClientsGateway, cities: tuple[str, ...] = DEFAULT_CITIES) -> None:
        self.booking = booking
        self.cities = cities
        self.flow = AvitoBookingFlow(booking, cities=cities)

    async def handle_text(self, text: str) -> AdminResult:
        command = parse_admin_command(text, cities=self.cities)
        if command.action == "create_appointment":
            return await self.create_appointment(command)
        if command.action == "move_appointment":
            return await self.move_appointment(command)
        if command.action == "cancel_appointment":
            return await self.cancel_appointment(command)
        if command.action == "update_client_notes":
            return await self.update_client_notes(command)
        return AdminResult(
            action="unknown",
            ok=False,
            message=(
                "Поняла текст, но не поняла действие. Можно написать: "
                "добавь запись, перенеси запись, отмени запись или обнови клиента."
            ),
        )

    async def create_appointment(self, command: AdminCommand) -> AdminResult:
        missing = _missing_fields(
            command,
            required=("city", "preferred_date", "preferred_time", "phone", "service_query"),
        )
        if missing:
            return AdminResult(
                action=command.action,
                ok=False,
                message=f"Для записи не хватает: {', '.join(missing)}.",
            )

        services = await self.booking.get_services(command.city)
        service = self.flow.match_service(command.service_query, services)
        if not service:
            available = ", ".join(item.title for item in services[:6]) or "услуги из YCLIENTS не найдены"
            return AdminResult(
                action=command.action,
                ok=False,
                message=f"Не нашла услугу в YCLIENTS. Доступно: {available}.",
            )

        slots = await self.booking.get_free_slots(command.city, service.id, command.preferred_date)
        slot = self.flow.match_slot(command.preferred_time, slots)
        if not slot:
            times = ", ".join(item.starts_at.strftime("%H:%M") for item in slots[:8]) or "свободных слотов нет"
            return AdminResult(
                action=command.action,
                ok=False,
                message=f"Не вижу свободного времени {command.preferred_time}. Доступно: {times}.",
            )

        appointment = Appointment(
            client=ClientProfile(name=command.client_name or "Клиент", phone=command.phone, city=command.city),
            service=service,
            city=command.city,
            starts_at=slot.starts_at,
            notes=command.notes or "Создано через Telegram admin bot",
        )
        appointment_id = await self.booking.create_appointment(appointment)
        return AdminResult(
            action=command.action,
            ok=True,
            message=(
                f"Готово, запись #{appointment_id}: {appointment.client.name}, "
                f"{service.title}, {command.city}, {slot.starts_at.strftime('%d.%m %H:%M')}."
            ),
            appointment_id=appointment_id,
            appointment=appointment,
        )

    async def move_appointment(self, command: AdminCommand) -> AdminResult:
        if not command.appointment_id:
            return AdminResult(action=command.action, ok=False, message="Укажите номер записи, которую нужно перенести.")
        if not command.preferred_date or not command.preferred_time:
            return AdminResult(action=command.action, ok=False, message="Укажите новую дату и время записи.")

        slot = Slot(
            city=command.city,
            starts_at=_combine_datetime(command.preferred_date, command.preferred_time),
        )
        appointment = await self.booking.move_appointment(command.appointment_id, slot, command.city)
        return AdminResult(
            action=command.action,
            ok=True,
            message=f"Готово, перенесла запись #{command.appointment_id} на {slot.starts_at.strftime('%d.%m %H:%M')}.",
            appointment_id=command.appointment_id,
            appointment=appointment,
        )

    async def cancel_appointment(self, command: AdminCommand) -> AdminResult:
        if not command.appointment_id:
            return AdminResult(action=command.action, ok=False, message="Укажите номер записи, которую нужно отменить.")
        cancelled = await self.booking.cancel_appointment(command.appointment_id, command.city)
        if not cancelled:
            return AdminResult(action=command.action, ok=False, message=f"Запись #{command.appointment_id} не найдена.")
        return AdminResult(
            action=command.action,
            ok=True,
            message=f"Готово, отменила запись #{command.appointment_id}.",
            appointment_id=command.appointment_id,
        )

    async def update_client_notes(self, command: AdminCommand) -> AdminResult:
        client_id = command.client_id
        if not client_id:
            query = command.client_query or command.phone or command.client_name
            clients = await self.booking.search_clients(query, command.city)
            if len(clients) != 1:
                return AdminResult(
                    action=command.action,
                    ok=False,
                    message="Не смогла однозначно найти клиента. Укажите id клиента или телефон.",
                )
            client_id = clients[0].external_id

        await self.booking.update_client_notes(client_id, command.notes, command.skin_type, command.city)
        parts = [f"тип кожи: {command.skin_type}" if command.skin_type else "", command.notes]
        summary = "; ".join(part for part in parts if part) or "пометка без текста"
        return AdminResult(
            action=command.action,
            ok=True,
            message=f"Готово, обновила клиента #{client_id}: {summary}.",
            client_id=client_id,
        )


def parse_admin_command(text: str, cities: tuple[str, ...] = DEFAULT_CITIES) -> AdminCommand:
    lowered = text.casefold()
    flow = AvitoBookingFlow(booking=_NoopGateway(), cities=cities)
    action = _detect_action(lowered)
    appointment_id = _extract_appointment_id(text) if action in {"move_appointment", "cancel_appointment"} else None
    skin_type = _extract_first(SKIN_TYPE_RE, text)
    notes = _extract_first(NOTES_RE, text)
    phone = flow.extract_phone(text)
    client_name = _extract_first(NAME_RE, text).strip(" ,")
    client_id = _extract_first(CLIENT_ID_RE, text) if action == "update_client_notes" else ""

    return AdminCommand(
        action=action,
        raw_text=text,
        city=flow.extract_city(text),
        service_query=text,
        preferred_date=extract_date(text),
        preferred_time=extract_time(text),
        client_name=client_name,
        phone=phone,
        appointment_id=appointment_id,
        client_id=client_id,
        client_query=phone or client_name,
        notes=notes,
        skin_type=skin_type,
    )


def _detect_action(lowered: str) -> str:
    if any(word in lowered for word in ("отмени", "удали", "сними запись", "отмена")):
        return "cancel_appointment"
    if any(word in lowered for word in ("перенеси", "перенос", "передвинь", "измени время")):
        return "move_appointment"
    if any(word in lowered for word in ("тип кожи", "кожа", "заметка", "пометка", "комментарий")) and "клиент" in lowered:
        return "update_client_notes"
    if any(word in lowered for word in ("запиши", "добавь запись", "создай запись", "новая запись")):
        return "create_appointment"
    return "unknown"


def _extract_appointment_id(text: str) -> int | None:
    match = APPOINTMENT_ID_RE.search(text)
    return int(match.group(1)) if match else None


def _extract_first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _missing_fields(command: AdminCommand, required: tuple[str, ...]) -> list[str]:
    labels = {
        "city": "город",
        "preferred_date": "дата",
        "preferred_time": "время",
        "phone": "телефон",
        "service_query": "услуга",
    }
    return [labels[field] for field in required if not getattr(command, field)]


def _combine_datetime(date_value: str, time_value: str) -> datetime:
    return datetime.fromisoformat(f"{date_value[:10]}T{time_value}:00")


class _NoopGateway:
    async def get_services(self, city: str = "") -> list[Service]:
        return []

    async def get_free_slots(self, city: str, service_id: int, date: str) -> list[Slot]:
        return []

    async def create_appointment(self, appointment: Appointment) -> int:
        return 0
