"""The link to the Campaign Agent: pickup with a cache and a fallback, the shape check and merge, the event outbox and
its sender. The Campaign Agent is a fake HTTP transport; nothing touches the network or a database."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from agent import campaign_link as cl

BASE = {
    "search": {
        "queries_per_run": 16,
        "capabilities": ["AOSP customization"],
        "use_job_feeds": False,
        "query_suffix": "",
    },
    "project_context": {},
    "qualification": {
        "min_qualify": 50,
        "min_draft": 60,
        "accept_service_companies": False,
        "min_confidence": 0.5,
    },
    "limits": {"max_new_leads_per_run": 10, "host_delay_seconds": 2.0},
    "contacts": {"max_pages": 4},
    "exclude_terms": ["Manufacturer"],
    "exclude_companies": [],
    "exclude_countries": [],
    "exclude_tlds": [".de"],
    "desktop_engines": ["default"],
}
OVERLAY = """\
config_version: "1.0"
project_context:
  name: Infotainment
  industries: [automotive]
search:
  queries_per_run: 12
qualification:
  min_qualify: 55
exclude_terms: [manufacturer, price]
"""
CID = "11111111-1111-1111-1111-111111111111"


def campaign(text: str = OVERLAY, version: int = 1, sha: str | None = None) -> cl.Campaign:
    return cl.Campaign(
        campaign_id=CID,
        version=version,
        sha256=sha or hashlib.sha256(text.encode()).hexdigest(),
        contract_version="1.0",
        overlay_yaml=text,
        tag=f"{CID}.{version}.{hashlib.sha256(text.encode()).hexdigest()[:16]}",
    )


@pytest.fixture
def cfg(tmp_path: Path) -> cl.LinkConfig:
    return cl.LinkConfig(
        url="http://campaign-agent:8787/ma/v1",
        token="t" * 40,
        cache_dir=tmp_path / "campaign",
        send_every_s=0.01,
    )


class FakeCA:
    """A Campaign Agent: serves `live`, answers events, and can be switched off."""

    def __init__(self) -> None:
        self.live: cl.Campaign | None = None
        self.down = False
        self.status = 200
        self.received: list[dict] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("no route to host")
        if request.headers.get("authorization") != "Bearer " + "t" * 40:
            return httpx.Response(401)
        if request.url.path.endswith("/campaign/active"):
            tag = self.live.tag if self.live else "none"
            etag = {"ETag": f'"{tag}"'}
            if request.headers.get("if-none-match") == f'"{tag}"':
                return httpx.Response(304, headers=etag)
            if self.live is None:
                return httpx.Response(204, headers=etag)
            return httpx.Response(
                200, json={k: v for k, v in self.live.as_json().items() if k != "tag"}, headers=etag
            )
        if request.url.path.endswith("/events"):
            if self.status != 200:
                return httpx.Response(self.status)
            events = json.loads(request.content)["events"]
            self.received += events
            return httpx.Response(
                200, json={"accepted": [e["event_id"] for e in events], "rejected": []}
            )
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def pick_up(ca: FakeCA, cfg: cl.LinkConfig, base: dict | None = None) -> dict:
    async def go() -> dict:
        async with ca.client() as client:
            return await cl.apply_campaign(base or BASE, cfg=cfg, client=client)

    return asyncio.run(go())


def queued(cfg: cl.LinkConfig) -> list[dict]:
    return cl.Outbox(cfg.cache_dir / "outbox.jsonl").pending(1000)


# --- settings ----------------------------------------------------------------------------------------------------------


def test_the_link_is_off_without_url_and_token(monkeypatch) -> None:
    monkeypatch.delenv("MA_LINK_URL", raising=False)
    monkeypatch.setenv("MA_LINK_TOKEN", "x" * 40)
    assert cl.link_config() is None
    monkeypatch.setenv("MA_LINK_URL", "http://campaign-agent:8787/ma/v1/")
    cfg = cl.link_config()
    assert cfg is not None and cfg.url == "http://campaign-agent:8787/ma/v1"  # no trailing slash


def test_with_no_link_the_base_is_returned_untouched_and_nothing_is_queued(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("MA_LINK_URL", raising=False)
    assert asyncio.run(cl.apply_campaign(BASE)) is BASE
    cl.emit("blocker", "error", "MA_X", "nothing happens")  # no link: no outbox, no error


# --- the shape check and the merge -------------------------------------------------------------------------------------


def test_a_good_overlay_passes_and_merges_like_the_campaign_agent_does() -> None:
    overlay = cl.parse_overlay(OVERLAY)
    cl.check_overlay(overlay, BASE)
    merged = cl.merge_overlay(BASE, overlay)
    assert merged["search"]["queries_per_run"] == 12 and merged["search"]["capabilities"] == [
        "AOSP customization"
    ]
    assert (
        merged["qualification"]["min_qualify"] == 55 and merged["qualification"]["min_draft"] == 60
    )
    assert merged["project_context"] == {"name": "Infotainment", "industries": ["automotive"]}
    # exclusions are added to the base, never replace it; "manufacturer" is already there (case-insensitive)
    assert merged["exclude_terms"] == ["Manufacturer", "price"]
    assert merged["exclude_tlds"] == [".de"] and merged["contacts"] == BASE["contacts"]
    assert (
        BASE["search"]["queries_per_run"] == 16 and BASE["project_context"] == {}  # type: ignore[index]
    )  # the base is not changed


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('config_version: "9.9"', "config_version"),
        ("project_context: {}", "config_version"),
        (
            'config_version: "1.0"\ncontacts: {max_pages: 99}',
            "contacts: a campaign may not set this key",
        ),
        ('config_version: "1.0"\ndesktop_engines: [x]', "desktop_engines"),
        (
            'config_version: "1.0"\napproval: {required: false}',
            "approval: a campaign may not set this key",
        ),
        ('config_version: "1.0"\nsearch: {min_qualfy: 70}', "search.min_qualfy: unknown key"),
        (
            'config_version: "1.0"\nqualification: {min_qualify: high}',
            "qualification.min_qualify: wrong type",
        ),
        ('config_version: "1.0"\nqualification: {accept_service_companies: 1}', "wrong type"),
        ('config_version: "1.0"\nsearch: {queries_per_run: true}', "wrong type"),
        ('config_version: "1.0"\nsearch: {capabilities: AOSP}', "wrong type"),
        ('config_version: "1.0"\nsearch: [a]', "search: must be a mapping"),
        (
            'config_version: "1.0"\nproject_context: {revenue: 5}',
            "project_context.revenue: unknown key",
        ),
        ('config_version: "1.0"\nproject_context: {products: [1, 2]}', "wrong type"),
        ('config_version: "1.0"\nexclude_terms: price', "exclude_terms: must be a list"),
        ("- a\n- b", "must be a YAML mapping"),
        ("key: [unclosed", "not valid YAML"),
    ],
)
def test_a_bad_overlay_is_refused_with_one_sentence(text: str, message: str) -> None:
    with pytest.raises(cl.OverlayError, match=message):
        cl.check_overlay(cl.parse_overlay(text), BASE)


def test_a_yaml_tag_cannot_run_code() -> None:
    with pytest.raises(cl.OverlayError):
        cl.check_overlay(
            cl.parse_overlay('config_version: !!python/object/apply:os.system ["true"]'), BASE
        )


def test_a_float_setting_accepts_an_integer_but_not_a_flag() -> None:
    cl.check_overlay(
        cl.parse_overlay('config_version: "1.0"\nlimits: {host_delay_seconds: 3}'), BASE
    )
    with pytest.raises(cl.OverlayError):
        cl.check_overlay(
            cl.parse_overlay('config_version: "1.0"\nlimits: {host_delay_seconds: true}'), BASE
        )


# --- pickup ------------------------------------------------------------------------------------------------------------


def test_a_published_campaign_is_applied_cached_and_reported(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    merged = pick_up(ca, cfg)
    assert merged["search"]["queries_per_run"] == 12
    assert cl.load_cache(cfg) == ca.live  # cached, whole
    codes = [(e["kind"], e["code"]) for e in queued(cfg)]
    assert ("applied", "MA_CAMPAIGN_APPLIED") in codes


def test_an_unchanged_campaign_uses_the_cache_and_sends_the_known_tag(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    pick_up(ca, cfg)
    ca.requests.clear()
    merged = pick_up(ca, cfg)
    assert merged["search"]["queries_per_run"] == 12
    assert ca.requests[0].headers["if-none-match"] == f'"{ca.live.tag}"'


def test_a_new_version_replaces_the_cached_one(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    pick_up(ca, cfg)
    ca.live = campaign(OVERLAY.replace("queries_per_run: 12", "queries_per_run: 9"), version=2)
    assert pick_up(ca, cfg)["search"]["queries_per_run"] == 9
    cached = cl.load_cache(cfg)
    assert cached is not None and cached.version == 2


def test_nothing_live_runs_the_base_and_forgets_the_cache(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    pick_up(ca, cfg)
    ca.live = None
    assert pick_up(ca, cfg) is BASE
    assert cl.load_cache(cfg) is None
    ca.down = True  # the CA stopping a campaign must not come back from the cache when the link later fails
    assert pick_up(ca, cfg) is BASE


def test_when_the_ca_is_down_the_last_good_campaign_keeps_running(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    pick_up(ca, cfg)
    ca.down = True
    assert pick_up(ca, cfg)["search"]["queries_per_run"] == 12
    blockers = [e for e in queued(cfg) if e["kind"] == "blocker"]
    assert [b["code"] for b in blockers] == ["MA_PICKUP_FAILED"] and blockers[0][
        "dedupe_key"
    ] == "campaign-pickup"


def test_when_the_ca_is_down_and_nothing_is_cached_the_base_runs(cfg) -> None:
    ca = FakeCA()
    ca.down = True
    assert pick_up(ca, cfg) is BASE


def test_the_pickup_blocker_is_closed_when_the_ca_comes_back_and_only_once(cfg) -> None:
    ca = FakeCA()
    ca.down = True
    pick_up(ca, cfg)
    pick_up(ca, cfg)
    ca.down = False
    pick_up(ca, cfg)
    pick_up(ca, cfg)
    resolved = [
        e for e in queued(cfg) if e["kind"] == "resolved" and e["dedupe_key"] == "campaign-pickup"
    ]
    assert len(resolved) == 1


def test_a_refused_token_runs_the_last_good_campaign_and_queues_nothing(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    pick_up(ca, cfg)
    before = len(queued(cfg))
    wrong = cl.LinkConfig(url=cfg.url, token="w" * 40, cache_dir=cfg.cache_dir)
    assert pick_up(ca, wrong)["search"]["queries_per_run"] == 12
    assert (
        len(queued(wrong)) == before
    )  # an event could not be delivered over a link that refuses us; Slack is told


def test_a_text_that_does_not_match_its_hash_is_refused_and_the_old_campaign_stays(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    pick_up(ca, cfg)
    tampered = campaign(
        OVERLAY.replace("queries_per_run: 12", "queries_per_run: 500"),
        version=2,
        sha=hashlib.sha256(b"other").hexdigest(),
    )
    ca.live = tampered
    merged = pick_up(ca, cfg)
    assert merged["search"]["queries_per_run"] == 12  # the last good one
    cached = cl.load_cache(cfg)
    assert cached is not None and cached.version == 1
    codes = {e["code"] for e in queued(cfg) if e["kind"] in ("rejected", "blocker")}
    assert "MA_CAMPAIGN_HASH_MISMATCH" in codes or "MA_CAMPAIGN_REJECTED" in codes


def test_a_campaign_the_loader_refuses_is_not_run_and_the_reason_is_sent(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign('config_version: "1.0"\nsearch: {min_qualfy: 70}\n')
    assert pick_up(ca, cfg) is BASE  # nothing was good before, so the base
    assert cl.load_cache(cfg) is None
    events = queued(cfg)
    assert {e["kind"] for e in events} == {"rejected", "blocker"}
    assert "search.min_qualfy: unknown key" in events[0]["message"]
    assert events[0]["campaign_id"] == CID and events[0]["version"] == 1


def test_a_later_good_campaign_closes_the_rejection(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign('config_version: "1.0"\nsearch: {min_qualfy: 70}\n')
    pick_up(ca, cfg)
    ca.live = campaign(OVERLAY, version=2)
    pick_up(ca, cfg)
    resolved = [e for e in queued(cfg) if e["kind"] == "resolved"]
    assert any(e["dedupe_key"] == "campaign-rejected" for e in resolved)


def test_a_damaged_cache_counts_as_no_cache(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    pick_up(ca, cfg)
    cache = cfg.cache_dir / "active.json"
    data = json.loads(cache.read_text())
    data["overlay_yaml"] = data["overlay_yaml"].replace("12", "13")  # the hash no longer matches
    cache.write_text(json.dumps(data))
    assert cl.load_cache(cfg) is None
    cache.write_text("{not json")
    assert cl.load_cache(cfg) is None


def test_a_campaign_can_never_touch_the_email_gate(cfg) -> None:
    for key in ("approval", "email", "sending", "slack", "email_approval"):
        with pytest.raises(cl.OverlayError):
            cl.check_overlay(
                cl.parse_overlay(f'config_version: "1.0"\n{key}: {{required: false}}'), BASE
            )


# --- the outbox and the sender -----------------------------------------------------------------------------------------


def test_events_are_queued_with_an_id_a_time_and_a_short_message(cfg) -> None:
    cl.emit(
        "blocker",
        "error",
        "MA_CHROME_FAILED",
        "Chrome  did\nnot start " + "x" * 400,
        dedupe_key="chrome",
        details={"tries": 3},
        cfg=cfg,
    )
    [e] = queued(cfg)
    assert (
        e["code"] == "MA_CHROME_FAILED"
        and e["details"] == {"tries": 3}
        and len(e["message"]) <= 300
    )
    assert (
        "\n" not in e["message"]
        and len(e["event_id"]) == 36
        and e["occurred_at"].endswith("+00:00")
    )


def test_an_unknown_kind_or_severity_is_dropped_not_sent(cfg) -> None:
    cl.emit("command", "error", "MA_X", "m", cfg=cfg)
    cl.emit("info", "fatal", "MA_X", "m", cfg=cfg)
    assert queued(cfg) == []


def test_a_full_outbox_drops_info_first_and_never_an_open_blocker(tmp_path) -> None:
    box = cl.Outbox(tmp_path / "o.jsonl", limit=3)
    box.add({"event_id": "b1", "kind": "blocker"})
    box.add({"event_id": "i1", "kind": "info"})
    box.add({"event_id": "i2", "kind": "info"})
    box.add({"event_id": "b2", "kind": "blocker"})
    assert [e["event_id"] for e in box.pending()] == ["b1", "i2", "b2"]
    box.add({"event_id": "b3", "kind": "blocker"})
    assert [e["event_id"] for e in box.pending()] == ["b1", "b2", "b3"]
    box.add({"event_id": "b4", "kind": "blocker"})  # only blockers left: the oldest goes
    assert [e["event_id"] for e in box.pending()] == ["b2", "b3", "b4"]


def test_events_are_sent_and_leave_the_outbox_only_when_accepted(cfg) -> None:
    ca = FakeCA()
    cl.emit("blocker", "error", "MA_LLM_DOWN", "Ollama does not answer", cfg=cfg)
    ca.down = True

    async def go(n: int) -> int:
        async with ca.client() as client:
            return await cl.send_pending(cfg, client)

    with pytest.raises(cl.LinkError):
        asyncio.run(go(1))
    assert len(queued(cfg)) == 1  # nothing lost while the CA is down
    ca.down = False
    assert asyncio.run(go(2)) == 1
    assert queued(cfg) == [] and [e["code"] for e in ca.received] == ["MA_LLM_DOWN"]
    assert asyncio.run(go(3)) == 0


def test_a_batch_the_ca_calls_invalid_is_dropped_not_sent_forever(cfg) -> None:
    ca = FakeCA()
    ca.status = 422
    cl.emit("info", "info", "MA_NOTE", "hello", cfg=cfg)

    async def go() -> int:
        async with ca.client() as client:
            return await cl.send_pending(cfg, client)

    assert asyncio.run(go()) == 1 and queued(cfg) == []


def test_a_server_error_keeps_the_events(cfg) -> None:
    ca = FakeCA()
    ca.status = 503
    cl.emit("info", "info", "MA_NOTE", "hello", cfg=cfg)

    async def go() -> int:
        async with ca.client() as client:
            return await cl.send_pending(cfg, client)

    with pytest.raises(cl.LinkError):
        asyncio.run(go())
    assert len(queued(cfg)) == 1


def test_the_sender_loop_delivers_after_an_outage_and_stops_on_request(cfg, monkeypatch) -> None:
    ca = FakeCA()
    ca.down = True
    cl.emit("blocker", "warning", "MA_SEARCH_BLOCKED", "captcha", dedupe_key="search", cfg=cfg)
    real_client = httpx.AsyncClient

    def make_client(*_a, **_k):  # every client the module opens talks to the fake CA
        return real_client(transport=httpx.MockTransport(ca.handler))

    monkeypatch.setattr(cl.httpx, "AsyncClient", make_client)
    monkeypatch.setattr(cl.random, "uniform", lambda a, b: 0.01)

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(cl.run_sender(stop, cfg))
        await asyncio.sleep(0.1)
        assert len(queued(cfg)) == 1
        ca.down = False
        for _ in range(100):
            if not queued(cfg):
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert queued(cfg) == [] and [e["code"] for e in ca.received] == ["MA_SEARCH_BLOCKED"]


def test_a_failed_pass_is_reported_and_a_good_one_closes_it(monkeypatch, tmp_path) -> None:
    from agent import control

    monkeypatch.setenv("MA_LINK_URL", "http://campaign-agent:8787/ma/v1")
    monkeypatch.setenv("MA_LINK_TOKEN", "t" * 40)
    monkeypatch.setenv("MA_LINK_CACHE_DIR", str(tmp_path / "c"))
    pipeline = control.Pipeline(rest_minutes=30)
    pipeline._finish(
        {"started_at": "t0", "outcome": "failed", "result": {"leads": {"error": "boom"}}}
    )
    pipeline._finish({"started_at": "t1", "outcome": "stopped"})
    pipeline._finish({"started_at": "t2", "outcome": "succeeded", "result": {}})
    box = cl.Outbox(tmp_path / "c" / "outbox.jsonl").pending()
    assert [(e["kind"], e["code"]) for e in box] == [
        ("info", "MA_PASS_FINISHED"),
        ("blocker", "MA_PASS_FAILED"),
        ("info", "MA_PASS_FINISHED"),
        ("info", "MA_PASS_FINISHED"),
        ("resolved", "MA_PASS_FAILED"),
    ]
    assert [e["dedupe_key"] for e in box if e["kind"] in ("blocker", "resolved")] == [
        "pass-failed"
    ] * 2
    assert "leads step failed" in box[1]["message"]


def test_a_new_pass_closes_the_failed_pass_alert_at_once(monkeypatch, tmp_path) -> None:
    from agent import control

    monkeypatch.setenv("MA_LINK_URL", "http://campaign-agent:8787/ma/v1")
    monkeypatch.setenv("MA_LINK_TOKEN", "t" * 40)
    monkeypatch.setenv("MA_LINK_CACHE_DIR", str(tmp_path / "c"))
    pipeline = control.Pipeline(rest_minutes=30)
    pipeline._finish(
        {"started_at": "t0", "outcome": "failed", "result": {"leads": {"error": "boom"}}}
    )
    pipeline._announce_pass("t1")
    box = cl.Outbox(tmp_path / "c" / "outbox.jsonl").pending()
    assert [(e["kind"], e["code"]) for e in box][-2:] == [
        ("info", "MA_PASS_STARTED"),
        ("resolved", "MA_PASS_FAILED"),
    ]
    assert box[-1]["dedupe_key"] == "pass-failed"


def test_a_failing_inbox_is_a_blocker_and_a_working_one_closes_it(monkeypatch, tmp_path) -> None:
    import asyncio

    from agent import supervisor

    monkeypatch.setenv("MA_LINK_URL", "http://campaign-agent:8787/ma/v1")
    monkeypatch.setenv("MA_LINK_TOKEN", "t" * 40)
    monkeypatch.setenv("MA_LINK_CACHE_DIR", str(tmp_path / "c"))

    class GmailAuthError(Exception):
        pass

    calls = iter([GmailAuthError("token"), None])

    async def job():
        outcome = next(calls, None)
        if outcome:
            raise outcome

    async def run() -> None:
        stop = asyncio.Event()

        task = asyncio.create_task(supervisor._loop("inbox", 0.01, job, stop))
        await asyncio.sleep(0.5)
        stop.set()
        await task

    asyncio.run(run())
    box = cl.Outbox(tmp_path / "c" / "outbox.jsonl").pending()
    assert [(e["kind"], e["code"], e["dedupe_key"]) for e in box][:2] == [
        ("blocker", "MA_GMAIL_AUTH", "gmail-auth"),
        ("resolved", "MA_GMAIL_AUTH", "gmail-auth"),
    ]
    assert all("token" not in e["message"] for e in box)


# --- the watcher: an approval reaches the agent without waiting for a pass ---------------------------------------------


def _watch(
    ca: FakeCA, cfg: cl.LinkConfig, monkeypatch, rounds: int, between=lambda i: None
) -> None:
    """Run the watcher for `rounds` checks against the fake Campaign Agent."""
    monkeypatch.setattr(cl, "link_config", lambda: cfg)
    real = cl.apply_campaign
    calls = {"n": 0}

    async def go() -> None:
        stop = asyncio.Event()

        async def apply(base, **kw):
            async with ca.client() as client:
                out = await real(base, client=client, **kw)
            calls["n"] += 1
            between(calls["n"])
            if calls["n"] >= rounds:
                stop.set()
            return out

        monkeypatch.setattr(cl, "apply_campaign", apply)
        await asyncio.wait_for(cl.run_watcher(stop, lambda: BASE, every_s=0.01), 5)

    asyncio.run(go())


def test_the_watcher_picks_up_an_approved_campaign_by_itself_and_says_so_once(
    cfg, monkeypatch
) -> None:
    ca = FakeCA()
    _watch(
        ca,
        cfg,
        monkeypatch,
        4,
        between=lambda i: setattr(ca, "live", campaign()) if i == 1 else None,
    )
    applied = [e for e in queued(cfg) if e["code"] == "MA_CAMPAIGN_APPLIED"]
    assert (
        len(applied) == 1 and "next pass will use it" in applied[0]["message"]
    )  # received, not "running"
    assert cl.load_cache(cfg).version == 1  # type: ignore[union-attr]


def test_the_watcher_reports_an_outage_once_not_every_check(cfg, monkeypatch) -> None:
    ca = FakeCA()
    ca.down = True
    _watch(ca, cfg, monkeypatch, 5)
    blockers = [e for e in queued(cfg) if e["kind"] == "blocker"]
    assert len(blockers) == 1 and blockers[0]["code"] == "MA_PICKUP_FAILED"


def test_the_watcher_does_nothing_without_a_link(monkeypatch) -> None:
    monkeypatch.setattr(cl, "link_config", lambda: None)
    asyncio.run(asyncio.wait_for(cl.run_watcher(asyncio.Event(), lambda: BASE, every_s=0.01), 2))


def test_a_pass_can_say_which_campaign_it_runs(cfg) -> None:
    assert (
        cl.describe_pass(cfg)["campaign_id"] is None  # type: ignore[index]
        and "base config" in cl.describe_pass(cfg)["message"]  # type: ignore[index]
    )
    ca = FakeCA()
    ca.live = campaign()
    pick_up(ca, cfg)
    said = cl.describe_pass(cfg)
    assert said["version"] == 1 and said["campaign_id"] == CID  # type: ignore[index]
    assert said["sha256"] == cl.load_cache(cfg).sha256[:12] and CID[:8] in said["message"]  # type: ignore[index, union-attr]


def test_with_no_link_a_pass_says_nothing(monkeypatch) -> None:
    monkeypatch.delenv("MA_LINK_URL", raising=False)
    assert cl.describe_pass() is None


# --- the CA's complete leadgen.yaml: run only when it equals this machine's base plus the campaign -------------------------


import yaml as _yaml  # noqa: E402


def complete(
    base: dict,
    overlay_text: str = OVERLAY,
    *,
    version: int = 1,
    tamper=None,
    bad_hash: bool = False,
) -> cl.Campaign:
    """What the CA sends: the overlay AND the complete file built from `base` (optionally a different, stale base)."""
    effective = cl.merge_overlay(base, cl.parse_overlay(overlay_text))
    if tamper:
        tamper(effective)
    text = (
        "# Complete leadgen.yaml generated by the Campaign Agent.\nconfig_version: '1.0'\n"
        + _yaml.safe_dump(effective, sort_keys=False)
    )
    sha = "0" * 64 if bad_hash else hashlib.sha256(text.encode()).hexdigest()
    base_c = campaign(overlay_text, version)
    return cl.Campaign(
        **{
            **base_c.__dict__,
            "leadgen_yaml": text,
            "leadgen_sha256": sha,
            "base_sha256": cl.base_fingerprint(base),
        }
    )


def test_a_complete_file_that_equals_the_local_base_plus_the_campaign_is_the_one_that_runs(
    cfg,
) -> None:
    ca = FakeCA()
    ca.live = complete(BASE)
    effective = pick_up(ca, cfg)
    assert effective == cl.merge_overlay(BASE, cl.parse_overlay(OVERLAY))
    assert (
        cfg.cache_dir / "leadgen.yaml"
    ).read_text() == ca.live.leadgen_yaml  # a readable copy of exactly what the CA sent
    applied = [e for e in queued(cfg) if e["code"] == "MA_CAMPAIGN_APPLIED"]
    assert "complete file" in applied[0]["message"]
    assert not [e for e in queued(cfg) if e["code"] == "MA_BASE_DRIFT"]
    assert "from the CA's complete leadgen file" in cl.describe_pass(cfg)["message"]  # type: ignore[index]


def test_a_complete_file_built_on_a_different_base_is_not_run_and_the_ca_is_told_once(cfg) -> None:
    ca = FakeCA()
    stale = {
        **BASE,
        "exclude_companies": ["SomeoneNew"],
    }  # the CA's snapshot lacks what was added on this machine
    local = {**BASE, "exclude_companies": ["SomeoneNew", "Added Here Ltd"]}
    ca.live = complete(stale)
    first = pick_up(ca, cfg, local)
    again = pick_up(ca, cfg, local)  # the next check sees the same file
    assert (
        first == again == cl.merge_overlay(local, cl.parse_overlay(OVERLAY))
    )  # this machine's base + the campaign, never the stale lists
    assert "Added Here Ltd" in first["exclude_companies"]
    drift = [e for e in queued(cfg) if e["code"] == "MA_BASE_DRIFT"]
    assert (
        len(drift) == 1
        and drift[0]["kind"] == "blocker"
        and "exclude_companies" in drift[0]["message"]
    )
    assert "(the CA's complete file was not used" in (cfg.cache_dir / "leadgen.yaml").read_text()
    assert "did not match" in cl.describe_pass(cfg)["message"]  # type: ignore[index]


def test_the_drift_blocker_closes_when_the_base_matches_again(cfg) -> None:
    ca = FakeCA()
    local = {**BASE, "exclude_companies": ["Added Here Ltd"]}
    ca.live = complete(BASE)
    pick_up(ca, cfg, local)
    ca.live = complete(local, version=2)  # the CA re-synced its copy
    pick_up(ca, cfg, local)
    codes = [(e["kind"], e["code"]) for e in queued(cfg) if e["code"] == "MA_BASE_DRIFT"]
    assert codes == [("blocker", "MA_BASE_DRIFT"), ("resolved", "MA_BASE_DRIFT")]


def test_a_complete_file_changed_on_the_way_is_not_run(cfg) -> None:
    ca = FakeCA()
    ca.live = complete(BASE, bad_hash=True)
    assert pick_up(ca, cfg) == cl.merge_overlay(BASE, cl.parse_overlay(OVERLAY))
    assert any(e["code"] == "MA_BASE_DRIFT" and "hash" in e["message"] for e in queued(cfg))


def test_a_complete_file_with_an_unexpected_change_is_not_run(cfg) -> None:
    """Only the campaign may change a setting. A file that also loosens a threshold is refused, whatever its hash says."""
    ca = FakeCA()
    ca.live = complete(BASE, tamper=lambda eff: eff["qualification"].update(min_qualify=1))
    effective = pick_up(ca, cfg)
    assert (
        effective["qualification"]["min_qualify"] == 55
    )  # the overlay's own value, not the sneaked-in 1
    assert any(
        e["code"] == "MA_BASE_DRIFT" and "qualification" in e["message"] for e in queued(cfg)
    )


def test_an_older_ca_without_a_complete_file_still_works_and_raises_no_alarm(cfg) -> None:
    ca = FakeCA()
    ca.live = campaign()
    assert pick_up(ca, cfg) == cl.merge_overlay(BASE, cl.parse_overlay(OVERLAY))
    assert not [e for e in queued(cfg) if e["code"] == "MA_BASE_DRIFT"]
    assert "on this machine's base file" in cl.describe_pass(cfg)["message"]  # type: ignore[index]


def test_the_fingerprint_ignores_order_and_the_first_difference_names_the_key() -> None:
    assert cl.base_fingerprint({"a": 1, "b": [1, 2]}) == cl.base_fingerprint({"b": [1, 2], "a": 1})
    assert (
        cl.first_difference({"search": {"x": 1, "y": 2}}, {"search": {"x": 1, "y": 3}})
        == "search.y"
    )
    assert cl.first_difference({"a": 1}, {"a": 1, "b": 2}) == "b"
    assert cl.first_difference({"a": 1}, {"a": 1}) is None


def test_a_pass_cut_off_by_a_restart_is_not_reported_as_stopped_by_someone(
    monkeypatch, tmp_path
) -> None:
    from agent import control

    monkeypatch.setenv("MA_LINK_URL", "http://campaign-agent:8787/ma/v1")
    monkeypatch.setenv("MA_LINK_TOKEN", "t" * 40)
    monkeypatch.setenv("MA_LINK_CACHE_DIR", str(tmp_path / "c"))
    pipeline = control.Pipeline(rest_minutes=30)
    pipeline._finish({"started_at": "t0", "outcome": "stopped"})  # someone pressed Stop
    pipeline._finish(
        {"started_at": "t1", "outcome": "stopped"}, interrupted=True
    )  # the service restarted under it
    said = [
        e["message"]
        for e in cl.Outbox(tmp_path / "c" / "outbox.jsonl").pending()
        if e["code"] == "MA_PASS_FINISHED"
    ]
    assert said[0] == "The pass was stopped"
    assert "restarting" in said[1] and "starts again by itself" in said[1]
    assert pipeline.last["outcome"] == "stopped"  # type: ignore[index]  # what the dashboard reads is unchanged
