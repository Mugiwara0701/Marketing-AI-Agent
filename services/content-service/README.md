# content-service

Content: plan topics, draft posts, publish to dev.to and LinkedIn after approval.

## Jobs

| Job | Purpose |
|---|---|
| `plan_topics` | Propose topics from knowledge docs and past performance. |
| `draft_post` | Write a draft post with retrieval; post to Slack. |
| `publish` | Publish approved posts to dev.to and LinkedIn. |
| `collect_metrics` | Pull post metrics for the learning loop. |

All jobs are stubs that raise `NotImplementedError` (the API reports `failed: not implemented`).
Implement them following `docs/`.

## Contract

`GET /health`, `POST /jobs/{name}`, `GET /runs/{id}` with header `X-Job-Token`. See `libs/agentkit/README.md`.

## Run

```bash
docker compose up --build content-service
curl localhost:8000/health   # port mapped in docker-compose.yml
```
