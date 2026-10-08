import asyncio

from agent import contacts, mailer, sources, web


def _part(msg, kind: str) -> str:
    """The plain or html part of a built message."""
    body = msg.get_body((kind,))
    assert body is not None, f"no {kind} part"
    return body.get_content()


def test_registrable_domain():
    assert web.registrable_domain("https://www.careers.acme.co.uk/jobs") == "acme.co.uk"
    assert web.registrable_domain("https://sub.example.com") == "example.com"
    assert web.registrable_domain("not a host") == ""
    assert not web.is_company_site("linkedin.com") and web.is_company_site("acme.io")


def test_private_hosts_blocked():
    assert asyncio.run(web.fetch("http://127.0.0.1/")) is None
    assert asyncio.run(web.fetch("file:///etc/passwd")) is None


def test_html_to_text_keeps_mailto():
    html = '<script>x</script><p>Hi</p><a href="mailto:sales@acme.io?subject=x">mail</a>'
    assert "sales@acme.io" in web.html_to_text(html)


def test_emails_only_on_company_domain():
    text = "write sales@acme.io or noreply@acme.io or bob@gmail.com or eng@mail.acme.io"
    assert contacts.emails_on_domain(text, "acme.io") == ["eng@mail.acme.io", "sales@acme.io"]


def test_matches_and_feed_parse():
    assert sources.matches("Senior AOSP engineer", ["aosp"]) and not sources.matches(
        "React dev", ["aosp"]
    )
    rss = "<rss><channel><item><title>T</title><link>http://x/1</link><description>&lt;b&gt;hi&lt;/b&gt;</description></item></channel></rss>"
    assert sources._parse_feed(rss) == [
        {"title": "T", "link": "http://x/1", "summary": "hi", "date": ""}
    ]
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="http://y/2"/></entry></feed>'
    assert sources._parse_feed(atom)[0]["link"] == "http://y/2"


def test_ddg_parse():
    html = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.io%2Fjobs">Acme <b>Jobs</b></a>'
    assert sources._ddg_results(html) == [("https://acme.io/jobs", "Acme Jobs")]


def test_signal_hash_stable():
    a = sources.Signal("job_post", "x", "http://u", "t", "body")
    assert a.hash == sources.Signal("job_post", "y", "http://u", "t2", "body").hash


