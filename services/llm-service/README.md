# llm-service

The self-hosted open-source LLM (component 4). It is **not** a Python service: it runs vLLM
(chat, OpenAI-compatible) and an embedding server on the company machine.

- Never expose the ports publicly. Reach them over Tailscale only; `--api-key` is required.
- Models and ports are declared in `config/models.yaml`; tasks map to aliases in `../../config/routing.yaml`.
- `adapters/` holds optional LoRA adapters (all must share one base model).

## Start on the GPU host (docker compose, recommended)

```bash
cd services/llm-service
cp llm.env.example llm.env       # set LLM_API_KEY, HF_TOKEN, BIND_IP (tailscale ip -4)
docker compose --env-file llm.env up -d
docker compose logs -f vllm      # first start downloads the model, then loads it (10-15 min)
```

This serves `agent-dev` on `BIND_IP:8001` (vLLM) and embeddings on `BIND_IP:8002` (TEI). `primary` and `fast` are not
served until their repo IDs in `models.yaml` are confirmed; until then use `ROUTING_CONFIG=config/routing.dev-only.yaml`.

## Verify from any machine on the tailnet

```bash
export LLM_BASE_URL=http://<tailscale-ip>:8001 EMBED_BASE_URL=http://<tailscale-ip>:8002 LLM_API_KEY=...
export ROUTING_CONFIG=config/routing.dev-only.yaml
make llm-verify        # or: bash services/llm-service/scripts/verify.sh
```

It checks reachability, a plain reply, a schema-constrained reply, embeddings (1024 dims), accuracy on the labelled
synthetic sets (`lead.qualify`, `lead.extract_contact`, threshold 0.85) and the generative
tasks (`outreach.draft`) against length and banned-phrase
checks. Any failure exits non-zero. Read the failing cases it prints, then adjust the prompt in
`agent/prompts/*.txt`. Generated text still needs a human read; the checks catch format problems only.

## Start manually (alternative)

```bash
export LLM_API_KEY=...            # same value as in the services' env
vllm serve Qwen/Qwen3.5-9B --served-model-name agent-dev \
  --max-model-len 16384 --api-key "$LLM_API_KEY" --host 0.0.0.0 --port 8001
```

Embeddings (TEI) example:

```bash
docker run --gpus all -p 8002:80 ghcr.io/huggingface/text-embeddings-inference:latest \
  --model-id Qwen/Qwen3-Embedding-0.6B
```

Verify before relying on it: TEI's OpenAI-compatible route (`/v1/embeddings` vs `/embed`), the
exact repo IDs marked TODO, and vLLM flags for your installed version.

## Power and availability

The host is not on 24/7. It powers on 30 minutes before each window (night 01:30-04:30 IST daily, day 09:00-17:00
IST weekdays) and powers down after the window once no job is running. vLLM, the embedding server, Tailscale and
the compose services start at boot (systemd / restart policies). Allow 10-15 minutes for the model to load;
workflows wait for `/v1/models` before running jobs. Measure boot-to-ready time on the real GPU.

## Local testing without the GPU host (Ollama)

For development on a small GPU (6 GB is enough for a 4B model). Ollama exposes the same OpenAI-compatible API,
so no code changes are needed; only the served name must match the routing alias.

```bash
ollama pull qwen3:4b-instruct-2507-q4_K_M
ollama cp qwen3:4b-instruct-2507-q4_K_M agent-dev      # routing alias "dev" -> agent-dev
export LLM_BASE_URL=http://127.0.0.1:11434 LLM_API_KEY=local ROUTING_CONFIG=config/routing.yaml
python eval/runner/smoke.py                              # health, plain, structured reply
PYTHONPATH=. python eval/runner/run_eval.py lead.qualify \
    eval/sets/lead_qualify.jsonl agent/prompts agent.tasks.qualify:QualifyResult --min 0.85
```

`eval/sets/lead_qualify.jsonl` is a small synthetic seed set for plumbing checks only; replace it with team-labelled
posts before trusting any accuracy number. Run tasks that use other aliases (`fast`, `primary`) by also copying a model
to `agent-fast` / `agent-primary`, or point them at `dev` in `config/routing.yaml`.
