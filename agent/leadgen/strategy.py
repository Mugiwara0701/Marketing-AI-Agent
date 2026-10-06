"""Search strategy: structured SEARCH INTENT first, query text second.

The business target is fixed in code and never comes from configuration or a project:

    target = potential_customer   an organisation that could BUY our embedded / AOSP / BSP / Linux / firmware /
                                  application engineering

What changes per project is the CONTEXT (config `project_context`, or `--project <file>`): the products or systems,
industries, technologies and engineering requirements involved. A technology in a project (RFID, cameras, CAN, GPS,
Jetson...) is a modifier of the PRODUCT ("RFID access control system"), never the thing searched for: the strategy never
looks for companies that sell it.

    PROJECT CONTEXT -> SearchIntent(target, product, industry, technology, capability, signal) -> query text

Families (each a customer signal):
    builder  "company developing <tech> <product>"            product companies (potential customers)
    need     "<product> company <need phrase> <capability>"     asking for a partner / vendor / outside team
    hiring   "<product> company hiring <capability> engineer"   named companies short of embedded capacity
    rfp      "<RFQ word> <capability> <product>"                tenders and RFQs
    direct   "<need phrase> <capability> project"               the same without a product
    partner  "<product> <capability> engineering company"       only when accept_service_companies is true

Every query passes check_customer_query(): no supplier wording (manufacturer, supplier, OEM, buy, price...). Queries run
recently (repeat_after_days) are skipped; the repository remembers them, so a restart continues.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from .config import LeadgenConfig
from .repository import Repository

TARGET: Literal["potential_customer"] = "potential_customer"
FAMILIES = ("builder", "need", "hiring", "rfp", "direct", "partner")
# Order within a run: product companies and explicit needs dominate.
CYCLE = ("builder", "need", "builder", "hiring", "builder", "rfp", "direct", "partner")

# Words that turn a customer search into a supplier search. A query containing one is a bug and is never sent.
SUPPLIER_WORDS = re.compile(
    r"\b(manufacturers?|suppliers?|distributors?|resellers?|wholesale|dealers?|exporters?|oem|odm|makers?|buy|"
    r"price|prices|for sale|shop|store|catalog(ue)?)\b",
    re.I,
)


class RequirementDriftError(AssertionError):
    """A query would target sellers, not customers."""


def check_customer_query(text: str) -> None:
    if m := SUPPLIER_WORDS.search(text):
        raise RequirementDriftError(
            f"query {text!r} targets suppliers ({m.group(0)!r}), not customers"
        )


@dataclass(frozen=True)
class ProjectContext:
    """What a project is about. Context only: it shapes queries, it never changes the target."""

    name: str = ""
    industries: tuple[str, ...] = ()
    products: tuple[str, ...] = ()  # products / systems a customer would BUILD (never components)
    technologies: tuple[str, ...] = ()  # technologies those products use (RFID, camera, CAN...)
    engineering_requirements: tuple[str, ...] = ()  # our capabilities the project needs

    @classmethod
    def from_dict(cls, d: dict | None) -> "ProjectContext":
        d = d or {}

        def t(key: str) -> tuple[str, ...]:
            return tuple(str(x) for x in (d.get(key) or []) if str(x).strip())

        return cls(
            name=str(d.get("name") or ""),
            industries=t("industries"),
            products=t("products"),
            technologies=(*t("technologies"), *t("hardware")),
            engineering_requirements=t("engineering_requirements"),
        )

    @property
    def empty(self) -> bool:
        return not (
            self.products or self.technologies or self.engineering_requirements or self.industries
        )


@dataclass(frozen=True)
class SearchIntent:
    family: str
    product: str
    capability: str
    signal: str = ""
    technology: str = ""  # project context: a modifier of the product
    industry: str = ""
    target: Literal["potential_customer"] = TARGET
    excluded: tuple[str, ...] = field(
        default=("suppliers", "resellers", "ecommerce", "service companies")
    )

    def query(self) -> str:
        tech_product = f"{self.technology} {self.product}".strip()
        if self.family == "builder":
            return f"{self.signal} {tech_product}"
        if self.family == "need":
            return f"{tech_product} company {self.signal} {self.capability}"
        if self.family == "hiring":
            return f"{tech_product} company hiring {self.capability} engineer"
        if self.family == "rfp":
            return f"{self.signal} {self.capability} {tech_product}"
        if self.family == "direct":
            return f"{self.signal} {self.capability} project"
        return f"{tech_product} {self.capability} engineering company"  # partner


@dataclass(frozen=True)
class Query:
    text: str
    family: str
    intent: SearchIntent | None = None


def _vocab(cfg: LeadgenConfig, project: ProjectContext) -> dict[str, list[str]]:
    s = cfg.search
    return {
        "products": list(project.products) or s.get("products") or ["embedded device"],
        "capabilities": list(project.engineering_requirements)
        or s.get("capabilities")
        or ["embedded Linux"],
        # project technologies first; the generic platforms keep a project search from narrowing to one word
        "technologies": [*project.technologies, *(s.get("platforms") or [""])],
        "industries": list(project.industries) or s.get("industries") or [""],
        "need": s.get("need_phrases") or ["looking for development partner for"],
        "rfp": s.get("rfp_words") or ["RFQ"],
        "builder": s.get("builder_phrases") or ["company developing"],
    }


def intents(
    cfg: LeadgenConfig,
    project: ProjectContext | None = None,
    *,
    hiring: bool = True,
    partners: bool = False,
) -> Iterator[SearchIntent]:
    """Every intent the strategy can make, in run order: the product changes with every query and the family rotates
    with it, so a dozen queries cover a dozen products; technologies and capabilities walk with coprime strides."""
    project = project or ProjectContext()
    v = _vocab(cfg, project)
    families = [f for f in CYCLE if (hiring or f != "hiring") and (partners or f != "partner")]
    prods, caps, techs, inds = v["products"], v["capabilities"], v["technologies"], v["industries"]
    total = len(prods) * len(caps) * len(families) * max(len(techs), 1)
    for n in range(total):
        fam = families[n % len(families)]
        lap = n // len(prods)
        product = prods[n % len(prods)]
        cap = caps[(n * 7 + lap) % len(caps)]
        tech = techs[(n // 2 + lap) % len(techs)] if techs else ""
        if tech and tech.lower() in product.lower():
            tech = ""  # "Android payment terminal" already says it
        signal = {
            "builder": v["builder"][(n + lap) % len(v["builder"])],
            "need": v["need"][(n * 3 + lap) % len(v["need"])],
            "direct": v["need"][(n * 5 + lap) % len(v["need"])],
            "rfp": v["rfp"][(n + lap) % len(v["rfp"])],
        }.get(fam, "")
        yield SearchIntent(
            fam, product, cap, signal, tech if fam != "direct" else "", inds[n % len(inds)]
        )


def generate(
    cfg: LeadgenConfig,
    project: ProjectContext | None = None,
    *,
    hiring: bool = True,
    partners: bool = False,
) -> Iterator[Query]:
    for it in intents(cfg, project, hiring=hiring, partners=partners):
        text = " ".join(it.query().split())
        check_customer_query(text)
        yield Query(text, it.family, it)


async def next_queries(
    repo: Repository,
    cfg: LeadgenConfig,
    count: int | None = None,
    project: ProjectContext | None = None,
) -> list[Query]:
    """The next `count` queries not run within repeat_after_days."""
    project = project or ProjectContext.from_dict(cfg.raw.get("project_context"))
    count = count or int(cfg.search.get("queries_per_run", 12))
    repeat = timedelta(days=int(cfg.search.get("repeat_after_days", 14)))
    now = datetime.now(UTC)
    out: list[Query] = []
    seen: set[str] = set()
    for q in generate(
        cfg,
        project,
        hiring=bool(cfg.q("accept_hiring_signals", True)),
        partners=bool(cfg.q("accept_service_companies", False)),
    ):
        key = q.text.lower()
        if key in seen:
            continue
        seen.add(key)
        last = await repo.query_last_run(q.text)
        if last is not None and now - (last if last.tzinfo else last.replace(tzinfo=UTC)) < repeat:
            continue
        out.append(q)
        if len(out) >= count:
            break
    return out
