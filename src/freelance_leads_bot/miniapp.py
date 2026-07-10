from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import ROOT, Settings


STATIC_DIR = Path(__file__).with_name("miniapp_static")
MAX_OUTPUT_CHARS = 60000
COMMAND_TIMEOUT_SECONDS = 25
AUTH_MAX_AGE_SECONDS = 86400
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
DOWNLOAD_TOKEN_TTL_SECONDS = 120
DOWNLOAD_TOKENS: dict[str, tuple[float, str]] = {}
DOWNLOAD_TOKENS_LOCK = threading.Lock()


@dataclass(frozen=True)
class MiniAppUser:
    user_id: int | None
    username: str
    first_name: str


@dataclass(frozen=True)
class DownloadPayload:
    path: Path
    filename: str
    content_type: str
    cleanup: bool = False


def prune_download_tokens(now: float | None = None) -> None:
    current = time.time() if now is None else now
    expired = [token for token, (expires_at, _) in DOWNLOAD_TOKENS.items() if expires_at <= current]
    for token in expired:
        DOWNLOAD_TOKENS.pop(token, None)


def create_download_token(path_text: str | None) -> str:
    if not path_text:
        raise ValueError("Path is required.")
    token = secrets.token_urlsafe(32)
    with DOWNLOAD_TOKENS_LOCK:
        prune_download_tokens()
        DOWNLOAD_TOKENS[token] = (time.time() + DOWNLOAD_TOKEN_TTL_SECONDS, path_text)
    return token


def get_download_token_path(token: str | None) -> str:
    if not token:
        raise PermissionError("Download token is required.")
    with DOWNLOAD_TOKENS_LOCK:
        prune_download_tokens()
        item = DOWNLOAD_TOKENS.get(token)
    if not item:
        raise PermissionError("Download token is expired or invalid.")
    return item[1]


def public_download_url(settings: Settings, token: str) -> str:
    base = settings.miniapp_public_url.rstrip("/") + "/"
    query = urllib.parse.urlencode({"token": token})
    return urllib.parse.urljoin(base, f"api/download?{query}")


def validate_init_data(init_data: str, bot_token: str, allowed_usernames: list[str]) -> MiniAppUser:
    parsed = urllib.parse.parse_qs(init_data, strict_parsing=True)
    received_hash = (parsed.get("hash") or [""])[0]
    if not received_hash:
        raise PermissionError("No Telegram auth hash.")

    pairs: list[str] = []
    for key in sorted(parsed):
        if key == "hash":
            continue
        value = parsed[key][0]
        pairs.append(f"{key}={value}")
    data_check_string = "\n".join(pairs)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise PermissionError("Bad Telegram auth hash.")

    auth_date_raw = (parsed.get("auth_date") or ["0"])[0]
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise PermissionError("Bad auth date.") from exc
    if time.time() - auth_date > AUTH_MAX_AGE_SECONDS:
        raise PermissionError("Telegram auth data expired.")

    user_raw = (parsed.get("user") or ["{}"])[0]
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise PermissionError("Bad Telegram user payload.") from exc
    username = str(user.get("username") or "").strip().lower()
    allowed = {name.lower().lstrip("@") for name in allowed_usernames}
    if username not in allowed:
        raise PermissionError("User is not allowed.")
    return MiniAppUser(
        user_id=user.get("id") if isinstance(user.get("id"), int) else None,
        username=username,
        first_name=str(user.get("first_name") or ""),
    )


