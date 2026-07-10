from __future__ import annotations

import json
import os
import queue
import re
import signal
import shutil
import subprocess
import threading
import textwrap
import time
from collections.abc import Callable
from datetime import datetime
from html import escape
from urllib.parse import urlparse
from pathlib import Path

from .config import ROOT


PROFILE_PATH = ROOT / "profiles" / "portfolio_profile.md"
CHECKLIST_PATH = ROOT / "profiles" / "onboarding_checklist.md"
REVIEWS_DIR = ROOT / "data" / "codex_reviews"
CHAT_DIR = ROOT / "data" / "codex_chat"
LOGIN_PID_PATH = ROOT / "data" / "codex_login.pid"
LOGIN_LOG_PATH = ROOT / "data" / "codex_login.last.log"
CODEX_AUTH_PATH = Path("/root/.codex/auth.json")
CODEX_AUTH_BACKUP_DIR = Path("/root/.codex/auth-backups")
CODEX_DEVICE_AUTH_URL = "https://auth.openai.com/codex/device"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{5}\b")
AUTH_ERROR_MARKERS = (
    "token_invalidated",
    "refresh_token_reused",
    "refreshtokenreused",
    "refresh token has already been used",
    "access token could not be refreshed",
    "please log out and sign in again",
)
CODEX_CHAT_HISTORY_LIMIT = 0
CODEX_CHAT_HISTORY_CONTENT_LIMIT = 700
DEFAULT_CHAT_TIMEOUT_SECONDS = 1800
CHAT_TIMEOUT_ENV = "CODEX_TIMEOUT_SECONDS"
TELEGRAM_MCP_TRIGGER_RE = re.compile(
    r"\b(telegram|телеграм|тг|mcp)\b.*\b("
    r"прочит\w*|найд\w*|поиск\w*|истори\w*|сообщени\w*|диалог\w*|чат\w*|"
    r"отправ\w*|редакт\w*|удал\w*|реакци\w*|контакт\w*|медиа\w*|аккаунт\w*"
    r")\b",
    re.I | re.S,
)


PRIVATE_HISTORY_RE = re.compile(
    r"\b("
    r"алкогол\w*|бух\w*|пьян\w*|похмел\w*|водк\w*|пив\w*|коньяк\w*|виск\w*|"
    r"запо\w*|трезв\w*|нарко\w*|депресс\w*|суицид\w*|болезн\w*|диагноз\w*|"
    r"лечение|таблет\w*|семейн\w*|личн\w*|интим\w*"
    r")\b",
    re.I,
)


