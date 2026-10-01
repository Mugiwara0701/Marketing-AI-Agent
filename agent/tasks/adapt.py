"""Rewrite the long-form post for one platform (dev.to, LinkedIn, X ...) with the LLM, then check the format.

Each platform has its own structure and length (config/platforms.yaml). The rewrite may drop detail but must
not add technical facts: identifiers not present in the original post are flagged. Failed checks trigger ONE
automatic repair attempt that lists the exact problems.
"""

import re

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

TASK = "content.adapt"
PROMPT_DIR = "agent/prompts"
_MARKDOWN = ("## ", "**", "```", "`", "\n- ", "\n* ")
_HASHTAG = re.compile(r"(?<!\w)#\w+")


class Variant(BaseModel):
    # Required (no defaults) so schema-constrained decoding makes the model fill them in.
    title: str = Field(max_length=140)
    description: str = Field(max_length=300)
    body: str = Field(min_length=100, max_length=12000)
    tags: list[str] = Field(max_length=8)


def clean_tags(tags: list[str], limit: int) -> list[str]:
    """Lowercase alphanumeric tags (dev.to rejects hyphens/spaces; hashtags have neither)."""
    out: list[str] = []
    for t in tags:
        t = re.sub(r"[^a-z0-9]", "", t.lower())  # noqa: PLW2901
        if t and t not in out:
            out.append(t)
    return out[:limit]


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _linkedin_layout(body: str, tags: list[str]) -> str:
    """Mechanical layout fixes small models get wrong: a stray plain-text tag line before the hashtags,
    and one big paragraph (the hook must be a line of its own, followed by short paragraphs)."""
    words = set(tags)

    def stray(line: str) -> bool:  # a line of only tag words: no punctuation, no hashtags
        t = line.strip().lower()
        return (
            bool(t)
            and "#" not in t
            and not re.search(r"[.!?:,]", t)
            and set(t.split()) <= words | {"and"}
        )

    lines = [ln for i, ln in enumerate(body.split("\n")) if i == 0 or not stray(ln)]
    # also a short keyword-style line (no sentence ending) directly above the hashtag line
    nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
    if (
        len(nonblank) >= 3
        and _HASHTAG.search(lines[nonblank[-1]])
        and "#" not in lines[nonblank[-1]].strip().split()[0][1:]
    ):
        cand = lines[nonblank[-2]].strip()
        if cand and not re.search(r"[.!?]$", cand) and len(cand.split()) <= 12 and "#" not in cand:
            del lines[nonblank[-2]]
    text = "\n".join(lines).strip()
    paras = text.split("\n\n")
    first = [x for x in _SENTENCE_END.split(paras[0]) if x]
    if (
        len(first) > 2 or len(paras[0]) > 200
    ):  # one big opening block: hook alone, then short paragraphs
        paras[0:1] = [first[0], *(" ".join(first[i : i + 2]) for i in range(1, len(first), 2))]
        text = "\n\n".join(paras)
    return text


def _x_thread_layout(body: str) -> str:
    """Posts are separated by blank lines; numbering is mechanical, so it is applied here ("1/", "2/" ...)."""
    posts = [re.sub(r"^\s*\d+\s*[/.)]\s*", "", p).strip() for p in body.split("\n\n") if p.strip()]
    return "\n\n".join(f"{i}/ {p}" for i, p in enumerate(posts, 1))


def render(spec: dict, v: Variant) -> str:
    """Final text for the platform from the model's fields."""
    tags = clean_tags(v.tags, spec.get("tags_max", 4))
    kind = spec.get("render")
    body = v.body.strip()
    if kind == "devto_frontmatter":
        head = f"---\ntitle: {v.title}\npublished: false\ndescription: {v.description}\ntags: {', '.join(tags)}\n---\n\n"
        return head + body
    if kind == "linkedin_post":
        body = _linkedin_layout(body, tags)
    elif kind == "x_thread":
        body = _x_thread_layout(body)
    if kind in ("linkedin_post", "x_thread") and tags and not _HASHTAG.search(body):
        return body + ("\n\n" if kind == "linkedin_post" else " ") + " ".join(f"#{t}" for t in tags)
    return body


