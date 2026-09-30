# llm-service

The self-hosted open-source LLM (component 4). It is **not** a Python service: it runs vLLM
(chat, OpenAI-compatible) and an embedding server on the company machine.

- Never expose the ports publicly. Reach them over Tailscale only; `--api-key` is required.
- Models and ports are declared in `config/models.yaml`; tasks map to aliases in `../../config/routing.yaml`.
- `adapters/` holds optional LoRA adapters (all must share one base model).

## Start (example, on the GPU host)

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
