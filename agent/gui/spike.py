"""Phase 3 exit check: run short, fixed browser tasks and report success rate, steps and seconds per step.

Run it once per candidate model (set MODEL_GUI_STEP) and compare. The plan's rule: under about 70% on short
tasks means stop and rethink. A task passes when the model says done AND the browser address contains the
expected text, which the harness reads itself. Editable: the sites below are public and may change.
"""

import time
from dataclasses import dataclass

from .executor_client import ExecutorClient
from .loop import current_url, run_task
from .policy import Policy


@dataclass(frozen=True)
class Task:
    goal: str
    url_has: str


TASKS = [
    Task("Open https://example.com in the browser, then finish with done.", "example.com"),
    Task("Open https://www.wikipedia.org, click the English Wikipedia link, then finish with done.", "en.wikipedia.org"),
    Task("Open https://www.python.org, click the 'Downloads' menu item, then finish with done.", "python.org/downloads"),
    Task("Open https://news.ycombinator.com, click the 'new' link at the top, then finish with done.", "newest"),
    Task("Open https://www.iana.org, click 'Domains' in the menu, then finish with done.", "iana.org/domains"),
    Task("Open https://httpbin.org, scroll down, then finish with done.", "httpbin.org"),
    Task("Open https://www.mozilla.org, click 'Firefox' in the top menu, then finish with done.", "firefox"),
    Task("Open https://www.rust-lang.org, click 'Learn' in the top menu, then finish with done.", "rust-lang.org/learn"),
    Task("Open https://www.debian.org, click 'Support' in the left menu, then finish with done.", "debian.org/support"),
    Task("Open https://developer.android.com, click 'Docs' in the top menu, then finish with done.", "developer.android.com/docs"),
]  # fmt: skip
_MAX_STEPS = 12


async def run(count: int = 10, ex: ExecutorClient | None = None) -> int:
    ex = ex or ExecutorClient()
    results = []
    for n, task in enumerate(TASKS[: max(1, min(count, len(TASKS)))], 1):
        start = time.monotonic()
        out = await run_task(
            task.goal, ex, Policy(max_steps=_MAX_STEPS, max_seconds=240), require_email=False
        )
        url = await current_url(ex) if out.status == "done" else ""
        ok = out.status == "done" and task.url_has in url.lower()
        secs = time.monotonic() - start
        results.append((ok, out.steps, secs))
        verdict = "PASS" if ok else f"FAIL ({out.status}: {out.reason[:60]})"
        print(f"{n:>2}. {verdict}  steps={out.steps}  {secs:.0f}s  {task.goal[:55]}")  # noqa: T201
    passed = sum(ok for ok, _, _ in results)
    steps = sum(s for _, s, _ in results)
    secs = sum(t for _, _, t in results)
    rate = passed / len(results)
    print(  # noqa: T201
        f"\n{passed}/{len(results)} passed ({rate:.0%}); "
        f"{steps / len(results):.1f} steps/task, {secs / max(steps, 1):.1f}s/step; need >= 70%"
    )
    return 0 if rate >= 0.7 else 1
