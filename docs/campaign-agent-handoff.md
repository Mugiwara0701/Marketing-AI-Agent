# Campaign Agent — Project Handoff

Purpose of this file: give a **new Claude session in a new repository** everything it needs to start building the **Campaign Agent (CA)**, without the chat history of the repository where it was designed. Read it top to bottom before writing code.

- Written: 2026-10-12, from the Marketing Agent repository, branch `feature/form-detection`.
- Two longer design documents exist in that repository and should be copied into the new repo's `docs/` if possible: `docs/campaign-setup-agent-architecture.md` (full CA architecture, 24 sections, revision 2) and `docs/marketing-agent-changes-for-campaign-setup.md` (what must change in the Marketing Agent). This file summarizes both and adds project background. Where this file and those disagree, ask the user.
- Evidence labels: **[verified]** = read in the Marketing Agent code; **[proposal]** = designed, not built; **[open]** = a decision the user has not made. Nothing in the CA design has been implemented, run or benchmarked. Numeric targets are objectives, not measurements.
- The user is Akshat Nautiyal, who owns the Marketing Agent. A separate developer builds the dashboard. Do not add Claude co-author or attribution lines to commits or PRs unless the user's instructions in the new repo say otherwise (in the old repo the user's standing rule was: never add Claude attribution).

---

## 1. The two systems

### 1.1 Marketing Agent (MA) — exists, in production use, separate repo

B2B lead generation and outreach for an **AOSP / BSP / embedded Linux engineering-services company**. It finds **companies that may buy our engineering** (they build EV chargers, kiosks, industrial controllers, robots, medical devices, infotainment, IoT/edge devices on Android or embedded Linux, or ask for outside engineering help). It never targets sellers of boards/devices or competitors. Every outreach email is sent **only after a person approves it in Slack**.

Pipeline (Python 3.12, package `agent/leadgen/`), run by `python -m agent leads run [--dry-run]`:

