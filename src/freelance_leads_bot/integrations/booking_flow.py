from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .models import Appointment, ClientProfile, Handoff, InboundMessage, Service, Slot
from .avito import avito_photo_handoff
from .config import DEFAULT_CITIES
from .yclients import YClientsGateway


PHONE_RE = re.compile(r"(?:\+7|8)?[\s(.-]*(\d{3})[\s).-]*(\d{3})[\s.-]*(\d{2})[\s.-]*(\d{2})")
DATE_ISO_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
DATE_DMY_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}))?\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.](\d{2})\b|\b(?:в\s*)?([01]?\d|2[0-3])\s*(?:час(?:а|ов)?|ч)\b")
CITY_ALIASES = {
    "Москва": ("москва", "москве", "москву", "москвы", "мск"),
    "Ростов-на-Дону": ("ростов", "ростове", "ростова", "ростову"),
    "Санкт-Петербург": ("санкт-петербург", "петербург", "петербурге", "питер", "питере", "спб"),
    "Краснодар": ("краснодар", "краснодаре", "краснодара", "краснодару"),
    "Геленджик": ("геленджик", "геленджике", "гелик"),
}


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
    handoff: Handoff | None = None
    slots: list[Slot] = field(default_factory=list)
    appointment_id: int | None = None
    service: Service | None = None


class AvitoBookingFlow:
    def __init__(self, booking: YClientsGateway, cities: tuple[str, ...] = DEFAULT_CITIES) -> None:
        self.booking = booking
        self.cities = cities

    async def process(self, request: BookingRequest) -> BookingDecision:
        handoff = avito_photo_handoff(request.message)
        if handoff:
            return BookingDecision(
                action="handoff",
                reply="Спасибо, фото посмотрят индивидуально и вернёмся с ответом.",
                handoff=handoff,
            )

        city = request.city or self.extract_city(request.message.text)
        if not city:
            return BookingDecision(
                action="ask_city",
                reply="Подскажите, пожалуйста, в каком городе вам удобно записаться?",
            )

        services = await self.booking.get_services(city)
        service = self.match_service(request.service_query or request.message.text, services)
        if not service:
            available = ", ".join(service.title for service in services[:6]) or "список услуг уточняется"
            return BookingDecision(
                action="ask_service",
                reply=f"Какая процедура вас интересует? Сейчас доступны: {available}.",
            )

        if not request.preferred_date:
            return BookingDecision(
                action="ask_date",
                reply=f"На какую дату посмотреть свободное время в городе {city}?",
                service=service,
            )

        slots = await self.booking.get_free_slots(city, service.id, request.preferred_date)
        if not request.preferred_time:
            if not slots:
                return BookingDecision(
                    action="no_slots",
                    reply=f"На {request.preferred_date} свободного времени по услуге {service.title} не нашла. Предложить другой день?",
                    service=service,
                )
            times = ", ".join(slot.starts_at.strftime("%H:%M") for slot in slots[:6])
            return BookingDecision(
                action="offer_slots",
                reply=f"В городе {city} на {request.preferred_date} есть время: {times}. Какое удобно?",
                slots=slots,
                service=service,
            )

        selected_slot = self.match_slot(request.preferred_time, slots)
        if not selected_slot:
            times = ", ".join(slot.starts_at.strftime("%H:%M") for slot in slots[:6]) or "нет свободных слотов"
            return BookingDecision(
                action="ask_time",
                reply=f"Не вижу свободного времени {request.preferred_time}. Доступно: {times}.",
                slots=slots,
                service=service,
            )

        phone = request.phone or self.extract_phone(request.message.text)
        if not phone:
            return BookingDecision(
                action="ask_contact",
                reply="Пришлите, пожалуйста, имя для записи и номер телефона для связи.",
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
            appointment_id=appointment_id,
            service=service,
        )

    def extract_city(self, text: str) -> str:
        lowered = text.casefold()
        for city in self.cities:
            if city.casefold() in lowered:
                return city
        configured = set(self.cities)
        for city, aliases in CITY_ALIASES.items():
            if city in configured and any(alias in lowered for alias in aliases):
                return city
        return ""

    def match_service(self, text: str, services: list[Service]) -> Service | None:
        lowered = text.casefold()
        for service in services:
            if service.title.casefold() in lowered:
                return service
        for service in services:
            if any(part and part in lowered for part in service.title.casefold().split()):
                return service
        return services[0] if len(services) == 1 else None

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
        return match.group(1)
    match = DATE_DMY_RE.search(text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3) or current.year)
        candidate = date(year, month, day)
        if not match.group(3) and candidate < current:
            candidate = date(year + 1, month, day)
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
