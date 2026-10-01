"""Exercise the real pipeline (live scraping + the real LLM) WITHOUT a database or any email.

Writes a Markdown report to out/ so a person can read exactly what the model decided and wrote.
"""

import time
from datetime import datetime
from pathlib import Path

from agentkit import llm

from . import blog, contacts, research, sources, variants, web
from .tasks import adapt, proposal, qualify


async def run(  # noqa: PLR0915
    max_leads: int = 3, max_signals: int = 40, do_blog: bool = True, do_leads: bool = True
) -> Path:
    t0 = time.monotonic()
    out = [
        f"# Dry run {datetime.now():%Y-%m-%d %H:%M}\n",
        f"LLM model for qualify: `{llm.model_for('lead.qualify')}`\n",
    ]
    if not await llm.health():
        raise SystemExit("LLM not reachable: check LLM_BASE_URL / LLM_API_KEY")

    signals = await sources.collect_signals(max_signals) if do_leads else []
    by_source: dict[str, int] = {}
    for s in signals:
        by_source[s.source] = by_source.get(s.source, 0) + 1
    out.append(f"## Part 1: leads\n\nListings found: {len(signals)} {by_source}\n")
    accepted = 0
    seen_domains: set[str] = set()
    for n, sig in enumerate(signals, 1):
        if accepted >= max_leads:
            break
        q, problems = await qualify.qualify_signal(sig.text)
        verdict = "ACCEPT" if q.relevant and not problems else "reject"
        out.append(
            f"### {n}. [{verdict}] {q.company_name or sig.company_hint or '?'}\n- {sig.url}\n- {q.reason} (confidence {q.confidence}, problems {problems})\n"
        )
        if verdict != "ACCEPT":
            continue
        name = q.company_name or sig.company_hint
        domain = web.registrable_domain(q.website) if q.website else ""
        if not web.is_company_site(domain):
            domain = await sources.resolve_website(name)
        if domain in seen_domains:
            out.append("- (company already handled above)\n")
            continue
        seen_domains.add(domain)
        out.append(
            f"- project: {q.project_summary}\n- tech: {q.technologies}, location: {q.location}, website: {domain or 'NOT FOUND'}\n"
        )
        found = await contacts.find_contact(domain) if domain else None
        if not found:
            out.append("- contact: none found (would be saved as qualified, no email)\n")
            continue
        c, url, c_problems = found
        out.append(f"- contact: {c.email} ({c.role}) from {url}, problems {c_problems}\n")
        ctx = f"Company: {name}\nProject: {q.project_summary}\nTechnologies: {', '.join(q.technologies)}\nLocation: {q.location}\nContact role: {c.role}"
        d, d_problems = await proposal.draft_proposal(ctx)
        out.append(f"**Subject:** {d.subject}\n\n{d.body}\n\n(checks: {d_problems or 'passed'})\n")
        accepted += 1
    out.append(f"Accepted {accepted} lead(s).\n")

    if do_blog:
        items = await research.research_items()
        c = await blog.compose(items, [])
        out.append(f"## Part 2: blog\n\nResearch items: {len(items)}\n")
        if c is None:
            out.append("No topic was both new and well sourced.\n")
        else:
            out.append(
                "Topics proposed:\n"
                + "\n".join(f"- ({t.kind}) {t.title}: {t.angle}" for t in c.plan.topics)
                + f"\n\nChosen: **{c.topic.title}** ({c.topic.kind}), built on:\n"
                + "\n".join(f"- {i.title} {i.url}" for i, _ in c.found)
                + f"\n\n### {c.post.title}\n\ntags: {c.post.tags}, checks: {c.problems or 'passed'}\n\n{c.post.body_markdown}\n"
            )
            for spec in variants.load_platforms().values():
                _, text, vp = await adapt.adapt(
                    spec, c.post.title, c.post.body_markdown, c.grounding
                )
                out.append(f"## {spec['label']} version (checks: {vp or 'passed'})\n\n{text}\n")
    out.append(f"\nTook {time.monotonic() - t0:.0f}s\n")
    path = Path("out") / f"dryrun-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    return path