1. **Search strategy** (`strategy.py`): builds queries from technology × business intent × product domain, in families (`builder`, `need`, `hiring`, `rfp`, `direct`, `partner`), no repeats within `repeat_after_days`. Every query passes `check_customer_query()` (no supplier words like "manufacturer", "price"). The target (`potential_customer`) is fixed in code, never configurable.
2. **Browser**: `http` (SearXNG + polite HTTP), `chrome` (Playwright) or `desktop` (visible Chrome driven with xdotool + OCR).
3. **Page rules**: shops, marketplaces, distributors, docs, directories, competitors are rejected without a model call.
4. **LLM reading** (`lead.assess`): page type, company, product, signal, needs, quotes (one call per page).
5. **Code checks**: company named on page, quotes literally present, website never guessed, exclusions.
6. **Score 0–100** (`scoring.py`), **dedup** (one company = one lead), **contact discovery** (company's own site only, never guessed), **email draft** from evidence only, **Slack approval** (Approve/Reject), **sending** only approved emails through one gated sender (`sender.py`).

Lead states: `DISCOVERED → QUALIFIED → CONTACT_FOUND → EMAIL_DRAFTED → PENDING_APPROVAL → APPROVED → SENT`, plus `REJECTED`, `FAILED`.

**Deployment shape (important for the CA design) [verified]:**

```text
Dashboard frontend -> Dashboard backend (other developer, own repo)
        │ HTTPS, Bearer token
        ▼
Pipeline control API (api/, FastAPI on Render)
        │ calls SQL function request_pipeline('running'|'stopped', requested_by)
        ▼
Supabase Postgres: pipeline_control (one row) + leads, emails, approvals, runs
        ▲ the MA polls every 2 s and writes a heartbeat + status back
Office machine (Xubuntu, BEHIND NAT): `python -m agent start`, systemd service `marketing-agent`
        Chrome · Ollama (local LLM, CPU only) · SearXNG · Gmail API
```

- **Nothing can call the MA.** It only makes outbound connections. Anything that must reach it goes through the Supabase database it polls.
- The pipeline runs only between a dashboard **Start** and **Stop**. A pass = lead discovery → follow-ups → blog step, 30 min apart. Separate loops: sender (20 s), inbox poll (10 min).
- Email safety is enforced **in code and SQL**, not configuration: an email is sent only if it is `approved`, has an `approvals` row with a recorded human decision, and `EMAIL_SENDING_ENABLED=true`. This must never become a campaign setting.
- Models: open models behind an OpenAI-compatible endpoint (`LLM_BASE_URL`); `config/routing.yaml` maps tasks to aliases (default `dev`, repo alias `Qwen/Qwen3.5-9B` in `services/llm-service/config/models.yaml`). On CPU-only hosts model time limits are raised (`LLM_MIN_TIMEOUT` 420 s).
- Shared Python library `libs/agentkit` (LLM client, DB access, prompts, Slack, Gmail, structured JSON logging via `get_logger`). Tooling: ruff, mypy, sqlfluff, yamllint, markdownlint, pytest, gitleaks, pip-audit; CI in `.github/workflows/`.
- Supabase migrations in `supabase/migrations/` (`0011_pipeline_control.sql`, `0012_pipeline_api_role.sql` are the pattern to copy for a limited-privilege role).

### 1.2 Campaign Agent (CA) — to be built, in the new repo

The CA turns a user's plain-language campaign description into a **validated, versioned campaign configuration** for the MA, with human review in the dashboard.

**The CA defines the campaign. The MA executes it.** The CA does not find leads, qualify companies, research prospects, find contacts, write emails, send anything, or run any pipeline step.

---

## 2. What the CA must do (confirmed flow)

1. A user writes a campaign message in the dashboard. The **dashboard backend** (not the browser) sends it to the CA over HTTPS with a service token.
2. The CA analyses it (LLM extraction plus **limited web search for campaign-concept research**) and drafts campaign details and a campaign configuration (`leadgen.yaml` overlay).
3. The CA returns the campaign details to the dashboard for review: summary by section, provenance of each field, questions, suggestions, "not applied" list, YAML preview, validation results.
4. The user asks for edits in the dashboard; the backend forwards them; the CA revises. Repeat.
5. The user approves. The dashboard sends the approval (exact version + sha256) to the CA.
6. The CA **publishes** the approved version to the shared Supabase database. The MA picks it up at the start of its next pass and runs the campaign accordingly.

Example user message: *"Find US companies with 500–5,000 employees building automotive infotainment on Android and find the right decision makers."* Parts of this are **not expressible today** (US-only, company size, decision-maker roles); the CA must report them as "not applied" with the reason, never invent keys or silently drop them.

### Two different approvals — do not confuse them

- **Campaign approval** (dashboard → CA): publishes a configuration. Sends no email.
- **Email approval** (Slack, per drafted email): the MA's own gate before anything is sent. Unchanged; not configurable by the CA. The CA must refuse to emit anything like `approval.required: false`.

---

## 3. Key findings that shape the design [all verified in MA code]

1. **The MA cannot be called**; hand-off must be a database pointer the MA reads.
2. **The MA has no database-backed campaign today.** It reads one local file chosen by `LEADGEN_CONFIG` (default `config/leadgen.yaml`); `service.run_leads()` calls `config.load()` at the start of each run, which is the natural pickup point.
3. **The loader is loose.** `agent/leadgen/config.py::load()` does `yaml.safe_load` then `raw.get("section") or {}`. Unknown/misspelled keys, wrong types and out-of-range values are accepted silently (`min_qualfy: 70` falls back to the default). So "the MA accepts the file" proves nothing until the MA gets a strict schema.
4. **Defaults disagree inside the MA.** `accept_service_companies` defaults `True` in `qualify.py`/`pipeline.py` but `False` in `contacts.py`/`strategy.py` and the file. Code defaults also differ from the file (`queries_per_run` 12 vs 16, `max_pages_per_run` 60 vs 100, `pages_per_query` 4 vs 6).
5. **`limits.host_delay_seconds` is read by no code** (dead key). The CA must not emit it.
6. **Most of `leadgen.yaml` is not campaign-specific**: ~400 lines of tuned vocabulary (capabilities, ~80 products, industries, builder/need phrases with fragile stride rules, RFP words) plus standing exclusions. The CA generates a **campaign overlay** merged onto a maintained base; it does not author the vocabulary lists in the MVP.
7. **Not expressible today**: US-only / include-countries, company-size range, decision-maker role preference, LinkedIn/other channels. Email approval is code-enforced. Email wording lives in `agent/prompts/*.txt` and `config/outreach_services.txt`, outside `leadgen.yaml`.
8. A technology (e.g. Android, RFID) is a **modifier of a product** in the MA's strategy, not a search target. "Find companies working on Android" is ambiguous and needs a question ("what product do they build?").

### Current `config/leadgen.yaml` structure (the file the CA's output must satisfy)

```yaml
search:            # queries_per_run, repeat_after_days, results_per_query, use_job_feeds, feed_items,
                   # pages_per_query, query_suffix, capabilities[], products[], industries[], platforms[],
                   # builder_phrases[], need_phrases[], rfp_words[]
project_context: {}  # name, industries[], products[], technologies[], hardware[] (alias merged into technologies),
                     # engineering_requirements[]  -- shapes query text only
qualification:     # min_qualify 50, min_draft 60, min_confidence 0.5, accept_hiring_signals true,
                   # accept_service_companies false, max_age_days 120, min_relevance_terms 2
exclude_terms: []  exclude_companies: []  exclude_countries: []  exclude_tlds: []
contacts:          # max_pages 4, max_attempts 3, link_labels [[...],[...]]
limits:            # max_new_leads_per_run 10, max_pages_per_run 100, max_pages_per_domain 6,
                   # host_delay_seconds 2.0 (DEAD), engine_rest_minutes 30
desktop_engines: [...]   # infrastructure, never emitted
```

Business rules the CA's validator must enforce: `min_draft >= min_qualify`; `max_pages_per_run >= queries_per_run × pages_per_query` (the file comment says late queries are starved otherwise); `results_per_query >= pages_per_query`; hard maxima on `queries_per_run`, `pages_per_query`, `max_pages_per_run`, `max_pages_per_domain`, `max_new_leads_per_run`; exclusion lists must not be shrunk below the base without an explicit confirmed user action; no secrets in YAML.

### What the CA can map today vs not

| User concept | Target key | Status |
|---|---|---|
| Campaign name, products, industries, technologies, engineering requirements | `project_context.*` | Supported |
| Capability selection | `search.capabilities` (select from base list only) | Supported |
| Thresholds, hiring/service policy | `qualification.*` | Supported |
| Query volume | `search.{queries_per_run, repeat_after_days, results_per_query, pages_per_query, ...}` | Supported |
| Exclusions | `exclude_terms/companies/countries/tlds` | Supported |
| Lead caps and pacing | `limits.{max_new_leads_per_run, max_pages_per_run, max_pages_per_domain, engine_rest_minutes}` | Supported |
| US-only / include countries | proposed `targeting.include_countries` | **Unsupported** (MA feature B3) |
| Company size | proposed `targeting.employee_range` | **Unsupported** (MA has no employee data; B4) |
| Decision-maker roles | proposed `contacts.preferred_roles` (controlled vocabulary) | **Unsupported** (hard-coded `ROLE_PRIORITY` in `contacts.py`; B5) |
| Email approval off | none | **Never** emitted |
| LinkedIn / other channel | none | **Unsupported** |
| Email tone/services text | not in YAML | Out of scope |

---

## 4. Recommended architecture [proposal]

**Modular monolith**: Python 3.11+, FastAPI, Pydantic v2, PyYAML (`safe_load`/`safe_dump` only), httpx, in-process job workers with jobs persisted in Postgres, Supabase Postgres as store and hand-off channel (SQLite for local dev only). No vector DB, no agent framework, no microservices.

### 4.1 Pipeline per user turn

```text
API (auth, idempotency, 202 + job)
 -> Input Guard (L1: empty, size, charset, secret-like strings, language)
 -> Extractor (LLM, 1 call): text or edit request -> IRPatch + research_needs (closed set)
 -> Patch Merger: closed schema, provenance, locks; unknown fields rejected
 -> [optional] Research: deterministic query templates -> query policy -> search -> fetch -> Summarizer (LLM) -> Suggestions
 -> Campaign IR (typed, single source of truth, persisted)
 -> Gap & Conflict Engine (L2) -> templated Questions (max 3 per turn, never repeated)
 -> Compiler (pure): IR -> overlay merged on pinned base config, with documented defaults
 -> Validators L3 schema (Pydantic), L4 business rules
 -> YAML Serializer (stable order, safe_dump, provenance header)
 -> Contract Validator L5 yamllint + round-trip, L6 the MA's real strict loader in an isolated subprocess
 -> Freeze candidate version (yaml, sha256, IR, report) -> returned to dashboard
```

Only **extraction** and **research summarization** use an LLM. Everything else (gaps, questions, defaults, compilation, validation, serialization, publishing) is deterministic. Normal job: 1–2 model calls; hard cap 6. The model never writes YAML or keys; it produces campaign data into a closed schema.

### 4.2 Campaign IR (Pydantic, `extra="forbid"`)

```text
CampaignIR
  meta        id, name, description, contract_version, revision
  objective   type (enum; single supported value customer_discovery), summary
  icp         standing description of good-fit companies (from base; shown read-only/extendable)
  audience    industries[], products[], technologies[], capabilities_needed[], signals[], geography?, size?
  qualification   min_qualify, min_draft, min_confidence, accept_hiring_signals, accept_service_companies, max_age_days
  contacts    preferred_roles[]?  (proposed extension)
  exclusions  companies[], terms[], countries[], tlds[]
  limits      max_new_leads_per_run, queries_per_run, pages_per_query, ...
  approval    fixed: required by the MA (read-only)
  provenance  per field: {status: confirmed|inferred|suggested|defaulted|unset,
                          source_turn, evidence_quote, sources[]}
```

Field metadata (from the contract package): `blocking`, `default`, `mapping_status` (supported/verify/proposed), `vocabulary`, `help_text`, `bounds`. Defaults come from the contract, never from the model. Explicit null from the model = missing; only the user can clear a field. Statements about the organisation ("we are a 200-person BSP firm") are stored as organisation context and not emitted. An unscoped preference ("we prefer small companies") becomes a question: this campaign or all?

### 4.3 Conversation state machine

States: `DRAFT → INTERPRETING → (RESEARCHING) → CLARIFICATION_REQUIRED | READY_FOR_VALIDATION | MODEL_UNAVAILABLE`; `READY_FOR_VALIDATION → VALIDATING → VALIDATED | VALIDATION_FAILED`; `VALIDATED → AWAITING_REVIEW → APPROVED → PUBLISHED | PUBLISH_FAILED`; also `DISCARDED`. A change after publication creates version N+1; version N stays active until N+1 is itself approved and published.

Rules: only the **latest** candidate can be approved (stale version or sha256 → `409`); direct field edits skip the LLM and go to validation; web-derived values are always `suggested` (shown with source URLs, never auto-confirmed, never overwrite confirmed values); one running job per campaign; `base_revision` optimistic concurrency on edits.

### 4.4 Web research boundary

- **Allowed**: concept research only (what a term means, which product categories use a technology, which industries a capability maps to).
- **Forbidden**: searching for companies, people, contact details, prospect lists. Enforced by a **query policy in code** (blocks "companies using", "list of", "customers of", names, emails/contact words, supplier words, anything matching an exclusion), not by prompt wording.
- Fixed pipeline, not a model-driven loop: the Extractor may emit up to 3 `research_needs` from a closed set (`term_meaning`, `product_categories_for_technology`, `industries_for_capability`); deterministic templates build the queries; ≤6 searches and ≤5 page fetches per job; allow/deny domain lists, timeouts, size caps, caching. Search results and fetched pages are **untrusted data** (prompt-injection surface). Fail-soft: research disabled/failing/empty → continue without suggestions. Behind a feature flag; benchmark research-on vs off.
- Search provider: the MA repo has a SearXNG deployment (`deploy/searxng/`); reuse for the CA is unverified [open].

### 4.5 LLM strategy

One model for the MVP: a 7–9B instruct model, 4-bit, via a provider-agnostic adapter (`OllamaAdapter` for dev, `OpenAICompatAdapter` for vLLM/hosted). Use the strongest supported mode (schema-constrained decoding → JSON mode → prompt-only JSON with strict parsing); schema validation is mandatory in every mode. temp 0, at most 1 repair call and 1 fallback per call site. User/web text is delimited as data. The dev laptop (RTX 4050 6 GB, 16 GB RAM) may fit a 4-bit 7–9B with small context — **not measured**; fallbacks are a 3–4B model, CPU offload, or a remote endpoint. Choose the smallest model that meets benchmark targets.

### 4.6 Publish and pickup [proposal; needs MA changes]

- New tables in the MA's Supabase project (migration lives in the MA repo's `supabase/migrations/`): `campaign_versions` (immutable: campaign id, version, overlay yaml, sha256, contract version, approved_by, approved_at), `campaign_active` (pointer to the active version + sha256). The CA also keeps its own working tables (`campaigns`, `campaign_turns`, `campaign_jobs`).
- **Publisher** uses a **separate DB role** that can insert into `campaign_versions`, update `campaign_active` (via a function), and optionally execute `request_pipeline()` — nothing else. Mirror `0012_pipeline_api_role.sql`. The CA has no credentials for Slack, Gmail, lead tables or the office machine.
- Publish is one transaction: verify approver, latest candidate and sha256; re-run L5–L6; insert approval; update `campaign_active`.
- **MA side**: at the start of each discovery pass, read `campaign_active`, strict-load, check sha256, merge over base, use for the whole pass. Switch only at a pass boundary. On any failure keep the last good campaign, report the error in `last_pass`/status, never run half-applied. No campaign published = behave exactly as today.
- MA reports `current_campaign: {id, version, sha256, applied_at, error?}` in pipeline status so the dashboard shows what is actually running.
- Optional `start_pipeline` flag on approval calls the same `request_pipeline` path as the Start button [open: does approval start the pipeline? Recommendation: publish only].

