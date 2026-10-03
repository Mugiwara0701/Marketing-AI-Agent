# Setting up a fresh Ubuntu machine

Everything to do after installing Ubuntu, in order. Target: **Ubuntu 24.04 LTS** (Server, Desktop or Xubuntu all work;
the sandbox desktop runs inside Docker, so the host's desktop environment does not matter). One machine runs
Ollama (models), the sandbox container (Chrome the vision model drives) and the agent.

Steps marked **(GPU)** are for an NVIDIA GPU; skip them on a CPU-only machine (models will be slow).
Steps were written from the project files and have not all been run on a fresh machine: if one fails, fix it
and update this file.

## 1. Base system

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git curl ca-certificates build-essential
sudo ufw allow OpenSSH && sudo ufw enable        # nothing else is reachable from the network
```

## 2. NVIDIA driver (GPU)

```bash
sudo ubuntu-drivers install
sudo reboot
nvidia-smi                                       # must list the GPU and its VRAM
```

## 3. Ollama and the models

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl edit ollama
```

In the editor that opens, add:

```ini
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=2m"
```

Why: the text model and the vision model are never needed together. With one loaded model at a time, Ollama
swaps them in and out of memory by itself.

```bash
sudo systemctl restart ollama
ollama pull <text model>         # the Qwen text model used for emails, qualifying, blog
ollama pull <vision model>       # e.g. a Qwen3-VL 8B tag: confirm the exact name on ollama.com/library
ollama list                      # note the exact names
curl -s localhost:11434/v1/models | head -c 200
```

## 4. Docker

```bash
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
```

Log out and back in (or reboot) so the group applies, then check `docker ps`.

## 5. Get the code

```bash
git clone <repo url> Marketing-AI-Agent
cd Marketing-AI-Agent
git checkout feature/ubuntu-controll-script      # or main once merged
```

## 6. Python environment

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
uv python install 3.12
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
playwright install chromium                      # only for `browse` and `fill-form`
python -m pytest agent -q                        # all tests should pass
```

## 7. Configuration (.env)

```bash
cp .env.example .env
```

Fill it in by hand (it holds secrets: never commit it, never paste it into chats). Values that matter here:

| Variable | Value |
|---|---|
| `LLM_BASE_URL` | `http://127.0.0.1:11434` |
| `LLM_MAX_CONCURRENT` | `1` (one request at a time, so Ollama never swaps models mid-batch) |
| `MODEL_GUI_STEP` | the vision model's name from `ollama list` |
| `MODEL_OUTREACH_DRAFT` etc. | optional: text model name per task |
| `EXECUTOR_URL` / `EXECUTOR_TOKEN` | `http://127.0.0.1:8765` / 24+ random characters (same value as step 8) |
| `DATABASE_URL`, `SLACK_*`, `MAIL_FROM` | from your Supabase and Slack accounts; `MAIL_FROM` is the Gmail address you log in with |
| `GMAIL_CREDENTIALS_PATH` / `GMAIL_TOKEN_PATH` | `credentials.json` / `token.json` (see the Gmail note below) |
| `TEST_RECIPIENT` | comma separated team inboxes while developing, so no real company is ever emailed |
| `EMAIL_SENDING_ENABLED` | `true` only when you want mail to go out (to `TEST_RECIPIENT` while it is set) |

Generate a token: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
**Gmail login on this headless host:** log in once on a machine with a browser (`pip install -r requirements.txt`,
then `python -m agent.gmail_check`), copy the resulting `token.json` and your `credentials.json` into the project
folder here (`chmod 600`), and run `python -m agent.gmail_check` again: it must print the account without a
browser. While the Google OAuth app is in "Testing", the refresh token expires after 7 days; repeat the login then
(or set the consent screen to "In production").

## 8. The sandbox (Chrome the vision model drives)

```bash
docker build -t aosp-sandbox sandbox/
docker run -d --name sandbox --restart unless-stopped \
  --cap-drop ALL --security-opt no-new-privileges --shm-size=2g \
  -e VNC_PASSWORD='<strong password>' -e EXECUTOR_TOKEN='<same token as .env>' \
  -p 127.0.0.1:8765:8765 -p 127.0.0.1:5900:5900 \
  aosp-sandbox
curl localhost:8765/health                       # {"ok": true}
docker logs sandbox | tail
```

Both ports are bound to localhost only. To watch the desktop from another computer, tunnel it:
`ssh -L 5900:127.0.0.1:5900 user@this-machine`, then open a VNC viewer on `localhost:5900`.

**If Chrome never appears:** Chrome's own sandbox needs user namespaces, which Docker's default profile blocks,
and the entrypoint does not pass `--no-sandbox` (on purpose: it removes a defence layer). Preferred fix: run
the container with a seccomp profile that allows user namespaces, or run it in a VM. Using `--no-sandbox` is
a last resort for a throwaway prototype box.

Not set up by these commands: blocking the container's access to your LAN, the host and `169.254.169.254`
(the plan wants public internet only). Add it with host firewall rules before running unattended on sites
you do not trust.

## 9. Checks, in this order

```bash
# Desktop actions work: repeats click/type/scroll 20 times, needs >= 95%
EXECUTOR_URL=http://127.0.0.1:8765 EXECUTOR_TOKEN='<token>' python3 sandbox/smoke_check.py 20

python -m agent check                            # LLM, database, Slack, search, email switch

# Does the vision model click accurately? 3 tasks first, then all 10 (needs >= 70%)
python -m agent gui-spike --tasks 3
python -m agent gui-spike

# One real company (nothing saved, nothing posted)
python -m agent gui-find --dry "ID Tech Solutions"
```

Compare models by changing `MODEL_GUI_STEP` and re-running `gui-spike`. Stop a run at any time by creating
the kill file: `touch /tmp/gui-agent.stop` (delete it to allow runs again). Every step is logged to
`out/gui/<run id>.jsonl`.

## 10. Day-to-day

```bash
python -m agent gui-find "Company A" "Company B"   # find contacts, draft, post to Slack for approval
python -m agent send --watch                       # sends within seconds of an Approve click
python -m agent test-email --count 3               # sample emails through Slack to TEST_RECIPIENT
```

The daily run (leads, blog, follow-ups) is scheduled with systemd: see `deploy/README.md`.
The Slack buttons need the `slack-interact` Supabase Edge Function deployed and set as the Slack app's
Request URL (`supabase/functions/README.md`).

## Troubleshooting

| Symptom | Check |
|---|---|
| `permission denied` on the Docker socket | you have not logged out and back in after `usermod -aG docker` |
| `executor unreachable` | `docker ps`, `docker logs sandbox`, token identical in `.env` and the container |
| Model answers are not valid steps | `ollama list` name matches `MODEL_GUI_STEP`; try a larger or different vision model |
| Every step is slow | models are swapping: confirm `OLLAMA_MAX_LOADED_MODELS=1` took effect and batch work (`gui-find` takes several names) |
| `GmailAuthError` / `Gmail token refresh failed` | delete `token.json`, log in again on a machine with a browser, copy it over (see step 7) |
| Chrome window missing in VNC | see "If Chrome never appears" in step 8 |
