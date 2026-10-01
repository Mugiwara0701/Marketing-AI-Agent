# agentkit

Shared library used by the daily agent (`agent/`).

- `jobs.py`: `create_app(service, jobs)` builds the FastAPI app with the job contract
  (`/health`, `POST /jobs/{name}` -> 202 + run_id, `GET /runs/{id}`), token auth via `JOB_TOKEN`,
  and one-run-at-a-time per job (409).
- `config.py`: env and `config/routing.yaml` helpers. `log.py`: JSON logs (no PII).
- `db` (asyncpg, claim queue, agent_runs), `llm` (`complete(task, ...)`, routing, schema validation, retries), `embed`, `retrieve`, `prompts`, `task_runner`, `checks`, `learn`, `redact`, `slack`.

```bash
pip install -e "libs/agentkit[dev]" && pytest libs/agentkit
```
