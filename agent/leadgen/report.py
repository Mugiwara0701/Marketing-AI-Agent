"""A Markdown report of a run: every lead touched, why it is a lead, its contact, draft and approval state."""

from datetime import datetime
from pathlib import Path

from .models import Lead
from .repository import Repository


def lead_section(lead: Lead, email_subject: str = "", email_body: str = "") -> str:
    s = lead.score
    parts = ", ".join(f"{k} {v}" for k, v in (s.parts.items() if s else []))
    pen = ", ".join(f"-{v} {k}" for k, v in (s.penalties.items() if s else [])) or "none"
    ev = "\n".join(f"  - {e.reason} ({e.url})" + (f'\n    > "{e.quote[:220]}"' if e.quote else "")
                   for e in lead.evidence) or "  - none"  # fmt: skip
    c = lead.contact
    contact = (
        f"{c.name or '-'} | {c.role or '-'} | {c.email} | rank {c.rank} | {c.source}"
        if c
        else "not found"
    )
    out = (
        f"### {lead.company_name} ({lead.company_website}) - {lead.status}\n\n"
        f"- score: **{lead.lead_score}** ({parts}; penalties: {pen})\n"
        f"- industry: {lead.industry or '-'}; product: {lead.product or '-'}\n"
        f"- signal: {lead.project_signal}; page type: {lead.page_type}\n"
        f"- opportunity: {lead.opportunity_description or '-'}\n"
        f"- technical: {', '.join(lead.technical_requirements) or '-'}\n"
        f"- contact: {contact}\n"
        f"- approval: {lead.approval_status or '-'}\n"
        f"- evidence:\n{ev}\n"
    )
    if email_subject:
        out += f"\n**Draft - {email_subject}**\n\n{email_body}\n"
    return out + "\n"


async def write(
    repo: Repository, lead_ids: list[str], stats: dict, out_dir: Path, *, dry_run: bool
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one small file at the end of a run
    path = out_dir / f"run-{datetime.now():%Y%m%d-%H%M%S}.md"
    lines = [
        f"# Lead run {datetime.now():%Y-%m-%d %H:%M}{'  (DRY RUN: no email sent, Slack simulated)' if dry_run else ''}\n"
    ]
    lines.append(
        "## Counts\n\n" + "\n".join(f"- {k}: {v}" for k, v in sorted(stats.items())) + "\n"
    )
    lines.append("## Leads\n")
    for lid in dict.fromkeys(lead_ids):
        lead = await repo.get_lead(lid)
        if lead is None:
            continue
        email = await repo.email_for_lead(lid)
        lines.append(
            lead_section(lead, email.subject if email else "", email.body if email else "")
        )
    if len(lines) == 3:
        lines.append("No new or updated leads in this run.\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
