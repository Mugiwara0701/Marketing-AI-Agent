#!/usr/bin/env bash
# Verify the LLM host end to end from any machine that can reach it (run from the repo root).
#   LLM_BASE_URL=http://100.x.y.z:8001 EMBED_BASE_URL=http://100.x.y.z:8002 LLM_API_KEY=... \
#     bash services/llm-service/scripts/verify.sh [--no-embed]
# Set ROUTING_CONFIG=config/routing.dev-only.yaml while only agent-dev is served.
set -euo pipefail
: "${LLM_BASE_URL:?set LLM_BASE_URL}"
: "${LLM_API_KEY:?set LLM_API_KEY}"
export ROUTING_CONFIG="${ROUTING_CONFIG:-config/routing.yaml}"
PY="${PYTHON:-python}"
export PYTHONPATH="${PYTHONPATH:-.}"

echo "== waiting for ${LLM_BASE_URL}/v1/models (model load can take 10-15 min)"
for _ in $(seq 1 90); do
  if curl -fsS -H "Authorization: Bearer ${LLM_API_KEY}" "${LLM_BASE_URL}/v1/models" >/dev/null 2>&1; then
    break
  fi
  sleep 10
done
curl -fsS -H "Authorization: Bearer ${LLM_API_KEY}" "${LLM_BASE_URL}/v1/models" | head -c 400
echo

echo "== smoke: reachability, plain reply, schema-constrained reply"
if [ "${1:-}" = "--no-embed" ]; then
  "$PY" eval/runner/smoke.py
else
  "$PY" eval/runner/smoke.py --embed
fi

echo "== eval: labelled tasks (synthetic seed sets)"
fail=0
run_eval() { # task set schema min
  "$PY" eval/runner/run_eval.py "$1" "eval/sets/$2.jsonl" agent/prompts "$3" --min "$4" || fail=1
}
run_eval lead.assess lead_assess agent.tasks.assess:Assessment 0.8
run_eval lead.extract_contact lead_extract_contact agent.tasks.contact:ContactResult 0.85

echo "== drafts: schema, length, banned phrases (outreach)"
"$PY" eval/runner/draft_check.py || fail=1

if [ "$fail" -ne 0 ]; then echo "VERIFY FAILED"; exit 1; fi
echo "VERIFY OK"
