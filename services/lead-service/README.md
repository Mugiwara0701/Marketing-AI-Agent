# lead-service

Lead discovery: collect public sources, parse job-alert emails, qualify, enrich contacts.

## Jobs

| Job | Purpose |
|---|---|
| `collect_sources` | Fetch leads from ATS feeds, company pages and manual pastes. |
| `ingest_alerts` | Parse LinkedIn job-alert emails and post candidates to Slack for approval. |
| `qualify` | Score candidate leads against the ICP with the LLM. |
| `enrich_contacts` | Find public business contacts on approved companies' websites. |

All jobs are stubs that raise `NotImplementedError` (the API reports `failed: not implemented`).
Implement them following `docs/`.

## Contract

`GET /health`, `POST /jobs/{name}`, `GET /runs/{id}` with header `X-Job-Token`. See `libs/agentkit/README.md`.

## Run

```bash
docker compose up --build lead-service
curl localhost:8000/health   # port mapped in docker-compose.yml
```
