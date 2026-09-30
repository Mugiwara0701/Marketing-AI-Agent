"""Prompt loading and assembly.

Active prompt = row in prompt_versions (active=true); falls back to a file `<task>.txt` in the
service's prompts directory. Untrusted text (scraped pages, emails, replies) is always passed as
data inside a delimited block; the model has no tools.
"""

from pathlib import Path

from . import db

_OPEN, _CLOSE = "<untrusted_data>", "</untrusted_data>"


def wrap_untrusted(text: str) -> str:
    """Delimit untrusted input so it is treated as data, and neutralise fake delimiters."""
    clean = (text or "").replace(_OPEN, "").replace(_CLOSE, "")
    return f"{_OPEN}\n{clean}\n{_CLOSE}"


async def load(task: str, fallback_dir: str | Path | None = None) -> tuple[str, int | None]:
    """Return (template, version). Version is None when the file fallback is used."""
    try:
        row = await db.fetchrow(
            "select template, version from prompt_versions where task=$1 and active", task
        )
    except Exception:  # noqa: BLE001 - DB down or not configured: use the file
        row = None
    if row:
        return row["template"], row["version"]
    if fallback_dir:
        p = Path(fallback_dir) / f"{task.replace('.', '_')}.txt"
        if p.exists():
            return p.read_text(), None
    raise FileNotFoundError(f"no prompt for task {task}")


def build_messages(template: str, *, untrusted: str = "", examples: list[dict] | None = None,
                   context: list[dict] | None = None) -> list[dict]:
    """system = template; few-shot examples as user/assistant turns; untrusted data last."""
    import json

    msgs: list[dict] = [{"role": "system", "content": template}]
    for ex in examples or []:
        msgs.append({"role": "user", "content": wrap_untrusted(ex["input_text"])})
        msgs.append({"role": "assistant", "content": json.dumps(ex["output_json"])})
    body = ""
    if context:
        body += "Reference material:\n" + "\n---\n".join(c["content"] for c in context) + "\n\n"
    msgs.append({"role": "user", "content": body + wrap_untrusted(untrusted)})
    return msgs
