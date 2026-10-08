# AI Marketing Agent

B2B lead generation and outreach for an AOSP / BSP / embedded Linux engineering-services company, plus a daily
technical blog (written by a person, not by the model: the weekly blog file comes from the dashboard). It finds **companies that may buy our engineering** (they build EV chargers, kiosks, industrial
controllers, robots, medical devices, infotainment, IoT / edge devices... on Android or embedded Linux, or they ask for
outside help), never sellers of boards or devices and never competitors. Every outreach email is **sent only after a
person approved it** in Slack. The pipeline runs only between a **Start** and a **Stop** pressed in the dashboard.

```text
Lead pipeline (agent/leadgen)                                         python -m agent leads run [--dry-run]
│
├── Search strategy      technology x business intent x product domain, 4 query families, no repeats (strategy.py)
├── Browser              http (SearXNG + polite HTTP) | chrome (Playwright) | desktop (visible Chrome, xdotool + OCR)
├── Page rules           shop / marketplace / distributor / docs / directory / competitor? -> rejected, no model call
├── LLM reading          lead.assess: page type, company, product, signal, needs, quotes (one call per page)
├── Code checks          company named on the page, quotes literally on the page, website never guessed, exclusions
├── Score 0-100          company / technical / project signal / commercial / contact / evidence, with penalties
├── Dedup                one company = one lead (domain, normalized name); new pages add evidence
├── Contact discovery    company's own site: Contact / Team / About links only; decision makers first; never guessed
├── Email draft          from the evidence only; invented technology and generic drafts are flagged
├── Slack approval       full lead card + draft, Approve / Reject (or simulated in a dry-run)
└── Sending              only APPROVED emails, through one gated sender (agent/leadgen/sender.py)
```

## How the pieces fit together

```text
Dashboard (frontend -> its own backend)
        │  HTTPS, Bearer token
        ▼
Pipeline control API (api/, hosted on Render)      cron-job.org calls /health every 5 min so it never sleeps
        │  writes "start" / "stop"
        ▼
Supabase Postgres  ── pipeline_control row ──┐     leads, emails, approvals, runs ... (all state)
        ▲                                   │
        │ every 2 s: "what is requested?"   │     Slack Approve / Reject -> Edge Function slack-interact
        │ + heartbeat, current step         │     -> decide_email() in the same database
        │                                   ▼
Office machine (Xubuntu, behind NAT): agent service `marketing-agent` (python -m agent start)
        visible Chrome for lead search · Ollama (local LLM) · SearXNG · Gmail API for sending and replies
```

Nothing ever connects **to** the office machine: it only makes outgoing connections, so the dashboard, the API and
the office can all be on different networks. If the office machine is off, the dashboard shows `offline`; a Start
pressed meanwhile is kept and applied when it is back.

## Running it: one service, started from the dashboard

`python -m agent start` runs everything in one long-running process (installed on the office machine as the systemd
user service `marketing-agent`; it starts by itself after a reboot). **Nothing runs on a schedule.** While the
pipeline is running:

| Loop | When | What |
|---|---|---|
| passes | one after another, 30 min apart (`PIPELINE_REST_MINUTES`) | lead discovery first (Chrome opens within seconds of Start; drafts go to Slack), then follow-ups, blog step (the model writes no blog: it only waits for the weekly file) |
| sender | every 20 s | sends what a person approved in Slack (nothing while `EMAIL_SENDING_ENABLED` is not true) |
| inbox | every 10 min | polls Gmail for replies and bounces; drafts answers for Slack approval |

**Stop** cancels the pass in progress and closes the agent's Chrome (only its own profile; a person's Chrome on that
desktop stays open). While stopped, nothing is searched, sent or polled; Slack approvals are still recorded and sent
after the next Start. The requested state lives in the database, so a restart or reboot resumes it.

The dashboard uses three endpoints (full reference: [docs/pipeline-api.md](docs/pipeline-api.md), live docs at
`<api url>/docs`):

| Method | Path | |
|---|---|---|
| `POST` | `/api/v1/pipeline/start` | ask the agent to start |
| `POST` | `/api/v1/pipeline/stop` | ask the agent to stop |
| `GET` | `/api/v1/pipeline/status` | `state` (`running` / `stopped` / `offline`), current step, last pass |

