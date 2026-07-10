from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import count
from typing import Any

from ..codex_runner import chat_with_codex
from .admin import AdminResult
from .agent_tools import AutomationToolbox
from .agent_trace import JsonlAgentTraceLogger
from .codex_planner import parse_codex_step
from .config import IntegrationSettings
from .mentor_memory import MentorMemoryService
from .roles import CodexRole, RoleProfile, role_profile


AdminCodexProgress = Callable[[str], None]
AdminCodexRunner = Callable[
    [dict[str, Any], list[dict[str, Any]], AdminCodexProgress | None],
    Awaitable[dict[str, Any] | None],
]


@dataclass(frozen=True)
class CodexTelegramAdminService:
    toolbox: AutomationToolbox
    settings: IntegrationSettings
    runner: AdminCodexRunner | None = None
    trace_logger: JsonlAgentTraceLogger | None = None
    mentor_memory: MentorMemoryService | None = None

    async def handle_text(self, text: str, progress_callback: AdminCodexProgress | None = None) -> AdminResult:
        return await self.handle_message({"text": text, "channel": "telegram_admin"}, progress_callback=progress_callback)

    async def handle_message(
        self,
        message: dict[str, Any],
        progress_callback: AdminCodexProgress | None = None,
    ) -> AdminResult:
        payload = self._payload(message)
        profile = _profile_from_payload(payload)
        trace: list[dict[str, Any]] = []
        runner = self.runner or CodexTelegramAdminRunner(timeout_seconds=self.settings.telegram_admin_codex_timeout_seconds)
        max_steps = self.settings.telegram_admin_codex_max_steps
        step_indexes = count() if max_steps <= 0 else range(max_steps)
        for _ in step_indexes:
            step = await runner(payload, trace, progress_callback)
            if not step:
                self._write_trace(payload, trace, {"action": "empty_step"})
                return AdminResult(action="codex_empty", ok=False, message="Codex не вернул решение. Ничего не сделано.")
            tool_calls = _tool_calls_from_step(step)
            if tool_calls:
                for call in tool_calls:
                    name = str(call.get("name") or "")
                    arguments = dict(call.get("arguments") or {})
                    if not profile.allows_tool(name):
                        trace.append(
                            {
                                "type": "tool_result",
                                "tool": name,
                                "arguments": arguments,
                                "ok": False,
                                "data": {},
                                "error": f"tool {name} is not allowed for role {profile.role.value}",
                            }
                        )
                        continue
                    trace.append({"type": "tool_call", "tool": name, "arguments": arguments})
                    result = await self.toolbox.execute(name, arguments)
                    self._observe_tool_result(profile, name, arguments, result, payload=payload)
                    trace.append(
                        {
                            "type": "tool_result",
                            "tool": name,
                            "arguments": arguments,
                            "ok": result.ok,
                            "data": _compact_tool_data(name, result.data),
                            "error": _truncate_text(result.error, 1200),
                        }
                    )
                continue
            message = str(step.get("reply") or step.get("message") or "").strip()
            action = str(step.get("action") or "codex_admin_reply")
            self._write_trace(payload, trace, step)
            if not message:
                return AdminResult(action=action, ok=False, message="Codex не сформулировал ответ. Ничего не сделано.")
            return AdminResult(
                action=action,
                ok=bool(step.get("ok", True)),
                message=message,
                appointment_id=_optional_int(step.get("appointment_id")),
                client_id=str(step.get("client_id") or ""),
                metadata={"trace": list(trace), "outcome": dict(step)},
            )
        outcome = {"action": "codex_admin_step_limit", "max_steps": max_steps}
        self._write_trace(payload, trace, outcome)
        return AdminResult(action="codex_admin_step_limit", ok=False, message="Codex не завершил решение за лимит шагов. Ничего не сделано.")

    def _payload(self, message: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(message, str):
            message = {"text": message, "channel": "telegram_admin"}
        profile = _profile_from_message(message)
        tool_schemas = [schema for schema in self.toolbox.tool_schemas() if profile.allows_tool(str(schema.get("name") or ""))]
        return {
            "role": profile.prompt_role,
            "role_name": profile.role.value,
            "conversation_key": str(message.get("history_key") or message.get("conversation_key") or ""),
            "goal": profile.goal,
            "message": dict(message),
            "available_tools": [name for name in self.toolbox.tool_names() if profile.allows_tool(name)],
            "tool_schemas": tool_schemas,
            "reply_rules": [
                *profile.reply_rules,
                "Если нужно создать, перенести, отменить запись или изменить заметки клиента, сначала вызови соответствующий tool.",
                "Если спрашивают про Avito-чаты, используй avito.chats.list и при необходимости avito.messages.list.",
                "Если просят отправить ответ в Avito, используй avito.messages.send только когда понятен chat_id и текст ответа.",
                "Ответ Ольги/админа на handoff-карточку — это внутренняя экспертная подсказка, а не готовый текст клиенту. Перед отправкой в Avito сам восстанови контекст карточки/истории, выдели факты и решение, затем сформулируй клиентский ответ естественно от сервиса.",
                "Если Ольга просто дала экспертный ответ/правку без явного 'отправляй/да/можно отправить', обычно не отправляй сразу: верни action='avito_client_draft', chat_id и draft_text, чтобы Ольга увидела черновик с кнопками подтверждения.",
                "Если Ольга отвечает на уже предложенный черновик: сам реши по её тексту — это подтверждение отправки, правка для нового черновика, запрет отправки или просьба уточнить.",
                "Никогда не отправляй клиенту текст Ольги дословно и не пиши клиенту 'Ольга сказала/ответила', 'косметолог сказала', 'по словам Ольги'. Клиенту нужен аккуратный итог: что можно/нельзя, когда, что прислать дальше, что уточняется.",
                "Если просят отправить телефон в Avito, используй avito.messages.send_phone только с подтверждённым номером.",
                "Если косметолог прислала фото и просит отправить его клиенту Avito, используй avito.messages.send_image с image_path из message.attachments или conversation_history.",
                "Если просят отправить видео или другой файл, используй avito.messages.send_file с подтверждённым file_path; если Avito API не поддержит тип файла, честно сообщи результат.",
                "Если Ольга/админ сообщает расписание по городам, используй schedule.city.set; если спрашивает график — schedule.city.list.",
                "Перед поиском слотов по городу учитывай локальный city schedule: Ольга одна, поэтому нельзя предлагать параллельные города на одну дату.",
                "Если администратор/косметолог просит разобраться в логах, состоянии проекта, доступных инструментах или поведении бота, используй workspace.* tools: logs.tail, files.list/read, command.run или python.run.",
                "workspace.* tools только для диагностики и чтения; не пытайся читать секреты, .env, токены, MFA или менять файлы/процессы.",
                "conversation_history — это рабочая память этой беседы; извлекай из неё город, услугу, дату, телефон, chat_id, image_path и предыдущие tool_trace. Для проверки слотов достаточно города и даты; услуга/service_id нужна для записи, переноса и других действий с конкретной услугой.",
                "Если role_name='olga_boss', пишет сама Ольга: отвечай ей как персональный ассистент владельца бизнеса.",
                "Если role_name='admin', пишет технический/операционный администратор: не называй его Ольгой и не обращайся к нему как к косметологу.",
                "В admin-контексте Ольга — владелец бизнеса и предмет управления, но не адресат ответа.",
                "Не веди пользователя по линейной анкете, если часть данных уже есть в conversation_history или trace; используй эти данные и двигай задачу дальше.",
                "Если вопрос общий вроде 'какие есть свободные слоты?', сначала используй последние известные город и дату из conversation_history и tool_trace. Не требуй услугу для read-only проверки слотов; если дата неудачная или слотов нет — расширь поиск на несколько ближайших разумных дат или предложи конкретные недостающие варианты, а не начинай сначала.",
                "Можно вернуть несколько tool_calls за один шаг, когда проверки независимы или нужен обзор вариантов.",
                "Не выдумывай результат YCLIENTS: опирайся на tool_result.",
                "Если данных не хватает, не вызывай mutating tool; коротко спроси недостающие данные.",
                "После отмены записи используй client_message из tool_result для подтверждения клиенту; отдельное уведомление Ольге отправляется автоматически.",
                "Для проблем и индивидуальной оценки не склоняй клиента к очной консультации. Если данных реально не хватает, один раз предложи онлайн-разбор с Ольгой и собери только недостающие данные/фото. Если оценка Ольги уже есть, дай клиенту итог без нового предложения консультации.",
                "Ответ должен быть коротким и понятным адресату текущей роли.",
            ],
        }

    def _write_trace(self, payload: dict[str, Any], trace: list[dict[str, Any]], outcome: dict[str, Any]) -> None:
        if not self.trace_logger:
            return
        try:
            self.trace_logger.write(planner="telegram_admin_codex", payload=payload, trace=trace, outcome=outcome)
        except OSError:
            return

    def _observe_tool_result(
        self,
        profile: RoleProfile,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        payload: dict[str, Any],
    ) -> None:
        if not self.mentor_memory or not getattr(result, "ok", False):
            return
        if name not in {"avito.messages.send", "avito.messages.send_phone"}:
            return
        text = str(arguments.get("text") or "")
        if name == "avito.messages.send_phone" and not text:
            text = str(arguments.get("phone") or "")
        actor = "olga" if profile.role == CodexRole.OLGA_BOSS else "admin"
        self.mentor_memory.observe_avito_send(
            chat_id=str(arguments.get("chat_id") or ""),
            text=text,
            actor=actor,
            source=profile.role.value,
            context=_mentor_context_from_payload(payload),
        )


@dataclass(frozen=True)
class CodexTelegramAdminRunner:
    timeout_seconds: int = 1800

    async def __call__(
        self,
        payload: dict[str, Any],
        trace: list[dict[str, Any]],
        progress_callback: AdminCodexProgress | None = None,
    ) -> dict[str, Any] | None:
        prompt = build_admin_codex_prompt(payload, trace)
        text, _path = await asyncio.to_thread(chat_with_codex, prompt, None, self.timeout_seconds, progress_callback, True)
        step = parse_codex_step(text)
        if step is not None:
            return step
        fallback = str(text or "").strip()
        if not fallback:
            return None
        return {
            "action": "codex_admin_reply",
            "ok": "не успел" not in fallback.casefold(),
            "reply": fallback,
        }


def build_admin_codex_prompt(payload: dict[str, Any], trace: list[dict[str, Any]]) -> str:
    prompt_payload = _compact_payload(payload)
    prompt_trace = [_compact_trace_entry(entry) for entry in trace]
    return (
        "Ты Codex-планировщик Telegram-админки косметолога.\n"
        f"Текущая роль: {payload.get('role_name') or payload.get('role')}. Conversation key: {payload.get('conversation_key') or ''}.\n"
        "Роль задаёт права, стиль ответа и доступные tools; не выходи за role profile.\n"
        "Бизнес-контекст: Ольга — косметолог и владелец экспертного контекста, а не клиент/адресат клиентского ответа.\n"
        "Если role_name='olga_boss', адресат — Ольга: общайся с ней как персональный ассистент владельца бизнеса.\n"
        "Если role_name='admin', адресат — технический/операционный администратор: не называй его Ольгой и не говори с ним как с косметологом.\n"
        "Если Ольга спрашивает, что ты умеешь для неё, отвечай про помощь с записями, клиентами, расписанием, Avito, контентом, диагностикой бота и проектом; не отвечай клиентской рамкой 'Я помощник Ольги...'. Для admin описывай возможности как админские/операционные.\n"
        "Если формулируешь ответ для клиентов Ольги, пиши как помощник Ольги или нейтрально от сервиса; не начинай клиентский текст обращением 'Ольга, ...'.\n"
        "Ответ Ольги на handoff-карточку — внутренняя подсказка. Не копируй её дословно в Avito: извлеки смысл, учти вопрос клиента/карточку/историю, затем напиши клиентский ответ нормальным языком сервиса. Не раскрывай клиенту внутреннюю механику согласования.\n"
        "Но если Ольга явно перечислила факты для клиента — цены, объёмы, препарат, срок эффекта, платная/бесплатная консультация, противопоказания, город/дату/условия — обязательно сохрани все эти факты в черновике. Не сокращай ответ до одного тезиса и не выкидывай прайс/условия консультации; можно только отформатировать и смягчить формулировки.\n"
        "Если Ольга дала ответ на карточку без явной команды отправить, лучше верни черновик для подтверждения, а не вызывай avito.messages.send. Формат черновика: {\"action\":\"avito_client_draft\",\"ok\":true,\"chat_id\":\"...\",\"draft_text\":\"...\",\"reply\":\"Подготовила черновик.\"}. Если Ольга ответила на черновик явным согласием, можно отправить через avito.messages.send.\n"
        "Нет отдельного парсера команд и нет intent detector: ты сам решаешь, какие tools нужны.\n"
        "Приложение не делает действий само, оно только исполнит твои tool_calls и вернет trace.\n"
        "Для mutates/external tools вызывай tool только когда хватает required данных и команда администратора явно просит действие.\n"
        "Для диагностики проекта действуй как инженер: смотри logs/files/команды/code через workspace.* tools, затем отвечай выводами.\n"
        "workspace.* tools read-only: не читай .env/секреты/токены/MFA и не пытайся менять состояние системы.\n"
        "График городов — локальный источник правды поверх YCLIENTS: если Ольга одна, сначала используй schedule.city.* и не предлагай слоты в городе, где её нет в эту дату.\n"
        "Работай нелинейно: conversation_history и TRACE — это состояние задачи, а не просто чат. Не спрашивай заново то, что уже можно вывести из истории.\n"
        "Для слотов сначала восстанови город и дату из истории; услугу/service_id не требуй для read-only проверки. Для записей/переносов восстанови также услугу/service_id, клиента и телефон. Если данных достаточно — вызывай tools.\n"
        "Если предыдущий read-only tool_result дал пустой список, не зависай на одном шаге: расширь поиск, проверь соседние даты или сформулируй один короткий пакетный вопрос.\n"
        "Верни строго один JSON-объект без markdown.\n"
        "Вызов tools:\n"
        '{"tool_calls":[{"name":"yclients.clients.search","arguments":{"query":"Анна"}}]}\n'
        "Финальный ответ:\n"
        '{"action":"codex_admin_reply","ok":true,"reply":"Готово, запись создана.","appointment_id":123,"client_id":null}\n'
        "Черновик клиентского ответа:\n"
        '{"action":"avito_client_draft","ok":true,"chat_id":"u2i-...","draft_text":"Клиентский текст","reply":"Подготовила черновик."}\n'
        "Если данных не хватает:\n"
        '{"action":"need_more_data","ok":false,"reply":"Не хватает телефона клиента."}\n\n'
        "PAYLOAD:\n"
        f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}\n\n"
        "TRACE:\n"
        f"{json.dumps(prompt_trace, ensure_ascii=False, indent=2)}\n"
    )


