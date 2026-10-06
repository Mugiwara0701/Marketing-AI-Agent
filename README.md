# AI Marketing Agent

B2B lead generation and outreach for an AOSP / BSP / embedded Linux engineering-services company, plus a daily
technical blog. It finds **companies that may buy our engineering** (they build EV chargers, kiosks, industrial
controllers, robots, medical devices, infotainment, IoT / edge devices... on Android or embedded Linux, or they ask for
outside help), never sellers of boards or devices and never competitors. Every outreach email is **sent only after a
person approved it**.

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

Daily run (python -m agent run, systemd timer): replies -> send approved -> follow-ups -> leads -> blog
```

## Lead states

`DISCOVERED -> QUALIFIED -> CONTACT_FOUND -> EMAIL_DRAFTED -> PENDING_APPROVAL -> APPROVED -> SENT`, plus `REJECTED`
and `FAILED` (`agent/leadgen/models.py`). Every state is stored (`companies.lead_status`), so a run that crashes is
resumed by the next one: leads stuck half-way are moved on first, pages and queries already done are not redone.

## Email safety

- An email is sent only if, **in the database query that claims it**, it is `approved`, has an `approvals` row with a
  recorded human decision (who, when) and, for an intro, its lead is `APPROVED`. The sender checks again right before
  sending, and a transport refuses anything the gate did not clear. Rejected, pending and unknown emails cannot be sent.
- Decisions are made in Slack (`supabase/functions/slack-interact` calls the SQL function `decide_email()`) or with
  `python -m agent leads approve|reject <email id>`. Each draft can be decided exactly once.
- Mail goes out through the **Gmail API** as the account that logged in once (`python -m agent gmail-check`
  creates `token.json`; follow-ups and replies stay in the original Gmail thread). `agent inbox` polls Gmail for
  replies and bounces. See `docs/aksaht_Docs/Gmail_Migration.md`.
- `EMAIL_SENDING_ENABLED` must be `true` as well (default false). `APP_ENV=dev` limits recipients to
  `ALLOWED_RECIPIENT_DOMAINS`; `TEST_RECIPIENT` redirects all mail; suppression list, send cap, unsubscribe footer.
- `--dry-run`: a local SQLite store, Slack messages written to `out/leadgen/approvals/`, approved mail written to
  `out/leadgen/outbox/`. Nothing leaves the machine.

## Running it: one service

`python -m agent start` runs everything in one long-running process (installed on the office machine as the systemd
user service `marketing-agent`):

| Loop | When | What |
|---|---|---|
| sender | every 20 s | sends what a person approved in Slack (nothing while `EMAIL_SENDING_ENABLED` is not true) |
| inbox | every 10 min | polls Gmail for replies and bounces; drafts answers for Slack approval |
| daily | 09:30 (and at start if today's run has not happened) | follow-ups, lead discovery (drafts go to Slack), blog |

From the laptop: `bash deploy/remote.sh install-service` once (retires the old daily timer), then `up` (sync +
restart), `down`, `status`, `logs -f`. Emails are HTML (`config/email_template.html`) with a plain-text alternative.

## Using it

```bash
cp .env.example .env                                     # fill in; never commit .env
python -m agent migrate                                  # apply supabase/migrations (incl. 0007_lead_pipeline.sql)
python -m agent leads queries                            # what the strategy will search next
python -m agent leads run --dry-run --max-leads 3        # full pipeline, no database, no Slack, no email
python -m agent leads review [--dry-run]                 # drafts waiting for a decision
python -m agent leads show <email id> [--dry-run]        # lead, evidence, score, contact, draft, approval
python -m agent leads approve <email id> [--dry-run]     # or reject; Slack buttons do the same
python -m agent send [--dry-run]                         # send what was approved (dry-run: to the outbox folder)
python -m agent run                                      # the daily run (replies, send, follow-ups, leads, blog)
```

Every run writes a Markdown report (`out/leadgen/run-*.md`): each lead with its score parts, penalties, evidence
(quotes and URLs), contact, draft and approval state. Logs are one JSON object per line (`Search executed`,
`Result rejected`, `Lead qualified`, `Contact found`, `Email generated`, `Slack approval requested`, `Email sent`...).

## Layout

| Path | What it is |
|---|---|
| `agent/leadgen/` | The lead pipeline (see its `__init__.py` for one line per module) |
| `agent/tasks/` | LLM tasks with schemas: `assess` (page reading), `contact`, `proposal`, `search` (desktop SERP), replies, follow-ups, blog |
| `agent/prompts/` | Prompt files (`lead_assess.txt`, `lead_extract_contact.txt`, `outreach_draft.txt`...) |
| `agent/gui/` | Desktop Chrome driver (xdotool, OCR, vision fallback) and the sandboxed GUI agent |
| `agent/` (rest) | Daily runner `run.py`, CLI `__main__.py`, mail building `mailer.py`, replies, follow-ups, blog, polite fetching `web.py`, job feeds `sources.py` |
| `config/leadgen.yaml` | Search vocabulary, thresholds, exclusions, page budgets (edit without touching code) |
| `config/sources.yaml` | Job / project feed APIs (optional for leads) and blog research feeds |
| `config/routing.yaml` | Task -> model alias |
| `libs/agentkit` | Shared LLM client, DB access, prompts, checks, Slack, Gmail |
| `supabase/` | Migrations and Edge Functions (`slack-interact`, `unsubscribe`) |
| `deploy/` | systemd timer + service, desktop set-up, remote control |
| `eval/` | Eval sets and runners for prompts/models (`make llm-verify`) |

## Models

Open models behind an OpenAI-compatible endpoint (`LLM_BASE_URL`: vLLM on the office GPU, or Ollama). Text model:
`lead.assess` (one call per page that passes the rules), `lead.extract_contact` (only on pages that name people or show
addresses), `outreach.draft`. The desktop backend also uses `lead.search` to list results from the screen and a vision
model (`gui.step`) only as a fallback for clicking. Everything else (page intent, prices, job boards, scoring, dedup,
contact guards, state) is deterministic code.

## Research rules

- Only pages a search result or a link on the page leads to are opened: no URL is built from a guess, no crawling.
  Per-run and per-domain page budgets, a per-host pause, robots.txt (HTTP backend), identifiable user agent.
- A CAPTCHA, Cloudflare check, login wall or rate limit is never worked around: the engine or site is rested / skipped
  for the run and recorded. LinkedIn, Indeed, Naukri, Glassdoor and similar portals are never fetched.
- Business exclusions (big brands, autonomous driving, competitors, Canada / UK / Germany) are in `config/leadgen.yaml`.

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
