#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m src.freelance_leads_bot.main run-once

