# scheduler

Component 5: GitHub Actions and cron. Workflows in `.github/workflows/` call the services'
`/jobs/*` endpoints over Tailscale via the reusable `_run-job.yml`. `jobs.yaml` documents the
intended schedule.

- The host and services run only in two windows (night 01:30-04:30 IST daily, day 09:00-17:00 IST weekdays). Each workflow waits up to 15 minutes for the host, then records a skipped run and exits; dispatch-triggered jobs are also swept in the next window.
- Scheduled workflows only run when repository variable `SCHEDULES_ENABLED` is `true`.
- GitHub schedules can be delayed or dropped, run only from the default branch, and are disabled
  after 60 days of repo inactivity (public repos). Jobs must be idempotent and safe to re-run.
- Secrets: `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET`, `JOB_TOKEN`, `LEAD_URL`, `OUTREACH_URL`,
  `CONTENT_URL`, `LLM_URL`, `LLM_API_KEY`.
- Pin third-party actions to commit SHAs before production.
- Slack approvals reach the services through Supabase Edge Functions that fire `repository_dispatch`
  (edge functions cannot reach the tailnet directly).
