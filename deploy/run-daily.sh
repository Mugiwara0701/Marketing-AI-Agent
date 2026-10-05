#!/usr/bin/env bash
# Wrapper for the daily run: loads .env, runs once, exits. Used by the systemd service and cron.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
. ./.env
set +a
# Lead research drives the visible Chrome on the desktop session (X11). Email is sent only when
# EMAIL_SENDING_ENABLED=true AND a person approved it (Slack / `python -m agent leads approve`).
export DISPLAY="${DISPLAY:-:0}"
export BROWSER_BACKEND="${BROWSER_BACKEND:-desktop}"
exec .venv/bin/python -m agent run "$@"
