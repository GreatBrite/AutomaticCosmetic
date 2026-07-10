#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
STAGE="$DIST_DIR/freelance-leads-bot-portable"
ARCHIVE="$DIST_DIR/freelance-leads-bot-portable.tar.gz"

rm -rf "$STAGE" "$ARCHIVE"
mkdir -p "$STAGE"

rsync -a "$ROOT_DIR/" "$STAGE/" \
  --exclude '.env' \
  --exclude '.venv_tts/' \
  --exclude '.codegraph/' \
  --exclude 'dist/' \
  --exclude 'data/' \
  --exclude 'restart_bot.sh' \
  --exclude 'screenshots/' \
  --exclude 'tests/' \
  --exclude 'docs/telegram_codex_agent.md' \
  --exclude 'mcp/telegram.mcp.toml.example' \
  --exclude 'mcp/*.json' \
  --include 'mcp/*.example' \
  --exclude '**/__pycache__/' \
  --exclude '*.pyc'

mkdir -p "$STAGE/data" "$STAGE/tools"

rm -f "$STAGE/src/freelance_leads_bot/account_listener.py"
rm -f "$STAGE/src/freelance_leads_bot/telegram_scout.py"

python3 - "$STAGE" <<'PY'
from pathlib import Path
import re
import sys

stage = Path(sys.argv[1])

config = stage / "src/freelance_leads_bot/config.py"
text = config.read_text(encoding="utf-8")
text = re.sub(
    r'miniapp_public_url=os\.getenv\("MINIAPP_PUBLIC_URL",\s*"[^"]*"\),',
    'miniapp_public_url=os.getenv("MINIAPP_PUBLIC_URL", ""),',
    text,
)
text = text.replace(
    'telegram_account_listener_enabled=os.getenv("TELEGRAM_ACCOUNT_LISTENER_ENABLED", "true").lower()',
    'telegram_account_listener_enabled=os.getenv("TELEGRAM_ACCOUNT_LISTENER_ENABLED", "false").lower()',
)
text = text.replace(
    'telegram_scout_enabled=os.getenv("TELEGRAM_SCOUT_ENABLED", "true").lower()',
    'telegram_scout_enabled=os.getenv("TELEGRAM_SCOUT_ENABLED", "false").lower()',
)
config.write_text(text, encoding="utf-8")

main = stage / "src/freelance_leads_bot/main.py"
text = main.read_text(encoding="utf-8")
text = text.replace("from .account_listener import TelegramAccountListener\n", "")
start = text.index("def start_telegram_account_listener(")
end = text.index("\ndef start_codex_chat_task(", start)
stub = '''def start_telegram_account_listener(
    settings: Settings,
    store: LeadStore,
    codex_tasks: CodexTaskRegistry,
) -> None:
    runtime_log("account_listener unavailable in portable bot-only build")

'''
text = text[:start] + stub + text[end + 1 :]
main.write_text(text, encoding="utf-8")

env = stage / ".env.example"
env.write_text("""TELEGRAM_BOT_TOKEN=put_bot_token_here
TELEGRAM_CHAT_ID=
ALLOWED_TELEGRAM_USERNAMES=
TELEGRAM_ACCOUNT_LISTENER_ENABLED=false
TELEGRAM_API_ID=0
TELEGRAM_API_HASH=
TELEGRAM_SESSION_STRING=
TELEGRAM_SCOUT_ENABLED=false
MINIAPP_PUBLIC_URL=
MINIAPP_HOST=127.0.0.1
MINIAPP_PORT=8045
MINIAPP_DEFAULT_CWD=/opt/freelance_leads_bot
OPENAI_API_KEY=put_openai_api_key_here
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
TRANSCRIBE_PROVIDER=faster-whisper
FASTER_WHISPER_MODEL=tiny
FASTER_WHISPER_DEVICE=cpu
FASTER_WHISPER_COMPUTE_TYPE=int8
FASTER_WHISPER_CPU_THREADS=2
FASTER_WHISPER_LANGUAGE=ru
FASTER_WHISPER_BEAM_SIZE=1
TTS_ENABLED=0
TTS_VENV=.venv_tts
TTS_SILERO_MODEL=v4_ru
TTS_SILERO_SPEAKER=aidar
TTS_MAX_CHARS=900
DB_PATH=data/leads.sqlite3
MAX_ITEMS_PER_RUN=12
MIN_SCORE=1
POLL_SECONDS=900
KEYWORDS=python,django,fastapi,flask,telegram bot,automation,scraping,parser,web scraping,backend,api,ai,openai,chatgpt,llm,data,sqlite,postgres
NEGATIVE_KEYWORDS=senior manager,unpaid,volunteer,internship,onsite only,clearance
""", encoding="utf-8")

readme = stage / "README.md"
text = readme.read_text(encoding="utf-8")
account_start = text.find("Telegram scout:")
notes_start = text.find("## Notes")
if account_start != -1 and notes_start != -1:
    text = text[:account_start] + "Telegram account/MTProto listener is intentionally excluded from this portable bot-only build.\n\n" + text[notes_start:]
readme.write_text(text, encoding="utf-8")
PY

if [[ -x /root/.codex/packages/standalone/current/bin/codex ]]; then
  mkdir -p "$STAGE/tools/codex"
  rsync -aL /root/.codex/packages/standalone/current/ "$STAGE/tools/codex/"
fi

if [[ -x /root/.local/bin/codegraph ]]; then
  mkdir -p "$STAGE/tools/codegraph"
  cp -L /root/.local/bin/codegraph "$STAGE/tools/codegraph/codegraph"
  chmod +x "$STAGE/tools/codegraph/codegraph"
fi

if [[ -d /root/.codex/skills ]]; then
  mkdir -p "$STAGE/tools/codex_skills"
  rsync -a /root/.codex/skills/ "$STAGE/tools/codex_skills/" \
    --exclude '**/.temp-*' \
    --exclude '**/node_modules/' \
    --exclude '**/__pycache__/'
fi

find "$STAGE" -type f \( -name '*.sh' -o -path '*/scripts/*' \) -exec chmod +x {} \;

tar -C "$DIST_DIR" -czf "$ARCHIVE" freelance-leads-bot-portable
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"

echo "$ARCHIVE"
