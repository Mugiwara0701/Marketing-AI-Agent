"""Smoke-test the LLM host: health, a plain reply, a structured reply, and embeddings.

Usage: LLM_BASE_URL=... LLM_API_KEY=... python eval/runner/smoke.py [--embed]
Exits non-zero on the first failing step.
"""

import argparse
import asyncio
import sys
import time

from pydantic import BaseModel

from agentkit import embed, llm


class Ping(BaseModel):
    ok: bool
    word: str


async def step(name: str, coro) -> bool:
    t0 = time.perf_counter()
    try:
        out = await coro
    except Exception as exc:
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        return False
    print(f"ok   {name} ({time.perf_counter() - t0:.1f}s) {out}")
    return True


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed", action="store_true", help="also test the embedding server")
    a = ap.parse_args()

    async def health():
        if not await llm.health():
            raise RuntimeError("/v1/models did not answer 200")
        return "reachable"

    async def plain():
        r = await llm.complete("lead.assess", [{"role": "user", "content": "Say hello."}])
        return f"model={r.model} tokens={r.tokens_in}/{r.tokens_out}"

    async def structured():
        msgs = [{"role": "user", "content": 'Reply with JSON {"ok": true, "word": "ping"}.'}]
        r = await llm.complete("lead.assess", msgs, Ping)
        return r.parsed

    async def embedding():
        v = await embed.embed_one("android platform engineer")
        return f"dims={len(v)}"

    steps = [("health", health()), ("plain", plain()), ("structured", structured())]
    if a.embed:
        steps.append(("embed", embedding()))
    for name, coro in steps:
        res = await step(name, coro)
        if not res:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
