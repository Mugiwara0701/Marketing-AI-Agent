"""Daily-run limits and sender identity. Everything comes from the environment with safe defaults."""

from dataclasses import dataclass

from agentkit.config import env


def _int(name: str, default: int) -> int:
    return int(env(name, str(default)) or default)


@dataclass(frozen=True)
class Settings:
    max_new_leads: int  # hard cap on new actionable leads per day
    max_signals: int  # cap on listings the LLM looks at per run
    max_minutes: int  # whole run, leads + blog
    blog_reserve_minutes: int  # always kept free for the blog
    send_cap: int  # max emails sent per run
    send_gap_seconds: int  # pause between sends
    followup_delay_days: int  # wait this long after the intro before the one follow-up
    followup_max_per_run: int  # follow-up drafts per run
    user_agent: str
    sources_file: str
    platforms_file: str
    lead_target: int  # stop the desktop search once this many qualified leads are stored
    lead_max: int  # hard cap on stored leads per day
    leads_mode: str  # "api" (job APIs, HTTP) or "desktop" (visible Chrome on the desktop)


def desktop_mode() -> bool:
    return (env("LEADS_MODE", "api") or "api").lower() == "desktop"


def load() -> Settings:
    return Settings(
        max_new_leads=_int("MAX_NEW_LEADS_PER_DAY", 10),
        max_signals=_int("MAX_SIGNALS_PER_RUN", 150),
        max_minutes=_int("AGENT_MAX_MINUTES", 120),
        blog_reserve_minutes=_int("BLOG_RESERVE_MINUTES", 25),
        send_cap=_int("DAILY_SEND_CAP_PER_MAILBOX", 10),
        send_gap_seconds=_int("SEND_GAP_SECONDS", 20),
        followup_delay_days=_int("FOLLOWUP_DELAY_DAYS", 4),
        followup_max_per_run=_int("FOLLOWUP_MAX_PER_RUN", 10),
        user_agent=env("AGENT_USER_AGENT", "AOSPMarketingAgent/1.0 (business research bot)")
        or "AOSPMarketingAgent/1.0",
        sources_file=env("SOURCES_CONFIG", "config/sources.yaml") or "config/sources.yaml",
        lead_target=_int("DAILY_LEAD_TARGET", 5),
        lead_max=_int("MAX_DAILY_LEADS", 6),
        leads_mode=(env("LEADS_MODE", "api") or "api").lower(),
        platforms_file=env("PLATFORMS_CONFIG", "config/platforms.yaml") or "config/platforms.yaml",
    )
