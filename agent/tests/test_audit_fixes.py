"""Fixes from the 2026-10-17 audit: the Campaign Agent's level-6 contract, full text in Slack, the campaign gate."""

import asyncio

from agent import __main__ as cli
from agent import campaign_link, notify


def _run(argv: list[str]) -> int:
    return asyncio.run(cli._main(argv))


def test_leads_queries_campaign_needs_no_database_and_prints_queries(tmp_path, capsys):
    """The exact command the Campaign Agent runs (validators/downstream.py), with no DATABASE_URL."""
    f = tmp_path / "campaign.yaml"
    f.write_text(
        "config_version: '1.0'\n"
        "project_context:\n  industries: [EV charging]\n  technologies: [embedded linux]\n",
        encoding="utf-8",
    )
    assert _run(["leads", "queries", "--campaign", str(f)]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines, "the Campaign Agent needs at least one query"


def test_leads_queries_campaign_rejects_an_unknown_key(tmp_path, capsys):
    f = tmp_path / "campaign.yaml"
    f.write_text("config_version: '1.0'\nsending:\n  approve: false\n", encoding="utf-8")
    assert _run(["leads", "queries", "--campaign", str(f)]) == 2
    assert "campaign file rejected" in capsys.readouterr().err


def test_leads_queries_campaign_rejects_an_unsupported_version(tmp_path, capsys):
    f = tmp_path / "campaign.yaml"
    f.write_text("config_version: '9.9'\n", encoding="utf-8")
    assert _run(["leads", "queries", "--campaign", str(f)]) == 2
    assert "not supported" in capsys.readouterr().err


def test_a_long_followup_is_shown_whole_in_slack():
    # about 7,000 characters
    body = "\n\n".join(f"Paragraph {i}: " + "word " * 120 for i in range(12))
    row: dict[str, object] = {"company": "Acme", "domain": "acme.io", "project_summary": "p", "technologies": [],
           "email": "hi@acme.io", "name": "", "role": "", "source_url": "", "review_note": None,
           "subject": "Re: Hello", "body": body, "step": 2}  # fmt: skip
    _text, blocks = notify.email_blocks(row, "email:ID-2")
    shown = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    for i in range(12):
        assert f"Paragraph {i}:" in shown
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")


def test_a_long_reply_draft_is_shown_whole_in_slack():
    draft = "\n\n".join(f"Point {i}: " + "detail " * 100 for i in range(8))
    row = {"company": "Acme", "email": "hi@acme.io", "label": "question", "review_note": None,
           "body": "Can you help?", "status": "classified", "draft_response": draft}  # fmt: skip
    _text, blocks = notify.reply_blocks(row, "reply:ID")
    shown = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    for i in range(8):
        assert f"Point {i}:" in shown


def test_live_campaign_id(monkeypatch, tmp_path):
    assert campaign_link.live_campaign_id() is None  # no link: campaigns are not checked
    cfg = campaign_link.LinkConfig(url="http://ca:8787/ma/v1", token="t" * 40, cache_dir=tmp_path)
    assert campaign_link.live_campaign_id(cfg) == "none"
    text = "config_version: '1.0'\n"
    import hashlib

    campaign_link.save_cache(
        cfg,
        campaign_link.Campaign(
            campaign_id="c-1", version=2, sha256=hashlib.sha256(text.encode()).hexdigest(),
            contract_version="1.0", overlay_yaml=text, tag="c-1.2",
        ),
    )  # fmt: skip
    assert campaign_link.live_campaign_id(cfg) == "c-1"


def test_require_campaign_defaults_on(monkeypatch):
    monkeypatch.delenv("MA_REQUIRE_CAMPAIGN", raising=False)
    assert campaign_link.require_campaign()
    monkeypatch.setenv("MA_REQUIRE_CAMPAIGN", "false")
    assert not campaign_link.require_campaign()


def test_a_pass_without_a_live_campaign_does_not_search(monkeypatch):
    """No approved campaign: no browser is started, nothing is searched or drafted."""
    from agent.leadgen import service

    async def no_campaign(raw, **_kw):
        return raw

    def boom(*_a, **_kw):
        raise AssertionError("the pass must not start a browser")

    monkeypatch.setattr(campaign_link, "apply_campaign", no_campaign)
    monkeypatch.setattr(service, "make_browser", boom)
    # a real run (never connected to)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    out = asyncio.run(service.run_leads(dry_run=False))
    assert "not configured" in out["skipped"]


# --- the campaign's countries -----------------------------------------------------------------------------------------


def test_a_company_placed_outside_the_campaign_countries_is_rejected():
    from agent.leadgen import intent

    assert intent.country_outside("acme.de", "", ["US"])  # a German site
    assert intent.country_outside("acme.io", "Munich, Germany", ["United States"])
    assert "Germany" not in intent.country_outside("acme.io", "Munich, Germany", ["Germany"])
    assert intent.country_outside("acme.io", "Munich, Germany", ["Germany", "Austria"]) == ""
    # the page wins over the domain
    assert intent.country_outside("acme.de", "Austin, Texas, USA", ["US"]) == ""
    assert intent.country_outside("acme.de", "", []) == ""  # no countries set: any country


def test_a_company_whose_country_cannot_be_told_is_kept_and_noted():
    from agent.leadgen import intent

    assert intent.country_outside("acme.io", "", ["US"]) == ""
    assert intent.country_unknown("acme.io", "", ["US"])
    assert not intent.country_unknown("acme.us", "", ["US"])
    assert not intent.country_unknown("acme.io", "", [])


def test_the_link_accepts_and_merges_include_countries():
    search = {"query_suffix": ""}
    base = {"config_version": "1.0", "search": search, "exclude_countries": ["Canada"]}
    overlay = {"config_version": "1.0", "include_countries": ["Germany", "Austria"]}
    campaign_link.check_overlay(overlay, base)
    merged = campaign_link.merge_overlay(base, overlay)
    assert merged["include_countries"] == ["Germany", "Austria"]
    assert "include_countries" not in campaign_link.merge_overlay(base, {"config_version": "1.0"})
    import pytest

    with pytest.raises(campaign_link.OverlayError):
        campaign_link.check_overlay({"config_version": "1.0", "include_countries": "Germany"}, base)
