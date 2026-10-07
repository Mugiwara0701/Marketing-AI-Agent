# Pipeline control API

A small REST service the dashboard backend calls to start and stop the agent. API reference for the dashboard
developer: [docs/pipeline-api.md](../docs/pipeline-api.md) (and `/docs` on the running service).

It does not run the agent and never contacts the office machine. It only reads and writes the `pipeline_control` row
in the agent's Supabase database (migration `supabase/migrations/0011_pipeline_control.sql`); the agent polls that row.
So it can be hosted anywhere that reaches Supabase: next to the dashboard backend, a VPS, Render, Railway, Fly...

## Settings (environment)

| Variable | |
|---|---|
| `DATABASE_URL` | required. The agent's Supabase Postgres (the transaction pooler URL works) |
| `PIPELINE_API_TOKEN` | required. Long random secret; the dashboard backend sends it as `Authorization: Bearer ...` |
| `PIPELINE_API_CORS_ORIGINS` | optional, comma separated. Only if a browser calls the API directly |
| `PIPELINE_OFFLINE_AFTER_SECONDS` | optional, default 60. Heartbeat age after which the agent shows as offline |
| `PIPELINE_API_DOCS` | optional, default true. `false` hides `/docs` and `/openapi.json` |

## Run

```bash
docker build -f api/Dockerfile -t pipeline-api .          # from the repo root
docker run -p 8000:8000 -e DATABASE_URL=... -e PIPELINE_API_TOKEN=... pipeline-api

# or without Docker
pip install -r api/requirements.txt
uvicorn api.app:app --host 0.0.0.0 --port 8000
```

Serve it over HTTPS (the host's TLS, or a reverse proxy in front): the token travels in a header.

Once per database: `python -m agent migrate` (from the agent) creates `pipeline_control`.

## Tests

`pytest api` (from the repo root). `api/tests/test_postgres.py` runs the whole loop (API request -> agent applies it
-> API shows it) against a throwaway Postgres when `TEST_DATABASE_URL` is set.
