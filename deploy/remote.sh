#!/usr/bin/env bash
# Code on this laptop, run on the office machine. Syncs the repo over SSH and runs commands there,
# with the office desktop (DISPLAY=:0) so the visible Chrome run works from here.
#
#   bash deploy/remote.sh init user@office-host    one-time: SSH key, remote folder, saves .remote.env
#   bash deploy/remote.sh run --desktop --force    sync, then: python -m agent run --desktop --force
#   bash deploy/remote.sh <command> ...            see the list below
#
# Settings come from the environment or .remote.env (git-ignored): REMOTE (user@host), REMOTE_DIR (default
# ~/Marketing-AI-Agent on the office machine). Secrets: .env and credentials.json are never synced, only by push-secrets.
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f .remote.env ]; then
  # shellcheck disable=SC1091
  . ./.remote.env
fi
REMOTE="${REMOTE:-}"
REMOTE_DIR="${REMOTE_DIR:-Marketing-AI-Agent}"   # relative to the remote home directory
SSH_OPTS=(-o ServerAliveInterval=30 -o ControlMaster=auto -o ControlPersist=10m -o "ControlPath=$HOME/.ssh/cm-%r@%h:%p")

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Commands:
  init user@host     one-time setup (SSH key, remote folder)
  sync               copy this repo to the office machine (no .env, .venv, Chrome profile, output)
  deps               sync, then install/update Python dependencies there
  run <args>         stop any running agent, sync, then run: python -m agent run <args>   (e.g. run --desktop --force)
  agent <args>       python -m agent <args>, no sync           (e.g. agent leads review)
  test               sync, then run the tests there
  exec <cmd...>      run any shell command in the remote repo
  shell              interactive shell in the remote repo
  stop               kill switch: the running agent ends after its current step
  resume             remove the kill switch
  logs               last run of the daily timer
  shots              download the screenshots of the latest runs into ./out/remote/
  push-secrets       copy YOUR local .env and credentials.json to the office machine (asks first; push-env is the same)
EOF
}

need_remote() {
  [ -n "$REMOTE" ] || { echo "REMOTE is not set: run 'bash deploy/remote.sh init user@host' first" >&2; exit 1; }
}

