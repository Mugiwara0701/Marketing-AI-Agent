# Gmail migration: what has been done

Gmail is now the only mail provider. Resend code, its webhook and its env vars are removed. Old migrations and
the Resend-era columns (`provider_id`, `delivered_at`, `opened_at`, `open_count`) are untouched, so old rows keep
their data.

## Flow

```
approved email (Supabase) -> agent/mailer.py -> agentkit/gmail.py -> Gmail API
                                  stores message_id, gmail_message_id, gmail_thread_id
Gmail inbox -> agent/inbox.py (poll) -> replies row (status received) -> agent/replies.py
                                  classify -> Slack approval -> approved emails row -> mailer -> Gmail (same thread)
```

Daily run order: `inbox -> replies -> send -> followups -> leads -> blog`. Run one step: `python -m agent inbox`.

## Three different ids (never mix them)

| Column | Meaning |
|---|---|
| `message_id` | RFC 5322 `Message-ID` header (read back from Gmail after sending, because Gmail may rewrite it). Used for `In-Reply-To` / `References` and for matching replies |
| `gmail_message_id` | Gmail API id of one message (unique on `emails` and on `replies`) |
| `gmail_thread_id` | Gmail conversation id; sent as `threadId` for follow-ups and replies |

## Files

| File | Change |
|---|---|
| `libs/agentkit/agentkit/gmail.py` | New: auth (`get_service`), `send_message`, `list_recent_messages`, `get_message`, `get_thread`, `parse_message`, HTML-to-text |
| `agent/gmail_check.py` | New: `python -m agent.gmail_check` prints `Connected Gmail account: ...` |
| `agent/inbox.py` | New: polls the inbox, stores replies, detects bounces, dedupes through `email_events` |
| `agent/mailer.py` | Provider layer replaced (`deliver` uses Gmail); all business rules unchanged |
| `agent/store.py` | Follow-up query no longer requires `opened_at is null` |
| `agent/run.py`, `agent/__main__.py` | New `inbox` step and CLI command |
| `supabase/migrations/0007_gmail.sql` | New: Gmail id columns, unique indexes |
| `libs/agentkit/agentkit/resend.py`, `supabase/functions/resend-webhook/` | Deleted |
| `agent/tests/test_gmail.py`, `agent/tests/test_inbox.py` | New tests (Gmail is mocked, nothing is sent) |
| `.env.example`, `.gitignore`, `requirements.txt`, `pyproject.toml`, README, deploy docs | Updated |

## Behaviour notes

- **Sending rules unchanged:** approval, suppression, dev allow-list, `TEST_RECIPIENT`, `EMAIL_SENDING_ENABLED`, daily cap, 3 attempts, `Reply-To`, `Date`, `Message-ID`, unsubscribe headers.
- **Retries:** Gmail 429/5xx are retried inside the API call (3 times with backoff); other failures use the existing attempts counter. An auth failure puts the emails back as `approved` without using an attempt and stops sending.
- **No double-send guard:** Resend had idempotency keys, Gmail has none. If Gmail accepts a mail but the database update then fails, the retry would send it again.
- **Replies** are matched by `In-Reply-To`/`References`, then Gmail thread, then sender address. Mail from your own address is ignored, so replying to yourself cannot be tested.
- **Bounces:** `mailer-daemon@`/`postmaster@` notices (not "delayed" ones) are matched to the sent email by Message-ID or thread, then the email is marked `bounced` and the contact suppressed. An unmatched notice is ignored.
- **Follow-ups:** sent, no reply, not bounced, not suppressed, delay reached. No fake open data.
- **Polling window:** inbox mail from the last 14 days, 50 messages per poll.

## Using it

```bash
pip install -r requirements.txt
python -m agent migrate            # applies 0007_gmail.sql (use whatever command you applied migrations with before)
python -m agent.gmail_check        # first run opens a browser; creates token.json
```

On the headless host: log in on a machine with a browser, copy `credentials.json` and `token.json` to the project folder, run `gmail_check` again (see `deploy/ubuntu-host-setup.md`). Refresh tokens expire after 7 days while the OAuth app is in Testing.

## Not done yet

- Gmail push (`users.watch` + Pub/Sub + `history.list`), as agreed; the watch would need renewal about weekly.
- Bounce fallback by failed-recipient address (only Message-ID/thread matching exists).
- Nothing has been run against real Gmail or a real database: the automated tests mock both.

## Housekeeping

- Local commit `a5b23c0` contains a real `RESEND_WEBHOOK_SECRET` in `.env.example`. Rotate that secret (and remove the Resend webhook) or rewrite the commit before pushing.
- Delete the old `resend-webhook` function and its secrets from the Supabase project if it was deployed.
