#!/usr/bin/env bash
# One-shot setup of a fresh Ubuntu (Xubuntu) machine: everything in deploy/new-machine-steps.md that can be automated.
# Run from a clone of the repo as your normal user (not root): bash deploy/bootstrap-ubuntu.sh
# Safe to re-run. Never overwrites an existing .env, never prints secrets from it, never sends email.
#
# Overrides: TEXT_MODEL, VISION_MODEL, SKIP_MODELS=1, SKIP_NVIDIA=1, SKIP_SEARXNG=1, SKIP_TIMER=1, SKIP_TESTS=1
set -euo pipefail

TEXT_MODEL="${TEXT_MODEL:-qwen3.5:9b}"        # served to the agent under the alias agent-dev
VISION_MODEL="${VISION_MODEL:-qwen3-vl:8b-instruct}"   # the -instruct build: the default tag "thinks" and runs out of tokens
cd "$(dirname "$0")/.."
REPO="$PWD"

[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not root" >&2; exit 1; }
[ "$(uname -m)" = x86_64 ] || echo "warning: not x86_64, Google Chrome install will be skipped" >&2
sudo -v

echo "==> 1/8 base packages"
sudo apt-get update
sudo apt-get full-upgrade -y
sudo apt-get install -y git curl ca-certificates build-essential ufw pciutils openssh-server rsync
sudo systemctl enable --now ssh   # lets the laptop run commands here: deploy/remote.sh
# Desktop automation: xdotool (mouse + keyboard), xclip (clipboard), imagemagick (screenshots), tesseract (OCR)
sudo apt-get install -y xdotool xclip imagemagick tesseract-ocr libnotify-bin
if [ "$(uname -m)" = x86_64 ] && ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null; then
  curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt-get install -y /tmp/chrome.deb
fi
sudo ufw allow OpenSSH
sudo ufw --force enable

echo "==> 2/8 NVIDIA driver"
if [ -z "${SKIP_NVIDIA:-}" ] && lspci | grep -qi nvidia && ! command -v nvidia-smi >/dev/null; then
  sudo ubuntu-drivers install
  echo "NVIDIA driver installed. Reboot, then re-run this script to continue." >&2
  exit 0
fi

echo "==> 3/8 Docker"
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

echo "==> 4/8 Ollama"
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=2m"
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ollama
sudo systemctl restart ollama
if [ -z "${SKIP_MODELS:-}" ]; then
  until curl -sf localhost:11434/api/version >/dev/null; do sleep 1; done
  ollama pull "$TEXT_MODEL"
  ollama cp "$TEXT_MODEL" agent-dev            # routing alias "dev" -> agent-dev
  ollama pull "$VISION_MODEL"
  ollama cp "$VISION_MODEL" vlm                # routing alias "vlm" (gui.step) works without MODEL_GUI_STEP
fi

echo "==> 5/8 Python 3.12 environment"
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; }
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.12
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/playwright install --with-deps chromium

echo "==> 6/8 configuration (.env)"
if [ -e .env ]; then
  echo ".env exists, left untouched"
else
  if [ -f .env.example ]; then cp .env.example .env
  elif git cat-file -e HEAD:.env.example 2>/dev/null; then git show HEAD:.env.example > .env
  else echo "no .env.example found: create .env by hand" >&2; fi
  [ ! -f .env ] || chmod 600 .env
fi

echo "==> 7/8 web search (SearXNG) and daily timer"
if [ -z "${SKIP_SEARXNG:-}" ]; then
  # the docker group is not active in this shell yet, so use sudo
  sudo docker compose -f deploy/searxng/docker-compose.yml up -d
fi
if [ -z "${SKIP_TIMER:-}" ]; then
  mkdir -p "$HOME/.config/systemd/user"
  # the unit assumes ~/Marketing-AI-Agent; point it at wherever this clone lives
  sed "s|%h/Marketing-AI-Agent|$REPO|g" deploy/aosp-agent.service > "$HOME/.config/systemd/user/aosp-agent.service"
  cp deploy/aosp-agent.timer "$HOME/.config/systemd/user/"
  # enable only (no --now): the first run must wait until .env is filled in
  systemctl --user daemon-reload || echo "no user systemd session: run the three 'systemctl --user' lines from deploy/README.md after logging in" >&2
  systemctl --user enable aosp-agent.timer || true
  sudo loginctl enable-linger "$USER"
fi
# Keep the desktop awake for unattended runs (Xfce only; harmless elsewhere)
if command -v xfconf-query >/dev/null && [ -n "${DISPLAY:-}" ]; then
  xfconf-query -c xfce4-screensaver -p /saver/enabled -n -t bool -s false 2>/dev/null || true
  xfconf-query -c xfce4-screensaver -p /lock/enabled -n -t bool -s false 2>/dev/null || true
  xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/blank-on-ac -n -t int -s 0 2>/dev/null || true
  xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/dpms-on-ac-sleep -n -t int -s 0 2>/dev/null || true
  xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/dpms-on-ac-off -n -t int -s 0 2>/dev/null || true
fi

echo "==> 8/8 checks"
if [ -z "${SKIP_TESTS:-}" ]; then .venv/bin/python -m pytest agent -q || echo "some tests failed: see output above" >&2; fi
ollama list
docker --version
[ "${XDG_SESSION_TYPE:-x11}" != wayland ] || echo "WARNING: this is a Wayland session; log in to the Xorg session (desktop control is refused on Wayland)" >&2

cat <<EOF

Done. Remaining by hand:
  1. Log out and back in (docker group, Xorg session). Enable auto-login in Settings > Session and Startup / login screen.
  2. Fill in .env (secrets: never commit). MODEL_GUI_STEP=$VISION_MODEL, BROWSER_BACKEND=desktop, SEARXNG_URL=http://127.0.0.1:8888
     Token for EXECUTOR_TOKEN:  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  3. .venv/bin/python -m agent migrate && .venv/bin/python -m agent check && .venv/bin/python -m agent desktop-check
  4. Try one run:  .venv/bin/python -m agent run --desktop --force
  5. Start the daily timer:  systemctl --user start aosp-agent.timer
  6. Sandbox (only for the vision fallback): see step 8 of deploy/new-machine-steps.md
EOF
