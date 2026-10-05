# New Ubuntu machine: all steps from scratch

Target: Ubuntu 24.04+ with nothing installed. Steps marked **(GPU)** are for an NVIDIA GPU only.
Shortcut: steps 1-6 are automated by `bash deploy/bootstrap-ubuntu.sh` (run from the repo root, after step 5).
The full runbook with checks and troubleshooting is `deploy/ubuntu-host-setup.md`.

## 1. Base system

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git curl ca-certificates build-essential ufw
sudo ufw allow OpenSSH && sudo ufw enable
```

## 2. NVIDIA driver (GPU)

```bash
sudo ubuntu-drivers install
sudo reboot
nvidia-smi                      # must list the GPU and its VRAM
```

## 3. Docker

```bash
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER   # then log out and back in
docker ps
```

## 4. Ollama and models

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl edit ollama
```

Add in the editor:

```ini
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=2m"
```

```bash
sudo systemctl restart ollama
ollama pull qwen3.5:9b            # text model (use qwen3:4b-instruct-2507-q4_K_M on a 6 GB GPU if slow)
ollama cp qwen3.5:9b agent-dev    # routing alias "dev" -> agent-dev
ollama pull qwen3-vl:8b-instruct           # vision model for the GUI agent
ollama list
```

## 5. Get the code

```bash
git clone <repo url> Marketing-AI-Agent
cd Marketing-AI-Agent
git checkout feature/ubuntu-controll-script
```

## 6. Python environment

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
uv python install 3.12
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements-dev.txt
playwright install --with-deps chromium
python -m pytest agent -q
```

## 7. Configuration

```bash
cp .env.example .env              # if missing on your branch: git show main:.env.example > .env
```

Fill in by hand (secrets: never commit or paste). Key values:

- `LLM_BASE_URL=http://127.0.0.1:11434`, `LLM_API_KEY=local`
- `LLM_MAX_CONCURRENT=1`
- `MODEL_GUI_STEP=qwen3-vl:8b-instruct`
- `EXECUTOR_URL=http://127.0.0.1:8765`, `EXECUTOR_TOKEN=` from `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- `DATABASE_URL`, `SUPABASE_*`, `SLACK_*`, `RESEND_API_KEY`, `MAIL_FROM` from your accounts
- `TEST_RECIPIENT` set while developing; `EMAIL_SENDING_ENABLED=false` until ready

## 8. Search (SearXNG) and sandbox

```bash
docker compose -f deploy/searxng/docker-compose.yml up -d

docker build -t aosp-sandbox sandbox/
docker run -d --name sandbox --restart unless-stopped \
  --cap-drop ALL --security-opt no-new-privileges --shm-size=2g \
  -e VNC_PASSWORD='<strong password>' -e EXECUTOR_TOKEN='<same token as .env>' \
  -p 127.0.0.1:8765:8765 -p 127.0.0.1:5900:5900 \
  aosp-sandbox
curl localhost:8765/health        # {"ok": true}
```

## 9. Checks

```bash
python3 sandbox/smoke_check.py 20     # needs EXECUTOR_URL and EXECUTOR_TOKEN exported
python -m agent check
python -m agent gui-spike --tasks 3
python -m agent gui-find --dry "Some Company"
```

## 10. Daily run

```bash
python -m agent demo                  # one real pipeline run
```

Schedule it with systemd: see `deploy/README.md`.
