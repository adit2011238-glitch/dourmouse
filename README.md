# DOURMOUSE // CENTRAL AGENT DISPATCH

A local, text-driven mission-control dashboard for an autonomous dispatch agent
(GENERAL mode). It runs a conversational AI assistant — with real internet
search, web fetching, code execution, file work, scheduling, message drafting,
and an Obsidian vault memory — all behind a Stark-Industries-style telemetry
HUD served from your own laptop. Nothing leaves your machine except the API
calls you make.

Built with the Python standard library + the `openai` SDK. **v4.0 runs fully
local by default**: if Ollama is running (see Requirements), DOURMOUSE uses your
local models with zero API spend and no key; NVIDIA NIM remains a configured
alternative. No Node, no build step, no cloud account.

---

## Quickstart (no jargon, ~3 minutes)

> **What this is:** a personal assistant that runs on *your* computer. You type
> or speak a goal — "sort the invoices in my Downloads folder", "summarise
> today's market news", "every Monday at 9am, check my inbox and draft
> replies" — and it does the steps, showing you each one, asking before
> anything risky.

1. **Install Python 3.10+** (python.org if you don't have it) and, optionally,
   [Ollama](https://ollama.com) for a fully-local brain.
2. **Unzip** the package anywhere. Double-click **`start.command`** (macOS) or
   **`start.bat`** (Windows). It sets itself up — no config files to edit.
3. The dashboard opens. **Type what you want done.** That's it.

**Three things to know before you start:**

- **It can act, not just talk.** It reads/writes files, runs scheduled tasks,
  and can send messages — always shown in the action log, and always asking
  before anything consequential. Watch the log; use the red kill switch to
  stop everything instantly.
- **Your data stays yours.** Conversations, memory, and files live on your
  machine (see `PRIVACY.md`). Nothing phones home. If you use a cloud model
  backend instead of local Ollama, what you type goes to that provider.
- **It learns.** After each session it remembers what it learned, so answers
  improve over time. Set `DOURMOUSE_LEARN=0` to turn that off.

Legal: using this software means you accept `LICENSE` and `EULA.md`; data
handling is described in `PRIVACY.md`.

---

## 0. What's new in v4.0 (self-hosted personal AI OS)

- **Local-first brain (Phase 2)** — `DOURMOUSE_LLM_BACKEND=auto` probes your local
  Ollama (`http://127.0.0.1:11434`) and uses it when live, else falls back to
  NVIDIA. Set it explicitly to `ollama` or `nvidia` to pin one. The dashboard
  header shows which backend is actually thinking, and a new `code_ollama`
  agent routes coding tasks to your local models too.
- **ATLAS command-centre (Phase 3)** — the new `atlas` roster agent reports
  real telemetry from your ATLAS repo: branch, latest commit, dirty state,
  FX bootstrap progress (per-pair bid-day counts), and deliverables. Ask
  "show running projects".
- **Morning report (Phase 11)** — at `DOURMOUSE_REPORT_TIME` (default 08:30) a
  deterministic daily report — news, markets, tasks, ATLAS status, system
  health — is posted to the bus. No LLM in the path, honest failures.
- **Multi-device (Phase 9, v5.13)** — optional `DOURMOUSE_ACCESS_TOKEN` +
  `DOURMOUSE_HOST` let you reach the dashboard from your phone/laptop over a
  Tailscale mesh (see `docs/tailscale.md`). Loopback stays token-free; a login
  page appears for non-local clients. All pages are responsive.
- **Phone link (v5.13)** — `python -m dourmouse.mobile_link` does the whole
  setup in one command: generates (or reuses) the token, writes
  `DOURMOUSE_HOST=0.0.0.0` + the token into `.env` idempotently, detects your
  LAN and Tailscale IPs, and prints a scannable QR per URL. `--rotate` mints a
  fresh token; `--no-write` dry-runs. The unauthenticated `/mobile` pairing
  page renders the same QR codes in the browser (server-side segno SVG),
  always pointing back at the URL the phone actually used, while every
  `/api/*` stays token-gated from non-loopback clients.
- **Premium HUD (Phases 7–8)** — radar sweep core, ambient particle field,
  corner-bracket panels, and a real 5-state motion model
  (idle → thinking → planning → executing → complete) driven by live events.
  Every pixel is CSS/SVG/Canvas2D — still zero external resources.
- **Self-review (Phase 13)** — the `memory` agent's `daily_digest` tool and
  `/api/selfimprove` reduce real bus traffic into per-agent stats and honest
  improvement suggestions. A silent agent is reported as silent, never
  invented as busy.
- **Voice (v4.1, Phase 7)** — fully local speech, zero cloud calls: `[MIC]`
  transcribes your directive with faster-whisper, `[SPK]` speaks the last
  reply with piper (or the macOS built-in `say`). Gate: `DOURMOUSE_VOICE=1`
  + `pip install -r requirements-voice.txt`. Without models the buttons
  degrade to an honest NOT CONFIGURED state — nothing is faked.

---

## 1. Requirements

- **macOS** (this build ships macOS launchers; the engine is cross-platform)
- **Python 3.10+** — the launcher auto-detects Homebrew/system Python. If you
  only have the system Python 3.9, install Python 3.12 first:
  - `brew install python@3.12`, or
  - download from https://python.org
- **Either** a local LLM runtime **or** an NVIDIA key (or both, with `auto`):
  - **Ollama (free, fully local, recommended):** `brew install ollama`,
    `ollama pull qwen3:8b`, then `ollama serve` (or leave the app running).
    No key, no network calls, zero API spend.
  - **NVIDIA API key** (free tier at https://build.nvidia.com) — still used
    when Ollama is down or `DOURMOUSE_LLM_BACKEND=nvidia` is set. Prompts for
    it on first run; never bundled.

## 2. Install & run (double-click)

1. Unzip `dourmouse-<version>.zip` anywhere (Downloads is fine).
2. Double-click **`start.command`**.
   - First run: it creates a private `.venv`, installs dependencies (including
     the desktop extra, `requirements-desktop.txt`), and asks for your NVIDIA
     API key.
   - **The key is validated LIVE first** — a real 1-token NVIDIA call — so an
     invalid, revoked, or inference-restricted key is rejected at the prompt
     with a clear reason (up to 3 attempts) and is never written to `.env`
     (which stays permissions 600). No more silent 401/403s mid-chat.
   - Then it opens **DOURMOUSE in its own native macOS window** (WebKit — no
     browser tab). The **Agent Map opens as a second native window** from the
     ⧉ AGENT MAP button.
   - **It stores data and learns from it.** Every completed session is
     auto-ingested into a long-term memory store, and each new request
     automatically recalls what it already learned about you and your work
     into its context (a `REMEMBERED CONTEXT` block) — so answers improve
     over time instead of starting from scratch. Rate any response with the
     👍/👎 buttons and it learns what you like. The header shows the real
     fact count. Set `DOURMOUSE_LEARN=0` to disable the learning loop.
   - **All agents are live and immediately working.** The preloaded Live
     agents (news, markets, mail, tasks, rnd) run always-on background loops
     that poll their REAL feeds (Google News, Yahoo Finance, your inbox,
     your task list) the moment DOURMOUSE starts — and every agent gets its own
     native window at startup, each showing a `[LIVE]` status and a stream of
     current activity. Set `DOURMOUSE_LIVE=0` to disable the always-on polling.
   - **Each agent gets its own live window.** The moment an agent starts
     working, its dedicated DOURMOUSE window opens automatically (native window
     in the desktop app, new tab in a browser) showing its status, toolkit,
     and a live activity feed — every tool call and result as it happens.
     Open one on demand from the roster ⧉ button or the map's [LIVE WINDOW]
     button.
   - If the native-window dependency (pywebview) can't be installed, it falls
     back to your default browser automatically and says so.
3. To stop: double-click **`stop.command`** (or `kill $(cat .dourmouse-ui.pid)`).

> If macOS blocks the launcher ("unidentified developer"): right-click
> `start.command` → *Open*, or run `xattr -d com.apple.quarantine start.command`
> in a terminal once.

### As a real app (Dock / /Applications)

Run `./build_app.command` once — it wraps the launcher into a double-clickable
**`dourmouse.app`** using `osacompile` (ships with macOS, zero new dependencies).
Drag `dourmouse.app` into /Applications or pin it to the Dock.

### Run from the terminal (same thing)

```bash
./start.command
```

### Headless / browser-only

```bash
./.venv/bin/python -m dourmouse.webui
# → http://127.0.0.1:8765
```

## 3. What you can ask it (RUN:GENERAL)

- **Research & internet** — `web_search`, `fetch_url`, `open_url`
- **Coding** — `run_python`, plus read/write/list/delete workspace files
  (deletes are confirmation-gated)
- **Claude Code** — `claude_code` delegates a coding task to your *real*
  Claude Code CLI (`claude -p` headless mode) and returns its real output.
  Auto-detects the `claude` CLI on PATH; override with `CLAUDE_CODE_CLI`.
  Honestly reports `NOT CONFIGURED` if the CLI isn't installed.
- **Full laptop access (Claude-Cowork style)** — the `system` subagent can
  work across your whole machine, not just the workspace:
  - `read_path` / `write_path` / `list_path` / `delete_path` — any file
    anywhere by absolute path (delete is confirmation-gated; writes/deletes
    are refused inside credential/system dirs: `~/.ssh`, `~/.aws`,
    `~/.gnupg`, `/etc`, `/usr`, `/System`, keychains)
  - `run_command` — run shell commands (a deterministic guard REFUSES
    destructive/irreversible ones: `sudo`, `rm`, `git push`, global package
    installs, `curl | sh`, device writes, disk formatting, power control)
  - `run_privileged_command` — run ANY command after you approve it in the
    INTERVENTIONS column (surfaces with the exact command)
  - `system_info` (real OS/CPU/memory/disk), `open_path` (open files/apps
    in Finder), `clipboard_get` / `clipboard_set` (macOS)
- **Scheduling** — propose meeting slots (deterministic; nothing is ever
  booked without your explicit approval)
- **Comms** — draft emails/messages (drafts are saved; *sending* requires
  confirmation and is `NOT CONFIGURED` until you wire a backend)
- **Memory** — search/read/write your Obsidian vault via `OBSIDIAN_VAULT_PATH`
- **Agents talk to each other — and you can WATCH them** — an inter-agent
  message bus connects the whole roster: the always-on live agents
  broadcast their REAL findings (news → markets → rnd) with zero LLM in
  the path, and a `messenger` subagent (`send_message` / `read_agent_inbox`)
  lets the orchestrator route knowledge between agents mid-task. Open the
  Agent Map: the instant a message hits the bus, a glowing pulse travels
  from the sender to every recipient on the neural-link graph (grid view
  flashes the nodes, a live COMMS STREAM ticker slides in), inboxes +
  unread badges live on the map and in every agent window, and bus traffic
  mirrors into long-term memory so the system learns from inter-agent
  knowledge too.
- **Each agent runs its own NVIDIA model** — set `DOURMOUSE_MODEL_<AGENT>` in
  `.env` (e.g. `DOURMOUSE_MODEL_DEV_CODING=nvidia/code-llama-70b`) to give a
  subagent a model tuned for its job; any agent without an override runs on
  the default `NVIDIA_MODEL`. Delegated runs routed at one agent, and
  [SEND_DIRECTIVE] focus routes, use that agent's model — and every
  roster card, map node, and agent window shows which model is thinking.
- Confirmation-gated actions surface in the **INTERVENTIONS** column with
  `[EXECUTE_PATCH]` / `[BYPASS_NODE]` buttons. Prohibited actions are refused
  outright. Nothing runs an LLM judgment call on risk paths.

## 4. Configuration (optional)

Everything is read from environment variables or `.env` in the app folder:

| Variable | Purpose | Default |
|---|---|---|
| `NVIDIA_API_KEY` | LLM backend key (asked on first run, validated live before saving) | — |
| `NVIDIA_BASE_URL` | NVIDIA NIM endpoint | `https://integrate.api.nvidia.com/v1` |
| `NVIDIA_MODEL` | Model id | `nvidia/nemotron-3-super-120b-a12b` |
| `DOURMOUSE_UI_PORT` | Dashboard port | `8765` |
| `DOURMOUSE_LIVE` | Always-on live agent polling (news/markets/mail/tasks/rnd feed loops) | `1` (set `0` to disable) |
| `DOURMOUSE_MODEL_<AGENT>` | Per-agent NVIDIA model override (e.g. `DOURMOUSE_MODEL_RESEARCH_INFO`); falls back to `NVIDIA_MODEL` | unset → default model |
| `DOURMOUSE_LEARN` | Store & Learn loop (auto-ingest sessions + recall into prompts + feedback) | `1` (set `0` to disable) |
| `DOURMOUSE_MEMORY_DB` | Where the long-term memory store lives | `<workspace>/memory/atlas_memory.db` |
| `DOURMOUSE_WORKSPACE` | Sandbox + session files dir | `<app>/workspace` |
| `OBSIDIAN_VAULT_PATH` | Vault for the memory agent | unset (memory tools report NOT CONFIGURED) |
| `CLAUDE_CODE_CLI` | Path to the `claude` binary for the `claude_code` tool (auto-detected if unset) | `claude` on PATH |
| `DOURMOUSE_MAX_POSITION_PCT` / `DOURMOUSE_MAX_SECTOR_PCT` / `DOURMOUSE_DAILY_LOSS_LIMIT_PCT` / `DOURMOUSE_TRADE_CONFIRM_USD` | Phase 0 guardrail numbers (used when the trading roster is enabled) | `0.10` / `0.30` / `0.03` / `1000` |
| `DOURMOUSE_LLM_BACKEND` | Brain selector: `auto` (probe local Ollama, else NVIDIA), `ollama`, `nvidia` | `auto` |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Local Ollama endpoint + chat model (e.g. `qwen3:8b`) | `http://127.0.0.1:11434` / `qwen3:8b` |
| `OLLAMA_CODING_MODEL` | Model for the `code_ollama` agent (e.g. `qwen2.5-coder:14b`) | falls back to `OLLAMA_MODEL` |
| `DOURMOUSE_ACCESS_TOKEN` | Bearer/cookie token required from NON-loopback clients (login page) | unset = loopback-only, no auth |
| `DOURMOUSE_HOST` | Bind address for the web server | `127.0.0.1` (use `0.0.0.0` only with a token set) |
| `DOURMOUSE_REPORT_TIME` | Daily morning report time (24h local) | `08:30` |
| `DOURMOUSE_VOICE` | Fully local voice endpoints (STT/TTS) | `0` (set `1` to enable) |
| `DOURMOUSE_WHISPER_MODEL` / `DOURMOUSE_WHISPER_DEVICE` | faster-whisper model id + device (`auto`/`cpu`/`cuda`) | `tiny` / `auto` |
| `DOURMOUSE_PIPER_VOICE` | piper TTS voice key | `en_US-lessac-medium` |

Sessions are audited to `<workspace>/sessions/*.jsonl` and resumable.

### Re-check an existing key

If DOURMOUSE starts failing with an auth error, the key already in `.env` may
have been revoked or lost access to the model. Validate it without touching
anything:

```bash
./.venv/bin/python -m dourmouse.key_check --check-existing
# exit 0 = key works, exit 1 = rejected (reason printed)
```

## 5. Security & honesty model

- **No secrets are shipped.** Your API key is written to `.env` (mode 600) on
  first run and never logged or printed.
- The server binds to `127.0.0.1` by default. Exposing it (`DOURMOUSE_HOST`)
  requires `DOURMOUSE_ACCESS_TOKEN` — non-loopback clients get a login page and
  every route enforces the bearer/cookie gate (loopback stays exempt so the
  desktop app never changes).
- Every claim the agent makes is backed by real tool output — no fabricated
  results, no silent stubs (unknown/unbuilt capabilities report honestly).
- This is the GENERAL dispatch roster only. Trading subagents (Research,
  Monitoring, Risk, Execution) are not enabled and no order-placing code
  exists in this build.

## 6. Project layout

```
dourmouse/        engine: dispatch.py, general_roster.py, chat.py,
                     webui.py, desktop.py, live_runtime.py, message_bus.py,
                     learn.py, memory_store.py, config.py, code_backends.py,
                     atlas_ops.py, report.py, self_improve.py, + tests
ui/                  index.html (dashboard), map.html, agent.html,
                     login.html (v4.0) — single files, zero build step
start.command        double-click launcher (opens the NATIVE desktop window)
stop.command         double-click stopper
build_app.command    builds the double-clickable dourmouse.app (osacompile)
requirements.txt     runtime deps (openai, python-dotenv)
requirements-desktop.txt   desktop extra (pywebview = the native window)
.env                your secrets (created on first run — do not share)
workspace/           sandbox + session audit logs
docs/tailscale.md    v4.0 multi-device guide (free, private, end-to-end encrypted)
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| "No Python 3.10+ found" | Install Python 3.12 (Homebrew or python.org), relaunch |
| "dependency install failed" | Check network; re-run start.command |
| Dashboard won't load | See `.dourmouse-ui.log` in the app folder |
| "NVIDIA_API_KEY is not set" | Add it to `.env`: `NVIDIA_API_KEY=nvapi-...` |
| Port 8765 busy | `export DOURMOUSE_UI_PORT=9000` before launching |

Run the test suite: `./.venv/bin/python -m pytest dourmouse/tests`
