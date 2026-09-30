#!/usr/bin/env bash
# Usage: call_service.sh <service> <job> [payload-json]
# Env: SERVICE_HOST_<SERVICE> style URL via SERVICE_URL, JOB_TOKEN, TIMEOUT_MIN (default 90)
set -euo pipefail

service="${1:?service}"; job="${2:?job}"; payload="${3:-}"
if [ -z "$payload" ]; then payload='{}'; fi
base="${SERVICE_URL:?SERVICE_URL}"
token="${JOB_TOKEN:?JOB_TOKEN}"
deadline=$(( $(date +%s) + ${TIMEOUT_MIN:-90} * 60 ))

if ! curl -fsS --max-time 15 "$base/health" >/dev/null; then
  echo "::warning::$service unreachable at health check; skipping $job"
  exit 0
fi

resp=$(mktemp)
code=$(curl -sS -o "$resp" -w '%{http_code}' --max-time 30 -X POST "$base/jobs/$job" \
  -H "X-Job-Token: $token" -H 'Content-Type: application/json' \
  -d "{\"payload\": $payload}")
if [ "$code" = "409" ]; then echo "::notice::$job already running; skipping"; exit 0; fi
if [ "$code" != "202" ]; then echo "::error::start failed ($code)"; cat "$resp"; exit 1; fi

run_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$resp")
echo "run_id=$run_id"
while :; do
  sleep 20
  curl -fsS --max-time 30 -H "X-Job-Token: $token" "$base/runs/$run_id" -o "$resp"
  status=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$resp")
  case "$status" in
    succeeded) echo "succeeded"; exit 0 ;;
    failed) echo "::error::$job failed"; cat "$resp"; exit 1 ;;
  esac
  if [ "$(date +%s)" -gt "$deadline" ]; then echo "::error::timeout waiting for $job"; exit 1; fi
done
