"""Slack: post items that need a human decision, with Approve/Reject buttons, plus the run summary.

Nothing about Slack is stored in the database (no message ids, no approval rows). Each button carries
"<kind>:<row id>"; the `slack-interact` Edge Function verifies Slack's signature and writes the decision
straight onto the email / blog row. Posting is best-effort and happens once, right after the item is
created: a Slack outage never fails a run. `python -m agent notify` manually re-posts everything still
undecided (it can duplicate messages, because nothing records what was already posted).
"""

from agentkit import db, slack
from agentkit.config import env
from agentkit.log import get_logger

log = get_logger("agent.notify")
_SECTION_MAX = 2800
_THREAD_CHUNK = 2800
_MAX_THREAD_CHUNKS = 8


def enabled() -> bool:
    return bool(env("SLACK_BOT_TOKEN"))


def esc(text: str) -> str:
    """Slack mrkdwn treats & < > specially; scraped text must not inject links or mentions."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def chunks(text: str, size: int = _THREAD_CHUNK) -> list[str]:
    out, cur = [], ""
    for raw in text.split("\n\n"):
        para = raw
        if cur and len(cur) + len(para) + 2 > size:
            out.append(cur)
            cur = ""
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def email_blocks(row, approval_id: str) -> tuple[str, list[dict]]:
    head = f"*New lead: {esc(row['company'])}*  ({esc(row['domain'])})"
    facts = [
        f"*Project:* {esc(clip(row['project_summary'] or '-', 500))}",
        f"*Tech:* {esc(', '.join(row['technologies'] or []) or '-')}",
        f"*Contact:* {esc(row['name'] or '')} {esc(row['role'] or '')} <mailto:{row['email']}|{esc(row['email'])}>".replace(
            "  ", " "
        ),
        f"*Source:* {esc(row['source_url'] or '-')}",
    ]
    if row["review_note"]:
        facts.append(f":warning: *Checks flagged:* {esc(row['review_note'])}")
    draft = f"*Subject:* {esc(row['subject'])}\n\n{esc(clip(row['body'], 1800))}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": clip(head + "\n" + "\n".join(facts), _SECTION_MAX)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": clip(draft, _SECTION_MAX)}},
        *slack.approval_blocks("Approve this email for sending?", approval_id, [("Approve", "approve_email"), ("Skip", "skip_email")])[1:],
    ]  # fmt: skip
    return f"New lead: {row['company']}", blocks


def post_blocks(row, approval_id: str) -> tuple[str, list[dict]]:
    meta = row["metadata"] or {}
    info = f"*Tags:* {esc(', '.join(row['tags'] or []) or '-')}   *Keywords:* {esc(', '.join(meta.get('keywords', [])) if isinstance(meta, dict) else '')}"
    if isinstance(meta, dict) and meta.get("problems"):
        info += f"\n:warning: *Checks flagged:* {esc('; '.join(meta['problems']))}"
    if isinstance(meta, dict) and meta.get("why_now"):
        info += f"\n*Why now:* {esc(meta['why_now'])}"
    excerpt = esc(clip(row["body_md"], 1200))
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Blog draft: {esc(row['title'])}*\n{info}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{excerpt}\n\n_Full text in the thread below. A person must check the technical details._"}},
        *slack.approval_blocks("Approve this post?", approval_id, [("Approve", "approve_post"), ("Reject", "reject_post")])[1:],
    ]  # fmt: skip
    return f"Blog draft: {row['title']}", blocks


_EMAIL_Q = """select e.id, e.subject, e.body, e.review_note, c.email, c.name, c.role,
                  co.name as company, co.domain, co.project_summary, co.technologies, co.source_url
             from emails e join contacts c on c.id=e.contact_id join companies co on co.id=c.company_id
            where e.status='drafted'"""
_POST_Q = "select id, title, body_md, tags, metadata from content_posts where status in ('drafted','in_review')"


async def _send(kind: str, row, channel: str, build) -> None:
    text, blocks = build(row, f"{kind}:{row['id']}")
    sent = await slack.post_message(channel, text, blocks)
    if kind == "post":
        for i, part in enumerate(chunks(row["body_md"])[:_MAX_THREAD_CHUNKS], 1):
            await slack.post_message(
                channel, f"({i}) {part}"[: _SECTION_MAX + 10], None, thread_ts=sent["ts"]
            )
        for v in await db.fetch(
            "select platform, body, review_note from content_variants where post_id=$1 order by platform",
            row["id"],
        ):
            flag = (
                f"  :warning: checks flagged: {esc(v['review_note'])}" if v["review_note"] else ""
            )
            await slack.post_message(
                channel, f"*{esc(v['platform'])} version*{flag}", None, thread_ts=sent["ts"]
            )
            for part in chunks(v["body"])[:_MAX_THREAD_CHUNKS]:
                await slack.post_message(
                    channel, esc(part)[: _SECTION_MAX + 200], None, thread_ts=sent["ts"]
                )


async def _safe_send(kind: str, row) -> bool:
    chan, build = (
        (env("SLACK_CHANNEL_OUTREACH", "#outreach-approvals"), email_blocks)
        if kind == "email"
        else (env("SLACK_CHANNEL_CONTENT", "#content"), post_blocks)
    )
    try:
        await _send(kind, row, chan or "", build)
    except Exception:
        log.exception("slack post failed")  # never fails the run
        return False
    return True


async def post_email(email_id) -> bool:
    if not enabled():
        return False
    row = await db.fetchrow(_EMAIL_Q + " and e.id=$1::uuid", str(email_id))
    return bool(row) and await _safe_send("email", row)


async def post_blog(post_id) -> bool:
    if not enabled():
        return False
    row = await db.fetchrow(_POST_Q + " and id=$1::uuid", str(post_id))
    return bool(row) and await _safe_send("post", row)


async def sweep() -> dict:
    """Manual: re-post every undecided draft. May duplicate messages already posted."""
    stats = {"emails": 0, "posts": 0, "failed": 0}
    if not enabled():
        log.info("slack not configured (SLACK_BOT_TOKEN); nothing posted")
        return stats
    for r in await db.fetch(_EMAIL_Q + " order by e.created_at limit 20"):
        stats["emails" if await _safe_send("email", r) else "failed"] += 1
    for r in await db.fetch(_POST_Q + " order by created_at limit 5"):
        stats["posts" if await _safe_send("post", r) else "failed"] += 1
    return stats


async def summary(out: dict) -> None:
    """One short line per step to the alerts channel (counts only, no personal data)."""
    parts = []
    for step, r in out.items():
        parts.append(
            f"{step}: "
            + (
                "ERROR " + str(r["error"])
                if "error" in r
                else ", ".join(f"{k}={v}" for k, v in r.items())
            )
        )
    text = "Daily agent run finished. " + " | ".join(parts)
    if enabled():
        await slack.post_message(env("SLACK_CHANNEL_ALERTS", "#agent-alerts") or "", text)
    else:
        await slack.alert(text)