### 4.7 API to the dashboard backend

Base `/api/v1`, `Authorization: Bearer <CAMPAIGN_AGENT_TOKEN>`, `Idempotency-Key` on mutations, `requested_by` (≤100 chars) on every mutation, UTC ISO 8601. Model-time mutations return `202` + job; the dashboard polls `GET /campaigns/{id}` every 5–10 s (same pattern as the pipeline status). Optional HMAC-signed webhook later.

| Method / path | Purpose | Notable errors |
|---|---|---|
| `POST /campaigns {message, requested_by}` | create draft, start interpretation → `202 {campaign_id, job_id}` | 401, 422, 429 |
| `GET /campaigns/{id}` | full view: state, job+step, IR view with provenance, questions, suggestions, unsupported list, validation per level, candidate `{version, sha256, contract_version, yaml}`, active version | 404 |
| `POST /campaigns/{id}/answers {answers:[{question_id, value\|"skip"}]}` | answer clarifications → 202 | 409, 422 |
| `POST /campaigns/{id}/edits {text \| ops:[{path,value}], base_revision}` | edit in words or direct field override | 409 stale, 422 |
| `POST /campaigns/{id}/suggestions/{sid} {decision: accept\|reject}` | resolve web suggestion | 404, 409 |
| `POST /campaigns/{id}/approve {version, sha256, start_pipeline?}` | publish exactly the reviewed candidate → `200 {published, version, effective_from: "next_pass"}` | 409 stale/hash mismatch, 422 not VALIDATED or L6 fails, 502 store failure (`PUBLISH_FAILED`, retry safe) |
| `POST /campaigns/{id}/discard` | drop draft | 409 if published |
| `GET /campaigns/{id}/versions[/{n}]` | history | 404 |
| `GET /jobs/{id}`, `GET /health`, `GET /openapi.json` | job status, liveness, spec | |

