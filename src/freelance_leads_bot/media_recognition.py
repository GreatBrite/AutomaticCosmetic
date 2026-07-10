from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .telegram import TelegramBot


OPENAI_API_URL = "https://api.openai.com/v1"
UPLOAD_DIR = Path("data/telegram_uploads")
MAX_TEXT_EXCERPT_CHARS = 12000


@dataclass(frozen=True)
class RecognizedMedia:
    kind: str
    text: str
    prompt_text: str


def _openai_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY не задан. Добавь ключ в .env и перезапусти бота.")
    return key


def _openai_multipart(path: str, fields: dict[str, str], files: dict[str, tuple[str, bytes, str]], timeout: int = 120) -> dict:
    boundary = f"----freelance-leads-bot-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, content, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8"),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        OPENAI_API_URL + path,
        data=b"".join(chunks),
        headers={
            "Authorization": f"Bearer {_openai_key()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API {path} failed: HTTP {exc.code}: {body[:500]}") from exc


def _download_telegram_file(bot: TelegramBot, file_id: str) -> tuple[str, bytes, str]:
    file_info = bot.get_file(file_id)
    file_path = str(file_info["file_path"])
    content = bot.download_file(file_path)
    filename = Path(file_path).name or f"telegram-{file_id}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return filename, content, content_type


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip() or "telegram-file"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120] or "telegram-file"


def _save_upload(filename: str, content: bytes) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    path = UPLOAD_DIR / f"{uuid.uuid4().hex[:10]}-{safe_name}"
    path.write_bytes(content)
    return path


def _decode_text_file(content: bytes, content_type: str, filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    text_like_suffixes = {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".tsx",
        ".py",
        ".sh",
        ".log",
    }
    if not (content_type.startswith("text/") or suffix in text_like_suffixes):
        return None
    for encoding in ("utf-8", "utf-16", "cp1251", "latin-1"):
        try:
            return content.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace").strip()


def _image_saved_text(saved_path: Path) -> str:
    return f"Изображение сохранено: {saved_path.resolve()}"


def describe_photo(bot: TelegramBot, message: dict) -> RecognizedMedia:
    photos = message.get("photo") or []
    if not photos:
        raise RuntimeError("В сообщении нет photo.")
    photo = max(photos, key=lambda item: int(item.get("file_size") or item.get("width", 0) * item.get("height", 0)))
    filename, content, content_type = _download_telegram_file(bot, str(photo["file_id"]))
    saved_path = _save_upload(filename, content)
    caption = (message.get("caption") or "").strip()
    text = _image_saved_text(saved_path)
    prompt_text = "Пользователь отправил фото."
    prompt_text += f"\nФайл сохранён: {saved_path}"
    prompt_text += f"\nАбсолютный путь: {saved_path.resolve()}"
    if caption:
        prompt_text += f"\nПодпись: {caption}"
    prompt_text += "\nПосмотри изображение локально по указанному пути и ответь на запрос пользователя."
    return RecognizedMedia("photo", text, prompt_text)


def describe_document(bot: TelegramBot, message: dict) -> RecognizedMedia:
    document = message.get("document")
    if not document:
        raise RuntimeError("В сообщении нет document.")
    filename = str(document.get("file_name") or f"telegram-{document['file_id']}")
    downloaded_filename, content, content_type = _download_telegram_file(bot, str(document["file_id"]))
    if not Path(filename).suffix:
        filename = downloaded_filename
    saved_path = _save_upload(filename, content)
    caption = (message.get("caption") or "").strip()
    text = _decode_text_file(content, content_type, filename)
    prompt_lines = [
        "Пользователь отправил файл.",
        f"Имя файла: {filename}",
        f"MIME: {content_type}",
        f"Размер: {len(content)} байт",
        f"Файл сохранён: {saved_path}",
    ]
    if caption:
        prompt_lines.append(f"Подпись: {caption}")
    if content_type.startswith("image/"):
        prompt_lines.append(f"Абсолютный путь: {saved_path.resolve()}")
        prompt_lines.append("Это изображение, отправленное как файл. Посмотри его локально по указанному пути и ответь на запрос пользователя.")
        return RecognizedMedia("document", _image_saved_text(saved_path), "\n".join(prompt_lines))
    if text:
        excerpt = text[:MAX_TEXT_EXCERPT_CHARS]
        if len(text) > MAX_TEXT_EXCERPT_CHARS:
            excerpt += "\n...[текст обрезан]"
        prompt_lines.append("Текст файла:\n" + excerpt)
        return RecognizedMedia("document", excerpt, "\n".join(prompt_lines))
    summary = f"Файл принят и сохранён: {saved_path}"
    prompt_lines.append("Содержимое не текстовое; при необходимости работай с файлом по указанному пути.")
    return RecognizedMedia("document", summary, "\n".join(prompt_lines))


def _local_whisper_enabled() -> bool:
    provider = os.getenv("TRANSCRIBE_PROVIDER", "").strip().lower()
    if provider in {"faster-whisper", "faster_whisper", "local", "whisper"}:
        return True
    if provider in {"openai", "api"}:
        return False
    return True


@lru_cache(maxsize=1)
def _local_whisper_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper не установлен. Установи пакет или задай OPENAI_API_KEY для OpenAI transcription."
        ) from exc

    model_name = os.getenv("FASTER_WHISPER_MODEL", "tiny").strip() or "tiny"
    device = os.getenv("FASTER_WHISPER_DEVICE", "cpu").strip() or "cpu"
    compute_type = os.getenv("FASTER_WHISPER_COMPUTE_TYPE", "int8").strip() or "int8"
    cpu_threads = int(os.getenv("FASTER_WHISPER_CPU_THREADS", "2"))
    return WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        num_workers=1,
    )


def _transcribe_voice_local(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(content)
        tmp.flush()
        segments, _info = _local_whisper_model().transcribe(
            tmp.name,
            language=os.getenv("FASTER_WHISPER_LANGUAGE", "ru").strip() or None,
            vad_filter=True,
            beam_size=int(os.getenv("FASTER_WHISPER_BEAM_SIZE", "1")),
        )
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()


def transcribe_audio_bytes(filename: str, content: bytes, content_type: str = "audio/ogg") -> str:
    if _local_whisper_enabled():
        text = _transcribe_voice_local(filename, content)
        if not text:
            raise RuntimeError("faster-whisper не вернул расшифровку голоса.")
        return text
    model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    data = _openai_multipart(
        "/audio/transcriptions",
        {
            "model": model,
            "response_format": "json",
        },
        {"file": (filename, content, content_type)},
    )
    text = str(data.get("text") or "").strip()
    if not text:
        raise RuntimeError("OpenAI не вернул расшифровку голоса.")
    return text


def transcribe_voice(bot: TelegramBot, message: dict) -> RecognizedMedia:
    media = message.get("voice") or message.get("audio")
    if not media:
        raise RuntimeError("В сообщении нет voice/audio.")
    filename, content, content_type = _download_telegram_file(bot, str(media["file_id"]))
    text = transcribe_audio_bytes(filename, content, content_type)
    return RecognizedMedia(
        "voice",
        text,
        "Пользователь отправил голосовое сообщение. Расшифровка:\n" + text,
    )


def recognize_message_media(bot: TelegramBot, message: dict) -> RecognizedMedia | None:
    if message.get("voice") or message.get("audio"):
        return transcribe_voice(bot, message)
    if message.get("photo"):
        return describe_photo(bot, message)
    if message.get("document"):
        return describe_document(bot, message)
    return None
