# Running DOURMOUSE in the cloud (so it doesn't need to be open)

This is the honest state of "run in the cloud" as of this doc, split into
what already works today, what this change adds, and what only you can do
(provisioning, accounts, secrets — none of that is something an agent should
do on your behalf).

## The reality check, first

**Does DOURMOUSE already run headless, reachable remotely via Tailscale,
with no window needing to stay open? Partially — the pieces exist but were
never wired into a persistent background service.**

- `dourmouse/webui.py` is a stdlib-only `ThreadingHTTPServer` (no Flask, no
  GUI toolkit in its own import chain — see its module docstring). It is
  genuinely headless-capable. `start.sh` (the Linux/generic launcher)
  already runs it exactly this way: `exec ./.venv/bin/python -m
  dourmouse.webui`. No window is created by that path.
- `docs/tailscale.md` already documents real, working remote access: set
  `DOURMOUSE_HOST=0.0.0.0` + `DOURMOUSE_ACCESS_TOKEN`, and any device on your
  tailnet reaches the dashboard at `http://<tailnet-ip>:8765/`. This is not
  new — it's shipped and described in this repo already.
- **But** the macOS launcher you actually double-click, `start.command`,
  wraps the same server in `dourmouse.desktop` and opens a native `pywebview`
  window — that's the "has to stay open" experience prompting this request.
  And on any platform, nothing in this repo today keeps `dourmouse.webui`
  (or the desktop wrapper) running in the background across a reboot or a
  crash without you manually re-launching it. `.dourmouse-ui.pid` +
  `stop.command` are a manual start/stop pair, not a supervised service.
- The only *scheduled/background-service* pattern that exists in this
  codebase (`schtasks` in `atlas-strategy-lab/docs/DELL_NODE.md`) is for a
  **different** trio of processes — `relay/relay_server.py`,
  `relay/agent_bridge.py`, `relay/autonomous_worker.py`, the ATLAS relay
  fabric on the Dell node — not for `dourmouse.webui`, the actual dashboard
  this request is about. There is no existing Windows Scheduled Task, no
  systemd unit, and no Docker packaging for the dashboard server itself
  before this change.

So: the *server* was already headless-capable and the *remote-access story*
was already real, but "runs unattended in the background, survives a reboot,
restarts itself after a crash" did not exist for the dashboard. That's the
actual gap this change fills — with tooling, not by inventing new product
behavior in `dourmouse/webui.py` itself (no Python file was touched).

## What this change adds

| File | What it is |
|---|---|
| `Dockerfile` | Headless server image: `python:3.12-slim` + `requirements.txt` + `dourmouse/` + `ui/`. Entry point is the unmodified `python -m dourmouse.webui`. |
| `.dockerignore` | Keeps the ~9GB of `jarvis/`, `dist/`, etc. out of the build context (see "what can't move" below). |
| `docker-compose.yml` | One service, a named volume for `/data` (session transcripts + the long-term memory DB), a healthcheck against `/`. |
| `dourmouse.service` | A systemd unit — the Linux equivalent of the `schtasks` pattern in `DELL_NODE.md`, but for the dashboard, and with the specific orphaned/duplicate-process discipline this codebase already documented hitting once (`atlas-strategy-lab/docs/OPERATIONS_RUNBOOK.md` §4.3/§7) — see the comments in the unit file for exactly how systemd's cgroup tracking closes that gap. |

None of this was build- or run-tested in the environment it was written in
(no Docker daemon, no spare Linux box available there). Run `docker build`,
`docker compose up`, and a real `systemctl start` yourself before trusting
either path in production. The files are written from a careful read of
`dourmouse/webui.py`'s actual import chain and runtime file layout (verified
which optional dependencies — voice, MetaTrader5, `atlas_terminal` — are
genuinely try/except-guarded lazy imports and which aren't), not templated
blindly.

## What you still have to do yourself

Nothing here creates an account, signs you up for hosting, or spends money.
That's deliberate — an agent should not do any of this on your behalf.

