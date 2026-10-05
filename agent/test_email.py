"""Dev smoke test of the approval flow: the LLM drafts outreach emails for made-up companies exactly as in a real
run, and each draft goes through the same states and the same approval gate (Slack, or `agent leads review`).

After Approve, `python -m agent send` sends it to TEST_RECIPIENT (never to the made-up contact); with --dry-run it
goes to the outbox folder. There is no way to send without approval."""

import re
import time

from agentkit.log import get_logger

from . import mailer
from .leadgen import config, service
from .leadgen.models import Contact, Evidence, Lead
from .leadgen.repository import open_repository

log = get_logger("agent.test_email")

SAMPLES = [
    ("Northwind Automotive", "Automotive", "In-vehicle infotainment platform on Android Automotive OS",
     ["AOSP", "HAL"], "Head of Engineering"),
    ("Lumen Devices", "Industrial handhelds", "Rugged handheld scanners running a custom Android build",
     ["AOSP", "Yocto", "BSP"], "CTO"),
    ("Pixelwave Labs", "Consumer devices", "Android-based set-top box with OTA updates",
     ["AOSP", "OTA", "Linux kernel"], "Engineering Manager"),
]  # fmt: skip


async def run(count: int = 1, *, dry_run: bool = False) -> int:
    if not dry_run and not mailer.test_recipient():
        print("set TEST_RECIPIENT in .env first: approved test mail goes only there")  # noqa: T201
        return 2
    rt = config.runtime(dry_run=dry_run)
    repo = await open_repository(rt)
    failed = 0
    try:
        for name, industry, product, tech, role in SAMPLES[: max(1, min(count, len(SAMPLES)))]:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            domain = f"{slug}-{int(time.time())}.test.example"  # never a real site
            lead = Lead(
                company_name=name, company_website=domain, industry=industry, product=product,
                opportunity_description=product, technical_requirements=tech, project_signal="product_development",
                evidence=[Evidence(url=f"https://{domain}", reason="test-email sample (made up)")],
            )  # fmt: skip
            contact = Contact(
                name="Test Contact",
                role=role,
                email=f"sales@{domain}",
                source=f"https://{domain}",
                rank=0,
            )
            try:
                email_id, report = await service.submit_lead(repo, lead, contact, source="test-email",
                                                             dry_run=dry_run, out_dir=rt.out_dir)  # fmt: skip
            except Exception as exc:
                failed += 1
                log.exception("test email failed")
                print(f"{name}: failed ({type(exc).__name__})")  # noqa: T201
                continue
            print(f"{name}: {report} (email id {email_id})")  # noqa: T201
    finally:
        await repo.close()
    nxt = "approve (Slack, or `python -m agent leads approve <id>`), then `python -m agent send`."
    print(f"Next: {nxt}")  # noqa: T201
    return 1 if failed else 0
