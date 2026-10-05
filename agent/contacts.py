"""Rules for public business addresses (pure functions). Discovery itself is agent.leadgen.contacts."""

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_USELESS = (
    "noreply",
    "no-reply",
    "donotreply",
    "abuse@",
    "privacy@",
    "dpo@",
    "webmaster@",
    "postmaster@",
    "press@",
    "unsubscribe",
)


def on_domain(address: str, domain: str) -> bool:
    """The address's host is `domain` or one of its subdomains ('x@notacme.com' is not on acme.com)."""
    host = address.rsplit("@", 1)[-1].lower()
    d = domain.lower()
    return bool(d) and (host == d or host.endswith("." + d))


def emails_on_domain(text: str, domain: str) -> list[str]:
    found = {m.lower().rstrip(".") for m in _EMAIL.findall(text)}
    return sorted(e for e in found if on_domain(e, domain) and not any(u in e for u in _USELESS))


# Role mailboxes that companies publish for business enquiries, best first.
_ROLE_ORDER = (
    "sales",
    "business",
    "partner",
    "contact",
    "hello",
    "enquir",
    "inquir",
    "info",
    "office",
    "engineering",
)


def pick_role_address(candidates: list[str]) -> str | None:
    """The best business role address among `candidates`, or None."""
    ranked = [
        (i, c)
        for c in candidates
        for i, k in enumerate(_ROLE_ORDER)
        if c.split("@", 1)[0].startswith(k)
    ]
    return min(ranked)[1] if ranked else None
