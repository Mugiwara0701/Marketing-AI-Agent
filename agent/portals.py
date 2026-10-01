"""Portals from docs/job-portals.xlsx: more open job boards, key-based aggregators (optional), and the
public hiring-system feeds of named companies (Greenhouse, Lever, Ashby, Recruitee, Personio, Workable).

Postings are used to find COMPANIES that need AOSP / embedded work, not to republish them. Every signal
keeps its source URL. Per-portal rate limits from the spreadsheet are enforced across runs.
"""

import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from agentkit.config import env
from agentkit.log import get_logger

from . import settings
from .sources import Signal, matches, strip_html

log = get_logger("agent.portals")
_CALLS = Path(".cache/portal_calls.json")
_MAX_TEXT = 3000
HOUR = 3600

# Minimum seconds between runs that call a portal (from the spreadsheet's limits column).
# Remotive: max 4 calls/day and we make 2 per run -> at most twice a day. Jobicy: poll at most hourly.
MIN_INTERVAL = {
    "remotive": 6 * HOUR,
    "jobicy": HOUR,
    "himalayas": HOUR,
    "themuse": HOUR,
    "adzuna": HOUR,
    "jooble": HOUR,
}


def due(portal: str) -> bool:
    """True if this portal may be called now; records the call time when it is."""
    gap = MIN_INTERVAL.get(portal, 0)
    try:
        calls = json.loads(_CALLS.read_text()) if _CALLS.exists() else {}
    except (OSError, ValueError):
        calls = {}
    now = time.time()
    if gap and now - calls.get(portal, 0) < gap:
        log.info("portal skipped (rate limit)", extra={"ctx": {"portal": portal}})
        return False
    calls[portal] = now
    try:
        _CALLS.parent.mkdir(exist_ok=True)
        _CALLS.write_text(json.dumps(calls))
    except OSError:
        log.warning("could not record portal call time")
    return True


async def _get(url: str, **params) -> httpx.Response | None:
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.load().user_agent}, timeout=30, follow_redirects=True
        ) as c:
            r = await c.get(url, params=params)
    except httpx.HTTPError:
        log.warning("portal unreachable", extra={"ctx": {"host": httpx.URL(url).host}})
        return None
    if r.status_code != 200:
        log.warning(
            "portal error", extra={"ctx": {"host": httpx.URL(url).host, "status": r.status_code}}
        )
        return None
    return r


async def _json(url: str, **params):
    r = await _get(url, **params)
    try:
        return r.json() if r else None
    except ValueError:
        return None


def _sig(  # noqa: PLR0917
    source: str, url: str, title: str, company: str, location: str, body: str, domain: str = ""
) -> Signal:
    text = f"Company: {company}. Title: {title}. Location: {location}. {strip_html(body)}"[
        :_MAX_TEXT
    ]
    s = Signal("job_post", source, url, title, text, company)
    s.domain_hint = domain
    return s


# ------------------------------------------------------------------ open boards (no key)


async def himalayas(kws: list[str]) -> list[Signal]:
    if not due("himalayas"):
        return []
    out: dict[str, Signal] = {}
    for q in ("android platform", "embedded linux", "aosp"):
        d = await _json("https://himalayas.app/jobs/api/search", q=q, sort="recent")
        for j in (d or {}).get("jobs", []) if isinstance(d, dict) else []:
            body = f"{j.get('excerpt', '')} {j.get('description', '')}"
            if matches(f"{j.get('title', '')} {body}", kws):
                url = j.get("applicationLink") or j.get("guid") or ""
                out[url] = _sig("himalayas", url, j.get("title", ""), j.get("companyName", ""),
                                ", ".join(map(str, j.get("locationRestrictions") or [])), body)  # fmt: skip
    return list(out.values())


async def themuse(kws: list[str]) -> list[Signal]:
    if not due("themuse"):
        return []
    out = []
    params = {"api_key": env("THEMUSE_API_KEY")} if env("THEMUSE_API_KEY") else {}
    for page in range(3):
        d = await _json(
            "https://www.themuse.com/api/public/jobs",
            page=page,
            category="Software Engineering",
            **params,
        )
        for j in (d or {}).get("results", []) if isinstance(d, dict) else []:
            if matches(f"{j.get('name', '')} {j.get('contents', '')}", kws):
                out.append(_sig("themuse", (j.get("refs") or {}).get("landing_page", ""), j.get("name", ""),
                                (j.get("company") or {}).get("name", ""),
                                ", ".join(x.get("name", "") for x in j.get("locations", [])), j.get("contents", "")))  # fmt: skip
    return out


# ------------------------------------------------------------------ aggregators (free key, optional)


async def adzuna(kws: list[str]) -> list[Signal]:
    app_id, key = env("ADZUNA_APP_ID"), env("ADZUNA_APP_KEY")
    if not (app_id and key) or not due("adzuna"):
        return []
    out = []
    for country in (env("ADZUNA_COUNTRIES", "gb,us,de,in,nl") or "").split(","):
        d = await _json(f"https://api.adzuna.com/v1/api/jobs/{country.strip()}/search/1",
                        app_id=app_id, app_key=key, what_or="aosp bsp yocto android embedded linux",
                        results_per_page=50)  # fmt: skip
        for j in (d or {}).get("results", []) if isinstance(d, dict) else []:
            if matches(f"{j.get('title', '')} {j.get('description', '')}", kws):
                out.append(_sig("adzuna", j.get("redirect_url", ""), j.get("title", ""),
                                (j.get("company") or {}).get("display_name", ""),
                                (j.get("location") or {}).get("display_name", ""), j.get("description", "")))  # fmt: skip
    return out


