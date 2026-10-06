import asyncio
from datetime import date

from agent import blog, contacts, mailer, sources, store, web
from agent.tasks import topics


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


def test_topic_similarity():
    assert blog.too_similar(
        "Bringing up a custom board on AOSP", ["Bringing up custom boards on AOSP"]
    )
    assert not blog.too_similar("Yocto layer hygiene", ["Bringing up a custom board on AOSP"])


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
    assert "Unsubscribe:" in msg.get_content() and "Acme Eng" in msg.get_content()
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_message_carries_unsubscribe_reply_to_and_threading_headers(monkeypatch):
    for k, v in {"UNSUBSCRIBE_BASE_URL": "https://x/u", "UNSUBSCRIBE_SECRET": "s", "MAIL_FROM": "a@b.io",
                 "COMPANY_NAME": "Acme Eng", "COMPANY_ADDRESS": "1 Road", "REPLY_TO": "r@in.b.io"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    row = {"id": "abc", "subject": "Re: Hi", "body": "Hello", "in_reply_to": "<m1@x>"}
    msg = mailer.build_message(row, "to@c.io")
    assert msg["To"] == "to@c.io" and msg["Reply-To"] == "r@in.b.io" and msg["From"] == "a@b.io"
    assert "Unsubscribe:" in msg.get_content()
    assert msg["In-Reply-To"] == "<m1@x>" and msg["References"] == "<m1@x>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and msg["Message-ID"]


def test_blog_is_skipped_when_today_exists(monkeypatch):
    async def exists(_):
        return True

    monkeypatch.setattr(store, "blog_exists", exists)
    assert asyncio.run(blog.run(date(2026, 1, 1))) == {"processed": 0, "skipped": 1}


def test_blog_picks_first_new_and_well_sourced_topic(monkeypatch):
    saved = {}

    def topic(title, ids):
        return topics.Topic(
            title=title, angle="a", keywords=[], why_now="w", kind="news_analysis", source_ids=ids
        )

    async def no(_):
        return False

    async def recent(*_):
        return ["Yocto vs Buildroot in 2026"]

    item = sources.Item("hn", "Android 17 news", "http://x/1", "s", score=500)

    async def items():
        return [item]

    async def plan(_):
        return topics.TopicPlan(topics=[
            topic("Yocto vs Buildroot in 2026!", [1]),  # repeat of a recent title
            topic("Thin topic", [9]),  # cites no real item, so there are no sources
            topic("GKI vendor module checklist", [1]),
        ])  # fmt: skip

    async def page(_url):
        return "Android 17 adds APIs. " * 120

    async def draft(t, grounding=""):
        saved["topic"], saved["grounding"] = t, grounding

        class P:
            title, tags = "T", ["gki"]
            body_markdown = "B" * 900

        return P(), []

    async def save(day, topic, post, meta, status):
        saved["picked"], saved["body"], saved["kind"] = (
            topic.title,
            post.body_markdown,
            meta["kind"],
        )
        return "id"

    async def no_variants(*_a, **_k):
        return {}

    monkeypatch.setattr(store, "blog_exists", no)
    monkeypatch.setattr(store, "recent_post_titles", recent)
    monkeypatch.setattr(store, "save_blog", save)
    monkeypatch.setattr(blog.research, "research_items", items)
    monkeypatch.setattr(blog.topics_task, "plan_topics", plan)
    monkeypatch.setattr(blog.web, "fetch_page_text", page)
    monkeypatch.setattr(blog.variants, "generate", no_variants)
    monkeypatch.setattr(blog.blog_task, "draft_post", draft)
    assert asyncio.run(blog.run(date(2026, 1, 1)))["processed"] == 1
    assert saved["picked"] == "GKI vendor module checklist" and saved["kind"] == "news_analysis"
    assert "SOURCES:" in saved["topic"] and "Android 17 adds APIs" in saved["grounding"]
    assert (
        saved["body"].endswith("- [Android 17 news](http://x/1)") and "## Sources" in saved["body"]
    )


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


def test_research_ranking_prefers_popular_and_fresh_and_drops_stale():
    import time

    from agent import research

    now = time.time()
    hot = sources.Item(
        "hn", "Android 17 drops AOSP source", "u1", score=900, comments=300, published=now - 86400
    )
    old_hot = sources.Item("hn", "Old viral post", "u2", score=900, published=now - 40 * 86400)
    quiet = sources.Item("feed", "Minor Yocto note", "u3", published=now - 86400)
    ranked = research.rank([quiet, old_hot, hot], now)
    assert [i.url for i in ranked] == ["u1", "u3"]


def test_recurring_terms_and_dates():
    from agent import research

    titles = [
        "Android 17 changes",
        "Why Android 17 matters",
        "Android 17 and AOSP",
        "Yocto release",
    ]
    items = [sources.Item("x", t, str(n)) for n, t in enumerate(titles)]
    assert ("android 17", 3) in research.recurring_terms(items)
    assert research.parse_date("Wed, 30 Sep 2026 10:00:00 +0000") and research.parse_date(
        "2026-09-30T10:00:00Z"
    )
    assert research.parse_date("not a date") is None


def test_platform_format_checks_and_render():
    from agent.tasks import adapt

    li = {"render": "linkedin_post", "max_chars": 2800, "tags_max": 5, "label": "LinkedIn"}
    body = (
        "Android 17 changes who gets new platform APIs.\n\nOEM teams should check their release plans.\n\nWhat is your plan? "
        + "x" * 80
    )
    good = adapt.Variant(
        title="", description="", body=body, tags=["Android", "GKI", "embedded-linux"]
    )
    text = adapt.render(li, good)
    assert text.endswith("#android #gki #embeddedlinux") and not adapt.check_format(li, good)
    bad = adapt.Variant(
        title="", description="", body="## Heading\n\n**bold** `code` " + "y" * 120, tags=["a"]
    )
    problems = adapt.check_format(li, bad)
    assert any("markdown" in p for p in problems) and any("hashtags" in p for p in problems)

    dv = {"render": "devto_frontmatter", "max_chars": 9000, "tags_max": 4, "label": "dev.to"}
    v = adapt.Variant(title="T", description="d" * 200, body="intro\n\n```bash\nls\n" + "z" * 120,
                      tags=["Android", "linux-kernel", "a b", "x", "y"])  # fmt: skip
    assert adapt.clean_tags(v.tags, 4) == ["android", "linuxkernel", "ab", "x"]
    rendered = adapt.render(dv, v)
    assert (
        rendered.startswith("---\ntitle: T\npublished: false")
        and "tags: android, linuxkernel, ab, x" in rendered
    )
    probs = adapt.check_format(dv, v)
    assert all(
        any(k in p for p in probs) for k in ("description", "unclosed", "TL;DR", "Key points")
    )
    ok = adapt.Variant(title="T", description="short", tags=["android"],
                       body="TL;DR: two sentences here.\n\n## Why this matters\n\nx\n\n## Key points\n\n- a\n- b\n" + "z" * 100)  # fmt: skip
    assert not adapt.check_format(dv, ok)


def test_x_thread_checks():
    from agent.tasks import adapt

    spec = {"render": "x_thread", "max_chars": 2400, "tags_max": 2, "label": "X"}
    posts = "\n\n".join(
        f"{i}/ point number {i} about the platform change and what teams should do"
        for i in range(1, 7)
    )
    ok = adapt.Variant(title="", description="", body=posts, tags=["android"])
    assert not adapt.check_format(spec, ok)
    long_first = adapt.Variant(
        title="", description="", body=posts.replace("1/ point", "1/ " + "w" * 300), tags=[]
    )
    assert any("max 270" in p for p in adapt.check_format(spec, long_first))


def test_grounding_flags_invented_identifiers_and_leaks():
    from agentkit import checks

    source = "Android 17 QPR1 adds APIs. Set CONFIG_KPROBES=y and read /sys/kernel/tracing."
    ok = "On Android 17, enable CONFIG_KPROBES and read /sys/kernel/tracing."
    assert not checks.check_grounded(ok, source)
    bad = "Use CONFIG_KDUMP, boot with --crash-kernel and open /var/lib/kdump on Linux 7.4."
    flagged = " ".join(checks.check_grounded(bad, source))
    assert all(
        x in flagged for x in ("CONFIG_KDUMP", "--crash-kernel", "/var/lib/kdump", "Linux 7.4")
    )
    assert checks.check_no_leaks("Reference material: foo") and not checks.check_no_leaks(
        "plain text"
    )


def test_draft_repairs_once_and_keeps_the_better_draft(monkeypatch):
    from agent.tasks import blog as blog_task

    calls = []

    async def once(topic, sources_text, feedback):
        calls.append(feedback)
        return ("draft", ["invented: CONFIG_X"]) if feedback is None else ("fixed", [])

    monkeypatch.setattr(blog_task, "_once", once)
    post, problems = asyncio.run(blog_task.draft_post("topic", "sources"))
    assert (post, problems) == ("fixed", []) and calls[0] is None and "CONFIG_X" in calls[1]

    async def worse(topic, sources_text, feedback):
        return ("a", ["p1"]) if feedback is None else ("b", ["p1", "p2"])

    monkeypatch.setattr(blog_task, "_once", worse)
    assert asyncio.run(blog_task.draft_post("t", "s")) == ("a", ["p1"])


def test_linkedin_and_thread_layout_fixes():
    from agent.tasks import adapt

    li = {"render": "linkedin_post", "max_chars": 2800, "tags_max": 5, "label": "LinkedIn"}
    raw = ("Android 17 changed who gets new APIs. OEM teams get them late. Backporting costs time. "
           "Security fixes lag too. How do you plan for it?\n\nandroid automotive embedded\n\n#android #aosp #bsp")  # fmt: skip
    v = adapt.Variant(
        title="", description="", body=raw, tags=["android", "automotive", "embedded"]
    )
    text = adapt.render(li, v)
    assert "automotive embedded" not in text
    assert text.split("\n\n")[0] == "Android 17 changed who gets new APIs."
    assert text.endswith("#android #aosp #bsp") and not adapt.check_format(li, v)

    x = {"render": "x_thread", "max_chars": 2400, "tags_max": 2, "label": "X"}
    body = "\n\n".join(
        [
            "1/ one point here",
            "two points made",
            "3) three of them",
            "4. four of them",
            "five more things",
            "six and done",
            "Last point? #android " + "w" * 90,
        ]
    )
    out = adapt.render(x, adapt.Variant(title="", description="", body=body, tags=[]))
    assert [p.split(" ")[0] for p in out.split("\n\n")] == [f"{i}/" for i in range(1, 8)]
    assert not adapt.check_format(x, adapt.Variant(title="", description="", body=body, tags=[]))


def test_linkedin_drops_keyword_line_above_hashtags():
    from agent.tasks import adapt

    li = {"render": "linkedin_post", "max_chars": 2800, "tags_max": 5, "label": "LinkedIn"}
    body = ("Hook line about the change.\n\nSecond paragraph explains what it means for teams in detail here.\n\n"
            "What is your plan?\n\nAOSP updates, security bulletins, and feature access\n\n#android #aosp #security")  # fmt: skip
    text = adapt.render(li, adapt.Variant(title="", description="", body=body, tags=["android"]))
    assert (
        "feature access" not in text and "What is your plan?" in text and text.endswith("#security")
    )


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
