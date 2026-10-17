"""Tasks that ask the model for JSON."""


def test_tasks_that_ask_for_json_switch_the_thinking_off(monkeypatch) -> None:
    """A thinking model spends max_tokens on its reasoning and returns an empty reply: every JSON task must say none."""
    import asyncio
    import contextlib

    from agent.tasks import followup, reply
    from agentkit import task_runner

    seen: dict[str, dict] = {}

    async def fake_run_task(task, untrusted, **kw):
        seen[task] = kw
        raise RuntimeError("stop here")

    monkeypatch.setattr(task_runner, "run_task", fake_run_task)
    for call in (lambda: followup.draft_followup("x"), lambda: reply.classify_and_draft("x")):
        with contextlib.suppress(RuntimeError):
            asyncio.run(call())
    assert seen["email.followup"]["reasoning_effort"] == "none"
    assert seen[reply.TASK]["reasoning_effort"] == "none"
