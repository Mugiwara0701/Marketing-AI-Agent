# GUI Computer-Use Agent: Feasibility, Architecture and Implementation Plan

Date: 2026-10-02 · Author: Akshat · Status: Proposal (nothing implemented)

Related: `Browser_Automation_Plan.md` (Playwright reading/form detection, already in progress on this branch).

## 1. Idea

Give the agent its own isolated Ubuntu desktop with Chrome. The agent sees the screen (screenshots) and acts only
through virtual mouse and keyboard, like a person: click the address bar, type a query, press Enter, read the page,
scroll, click links.

```
Agent loop -> screenshot -> VLM decides action -> virtual mouse/keyboard -> Chrome -> website
```

Constraints decided so far:

- Interaction through the GUI input layer, not DOM manipulation or injected JavaScript.
- Everything runs in an isolated environment, separate from the host.
- Prefer open source and self-hosted components; no paid external APIs.

## 2. Verdict

| Question | Answer |
|---|---|
| Is it possible? | Yes. It is the standard "computer-use" pattern and open-source pieces exist for every layer. |
| Does it give the behaviour wanted? | Yes for generality (works on any visible site, no per-site selectors). No for stealth: it does **not** guarantee avoiding bot detection (section 8). |
| Is it the right tool for most of our pipeline? | No. Discovery, extraction, qualification and drafting are cheaper, faster and more reliable as scripts plus a text LLM. Use the GUI agent only for steps scripts cannot do (section 3). |
| Is Docker required? | No. Isolation is required; Docker is one way to get it (section 5). |
| Is a heavy GPU required? | Not for the sandbox. Only the model server needs one, and the amount depends on the model size (section 6). |

## 3. Where the GUI agent fits

Current pipeline (see `Browser_Automation_Plan.md`): SearXNG and job feeds find companies, a local LLM qualifies and
extracts contacts, Playwright reads JS-rendered pages and detects forms, a human reviews outreach.

| Step | Best tool | GUI agent needed? |
|---|---|---|
| Discovery from stable sources (SearXNG, job feeds, GitHub, RSS) | Scripts | No |
| Extract fields from consistently structured pages | Scripts / existing Playwright reader | No |
| Qualify a company, draft outreach | Text LLM (already in repo) | No |
| Navigate an unfamiliar site with an unpredictable layout | GUI agent | Maybe |
| Fill and submit a contact form that resists the scripted filler | GUI agent, with human approval | Maybe |
| Interact with canvas widgets, native dialogs, file pickers | GUI agent | Yes |

Recommendation: build the GUI agent as an **optional fallback mode**, not a replacement. Measure how many leads
actually need it before investing heavily.

## 4. Architecture

```
Orchestrator (Python)
  |- Model client: planner + grounding VLM (OpenAI-compatible endpoint, self-hosted)
  |- Policy layer: domain rules, step budget, approval gates, action log
  `- Executor client (authenticated, localhost/VPN)
                     |
                     v
   Isolated desktop (dedicated machine, or KVM VM, or hardened container)
     Executor service: screenshot, mouse, keyboard, scroll, wait, clipboard read
     Virtual display (Xvfb, or the VM display) + light window manager (openbox)
     Chrome, throwaway profile, launched as a normal app (no debug port)
     Optional noVNC on localhost for human supervision / takeover
                     |
                     v
        Filtering egress proxy -> public internet only (no LAN, no host, no cloud metadata)
