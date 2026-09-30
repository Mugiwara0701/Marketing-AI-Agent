"""Strip PII from text before it reaches logs or alerts."""

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().\-]{7,}\d")


def redact(text: str) -> str:
    """Replace email addresses and phone numbers with placeholders."""
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", text or ""))
