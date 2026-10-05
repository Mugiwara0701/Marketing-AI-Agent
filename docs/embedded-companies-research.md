# Task: embedded engineering companies and their openings (a research list)

## Goal

Build a spreadsheet of companies worldwide that do embedded engineering, with their official contact emails and
any current relevant job openings. Every company goes in the sheet, even with no openings right now.

This is a **research list, not a lead list**. It is kept apart from the lead pipeline on purpose:

| | Lead pipeline (`python -m agent run --desktop`) | This research list |
|---|---|---|
| Who | companies that make devices or ask for a project, which we pitch | companies that do embedded engineering themselves (consultancies, OTA platforms, service firms, SoM vendors) |
| Job ads | rejected at four points (see `deploy/desktop-lead-search.md`) | collected, as the "Openings" sheet |
| Countries | Canada, the UK and Germany are never pitched | global, see "Decisions" below |
| Where results go | database tables `companies`, `contacts`, `emails` (drafts, Slack approval) | files under `out/embedded_companies/` only |

**Never write these companies into the `companies` table.** If they land there, the contact retry revisits them and
the drafting step writes proposal emails to them.

## Target domains

A company qualifies if it does real work in at least one of these. Tags in brackets are the short focus tags used
in the sheet.

- Embedded software and firmware engineering `[firmware]`
- AOSP platform development, Android Automotive `[aosp]` `[aaos]`
- Yocto Project, OpenEmbedded, Buildroot, embedded Linux `[yocto]` `[embedded-linux]`
- OTA software update systems `[ota]`
- Embedded and device security: secure boot, TrustZone/TEE, hardening, CRA compliance `[security]`
- BSP development, board bring-up `[bsp]`
- HAL integration, device drivers, Linux kernel `[hal]` `[kernel]`

These are the same areas as `keywords:` at the top of `config/sources.yaml` (plus OTA and security), so the
whole-word matcher `agent.sources.matches` can pre-tag a page before the model is asked.

## Scope

- Geography: global (Europe, North America, India, Asia, Latin America...).
- At least 50 companies, a mix of:
  - Specialist embedded consultancies, e.g. Bootlin, BayLibre, Collabora, Pengutronix, Linutronix, Konsulko,
    Amarula Solutions, DENX, Kynetics, O.S. Systems, Witekio, Timesys.
  - OTA and device-platform companies, e.g. Mender/Northern.tech, Foundries.io, Toradex, Esper, emteria, Sibros,
    Excelfore, Airbiquity.
  - Engineering service firms, e.g. VVDN, Sibrain, Silicon Signals, KritiKal, eInfochips, Mistral Solutions,
    Ignitarium, Tata Elxsi, KPIT, Sasken, L&T Technology Services, Thundersoft.
  - SoM and hardware vendors with strong BSP teams, e.g. Variscite, CompuLab, PHYTEC, e-con Systems.
- Find more through the Yocto Project member and participant lists, the Toradex, NXP, TI and Qualcomm partner
  directories, the Linux Foundation member lists, and searches such as "AOSP BSP services company",
  "Yocto consulting", "embedded Linux consultancy".
- The examples are starting points only. Check that each one still exists and still fits.

Several names on these lists are also in `desktop.exclude_companies` (Timesys, Mender, Linaro...) because the
lead search must not pitch them. That list does not apply here: this task reads `config/sources.yaml` only for
`keywords`, never for `exclude_*`.

## For each company, collect

1. Company name
2. Official website (homepage URL)
3. Headquarters country, and other office countries if easy to find
4. Focus areas: the tags above
5. Contact email(s): only addresses published on the company's own website. Prefer a careers/jobs/HR address,
   then a general info/contact address. Record the page it was found on.
6. Careers page URL
7. Relevant openings: for each one, the job title, location (including remote/hybrid), experience required (if
   stated) and the direct job link. Relevant means embedded, firmware, kernel, BSP, AOSP/Android platform, Yocto,
   OTA, embedded security, HAL. If none: "No relevant opening found" in the opening columns.
8. Date checked (today)
9. Notes, e.g. "careers email not listed, only contact form", "acquired by X"

## Rules

These match rules the project already enforces in code. Reuse that code instead of writing new checks.

- **Never invent or guess an email address** (no `firstname@company.com` patterns). Use
  `agent.contacts.emails_on_domain(text, domain)`: it returns only addresses written literally on the page and on
  the company's own domain. If no address is published, write `Not listed – contact form: <URL>`.
