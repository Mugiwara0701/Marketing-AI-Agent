"""Gmail API client: OAuth login, token reuse and refresh. Sending and reading build on get_service()."""

import base64
import contextlib
import os
import re
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import env

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailAuthError(RuntimeError):
    """Gmail cannot be used until a person fixes the credentials. Messages never contain secrets."""


def _credentials_path() -> Path:
    return Path(env("GMAIL_CREDENTIALS_PATH", "credentials.json") or "credentials.json")


def _token_path() -> Path:
    return Path(env("GMAIL_TOKEN_PATH", "token.json") or "token.json")


def _save_token(path: Path, creds: Credentials) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only: holds the refresh token
    os.chmod(path, 0o600)  # the mode above only applies to a newly created file
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def _login(credentials_path: Path, token_path: Path) -> Credentials:
    if not credentials_path.is_file():
        raise GmailAuthError(
            f"Gmail credentials file not found: {credentials_path}. Download the OAuth client JSON "
            "from Google Cloud Console and set GMAIL_CREDENTIALS_PATH."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)  # opens a browser on this machine
    _save_token(token_path, creds)
    return creds


def load_credentials(interactive: bool = False) -> Credentials:
    """Return valid credentials: reuse token.json, refresh it when expired, else log in (interactive only).

    Unattended runs (the daily agent) pass interactive=False and get a GmailAuthError instead of a
    browser prompt that nobody could answer.
    """
    credentials_path, token_path = _credentials_path(), _token_path()
    creds: Credentials | None = None
    if token_path.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, KeyError) as exc:
            raise GmailAuthError(
                f"{token_path} is not a valid Gmail token file; delete it and log in again."
            ) from exc
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Revoked, or expired: refresh tokens last 7 days while the OAuth app is in "Testing".
            if not interactive:
                raise GmailAuthError(
                    f"Gmail token refresh failed. Delete {token_path} and run the Gmail check command "
                    "to log in again."
                ) from None
            creds = None
        else:
            _save_token(token_path, creds)
            return creds
    if not interactive:
        raise GmailAuthError(
            f"No valid Gmail token at {token_path}. Run the Gmail check command once to log in."
        )
    return _login(credentials_path, token_path)


def get_service(interactive: bool = False):
    """Authenticated Gmail API client (users().messages(), users().threads(), ...)."""
    return build("gmail", "v1", credentials=load_credentials(interactive), cache_discovery=False)


# ---------- sending ----------

_RETRIES = 3  # googleapiclient retries 429 / 5xx with exponential backoff


def encode_message(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send_message(service, msg: EmailMessage, thread_id: str | None = None) -> dict:
    """Send `msg` as the authenticated account. Pass `thread_id` for a follow-up or reply so Gmail keeps
    it in the same conversation (the In-Reply-To / References headers must be set on `msg` as well).

    Returns three different identifiers, never to be mixed up:
      message_id        the RFC 5322 Message-ID header Gmail actually stamped on the mail
      gmail_message_id  Gmail's API id of the sent message
      gmail_thread_id   Gmail's conversation id
    """
    body: dict = {"raw": encode_message(msg)}
    if thread_id:
        body["threadId"] = thread_id
    sent = service.users().messages().send(userId="me", body=body).execute(num_retries=_RETRIES)
    gmail_id = sent.get("id")
    if not gmail_id:
        raise RuntimeError("gmail send returned no message id")
    # Gmail may rewrite the Message-ID we set, and replies quote the one it used: read it back.
    message_id = msg["Message-ID"]
    with contextlib.suppress(Exception):  # best effort: the mail is already sent
        meta = (
            service.users().messages()
            .get(userId="me", id=gmail_id, format="metadata", metadataHeaders=["Message-ID"])
            .execute(num_retries=_RETRIES)
        )  # fmt: skip
        message_id = _header(meta.get("payload", {}), "message-id") or message_id
    return {
        "message_id": message_id,
        "gmail_message_id": gmail_id,
        "gmail_thread_id": sent.get("threadId") or thread_id,
    }


def profile_address(service) -> str:
    return service.users().getProfile(userId="me").execute(num_retries=_RETRIES)["emailAddress"]


# ---------- reading ----------


def list_recent_messages(
    service, query: str = "in:inbox newer_than:14d", max_results: int = 50
) -> list[dict]:
    """[{id, threadId}] newest first. Only ids: fetch the ones you have not seen with get_message()."""
    out: list[dict] = []
    page = None
    while len(out) < max_results:
        r = (
            service.users().messages()
            .list(userId="me", q=query, maxResults=min(100, max_results - len(out)), pageToken=page)
            .execute(num_retries=_RETRIES)
        )  # fmt: skip
        out += r.get("messages", [])
        page = r.get("nextPageToken")
        if not page:
            break
    return out[:max_results]


def get_message(service, message_id: str) -> dict:
    raw = service.users().messages().get(userId="me", id=message_id, format="full").execute(
        num_retries=_RETRIES
    )
    return parse_message(raw)


def get_thread(service, thread_id: str) -> list[dict]:
    raw = service.users().threads().get(userId="me", id=thread_id, format="full").execute(
        num_retries=_RETRIES
    )
    return [parse_message(m) for m in raw.get("messages", [])]


def _header(payload: dict, name: str) -> str | None:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name:
            return h.get("value")
    return None


class _Text(HTMLParser):
    _SKIP: ClassVar[set[str]] = {"script", "style", "head"}
    _BREAK: ClassVar[set[str]] = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Readable text from an HTML-only mail. Tags are dropped, never executed or fetched."""
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(p.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _decode(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    m = re.search(r"charset=\"?([\w-]+)", _header(part, "content-type") or "", re.IGNORECASE)
    try:
        return raw.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _bodies(part: dict, out: dict) -> None:
    if part.get("filename"):  # attachment
        return
    mime = part.get("mimeType", "")
    if mime in ("text/plain", "text/html"):
        key = "text" if mime == "text/plain" else "html"
        out[key] = (out.get(key) or "") + _decode(part)
    for sub in part.get("parts", []):
        _bodies(sub, out)


def parse_message(raw: dict) -> dict:
    """Flatten a Gmail `format=full` message. The body prefers text/plain and falls back to HTML as text."""
    payload = raw.get("payload", {})
    found: dict = {}
    _bodies(payload, found)
    html = found.get("html", "")
    text = found.get("text", "").strip() or (html_to_text(html) if html else "")
    return {
        "gmail_message_id": raw.get("id"),
        "gmail_thread_id": raw.get("threadId"),
        "labels": raw.get("labelIds", []),
        "message_id": _header(payload, "message-id"),
        "in_reply_to": _header(payload, "in-reply-to"),
        "references": _header(payload, "references"),
        "from": _header(payload, "from") or "",
        "to": _header(payload, "to") or "",
        "subject": _header(payload, "subject") or "",
        "date": _header(payload, "date"),
        "text": text,
        "html": html,
    }


def bare_address(value: str) -> str:
    """'Ann <Ann@X.io>' -> 'ann@x.io'."""
    return parseaddr(value)[1].strip().lower()
