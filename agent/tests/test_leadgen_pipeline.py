"""The whole pipeline over a fake web, the real rules, the real SQLite store and the real approval/sender code;
only the model and the email writer are scripted."""

import asyncio
import functools
import re

from agent.leadgen import approval, contacts, sender, strategy
from agent.leadgen.models import LeadStatus, Page, SearchResult
from agent.leadgen.pipeline import Pipeline, Services
from agent.leadgen.repository.sqlite import SqliteRepository
from agent.tasks.contact import ContactResult, Person
from agent.tests.leadgen_fakes import FakeBrowser, ScriptedModel, assessment, cfg, fake_draft, html

EV_HOME = "https://voltgrid.com"
EV_TEXT = (
    "VoltGrid Energy designs and manufactures DC fast chargers for bus fleets. Our charger runs embedded Linux on "
    "an i.MX8 controller board with CAN bus and UART links. Our engineering team is developing the next-generation "
    "platform with OTA updates."
)
WEB = {
    EV_HOME: html("VoltGrid Energy", EV_TEXT, [("About", f"{EV_HOME}/about"), ("Contact us", f"{EV_HOME}/contact"),
                                               ("Buy chargers", "https://shop.example/x")]),
    f"{EV_HOME}/contact": html("Contact", "Write to sales@voltgrid.com. Support: support@othermail.com"),
    f"{EV_HOME}/about": html("Team", "Ana Ruiz, CTO, ana.ruiz@voltgrid.com. Bo Li, Marketing Lead.",
                             [("LinkedIn", "https://www.linkedin.com/in/ana-ruiz")]),
    "https://shop.example/rk3568": html("RK3568 board", "Industrial Android development board available for ₹15,000. "
                                        "RK3568 embedded Linux. Add to cart. Ships within 3 days. In stock."),
    "https://news.example/voltgrid": html("VoltGrid raises", "VoltGrid Energy, maker of DC fast chargers running "
                                          "embedded Linux on i.MX8 with CAN bus, announced a new depot charger.",
                                          [("VoltGrid", EV_HOME)]),
    "https://blocked.example/x": "BLOCKED",
}  # fmt: skip
RESULTS = [
    SearchResult(
        "RK3568 board for sale", "https://shop.example/rk3568", "", "fake", "shop.example"
    ),
    SearchResult("VoltGrid Energy - DC fast chargers", EV_HOME, "", "fake", "voltgrid.com"),
    SearchResult("Blocked", "https://blocked.example/x", "", "fake", "blocked.example"),
    SearchResult("Mouser listing", "https://www.mouser.com/x", "", "fake", "mouser.com"),
]
EV = assessment(
    company_name="VoltGrid Energy", industry="EV charging", product="DC fast chargers",
    engineering_needs=["embedded Linux", "device drivers", "OTA"],
    opportunity="Embedded Linux / driver work for their charger controller.",
    evidence=[{"quote": "Our charger runs embedded Linux on an i.MX8 controller board", "reason": "Linux device"}],
)  # fmt: skip
NEWS = assessment(page_type="news_article", company_name="VoltGrid Energy", company_website="voltgrid.com",
                  evidence=[{"quote": "maker of DC fast chargers running embedded Linux", "reason": "second source"}])  # fmt: skip


async def fake_people(text: str, links: str) -> ContactResult:
    """Stands in for the contact-reading model: 'Name, Role, address' triples and any address."""
    people = [Person(name=n, role=r, email=e, linkedin=next(iter(re.findall(r"https://\S+", links)), ""))
              for n, r, e in re.findall(r"([A-Z][a-z]+ [A-Z][a-z]+), ([A-Za-z ]+), (\S+@\S+?)\.?(?:\s|$)", text)]  # fmt: skip
    return ContactResult(people=people, business_emails=re.findall(r"\b\w+@[\w.]+\w", text))


def _pipeline(repo, browser, model, approver, **conf):
    svc = Services(repo=repo, browser=browser, approver=approver, assess=model, draft=fake_draft,
                   find_contacts=functools.partial(contacts.discover, extract=fake_people))  # fmt: skip
    return Pipeline(svc, cfg(**conf))


def _run(p):
    return asyncio.run(p.run())


