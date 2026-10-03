"""Phase 4: what the GUI agent may do, how long it may run, and the audit trail.

The model never gets a shell. On top of the executor's fixed action set this layer adds: a step and time
budget, a kill switch (create the file named by GUI_KILL_FILE to stop every run), a key allowlist (no
ctrl+s / ctrl+p / downloads / dev tools), the portal denylist from agent.web, loop detection, and a JSONL
log of every step.
"""

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from agentkit.config import env

from .. import web
from .actions import GuiStep

ALLOWED_KEYS = frozenset(
    {"return", "escape", "tab", "ctrl+l", "ctrl+f", "ctrl+a", "ctrl+c", "page_down", "page_up",
     "down", "up", "home", "end", "alt+left"}
)  # fmt: skip
_LOOP_REPEATS = 3


@dataclass
class Policy:
    max_steps: int = 25
    max_seconds: float = 300
    kill_file: Path = field(
        default_factory=lambda: Path(env("GUI_KILL_FILE", "/tmp/gui-agent.stop") or "")  # noqa: S108
    )
    _recent: deque = field(default_factory=lambda: deque(maxlen=_LOOP_REPEATS))

    @classmethod
    def from_env(cls) -> "Policy":
        return cls(
            max_steps=int(env("GUI_MAX_STEPS", "25") or 25),
            max_seconds=float(env("GUI_MAX_SECONDS", "300") or 300),
        )

    def stop_reason(self, steps: int, started: float) -> str | None:
        if self.kill_file.exists():
            return f"kill switch ({self.kill_file}) is set"
        if steps >= self.max_steps:
            return f"step budget of {self.max_steps} used"
        if time.monotonic() - started > self.max_seconds:
            return f"time budget of {self.max_seconds:g}s used"
        return None

    @staticmethod
    def refuse(step: GuiStep) -> str | None:
        """Reason this model-chosen action is not allowed, or None."""
        if step.action == "key" and (step.key or "").lower() not in ALLOWED_KEYS:
            return f"key {step.key!r} is not allowed"
        if step.action in ("click", "double_click") and (step.x is None or step.y is None):
            return "click without coordinates"
        if step.action == "type" and not step.text:
            return "type without text"
        return None

    @staticmethod
    def url_allowed(url: str) -> bool:
        """Browser address bar content after navigation. Portals on the denylist are never visited."""
        u = (url or "").strip().lower()
        if u.startswith(("chrome://newtab", "about:blank")):
            return True  # a fresh browser has not navigated yet
        return u.startswith(("http://", "https://")) and not web.blocked(u)

    def looping(self, step: GuiStep, screenshot_b64: str) -> bool:
        """Same action, same screen, several times in a row: the model is stuck."""
        self._recent.append((step.signature(), hashlib.sha1(screenshot_b64.encode()).hexdigest()))  # noqa: S324
        return len(self._recent) == _LOOP_REPEATS and len(set(self._recent)) == 1


class ActionLog:
    """One JSON line per step (no screenshots), so a run can be audited afterwards."""

    def __init__(self, run_id: str, root: str | None = None) -> None:
        d = Path(root or env("GUI_LOG_DIR", "out/gui") or "out/gui")
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{run_id}.jsonl"

    def write(self, **entry) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}) + "\n")
