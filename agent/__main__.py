"""CLI. Lead pipeline: `python -m agent leads run|review|show|approve|reject|queries [--dry-run]`,
`python -m agent send [--dry-run]`. Daily run: `python -m agent run`. See README."""

import argparse
import asyncio
import json
import os
import sys
import time

from agentkit import db


async def _main(argv: list[str]) -> int:  # noqa: PLR0911, PLR0912, PLR0915
    ap = argparse.ArgumentParser(prog="agent", description="Daily AOSP/embedded lead + blog agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser(
        "run",
        help="one bounded daily run (replies -> send approved -> follow-ups -> leads -> blog)",
    )
    r.add_argument("--only", choices=["inbox", "replies", "send", "followups", "leads", "blog"])
    r.add_argument("--force", action="store_true", help="run even if today's run already succeeded")
    r.add_argument(
        "--redo-blog",
        action="store_true",
        help="DELETE today's saved blog post (and its platform versions), then write a new one",
    )
    r.add_argument(
        "--browser", choices=["http", "chrome", "desktop"], help="how lead research reaches the web"
    )
    r.add_argument("--desktop", action="store_true", help="same as --browser desktop")
    lp = sub.add_parser("leads", help="lead pipeline: discover, review, approve (see README)")
    lsub = lp.add_subparsers(dest="leads_cmd", required=True)
    lr = lsub.add_parser(
        "run", help="one discovery run: search -> qualify -> contact -> draft -> approval"
    )
    lr.add_argument("--max-leads", type=int, help="stop after this many new drafted leads")
    lr.add_argument("--minutes", type=float, default=60, help="time budget")
    lr.add_argument(
        "--project",
        help="YAML with project_context (products, technologies...): context only, not the target",
    )
    lsub.add_parser("review", help="drafted emails waiting for a decision")
    lsh = lsub.add_parser("show", help="one draft with its lead, evidence and approval")
    lsh.add_argument("email_id")
    for name in ("approve", "reject"):
        lpd = lsub.add_parser(name, help=f"{name} drafted emails (records who decided)")
        lpd.add_argument("ids", nargs="*")
        lpd.add_argument("--all", action="store_true", help="every drafted email")
    lc = lsub.add_parser(
        "check-domains",
        help="find stored leads whose website is dead or not the company's (report only)",
    )
    lc.add_argument(
        "--apply", action="store_true", help="reject leads whose stored website does not answer"
    )
    lq = lsub.add_parser("queries", help="print the next search queries the strategy would run")
    lq.add_argument("--count", type=int, default=20)
    lq.add_argument("--project", help="YAML with project_context, to preview its queries")
    for sp_ in lsub.choices.values():
        sp_.add_argument(
            "--dry-run",
            action="store_true",
            help="local SQLite store, Slack written to files, no email can leave",
        )
        sp_.add_argument("--browser", choices=["http", "chrome", "desktop"])
    sub.add_parser(
        "desktop-check", help="check the desktop tools Chrome automation needs (xdotool, ...)"
    )
    el = sub.add_parser(
        "embedded-list",
        help="research list: embedded companies, public emails and openings -> out/embedded_companies/*.xlsx",
    )
    el.add_argument(
        "--no-discover",
        action="store_true",
        help="only the seeds in config/embedded_companies.yaml",
    )
    el.add_argument("--limit", type=int, help="research at most this many companies in this run")
    el.add_argument(
        "--fresh", action="store_true", help="start over: delete the progress files first"
    )
    el.add_argument(
        "--report-only",
        action="store_true",
        help="verify and rebuild the workbook from progress files",
    )
    sub.add_parser(
        "start",
        help="the whole agent as one long-running service: send approved emails, poll Gmail, daily leads + blog",
    )
    pc = sub.add_parser(
        "pipeline",
        help="what the dashboard buttons do: start / stop the pipeline (the service applies it within seconds)",
    )
    pc.add_argument("action", choices=["start", "stop", "status"], nargs="?", default="status")
    sub.add_parser(
        "api-credentials",
        help="new password for the pipeline API's limited database login; prints its DATABASE_URL (for Render)",
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
    pk = sub.add_parser(
        "popup-check",
        help="open sites in the desktop Chrome and close their popups; before/after screenshots, no database",
    )
    pk.add_argument("urls", nargs="+")
    fc = sub.add_parser(
        "form-check",
        help="look for a contact form on company sites and post it to Slack (#form-fill); no database",
    )
    fc.add_argument("domains", nargs="+", help="company domains, e.g. acme-ev.com")
    fc.add_argument("--browser", choices=["http", "chrome", "desktop"])
    fc.add_argument("--no-slack", action="store_true", help="only look, post nothing")
    fc.add_argument(
        "--post-anyway", action="store_true", help="post the home page even if no form was found"
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
    sn.add_argument(
        "--dry-run", action="store_true", help="dry-run store; mail goes to the outbox folder"
    )
    te = sub.add_parser(
        "test-email",
        help="LLM drafts sample outreach emails for made-up companies; each goes through approval",
    )
    te.add_argument("--count", type=int, default=1, help="how many sample emails (1-3)")
    te.add_argument("--dry-run", action="store_true", help="dry-run store and simulated Slack")
    sub.add_parser("inbox", help="poll Gmail now: store new replies, mark bounces")
    sub.add_parser(
        "gmail-check",
        help="log in to Gmail once (opens a browser, saves token.json) and show the account",
    )
    sub.add_parser("replies", help="classify new replies and queue approved answers now")
    sub.add_parser("followups", help="draft follow-ups for unopened, unanswered intros now")
    sub.add_parser("review", help="same as `leads review`")
    s = sub.add_parser("show", help="same as `leads show`")
    s.add_argument("email_id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"same as `leads {name}`")
        p.add_argument("ids", nargs="*")
        p.add_argument("--all", action="store_true", help="every drafted email")
    a = ap.parse_args(argv)
    if getattr(a, "desktop", False):
        a.browser = "desktop"
    if getattr(a, "browser", None):
        os.environ["BROWSER_BACKEND"] = a.browser
    if a.cmd in ("review", "show", "approve", "reject"):  # old spellings of the `leads` subcommands
        a.leads_cmd, a.cmd, a.dry_run = a.cmd, "leads", False

    try:
        dry = bool(getattr(a, "dry_run", False))
        if (
            a.cmd
            not in (
                "check",
                "desktop-check",
                "popup-check",
                "form-check",
                "slack-setup",
                "embedded-list",
                "gmail-check",
            )
            and not dry
            and not os.environ.get("DATABASE_URL")
        ):
            print(  # noqa: T201
                f"'{a.cmd}' needs a database: set DATABASE_URL in .env (see README), "
                "or add --dry-run to use a local SQLite store."
            )
            return 2
        if a.cmd == "leads":
            return await _leads(a, dry)
        if a.cmd == "start":
            from . import supervisor  # noqa: PLC0415

            return await supervisor.start()
        if a.cmd == "run":
            from . import run  # noqa: PLC0415

            result = await run.daily_run(force=a.force, only=a.only, redo_blog=a.redo_blog)
            print(json.dumps(result, default=str))  # noqa: T201
        elif a.cmd == "desktop-check":
            from .gui import desktop  # noqa: PLC0415

            checks = desktop.preflight()
            for ok, name, detail, required in checks:
                print(f"{'ok  ' if ok else 'FAIL' if required else 'warn'}  {name}: {detail}")  # noqa: T201
            return 0 if all(ok for ok, _, _, required in checks if required) else 1
        elif a.cmd == "embedded-list":
            from . import embedded_list  # noqa: PLC0415

            summary = await (
                embedded_list.finish()
                if a.report_only
                else embedded_list.run(discover=not a.no_discover, limit=a.limit, fresh=a.fresh)
            )
            print(json.dumps(summary, indent=2))  # noqa: T201
        elif a.cmd == "pipeline":
            from . import control  # noqa: PLC0415

            if a.action != "status":
                state = "running" if a.action == "start" else "stopped"
                await control.request(state, f"cli:{os.environ.get('USER', 'unknown')}")
            print(json.dumps(await control.read(), indent=2, default=str))  # noqa: T201
        elif a.cmd == "api-credentials":
            from . import control  # noqa: PLC0415

            url = await control.api_credentials(os.environ["DATABASE_URL"])
            if ".supabase.co" in url and "pooler" not in url:
                print("warning: direct Supabase host (IPv6 only); Render needs the pooler URL")  # noqa: T201
            what = "DATABASE_URL for the pipeline API (a Render secret; any earlier one stops working):"
            print(what)  # noqa: T201
            print(url)  # noqa: T201
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
        elif a.cmd == "form-check":
            from .leadgen import service  # noqa: PLC0415

            return await service.form_check(
                a.domains, post=not a.no_slack, post_anyway=a.post_anyway
            )
        elif a.cmd == "popup-check":
            from .gui import desktop  # noqa: PLC0415

            return await desktop.popup_check(a.urls)
        elif a.cmd == "gui-spike":
            from .gui import spike  # noqa: PLC0415

            return await spike.run(a.tasks)
        elif a.cmd == "test-email":
            from . import test_email  # noqa: PLC0415

            return await test_email.run(a.count, dry_run=dry)
        elif a.cmd == "inbox":
            from . import inbox  # noqa: PLC0415

            print(json.dumps(await inbox.poll()))  # noqa: T201
        elif a.cmd == "gmail-check":
            from . import gmail_check  # noqa: PLC0415

            return gmail_check.main()
        elif a.cmd == "replies":
            from . import replies  # noqa: PLC0415

            print(json.dumps(await replies.run()))  # noqa: T201
        elif a.cmd == "followups":
            from . import followups  # noqa: PLC0415

            print(json.dumps(await followups.run()))  # noqa: T201
        elif a.cmd == "send":
            from .leadgen import service  # noqa: PLC0415

            while True:
                stats = await service.send(dry_run=dry)
                if stats.get("disabled"):
                    print("EMAIL_SENDING_ENABLED is not true: nothing sent")  # noqa: T201
                    return 1
                if not a.watch:
                    print(json.dumps(stats))  # noqa: T201
                    break
                if stats["sent"] or stats["failed"] or stats["skipped"] or stats["blocked"]:
                    print(json.dumps(stats))  # noqa: T201
                await asyncio.sleep(a.interval)
    finally:
        await db.close_pool()
    return 0


async def _leads(a: argparse.Namespace, dry: bool) -> int:  # noqa: PLR0912 - one branch per subcommand
    from .leadgen import config, service, strategy  # noqa: PLC0415
    from .leadgen.repository import open_repository  # noqa: PLC0415

    cmd = a.leads_cmd
    if cmd == "run":
        deadline = time.monotonic() + a.minutes * 60
        result = await service.run_leads(
            dry_run=dry, deadline=deadline, max_leads=a.max_leads, project=a.project
        )
        print(json.dumps(result, indent=2, default=str))  # noqa: T201
    elif cmd == "review":
        rows = await service.review(dry_run=dry)
        for r in rows:
            flag = f"  [CHECK: {r['flags']}]" if r["flags"] else ""
            head = f"{r['email_id']}  {r['company']} (score {r['score']}, {r['lead_status']})"
            print(f"{head} <{r['to']}>  {r['subject']}{flag}")  # noqa: T201
        print(f"{len(rows)} draft(s) waiting for a decision")  # noqa: T201
    elif cmd == "show":
        print(await service.show(a.email_id, dry_run=dry))  # noqa: T201
    elif cmd in ("approve", "reject"):
        ids = [r["email_id"] for r in await service.review(dry_run=dry)] if a.all else a.ids
        print(json.dumps(await service.decide(ids, cmd == "approve", dry_run=dry), indent=2))  # noqa: T201
    elif cmd == "check-domains":
        from .leadgen import domain_check  # noqa: PLC0415

        repo = await open_repository(config.runtime(dry_run=dry))
        try:
            found = await domain_check.run(repo, apply=a.apply)
        finally:
            await repo.close()
        for f in found:
            if f.status != "ok":
                print(f"{f.status.upper():8} {f.company} ({f.domain}): {f.detail}  {f.action}")  # noqa: T201
        bad = sum(f.status != "ok" for f in found)
        hint = "" if a.apply or not bad else "  (report only: add --apply to reject the dead ones)"
        print(f"{len(found)} lead(s) checked, {bad} with a problem{hint}")  # noqa: T201
    elif cmd == "queries":
        repo = await open_repository(config.runtime(dry_run=dry))
        try:
            cfg = config.load()
            if a.project:
                cfg.raw["project_context"] = service.load_project(a.project)
            for q in await strategy.next_queries(repo, cfg, a.count):
                print(f"[{q.family}] {q.text}")  # noqa: T201
        finally:
            await repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
