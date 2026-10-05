"""Executor service: the only interface between the agent and the sandbox desktop.

Runs inside the sandbox container. Exposes a fixed set of GUI actions (screenshot, mouse, keyboard,
scroll, clipboard read) over authenticated HTTP. There is no shell, no file access and no browser
API: Chrome is driven purely through X11 input events and observed through screenshots.

Standard library only, so the container image needs no pip install.
"""

import base64
import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

log = logging.getLogger("executor")

MAX_BODY_BYTES = 64 * 1024
MAX_TYPE_CHARS = 2000
MAX_CLIPBOARD_CHARS = 20000
KEY_RE = re.compile(r"^[A-Za-z0-9_+\-]{1,40}$")
KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "BackSpace",
    "del": "Delete",
    "delete": "Delete",
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
    "cmd": "super",
    "win": "super",
    "ctrl": "ctrl",
    "control": "ctrl",
    "tab": "Tab",
    "space": "space",
}
SCROLL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}
MOUSE_BUTTONS = {"left": "1", "middle": "2", "right": "3"}


class ActionError(Exception):
    """A request the executor refuses (bad input). Maps to HTTP 400."""


class LimitError(Exception):
    """The per-session action cap was reached. Maps to HTTP 429."""


class ExecError(Exception):
    """A desktop command failed or timed out. Maps to HTTP 500."""


class Runner(Protocol):
    def __call__(self, cmd: list[str], timeout: float) -> bytes: ...