```

Rules:

- The model never gets a shell. It only gets the fixed action set: `screenshot`, `click(x,y)`, `double_click`,
  `type(text)`, `key(combo)`, `scroll(dx,dy)`, `wait(s)`, `read_clipboard`.
- Fixed resolution (for example 1280x800), 100% browser zoom. Keep it constant so model coordinates map to pixels.
- After every action the executor returns a fresh screenshot. Decisions use the result, not an assumed result.
- Every action and screenshot is logged for audit and debugging.

## 5. Isolation options

Isolation is the requirement; the mechanism is a choice.

| Option | Isolation | Cost / effort | Use when |
|---|---|---|---|
| Dedicated spare machine (Ubuntu, own VLAN/guest network, nothing valuable on it) | Strong (the machine is the boundary) | Low | First real deployment. Fits "another system" |
| KVM/QEMU VM | Strong (separate kernel) | 4-8 GB RAM, more setup | Unattended browsing of untrusted sites |
| Docker container (non-root, caps dropped, read-only rootfs, seccomp, no mounts) | Medium (shared kernel) | Lowest | Prototype on a throwaway box |
| Docker + gVisor / Kata / Firecracker | Near-VM | Medium | Scale-out, density |
| Separate unprivileged Linux user + nftables owner rules (+ firejail/bubblewrap) | Weak-medium | Low | Quick prototype only |
| Running as the main user on the main machine | None | None | **Do not** |

Notes:

- Chrome's own sandbox needs user namespaces. Inside Docker it is often disabled with `--no-sandbox`, which removes a
  defence layer. Prefer a VM or a seccomp profile that allows user namespaces.
- Docker adds no meaningful CPU/RAM overhead and does not create the GPU requirement; the model does.
- Network isolation matters as much as filesystem isolation. Block RFC1918 ranges, the host, and 169.254.169.254.

## 6. Components (all open source)

### 6.1 Display, capture, input

- Display: Xvfb + openbox (container/box), or the VM's virtual display. noVNC optional, bound to localhost.
- Capture: `scrot` or ImageMagick `import` (X11); QMP `screendump` or VNC framebuffer (VM). Downscale/compress to
  control tokens; keep the scale factor in the executor.
- Input: `xdotool` (move, click, type, key, scroll via buttons 4/5). Alternatives: `ydotool`/python-evdev
  (kernel uinput virtual device, works on Wayland too), QEMU QMP `input-send-event` (VM-level injection).
  PyAutoGUI adds nothing over xdotool on Linux.
- Known gaps: some native Chrome UI (file pickers, permission prompts) needs special handling.

### 6.2 Model (the real constraint)

A GUI-grounded vision-language model takes a screenshot plus a goal and returns an action with coordinates.
Candidates (verify current license and benchmarks before committing; this area changes fast):

- UI-TARS / UI-TARS-1.5 (ByteDance): built for computer use, planning + grounding in one model. Best first try.
- Qwen2.5-VL / Qwen3-VL (Alibaba): strong general VLMs with grounding, mostly Apache-2.0.
- Specialist grounding models (OS-Atlas, ShowUI, Aguvis, GUI-Actor, OpenCUA, Holo1): good at "where to click", weaker
  at planning. Pair with a separate planner (the Agent S pattern).

Serving: vLLM (OpenAI-compatible, CUDA GPU) or llama.cpp/Ollama (quantized, smaller GPU or CPU; check per-model VLM
support). The model server can be a different machine from the sandbox.

Hardware guide:

| Workload | Realistic hardware |
|---|---|
| Text-only LLM for qualify/draft (7-9B, quantized) | 8 GB VRAM, or CPU + 16 GB RAM (slow, fine for a daily batch) |
| Small VLM for screenshots (7B class, quantized) | 8-16 GB VRAM; CPU possible but tens of seconds per step |
| Larger, more accurate VLM (32B+) | 24 GB+ VRAM or multiple GPUs |

Ways to cut the requirement: use the VLM only at decision points, lower screenshot resolution, quantize (4-bit),
keep scripts + text LLM for everything that does not need pixels, and accept slow batch runs (the agent is daily,
not interactive).

Expectation: open GUI models are usable but behind the best proprietary ones on long tasks. Expect misclicks and
loops; short scoped tasks (search, open result, read page) work far better than 40-step workflows.

### 6.3 Orchestration

A small custom loop (roughly 200 lines) in the style of the existing `agentkit` is likely enough. Agent S (Simular,
open source) is a reference for the planner/grounding split. Check whether `libs/agentkit/agentkit/llm.py` can already
talk to an OpenAI-compatible endpoint; if so, the model swap is configuration.

### 6.4 Search without paid APIs

SearXNG is already used for discovery. A GUI agent driving Google/Bing hits CAPTCHAs and blocks quickly. Treat free
search as best-effort. Also use open sources that need no key: GitHub/GitLab public search (orgs contributing to
AOSP), job-board pages and RSS, company sitemaps, Hacker News "Who is hiring", conference sponsor lists, Common Crawl.

## 7. Authentication, cookies, downloads, files

- Browser profile: throwaway (`--user-data-dir` on tmpfs) per session. A persistent profile only for a dedicated agent
  identity in a named volume. **Never** reuse a personal Chrome profile or logged-in sessions.
- Logins: dedicated low-privilege agent accounts. The harness performs login (inject credentials/cookies at session
  start); passwords do not pass through the model. MFA and CAPTCHAs need a human handoff via noVNC.
- Downloads: scratch volume, type/size allowlist, malware scan (ClamAV), never opened on the host.
- Uploads: explicit allowlisted files only.
- No host mounts, no Docker socket, no `--privileged`, no host networking, debug/VNC ports not exposed off-box.

## 8. Risks and limitations

1. **Prompt injection** (largest risk). Any page can contain text trying to redirect the agent. Mitigate: treat page
   content as data, domain allowlists for sensitive actions, human approval for irreversible actions (sending
   messages, submitting forms with personal data), nothing valuable in the session. Small open models are more
   susceptible, so the policy layer matters more.
2. **Bot detection is not solved by human-like input.** Sites also look at IP reputation (datacenter ranges are
   penalised), TLS/HTTP fingerprints, browser/device fingerprints (Xvfb/VM without a real GPU looks unusual), fresh
   profiles with no history, request rates and session-level behaviour. A VM or container is itself a signal.
   Model-driven timing and pointer paths are measurable. Do not design the project on the assumption that GUI input
   avoids detection, and do not build features whose purpose is to defeat a site's defences.
3. **CAPTCHAs/challenges**: hand off to a human; no automated solving.
4. **Terms of service and law**: sites such as LinkedIn and Google prohibit automated access. GUI input is still
   automated access. Prefer official APIs, licensed data, or human-performed logged-in steps; respect robots.txt and
   rate limits; stay consistent with the existing `DO_NOT_SCRAPE` list.
5. **Cost and latency**: each step is a model call with an image (seconds to tens of seconds on modest hardware).
6. **Accuracy**: misclicks, wrong scroll distance, layout shifting between screenshot and click, small text. Errors
   compound over long tasks; need step limits, loop detection, checkpoints, and a kill switch.
7. **Text reliability**: reading text from pixels is less reliable than structured text. Use keyboard select-all +
   copy with a clipboard read in the executor (still GUI-level, no DOM access), plus OCR (Tesseract/PaddleOCR).
8. **Container/browser escape**: mitigated by VM/dedicated machine, patched Chrome, regular image rebuilds.
9. **Data exfiltration**: egress filtering and keeping secrets out of the session.
10. **Resources**: about 1-2 GB RAM per Chrome + Xvfb session; plan concurrency.

## 9. Implementation plan

Each phase has an exit criterion; do not start the next phase until it is met.

### Phase 0: Decide scope and hardware (1 day)

1. List which leads currently fail because of site structure (no email, contact form, JS) after the Playwright work.
   This is the addressable set for a GUI agent.
2. Record available hardware (CPU, RAM, GPU/VRAM) for the model server and for the sandbox machine.
3. Decide the isolation tier from section 5 for the prototype (suggested: dedicated spare machine or VM).

Exit: written list of target tasks (3-5 concrete ones) and chosen hardware/isolation.

### Phase 1: Isolated desktop with Chrome (2-3 days)

1. Create the sandbox (VM or spare machine): Ubuntu, non-root user, no personal data.
2. Install Xvfb (or use the VM display), openbox, Chrome/Chromium, xdotool, scrot, noVNC (localhost only).
3. Network: separate network, egress proxy (Squid or similar) that denies RFC1918, host, and metadata addresses.
4. Launch Chrome with a throwaway profile, fixed resolution, no remote-debugging flag.
5. Verify manually via noVNC: browse, type, click, scroll, and confirm the sandbox cannot reach the LAN or host.

Exit: a human can use the sandboxed Chrome remotely; isolation checks pass (cannot ping/curl LAN or host).

### Phase 2: Executor service (2-3 days)

1. Small HTTP service in the sandbox with the fixed action set (section 4), token-authenticated, bound to localhost or
   VPN only.
2. Implement screenshot (with downscale and scale factor), click, double-click, type, key, scroll, wait, clipboard.
3. Return a post-action screenshot with every action; add timeouts and a per-session action cap.
4. Unit/integration tests against a local static test page (known button positions).

Exit: scripted sequence of actions completes the test page reliably (>95% over repeated runs).

### Phase 3: Model server and spike (3-5 days)

1. Serve a candidate VLM (UI-TARS-1.5 or Qwen-VL) with vLLM or llama.cpp on the chosen hardware.
2. Write the minimal agent loop: screenshot -> model -> parse action -> executor -> repeat, with step limit and
   loop detection.
3. Run 10 short tasks (search a query, open the third result, read the heading, scroll to the footer, find a contact
   link). Record success rate, steps per task, seconds per step, failure types.
4. Compare at least two models/quantizations.

Exit: decision on model and on whether accuracy is adequate. If success on short tasks is under about 70%, stop and
re-evaluate (smaller scope, better hardware, or drop the GUI mode).

### Phase 4: Policy, safety and logging (3-4 days)

1. Domain allowlist/denylist (reuse `DO_NOT_SCRAPE`), per-task step and time budgets, kill switch.
2. Approval gate: any irreversible action (form submit, send, purchase, login with credentials) pauses for human
   approval via noVNC or a review queue.
3. Prompt-injection hardening: system prompt marks page content as untrusted data; reject actions that leave the
   task's domain scope; log and flag suspicious page instructions.
4. Persist every action and screenshot (with retention limit) for audit.
5. Credentials handled by the harness only; confirm no secrets appear in model prompts or logs.

Exit: red-team test pages (hidden "ignore instructions" text, fake login, off-domain redirects) do not cause
out-of-scope actions.

### Phase 5: Integrate with the lead pipeline (3-5 days)

1. Add an optional `agent/gui.py` mode behind a config flag, called only when the scripted/Playwright path fails for a
   lead (no contact found, form not fillable by `formfill.py`).
2. Output goes through the same checks as today: guard that extracted contacts appear on the page and on the
   company's domain; results saved to the existing tables; human review before any send.
3. Add to `config/sources.yaml`/settings: enable flag, endpoint URL, budgets.
4. Add tests (mock executor and mock model) to `agent/tests/`.

Exit: end-to-end run on 20 previously failing leads; report how many became usable.

### Phase 6: Evaluate and decide (1-2 days)

1. Measure: usable-lead yield versus scripted-only, cost (hardware time), failure and approval-gate stats.
2. Review ToS/compliance for each site class the agent touched.
3. Decide: keep as fallback, expand, or retire.

Exit: written go/no-go with numbers.

Total estimate: about 3-4 weeks of part-time work for a controlled fallback mode.

## 10. Open questions

1. What hardware is available for the model server (GPU/VRAM)? This decides the model size and speed.
2. Which sites do we actually need the GUI agent on? If they are mostly sites we may not scrape (LinkedIn, Google),
   the plan changes.
3. Is a dedicated spare machine available, or do we use a VM on the existing box?
4. Who approves outreach and form submissions, and where (noVNC, review queue, Slack)?
5. Does `agentkit` already support an OpenAI-compatible local endpoint for the VLM?

## 11. Recommendation

Build the scripted pipeline plus text LLM as the primary path (already underway). Add the GUI agent as an optional,
policy-gated fallback after Phase 3 shows the chosen open model is accurate enough on short tasks. Keep isolation,
approval gates and audit logging non-negotiable, and treat bot detection as a business constraint, not a problem to
engineer around.

---

# Appendix A: Step-by-step setup guide (Phases 1-3)

Target: Ubuntu 22.04/24.04 on the **sandbox machine** (a dedicated spare box is best). The model server can be the same
machine or a different one (Part F). Commands were written from the official install procedures but have not been run
on this repo's hardware; check each step's output before moving on.

Two machines are assumed below. If you only have one, run both parts on it, but then the isolation in Part D matters
even more.

| Name | Role |
|---|---|
| `sandbox` | Runs Docker, the desktop container, Chrome |
| `modelbox` | Runs the VLM/LLM server (needs the GPU, if any) |

## Part A: Prepare the sandbox machine

1. Fresh Ubuntu, a normal admin user for you, nothing personal on it. Update and enable automatic security updates:

   ```bash
   sudo apt update && sudo apt -y upgrade
   sudo apt -y install unattended-upgrades ufw curl ca-certificates git
   sudo dpkg-reconfigure -plow unattended-upgrades
   ```

2. SSH access with keys only (so you never need to sit at the box):

   ```bash
   sudo apt -y install openssh-server
   # from your own computer:  ssh-copy-id <user>@<sandbox-ip>
   sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
   sudo systemctl restart ssh
   ```

3. Host firewall: allow only SSH in. (The container's ports are bound to localhost and reached through SSH tunnels, so
   nothing else needs to be opened.)

   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw allow OpenSSH
   sudo ufw enable
   ```

