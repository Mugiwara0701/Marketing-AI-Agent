"""Blog research: what is hot right now in AOSP / Android platform / embedded Linux.

Gathers articles and discussions from sources that expose popularity (Hacker News points, dev.to reactions)
and from editorial feeds, drops stale items, and ranks by popularity x freshness. The ranked list and the
terms that recur across many items go to the topic-planning LLM, which turns them into blog topics.
"""

import math
import re
import time
from collections import Counter
from datetime import datetime
from email.utils import parsedate_to_datetime
from itertools import pairwise
from urllib.parse import urlparse

from agentkit.log import get_logger

from . import sources
from .sources import Item

log = get_logger("agent.research")
_MAX_AGE_DAYS = 21
_HALF_LIFE_DAYS = 7.0
_STOP = {
    "the",
    "and",
    "for",
    "with",
    "new",
    "how",
    "what",
    "why",
    "your",
    "you",
    "from",
    "this",
    "that",
    "are",
    "was",
    "not",
    "but",
    "can",
    "will",
    "use",
    "using",
    "into",
    "over",
    "about",
    "more",
    "than",
    "its",
    "our",
    "out",
    "one",
    "all",
    "via",
    "vs",
    "get",
    "got",
    "has",
    "have",
    "just",
    "now",
    "top",
    "best",
    "make",
    "made",
    "day",
    "week",
    "year",
}


def parse_date(text: str) -> float | None:
    """RSS (RFC 822) or Atom (ISO 8601) timestamp -> epoch seconds."""
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Items about these get a relevance boost: generic "linux" posts (server setup, desktop fixes) are not our topic.
_CORE = ["aosp", "android", "bsp", "yocto", "buildroot", "embedded", "kernel", "gki", "hal", "automotive", "bring-up",
         "device tree", "u-boot", "risc-v", "soc", "firmware", "selinux", "treble", "vendor"]  # fmt: skip
_CORE_BOOST = 1.8


def rank(items: list[Item], now: float | None = None) -> list[Item]:
    """Sets heat = (1 + ln(1 + score + 2*comments)) x 0.5^(age/7 days); drops items past the age limit."""
    now = now or time.time()
    out = []
    for it in items:
        age = (
            (now - it.published) / 86400 if it.published else _HALF_LIFE_DAYS
        )  # unknown date: treat as a week old
        if age > _MAX_AGE_DAYS:
            continue
        it.heat = (1 + math.log1p(max(it.score, 0) + 2 * max(it.comments, 0))) * 0.5 ** (
            max(age, 0) / _HALF_LIFE_DAYS
        )
        out.append(it)
    return sorted(out, key=lambda i: i.heat, reverse=True)


def recurring_terms(items: list[Item], top: int = 12, min_items: int = 3) -> list[tuple[str, int]]:
    """Words and word pairs that appear in the titles of at least `min_items` different items."""
    seen: Counter[str] = Counter()
    for it in items:
        words = [
            w for w in re.findall(r"[a-z0-9][a-z0-9+.\-]{1,}", it.title.lower()) if w not in _STOP
        ]
        grams = set(words) | {f"{a} {b}" for a, b in pairwise(words)}
        seen.update(grams)
    ranked = [(t, n) for t, n in seen.most_common(80) if n >= min_items]
    # prefer the pair over its single words when both have the same count
    keep = [(t, n) for t, n in ranked if not any(t != o and t in o and n == m for o, m in ranked)]
    return keep[:top]


async def _hackernews(queries: list[str], kws: list[str], days: int) -> list[Item]:
    since = int(time.time()) - days * 86400
    wanted = [*kws, "android", "linux", "kernel", "embedded"]
    out: list[Item] = []
    for q in queries:
        data = await sources._json(
            "https://hn.algolia.com/api/v1/search",
            query=q, tags="story", hitsPerPage=20, numericFilters=f"created_at_i>{since},points>15",
        )  # fmt: skip
        for h in (data or {}).get("hits", []) if isinstance(data, dict) else []:
            if h.get("title") and sources.matches(h["title"], wanted):
                url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
                out.append(Item("hackernews", h["title"], url, "", h.get("points") or 0, h.get("num_comments") or 0, h.get("created_at_i")))  # fmt: skip
    return out


async def _devto(tags: list[str], days: int) -> list[Item]:
    out: list[Item] = []
    for tag in tags:
        data = await sources._json("https://dev.to/api/articles", tag=tag, top=days, per_page=20)
        for a in data if isinstance(data, list) else []:
            if (a.get("positive_reactions_count") or 0) >= 3:
                out.append(Item("dev.to", a.get("title", ""), a.get("url", ""), sources.strip_html(a.get("description", ""))[:300],
                                a.get("positive_reactions_count") or 0, a.get("comments_count") or 0,
                                parse_date(a.get("published_at", ""))))  # fmt: skip
    return out


async def _feeds(urls: list[str], kws: list[str]) -> list[Item]:
    wanted = [
        *kws,
        "android",
        "kernel",
        "embedded",
        "arm",
        "yocto",
        "risc-v",
        "raspberry",
        "board",
        "soc",
    ]
    out: list[Item] = []
    for url in urls:
        host = urlparse(url).hostname or "feed"
        for e in (await sources._feed(url))[:30]:
            if sources.matches(f"{e['title']} {e['summary']}", wanted):
                out.append(
                    Item(
                        host,
                        e["title"],
                        e["link"],
                        e["summary"][:400],
                        0,
                        0,
                        parse_date(e.get("date", "")),
                    )
                )
    return out


async def research_items(days: int = 14) -> list[Item]:
    """Ranked, de-duplicated research items (hottest first)."""
    cfg = sources.load_config()
    kws: list[str] = cfg.get("keywords", [])
    items = await _hackernews(cfg.get("topic_hn_queries", []), kws, days)
    items += await _devto(cfg.get("topic_devto_tags", []), days)
    items += await _feeds(cfg.get("topic_feeds", []), kws)
    by_url: dict[str, Item] = {}
    for it in items:
        old = by_url.get(it.url)
        if not old or it.score > old.score:
            by_url[it.url] = it
    ranked = rank(list(by_url.values()))
    log.info("research items", extra={"ctx": {"count": len(ranked)}})
    return ranked[:80]
