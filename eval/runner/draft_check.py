"""Run the generative tasks on sample inputs and apply the code checks (no labels needed).

Uses the same module functions the services use, so it exercises prompt + schema + checks together.
Without DATABASE_URL, retrieval finds nothing and the prompt runs without examples or knowledge.
Exits non-zero if any sample returns a schema failure or check problems. Run from the repo root.
"""

import asyncio
import importlib.util
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path


def _load(service: str, module: str):
    path = (
        Path(__file__).resolve().parents[2] / "services" / service / "app" / module / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(f"{service}_{module}", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LEAD = (
    "Company: Vehicora Motors. Signal: job post 'Senior AOSP Engineer - Android Automotive infotainment, "
    "Vehicle HAL, SELinux, OTA'. Contact role: VP Engineering."
)
THREAD = (
    "Our email: intro about AOSP and BSP help.\n"
    "Their reply: Interesting. Do you also do Yocto based BSPs for i.MX8 boards?"
)
BRIEF = "Recent posts: 'What is AOSP', 'Yocto vs Buildroot'. Best performer: how-to posts on kernel drivers."
TOPIC = "Title: Bringing up a custom board on AOSP. Angle: the checklist from device tree to first boot."


async def _run(name: str, fn: Callable[[], Awaitable[list[str]]]) -> bool:
    try:
        problems = await fn()
    except Exception as exc:
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        return False
    print(f"{'ok  ' if not problems else 'FAIL'} {name} {problems or ''}")
    return not problems


async def main() -> int:
    outreach_draft = _load("outreach-service", "drafting")
    replies = _load("outreach-service", "replies")
    topics = _load("content-service", "topics")
    posts = _load("content-service", "drafting")

    async def d1():
        return (await outreach_draft.draft_email(LEAD))[1]

    async def d2():
        return (await replies.draft_reply(THREAD))[1]

    async def d3():
        plan = await topics.plan_topics(BRIEF)
        return [] if plan.topics else ["no topics"]

    async def d4():
        return (await posts.draft_post(TOPIC))[1]

    results = [
        await _run("outreach.draft", d1),
        await _run("outreach.draft_reply", d2),
        await _run("content.plan", d3),
        await _run("content.draft_post", d4),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
