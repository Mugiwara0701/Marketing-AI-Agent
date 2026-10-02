# Marketing AI Agent: Developer Guide and Work Done So Far

Last updated 2026-10-01 · Author: Akshat · Describes `main` after PR #31 (dashboard) and PR #32 (agent)

This document is for a developer who has not seen the code. It explains what the system does, how it is built, how
each part works, what has been tested, and how to run and extend it. Command and setup details are in
`README.md` and `deploy/README.md`.

## 1. What the system is

A small scheduled agent for an AOSP / embedded-engineering services company. It is **not** a general-purpose LLM
system and it does **not** run continuously. Once a day it runs for at most 2 hours, saves everything to the
database and stops. It has two jobs:

1. **Lead generation (max 10 new leads a day).** Find companies that need AOSP, Android platform, BSP, Embedded
   Linux or device bring-up work done, store them, and draft a company-specific proposal email for each.
   We sell services; we are **not** recruiting. A job post is only evidence that a company has the work.
2. **Daily technical blog (1 post a day).** Research what is hot, write one post grounded in real articles, and
   rewrite it for dev.to, LinkedIn and X.

Every draft goes to Slack for a human to approve. Sending email is implemented but **switched off**.

The LLM is one component. The rest is search and scraping, a database, de-duplication, scheduling, Slack and email.

## 2. Architecture

```text
 systemd timer (daily)
        │
        ▼
 python -m agent run ──────────────► Supabase Postgres (leads, emails, posts, runs)
   │  agent/run.py                          ▲
   │                                        │ asyncpg
   ├─ send approved emails  (agent/mailer.py, off by default)
   ├─ leads  (agent/leads.py)  ── sources ─► job APIs, company hiring feeds, SearXNG, company websites
   ├─ blog   (agent/blog.py)   ── research ► HN, dev.to, news feeds, source articles
   │
   ├─ every LLM call ──► agentkit.task_runner ─► agentkit.llm ─► Ollama (laptop) or vLLM (office GPU)
   └─ Slack posts   (agent/notify.py) ──► Slack ──► button click ──► Supabase Edge Function
                                                                      (slack-interact) ──► database row
```

Design rules that explain most of the code:

- **One bounded run.** Time budget, per-step timeouts, and every step can fail without stopping the next.
- **Idempotent.** Re-running the same day does not duplicate leads, listings or the blog post.
- **LLM output is never trusted.** Schema-checked JSON, then code checks, then a human approves.
- **Free sources only.** No paid APIs. Portals that forbid scraping are blocked in code.
- **Approval before anything goes out.** Nothing is emailed or published without a person.

## 3. Repository map

| Path | Purpose |
|---|---|
| `agent/` | The daily agent (about 2,900 lines of Python) |
| `agent/run.py` | Orchestrates one daily run with the time budget |
| `agent/leads.py`, `contacts.py`, `sources.py`, `portals.py`, `web.py` | Lead pipeline, contact finder, listing sources, polite fetching |
| `agent/blog.py`, `research.py`, `variants.py` | Blog pipeline, topic research, per-platform versions |
| `agent/notify.py`, `slack_setup.py` | Slack posting and channel creation |
| `agent/mailer.py`, `review.py` | Email sending, command-line approval |
| `agent/store.py`, `migrate.py` | Database reads and writes, migration runner |
| `agent/demo.py`, `dryrun.py`, `__main__.py`, `settings.py` | Preflight and demo, no-database test mode, command-line tool, limits |
| `agent/tasks/` | The six LLM tasks with their schemas and output checks |
| `agent/prompts/` | One prompt file per task |
| `agent/tests/` | Unit tests for the agent |
| `config/sources.yaml` | Keywords, portals, company hiring boards, search queries, research feeds |
| `config/platforms.yaml` | What each publishing platform expects |
| `config/routing.yaml` | Which model alias each LLM task uses (`routing.dev-only.yaml` sends all to one model) |
| `libs/agentkit/` | Shared library: LLM client, database, prompts, checks, Slack helpers, `.env` loading |
| `services/llm-service/` | Docker setup (vLLM and embeddings) for the office GPU machine, plus `make llm-verify` |
| `supabase/migrations/` | Database schema, migrations 0001-0004 |
| `supabase/functions/` | Edge Functions: `slack-interact` (button clicks) and `unsubscribe` |
| `deploy/` | systemd timer and service, daily run script, SearXNG Docker setup |
| `eval/` | Eval runners and small synthetic sets, plus `eval/dashboard/` |
| `docs/` | Design documents (outdated, see section 14), `job-portals.xlsx`, this guide |