4. Put the sandbox on its own network segment or guest Wi-Fi/VLAN so it cannot reach other devices on your LAN. This is
   done on the router/switch, not on the machine. Without it, rely on Part D's firewall rules.

## Part B: Install Docker

Use Docker's official apt repository (Ubuntu's `docker.io` package also works if your release is too new for Docker's
repo).

```bash
# remove old/conflicting packages (ok if none are installed)
sudo apt-get remove -y docker.io docker-doc docker-compose podman-docker containerd runc 2>/dev/null || true

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
 | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo systemctl enable --now docker
sudo docker run --rm hello-world        # verify
```

If `apt-get update` says the repo has no release for your Ubuntu version, fall back to `sudo apt-get install -y docker.io`.

Do **not** add the agent's account to the `docker` group: that group is root-equivalent. Run docker with `sudo`, or
only add your own admin user on this dedicated box.

## Part C: Build the desktop image

Create a working folder (suggested location in the repo: `sandbox/`, or keep it on the sandbox machine only):

```bash
mkdir -p ~/agent-desktop && cd ~/agent-desktop
```

`Dockerfile`:

```dockerfile
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb openbox x11vnc xdotool scrot xclip imagemagick \
      python3 python3-pip python3-venv curl wget ca-certificates \
      fonts-liberation fonts-noto fonts-noto-color-emoji dbus-x11 \
    && rm -rf /var/lib/apt/lists/*

# Chrome must be the Google .deb (Ubuntu's chromium package is a snap and does not work in containers).
RUN wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update && apt-get install -y /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1500 -s /bin/bash agent
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER agent
WORKDIR /home/agent
ENTRYPOINT ["/entrypoint.sh"]
```