def test_discovery_to_approval_and_only_then_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    repo = SqliteRepository(":memory:")
    browser = FakeBrowser(WEB, RESULTS)
    model = ScriptedModel({EV_HOME: EV, "https://voltgrid.com": EV})
    p = _pipeline(repo, browser, model, approval.SimulatedApprover(tmp_path))
    stats = _run(p)

    # seller page and directory-like results never reached the model; the blocked site was skipped
    assert model.calls == [EV_HOME]
    assert "https://www.mouser.com/x" not in browser.opened
    assert stats["pages_blocked"] >= 1 and stats["leads_qualified"] == 1

    lead = asyncio.run(repo.find_lead(domain="voltgrid.com"))
    assert lead and lead.status == LeadStatus.PENDING_APPROVAL
    assert (
        lead.contact and lead.contact.name == "Ana Ruiz" and lead.contact.role == "CTO"
    )  # decision maker first
    assert lead.contact.linkedin == "https://www.linkedin.com/in/ana-ruiz"
    assert all(
        "othermail" not in c.email for c in asyncio.run(repo.contacts_for(lead.lead_id or ""))
    )
    assert lead.lead_score >= 60 and any(e.source == "llm" for e in lead.evidence)

    email = asyncio.run(repo.email_for_lead(lead.lead_id or ""))
    assert email and email.status == "drafted" and "VoltGrid Energy" in email.body
    assert (tmp_path / "approvals" / f"{email.email_id}.md").exists()

    outbox = sender.OutboxTransport(tmp_path)
    assert (
        asyncio.run(sender.send_approved(repo, outbox, gap_seconds=0))["sent"] == 0
    )  # not approved yet
    assert asyncio.run(approval.decide(repo, email.email_id, True, "tester")) == "approved"
    assert asyncio.run(sender.send_approved(repo, outbox, gap_seconds=0))["sent"] == 1
    assert asyncio.run(repo.get_lead(lead.lead_id or "")).status == LeadStatus.SENT  # type: ignore[union-attr]


def test_same_company_from_two_pages_is_one_lead(tmp_path):
    repo = SqliteRepository(":memory:")
    results = [
        RESULTS[1],
        SearchResult("VoltGrid raises", "https://news.example/voltgrid", "", "fake"),
    ]
    model = ScriptedModel({EV_HOME: EV, "https://news.example/voltgrid": NEWS})
    p = _pipeline(repo, FakeBrowser(WEB, results), model, approval.SimulatedApprover(tmp_path))
    p.cfg.search["queries_per_run"] = 1
    _run(p)
    n = repo.db.execute("select count(*) from companies").fetchone()[0]
    lead = asyncio.run(repo.find_lead(domain="voltgrid.com"))
    assert n == 1 and lead and len(lead.source_urls) == 2
    assert {e.url for e in lead.evidence} >= {EV_HOME, "https://news.example/voltgrid"}


def test_second_run_does_not_redo_pages_or_queries(tmp_path):
    repo = SqliteRepository(":memory:")
    model = ScriptedModel({EV_HOME: EV})
    first = _pipeline(repo, FakeBrowser(WEB, RESULTS), model, approval.SimulatedApprover(tmp_path))
    _run(first)
    calls = len(model.calls)
    browser = FakeBrowser(WEB, RESULTS)
    second = _pipeline(repo, browser, model, approval.SimulatedApprover(tmp_path))
    used = {q.text for q in asyncio.run(strategy.next_queries(repo, second.cfg))}
    assert not used & set(first.svc.browser.searches)
    _run(second)
    assert len(model.calls) == calls  # nothing judged twice
    assert repo.db.execute("select count(*) from emails").fetchone()[0] == 1


