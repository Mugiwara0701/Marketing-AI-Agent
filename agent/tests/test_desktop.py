"""The desktop Chrome driver (agent.gui.desktop): OCR, vision fallback, mouse movement. No real desktop."""

import asyncio

from agent.gui.desktop import find_phrase, parse_tsv


def test_find_phrase_on_ocr_output():
    tsv = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n" + "\n".join(
        f"5\t1\t2\t1\t1\t{i}\t{100 + i * 60}\t300\t50\t20\t90\t{w}"
        for i, w in enumerate(["Android", "BSP", "Outsourcing", "Partner"])
    )
    words = parse_tsv(tsv)
    assert find_phrase(words, "Android BSP Outsourcing Partner | Acme") == (215, 310)
    assert (
        find_phrase(words, "Android BSP Outsourcing Partner", min_y=400) is None
    )  # below the toolbar only
    assert find_phrase(words, "Linux kernel drivers") is None


def test_popup_hints_and_buttons():
    from agent.gui.desktop import _POPUP_BUTTONS, _POPUP_HINTS

    assert _POPUP_HINTS.search("Cookies and Data Processing We and our 12 partners")
    assert _POPUP_HINTS.search("Set your country and language")
    assert not _POPUP_HINTS.search("Senior Android engineer, AOSP bring-up, apply now")
    tsv = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n" + "".join(
        f"5\t1\t1\t1\t{ln}\t{i}\t{x}\t400\t60\t20\t90\t{t}\n"
        for ln, (x, words) in enumerate([(730, "Decline All"), (880, "Accept All")], start=1)
        for i, t in enumerate(words.split())
    )
    words = parse_tsv(tsv)
    spot = next(p for b in _POPUP_BUTTONS if (p := find_phrase(words, b, 110)))
    assert spot[0] < 800  # refuses before accepting


def test_vision_points_to_pixels():
    from agent.gui import vision

    assert vision.to_pixels(500, 500, 1280, 800) == (640, 400)
    assert vision.to_pixels(1000, 1000, 1280, 800) == (1279, 799)  # corner stays on screen
    assert vision.to_pixels(None, 10, 1280, 800) is None
    assert vision.to_pixels(900, 1100, 1280, 800) == (900, 799)  # beyond the grid: already pixels


def test_popup_closed_by_vision_click(monkeypatch):
    from agent.gui import desktop, vision

    clicks = []
    looks = iter(
        [vision.Look(popup=True, label="Decline All", x=700, y=470), vision.Look(popup=False)]
    )

    async def fake_see(self):
        self.last_look = next(looks)
        return self.last_look

    async def fake_click(self, x, y):
        clicks.append((x, y))

    monkeypatch.setenv("DESKTOP_VISION_FIRST", "1")
    monkeypatch.setattr(desktop.Desktop, "see", fake_see)
    monkeypatch.setattr(desktop.Desktop, "click_at", fake_click)
    monkeypatch.setattr(desktop.Desktop, "_shot_size", lambda self: (1280, 800))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(desktop.asyncio, "sleep", no_sleep)
    d = desktop.Desktop("t")
    assert asyncio.run(d.dismiss_popups()) == 1
    assert clicks == [(896, 376)]


def test_glide_ends_on_the_target_and_is_a_real_glide(monkeypatch):
    from agent.gui import desktop

    moves = []

    async def fake_run(self, cmd, timeout=10):
        if cmd[1] == "getmouselocation":
            return b"x:10 y:10 screen:0 window:1"
        moves.append((int(cmd[2]), int(cmd[3])))
        return b""

    async def no_sleep(_):
        return None

    monkeypatch.setattr(desktop.Desktop, "_run", fake_run)
    monkeypatch.setattr(desktop.Desktop, "_shot_size", lambda self: (1280, 800))
    monkeypatch.setattr(desktop.asyncio, "sleep", no_sleep)
    d = desktop.Desktop("t")
    d.screen = (1440, 900)
    asyncio.run(d.glide(640, 400))  # screen target (720, 450)
    assert len(moves) > 5  # a glide, not a jump
    assert moves[-1] == (720, 450)


def test_click_and_scroll_never_use_mousemove_sync(monkeypatch):
    """`xdotool mousemove --sync` to the spot the pointer is already on hangs 10 s: it killed every search."""
    from agent.gui import desktop

    cmds, bodies = [], []

    async def fake_run(self, cmd, timeout=10):
        cmds.append(cmd)
        return b"x:1 y:1"

    async def fake_act(self, **body):
        bodies.append(body)
        return {}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(desktop.Desktop, "_run", fake_run)
    monkeypatch.setattr(desktop.Desktop, "act", fake_act)
    monkeypatch.setattr(desktop.Desktop, "_shot_size", lambda self: (1280, 800))
    monkeypatch.setattr(desktop.asyncio, "sleep", no_sleep)
    d = desktop.Desktop("t")
    d.screen = (1440, 900)
    asyncio.run(d.click_at(640, 400))
    asyncio.run(d.scroll(2))
    assert not any("--sync" in c for c in cmds)
    assert any(c[:2] == ["xdotool", "click"] for c in cmds)
    scrolls = [b for b in bodies if b.get("action") == "scroll"]
    assert len(scrolls) == 2 and all("x" not in b and "y" not in b for b in scrolls)