`entrypoint.sh` (starts the virtual display, window manager, VNC for human supervision, and Chrome):

```bash
#!/bin/bash
set -e
export DISPLAY=:99
RES="${SCREEN_RES:-1280x800x24}"

Xvfb :99 -screen 0 "$RES" -nolisten tcp &
sleep 1
openbox &

mkdir -p ~/.vnc
x11vnc -storepasswd "${VNC_PASSWORD:?set VNC_PASSWORD}" ~/.vnc/passwd
x11vnc -display :99 -rfbauth ~/.vnc/passwd -forever -shared -listen 0.0.0.0 -rfbport 5900 -quiet &

# Chrome as a normal desktop app: no remote-debugging port, throwaway profile.
exec google-chrome --user-data-dir=/tmp/chrome-profile --no-first-run --no-default-browser-check \
     --window-position=0,0 --window-size=1280,800 --disable-features=Translate
```

Build:

```bash
echo 'VNC_PASSWORD=change-this-long-random-value' > vnc.env && chmod 600 vnc.env
sudo docker build -t agent-desktop .
```

Notes:

- If you want the browser in a browser, `apt install novnc websockify` in the image and run
  `websockify --web /usr/share/novnc 6080 localhost:5900`. Plain VNC over an SSH tunnel is simpler and safer to start.
