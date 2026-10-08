"""Pipeline orchestrator: discovery -> qualification -> contact discovery -> storage -> email -> Slack approval.

    resume    leads a previous run left half-way (crash, Slack down, no contact yet) are moved on first
    search    the strategy's next queries go to the browser; obvious non-leads are skipped from the result list
    observe   each opened page is qualified (rules, then one model call); a company is stored once and merged
              when seen again; a qualified lead then gets a contact, a draft and an approval request

No email is sent here: sending is agent.leadgen.sender, after a person approved. Any one page, site, model call or
Slack post may fail; it is logged and the run goes on.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agentkit.config import env
from agentkit.llm import LLMError
from agentkit.log import get_logger

from .. import mailer, sources, web
from ..tasks.assess import assess_page
from . import approval, contacts, identity, intent, outreach, scoring, strategy
from .browser import Browser, BudgetExhaustedError, SearchBlockedError
from .config import LeadgenConfig
from .models import Contact, Evidence, Lead, LeadStatus, Observation, Page, SearchResult
from .qualify import Assessor, Verdict, qualify_page, requirements
from .repository import Repository

log = get_logger("agent.leadgen.pipeline")

_LLM_FAILURES_TO_STOP = 3  # the model host is down: stop opening pages, they would all be wasted


@dataclass
class Services:
    """Everything the pipeline talks to, injected so each part can be replaced in tests."""

    repo: Repository
    browser: Browser
    approver: approval.Approver | None
    assess: Assessor = assess_page
    draft: Callable[[Lead, Contact], Awaitable] = outreach.draft
    find_contacts: Callable[..., Awaitable[contacts.Discovery]] = contacts.discover
    manual_notify: approval.ManualNotifier | None = (
        None  # qualified lead, no contact: name + website to Slack
    )
    form_notify: approval.FormNotifier | None = (
        None  # a contact form was found: name + form page to Slack
    )


@dataclass
class Stats:
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


class Pipeline:
    def __init__(self, svc: Services, cfg: LeadgenConfig, *, deadline: float | None = None,
                 kill_file: Path | None = None) -> None:  # fmt: skip
        self.svc, self.repo, self.cfg = svc, svc.repo, cfg
        self.deadline = deadline or time.monotonic() + 3600
        self.kill_file = kill_file
        self.stats = Stats()
        self.new_drafts = 0
        self.max_new = int(cfg.limit("max_new_leads_per_run", 10))
        self.llm_failures = 0
        self.touched: list[str] = []  # lead ids this run created or moved, for the report

    # --- control -------------------------------------------------------------------------------------------

    def stop_reason(self) -> str | None:
        if time.monotonic() > self.deadline:
            return "time budget used"
        if self.kill_file and self.kill_file.exists():
            return f"stop file {self.kill_file} exists"
        if self.new_drafts >= self.max_new:
            return f"{self.max_new} new leads drafted this run"
        if self.llm_failures >= _LLM_FAILURES_TO_STOP:
            return "the model host keeps failing"
        if self.svc.browser.guard.pages >= self.svc.browser.guard.max_pages:
            return "page budget used"
        return None

    async def run(self) -> dict:
        started = time.monotonic()
        log.info("Pipeline started", extra={"ctx": {"browser": self.svc.browser.name, "store": self.repo.name,
                                                    "max_new_leads": self.max_new}})  # fmt: skip
        daily = int(env("MAX_NEW_LEADS_PER_DAY", "10") or 10)
        self.max_new = min(
            self.max_new, max(0, daily - await self.repo.drafted_since(today_start()))
        )
        try:
            await self.svc.browser.start()
            await self.resume()
            queries = await strategy.next_queries(self.repo, self.cfg)
            log.info("Search strategy generated", extra={"ctx": {
                "queries": len(queries), "families": sorted({q.family for q in queries}),
                "first": [q.text for q in queries[:3]]}})  # fmt: skip
            for q in queries:
                if why := self.stop_reason():
                    log.info("Discovery stopped", extra={"ctx": {"why": why}})
                    break
                await self.search(q)
            if self.cfg.search.get("use_job_feeds") and not self.stop_reason():
                await self.feeds()
        except BudgetExhaustedError as exc:
            log.info("Discovery stopped", extra={"ctx": {"why": str(exc)}})
        finally:
            await self.svc.browser.close()
        out = {
            **self.stats.counts,
            "seconds": round(time.monotonic() - started),
            "leads_touched": len(self.touched),
        }
        log.info("Pipeline finished", extra={"ctx": out})
        return out

    # --- resume --------------------------------------------------------------------------------------------

    async def resume(self) -> None:
        """Move on leads a previous run left half-way. Each lead is independent: one failing does not stop others."""
        retry_contacts = int(env("LEADGEN_CONTACT_RETRIES_PER_RUN", "3") or 3)
        groups = (
            ([LeadStatus.DISCOVERED], 50),
            ([LeadStatus.CONTACT_FOUND, LeadStatus.EMAIL_DRAFTED], 50),
        )
        max_attempts = int(self.cfg.contacts.get("max_attempts", 3))
        batches = [await self.repo.leads_with_status(statuses, limit) for statuses, limit in groups]
        batches.append(await self.repo.leads_needing_contact(max_attempts, retry_contacts))
        for batch in batches:
            for lead in batch:
                if self.stop_reason():
                    return
                if await self.drop_if_excluded(lead):
                    continue
                self.stats.add("resumed")
                try:
                    await self.advance(lead)
                except (BudgetExhaustedError, SearchBlockedError):
                    raise
                except Exception:
                    self.stats.add("errors")
                    log.exception("Resuming lead failed", extra={"ctx": {"lead_id": lead.lead_id}})

    async def drop_if_excluded(self, lead: Lead) -> bool:
        """A company added to exclude_companies after it was stored: reject it instead of working on it again."""
        why = intent.exclusion_reason("", lead.company_website or "", lead.company_name or "", [],
                                      self.cfg.exclude_companies)  # fmt: skip
        if not why or not lead.lead_id:
            return False
        await self.repo.set_status(lead.lead_id, LeadStatus.REJECTED, note=why)
        lead.status = LeadStatus.REJECTED
        self.stats.add("rejected")
        log.info("Lead rejected", extra={"ctx": {"lead_id": lead.lead_id, "why": why}})
        return True

    # --- discovery -----------------------------------------------------------------------------------------

    def skip_result(self, r: SearchResult) -> str | None:  # noqa: PLR0911 - one reason per rule
        """Cheap pre-filter on the result list (no page opened): kinds of site that are never leads."""
        if web.blocked(r.url or r.domain):
            return "portal whose terms forbid scraping (never opened)"
        pi = intent.analyze(r.url or f"https://{r.domain}", r.title, r.snippet)
        asks = pi.project_request >= 1
        if pi.host_kind in ("marketplace", "distributor", "directory") and not asks:
            return f"{pi.host_kind} site"
        if pi.host_kind == "docs" and not asks:
            return "documentation / code / Q&A site"
        if pi.host_kind == "freelance" and not asks:
            return "freelancer profile or listing, not a company's project"
        if intent.SELF_PROMO.search(f"{r.title} {r.snippet}") and not asks:
            return "a developer or agency advertising itself, not a company with a project"
        if pi.selling >= 2 and not asks:
            return "result looks like a product listing"
        services = bool(self.cfg.q("accept_service_companies", True))
        if not services and intent.SERVICES_TITLE.search(r.title) and not asks:
            return "an engineering-services company (a competitor), not a buyer"
        if intent.LIST_PAGE.search(r.title) and not asks:
            return "a list or directory of companies, not a company"
        if r.snippet and pi.relevance_terms == 0 and pi.hardware == 0 and not asks:
            return "title and snippet mention none of our technology or markets"
        host = intent.registrable_domain(r.url or r.domain)
        if why := intent.country_excluded(host, "", [], self.cfg.exclude_tlds):
            return why
        return intent.exclusion_reason(f"{r.title} {r.snippet}", host, "", self.cfg.exclude_terms,
                                       self.cfg.exclude_companies) or None  # fmt: skip

    async def search(self, q: strategy.Query) -> None:
        suffix = str(self.cfg.search.get("query_suffix") or "").strip()
        text = f"{q.text} {suffix}".strip()
        try:
            results = await self.svc.browser.search(
                text, int(self.cfg.search.get("results_per_query", 10))
            )
        except SearchBlockedError as exc:
            self.stats.add("searches_blocked")
            if "results page" in str(exc):
                self.llm_failures += 1  # the model host is struggling: stop after a few in a row
            log.warning(
                "Search blocked; query kept for a later run",
                extra={"ctx": {"query": q.text, "why": str(exc)}},
            )
            return
        except BudgetExhaustedError:
            raise
        except Exception:
            self.stats.add("errors")
            log.exception(
                "Search failed; query kept for a later run", extra={"ctx": {"query": q.text}}
            )
            return
        self.llm_failures = 0
        self.stats.add("searches")
        it = q.intent
        log.info(
            "Search intent",
            extra={"ctx": {"target": it.target if it else "potential_customer", "family": q.family,
                           "product": it.product if it else "", "technology": it.technology if it else "",
                           "capability": it.capability if it else ""}},
        )  # fmt: skip
        log.info(
            "Search executed",
            extra={"ctx": {"query": q.text, "family": q.family, "results": len(results)}},
        )
        opened = 0
        per_query = int(self.cfg.search.get("pages_per_query", 4))
        for r in rank(results):
            if opened >= per_query or self.stop_reason():
                break
            if why := self.skip_result(r):
                self.stats.add("results_skipped")
                log.info(
                    "Result rejected",
                    extra={"ctx": {"why": why, "title": r.title[:80], "domain": r.domain}},
                )
                continue
            if r.url and await self.repo.page_seen(identity.url_key(r.url)):
                self.stats.add("results_already_seen")
                continue
            known = await self.repo.find_lead(domain=intent.registrable_domain(r.url or r.domain))
            if known and known.status != LeadStatus.REJECTED:
                self.stats.add("results_known_company")
                continue
            opened += 1
            try:
                page = await self.svc.browser.open(r)
            except BudgetExhaustedError:
                raise
            except Exception:
                self.stats.add("errors")
                log.exception("Opening a result failed", extra={"ctx": {"title": r.title[:80]}})
                continue
            if page is None:
                self.stats.add("pages_failed")
                continue
            await self.observe(Observation(q.text, page, r))
        await self.repo.record_query(
            q.text, q.family, results[0].engine if results else "", len(results), opened
        )

    async def feeds(self) -> None:
        """Job / project listings from the free feed APIs (config/sources.yaml): text only, no page to open."""
        try:
            signals = await sources.collect_signals(int(self.cfg.search.get("feed_items", 40)))
        except Exception:
            self.stats.add("errors")
            log.exception("Job feeds failed")
            return
        for sig in signals:
            if self.stop_reason():
                return
            text = f"{sig.company_hint and f'Company: {sig.company_hint}. '}{sig.text}"
            await self.observe(
                Observation(
                    "feeds",
                    Page(url=sig.url, title=sig.title, text=text),
                    origin=f"feed:{sig.source}",
                )
            )

    async def observe(self, obs: Observation) -> None:
        page = obs.page
        key = identity.url_key(page.url)
        self.stats.add("pages_read")
        log.info("Result discovered", extra={"ctx": {"url": page.url, "query": obs.query[:80]}})
        if page.blocked:
            self.stats.add("pages_blocked")
            await self.repo.record_page(key, page.url, "blocked", {"blocked": page.blocked})
            log.info(
                "Result rejected",
                extra={"ctx": {"why": f"blocked: {page.blocked}", "url": page.url}},
            )
            return
        if await self.repo.page_seen(key):
            self.stats.add("pages_already_seen")
            return
        try:
            verdict = await qualify_page(page, self.cfg, self.svc.assess)
            self.llm_failures = 0
        except (LLMError, TypeError, ValueError, TimeoutError) as exc:
            self.llm_failures += 1
            self.stats.add("llm_errors")
            log.warning("Qualification failed; page left for a later run",
                        extra={"ctx": {"url": page.url, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}})  # fmt: skip
            return
        if verdict.used_model:
            self.stats.add("model_calls")
        await self.handle(verdict, page, key, obs.origin)

    async def handle(self, verdict: Verdict, page: Page, key: str, origin: str) -> None:
        lead = verdict.lead
        if lead is None:
            self.stats.add("rejected")
            await self.repo.record_page(key, page.url, "rejected", verdict.record())
            log.info("Result rejected", extra={"ctx": {"why": verdict.reason, "url": page.url}})
            return
        existing = await self.repo.find_lead(domain=lead.company_website, name_key=lead.name_key)
        if existing:
            await self.merge(existing, lead, verdict)
            await self.repo.record_page(
                key, page.url, "duplicate", verdict.record(), existing.lead_id
            )
            return
        if not verdict.accepted:
            self.stats.add("rejected")
            await self.repo.record_page(key, page.url, "rejected", verdict.record())
            log.info("Result rejected", extra={"ctx": {"why": verdict.reason, "company": lead.company_name,
                                                       "score": lead.lead_score}})  # fmt: skip
            return
        log.info("Company identified", extra={"ctx": {"company": lead.company_name, "website": lead.company_website,
                                                      "score": lead.lead_score, "signal": lead.project_signal}})  # fmt: skip
        if not lead.company_website:
            lead.company_website = await self.find_website(lead)
            if not lead.company_website:
                self.stats.add("rejected")
                await self.repo.record_page(key, page.url, "no_website", verdict.record())
                log.info("Result rejected", extra={"ctx": {"why": "company website not found",
                                                           "company": lead.company_name}})  # fmt: skip
                return
            if why := intent.country_excluded(lead.company_website, lead.location, self.cfg.exclude_countries,
                                              self.cfg.exclude_tlds):  # fmt: skip
                self.stats.add("rejected")
                await self.repo.record_page(
                    key, page.url, "rejected", {**verdict.record(), "why": why}
                )
                return
            if existing := await self.repo.find_lead(domain=lead.company_website):
                await self.merge(existing, lead, verdict)
                await self.repo.record_page(
                    key, page.url, "duplicate", verdict.record(), existing.lead_id
                )
                return
        lead_id = await self.repo.insert_lead(lead, source=f"leadgen:{origin}")
        await self.repo.record_page(key, page.url, "lead", verdict.record(), lead_id)
        self.touched.append(lead_id)
        self.stats.add("leads_discovered")
        await self.repo.set_status(lead_id, LeadStatus.QUALIFIED, note=verdict.reason)
        lead.status = LeadStatus.QUALIFIED
        self.stats.add("leads_qualified")
        log.info("Lead qualified", extra={"ctx": {"lead_id": lead_id, "company": lead.company_name,
                                                  "score": lead.lead_score, "evidence": len(lead.evidence)}})  # fmt: skip
        await self.advance(lead)

    async def merge(self, existing: Lead, new: Lead, verdict: Verdict) -> None:
        """Same company from another page: one lead, more evidence. A rejected company may qualify now."""
        self.stats.add("duplicates_merged")
        existing.add_evidence(new.evidence)
        existing.technical_requirements = requirements(
            existing.technical_requirements, new.technical_requirements
        )
        existing.score = scoring.merge(existing.score, new.score, len(existing.source_urls))
        existing.lead_score = existing.score.total if existing.score else existing.lead_score
        if not existing.product and new.product:
            existing.product = new.product
        rank = [
            "none",
            "product_development",
            "partner_capacity",
            "hiring",
            "outsourcing_request",
            "rfp_or_tender",
        ]
        if rank.index(new.project_signal) > rank.index(existing.project_signal):
            existing.project_signal = new.project_signal
            existing.opportunity_description = (
                new.opportunity_description or existing.opportunity_description
            )
        await self.repo.save_lead(existing)
        log.info("Duplicate company: evidence merged", extra={"ctx": {
            "lead_id": existing.lead_id, "company": existing.company_name, "sources": len(existing.source_urls),
            "score": existing.lead_score}})  # fmt: skip
        assert existing.lead_id  # noqa: S101 - stored leads have ids
        revive = (
            existing.status == LeadStatus.REJECTED
            and verdict.accepted
            and existing.email_draft_id is None  # a person's rejection of a draft is final
        )
        if revive and await self.repo.set_status(
            existing.lead_id, LeadStatus.QUALIFIED, note="re-qualified on new evidence"
        ):
            existing.status = LeadStatus.QUALIFIED
            self.touched.append(existing.lead_id)
            await self.advance(existing)

    async def find_website(self, lead: Lead) -> str:
        """The company's own site, found the way a person would: search its name, take a result whose domain
        carries the name. Never constructed from the name."""
        try:
            results = await self.svc.browser.search(f'"{lead.company_name}"', 8)
        except SearchBlockedError:
            return ""
        for r in results:
            dom = intent.registrable_domain(r.url or r.domain)
            if identity.is_company_site(dom) and identity.name_matches_domain(
                lead.company_name, dom
            ):
                return dom
        return ""

    # --- after qualification -----------------------------------------------------------------------------------

    async def advance(self, lead: Lead) -> None:
        """Move one lead as far as it can go now: QUALIFIED -> CONTACT_FOUND -> EMAIL_DRAFTED -> PENDING_APPROVAL."""
        assert lead.lead_id  # noqa: S101
        min_q, min_d = int(self.cfg.q("min_qualify", 50)), int(self.cfg.q("min_draft", 60))
        if lead.status == LeadStatus.DISCOVERED:  # a crash right after it was stored
            ok = lead.lead_score >= min_q
            new = LeadStatus.QUALIFIED if ok else LeadStatus.REJECTED
            await self.repo.set_status(lead.lead_id, new, note="resumed")
            lead.status = new
            if not ok:
                return
        if lead.status == LeadStatus.QUALIFIED and not await self.contact(lead):
            return
        if lead.status == LeadStatus.CONTACT_FOUND:
            if lead.lead_score < min_d:
                await self.repo.set_status(lead.lead_id, LeadStatus.REJECTED,
                                           note=f"score {lead.lead_score} below draft threshold {min_d}")  # fmt: skip
                lead.status = LeadStatus.REJECTED
                self.stats.add("rejected_below_draft_score")
                log.info("Lead rejected", extra={"ctx": {"lead_id": lead.lead_id, "score": lead.lead_score,
                                                         "why": "below draft threshold"}})  # fmt: skip
                return
            if not await self.write_email(lead):
                return
        if lead.status == LeadStatus.EMAIL_DRAFTED:
            email = await self.repo.email_for_lead(lead.lead_id)
            if (
                email
                and lead.contact
                and await approval.request(self.repo, self.svc.approver, lead, lead.contact, email)
            ):
                lead.status = LeadStatus.PENDING_APPROVAL
                self.stats.add("approvals_requested")

    async def contact(self, lead: Lead) -> bool:
        assert lead.lead_id  # noqa: S101
        found = await self.svc.find_contacts(
            self.svc.browser, lead.company_website, self.cfg, company=lead.company_name
        )
        if found.competitor:
            note = f"competitor, not a buyer: {found.competitor}"
            await self.repo.set_status(lead.lead_id, LeadStatus.REJECTED, note=note)
            lead.status = LeadStatus.REJECTED
            self.stats.add("rejected_competitor")
            log.info("Lead rejected", extra={"ctx": {"lead_id": lead.lead_id, "why": note}})
            return False
        new_form = await self.repo.set_manual(lead.lead_id, found.form_url, found.blocked)
        if new_form and found.form_url:
            await self._form_fill(lead, found.form_url)
        if not found.best:
            reason = found.blocked or "no public business contact on the company's site"
            attempts = await self.repo.record_contact_attempt(lead.lead_id)
            max_attempts = int(self.cfg.contacts.get("max_attempts", 3))
            lead.score = (
                scoring.with_contact(lead.score, None, form_only=bool(found.form_url))
                if lead.score
                else None
            )
            lead.lead_score = lead.score.total if lead.score else lead.lead_score
            await self.repo.save_lead(lead)
            self.stats.add("contact_not_found")
            log.info("Contact not found", extra={"ctx": {"lead_id": lead.lead_id, "why": reason, "attempt": attempts,
                                                         "form": bool(found.form_url)}})  # fmt: skip
            # The last attempt (a contact form is retried too): a person takes it from here, once.
            if attempts == max_attempts and not found.form_url:  # a form goes to #form-fill instead
                await self._manual_check(lead, reason, found.form_url)
            if attempts >= max_attempts and not found.form_url:
                note = f"no contact after {attempts} attempts: {reason}"
                await self.repo.set_status(lead.lead_id, LeadStatus.REJECTED, note=note)
                lead.status = LeadStatus.REJECTED
                self.stats.add("rejected_no_contact")
                log.info("Lead rejected", extra={"ctx": {"lead_id": lead.lead_id, "why": note}})
            return False
        for c in found.contacts[:3]:
            c.id = await self.repo.save_contact(lead.lead_id, c)
        lead.contact = found.best
        lead.add_evidence(_contact_evidence(found.best))
        if lead.score:
            lead.score = scoring.with_contact(lead.score, found.best)
            lead.lead_score = lead.score.total
        await self.repo.save_lead(lead)
        await self.repo.set_status(lead.lead_id, LeadStatus.CONTACT_FOUND)
        lead.status = LeadStatus.CONTACT_FOUND
        self.stats.add("contacts_found")
        log.info("Contact found", extra={"ctx": {"lead_id": lead.lead_id, "role": found.best.role,
                                                 "rank": found.best.rank, "named": bool(found.best.name)}})  # fmt: skip
        return True

    async def _form_fill(self, lead: Lead, form_url: str) -> None:
        """Tell a person where a company's contact form is, so they fill it by hand. Never fails the run."""
        if self.svc.form_notify is None:
            return
        try:
            await self.svc.form_notify(lead, form_url)
        except Exception as exc:
            log.warning("form-fill post failed", extra={"ctx": {"lead_id": lead.lead_id, "error": str(exc)[:200]}})  # fmt: skip
            return
        self.stats.add("form_fill_posted")
        log.info("Contact form posted", extra={"ctx": {"lead_id": lead.lead_id, "company": lead.company_name}})  # fmt: skip

    async def _manual_check(self, lead: Lead, reason: str, form_url: str | None) -> None:
        """Tell a person about a qualified company we found no contact for. Never fails the run."""
        if self.svc.manual_notify is None:
            return
        try:
            await self.svc.manual_notify(lead, reason, form_url)
        except Exception as exc:
            log.warning("manual-check post failed", extra={"ctx": {"lead_id": lead.lead_id, "error": str(exc)[:200]}})  # fmt: skip
            return
        self.stats.add("manual_check_posted")
        log.info("Manual check requested", extra={"ctx": {"lead_id": lead.lead_id, "company": lead.company_name}})  # fmt: skip

    async def write_email(self, lead: Lead) -> bool:
        assert lead.lead_id  # noqa: S101
        assert lead.contact  # noqa: S101
        if (existing := await self.repo.email_for_lead(lead.lead_id)) is None:
            try:
                d, problems = await self.svc.draft(lead, lead.contact)
            except Exception as exc:
                self.stats.add("llm_errors")
                log.warning("Email generation failed; retried next run",
                            extra={"ctx": {"lead_id": lead.lead_id, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}})  # fmt: skip
                return False
            if setup := mailer.identity_problems():
                log.warning(
                    "Email identity settings incomplete", extra={"ctx": {"problems": setup}}
                )
            contact_id = lead.contact.id or await self.repo.save_contact(lead.lead_id, lead.contact)
            note = "; ".join(problems) or None
            if (
                await self.repo.save_email_draft(lead.lead_id, contact_id, d.subject, d.body, note)
                is None
            ):
                log.warning(
                    "Email draft not saved (one already exists)",
                    extra={"ctx": {"lead_id": lead.lead_id}},
                )
            self.new_drafts += 1
            self.stats.add("emails_drafted")
            log.info(
                "Email generated", extra={"ctx": {"lead_id": lead.lead_id, "flags": len(problems)}}
            )
        elif existing.status != "drafted":
            log.warning("Lead and email disagree; left for a person",
                        extra={"ctx": {"lead_id": lead.lead_id, "email_status": existing.status}})  # fmt: skip
            return False
        await self.repo.set_status(lead.lead_id, LeadStatus.EMAIL_DRAFTED)
        lead.status = LeadStatus.EMAIL_DRAFTED
        return True


def rank(results: list[SearchResult]) -> list[SearchResult]:
    """Most promising first: an explicit request, then the most of our technology / markets in title and snippet.
    The page budget per query is then spent on the best results, not on whatever the engine listed first."""

    def key(r: SearchResult) -> tuple[int, int]:
        pi = intent.analyze(r.url or f"https://{r.domain}", r.title, r.snippet)
        return (pi.asks, pi.relevance_terms)

    return sorted(results, key=key, reverse=True)  # stable: the engine's order breaks ties


def _contact_evidence(c: Contact) -> list[Evidence]:
    who = " ".join(x for x in (c.name, f"({c.role})" if c.role else "") if x)
    return [Evidence(url=c.source, reason=f"business contact published on the company site: {who or c.email}",
                     source="contact")]  # fmt: skip


def today_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
