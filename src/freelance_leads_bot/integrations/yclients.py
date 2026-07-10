from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import json
import logging
from typing import Any, Protocol

import httpx

from .config import IntegrationSettings
from .models import Appointment, ClientProfile, Service, Slot


LIVE_API_BASE = "https://api.yclients.com"
logger = logging.getLogger(__name__)


class YClientsGateway(Protocol):
    async def get_company_address(self, city: str = "") -> dict[str, Any]:
        ...

    async def get_services(self, city: str = "") -> list[Service]:
        ...

    async def get_free_slots(self, city: str, service_id: int, date: str) -> list[Slot]:
        ...

    async def create_appointment(self, appointment: Appointment) -> int:
        ...

    async def move_appointment(self, appointment_id: int, slot: Slot, city: str = "") -> Appointment:
        ...

    async def cancel_appointment(self, appointment_id: int, city: str = "") -> Appointment | None:
        ...

    async def list_appointments(self, date: str, client_id: str = "", city: str = "") -> list[Appointment]:
        ...

    async def search_clients(self, query: str, city: str = "") -> list[ClientProfile]:
        ...

    async def update_client_notes(self, client_id: str, notes: str, skin_type: str = "", city: str = "") -> None:
        ...

    async def get_staff_schedule(self, city: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        ...

    async def set_staff_schedule(
        self,
        city: str,
        dates: list[str],
        slots: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        ...

    async def delete_staff_schedule(self, city: str, dates: list[str]) -> dict[str, Any]:
        ...


class DryRunYClientsGateway:
    """In-memory YCLIENTS-shaped gateway for safe booking flow development."""

    def __init__(
        self,
        *,
        services: list[Service] | None = None,
        slots: list[Slot] | None = None,
        clients: list[ClientProfile] | None = None,
    ) -> None:
        self.services = services or [
            Service(id=1, title="Чистка лица", price=3500, duration_minutes=90),
            Service(id=2, title="Пилинг", price=3000, duration_minutes=60),
            Service(id=3, title="Консультация косметолога", price=0, duration_minutes=30),
        ]
        self.slots = slots or self._default_slots()
        self.clients = clients or []
        self.appointments: list[Appointment] = []
        self.client_notes: dict[str, tuple[str, str]] = {}
        self.staff_schedules: dict[tuple[str, str], list[dict[str, str]]] = {}
        self._next_appointment_id = 1

    async def get_company_address(self, city: str = "") -> dict[str, Any]:
        for service in self.services:
            if city and service.city == city:
                return {"city": city, "address": "", "source": "dry_run"}
        return {"city": city, "address": "", "source": "dry_run"}

    async def get_services(self, city: str = "") -> list[Service]:
        if not city:
            return list(self.services)
        return [service for service in self.services if not service.city or service.city == city]

    async def get_free_slots(self, city: str, service_id: int, date: str) -> list[Slot]:
        return [
            slot
            for slot in self.slots
            if slot.city == city
            and slot.service_id in {0, service_id}
            and slot.starts_at.date().isoformat() == date
            and not self._is_taken(slot)
        ]

    async def create_appointment(self, appointment: Appointment) -> int:
        appointment_id = self._next_appointment_id
        self._next_appointment_id += 1
        self.appointments.append(replace(appointment, id=appointment_id, status=appointment.status or "dry_run"))
        return appointment_id

    async def move_appointment(self, appointment_id: int, slot: Slot, city: str = "") -> Appointment:
        for index, appointment in enumerate(self.appointments):
            if appointment.id == appointment_id:
                updated = replace(appointment, starts_at=slot.starts_at, city=slot.city or appointment.city)
                self.appointments[index] = updated
                return updated
        raise KeyError(f"appointment {appointment_id} not found")

    async def cancel_appointment(self, appointment_id: int, city: str = "") -> Appointment | None:
        for appointment in self.appointments:
            if appointment.id == appointment_id and (not city or appointment.city == city):
                self.appointments = [item for item in self.appointments if item.id != appointment_id]
                return appointment
        return None

    async def list_appointments(self, date: str, client_id: str = "", city: str = "") -> list[Appointment]:
        day = date[:10]
        return [
            appointment
            for appointment in self.appointments
            if appointment.starts_at
            and appointment.starts_at.date().isoformat() == day
            and (not client_id or appointment.client.external_id == client_id)
            and (not city or appointment.city == city)
        ]

    async def search_clients(self, query: str, city: str = "") -> list[ClientProfile]:
        needle = query.casefold().strip()
        if not needle:
            return []
        return [
            client
            for client in self.clients
            if needle in client.name.casefold() or needle in client.phone or needle in client.external_id.casefold()
            if not city or client.city == city
        ]

    async def update_client_notes(self, client_id: str, notes: str, skin_type: str = "", city: str = "") -> None:
        matches = [client for client in self.clients if client.external_id == str(client_id) and (not city or client.city == city)]
        if len(matches) > 1:
            raise RuntimeError(f"YCLIENTS client is ambiguous: client_id={client_id!r} city={city!r}")
        self.client_notes[str(client_id)] = (notes, skin_type)

    async def get_staff_schedule(self, city: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        canonical = _canonical_city_name(city)
        return [
            {"staff_id": 1, "date": schedule_date, "slots": [dict(slot) for slot in slots]}
            for (schedule_city, schedule_date), slots in sorted(self.staff_schedules.items())
            if schedule_city == canonical and start_date[:10] <= schedule_date <= end_date[:10]
        ]

    async def set_staff_schedule(
        self,
        city: str,
        dates: list[str],
        slots: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        canonical = _canonical_city_name(city)
        normalized_dates = _schedule_dates(dates)
        effective_slots = _normalize_schedule_slots(slots or _default_schedule_slots(canonical))
        for schedule_date in normalized_dates:
            self.staff_schedules[(canonical, schedule_date)] = [dict(slot) for slot in effective_slots]
        return {
            "city": canonical,
            "dates": normalized_dates,
            "slots": effective_slots,
            "updated_count": len(normalized_dates),
            "source": "dry_run",
        }

    async def delete_staff_schedule(self, city: str, dates: list[str]) -> dict[str, Any]:
        canonical = _canonical_city_name(city)
        normalized_dates = _schedule_dates(dates)
        deleted_count = 0
        for schedule_date in normalized_dates:
            if self.staff_schedules.pop((canonical, schedule_date), None) is not None:
                deleted_count += 1
        return {
            "city": canonical,
            "dates": normalized_dates,
            "deleted_count": deleted_count,
            "source": "dry_run",
        }

    def _is_taken(self, slot: Slot) -> bool:
        return any(appointment.starts_at == slot.starts_at and appointment.city == slot.city for appointment in self.appointments)

    def _default_slots(self) -> list[Slot]:
        tomorrow = datetime.now().date() + timedelta(days=1)
        cities = ("Ростов-на-Дону", "Москва", "Санкт-Петербург", "Краснодар", "Геленджик")
        slots: list[Slot] = []
        for city_index, city in enumerate(cities, start=1):
            for hour in (11, 14, 17):
                slots.append(
                    Slot(
                        city=city,
                        starts_at=datetime.combine(tomorrow, time(hour=hour)),
                        staff_id=1,
                        service_id=0,
                    )
                )
        return slots


class LiveReadDryRunYClientsGateway:
    """Read live YCLIENTS data but keep all mutations in memory."""

    def __init__(self, read_gateway: YClientsGateway, dry_run_gateway: DryRunYClientsGateway | None = None) -> None:
        self.read_gateway = read_gateway
        self.dry_run_gateway = dry_run_gateway or DryRunYClientsGateway()

    async def get_company_address(self, city: str = "") -> dict[str, Any]:
        return await self.read_gateway.get_company_address(city)

    async def get_services(self, city: str = "") -> list[Service]:
        return await self.read_gateway.get_services(city)

    async def get_free_slots(self, city: str, service_id: int, date: str) -> list[Slot]:
        return await self.read_gateway.get_free_slots(city, service_id, date)

    async def create_appointment(self, appointment: Appointment) -> int:
        return await self.dry_run_gateway.create_appointment(appointment)

    async def move_appointment(self, appointment_id: int, slot: Slot, city: str = "") -> Appointment:
        return await self.dry_run_gateway.move_appointment(appointment_id, slot, city)

    async def cancel_appointment(self, appointment_id: int, city: str = "") -> Appointment | None:
        return await self.dry_run_gateway.cancel_appointment(appointment_id, city)

    async def list_appointments(self, date: str, client_id: str = "", city: str = "") -> list[Appointment]:
        return await self.read_gateway.list_appointments(date, client_id, city)

    async def search_clients(self, query: str, city: str = "") -> list[ClientProfile]:
        return await self.read_gateway.search_clients(query, city)

    async def update_client_notes(self, client_id: str, notes: str, skin_type: str = "", city: str = "") -> None:
        await self.dry_run_gateway.update_client_notes(client_id, notes, skin_type, city)

    async def get_staff_schedule(self, city: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return await self.read_gateway.get_staff_schedule(city, start_date, end_date)

    async def set_staff_schedule(
        self,
        city: str,
        dates: list[str],
        slots: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return await self.dry_run_gateway.set_staff_schedule(city, dates, slots)

    async def delete_staff_schedule(self, city: str, dates: list[str]) -> dict[str, Any]:
        return await self.dry_run_gateway.delete_staff_schedule(city, dates)


class YClientsMutationDisabled(RuntimeError):
    pass


class YClientsHttpGateway:
    """YCLIENTS API gateway with read-only live calls and guarded mutations."""

    def __init__(
        self,
        settings: IntegrationSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = LIVE_API_BASE,
        allow_mutations: bool | None = None,
    ) -> None:
        self.settings = settings or IntegrationSettings.from_env()
        self.company_id = self.settings.yclients_company_id
        self.allow_mutations = self.settings.yclients_allow_mutations if allow_mutations is None else allow_mutations
        self._headers = {
            "Accept": "application/vnd.yclients.v2+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.yclients_api_key}, User {self.settings.yclients_user_token}",
        }
        self._base_url = base_url
        self._timeout = 30.0
        self.client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()

    async def get_services(self, city: str = "") -> list[Service]:
        self._require_ready()
        company_id = self._company_id_for_city(city)
        data = await self._get_json(f"/api/v1/book_services/{company_id}", "book_services")
        payload = data.get("data") or {}
        rows = payload.get("services") or []
        if not rows:
            data = await self._get_json(f"/api/v1/company/{company_id}/services", "company services")
            rows = data.get("data") or []
        return sorted((self._service_from_payload(row) for row in rows), key=lambda item: item.title)

    async def get_company_address(self, city: str = "") -> dict[str, Any]:
        self._require_ready()
        company_id = self._company_id_for_city(city)
        data = await self._get_json(f"/api/v1/company/{company_id}", "company")
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "company_id": company_id,
            "title": str(payload.get("public_title") or payload.get("title") or ""),
            "city": str(payload.get("city") or city or ""),
            "address": str(payload.get("address") or "").strip(),
            "coordinate_lat": payload.get("coordinate_lat"),
            "coordinate_lon": payload.get("coordinate_lon"),
            "timezone_name": str(payload.get("timezone_name") or ""),
            "source": "yclients.company",
        }

    async def get_free_slots(self, city: str, service_id: int, date: str) -> list[Slot]:
        self._require_ready()
        company_id = self._company_id_for_city(city)
        staff_id = await self._staff_id_for_city(company_id, city)
        if staff_id is None:
            return []
        data = await self._get_json(f"/api/v1/book_times/{company_id}/{staff_id}/{date[:10]}", "book_times")
        rows = data.get("data") or []
        slots: list[Slot] = []
        for row in rows:
            dt_raw = row if isinstance(row, str) else row.get("datetime") or row.get("time")
            if isinstance(dt_raw, str) and "T" not in dt_raw and len(dt_raw) <= 5:
                dt_raw = f"{date[:10]}T{dt_raw}:00"
            slots.append(Slot(city=city, starts_at=_parse_datetime(dt_raw), staff_id=staff_id, service_id=service_id))
        return slots

    def _company_id_for_city(self, city: str) -> int:
        normalized = _canonical_city_name(city)
        return self.settings.yclients_city_company_ids.get(normalized, self.company_id)

    def _company_targets(self, city: str = "") -> list[tuple[int, str]]:
        if city.strip():
            canonical = _canonical_city_name(city)
            return [(self._company_id_for_city(canonical), canonical)]
        targets: list[tuple[int, str]] = [(self.company_id, "")]
        seen = {self.company_id}
        for configured_city, company_id in self.settings.yclients_city_company_ids.items():
            if company_id and company_id not in seen:
                targets.append((company_id, configured_city))
                seen.add(company_id)
        return targets

    async def _staff_id_for_city(self, company_id: int, city: str) -> int | None:
        if not city.strip():
            return 0
        canonical = _canonical_city_name(city)
        override = self.settings.yclients_city_staff_ids.get(canonical)
        if override:
            return override
        if canonical == "Санкт-Петербург" and "Санкт-Петербург" not in self.settings.yclients_city_company_ids:
            return None
        data = await self._get_json(f"/api/v1/book_staff/{company_id}", "book_staff")
        target = _normalize_city_name(canonical)
        for row in data.get("data") or []:
            if not isinstance(row, dict):
                continue
            title = _normalize_city_name(str(row.get("name") or row.get("title") or ""))
            if title == target:
                return int(row.get("id") or 0)
        return None

    async def list_appointments(self, date: str, client_id: str = "", city: str = "") -> list[Appointment]:
        self._require_ready()
        params: dict[str, Any] = {
            "start_date": date[:10],
            "end_date": date[:10],
            "count": 200,
            "page": 1,
        }
        if client_id:
            params["client_id"] = client_id
        appointments: list[Appointment] = []
        for company_id, target_city in self._company_targets(city):
            data = await self._get_json(f"/api/v1/records/{company_id}", "records list", params=params)
            appointments.extend(
                self._appointment_from_payload(row, city=target_city, company_id=company_id)
                for row in data.get("data") or []
            )
        return appointments

    async def create_appointment(self, appointment: Appointment) -> int:
        self._require_mutations("create appointment")
        self._require_ready()
        company_id = self._company_id_for_city(appointment.city)
        if not appointment.starts_at:
            raise ValueError("appointment.starts_at is required")
        staff_id = int(appointment.raw.get("staff_id") or 0)
        if not staff_id:
            staff_id = int(await self._staff_id_for_city(company_id, appointment.city) or 0)
        if not staff_id:
            raise RuntimeError(f"YCLIENTS staff_id missing for city={appointment.city!r}")
        service_cost = int(appointment.service.price or 0)
        payload = {
            "staff_id": staff_id,
            "services": [
                {
                    "id": int(appointment.service.id),
                    "first_cost": service_cost,
                    "discount": 0,
                    "cost": service_cost,
                }
            ],
            "client": {
                "phone": _phone_payload(appointment.client.phone),
                "name": appointment.client.name,
                "surname": "",
                "patronymic": "",
                "email": appointment.raw.get("email") or "",
            },
            "save_if_busy": False,
            "datetime": appointment.starts_at.strftime("%Y-%m-%d %H:%M:%S"),
            "seance_length": max(1, int(appointment.service.duration_minutes or 60)) * 60,
            "send_sms": False,
            "comment": appointment.notes,
            "sms_remain_hours": 0,
            "email_remain_hours": 0,
            "attendance": 0,
        }
        data = await self._post_json(f"/api/v1/records/{company_id}", payload, "records POST")
        payload_data = data.get("data") or {}
        first = payload_data[0] if isinstance(payload_data, list) and payload_data else payload_data
        record_id = first.get("record_id") or first.get("id") if isinstance(first, dict) else None
        if record_id is None:
            raise RuntimeError(f"YCLIENTS record id missing: {data}")
        return int(record_id)

    async def move_appointment(self, appointment_id: int, slot: Slot, city: str = "") -> Appointment:
        self._require_mutations("move appointment")
        source_city = _canonical_city_name(city or slot.city)
        target_city = _canonical_city_name(slot.city or source_city)
        source_company_id = self._company_id_for_city(source_city)
        target_company_id = self._company_id_for_city(target_city)
        if source_company_id != target_company_id:
            raise RuntimeError("YCLIENTS cannot move an appointment between different companies; create a new record and cancel the old one")
        payload = {"datetime": slot.starts_at.strftime("%Y-%m-%d %H:%M:%S")}
        if slot.staff_id:
            payload["staff_id"] = slot.staff_id
        data = await self._put_json(f"/api/v1/record/{source_company_id}/{appointment_id}", payload, "record PUT")
        return self._appointment_from_payload(
            data.get("data") or {},
            city=target_city,
            company_id=target_company_id,
        )

    async def cancel_appointment(self, appointment_id: int, city: str = "") -> Appointment | None:
        self._require_mutations("cancel appointment")
        company_id = self._company_id_for_city(city)
        current = await self._get_json(f"/api/v1/record/{company_id}/{appointment_id}", "record GET")
        appointment = self._appointment_from_payload(
            current.get("data") or {},
            city=_canonical_city_name(city),
            company_id=company_id,
        )
        response = await self._request("DELETE", f"/api/v1/record/{company_id}/{appointment_id}")
        if response.status_code == 204:
            return appointment
        data = self._unwrap(response, "record DELETE")
        return appointment if data.get("success") else None

    async def search_clients(self, query: str, city: str = "") -> list[ClientProfile]:
        self._require_ready()
        body = {
            "page": 1,
            "page_size": 25,
            "operation": "AND",
            "filters": [{"type": "quick_search", "state": {"value": query}}],
        }
        clients: list[ClientProfile] = []
        for company_id, target_city in self._company_targets(city):
            data = await self._post_json(f"/api/v1/company/{company_id}/clients/search", body, "clients search", mutation=False)
            payload = data.get("data") or []
            rows = payload.get("clients") or payload.get("items") or [] if isinstance(payload, dict) else payload
            clients.extend(
                self._client_from_payload(row, company_id=company_id, city=target_city)
                for row in rows
                if isinstance(row, dict)
            )
        return clients

    async def update_client_notes(self, client_id: str, notes: str, skin_type: str = "", city: str = "") -> None:
        self._require_mutations("update client notes")
        if not city.strip():
            raise ValueError("city is required to update YCLIENTS client notes")
        company_id = self._company_id_for_city(city)
        matches = [client for client in await self.search_clients(client_id, city) if client.external_id == str(client_id)]
        if len(matches) != 1:
            raise RuntimeError(f"YCLIENTS client selection is ambiguous: client_id={client_id!r} city={city!r} matches={len(matches)}")
        text = "; ".join(part for part in [f"Тип кожи: {skin_type}" if skin_type else "", notes] if part)
        if not text:
            return
        await self._post_json(
            f"/api/v1/company/{company_id}/clients/{client_id}/comments",
            {"text": text},
            "client comment",
        )

    async def get_staff_schedule(self, city: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        self._require_ready()
        canonical = _canonical_city_name(city)
        company_id = self._company_id_for_city(canonical)
        staff_id = int(await self._staff_id_for_city(company_id, canonical) or 0)
        if not staff_id:
            raise RuntimeError(f"YCLIENTS staff_id missing for city={canonical!r}")
        params = [
            ("start_date", start_date[:10]),
            ("end_date", end_date[:10]),
            ("staff_ids[]", str(staff_id)),
            ("include[]", "off_day_type"),
        ]
        data = await self._get_json(
            f"/api/v1/company/{company_id}/staff/schedule",
            "staff schedule GET",
            params=params,
        )
        return [dict(row) for row in (data.get("data") or []) if isinstance(row, dict)]

    async def set_staff_schedule(
        self,
        city: str,
        dates: list[str],
        slots: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        self._require_mutations("set staff schedule")
        self._require_ready()
        canonical = _canonical_city_name(city)
        company_id = self._company_id_for_city(canonical)
        staff_id = int(await self._staff_id_for_city(company_id, canonical) or 0)
        if not staff_id:
            raise RuntimeError(f"YCLIENTS staff_id missing for city={canonical!r}")
        normalized_dates = _schedule_dates(dates)
        effective_slots = _normalize_schedule_slots(slots or _default_schedule_slots(canonical))
        data = await self._put_json(
            f"/api/v1/company/{company_id}/staff/schedule",
            {
                "schedules_to_set": [
                    {
                        "staff_id": staff_id,
                        "dates": normalized_dates,
                        "slots": effective_slots,
                    }
                ],
                "schedules_to_delete": [],
            },
            "staff schedule PUT",
        )
        return {
            "city": canonical,
            "company_id": company_id,
            "staff_id": staff_id,
            "dates": normalized_dates,
            "slots": effective_slots,
            "updated_count": len(normalized_dates),
            "yclients_data": data.get("data"),
        }

    async def delete_staff_schedule(self, city: str, dates: list[str]) -> dict[str, Any]:
        self._require_mutations("delete staff schedule")
        self._require_ready()
        canonical = _canonical_city_name(city)
        company_id = self._company_id_for_city(canonical)
        staff_id = int(await self._staff_id_for_city(company_id, canonical) or 0)
        if not staff_id:
            raise RuntimeError(f"YCLIENTS staff_id missing for city={canonical!r}")
        normalized_dates = _schedule_dates(dates)
        data = await self._put_json(
            f"/api/v1/company/{company_id}/staff/schedule",
            {
                "schedules_to_set": [],
                "schedules_to_delete": [{"staff_id": staff_id, "dates": normalized_dates}],
            },
            "staff schedule DELETE",
        )
        return {
            "city": canonical,
            "company_id": company_id,
            "staff_id": staff_id,
            "dates": normalized_dates,
            "deleted_count": len(normalized_dates),
            "yclients_data": data.get("data"),
        }

    def _require_ready(self) -> None:
        if not self.settings.yclients_ready:
            raise RuntimeError("YCLIENTS credentials are incomplete")

    def _require_mutations(self, action: str) -> None:
        if not self.allow_mutations:
            raise YClientsMutationDisabled(f"YCLIENTS mutation disabled for {action}")

    async def _get_json(self, url: str, context: str, params: Any = None) -> dict[str, Any]:
        return self._unwrap(await self._request("GET", url, params=params), context)

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        context: str,
        *,
        mutation: bool = True,
    ) -> dict[str, Any]:
        if mutation:
            self._require_mutations(context)
        response = await self._request("POST", url, json=payload)
        if response.status_code >= 400:
            logger.warning(
                "YCLIENTS %s failed: status=%s url=%s body=%s payload=%s",
                context,
                response.status_code,
                url,
                _response_body_for_log(response),
                _redact_payload_for_log(payload),
            )
        return self._unwrap(response, context)

    async def _put_json(self, url: str, payload: dict[str, Any], context: str) -> dict[str, Any]:
        self._require_mutations(context)
        return self._unwrap(await self._request("PUT", url, json=payload), context)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.client is not None:
            return await self.client.request(method, url, **kwargs)
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=self._headers,
        ) as client:
            return await client.request(method, url, **kwargs)

    def _unwrap(self, response: httpx.Response, context: str) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"YCLIENTS {context}: invalid JSON status={response.status_code}") from exc
        if response.status_code >= 400 or not data.get("success", response.status_code < 400):
            raise RuntimeError(f"YCLIENTS {context}: status={response.status_code} body={_compact_json(data)}")
        return data

    def _service_from_payload(self, row: dict[str, Any]) -> Service:
        duration_seconds = row.get("seance_length") or row.get("duration") or row.get("length") or 3600
        return Service(
            id=int(row.get("id") or 0),
            title=str(row.get("title") or row.get("name") or "услуга"),
            price=int(row.get("price_min") or row.get("price") or 0),
            duration_minutes=max(1, int(duration_seconds) // 60),
        )

    def _client_from_payload(self, row: dict[str, Any], *, company_id: int = 0, city: str = "") -> ClientProfile:
        name = row.get("display_name") or row.get("name") or "Без имени"
        return ClientProfile(
            name=str(name),
            phone=str(row.get("phone") or ""),
            external_id=str(row.get("id") or ""),
            company_id=company_id,
            notes=str(row.get("comment") or ""),
            city=city,
        )

    def _appointment_from_payload(self, row: dict[str, Any], *, city: str = "", company_id: int = 0) -> Appointment:
        client_raw = row.get("client") if isinstance(row.get("client"), dict) else {}
        services = row.get("services") or []
        service_raw = services[0] if services and isinstance(services[0], dict) else {}
        staff = row.get("staff") if isinstance(row.get("staff"), dict) else {}
        return Appointment(
            id=int(row.get("id") or row.get("record_id") or 0),
            client=self._client_from_payload(client_raw, company_id=company_id, city=city),
            service=self._service_from_payload(service_raw),
            city=city,
            starts_at=_parse_datetime(row.get("datetime") or row.get("date")),
            status=str(row.get("attendance") or row.get("status") or "scheduled"),
            notes=str(row.get("comment") or ""),
            raw={"staff_id": staff.get("id") or row.get("staff_id") or 0, "source": "yclients"},
        )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if "T" not in raw and " " in raw:
            raw = raw.replace(" ", "T", 1)
        return datetime.fromisoformat(raw.replace("+0000", "+00:00").replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def _phone_payload(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits or phone


def _response_body_for_log(response: httpx.Response) -> str:
    try:
        return _compact_json(response.json())
    except Exception:
        return response.text[:2000]


def _compact_json(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:2000]
    except Exception:
        return str(data)[:2000]


def _redact_payload_for_log(value: Any) -> str:
    sensitive = {"phone", "email", "name", "surname", "patronymic", "comment"}

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: ("[redacted]" if str(key).casefold() in sensitive else walk(val)) for key, val in item.items()}
        if isinstance(item, list):
            return [walk(val) for val in item]
        return item

    return _compact_json(walk(value))


def _normalize_city_name(value: str) -> str:
    return value.strip().casefold().replace("ё", "е").replace("-", " ")


def _canonical_city_name(value: str) -> str:
    normalized = _normalize_city_name(value)
    aliases = {
        "ростов": "Ростов-на-Дону",
        "ростов на дону": "Ростов-на-Дону",
        "санкт петербург": "Санкт-Петербург",
        "спб": "Санкт-Петербург",
        "питер": "Санкт-Петербург",
        "москва": "Москва",
        "мск": "Москва",
        "краснодар": "Краснодар",
        "геленджик": "Геленджик",
        "гелик": "Геленджик",
    }
    return aliases.get(normalized, value.strip())


def _schedule_dates(values: list[str]) -> list[str]:
    dates: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = datetime.fromisoformat(str(value)[:10]).date().isoformat()
        if normalized not in seen:
            seen.add(normalized)
            dates.append(normalized)
    if not dates:
        raise ValueError("at least one schedule date is required")
    return dates


def _normalize_schedule_slots(slots: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for slot in slots:
        starts_at = str(slot.get("from") or "")[:5]
        ends_at = str(slot.get("to") or "")[:5]
        try:
            start_time = time.fromisoformat(starts_at)
            end_time = time.fromisoformat(ends_at)
        except ValueError as exc:
            raise ValueError(f"invalid YCLIENTS schedule slot: {slot!r}") from exc
        if start_time >= end_time:
            raise ValueError(f"schedule slot must end after it starts: {slot!r}")
        normalized.append({"from": start_time.strftime("%H:%M"), "to": end_time.strftime("%H:%M")})
    if not normalized:
        raise ValueError("at least one schedule slot is required")
    return normalized


def _default_schedule_slots(city: str) -> list[dict[str, str]]:
    canonical = _canonical_city_name(city)
    hours = {
        "Ростов-на-Дону": ("11:00", "20:00"),
        "Москва": ("10:00", "21:00"),
        "Краснодар": ("10:00", "20:00"),
        "Геленджик": ("10:00", "20:00"),
    }
    starts_at, ends_at = hours.get(canonical, ("10:00", "20:00"))
    return [{"from": starts_at, "to": ends_at}]
