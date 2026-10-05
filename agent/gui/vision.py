"""The vision model as the agent's eyes: it looks at a screenshot and says what is on screen and where to click.

It never acts and never supplies page content for the lead (an email must still be literally on the copied page
text). Coordinates come back on a 0-1000 grid so they do not depend on the screenshot size.
"""

import asyncio
import base64
from pathlib import Path

from pydantic import BaseModel

from agentkit import llm
from agentkit.config import env
from agentkit.log import get_logger

log = get_logger("agent.gui.vision")
TASK = "gui.step"  # same vision model as the GUI agent (MODEL_GUI_STEP)
_LOOK = Path("agent/prompts/gui_look.txt")
_LOCATE = Path("agent/prompts/gui_locate.txt")
GRID = 1000


class Look(BaseModel):
    popup: bool = False
    label: str = ""
    x: int | None = None
    y: int | None = None
    not_found: bool = False
    blocked: bool = False
    summary: str = ""


class Target(BaseModel):
    found: bool = False
    x: int | None = None
    y: int | None = None
    label: str = ""


def to_pixels(x: int | None, y: int | None, width: int, height: int) -> tuple[int, int] | None:
    """Model point (0-1000 grid) -> screenshot pixels. Values beyond the grid are taken as pixels already."""
    if x is None or y is None or x < 0 or y < 0:
        return None
    if x > GRID or y > GRID:
        return (min(x, width - 1), min(y, height - 1))
    return (min(width - 1, x * width // GRID), min(height - 1, y * height // GRID))


async def shrink(shot_b64: str, width: int = 768) -> str:
    """The screenshot scaled down to `width` px (ImageMagick): fewer image tokens, much faster on a CPU. The model's
    coordinates use a 0-1000 grid, so the size does not matter to them. Unchanged if ImageMagick fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "convert", "png:-", "-resize", f"{width}x", "png:-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        out, _ = await asyncio.wait_for(proc.communicate(base64.b64decode(shot_b64)), 20)
    except (OSError, TimeoutError):
        return shot_b64
    return base64.b64encode(out).decode() if proc.returncode == 0 and out else shot_b64


async def _ask(system: Path, text: str, shot_b64: str, schema, max_tokens: int):
    shot_b64 = await shrink(shot_b64)
    messages: list[dict] = [
        {"role": "system", "content": system.read_text(encoding="utf-8")},  # noqa: ASYNC240 - tiny file
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot_b64}"}},
            ],
        },
    ]
    # A "thinking" model (the plain qwen3-vl tags) reasons before it answers and needs a large budget; an instruct
    # build only needs a few dozen tokens. VISION_MAX_TOKENS overrides the budget for both calls.
    budget = int(env("VISION_MAX_TOKENS", "") or max_tokens)
    wait = float(env("DESKTOP_VISION_TIMEOUT", "240") or 240) + 30
    done = await llm.complete(
        TASK, messages, schema, temperature=0.0, max_tokens=budget, timeout=wait
    )
    if not isinstance(done.parsed, schema):
        raise TypeError("no structured reply")
    return done.parsed


async def look(shot_b64: str) -> Look:
    """What is on screen: popup to close, 404 / blocked page, one-line summary."""
    out = await _ask(_LOOK, "Describe this screen.", shot_b64, Look, 2000)
    out.summary = out.summary[:300]
    return out


async def locate(shot_b64: str, what: str) -> Target:
    """Where `what` (a link, menu item or result title) is on this screen, or found=False."""
    return await _ask(_LOCATE, f"Find and locate: {what[:200]}", shot_b64, Target, 1200)


def png_b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()
