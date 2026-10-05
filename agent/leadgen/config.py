"""Lead pipeline settings: config/leadgen.yaml (vocabulary, thresholds, exclusions) plus the environment
(which browser, which database, dry-run). Nothing machine-specific or secret is hard-coded."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentkit.config import env


@dataclass(frozen=True)
class LeadgenConfig:
    raw: dict[str, Any]
    search: dict[str, Any]
    qualification: dict[str, Any]
    contacts: dict[str, Any]
    limits: dict[str, Any]
    exclude_terms: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    exclude_countries: list[str] = field(default_factory=list)
    exclude_tlds: list[str] = field(default_factory=list)
    desktop_engines: list[str] = field(default_factory=list)

    def q(self, key: str, default: Any) -> Any:
        return self.qualification.get(key, default)

    def limit(self, key: str, default: Any) -> Any:
        return self.limits.get(key, default)


def load(path: str | None = None) -> LeadgenConfig:
    p = Path(path or env("LEADGEN_CONFIG", "config/leadgen.yaml") or "config/leadgen.yaml")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return LeadgenConfig(
        raw=raw,
        search=raw.get("search") or {},
        qualification=raw.get("qualification") or {},
        contacts=raw.get("contacts") or {},
        limits=raw.get("limits") or {},
        exclude_terms=list(raw.get("exclude_terms") or []),
        exclude_companies=list(raw.get("exclude_companies") or []),
        exclude_countries=list(raw.get("exclude_countries") or []),
        exclude_tlds=list(raw.get("exclude_tlds") or []),
        desktop_engines=list(raw.get("desktop_engines") or ["default"]),
    )


@dataclass(frozen=True)
class Runtime:
    """How this run executes. dry_run: own SQLite database, simulated Slack, no email can leave."""

    dry_run: bool
    browser: str  # http | chrome | desktop
    sqlite_path: str  # used when dry_run (or when no DATABASE_URL is set)
    out_dir: Path

    @property
    def uses_postgres(self) -> bool:
        return not self.dry_run and bool(env("DATABASE_URL"))


def runtime(*, dry_run: bool | None = None, browser: str | None = None) -> Runtime:
    dry = (
        dry_run
        if dry_run is not None
        else (env("LEADGEN_DRY_RUN", "false") or "").lower() == "true"
    )
    out = Path(env("LEADGEN_OUT_DIR", "out/leadgen") or "out/leadgen")
    return Runtime(
        dry_run=dry,
        browser=(browser or env("BROWSER_BACKEND", "http") or "http").lower(),
        sqlite_path=env("LEADGEN_SQLITE", str(out / "dryrun.sqlite3"))
        or str(out / "dryrun.sqlite3"),
        out_dir=out,
    )
