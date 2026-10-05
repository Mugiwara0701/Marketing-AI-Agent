"""Page observation -> content: HTML to visible text, title and links. Pure functions, no I/O."""

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .. import web
from .models import Link, Page

_SKIP = ("script", "style", "noscript", "svg", "template")


class _Links(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base
        self.links: list[Link] = []
        self.title = ""
        self._in_title = False
        self._href: str | None = None
        self._text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "base":
            href = dict(attrs).get("href")
            if href:
                self.base = urljoin(self.base, href)
        elif tag == "a":
            self._href, self._text = dict(attrs).get("href"), []
            label = dict(attrs).get("aria-label") or dict(attrs).get("title")
            if label:
                self._text.append(label)

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            href = self._href.strip()
            if href and not href.startswith(("#", "javascript:", "tel:")):
                url = href if href.startswith("mailto:") else urljoin(self.base, href)
                self.links.append(Link(" ".join(" ".join(self._text).split())[:120], url))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._href is not None:
            self._text.append(data.strip())


def page_from_html(url: str, html: str) -> Page:
    p = _Links(url)
    try:
        p.feed(html)
    except Exception:
        p.links = p.links or []
    seen: set[str] = set()
    links = []
    for link in p.links:
        if link.url not in seen and urlparse(link.url).scheme in ("http", "https", "mailto"):
            seen.add(link.url)
            links.append(link)
    return Page(
        url=url,
        title=" ".join(unescape(p.title).split())[:200],
        text=web.html_to_text(html),
        links=links,
    )


def _label_match(text: str, label: str) -> bool:
    t, lab = text.strip().lower(), label.strip().lower()
    return (
        t == lab or t.startswith(lab + " ") or re.fullmatch(rf"{re.escape(lab)}\W*", t) is not None
    )


def pick_link(
    page: Page, labels: list[str], *, same_site: bool = True, exclude: set[str] | None = None
) -> Link | None:
    """The first link a person would click for one of `labels` (in label order), on the same site by default.
    Only links that are on the page are ever returned: no URL is made up."""
    here = web.registrable_domain(page.url)
    for label in labels:
        for link in page.links:
            if link.url.startswith("mailto:") or (exclude and link.url in exclude):
                continue
            if same_site and web.registrable_domain(link.url) != here:
                continue
            if _label_match(link.text, label):
                return link
    return None
