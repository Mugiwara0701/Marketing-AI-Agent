"""Run a labelled eval set against the configured model and report accuracy.

Set file (JSONL), one case per line: {"input": "...", "expected": {"field": value, ...}}
Usage: python eval/runner/run_eval.py <task> <set.jsonl> <prompt_dir> <module:SchemaClass> [--min 0.85]
Exits non-zero if accuracy is below --min, so CI can block prompt or model regressions.
"""

import argparse
import asyncio
import importlib
import json
import sys
from pathlib import Path

from agentkit import llm, prompts


def _load_schema(spec: str):
    mod, name = spec.split(":")
    return getattr(importlib.import_module(mod), name)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("set_file")
    ap.add_argument("prompt_dir")
    ap.add_argument("schema")
    ap.add_argument("--min", type=float, default=0.0)
    a = ap.parse_args()

    schema = _load_schema(a.schema)
    template, _ = await prompts.load(a.task, a.prompt_dir)
    cases = [json.loads(x) for x in Path(a.set_file).read_text().splitlines() if x.strip()]
    correct = 0
    for i, case in enumerate(cases):
        msgs = prompts.build_messages(template, untrusted=case["input"])
        try:
            got = (await llm.complete(a.task, msgs, schema)).parsed.model_dump()
        except llm.LLMError:
            got = {}
        ok = all(got.get(k) == v for k, v in case["expected"].items())
        correct += ok
        if not ok:
            print(f"case {i}: expected {case['expected']} got {got}")
    acc = correct / len(cases) if cases else 0.0
    print(json.dumps({"task": a.task, "model": llm.model_for(a.task), "cases": len(cases), "accuracy": acc}))
    return 0 if acc >= a.min else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
