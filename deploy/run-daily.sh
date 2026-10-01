#!/usr/bin/env bash
# Wrapper for the daily run: loads .env, runs once, exits. Used by the systemd service and cron.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
. ./.env
set +a
exec .venv/bin/python -m agent run "$@"
