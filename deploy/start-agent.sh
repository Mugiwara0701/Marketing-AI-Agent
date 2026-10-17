#!/usr/bin/env bash
# Starts the whole agent as one long-running process (python -m agent start). Used by marketing-agent.service.
set -euo pipefail
cd "$(dirname "$0")/.."
# The agent must run on the Marketing Agent machine, never on a laptop whose .env points at the live database.
if [ "${MA_ON_OFFICE_MACHINE:-}" != 1 ]; then
  echo "Not starting: this is not the Marketing Agent machine. From the laptop use: bash deploy/remote.sh up" >&2
  exit 2
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a
export DISPLAY="${DISPLAY:-:0}"
export BROWSER_BACKEND="${BROWSER_BACKEND:-desktop}"
exec .venv/bin/python -m agent start
