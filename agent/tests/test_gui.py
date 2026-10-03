import asyncio

import pytest

from agent.gui import lead, loop
from agent.gui.actions import GuiStep
from agent.gui.executor_client import ExecutorError
from agent.gui.policy import Policy


class FakeExecutor:
    """Stands in for the sandbox: records actions, serves a clipboard and a fixed screenshot."""

    def __init__(self, urls=None, page_text="", shots=None):
        self.actions: list[dict] = []
        self.urls = list(urls or ["https://acme.io/contact"])
        self.page_text = page_text
        self.shots = shots
        self._copied = ""
        self._n = 0

    async def reset(self):
        pass

    async def act(self, **body):
        self.actions.append(body)
        self._n += 1
        if body["action"] == "read_clipboard":
            return {"ok": True, "text": self._copied}
        if body.get("key") == "ctrl+c":
            # ctrl+l ... ctrl+c copies the URL; ctrl+a ... ctrl+c copies the page
            prev = [a.get("key") for a in self.actions[-3:-1]]
            self._copied = self.page_text if "ctrl+a" in prev else self.urls[0]
        shot = self.shots[self._n % len(self.shots)] if self.shots else f"shot{self._n}"
        return {"ok": True, "screenshot": shot}

    async def clipboard_after(self, *keys):
        for k in keys:
            await self.act(action="key", key=k)
        return (await self.act(action="read_clipboard"))["text"]


def scripted(*steps):
    it = iter(steps)

    async def decide(goal, history, shot):
        return next(it)

    return decide


def policy(tmp_path, **kw):
    return Policy(kill_file=tmp_path / "stop", **kw)


def run(ex, decide_fn, tmp_path, **kw):
    return asyncio.run(
        loop.run_task("goal", ex, policy(tmp_path, **kw), decide_fn, log_dir=str(tmp_path))
    )


def test_done_with_email_ends_the_run(tmp_path):
    ex = FakeExecutor()
    out = run(
        ex,
        scripted(
            GuiStep(action="click", x=10, y=20),
            GuiStep(action="done", contact_email="sales@acme.io"),
        ),
        tmp_path,
    )
    assert out.status == "done" and out.steps == 2
    assert {"action": "click", "x": 10, "y": 20} in ex.actions
    assert list(tmp_path.glob("*.jsonl"))  # audit log written


def test_done_without_email_is_a_failure(tmp_path):
    out = run(FakeExecutor(), scripted(GuiStep(action="done")), tmp_path)
    assert out.status == "failed"


def test_step_budget_stops_the_run(tmp_path):
    steps = [GuiStep(action="click", x=i, y=i) for i in range(10)]
    out = run(FakeExecutor(), scripted(*steps), tmp_path, max_steps=3)
    assert out.status == "stopped" and "step budget" in out.reason and out.steps == 3


def test_kill_switch_stops_before_any_action(tmp_path):
    (tmp_path / "stop").write_text("x")
    ex = FakeExecutor()
    out = run(ex, scripted(GuiStep(action="click", x=1, y=1)), tmp_path)
    assert out.status == "stopped" and "kill switch" in out.reason
    assert all(a["action"] != "click" for a in ex.actions)


def test_disallowed_key_never_reaches_the_desktop(tmp_path):
    ex = FakeExecutor()
    out = run(
        ex,
        scripted(GuiStep(action="key", key="ctrl+s"), GuiStep(action="fail", thought="x")),
        tmp_path,
    )
    assert out.status == "failed"
    assert not any(a.get("key") == "ctrl+s" for a in ex.actions)


def test_stuck_model_is_stopped(tmp_path):
    same = GuiStep(action="click", x=5, y=5)
    out = run(FakeExecutor(shots=["same"]), scripted(same, same, same, same), tmp_path)
    assert out.status == "stopped" and "stuck" in out.reason


def test_blocked_portal_stops_the_run(tmp_path):
    ex = FakeExecutor(urls=["https://www.linkedin.com/company/acme"])
    steps = [GuiStep(action="click", x=i, y=i) for i in range(1, 6)]
    out = run(ex, scripted(*steps), tmp_path)
    assert out.status == "stopped" and "disallowed page" in out.reason


