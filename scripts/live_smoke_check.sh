#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
SINCE="${SINCE:-2 hours ago}"

SERVICES=(
  "freelance-leads-bot.service"
  "yclients-avito-webhook.service"
  "yclients-avito-missed-poller.service"
  "yclients-avito-unanswered-monitor.service"
  "yclients-yclients-integration.service"
)
JOURNAL_ARGS=()
for service in "${SERVICES[@]}"; do
  JOURNAL_ARGS+=("-u" "${service}")
done

echo "== AutomaticCosmetic ops_status =="
PYTHONPATH="${PWD}" "${PYTHON_BIN}" -m src.freelance_leads_bot.integrations.ops_status

echo
echo "== Failed systemd units =="
failed_units="$(systemctl --failed --no-legend --plain --no-pager || true)"
if [[ -n "${failed_units}" ]]; then
  echo "${failed_units}"
  exit 1
fi
echo "No failed systemd units."

echo
echo "== Runtime service states =="
systemctl is-active -- "${SERVICES[@]}"

echo
echo "== Runtime warnings/errors since ${SINCE} =="
journal_output="$(journalctl "${JOURNAL_ARGS[@]}" --since "${SINCE}" -p warning..alert --no-pager || true)"
if [[ -n "${journal_output}" && "${journal_output}" != "-- No entries --" ]]; then
  echo "${journal_output}"
  exit 1
fi
echo "No runtime warning/error journal entries."

echo
echo "Live smoke check OK."
