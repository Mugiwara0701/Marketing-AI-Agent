# agentkit

Shared library used by lead-, outreach- and content-service.

- `jobs.py`: `create_app(service, jobs)` builds the FastAPI app with the job contract
  (`/health`, `POST /jobs/{name}` -> 202 + run_id, `GET /runs/{id}`), token auth via `JOB_TOKEN`,
  and one-run-at-a-time per job (409).
- `config.py`: env and `config/routing.yaml` helpers. `log.py`: JSON logs (no PII).
- Empty placeholders to fill from the retrieval doc: `db, llm, embed, retrieve, prompts, task_runner, checks, learn, redact, slack`.

```bash
pip install -e "libs/agentkit[dev]" && pytest libs/agentkit
```
