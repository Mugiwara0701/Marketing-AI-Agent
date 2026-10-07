# Pipeline control REST API (dashboard Start / Stop)

The agent runs on the office machine, behind the office router: nothing on the internet can call it. So the dashboard
calls this **REST API** (folder `api/` in this repo, hosted on any server), which records the requested state in the
database. The agent checks the database every 2 seconds over its own outbound connection, applies the request, and
writes back what it is doing plus a heartbeat.

```
dashboard frontend ─▶ dashboard backend ──HTTPS──▶ pipeline API ──▶ Supabase Postgres ◀── office agent
   (its own repo)       (its own repo)              (api/, hosted)    pipeline_control     (polls every 2 s,
                                                                                            reports + heartbeat)
```

Nothing runs on a schedule: the pipeline runs only between Start and Stop.

## Connection

| | |
|---|---|
| Base URL | where the API is hosted, e.g. `https://marketing-agent-pipeline-api.onrender.com` (see `api/README.md`) |
| Auth | header `Authorization: Bearer <PIPELINE_API_TOKEN>` on every `/api/v1/*` route |
| Format | JSON. `start` / `stop` take an optional body `{"requested_by": "<dashboard user>"}` (max 100 chars) |
| Interactive docs | `<base URL>/docs` (Swagger UI); machine-readable spec at `<base URL>/openapi.json` |

Call it from the dashboard **backend**, so the token never reaches a browser. If a browser must call it directly, list
the dashboard's origin in the API's `PIPELINE_API_CORS_ORIGINS`.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/v1/pipeline/start` | Ask the agent to start. Already asked: nothing changes (`"changed": false`). |
| `POST` | `/api/v1/pipeline/stop` | Ask the agent to stop. It cancels the pass in progress (for example a lead search) and closes Chrome on the office machine. |
| `GET` | `/api/v1/pipeline/status` | What the agent is doing. Poll every 5–10 s while the page is open. |
| `GET` | `/health` | `{"status": "ok"}` when the API is up. No token. Says nothing about the office machine. |

Start and Stop are **requests**: they return at once, and the agent applies them within about 2 seconds (stopping
can take up to 30 s while the current step winds down). The response shows the request (`desired_state`) and what the
agent was doing at that moment (`state`); poll `status` until `in_sync` is true.

If the office machine is off, the request is kept and applied when it comes back online.

## Response body

`status` returns the object below; `start` and `stop` return the same plus `changed`.

```json
{
  "desired_state": "running",
  "state": "running",
  "online": true,
  "in_sync": true,
  "requested_by": "dashboard:akshat@example.com",
  "requested_at": "2026-10-07T05:12:38.512000Z",
  "state_since": "2026-10-07T05:12:40Z",
  "current_pass": { "started_at": "2026-10-07T05:12:40Z", "step": "leads" },
  "next_pass_at": null,
  "last_pass": {
    "started_at": "2026-10-07T04:20:02+00:00",
    "finished_at": "2026-10-07T04:58:31+00:00",
    "outcome": "succeeded",
    "result": { "leads": { "searches": 12, "leads_qualified": 4, "emails_drafted": 3, "approvals_requested": 3 } }
  },
  "sending_enabled": true,
  "test_mode": true,
  "agent_host": "office-machine",
  "heartbeat_at": "2026-10-07T05:20:11.204000Z",
  "changed": true
}
```

| Field | Meaning |
|---|---|
| `desired_state` | What the dashboard last asked for: `"running"` or `"stopped"`. |
| `state` | What the agent is doing: `"running"`, `"stopped"`, or `"offline"` when its last heartbeat is older than 60 s. |
| `online` | `false`: the office machine is off, asleep, has no network, or the agent service is stopped. |
| `in_sync` | `true` once the agent has applied `desired_state`. |
| `requested_by`, `requested_at` | Who pressed the button last, and when. |
| `state_since` | When the agent last started or stopped (`null` when offline). |
| `current_pass` | The pass in progress, or `null`. `step` is `leads`, `followups` or `blog`, in that order. |
| `next_pass_at` | While running, between two passes: when the next one starts. Otherwise `null`. |
| `last_pass` | The last finished pass, or `null`. `outcome`: `succeeded`, `failed` or `stopped`. It has `result` (numbers per step; a step that failed has an `error` key) or `error`. |
| `sending_enabled` | `false` means approved emails are not sent at all. |
| `test_mode` | `true` means every email goes to the test inbox, not to the real contact. |
| `agent_host`, `heartbeat_at` | Which machine reported, and when. |
| `changed` | (`start` / `stop` only) `false` when that state had already been requested. |

All times are UTC, ISO 8601.

### Suggested UI

| Condition | Show |
|---|---|
| `!online` | "Agent offline" (and, if `desired_state` is `running`: "will start when it comes back") |
| `online && !in_sync` | "Starting…" / "Stopping…", buttons disabled |
| `in_sync && state == "running"` | Stop button; `current_pass.step`, or "next pass at `next_pass_at`" |
| `in_sync && state == "stopped"` | Start button |

## Errors

Errors have the body `{"detail": "<message>"}`.

| Status | When |
|---|---|
| `401` | Missing or wrong token. |
| `405` | Wrong method (`GET` for `status`, `POST` for `start` and `stop`). |
| `422` | Invalid body (for example `requested_by` longer than 100 characters). |
| `500` | The database is not reachable, or the agent's migrations were not applied. |
| `503` | `PIPELINE_API_TOKEN` is not set on the API. |

## What "running" means

- Passes run one after another, 30 minutes apart. A pass is: lead discovery (Chrome opens on the office desktop within
  seconds of Start) → follow-ups → blog (at most one blog post a day).
- Alongside the passes: emails approved in Slack go out within about 20 seconds, and Gmail is polled for replies and
  bounces every 10 minutes (the first time right at Start).

While stopped, nothing is searched, drafted, sent or polled. Approve/Reject in Slack still records the decision (Slack
talks to Supabase, not to the office machine), and those emails go out after the next start.

## Examples

```bash
BASE=https://marketing-agent-pipeline-api.onrender.com
TOKEN=...   # PIPELINE_API_TOKEN
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"requested_by": "akshat"}' $BASE/api/v1/pipeline/start
curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/v1/pipeline/stop
curl -H "Authorization: Bearer $TOKEN" $BASE/api/v1/pipeline/status
```

```js
// Dashboard backend (Node 18+)
const BASE = `${process.env.AGENT_API_URL}/api/v1/pipeline`;
const auth = { Authorization: `Bearer ${process.env.AGENT_API_TOKEN}` };
const post = (path, user) =>
  fetch(`${BASE}/${path}`, {
    method: "POST",
    headers: { ...auth, "Content-Type": "application/json" },
    body: JSON.stringify({ requested_by: user }),
  }).then((r) => r.json());

export const pipelineStatus = () => fetch(`${BASE}/status`, { headers: auth }).then((r) => r.json());
export const startPipeline = (user) => post("start", user);
export const stopPipeline = (user) => post("stop", user);
```

Without the dashboard (any machine with the agent's `.env`): `python -m agent pipeline start|stop|status`, or from the
laptop `bash deploy/remote.sh pipeline start|stop|status`.
