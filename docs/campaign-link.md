# Link to the Campaign Agent

The Campaign Agent (CA) decides which campaign this agent runs and hears about blockers. The two machines talk directly over
Tailscale; this agent only makes outgoing calls, nothing listens here. The full protocol, the Tailscale setup and the CA side
are in the Campaign Agent repository: `docs/ca-ma-link.md` and `docs/tailscale-setup.md`.

Code: `agent/campaign_link.py`. Hooks: `agent/leadgen/service.py` (`run_leads` applies the campaign for the whole pass),
`agent/supervisor.py` (starts the event sender), `agent/control.py` (a failed pass is a blocker).

## Switching it on

Off unless both keys are set; then the agent behaves exactly as before. In `.env` on the office machine (sent with
`bash deploy/remote.sh push-secrets`):

| Key | Default | Meaning |
|---|---|---|
| `MA_LINK_URL` | none | `http://campaign-agent.<tailnet>.ts.net:8787/ma/v1` (the full Tailscale name, no IP address) |
| `MA_LINK_TOKEN` | none | The same secret as `MA_LINK_TOKEN` on the CA machine |
| `MA_LINK_CACHE_DIR` | `.cache/campaign` | `active.json` (last good campaign), `outbox.jsonl` (events waiting), `state.json` |
| `MA_LINK_TIMEOUT_S` | `10` | Per call |
| `MA_LINK_SEND_EVERY_S` | `5` | How often queued events are sent |

## What it does

- **Start of every discovery pass:** asks the CA what is live. The text must match its sha256, pass a shape check (only the
  campaign-owned keys of `config/leadgen.yaml`, with the right types) and is merged over the base config (same rule as the CA:
  a key replaces, exclusion lists are added to). It is used for the whole pass. A campaign can never set `contacts`,
  `desktop_engines`, anything about approval or sending: those keys are refused.
- **Anything goes wrong** (CA off, wrong token, bad hash, refused key): the last good campaign keeps running, or the base config
  if there is none. Nothing is half-applied. The reason is queued as an event and shown to people.
- **Nothing live:** the base config, as today. The cache is forgotten, so a campaign the CA stopped does not come back when the
  link later fails.
- **Events** go to a local outbox and are sent in the background with retries (1 s growing to 60 s), so they survive a CA or
  network outage. The outbox holds 1000 events; `info` ones are dropped first. A critical event is also posted to Slack
  (`SLACK_ALERTS_WEBHOOK`), and so is a refused token, because the link itself is then what is broken.
- **Status row:** after each pickup the agent writes `ma_campaign_status` (what it runs, or the version it refused and why),
  best effort: no database or no table is logged and ignored.

## Events sent today

`MA_CAMPAIGN_APPLIED`, `MA_CAMPAIGN_REJECTED`, `MA_CAMPAIGN_HASH_MISMATCH`, `MA_PICKUP_FAILED` and `MA_PASS_FAILED`. Others
(Chrome, model, search, Slack, Gmail failures) are listed in the CA's `docs/ca-ma-link.md` and need `campaign_link.emit(...)` calls at
their failure points. Never put secrets or page contents in a message or in `details`.

## Not done

The Supabase fallback for pickup (this agent's database role has no grant on the CA's tables), the strict versioned loader
(`check_overlay` is a stand-in), `agent leads queries --campaign`, and early notice by long poll.

## The complete file from the CA

A new CA sends `leadgen_yaml` (the whole leadgen file), its hash and `base_sha256` next to the overlay. The agent decides what to run:

1. Hash of the complete file wrong, or the text is not a mapping: not used.
2. The complete file must **equal this machine's `config/leadgen.yaml` plus the campaign** (the same merge the agent always did). Any difference,
   even one value, means the CA's copy of the base is out of date: not used, and the first differing key is named.
3. Equal: **the CA's file runs**, and a readable copy is written to `.cache/campaign/leadgen.yaml`.
4. Not used (1 or 2): the campaign is applied on this machine's own base file, and the CA gets one `MA_BASE_DRIFT` blocker (closed when the files match
   again). The pass is never run on stale lists.

An older CA without `leadgen_yaml` works as before. `state.json` records the mode (`complete`, `overlay_fallback`, `overlay`); each pass tells the CA which
(`MA_PASS_CAMPAIGN`). To check by hand: compare `.cache/campaign/leadgen.yaml` with `config/leadgen.yaml`: only the campaign's settings differ.
