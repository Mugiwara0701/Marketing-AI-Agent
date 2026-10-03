"""Phase 3: the agent loop. screenshot -> vision model -> one action -> executor -> repeat.

Page content only ever reaches the model as pixels, in a user turn, under a system prompt that says it is
untrusted. Every step goes through Policy (Phase 4) before it reaches the desktop.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from agentkit import llm
from agentkit.log import get_logger

from .actions import GuiStep
from .executor_client import ExecutorClient, ExecutorError
from .policy import ActionLog, Policy

log = get_logger("agent.gui")
TASK = "gui.step"
_SYSTEM = Path("agent/prompts/gui_step.txt")
_HISTORY = 8
_URL_CHECK_EVERY = 4

DecideFn = Callable[[str, list[str], str], Awaitable[GuiStep]]


@dataclass
class Outcome:
    status: str  # done | failed | stopped
    reason: str
    steps: int
    final: GuiStep | None = None
    run_id: str = ""


async def decide(goal: str, history: list[str], screenshot_b64: str) -> GuiStep:
    """Ask the vision model for the next action."""
    system_prompt = _SYSTEM.read_text(encoding="utf-8")  # noqa: ASYNC240 - tiny local file
    recent = "\n".join(history[-_HISTORY:]) or "(none yet)"
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Goal: {goal}\n\nYour previous actions:\n{recent}"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                },
            ],
        },
    ]
    completion = await llm.complete(
        TASK, messages, GuiStep, temperature=0.1, max_tokens=400, timeout=120
    )
    if not isinstance(completion.parsed, GuiStep):
        raise TypeError("no structured step")
    return completion.parsed


async def current_url(ex: ExecutorClient) -> str:
    """Address bar text, read the way a person would: focus it, copy, read the clipboard, leave."""
    url = await ex.clipboard_after("ctrl+l", "ctrl+c")
    await ex.act(action="key", key="Escape")
    return url.strip()


async def run_task(  # noqa: PLR0911 - each stop condition returns its own outcome
    goal: str,
    ex: ExecutorClient,
    policy: Policy | None = None,
    decide_fn: DecideFn = decide,
    *,
    log_dir: str | None = None,
    require_email: bool = True,
) -> Outcome:
    policy = policy or Policy.from_env()
    run_id = uuid.uuid4().hex[:12]
    audit = ActionLog(run_id, log_dir)
    audit.write(event="start", goal=goal)
    started = time.monotonic()
    history: list[str] = []
    steps = 0

    def end(status: str, reason: str, final: GuiStep | None = None) -> Outcome:
        audit.write(event="end", status=status, reason=reason, steps=steps)
        return Outcome(status, reason, steps, final, run_id)

    try:
        await ex.reset()
        shot = (await ex.act(action="screenshot"))["screenshot"]
        while True:
            if why := policy.stop_reason(steps, started):
                return end("stopped", why)
            step = await decide_fn(goal, history, shot)
            steps += 1
            audit.write(event="step", n=steps, **step.model_dump(exclude_none=True))
            if step.action == "done":
                if require_email and not step.contact_email:
                    return end("failed", "model said done without an email", step)
                return end("done", "model reports a contact", step)
            if step.action == "fail":
                return end("failed", step.thought or "model gave up", step)
            if why := policy.refuse(step):
                history.append(f"(refused: {why})")
                audit.write(event="refused", reason=why)
                if steps >= 3 and sum("refused" in h for h in history) >= 3:
                    return end("stopped", "model keeps choosing disallowed actions")
                continue
            result = await ex.act(**step.to_executor())
            shot = result["screenshot"]
            history.append(f"{step.action} {step.text or step.key or (step.x, step.y)}"[:80])
            if policy.looping(step, shot):
                return end("stopped", "stuck: same action on the same screen")
            if steps % _URL_CHECK_EVERY == 0:
                url = await current_url(ex)
                audit.write(event="url", url=url)
                if url and not policy.url_allowed(url):
                    return end("stopped", f"navigated to a disallowed page: {url[:80]}")
    except ExecutorError as exc:
        return end("failed", str(exc))
    except Exception as exc:
        log.exception("gui run failed")
        return end("failed", f"{type(exc).__name__}")
