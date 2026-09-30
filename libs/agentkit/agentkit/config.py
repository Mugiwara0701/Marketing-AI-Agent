"""Environment and routing config helpers."""

import os
from functools import lru_cache
from pathlib import Path

import yaml


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@lru_cache(maxsize=1)
def load_routing(path: str | None = None) -> dict:
    """Load config/routing.yaml (task -> model alias). Path via ROUTING_CONFIG."""
    p = Path(path or os.environ.get("ROUTING_CONFIG", "config/routing.yaml"))
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}
