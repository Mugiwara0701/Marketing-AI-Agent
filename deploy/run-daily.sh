#!/usr/bin/env bash
# Wrapper for the daily run: loads .env, runs once, exits. Used by the systemd service and cron.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
. ./.env
set +a
# The scheduled run drives a visible Chrome on the desktop session (X11) and never sends email.
export DISPLAY="${DISPLAY:-:0}"
export LEADS_MODE="${LEADS_MODE:-desktop}"
exec .venv/bin/python -m agent run "$@"
