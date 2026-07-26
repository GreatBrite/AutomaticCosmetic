from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from .models import Appointment, ClientProfile, Handoff, HandoffReason, InboundMessage, Service, Slot
from .avito import avito_photo_handoff
from .city_utils import cities_text, explicit_supported_city
from .config import DEFAULT_CITIES
from .yclients import YClientsGateway


PHONE_RE = re.compile(r"(?:\+7|8)?[\s(.-]*(\d{3})[\s).-]*(\d{3})[\s.-]*(\d{2})[\s.-]*(\d{2})")
DATE_ISO_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
DATE_DMY_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}))?\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.](\d{2})\b|\b(?:в\s*)?([01]?\d|2[0-3])\s*(?:час(?:а|ов)?|ч)\b")
def _cities_text(cities: tuple[str, ...]) -> str:
    return cities_text(cities)


@dataclass(frozen=True)
class BookingRequest:
    message: InboundMessage
    city: str = ""
    service_query: str = ""
    preferred_date: str = ""
    preferred_time: str = ""
    client_name: str = ""
    phone: str = ""
    notes: str = ""


@dataclass(frozen=True)
class BookingDecision:
    action: str
    reply: str
    state: str = ""
    handoff: Handoff | None = None
    slots: list[Slot] = field(default_factory=list)
    appointment_id: int | None = None
    service: Service | None = None