def test_crash_after_contact_is_resumed_without_duplicates(tmp_path):
    repo = SqliteRepository(":memory:")

    async def broken_draft(lead, contact):
        raise TimeoutError("model host down")

    model = ScriptedModel({EV_HOME: EV})
    p = _pipeline(repo, FakeBrowser(WEB, RESULTS), model, approval.SimulatedApprover(tmp_path))
    p.svc.draft = broken_draft
    stats = _run(p)
    assert stats["llm_errors"] >= 1
    lead = asyncio.run(repo.find_lead(domain="voltgrid.com"))
    assert lead and lead.status == LeadStatus.CONTACT_FOUND  # kept, not lost

    again = _pipeline(
        repo, FakeBrowser(WEB, []), ScriptedModel({}), approval.SimulatedApprover(tmp_path)
    )
    out = _run(again)
    lead = asyncio.run(repo.find_lead(domain="voltgrid.com"))
    assert out["resumed"] >= 1 and lead and lead.status == LeadStatus.PENDING_APPROVAL
    assert repo.db.execute("select count(*) from emails").fetchone()[0] == 1


def test_model_failures_do_not_crash_and_pages_are_retried_later(tmp_path):
    repo = SqliteRepository(":memory:")
    model = ScriptedModel({}, fail={EV_HOME})
    stats = _run(
        _pipeline(repo, FakeBrowser(WEB, RESULTS), model, approval.SimulatedApprover(tmp_path))
    )
    assert stats["llm_errors"] >= 1
    assert repo.db.execute("select count(*) from companies").fetchone()[0] == 0
    from agent.leadgen import identity

    assert not asyncio.run(repo.page_seen(identity.url_key(EV_HOME)))  # left for a later run


def test_blocked_search_engine_is_survived(tmp_path):
    repo = SqliteRepository(":memory:")
    stats = _run(_pipeline(repo, FakeBrowser(WEB, RESULTS, blocked_search=True), ScriptedModel({}),
                           approval.SimulatedApprover(tmp_path)))  # fmt: skip
    assert stats["searches_blocked"] >= 1 and stats.get("searches", 0) == 0
    assert (
        asyncio.run(repo.query_last_run(next(iter(strategy.generate(cfg()))).text)) is None
    )  # retried later


def test_low_score_lead_is_rejected_not_drafted(tmp_path):
    repo = SqliteRepository(":memory:")
    weak = assessment(company_name="VoltGrid Energy", confidence=0.55)
    p = _pipeline(repo, FakeBrowser(WEB, RESULTS), ScriptedModel({EV_HOME: weak}),
                  approval.SimulatedApprover(tmp_path), min_qualify=40, min_draft=95)  # fmt: skip
    _run(p)
    lead = asyncio.run(repo.find_lead(domain="voltgrid.com"))
    assert lead and lead.status == LeadStatus.REJECTED
    assert repo.db.execute("select count(*) from emails").fetchone()[0] == 0


def test_contact_rules_literal_on_domain_and_role(tmp_path):
    page = Page(f"{EV_HOME}/team", "Team", "Ana Ruiz, CTO, ana.ruiz@voltgrid.com. Joe, joe@gmail.com. "
                "info@voltgrid.com. noreply@voltgrid.com")  # fmt: skip
    from agent.tasks.contact import ContactResult, Person

    model = ContactResult(people=[Person(name="Ana Ruiz", role="CTO", email="ana.ruiz@voltgrid.com"),
                                  Person(name="Invented Person", role="CTO", email="ceo@voltgrid.com"),
                                  Person(name="Joe", role="Founder", email="joe@gmail.com")],
                          business_emails=["info@voltgrid.com"])  # fmt: skip
    got = contacts.candidates_from(page, "voltgrid.com", model)
    assert [c.email for c in got] == ["ana.ruiz@voltgrid.com", "info@voltgrid.com"]
    assert contacts.role_rank("VP of Engineering") == 1 and contacts.role_rank("Co-founder") == 6
    assert contacts.role_rank("Founder & CEO") == 5 and contacts.role_rank("Marketing Lead") is None


def _stored_qualified(repo, domain="deadsite.com"):
    return asyncio.run(_insert_qualified(repo, domain))


async def _insert_qualified(repo, domain):
    from agent.leadgen.models import Lead

    lid = await repo.insert_lead(
        Lead(company_name="Dead Co", company_website=domain, lead_score=70), source="t"
    )
    await repo.set_status(lid, LeadStatus.QUALIFIED)
    return lid


