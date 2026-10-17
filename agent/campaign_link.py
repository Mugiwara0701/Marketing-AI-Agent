"""The direct link to the Campaign Agent (CA) over Tailscale: which campaign to run, and what went wrong.

    Marketing Agent (this machine) --HTTP over the tailnet, outgoing only--> Campaign Agent (campaign-agent:8787)

The CA is the only server; nothing listens on this machine. Two things travel:

* the live campaign, fetched at the start of every discovery pass (`apply_campaign`), checked (sha256, shape), merged over
  config/leadgen.yaml and used for the whole pass. On any failure the last good campaign (a file in the cache folder) or the
  base config keeps running; a half-applied campaign never runs.
* events (`emit`): a blocker, a refused campaign, ... written to a local outbox and sent by `run_sender` with retries, so
  nothing is lost while the CA or the network is down. Events are shown to people; they are not commands.

Off unless MA_LINK_URL and MA_LINK_TOKEN are set: then the agent behaves exactly as before. Email approval is not a
campaign setting and nothing here can change it: a campaign may only fill the keys listed in OVERLAY_SECTIONS.
Protocol: Campaign-Agent/docs/ca-ma-link.md.
"""

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import random
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from agentkit import db, slack
from agentkit.config import env
from agentkit.log import get_logger

from .leadgen.roles import PREFERRED_ROLES

log = get_logger("agent.campaign_link")

# The config_version values this loader accepts. The CA checks this against what the status row reports.
SUPPORTED_CONFIG_VERSIONS = ("1.0",)
# The only sections of leadgen.yaml a campaign may fill (the CA's contract), and the lists it may add to.
OVERLAY_SECTIONS = ("project_context", "search", "qualification", "limits")
EXCLUDE_KEYS = ("exclude_terms", "exclude_companies", "exclude_countries", "exclude_tlds")
# Top-level lists only a campaign sets; each replaces (the base has none).
CAMPAIGN_LISTS = ("include_countries", "preferred_roles")
PROJECT_KEYS = {
    "name",
    "industries",
    "products",
    "technologies",
    "hardware",
    "engineering_requirements",
}
NO_CAMPAIGN = "none"
MAX_OUTBOX = 1000
BATCH = 50
KINDS = ("applied", "rejected", "blocker", "resolved", "info")
SEVERITIES = ("info", "warning", "error", "critical")


class LinkError(Exception):
    """The CA could not be reached or answered with an error. `auth` is True for a refused token."""

    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


class OverlayError(ValueError):
    """The campaign file is not acceptable. The message is one sentence naming the key and the reason."""


# --- settings -----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkConfig:
    url: str
    token: str
    cache_dir: Path
    timeout_s: float = 10.0
    send_every_s: float = 5.0


def link_config() -> LinkConfig | None:
    """None when the link is not configured: the agent then behaves exactly as before."""
    url, token = env("MA_LINK_URL"), env("MA_LINK_TOKEN")
    if not url or not token:
        return None
    return LinkConfig(
        url=url.rstrip("/"),
        token=token,
        cache_dir=Path(env("MA_LINK_CACHE_DIR", ".cache/campaign") or ".cache/campaign"),
        timeout_s=float(env("MA_LINK_TIMEOUT_S", "10") or 10),
        send_every_s=float(env("MA_LINK_SEND_EVERY_S", "5") or 5),
    )


