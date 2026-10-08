"""The real Xubuntu desktop as a computer-use surface: a visible Chrome window driven only with the mouse and
keyboard (xdotool), observed with screenshots, the clipboard (select all + copy, what a person would do) and OCR.

No Playwright, no Selenium, no DevTools port: Chrome is started as an ordinary desktop app, so the sites it
visits see a normal browser. The low-level actions are the same audited ones the sandbox executor uses
(sandbox/executor.py), run in-process against the local X11 display instead of over HTTP.
"""

import asyncio
import base64
import contextlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agentkit.config import env
from agentkit.log import get_logger
from sandbox.executor import ActionError, Config, ExecError, Executor, LimitError, subprocess_runner

from . import vision
from .executor_client import ExecutorError
from .loop import run_task
from .policy import Policy

log = get_logger("agent.desktop")

_MIN_PAGE_TEXT = 80  # less copied text than this: the page is canvas/image based, read it with OCR
# What Ctrl+C gives when the address bar, not the page, has the keyboard focus.
_ONLY_A_URL = re.compile(r"(https?://|www\.)\S+|[\w.-]+\.[a-z]{2,}(/\S*)?", re.I)
_CHROME_BINARIES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")


@dataclass(frozen=True)
class PopupKind:
    """One kind of popup: the text that shows it is covering the page, and its buttons in the order to try them."""

    name: str
    hints: re.Pattern[str]
    buttons: tuple[str, ...]


# Checked in this order. Region pickers keep the site we opened ("Stay on ..."), never switch to another one; ads and
# sign-up boxes are refused, never accepted. A tiny "x" is not readable by OCR: the vision model closes those.
# fmt: off
_POPUPS = (
    PopupKind(
        "cookie",
        re.compile(r"cookie|consent|your privacy|privacy (choices|preferences|settings)", re.I),
        ("Decline All", "Reject All", "Reject non-essential", "Only necessary", "Necessary only", "Deny",
         "Accept All", "Accept cookies", "Allow all", "I agree", "Agree", "Accept", "Save", "Confirm",
         "Got it", "Close"),
    ),
    PopupKind(
        "region",
        re.compile(
            r"(select|choose|change) your (country|region|language|location)|country and language|"
            r"(country|region|language) selector|based on your (location|region|country)|"
            r"you( a|')re (visiting|viewing|browsing|on) (our|the|a)\b.{0,40}\b(site|website|page)|"
            r"(looks|seems) like you( a|')re (in|visiting|from|located)|"
            r"(visit|go to|switch to|continue to) (our|the)\b.{0,30}\b(site|website)|"
            r"stay on (this|the|our)\b.{0,30}\b(site|website|page)|"
            r"(global|international|local|regional) (site|website)",
            re.I,
        ),
        ("Stay on", "Stay here", "Remain on", "No, stay", "Continue on this", "Global site", "Global website",
         "International site", "International website", "Not now", "No thanks", "Close"),
    ),
    PopupKind(
        "ad",
        re.compile(
            r"subscribe to our newsletter|sign up for our newsletter|join our (newsletter|mailing list)|"
            r"\b\d{1,2}\s?% off\b|limited[- ]time (offer|deal)|special offer|exclusive (offer|deal)|"
            r"before you (go|leave)|don'?t miss (out|this)|"
            r"\bno,? thanks\b|maybe later|i'?m not interested|no,? i don'?t want",
            re.I,
        ),
        ("No thanks", "No, thanks", "Maybe later", "Not now", "I'm not interested", "No, I", "Skip", "Dismiss",
         "Continue to site", "Continue to website", "Close"),
    ),
)
# fmt: on


