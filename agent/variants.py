"""Make the per-platform versions of today's post and store them."""

from pathlib import Path

import yaml

from agentkit.log import get_logger

from . import settings, store
from .tasks import adapt

log = get_logger("agent.variants")


def load_platforms() -> dict[str, dict]:
    p = Path(settings.load().platforms_file)
    return (
        (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("platforms", {})
        if p.exists()
        else {}
    )


async def generate(
    post_id, title: str, body_markdown: str, source_text: str = ""
) -> dict[str, list[str]]:
    """One LLM rewrite per configured platform. A platform that fails is skipped, never fatal.
    Returns {platform: problems}."""
    results: dict[str, list[str]] = {}
    for key, spec in load_platforms().items():
        try:
            variant, text, problems = await adapt.adapt(spec, title, body_markdown, source_text)
        except Exception:
            log.exception("variant failed", extra={"ctx": {"platform": key}})
            results[key] = ["generation failed"]
            continue
        await store.save_variant(
            post_id, key, variant.title, text, adapt.clean_tags(variant.tags, spec.get("tags_max", 4)),
            "; ".join(problems) or None,
        )  # fmt: skip
        results[key] = problems
    return results
