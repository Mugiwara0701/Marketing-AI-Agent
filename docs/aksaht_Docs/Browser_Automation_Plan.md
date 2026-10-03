# Browser Automation Plan (Playwright)

Date: 2026-10-02 · Author: Akshat

## 1. Problem

The agent finds companies that need AOSP / embedded work, then looks for a **public business email** on the company's
own site (`agent/contacts.py`). Two things go wrong today:

1. **Many sites have no email, only a contact form.** The agent finds nothing and saves the company as "qualified, no
   contact". The lead is wasted.
2. **Some sites need JavaScript to show any content.** The plain HTTP fetch (`web.fetch`) gets an almost empty page,
   so contact details and forms are never seen.

A separate bug was fixed on 2026-10-02: the local thinking model (`agent-dev`, Qwen3.5) used all 300 `max_tokens` on
hidden reasoning, so contact extraction and qualification returned empty JSON. Both tasks now send
`reasoning_effort="none"`.

## 2. Goal

Get more usable leads from the same discovery, without sending anything unreviewed:

- Read JavaScript-rendered company sites.
- Detect contact forms and remember them as the lead's contact route.
- Let a human send a form message quickly, with the drafted text already filled in.

## 3. Decision: what each tool does

| Piece | Job | Stays? |
|---|---|---|
| SearXNG (self-hosted) | Discovery: finds companies and job posts on the open web | Yes. Playwright cannot replace it |
| Playwright (new) | Reading: renders JS sites, finds contact pages and forms | Added |
| LLM | Judgment: qualify leads, extract the contact, draft outreach | Yes |

Why not drive Google or Bing with Playwright instead of SearXNG: they detect bots (CAPTCHAs, IP blocks), automated
scraping breaks their terms, result HTML changes often, and it is slower. SearXNG is free, returns JSON, and spreads
queries over several engines.

Playwright is free and open source (Apache 2.0). Cost is only disk (about 150-300 MB for Chromium) and RAM while a
browser runs.

## 4. Flow after the change

1. SearXNG and the job feeds produce candidate companies.
2. The LLM qualifies them (`qualify.py`).
3. Company pages are fetched with plain HTTP. If a page answers with almost no text, it is re-fetched in headless
   Chromium.
4. The LLM extracts a contact (`contact.py`); the code guard checks the address is on the page and on the company's
   domain. If there is no email but there is a contact form, the form URL is saved on the company.
5. The LLM drafts the outreach. A human reviews it before anything is sent.

## 5. Scope

### In scope

1. **Browser fetch fallback** (`web.fetch_rendered`, `web.fetch_smart`). Same safety rules as `fetch`: robots.txt,
   per-host delay, no private addresses, never the portals in `DO_NOT_SCRAPE`. Playwright is an optional import; if it is
   missing the agent behaves as before.
2. **Form detection** (`web.has_contact_form`). A contact form has a free-text message box, no password field, and is
   not a search box. The URL is stored in `companies.contact_form_url` (migration `0005_contact_form.sql`).
3. **Fill-and-pause** (`python -m agent fill-form <domain|id>`). Opens a visible Chrome window, fills name, email,
   company, subject and message from the drafted proposal, then waits. The person checks it, solves any CAPTCHA,
   clicks Send, and closes the window.

4. **Watchable lookup** (`python -m agent browse "<company or domain>"`, `agent/browse.py`). A visible Chromium
   searches DuckDuckGo for the company, opens its site and contact pages, and prints the contact or the form URL. It is
   one company at a time and slowed down so it can be followed. It is separate from the daily run, which stays headless
   and uses SearXNG. DuckDuckGo blocks headless browsers, so this only works with a visible window; if a CAPTCHA
   appears, solve it in the window.

### Out of scope (on purpose)

- **Auto-submitting forms.** Unreviewed sends cannot be undone and raise spam-law and site-terms problems
  (CAN-SPAM, GDPR/PECR).
- **Bypassing CAPTCHAs or bot checks**, including paid solving services.
- **Replacing SearXNG** with scraped search-engine pages.
- Scraping the portals already forbidden in `DO_NOT_SCRAPE` (LinkedIn, Indeed, Naukri and so on).

## 6. Files

| File | Change |
|---|---|
| `agent/web.py` | `fetch_rendered`, `fetch_smart`, `has_contact_form` |
| `agent/contacts.py` | uses `fetch_smart`; records `form_urls[domain]` when a form is seen |
| `agent/leads.py` | saves the form URL when no email is found (new leads and the retry pass) |
| `agent/store.py` | `save_company(form_url=...)`, `set_form_url`, `company_with_form` |
| `agent/formfill.py` | new: the fill-and-pause command |
| `agent/browse.py` | new: the watchable `browse` command |
| `agent/__main__.py` | new `fill-form` subcommand |
| `supabase/migrations/0005_contact_form.sql` | adds `companies.contact_form_url` |
| `requirements.txt` | adds `playwright` (optional) |
| `agent/tests/test_agent.py` | tests for form detection and field matching |

## 7. Status

- Code for items 1-3 is written. The existing tests pass (35) with the two new tests, and ruff is clean.
- **Not yet done:** installing Playwright and Chromium (`pip install playwright`, `playwright install chromium`),
  applying the migration (`python -m agent migrate`), and trying the browser fetch and `fill-form` on real sites.
- Not yet done: showing the form link in the dashboard.

## 8. How to know it works

- A JavaScript-only company site returns contact text through the fallback.
- A form-only company is saved with `contact_form_url` and appears for `fill-form`.
- `fill-form` opens Chrome, pre-fills the fields, and submits nothing.
- With Playwright uninstalled, `python -m agent run` still works as before.

## 9. Risks

- **Form variety:** field matching is by name, id, placeholder and label text, so unusual forms will be partly filled.
  The person fills the rest.
- **Memory:** headless Chromium plus the local LLM on a small machine. The browser is used only as a fallback.
- **Speed:** a browser fetch is slower than HTTP, so it runs only when the plain page is nearly empty.