### 4.8 Validation ladder

| Level | Check | LLM? |
|---|---|---|
| 1 Input | empty, size, charset, language, secret-like strings | No |
| 2 Semantic | missing blocking fields, contradictions, unsupported requests, target drift (supplier/manufacturer wording) | feeds only |
| 3 Schema | IR + compiled config match Pydantic; enums, ranges | No |
| 4 Business | rules listed in section 3 | No |
| 5 YAML | `safe_load`, `.yamllint.yaml`, round-trip equality; reject anchors/aliases/tags/multi-doc | No |
| 6 Downstream | the MA's real strict loader accepts the file (isolated subprocess, no network, timeout, pinned MA commit) + no-network query-generation check (`python -m agent leads queries` equivalent) | No |

L6 runs when a candidate is frozen and again right before publish. Repair of L3/L4 failures is deterministic or a question; never "ask the model to make it pass".

### 4.9 Security

Untrusted: user text, web content, LLM output. Model has no tools; emits only `IRPatch`/`Suggestion`. YAML built from typed values, never string-templated. Hard caps on limits that affect third-party sites and cost. Secret-like strings rejected at input and never sent to the model or search. Audit every approval (`approved_by`, version, sha256, time). Dashboard user auth is the dashboard developer's job; the CA trusts its service token and records the claimed user. Defense in depth: the MA re-validates and checks sha256.