- **Company-level addresses only, no personal emails.** `agent.contacts` already ranks role mailboxes
  (`_ROLE_ORDER`); for this task put `careers`, `jobs`, `hr`, `recruit` first, then `info`, `contact`, `hello`.
- **Every email, opening and URL must come from a page actually opened.** Keep the source URL in its own column.
- **Careers pages first.** The fallback in the original task (LinkedIn, Indeed, Naukri) is not allowed in this
  project: `agent.web.blocked()` refuses LinkedIn, Indeed, Glassdoor, Naukri, Monster and ZipRecruiter because
  their terms forbid scraping. When a company's openings are only on such a portal, write
  `Openings only on <portal> – check manually` in Notes and move on. The open job boards in `agent/portals.py`
  may be used as a fallback, marked as the source.
- Skip openings that are clearly expired or closed.
- **Do not loop on a site that blocks fetching.** Use `agent.gui.leadscore.block_reason` (CAPTCHA, Cloudflare,
  access denied, login wall): mark the company `Could not access – check manually` and move on.
- Deduplicate companies, including subsidiaries and acquired brands. Compare on the registrable domain
  (`agent.web.registrable_domain`) and the normalised name.
- Respect `robots.txt` and the polite fetch rules in `agent/web.py`.

## How to fetch pages in this project

- **Plain HTTP first:** `agent/web.py` (fetch with robots check, `html_to_text`). Fast, and enough for most
  company and careers pages.
- **Visible Chrome only when needed:** `agent.gui.desktop.Desktop` (`navigate`, `read_page`, `click_link`,
  `dismiss_popups`) for pages that render with JavaScript, for example careers pages on an applicant system.
  On the office machine without a GPU this is slow (see `deploy/remote-control.md`, section F).
- **The model only classifies.** Use the dev text model to tag focus areas and to decide whether an opening is
  relevant. Emails and links are extracted by code, never by the model.

## Workflow

All files go in `out/embedded_companies/` (inside `out/`, which is git-ignored).

1. Build the candidate list first and save it to `companies_candidates.csv` (name, website, why it is a
   candidate, where it was found).
2. Research the companies one by one. After each company, append its rows to `companies_progress.csv` and
   `openings_progress.csv`, so work is never lost. On a restart, skip companies already in `companies_progress.csv`
   (the same resume idea as `out/desktop/state.json`).
3. When all are done, build `embedded_companies_and_openings.xlsx` with three sheets:
   - **Companies:** one row per company: name, website, country, focus areas, contact email, email source URL,
     careers page, number of relevant openings, date checked, notes.
   - **Openings:** one row per job: company, job title, location, remote?, experience, job link, source, date
     checked.
   - **Summary:** total companies, companies with openings, total openings, counts by country and by focus area.
   - Format: bold frozen header row, auto-filter on, sensible column widths, clickable hyperlinks.
   - Needs `openpyxl` (in `requirements.txt`; install with `uv pip install -r requirements.txt`).
4. Verification pass at the end:
   - Re-open a random 20% of emails and job links and check them against their source pages.
   - Confirm no row has an email without a source URL.
   - List every company whose data could not be verified.

## How to run it

```bash
python -m agent embedded-list                 # candidates -> research -> verification -> workbook (resumes)
python -m agent embedded-list --no-discover   # only the seeds in config/embedded_companies.yaml
python -m agent embedded-list --limit 10      # at most 10 more companies in this run
python -m agent embedded-list --report-only   # verify again and rebuild the workbook from the progress files
python -m agent embedded-list --fresh         # start over
```

- Code: `agent/embedded_list.py`. Seeds, discovery searches, directory pages, skipped domains: `config/embedded_companies.yaml`.
- It needs no database, no Slack and no model: it runs on the laptop. `SEARXNG_URL` makes discovery searches reliable.
- It is resumable: every company is appended to `companies_progress.csv` as soon as it is done; a new run skips those.
- Countries: global, as the task says. Headquarters is a best guess from the contact / about / imprint pages (an
  address counts more than a mention) and the site ending; check it for companies with offices in many countries.
- Some sites refuse automated visitors (HTTP 403, e.g. Toradex, Variscite): they are listed as
  `Could not access - check manually`.

## Final report

- Path to the Excel file
- How many companies were found, how many have openings, total openings
- Companies that could not be accessed or verified
