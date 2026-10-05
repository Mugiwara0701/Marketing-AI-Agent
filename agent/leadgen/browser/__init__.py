"""Browser control, behind one interface, so research does not care how pages are reached.

    http     polite HTTP client + SearXNG (or DuckDuckGo HTML) for search. Headless servers, dry-runs, CI.
    chrome   a real Chrome (Playwright, persistent profile) for JavaScript sites; visible unless CHROME_HEADLESS=1.
    desktop  the visible Chrome on the Xubuntu desktop driven by mouse/keyboard + OCR/VLM (agent.gui.desktop).

Every backend goes through the same Guard: page budget per run and per domain, per-host pause, portals that forbid
scraping, and hosts that showed a bot check (never retried this run; the agent never tries to get past one).
"""

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from agentkit.log import get_logger

from .. import intent
from ..config import LeadgenConfig
from ..models import Page, SearchResult

log = get_logger("agent.leadgen.browser")


class SearchBlockedError(RuntimeError):
    """The search engine refused (CAPTCHA, rate limit, error). The caller rests it and moves on."""


class BudgetExhaustedError(RuntimeError):
    pass


@dataclass
class Guard:
    max_pages: int = 60
    max_per_domain: int = 6
    pages: int = 0
    per_domain: Counter = field(default_factory=Counter)
    blocked_hosts: dict[str, str] = field(default_factory=dict)  # registrable domain -> reason
    engine_rest: dict[str, float] = field(
        default_factory=dict
    )  # engine -> monotonic time it may be used again

    @classmethod
    def from_config(cls, cfg: LeadgenConfig) -> "Guard":
        return cls(max_pages=int(cfg.limit("max_pages_per_run", 60)),
                   max_per_domain=int(cfg.limit("max_pages_per_domain", 6)))  # fmt: skip

    def refuse(self, url: str) -> str | None:
        """Why this page must not be opened now, or None."""
        dom = intent.registrable_domain(url)
        if self.pages >= self.max_pages:
            raise BudgetExhaustedError(f"page budget of {self.max_pages} used")
        if dom in self.blocked_hosts:
            return f"site blocked earlier this run ({self.blocked_hosts[dom]})"
        if self.per_domain[dom] >= self.max_per_domain:
            return f"already read {self.max_per_domain} pages on {dom}"
        return None

    def opened(self, url: str) -> None:
        self.pages += 1
        self.per_domain[intent.registrable_domain(url)] += 1

    def block(self, url: str, reason: str) -> None:
        dom = intent.registrable_domain(url)
        if dom and dom not in self.blocked_hosts:
            self.blocked_hosts[dom] = reason
            log.warning(
                "Source blocked, skipped for this run",
                extra={"ctx": {"domain": dom, "why": reason}},
            )

    def rest_engine(self, engine: str, minutes: float, why: str) -> None:
        self.engine_rest[engine] = time.monotonic() + minutes * 60
        log.warning(
            "Search engine resting",
            extra={"ctx": {"engine": engine, "why": why, "minutes": minutes}},
        )

    def engine_ready(self, engine: str) -> bool:
        return self.engine_rest.get(engine, 0) <= time.monotonic()


class Browser(Protocol):
    name: str
    guard: Guard

    async def start(self) -> None: ...
    async def search(self, query: str, limit: int) -> list[SearchResult]: ...
    async def open(self, result: SearchResult) -> Page | None: ...
    async def open_url(self, url: str) -> Page | None: ...
    async def follow(self, labels: list[str]) -> Page | None: ...
    async def close(self) -> None: ...


def make_browser(kind: str, cfg: LeadgenConfig) -> Browser:
    guard = Guard.from_config(cfg)
    if kind == "http":
        from .http import HttpBrowser  # noqa: PLC0415

        return HttpBrowser(guard)
    if kind == "chrome":
        from .chrome import ChromeBrowser  # noqa: PLC0415

        return ChromeBrowser(guard)
    if kind == "desktop":
        from .desktop import DesktopBrowser  # noqa: PLC0415

        return DesktopBrowser(
            guard, cfg.desktop_engines, float(cfg.limit("engine_rest_minutes", 30))
        )
    raise ValueError(f"unknown BROWSER_BACKEND {kind!r}: use http, chrome or desktop")