### 4.10 Error handling (summary)

Model down → retry once, state `MODEL_UNAVAILABLE`, draft kept, field edits still work. Malformed JSON → one repair, one fallback, then ask user to rephrase. Search failing → continue, warn. Query blocked → dropped, logged. Patch violates a lock → dropped, user told which confirmed value was kept. Context overflow → compact IR summary, drop oldest turns, never the latest text. L5 failure = serializer bug, fail closed. L6 failure → no candidate/publish. Stale approval → `409` with current candidate. MA offline → publish still succeeds; applied at next pass after it returns. MA rejects a published campaign → keeps last good, reports in status.

---

## 5. Decisions already made vs still open

**Made (by the user, in the earlier design sessions):**

- Dashboard is built by another developer and drives the CA over an API; the CA ships no UI.
- CA may use **limited web search for campaign research** (concept research only).
- After dashboard approval the CA **publishes** the campaign for the MA to run; the CA does not run the MA.
- Architecture first; nothing implemented yet.

**Open — ask the user before coding the affected part:**

| # | Question | Recommendation |
|---|---|---|
| U1 | US-only, company size, decision-maker roles: build in the MA, or permanent "not applied"? | Report "not applied" in MVP; schedule MA work separately |
| U2 | Does approval start the pipeline? | Publish only; optional `start_pipeline` flag |
| U3 | When may the MA switch campaigns? | At the next pass boundary |
| U4 | Polling vs webhook for dashboard | Polling baseline |
| U5 | Search provider for research | SearXNG if reusable; otherwise decide |
| U6 | Per-key consumer trace of `leadgen.yaml` | Do before coding (MA repo) |
| U7 | Model access: standalone adapter vs MA's `services/llm-service` | Own adapter; may point at the same OpenAI-compatible endpoint |
| U8 | Retention of campaign text and fetched web content | Open |
| A5 | Contract schema ownership/packaging | Lives in the MA repo, imported by the CA as a versioned dependency |
| — | Overlay vs full file | Overlay on a pinned base |
| — | Hosting of the CA (must be reachable by the dashboard backend; not the office machine) | Open |

