"""Resend client: send one email. Webhook events come back through supabase/functions/resend-webhook."""

import httpx

from .config import env

API = "https://api.resend.com"


async def send_email(payload: dict, idempotency_key: str) -> str:
    """POST /emails and return Resend's email id. Raises if Resend rejects.

    The idempotency key makes a retry after a timeout a no-op instead of a second mail.
    The error text carries only the status and Resend's error name, never addresses or bodies.
    """
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{API}/emails",
            json=payload,
            headers={
                "Authorization": f"Bearer {env('RESEND_API_KEY', required=True)}",
                "Idempotency-Key": idempotency_key,
            },
        )
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 300 or not data.get("id"):
        raise RuntimeError(f"resend error {r.status_code}: {data.get('name') or 'unknown'}")
    return data["id"]