- The executor service (Phase 2) is added to this image later; it is not needed to prove the desktop works.

## Part D: Isolated network and hardened run

### D1. A dedicated Docker network

```bash
sudo docker network create --subnet 172.30.0.0/24 agent-net
```

### D2. Egress rules: internet yes, LAN/host/metadata no

Docker evaluates the `DOCKER-USER` chain before its own rules, so rules inserted there apply to container traffic.

```bash
SUBNET=172.30.0.0/24
for NET in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
  sudo iptables -I DOCKER-USER -s $SUBNET -d $NET -j DROP
done
# traffic from the container to the host itself (docker gateway) goes through INPUT, not DOCKER-USER:
sudo iptables -I INPUT -s $SUBNET -m conntrack --ctstate NEW -j DROP

sudo apt -y install iptables-persistent && sudo netfilter-persistent save
```

Notes: this makes the container use public DNS directly (see `--dns` below) because the host's DNS stub would be
blocked. If ufw rewrites rules after a reboot, re-check with `sudo iptables -S DOCKER-USER`. If your Docker uses
nftables only, express the same rules in nftables.

A stricter, better version is to force all traffic through an allow/deny proxy (Squid) so domains can be restricted;
add that in Phase 4.

### D3. Seccomp profile so Chrome's own sandbox can work

