from __future__ import annotations

import os
import re
import subprocess
import time
from html import unescape
from pathlib import Path

from .config import ROOT


TTS_DIR = ROOT / "data" / "tts"
GENERATOR_PATH = ROOT / "scripts" / "silero_tts_generate.py"
MAX_TTS_FILES = 40


def tts_enabled() -> bool:
    return os.getenv("TTS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def text_for_tts(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[\[send_(?:file|photo):.+?\]\]", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\|\|([^|]+)\|\|", r"\1", text)
    text = re.sub(r"[*_~#>]+", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"https?://\S+", " ссылка ", text)
    text = re.sub(r"\s+", " ", text).strip()
    limit = int(os.getenv("TTS_MAX_CHARS", "900"))
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].strip() or text[:limit]
    return text


def cleanup_old_tts_files() -> None:
    if not TTS_DIR.exists():
        return
    files = sorted(TTS_DIR.glob("*.ogg"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[MAX_TTS_FILES:]:
        path.unlink(missing_ok=True)


def synthesize_voice(text: str) -> Path | None:
    if not tts_enabled():
        return None
    clean_text = text_for_tts(text)
    if not clean_text:
        return None

    venv = os.getenv("TTS_VENV", ".venv_tts").strip() or ".venv_tts"
    python_path = Path(venv)
    if not python_path.is_absolute():
        python_path = ROOT / python_path
    python_bin = python_path / "bin" / "python"
    if not python_bin.exists():
        raise RuntimeError(f"TTS python not found: {python_bin}")

    TTS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_tts_files()
    output_path = TTS_DIR / f"codex-{int(time.time() * 1000)}.ogg"
    cmd = [
        str(python_bin),
        str(GENERATOR_PATH),
        "--text",
        clean_text,
        "--output",
        str(output_path),
        "--model",
        os.getenv("TTS_SILERO_MODEL", "v4_ru").strip() or "v4_ru",
        "--speaker",
        os.getenv("TTS_SILERO_SPEAKER", "aidar").strip() or "aidar",
    ]
    subprocess.run(cmd, cwd=ROOT, check=True, timeout=180, capture_output=True, text=True)
    return output_path