class AvitoBookingFlow:
    def __init__(
        self,
        booking: YClientsGateway,
        cities: tuple[str, ...] = DEFAULT_CITIES,
        *,
        allow_create: bool = True,
        slot_lookup: Callable[[str, int, str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.booking = booking
        self.cities = cities
        self.allow_create = allow_create
        self.slot_lookup = slot_lookup

    async def process(self, request: BookingRequest) -> BookingDecision:
        handoff = avito_photo_handoff(request.message)
        if handoff:
            return BookingDecision(
                action="ask_consultation_details",
                reply=(
                    "Уточните, пожалуйста: какая зона интересует, опишите зону и что хотите получить в результате. "
                    "Так Ольга сможет оценить фото точнее."
                ),
                state="requested_details",
            )

        city = request.city or self.extract_city(request.message.text)
        if not city:
            return BookingDecision(
                action="ask_city",
                reply=f"Приём ведём в фиксированных городах: {_cities_text(self.cities)}. В каком из них вам удобно записаться?",
                state="requested_slot",
            )

        services = await self.booking.get_services(city)
        service = self.match_service(request.service_query or request.message.text, services)
        if not service:
            available = ", ".join(service.title for service in services[:6]) or "список услуг уточняется"
            return BookingDecision(
                action="ask_service",
                reply=f"Какая процедура вас интересует? Сейчас доступны: {available}.",
                state="requested_slot",
            )

        if not request.preferred_date:
            return BookingDecision(
                action="ask_date",
                reply=f"На какую дату посмотреть свободное время в городе {city}?",
                state="requested_slot",
                service=service,
            )

        slot_result = await self._lookup_slots(city, service.id, request.preferred_date)
        schedule_status = str(slot_result.get("schedule_status") or "known")
        slots = _slots_from_lookup(slot_result)
        if schedule_status == "unknown":
            return BookingDecision(
                action="booking_schedule_unknown",
                reply="Проверю эту дату и вернусь с подтверждением.",
                state="awaiting_olga",
                handoff=Handoff(
                    reason=HandoffReason.BOOKING_AMBIGUOUS,
                    message=request.message,
                    summary=(
                        f"Клиент хочет записаться: {service.title}, {city}, {request.preferred_date}. "
                        "График Ольги на дату не задан; нельзя говорить, что мест нет. Нужно проверить дату и дать клиенту финальный ответ."
                    ),
                ),
                service=service,
            )
        if schedule_status != "known":
            schedule_city = str(slot_result.get("schedule_city") or "").strip()
            requested_city = str(slot_result.get("requested_city") or city).strip()
            if schedule_status == "known_wrong_city" and schedule_city:
                reply = f"На эту дату Ольга принимает в городе {schedule_city}, а не {requested_city}. Проверю варианты и вернусь с подтверждением."
                summary = (
                    f"Клиент хочет записаться: {service.title}, {requested_city}, {request.preferred_date}. "
                    f"График на дату задан для другого города: {schedule_city}. Нужно предложить корректный следующий шаг."
                )
            else:
                reply = "Проверю эту дату и вернусь с подтверждением."
                summary = (
                    f"Клиент хочет записаться: {service.title}, {city}, {request.preferred_date}. "
                    f"Статус графика: {schedule_status}; нельзя говорить, что мест нет без проверки."
                )
            return BookingDecision(
                action="booking_schedule_check_required",
                reply=reply,
                state="awaiting_olga",
                handoff=Handoff(reason=HandoffReason.BOOKING_AMBIGUOUS, message=request.message, summary=summary),
                service=service,
            )
        if not request.preferred_time:
            if not slots:
                return BookingDecision(
                    action="no_slots",
                    reply=f"На {request.preferred_date} свободного времени по услуге {service.title} не нашла. Предложить другой день?",
                    state="failed",
                    service=service,
                )
            times = ", ".join(slot.starts_at.strftime("%H:%M") for slot in slots[:6])
            return BookingDecision(
                action="offer_slots",
                reply=f"В городе {city} на {request.preferred_date} есть время: {times}. Какое удобно?",
                state="offered_slot",
                slots=slots,
                service=service,
            )

        selected_slot = self.match_slot(request.preferred_time, slots)
        if not selected_slot:
            times = ", ".join(slot.starts_at.strftime("%H:%M") for slot in slots[:6]) or "нет свободных слотов"
            return BookingDecision(
                action="ask_time",
                reply=f"Не вижу свободного времени {request.preferred_time}. Доступно: {times}.",
                state="requested_slot",
                slots=slots,
                service=service,
            )

        phone = request.phone or self.extract_phone(request.message.text)
        if not phone:
            return BookingDecision(
                action="ask_contact",
                reply="Пришлите, пожалуйста, имя для записи и номер телефона для связи.",
                state="offered_slot",
                slots=slots,
                service=service,
            )

        if not self.allow_create:
            return BookingDecision(
                action="booking_confirmation_required",
                reply=(
                    f"Вижу подходящее время: {service.title}, {city}, "
                    f"{selected_slot.starts_at.strftime('%d.%m %H:%M')}. "
                    "Сейчас проверю оформление записи и вернусь с подтверждением."
                ),
                state="awaiting_olga",
                handoff=Handoff(
                    reason=HandoffReason.BOOKING_AMBIGUOUS,
                    message=request.message,
                    summary=(
                        f"Клиент хочет записаться: {service.title}, {city}, "
                        f"{selected_slot.starts_at.strftime('%d.%m.%Y %H:%M')}, телефон {phone}. "
                        "Fallback Avito не создаёт live-запись автоматически; нужно подтвердить оформление."
                    ),
                ),
                slots=slots,
                service=service,
            )

        client = ClientProfile(name=request.client_name or "Клиент Авито", phone=phone, city=city)
        appointment = Appointment(
            client=client,
            service=service,
            city=city,
            starts_at=selected_slot.starts_at,
            notes=request.notes or f"Источник: Avito, chat_id={request.message.chat_id}",
        )
        appointment_id = await self.booking.create_appointment(appointment)
        return BookingDecision(
            action="created",
            reply=(
                f"Записала: {service.title}, {city}, "
                f"{selected_slot.starts_at.strftime('%d.%m %H:%M')}. "
                "Если что-то изменится, напишем."
            ),
            state="confirmed",
            appointment_id=appointment_id,
            service=service,
        )

    def extract_city(self, text: str) -> str:
        return explicit_supported_city(text, cities=self.cities)

    def match_service(self, text: str, services: list[Service]) -> Service | None:
        lowered = text.casefold()
        for service in services:
            if service.title.casefold() in lowered:
                return service
        for service in services:
            parts = [part for part in re.findall(r"[а-яёa-z0-9]{4,}", service.title.casefold()) if part not in {"лица", "услуга", "процедура"}]
            if parts and any(part in lowered for part in parts):
                return service
        return None

    def match_slot(self, preferred_time: str, slots: list[Slot]) -> Slot | None:
        target = preferred_time.strip()
        for slot in slots:
            if slot.starts_at.strftime("%H:%M") == target or slot.starts_at.strftime("%H") == target:
                return slot
        return None

    def extract_phone(self, text: str) -> str:
        match = PHONE_RE.search(text)
        if not match:
            return ""
        return "+7" + "".join(match.groups())

    async def _lookup_slots(self, city: str, service_id: int, preferred_date: str) -> dict[str, Any]:
        if self.slot_lookup:
            return await self.slot_lookup(city, service_id, preferred_date)
        slots = await self.booking.get_free_slots(city, service_id, preferred_date)
        return {"schedule_status": "known", "slots": slots}


def _slots_from_lookup(result: dict[str, Any]) -> list[Slot]:
    raw_slots = result.get("slots") if isinstance(result, dict) else []
    slots: list[Slot] = []
    for item in raw_slots or []:
        if isinstance(item, Slot):
            slots.append(item)
        elif isinstance(item, dict):
            starts_at = _parse_slot_datetime(item.get("starts_at"))
            if starts_at:
                slots.append(
                    Slot(
                        city=str(item.get("city") or ""),
                        starts_at=starts_at,
                        service_id=int(item.get("service_id") or 0),
                        staff_id=int(item.get("staff_id") or 0),
                    )
                )
    return slots


def _parse_slot_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def booking_request_from_message(message: InboundMessage, cities: tuple[str, ...] = DEFAULT_CITIES) -> BookingRequest:
    flow = AvitoBookingFlow(booking=_NoopBookingGateway(), cities=cities)
    return BookingRequest(
        message=message,
        city=flow.extract_city(message.text),
        service_query=message.text,
        preferred_date=extract_date(message.text),
        preferred_time=extract_time(message.text),
        phone=flow.extract_phone(message.text),
    )


def extract_date(text: str, today: date | None = None) -> str:
    current = today or date.today()
    lowered = text.casefold()
    if "послезавтра" in lowered:
        return (current + timedelta(days=2)).isoformat()
    if "завтра" in lowered:
        return (current + timedelta(days=1)).isoformat()
    match = DATE_ISO_RE.search(text)
    if match:
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            return ""
    match = DATE_DMY_RE.search(text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3) or current.year)
        try:
            candidate = date(year, month, day)
        except ValueError:
            return ""
        if not match.group(3) and candidate < current:
            try:
                candidate = date(year + 1, month, day)
            except ValueError:
                return ""
        return candidate.isoformat()
    return ""


def extract_time(text: str) -> str:
    match = TIME_RE.search(text)
    if not match:
        return ""
    hour = match.group(1) or match.group(3)
    minute = match.group(2) or "00"
    return f"{int(hour):02d}:{int(minute):02d}"


class _NoopBookingGateway:
    async def get_services(self, city: str = "") -> list[Service]:
        return []

    async def get_free_slots(self, city: str, service_id: int, date: str) -> list[Slot]:
        return []

    async def create_appointment(self, appointment: Appointment) -> int:
        return 0

    async def move_appointment(self, appointment_id: int, slot: Slot, city: str = "") -> Appointment:
        raise NotImplementedError

    async def cancel_appointment(self, appointment_id: int, city: str = "") -> Appointment | None:
        return None

    async def search_clients(self, query: str, city: str = "") -> list[ClientProfile]:
        return []

    async def update_client_notes(self, client_id: str, notes: str, skin_type: str = "", city: str = "") -> None:
        return None
