# Implementation Status

Date: 2026-09-30 · Author: Akshat

A summary of what has been built so far. Job functions in the three services are still stubs; the LLM layer is
implemented and ready to be tested on the office GPU machine.

## 1. Repository and CI

- Structure: `services/` (lead, outreach, content, llm), `libs/agentkit`, `scheduler/`, `config/`, `supabase/`, `eval/`, `docs/`.
- Workflows: `ci.yml` (tests, docker builds), `lint.yml` (ruff, mypy, sqlfluff, yamllint, shellcheck, actionlint, hadolint, deno, markdownlint), `security.yml` (gitleaks, pip-audit), scheduler workflows and `health.yml`.
- Fixed CI failures: actionlint SC2015 in `health.yml` (now an explicit `if`), and markdownlint errors in `commands.md` and `eval/docs/README.md`.
- Dependabot is enabled and opened 28 update branches (one PR per dependency). These are not errors; merge or close them.
- Commits are authored only by Akshat (the Claude co-author trailer was removed).

## 2. agentkit (shared library)

`libs/agentkit/agentkit/`: job contract (`/health`, `/jobs/{name}`, `/runs/{id}`, `X-Job-Token`), config and routing, `db`,
`llm`, `embed`, `retrieve`, `prompts`, `task_runner`, `checks`, `learn`, `redact`, `slack`, `log`.

Change made this session: `task_runner.run_task` now continues without examples or knowledge if retrieval fails
(database or embedder down), instead of crashing the task. A unit test covers this.

## 3. LLM layer (all seven routed tasks)

Each task has a pydantic schema, a prompt file, and code checks. Low confidence or failed checks produce "problems",
which mean "send to human review", never an automatic action.

| Task | Model alias | Code | Prompt |
|---|---|---|---|
| `lead.qualify` | dev | `lead-service/app/qualify` | `lead_qualify.txt` |
| `lead.extract_contact` | dev | `lead-service/app/enrich` | `lead_extract_contact.txt` |
| `outreach.draft` | dev | `outreach-service/app/drafting` | `outreach_draft.txt` |
| `outreach.classify_reply` | fast | `outreach-service/app/replies` | `outreach_classify_reply.txt` |
| `outreach.draft_reply` | dev | `outreach-service/app/replies` | `outreach_draft_reply.txt` |
| `content.plan` | dev | `content-service/app/topics` | `content_plan.txt` |
| `content.draft_post` | primary | `content-service/app/drafting` | `content_draft_post.txt` |

Prompts live in each service's `app/prompts/`. Untrusted text is always wrapped in `<untrusted_data>` and the model has
no tools.

## 4. LLM host (office machine)

- `services/llm-service/docker-compose.yml`: vLLM (`agent-dev`, port 8001) and TEI embeddings (port 8002), bound to `BIND_IP` (Tailscale IP), API key required.
- `services/llm-service/llm.env.example`: settings template (`llm.env` is gitignored).
- `config/routing.dev-only.yaml`: routes every task to `dev`, for use while only one model is served.
- `primary` and `fast` repo IDs in `models.yaml` are still TODO and must be confirmed on Hugging Face.

## 5. Testing tools

- `eval/runner/smoke.py`: reachability, plain reply, schema-constrained reply, optional embeddings.
- `eval/runner/run_eval.py`: accuracy on labelled JSONL sets with a `--min` gate.
- `eval/runner/draft_check.py`: runs the four generative tasks and applies length, banned-phrase and name checks.
- `services/llm-service/scripts/verify.sh` (`make llm-verify`): runs all of the above and exits non-zero on failure.
- Seed sets in `eval/sets/`: `lead_qualify` (16), `lead_extract_contact` (7), `outreach_classify_reply` (14). These are synthetic, for plumbing checks only; team-labelled data is still needed before trusting any accuracy number.

## 6. Test on the office machine

```bash
cd services/llm-service && cp llm.env.example llm.env      # set LLM_API_KEY, HF_TOKEN, BIND_IP
docker compose --env-file llm.env up -d                    # wait 10-15 min for the model to load
cd ../..
export LLM_BASE_URL=http://<tailscale-ip>:8001 EMBED_BASE_URL=http://<tailscale-ip>:8002 LLM_API_KEY=...
export ROUTING_CONFIG=config/routing.dev-only.yaml
make llm-verify
```

## 7. Status of checks

- ruff, mypy and unit tests (agentkit 16, each service) pass locally.
- **Not yet run against a real model.** A local Ollama model (Qwen3 4B) was downloading for a first local run; the
  vLLM compose stack, TEI route and `verify.sh` are untested until run on the GPU machine.
- Migrations and Edge Functions are syntax-checked only.

## 8. Next steps

1. Run `make llm-verify` on the office machine and fix failing prompts.
2. Confirm `primary` and `fast` model repo IDs, then drop `routing.dev-only.yaml`.
3. Get team-labelled eval data (targets: qualify 100, extract 60, 20 each of email, reply and blog drafts).
4. Implement the service job functions on top of the LLM layer.
5. Apply migrations to a live Supabase project; build the dashboard.

## 9. Redesign (2026-10-01): one daily runner

The earlier layout (three always-on HTTP services, weekly/2-hourly schedules, Slack approvals, LinkedIn/dev.to
publishing) did not match the intended product. It is replaced by a single bounded daily run with two
workflows: up to 10 new project leads with company-specific proposal emails (approval-gated), and one technical
blog per day. See `README.md`.

- Removed: `services/lead-service`, `outreach-service`, `content-service`, `scheduler/`, dispatch/lead/outreach/content/health
  workflows, `slack-interact` and `trigger-run` Edge Functions, reply classification/drafting, root `docker-compose.yml`.
- Added: `agent/` (runner, sources, contacts, mailer, review CLI, blog pipeline), `config/sources.yaml`,
  `deploy/` (systemd timer), migration `0003_daily_agent.sql`.
- Task modules moved to `agent/tasks/`; prompts to `agent/prompts/`. `lead.qualify` now also extracts company,
  project, technologies, location and website; the proposal prompt is company-specific.