Docker's default seccomp profile blocks the user-namespace calls Chrome's sandbox uses. Download a Chrome-compatible
profile, review it, and use it instead of turning the sandbox off:

```bash
# widely used profile for running Chrome in Docker (review before using):
curl -fsSLo chrome.json https://raw.githubusercontent.com/jessfraz/dotfiles/master/etc/docker/seccomp/chrome.json
```

If Chrome still refuses to start, the fallback is adding `--no-sandbox` to the Chrome flags in `entrypoint.sh`. That
removes one defence layer, so only do it when the container itself sits inside the isolated machine/VM of Part A.
Never use `--privileged` or `--cap-add SYS_ADMIN` as a shortcut.

### D4. Run

```bash
sudo docker run -d --name agent-desktop \
  --network agent-net --dns 1.1.1.1 --dns 9.9.9.9 \
  --env-file vnc.env \
  --cap-drop ALL --security-opt no-new-privileges --security-opt seccomp=chrome.json \
  --read-only --tmpfs /tmp:rw,size=1g --tmpfs /home/agent:rw,uid=1500,gid=1500,size=512m \
  --shm-size 2g --memory 4g --cpus 2 --pids-limit 512 \
  -p 127.0.0.1:5900:5900 \
  agent-desktop
sudo docker logs -f agent-desktop        # Ctrl-C to stop following
```

Only `127.0.0.1` is published: nothing is reachable from the network except through SSH.

### D5. See it (human supervision)

From your own computer:

```bash
ssh -L 5900:127.0.0.1:5900 <user>@<sandbox-ip>
# then open any VNC viewer (Remmina, TigerVNC, RealVNC) at localhost:5900 with the VNC_PASSWORD
```

You should see an empty desktop with Chrome open.

## Part E: Verify isolation and input (Phase 1 exit)

1. Internet works, LAN/host/metadata do not:

   ```bash
   sudo docker exec agent-desktop curl -sS -m 5 -o /dev/null -w "%{http_code}\n" https://example.com     # expect 200
   sudo docker exec agent-desktop curl -sS -m 5 http://169.254.169.254/ ; echo "exit=$?"                # expect failure
   sudo docker exec agent-desktop curl -sS -m 5 http://<a-LAN-device-ip>/ ; echo "exit=$?"              # expect failure
   sudo docker exec agent-desktop curl -sS -m 5 http://172.30.0.1/ ; echo "exit=$?"                     # host gateway: expect failure
   ```

2. Virtual mouse, keyboard and screenshot work (these are the primitives the executor will wrap):

   ```bash
   sudo docker exec -e DISPLAY=:99 agent-desktop xdotool mousemove 640 400 click 1
   sudo docker exec -e DISPLAY=:99 agent-desktop xdotool key ctrl+l
   sudo docker exec -e DISPLAY=:99 agent-desktop xdotool type --delay 80 "example.com"
   sudo docker exec -e DISPLAY=:99 agent-desktop xdotool key Return
   sudo docker exec -e DISPLAY=:99 agent-desktop scrot -o /tmp/shot.png
   sudo docker cp agent-desktop:/tmp/shot.png ./shot.png     # open it: should show example.com
   ```