def test_one_vision_timeout_switches_it_off_for_the_run(monkeypatch):
    from agent.gui import desktop, vision

    async def fake_act(self, **body):
        return {"screenshot": "x"}

    async def slow_look(shot):
        await asyncio.sleep(5)

    monkeypatch.setattr(desktop.Desktop, "act", fake_act)
    monkeypatch.setattr(vision, "look", slow_look)
    monkeypatch.setenv("DESKTOP_VISION", "1")
    monkeypatch.setenv("DESKTOP_VISION_TIMEOUT", "0.05")
    d = desktop.Desktop("t")
    assert d.use_vision
    assert asyncio.run(d.see()) is None
    assert not d.use_vision  # too slow once: off for the rest of the run


def test_other_vision_errors_get_a_second_chance(monkeypatch):
    from agent.gui import desktop, vision

    async def fake_act(self, **body):
        return {"screenshot": "x"}

    async def broken_look(shot):
        raise ValueError("bad reply")

    monkeypatch.setattr(desktop.Desktop, "act", fake_act)
    monkeypatch.setattr(vision, "look", broken_look)
    monkeypatch.setenv("DESKTOP_VISION", "1")
    d = desktop.Desktop("t")
    assert asyncio.run(d.see()) is None
    assert d.use_vision  # one bad reply is tolerated
    assert asyncio.run(d.see()) is None
    assert not d.use_vision  # two in a row: off


def test_vision_is_on_by_default_even_without_a_gpu(monkeypatch):
    from agent.gui import desktop

    monkeypatch.delenv("DESKTOP_VISION", raising=False)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert desktop.Desktop("t").use_vision
    monkeypatch.setenv("DESKTOP_VISION", "0")
    assert not desktop.Desktop("t").use_vision


def test_llm_min_timeout_raises_short_limits(monkeypatch):
    from agentkit import llm

    seen = []

    async def fake_post(payload, timeout):
        seen.append(timeout)
        return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    monkeypatch.setattr(llm, "_post", fake_post)
    monkeypatch.setenv("LLM_MIN_TIMEOUT", "300")
    asyncio.run(llm.complete("outreach.draft", [{"role": "user", "content": "x"}], timeout=30))
    assert seen == [300.0]


def test_popup_closed_by_ocr_without_calling_the_vision_model(monkeypatch):
    from agent.gui import desktop

    rows = ["level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext"]
    for ln, x, t in [
        (1, 100, "We use cookies"),
        (2, 730, "Decline"),
        (2, 790, "All"),
    ]:  # "Decline All" on one line
        rows.append(f"5\t1\t1\t1\t{ln}\t1\t{x}\t400\t50\t20\t90\t{t}")
    tsv = "\n".join(rows)
    clicks: list[tuple[int, int]] = []
    looks: list = []

    async def fake_ocr(self, mode):
        return tsv if clicks == [] else ""  # the popup is gone after the click

    async def fake_see(self):
        looks.append(1)

    async def fake_click(self, x, y):
        clicks.append((x, y))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(desktop.Desktop, "_ocr", fake_ocr)
    monkeypatch.setattr(desktop.Desktop, "see", fake_see)
    monkeypatch.setattr(desktop.Desktop, "click_at", fake_click)
    monkeypatch.setattr(desktop.Desktop, "_shot_size", lambda self: (1280, 800))
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop.asyncio, "sleep", no_sleep)
    monkeypatch.delenv("DESKTOP_VISION_FIRST", raising=False)
    assert asyncio.run(desktop.Desktop("t").dismiss_popups()) == 1
    assert (
        clicks and clicks[0][0] > 700 and not looks
    )  # clicked "Decline All"; the model was never asked