# shellcheck disable=SC2029  # the command string is built on this side on purpose
rssh() { ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"; }

# Run a command in the remote repo with .env loaded and the office desktop selected.
remote_run() {
  local cmd
  cmd=$(printf '%q ' "$@")
  rssh -t "cd ~/$REMOTE_DIR && set -a && [ -f .env ] && . ./.env; set +a; export DISPLAY=\${DISPLAY:-:0}; export PATH=\$HOME/.local/bin:\$PATH; $cmd"
}

do_sync() {
  need_remote
  rssh "mkdir -p ~/$REMOTE_DIR"
  rsync -az --delete --info=stats0,name1 -e "ssh ${SSH_OPTS[*]}" \
    --exclude='.git/' --exclude='.venv/' --exclude='.env' --exclude='credentials.json' --exclude='credential.json' --exclude='.remote.env' \
    --exclude='.chrome-profile/' --exclude='.chrome-desktop-profile/' --exclude='out/' \
    --exclude='.cache/' --exclude='__pycache__/' --exclude='.*_cache/' --exclude='.agent-visited.json' \
    --exclude='node_modules/' --exclude='*.pyc' \
    ./ "$REMOTE:$REMOTE_DIR/"
}

# A run that is already going keeps the code it started with (a sync does not change it) and would share the Chrome
# window with the new run. Stop it first: the scheduled service, then any manual run.
stop_running_agents() {
  need_remote
  # shellcheck disable=SC2016  # expanded on the office machine, on purpose
  rssh 'systemctl --user stop aosp-agent.service 2>/dev/null || true
        if pgrep -f "^\.venv/bin/python -m agent run" >/dev/null; then
          echo "stopping a running agent: $(pgrep -af "^\.venv/bin/python -m agent run" | cut -c1-120)"
          pkill -f "^\.venv/bin/python -m agent run"
          for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "^\.venv/bin/python -m agent run" >/dev/null || break; sleep 1; done
          pkill -9 -f "^\.venv/bin/python -m agent run" || true
        fi'
}

cmd="${1:-help}"
[ $# -eq 0 ] || shift

case "$cmd" in
  init)
    [ $# -eq 1 ] || { echo "usage: remote.sh init user@host" >&2; exit 1; }
    REMOTE="$1"
    [ -f "$HOME/.ssh/id_ed25519" ] || ssh-keygen -t ed25519 -N '' -f "$HOME/.ssh/id_ed25519"
    mkdir -p "$HOME/.ssh"
    ssh-copy-id -i "$HOME/.ssh/id_ed25519.pub" "$REMOTE"
    printf 'REMOTE=%s\nREMOTE_DIR=%s\n' "$REMOTE" "$REMOTE_DIR" > .remote.env
    do_sync
    echo "Ready. On the office machine run once: bash deploy/bootstrap-ubuntu.sh (inside ~/$REMOTE_DIR)."
    echo "Then from here: bash deploy/remote.sh push-env   and   bash deploy/remote.sh run --desktop --force"
    ;;
  sync) do_sync ;;
  deps)
    do_sync
    remote_run uv pip install --python .venv/bin/python -r requirements-dev.txt
    ;;
  run)
    stop_running_agents
    do_sync
    remote_run .venv/bin/python -m agent run "$@"
    ;;
  agent)
    need_remote
    remote_run .venv/bin/python -m agent "$@"
    ;;
  test)
    do_sync
    remote_run .venv/bin/python -m pytest agent -q "$@"
    ;;
  exec)
    need_remote
    [ $# -gt 0 ] || { echo "usage: remote.sh exec <command...>" >&2; exit 1; }
    rssh -t "cd ~/$REMOTE_DIR && export DISPLAY=\${DISPLAY:-:0} && $*"
    ;;
  shell)
    need_remote
    rssh -t "cd ~/$REMOTE_DIR && export DISPLAY=\${DISPLAY:-:0}; exec \$SHELL -l"
    ;;
  stop)
    need_remote
    rssh "touch /tmp/gui-agent.stop" && echo "kill switch set: the run ends after its current step"
    ;;
  resume)
    need_remote
    rssh "rm -f /tmp/gui-agent.stop" && echo "kill switch removed"
    ;;
  logs)
    need_remote
    rssh "journalctl --user -u aosp-agent.service -n 100 --no-pager"
    ;;
  shots)
    need_remote
    mkdir -p out/remote
    rsync -az -e "ssh ${SSH_OPTS[*]}" "$REMOTE:$REMOTE_DIR/out/desktop/" out/remote/
    echo "saved to out/remote/"
    ;;
  push-secrets | push-env)
    need_remote
    files=()
    for f in .env credentials.json credential.json; do [ -f "$f" ] && files+=("$f"); done
    [ ${#files[@]} -gt 0 ] || { echo "no local .env or credentials.json" >&2; exit 1; }
    read -r -p "Copy ${files[*]} (secrets) to $REMOTE:$REMOTE_DIR/ ? [y/N] " yn
    [ "$yn" = y ] || exit 1
    do_sync
    for f in "${files[@]}"; do
      scp "${SSH_OPTS[@]}" "$f" "$REMOTE:$REMOTE_DIR/$f"
      rssh "chmod 600 ~/$REMOTE_DIR/$f"
    done
    echo "done: ${files[*]} copied (mode 600). The office machine needs its own LLM_BASE_URL / EXECUTOR_* values if they differ"
    ;;
  help | -h | --help) usage ;;
  *) echo "unknown command: $cmd" >&2; usage >&2; exit 1 ;;
esac
