# AI Marketing Agent

A small autonomous B2B lead-generation and technical-content agent for an AOSP / embedded-engineering
services company. It is **not** a general-purpose LLM system and it does **not** run continuously: once a
day it runs for at most 1-2 hours, saves everything to the database and exits.

```text
Daily run (python -m agent run, started by a systemd timer)
│
├── Send emails you approved after the previous run
│
├── Part 1: Lead generation  (max 10 new leads/day)
│   ├── Search free public sources: job boards, HN hiring threads, project posts, web search
│   ├── LLM qualifies each listing: does a named company need AOSP / BSP / Embedded Linux work done?
│   ├── Extract company, project, technologies, location, website, source URL
│   ├── Find a public business contact on the company's own site (never guessed)
│   ├── Deduplicate (by listing and by company domain), save to the database
│   └── LLM drafts a company-specific proposal email  ->  waits for your approval
│
└── Part 2: Daily blog  (1 post/day)
    ├── Research current AOSP / Android / embedded Linux news and discussions
    ├── LLM proposes ranked topics; code picks the best one that is not a repeat
    ├── LLM writes one technical post
    └── Save post + metadata (sources, tags, checks) to the database
```

We look for **companies that need this engineering work done** (to sell them our services), not for
engineers to hire. A job post is only evidence that a company has the work and is short of capacity.

## Layout

| Path | What it is |
|---|---|
| `agent/` | The daily runner: `run.py` (orchestration + time budget), `leads.py`, `blog.py`, `sources.py`, `contacts.py`, `mailer.py`, `replies.py`, `followups.py`, `review.py`, `store.py`, `web.py` |
| `agent/tasks/` | The LLM tasks (qualify, extract contact, proposal, reply, follow-up, topics, blog) with schemas and output checks; prompts in `agent/prompts/` |
| `config/sources.yaml` | Keywords, job APIs, search queries, blog feeds (edit without touching code) |
| `config/routing.yaml` | Task -> model alias |
| `libs/agentkit` | Shared LLM client, DB access, prompts, checks |
| `services/llm-service` | Self-hosted open-source LLM (vLLM + embeddings) on the office GPU machine |
| `supabase/` | Migrations and the Edge Functions (`unsubscribe`, `slack-interact`) |
| `deploy/` | systemd timer + service, approval workflow |
| `eval/` | Eval sets and runners for prompts/models |

## Rules built in

- **Limits:** `MAX_NEW_LEADS_PER_DAY=10`, one blog per day (idempotent), `AGENT_MAX_MINUTES=120` hard stop with
  25 minutes always reserved for the blog; a failing step never blocks the next.
- **Sending is off by default** (`EMAIL_SENDING_ENABLED=false`): the run still scrapes, qualifies, saves leads and
  drafts proposals. When enabled, emails are saved as `drafted` and only approved ones are sent (`python -m agent approve`).
- **Replies and follow-ups:** mail goes out through the Gmail API (`agentkit/gmail.py`, one-time login with
  `python -m agent.gmail_check`). Each run first polls the inbox (`python -m agent inbox`): replies are matched to
  the mail we sent (Message-ID headers, Gmail thread, then sender) and stored once; mailer-daemon failure notices
  mark the email bounced and suppress the contact. `python -m agent run` then classifies replies and drafts an
  answer for interested people and questions; an intro with no reply after `FOLLOWUP_DELAY_DAYS=4` gets one
  drafted follow-up, sent in the same Gmail thread. Both are posted to Slack and **sent only after a person
  approves**. Gmail gives no open or delivery events, so none are tracked or faked. Setup: `docs/aksaht_Docs/Gmail_Migration.md`.
- **Contacts:** only addresses that appear literally on the company's own website and belong to its own domain.
- **Polite scraping:** robots.txt respected, per-host delay, identifiable user agent, no private addresses.
- **Compliance:** suppression list, one-click unsubscribe link and headers, identity + postal address footer
  (CAN-SPAM / GDPR / DPDP), send cap, `APP_ENV=dev` restricts recipients to `ALLOWED_RECIPIENT_DOMAINS`.
- **Free sources only:** no paid APIs. Reliable open-web search needs a self-hosted SearXNG (`SEARXNG_URL`);
  DuckDuckGo's page is used as a fallback but usually blocks bots.
- LinkedIn is not scraped (its terms forbid it).

## Status

Implemented and unit-tested (31 tests, ruff, mypy): sources, qualification flow, contact guard, proposal drafting,
blog pipeline, mailer, approval CLI, time-budgeted runner. Free feeds were checked live. **Not yet run end to end**:
there has been no run against a real LLM host or a live Supabase project (apply `supabase/migrations/*.sql`,
including `0003_daily_agent.sql`). Expect to tune `config/sources.yaml` and the prompts once real data flows;
10 qualified leads with public contacts per day is a target, and free sources may yield fewer.
Design documents in `docs/` describe the earlier multi-service design and are superseded by this README.

## Quick start

```bash
cp .env.example .env                 # fill in values
make install && make test
python -m agent run --only blog      # try one part (needs LLM_BASE_URL and DATABASE_URL)
python -m agent run                  # full daily run
python -m agent replies              # classify new replies, queue approved answers
python -m agent followups            # draft follow-ups for unopened, unanswered intros
python -m agent review               # drafted emails (intros, follow-ups); then: approve <id> | --all, reject <id>
```

LLM host: see `services/llm-service/README.md` (`make llm-verify`). Scheduling: `deploy/README.md`.

## Code quality and CI

Every pull request and push to `main` runs three workflows; all three must be green to merge.

| Workflow | Checks | Config |
|---|---|---|
| `ci.yml` | unit tests (agentkit and each service), docker compose config and image builds | `Makefile`, `pyproject.toml` |
| `lint.yml` | ruff lint and format, mypy, sqlfluff (migrations), yamllint, shellcheck, actionlint (workflows), hadolint (Dockerfiles), deno fmt/lint/check (Edge Functions), markdownlint | `ruff.toml`, `mypy.ini`, `.sqlfluff`, `.yamllint.yaml`, `.hadolint.yaml`, `deno.json`, `.markdownlint.yaml` |
| `security.yml` | gitleaks (secrets, full history), pip-audit (dependencies); also weekly | `.gitleaks.toml` |

Dependabot (`.github/dependabot.yml`) opens weekly update PRs for Actions, pip and Docker.

Locally: `make install`, then `make check` (lint, typecheck, tests) and `make format` to auto-fix.
To run the same checks on every commit: `pre-commit install` (see `.pre-commit-config.yaml`).
Set branch protection on `main` to require the `ci`, `lint` and `security` jobs.

## To verify before production

Pin GitHub Actions to commit SHAs and check Tailscale action inputs; TEI's OpenAI-compatible route;
model repo IDs marked TODO in `models.yaml`; vLLM flags and LoRA support for the chosen base model.
