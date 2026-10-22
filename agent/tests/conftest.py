import pytest


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch):
    """Unit tests never touch a real database, LLM host or mail server, whatever .env contains."""
    for name in (
        "DATABASE_URL",
        "LLM_BASE_URL",
        "GMAIL_CREDENTIALS_PATH",
        "GMAIL_TOKEN_PATH",
        "TEST_RECIPIENT",
        "REPLY_TO",
        "EMAIL_SENDING_ENABLED",
        "SLACK_BOT_TOKEN",
        "SLACK_ALERTS_WEBHOOK",
        "MA_LINK_URL",
        "MA_LINK_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_site_checks(request, monkeypatch):
    """The browser backends ask web.may_visit (DNS and robots.txt over the network) before opening a page. Unit tests
    use made-up sites and no network, so it allows everything here; test_web_rules.py tests the real rules."""
    if request.module.__name__.endswith("test_web_rules"):
        return
    from agent import web

    async def allow(_url: str) -> None:
        return None

    monkeypatch.setattr(web, "may_visit", allow)
    if request.module.__name__.endswith("test_mail_domain"):
        return
    from agent.leadgen import contacts

    async def deliverable(_address: str) -> bool:
        return True

    monkeypatch.setattr(contacts, "mail_domain_ok", deliverable)


@pytest.fixture(autouse=True)
def _fresh_web_client():
    """web reuses one httpx client; every test (and its event loop) gets a fresh one so mock transports don't leak."""
    from agent import web

    web._client[0] = None
    yield
    web._client[0] = None
