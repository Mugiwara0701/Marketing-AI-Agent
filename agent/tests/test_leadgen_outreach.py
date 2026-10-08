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

    async def model(ctx, company=""):
        return proposal.EmailDraft(
            subject="Support for Acme EV", body=BODY + " " * 0 + "x" * 40
        ), []

    monkeypatch.setattr(proposal, "draft_pitch", model)
    d, problems = asyncio.run(
        outreach.draft(
            Lead(company_name="Acme EV", company_website="a.io"), Contact(email="a@a.io")
        )
    )
    assert "We also provide Yocto and OTA work." in d.body
    assert not any(
        "Yocto" in p or "OTA" in p for p in problems
    )  # our own text is not an invented claim


def test_pitch_parts_are_assembled_in_a_fixed_order():
    p = proposal.PitchParts(
        subject="Android BSP support for your Tab-IND 22", greeting="Hello Anna,",
        intro="We are Ftechiz, an engineering company. We saw that Acme EV builds the charger terminal.",
        understanding="A terminal that combines NFC, cameras and a display usually needs stable drivers.",
        offer_intro="We could take on these defined pieces of work for you:",
        deliverables=["Board bring-up and BSP: a stable image", "- NFC driver and HAL: reads cards reliably", "Display integration: touch and brightness"],
        engagement="We work as a defined-scope project with milestones, or as extra engineers beside your team.",
        closing_question="Would a 30-minute call next week help? We can send a one-page scope first.",
    )  # fmt: skip
    body = proposal.assemble(p).body
    paras = body.split("\n\n")
    assert paras[0] == "Hello Anna," and paras[-1].startswith("Would a 30-minute call")
    assert (
        "- NFC driver and HAL: reads cards reliably" in body and "--" not in body
    )  # one bullet marker only
    assert [x[:2] for x in paras] == ["He", "We", "A ", "We", "We", "Wo"]
    full = outreach.with_services(body, SERVICES)
    assert full.split("\n\n")[-2] == SERVICES  # services sit right before the closing question


def test_long_drafts_are_shown_whole_in_slack():
    from agent.leadgen import approval
    from agent.leadgen.models import EmailDraft

    body = "\n\n".join(f"Paragraph {i}. " + "word " * 130 for i in range(8))
    e = EmailDraft(
        email_id="e1",
        lead_id="l",
        contact_id="c",
        to="a@b.io",
        subject="S",
        body=body,
        status="drafted",
    )
    shown = " ".join(b["text"]["text"] for b in approval.draft_sections(e))
    assert all(f"Paragraph {i}." in shown for i in range(8)) and len(approval.draft_sections(e)) > 1