---

## 6. Cross-repo dependencies (the CA cannot be trusted end-to-end until these exist in the MA)

Priority P0 for starting the CA:

1. **Strict, versioned loader** in the MA: Pydantic model of the whole file, `extra="forbid"` at every level, ranges, `config_version: "1.0"`; defaults declared once (resolve `accept_service_companies` and other inline `cfg.q(key, literal)` defaults); business rules enforced there.
2. **Contract package** importable by the CA: model, version, defaults, field metadata (campaign-owned vs base/infrastructure), vocabularies; golden files (`config/leadgen.yaml` + valid/invalid fixtures) tested in both CIs.
3. **Overlay merge**: validated campaign-owned keys merged over the base config; `--campaign <file>` for local/dry-run; effective config hash + campaign id written next to each run report.
4. **Publish/pickup channel**: migration (`campaign_versions`, `campaign_active`, Publisher role), pass-start pickup with rejection behavior, `current_campaign` in pipeline status.

P1/P2 (can follow the CA MVP): include-countries (B3), employee size (B4 — needs extraction in the MA first), preferred decision-maker roles from a controlled vocabulary (B5; never accept free-text regexes), vocabulary load-time checks, campaign identity on leads.

**Until these exist, the new repo can still start**: build against a vendored copy of the current keys with a Pydantic model written in the CA repo, mark it as a stand-in, and replace it with the MA's contract package later. Do not claim L6 "downstream acceptance" is meaningful before the MA loader is strict.

