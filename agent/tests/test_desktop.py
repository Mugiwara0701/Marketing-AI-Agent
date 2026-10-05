import asyncio

import pytest

from agent import mailer, settings
from agent.gui import discover, leadscore
from agent.gui.desktop import find_phrase, parse_tsv
from agent.tasks import qualify, search

JOB = "Senior Android Engineer. Full-time. 5+ years of experience in AOSP. Apply now. Benefits and salary offered."
PROJECT = (
    "We are an automotive OEM and are looking for an engineering partner to outsource AAOS "
    "board bring-up and Android HAL development for our new head unit. RFP open until June."
)


def test_job_posting_vs_project():
    assert leadscore.is_job_posting(JOB, "https://acme.io/careers/android")
    assert not leadscore.is_job_posting(PROJECT, "https://acme.io/news")
    assert leadscore.is_job_posting(
        PROJECT, "https://www.upwork.com/jobs/123"
    )  # individual-hiring site
    assert not leadscore.is_job_posting(
        "Our rugged handheld runs Android 14 on i.MX8.", "https://acme.io/p"
    )


def test_opportunity_type():
    assert leadscore.opportunity_type(PROJECT) == "rfp"
    assert leadscore.opportunity_type("we outsource firmware") == "outsourcing"
    assert leadscore.opportunity_type("a freelance consultant is needed") == "contract project"


@pytest.mark.parametrize(
    ("text", "title", "reason"),
    [
        ("Our systems have detected unusual traffic from your computer network", "", "captcha"),
        ("Just a moment...", "Just a moment...", "cloudflare"),
        ("Access denied. You do not have permission.", "", "access denied"),
        ("Please log in to continue reading", "", "login wall"),
    ],
)
def test_block_reason(text, title, reason):
    assert leadscore.block_reason(text, title) == reason


def test_long_article_mentioning_captcha_is_not_blocked():
    article = "word " * 1000 + "we added a login to continue flow"
    assert leadscore.block_reason("Intro about AOSP. " + article, "") is None


def test_canonical_url_and_similar():
    a = leadscore.canonical_url("https://www.Acme.io/rfp/?utm_source=x&id=7#top")
    assert a == leadscore.canonical_url("https://acme.io/rfp?id=7")
    assert leadscore.similar(
        "AOSP development project outsourcing", "AOSP development projects outsourcing"
    )
    assert not leadscore.new_query(
        "AOSP development project outsourcing", ["AOSP development projects outsourcing"]
    )
    assert leadscore.new_query(
        "Yocto board bring-up vendor needed", ["AOSP development project outsourcing"]
    )


def test_score_rewards_evidence():
    strong, _ = leadscore.score_lead(confidence=0.9, vendor=3, keyword_hits=4, text_len=2000, has_summary=True,
                                     own_site=True, contact=True)  # fmt: skip
    weak, _ = leadscore.score_lead(confidence=0.6, vendor=0, keyword_hits=1, text_len=200, has_summary=False,
                                   own_site=False, contact=False)  # fmt: skip
    assert strong > 85
    assert weak < 60


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


def test_queue_and_engine_target():
    cfg = {"platforms": [{"name": "web", "template": "{q}"}, {"name": "reddit", "template": "site:reddit.com {q}"}],
           "queries": ["aosp vendor", "bsp outsourcing"]}  # fmt: skip
    q = list(discover.build_queue(cfg))
    assert q[:2] == [("web", "aosp vendor"), ("web", "bsp outsourcing")] and len(q) == 4
    assert discover.engine_target("default", "aosp vendor") == "aosp vendor"
    assert (
        discover.engine_target("https://duckduckgo.com/?q={q}", "a b")
        == "https://duckduckgo.com/?q=a+b"
    )


def test_domain_helpers():
    assert (
        discover.domain_from_page("https://www.acme-devices.io/rfp", "Acme Devices Ltd")
        == "acme-devices.io"
    )
    assert discover.domain_from_page("https://techcrunch.com/acme", "Acme Devices Ltd") == ""
    assert (
        discover.domains_in(
            "Acme Devices - Rugged handhelds\nacmedevices.com > about\nwikipedia.org",
            "Acme Devices",
        )
        == "acmedevices.com"
    )