3. Container is hardened:

   ```bash
   sudo docker exec agent-desktop id                 # uid=1500(agent), not root
   sudo docker inspect agent-desktop --format '{{.HostConfig.Privileged}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}'
   ```

Exit criterion for Phase 1: all four egress checks behave as expected, the screenshot shows the page that was typed
into the address bar, and you can watch it over VNC.

## Part F: Model server (`modelbox`)

Keep this separate from the sandbox. The sandbox container never needs a GPU, and the model server never needs to
accept connections from the internet.

1. NVIDIA driver (if there is an NVIDIA GPU):

   ```bash
   sudo ubuntu-drivers autoinstall && sudo reboot
   nvidia-smi                       # after reboot: shows the GPU and VRAM
   ```

2. Pick one serving option.

   **Option 1, Ollama (simplest, supports quantized models, CPU fallback):**

   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ollama pull qwen2.5vl:7b          # or another vision model from the Ollama library
   ollama run qwen2.5vl:7b "describe this" --      # quick sanity check
   # API: http://localhost:11434 (OpenAI-compatible at /v1)
   ```

   **Option 2, vLLM (better throughput, needs a CUDA GPU):**

   ```bash
   sudo apt -y install python3-venv
   python3 -m venv ~/vllm-env && source ~/vllm-env/bin/activate
   pip install vllm
   vllm serve Qwen/Qwen2.5-VL-7B-Instruct --max-model-len 8192 --port 8000
   # API: http://localhost:8000/v1 (OpenAI-compatible)
   ```

   Model IDs and quantization support change often. For the GUI-specialist model, check the current Hugging Face page
   for UI-TARS-1.5 and its recommended prompt/action format before using it; the exact prompt format is part of the
   Phase 3 spike.

3. Do not expose the port to the internet. If `modelbox` is a different machine, bind to the private interface and
   reach it over SSH tunnel or VPN (for example WireGuard):

   ```bash
   ssh -N -L 8000:127.0.0.1:8000 <user>@<modelbox-ip>     # run on the orchestrator host
   ```

4. Verify with an image (replace the model name as served):

   ```bash
   curl -s http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
     "model": "Qwen/Qwen2.5-VL-7B-Instruct",
     "messages": [{"role":"user","content":[
        {"type":"text","text":"What website is shown?"},
        {"type":"image_url","image_url":{"url":"data:image/png;base64,'"$(base64 -w0 shot.png)"'"}}]}],
     "max_tokens": 100}'
   ```

   The answer should mention example.com. That proves the screenshot to model path works.

If there is no GPU, use Ollama with a small quantized model and expect tens of seconds per step. That is acceptable for
a daily batch but not for interactive use.

## Part F2: Phase 2, the executor service (implemented in `sandbox/`)

Files:

| File | Purpose |
|---|---|
| `sandbox/executor.py` | The service. Standard library only. Runs inside the container as the `agent` user |
| `sandbox/Dockerfile`, `sandbox/entrypoint.sh` | Image with the desktop, Chrome and the executor (supersedes the Part C files) |
| `sandbox/testpage.html` | Static page with large click/type/scroll targets; reports state in the window title |
| `sandbox/tests/test_executor.py` | 30 unit and HTTP tests with a fake command runner (no desktop needed). Run by `make test` |
| `sandbox/smoke_check.py` | Phase 2 exit check against the real desktop, repeated N times |

API (JSON over HTTP, `Authorization: Bearer <EXECUTOR_TOKEN>`, port 8765):

| Call | Purpose |
|---|---|
| `POST /action` `{"action": ...}` | One of: `screenshot`, `click`, `double_click` (`x`,`y`,`button`), `mouse_move`, `type` (`text`), `key` (`key`, e.g. `ctrl+l`, `Return`), `scroll` (`direction`,`amount`, optional `x`,`y`), `wait` (`seconds`), `read_clipboard` |
| `POST /reset` | Reset the per-session action counter |
| `GET /health` | Liveness, no token needed |
| `GET /window_title` | Focused window title. For the harness and tests only, not a model action |

Behaviour:

- Every action returns `{ok, action, actions_used, screenshot (base64 PNG), width, height}`.
- Coordinates are in screenshot space and are scaled to screen pixels (set `SHOT_WIDTH` below `SCREEN_RES` width to
  send smaller images to the model).
- Input is validated: coordinates in range, text at most 2000 characters, key names restricted to `[A-Za-z0-9_+-]`,
  scroll amount 1 to 30, body at most 64 KB. Commands run as argument lists, never through a shell.
- Per-session cap (`MAX_ACTIONS`, default 200) returns HTTP 429. Bad input returns 400, auth failure 401, a failed
  desktop command 500.
- The service refuses to start unless `EXECUTOR_TOKEN` is at least 24 characters.
- Actions are serialized: the desktop has one mouse and one keyboard.
- Logged to `docker logs`: action name and counter only, not typed text (it could contain credentials).

Deploy (after the Part D network and egress rules exist):

```bash
cd sandbox
printf 'VNC_PASSWORD=%s\nEXECUTOR_TOKEN=%s\n' "$(openssl rand -hex 16)" "$(openssl rand -hex 24)" > vnc.env
chmod 600 vnc.env
sudo docker build -t agent-desktop .
# same run command as Part D4, plus:   -p 127.0.0.1:8765:8765
```

Run the exit check from your own computer through an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 <user>@<sandbox-ip>
export EXECUTOR_TOKEN=<value from vnc.env>
python3 sandbox/smoke_check.py 20       # needs >= 95% of runs to pass every step
```

