# Desktop lead search (visible Chrome on Xubuntu)

`python -m agent run --desktop` finds **companies that make their own electronic devices** (RFID / NFC / smart-card readers,
biometric and access-control terminals, POS and payment terminals, rugged handhelds, kiosks, set-top boxes, IoT gateways,
trackers and similar) anywhere in the world **except Canada, the United Kingdom and Germany**: companies we can pitch AOSP,
BSP, firmware and embedded Linux engineering to. It uses a real, visible Chrome window the way a person does: Ctrl+L, type a
search, read the results, click a result, scroll, read, judge. It stores the qualified leads in the existing database and drafts
(never sends) a proposal email for each lead that has a public contact.

## How it works

| Step | Mechanism |
|---|---|
| Chrome | started as a normal app with its own profile (`.chrome-desktop-profile/`), no DevTools port, no Playwright |
| Mouse / keyboard | `xdotool`, through the audited action set in `sandbox/executor.py` (`agent/gui/desktop.py`) |
| Reading a page | select all + copy (clipboard), plus a screenshot saved to `out/desktop/<run>/`; OCR (`tesseract`) when a page has no selectable text |
| Clicking a result | OCR finds the title on screen and the mouse clicks it; fallbacks: Ctrl+F + Return, then the vision model (`gui.step`) |
| Picking results | text LLM task `lead.search` reads the results text, returns promising hits and new queries |
| Judging a page | job-post filter, keyword filter, `lead.qualify`, then a 0-100 score (`agent/gui/leadscore.py`) |
| Contact | the company's own site and `/contact`, `/about`... opened in the same window; the existing guard applies (email literally on the page, on the company's domain) |
| Dedup | canonical URL (signal hash), company domain, normalised company name; state in `out/desktop/state.json` |
| Blocked | CAPTCHA, Cloudflare check, access denied, rate limit, login wall: logged, the engine or site rests 30 min, the run goes on. Nothing tries to get past them |
| Recovery | Chrome gone or failing: the window is closed and reopened, the search is retried later; 4 failures in a row end the run |
| Email | `mailer.lock_sending()` is called first: sending is impossible for the process, whatever `EMAIL_SENDING_ENABLED` says |

Leads are rows in `companies` (`source = 'desktop'`), the full lead record (evidence, score parts, opportunity type,
AOSP / Android / embedded relevance, query, engine) is in `lead_signals.extracted`, contacts in `contacts`, drafts in `emails`
with status `drafted`.

## Set-up on the Xubuntu machine

1. `bash deploy/bootstrap-ubuntu.sh` installs `xdotool xclip imagemagick tesseract-ocr` and Google Chrome (or install them by hand).
2. Log in to the **Xubuntu (Xorg)** session. Wayland is refused. Turn off screen lock and blanking
   (Settings > Power Manager, Screensaver) and enable auto-login, so the window stays usable unattended.
3. In `.env` set `LEADS_MODE=desktop` (or always pass `--desktop`). Optional settings below.
4. `python -m agent desktop-check` lists what is missing. Then `python -m agent migrate` if not done.

## Run it

```bash
python -m agent run --desktop --force      # one bounded run: up to AGENT_MAX_MINUTES (120), stops at the daily target
python -m agent leads-today                # what was stored, with score, contact and draft count
python -m agent review                     # the email drafts (nothing is sent)
touch /tmp/gui-agent.stop                  # kill switch: the run ends after the current step (delete the file afterwards)
```

The run prints a JSON summary: `searches`, `pages_inspected`, `qualified`, `stored`, `drafts`, `duplicates`,
`blocked_sources`, `rejected_*`, `browser_recoveries`, `emails_sent` (always 0), `run_seconds`.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LEADS_MODE` | `api` | `desktop` switches the lead step to the visible Chrome search |
| `DAILY_LEAD_TARGET` / `MAX_DAILY_LEADS` | 5 / 6 | stop at the target; never store more than the cap (`DESKTOP_STOP_AT_TARGET=false` searches on up to the cap) |
| `DESKTOP_MIN_SCORE` | 60 | minimum qualification score (0-100) to store a lead |
| `DESKTOP_HITS_PER_QUERY` | 4 | results opened per search |
| `DESKTOP_MIN_HIT_SCORE` | 0.4 | minimum model score for a result to be opened |
| `DESKTOP_SEARCH_GAP` | 8 | seconds between searches |
| `DESKTOP_PAGE_WAIT` | 5 | seconds a page is given to load |
| `DESKTOP_SAVE_SHOTS` | 1 | save screenshots under `out/desktop/` |
| `CHROME_BIN`, `DESKTOP_CHROME_PROFILE`, `DESKTOP_STATE_FILE`, `DISPLAY` | auto | overrides |

Queries, search engines and platforms (`site:` filters) are in `config/sources.yaml` under `desktop:`.
Models: `lead.search`, `lead.qualify`, `lead.extract_contact`, `outreach.draft` use the dev text model; `gui.step` (vision) is only a fallback for clicking.
The scheduled run (`deploy/aosp-agent.service`) sets `DISPLAY=:0` and `LEADS_MODE=desktop`.

## What counts as a lead, and what never does

- **Device maker:** the page must show at least `DESKTOP_MIN_KEYWORDS` (2) different words from `keywords` (AOSP, BSP...) plus
  `desktop.product_keywords` (rfid, reader, biometric, terminal, manufacturer...), and the model (prompt
  `agent/prompts/device/lead_qualify.txt`) must call it a device maker with confidence of at least `DESKTOP_MIN_CONFIDENCE` (0.7).
  The site does not need to mention Android or Linux.
- **Countries:** `desktop.exclude_countries` and `desktop.exclude_tlds` in `config/sources.yaml`. A company is dropped when its site
  ends in `.ca`, `.uk`, `.co.uk` or `.de`, or when the location the model reads from the page names one of those countries. A
  `.com` company whose page shows no country is not dropped: check the location in the stored lead.
- **Never:** `desktop.exclude_terms` (robotaxi, autonomous driving...) and `desktop.exclude_companies` (autonomous-driving firms, big brands).
- **Queries:** the model's follow-up queries are kept only if they name a kind of device and a "who" (manufacturer, supplier...)
  (`query_topic_terms`, `query_actor_terms`) and no excluded country or topic.
