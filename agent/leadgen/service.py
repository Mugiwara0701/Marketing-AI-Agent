"""Entry points used by the CLI and the daily run: wire config, repository, browser, approver and transport."""

import os
import shutil
import time
from pathlib import Path

import yaml

from agentkit.config import env
from agentkit.log import get_logger

from . import approval, config, outreach, report, sender
from .browser import make_browser
from .models import Contact, Lead, LeadStatus
from .pipeline import Pipeline, Services
from .repository import Repository, open_repository

log = get_logger("agent.leadgen")


def cpu_model_time_limits() -> None:
    """No NVIDIA GPU: the models run on the CPU and one call can take minutes. Raise every call's time limit
    (LLM_MIN_TIMEOUT, unless already set) so slow answers are waited for instead of failing."""
    if not shutil.which("nvidia-smi"):
        os.environ.setdefault("LLM_MIN_TIMEOUT", "420")
        log.info(
            "No NVIDIA GPU: model time limits raised",
            extra={"ctx": {"seconds": env("LLM_MIN_TIMEOUT")}},
        )


def _kill_file() -> Path:
    return Path(env("GUI_KILL_FILE", "/tmp/gui-agent.stop") or "/tmp/gui-agent.stop")  # noqa: S108


def load_project(path: str | None) -> dict:
    """A project file only fills project_context (products, technologies, industries, engineering_requirements):
    context for queries. It cannot change the target, the rules or the thresholds."""
    if not path:
        return {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    data: dict = raw.get("project_context", raw) or {}
    allowed = {
        "name",
        "industries",
        "products",
        "technologies",
        "hardware",
        "engineering_requirements",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(
            f"project file {path}: unknown keys {sorted(unknown)} (allowed: {sorted(allowed)})"
        )
    return data


async def run_leads(*, dry_run: bool | None = None, browser: str | None = None, deadline: float | None = None,
                    max_leads: int | None = None, project: str | None = None
) -> dict:  # fmt: skip
    """One discovery run. Never sends email. In a dry-run: own SQLite file, Slack written to files."""
    rt = config.runtime(dry_run=dry_run, browser=browser)
    cfg = config.load()
    cpu_model_time_limits()
    if max_leads is not None:
        cfg.limits["max_new_leads_per_run"] = max_leads
    if project:
        cfg.raw["project_context"] = load_project(project)
        log.info(
            "Project context loaded", extra={"ctx": {"file": project, **cfg.raw["project_context"]}}
        )
    repo = await open_repository(rt)
    try:
        svc = Services(repo=repo, browser=make_browser(rt.browser, cfg),
                       approver=approval.make_approver(rt.dry_run, rt.out_dir),
                       manual_notify=approval.make_manual_notifier(rt.dry_run, rt.out_dir),
                       form_notify=approval.make_form_notifier(rt.dry_run, rt.out_dir))  # fmt: skip
        pipe = Pipeline(
            svc, cfg, deadline=deadline or time.monotonic() + 3600, kill_file=_kill_file()
        )
        stats = await pipe.run()
        path = await report.write(repo, pipe.touched, stats, rt.out_dir, dry_run=rt.dry_run)
        log.info(
            "Run report written",
            extra={"ctx": {"path": str(path), "dry_run": rt.dry_run, "store": repo.name}},
        )
        return {**stats, "report": str(path), "dry_run": rt.dry_run, "store": repo.name}
    finally:
        await repo.close()


async def _repo(dry_run: bool) -> tuple[Repository, config.Runtime]:
    rt = config.runtime(dry_run=dry_run)
    return await open_repository(rt), rt


async def send(*, dry_run: bool = False, limit: int | None = None) -> dict:
    """Send what a person approved (the only send path). Dry-run: to the outbox folder, nothing leaves."""
    repo, rt = await _repo(dry_run)
    try:
        transport = sender.make_transport(rt.dry_run, rt.out_dir)
        return await sender.send_approved(
            repo, transport, limit=limit, gap_seconds=0 if rt.dry_run else None
        )
    finally:
        await repo.close()


async def decide(
    email_ids: list[str], approve: bool, *, dry_run: bool = False, by: str | None = None
) -> dict:
    repo, _ = await _repo(dry_run)
    who = by or f"cli:{env('USER', 'unknown') or 'unknown'}"
    try:
        return {i: await approval.decide(repo, i, approve, who) for i in email_ids}
    finally:
        await repo.close()


async def review(*, dry_run: bool = False) -> list[dict]:
    repo, _ = await _repo(dry_run)
    try:
        out = []
        for d in await repo.drafts():
            lead = await repo.get_lead(d.lead_id)
            out.append({"email_id": d.email_id, "company": lead.company_name if lead else "?",
                        "score": lead.lead_score if lead else None, "lead_status": lead.status if lead else None,
                        "to": d.to, "subject": d.subject, "flags": d.review_note or ""})  # fmt: skip
        return out
    finally:
        await repo.close()


async def show(email_id: str, *, dry_run: bool = False) -> str:
    repo, _ = await _repo(dry_run)
    try:
        d = await repo.get_email(email_id)
        if d is None:
            return "not found"
        lead = await repo.get_lead(d.lead_id)
        ap = await repo.get_approval(email_id)
        head = report.lead_section(lead, d.subject, d.body) if lead else ""
        return f"{head}email status: {d.status}; approval: {ap.model_dump() if ap else 'none'}\nto: {d.to}"
    finally:
        await repo.close()


async def submit_lead(repo: Repository, lead: Lead, contact: Contact, *, source: str, dry_run: bool = False,
                      out_dir: Path | None = None) -> tuple[str | None, str]:  # fmt: skip
    """A lead that did not come from discovery (a company an operator named, the GUI agent, the test-email smoke
    test): stored, drafted and put up for approval through the same states and the same sending gate.
    Returns (email id or None, one-line report)."""
    if await repo.find_lead(domain=lead.company_website):
        return None, f"{lead.company_website} is already a lead"
    lead_id = await repo.insert_lead(lead, source=source)
    await repo.set_status(lead_id, LeadStatus.QUALIFIED, note=f"submitted by {source}")
    contact.id = await repo.save_contact(lead_id, contact)
    await repo.set_status(lead_id, LeadStatus.CONTACT_FOUND)
    lead.contact, lead.status = contact, LeadStatus.CONTACT_FOUND
    d, problems = await outreach.draft(lead, contact)
    email_id = await repo.save_email_draft(lead_id, contact.id, d.subject, d.body,
                                           "; ".join([f"submitted by {source}: check the lead", *problems]))  # fmt: skip
    await repo.set_status(lead_id, LeadStatus.EMAIL_DRAFTED)
    lead.status = LeadStatus.EMAIL_DRAFTED
    email = await repo.email_for_lead(lead_id)
    approver = approval.make_approver(dry_run, out_dir or config.runtime(dry_run=dry_run).out_dir)
    posted = bool(email) and await approval.request(repo, approver, lead, contact, email)  # type: ignore[arg-type]
    return (
        email_id,
        "draft posted for approval" if posted else "draft saved; approve with `agent leads review`",
    )


async def form_check(domains: list[str], *, post: bool = True, post_anyway: bool = False) -> int:
    """Look for a contact form on each company site (the same contact search as a run) and, if one is found, post it
    to the form channel for real. No database, no discovery, no email. `post_anyway` posts even without a form (tests
    the Slack channel). Exit code 1 when a Slack post failed."""
    from . import contacts  # noqa: PLC0415

    cfg = config.load()
    cpu_model_time_limits()
    rt = config.runtime(dry_run=False)
    notify = approval.make_form_notifier(False, rt.out_dir) if post else None
    if post and notify is None:
        print("SLACK_BOT_TOKEN is not set: nothing can be posted (use --no-slack to only look)")  # noqa: T201
        return 1
    browser = make_browser(rt.browser, cfg)
    failed = False
    try:
        await browser.start()
        for domain in domains:
            found = await contacts.discover(browser, domain, cfg, company=domain)
            form = found.form_url or (f"https://{domain}" if post_anyway else None)
            print(  # noqa: T201
                f"{domain}: form={found.form_url or '-'} emails={len(found.contacts)} "
                f"blocked={found.blocked or '-'}"
            )
            if form and notify:
                try:
                    await notify(Lead(company_name=domain, company_website=domain), form)
                    print(f"  posted to Slack: {form}")  # noqa: T201
                except Exception as exc:
                    failed = True
                    print(f"  Slack post failed: {exc}")  # noqa: T201
    finally:
        await browser.close()
    return 1 if failed else 0
