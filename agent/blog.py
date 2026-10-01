"""Part 2: research current topics, pick ONE, write ONE technical blog, save it."""

import re
import time
from dataclasses import dataclass
from datetime import date

from agentkit.log import get_logger

from . import notify, research, sources, store, variants, web
from .tasks import blog as blog_task
from .tasks import topics as topics_task

log = get_logger("agent.blog")


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", s.lower()))


def too_similar(title: str, existing: list[str], threshold: float = 0.6) -> bool:
    a = _words(title)
    return any(a and (b := _words(t)) and len(a & b) / len(a | b) >= threshold for t in existing)


BRIEF_ITEMS = 45
_SOURCE_CHARS = 5000
_MIN_SOURCE_CHARS = 1500  # less than this and a post cannot be grounded: try the next topic


def build_brief(items: list[sources.Item], recent: list[str]) -> str:
    """NUMBERED items, hottest first, with popularity and age; recurring terms; our recent titles."""
    now = time.time()

    def line(n: int, i: sources.Item) -> str:
        bits = [f"{i.score} points/reactions" if i.score else "", f"{i.comments} comments" if i.comments else "",
                f"{max(0, round((now - i.published) / 86400))}d ago" if i.published else ""]  # fmt: skip
        meta = ", ".join(b for b in bits if b)
        return (
            f"[{n}] [{i.source}] {i.title}"
            + (f" ({meta})" if meta else "")
            + (f": {i.summary[:160]}" if i.summary else "")
        )

    terms = ", ".join(f"{t} (x{n})" for t, n in research.recurring_terms(items))
    research_block = (
        "\n".join(line(n, i) for n, i in enumerate(items[:BRIEF_ITEMS], 1))
        or "(none found; use general current practice)"
    )
    return (
        f"Research items, hottest first:\n{research_block}\n\n"
        f"Terms recurring across items: {terms or '-'}\n\n"
        "Our recent post titles:\n" + "\n".join(f"- {t}" for t in recent[:40])
    )


async def gather_sources(
    topic: topics_task.Topic, items: list[sources.Item]
) -> list[tuple[sources.Item, str]]:
    """The cited research items with the text of their pages (falls back to the item's own summary)."""
    out: list[tuple[sources.Item, str]] = []
    for n in dict.fromkeys(topic.source_ids):
        if not 1 <= n <= min(len(items), BRIEF_ITEMS):
            continue
        item = items[n - 1]
        text = await web.fetch_page_text(item.url) or ""
        text = (text if len(text) > len(item.summary) else f"{item.summary} {text}").strip()
        out.append((item, f"{item.title}. {text}"[:_SOURCE_CHARS]))
    return out


def sources_block(found: list[tuple[sources.Item, str]]) -> str:
    return "\n\n".join(
        f"[{n}] {item.title} ({item.url})\n{text}" for n, (item, text) in enumerate(found, 1)
    )


def sources_section(found: list[tuple[sources.Item, str]]) -> str:
    return "\n\n## Sources\n\n" + "\n".join(f"- [{i.title}]({i.url})" for i, _ in found)


@dataclass
class Composed:
    topic: topics_task.Topic
    plan: topics_task.TopicPlan
    found: list[tuple[sources.Item, str]]
    grounding: str
    post: blog_task.PostDraft
    problems: list[str]


async def compose(items: list[sources.Item], recent: list[str]) -> Composed | None:
    """Plan topics, pick the first that is new AND well sourced, fetch its articles, write the post.
    Used by the daily run and by the dry run, so both exercise exactly the same steps."""
    plan = await topics_task.plan_topics(build_brief(items, recent))
    for t in plan.topics:
        if too_similar(t.title, recent):
            continue
        found = await gather_sources(t, items)
        if sum(len(text) for _, text in found) < _MIN_SOURCE_CHARS:
            log.info(
                "topic skipped: sources too thin to ground a post",
                extra={"ctx": {"title": t.title}},
            )
            continue
        grounding = " ".join(text for _, text in found)
        post, problems = await blog_task.draft_post(
            f"Topic: {t.title}\nKind: {t.kind}\nAngle: {t.angle}\nWhy now: {t.why_now}\n\n"
            f"SOURCES:\n{sources_block(found)}",
            grounding,
        )
        post.body_markdown = post.body_markdown.rstrip() + sources_section(found)
        return Composed(t, plan, found, grounding, post, problems)
    log.warning("no proposed topic is new and well sourced")
    return None


async def run(today: date | None = None) -> dict:
    day = today or date.today()
    if await store.blog_exists(day):
        log.info("today's blog already exists")
        return {"processed": 0, "skipped": 1}
    c = await compose(await research.research_items(), await store.recent_post_titles())
    if c is None:
        return {"processed": 0, "failed": 1}
    meta = {
        "kind": c.topic.kind, "keywords": c.topic.keywords, "why_now": c.topic.why_now, "problems": c.problems,
        "alternatives": [t.title for t in c.plan.topics if t is not c.topic],
        "sources": [{"title": i.title, "url": i.url} for i, _ in c.found],
    }  # fmt: skip
    post_id = await store.save_blog(
        day, c.topic, c.post, meta, "in_review" if c.problems else "drafted"
    )
    await variants.generate(post_id, c.post.title, c.post.body_markdown, c.grounding)
    await notify.post_blog(post_id)
    log.info("blog saved", extra={"ctx": {"post_id": str(post_id), "problems": len(c.problems)}})
    return {"processed": 1, "needs_review": bool(c.problems)}
