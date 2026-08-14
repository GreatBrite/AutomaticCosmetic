from __future__ import annotations

import json
import mimetypes
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from uuid import uuid4


class TelegramBot:
    def __init__(self, token: str):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is empty")
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"

    def api(self, method: str, payload: dict | None = None, timeout: int = 30) -> dict:
        data = urllib.parse.urlencode(payload or {}).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}/{method}", data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API {method} failed: HTTP {exc.code}: {body}") from exc

    def api_multipart(
        self,
        method: str,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        timeout: int = 60,
    ) -> dict:
        boundary = f"----freelance-leads-bot-{uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                    str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )
        for name, (filename, content, content_type) in files.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    (
                        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                        f"Content-Type: {content_type}\r\n\r\n"
                    ).encode("utf-8"),
                    content,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API {method} failed: HTTP {exc.code}: {body}") from exc

    def _add_delivery_params(
        self,
        payload: dict,
        message_thread_id: str | int | None = None,
        direct_messages_topic_id: str | int | None = None,
        business_connection_id: str | None = None,
    ) -> dict:
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id
        if direct_messages_topic_id:
            payload["direct_messages_topic_id"] = direct_messages_topic_id
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        return payload

    def send_message(
        self,
        chat_id: str,
        text: str,
        message_thread_id: str | int | None = None,
        direct_messages_topic_id: str | int | None = None,
        business_connection_id: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = {
                "chat_id": chat_id,
                "text": text[:3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        self._add_delivery_params(payload, message_thread_id, direct_messages_topic_id, business_connection_id)
        return self.api("sendMessage", payload)

    def create_forum_topic(
        self,
        chat_id: str,
        name: str,
        icon_color: int | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> dict:
        payload: dict[str, str | int] = {
            "chat_id": chat_id,
            "name": name[:128],
        }
        if icon_color is not None:
            payload["icon_color"] = icon_color
        if icon_custom_emoji_id:
            payload["icon_custom_emoji_id"] = icon_custom_emoji_id
        return self.api("createForumTopic", payload)

    def send_web_app_button(
        self,
        chat_id: str,
        text: str,
        button_text: str,
        web_app_url: str,
        message_thread_id: str | int | None = None,
        direct_messages_topic_id: str | int | None = None,
        business_connection_id: str | None = None,
    ) -> dict:
        reply_markup = {
            "inline_keyboard": [[{"text": button_text, "web_app": {"url": web_app_url}}]],
        }
        return self.send_message(
            chat_id,
            text,
            message_thread_id=message_thread_id,
            direct_messages_topic_id=direct_messages_topic_id,
            business_connection_id=business_connection_id,
            reply_markup=reply_markup,
        )

    def send_document(
        self,
        chat_id: str,
        path: str | Path,
        caption: str | None = None,
        message_thread_id: str | int | None = None,
        direct_messages_topic_id: str | int | None = None,
        business_connection_id: str | None = None,
    ) -> dict:
        file_path = Path(path)
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        payload = {"chat_id": chat_id}
        self._add_delivery_params(payload, message_thread_id, direct_messages_topic_id, business_connection_id)
        if caption:
            payload["caption"] = caption[:1000]
            payload["parse_mode"] = "HTML"
        return self.api_multipart(
            "sendDocument",
            payload,
            {"document": (file_path.name, content, content_type)},
            timeout=120,
        )

    def send_photo(
        self,
        chat_id: str,
        path: str | Path,
        caption: str | None = None,
        message_thread_id: str | int | None = None,
        direct_messages_topic_id: str | int | None = None,
        business_connection_id: str | None = None,
    ) -> dict:
        file_path = Path(path)
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
        payload = {"chat_id": chat_id}
        self._add_delivery_params(payload, message_thread_id, direct_messages_topic_id, business_connection_id)
        if caption:
            payload["caption"] = caption[:1000]
            payload["parse_mode"] = "HTML"
        return self.api_multipart(
            "sendPhoto",
            payload,
            {"photo": (file_path.name, content, content_type)},
            timeout=120,
        )

    def send_photo_url(
        self,
        chat_id: str,
        photo_url: str,
        caption: str | None = None,
        message_thread_id: str | int | None = None,
        direct_messages_topic_id: str | int | None = None,
        business_connection_id: str | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "photo": photo_url}
        self._add_delivery_params(payload, message_thread_id, direct_messages_topic_id, business_connection_id)
        if caption:
            payload["caption"] = caption[:1000]
            payload["parse_mode"] = "HTML"
        return self.api("sendPhoto", payload, timeout=120)

    def send_voice(
        self,
        chat_id: str,
        path: str | Path,
        caption: str | None = None,
        message_thread_id: str | int | None = None,
        direct_messages_topic_id: str | int | None = None,
        business_connection_id: str | None = None,
    ) -> dict:
        file_path = Path(path)
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "audio/ogg"
        payload = {"chat_id": chat_id}
        self._add_delivery_params(payload, message_thread_id, direct_messages_topic_id, business_connection_id)
        if caption:
            payload["caption"] = caption[:1000]
            payload["parse_mode"] = "HTML"
        return self.api_multipart(
            "sendVoice",
            payload,
            {"voice": (file_path.name, content, content_type)},
            timeout=120,
        )

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        business_connection_id: str | None = None,
    ) -> dict:
        payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
        }
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        return self.api("editMessageText", payload, timeout=8)

    def send_message_draft(
        self,
        chat_id: str,
        draft_id: int,
        text: str | None = None,
        message_thread_id: str | int | None = None,
    ) -> None:
        payload = {
                "chat_id": chat_id,
                "draft_id": draft_id,
        }
        self._add_delivery_params(payload, message_thread_id)
        if text is not None:
            payload["text"] = text[:3900]
            payload["parse_mode"] = "HTML"
        self.api("sendMessageDraft", payload, timeout=8)

    def send_chat_action(
        self,
        chat_id: str,
        action: str = "typing",
        message_thread_id: str | int | None = None,
        direct_messages_topic_id: str | int | None = None,
        business_connection_id: str | None = None,
    ) -> None:
        payload = {"chat_id": chat_id, "action": action}
        self._add_delivery_params(payload, message_thread_id, direct_messages_topic_id, business_connection_id)
        self.api("sendChatAction", payload, timeout=8)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        try:
            self.api("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:180]})
        except RuntimeError as exc:
            # Telegram callback queries expire quickly. A stale button click should not
            # crash the long-running bot process.
            if "query is too old" not in str(exc) and "query ID is invalid" not in str(exc):
                raise

    def get_updates(self, offset: int | None = None) -> list[dict]:
        payload = {
            "timeout": 25,
            "allowed_updates": json.dumps(
                [
                    "message",
                    "callback_query",
                    "business_connection",
                    "business_message",
                    "edited_business_message",
                ]
            ),
        }
        if offset is not None:
            payload["offset"] = offset
        data = self.api("getUpdates", payload)
        return data.get("result", [])

    def get_file(self, file_id: str) -> dict:
        data = self.api("getFile", {"file_id": file_id})
        result = data.get("result") or {}
        if not result.get("file_path"):
            raise RuntimeError("Telegram getFile returned no file_path")
        return result

    def download_file(self, file_path: str, timeout: int = 60) -> bytes:
        safe_path = urllib.parse.quote(file_path, safe="/")
        req = urllib.request.Request(f"{self.file_base_url}/{safe_path}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram file download failed: HTTP {exc.code}: {body}") from exc


def render_lead(lead: dict) -> str:
    estimate = lead.get("estimate") or {}
    lines = [
        f"<b>{escape(lead['title'])}</b>",
        (
            f"Тип: {escape(type_label(lead))} | "
            f"Канал: {escape(apply_channel_label(lead))} | "
            f"Источник: {escape(lead['source'])} | score {lead['score']} | {escape(freshness_label(lead))}"
        ),
    ]
    if lead.get("company"):
        lines.append(f"Компания: {escape(lead['company'])}")
    if lead.get("budget"):
        lines.append(f"Бюджет: {escape(lead['budget'])}")
    if lead.get("matches"):
        lines.append(f"Совпало: {escape(', '.join(lead['matches']))}")
    if estimate:
        lines.append(
            "Оценка: "
            f"{estimate.get('days_min')}-{estimate.get('days_max')} дн., "
            f"${estimate.get('day_rate_usd')}/день, "
            f"риск: {escape(str(estimate.get('risk')))}"
        )
        if estimate.get("suspicious_hits"):
            lines.append(f"Подозрительно: {escape(', '.join(estimate['suspicious_hits']))}")
        if estimate.get("client_infra"):
            lines.append(f"Инфра за счёт клиента: {escape(', '.join(estimate['client_infra']))}")
    lines.append(f"<a href=\"{escape(lead['url'])}\">Открыть заказ</a>")
    return "\n".join(lines)


def freshness_label(lead: dict) -> str:
    raw = lead.get("last_seen_at") or lead.get("created_at") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hours = max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 3600))
    except ValueError:
        return "актуальность неизвестна"
    if hours < 1:
        return "свежий, <1ч"
    if hours < 24:
        return f"свежий, {hours}ч"
    days = hours // 24
    if days <= 3:
        return f"актуален, {days}д"
    if days <= 7:
        return f"стареет, {days}д"
    return f"старый, {days}д"


def type_label(lead: dict) -> str:
    lead_type = lead.get("lead_type")
    if not lead_type:
        estimate = lead.get("estimate") or {}
        if isinstance(estimate, dict):
            lead_type = estimate.get("lead_type")
    return "проект" if lead_type == "project" else "вакансия"


def apply_channel_label(lead: dict) -> str:
    channel = lead.get("apply_channel")
    if not channel:
        estimate = lead.get("estimate") or {}
        if isinstance(estimate, dict):
            channel = estimate.get("apply_channel")
    labels = {
        "research_only": "разведка",
        "can_apply_if_account_ok": "можно откликаться",
        "job_board": "вакансия",
        "unknown": "неизвестно",
    }
    return labels.get(str(channel), str(channel or "неизвестно"))


def render_leads_digest(leads: list[dict], errors: list[str]) -> str:
    if not leads:
        suffix = "\n\nОшибки источников:\n" + "\n".join(escape(e) for e in errors) if errors else ""
        return "Новых подходящих лидов пока нет." + suffix
    lines = [f"<b>Новые лиды: {len(leads)}</b>"]
    for i, lead in enumerate(leads[:5], start=1):
        lines.append(
            f"{i}. {escape(lead['title'])} | {escape(apply_channel_label(lead))} | score {lead['score']} | {escape(freshness_label(lead))}"
        )
    if len(leads) > 5:
        lines.append(f"Ещё {len(leads) - 5} в списке.")
    if errors:
        lines.append("Часть источников не ответила:\n" + "\n".join(escape(e) for e in errors))
    return "\n".join(lines)


def send_leads(bot: TelegramBot, chat_id: str, leads: list[dict], errors: list[str]) -> None:
    bot.send_message(chat_id, render_leads_digest(leads, errors))