async def jooble(kws: list[str]) -> list[Signal]:
    key = env("JOOBLE_API_KEY")
    if not key or not due("jooble"):
        return []
    out = []
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            for q in ("AOSP", "Android platform", "embedded linux BSP"):
                r = await c.post(f"https://jooble.org/api/{key}", json={"keywords": q})
                for j in r.json().get("jobs", []) if r.status_code == 200 else []:
                    if matches(f"{j.get('title', '')} {j.get('snippet', '')}", kws):
                        out.append(_sig("jooble", j.get("link", ""), j.get("title", ""), j.get("company", ""),
                                        j.get("location", ""), j.get("snippet", "")))  # fmt: skip
    except (httpx.HTTPError, ValueError):
        log.warning("jooble unavailable")
    return out


# ------------------------------------------------------------------ company hiring systems


async def _greenhouse(b: dict, kws: list[str]) -> list[Signal]:
    d = await _json(f"https://boards-api.greenhouse.io/v1/boards/{b['board']}/jobs", content="true")
    out = []
    for j in (d or {}).get("jobs", []) if isinstance(d, dict) else []:
        body = j.get("content", "")  # HTML-escaped by Greenhouse
        if matches(f"{j.get('title', '')} {strip_html(body)}", kws):
            out.append(_sig("greenhouse", j.get("absolute_url", ""), j.get("title", ""), b.get("name") or j.get("company_name", ""),
                            (j.get("location") or {}).get("name", ""), body, b.get("domain", "")))  # fmt: skip
    return out


async def _lever(b: dict, kws: list[str]) -> list[Signal]:
    d = await _json(f"https://api.lever.co/v0/postings/{b['board']}", mode="json", limit=500)
    out = []
    for j in d if isinstance(d, list) else []:
        body = j.get("descriptionPlain", "") + " " + j.get("additionalPlain", "")
        if matches(f"{j.get('text', '')} {body}", kws):
            out.append(_sig("lever", j.get("hostedUrl", ""), j.get("text", ""), b.get("name", b["board"]),
                            (j.get("categories") or {}).get("location", ""), body, b.get("domain", "")))  # fmt: skip
    return out


async def _ashby(b: dict, kws: list[str]) -> list[Signal]:
    d = await _json(f"https://api.ashbyhq.com/posting-api/job-board/{b['board']}")
    out = []
    for j in (d or {}).get("jobs", []) if isinstance(d, dict) else []:
        body = j.get("descriptionPlain", "")
        if matches(f"{j.get('title', '')} {body}", kws):
            out.append(_sig("ashby", j.get("jobUrl", ""), j.get("title", ""), b.get("name", b["board"]),
                            j.get("location", ""), body, b.get("domain", "")))  # fmt: skip
    return out


async def _recruitee(b: dict, kws: list[str]) -> list[Signal]:
    d = await _json(f"https://{b['board']}.recruitee.com/api/offers/")
    out = []
    for j in (d or {}).get("offers", []) if isinstance(d, dict) else []:
        if matches(f"{j.get('title', '')} {strip_html(j.get('description', ''))}", kws):
            out.append(_sig("recruitee", j.get("careers_url", ""), j.get("title", ""), b.get("name") or j.get("company_name", ""),
                            j.get("location", ""), j.get("description", ""), b.get("domain", "")))  # fmt: skip
    return out


async def _personio(b: dict, kws: list[str]) -> list[Signal]:
    r = await _get(f"https://{b['board']}.jobs.personio.de/xml", language="en")
    if not r:
        return []
    try:
        root = ET.fromstring(r.text)  # noqa: S314
    except ET.ParseError:
        return []
    out = []
    for p in root.iter("position"):
        title = p.findtext("name") or ""
        body = " ".join((v.findtext("value") or "") for v in p.iter("jobDescription"))
        if matches(f"{title} {strip_html(body)}", kws):
            pid = p.findtext("id") or ""
            out.append(_sig("personio", f"https://{b['board']}.jobs.personio.de/job/{pid}", title, b.get("name", b["board"]),
                            p.findtext("office") or "", body, b.get("domain", "")))  # fmt: skip
    return out


async def _workable(b: dict, kws: list[str]) -> list[Signal]:
    d = await _json(
        f"https://apply.workable.com/api/v1/widget/accounts/{b['board']}", details="true"
    )
    out = []
    for j in (d or {}).get("jobs", []) if isinstance(d, dict) else []:
        if matches(f"{j.get('title', '')} {strip_html(j.get('description', ''))}", kws):
            out.append(_sig("workable", j.get("url") or j.get("shortlink", ""), j.get("title", ""), b.get("name", b["board"]),
                            j.get("city", "") or j.get("country", ""), j.get("description", ""), b.get("domain", "")))  # fmt: skip
    return out


_ATS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "recruitee": _recruitee,
    "personio": _personio,
    "workable": _workable,
}


async def company_boards(cfg: dict, kws: list[str]) -> list[Signal]:
    """cfg: {'greenhouse': [{'board': 'nuro', 'name': 'Nuro', 'domain': 'nuro.ai'}, ...], ...}"""
    out: list[Signal] = []
    for system, fn in _ATS.items():
        for b in cfg.get(system) or []:
            b = {"board": b} if isinstance(b, str) else b  # noqa: PLW2901
            try:
                out += await fn(b, kws)
            except Exception:
                log.exception(
                    "company board failed",
                    extra={"ctx": {"system": system, "board": b.get("board")}},
                )
    return out