def test_email_lock_blocks_sending(monkeypatch):
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("EMAIL_SENDING_LOCKED", "")  # so teardown puts it back as it was
    assert mailer.sending_enabled()
    mailer.lock_sending()
    assert not mailer.sending_enabled()
    assert asyncio.run(mailer.send_approved()) == {
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "disabled": 1,
    }
    from agentkit import resend

    with pytest.raises(RuntimeError, match="locked"):
        asyncio.run(resend.send_email({}, "k"))


def test_desktop_mode_setting(monkeypatch):
    monkeypatch.delenv("LEADS_MODE", raising=False)
    assert not settings.desktop_mode()
    monkeypatch.setenv("LEADS_MODE", "desktop")
    assert settings.desktop_mode()
    s = settings.load()
    assert (s.lead_target, s.lead_max) == (5, 6)


# --- evaluate(): the judge-and-store step, with the browser, model and database faked ----------------


def _ctx(text_stats=None):
    stats = dict.fromkeys(("duplicates", "qualified", "stored", "drafts", "blocked_sources", "rejected_job", "rejected_irrelevant", "rejected_score"), 0)  # fmt: skip
    return discover.Ctx(desk=None, cfg={}, keywords=["aosp", "bsp", "android hal", "aaos"], max_age_days=45,  # type: ignore[arg-type]
                        state=discover.State(day="d"), deadline=1e12, stats=stats, min_score=60,
                        hits_per_query=4, min_hit_score=0.4)  # fmt: skip


def _fake_store(monkeypatch, saved):
    async def false(*_a, **_k):
        return False

    async def record(sig, extracted, cid=None):
        saved["signals"].append(extracted)

    async def company(**kw):
        saved["companies"].append(kw)
        return "cid"

    monkeypatch.setattr(discover.store, "signal_seen", false)
    monkeypatch.setattr(discover.store, "domain_known", false)
    monkeypatch.setattr(discover.store, "company_name_known", false)
    monkeypatch.setattr(discover.store, "record_signal", record)
    monkeypatch.setattr(discover.store, "save_company", company)


def test_evaluate_rejects_job_post_without_calling_the_model(monkeypatch):
    saved: dict[str, list] = {"signals": [], "companies": []}
    _fake_store(monkeypatch, saved)

    async def boom(*_a, **_k):
        raise AssertionError("model must not be called for a job post")

    monkeypatch.setattr(discover.qualify, "qualify_signal", boom)
    ctx = _ctx()
    hit = search.Hit(
        title="Android Engineer", domain="acme.io", snippet="", likely_project=True, score=0.8
    )
    assert (
        asyncio.run(discover.evaluate(ctx, hit, "https://acme.io/jobs/1", JOB, "q", "default"))
        == "rejected"
    )
    assert ctx.stats["rejected_job"] == 1 and not saved["companies"]


def test_evaluate_stores_a_real_project_and_dedups_the_second_time(monkeypatch):
    saved: dict[str, list] = {"signals": [], "companies": []}
    _fake_store(monkeypatch, saved)
    q = qualify.QualifyResult(relevant=True, service_fit=["AOSP", "Automotive"], confidence=0.9, reason="OEM seeks AAOS partner",
                              company_name="Acme Auto", project_summary="Head unit bring-up", technologies=["AAOS"],
                              location="Germany", website="acme-auto.com")  # fmt: skip

    async def fake_q(_text, _profile=""):
        return q, []

    async def no_contact(*_a, **_k):
        return None

    monkeypatch.setattr(discover.qualify, "qualify_signal", fake_q)
    monkeypatch.setattr(discover, "visible_contact", no_contact)
    ctx = _ctx()
    hit = search.Hit(
        title="AAOS partner wanted",
        domain="acme-auto.com",
        snippet="",
        likely_project=True,
        score=0.8,
    )
    url = "https://acme-auto.com/rfp/head-unit?utm_source=x"
    assert (
        asyncio.run(discover.evaluate(ctx, hit, url, PROJECT + " AOSP BSP", "q", "default"))
        == "stored"
    )
    company = saved["companies"][0]
    assert (
        company["domain"] == "acme-auto.com"
        and company["source"] == "desktop"
        and company["status"] == "qualified"
    )
    rec = saved["signals"][0]
    assert rec["opportunity_type"] == "rfp" and rec["aosp_relevance"] and rec["contact"] is None
    assert rec["qualification_score"] >= 60 and "utm_source" not in rec["source_url"]
    # the same page again is a duplicate, whatever tracking parameters it carries
    assert (
        asyncio.run(discover.evaluate(ctx, hit, url + "&gclid=1", PROJECT, "q", "default"))
        == "duplicate"
    )
    assert ctx.stats["stored"] == 1 and ctx.stats["duplicates"] == 1


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