def _no_contact_pipeline(repo, tmp_path, form_url=None):
    async def nothing(browser, domain, cfg, **kw):
        return contacts.Discovery(blocked="home page not reachable", form_url=form_url)

    svc_browser = FakeBrowser({}, [])
    p = _pipeline(repo, svc_browser, ScriptedModel({}), approval.SimulatedApprover(tmp_path))
    p.svc.find_contacts = nothing
    p.cfg.search["queries_per_run"] = 0  # resume only
    return p


def test_dead_site_is_retried_a_few_times_then_rejected(tmp_path):
    repo = SqliteRepository(":memory:")
    lid = _stored_qualified(repo)
    for run in range(1, 4):
        _run(_no_contact_pipeline(repo, tmp_path))
        lead = asyncio.run(repo.get_lead(lid))
        assert lead and lead.status == (LeadStatus.REJECTED if run == 3 else LeadStatus.QUALIFIED)
    assert (
        "no contact after 3 attempts"
        in repo.db.execute("select status_note from companies").fetchone()[0]
    )
    out = _run(_no_contact_pipeline(repo, tmp_path))  # a fourth run does not open it again
    assert out.get("resumed", 0) == 0


def test_form_only_lead_stops_being_retried_but_stays_for_manual_contact(tmp_path):
    repo = SqliteRepository(":memory:")
    lid = _stored_qualified(repo, "formonly.com")
    for _ in range(3):
        _run(_no_contact_pipeline(repo, tmp_path, form_url="https://formonly.com/contact"))
    assert asyncio.run(repo.get_lead(lid)).status == LeadStatus.QUALIFIED  # type: ignore[union-attr]
    assert asyncio.run(repo.leads_needing_contact(3, 10)) == []


def test_an_unexpected_search_error_costs_one_query_not_the_run(tmp_path):
    class Broken(FakeBrowser):
        async def search(self, query, limit):
            self.searches.append(query)
            raise RuntimeError("desktop glitch")

    repo = SqliteRepository(":memory:")
    b = Broken(WEB, RESULTS)
    stats = _run(_pipeline(repo, b, ScriptedModel({}), approval.SimulatedApprover(tmp_path)))
    assert stats["errors"] == len(b.searches) == 3  # every query tried, the run finished


def test_self_promoting_results_are_not_opened(tmp_path):
    repo = SqliteRepository(":memory:")
    results = [
        SearchResult(
            "Freelance AOSP Developer - available now", "https://ravi-dev.example/", "", "fake"
        ),
        SearchResult(
            "Hire Dedicated Embedded Linux Developers", "https://agency.example/hire", "", "fake"
        ),
        SearchResult("AOSP freelancer", "https://www.upwork.com/freelancers/~01abc", "", "fake"),
        RESULTS[1],
    ]
    b = FakeBrowser(WEB, results)
    _run(_pipeline(repo, b, ScriptedModel({EV_HOME: EV}), approval.SimulatedApprover(tmp_path)))
    assert not {"https://ravi-dev.example/", "https://agency.example/hire"} & set(b.opened)
    assert not any("upwork" in u for u in b.opened)
    assert EV_HOME in b.opened


def test_lists_directories_and_off_topic_results_are_not_opened_and_the_best_go_first(tmp_path):
    from agent.leadgen.pipeline import rank

    results = [
        SearchResult(
            "EV partnership opportunities",
            "https://www.transportation.gov/ev",
            "Rural toolkit for funding",
            "f",
        ),
        SearchResult(
            "Top 66 Electric Vehicle Charging startups", "https://startups.example/ev", "list", "f"
        ),
        SearchResult(
            "EV Business Listings",
            "https://evreporter.example/business-listings/",
            "EV directory",
            "f",
        ),
        SearchResult(
            "VoltGrid Energy - DC fast chargers", EV_HOME, "Embedded Linux on i.MX8, CAN bus", "f"
        ),
    ]
    p = _pipeline(SqliteRepository(":memory:"), FakeBrowser(WEB, results), ScriptedModel({}),
                  approval.SimulatedApprover(tmp_path))  # fmt: skip
    assert [p.skip_result(r) is None for r in results] == [False, False, False, True]
    assert rank(results)[0].url == EV_HOME


