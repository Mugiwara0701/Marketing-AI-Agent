# Scheduling the daily run

The agent is not a service. A systemd timer starts `python -m agent run` once a day; it runs for at most
`AGENT_MAX_MINUTES` (default 120), saves everything to the database and exits.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/aosp-agent.service deploy/aosp-agent.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aosp-agent.timer
systemctl --user list-timers aosp-agent.timer        # next run
journalctl --user -u aosp-agent.service -n 100       # last run's JSON logs
```

Edit `WorkingDirectory`/`ExecStart` in the service if the repo is not in `~/Marketing-AI-Agent`.
`loginctl enable-linger $USER` lets the timer fire when you are logged out. Cron alternative:
`30 9 * * *  /path/to/Marketing-AI-Agent/deploy/run-daily.sh`.

The LLM host (vLLM) must be up when the timer fires; the run fails fast and records the error in
`agent_runs` if it is not.

## Approving emails

Nothing is sent without approval. After a run:

```bash
python -m agent review              # drafted emails, with [CHECK: ...] where a quality check flagged one
python -m agent show <id>
python -m agent approve <id> ...     # or --all
python -m agent reject <id> ...
```

Approved emails are sent at the start of the next daily run (or now with `python -m agent send`).

## Web search (SearXNG, free)

Without it the agent only reads the job feeds (about 20 listings a day). SearXNG adds open-web search.

```bash
sudo usermod -aG docker $USER      # once; then log out and back in (fixes "permission denied" on the docker socket)
cd deploy/searxng && docker compose up -d
curl -s 'http://127.0.0.1:8888/search?q=aosp+bsp+engineer&format=json' | head -c 300   # should print JSON
echo 'SEARXNG_URL=http://127.0.0.1:8888' >> ../../.env
python -m agent check              # "Web search (SearXNG)" should say OK
```
