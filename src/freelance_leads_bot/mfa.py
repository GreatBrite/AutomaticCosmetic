from __future__ import annotations

import base64
import hmac
import json
import re
import struct
import time
from hashlib import sha1, sha256, sha512
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import ROOT


MFA_PATH = ROOT / "data" / "mfa_totp.json"
SECRET_RE = re.compile(r"^[A-Z2-7=\s-]{16,}$", re.I)
HASHES = {
    "SHA1": sha1,
    "SHA256": sha256,
    "SHA512": sha512,
}


def _normalize_secret(secret: str) -> str:
    value = re.sub(r"[\s-]+", "", secret.strip()).upper()
    if not SECRET_RE.match(value):
        raise ValueError("Секрет должен быть Base32 из authenticator app или otpauth:// URI.")
    return value.rstrip("=")


def _parse_totp_input(raw: str) -> dict:
    value = raw.strip()
    if value.startswith("otpauth://"):
        parsed = urlparse(value)
        if parsed.netloc != "totp":
            raise ValueError("Поддерживается только otpauth://totp.")
        params = parse_qs(parsed.query)
        secret = _normalize_secret((params.get("secret") or [""])[0])
        issuer = (params.get("issuer") or [""])[0].strip()
        label = unquote(parsed.path.lstrip("/")).strip()
        algorithm = (params.get("algorithm") or ["SHA1"])[0].upper()
        digits = int((params.get("digits") or ["6"])[0])
        period = int((params.get("period") or ["30"])[0])
    else:
        secret = _normalize_secret(value)
        issuer = ""
        label = "server-authenticator"
        algorithm = "SHA1"
        digits = 6
        period = 30

    if algorithm not in HASHES:
        raise ValueError("Поддерживаются алгоритмы SHA1, SHA256, SHA512.")
    if digits not in {6, 7, 8}:
        raise ValueError("Поддерживаются коды длиной 6, 7 или 8 цифр.")
    if not 10 <= period <= 120:
        raise ValueError("Период TOTP должен быть от 10 до 120 секунд.")

    return {
        "secret": secret,
        "issuer": issuer,
        "label": label,
        "algorithm": algorithm,
        "digits": digits,
        "period": period,
        "created_at": int(time.time()),
    }


def save_totp_secret(raw: str) -> str:
    data = _parse_totp_input(raw)
    MFA_PATH.parent.mkdir(parents=True, exist_ok=True)
    MFA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    MFA_PATH.chmod(0o600)
    return mfa_status()


def delete_totp_secret() -> str:
    MFA_PATH.unlink(missing_ok=True)
    return "MFA-секрет удалён с сервера."


def _load_totp() -> dict:
    if not MFA_PATH.exists():
        raise FileNotFoundError("MFA-секрет ещё не настроен. Используй `/mfa_set <secret-or-otpauth-uri>`.")
    return json.loads(MFA_PATH.read_text(encoding="utf-8"))


def _secret_bytes(secret: str) -> bytes:
    padded = secret + ("=" * ((8 - len(secret) % 8) % 8))
    return base64.b32decode(padded, casefold=True)


def totp_code(at_time: int | None = None) -> tuple[str, int]:
    data = _load_totp()
    now = int(time.time() if at_time is None else at_time)
    period = int(data.get("period", 30))
    digits = int(data.get("digits", 6))
    algorithm = str(data.get("algorithm", "SHA1")).upper()
    counter = now // period
    digest = hmac.new(_secret_bytes(str(data["secret"])), struct.pack(">Q", counter), HASHES[algorithm]).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    code = str(binary % (10**digits)).zfill(digits)
    seconds_left = period - (now % period)
    return code, seconds_left


def mfa_status() -> str:
    if not MFA_PATH.exists():
        return "MFA не настроена. Используй `/mfa_set <secret-or-otpauth-uri>`."
    data = _load_totp()
    label = data.get("label") or "без названия"
    issuer = data.get("issuer") or "без issuer"
    return (
        "MFA настроена.\n"
        f"Аккаунт: `{label}`\n"
        f"Issuer: `{issuer}`\n"
        f"Алгоритм: `{data.get('algorithm', 'SHA1')}`, цифр: `{data.get('digits', 6)}`, период: `{data.get('period', 30)}s`"
    )


def mfa_code_text() -> str:
    code, seconds_left = totp_code()
    return f"Текущий MFA-код: `{code}`\nОбновится через {seconds_left} сек."