def _is_safe_link(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def telegram_markdown_to_html(text: str) -> str:
    """Convert a practical Markdown subset to Telegram-safe HTML."""
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\u0000{len(placeholders) - 1}\u0000"

    def fenced_code(match: re.Match[str]) -> str:
        return stash(f"<pre>{escape(match.group(1).strip(), quote=False)}</pre>")

    def inline_code(match: re.Match[str]) -> str:
        return stash(f"<code>{escape(match.group(1), quote=False)}</code>")

    def link(match: re.Match[str]) -> str:
        label = escape(match.group(1), quote=False)
        url = match.group(2).strip()
        if not _is_safe_link(url):
            return stash(f"{label}: {escape(url, quote=False)}")
        return stash(f'<a href="{escape(url, quote=True)}">{label}</a>')

    def spoiler(match: re.Match[str]) -> str:
        return stash(f'<span class="tg-spoiler">{escape(match.group(1), quote=False)}</span>')

    text = text.strip()
    text = re.sub(r"```(?:[a-zA-Z0-9_-]+)?\n?([\s\S]*?)```", fenced_code, text)
    text = re.sub(r"`([^`\n]+)`", inline_code, text)
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\|\|([^|\n]+)\|\|", spoiler, text)
    text = escape(text, quote=False)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*(.+)$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", text)
    text = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    for index, value in enumerate(placeholders):
        text = text.replace(f"\u0000{index}\u0000", value)
    return text.strip()


def telegram_plain_text(text: str) -> str:
    return telegram_markdown_to_html(text)


def codex_bin_path() -> str:
    return shutil.which("codex") or "/root/.local/bin/codex"


def codex_chat_timeout_seconds() -> int:
    raw = os.getenv(CHAT_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_CHAT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CHAT_TIMEOUT_SECONDS
    return max(60, value)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def is_codex_auth_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)


def codex_auth_error_message() -> str:
    return (
        "Codex не авторизован или токен протух.\n\n"
        "Что сделать в Telegram:\n"
        "1. `/codex_logout` - сбросить старый токен.\n"
        "2. `/codex_login` - получить ссылку и одноразовый код для входа.\n"
        "3. После входа повторить запрос."
    )


def codex_auth_status() -> str:
    codex_bin = codex_bin_path()
    if not Path(codex_bin).exists():
        return f"Codex CLI не найден: `{codex_bin}`"
    result = subprocess.run(
        [codex_bin, "login", "status"],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
    return output or f"Codex login status завершился с кодом {result.returncode}."


def _kill_pending_codex_login() -> None:
    if not LOGIN_PID_PATH.exists():
        return
    try:
        pid = int(LOGIN_PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        LOGIN_PID_PATH.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    LOGIN_PID_PATH.unlink(missing_ok=True)


def codex_logout_reset() -> str:
    _kill_pending_codex_login()
    CODEX_AUTH_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    if CODEX_AUTH_PATH.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = CODEX_AUTH_BACKUP_DIR / f"auth.json.{timestamp}.bak"
        suffix = 1
        while backup_path.exists():
            backup_path = CODEX_AUTH_BACKUP_DIR / f"auth.json.{timestamp}.{suffix}.bak"
            suffix += 1
        CODEX_AUTH_PATH.rename(backup_path)
        backup_path.chmod(0o600)
        action = f"moved:{backup_path}"
    else:
        action = "no_active_auth_json"

    auth_state = "auth_present" if CODEX_AUTH_PATH.exists() else "auth_removed"
    backups = sorted(CODEX_AUTH_BACKUP_DIR.glob("auth.json*.bak"), key=lambda path: path.stat().st_mtime, reverse=True)
    latest_backup = f"\nПоследний backup: `{backups[0]}`" if backups else ""
    return (
        "Codex logout выполнен мягко: активный auth-файл не удаляется, а переносится в backup.\n\n"
        f"`{action}`\n"
        f"`{auth_state}`\n"
        f"Backup-файлов: `{len(backups)}`"
        f"{latest_backup}\n\n"
        "Если ошибка авторизации останется, сначала проверь `codex app-server` и останавливай только конкретный старый основной процесс."
    )


def start_codex_device_login(initial_wait_seconds: int = 8) -> str:
    codex_bin = codex_bin_path()
    if not Path(codex_bin).exists():
        return f"Codex CLI не найден: `{codex_bin}`"
    _kill_pending_codex_login()
    LOGIN_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [codex_bin, "login", "--device-auth"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    LOGIN_PID_PATH.write_text(str(process.pid), encoding="utf-8")
    lines: list[str] = []
    output_queue: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                output_queue.put(line)
            process.wait()
        finally:
            LOGIN_LOG_PATH.write_text(strip_ansi("".join(lines)), encoding="utf-8")
            output_queue.put(None)

    threading.Thread(target=reader, name="codex-device-login-reader", daemon=True).start()
    deadline = time.monotonic() + initial_wait_seconds
    seen: list[str] = []
    while time.monotonic() < deadline:
        try:
            item = output_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        if item is None:
            break
        seen.append(item)
        clean = strip_ansi("".join(seen))
        if CODEX_DEVICE_AUTH_URL in clean and DEVICE_CODE_RE.search(clean):
            break
    clean = strip_ansi("".join(seen) or "".join(lines)).strip()
    code_match = DEVICE_CODE_RE.search(clean)
    if code_match:
        return (
            "Открыл новый вход Codex.\n\n"
            f"Ссылка: {CODEX_DEVICE_AUTH_URL}\n"
            f"Код: `{code_match.group(0)}`\n\n"
            "Код живёт около 15 минут. После входа отправь `/codex_auth`, затем повтори запрос."
        )
    if process.poll() is not None:
        return "Не удалось начать Codex login:\n" + (clean or f"процесс завершился с кодом {process.returncode}")
    return (
        "Codex login запущен, но код не успел появиться.\n"
        f"Проверь лог: `{LOGIN_LOG_PATH}`"
    )


def _markdown_code_block(text: str, language: str = "") -> str:
    safe_text = text.replace("```", "`\u200b``").strip()
    language_suffix = language.strip()
    return f"```{language_suffix}\n{safe_text}\n```"


def summarize_codex_event(line: str) -> str | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        line = line.strip()
        return line if line else None
    if not isinstance(event, dict):
        text = str(event).strip()
        return text if text else None

    event_type = event.get("type")
    if event_type == "thread.started":
        return "**Система:** Codex открыл сессию."
    if event_type == "turn.started":
        return "**Система:** Codex начал задачу."
    if event_type == "turn.completed":
        return "**Система:** Codex завершает ответ."

    item = event.get("item") or {}
    item_type = item.get("type")
    if event_type == "item.started":
        if item_type in {"command_execution", "exec_command"}:
            command = item.get("command") or item.get("cmd") or "команду"
            return "**Команда:** выполняю\n" + _markdown_code_block(str(command), "bash")
        if item_type in {"web_search", "web.run"}:
            return "**Система:** проверяю информацию в сети."
        if item_type:
            return f"**Шаг:** `{item_type}`."
        return "**Система:** Codex работает."
    if event_type == "item.completed":
        text = item.get("text")
        if item_type == "agent_message" and text:
            return "**Сообщение Codex:**\n" + str(text).strip()
        if item_type in {"command_execution", "exec_command"}:
            command = item.get("command") or item.get("cmd") or "команда"
            return "**Команда готова:**\n" + _markdown_code_block(str(command), "bash")
        if item_type:
            return f"**Готово:** `{item_type}`."

    return None


def _fit_single_progress_entry(entry: str, limit: int) -> str:
    entry = entry.strip()
    if len(entry) <= limit:
        return entry

    code_match = re.search(r"(?s)^(.*?```[a-zA-Z0-9_-]*\n)(.*)(\n```)\s*$", entry)
    if code_match:
        prefix, body, suffix = code_match.groups()
        marker = "\n...\n"
        available = limit - len(prefix) - len(marker) - len(suffix)
        if available > 20:
            return prefix + body[:available].rstrip() + marker + suffix

    return entry[: max(0, limit - 3)].rstrip() + "..."


def _tail_for_draft(lines: list[str], limit: int = 2200) -> str:
    entries = [line.strip() for line in lines if line.strip()]
    selected: list[str] = []
    for entry in reversed(entries):
        candidate = "\n".join([entry, *selected])
        if len(candidate) <= limit:
            selected.insert(0, entry)
            continue
        if not selected:
            selected.insert(0, _fit_single_progress_entry(entry, limit))
        break
    return "\n".join(selected).strip()


def is_private_history_content(text: str) -> bool:
    return bool(PRIVATE_HISTORY_RE.search(text))


def _chat_needs_telegram_mcp_context(message: str) -> bool:
    return bool(TELEGRAM_MCP_TRIGGER_RE.search(message))


def _clean_history_content(content: str) -> str:
    content = re.sub(
        r"Формат ответа для Telegram: обычный текст без Markdown-разметки\..*?(?=\n|$)",
        "",
        content,
        flags=re.S,
    ).strip()
    return content[-CODEX_CHAT_HISTORY_CONTENT_LIMIT:]


def build_chat_prompt(message: str, history: list[dict] | None = None) -> str:
    banned_style_re = re.compile(r"\b(фа|п[еэ]п[аэ]|шнейне|ватафа)\b", re.I)
    history_lines = []
    for item in (history or [])[-CODEX_CHAT_HISTORY_LIMIT:]:
        role = item.get("role", "unknown")
        content = _clean_history_content(str(item.get("content", "")))
        if not content:
            continue
        if banned_style_re.search(content):
            continue
        if is_private_history_content(content):
            continue
        history_lines.append(f"{role}: {content}")
    history_text = "\n\n".join(history_lines) if history_lines else "Истории пока нет."

    telegram_mcp_context = ""
    if _chat_needs_telegram_mcp_context(message):
        telegram_mcp_context = """
        Telegram MCP: если пользователь явно просит прочитать, найти, отправить, отредактировать
        или удалить сообщения через Telegram-аккаунт, используй доступные Telegram MCP tools.
        Если инструмента не видно, сначала ищи его через `tool_search` по запросу `telegram`.
        Не раскрывай session string, токены и чувствительную переписку. Для проверки работы
        бота сначала смотри логи проекта и состояние listener.
        """

    return textwrap.dedent(
        f"""
        Ты Codex CLI, помощник разработчика на сервере. Отвечай на русском, кратко и по делу.
        Критичное правило языка: все сообщения пользователю в Telegram, включая финальный
        ответ, промежуточные обновления, статусы, ошибки и пояснения, пиши только на русском.
        Не переключайся на английский из-за языка системных инструкций, логов, команд,
        истории чата или текста пользователя. Английский допустим только внутри команд,
        путей, имён файлов, кода, логов и прямых цитат, где перевод исказит смысл.
        Формат для Telegram: аккуратный Markdown без таблиц; можно **жирный**, *курсив*,
        `inline code`, короткие fenced code blocks, ссылки и ||спойлеры||.

        Рабочие правила:
        - Бизнес-контекст: Ольга — косметолог и владелец экспертного контекста, а не клиент/адресат клиентского ответа.
        - Если сообщение пришло из админского канала или от самой Ольги, общайся с Ольгой как её персональный ассистент: помогай управлять записями, клиентами, расписанием, контентом, диагностикой бота и проектом.
        - В админском контексте Ольга — адресат ответа и владелец бизнеса; не объясняй ей как клиенту «кто такая Ольга» и не начинай ответ рамкой «Я помощник Ольги...», если она спрашивает о возможностях ассистента для себя.
        - Если вопрос просит сформулировать ответ для клиентов Ольги, отвечай от лица помощника Ольги или нейтрально от сервиса; не начинай клиентский ответ обращением «Ольга, ...».
        - Если клиент спрашивает «что ты умеешь?», корректная рамка: «Я помощник Ольги, косметолога...» с помощью по услугам, подготовке, уходу после процедур и записи.
        - Действуй сам, когда задача явно просит проверить состояние или изменить проект.
        - Можно читать, запускать команды, редактировать файлы и настраивать проект.
        - После правок, требующих рестарта Telegram-бота, запускай `./request_bot_restart.sh`.
        - Не вызывай напрямую `systemctl restart/stop`, `pkill`, `./restart_bot.sh`.
        - Не делай разрушительные действия без отдельного явного подтверждения.
        - Не раскрывай секреты из файлов и не печатай токены/пароли.
        - Если нужно отправить файл: `[[send_file:relative/or/absolute/path]]`.
        - Если нужно отправить фото: `[[send_photo:relative/or/absolute/path]]`.
        - Не пересказывай чужую чувствительную личную историю; нейтральные технические темы можно.
        - IPTV: только легальные публичные бесплатные M3U, без пиратских плейлистов и обхода доступа.

        {telegram_mcp_context.strip()}

        Короткая история Telegram-чата:
        {history_text}

        Сообщение пользователя из Telegram:
        {message}
        """
    ).strip()


def _run_codex_streaming(
    cmd: list[str],
    output_path: Path,
    debug_path: Path,
    timeout_seconds: int,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[str, Path]:
    stdout_lines: list[str] = []
    progress_lines: list[str] = []
    started = time.monotonic()
    timed_out = False

    try:
        process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        text = f"Не удалось запустить Codex CLI: {exc}"
        output_path.write_text(text, encoding="utf-8")
        return text, output_path

    assert process.stdout is not None
    while True:
        if time.monotonic() - started > timeout_seconds:
            timed_out = True
            process.kill()
            break

        line = process.stdout.readline()
        if line:
            line = line.rstrip("\n")
            stdout_lines.append(line)
            summary = summarize_codex_event(line)
            if summary:
                progress_lines.append(summary)
                if progress_callback:
                    progress_callback(_tail_for_draft(progress_lines))
            continue

        if process.poll() is not None:
            break
        time.sleep(0.1)

    remaining = process.stdout.read()
    if remaining:
        for line in remaining.splitlines():
            stdout_lines.append(line)
            summary = summarize_codex_event(line)
            if summary:
                progress_lines.append(summary)
    process.stdout.close()

    combined_output = "\n".join(stdout_lines)
    debug_path.write_text("STDOUT:\n" + combined_output, encoding="utf-8")

    if timed_out:
        text = "Codex не успел ответить за отведенное время."
        output_path.write_text(text, encoding="utf-8")
        return text, output_path

    text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
    if not text:
        if is_codex_auth_error(combined_output):
            text = codex_auth_error_message()
            output_path.write_text(text, encoding="utf-8")
            return text, output_path

        agent_messages = []
        for line in stdout_lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item") or {}
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message" and item.get("text"):
                agent_messages.append(str(item["text"]))
        fallback = "\n".join(agent_messages).strip() or "\n".join(stdout_lines).strip()
        text = fallback[-3500:] if fallback else "Codex завершился, но не вернул ответ."
        output_path.write_text(text, encoding="utf-8")

    return text, output_path


def build_prompt(lead: dict) -> str:
    profile = PROFILE_PATH.read_text(encoding="utf-8")
    checklist = CHECKLIST_PATH.read_text(encoding="utf-8")
    lead_json = json.dumps(lead, ensure_ascii=False, indent=2)
    return textwrap.dedent(
        f"""
        Ты оцениваешь заявку или рабочий сценарий для операционной системы косметолога.

        Профиль возможностей:
        {profile}

        Политика и онбординг:
        {checklist}

        Входные данные:
        {lead_json}

        Дай краткий ответ на русском строго по структуре:
        1. Вердикт: делаем / не делаем / нужно уточнить.
        2. Почему подходит или не подходит.
        3. Подозрительность и риски.
        4. Оценка сроков в днях.
        5. Что нужно проверить в Avito, Telegram, YCLIENTS или knowledge.
        6. Какие предохранители нельзя отключать без контрольного теста.
        7. Короткий следующий шаг для оператора.

        Не предлагай нарушать правила площадок, отключать предохранители без проверки или писать клиентам без достаточного контекста.
        """
    ).strip()


def analyze_with_codex(lead: dict, timeout_seconds: int = 240) -> tuple[str, Path]:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = REVIEWS_DIR / f"{lead['id']}.txt"
    output_path.unlink(missing_ok=True)
    prompt = build_prompt(lead)
    codex_bin = codex_bin_path()
    if not Path(codex_bin).exists():
        text = f"Codex CLI не найден: {codex_bin}"
        output_path.write_text(text, encoding="utf-8")
        return text, output_path
    cmd = [
        codex_bin,
        "-a",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(ROOT),
        "--output-last-message",
        str(output_path),
        prompt,
    ]
    try:
        result = subprocess.run(cmd, check=False, timeout=timeout_seconds, cwd=ROOT, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        text = "Codex не успел завершить анализ за отведенное время."
        output_path.write_text(text, encoding="utf-8")
        return text, output_path
    except OSError as exc:
        text = f"Не удалось запустить Codex CLI: {exc}"
        output_path.write_text(text, encoding="utf-8")
        return text, output_path

    debug_path = REVIEWS_DIR / f"{lead['id']}.debug.log"
    debug_path.write_text(
        "STDOUT:\n"
        + (result.stdout or "")
        + "\n\nSTDERR:\n"
        + (result.stderr or ""),
        encoding="utf-8",
    )
    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0 and is_codex_auth_error(combined_output):
        text = codex_auth_error_message()
        output_path.write_text(text, encoding="utf-8")
        return text, output_path

    if output_path.exists():
        text = output_path.read_text(encoding="utf-8").strip()
    else:
        fallback = (result.stdout or result.stderr or "").strip()
        text = fallback[-3500:] if fallback else "Codex завершился, но не вернул финальное сообщение."
        output_path.write_text(text, encoding="utf-8")
    if text == "Codex завершился, но не вернул финальное сообщение." and result.returncode != 0:
        fallback = (result.stderr or result.stdout or "").strip()
        if fallback:
            text = fallback[-3500:]
            output_path.write_text(text, encoding="utf-8")
    return text, output_path


def chat_with_codex(
    message: str,
    history: list[dict] | None = None,
    timeout_seconds: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    raw_prompt: bool = False,
) -> tuple[str, Path]:
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = str(abs(hash(message)))[:18]
    output_path = CHAT_DIR / f"{safe_name}.txt"
    output_path.unlink(missing_ok=True)
    prompt = message if raw_prompt else build_chat_prompt(message, history)
    codex_bin = codex_bin_path()
    if not Path(codex_bin).exists():
        text = f"Codex CLI не найден: {codex_bin}"
        output_path.write_text(text, encoding="utf-8")
        return text, output_path
    cmd = [
        codex_bin,
        "-a",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
        "--cd",
        str(ROOT),
        "--json",
        "--output-last-message",
        str(output_path),
        prompt,
    ]
    debug_path = CHAT_DIR / f"{safe_name}.debug.log"
    text, path = _run_codex_streaming(
        cmd,
        output_path,
        debug_path,
        timeout_seconds if timeout_seconds is not None else codex_chat_timeout_seconds(),
        progress_callback,
    )
    output_path.write_text(text, encoding="utf-8")
    return text, path
