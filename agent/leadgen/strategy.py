"""Search strategy: many different queries built from three vocabularies (technology x business intent x product
domain), instead of one query or every combination.

Families (each run interleaves them so no single angle dominates):
    need     "<need phrase> <tech> <domain>"              explicit outsourcing / partner / contract requests
    rfp      "<rfp word> <tech> <domain>"                 RFQs and tenders
    builder  "<domain> company <builder phrase> <tech>"   product companies building on our stack
    hiring   "<domain> company hiring <tech> engineer"    named companies short of embedded capacity

The pairing is deterministic but spread (a stride walk over the lists), so consecutive queries differ in every part
and the whole space is covered over many runs. Queries run recently (repeat_after_days) are skipped; the repository
remembers when each ran, so a restart continues instead of repeating.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import LeadgenConfig
from .repository import Repository

FAMILIES = ("need", "builder", "rfp", "hiring")


@dataclass(frozen=True)
class Query:
    text: str
    family: str


def _walk(n: int, *sizes: int) -> tuple[int, ...]:
    """The n-th combination of indexes, strided by coprime steps so neighbours share nothing."""
    steps = (1, 7, 13, 17)
    return tuple((n * steps[i]) % s for i, s in enumerate(sizes))


def generate(cfg: LeadgenConfig, *, hiring: bool = True) -> Iterator[Query]:
    s = cfg.search
    tech = s.get("tech") or ["embedded Linux"]
    need = s.get("need_phrases") or ["looking for developer"]
    rfp = s.get("rfp_words") or ["RFP"]
    build = s.get("builder_phrases") or ["developing"]
    dom = s.get("domains") or ["IoT device"]
    families = [f for f in FAMILIES if hiring or f != "hiring"]
    total = len(tech) * len(dom) * max(len(need), len(build))
    for n in range(total):
        for fam in families:
            if fam == "need":
                a, b, c = _walk(n, len(need), len(tech), len(dom))
                yield Query(f"{need[a]} {tech[b]} {dom[c]}", fam)
            elif fam == "rfp":
                a, b, c = _walk(n, len(rfp), len(tech), len(dom))
                yield Query(f"{rfp[a]} {tech[b]} {dom[c]}", fam)
            elif fam == "builder":
                a, b, c = _walk(n, len(dom), len(build), len(tech))
                yield Query(f"{dom[a]} company {build[b]} {tech[c]}", fam)
            else:
                a, c = _walk(n, len(dom), len(tech))
                yield Query(f"{dom[a]} company hiring {tech[c]} engineer", fam)


async def next_queries(
    repo: Repository, cfg: LeadgenConfig, count: int | None = None
) -> list[Query]:
    """The next `count` queries not run within repeat_after_days."""
    count = count or int(cfg.search.get("queries_per_run", 12))
    repeat = timedelta(days=int(cfg.search.get("repeat_after_days", 14)))
    now = datetime.now(UTC)
    out: list[Query] = []
    seen: set[str] = set()
    for q in generate(cfg, hiring=bool(cfg.q("accept_hiring_signals", True))):
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
