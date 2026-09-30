"""Slack helpers: approval messages (buttons carry only an approval id), alerts, signature check."""

import hashlib
import hmac
import time

import httpx

from .config import env

API = "https://slack.com/api"


def verify_signature(signing_secret: str, timestamp: str, body: bytes, signature: str,
                     max_age: int = 300) -> bool:
    """Slack request signing: v0=HMAC_SHA256(secret, 'v0:ts:body'); reject anything older than 5 min."""
    try:
        if abs(time.time() - int(timestamp)) > max_age:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def approval_blocks(text: str, approval_id: str, buttons: list[tuple[str, str]]) -> list[dict]:
    """buttons: [(label, action_id)], e.g. [("Accept", "accept"), ("Ignore", "ignore")]."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": label},
             "action_id": action, "value": approval_id} for label, action in buttons]},
    ]


async def post_message(channel: str, text: str, blocks: list[dict] | None = None) -> dict:
    """Post with the bot token. Returns {ts, channel}. Raises if Slack rejects (caller keeps approval pending)."""
    payload: dict = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API}/chat.postMessage", json=payload,
                         headers={"Authorization": f"Bearer {env('SLACK_BOT_TOKEN', required=True)}"})
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"slack error: {data.get('error')}")
    return {"ts": data["ts"], "channel": data["channel"]}


async def alert(text: str) -> None:
    """Send to #agent-alerts via incoming webhook. Never include personal data."""
    url = env("SLACK_ALERTS_WEBHOOK")
    if not url:
        return
    async with httpx.AsyncClient(timeout=15) as c:
        await c.post(url, json={"text": text})
