from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Channel(str, Enum):
    AVITO = "avito"
    TELEGRAM_ADMIN = "telegram_admin"
    TELEGRAM_CLIENT = "telegram_client"
    VK = "vk"


class HandoffReason(str, Enum):
    PHOTO_CONSULTATION = "photo_consultation"
    HUMAN_REQUESTED = "human_requested"
    BOOKING_AMBIGUOUS = "booking_ambiguous"
    COMPLAINT_OR_RISK = "complaint_or_risk"
    MISSING_DATA = "missing_data"


@dataclass(frozen=True)
class ClientProfile:
    name: str = ""
    phone: str = ""
    external_id: str = ""
    company_id: int = 0
    skin_type: str = ""
    notes: str = ""
    city: str = ""


@dataclass(frozen=True)
class Service:
    id: int = 0
    title: str = ""
    price: int = 0
    duration_minutes: int = 0
    city: str = ""


@dataclass(frozen=True)
class Appointment:
    id: int = 0
    client: ClientProfile = field(default_factory=ClientProfile)
    service: Service = field(default_factory=Service)
    city: str = ""
    starts_at: datetime | None = None
    status: str = ""
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Slot:
    city: str
    starts_at: datetime
    staff_id: int = 0
    service_id: int = 0


@dataclass(frozen=True)
class AvitoListingContext:
    item_id: int | None = None
    title: str = ""
    url: str = ""
    price_string: str = ""
    city: str = ""

    @property
    def has_listing(self) -> bool:
        return bool(self.item_id or self.title or self.url)

    def to_prompt_context(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "url": self.url,
            "price_string": self.price_string,
            "city": self.city,
        }


@dataclass(frozen=True)
class InboundMessage:
    channel: Channel
    client_id: str
    chat_id: str = ""
    message_id: str = ""
    text: str = ""
    created_at: int = 0
    has_photo: bool = False
    listing: AvitoListingContext | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Handoff:
    reason: HandoffReason
    message: InboundMessage
    summary: str = ""


@dataclass(frozen=True)
class UpsellRule:
    source_service: str
    delay_days: int
    recommendation: str
    target_service: str = ""
    product_hint: str = ""
    requires_skin_type: bool = False
