import asyncio

from agentkit import llm
from app import qualify


def _fake(conf: float):
    async def run_task(task, untrusted, *, schema, validate, **_):
        parsed = qualify.QualifyResult(
            relevant=True, service_fit=["AOSP"], confidence=conf, reason="AOSP hiring"
        )
        c = llm.Completion("{}", parsed, "agent-dev", 1, 1)
        return c, validate(c)

    return run_task


def test_high_confidence_passes(monkeypatch):
    monkeypatch.setattr(qualify.task_runner, "run_task", _fake(0.9))
    result, problems = asyncio.run(qualify.qualify_signal("AOSP engineer wanted"))
    assert result.relevant and not problems


def test_low_confidence_flagged(monkeypatch):
    monkeypatch.setattr(qualify.task_runner, "run_task", _fake(0.3))
    _, problems = asyncio.run(qualify.qualify_signal("vague"))
    assert problems
