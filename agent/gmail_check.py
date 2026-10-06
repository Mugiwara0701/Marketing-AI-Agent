"""Dev check: log in to Gmail (first run opens a browser, then token.json is reused) and print the account.

python -m agent.gmail_check
"""

import sys

from agentkit import gmail


def main() -> int:
    try:
        profile = gmail.get_service(interactive=True).users().getProfile(userId="me").execute()
    except gmail.GmailAuthError as exc:
        print(f"Gmail auth failed: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    except (
        Exception
    ) as exc:  # network, API disabled, wrong account: show the type only, never tokens
        print(f"Gmail check failed: {type(exc).__name__}", file=sys.stderr)  # noqa: T201
        return 1
    print(f"Connected Gmail account: {profile['emailAddress']}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
