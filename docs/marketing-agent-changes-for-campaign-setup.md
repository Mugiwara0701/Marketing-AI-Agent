# Marketing Agent — Fixes and Additions Required for the Campaign Setup Agent

Companion to [campaign-setup-agent-architecture.md](campaign-setup-agent-architecture.md). That document designs the Campaign Setup Agent (CSA); this one lists what must change **in this repository (the Marketing Agent, MA)** so a generated `leadgen.yaml` can be trusted and so the campaigns users ask for are expressible.

Naming: the architecture document (revision 2) calls the new system the **Campaign Agent (CA)**. "CSA" below is the same system under its earlier name. Its flow: dashboard backend sends a campaign message to the CA, the CA drafts and validates `leadgen.yaml`, the dashboard user reviews, edits and approves, then the CA publishes the approved version for the MA to pick up (B11).

Evidence level: every finding below comes from reading the code on branch `feature/form-detection` (file and line references given). Nothing has been run or tested. Items are sorted into **A: fix what exists**, **B: add new capability**, **C: decide, no code yet**.

Priority: **P0** = CSA cannot be built reliably without it, **P1** = needed for the main use cases, **P2** = improvement.

---

## A. Fixes to existing behavior

### A1 (P0). Config loading accepts anything — no validation

- **Where:** [agent/leadgen/config.py](../agent/leadgen/config.py) `load()`.
- **Problem:** `yaml.safe_load`, then `raw.get("search") or {}` for each section. Unknown top-level keys, unknown keys inside a section, wrong types and out-of-range values are all accepted. A typo (`min_qualfy: 70`) silently falls back to the default (`cfg.q("min_qualify", 50)`). A string where a number is expected fails later, mid-run, at an `int(...)` call.
- **Why it matters for the CSA:** its final check ("the MA loader accepts the file") would pass for broken files, so the check proves nothing.
- **Fix:**
  1. Add a Pydantic model for the whole file (`extra="forbid"` at every level, typed fields, ranges: e.g. `min_confidence` 0–1, `min_qualify`/`min_draft` 0–100, `queries_per_run` ≥ 1).
  2. Call it from `load()`; fail at startup with the key path and reason.
  3. Keep one escape hatch only if needed: a `--lenient` flag for old files during migration, off by default.
  4. Keep `LeadgenConfig` as the runtime object so downstream code does not change.
- **Done when:** a file with an unknown or misspelled key, a wrong type, or an out-of-range value makes `python -m agent leads run` exit before any browser or model work, and the existing `config/leadgen.yaml` still loads.

### A2 (P0). No config version

- **Where:** `config/leadgen.yaml` has no version key.
- **Fix:** add `config_version: "1.0"`. The loader (A1) rejects missing/unknown major versions with a clear message. Document the rule: new optional key = minor; rename, remove or change meaning = major.
- **Done when:** loader refuses `config_version: "2.0"` and the CSA can record which contract version it targeted.

### A3 (P0). Same setting, different defaults in different files

