# outreach-service

Email outreach: draft, send after approval, poll and answer replies.

## Jobs

| Job | Purpose |
|---|---|
| `draft` | Draft first-touch emails for approved leads; post to Slack. |
| `send` | Send approved emails within daily caps; respect suppression list. |
| `poll_replies` | Read the inbox, classify replies, update the CRM. |
| `draft_replies` | Draft answers to replies; post to Slack for approval. |

All jobs are stubs that raise `NotImplementedError` (the API reports `failed: not implemented`).
Implement them following `docs/`.

## Contract

`GET /health`, `POST /jobs/{name}`, `GET /runs/{id}` with header `X-Job-Token`. See `libs/agentkit/README.md`.

## Run

```bash
docker compose up --build outreach-service
curl localhost:8000/health   # port mapped in docker-compose.yml
```