def test_not_found_page_detected():
    assert discover._NOT_FOUND.search("404 - Page not found - Murena")
    assert not discover._NOT_FOUND.search("Contact Philips Support")


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


def test_contact_labels_are_groups_of_link_texts():
    """A flat string here was spread into single letters ('C', 'o', 'n'...) by click_link(*label)."""
    assert all(isinstance(g, tuple) and len(g) > 1 for g in discover._CONTACT_LABELS)
    assert all(isinstance(t, str) and len(t) > 2 for g in discover._CONTACT_LABELS for t in g)


def test_evaluate_needs_two_keywords_and_enough_confidence(monkeypatch):
    saved: dict[str, list] = {"signals": [], "companies": []}
    _fake_store(monkeypatch, saved)

    async def boom(*_a, **_k):
        raise AssertionError("model must not be called")

    monkeypatch.setattr(discover.qualify, "qualify_signal", boom)
    ctx = _ctx()
    hit = search.Hit(title="Robotaxi", domain="x.com", snippet="", likely_project=True, score=0.8)
    one = "Our robotaxi company is hiring. We do some AOSP on the side. " * 30
    assert (
        asyncio.run(discover.evaluate(ctx, hit, "https://x.com/", one, "q", "default"))
        == "rejected"
    )
    assert ctx.stats["rejected_irrelevant"] == 1  # one keyword is not enough, model never asked

    weak = qualify.QualifyResult(relevant=True, service_fit=["AOSP"], confidence=0.55, reason="vague",
                                 company_name="X", project_summary="s", technologies=[], location="", website="x.com")  # fmt: skip

    async def fake_q(_text, _profile=""):
        return weak, []

    monkeypatch.setattr(discover.qualify, "qualify_signal", fake_q)
    two = "We build devices. AOSP and BSP work for our hardware. " * 30
    assert (
        asyncio.run(discover.evaluate(_ctx(), hit, "https://x.com/a", two, "q", "default"))
        == "rejected"
    )
    assert not saved["companies"]  # confidence 0.55 < 0.7


def test_exclusions_block_autonomous_driving_and_big_brands():
    terms, companies = ["robotaxi", "autonomous driving"], ["nuro", "zoox", "google"]
    assert "robotaxi" in leadscore.exclusion_reason(
        "Zoox robotaxi launch", "zoox.com", "", terms, companies
    )
    assert "nuro" in leadscore.exclusion_reason("home page", "www.nuro.ai", "", terms, companies)
    assert "google" in leadscore.exclusion_reason("", "", "Google LLC", terms, companies)
    assert (
        leadscore.exclusion_reason(
            "Android BSP bring-up for rugged handhelds",
            "acme-devices.com",
            "Acme Devices",
            terms,
            companies,
        )
        == ""
    )
    assert (
        leadscore.exclusion_reason(
            "", "googleplex-partners.io", "Googleplex Partners", [], companies
        )
        == ""
    )  # not a prefix match


def test_followup_queries_must_stay_on_topic():
    service, project, exclude = (
        ["aosp", "bsp", "embedded"],
        ["outsourcing", "vendor", "rfp"],
        ["robotaxi"],
    )
    ok = leadscore.query_on_topic
    assert ok("AOSP BSP bring-up outsourcing vendor for rugged tablets", service, project, exclude)
    assert not ok(
        "companies like Nuro autonomous delivery", service, project, exclude
    )  # no service/project wording
    assert not ok(
        "embedded vendor for robotaxi sensors", service, project, exclude
    )  # excluded topic
    assert not ok(
        "android app development company", service, project, exclude
    )  # no project need word
    assert ok("anything", [], [], [])  # nothing configured: nothing filtered


