#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export AVITO_SEND_ENABLED=false
export AVITO_CODEX_ENABLED=true
export AVITO_TEST_MODE=true
export AVITO_LIVE_TELEGRAM_CHAT_ID="${AVITO_LIVE_TELEGRAM_CHAT_ID:--1003784160049}"

PYTHON="${PYTHON:-python3}"
if [[ -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
fi

"$PYTHON" - <<'PY'
from scripts.avito_live_telegram_relay import (
    HANDOFF_OUTBOX_PATH,
    HANDOFF_SEEN_PATH,
    PREVIEW_OUTBOX_PATH,
    PREVIEW_SEEN_PATH,
    handoff_event_key,
    iter_handoff_outbox,
    iter_preview_outbox,
    preview_event_key,
)
from src.freelance_leads_bot.integrations.avito_dedup import PersistentProcessedEventStore

preview_seen = PersistentProcessedEventStore(PREVIEW_SEEN_PATH)
for row in iter_preview_outbox(PREVIEW_OUTBOX_PATH, since_ts=0):
    preview_seen.mark_once(preview_event_key(row))

handoff_seen = PersistentProcessedEventStore(HANDOFF_SEEN_PATH)
for row in iter_handoff_outbox(HANDOFF_OUTBOX_PATH, since_ts=0):
    handoff_seen.mark_once(handoff_event_key(row))
PY

POLLER_PID=""
RELAY_PID=""

start_poller() {
  "$PYTHON" scripts/avito_missed_message_poller.py &
  POLLER_PID=$!
  echo "started avito_missed_message_poller pid=$POLLER_PID"
}

start_relay() {
  "$PYTHON" scripts/avito_live_telegram_relay.py &
  RELAY_PID=$!
  echo "started avito_live_telegram_relay pid=$RELAY_PID"
}

is_running() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ "$(ps -p "$pid" -o stat= 2>/dev/null | tr -d ' ')" != Z* ]]
}

cleanup() {
  if [[ -n "${POLLER_PID:-}" ]]; then
    kill "$POLLER_PID" 2>/dev/null || true
  fi
  if [[ -n "${RELAY_PID:-}" ]]; then
    kill "$RELAY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'cleanup; exit 0' INT TERM

start_poller
start_relay

while true; do
  if ! is_running "$POLLER_PID"; then
    wait "$POLLER_PID" 2>/dev/null || true
    echo "avito_missed_message_poller stopped; restarting"
    start_poller
  fi
  if ! is_running "$RELAY_PID"; then
    wait "$RELAY_PID" 2>/dev/null || true
    echo "avito_live_telegram_relay stopped; restarting"
    start_relay
  fi
  sleep 10
done