Exit criterion for Phase 2: `smoke_check.py` reports at least 95% passed over repeated runs on the real desktop.
The unit tests pass, but the desktop run has to be done on the sandbox machine.

## Part G: Next steps after the setup works

1. Phase 2: add the executor service (small authenticated HTTP API wrapping the xdotool/scrot commands from Part E)
   into the image, published only on `127.0.0.1` like the VNC port.
2. Phase 3: write the loop (screenshot -> model -> action -> executor) against the Part F endpoint and run the 10-task
   spike.
3. Phase 4: replace the plain egress rules with a Squid allow/deny proxy, add approval gates and logging.

## Alternative: KVM VM instead of Docker

Use this if the sandbox must have a separate kernel (stronger isolation). Replaces Parts B-D.

```bash
sudo apt -y install qemu-kvm libvirt-daemon-system libvirt-clients virt-manager bridge-utils cpu-checker
kvm-ok                                  # expect "KVM acceleration can be used"
sudo adduser $USER libvirt && newgrp libvirt
# create an Ubuntu Desktop VM (4 vCPU, 8 GB RAM, 40 GB disk) with virt-manager, install Chrome (.deb) inside,
# use a NAT network and apply the same egress rules on the libvirt bridge (virbr0).
```

Inside the VM: normal desktop session (or Xvfb), `sudo apt install xdotool scrot`, Google Chrome `.deb`. Input and
screenshots can be driven from the host through the VM's VNC/SPICE console or QEMU QMP (`input-send-event`,
`screendump`) so nothing runs inside the guest at all. This is more work but the strongest boundary.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Chrome exits immediately in the container | Sandbox/seccomp: check `docker logs`; use the Part D3 profile; last resort `--no-sandbox` inside an isolated machine |
| Chrome crashes on heavy pages | `--shm-size` too small; raise to 2g or more |
| Blank VNC screen | Xvfb did not start; check `docker logs`, remove stale `/tmp/.X99-lock` |
| `Xvfb` warns about `/tmp/.X11-unix` | Harmless on a tmpfs `/tmp`; create it in the entrypoint if it becomes an error |
| Container cannot resolve names | DNS to the host stub is blocked by design; keep `--dns 1.1.1.1` |
| Egress rules vanish after reboot | Persist with `netfilter-persistent save`; re-check ufw interaction |
| Clicks land in the wrong place | Resolution or zoom changed; keep 1280x800 and 100% zoom, scale coordinates if screenshots are downscaled |
| `ollama`/`vllm` out of memory | Smaller model or lower quantization, smaller `--max-model-len`, or fewer images per request |