- **Where:** `accept_service_companies`:
  - `True`: [qualify.py:161](../agent/leadgen/qualify.py#L161) and [pipeline.py:180](../agent/leadgen/pipeline.py#L180)
  - `False`: [contacts.py:176](../agent/leadgen/contacts.py#L176), [strategy.py:206](../agent/leadgen/strategy.py#L206), and `config/leadgen.yaml:221`
- **Problem:** with the key absent from the YAML, the search strategy treats service companies as rejected while qualification and the pipeline treat them as accepted. The behavior depends on whether the key is present.
- **Fix:** the default lives in one place (the schema from A1, default `false` to match the file). Replace all `cfg.q("accept_service_companies", <literal>)` calls by a typed attribute (`cfg.accept_service_companies`). Same treatment for the other keys read with inline literal defaults: `accept_hiring_signals`, `min_qualify`, `min_draft`, `min_confidence`, `max_age_days`, `min_relevance_terms`, `max_new_leads_per_run`.
- **Done when:** no `cfg.q(` / `cfg.limit(` call carries a default literal; a test asserts an empty `qualification:` section yields the same behavior in every module.

### A4 (P0). Defaults scattered across code as `.get("key", literal)`

- **Where:** [pipeline.py](../agent/leadgen/pipeline.py) (`results_per_query` 10, `pages_per_query` 4, `feed_items` 40, `max_attempts` 3 at lines ~132/198/231/269/479), [strategy.py](../agent/leadgen/strategy.py) (`queries_per_run` 12, `repeat_after_days` 14, vocabulary fallbacks such as `["embedded device"]`), [browser/__init__.py](../agent/leadgen/browser/__init__.py) (`max_pages_per_run` 60), [contacts.py](../agent/leadgen/contacts.py) (`max_pages` 4).
- **Problem:** the real defaults differ from the values in the file (e.g. `queries_per_run` 12 in code vs 16 in the file; `max_pages_per_run` 60 vs 100; `pages_per_query` 4 vs 6). The CSA cannot document "the default" because there are several.
- **Fix:** covered by A1 and A3: defaults are declared once in the schema; code reads typed attributes. Hard-coded vocabulary fallbacks (`["embedded device"]`, `["embedded Linux"]`, `["RFQ"]`) either move into the schema or become validation errors ("search.products must not be empty").

### A5 (P1). Dead configuration key

- **Where:** `limits.host_delay_seconds` in `config/leadgen.yaml`; a search of `agent/**/*.py` finds no code that reads it.
- **Problem:** a setting that looks like politeness pacing but does nothing. The CSA would expose and validate a lever that has no effect, and a user could believe a lower or higher value changes behavior.
- **Fix:** either wire it into the HTTP/browser fetch path (if per-host pacing exists elsewhere, point it at this key) or delete it from the file and schema. Also check `contacts.link_labels`, `desktop_engines` and every vocabulary list for actual consumers when writing the contract table (see B-prereq below).

### A6 (P1). Inconsistent invariant only stated in a comment

- **Where:** `config/leadgen.yaml` comment: `max_pages_per_run` must be ≥ `queries_per_run × pages_per_query` or "late queries are starved".
- **Fix:** enforce as a schema rule in A1 (error, or a warning with explicit override). Also: `min_draft ≥ min_qualify`, and `results_per_query ≥ pages_per_query`. These are the business rules the CSA's validation level 4 will rely on; they should be authoritative in the MA, not duplicated in the CSA.

### A7 (P1). Project file loader and main loader apply different rules

- **Where:** [service.py `load_project`](../agent/leadgen/service.py#L40) rejects unknown keys (allowed: `name, industries, products, technologies, hardware, engineering_requirements`); the main loader (A1) does not. `hardware` is silently merged into `technologies` in [strategy.py `ProjectContext.from_dict`](../agent/leadgen/strategy.py#L77).
- **Fix:** move `ProjectContext` validation into the same schema package; accept `project_context` inside `leadgen.yaml` through the same strict model (today `cfg.raw["project_context"]` is read raw in `strategy.next_queries`, so an inline `project_context` with a typo is ignored and an inline unknown key is not rejected). Decide whether `hardware` stays an alias; if yes, document it.

### A8 (P1). Vocabulary rules exist only as comments

- **Where:** comments in `config/leadgen.yaml` ("each phrase ends where a technology/product fits", "19 phrases: not a multiple of 3 or 5", "no 'Android-based' here"), and `strategy.check_customer_query()` / `SUPPLIER_WORDS`.
- **Problem:** the rules a generated or edited vocabulary must satisfy are not machine-checkable except `check_customer_query` at query time (which raises `RequirementDriftError` mid-run).
- **Fix:** at load time, run `check_customer_query` over a sample (or all) generated queries and fail fast; add schema checks for list sizes/duplicates; for the coprime-stride constraint, either test it or remove the fragility (the stride logic in `strategy.intents` could use a permutation that does not depend on list-length coprimality). Until then, the CSA must not generate new phrase lists (it selects from the base only).

### A9 (P2). `cfg.raw` is a mutable, untyped escape hatch

- **Where:** [service.py:68-72](../agent/leadgen/service.py#L68) mutates `cfg.limits[...]` and `cfg.raw["project_context"]` on a `frozen=True` dataclass (mutating the inner dicts).
- **Fix:** after A1 make config immutable and produce an updated copy (`dataclasses.replace`) for CLI overrides (`--max-leads`, `--project`), so the effective config can be logged once and written next to the run report.

---

## B. New capability to add

### B1 (P0). Contract package and conformance tests

- **What:** a small importable module (e.g. `agent/leadgen/contract/`) containing: the Pydantic model (A1), the version (A2), the default values, field metadata (type, range, description, "campaign-owned" vs "base/infrastructure"), and controlled vocabularies.
- **Why:** the CSA imports exactly this, so there is one definition. Without it two repos describe the file independently and drift.
- **Tests:** `config/leadgen.yaml` and each file in a new `tests/fixtures/leadgen/` (valid and deliberately invalid) are checked in MA CI; the existing [leadgen_fakes.py](../agent/tests/leadgen_fakes.py) already loads `config/leadgen.yaml`, which becomes the first golden file.
- **Prerequisite work:** produce the contract table by tracing every consumer of every key (type, range, default, readers). Draft status from this review:

| Key | Read by | Notes |
|---|---|---|
| `search.queries_per_run`, `repeat_after_days` | strategy.py | code defaults 12/14 vs file 16/14 |
| `search.results_per_query`, `pages_per_query`, `use_job_feeds`, `feed_items`, `query_suffix` | pipeline.py | |
| `search.capabilities`, `products`, `industries`, `platforms`, `builder_phrases`, `need_phrases`, `rfp_words` | strategy.py `_vocab` | `industries` and `products` are replaced by `project_context` values when set |
| `qualification.*` (7 keys) | qualify.py, pipeline.py, contacts.py, strategy.py | A3 |
| `exclude_terms/companies/countries/tlds` | qualify.py, pipeline.py | |
| `contacts.max_pages`, `max_attempts`, `link_labels` | contacts.py, pipeline.py | |
| `limits.max_new_leads_per_run` | pipeline.py | |
| `limits.max_pages_per_run`, `max_pages_per_domain`, `engine_rest_minutes` | browser/__init__.py | |
| `limits.host_delay_seconds` | **nothing found** | A5 |
| `desktop_engines` | browser/__init__.py, config.py | |
| `project_context` | strategy.py, service.py | A7 |

### B2 (P0). Campaign overlay merged onto the base config

- **Today:** one local file, selected by `LEADGEN_CONFIG` (default `config/leadgen.yaml`) [config.py:34]; `--project` is the only overlay and accepts six keys.
- **Add:** an *overlay* concept: a validated set of campaign-owned keys (listed in B1) merged over the maintained base config. Keeps the ~400-line tuned vocabulary and exclusion lists under human control and lets the Campaign Agent (CA) emit a small document. Source of the overlay is B11 (database) in production and `--campaign <file>` for local and dry-run use.
- **Also add:** write the effective merged config (or its hash plus campaign id) next to each run report (`out/leadgen/run-*.md`, [report.py](../agent/leadgen/report.py)) so a run can always be traced to the exact campaign that produced it.
- **Superseded alternative:** "CA writes a file and the operator sets `LEADGEN_CONFIG`" no longer works, because the MA runs on an office machine behind NAT and cannot be reached from where the CA runs (see B11).

### B3 (P1). Geography include list

- **Need:** "US companies only". Today only `exclude_countries` and `exclude_tlds` exist [config.py, `intent.country_excluded`]; including a country means listing every other country.
- **Add:** `targeting.include_countries: [US]` (optional; empty = no restriction). Apply in `intent.country_excluded` / `qualify.py:198` / `pipeline.py:345` using the location read from the page and the site's TLD. Decide semantics for unknown location (reject, keep, or send to manual check). Start with "unknown location = keep, lower score", then tune.
- **Test:** a lead located in the US passes, Germany is dropped, unknown location behaves as specified.

### B4 (P1). Company size

- **Need:** "500–5,000 employees".
- **Today:** no employee data is extracted. [models.py](../agent/leadgen/models.py) has no such field and the page-reading prompt ([lead_assess.txt](../agent/prompts/lead_assess.txt)) does not ask for it [verify prompt].
- **Add (in order):** (1) extend the assessment output with `employee_count_estimate` + `evidence`; (2) store it on the lead; (3) add `targeting.employee_range: {min, max}` and a scoring/qualification rule; (4) show it in the Slack approval message. Employee size is often not on a company's own site, so decide up front whether unknown = pass with a flag. Without steps 1–2 the key would be accepted and ignored, which is exactly the failure A1 is meant to prevent.

### B5 (P1). Decision-maker role preference

- **Need:** "find appropriate decision-makers".
- **Today:** a hard-coded role ranking in [contacts.py `ROLE_PRIORITY`](../agent/leadgen/contacts.py#L28) (CTO first, then VP Engineering, ... procurement last) plus a mailbox ranking. No configuration.
- **Add:** `contacts.preferred_roles` as a list of names from a controlled vocabulary mapping to the existing patterns (e.g. `cto`, `vp_engineering`, `engineering_director`, `engineering_manager`, `founder`, `procurement`), used to reorder or restrict `role_rank`. Do not accept free-text regexes from the file (untrusted input compiled into a regex is a ReDoS and injection risk).

### B6 (P2). Campaign identity and traceability

- **Add:** `campaign: {id, name, description, created_by, contract_version}` stored with each run and each lead (`companies` table column or run record), so leads can be reported "per campaign" and a config change is auditable. Today the only identity is `project_context.name`, which is optional free text.

### B7 (P2). Objective

- **Today:** one objective (find buyers of embedded engineering and draft outreach), fixed in code (`strategy.TARGET`).
- **Add only if wanted:** `campaign.objective` as an enum. Keep a single value `customer_discovery`; additional values require real pipeline support. The point of adding the key now is so the CSA can reject other objectives explicitly instead of guessing.

### B8 (P2). Approval stays code-enforced, but make it visible

- **Today:** human approval is enforced by the pipeline/sender: [sender.py](../agent/leadgen/sender.py) sends only emails with a recorded approval, and sending is off by default (`make_transport`). It is not a config switch, which is correct.
- **Add:** nothing configurable. Add a read-only line in the effective-config output (B2) stating approval is required, so the CSA and users can show it truthfully. **Do not** add an `approval.required: false` option as part of this work.

### B9 (P2). Safe-bounds for externally supplied limits

- **Add:** schema maxima for the settings that affect third-party sites and costs: `queries_per_run`, `pages_per_query`, `max_pages_per_run`, `max_pages_per_domain`, `max_new_leads_per_run`, plus a **minimum** per-host delay once A5 is resolved. A generated or hand-edited file then cannot request unbounded crawling.

### B11 (P0). Publish/pickup channel: active campaign in the database

Why: the MA runs on the office machine behind NAT, nothing calls in, and it already works by polling Supabase (`pipeline_control`, `agent/control.py`, [docs/pipeline-api.md](pipeline-api.md)). So the CA cannot "send" a campaign to the MA; it must publish it where the MA already looks.

- **Migration** (new file in `supabase/migrations/`, same style as `0012_pipeline_api_role.sql`):
  - `campaign_versions`: immutable rows (campaign id, version, overlay yaml, sha256, contract version, approved_by, approved_at).
  - `campaign_active`: pointer to the active version and its sha256.
  - A database role for the CA's Publisher with insert on `campaign_versions` and update of `campaign_active` through a function, and nothing else. No access to leads, emails, Slack tables or the MA's other state. The MA's existing API role stays as it is.
- **MA pickup:** at the start of each discovery run (`service.run_leads()` already loads config fresh per run [verified]), read `campaign_active`, load the version, validate with the strict schema (A1), check the sha256, merge over the base (B2), use it for the whole pass. Switch only at a pass boundary, never mid-pass.
- **Failure behavior:** if the published version is invalid, hash-mismatched, or a newer contract version, keep the last good campaign (or the base config if none), record the error in `last_pass`, and report it in status. Never run a half-applied campaign.
- **No campaign yet:** behave exactly as today (base config), so existing deployments are unaffected.
- **Done when:** publishing a valid version changes the queries printed by `python -m agent leads queries` at the next pass; publishing an invalid one changes nothing and shows an error; the MA being offline during publish is harmless and applies on return.

### B12 (P1). Report the active campaign in pipeline status

- **Add** to the status object served by the pipeline API (`current_campaign: {id, version, sha256, applied_at, error?}`), written by `agent/control.py` with the existing heartbeat, and mirrored in `docs/pipeline-api.md`.
- **Why:** the dashboard must show which campaign is actually running and whether the latest approved version has been applied or rejected, instead of assuming.

### B13 (P1). Starting the pipeline from the CA (optional)

- The CA may request a start after approval by calling the same `request_pipeline` function the dashboard's Start button uses [verified function in migration 0012]. Needs: grant the Publisher role execute on that one function only, and record `requested_by` as the dashboard user. No new control path. Whether approval should start the pipeline at all is decision C6.

### B10 (P2). Channel and content preferences — explicitly not in scope for the YAML

- Outreach is email only; wording comes from `agent/prompts/outreach_*.txt` and `config/outreach_services.txt`; follow-ups and replies have their own tasks. These are **not** controlled by `leadgen.yaml` and the CSA will not set them. Record this decision rather than adding keys. If product wants per-campaign tone or services text, that is a separate design (a campaign-scoped prompt fragment with allow-listed variables).

---

## C. Decisions needed from the product owner (no code yet)

1. **Overlay or full file** (B2). Recommendation: overlay.
2. **Which of B3 (US-only), B4 (employee size), B5 (roles) are real requirements for the first CSA release.** Each is a feature in the MA, not a config tweak. If not scheduled, the CSA reports them as "not applied" and the example prompt in the brief is only partially satisfied.
3. **Service companies default** (A3): confirm `false` is intended (it matches the file and the comment that such firms are competitors).
4. **Ownership and packaging of the contract** (B1): in this repo, imported by the CSA as a dependency or vendored copy. Recommendation: published from this repo with a version tag.
5. **Strictness policy for old files** (A1): hard fail, or a deprecation window with warnings.
6. **Does approving a campaign also start the pipeline?** (B13) Recommendation: publish only; an optional flag calls the existing Start path.
7. **When does a newly published campaign take effect?** (B11) Recommendation: next pass boundary.
8. **Where should the CA's tables live?** In the MA's Supabase project and migrations (recommended; the MA reads them), or a separate database with a replicated pointer.

---

## Suggested order of work

| Step | Items | Result |
|---|---|---|
| 1 | A3, A4, A5 inventory; B1 contract table | Know every key, its reader and its real default |
| 2 | A1, A2, A6, A7, A9 | Strict versioned loader; typos fail; defaults in one place |
| 3 | B1 package + conformance tests; B9 bounds | CSA can import and test against the contract |
| 4 | B2 overlay + effective-config output + run traceability (B6) | A campaign document has a defined, auditable path into a run |
| 5 | B11 migration, role and pickup; B12 status; B13 if chosen | An approved campaign reaches the MA through the database and is visible in status |
| 6 | A8 vocabulary load-time checks | Safe to let the CA select from, and later extend, vocabularies |
| 7 | B3, B5, then B4 (as decided in C2) | Campaign fields beyond the current ones become real |

Steps 1–5 are the prerequisites for starting Campaign Agent development. Steps 5–6 can follow CSA MVP; until they ship the CSA reports those requests as "not applied".

## What I did not check

- Prompt content (`lead_assess.txt`) for employee-size extraction, and how `scoring.py` weights signals (it reads no config keys in my search, so score weights are code constants; not examined further).
- The `services/` directory (`lead-service`, `outreach-service`, `llm-service`) and `libs/agentkit`: whether any of them reads `leadgen.yaml` or duplicates its fields.
- Postgres repository columns that would need a migration for B4/B6.