def popup_on_screen(words: list["Word"]) -> tuple[str, tuple[int, int] | None] | None:
    """(kind, where its button is) for the first popup whose wording OCR sees, or None. The spot is None when the
    wording is there but none of its buttons is (an "x" icon only, an unusual label): the vision model then looks."""
    text = " ".join(w.text for w in words)
    for kind in _POPUPS:
        if kind.hints.search(text):
            return kind.name, next(
                (p for b in kind.buttons if (p := find_phrase(words, b, 110))), None
            )
    return None


class DesktopError(ExecutorError):
    """The desktop could not do what was asked (tool missing, window gone, X11 command failed)."""


@dataclass(frozen=True)
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    line: tuple[int, int, int, int]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_tsv(tsv: str) -> list[Word]:
    """tesseract TSV output -> words with boxes (screenshot pixels)."""
    words = []
    for row in tsv.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            words.append(Word(c[11].strip(), int(c[6]), int(c[7]), int(c[8]), int(c[9]),
                              (int(c[1]), int(c[2]), int(c[3]), int(c[4]))))  # fmt: skip
        except ValueError:
            continue
    return words


def find_phrase(words: list[Word], phrase: str, min_y: int = 0) -> tuple[int, int] | None:
    """Centre of the first place on screen where the start of `phrase` is written, or None.
    Matches the first few words in order on one line and tolerates one OCR misread."""
    toks = [t for t in (_norm(w) for w in phrase.split()) if t][:4]
    if not toks:
        return None
    need = len(toks) if len(toks) <= 2 else len(toks) - 1
    lines: dict[tuple[int, int, int, int], list[Word]] = {}
    for w in words:
        if w.y >= min_y:
            lines.setdefault(w.line, []).append(w)
    for ws in lines.values():
        for i in range(len(ws)):
            span = ws[i : i + len(toks)]
            hits = sum(1 for t, w in zip(toks, span, strict=False) if _norm(w.text) == t)
            if hits >= need and _norm(span[0].text) == toks[0]:
                x0, x1 = span[0].x, span[-1].x + span[-1].w
                y0 = min(w.y for w in span)
                y1 = max(w.y + w.h for w in span)
                return (x0 + x1) // 2, (y0 + y1) // 2
    return None


def _near(a: tuple[int, int], b: tuple[int, int], px: int = 15) -> bool:
    return abs(a[0] - b[0]) <= px and abs(a[1] - b[1]) <= px


def preflight() -> list[tuple[bool, str, str, bool]]:
    """(ok, check, detail, required) for everything the desktop agent needs."""
    out = []
    session = os.environ.get("XDG_SESSION_TYPE", "")
    out.append((session != "wayland", "X11 session", session or "unknown (assuming X11)", True))
    out.append(
        (
            bool(env("DISPLAY")),
            "DISPLAY set",
            env("DISPLAY") or "not set: run from the desktop",
            True,
        )
    )
    for tool, pkg in (("xdotool", "xdotool"), ("xclip", "xclip"), ("import", "imagemagick")):
        path = shutil.which(tool)
        out.append((bool(path), tool, path or f"missing: sudo apt install {pkg}", True))
    chrome = next((p for b in _CHROME_BINARIES if (p := shutil.which(b))), None)
    out.append(
        (bool(chrome), "Google Chrome", chrome or "missing: install google-chrome-stable", True)
    )
    tess = shutil.which("tesseract")
    out.append(
        (
            bool(tess),
            "tesseract (OCR)",
            tess or "missing: sudo apt install tesseract-ocr (needed to click links)",
            False,
        )
    )
    return out