def path_info(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        stat = None
    return {
        "name": path.name or str(path),
        "path": str(path),
        "is_dir": path.is_dir(),
        "size": stat.st_size if stat else None,
        "mtime": int(stat.st_mtime) if stat else None,
    }


def list_directory(path_text: str | None, default_cwd: Path) -> dict[str, Any]:
    path = Path(path_text or default_cwd).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if path.is_file():
        path = path.parent
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        try:
            entries.append(path_info(child))
        except OSError:
            continue
    return {
        "cwd": str(path),
        "parent": str(path.parent) if path.parent != path else "",
        "entries": entries[:500],
    }


def download_filename(path: Path) -> str:
    name = path.name or "download"
    return "".join(char if char not in '/\\\0\r\n"' else "_" for char in name) or "download"


def directory_size(path: Path, limit: int = MAX_DOWNLOAD_BYTES) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
        if total > limit:
            raise ValueError(f"Directory is too large to download over Mini App ({limit // 1024 // 1024}MB limit).")
    return total


def prepare_download(path_text: str | None, default_cwd: Path) -> DownloadPayload:
    if not path_text:
        raise ValueError("Path is required.")
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    if path.is_file():
        size = path.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"File is too large to download over Mini App ({MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit).")
        return DownloadPayload(
            path=path,
            filename=download_filename(path),
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )

    if path.is_dir():
        directory_size(path)
        archive_name = f"{download_filename(path)}.tar.gz"
        tmp = tempfile.NamedTemporaryFile(prefix="miniapp-download-", suffix=".tar.gz", delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            with tarfile.open(tmp_path, "w:gz") as archive:
                archive.add(path, arcname=download_filename(path))
            if tmp_path.stat().st_size > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"Archive is too large to download over Mini App ({MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit).")
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        return DownloadPayload(tmp_path, archive_name, "application/gzip", cleanup=True)

    raise ValueError(f"Path is not a regular file or directory: {path}")


def download_link_metadata(path_text: str | None, default_cwd: Path) -> dict[str, str]:
    if not path_text:
        raise ValueError("Path is required.")
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if path.is_file():
        if path.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"File is too large to download over Mini App ({MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit).")
        return {"filename": download_filename(path)}
    if path.is_dir():
        directory_size(path)
        return {"filename": f"{download_filename(path)}.tar.gz"}
    raise ValueError(f"Path is not a regular file or directory: {path}")


def read_cpu_times() -> tuple[int, int]:
    line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    parts = [int(value) for value in line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    return sum(parts), idle


def process_count() -> int:
    return sum(1 for path in Path("/proc").iterdir() if path.name.isdigit())


def uptime_seconds() -> int:
    raw = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
    return int(float(raw))


def network_totals() -> dict[str, int]:
    rx_bytes = 0
    tx_bytes = 0
    for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
        name, _, values = line.partition(":")
        if name.strip() == "lo":
            continue
        parts = values.split()
        if len(parts) >= 16:
            rx_bytes += int(parts[0])
            tx_bytes += int(parts[8])
    return {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}


def system_stats(default_cwd: Path) -> dict[str, Any]:
    total_a, idle_a = read_cpu_times()
    time.sleep(0.08)
    total_b, idle_b = read_cpu_times()
    total_delta = max(1, total_b - total_a)
    idle_delta = max(0, idle_b - idle_a)
    cpu_percent = max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))

    meminfo: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        meminfo[key] = int(value.strip().split()[0]) * 1024
    memory_total = meminfo.get("MemTotal", 0)
    memory_available = meminfo.get("MemAvailable", 0)
    memory_used = max(0, memory_total - memory_available)
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)

    disk = shutil.disk_usage(default_cwd)
    vfs = os.statvfs(default_cwd)
    inode_total = vfs.f_files
    inode_free = vfs.f_ffree
    inode_used = max(0, inode_total - inode_free)
    cpu_count = os.cpu_count() or 1
    load_avg = os.getloadavg()
    net = network_totals()
    return {
        "cpu_percent": round(cpu_percent, 1),
        "load": [round(value, 2) for value in load_avg],
        "load_percent": round((load_avg[0] / cpu_count) * 100, 1) if cpu_count else 0,
        "cpu_count": cpu_count,
        "process_count": process_count(),
        "uptime_seconds": uptime_seconds(),
        "memory_total": memory_total,
        "memory_used": memory_used,
        "memory_percent": round((memory_used / memory_total) * 100, 1) if memory_total else 0,
        "memory_available": memory_available,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_percent": round((swap_used / swap_total) * 100, 1) if swap_total else 0,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
        "disk_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
        "inode_total": inode_total,
        "inode_used": inode_used,
        "inode_percent": round((inode_used / inode_total) * 100, 1) if inode_total else 0,
        **net,
    }