def test_unsubscribe_token_matches_edge_function(monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setenv("UNSUBSCRIBE_BASE_URL", "https://x/unsub")
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", "s")
    expect = hmac.new(b"s", b"abc", hashlib.sha256).hexdigest()
    assert mailer.unsubscribe_url("abc") == f"https://x/unsub?e=abc&t={expect}"


def test_message_has_footer_and_unsubscribe_headers(monkeypatch):
    for k, v in {"UNSUBSCRIBE_BASE_URL": "https://x/u", "UNSUBSCRIBE_SECRET": "s", "MAIL_FROM": "a@b.io",
                 "COMPANY_NAME": "Acme Eng", "COMPANY_ADDRESS": "1 Road"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    msg = mailer.build_message({"id": "abc", "subject": "Hi", "body": "Hello"}, "to@c.io")
    assert "Unsubscribe:" in _part(msg, "plain") and "Acme Eng" in _part(msg, "html")
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_message_carries_unsubscribe_reply_to_and_threading_headers(monkeypatch):
    for k, v in {"UNSUBSCRIBE_BASE_URL": "https://x/u", "UNSUBSCRIBE_SECRET": "s", "MAIL_FROM": "a@b.io",
                 "COMPANY_NAME": "Acme Eng", "COMPANY_ADDRESS": "1 Road", "REPLY_TO": "r@in.b.io"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    row = {"id": "abc", "subject": "Re: Hi", "body": "Hello", "in_reply_to": "<m1@x>"}
    msg = mailer.build_message(row, "to@c.io")
    assert msg["To"] == "to@c.io" and msg["Reply-To"] == "r@in.b.io" and msg["From"] == "a@b.io"
    assert "Unsubscribe:" in _part(msg, "plain")
    assert msg["In-Reply-To"] == "<m1@x>" and msg["References"] == "<m1@x>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and msg["Message-ID"]


def test_slack_text_is_escaped_and_chunked():
    from agent import notify

    assert notify.esc("<!channel> a & b") == "&lt;!channel&gt; a &amp; b"
    parts = notify.chunks("\n\n".join(["p" * 1500] * 5), 2800)
    assert all(len(p) <= 2800 for p in parts) and len(parts) >= 3
    assert notify.chunks("x" * 7000, 2800) == ["x" * 2800, "x" * 2800, "x" * 1400]


def test_email_blocks_flag_problems_and_carry_approval_id():
    from agent import notify

    row = {"company": "Acme", "domain": "acme.io", "project_summary": "AAOS port", "technologies": ["AAOS"],
           "email": "hi@acme.io", "name": "", "role": "VP", "source_url": "http://s", "review_note": "banned phrase",
           "subject": "Hello", "body": "Body <b>"}  # fmt: skip
    _, blocks = notify.email_blocks(row, "email:ID-1")
    buttons = blocks[-1]["elements"]
    assert [b["action_id"] for b in buttons] == ["approve_email", "skip_email"]
    assert all(b["value"] == "email:ID-1" for b in buttons)
    assert "banned phrase" in blocks[0]["text"]["text"] and "&lt;b&gt;" in blocks[1]["text"]["text"]


def test_notify_is_noop_without_slack_token():
    from agent import notify

    assert asyncio.run(notify.sweep()) == {"emails": 0, "replies": 0, "posts": 0, "failed": 0}
    assert (
        asyncio.run(notify.post_email("x")) is False and asyncio.run(notify.post_blog("x")) is False
    )
    assert asyncio.run(notify.post_reply("x")) is False


def test_unbacked_experience_claims_are_flagged():
    from agent.tasks import proposal

    assert proposal._check_claims("Based on our experience, we\u2019ve delivered similar projects")
    assert not proposal._check_claims("We would start with a bring-up audit of your AOSP stack.")


def test_role_address_fallback_prefers_business_mailboxes():
    assert contacts.pick_role_address(["info@a.io", "sales@a.io", "bob@a.io"]) == "sales@a.io"
    assert contacts.pick_role_address(["info@a.io"]) == "info@a.io"
    assert contacts.pick_role_address(["jane.doe@a.io"]) is None


def test_do_not_scrape_portals_are_blocked():
    for url in (
        "https://www.linkedin.com/jobs/1",
        "https://in.indeed.com/x",
        "https://www.naukri.com/aosp",
        "https://indeed.co.uk/y",
    ):
        assert web.blocked(url)
        assert asyncio.run(web.fetch(url)) is None
    assert not web.blocked("https://acme.io/careers")


def test_portal_rate_limit(tmp_path, monkeypatch):
    from agent import portals

    monkeypatch.setattr(portals, "_CALLS", tmp_path / "c.json")
    assert portals.due("jobicy") is True
    assert portals.due("jobicy") is False  # called again within the hour
    assert (
        portals.due("greenhouse") is True and portals.due("greenhouse") is True
    )  # no limit configured


def test_company_board_parsers(monkeypatch):
    from agent import portals

    async def fake_json(url, **_):
        if "greenhouse" in url:
            return {"jobs": [{"title": "Android Platform Engineer", "absolute_url": "http://g/1", "content": "&lt;p&gt;AOSP work&lt;/p&gt;",
                              "location": {"name": "Berlin"}, "company_name": "Nuro"},
                             {"title": "Accountant", "absolute_url": "http://g/2", "content": "books"}]}  # fmt: skip
        if "lever" in url:
            return [{"text": "BSP engineer", "hostedUrl": "http://l/1", "descriptionPlain": "bring-up", "additionalPlain": "",
                     "categories": {"location": "Zurich"}}]  # fmt: skip
        return {
            "jobs": [
                {
                    "title": "Embedded Linux dev",
                    "jobUrl": "http://a/1",
                    "descriptionPlain": "yocto",
                    "location": "Remote",
                }
            ]
        }

    monkeypatch.setattr(portals, "_json", fake_json)
    kws = ["aosp", "bsp", "embedded linux"]
    boards = {
        "greenhouse": [{"board": "nuro", "name": "Nuro", "domain": "nuro.ai"}],
        "lever": ["zoox"],
        "ashby": ["skydio"],
    }
    sigs = asyncio.run(portals.company_boards(boards, kws))
    assert [s.source for s in sigs] == ["greenhouse", "lever", "ashby"]
    assert (
        sigs[0].domain_hint == "nuro.ai"
        and "Company: Nuro" in sigs[0].text
        and sigs[0].url == "http://g/1"
    )


def test_balance_caps_per_company_and_interleaves_sources():
    nuro = [
        sources.Signal("job_post", "greenhouse", f"http://g/{i}", "t", f"x{i}", "Nuro")
        for i in range(6)
    ]
    others = [
        sources.Signal("job_post", "ashby", f"http://a/{i}", "t", f"y{i}", f"Co{i}")
        for i in range(3)
    ]
    out = sources.balance(nuro + others, 5)
    assert len(out) == 5 and [s.source for s in out].count("greenhouse") == 2
    assert out[0].source == "greenhouse" and out[1].source == "ashby"


def test_contact_form_detection():
    form = '<form><input name="email"><textarea name="msg"></textarea></form>'
    assert web.has_contact_form(form)
    assert not web.has_contact_form(
        '<form role="search"><input name="q"><textarea></textarea></form>'
    )
    assert not web.has_contact_form('<form><input type="password"><textarea></textarea></form>')
    assert not web.has_contact_form('<form><input name="email"></form>')  # newsletter box


def test_form_field_kinds():
    from agent import formfill

    assert formfill._kind("your-email", "input", "text") == "email"
    assert formfill._kind("full name", "input", "text") == "name"
    assert formfill._kind("", "textarea", "text") == "message"
    assert formfill._kind("phone", "input", "tel") is None


def test_overlong_hostname_is_refused_not_crashed():
    assert asyncio.run(web.fetch("https://" + "a" * 70 + ".com")) is None


def test_project_filter_drops_permanent_jobs():
    from agent import sources

    pk = ["contract", "freelance"]
    mk = lambda kind, text: sources.Signal(kind, "x", "u", "t", text)  # noqa: E731
    assert sources.is_project_work(mk("job_post", "AOSP contract engineer, 6 months"), pk)
    assert not sources.is_project_work(mk("job_post", "Full-time AOSP engineer, salary 120k"), pk)
    assert not sources.is_project_work(mk("job_post", "AOSP engineer wanted"), pk)
    assert sources.is_project_work(mk("project_post", "SEEKING FREELANCER: AOSP"), pk)
    assert sources.is_project_work(mk("company_page", "RFID readers"), pk)


def test_old_posts_are_dropped():
    from datetime import UTC, datetime

    from agent import sources

    now = datetime(2026, 10, 2, tzinfo=UTC).timestamp()

    def mk(text, **kw):
        return sources.Signal(kw.pop("kind", "web_page"), "x", kw.pop("url", "u"), "t", text, **kw)

    old = datetime(2025, 8, 1, tzinfo=UTC).timestamp()
    new = datetime(2026, 9, 20, tzinfo=UTC).timestamp()
    assert sources.is_stale(mk("x", published=old), 45, now)
    assert not sources.is_stale(mk("x", published=new), 45, now)
    assert sources.is_stale(mk("Posted March 12, 2025. Contract AOSP engineer"), 45, now)
    assert sources.is_stale(mk("project", url="https://a.io/blog/2025/03/aosp"), 45, now)
    assert not sources.is_stale(mk("Posted September 2026. Contract AOSP"), 45, now)
    assert not sources.is_stale(mk("Contract AOSP engineer, remote"), 45, now)  # no date: kept
    assert not sources.is_stale(
        mk("Founded March 2012. RFID readers", kind="company_page"), 45, now
    )


def test_bot_check_pages_are_recognised():
    cf = "<html><title>Just a moment...</title><body>Verifying you are human. This may take a few seconds.</body></html>"
    assert web.bot_check(cf)
    assert not web.bot_check(
        "<html><body>" + "Contact us at sales@acme.io. " * 20 + "</body></html>"
    )


def test_test_recipient_accepts_several_addresses(monkeypatch):
    monkeypatch.setenv("TEST_RECIPIENT", " a@x.io, b@y.io ,,c@z.io ")
    assert mailer.test_recipient() == "a@x.io, b@y.io, c@z.io"
    monkeypatch.setenv("UNSUBSCRIBE_BASE_URL", "https://x/u")
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", "s")
    monkeypatch.setenv("MAIL_FROM", "f@b.io")
    monkeypatch.setenv("COMPANY_NAME", "Co")
    monkeypatch.setenv("COMPANY_ADDRESS", "addr")
    to = mailer.test_recipient() or ""
    # the Gmail transport sends one message per address of the list
    msgs = [
        mailer.build_message({"id": "1", "subject": "S", "body": "B"}, a.strip())
        for a in to.split(",")
    ]
    assert [m["To"] for m in msgs] == ["a@x.io", "b@y.io", "c@z.io"]


def _mail_env(monkeypatch):
    for k, v in {"UNSUBSCRIBE_BASE_URL": "https://x/u", "UNSUBSCRIBE_SECRET": "s", "MAIL_FROM": "a@b.io",
                 "COMPANY_NAME": "Acme Eng", "COMPANY_ADDRESS": "1 Road", "SENDER_NAME": "Ana",
                 "COMPANY_WEBSITE": "acme.io"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)


def test_email_is_html_with_a_clean_text_alternative(monkeypatch):
    _mail_env(monkeypatch)
    body = ("We noted that Kestrel builds the Android\nkiosk terminal.\n\nWe could help with:\n- BSP bring-up\n"
            "- HAL work\n\nWould a short call help?")  # fmt: skip
    msg = mailer.build_message({"id": "abc", "subject": "Hi", "body": body}, "to@c.io")
    assert msg.get_content_type() == "multipart/alternative"
    text = _part(msg, "plain")
    assert (
        "Android kiosk terminal." in text
    )  # the model's line wrapping is joined, not shown as broken lines
    assert "- BSP bring-up\n- HAL work" in text and "Unsubscribe: https://x/u?e=abc" in text
    html = _part(msg, "html")
    assert "<li" in html and "BSP bring-up" in html and "Android kiosk terminal." in html
    assert "https://x/u?e=abc&amp;t=" in html and "1 Road" in html and "https://acme.io" in html
    assert "{{" not in html  # every placeholder filled


def test_model_text_cannot_inject_html(monkeypatch):
    _mail_env(monkeypatch)
    body = 'Hello <script>alert(1)</script> <a href="http://evil">click</a>'
    html = _part(
        mailer.build_message({"id": "abc", "subject": "<b>x</b>", "body": body}, "to@c.io"), "html"
    )
    assert "<script>" not in html and 'href="http://evil"' not in html and "&lt;script&gt;" in html


def test_custom_template_file(monkeypatch, tmp_path):
    _mail_env(monkeypatch)
    t = tmp_path / "t.html"
    t.write_text("<p>{{company_name}}</p>{{body}}<a href='{{unsubscribe_url}}'>u</a>")
    monkeypatch.setenv("EMAIL_TEMPLATE", str(t))
    html = _part(
        mailer.build_message({"id": "abc", "subject": "S", "body": "Hi"}, "to@c.io"), "html"
    )
    assert html.startswith("<p>Acme Eng</p>") and "<p" in html and "u</a>" in html


def test_signature_shows_only_what_is_set_and_the_letter_has_no_banner(monkeypatch):
    _mail_env(monkeypatch)
    monkeypatch.setenv("SENDER_TITLE", "Embedded Engineering")
    monkeypatch.delenv("COMPANY_PHONE", raising=False)
    msg = mailer.build_message(
        {"id": "abc", "subject": "S", "body": "Hello,\n\nA line."}, "to@c.io"
    )
    html, text = _part(msg, "html"), _part(msg, "plain")
    assert (
        "Best regards," in html
        and "<strong>Ana</strong>" in html
        and "Embedded Engineering" in html
    )
    assert (
        'href="https://acme.io"' in html and "border-bottom:3px" not in html
    )  # no newsletter banner
    assert "Best regards,\nAna\nEmbedded Engineering\nAcme Eng\nacme.io" in text
    monkeypatch.delenv("COMPANY_WEBSITE")
    monkeypatch.delenv("SENDER_TITLE")
    bare = _part(
        mailer.build_message({"id": "abc", "subject": "S", "body": "Hi"}, "to@c.io"), "plain"
    )
    assert "Best regards,\nAna\nAcme Eng\n\n--" in bare  # nothing empty is printed


def test_identity_problems_name_placeholders_and_missing_fields(monkeypatch):
    for k, v in {"SENDER_NAME": "", "COMPANY_WEBSITE": "", "COMPANY_ADDRESS": "address"}.items():
        monkeypatch.setenv(k, v)
    assert len(mailer.identity_problems()) == 3
    _mail_env(monkeypatch)
    assert mailer.identity_problems() == []
