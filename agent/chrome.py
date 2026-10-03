"""Visible-browser mode (`--visible` / BROWSER_VISIBLE=1): the agent's web work happens in one real Chrome window.

Searches go through Google and every company page opens in the same window, one after another, so a person can
watch. Pages are still checked by web.py first (robots.txt, no private addresses, no blocked portals).
If Google shows a CAPTCHA the agent waits while the person solves it in the window."""

import asyncio
import contextlib
import re
import time
from urllib.parse import quote_plus

from agentkit.config import env
from agentkit.log import get_logger

log = get_logger("agent.chrome")

_PROFILE = ".chrome-profile"  # keeps cookies/consent between runs; git-ignored
_SEARCH_GAP = 6.0  # seconds between Google searches
_CAPTCHA_WAIT = 180  # seconds to wait for a person to solve a CAPTCHA

BLOCKED = "<!--bot-check-->"  # returned by goto() when a site refuses automated visitors

_pw = None
_ctx = None
_page = None
_lock = asyncio.Lock()
_last_search = 0.0
_google_off = False  # set when a CAPTCHA went unsolved: the rest of the run searches elsewhere


def _dwell_ms() -> int:
    """How long each page stays on screen (BROWSER_DWELL_SECONDS, default 6)."""
    try:
        return int(float(env("BROWSER_DWELL_SECONDS", "6") or 6) * 1000)
    except ValueError:
        return 6000


async def _linger(page) -> None:
    """Let a person see the page: scroll down gently, then hold it for the dwell time."""
    step = _dwell_ms() // 3
    try:
        for _ in range(2):
            await page.wait_for_timeout(step)
            await page.mouse.wheel(0, 600)
        await page.wait_for_timeout(step)
    except Exception:  # noqa: S110 - the page may have navigated away; nothing to show then
        pass


_BOT_CHECK = re.compile(
    r"verifying you are human|just a moment\.\.\.|checking your browser|attention required|"
    r"security service to protect against malicious bots|enable javascript and cookies to continue|"
    r"verify you are human|performing security verification",
    re.I,
)
_CHECK_WAIT = 8  # seconds a site's own check may take to clear by itself before a person is asked


def bot_check(html: str) -> bool:
    """True if this is a site's bot-protection page (Cloudflare and similar), not the real content."""
    return bool(_BOT_CHECK.search(html[:6000])) and len(html) < 60_000


def enabled() -> bool:
    return bool(env("BROWSER_VISIBLE"))


async def _open():
    """One persistent Chrome window for the whole run (real Chrome if installed, else bundled Chromium)."""
    global _pw, _ctx, _page  # noqa: PLW0603 - one shared window per process
    if _page is not None:
        return _page
    from playwright.async_api import async_playwright  # noqa: PLC0415 - optional dependency

    _pw = await async_playwright().start()
    kwargs = {"headless": False, "chromium_sandbox": True, "slow_mo": 250, "viewport": None}
    try:
        _ctx = await _pw.chromium.launch_persistent_context(_PROFILE, channel="chrome", **kwargs)
    except Exception:
        log.info("Google Chrome not found, using bundled Chromium")
        _ctx = await _pw.chromium.launch_persistent_context(_PROFILE, **kwargs)
    _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()
    return _page


async def _reset() -> None:
    """Forget a dead window so _open() starts a new one."""
    global _pw, _ctx, _page  # noqa: PLW0603
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:  # noqa: S110 - already gone
        pass
    _pw = _ctx = _page = None


async def close() -> None:
    global _pw, _ctx, _page  # noqa: PLW0603
    try:  # the person may already have closed the window: nothing left to clean up then
        if _ctx is not None:
            await _ctx.close()
        if _pw is not None:
            await _pw.stop()
    except Exception:
        log.info("browser already closed")
    _pw = _ctx = _page = None


async def goto(url: str) -> str | None:
    """Open `url` in the window and return the page HTML (None on error or non-200)."""
    async with _lock:
        try:
            page = await _open()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # let scripts settle, but a page that never goes quiet (bot checks, trackers) is read anyway
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=6_000)
            status = resp.status if resp else None
            html = await page.content()  # read first, then let the person look at it
            if bot_check(
                html
            ):  # checks are often served with a 403/503 status, so look before judging the status
                if not await _check_cleared(page, url):
                    return BLOCKED
                html, status = await page.content(), 200
            if status != 200:
                log.info("visible fetch refused", extra={"ctx": {"url": url, "status": status}})
                return None
            await _linger(page)
            return html
        except Exception as exc:
            log.info(
                "visible fetch failed", extra={"ctx": {"url": url, "error": type(exc).__name__}}
            )
            if (
                "closed" in str(exc).lower()
            ):  # the window was closed: open a fresh one on the next page
                await _reset()
            return None


async def _check_cleared(page, url: str) -> bool:
    """A site's bot check: give it a few seconds to pass by itself, then ask the person to pass it in the window.
    The agent never tries to get through a check; if nobody does within the wait, the site is skipped."""
    waited = 0
    while bot_check(await page.content()):
        if waited == _CHECK_WAIT:
            await _notify(
                f"{url} shows a human check: pass it in the Chrome window (the run waits)."
            )
        if waited >= _CHECK_WAIT + _CAPTCHA_WAIT:
            log.info("site shows a bot check, skipping", extra={"ctx": {"url": url}})
            return False
        await asyncio.sleep(2)
        waited += 2
    return True


async def _notify(msg: str) -> None:
    print(msg)  # noqa: T201
    try:  # desktop popup where available; the terminal line is enough elsewhere
        proc = await asyncio.create_subprocess_exec("notify-send", "Marketing agent", msg)
        await proc.wait()
    except OSError:
        pass


async def _captcha_cleared(page) -> bool:
    """True if there is no CAPTCHA, or a person solved it in time. A person must do it: the agent never does."""
    waited = 0
    while "/sorry/" in page.url or "unusual traffic" in (await page.content()).lower():
        if waited == 0:
            await _notify("Google wants a CAPTCHA: solve it in the Chrome window (the run waits).")
        if waited >= _CAPTCHA_WAIT:
            return False
        await asyncio.sleep(3)
        waited += 3
    return True


async def google(query: str, limit: int = 8) -> list[tuple[str, str]]:
    """(url, title) of the organic results for `query`, searched in the visible window."""
    global _last_search, _google_off  # noqa: PLW0603
    if _google_off:
        return []
    async with _lock:
        try:
            page = await _open()
            wait = _last_search + _SEARCH_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _last_search = time.monotonic()
            await page.goto(
                f"https://www.google.com/search?q={quote_plus(query)}&hl=en",
                wait_until="domcontentloaded",
            )
            try:  # the cookie-consent page, shown in some regions
                btn = page.get_by_role("button", name=re.compile("accept all|i agree", re.I))
                await btn.first.click(timeout=2000)
            except Exception:  # noqa: S110 - no consent page: nothing to click
                pass
            if not await _captcha_cleared(page):
                _google_off = True
                await _notify("CAPTCHA not solved: using SearXNG for the rest of this run.")
                return []
            await page.wait_for_selector("a h3", timeout=10_000)
            pairs = await page.eval_on_selector_all(
                "a:has(h3)", "els => els.map(e => [e.href, e.querySelector('h3').innerText])"
            )
            await _linger(page)
        except Exception:
            log.info("google search failed")
            return []
    seen: set[str] = set()
    out = []
    for url, title in pairs:
        if url.startswith("http") and "google." not in url.split("/")[2] and url not in seen:
            seen.add(url)
            out.append((url, title))
    return out[:limit]
