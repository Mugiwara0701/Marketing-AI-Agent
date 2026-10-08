"""The outreach draft: the fixed "other services" paragraph is added once, before the closing question."""

import asyncio

from agent.leadgen import outreach
from agent.leadgen.models import Contact, Lead
from agent.tasks import proposal

SERVICES = "Beyond this, we also provide Android BSP and board bring-up."
BODY = (
    "Hello,\n\nWe saw that Acme EV builds the charger.\n- BSP bring-up\n\nWould a short call help?"
)


def test_services_go_before_the_closing_question():
    out = outreach.with_services(BODY, SERVICES)
    paras = out.split("\n\n")
    assert paras[-2] == SERVICES and paras[-1] == "Would a short call help?"
    assert outreach.with_services(out, SERVICES) == out  # added once


def test_short_body_gets_the_services_at_the_end_and_no_text_means_no_change():
    assert outreach.with_services("Hello,\n\nOne line.", SERVICES).endswith(SERVICES)
    assert outreach.with_services(BODY, "") == BODY


def test_services_text_comes_from_the_editable_file(monkeypatch, tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("We also do\nfirmware   work.\n")
    monkeypatch.setenv("OUTREACH_SERVICES_FILE", str(f))
    assert outreach.services_paragraph() == "We also do firmware work."
    monkeypatch.setenv("OUTREACH_SERVICES_FILE", str(tmp_path / "missing.txt"))
    assert outreach.services_paragraph() == ""


def test_draft_adds_services_after_the_checks(monkeypatch, tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("We also provide Yocto and OTA work.")
    monkeypatch.setenv("OUTREACH_SERVICES_FILE", str(f))

    async def model(ctx):
        return proposal.EmailDraft(
            subject="Support for Acme EV", body=BODY + " " * 0 + "x" * 40
        ), []

    monkeypatch.setattr(proposal, "draft_proposal", model)
    d, problems = asyncio.run(
        outreach.draft(
            Lead(company_name="Acme EV", company_website="a.io"), Contact(email="a@a.io")
        )
    )
    assert "We also provide Yocto and OTA work." in d.body
    assert not any(
        "Yocto" in p or "OTA" in p for p in problems
    )  # our own text is not an invented claim