def _tool_calls_from_step(step: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = step.get("tool_calls") or step.get("tools") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    return [call for call in raw_calls if isinstance(call, dict) and call.get("name")]


def _profile_from_message(message: dict[str, Any]) -> RoleProfile:
    raw = str(message.get("role_name") or message.get("codex_role") or CodexRole.ADMIN.value)
    try:
        return role_profile(CodexRole(raw))
    except ValueError:
        return role_profile(CodexRole.ADMIN)


def _profile_from_payload(payload: dict[str, Any]) -> RoleProfile:
    return _profile_from_message(dict(payload.get("message") or {"role_name": payload.get("role_name")}))


def _mentor_context_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    message = dict(payload.get("message") or {})
    text = str(message.get("text") or "")
    context = {
        "handoff_text": text,
        "source_message_id": message.get("source_message_id") or message.get("reply_to_message_id") or "",
        "olga_reply_message_id": message.get("message_id") or "",
    }
    for key in ("avito_chat_id", "chat_id"):
        if message.get(key):
            context["chat_id"] = message.get(key)
            break
    client_message = _extract_client_message(text)
    if client_message:
        context["client_message"] = client_message
    return context


def _extract_client_message(text: str) -> str:
    import re

    match = re.search(r"(?m)^Сообщение:\s*(.+?)(?:\nКонтекст:|\n\n|$)", str(text or ""), flags=re.S)
    return " ".join(match.group(1).split()) if match else ""


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed or None


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    message = dict(compact.get("message") or {})
    history = message.get("conversation_history")
    if isinstance(history, list):
        compact_history: list[dict[str, Any]] = []
        for item in history[-8:]:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["content"] = _truncate_text(str(row.get("content") or ""), 1800)
            compact_history.append(row)
        message["conversation_history"] = compact_history
    compact["message"] = message
    return compact


def _compact_trace_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    compact = dict(entry)
    if compact.get("type") == "tool_result":
        compact["data"] = _compact_tool_data(str(compact.get("tool") or ""), dict(compact.get("data") or {}))
        compact["error"] = _truncate_text(str(compact.get("error") or ""), 1200)
    if compact.get("type") == "tool_call":
        compact["arguments"] = _compact_json_value(compact.get("arguments") or {}, 1200)
    return compact


def _compact_tool_data(tool: str, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"value": _truncate_text(str(data), 4000)}
    if tool == "yclients.services.list" and isinstance(data.get("services"), list):
        services = data["services"]
        relevant = _relevant_services_sample(services)
        compact = {"services_count": len(services), "services_sample": services[:15]}
        if relevant:
            compact["services_relevant"] = relevant
        return compact
    if tool == "yclients.slots.list" and isinstance(data.get("slots"), list):
        slots = data["slots"]
        return {
            "slots_count": len(slots),
            "slots_sample": slots[:20],
            "blocked_by_city_schedule": data.get("blocked_by_city_schedule"),
            "schedule_city": data.get("schedule_city"),
            "requested_city": data.get("requested_city"),
            "date": data.get("date"),
        }
    if tool == "yclients.appointments.list" and isinstance(data.get("appointments"), list):
        appointments = data["appointments"]
        return {"appointments_count": len(appointments), "appointments_sample": appointments[:15]}
    if tool == "yclients.clients.search" and isinstance(data.get("clients"), list):
        clients = data["clients"]
        return {"clients_count": len(clients), "clients_sample": clients[:15]}
    if tool == "workspace.files.list" and isinstance(data.get("files"), list):
        files = data["files"]
        return {"files_count": len(files), "files_sample": files[:40], "truncated": data.get("truncated")}
    if tool in {"workspace.command.run", "workspace.python.run"}:
        return {
            "returncode": data.get("returncode"),
            "stdout": _truncate_text(str(data.get("stdout") or ""), 5000),
            "stderr": _truncate_text(str(data.get("stderr") or ""), 2000),
            "args": data.get("args"),
        }
    if tool == "workspace.logs.tail":
        return {"path": data.get("path"), "content": _truncate_text(str(data.get("content") or ""), 5000)}
    return _compact_json_value(data, 7000)


def _compact_json_value(value: Any, max_chars: int) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return _truncate_text(str(value), max_chars)
    if len(encoded) <= max_chars:
        return value
    return {"truncated": True, "preview": encoded[:max_chars]}


def _truncate_text(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"


def _relevant_services_sample(services: list[Any], limit: int = 30) -> list[Any]:
    keywords = (
        "cog",
        "ког",
        "нити",
        "нить",
        "нитев",
        "лифт",
        "губ",
        "груд",
        "ягод",
        "скул",
        "подбород",
        "челюст",
        "овал",
    )
    relevant: list[Any] = []
    seen: set[str] = set()
    for service in services:
        if isinstance(service, dict):
            title = str(service.get("title") or service.get("name") or "")
            service_id = str(service.get("id") or "")
        else:
            title = str(getattr(service, "title", "") or service)
            service_id = str(getattr(service, "id", "") or "")
        haystack = title.casefold()
        if not any(keyword in haystack for keyword in keywords):
            continue
        key = service_id or title
        if key in seen:
            continue
        seen.add(key)
        relevant.append(service)
        if len(relevant) >= limit:
            break
    return relevant
