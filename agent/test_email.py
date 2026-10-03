"""Dev smoke test: the LLM drafts outreach emails exactly as in the daily run for made-up companies.

Default: each draft is saved for a made-up contact and posted to Slack with Approve / Skip. After Approve
it is sent by `python -m agent send --watch` to TEST_RECIPIENT, never to the made-up contact.
--direct: skip Slack and database, send straight to TEST_RECIPIENT / --to."""

import re
import time
import uuid
from types import SimpleNamespace

from agentkit.config import env

from . import mailer, notify, store
from .tasks import proposal

SAMPLES = [
    "Company: Northwind Automotive\nProject: In-vehicle infotainment platform on Android Automotive OS\n"
    "Technologies: AOSP, Android Automotive, HAL, Kotlin\nLocation: Germany\nContact role: Head of Engineering\n",
    "Company: Lumen Devices\nProject: Rugged handheld scanners running a custom Android build\n"
    "Technologies: AOSP 13, Yocto, BSP, SELinux\nLocation: Netherlands\nContact role: CTO\n",
    "Company: Pixelwave Labs\nProject: Android-based set-top box with OTA updates\n"
    "Technologies: AOSP, Kernel, OTA, Widevine\nLocation: India\nContact role: Engineering Manager\n",
]


async def _queue_for_slack(company: str, ctx: str, draft, problems: list[str]) -> bool:
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    domain = f"{slug}-{int(time.time())}.test.example"  # unique per run, never a real site
    role = ctx.split("Contact role: ", 1)[1].strip()
    q = SimpleNamespace(
        confidence=0.9,
        reason="test-email sample",
        location=None,
        technologies=["AOSP"],
        project_summary=ctx.split("Project: ", 1)[1].split("\n", 1)[0],
    )
    company_id = await store.save_company(
        name=company,
        domain=domain,
        status="contact_found",
        q=q,
        source="test-email",
        source_url=f"https://{domain}",
        review=False,
    )
    contact = SimpleNamespace(email=f"sales@{domain}", name="Test Contact", role=role)
    contact_id = await store.save_contact(company_id, contact, f"https://{domain}")
    email_id = await store.save_email_draft(
        contact_id, draft.subject, draft.body, "; ".join(problems) or None
    )
    return bool(email_id) and await notify.post_email(email_id)


async def run(count: int = 1, to: str | None = None, direct: bool = False) -> int:
    recipients = (to or mailer.test_recipient() or "").strip()
    recipients = ", ".join(a.strip() for a in recipients.split(",") if a.strip())
    if direct and not recipients:
        print("no recipients: set TEST_RECIPIENT in .env or pass --to a@x.io,b@y.io")  # noqa: T201
        return 2
    if not direct and not (env("DATABASE_URL") and notify.enabled()):
        print("Slack mode needs DATABASE_URL and SLACK_BOT_TOKEN; or use --direct")  # noqa: T201
        return 2
    failed = 0
    for ctx in SAMPLES[: max(1, min(count, len(SAMPLES)))]:
        company = ctx.split("\n", 1)[0].removeprefix("Company: ")
        draft, problems = await proposal.draft_proposal(ctx)
        print(f"\n--- {company}: {draft.subject}\n{draft.body}")  # noqa: T201
        if problems:
            print(f"[checks flagged: {'; '.join(problems)}]")  # noqa: T201
        try:
            if direct:
                row = {
                    "id": str(uuid.uuid4()),  # only feeds the unsubscribe token and idempotency key
                    "subject": f"[TEST for {company}] {draft.subject}",
                    "body": draft.body,
                }
                sent = await mailer.deliver(row, recipients)
                print(f"sent to {recipients} (gmail id {sent['gmail_message_id']})")  # noqa: T201
            elif await _queue_for_slack(company, ctx, draft, problems):
                print("posted to Slack: approve it there")  # noqa: T201
            else:
                failed += 1
                print("could not post to Slack (see the log above)")  # noqa: T201
        except Exception as exc:
            failed += 1
            print(f"failed: {exc}")  # noqa: T201
    if not direct:
        print(  # noqa: T201
            "\nNext: python -m agent send --watch   (needs EMAIL_SENDING_ENABLED=true; "
            f"approved mail goes to: {mailer.test_recipient() or 'NOBODY - set TEST_RECIPIENT'})"
        )
    return 1 if failed else 0