def test_retry_drops_stored_excluded_companies_without_opening_them(monkeypatch):
    rows = [
        {"id": "1", "name": "Zoox, Inc.", "domain": "zoox.com", "project_summary": "robotaxi", "location": None, "technologies": None, "source": "desktop", "source_url": ""},
        {"id": "2", "name": "Acme Devices", "domain": "acme-devices.com", "project_summary": "rugged Android handhelds", "location": None, "technologies": None, "source": "desktop", "source_url": ""},
    ]  # fmt: skip
    rejected, visited = [], []

    async def fetch():
        return rows

    async def reject(cid):
        rejected.append(cid)

    async def no_contact(ctx, domain):
        visited.append(domain)

    async def touch(cid):
        return None

    monkeypatch.setattr(discover.store, "companies_without_contact", fetch)
    monkeypatch.setattr(discover.store, "reject_company", reject)
    monkeypatch.setattr(discover.store, "touch_company", touch)
    monkeypatch.setattr(discover, "visible_contact", no_contact)
    ctx = _ctx()
    ctx.cfg = {"exclude_terms": ["robotaxi"], "exclude_companies": ["zoox"]}
    asyncio.run(discover.retry_missing_contacts(ctx))
    assert rejected == ["1"] and visited == ["acme-devices.com"]


_RFID_PAGE = (
    "ID Tech Solutions. The world's only 360 degree provider of RFID and identity solutions. "
    "RFID readers fixed and handheld, smart card readers, card printers, biometric devices, "
    "access control and attendance terminals. Manufacturer based in Gurgaon. "
) * 12
_DEVICE_CFG = {
    "product_keywords": ["rfid", "reader", "smart card", "biometric", "terminal", "manufacturer"],
    "exclude_countries": ["canada", "united kingdom", "uk", "germany"],
    "exclude_tlds": [".ca", ".uk", ".co.uk", ".de"],
}


def _device_q(location="Gurgaon, India", website="idtech.example"):
    return qualify.QualifyResult(relevant=True, service_fit=["Embedded Linux"], confidence=0.8, reason="RFID reader maker",
                                 company_name="ID Tech Solutions", project_summary="RFID readers and biometric devices",
                                 technologies=["UHF RFID"], location=location, website=website)  # fmt: skip


def _run_device(monkeypatch, q, url="https://idtech.example/"):
    saved: dict[str, list] = {"signals": [], "companies": []}
    _fake_store(monkeypatch, saved)
    seen_profile = []

    async def fake_q(_text, profile=""):
        seen_profile.append(profile)
        return q, []

    async def no_contact(*_a, **_k):
        return None

    monkeypatch.setattr(discover.qualify, "qualify_signal", fake_q)
    monkeypatch.setattr(discover, "visible_contact", no_contact)
    ctx = _ctx()
    ctx.cfg = dict(_DEVICE_CFG)
    ctx.keywords = ["aosp", "bsp"]  # the page has none of these: only device wording
    hit = search.Hit(
        title="ID Tech", domain="idtech.example", snippet="", likely_project=True, score=0.8
    )
    out = asyncio.run(discover.evaluate(ctx, hit, url, _RFID_PAGE, "q", "default"))
    return out, saved, seen_profile


def test_device_maker_without_android_wording_is_a_lead(monkeypatch):
    out, saved, profile = _run_device(monkeypatch, _device_q())
    assert out == "stored" and saved["companies"]
    assert profile == ["device"]  # judged with the device-maker prompt


@pytest.mark.parametrize(
    "location", ["Munich, Germany", "Toronto, Canada", "Leeds, United Kingdom", "UK"]
)
def test_excluded_country_is_rejected(monkeypatch, location):
    out, saved, _ = _run_device(monkeypatch, _device_q(location=location))
    assert out == "rejected" and not saved["companies"]


def test_excluded_site_ending_is_rejected(monkeypatch):
    out, saved, _ = _run_device(monkeypatch, _device_q(website="acme.de"), url="https://acme.de/")
    assert out == "rejected" and not saved["companies"]