Without the dashboard: `python -m agent pipeline start|stop|status` (any machine with the agent's `.env`), or from the
laptop `bash deploy/remote.sh pipeline start|stop|status`.

## Lead states

`DISCOVERED -> QUALIFIED -> CONTACT_FOUND -> EMAIL_DRAFTED -> PENDING_APPROVAL -> APPROVED -> SENT`, plus `REJECTED`
and `FAILED` (`agent/leadgen/models.py`). Every state is stored (`companies.lead_status`), so a run that is stopped or
crashes is resumed by the next one: leads stuck half-way are moved on first, pages and queries already done are not
redone. A company that gives no contact after 3 attempts (`contacts.max_attempts`) is rejected.

## Email safety

- An email is sent only if, **in the database query that claims it**, it is `approved`, has an `approvals` row with a
  recorded human decision (who, when) and, for an intro, its lead is `APPROVED`. The sender checks again right before
  sending, and a transport refuses anything the gate did not clear. Rejected, pending and unknown emails cannot be sent.
- Decisions are made in Slack (`supabase/functions/slack-interact` calls the SQL function `decide_email()`) or with
  `python -m agent leads approve|reject <email id>`. Each draft can be decided exactly once.
- Mail goes out through the **Gmail API** as the account that logged in once (`python -m agent gmail-check`
  creates `token.json`; follow-ups and replies stay in the original Gmail thread). Emails are HTML
  (`config/email_template.html`) with a plain-text alternative. See `docs/aksaht_Docs/Gmail_Migration.md`.
- `EMAIL_SENDING_ENABLED` must be `true` as well (default false). `APP_ENV=dev` limits recipients to
  `ALLOWED_RECIPIENT_DOMAINS`; `TEST_RECIPIENT` redirects all mail; suppression list, send cap, unsubscribe footer.
- `--dry-run`: a local SQLite store, Slack messages written to `out/leadgen/approvals/`, approved mail written to
  `out/leadgen/outbox/`. Nothing leaves the machine.

## Set-up

### Agent (office machine)

1. Python 3.12, then `pip install -r requirements.txt` (installs `libs/agentkit` too).
2. Local services: Ollama with the model in `config/routing.yaml`, SearXNG (`deploy/searxng`, `docker compose up -d`),
   Google Chrome, and the desktop tools from `deploy/desktop-lead-search.md` (`python -m agent desktop-check`).
3. Create `.env` in the repo root (never committed; there is no template in the repo, the settings are listed below).
4. `python -m agent migrate`, `python -m agent gmail-check` (once, in a browser, creates `token.json`),
   `python -m agent check` (LLM, database, Slack, search, email switch).
5. From the laptop: `bash deploy/remote.sh init user@office-host`, `push-secrets`, `install-service`, `up`.

Main settings (`.env`):

| Group | Variables |
|---|---|
| Database | `DATABASE_URL` (Supabase pooler URL; empty = only `--dry-run` commands work) |
| Models | `LLM_BASE_URL`, `LLM_API_KEY`, `ROUTING_CONFIG`, `LLM_MIN_TIMEOUT` (raise on a CPU-only machine) |
| Lead search | `BROWSER_BACKEND` (`http` / `chrome` / `desktop`), `SEARXNG_URL`, `MAX_NEW_LEADS_PER_DAY`, `HOST_DELAY_SECONDS` |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_ALLOWED_USERS`, `SLACK_CHANNEL_OUTREACH` / `_CONTENT` / `_ALERTS` / `_MANUAL` (qualified leads with no contact: company + website, default `#manual-check`) / `_FORM` (a contact form was found: company + form page, to fill by hand, default `#form-fill`) |
| Gmail | `GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKEN_PATH`, `MAIL_FROM`, `REPLY_TO` |
| Sending safety | `EMAIL_SENDING_ENABLED`, `APP_ENV`, `ALLOWED_RECIPIENT_DOMAINS`, `TEST_RECIPIENT`, `DAILY_SEND_CAP_PER_MAILBOX` |
| Email identity | `SENDER_NAME`, `COMPANY_NAME`, `COMPANY_ADDRESS`, `COMPANY_WEBSITE`, `UNSUBSCRIBE_BASE_URL`, `UNSUBSCRIBE_SECRET` |
| Service | `PIPELINE_POLL_SECONDS` (2), `PIPELINE_REST_MINUTES` (30), `SEND_EVERY_SECONDS` (20), `INBOX_EVERY_MINUTES` (10), `DESKTOP_KEEP_CHROME` |

`credentials.json`, `token.json` and `.env` are secrets: they stay on the office machine (`deploy/remote.sh
push-secrets` copies them over SSH) and are git-ignored.

### Pipeline control API (Render)

Deployed from `api/Dockerfile` (`render.yaml` is a ready Render Blueprint). It needs only `DATABASE_URL` (the limited
`pipeline_api` login printed by `python -m agent api-credentials`, which can only read and request the pipeline
state) and `PIPELINE_API_TOKEN` (shared with the dashboard backend). A cron-job.org job calls `/health` every 5 minutes
so the free instance does not sleep. Details: [api/README.md](api/README.md).

## Commands

```bash
python -m agent start                                    # the service (normally run by systemd)
python -m agent pipeline start|stop|status               # what the dashboard buttons do
python -m agent migrate                                  # apply supabase/migrations (idempotent)
python -m agent api-credentials                          # database URL for the hosted API (rotates its password)
python -m agent leads queries                            # what the strategy will search next
python -m agent leads run --dry-run --max-leads 3        # full pipeline, no database, no Slack, no email
python -m agent leads review [--dry-run]                 # drafts waiting for a decision
python -m agent leads show <email id> [--dry-run]        # lead, evidence, score, contact, draft, approval
python -m agent leads approve <email id> [--dry-run]     # or reject; Slack buttons do the same
python -m agent send [--dry-run]                         # send what was approved (dry-run: to the outbox folder)
python -m agent run [--force]                            # one full manual run (inbox, replies, send, follow-ups, leads, blog)
```

From the laptop (`deploy/remote.sh`, over SSH on the office network): `up` (sync + restart), `down`, `status`,
`logs -f`, `pipeline start|stop|status`, `run <args>` (one manual run), `push-secrets`.

Every lead search writes a Markdown report (`out/leadgen/run-*.md`): each lead with its score parts, penalties,
evidence (quotes and URLs), contact, draft and approval state. Logs are one JSON object per line (`Search executed`,
`Result rejected`, `Lead qualified`, `Contact found`, `Email generated`, `Slack approval requested`, `Email sent`...).

## Layout

| Path | What it is |
|---|---|
| `agent/leadgen/` | The lead pipeline (see its `__init__.py` for one line per module) |
| `agent/control.py`, `agent/supervisor.py` | Start / stop from the database, the long-running service and its loops |
| `agent/tasks/` | LLM tasks with schemas: `assess` (page reading), `contact`, `proposal`, `search` (desktop SERP), replies, follow-ups |
| `agent/prompts/` | Prompt files (`lead_assess.txt`, `lead_extract_contact.txt`, `outreach_draft.txt`...) |
| `agent/gui/` | Desktop Chrome driver (xdotool, OCR, vision fallback) and the sandboxed GUI agent |
| `agent/` (rest) | Run steps `run.py`, CLI `__main__.py`, mail building `mailer.py`, replies, follow-ups, polite fetching `web.py`, job feeds `sources.py` |
| `api/` | Pipeline control REST API for the dashboard (own requirements and Dockerfile; `api/README.md`) |
| `config/leadgen.yaml` | Search vocabulary, thresholds, exclusions, page budgets (edit without touching code) |
| `config/sources.yaml` | Job / project feed APIs (optional for leads) |
| `config/routing.yaml` | Task -> model alias |
| `config/email_template.html` | The HTML email design |
| `libs/agentkit` | Shared LLM client, DB access, prompts, checks, Slack, Gmail |
| `supabase/` | Migrations (`0011` / `0012`: pipeline control and the API's limited login) and Edge Functions (`slack-interact`, `unsubscribe`) |
| `deploy/` | systemd service, start script, remote control from the laptop, SearXNG, desktop set-up |
| `docs/pipeline-api.md` | API reference for the dashboard developer |
| `render.yaml` | Render Blueprint for the API |
| `eval/` | Eval sets and runners for prompts/models (`make llm-verify`) |

## Models

Open models behind an OpenAI-compatible endpoint (`LLM_BASE_URL`): Ollama on the office machine (CPU only, so model
time limits are raised automatically), or vLLM on a GPU host. Text model: `lead.assess` (one call per page that passes
the rules), `lead.extract_contact` (only on pages that name people or show addresses), `outreach.draft`. The desktop
backend takes result links from SearXNG and uses a vision model (`gui.step`) only as a fallback for clicking.
Everything else (page intent, prices, job boards, scoring, dedup, contact guards, state) is deterministic code.

## Research rules

- Only pages a search result or a link on the page leads to are opened: no URL is built from a guess, no crawling.
  Per-run and per-domain page budgets, a per-host pause, robots.txt (HTTP backend), identifiable user agent.
- A CAPTCHA, Cloudflare check, login wall or rate limit is never worked around: the engine or site is rested / skipped
  for the run and recorded. A site whose server answers with an error page ("no available server", 502 / 503...) is
  tried once on `www.`, then counted as a failed attempt. LinkedIn, Indeed, Naukri, Glassdoor and similar portals are
  never fetched.
- Business exclusions (big brands, autonomous driving, competitors, Bosch Rexroth, Canada / UK / Germany) are in
  `config/leadgen.yaml`. A company added to `exclude_companies` later is also dropped from the stored leads.

## Code quality and CI

Every pull request and push to `main` runs three workflows; all must be green to merge.

| Workflow | Checks | Config |
|---|---|---|
| `ci.yml` | unit tests (`make test`: agentkit, agent, sandbox, api) | `Makefile`, `requirements-dev.txt` |
| `lint.yml` | ruff lint and format, mypy, sqlfluff (migrations), yamllint, actionlint (workflows), deno fmt/lint/check (Edge Functions), markdownlint | `ruff.toml`, `mypy.ini`, `.sqlfluff`, `.yamllint.yaml`, `deno.json`, `.markdownlint.yaml` |
| `security.yml` | gitleaks (secrets, full history), pip-audit (dependencies); also weekly | `.gitleaks.toml` |

Dependabot (`.github/dependabot.yml`) opens weekly update PRs for Actions, pip and Docker.

Locally: `make install`, then `make check` (lint, typecheck, tests) and `make format` to auto-fix. The pre-commit hook
(`pre-commit install`, `.pre-commit-config.yaml`) runs the same linters, plus shellcheck, on every commit; it uses the
same sqlfluff version as CI. Postgres integration tests run when `TEST_DATABASE_URL` points at a throwaway database
(see `agent/tests/test_leadgen_postgres.py`).