def test_executor_error_is_reported_not_raised(tmp_path):
    class Broken(FakeExecutor):
        async def act(self, **body):
            raise ExecutorError("executor unreachable")

    out = run(Broken(), scripted(GuiStep(action="click", x=1, y=1)), tmp_path)
    assert out.status == "failed" and "unreachable" in out.reason


def test_policy_url_rules():
    assert Policy.url_allowed("https://acme.io/contact")
    assert Policy.url_allowed("chrome://newtab/")
    assert not Policy.url_allowed("file:///etc/passwd")
    assert not Policy.url_allowed("https://in.indeed.com/jobs")
    assert not Policy.url_allowed("")


def test_verify_accepts_email_on_page_and_domain():
    ex = FakeExecutor(page_text="Write to sales@acme.io or bob@gmail.com")
    _, domain, problem = asyncio.run(lead.verify(ex, "sales@acme.io"))
    assert problem == "" and domain == "acme.io"


def test_verify_rejects_email_not_on_page():
    ex = FakeExecutor(page_text="Contact us via the form")
    assert asyncio.run(lead.verify(ex, "ceo@acme.io"))[2]


def test_verify_rejects_email_on_other_domain():
    ex = FakeExecutor(page_text="mail evil@other.com")
    assert asyncio.run(lead.verify(ex, "evil@other.com"))[2]


def test_verify_rejects_non_company_page():
    ex = FakeExecutor(urls=["https://github.com/acme"], page_text="a@github.com")
    assert asyncio.run(lead.verify(ex, "a@github.com"))[2]


@pytest.mark.parametrize("status", ["stopped", "failed"])
def test_find_company_reports_unfinished_runs(monkeypatch, status):
    async def fake_run(goal, ex, policy=None):
        return loop.Outcome(status, "why", 3, None, "r1")

    monkeypatch.setattr(lead, "run_task", fake_run)
    found = asyncio.run(lead.find_company("Acme", FakeExecutor()))
    assert not found.ok and status in found.message


def test_model_env_override_wins_over_routing(monkeypatch):
    from agentkit import config, llm

    monkeypatch.setenv("ROUTING_CONFIG", "config/routing.yaml")
    config.load_routing.cache_clear()
    assert llm.model_for("gui.step") == "vlm"  # config/routing.yaml
    monkeypatch.setenv("MODEL_GUI_STEP", "qwen3-vl:8b")
    assert llm.model_for("gui.step") == "qwen3-vl:8b"
    assert llm.model_for("outreach.draft") == "agent-dev"  # other tasks untouched
    config.load_routing.cache_clear()


def test_find_many_finds_all_before_drafting_any(monkeypatch):
    """One model swap per batch: every vision pass happens before the first text-model call."""
    order = []

    async def fake_find(name, ex=None, policy=None):
        order.append(f"find:{name}")
        return lead.Found(True, "ok", f"{name.lower()}.io", f"a@{name.lower()}.io")

    async def fake_save(name, found):
        order.append(f"save:{name}")
        return "posted"

    monkeypatch.setattr(lead, "find_company", fake_find)
    monkeypatch.setattr(lead, "save_and_post", fake_save)
    out = asyncio.run(lead.find_many(["A", "B", "C"], FakeExecutor()))
    assert order == ["find:A", "find:B", "find:C", "save:A", "save:B", "save:C"]
    assert [r for _, _, r in out] == ["posted"] * 3


def test_find_many_skips_saving_unverified_contacts(monkeypatch):
    async def fake_find(name, ex=None, policy=None):
        return lead.Found(False, "stopped: why")

    async def fake_save(name, found):
        raise AssertionError("must not save")

    monkeypatch.setattr(lead, "find_company", fake_find)
    monkeypatch.setattr(lead, "save_and_post", fake_save)
    out = asyncio.run(lead.find_many(["A"], FakeExecutor()))
    assert out[0][2] == "stopped: why"


def test_spike_counts_passes(monkeypatch, capsys):
    from agent.gui import spike

    async def fake_run(goal, ex, policy=None, decide_fn=None, **kw):
        return loop.Outcome("done", "ok", 3, GuiStep(action="done"), "r")

    async def fake_url(ex):
        return "https://example.com/"

    monkeypatch.setattr(spike, "run_task", fake_run)
    monkeypatch.setattr(spike, "current_url", fake_url)
    assert asyncio.run(spike.run(1, FakeExecutor())) == 0  # first task expects example.com
    assert "1/1 passed" in capsys.readouterr().out