---

## 7. Recommended MVP (smallest reviewed → published, valid `leadgen.yaml`)

1. Contract stand-in (strict Pydantic model of current keys, base config, field metadata).
2. IR with provenance for supported concepts only.
3. Extractor + Ollama adapter (+ OpenAI-compatible), one repair call.
4. Patch Merger, gap engine with templated questions, "not applied" report.
5. Compiler (overlay on base) + serializer; validators L3–L5 (L6 once the MA loader exists).
6. Postgres store; async job API (create, get, answers, edits, approve, discard, versions).
7. Publisher with limited DB role.
8. Research module behind a flag, enabled once the query-policy tests pass.

Deferred: webhooks, version diffs, vocabulary expansion, LLM-worded questions, multi-language, MA extension keys, RBAC, multiple concurrently active campaigns.

**Suggested first work in the new repo (in order):** scaffold (pyproject, ruff, mypy, pytest, CI mirroring the MA's lint/test/security workflows) → contract stand-in + golden fixtures → IR + compiler + serializer + validators (all deterministic, fully testable without a model) → LLM adapter + extractor with a stub model → gap engine/questions → store + job API → publisher → research.

---

## 8. Testing and evaluation plan

- **Unit**: compiler purity and byte-identical output, merger lock rules, gap engine, each business rule, serializer, input guard, query policy (adversarial: "list of companies using AOSP", names, emails).
- **Contract**: real MA loader on golden and generated files; unknown key fails; every compiled key is in the allow-list.
- **Model robustness**: malformed JSON, extra keys, wrong enums, null storms, injection strings in user text **and fetched pages**, oversized text.
- **Conversation**: ambiguity cases ("embedded engineers" → ask industries; "automotive" → ask capability; "Android" → ask product; "campaign for our services" → ask industry/area), contradictions, correction of confirmed values, repeated questions, "skip", suggestion accept/reject.
- **Job/API**: idempotency, stale `base_revision`, stale approval, approval after discard, double approve, publish retry, concurrent edits.
- **Hand-off**: publish to a test DB; MA loader picks it up at pass start; invalid published campaign leaves the previous one active.
- **E2E**: message → approved version → MA `--dry-run` query generation shows queries reflect the campaign. Stub-model deterministic run in CI, nightly real-model run.
- **Benchmark dataset to build**: ~100–200 labeled campaign descriptions (complete, ambiguous, contradictory, unsupported, adversarial, multi-turn, edits), held-out split, research-on/off comparison.

Proposed targets (objectives, not results): schema validity after ≤1 repair ≥98%; missing-field recall ≥95%; clarification quality ≥90% with 0 repeats; downstream acceptance of published versions 100%; research policy violations reaching the search provider 0; research suggestions with valid sources and in-vocabulary values ≥95%; latency/VRAM measured first, then bounded.

---

## 9. Environment and practical notes

- The MA runs on an office Xubuntu machine, CPU-only Ollama. Development laptop: Linux, RTX 4050 6 GB, 16 GB RAM, Ollama. The MA's `--dry-run` writes Slack messages and mail to files, so the CA's hand-off can be tested without real Slack or sending.
- The MA repo lives at `/home/akshat-nautiyal/Desktop/Marketing-AI-Agent` (branch `feature/form-detection`, main branch `main`). It is a separate repo from the CA; the CA must not import MA runtime code directly except through the contract package and the isolated L6 subprocess.
- Secrets (`.env`, `credentials.json`, `token.json`) exist in the MA repo working directory and are git-ignored. Never copy them into the new repo or into any document.
- Conventions worth mirroring from the MA: structured JSON logs with context (no full prompts or fetched pages at INFO), ruff/mypy/yamllint/markdownlint/gitleaks in CI, service bearer-token auth like `docs/pipeline-api.md`.
- Git: do not add Claude co-author/attribution lines to commits or PRs unless the user's new-repo instructions say otherwise.

## 10. How to start the new session

Suggested first message to the new Claude session: *"Read docs/campaign-agent-handoff.md (and the two linked design docs if present). We are building the Campaign Agent in this repo. Start with the scaffold and the deterministic core (contract stand-in, IR, compiler, serializer, validators) and ask me about any item marked open before building it."*
