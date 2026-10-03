"""CLI: python -m agent run | dryrun | browse | fill-form | send | review | approve | reject."""

"""CLI: python -m agent run | dryrun | send | replies | followups | review | approve | reject."""

import argparse
import asyncio
import json
import os
import sys

from agentkit import db


async def _main(argv: list[str]) -> int:  # noqa: PLR0912, PLR0915
    ap = argparse.ArgumentParser(prog="agent", description="Daily AOSP/embedded lead + blog agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser(
        "run",
        help="one bounded daily run (replies -> send approved -> follow-ups -> leads -> blog)",
    )
    r.add_argument("--only", choices=["replies", "send", "followups", "leads", "blog"])
    r.add_argument("--force", action="store_true", help="run even if today's run already succeeded")
    r.add_argument(
        "--redo-blog",
        action="store_true",
        help="DELETE today's saved blog post (and its platform versions), then write a new one",
    )
    r.add_argument(
        "--visible", action="store_true", help="do the web work in a visible Chrome window (Google)"
    )
    d = sub.add_parser(
        "dryrun", help="live scraping + real LLM, no database, no email; writes out/*.md"
    )
    d.add_argument("--visible", action="store_true", help="use a visible Chrome window (Google)")
    d.add_argument("--leads", type=int, default=3)
    d.add_argument("--signals", type=int, default=40)
    d.add_argument("--no-blog", action="store_true")
    d.add_argument(
        "--blog-only",
        action="store_true",
        help="skip lead search; research, write and adapt one post",
    )
    sub.add_parser("migrate", help="apply supabase/migrations/*.sql to DATABASE_URL (idempotent)")
    sub.add_parser(
        "notify", help="post unreviewed drafts to Slack (done automatically after each run)"
    )
    sub.add_parser(
        "slack-setup",
        help="create the Slack channels and invite approvers (needs extra bot scopes)",
    )
    vp = sub.add_parser(
        "variants", help="print the per-platform versions of a blog post (default: latest)"
    )
    vp.add_argument("post_id", nargs="?")
    sub.add_parser("check", help="preflight: LLM, database, Slack, search, email switch")
    dm = sub.add_parser(
        "demo", help="check -> migrate -> full run -> Slack -> print what was produced"
    )
    dm.add_argument(
        "--no-run", action="store_true", help="only the checks and the summary of existing data"
    )
    ff = sub.add_parser(
        "fill-form",
        help="open a company's contact form in Chrome, pre-filled with a draft; you review and send",
    )
    ff.add_argument("company", help="domain or id of a company that has a contact form")
    br = sub.add_parser(
        "browse", help="watch a visible Chromium search for a company and read its contact details"
    )
    br.add_argument("company", help="company name or domain")
    gf = sub.add_parser(
        "gui-find",
        help="a vision model drives the sandbox Chrome to find a company's contact; the lead goes to Slack",
    )
    gf.add_argument(
        "companies",
        nargs="+",
        help="company names, e.g. 'ID Tech Solutions' (all done in one batch)",
    )
    gf.add_argument(
        "--dry", action="store_true", help="only print the verified contact: no database, no Slack"
    )
    sp = sub.add_parser(
        "gui-spike",
        help="run short fixed browser tasks with the vision model and report the success rate",
    )
    sp.add_argument("--tasks", type=int, default=10)
    sub.add_parser(
        "manual",
        help="companies to contact by hand: bot-protected sites and contact-form-only sites",
    )
    sn = sub.add_parser("send", help="send approved emails now")
    sn.add_argument(
        "--watch",
        action="store_true",
        help="keep running and send each email within seconds of its Slack approval",
    )
    sn.add_argument("--interval", type=float, default=5, help="seconds between checks (--watch)")
    te = sub.add_parser(
        "test-email",
        help="LLM writes sample outreach emails: posted to Slack for approval (or --direct send)",
    )
    te.add_argument(
        "--direct", action="store_true", help="skip Slack/database, send now to TEST_RECIPIENT"
    )
    te.add_argument("--count", type=int, default=1, help="how many sample emails (1-3)")
    te.add_argument("--to", help="comma separated override for TEST_RECIPIENT")
    sub.add_parser("replies", help="classify new replies and queue approved answers now")
    sub.add_parser("followups", help="draft follow-ups for unopened, unanswered intros now")
    sub.add_parser("review", help="list drafted emails awaiting approval")
    s = sub.add_parser("show", help="show one draft in full")
    s.add_argument("id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="*")
        p.add_argument("--all", action="store_true", help="every drafted email")
    a = ap.parse_args(argv)
    if getattr(a, "visible", False):
        os.environ["BROWSER_VISIBLE"] = "1"

    try:
        if a.cmd not in (
            "dryrun",
            "check",
            "slack-setup",
            "browse",
            "test-email",
        ) and not os.environ.get("DATABASE_URL"):
            print(  # noqa: T201
                f"'{a.cmd}' needs a database: set DATABASE_URL in .env (see README), "
                "or use `python -m agent dryrun`, which needs none."
            )
            return 2
        if a.cmd == "run":
            from . import run  # noqa: PLC0415

            result = await run.daily_run(force=a.force, only=a.only, redo_blog=a.redo_blog)
            print(json.dumps(result, default=str))  # noqa: T201
        elif a.cmd == "dryrun":
            from . import dryrun  # noqa: PLC0415

            print(f"report: {await dryrun.run(a.leads, a.signals, not a.no_blog, not a.blog_only)}")  # noqa: T201
        elif a.cmd == "migrate":
            from . import migrate  # noqa: PLC0415

            print(f"applied: {await migrate.apply() or 'nothing new'}")  # noqa: T201
        elif a.cmd == "slack-setup":
            from . import slack_setup  # noqa: PLC0415

            print("\n".join(await slack_setup.run()))  # noqa: T201
        elif a.cmd == "variants":
            rows = await db.fetch(
                """select p.title, v.platform, v.body, v.review_note from content_variants v
                     join content_posts p on p.id=v.post_id
                    where p.id = coalesce($1::uuid, (select id from content_posts order by created_at desc limit 1))
                    order by v.platform""",
                a.post_id,
            )
            for r in rows:
                note = f"  CHECKS: {r['review_note']}" if r["review_note"] else ""
                print(f"\n===== {r['platform']}  (post: {r['title']}){note}\n{r['body']}")  # noqa: T201
            if not rows:
                print("no variants found")  # noqa: T201
        elif a.cmd == "check":
            from . import demo  # noqa: PLC0415

            return 0 if await demo.preflight() else 1
        elif a.cmd == "demo":
            from . import demo  # noqa: PLC0415

            return await demo.run(execute=not a.no_run)
        elif a.cmd == "notify":
            from . import notify  # noqa: PLC0415

            print(json.dumps(await notify.sweep()))  # noqa: T201
        elif a.cmd == "manual":
            from . import store  # noqa: PLC0415

            rows = await store.companies_to_contact_manually()
            for r in rows:
                how = r["manual_reason"] or f"contact form: {r['contact_form_url']}"
                print(f"{r['name']}  https://{r['domain']}  [{how}]")  # noqa: T201
            if not rows:
                print("nothing to contact by hand")  # noqa: T201
        elif a.cmd == "browse":
            from . import browse  # noqa: PLC0415

            print(await browse.lookup(a.company))  # noqa: T201
        elif a.cmd == "fill-form":
            from . import formfill  # noqa: PLC0415

            print(await formfill.run(a.company))  # noqa: T201
        elif a.cmd == "gui-find":
            from .gui import lead  # noqa: PLC0415

            if a.dry:
                results = [(n, await lead.find_company(n), "") for n in a.companies]
            else:
                results = await lead.find_many(a.companies)
            for name, found, report in results:
                print(f"\n{name}: {found.message}")  # noqa: T201
                if found.ok:
                    print(f"  {found.email}  ({found.role or 'role unknown'})  {found.page_url}")  # noqa: T201
                    if report:
                        print(f"  {report}")  # noqa: T201
            return 0 if all(f.ok for _, f, _ in results) else 1
        elif a.cmd == "gui-spike":
            from .gui import spike  # noqa: PLC0415

            return await spike.run(a.tasks)
        elif a.cmd == "test-email":
            from . import test_email  # noqa: PLC0415

            return await test_email.run(a.count, a.to, a.direct)
        elif a.cmd == "replies":
            from . import replies  # noqa: PLC0415

            print(json.dumps(await replies.run()))  # noqa: T201
        elif a.cmd == "followups":
            from . import followups  # noqa: PLC0415

            print(json.dumps(await followups.run()))  # noqa: T201
        elif a.cmd == "send":
            from . import mailer  # noqa: PLC0415

            if a.watch:
                print(f"watching for approved emails every {a.interval:g}s; Ctrl+C to stop")  # noqa: T201
                while True:
                    stats = await mailer.send_approved()
                    if stats.get("disabled"):
                        print("EMAIL_SENDING_ENABLED is not true; stopping")  # noqa: T201
                        return 1
                    if stats["sent"] or stats["failed"] or stats["skipped"]:
                        print(json.dumps(stats))  # noqa: T201
                    await asyncio.sleep(a.interval)
            print(json.dumps(await mailer.send_approved()))  # noqa: T201
        else:
            from . import review  # noqa: PLC0415

            if a.cmd == "review":
                for d in await review.list_drafts():
                    flag = f"  [CHECK: {d['review_note']}]" if d["review_note"] else ""
                    print(f"{d['id']}  {d['company']} <{d['email']}>  {d['subject']}{flag}")  # noqa: T201
            elif a.cmd == "show":
                d = await review.show(a.id)
                print(dict(d) if d else "not found")  # noqa: T201
                if d:
                    print(f"\n{d['subject']}\n\n{d['body']}")  # noqa: T201
            else:
                ids = [str(d["id"]) for d in await review.list_drafts()] if a.all else a.ids
                print(f"{await review.decide(ids, a.cmd == 'approve')} {a.cmd}d")  # noqa: T201
    finally:
        from . import chrome  # noqa: PLC0415

        await chrome.close()
        await db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
