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
    ):
        monkeypatch.delenv(name, raising=False)
