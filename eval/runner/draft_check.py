"""Run the generative tasks on sample inputs and apply the code checks (no labels needed).

Uses the same task functions the daily agent uses, so it exercises prompt + schema + checks together.
Without DATABASE_URL, retrieval finds nothing and the prompt runs without examples or knowledge.
Exits non-zero if any sample returns a schema failure or check problems. Run from the repo root.
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable

from agent.tasks import blog as posts
from agent.tasks import proposal as outreach_draft
from agent.tasks import topics

LEAD = (
    "Company: Vehicora Motors\nProject: Android Automotive infotainment platform with Vehicle HAL and OTA.\n"
    "Technologies: AAOS, Vehicle HAL, SELinux, OTA\nLocation: Pune, India\nContact role: VP Engineering"
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
    async def d1():
        return (await outreach_draft.draft_proposal(LEAD))[1]

    async def d2():
        plan = await topics.plan_topics(BRIEF)
        return [] if plan.topics else ["no topics"]

    async def d3():
        return (await posts.draft_post(TOPIC))[1]

    results = [
        await _run("outreach.draft", d1),
        await _run("content.plan", d2),
        await _run("content.draft_post", d3),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
