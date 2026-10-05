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
_CHROME_BINARIES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")


# Text that shows a popup is covering the page, and its buttons in the order to try them (refuse before accept).
_POPUP_HINTS = re.compile(
    r"cookie|consent|your privacy|privacy (choices|preferences|settings)|"
    r"(select|choose) your (country|region|language)|country and language|"
    r"subscribe to our newsletter|sign up for our newsletter",
    re.I,
)
_POPUP_BUTTONS = (
    "Decline All", "Reject All", "Reject non-essential", "Only necessary", "Necessary only", "Deny",
    "No thanks", "Not now", "Maybe later", "Accept All", "Accept cookies", "Allow all", "I agree",
    "Agree", "Accept", "Save", "Confirm", "Continue", "Got it", "Close",
)  # fmt: skip


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
        self.use_vision = (env("DESKTOP_VISION", "1") or "1") != "0"

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
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        with contextlib.suppress(OSError):
            await asyncio.to_thread(
                subprocess.run,
                ["pkill", "-f", "--", f"--user-data-dir={self.profile}"],
                check=False,
                timeout=10,
            )
        await asyncio.sleep(3)
        await self.ensure_chrome()

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
        try:
            return await asyncio.to_thread(self._ex.handle, body)
        except (ActionError, LimitError, ExecError) as exc:
            raise DesktopError(str(exc)) from exc

    async def reset(self) -> None:
        return None

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

    async def navigate(self, target: str) -> None:
        """Address bar use, like a person: mouse to the address bar, click, type, Return. `target` is a URL
        or a search."""
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
            await self.act(action="scroll", direction="down", amount=amount, x=w // 2, y=h // 2)
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
        if len(text.strip()) < _MIN_PAGE_TEXT and shutil.which("tesseract"):
            text = await self.ocr_text() or text
        return text.strip()

    async def current_url(self) -> str:
        with contextlib.suppress(DesktopError):
            await self._run(["xclip", "-selection", "clipboard", "-i", "/dev/null"], 5)
        url = await self.clipboard_after("ctrl+l", "ctrl+c")
        await self.act(action="key", key="Escape")
        return url.strip()

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
        # Stop one pixel short: the click/scroll that follows moves there with `xdotool mousemove --sync`, which
        # waits for a movement and hangs when the pointer is already exactly on the spot.
        ty = ty - 1 if ty > 0 else 1
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
        """Glide to (x, y) and click there."""
        await self.glide(x, y)
        await self.act(action="click", x=x, y=y)

    async def see(self) -> vision.Look | None:
        """Ask the vision model what is on screen. None when it is off or unreachable (OCR takes over)."""
        if not self.use_vision:
            return None
        try:
            shot = (await self.act(action="screenshot"))["screenshot"]
            look = await vision.look(shot)
        except Exception as exc:  # model down, bad reply, timeout: never stop the run for this
            log.warning("vision unavailable", extra={"ctx": {"error": type(exc).__name__}})
            return None
        self.last_look = look
        return look

    async def _locate(self, what: str) -> tuple[int, int] | None:
        """Where the vision model sees `what` on screen, in screenshot pixels."""
        if not self.use_vision:
            return None
        try:
            shot = (await self.act(action="screenshot"))["screenshot"]
            target = await vision.locate(shot, what)
        except Exception as exc:
            log.warning("vision locate failed", extra={"ctx": {"error": type(exc).__name__}})
            return None
        if not target.found:
            return None
        w, h = self._shot_size()
        return vision.to_pixels(target.x, target.y, w, h)

    async def dismiss_popups(self) -> int:
        """Close what covers the page (cookie banner, country/language picker, newsletter box) by clicking its
        button with the mouse, the way a person would. The vision model looks at the screen; if it is
        unavailable, OCR looks for popup wording and known button labels. Up to 3 rounds."""
        if (env("DESKTOP_DISMISS_POPUPS", "1") or "1") == "0":
            return 0
        closed = 0
        for _ in range(3):
            look = await self.see()
            if look is not None:
                if not look.popup:
                    break
                w, h = self._shot_size()
                spot = vision.to_pixels(look.x, look.y, w, h)
                log.info("popup seen", extra={"ctx": {"button": look.label, "at": spot}})
            elif shutil.which("tesseract"):
                words = parse_tsv(await self._ocr("tsv"))
                if not _POPUP_HINTS.search(" ".join(w.text for w in words)):
                    break
                spot = next((p for b in _POPUP_BUTTONS if (p := find_phrase(words, b, 110))), None)
            else:
                break
            if spot is None:
                await self.act(action="key", key="Escape")  # many modals close on Escape
            else:
                await self.click_at(*spot)
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
        """Click a link the way a person would: the vision model finds it on screen, the mouse glides there and
        clicks; if the model does not see it, OCR looks for the text. `labels` are alternative link texts."""
        if spot := await self._locate(
            "a link or button reading " + " or ".join(f"'{x}'" for x in labels)
        ):
            self.last_look = None
            await self.click_at(*spot)
            await self._loaded()
            await self.dismiss_popups()
            return True
        for label in labels:
            if await self.click_text(label):
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
            ("vision click", lambda: self.click_link(title)),
            ("ocr click", lambda: self.click_text(title)),
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
