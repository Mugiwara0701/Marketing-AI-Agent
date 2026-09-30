#!/usr/bin/env bash
# Checks /health of every service. Env: LEAD_URL OUTREACH_URL CONTENT_URL LLM_URL LLM_API_KEY
set -uo pipefail
fail=0
for pair in "lead:${LEAD_URL:-}" "outreach:${OUTREACH_URL:-}" "content:${CONTENT_URL:-}"; do
  name="${pair%%:*}"; url="${pair#*:}"
  if curl -fsS --max-time 15 "$url/health" >/dev/null; then echo "ok   $name"; else echo "DOWN $name"; fail=1; fi
done
if curl -fsS --max-time 15 -H "Authorization: Bearer ${LLM_API_KEY:-}" "${LLM_URL:-}/v1/models" >/dev/null; then echo "ok   llm"; else echo "DOWN llm"; fail=1; fi
exit $fail