FOOGLE_CONTACT = (
    "Embedded Systems & IoT Development Company — Surat, Gujarat, India Services Technologies Contact Us "
    "GET IN TOUCH Let's build something great together. Whether you have a project in mind... CALL US +91 8320352507 "
    "EMAIL US foogletech@gmail.com We reply within 24 hours OUR LOCATION Surat, Gujarat, India Send a Message "
    "Join Our Team Start a conversation Fill in the form and our team will reach out shortly. Full Name * "
    "Work Email * Phone Number Service Needed * Tell us about your project * Send Message"
)


def test_company_free_mail_address_and_text_form_are_found(tmp_path):
    from agent.leadgen import intent

    page = Page("https://foogletech.com/contact-us", "Contact Us", FOOGLE_CONTACT)
    got = contacts.candidates_from(page, "foogletech.com", None, "FoogleTech Software")
    assert [c.email for c in got] == ["foogletech@gmail.com"] and "free-mail" in got[0].role
    assert intent.looks_like_contact_form(FOOGLE_CONTACT)
    # a person's private address on the same page is not taken, nor any address for another company's name
    other = Page(
        "https://foogletech.com/team", "Team", "Write to ravi.patel1987@gmail.com or acme@gmail.com"
    )
    assert contacts.candidates_from(other, "foogletech.com", None, "FoogleTech Software") == []
    assert not intent.looks_like_contact_form(
        "We build kiosks. Email sales and subscribe to our newsletter."
    )


def test_contact_discovery_on_a_desktop_style_page_finds_the_address_and_form(tmp_path):
    web = {
        "https://foogletech.com": html("FoogleTech", "Embedded Systems & IoT Development Company",
                                       [("Contact Us", "https://foogletech.com/contact-us")]),
        "https://foogletech.com/contact-us": html("Contact Us", FOOGLE_CONTACT),
    }  # fmt: skip
    found = asyncio.run(contacts.discover(FakeBrowser(web, []), "foogletech.com", cfg(), extract=fake_people,
                                          company="FoogleTech Software"))  # fmt: skip
    assert found.best and found.best.email == "foogletech@gmail.com"
    assert found.form_url == "https://foogletech.com/contact-us"


def test_a_stored_lead_whose_home_page_is_a_services_firm_is_rejected_not_contacted(tmp_path):
    from agent.leadgen.models import Lead

    web = {
        "https://foogletech.com": html(
            "Embedded Systems & IoT Development Company | FoogleTech Software",
            "Embedded & IoT Software Services. Our services: embedded Linux, BSP, Yocto. Hire Embedded Engineers. "
            "Outsource Embedded Dev. 40+ global clients. Get a free consultation.",
            [("Contact Us", "https://foogletech.com/contact-us")],
        ),
        "https://foogletech.com/contact-us": html("Contact", FOOGLE_CONTACT),
    }
    repo = SqliteRepository(":memory:")

    async def stored():
        lid = await repo.insert_lead(Lead(company_name="FoogleTech Software", company_website="foogletech.com",
                                          lead_score=68), source="earlier-run")  # fmt: skip
        await repo.set_status(lid, LeadStatus.QUALIFIED)
        return lid

    lid = asyncio.run(stored())
    b = FakeBrowser(web, [])
    p = _pipeline(repo, b, ScriptedModel({}), approval.SimulatedApprover(tmp_path))
    p.cfg.search["queries_per_run"] = 0
    out = _run(p)
    lead = asyncio.run(repo.get_lead(lid))
    assert out["rejected_competitor"] == 1 and lead and lead.status == LeadStatus.REJECTED
    assert b.opened == ["https://foogletech.com"]  # its contact page was never opened
    assert repo.db.execute("select count(*) from contacts").fetchone()[0] == 0


def test_stopping_the_run_closes_the_browser():
    """Dashboard Stop cancels the run mid-search: the browser (Chrome on the office desktop) still gets closed."""

    class SlowBrowser(FakeBrowser):
        closed = 0

        async def search(self, query, limit):
            await asyncio.sleep(60)
            return []

        async def close(self):
            SlowBrowser.closed += 1

    async def go():
        p = _pipeline(
            SqliteRepository(":memory:"), SlowBrowser(WEB, RESULTS), ScriptedModel({}), None
        )
        task = asyncio.create_task(p.run())
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert SlowBrowser.closed == 1
