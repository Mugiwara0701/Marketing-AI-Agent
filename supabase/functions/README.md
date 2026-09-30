# Edge Functions (Deno)

| Function         | Purpose                                                                                                          |
| ---------------- | ---------------------------------------------------------------------------------------------------------------- |
| `slack-interact` | Slack interactivity URL: verify signature, check `team_members`, `decide_approval()` once, `repository_dispatch` |
| `unsubscribe`    | RFC 8058 one-click unsubscribe + confirmation page; writes hashed address to `suppression_list`                  |
| `trigger-run`    | Dashboard "Start run" / Slack Retry; JWT + role checked, allow-listed jobs only                                  |

Secrets (function secrets, never in the repo): `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`, `SLACK_SIGNING_SECRET`, `UNSUBSCRIBE_SECRET`, `GITHUB_REPO` (`owner/name`),
`GITHUB_DISPATCH_TOKEN` (fine-grained, dispatch on this repo only).

Deploy: `supabase functions deploy slack-interact --no-verify-jwt` (Slack and mail clients send no JWT; the
functions verify their own signatures/tokens). `trigger-run` verifies the user JWT itself. Not yet run against
a live project: verify end to end at Gate 1.
