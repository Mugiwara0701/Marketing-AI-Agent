# Desktop lead search (visible Chrome on Xubuntu)

`python -m agent leads run --browser desktop` (or `python -m agent run --desktop`) runs the lead pipeline
(`agent/leadgen`, see the README) with research done in a real, visible Chrome window the way a person does it: Ctrl+L, type
a search, read the results, click a result, scroll, read. Only the browser layer differs from the HTTP backend; qualification,
contact discovery, drafting, Slack approval and the sending gate are the same code.

## How it works

| Step | Mechanism |
|---|---|
| Chrome | started as a normal app with its own profile (`.chrome-desktop-profile/`), no DevTools port, no Playwright |
| Mouse / keyboard | `xdotool`, through the audited action set in `sandbox/executor.py` (`agent/gui/desktop.py`) |
| Reading a page | select all + copy (clipboard), plus a screenshot saved to `out/desktop/<run>/`; OCR (`tesseract`) when a page has no selectable text |
| Search results | the desktop cannot see links, so the text LLM task `lead.search` lists the results (title, site, snippet); it does not judge them |
| Clicking a result | OCR finds the title on screen and the mouse clicks it; fallbacks: Ctrl+F + Return, then the vision model (`gui.step`) |
| Judging a page | `agent/leadgen/intent.py` rules (shops, distributors, docs, job boards...), then `lead.assess`, code checks and the 0-100 score |
| Contact | the company's home page, then the Contact / Team / About links clicked on screen (no URL is ever typed from a guess); an address must be literally on the company's own site |
| Dedup / resume | canonical URL, company domain and normalised name, stored in the database; queries already run are skipped |
| Blocked | CAPTCHA, Cloudflare check, access denied, rate limit, login wall: logged, the engine or site rests, the run goes on. Nothing tries to get past them |
| Recovery | Chrome gone or failing: the window is closed and reopened |
| Email | drafts go to Slack for approval; nothing is sent unless a person approved it and `EMAIL_SENDING_ENABLED=true` |

## Set-up on the Xubuntu machine

1. `bash deploy/bootstrap-ubuntu.sh` installs `xdotool xclip imagemagick tesseract-ocr` and Google Chrome (or install them by hand).
2. Log in to the **Xubuntu (Xorg)** session. Wayland is refused. Turn off screen lock and blanking
   (Settings > Power Manager, Screensaver) and enable auto-login, so the window stays usable unattended.
3. In `.env` set `BROWSER_BACKEND=desktop` (or always pass `--desktop`). Optional settings below.
4. `python -m agent desktop-check` lists what is missing. Then `python -m agent migrate` if not done.

## Run it

```bash
python -m agent leads run --browser desktop --minutes 90   # one bounded discovery run (or: python -m agent run --desktop --force)
python -m agent leads review                               # drafts waiting for approval, with score and contact
python -m agent leads show <email id>                      # one lead: evidence, score parts, contact, draft
touch /tmp/gui-agent.stop                                  # kill switch: the run ends after the current step
```

The run prints a JSON summary (searches, pages read, rejected, leads qualified, contacts found, emails drafted, approvals
requested, blocked sources, model errors) and writes a Markdown report to `out/leadgen/run-*.md`.

## Settings

Search vocabulary, thresholds, exclusions, page budgets and the desktop search engines are in `config/leadgen.yaml`
(`desktop_engines`). Desktop driver settings are environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `BROWSER_BACKEND` | `http` | `desktop` switches lead research to the visible Chrome on the desktop |
| `DESKTOP_PAGE_WAIT` | 5 | seconds a page is given to load |
| `DESKTOP_SAVE_SHOTS` | 1 | save screenshots under `out/desktop/` |
| `DESKTOP_VISION` / `DESKTOP_VISION_FIRST` / `DESKTOP_VISION_TIMEOUT` | 1 / 0 / 240 | vision model as fallback for clicking |
| `CHROME_BIN`, `DESKTOP_CHROME_PROFILE`, `DISPLAY` | auto | overrides |

Models: `lead.search` (results list), `lead.assess`, `lead.extract_contact`, `outreach.draft` use the text model; `gui.step`
(vision) is only a fallback for clicking. The scheduled run (`deploy/aosp-agent.service`) sets `DISPLAY=:0` and
`BROWSER_BACKEND=desktop`.
