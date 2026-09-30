# AI Marketing Agent

Automated marketing for an AOSP / embedded-engineering services company, built on open-source
models and our own stack. Three modules: **lead discovery**, **email outreach + replies**,
**blog writing and publishing** (LinkedIn, dev.to). Slack is the control surface: every approval
and error goes there. Nothing is sent or published without manual approval.

## Architecture: five components

```
GitHub Actions / cron (scheduler) --Tailscale--> lead-service    --\
                                              -> outreach-service --> llm-service (vLLM + embeddings, own GPU host)
                                              -> content-service  --/          |
                     Slack <-> Supabase Edge Functions (repository_dispatch)   v
                                                              Supabase Postgres + pgvector
```

| # | Component | Path | What it is |
|---|---|---|---|
| 1 | lead-service | `services/lead-service` | Collect leads, parse job-alert emails, qualify, enrich contacts |
| 2 | outreach-service | `services/outreach-service` | Draft, send (after approval), read and answer replies |
| 3 | content-service | `services/content-service` | Plan, draft, publish posts (after approval), metrics |
| 4 | llm-service | `services/llm-service` | Self-hosted open-source LLM (vLLM) and embedding server |
| 5 | scheduler | `scheduler/`, `.github/workflows/` | GitHub Actions and cron that call the services |

Shared: `libs/agentkit` (job contract, config, logging, later LLM/retrieval/Slack helpers),
`config/routing.yaml` (task -> model), `supabase/` (migrations, edge functions), `dashboard/`
(read-only lead view and pipeline start), `eval/` (eval sets), `docs/`.

### Job contract (all three services)

`GET /health`, `POST /jobs/{name}` (202 + `run_id`), `GET /runs/{id}`, header `X-Job-Token`.
One run per job at a time (409). Jobs must be idempotent: schedules can be delayed or dropped.

## Trade-offs to know

- Services must be **always on** (the company machine on the tailnet). GitHub Actions only triggers them;
  agent code does not run inside runners.
- Supabase Edge Functions cannot reach the tailnet, so Slack approvals fire `repository_dispatch`
  events, and workflows call the services.
- LinkedIn forbids scraping. Leads come from LinkedIn job-alert emails, manual pastes and ATS feeds
  (accepted risk, needs senior sign-off).
- Compliance: CAN-SPAM, GDPR/CASL, India DPDP Rules, Gmail/Yahoo bulk-sender rules (SPF/DKIM/DMARC,
  one-click unsubscribe, complaints under 0.3%). Keep a suppression list; no PII in logs.

## Status

Scaffold only. Job functions are stubs (`NotImplementedError`); the job contract, workflows, config
and the retrieval migration are in place. Scheduled workflows run only when repo variable
`SCHEDULES_ENABLED=true`.

## Quick start

```bash
cp .env.example .env         # fill in values
make install && make test
make up && make health       # services on 127.0.0.1:8101-8103
```

LLM host: see `services/llm-service/README.md`. Apply `supabase/migrations/*.sql` to your project.
Add repo secrets listed in `scheduler/README.md`.

## Build order

1. Retrieval and prompt path (`libs/agentkit`: embed, retrieve, prompts, task_runner, checks).
2. lead-service, then outreach-service, then content-service.
3. Optional: fine-tuned LoRA adapters after the evaluation gate (see docs).

## To verify before production

Pin GitHub Actions to commit SHAs and check Tailscale action inputs; TEI's OpenAI-compatible route;
model repo IDs marked TODO in `models.yaml`; vLLM flags and LoRA support for the chosen base model.