def subprocess_runner(display: str) -> Runner:
    env = {**os.environ, "DISPLAY": display}

    def run(cmd: list[str], timeout: float) -> bytes:
        try:
            done = subprocess.run(  # noqa: S603 - argv list, never a shell
                cmd, env=env, capture_output=True, timeout=timeout, check=True
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecError(f"{cmd[0]} timed out") from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            raise ExecError(f"{cmd[0]} failed: {detail.decode(errors='replace')[:200]}") from exc
        return done.stdout

    return run


@dataclass
class Config:
    token: str
    display: str = ":99"
    screen_w: int = 1280
    screen_h: int = 800
    shot_w: int = 1280  # width of returned screenshots; height follows the aspect ratio
    max_actions: int = 200
    settle_seconds: float = 0.7
    max_wait_seconds: float = 10.0

    @property
    def shot_h(self) -> int:
        return round(self.screen_h * self.shot_w / self.screen_w)

    @property
    def scale(self) -> float:
        """Screen pixels per screenshot pixel. Model coordinates are in screenshot space."""
        return self.screen_w / self.shot_w

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("EXECUTOR_TOKEN", "")
        if len(token) < 24:
            raise SystemExit(
                "EXECUTOR_TOKEN must be set to a random value of at least 24 characters"
            )
        w, h = os.environ.get("SCREEN_RES", "1280x800x24").split("x")[:2]
        return cls(
            token=token,
            display=os.environ.get("DISPLAY", ":99"),
            screen_w=int(w),
            screen_h=int(h),
            shot_w=int(os.environ.get("SHOT_WIDTH", w)),
            max_actions=int(os.environ.get("MAX_ACTIONS", "200")),
            settle_seconds=float(os.environ.get("SETTLE_SECONDS", "0.7")),
        )


@dataclass
class Executor:
    cfg: Config
    run: Runner
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self) -> None:
        with self._lock:
            self.used = 0

    def handle(self, body: dict[str, Any]) -> dict[str, Any]:
        """Validate and perform one action, then return a fresh screenshot."""
        action = body.get("action")
        if not isinstance(action, str):
            raise ActionError("missing 'action'")
        with self._lock:  # one action at a time: the desktop has a single mouse and keyboard
            if self.used >= self.cfg.max_actions:
                raise LimitError(f"action cap of {self.cfg.max_actions} reached; reset the session")
            handler = getattr(self, f"_do_{action}", None)
            if handler is None or action.startswith("_"):
                raise ActionError(f"unknown action '{action}'")
            extra = handler(body) or {}
            self.used += 1
            if action != "screenshot":
                time.sleep(self.cfg.settle_seconds if action != "wait" else 0)
            result = {"ok": True, "action": action, "actions_used": self.used, **extra}
            result.update(self._screenshot_fields())
            return result

    # --- actions -------------------------------------------------------------------------------

    def _do_screenshot(self, _body: dict[str, Any]) -> None:
        return None

    def _do_mouse_move(self, body: dict[str, Any]) -> None:
        x, y = self._point(body)
        self._xdo("mousemove", "--sync", str(x), str(y))

    def _do_click(self, body: dict[str, Any]) -> None:
        self._click(body, repeat=1)

    def _do_double_click(self, body: dict[str, Any]) -> None:
        self._click(body, repeat=2)

    def _do_type(self, body: dict[str, Any]) -> None:
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise ActionError("'text' must be a non-empty string")
        if len(text) > MAX_TYPE_CHARS:
            raise ActionError(f"'text' longer than {MAX_TYPE_CHARS} characters")
        delay_ms = 40
        timeout = 10 + len(text) * delay_ms / 1000 * 2
        # '--' stops xdotool treating text that starts with '-' as an option.
        self._xdo("type", "--delay", str(delay_ms), "--", text, timeout=timeout)

    def _do_key(self, body: dict[str, Any]) -> None:
        self._xdo("key", "--", self._normalize_key(body.get("key")))

    def _do_scroll(self, body: dict[str, Any]) -> None:
        direction = body.get("direction")
        if direction not in SCROLL_BUTTONS:
            raise ActionError("'direction' must be up, down, left or right")
        amount = body.get("amount", 5)
        if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= 30:
            raise ActionError("'amount' must be an integer from 1 to 30")
        if "x" in body or "y" in body:
            x, y = self._point(body)
            self._xdo("mousemove", "--sync", str(x), str(y))
        self._xdo("click", "--repeat", str(amount), "--delay", "30", SCROLL_BUTTONS[direction])

    def _do_wait(self, body: dict[str, Any]) -> None:
        seconds = body.get("seconds", 1)
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            raise ActionError("'seconds' must be a number")
        time.sleep(max(0.0, min(float(seconds), self.cfg.max_wait_seconds)))

    def _do_read_clipboard(self, _body: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = self.run(["xclip", "-selection", "clipboard", "-o"], 5)
        except ExecError:
            raw = b""  # empty clipboard makes xclip exit non-zero
        return {"text": raw.decode(errors="replace")[:MAX_CLIPBOARD_CHARS]}

    # --- helpers -------------------------------------------------------------------------------

    def window_title(self) -> str:
        """Title of the focused window. For harness tests and debugging only; not a model action."""
        return (
            self.run(["xdotool", "getactivewindow", "getwindowname"], 5)
            .decode(errors="replace")
            .strip()
        )

    def _click(self, body: dict[str, Any], repeat: int) -> None:
        button = body.get("button", "left")
        if button not in MOUSE_BUTTONS:
            raise ActionError("'button' must be left, middle or right")
        x, y = self._point(body)
        self._xdo(
            "mousemove", "--sync", str(x), str(y), "click", "--repeat", str(repeat),
            "--delay", "80", MOUSE_BUTTONS[button],
        )  # fmt: skip

    def _point(self, body: dict[str, Any]) -> tuple[int, int]:
        """Validate screenshot-space coordinates and convert them to screen pixels."""
        x, y = body.get("x"), body.get("y")
        if (
            not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
            or isinstance(x, bool)
            or isinstance(y, bool)
        ):
            raise ActionError("'x' and 'y' must be numbers")
        if not (0 <= x < self.cfg.shot_w and 0 <= y < self.cfg.shot_h):
            raise ActionError(f"point outside the {self.cfg.shot_w}x{self.cfg.shot_h} screenshot")
        return round(x * self.cfg.scale), round(y * self.cfg.scale)

    @staticmethod
    def _normalize_key(raw: object) -> str:
        if not isinstance(raw, str) or not KEY_RE.match(raw):
            raise ActionError("'key' must look like 'Return' or 'ctrl+l'")
        parts = [KEY_ALIASES.get(p.lower(), p) for p in raw.split("+")]
        if any(not p for p in parts):
            raise ActionError("'key' has an empty part")
        return "+".join(parts)

    def _xdo(self, *args: str, timeout: float = 10) -> None:
        self.run(["xdotool", *args], timeout)

    def _screenshot_fields(self) -> dict[str, Any]:
        cmd = ["import", "-display", self.cfg.display, "-window", "root"]
        if self.cfg.shot_w != self.cfg.screen_w:
            cmd += ["-resize", f"{self.cfg.shot_w}x{self.cfg.shot_h}!"]
        png = self.run([*cmd, "png:-"], 15)
        return {
            "screenshot": base64.b64encode(png).decode(),
            "width": self.cfg.shot_w,
            "height": self.cfg.shot_h,
        }


# --- HTTP layer ----------------------------------------------------------------------------------


def make_handler(executor: Executor) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "executor"

        def log_message(self, fmt: str, *args: object) -> None:  # silence default stderr access log
            pass

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            sent = self.headers.get("Authorization", "")
            return hmac.compare_digest(sent.encode(), f"Bearer {executor.cfg.token}".encode())

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"ok": True})
            elif not self._authorized():
                self._send(401, {"ok": False, "error": "unauthorized"})
            elif self.path == "/window_title":
                self._guard(lambda: {"ok": True, "title": executor.window_title()})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            if not self._authorized():
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            if self.path == "/reset":
                executor.reset()
                self._send(200, {"ok": True, "actions_used": 0})
                return
            if self.path != "/action":
                self._send(404, {"ok": False, "error": "not found"})
                return
            try:
                body = self._read_body()
            except (ValueError, ActionError) as exc:
                self._send(400, {"ok": False, "error": str(exc) or "bad request"})
                return
            self._guard(lambda: executor.handle(body), action=str(body.get("action")))

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ActionError("request body missing or too large")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ActionError("body must be a JSON object")
            return body

        def _guard(self, fn: Any, action: str = "") -> None:
            try:
                result = fn()
            except ActionError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            except LimitError as exc:
                self._send(429, {"ok": False, "error": str(exc)})
            except ExecError as exc:
                log.warning("action %s failed: %s", action, exc)
                self._send(500, {"ok": False, "error": str(exc)})
            else:
                if action:
                    log.info("action=%s used=%s", action, result.get("actions_used"))
                self._send(200, result)

    return Handler


def serve(cfg: Config, host: str = "0.0.0.0", port: int = 8765) -> None:  # noqa: S104 - container only
    executor = Executor(cfg, subprocess_runner(cfg.display))
    server = ThreadingHTTPServer((host, port), make_handler(executor))
    log.info("executor listening on %s:%s (screen %sx%s)", host, port, cfg.screen_w, cfg.screen_h)
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    serve(Config.from_env(), port=int(os.environ.get("EXECUTOR_PORT", "8765")))