def _check_linkedin(text: str) -> list[str]:
    problems = [
        f"markdown in a plain-text post: {m.strip()!r}" for m in _MARKDOWN if m in "\n" + text
    ]
    n = len(_HASHTAG.findall(text))
    if not 3 <= n <= 5:
        problems.append(f"expected 3-5 hashtags, found {n}")
    if len(text.split("\n", 1)[0]) > 200:
        problems.append("first line (hook) is longer than 200 characters")
    return problems


def _check_x_thread(text: str) -> list[str]:
    posts = [p.strip() for p in text.split("\n\n") if p.strip()]
    problems = []
    if not 5 <= len(posts) <= 7:
        problems.append(f"expected 5-7 posts separated by blank lines, found {len(posts)}")
    for i, p in enumerate(posts, 1):
        if len(p) > 270:
            problems.append(f"post {i} is {len(p)} characters (max 270)")
    problems += [
        f"markdown in a thread: {m.strip()!r}" for m in ("```", "`", "**", "## ") if m in text
    ]
    return problems[:8]


def _check_devto(v: Variant) -> list[str]:
    problems = []
    if len(v.description) > 150:
        problems.append("dev.to description longer than 150 characters")
    if not clean_tags(v.tags, 4):
        problems.append("no valid dev.to tags")
    body = v.body.lstrip()
    if body.startswith("---"):
        problems.append("body contains its own front matter")
    if v.body.count("```") % 2:
        problems.append("unclosed code fence")
    if "tl;dr" not in body[:200].lower():
        problems.append("missing the TL;DR line at the top")
    if "## key points" not in v.body.lower():
        problems.append("missing the '## Key points' section")
    return problems


def check_format(spec: dict, v: Variant, source_text: str = "") -> list[str]:
    """Format, leak and invented-detail problems; empty = looks right. Anything flagged goes to review."""
    text = render(spec, v)
    problems = checks.run_checks(
        checks.check_banned(text),
        checks.check_length(text, 100, spec.get("max_chars", 12000)),
        checks.check_no_leaks(text),
        checks.check_grounded(text, source_text) if source_text else [],
    )
    kind = spec.get("render")
    if kind == "linkedin_post":
        problems += _check_linkedin(text)
    elif kind == "x_thread":
        problems += _check_x_thread(text)
    elif kind == "devto_frontmatter":
        problems += _check_devto(v)
    return problems


async def _once(
    spec: dict, title: str, body_markdown: str, source_text: str, feedback: str | None
) -> tuple[Variant, list[str]]:
    def _validate(result: llm.Completion) -> list[str]:
        p = result.parsed
        return check_format(spec, p, source_text) if isinstance(p, Variant) else []

    completion, problems = await task_runner.run_task(
        TASK,
        f"Title: {title}\n\n{body_markdown}",
        schema=Variant,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        validate=_validate,
        template_vars={"PLATFORM": spec["label"], "RULES": spec["rules"]},
        feedback=feedback,
        temperature=0.4,
        max_tokens=2200,
        timeout=300.0,
    )
    parsed = completion.parsed
    if not isinstance(parsed, Variant):
        raise TypeError("adapt returned no structured result")
    return parsed, problems


async def adapt(
    spec: dict, title: str, body_markdown: str, extra_source: str = ""
) -> tuple[Variant, str, list[str]]:
    """Returns (variant, rendered text, problems). Identifiers must appear in the original post or in
    `extra_source` (the articles it was written from)."""
    source_text = f"{title} {body_markdown} {extra_source}"
    variant, problems = await _once(spec, title, body_markdown, source_text, None)
    if problems:
        fix = (
            "Your version has these problems: "
            + "; ".join(problems[:8])
            + ". Rewrite it and fix every one, "
            "following the platform rules exactly. Do not add technical details that are not in the original."
        )
        retry, retry_problems = await _once(spec, title, body_markdown, source_text, fix)
        if len(retry_problems) <= len(problems):
            variant, problems = retry, retry_problems
    return variant, render(spec, variant), problems
