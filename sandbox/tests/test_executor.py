import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from sandbox import executor as ex

TOKEN = "t" * 32


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.fail_on: str | None = None

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        if self.fail_on == cmd[0]:
            raise ex.ExecError(f"{cmd[0]} failed")
        if cmd[0] == "import":
            return b"\x89PNG-fake"
        if cmd[0] == "xclip":
            return b"copied text"
        if cmd[0] == "xdotool" and cmd[1] == "getactivewindow":
            return b"title - Google Chrome\n"
        return b""


def make(**cfg):
    runner = FakeRunner()
    config = ex.Config(token=TOKEN, settle_seconds=0, **cfg)
    return ex.Executor(config, runner), runner


def xdotool_calls(runner):
    return [c[1:] for c in runner.calls if c[0] == "xdotool"]


def test_click_moves_then_clicks_and_returns_screenshot():
    e, runner = make()
    out = e.handle({"action": "click", "x": 640, "y": 400})
    assert xdotool_calls(runner) == [
        ["mousemove", "--sync", "640", "400", "click", "--repeat", "1", "--delay", "80", "1"]
    ]
    assert base64.b64decode(out["screenshot"]) == b"\x89PNG-fake"
    assert (out["width"], out["height"], out["actions_used"]) == (1280, 800, 1)


def test_double_and_right_click():
    e, runner = make()
    e.handle({"action": "double_click", "x": 1, "y": 2})
    e.handle({"action": "click", "x": 1, "y": 2, "button": "right"})
    calls = xdotool_calls(runner)
    assert calls[0][6] == "2" and calls[0][-1] == "1"
    assert calls[1][-1] == "3"


def test_coordinates_are_scaled_from_screenshot_space():
    e, runner = make(shot_w=640)  # screenshots are half size
    out = e.handle({"action": "click", "x": 100, "y": 50})
    assert xdotool_calls(runner)[0][2:4] == ["200", "100"]
    assert (out["width"], out["height"]) == (640, 400)
    shot_cmd = next(c for c in runner.calls if c[0] == "import")
    assert "640x400!" in shot_cmd


@pytest.mark.parametrize(
    "body",
    [
        {"action": "click", "x": 1280, "y": 0},
        {"action": "click", "x": -1, "y": 0},
        {"action": "click", "x": "1", "y": 0},
        {"action": "click", "x": True, "y": 0},
        {"action": "click", "x": 1, "y": 1, "button": "side"},
        {"action": "type", "text": ""},
        {"action": "type", "text": "a" * 2001},
        {"action": "key", "key": "ctrl+l; rm -rf /"},
        {"action": "key", "key": "ctrl++"},
        {"action": "scroll", "direction": "sideways"},
        {"action": "scroll", "direction": "down", "amount": 31},
        {"action": "scroll", "direction": "down", "amount": True},
        {"action": "shell", "cmd": "ls"},
        {"action": "_do_click"},
        {},
    ],
)
def test_bad_requests_are_rejected_without_touching_the_desktop(body):
    e, runner = make()
    with pytest.raises(ex.ActionError):
        e.handle(body)
    assert runner.calls == [] and e.used == 0


def test_type_passes_text_as_one_argument_after_double_dash():
    e, runner = make()
    e.handle({"action": "type", "text": "--help; $(id)"})
    assert xdotool_calls(runner)[0] == ["type", "--delay", "40", "--", "--help; $(id)"]


def test_key_aliases():
    e, runner = make()
    e.handle({"action": "key", "key": "enter"})
    e.handle({"action": "key", "key": "Ctrl+L"})
    assert xdotool_calls(runner) == [["key", "--", "Return"], ["key", "--", "ctrl+L"]]


def test_scroll_uses_wheel_buttons_and_optional_position():
    e, runner = make()
    e.handle({"action": "scroll", "direction": "down", "amount": 3, "x": 10, "y": 20})
    assert xdotool_calls(runner) == [
        ["mousemove", "--sync", "10", "20"],
        ["click", "--repeat", "3", "--delay", "30", "5"],
    ]


def test_wait_is_capped():
    e, _ = make(max_wait_seconds=0)
    assert e.handle({"action": "wait", "seconds": 999})["ok"]


def test_read_clipboard_and_empty_clipboard():
    e, runner = make()
    assert e.handle({"action": "read_clipboard"})["text"] == "copied text"
    runner.fail_on = "xclip"
    assert e.handle({"action": "read_clipboard"})["text"] == ""


def test_action_cap_and_reset():
    e, _ = make(max_actions=2)
    e.handle({"action": "screenshot"})
    e.handle({"action": "screenshot"})
    with pytest.raises(ex.LimitError):
        e.handle({"action": "screenshot"})
    e.reset()
    assert e.handle({"action": "screenshot"})["actions_used"] == 1


def test_failed_command_does_not_count_as_used():
    e, runner = make()
    runner.fail_on = "xdotool"
    with pytest.raises(ex.ExecError):
        e.handle({"action": "click", "x": 1, "y": 1})
    assert e.used == 0


# --- HTTP layer ------------------------------------------------------------------------------


@pytest.fixture
def server():
    e, runner = make(max_actions=4)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ex.make_handler(e))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", e, runner
    srv.shutdown()
    srv.server_close()


def call(url, path, body=None, token=TOKEN, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310
        url + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_http_health_needs_no_token_but_everything_else_does(server):
    url, _, _ = server
    assert call(url, "/health", token=None)[0] == 200
    assert call(url, "/action", {"action": "screenshot"}, token=None)[0] == 401
    assert call(url, "/action", {"action": "screenshot"}, token="wrong")[0] == 401  # noqa: S106
    assert call(url, "/window_title", token=None)[0] == 401
    assert call(url, "/reset", {}, token=None)[0] == 401


def test_http_action_errors_map_to_status_codes(server):
    url, _, runner = server
    status, out = call(url, "/action", {"action": "click", "x": 9999, "y": 0})
    assert status == 400 and not out["ok"]
    assert call(url, "/action", {"action": "nope"})[0] == 400
    assert call(url, "/nowhere", {})[0] == 404
    runner.fail_on = "import"
    assert call(url, "/action", {"action": "screenshot"})[0] == 500  # still counted: it ran
    runner.fail_on = None
    for _ in range(3):
        assert call(url, "/action", {"action": "screenshot"})[0] == 200
    assert call(url, "/action", {"action": "screenshot"})[0] == 429
    assert call(url, "/reset", {})[0] == 200
    assert call(url, "/action", {"action": "screenshot"})[0] == 200


def test_http_rejects_non_object_and_garbage_bodies(server):
    url, _, _ = server
    assert call(url, "/action", [1, 2])[0] == 400
    req = urllib.request.Request(  # noqa: S310
        url + "/action",
        data=b"{not json",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310
    assert err.value.code == 400


def test_http_window_title(server):
    url, _, _ = server
    assert call(url, "/window_title") == (200, {"ok": True, "title": "title - Google Chrome"})


def test_config_requires_a_long_token(monkeypatch):
    monkeypatch.setenv("EXECUTOR_TOKEN", "short")
    with pytest.raises(SystemExit):
        ex.Config.from_env()