# --- the campaign -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    version: int
    sha256: str
    contract_version: str
    overlay_yaml: str
    tag: str
    # The complete leadgen.yaml the CA built, the hash of its text, and the fingerprint of the base it was built on.
    # None from an older CA: the overlay is then merged onto this machine's own base, as before.
    leadgen_yaml: str | None = None
    leadgen_sha256: str | None = None
    base_sha256: str | None = None

    def intact(self) -> bool:
        """Is the text what the CA approved? The hash is checked again here, whatever the CA says."""
        return hashlib.sha256(self.overlay_yaml.encode("utf-8")).hexdigest() == self.sha256

    def as_json(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "version": self.version,
            "sha256": self.sha256,
            "contract_version": self.contract_version,
            "overlay_yaml": self.overlay_yaml,
            "tag": self.tag,
            **(
                {
                    "leadgen_yaml": self.leadgen_yaml,
                    "leadgen_sha256": self.leadgen_sha256,
                    "base_sha256": self.base_sha256,
                }
                if self.leadgen_yaml
                else {}
            ),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], tag: str | None = None) -> "Campaign":
        try:
            return cls(
                campaign_id=str(data["campaign_id"]),
                version=int(data["version"]),
                sha256=str(data["sha256"]),
                contract_version=str(data["contract_version"]),
                overlay_yaml=str(data["overlay_yaml"]),
                tag=str(tag or data["tag"]),
                leadgen_yaml=str(data["leadgen_yaml"]) if data.get("leadgen_yaml") else None,
                leadgen_sha256=str(data["leadgen_sha256"]) if data.get("leadgen_sha256") else None,
                base_sha256=str(data["base_sha256"]) if data.get("base_sha256") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LinkError(f"the campaign answer is incomplete: {exc!r}") from exc


def _norm(text: str) -> str:
    return " ".join(str(text).casefold().split())


def _type_ok(base: Any, value: Any) -> bool:
    if isinstance(base, bool):
        return isinstance(value, bool)
    if isinstance(base, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(base, float):
        return isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(base, list):
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    if isinstance(base, str):
        return isinstance(value, str)
    return False


def parse_overlay(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OverlayError(
            f"the campaign file is not valid YAML: {str(exc).splitlines()[0]}"
        ) from exc
    if not isinstance(data, dict):
        raise OverlayError("the campaign file must be a YAML mapping")
    return data


def check_overlay(overlay: dict[str, Any], base: dict[str, Any]) -> None:  # noqa: PLR0912 - one branch per rule
    """Shape check against the base config: only known keys, of the right type, in the campaign-owned sections.

    This is a stand-in for the strict, versioned loader (M8 item 1); it never accepts more than that loader would.
    Raises OverlayError with one sentence.
    """
    version = overlay.get("config_version")
    if str(version) not in SUPPORTED_CONFIG_VERSIONS:
        raise OverlayError(
            f"config_version {version!r} is not supported (this agent loads {', '.join(SUPPORTED_CONFIG_VERSIONS)})"
        )
    allowed_top = {"config_version", *OVERLAY_SECTIONS, *EXCLUDE_KEYS, *CAMPAIGN_LISTS}
    for key in overlay:
        if key not in allowed_top:
            raise OverlayError(f"{key}: a campaign may not set this key")
    for section in OVERLAY_SECTIONS:
        values = overlay.get(section)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise OverlayError(f"{section}: must be a mapping")
        known = base.get(section) or {}
        for key, value in values.items():
            if section == "project_context":
                if key not in PROJECT_KEYS:
                    raise OverlayError(f"{section}.{key}: unknown key")
                ok = isinstance(value, str) if key == "name" else _type_ok([], value)
            else:
                if key not in known:
                    raise OverlayError(f"{section}.{key}: unknown key")
                ok = _type_ok(known[key], value)
            if not ok:
                raise OverlayError(f"{section}.{key}: wrong type")
    for key in (*EXCLUDE_KEYS, *CAMPAIGN_LISTS):
        value = overlay.get(key)
        if value is not None and not _type_ok([], value):
            raise OverlayError(f"{key}: must be a list of text")
    unknown = [r for r in overlay.get("preferred_roles") or [] if r not in PREFERRED_ROLES]
    if unknown:
        raise OverlayError(f"preferred_roles: unknown role(s) {unknown}; known: {sorted(PREFERRED_ROLES)}")


def merge_overlay(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """The configuration to run: the base with the campaign applied. Same rule as the CA's `merge_onto_base`: a section key
    replaces the base value, exclusion lists are added to the base's. The base is not changed."""
    effective = copy.deepcopy(base)
    for section in OVERLAY_SECTIONS:
        values = overlay.get(section) or {}
        if values:
            effective.setdefault(section, {})
            if not isinstance(effective[section], dict):
                effective[section] = {}
        for key, value in values.items():
            effective[section][key] = list(value) if isinstance(value, list) else value
    for key in EXCLUDE_KEYS:
        merged = list(effective.get(key) or [])
        seen = {_norm(x) for x in merged}
        for item in overlay.get(key) or []:
            if _norm(item) not in seen:
                seen.add(_norm(item))
                merged.append(item)
        effective[key] = merged
    # Replace, and are present only when the campaign sets them (the CA's compiler/complete.py does the same).
    for key in CAMPAIGN_LISTS:
        if overlay.get(key):
            effective[key] = list(overlay[key])
    return effective


def base_fingerprint(raw: dict[str, Any]) -> str:
    """A hash of the parsed base file (comments and spacing do not matter). The CA computes the same from its snapshot."""
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    """The first key where two parsed files differ, as a dotted path; None when they are equal."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                return f"{path}{key}"
            if (found := first_difference(a[key], b[key], f"{path}{key}.")) is not None:
                return found
        return None
    return None if a == b else (path.rstrip(".") or "(file)")


def resolve(base: dict[str, Any], campaign: Campaign) -> tuple[dict[str, Any], str, str | None]:
    """(configuration to run, mode, note). Modes: "overlay" (an older CA: the campaign on this machine's base),
    "complete" (the CA's complete file, proven to be this machine's base plus the campaign) and "overlay_fallback" (the
    complete file did not match: the campaign is applied on this machine's base and `note` says where it differed).
    A complete file is never run unless it equals what this machine would build itself. Raises OverlayError if the
    campaign itself is not acceptable."""
    overlay = parse_overlay(campaign.overlay_yaml)
    check_overlay(overlay, base)
    expected = merge_overlay(base, overlay)
    if not campaign.leadgen_yaml:
        return expected, "overlay", None
    if hashlib.sha256(campaign.leadgen_yaml.encode("utf-8")).hexdigest() != campaign.leadgen_sha256:
        return expected, "overlay_fallback", "the complete file does not match its own hash"
    try:
        full = yaml.safe_load(campaign.leadgen_yaml)
    except yaml.YAMLError:
        return expected, "overlay_fallback", "the complete file is not valid YAML"
    if not isinstance(full, dict):
        return expected, "overlay_fallback", "the complete file is not a mapping"
    full.pop("config_version", None)
    if (where := first_difference(expected, full)) is not None:
        return expected, "overlay_fallback", f"it differs from this machine's base file at {where}"
    return full, "complete", None


# --- the cache: the last good campaign, and what the link last did ------------------------------------------------------


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_cache(cfg: LinkConfig) -> Campaign | None:
    """The last good campaign, or None. A damaged or altered file counts as none: it is checked again."""
    try:
        data = json.loads((cfg.cache_dir / "active.json").read_text(encoding="utf-8"))
        campaign = Campaign.from_json(data)
    except (OSError, ValueError, LinkError):
        return None
    return campaign if campaign.intact() else None


def save_cache(cfg: LinkConfig, campaign: Campaign) -> None:
    _write_atomic(cfg.cache_dir / "active.json", json.dumps(campaign.as_json()))


def clear_cache(cfg: LinkConfig) -> None:
    with contextlib.suppress(OSError):
        (cfg.cache_dir / "active.json").unlink()


def _load_state(cfg: LinkConfig) -> dict[str, Any]:
    try:
        data = json.loads((cfg.cache_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(cfg: LinkConfig, state: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        _write_atomic(cfg.cache_dir / "state.json", json.dumps(state))


# --- events: a local outbox, sent in the background ---------------------------------------------------------------------

_alert_tasks: set[asyncio.Task[None]] = set()


class Outbox:
    """Events waiting for the CA, one JSON object per line. Bounded: when full, `info` events go first, then applied/
    rejected/resolved ones; the oldest blocker only when nothing else is left."""

    def __init__(self, path: Path, limit: int = MAX_OUTBOX) -> None:
        self.path = path
        self.limit = limit

    def _read(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events = []
        for line in lines:
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, dict) and "event_id" in item:
                    events.append(item)
        return events

    def _write(self, events: list[dict[str, Any]]) -> None:
        _write_atomic(self.path, "".join(json.dumps(e) + "\n" for e in events))

    def add(self, event: dict[str, Any]) -> None:
        events = self._read()
        events.append(event)
        while len(events) > self.limit:
            drop = next((i for i, e in enumerate(events) if e["kind"] == "info"), None)
            if drop is None:
                drop = next((i for i, e in enumerate(events) if e["kind"] != "blocker"), 0)
            del events[drop]
        self._write(events)

    def pending(self, n: int = BATCH) -> list[dict[str, Any]]:
        return self._read()[:n]

    def remove(self, ids: set[str]) -> None:
        if not ids:
            return
        self._write([e for e in self._read() if e["event_id"] not in ids])

    def __len__(self) -> int:
        return len(self._read())


def _text(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def emit(
    kind: str,
    severity: str,
    code: str,
    message: str,
    *,
    campaign_id: str | None = None,
    version: int | None = None,
    pass_id: str | None = None,
    dedupe_key: str | None = None,
    details: dict[str, Any] | None = None,
    cfg: LinkConfig | None = None,
) -> None:
    """Queue an event for the CA. Never raises and never blocks: a problem here must not hurt a pass.

    `message` and `details` must not carry secrets or page contents. A critical event is also posted to Slack, because
    the CA may be the thing that is down. No link configured: nothing is queued.
    """
    cfg = cfg or link_config()
    if cfg is None or kind not in KINDS or severity not in SEVERITIES:
        return
    event: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "severity": severity,
        "code": code,
        "message": _text(message, 300) or code,
    }
    for name, value in (
        ("campaign_id", campaign_id),
        ("version", version),
        ("pass_id", pass_id),
        ("dedupe_key", dedupe_key),
    ):
        if value is not None:
            event[name] = value  # noqa: PERF403
    if details:
        event["details"] = details
    try:
        Outbox(cfg.cache_dir / "outbox.jsonl").add(event)
    except OSError:
        log.exception("Could not queue an event", extra={"ctx": {"code": code}})
    if severity == "critical":
        _alert(f"[marketing-agent] {code}: {event['message']}")


def _alert(text: str) -> None:
    """Slack, without waiting. Outside an event loop (a test, a script) it does nothing."""
    try:
        task = asyncio.get_running_loop().create_task(slack.alert(text))
    except RuntimeError:
        return
    _alert_tasks.add(task)
    task.add_done_callback(_alert_tasks.discard)


def _client(cfg: LinkConfig, client: httpx.AsyncClient | None) -> httpx.AsyncClient:
    return client or httpx.AsyncClient(timeout=cfg.timeout_s)


def _headers(cfg: LinkConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.token}"}


async def send_events(
    cfg: LinkConfig, events: list[dict[str, Any]], client: httpx.AsyncClient | None = None
) -> tuple[set[str], set[str]]:
    """POST one batch. Returns (accepted ids, rejected ids). A rejected event is never sent again unchanged."""
    owns = client is None
    http = _client(cfg, client)
    try:
        r = await http.post(f"{cfg.url}/events", json={"events": events}, headers=_headers(cfg))
    except httpx.HTTPError as exc:
        raise LinkError(f"{type(exc).__name__}: {exc}"[:200]) from exc
    finally:
        if owns:
            await http.aclose()
    if r.status_code == 401:
        raise LinkError("the CA refused the token", auth=True)
    if r.status_code in (413, 422):
        # The whole batch is invalid (a bug, not a network problem): drop it rather than send it forever.
        return set(), {e["event_id"] for e in events}
    if r.status_code != 200:
        raise LinkError(f"the CA answered {r.status_code}")
    body = r.json()
    accepted = {str(i) for i in body.get("accepted", [])}
    rejected = {str(x.get("event_id")) for x in body.get("rejected", []) if isinstance(x, dict)}
    return accepted, rejected


async def send_pending(cfg: LinkConfig, client: httpx.AsyncClient | None = None) -> int:
    """Send one batch from the outbox. Returns how many events left it. Raises LinkError when the CA cannot be reached."""
    box = Outbox(cfg.cache_dir / "outbox.jsonl")
    batch = box.pending()
    if not batch:
        return 0
    accepted, rejected = await send_events(cfg, batch, client)
    if rejected - accepted:
        log.warning("The CA rejected events", extra={"ctx": {"count": len(rejected - accepted)}})
    box.remove(accepted | rejected)
    return len(accepted | rejected)


async def run_sender(stop: asyncio.Event, cfg: LinkConfig | None = None) -> None:
    """Send queued events until `stop`: every few seconds when there is something, backing off up to a minute on failure."""
    cfg = cfg or link_config()
    if cfg is None:
        return
    failures, warned_auth = 0, False
    while not stop.is_set():
        wait = cfg.send_every_s
        try:
            sent = await send_pending(cfg)
            failures, warned_auth = 0, False
            if sent == BATCH:
                wait = 0.0  # more may be waiting
        except asyncio.CancelledError:
            raise
        except LinkError as exc:
            failures += 1
            wait = min(60.0, 2.0**failures) * random.uniform(0.8, 1.2)  # noqa: S311
            if exc.auth and not warned_auth:
                warned_auth = True
                _alert("[marketing-agent] MA_LINK_AUTH: the Campaign Agent refused the link token")
            if failures in (1, 10) or failures % 100 == 0:
                log.warning(
                    "Campaign Agent unreachable",
                    extra={"ctx": {"in_a_row": failures, "error": str(exc)}},
                )
        except Exception:
            failures += 1
            wait = 60.0
            log.exception("Sending events failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=wait)


# --- fetching and applying the campaign ---------------------------------------------------------------------------------


async def fetch_active(
    cfg: LinkConfig, known_tag: str | None, client: httpx.AsyncClient | None = None
) -> tuple[str, Campaign | None]:
    """Ask the CA what is live. Returns ("changed", campaign), ("unchanged", None) or ("none", None).

    Raises LinkError when the CA cannot be reached or answers with an error.
    """
    headers = _headers(cfg)
    if known_tag:
        headers["If-None-Match"] = f'"{known_tag}"'
    owns = client is None
    http = _client(cfg, client)
    try:
        r = await http.get(f"{cfg.url}/campaign/active", headers=headers)
    except httpx.HTTPError as exc:
        raise LinkError(f"{type(exc).__name__}: {exc}"[:200]) from exc
    finally:
        if owns:
            await http.aclose()
    if r.status_code == 401:
        raise LinkError("the CA refused the token", auth=True)
    if r.status_code == 304:
        return "unchanged", None
    if r.status_code == 204:
        return "none", None
    if r.status_code != 200:
        raise LinkError(f"the CA answered {r.status_code}")
    try:
        data = r.json()
    except ValueError as exc:
        raise LinkError("the CA answered with something that is not JSON") from exc
    tag = r.headers.get("etag", "").strip().removeprefix("W/").strip('"')
    return "changed", Campaign.from_json(data, tag or None)


async def report_status(campaign: Campaign | None, error: str | None = None) -> None:
    """Write what this agent runs (or why it refused a version) to ma_campaign_status, for the CA and the dashboard.

    Best effort: no database, a missing table or a missing grant is logged once and ignored.
    """
    if not env("DATABASE_URL"):
        return
    current: dict[str, Any] | None = None
    if campaign is not None:
        current = {
            "id": campaign.campaign_id,
            "version": campaign.version,
            "sha256": campaign.sha256,
            "applied_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        if error:
            current.pop("applied_at")
            current["error"] = error[:300]
    try:
        await db.execute(
            """insert into ma_campaign_status (id, supported_contract_versions, current_campaign, reported_at)
               values (true, $1::text[], $2::jsonb, now())
               on conflict (id) do update set supported_contract_versions = excluded.supported_contract_versions,
                   current_campaign = excluded.current_campaign, reported_at = now()""",
            list(SUPPORTED_CONFIG_VERSIONS),
            json.dumps(current) if current is not None else None,
        )
    except Exception:
        log.warning("Could not write ma_campaign_status", exc_info=True)


def _settle(  # noqa: PLR0917
    cfg: LinkConfig,
    campaign: Campaign,
    effective: dict[str, Any],
    mode: str,
    note: str | None,
    pass_id: str | None,
) -> None:
    """After a configuration was chosen: remember how, tell the CA once when its complete file does not match this
    machine's base (and once when it matches again), and keep a readable copy of what runs at .cache/campaign/leadgen.yaml."""
    state = _load_state(cfg)
    before = dict(state)
    drifted = mode == "overlay_fallback"
    if drifted and not state.get("base_drift"):
        state["base_drift"] = True
        emit(
            "blocker", "warning", "MA_BASE_DRIFT",
            f"The CA's complete leadgen file does not match this machine's base file ({note}). Its campaign settings are "
            "applied on this machine's base instead. Re-sync the CA's contract/base_config.yaml.",
            campaign_id=campaign.campaign_id, version=campaign.version, dedupe_key="base-drift", pass_id=pass_id, cfg=cfg,
        )  # fmt: skip
        log.warning(
            "The CA's complete file does not match the local base", extra={"ctx": {"note": note}}
        )
    elif not drifted and state.pop("base_drift", None):
        emit(
            "resolved",
            "info",
            "MA_BASE_DRIFT",
            "The CA's complete file matches this machine's base file again",
            dedupe_key="base-drift",
            pass_id=pass_id,
            cfg=cfg,
        )
    state["mode"] = mode
    if state != before:
        _save_state(cfg, state)
    if mode == "complete" and campaign.leadgen_yaml:
        text = campaign.leadgen_yaml
    else:
        header = (
            f"# The configuration this agent runs for campaign {campaign.campaign_id} version {campaign.version}: the campaign on\n"
            "# this machine's own base file"
            + (f" (the CA's complete file was not used: {note})" if note else "")
            + ".\n"
        )
        text = header + yaml.safe_dump(effective, sort_keys=False, allow_unicode=True, width=100)
    target = cfg.cache_dir / "leadgen.yaml"
    try:
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            _write_atomic(target, text)
    except OSError:
        log.debug("could not write the readable copy", exc_info=True)


def describe_pass(cfg: LinkConfig | None = None) -> dict[str, Any] | None:
    """Which campaign a pass that starts now runs, for the log and for the Campaign Agent. None: no link configured."""
    cfg = cfg or link_config()
    if cfg is None:
        return None
    current = load_cache(cfg)
    if current is None:
        return {
            "message": "This pass runs the base config (no campaign is live)",
            "campaign_id": None,
            "version": None,
            "sha256": None,
        }
    mode = _load_state(cfg).get("mode")
    how = {
        "complete": "from the CA's complete leadgen file",
        "overlay_fallback": "on this machine's base file (the CA's complete file did not match it)",
        "overlay": "on this machine's base file",
    }.get(str(mode), "")
    return {
        "message": f"This pass runs campaign {current.campaign_id[:8]} version {current.version} (sha256 {current.sha256[:12]})"
        + (f" {how}" if how else ""),
        "campaign_id": current.campaign_id,
        "version": current.version,
        "sha256": current.sha256[:12],
    }


def live_campaign_id(cfg: LinkConfig | None = None) -> str | None:
    """The campaign whose emails may be sent now: its id, "none" when nothing is live (as far as this agent knows), or
    None when no link is configured (then campaigns are not checked). Read from the last good campaign on disk."""
    cfg = cfg or link_config()
    if cfg is None:
        return None
    current = load_cache(cfg)
    return current.campaign_id if current is not None else NO_CAMPAIGN


def require_campaign() -> bool:
    """MA_REQUIRE_CAMPAIGN (default true): a discovery pass runs only for a live, approved campaign, never on the base
    config alone. false keeps the old behaviour (the base config runs when nothing is live)."""
    return (env("MA_REQUIRE_CAMPAIGN", "true") or "true").strip().lower() != "false"


WATCH = "watch"  # pass_id of a pickup made by the watcher, not by a pass


async def run_watcher(
    stop: asyncio.Event, load_base: Callable[[], dict[str, Any]], every_s: float | None = None
) -> None:
    """Look for a new live campaign on its own, so an approval reaches this agent in seconds and not at the start of the
    next pass. The check sends the known tag and gets 304 when nothing changed. It only fetches, checks and caches
    (and tells the Campaign Agent); it never starts a pass. No link configured: returns at once."""
    cfg = link_config()
    if cfg is None:
        return
    every = every_s if every_s is not None else float(env("MA_LINK_WATCH_EVERY_S", "20") or 20)
    while not stop.is_set():
        try:
            await apply_campaign(load_base(), cfg=cfg, pass_id=WATCH)
        except Exception:  # never end the watcher
            log.exception("Campaign watcher failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


async def apply_campaign(  # noqa: PLR0912, PLR0915 - one flow, in order
    base: dict[str, Any],
    *,
    cfg: LinkConfig | None = None,
    client: httpx.AsyncClient | None = None,
    pass_id: str | None = None,
) -> dict[str, Any]:
    """The raw leadgen config to use for this pass: the base, or the base with the live campaign merged in.

    Never raises: any problem keeps the last good campaign (or the base) and queues an event. No link configured: the base.
    """
    cfg = cfg or link_config()
    if cfg is None:
        return base
    state = _load_state(cfg)
    cached = load_cache(cfg)
    current = cached
    try:
        outcome, fetched = await fetch_active(cfg, cached.tag if cached else None, client)
    except LinkError as exc:
        outcome, fetched = "failed", None
        first_failure = not state.get("pickup_failing")
        if exc.auth:
            # The link itself is what is broken, so an event could not be delivered anyway: tell people in Slack.
            if first_failure:
                _alert("[marketing-agent] MA_LINK_AUTH: the Campaign Agent refused the link token")
        elif first_failure:
            emit(
                "blocker",
                "warning",
                "MA_PICKUP_FAILED",
                f"Could not read the campaign from the Campaign Agent: {exc}"
                + (" (running the last good campaign)" if cached else " (running the base config)"),
                dedupe_key="campaign-pickup",
                pass_id=pass_id,
                cfg=cfg,
            )
        if first_failure:
            state["pickup_failing"] = True
            _save_state(cfg, state)
        (log.warning if first_failure else log.debug)(
            "Campaign pickup failed", extra={"ctx": {"error": str(exc), "cached": bool(cached)}}
        )
    else:
        if state.pop("pickup_failing", None):
            _save_state(cfg, state)
            emit(
                "resolved",
                "info",
                "MA_PICKUP_FAILED",
                "The Campaign Agent is reachable again",
                dedupe_key="campaign-pickup",
                pass_id=pass_id,
                cfg=cfg,
            )

    if outcome == "none":
        if cached is not None:
            clear_cache(cfg)
            log.info("No campaign is live: running the base config")
        current = None
        await report_status(None)
    elif outcome == "changed" and fetched is not None:
        try:
            if not fetched.intact():
                raise OverlayError("the text does not match its sha256: it was changed on the way")  # noqa: TRY301
            merged, mode, note = resolve(base, fetched)
        except OverlayError as exc:
            error = str(exc)
            emit(
                "rejected",
                "error",
                "MA_CAMPAIGN_HASH_MISMATCH" if "sha256" in error else "MA_CAMPAIGN_REJECTED",
                f"Version {fetched.version} was refused: {error}",
                campaign_id=fetched.campaign_id,
                version=fetched.version,
                dedupe_key="campaign-rejected",
                pass_id=pass_id,
                cfg=cfg,
            )
            emit(
                "blocker",
                "error",
                "MA_CAMPAIGN_REJECTED",
                f"Version {fetched.version} was refused: {error}",
                campaign_id=fetched.campaign_id,
                version=fetched.version,
                dedupe_key="campaign-rejected",
                pass_id=pass_id,
                cfg=cfg,
            )
            log.warning(
                "Campaign refused", extra={"ctx": {"version": fetched.version, "error": error}}
            )
            await report_status(fetched, error)
        else:
            save_cache(cfg, fetched)
            current = fetched
            emit(
                "resolved",
                "info",
                "MA_CAMPAIGN_REJECTED",
                "A campaign was applied",
                dedupe_key="campaign-rejected",
                pass_id=pass_id,
                cfg=cfg,
            )
            emit(
                "applied",
                "info",
                "MA_CAMPAIGN_APPLIED",
                (
                    f"Version {fetched.version} was received and checked; the next pass will use it"
                    if pass_id == WATCH
                    else f"Version {fetched.version} is running from this pass"
                )
                + (
                    " (the CA's complete file)"
                    if mode == "complete"
                    else " (on this machine's base file)"
                ),
                campaign_id=fetched.campaign_id,
                version=fetched.version,
                dedupe_key=f"applied-{fetched.campaign_id}-{fetched.version}",
                pass_id=pass_id,
                cfg=cfg,
            )
            log.info(
                "Campaign applied",
                extra={"ctx": {"campaign": fetched.campaign_id, "version": fetched.version}},
            )
            await report_status(fetched)
            _settle(cfg, fetched, merged, mode, note, pass_id)
            return merged
    # unchanged, failed, refused or none: run what was last good (or the base)
    if current is None:
        return base
    try:
        merged, mode, note = resolve(base, current)
    except OverlayError as exc:
        # The base config changed under a cached campaign. Do not guess: run the base and say so.
        emit(
            "blocker",
            "error",
            "MA_CAMPAIGN_REJECTED",
            f"The last good campaign no longer loads: {exc}",
            campaign_id=current.campaign_id,
            version=current.version,
            dedupe_key="campaign-rejected",
            pass_id=pass_id,
            cfg=cfg,
        )
        return base
    if outcome == "unchanged":
        await report_status(current)
    _settle(cfg, current, merged, mode, note, pass_id)
    return merged
