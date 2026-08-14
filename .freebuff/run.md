# Preview run doc — laptop chat dashboard (Agent Relay — Live Chat)

Serves the laptop-side live relay feed at **http://127.0.0.1:8789/**.
No build step, no dependencies beyond stdlib — the server is
`relay/chat_feed.py` in the atlas-strategy-lab repo.

## Reproduce the artifacts

Nothing to build. The only artifact the server needs is its config:

- `relay/relay_config.txt` (git-ignored) must exist with `RELAY_URL`,
  `TOKEN`, `ME`, `DASH_PORT` — copy from `relay/relay_config.example.txt`
  and fill in the real values (never commit).
- The dashboard client gate uses a local dash token (currently
  `laptop-dash-2026`) passed as `--send-token`; `/send` without it returns 401.

## Run the server

From the repo root (`/Volumes/ATLAS /dourmouse-4.0.0/atlas-strategy-lab`):

```bash
python3 relay/chat_feed.py \
  --relay "$(grep RELAY_URL relay/relay_config.txt | cut -d= -f2)" \
  --token "$(grep TOKEN relay/relay_config.txt | cut -d= -f2)" \
  --me laptop-dourmouse --port 8789 --send-token laptop-dash-2026
```

Port 8789 is the project default for the laptop dashboard. The process must
outlive the shell — plain `nohup ... & disown` gets reaped by the Freebuff
runner (process-group kill). **launchd is NOT usable on this Mac**: macOS
System Policy (sandbox) denies launchd-spawned processes file access to the
workspace (kernel log: `deny file-read-data relay_config.txt` /
`deny file-write-data bridge.log`; jobs exit 1 / 126 / EX_CONFIG).

The working recipe is a detached launcher using `start_new_session=True`
(new session = new process group, survives the runner, reparented to init):

```bash
cd /Volumes/ATLAS\ /Atlas/dourmouse-4.0.0 && ./.venv/bin/python .freebuff/start_daemons.py
```

This starts BOTH the bridge (`agent_bridge.py --me laptop-dourmouse`) and the
chat feed (`chat_feed.py --port 8789 --send-token laptop-dash-2026`) with
logs at `.freebuff/bridge.log` and `.freebuff/chat_feed.log`. **Re-run after
every Freebuff restart** (the daemons die with the session).

Then verify: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8789/`
should print 200 before registering. Take the feed pid from
`pgrep -f chat_feed.py` for `register_preview`.