class Desktop:
    """One visible Chrome window plus the mouse and keyboard to use it. Also quacks like ExecutorClient,
    so the vision step loop (agent.gui.loop.run_task) can drive it."""

    def __init__(self, run_id: str) -> None:
        self.display = env("DISPLAY", ":0") or ":0"
        self.run_id = run_id
        self.profile = Path(
            env("DESKTOP_CHROME_PROFILE", ".chrome-desktop-profile") or ""
        ).resolve()
        self.shot_dir = Path(env("DESKTOP_SHOT_DIR", "out/desktop") or "out/desktop") / run_id
        self.save_shots = (env("DESKTOP_SAVE_SHOTS", "1") or "1") != "0"
        self.page_wait = float(env("DESKTOP_PAGE_WAIT", "5") or 5)
        self.recoveries = 0
        self._runner = subprocess_runner(self.display)
        self._ex: Executor | None = None
        self._proc: subprocess.Popen | None = None
        self._shots = 0
        self.screen = (0, 0)
        self.last_look: vision.Look | None = None  # what the vision model saw on the latest page
        # DESKTOP_VISION=0 turns the vision model off. It also works on a CPU (use a small model, e.g. qwen3-vl:2b);
        # a call over the time limit twice in a row turns it off for the run and OCR takes over.
        self.use_vision = (env("DESKTOP_VISION", "1") or "1") != "0"
        # OCR first (seconds), the vision model only when OCR cannot find the popup button or the link. On a CPU one
        # vision call takes minutes. DESKTOP_VISION_FIRST=1 asks the vision model first (fast only on a GPU).
        self.vision_first = (env("DESKTOP_VISION_FIRST", "0") or "0") == "1"
        # DESKTOP_VISION_LOOK=1 also asks the vision model on pages where OCR sees no popup wording, for popups OCR
        # cannot read (an ad that is one image with an "x"). One model call per page: worth it on a GPU only.
        self.vision_look = (env("DESKTOP_VISION_LOOK", "0") or "0") == "1"
        self.vision_timeout = float(env("DESKTOP_VISION_TIMEOUT", "240") or 240)
        self._vision_fails = 0

    # --- start-up and recovery ------------------------------------------------------------------

    async def start(self) -> None:
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            raise DesktopError(
                "this session is Wayland; log in to the Xorg/X11 session (Xubuntu default)"
            )
        geo = (await self._run(["xdotool", "getdisplaygeometry"])).decode().split()
        w, h = int(geo[0]), int(geo[1])
        self.screen = (w, h)
        cfg = Config(token="local-desktop",  # noqa: S106 - in-process, no HTTP, nothing to protect
                     display=self.display, screen_w=w, screen_h=h,
                     shot_w=min(w, 1280), max_actions=10**9,
                     settle_seconds=float(env("DESKTOP_SETTLE", "0.7") or 0.7))  # fmt: skip
        self._ex = Executor(cfg, self._runner)
        await self.ensure_chrome()
        log.info("desktop ready", extra={"ctx": {"screen": f"{w}x{h}", "display": self.display}})

    async def ensure_chrome(self) -> None:
        if await self.chrome_windows():
            return
        await self._launch()
        for _ in range(40):
            await asyncio.sleep(1)
            if await self.chrome_windows():
                await asyncio.sleep(2)
                await self._arrange()
                return
        raise DesktopError("Chrome did not open a window")

    async def _launch(self) -> None:
        binary = env("CHROME_BIN") or next(
            (p for b in _CHROME_BINARIES if (p := shutil.which(b))), None
        )
        if not binary:
            raise DesktopError("Google Chrome is not installed")
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [binary, f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate", "--hide-crash-restore-bubble", "--start-maximized",
                # no system keyring: with auto-login it is locked and Chrome's "Unlock keyring" prompt takes the keys
                "--password-store=basic",
                "--new-window", "about:blank"]  # fmt: skip
        log.info("launching Chrome (visible, no automation port)")
        self._proc = subprocess.Popen(  # noqa: S603, ASYNC220 - fixed argv, no shell; returns at once
            args, env={**os.environ, "DISPLAY": self.display}, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip

    async def _arrange(self) -> None:
        """Bring the window to the front, top-left, full size, so screenshot coordinates are predictable."""
        wins = await self.chrome_windows()
        if not wins:
            return
        w, h = self.screen
        wid = wins[-1]
        for cmd in (["windowactivate", "--sync", wid], ["windowmove", wid, "0", "0"],
                    ["windowsize", wid, str(w), str(h)]):  # fmt: skip
            with contextlib.suppress(DesktopError):
                await self._run(["xdotool", *cmd])

    async def recover(self) -> None:
        """Chrome crashed or hangs: close what is left of it and open a fresh window."""
        self.recoveries += 1
        log.warning("recovering Chrome", extra={"ctx": {"count": self.recoveries}})
        await self.close_chrome()
        await asyncio.sleep(1)
        await self.ensure_chrome()

    async def close_chrome(self, wait: float = 10) -> bool:
        """Close the agent's Chrome: only the processes using its own profile (--user-data-dir), so a person's
        Chrome on the same desktop stays open. Asks politely first, kills what is left after `wait` seconds.
        Returns True if an agent Chrome was running."""
        mine = ["--", f"--user-data-dir={self.profile}"]

        async def proc(cmd: list[str]) -> int:
            try:
                done = await asyncio.to_thread(subprocess.run, cmd, check=False, timeout=10,
                                               capture_output=True)  # fmt: skip
            except (OSError, subprocess.SubprocessError):
                return 1
            return done.returncode

        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        running = await proc(["pgrep", "-f", *mine]) == 0
        if running:
            await proc(["pkill", "-f", *mine])
            for _ in range(int(wait * 2)):
                if await proc(["pgrep", "-f", *mine]) != 0:
                    break
                await asyncio.sleep(0.5)
            else:
                await proc(["pkill", "-9", "-f", *mine])
            log.info("Chrome closed")
        self._proc = None
        return running

    async def chrome_windows(self) -> list[str]:
        try:
            out = await self._run(["xdotool", "search", "--onlyvisible", "--class", "chrome"], 5)
        except DesktopError:
            return []  # xdotool exits non-zero when nothing matches
        return out.decode().split()

    # --- low-level actions ----------------------------------------------------------------------

    async def _run(self, cmd: list[str], timeout: float = 10) -> bytes:
        try:
            return await asyncio.to_thread(self._runner, cmd, timeout)
        except ExecError as exc:
            raise DesktopError(str(exc)) from exc

    async def act(self, **body):
        """One mouse/keyboard/screenshot action; the result carries a fresh base64 PNG in 'screenshot'."""
        if self._ex is None:
            raise DesktopError("desktop not started")
        if body.get("action") in ("type", "key"):
            await self._keys_to_chrome()
        try:
            return await asyncio.to_thread(self._ex.handle, body)
        except (ActionError, LimitError, ExecError) as exc:
            raise DesktopError(str(exc)) from exc

    async def reset(self) -> None:
        return None

    async def _keys_to_chrome(self) -> None:
        """Keystrokes go to the agent's Chrome only. Another window in front (an "Unlock keyring" prompt, a system
        dialog): Chrome is brought back to the front once; if it cannot be, nothing is typed."""
        wins = await self.chrome_windows()
        if not wins:
            raise DesktopError("Chrome has no window: nothing typed")
        for attempt in range(2):
            with contextlib.suppress(DesktopError):
                if (await self._run(["xdotool", "getactivewindow"], 5)).decode().strip() in wins:
                    return
            if attempt == 0:
                name = ""
                with contextlib.suppress(DesktopError):
                    active = (await self._run(["xdotool", "getactivewindow"], 5)).decode().strip()
                    name = (
                        (await self._run(["xdotool", "getwindowname", active], 5)).decode().strip()
                    )
                log.warning(
                    "another window is in front of Chrome", extra={"ctx": {"window": name[:80]}}
                )
                with contextlib.suppress(DesktopError):
                    await self._run(["xdotool", "windowactivate", "--sync", wins[-1]])
        raise DesktopError(
            "another window has the keyboard (e.g. an 'Unlock keyring' prompt): nothing typed"
        )

    async def clipboard_after(self, *keys: str) -> str:
        for k in keys:
            await self.act(action="key", key=k)
        return (await self.act(action="read_clipboard")).get("text", "")

    async def title(self) -> str:
        if self._ex is None:
            return ""
        try:
            return await asyncio.to_thread(self._ex.window_title)
        except ExecError:
            return ""

    async def shot(self, label: str) -> str:
        """Screenshot of the whole desktop; saved under out/desktop/<run>/ so a run can be reviewed."""
        b64 = (await self.act(action="screenshot"))["screenshot"]
        if self.save_shots:
            self._shots += 1
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"\W+", "-", label.lower())[:40].strip("-")
            (self.shot_dir / f"{self._shots:03d}-{name}.png").write_bytes(base64.b64decode(b64))
        return b64

    # --- browsing -------------------------------------------------------------------------------

    async def navigate(self, target: str, *, popups: bool = True) -> None:
        """Address bar use, like a person: mouse to the address bar, click, type, Return. `target` is a URL
        or a search. `popups=False` leaves what covers the page open (popup-check shows it first)."""
        await self.ensure_chrome()
        if wins := await self.chrome_windows():
            with contextlib.suppress(DesktopError):
                await self._run(["xdotool", "windowactivate", "--sync", wins[-1]])
        self.last_look = None
        await self._click_address_bar()
        await self.act(
            action="key", key="ctrl+l"
        )  # also selects the whole address, whatever the click hit
        await self.act(action="type", text=target)
        await self.act(
            action="key", key="Delete"
        )  # drop the inline autocomplete so Return opens what was typed
        await self.act(action="key", key="Return")
        await self._loaded()
        if popups:
            await self.dismiss_popups()

    async def _click_address_bar(self) -> None:
        """Move the pointer to the middle of Chrome's address bar and click it (the window is maximised)."""
        w, _ = self._shot_size()
        y = int(env("DESKTOP_OMNIBOX_Y", "88") or 88) * w // max(self.screen[0], 1)
        with contextlib.suppress(DesktopError):
            await self.click_at(w // 2, y)

    async def _loaded(self) -> None:
        await asyncio.sleep(self.page_wait)
        last = None
        for _ in range(6):  # the window title stops changing once the page has loaded
            now = await self.title()
            if now and now == last and not now.lower().startswith(("loading", "connecting")):
                return
            last = now
            await asyncio.sleep(1.5)

    async def back(self) -> None:
        await self.act(action="key", key="alt+Left")
        await self._loaded()

    async def scroll(self, times: int = 2, amount: int = 8) -> None:
        """Scroll the page with the mouse wheel, pointer in the middle of the page, pausing like a reader."""
        w, h = self._shot_size()
        await self.glide(w // 2, h // 2)
        for _ in range(times):
            # no x/y: the pointer is already there (glide), and the executor's `mousemove --sync` hangs on this
            # xdotool when the pointer is already on the spot
            await self.act(action="scroll", direction="down", amount=amount)
            await asyncio.sleep(1.2)

    def _shot_size(self) -> tuple[int, int]:
        cfg = self._ex.cfg if self._ex else None
        return (cfg.shot_w, cfg.shot_h) if cfg else (1280, 800)

    async def read_page(self) -> str:
        """The visible text of the page: select all + copy, then read the clipboard. OCR if the page has no
        selectable text. Never touches the page's DOM."""
        with contextlib.suppress(DesktopError):
            await self._run(
                ["xclip", "-selection", "clipboard", "-i", "/dev/null"], 5
            )  # empty first
        text = await self.clipboard_after("ctrl+a", "ctrl+c")
        if _ONLY_A_URL.fullmatch(text.strip()):
            # The keyboard focus was in the address bar, so Ctrl+A copied the address, not the page.
            log.info(
                "copied the address bar instead of the page; focusing the page and copying again"
            )
            await self.focus_page()
            text = await self.clipboard_after("ctrl+a", "ctrl+c")
        if len(text.strip()) < _MIN_PAGE_TEXT and shutil.which("tesseract"):
            text = await self.ocr_text() or text
        return text.strip()

    async def current_url(self) -> str:
        """The address in Chrome's address bar. Leaves the keyboard focus back on the page, so that a following
        Ctrl+A / Home / End acts on the page and not on the address bar."""
        with contextlib.suppress(DesktopError):
            await self._run(["xclip", "-selection", "clipboard", "-i", "/dev/null"], 5)
        url = await self.clipboard_after("ctrl+l", "ctrl+c")
        await self.act(action="key", key="Escape")
        await self.focus_page()
        return url.strip()

    async def focus_page(self) -> None:
        """Give the keyboard focus to the page: a click on the right edge of the window, on the scrollbar (at most it
        scrolls the page), away from links, menus and chat widgets."""
        w, h = self._shot_size()
        with contextlib.suppress(DesktopError):
            await self.click_at(w - 3, h // 2)

    async def dismiss_consent(self) -> bool:
        """A cookie banner or consent page: click the accept button if OCR can see one."""
        for label in ("Accept all", "I agree", "Allow all", "Accept", "Got it", "OK"):
            if await self.click_text(label, min_y=0):
                return True
        return False

    async def glide(self, x: int, y: int) -> None:
        """Move the pointer to (x, y), in screenshot pixels, in small eased steps like a hand, so it is seen moving."""
        shot_w, _ = self._shot_size()
        scale = max(self.screen[0], 1) / shot_w
        tx, ty = int(x * scale), int(y * scale)
        cx, cy = tx, ty
        with contextlib.suppress(DesktopError, AttributeError):
            where = (await self._run(["xdotool", "getmouselocation"])).decode()
            cx = int(re.search(r"x:(\d+)", where).group(1))  # type: ignore[union-attr]
            cy = int(re.search(r"y:(\d+)", where).group(1))  # type: ignore[union-attr]
        steps = max(1, int(env("DESKTOP_MOUSE_STEPS", "14") or 14))
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)  # ease in and out
            with contextlib.suppress(DesktopError):
                await self._run(
                    [
                        "xdotool",
                        "mousemove",
                        str(int(cx + (tx - cx) * e)),
                        str(int(cy + (ty - cy) * e)),
                    ]
                )
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)

    async def click_at(self, x: int, y: int) -> None:
        """Glide to (x, y) and click there. The pointer is already on the spot, so this clicks in place: the
        executor's click would `xdotool mousemove --sync` to where the pointer already is, which hangs."""
        await self.glide(x, y)
        await self._run(["xdotool", "click", "--delay", "80", "1"])

    async def _vision_call(self, make):
        """Run one vision-model request with a time limit. A timeout, or two other failures in a row, switch vision off
        for the run (OCR and the keyboard take over), so a slow or broken model can never stall it. None on failure."""
        try:
            shot = (await self.act(action="screenshot"))["screenshot"]
            out = await asyncio.wait_for(make(shot), self.vision_timeout)
        except Exception as exc:  # model down, bad reply, timeout: never stop the run for this
            self._vision_fails += 1
            detail = str(exc)[:160] or type(exc).__name__
            log.warning("vision unavailable", extra={"ctx": {"error": detail}})
            # One timeout already shows the model is too slow on this machine (a CPU with a thinking model);
            # other errors get a second chance.
            if isinstance(exc, TimeoutError) or self._vision_fails >= 2:
                self.use_vision = False
                log.warning(
                    "vision turned off for this run: too slow or failing, using OCR instead"
                )
            return None
        self._vision_fails = 0
        return out

    async def see(self) -> vision.Look | None:
        """Ask the vision model what is on screen. None when it is off or unreachable (OCR takes over)."""
        if not self.use_vision:
            return None
        look = await self._vision_call(vision.look)
        if look is not None:
            self.last_look = look
        return look

    async def _locate(self, what: str) -> tuple[int, int] | None:
        """Where the vision model sees `what` on screen, in screenshot pixels."""
        if not self.use_vision:
            return None
        target = await self._vision_call(lambda shot: vision.locate(shot, what))
        if target is None or not target.found:
            return None
        w, h = self._shot_size()
        return vision.to_pixels(target.x, target.y, w, h)

    async def _popup_spot(self, ask_model: bool = False) -> tuple[bool, tuple[int, int] | None]:
        """(is a popup covering the page, where to click to close it). OCR looks first: popup wording plus a known
        button label. The vision model is asked when OCR sees popup wording but no button, when OCR's button did not
        close it (`ask_model`), on every page with DESKTOP_VISION_LOOK=1, or when OCR is missing or
        DESKTOP_VISION_FIRST=1."""
        w, h = self._shot_size()
        have_ocr = bool(shutil.which("tesseract")) and not self.vision_first
        seen = None
        if have_ocr:
            seen = popup_on_screen(parse_tsv(await self._ocr("tsv")))
            if seen is None and not self.vision_look:
                return False, None
            if seen is not None and seen[1] is not None and not ask_model:
                log.info("popup seen by OCR", extra={"ctx": {"kind": seen[0]}})
                return True, seen[1]
        look = await self.see()
        if look is None:  # model off or failing: OCR's answer (no button: Escape)
            return seen is not None, seen[1] if seen else None
        log.info(
            "popup seen",
            extra={"ctx": {"popup": look.popup, "kind": look.kind, "button": look.label}},
        )
        return look.popup, vision.to_pixels(look.x, look.y, w, h) if look.popup else None

    async def dismiss_popups(self) -> int:
        """Close what covers the page (cookie banner, stay-on-this-region box, ad or newsletter box, chat prompt) by
        clicking its button with the mouse, the way a person would (one Escape when nothing is recognised). A click that leaves the popup there is not
        repeated: Escape, and the vision model chooses the next click. Up to 4 rounds."""
        if (env("DESKTOP_DISMISS_POPUPS", "1") or "1") == "0":
            return 0
        closed = 0
        last: tuple[int, int] | None = None
        ask_model = False
        for _ in range(4):
            popup, spot = await self._popup_spot(ask_model)
            if not popup:
                if not closed:
                    # An ad that is one image (no wording for OCR, no vision on a CPU) usually still closes on Escape;
                    # a page without a popup ignores it. Never click the dimmed area: it can be a link.
                    # The first Escape can go to the address bar (a suggestion list), so press it twice.
                    for _ in range(2):
                        await self.act(action="key", key="Escape")
                        await asyncio.sleep(0.7)
                break
            if spot is not None and last is not None and _near(spot, last):
                spot, ask_model = None, True  # the same click again would not close it
            if spot is None:
                await self.act(action="key", key="Escape")  # many modals close on Escape
            else:
                await self.click_at(*spot)
            last = spot
            closed += 1
            await asyncio.sleep(1.5)
        if closed:
            log.info("popup closed", extra={"ctx": {"rounds": closed}})
        return closed

    # --- seeing: OCR, clicking ------------------------------------------------------------------

    async def _ocr(self, mode: str) -> str:
        if not shutil.which("tesseract"):
            return ""
        png = base64.b64decode((await self.act(action="screenshot"))["screenshot"])
        try:
            done = await asyncio.to_thread(
                subprocess.run, ["tesseract", "stdin", "stdout", "--psm", "11", *([mode] if mode else [])],
                input=png, capture_output=True, timeout=90, check=True,
            )  # fmt: skip
        except (subprocess.SubprocessError, OSError):
            return ""
        return done.stdout.decode(errors="replace")

    async def ocr_text(self) -> str:
        return await self._ocr("")

    async def click_text(self, phrase: str, *, min_y: int = 110) -> bool:
        """Find `phrase` on screen with OCR and click it with the mouse. min_y skips Chrome's own toolbar."""
        words = parse_tsv(await self._ocr("tsv"))
        spot = find_phrase(words, phrase, min_y)
        if spot is None:
            return False
        await self.click_at(*spot)
        await self._loaded()
        return True

    async def click_link(self, *labels: str) -> bool:
        """Click a link the way a person would. OCR finds the text on screen and the mouse glides there and clicks;
        when OCR cannot find it, the vision model looks (first, with DESKTOP_VISION_FIRST=1). `labels` are alternative
        link texts."""

        async def by_ocr() -> bool:
            for label in labels:
                words = parse_tsv(await self._ocr("tsv"))
                if spot := find_phrase(words, label, 110):
                    await self.click_at(*spot)
                    return True
            return False

        async def by_vision() -> bool:
            what = "a link or button reading " + " or ".join(f"'{x}'" for x in labels)
            if spot := await self._locate(what):
                await self.click_at(*spot)
                return True
            return False

        await (
            self.dismiss_popups()
        )  # an ad or region box that opened after the page loaded would take the click
        for how in (by_vision, by_ocr) if self.vision_first else (by_ocr, by_vision):
            if await how():
                self.last_look = None
                await self._loaded()
                await self.dismiss_popups()
                return True
        return False

    async def activate_by_find(self, phrase: str) -> None:
        """Fallback for clicking: Ctrl+F the link text, close the find bar, press Return on the selected link."""
        await self.act(action="key", key="ctrl+f")
        await self.act(action="type", text=phrase[:80])
        await self.act(action="key", key="Return")
        await self.act(action="key", key="Escape")
        await self.act(action="key", key="Return")
        await self._loaded()

    async def vision_open(self, title: str) -> bool:
        """Last resort: the vision model looks at the screen and clicks the result itself."""
        goal = f"Click the search result titled '{title[:100]}' so its page opens, then finish with done."
        out = await run_task(goal, self, Policy(max_steps=8, max_seconds=150), require_email=False)  # type: ignore[arg-type]
        return out.status == "done"

    async def open_link(self, title: str, results_url: str) -> str:
        """Open the on-screen link whose text starts with `title`. The vision model finds it and the mouse clicks it; then OCR,
        then keyboard find, then the vision step loop. Returns the new page address, or "" when nothing opened."""
        attempts = (
            ("click", lambda: self.click_link(title)),
            ("find on page", lambda: self.activate_by_find(title)),
            ("vision", lambda: self.vision_open(title)),
        )
        for name, attempt in attempts:
            try:
                if await attempt() is False:
                    continue  # the link text was not found on screen
            except ExecutorError:
                log.info("open link attempt failed", extra={"ctx": {"how": name}})
                continue
            now = await self.current_url()
            if now and now != results_url:
                log.info("result opened", extra={"ctx": {"how": name}})
                return now
        return ""


async def popup_check(urls: list[str]) -> int:
    """Manual check of popup handling (`python -m agent popup-check <url>...`): open each site in the visible Chrome,
    screenshot it, close what covers it, screenshot again. Nothing is stored; the screenshots go to out/desktop/."""
    desk = Desktop("popup-check-" + time.strftime("%Y%m%d-%H%M%S"))
    desk.save_shots = True
    await desk.start()
    try:
        for url in urls:
            await desk.navigate(url, popups=False)
            await desk.shot(f"{url} before")
            rounds = await desk.dismiss_popups()
            await desk.shot(f"{url} after")
            look = desk.last_look
            seen = (
                f"vision: kind={look.kind or '-'} button={look.label or '-'}"
                if look
                else "vision: not asked"
            )
            print(f"{url}\n  rounds={rounds}  {seen}")  # noqa: T201
    finally:
        await desk.close_chrome()
    print(f"screenshots: {desk.shot_dir}")  # noqa: T201
    return 0
