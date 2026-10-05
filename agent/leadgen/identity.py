"""Company identity: one company = one lead, however many pages mention it.

A company is the same lead when its website domain matches, or when its normalized name matches (the same company
seen on a job board before its website was known). URLs are canonicalized so the same page is never read twice.
"""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import intent

_LEGAL = {
    "inc", "incorporated", "ltd", "limited", "llc", "llp", "gmbh", "ag", "corp", "corporation", "co", "company",
    "pvt", "private", "plc", "sa", "sas", "srl", "bv", "nv", "oy", "ab", "as", "kk", "pte", "pty", "the",
}  # fmt: skip
_GENERIC = {"technologies", "technology", "tech", "systems", "solutions", "group", "labs", "lab", "global",
            "international", "industries", "electronics", "devices", "innovations"}  # fmt: skip
_TRACKING = ("utm_", "gclid", "fbclid", "mc_", "ref_src", "igshid", "sca_", "ved", "sxsrf")

# Sites that are never "the company's own website".
PLATFORM_HOSTS = (
    intent.MARKETPLACES | intent.DISTRIBUTOR_HOSTS | intent.JOB_HOSTS | intent.ATS_HOSTS
    | intent.FREELANCE_HOSTS | intent.DIRECTORY_HOSTS | intent.DOCS_HOSTS
    | frozenset({"facebook.com", "twitter.com", "x.com", "instagram.com", "reddit.com", "ycombinator.com",
                 "duckduckgo.com", "google.com", "bing.com", "angel.co", "wellfound.com", "wordpress.com",
                 "blogspot.com", "substack.com", "prnewswire.com", "businesswire.com", "globenewswire.com"})
)  # fmt: skip


def name_key(name: str) -> str:
    """'Acme EV Technologies Pvt. Ltd.' -> 'acmeev'. Empty when nothing distinctive is left."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    core = [w for w in words if w not in _LEGAL]
    distinctive = [w for w in core if w not in _GENERIC] or core
    return "".join(distinctive)


def canonical_url(url: str) -> str:
    """Same page, same string: no fragment, tracking parameters, 'www.' or trailing slash."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.")
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith(_TRACKING))
    )
    return urlunparse((u.scheme.lower() or "https", host, u.path.rstrip("/"), "", query, ""))


def url_key(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def is_company_site(domain: str) -> bool:
    d = intent.registrable_domain(domain)
    return bool(d) and d not in PLATFORM_HOSTS and not intent.host_in(d, PLATFORM_HOSTS)


def _tokens(name: str) -> list[str]:
    return [
        w for w in re.findall(r"[a-z0-9]{3,}", (name or "").lower()) if w not in _LEGAL | _GENERIC
    ]


def name_matches_domain(name: str, domain: str) -> bool:
    """Cheap guard against attributing a page to the wrong company: a distinctive word of the name is in the
    domain's label ('Acme EV' ~ acme-ev.com, acmeev.io), or the label is the name's initials (ABB ~ abb.com)."""
    label = re.sub(r"[^a-z0-9]", "", intent.registrable_domain(domain).split(".")[0])
    if not label:
        return False
    toks = _tokens(name)
    if any(t in label or label in t for t in toks):
        return True
    initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _LEGAL)
    return len(initials) >= 2 and label == initials


def name_on_page(name: str, text: str) -> bool:
    """The company name (or its distinctive words) is actually written on the page: the model did not invent it."""
    if not name:
        return False
    low = re.sub(r"\s+", " ", text.lower())
    if name.lower() in low:
        return True
    toks = _tokens(name)
    return bool(toks) and all(re.search(rf"\b{re.escape(t)}", low) for t in toks)


def similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """Token overlap (Jaccard) of two titles or queries; tolerates small wording changes."""
    ta = {t.removesuffix("s") for t in re.split(r"\W+", a.lower()) if len(t) > 2}
    tb = {t.removesuffix("s") for t in re.split(r"\W+", b.lower()) if len(t) > 2}
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / len(ta | tb) >= threshold