def run_command(command: str, cwd_text: str | None, default_cwd: Path) -> dict[str, Any]:
    command = command.strip()
    if not command:
        raise ValueError("Command is empty.")
    cwd = Path(cwd_text or default_cwd).expanduser().resolve()
    if cwd.is_file():
        cwd = cwd.parent
    if not cwd.exists():
        raise FileNotFoundError(f"Working directory not found: {cwd}")

    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env={**os.environ, "HOME": os.environ.get("HOME", "/root")},
        shell=True,
        executable="/bin/bash",
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[-MAX_OUTPUT_CHARS:]
        truncated = True
    else:
        truncated = False
    next_cwd = cwd
    if command.startswith("cd ") or command == "cd":
        try:
            parts = shlex.split(command)
            target = parts[1] if len(parts) > 1 else os.path.expanduser("~")
            next_cwd = (cwd / target).expanduser().resolve() if not Path(target).is_absolute() else Path(target).expanduser().resolve()
            if not next_cwd.is_dir():
                next_cwd = cwd
        except ValueError:
            next_cwd = cwd
    return {
        "command": command,
        "cwd": str(next_cwd),
        "returncode": completed.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output": output,
        "truncated": truncated,
    }


class MiniAppHandler(BaseHTTPRequestHandler):
    server_version = "FreelanceMiniApp/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def app_settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/miniapp", "/miniapp/"}:
            self.send_static("index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/miniapp/api/download":
            try:
                query = urllib.parse.parse_qs(parsed.query)
                token = (query.get("token") or [""])[0]
                path_text = get_download_token_path(token)
                self.send_download(prepare_download(path_text, self.app_settings.miniapp_default_cwd))
            except PermissionError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/miniapp/static/"):
            name = parsed.path.removeprefix("/miniapp/static/")
            content_type = "text/css; charset=utf-8" if name.endswith(".css") else "application/javascript; charset=utf-8"
            self.send_static(name, content_type)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/miniapp", "/miniapp/"}:
            self.send_static("index.html", "text/html; charset=utf-8", include_body=False)
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:
        try:
            self.require_user()
            payload = self.read_json()
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/miniapp/api/list":
                self.send_json(list_directory(payload.get("path"), self.app_settings.miniapp_default_cwd))
                return
            if parsed.path == "/miniapp/api/download":
                self.send_download(prepare_download(payload.get("path"), self.app_settings.miniapp_default_cwd))
                return
            if parsed.path == "/miniapp/api/download-link":
                metadata = download_link_metadata(payload.get("path"), self.app_settings.miniapp_default_cwd)
                token = create_download_token(payload.get("path"))
                self.send_json(
                    {
                        "url": public_download_url(self.app_settings, token),
                        "filename": metadata["filename"],
                        "expires_in": DOWNLOAD_TOKEN_TTL_SECONDS,
                    }
                )
                return
            if parsed.path == "/miniapp/api/exec":
                self.send_json(run_command(str(payload.get("command") or ""), payload.get("cwd"), self.app_settings.miniapp_default_cwd))
                return
            if parsed.path == "/miniapp/api/stats":
                self.send_json(system_stats(self.app_settings.miniapp_default_cwd))
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except subprocess.TimeoutExpired:
            self.send_json({"error": f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s"}, HTTPStatus.REQUEST_TIMEOUT)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def require_user(self) -> MiniAppUser:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("tma "):
            raise PermissionError("No Telegram Mini App authorization.")
        return validate_init_data(auth.removeprefix("tma ").strip(), self.app_settings.telegram_bot_token, self.app_settings.allowed_telegram_usernames)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length > 512000:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def send_static(self, name: str, content_type: str, include_body: bool = True) -> None:
        path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in path.parents and path != STATIC_DIR.resolve():
            self.send_json({"error": "bad path"}, HTTPStatus.BAD_REQUEST)
            return
        if not path.is_file():
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, payload: DownloadPayload) -> None:
        quoted_name = urllib.parse.quote(payload.filename)
        ascii_name = download_filename(Path(payload.filename)).encode("ascii", "ignore").decode("ascii") or "download"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", payload.content_type)
        self.send_header("Content-Length", str(payload.path.stat().st_size))
        self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted_name}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with payload.path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile)
        finally:
            if payload.cleanup:
                payload.path.unlink(missing_ok=True)


def start_miniapp_server(settings: Settings) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((settings.miniapp_host, settings.miniapp_port), MiniAppHandler)
    server.settings = settings  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="telegram-miniapp", daemon=True)
    thread.start()
    return server
