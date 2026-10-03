"""One entry point for every model call: `complete(task, ...)`.

Picks the model alias from config/routing.yaml, talks to the OpenAI-compatible endpoint
(LLM_BASE_URL, LLM_API_KEY), validates structured output against a pydantic model and retries.
Changing a model is a config change, not a code change.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import env, load_routing
from .log import get_logger

log = get_logger("agentkit.llm")
T = TypeVar("T", bound=BaseModel)

_sem: asyncio.Semaphore | None = None
_MAX_CONCURRENT = (
    3  # LLM_MAX_CONCURRENT overrides; use 1 when one server swaps models in and out of memory
)
# Aliases in models.yaml -> served names on the vLLM host.
SERVED = {"dev": "agent-dev", "primary": "agent-primary", "fast": "agent-fast"}


class LLMError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    parsed: BaseModel | None
    model: str
    tokens_in: int
    tokens_out: int


def model_for(task: str) -> str:
    """Resolve task -> served model name. MODEL_<TASK> (e.g. MODEL_GUI_STEP=qwen3-vl:8b) wins over
    config/routing.yaml, so a model can be swapped without editing files."""
    if override := env("MODEL_" + re.sub(r"\W+", "_", task).upper()):
        return override
    cfg = load_routing()
    alias = (cfg.get("tasks") or {}).get(task) or cfg.get("default", "dev")
    return SERVED.get(alias, alias)


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(
            int(env("LLM_MAX_CONCURRENT", str(_MAX_CONCURRENT)) or _MAX_CONCURRENT)
        )
    return _sem


def _extract_json(text: str) -> dict:
    """Parse JSON from a model reply, tolerating code fences and surrounding prose.

    The whole reply is tried first: a blog body inside the JSON may itself contain ``` fences.
    """
    try:
        whole = json.loads(text.strip())
        if isinstance(whole, dict):
            return whole
    except ValueError:
        pass
    m = re.match(r"\s*```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)  # fence around everything
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


def _server_schema(schema: Any) -> Any:
    """JSON schema for the server's constrained decoding. Length bounds are dropped: some servers
    (Ollama/llama.cpp) fail to build a grammar from them. pydantic still enforces them on the reply."""
    if isinstance(schema, dict):
        return {
            k: _server_schema(v) for k, v in schema.items() if k not in ("minLength", "maxLength")
        }
    if isinstance(schema, list):
        return [_server_schema(v) for v in schema]
    return schema


async def _post(payload: dict, timeout: float) -> dict:
    base = env("LLM_BASE_URL", required=True).rstrip("/")
    headers = {"Authorization": f"Bearer {env('LLM_API_KEY', '')}"}
    last: Exception | None = None
    for attempt in range(3):  # 2 retries with back-off
        try:
            async with _semaphore(), httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{base}/v1/chat/completions", json=payload, headers=headers)
            if r.status_code >= 500:
                raise LLMError(f"llm server {r.status_code}")  # noqa: TRY301 - retried below
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{r.status_code}: {r.text[:300]}", request=r.request, response=r
                )
            return r.json()
        except (httpx.TransportError, LLMError) as exc:
            last = exc
            await asyncio.sleep(2**attempt)
    raise LLMError(f"llm unreachable: {last!r}")


async def complete(
    task: str,
    messages: list[dict],
    schema: type[T] | None = None,
    *,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    timeout: float | None = None,
    reasoning_effort: str | None = None,
) -> Completion:
    """Call the model for `task`. With `schema`, the reply is validated (one retry on failure)."""
    model = model_for(task)
    if timeout is None:
        timeout = 30.0 if schema is not None else 120.0
    payload: dict = {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": _server_schema(schema.model_json_schema()),
            },
        }
    tin = tout = 0
    error: str | None = None
    for attempt in range(2 if schema is not None else 1):
        if error:
            payload["messages"] = [
                *messages,
                {
                    "role": "user",
                    "content": f"Your last reply was invalid ({error}). Reply with valid JSON only.",
                },
            ]
        data = await _post(payload, timeout)
        usage = data.get("usage") or {}
        tin += usage.get("prompt_tokens", 0)
        tout += usage.get("completion_tokens", 0)
        text = data["choices"][0]["message"]["content"] or ""
        if schema is None:
            return Completion(text, None, model, tin, tout)
        try:
            return Completion(text, schema.model_validate(_extract_json(text)), model, tin, tout)
        except (ValueError, ValidationError) as exc:
            error = type(exc).__name__
            log.warning(
                "invalid structured output", extra={"ctx": {"task": task, "attempt": attempt}}
            )
    raise LLMError(f"invalid structured output for {task}: {error}")


async def health() -> bool:
    """True if the LLM endpoint answers /v1/models."""
    base = env("LLM_BASE_URL", required=True).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{base}/v1/models", headers={"Authorization": f"Bearer {env('LLM_API_KEY', '')}"}
            )
        return r.status_code == 200
    except httpx.HTTPError:
        return False
