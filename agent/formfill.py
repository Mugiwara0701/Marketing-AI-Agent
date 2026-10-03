"""Fill a company's contact form in a visible Chrome window, then stop. A human reviews and clicks Send.

Never submits and never touches CAPTCHAs: the window stays open until the person closes it."""

from agentkit.config import env

from . import store
from .tasks import proposal


def _kind(hint: str, tag: str, typ: str) -> str | None:
    """Which of our values belongs in a field, judged from its name/id/placeholder/label text."""
    if tag == "textarea":
        return "message"
    if typ == "email" or "email" in hint or "e-mail" in hint:
        return "email"
    if "subject" in hint or "topic" in hint:
        return "subject"
    if "company" in hint or "organi" in hint:
        return "company"
    if "name" in hint:
        return "name"
    return None


async def run(ref: str) -> str:
    row = await store.company_with_form(ref)
    if row is None:
        return f"no company with a contact form matches '{ref}'"
    ctx = (
        f"Company: {row['name']}\nProject: {row['project_summary'] or ''}\n"
        f"Technologies: {', '.join(row['technologies'] or []) or 'not stated'}\n"
        f"Location: {row['location'] or 'not stated'}\nContact role: business contact\n"
    )
    draft, problems = await proposal.draft_proposal(ctx)
    values = {
        "message": f"{draft.subject}\n\n{draft.body}",
        "subject": draft.subject,
        "email": env("MAIL_FROM", "") or "",
        "name": env("SENDER_NAME", "") or "",
        "company": env("COMPANY_NAME", "") or "",
    }
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415 - optional dependency
    except ImportError:
        return "Playwright is not installed: pip install playwright && playwright install chromium"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto(row["contact_form_url"], wait_until="domcontentloaded")
        filled = []
        for el in await page.query_selector_all("form input, form textarea"):
            typ = (await el.get_attribute("type") or "text").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio", "password", "file"):
                continue
            if not await el.is_visible():
                continue
            tag = await el.evaluate("e => e.tagName.toLowerCase()")
            hint = " ".join(
                [
                    await el.get_attribute(a) or ""
                    for a in ("name", "id", "placeholder", "aria-label")
                ]
            ).lower()
            kind = _kind(hint, tag, typ)
            if kind and values[kind]:
                await el.fill(values[kind])
                filled.append(kind)
        print(  # noqa: T201
            f"filled: {', '.join(filled) or 'nothing'}; draft checks: {problems or 'passed'}\n"
            "Review the form, solve any CAPTCHA, click Send yourself, then close the window."
        )
        await page.wait_for_event("close", timeout=0)
        await browser.close()
    return "done (nothing was submitted by the agent)"
