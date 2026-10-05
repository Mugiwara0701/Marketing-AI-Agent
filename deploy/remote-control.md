# Control the office machine from your laptop

Write code on your laptop. `deploy/remote.sh` copies it to the office machine over SSH and runs the agent there,
on the office desktop (`DISPLAY=:0`), so the visible Chrome run works from here.

- **Laptop:** where you edit code, commit and push. Never edit files on the office machine, the next sync overwrites them.
- **Office machine:** where the agent, Ollama, Docker and Chrome run.

## A. One-time setup on the office machine

1. Log in to the **Xubuntu (Xorg)** session. Turn on auto-login. Turn off screen lock and blanking.
2. Get the code and install everything (skip if already done):
   ```bash
   git clone https://github.com/Mugiwara0701/Marketing-AI-Agent.git
   cd Marketing-AI-Agent && git checkout feature/ubuntu-controll-script
   bash deploy/bootstrap-ubuntu.sh
   ```
   If the machine was set up before SSH was added to the script, run this instead:
   ```bash
   sudo apt install -y openssh-server rsync && sudo systemctl enable --now ssh
   ```
3. Note the address and username:
   ```bash
   hostname -I        # e.g. 192.168.1.50
   whoami
   ```
4. Fill in `.env` there (or push yours from the laptop, step B3).

## B. One-time setup on the laptop

1. Check the office machine is reachable: `ping 192.168.1.50`.
   Different networks: install Tailscale on both machines and use the Tailscale address.
2. Connect (asks for the office user's password once, to copy the SSH key):
   ```bash
   bash deploy/remote.sh init user@192.168.1.50
   ```
   This saves the address in `.remote.env` (git-ignored) and does a first sync.
3. Copy your `.env` (secrets) to the office machine. It asks before copying:
   ```bash
   bash deploy/remote.sh push-env
   ```
   On the office machine make sure `LLM_BASE_URL=http://127.0.0.1:11434`, `LEADS_MODE=desktop` and `MODEL_GUI_STEP=qwen3-vl:8b`.
4. Check the setup:
   ```bash
   bash deploy/remote.sh agent migrate
   bash deploy/remote.sh agent check
   bash deploy/remote.sh agent desktop-check
   ```

## C. Daily use (from the laptop)

1. Edit code on the laptop.
2. Run it on the office machine (syncs first, then streams the output here):
   ```bash
   bash deploy/remote.sh run --desktop --force
   ```
3. Look at the result:
   ```bash
   bash deploy/remote.sh agent leads-today       # stored leads
   bash deploy/remote.sh agent review            # email drafts (nothing is sent)
   bash deploy/remote.sh shots                   # screenshots into out/remote/
   ```
4. Commit and push from the laptop as usual.

## D. Commands

| Command | What it does |
|---|---|
| `init user@host` | one-time: SSH key, remote folder, saves `.remote.env` |
| `sync` | copy the repo (no `.git`, `.venv`, `.env`, Chrome profile, `out/`) |
| `deps` | sync, then install Python dependencies there |
| `run <args>` | sync, then `python -m agent run <args>` |
| `agent <args>` | `python -m agent <args>` without syncing |
| `test` | sync, then run the tests there |
| `exec <cmd>` | run any command in the remote repo |
| `shell` | interactive shell in the remote repo |
| `stop` / `resume` | set / remove the kill switch (`/tmp/gui-agent.stop`) |
| `logs` | last run of the daily timer |
| `shots` | download the run screenshots |
| `push-env` | copy your local `.env` to the office machine |

## E. Daily schedule on the office machine

```bash
systemctl --user start aosp-agent.timer
systemctl --user list-timers aosp-agent.timer
```

## F. Troubleshooting

- **`Connection timed out`**: not on the same network. Use Tailscale, or check `ping` and the office firewall (`sudo ufw allow OpenSSH`).
- **`Permission denied (publickey)`**: run `init` again, or `ssh-copy-id user@host`.
- **Chrome does not open on the office screen**: the office machine is not logged in, is locked, or is in a Wayland session. Log in to Xubuntu (Xorg), then `bash deploy/remote.sh agent desktop-check`.
- **Stop a run that is going wrong**: `bash deploy/remote.sh stop` (then `resume` before the next run), or Ctrl+C in the terminal running `run`.
- **`docker: permission denied` on the office machine**: log out and back in once (Docker group).