def test_country_excluded_helper():
    c, t = ["canada", "germany"], [".ca", ".de"]
    assert leadscore.country_excluded("www.acme.de", "", c, t) == "site ends in .de"
    assert leadscore.country_excluded("acme.com", "Berlin, Germany", c, t) == "based in germany"
    assert leadscore.country_excluded("acme.in", "Gurgaon, India", c, t) == ""
    assert (
        leadscore.country_excluded("acme.com", "", c, t) == ""
    )  # unknown location is not excluded


def test_exclusion_matches_multi_word_company_names():
    c = ["red hat", "wind river", "canonical"]
    assert leadscore.exclusion_reason("", "", "Red Hat, Inc.", [], c)
    assert leadscore.exclusion_reason("", "redhat.com", "", [], c)
    assert leadscore.exclusion_reason("", "", "Wind River Systems", [], c)
    assert leadscore.exclusion_reason("", "canonical.com", "Canonical Ltd", [], c)
    assert (
        leadscore.exclusion_reason("", "", "Red Hatter Devices", [], c) == ""
    )  # a word, not a prefix of one


def test_job_ads_are_caught_by_title_url_and_host_even_with_vendor_wording():
    lj = leadscore.looks_like_job
    assert lj("Senior Android Engineer - Acme", "https://acme.io/x")
    assert lj("Embedded Linux Developer", "https://acme.io/careers/embedded-linux")
    assert lj("RFID Firmware - we're hiring", "")
    assert lj("Acme", "https://jobs.acme.io/123")
    assert lj("Android BSP Engineer", "https://in.indeed.com/viewjob")
    assert not lj("RFID reader manufacturer - Acme Devices", "https://acme.io/products/readers")
    assert not lj("Android BSP porting RFQ for custom board", "https://acme.io/rfq")
    # an ad that mentions OEM / partner / vendor is still an ad
    ad = (
        "We are hiring. Full-time role, salary offered, apply now. Our OEM partner and vendor network. "
        * 3
    )
    assert leadscore.is_job_posting(ad, "https://acme.io/news", "Android engineer")
    assert leadscore.is_job_posting(ad, "https://acme.io/news")  # three employment markers


def test_project_request_is_not_a_job():
    text = "Request for quotation: Android BSP porting and RFID reader app for our custom board. Vendor proposals by June."
    assert not leadscore.is_job_posting(
        text, "https://acme.io/rfq", "RFQ: Android BSP porting for our board"
    )


def test_evaluate_rejects_a_job_ad_by_title(monkeypatch):
    saved: dict[str, list] = {"signals": [], "companies": []}
    _fake_store(monkeypatch, saved)

    async def boom(*_a, **_k):
        raise AssertionError("model must not be called for a job ad")

    monkeypatch.setattr(discover.qualify, "qualify_signal", boom)
    ctx = _ctx()
    hit = search.Hit(
        title="Senior Android Engineer",
        domain="acme.io",
        snippet="",
        likely_project=True,
        score=0.8,
    )
    out = asyncio.run(discover.evaluate(ctx, hit, "https://acme.io/p", PROJECT, "q", "default"))
    assert out == "rejected" and ctx.stats["rejected_job"] == 1


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
    clicks, looks = [], []

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


def test_retry_drops_stored_job_ad_leads(monkeypatch):
    rows = [{"id": "1", "name": "Arrow Electronics", "domain": "arrow.com", "project_summary": "", "location": "India", "technologies": None, "source": "desktop",
             "source_url": "https://careers.arrow.com/us/en/job/R1/Engineer-Linux-BSP"}]  # fmt: skip
    rejected, visited = [], []

    async def fetch():
        return rows

    async def reject(cid):
        rejected.append(cid)

    async def visit(ctx, domain):
        visited.append(domain)

    monkeypatch.setattr(discover.store, "companies_without_contact", fetch)
    monkeypatch.setattr(discover.store, "reject_company", reject)
    monkeypatch.setattr(discover, "visible_contact", visit)
    asyncio.run(discover.retry_missing_contacts(_ctx()))
    assert rejected == ["1"] and not visited
