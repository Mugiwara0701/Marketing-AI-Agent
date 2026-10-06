#!/usr/bin/env bash
# Starts the whole agent as one long-running process (python -m agent start). Used by marketing-agent.service.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
. ./.env
set +a
export DISPLAY="${DISPLAY:-:0}"
export BROWSER_BACKEND="${BROWSER_BACKEND:-desktop}"
exec .venv/bin/python -m agent start