def test_reading_a_page_never_returns_just_the_address_bar(monkeypatch):
    """Ctrl+A with the focus in the address bar copies the URL, not the page: the page then looked empty."""
    from agent.gui import desktop

    copies = iter(
        ["https://www.statiq.in", "Building the largest network of EV chargers in India " * 5]
    )
    clicks = []

    async def fake_clip(self, *keys):
        return next(copies)

    async def fake_click(self, x, y):
        clicks.append((x, y))

    async def nothing(self, cmd, timeout=10):
        return b""

    monkeypatch.setattr(desktop.Desktop, "clipboard_after", fake_clip)
    monkeypatch.setattr(desktop.Desktop, "click_at", fake_click)
    monkeypatch.setattr(desktop.Desktop, "_run", nothing)
    monkeypatch.setattr(desktop.Desktop, "_shot_size", lambda self: (1280, 800))
    text = asyncio.run(desktop.Desktop("t").read_page())
    assert text.startswith("Building the largest network")
    assert clicks == [(1277, 400)]  # focus given back to the page, on the scrollbar edge


def test_asking_for_the_address_gives_focus_back_to_the_page(monkeypatch):
    from agent.gui import desktop

    keys: list = []
    clicks: list = []

    async def fake_clip(self, *k):
        keys.extend(k)
        return "https://acme.io/contact"

    async def fake_act(self, **body):
        keys.append(body.get("key"))
        return {}

    async def fake_click(self, x, y):
        clicks.append((x, y))

    async def nothing(self, cmd, timeout=10):
        return b""

    monkeypatch.setattr(desktop.Desktop, "clipboard_after", fake_clip)
    monkeypatch.setattr(desktop.Desktop, "act", fake_act)
    monkeypatch.setattr(desktop.Desktop, "click_at", fake_click)
    monkeypatch.setattr(desktop.Desktop, "_run", nothing)
    monkeypatch.setattr(desktop.Desktop, "_shot_size", lambda self: (1280, 800))
    assert asyncio.run(desktop.Desktop("t").current_url()) == "https://acme.io/contact"
    assert keys == ["ctrl+l", "ctrl+c", "Escape"] and clicks == [(1277, 400)]


class _Procs:
    """Stands in for subprocess.run: pgrep says an agent Chrome runs for `alive` checks, then it is gone."""

    def __init__(self, alive: int):
        self.alive = alive
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, **_k):
        import subprocess

        self.cmds.append(cmd)
        code = 0
        if cmd[0] == "pgrep":
            code = 0 if self.alive > 0 else 1
            self.alive -= 1
        return subprocess.CompletedProcess(cmd, code, b"", b"")


def test_stop_closes_only_the_agents_chrome(monkeypatch, tmp_path):
    from agent.gui import desktop

    monkeypatch.setenv("DESKTOP_CHROME_PROFILE", str(tmp_path / "agent-profile"))
    procs = _Procs(alive=2)  # running, still running once after the polite close, then gone
    monkeypatch.setattr(desktop.subprocess, "run", procs)
    assert asyncio.run(desktop.Desktop("t").close_chrome(wait=1)) is True
    mine = f"--user-data-dir={tmp_path / 'agent-profile'}"
    assert ["pkill", "-f", "--", mine] in procs.cmds
    assert not any("-9" in c for c in procs.cmds)  # it closed on its own: no force kill
    assert all(
        c[-1] == mine for c in procs.cmds
    )  # never a plain "chrome": a person's Chrome stays open


def test_a_chrome_that_will_not_close_is_killed(monkeypatch, tmp_path):
    from agent.gui import desktop

    monkeypatch.setenv("DESKTOP_CHROME_PROFILE", str(tmp_path / "p"))
    procs = _Procs(alive=10**6)
    monkeypatch.setattr(desktop.subprocess, "run", procs)
    monkeypatch.setattr(desktop.asyncio, "sleep", _no_sleep)
    assert asyncio.run(desktop.Desktop("t").close_chrome(wait=1)) is True
    assert procs.cmds[-1][:3] == ["pkill", "-9", "-f"]


def test_nothing_to_close_does_nothing(monkeypatch, tmp_path):
    from agent.gui import desktop

    monkeypatch.setenv("DESKTOP_CHROME_PROFILE", str(tmp_path / "p"))
    procs = _Procs(alive=0)
    monkeypatch.setattr(desktop.subprocess, "run", procs)
    assert asyncio.run(desktop.Desktop("t").close_chrome()) is False
    assert [c[0] for c in procs.cmds] == ["pgrep"]


async def _no_sleep(_s):
    return None


def test_lead_browser_closes_chrome_unless_asked_to_keep_it(monkeypatch):
    from agent.leadgen.browser.desktop import DesktopBrowser

    class Desk:
        closed = 0

        async def close_chrome(self):
            Desk.closed += 1
            return True

    from typing import Any, cast

    b = DesktopBrowser(cast(Any, None), [], 0, desk=cast(Any, Desk()), lookup=cast(Any, object()))
    asyncio.run(b.close())
    assert Desk.closed == 1
    monkeypatch.setenv("DESKTOP_KEEP_CHROME", "1")
    asyncio.run(b.close())
    assert Desk.closed == 1