1. **Rent (or claim a free tier of) a small Linux VPS**, or pick a
   container-runner service that will accept the Dockerfile. Anything with
   ~1–2 GB RAM and a couple GB disk is enough for the dashboard itself (the
   ATLAS quant/data features it can call into want more, per
   `atlas-strategy-lab/docs/DELL_NODE.md`'s own 8GB-RAM verdict on the Dell
   node — treat that as a floor, not this dashboard's ceiling).
2. **Put the repo there** (`git clone`, or build the Docker image from a
   copy) and create `.env` (bare-metal/systemd) or `.env.cloud`
   (docker-compose) from `.env.example`. You do **not** need most of
   `.env.example`'s ~250 lines — the desktop-only sections (pywebview
   window geometry, MT5, IBKR paper trading, voice) are irrelevant to a
   headless cloud dashboard. At minimum you need:
   - An LLM backend: `NVIDIA_API_KEY` (the primary backend per
     `dourmouse/backend_fallback.py`) **or** `OLLAMA_BASE_URL` pointed at a
     reachable Ollama instance. Read `backend_fallback.py` before assuming
     the fallback saves you here: on a fresh VPS with no local Ollama, the
     silent fallback target (`127.0.0.1:11434`) won't exist either, so the
     primary backend key becomes effectively required, not optional.
   - `DOURMOUSE_ACCESS_TOKEN` — a long random string
     (`openssl rand -hex 24`), **required** the moment this is reachable
     beyond `127.0.0.1`. Without it the app serves anyway with a loud
     warning printed at startup — don't rely on that.
   - `DOURMOUSE_HOST=0.0.0.0` (systemd path only — the Docker image already
     sets this at the container level; whether it's reachable from outside
     still depends on what you do in step 4).
3. **Install Docker** (if using that path) or **Python 3.10+** and run
   `setup.sh` (if using the systemd path) on the box.
4. **Choose how you reach it remotely** — this is the actual "so it doesn't
   need to open [on your Mac]" part:
   - **Tailscale (recommended, matches `docs/tailscale.md`)**: install
     Tailscale on the VPS, sign into the same tailnet as your other
     devices. Nothing needs to be publicly exposed — reach
     `http://<vps-tailnet-ip>:8765/` from any device on your tailnet, same
     as the existing doc describes for your Mac.
   - **Tailscale Funnel**, if you specifically want a public HTTPS URL
     (e.g. to hand someone off-tailnet a link): `tailscale funnel 8765` on
     the VPS. This is a genuinely public URL — the
     `DOURMOUSE_ACCESS_TOKEN` gate is then your only defense, so treat it
     as seriously as any other public server's auth.
   - **A domain + reverse proxy (Caddy/Nginx) + your own TLS cert**, if you
     want a stable custom hostname instead of a tailnet/funnel address.
     This is more moving parts and more surface area than Tailscale for no
     functional gain unless you specifically need a branded URL.

   `docs/tailscale.md`'s own warning still applies here: do **not** front
   this with a generic public tunnel (ngrok/Cloudflare Quick Tunnel/etc.)
   without at least the access token — that doc calls this out for the Mac
   case and the reasoning is identical on a VPS.
5. **Start it and verify**: `docker compose up -d` or
   `sudo systemctl enable --now dourmouse`, then hit `/` from another
   device on the tailnet and confirm the ACCESS GATE prompts for the token.

## What genuinely cannot "move to the cloud" without you re-provisioning
infrastructure

- **The 500-agent JARVIS network living on the desktop's `D:` drive.** The
  `jarvis/` directory in *this* checkout alone is already ~4GB, and per
  session memory that's a smaller mirror than what actually lives on the
  desktop's `D:` drive in production. A JARVIS-class deployment is hundreds
  of gigabytes of models/data on dedicated local storage, not something a
  Dockerfile or a small VPS absorbs. Moving that to the cloud is a real
  infrastructure project on its own (large block storage, likely GPU
  instances for anything beyond the light CPU inference `DELL_NODE.md`
  already scoped for that class of hardware) — it is out of scope for what
  "package the dashboard for headless operation" can honestly deliver, and
  nothing in this change attempts it.
- **MetaTrader5 / IBKR Gateway integrations** (`dourmouse/mt5_ops.py`,
  `atlas-strategy-lab/docs/IBKR_PAPER_SETUP.md`) assume a running desktop
  trading terminal on the same machine or LAN. Containerizing the dashboard
  doesn't create that terminal in the cloud; those features will report
  "unavailable" honestly (the existing guarded-import pattern) on a VPS
  that doesn't have one, exactly as they would on any machine that never
  installed MetaTrader5.
- **macOS-only voice TTS** (the built-in `say` command, per
  `requirements-voice.txt`'s own comment) obviously isn't available on a
  Linux VPS. The `piper-tts` / `faster-whisper` wheels in
  `requirements-voice.txt` are cross-platform and could be added to the
  Docker image if you want cloud-side voice, but this change doesn't
  install them by default (see the Dockerfile's comments) — voice endpoints
  will report NOT CONFIGURED, same as any machine that skipped that extra.
- **The Dell node's relay/bridge/worker fabric** described in
  `atlas-strategy-lab/docs/DELL_NODE.md` is a separate multi-machine mesh
  (desktop + Mac + Dell, all on one tailnet) with its own `schtasks`
  autostart pattern. This change doesn't touch or replace that — a cloud
  VPS running the dashboard could join that same tailnet as a fourth node
  if you want it in the mesh, but that's an extension of the existing relay
  design, not something `Dockerfile`/`dourmouse.service` do on their own.
