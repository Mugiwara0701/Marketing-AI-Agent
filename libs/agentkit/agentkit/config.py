"""Environment and routing config helpers."""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, overload

import yaml


def load_dotenv(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines from a .env file into os.environ (real environment variables win)."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = re.sub(r"\s+#.*$", "", value.strip())  # inline comment
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_dotenv()


@overload
def env(name: str, default: str | None = None, *, required: Literal[True]) -> str: ...
@overload
def env(name: str, default: str | None = None, required: bool = False) -> str | None: ...
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
