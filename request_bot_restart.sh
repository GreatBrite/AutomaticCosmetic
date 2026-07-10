#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p data
date -Is > data/restart_requested
echo "Restart requested. The Telegram bot will restart after the current Codex answer is sent."
