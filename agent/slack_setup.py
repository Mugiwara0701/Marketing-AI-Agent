"""Create the Slack channels the agent posts to (idempotent) and add the allowed approvers.

Needs bot scopes: channels:manage (create/invite), channels:read (find existing), channels:join.
Add them under OAuth & Permissions, then "Reinstall to Workspace"; the bot token stays the same.
"""

import httpx

from agentkit.config import env

API = "https://slack.com/api"
_CHANNELS = [
    ("SLACK_CHANNEL_OUTREACH", "outreach-approvals"),
    ("SLACK_CHANNEL_CONTENT", "content"),
    ("SLACK_CHANNEL_ALERTS", "agent-alerts"),
]


async def _call(c: httpx.AsyncClient, method: str, **params) -> dict:
    r = await c.post(f"{API}/{method}", data=params)
    data = r.json()
    return data if isinstance(data, dict) else {"ok": False, "error": "bad response"}


async def _find(c: httpx.AsyncClient, name: str) -> str | None:
    cursor = ""
    while True:
        d = await _call(
            c,
            "conversations.list",
            types="public_channel",
            limit=200,
            exclude_archived="true",
            cursor=cursor,
        )
        if not d.get("ok"):
            return None
        for ch in d.get("channels", []):
            if ch["name"] == name:
                return ch["id"]
        cursor = (d.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            return None


async def run() -> list[str]:
    """Returns human-readable result lines."""
    token = env("SLACK_BOT_TOKEN", required=True)
    users = [u.strip() for u in (env("SLACK_ALLOWED_USERS", "") or "").split(",") if u.strip()]
    out: list[str] = []
    async with httpx.AsyncClient(timeout=20, headers={"Authorization": f"Bearer {token}"}) as c:
        for var, default in _CHANNELS:
            name = (env(var, f"#{default}") or default).lstrip("#")
            d = await _call(c, "conversations.create", name=name)
            if d.get("ok"):
                cid, note = d["channel"]["id"], "created"
            elif d.get("error") == "name_taken":
                cid, note = await _find(c, name), "already exists"
                if cid:
                    j = await _call(c, "conversations.join", channel=cid)
                    note += (
                        "; bot joined"
                        if j.get("ok")
                        else f"; bot could not join ({j.get('error')})"
                    )
            else:
                out.append(
                    f"#{name}: FAILED {d.get('error')} ({d.get('needed', 'add scopes channels:manage, channels:read, channels:join and reinstall')})"
                )
                continue
            if not cid:
                out.append(f"#{name}: could not locate channel id")
                continue
            for u in users:
                i = await _call(c, "conversations.invite", channel=cid, users=u)
                if not i.get("ok") and i.get("error") not in (
                    "already_in_channel",
                    "cant_invite_self",
                ):
                    note += f"; invite {u} failed ({i.get('error')})"
            out.append(f"#{name}: {note}")
    return out
