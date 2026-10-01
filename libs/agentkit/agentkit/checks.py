"""Output validation for model text. Each check returns a list of problems (empty = ok)."""

import re

BANNED_PHRASES = [
    "guaranteed",
    "100% free",
    "act now",
    "limited time",
    "no obligation",
    "risk-free",
    "click here",
]


def check_length(text: str, min_chars: int = 0, max_chars: int = 5000) -> list[str]:
    n = len(text or "")
    out = []
    if n < min_chars:
        out.append(f"too short ({n} < {min_chars})")
    if n > max_chars:
        out.append(f"too long ({n} > {max_chars})")
    return out


def check_banned(text: str, banned: list[str] | None = None) -> list[str]:
    low = (text or "").lower()
    return [f"banned phrase: {p}" for p in (banned or BANNED_PHRASES) if p in low]


def check_footer(text: str, required: list[str]) -> list[str]:
    """Required footer parts (company name, postal address, unsubscribe text) must be present."""
    return [f"missing footer part: {r}" for r in required if r.lower() not in (text or "").lower()]


def check_label(label: str, allowed: set[str]) -> list[str]:
    return [] if label in allowed else [f"label {label!r} not in allowed set"]


def check_known_names(text: str, allowed: set[str]) -> list[str]:
    """Flag capitalised company-like names in a draft that are not in `allowed` (no invented clients)."""
    found = set(
        re.findall(r"\b[A-Z][A-Za-z0-9]+(?:\s+(?:Inc|Ltd|LLC|GmbH|Corp|Pvt)\.?)\b", text or "")
    )
    return [f"unknown company name: {n}" for n in sorted(found) if n not in allowed]


def check_confidence(confidence: float, threshold: float = 0.6) -> list[str]:
    return [] if confidence >= threshold else [f"confidence {confidence:.2f} < {threshold}"]


_TECH_ID = re.compile(
    r"CONFIG_[A-Z0-9_]+"  # kernel config symbols
    r"|(?<![\w.])/(?:sys|proc|dev|etc|var|usr|data|vendor|system|boot|mnt)\b[\w./-]*"  # device/system paths
    r"|(?<!\w)--[a-z][a-z0-9-]+"  # long command-line flags
    r"|\b(?:Android|Linux|kernel|AOSP)\s+v?\d+(?:\.\d+)*",  # versions
    re.IGNORECASE,
)


def check_grounded(text: str, source: str, limit: int = 8) -> list[str]:
    """Technical identifiers in `text` (CONFIG_ symbols, system paths, --flags, versions) that never appear in
    `source`. A small model writes plausible-looking names from memory; anything it cannot point to in the
    source material is treated as invented."""
    low = re.sub(r"\s+", " ", (source or "").lower())
    missing: list[str] = []
    for m in _TECH_ID.finditer(text or ""):
        ident = re.sub(r"\s+", " ", m.group(0).lower().rstrip(".,;:)"))
        if ident and ident not in low and ident not in [x.lower() for x in missing]:
            missing.append(m.group(0))
    return [f"not found in the source material: {x}" for x in missing[:limit]]


_LEAKS = ("reference material", "untrusted_data", "<untrusted", "as an ai", "source material")


def check_no_leaks(text: str) -> list[str]:
    """Prompt scaffolding must never appear in published text."""
    low = (text or "").lower()
    return [f"prompt text leaked into output: {p!r}" for p in _LEAKS if p in low]


def run_checks(*results: list[str]) -> list[str]:
    return [p for r in results for p in r]
