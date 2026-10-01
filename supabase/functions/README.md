# Edge Functions (Deno)

| Function         | Purpose                                                                                                   |
| ---------------- | --------------------------------------------------------------------------------------------------------- |
| `slack-interact` | Slack Request URL for the Approve/Reject buttons: verifies signature, writes the decision onto the email / blog post row (no Slack data is stored) |
| `unsubscribe`    | RFC 8058 one-click unsubscribe + confirmation page; writes hashed address to `suppression_list`           |

Secrets (function secrets, never in the repo): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`SLACK_SIGNING_SECRET`, `SLACK_ALLOWED_USERS` (comma-separated Slack user ids allowed to approve, e.g.
`U012ABC,U034DEF`), `UNSUBSCRIBE_SECRET` (same value as in the agent's `.env`).

```bash
supabase functions deploy slack-interact --no-verify-jwt     # Slack sends no JWT; the function checks its own signature
supabase functions deploy unsubscribe --no-verify-jwt
supabase secrets set SLACK_SIGNING_SECRET=... SLACK_ALLOWED_USERS=U012ABC UNSUBSCRIBE_SECRET=...
```

Not yet run against a live project.

## Slack app setup (api.slack.com/apps)

1. Create an app. **OAuth & Permissions** -> bot scope `chat:write`; install to the workspace; copy the
   **Bot User OAuth Token** (`xoxb-...`) into the agent's `.env` as `SLACK_BOT_TOKEN`.
2. Either run `python -m agent slack-setup` (creates the channels and invites you; first add bot scopes
   `channels:manage`, `channels:read`, `channels:join` and reinstall the app), or invite the bot to the three channels (`/invite @YourBot`): `#outreach-approvals`, `#content`, `#agent-alerts`
   (or change the `SLACK_CHANNEL_*` names).
3. **Basic Information** -> **Signing Secret** -> set as the `SLACK_SIGNING_SECRET` function secret.
4. **Interactivity & Shortcuts** -> on -> Request URL `https://<project-ref>.supabase.co/functions/v1/slack-interact`.
5. Your Slack user id (profile -> More -> Copy member ID) goes in `SLACK_ALLOWED_USERS`.