`agentkit` also contains an older HTTP job-contract app (`jobs.py`), a learning-loop helper (`learn.py`) and
retrieval helpers (`retrieve.py`, `embed.py`). The daily agent does not use `jobs.py` or `learn.py`; retrieval is
optional and finds nothing while the example and knowledge tables are empty.

## 4. The daily run (`agent/run.py`)

`python -m agent run` does, in order:

1. **Guard.** Skips if a `daily_run` row in `agent_runs` already succeeded today or started under 3 hours ago.
   `--force` overrides this.
2. **Send** approved emails, only if `EMAIL_SENDING_ENABLED=true` (15 minute limit).
3. **Leads.** Gets the remaining time after reserving `BLOG_RESERVE_MINUTES` (25) for the blog.
4. **Blog.** Gets whatever time is left (at least 5 minutes).
5. **Slack summary** to the alerts channel (counts only, no personal data).

Each step is a row in `agent_runs` (`running`, `succeeded` or `failed`). A step that raises or times out is logged
and the run moves on. Total time is capped by `AGENT_MAX_MINUTES` (120). Useful flags: `--only send|leads|blog`,
`--force`, `--redo-blog` (deletes today's saved post, for testing).

## 5. Lead pipeline (`agent/leads.py`)

1. **Slots.** `MAX_NEW_LEADS_PER_DAY` minus leads already created today. Zero means stop.
2. **Retry pass.** Companies saved earlier as `qualified` (no contact found) get a second contact lookup.
3. **Collect** (`sources.collect_signals`). Pulls listings from all sources, keeps only those mentioning a keyword
   from `config/sources.yaml` (whole-word match), removes duplicates, allows at most 2 postings per company, and
   takes listings from each source in turn up to `MAX_SIGNALS_PER_RUN` (150).
4. For each listing:
   - skip if its hash is already in `lead_signals`, or its company was already handled this run;
   - **Qualify** with the LLM (`lead.qualify`). Rejected if not relevant, confidence below 0.6, or no company name;
   - **Website:** the source's known domain, else the model's, else `sources.resolve_website` (web search, or
     guessing `name.com`, `.io`, `.ai` and checking the homepage mentions the name);
   - skip if the domain already exists in `companies`;
   - **Find a contact** (`contacts.find_contact`, section 5.2);
   - save the company (`contact_found` or `qualified`), the contact and the signal;
   - **Draft** the proposal (`outreach.draft`), save it as an `emails` row with status `drafted`, post it to Slack.
5. At most 40 contact lookups per run. The loop stops when slots run out or the deadline passes.

### 5.1 Sources (`agent/sources.py`, `agent/portals.py`)

- Hacker News "Who is hiring" and "Seeking freelancer" threads (Algolia API), RemoteOK, Remotive, Arbeitnow,
  Jobicy, We Work Remotely, Himalayas, The Muse.
- Adzuna and Jooble run only if their keys are in `.env`.
- **Company hiring feeds:** public job feeds of named companies on Greenhouse, Lever, Ashby, Personio, Recruitee
  and Workable. Companies are listed under `company_boards` in `config/sources.yaml` (23 today; Recruitee and
  Workable have code but no companies yet).
- **Web search:** self-hosted SearXNG (`SEARXNG_URL`). DuckDuckGo is only a fallback and usually blocks bots.
- Rate limits from `docs/job-portals.xlsx` are kept across runs in `.cache/portal_calls.json` (Remotive twice a
  day, Jobicy, Himalayas, The Muse, Adzuna and Jooble hourly). A repeated run inside the window skips those sources.
- `web.blocked()` refuses LinkedIn, Indeed, Naukri, Glassdoor, Monster, Foundit and similar, in every country domain.
- All fetching goes through `web.fetch`: robots.txt respected, 1.5 s between requests to a host, identifiable user
  agent, and private or local addresses refused.

### 5.2 Contact finder (`agent/contacts.py`)

Tries `https://domain/` (then `www.`), then `/contact`, `/contact-us`, `/impressum`, `/imprint`, `/about`,
`/company`, `/team`, reading up to 5 distinct pages. Candidate addresses must be on the company's own domain and
not `noreply`, `abuse`, `privacy` and similar. If any exist, the LLM (`lead.extract_contact`) picks one, and code
requires that the address appears literally in the page text. If the model fails, `pick_role_address` takes the
best business mailbox (`sales`, `business`, `partner`, `contact`, `hello`, `info`, `office`, ...) and marks the
draft "chosen by rule". Addresses are never guessed or constructed. Large companies often publish none.

### 5.3 Proposal (`agent/tasks/proposal.py`)

The prompt gets company, project, technologies, location, source and the contact's role, and must write 120-180
words about their project with one concrete first step. Checks: length, banned phrases, unknown company names, and
unbacked claims about past work ("our experience", "we've delivered", "similar projects" and similar). A flagged
draft is still saved, with the problems in `emails.review_note` and shown in Slack.

## 6. Blog pipeline (`agent/blog.py`, `research.py`, `variants.py`)

1. **Research** (`research.research_items`). Collects Hacker News stories (points, comments), dev.to articles
   (reactions) and about a dozen feeds (LWN, Android Developers Blog, Phoronix, Bootlin, CNX Software, Hackaday,
   Android Authority, 9to5Google, Linux Foundation, Yocto, Lobsters). `rank` scores each item as popularity times
   freshness (half-life 7 days, dropped after 21 days) and boosts items about core topics (aosp, bsp, kernel, ...).
2. **Plan.** The LLM gets the top 45 items numbered, the recurring terms and our recent titles, and returns 3-5
   topics. Each topic has a `kind` (news_analysis, explainer, tutorial, checklist) and `source_ids`, the numbers of
   the items it is built on.
3. **Choose.** The first topic that is not similar to a recent title (word overlap) and whose cited articles give
   at least 1,500 characters of text.
4. **Ground.** `gather_sources` fetches each cited article (5,000 characters each, falling back to the item's own
   summary).
5. **Write** (`content.draft_post`, temperature 0.3). The prompt allows only facts found in the sources and has a
   structure per kind. `check_grounded` flags any `CONFIG_` symbol, system path, `--flag` or version number in the
   post that is not in the sources; `check_no_leaks` flags leaked prompt text. If anything is flagged, the model
   gets **one** rewrite request listing the exact problems, and the better of the two drafts is kept. The code then
   appends a Sources section from the real URLs.
6. **Adapt** (`variants.generate`, `content.adapt`). For each platform in `config/platforms.yaml`:
   - **dev.to:** front matter (`published: false`), TL;DR, Key points, What to try, Further reading;
   - **LinkedIn:** plain text, hook, short paragraphs, closing question, 3-5 hashtags;
   - **X thread:** 5-7 posts of at most 270 characters, numbered `1/`, `2/` and so on.

   Each version passes format checks, an invented-detail check against the original post, and one repair attempt.
   Layout mistakes small models make (one big paragraph, stray tag lines, missing numbering) are fixed in code.
7. **Save** the post to `content_posts` (status `drafted`, or `in_review` if checks failed), the platform versions to
   `content_variants`, and post to Slack with the full text and each version in the thread.

Only one post is saved per day (`idempotency_key = blog-YYYY-MM-DD`).

## 7. LLM layer (`libs/agentkit` and `agent/tasks/`)

Every model call goes through `task_runner.run_task`, then `llm.complete`.

1. `prompts.load` reads the active prompt from the `prompt_versions` table, else the file in `agent/prompts/`.
   `template_vars` fill `{{KEY}}` placeholders with trusted text (used for platform rules).
2. Scraped or generated text is wrapped in `<untrusted_data>` tags, and the model has no tools.
3. `llm.complete` sends an OpenAI-style chat request with a JSON schema, validates the reply with pydantic, and
   retries once if invalid. Network errors are retried with back-off, at most 3 calls run at once.
4. The task's `validate` function returns a list of problems. An empty list means it passed; otherwise the item is
   saved but flagged for review. Some tasks also pass the problems back as `feedback` for one rewrite.

| Task | Code | Prompt | Output | Alias |
|---|---|---|---|---|
| `lead.qualify` | `tasks/qualify.py` | `lead_qualify.txt` | relevance, confidence, company, project, technologies, location, website | dev |
| `lead.extract_contact` | `tasks/contact.py` | `lead_extract_contact.txt` | found, email, name, role, evidence | dev |
| `outreach.draft` | `tasks/proposal.py` | `outreach_draft.txt` | subject, body | dev |
| `content.plan` | `tasks/topics.py` | `content_plan.txt` | topics with kind and source ids | dev |
| `content.draft_post` | `tasks/blog.py` | `content_draft_post.txt` | title, Markdown body, tags | primary |
| `content.adapt` | `tasks/adapt.py` | `content_adapt.txt` | title, description, body, tags | dev |

Aliases (`dev`, `primary`, `fast`) map to served names (`agent-dev`, ...) in `libs/agentkit/agentkit/llm.py` and are
chosen per task in `config/routing.yaml`. **To change a model, change what the served name points to** (for Ollama:
`ollama cp <model> agent-dev`); no code changes. `routing.dev-only.yaml` sends everything to `dev`.

Two quirks are handled in `llm.py`: some servers (Ollama) cannot build a decoding grammar from `minLength` and
`maxLength`, so those are removed from the schema sent to the server (pydantic still enforces them), and optional
schema fields are avoided because small models then skip them.

## 8. Database

Supabase Postgres. `python -m agent migrate` applies `supabase/migrations/*.sql` once each. Main tables:

| Table | Holds | Status values |
|---|---|---|
| `companies` | lead company, domain (unique), project, technologies, location, source | `qualified`, `contact_found`, ... |
| `contacts` | public contact with the page it came from (required) | verification |
| `lead_signals` | every listing seen, with a content hash (de-duplication) and the model's verdict | |
| `emails` | proposal drafts and their lifecycle, `review_note` for failed checks | `drafted`, `approved`, `sending`, `sent`, `skipped`, `bounced`, `expired` |
| `content_posts` | the daily post, `metadata` (kind, sources, checks), one per day | `drafted`, `in_review`, `approved`, `rejected`, ... |
| `content_variants` | one row per platform version of a post | |
| `topics` | the chosen topic | |
| `agent_runs` | one row per step with counts and errors | `running`, `succeeded`, `failed` |
| `suppression_list` | hashed addresses never to email again | |
| `send_counters`, `approvals`, `team_members` | daily send cap; audit rows from the command-line approvals; reviewers | |
| `examples`, `knowledge_*`, `prompt_versions`, `retrieval_logs` | optional retrieval and prompt storage (empty today) | |

Nothing about Slack is stored (no message ids, no approval rows for Slack).

## 9. Slack and approvals

- `notify.post_email` and `notify.post_blog` post right after an item is created. Failures are logged and never
  fail the run. `python -m agent notify` manually re-posts everything still undecided (it can duplicate messages).
- Each button carries `email:<id>` or `post:<id>`. The Edge Function `slack-interact` checks Slack's request
  signature, checks the clicker is in `SLACK_ALLOWED_USERS` (or `team_members`), then updates the row only if it
  is still undecided: email `drafted` to `approved` or `skipped`, post `drafted` or `in_review` to `approved` or
  `rejected`. A second click gets "Already decided".
- Scraped text is escaped so a listing cannot inject links or mentions into Slack.
- Without Slack, the same decisions can be made on the command line: `review`, `show <id>`, `approve`, `reject`.

## 10. Email sending (off by default)

`agent/mailer.py`, enabled only with `EMAIL_SENDING_ENABLED=true`. It takes `approved` emails, skips suppressed
addresses, applies the daily cap (`try_increment_send` in the database), adds the identity and postal-address
footer and a signed unsubscribe link with `List-Unsubscribe` headers, and sends over SMTP. In `APP_ENV=dev` only
domains in `ALLOWED_RECIPIENT_DOMAINS` are allowed. The `unsubscribe` Edge Function writes to `suppression_list`.
A failed send is retried up to 3 times, then marked `expired`. **This has never been run**; before using it set up
SPF, DKIM and DMARC for the sending domain.

## 11. Configuration

All settings are environment variables loaded from `.env` (template: `.env.example`; real environment variables
win). The main ones:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Supabase **transaction-pooler** string. The direct `db.<ref>.supabase.co` host is IPv6 only |
| `LLM_BASE_URL`, `LLM_API_KEY`, `ROUTING_CONFIG` | Model server and routing file |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_OUTREACH`, `_CONTENT`, `_ALERTS` | Slack posting |
| `SEARXNG_URL` | Web search server |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `JOOBLE_API_KEY`, `THEMUSE_API_KEY` | Optional portal keys |
| `MAX_NEW_LEADS_PER_DAY`, `MAX_SIGNALS_PER_RUN`, `AGENT_MAX_MINUTES`, `BLOG_RESERVE_MINUTES` | Run limits |
| `EMAIL_SENDING_ENABLED`, `SMTP_*`, `MAIL_FROM`, `COMPANY_NAME`, `COMPANY_ADDRESS`, `UNSUBSCRIBE_*`, `APP_ENV` | Email |

Edge Function secrets (`SLACK_SIGNING_SECRET`, `SLACK_ALLOWED_USERS`, `UNSUBSCRIBE_SECRET`) are set in Supabase,
not in `.env`. Behaviour that changes often lives in YAML: `config/sources.yaml` and `config/platforms.yaml`.

## 12. Running and testing

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                  # fill in values
python -m agent check                 # LLM, database, search, Slack, email switch
python -m agent migrate               # create tables
python -m agent demo                  # check, migrate, full run, print the results
```

| Command | Use |
|---|---|
| `python -m agent run [--only leads\|blog] [--force] [--redo-blog]` | The daily run |
| `python -m agent dryrun [--blog-only] [--leads N]` | Real scraping and real LLM, **no database, no Slack**; writes `out/*.md` |
| `python -m agent review` / `show <id>` / `approve` / `reject` | Handle drafts without Slack |
| `python -m agent variants [post id]` | Print the saved platform versions |
| `python -m agent notify` / `slack-setup` / `send` | Re-post to Slack, create channels, send approved emails |

**Scheduling:** see `deploy/README.md` (systemd timer). **Office LLM host:** `services/llm-service/README.md`.

**Tests:** `pytest agent libs/agentkit` runs 53 unit tests. They never touch a real database, LLM host or mail
server (`conftest.py` clears those settings). `make check` runs ruff, mypy and the tests. `make llm-verify` checks a
real model host against the small synthetic sets in `eval/sets/`. CI (`ci.yml`, `lint.yml`, `security.yml`) runs
tests, linting (ruff, mypy, sqlfluff, yamllint, actionlint, deno, markdownlint) and secret and dependency scans.

## 13. How to extend

- **Add a company hiring feed:** add `{board, name, domain}` under `company_boards` in `config/sources.yaml`.
- **Add a job source:** write a function in `agent/portals.py` that returns `Signal` objects, call it from
  `collect_signals`, and add its limit to `MIN_INTERVAL` if it has one.
- **Add or remove a publishing platform:** edit `config/platforms.yaml`. A new `render` kind needs a layout and a
  check in `agent/tasks/adapt.py`.
- **Add an LLM task:** a schema and function in `agent/tasks/`, a prompt file named after the task (`.` becomes `_`),
  and a line in `config/routing.yaml`. Make every schema field required.
- **Change keywords or research feeds:** `config/sources.yaml`.
- **Change a model:** section 7.

## 14. Status

**Run for real** (laptop with a 6 GB GPU, Qwen3 4B on Ollama, a real Supabase project):

- Migrations 0001-0004 applied.
- Blog path end to end: the latest post was grounded in two real articles with no invented commands, and the
  dev.to, LinkedIn and X versions passed their checks.
- Lead path: one run read 17 new listings, qualified 10, and produced 1 lead with a contact and a draft. The
  contact finder was improved afterwards and tested on real sites.
- Collection returns about 300 candidate listings (job feeds, company boards, Adzuna, Jooble, search).
- Slack: the token works, a lead and a blog draft were posted, and `slack-setup` created the channels.

**Not tested yet:** the Slack buttons (the Edge Function is not deployed and the Interactivity URL is not set),
email sending and the unsubscribe function, the systemd timer, the 9B model through the agent (`qwen3.5:9b` is
downloaded locally), the vLLM stack on the office GPU, and the lint jobs for SQL, YAML, shell and TypeScript.

**Dashboard** (`eval/dashboard/`, added by a teammate): a static UI with Dashboard, History and Blogs pages that
runs on dummy data. All data calls are in `js/api.js`, with the matching endpoint named in a comment.

## 15. Known limits and lessons learned

- **Few leads.** The free sources and a small model find a few leads a day, not 10. Many large companies publish no
  contact email, and some sites forbid bots in robots.txt. A company-size filter is not built, so large
  enterprises still qualify.
- **Small models make things up.** The grounding checks catch invented identifiers, not every wrong claim. A
  person must read every technical post. Judge real quality on the 9B model.
- **Gotchas already hit:** Supabase direct host is IPv6 only (use the pooler string); passwords with `?` or `@`
  are handled by `db.parse_dsn`; Ollama fails on length bounds in schemas; DuckDuckGo blocks bots (use SearXNG);
  `.env` needs a trailing newline before appending; Docker needs the user in the `docker` group; repeated test
  runs skip portals inside their rate-limit window.
- The design documents in `docs/` (Architecture, Milestones, Research Proposal) describe the earlier
  three-service design. `Implementation_Status.md` also describes it up to section 8.
- Portal terms have not been reviewed by a senior person, as the portal spreadsheet asks.

## 16. Next steps

1. Deploy `slack-interact`, set the Interactivity URL, and test Approve and Reject.
2. Run on the office GPU with the 9B model and compare lead and post quality.
3. Run the daily job for a week with email off and review every lead and draft by hand.
4. Label 30-40 real listings in `eval/sets/lead_qualify.jsonl` and gate on accuracy with `make llm-verify`.
5. Install the systemd timer.
6. Add a company-size filter and more company hiring feeds.
7. Connect the dashboard to the database.
8. When ready, test email to your own addresses (SPF, DKIM, DMARC, footer, unsubscribe, spam placement).
