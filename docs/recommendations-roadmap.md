# DOURMOUSE — Recommendations, Roadmap & Connection Status (v5.3)

**Date:** 2026-08-08 · **Branch:** main · **Server:** http://127.0.0.1:8765/ (PID live)

This file is the "what I would add" + "what I actually did" record for the
v5.3 pass. Everything below is grounded in the real codebase — each
recommendation names the component it touches and why it matters.

---

## 1. Account connections — status right now

The new `/api/connections` endpoint + `check_connections` tool (system
subagent) + SETUP-panel rows report the REAL state, verified live:

| Service    | Status (measured) |
|------------|-------------------|
| Ollama     | ● running (127.0.0.1:11434) — local brain |
| NVIDIA NIM | ● NVIDIA_API_KEY present |
| **Claude** | ● Claude Code CLI 2.1.220 on PATH — `claude -p` AUTH_OK live. Your claude.ai account (the Gmail login) is what the CLI uses. |
| **Codex**  | ● codex-cli 0.144.6 · logged in (chatgpt) — your ChatGPT account. ⚠ currently at the **usage limit** (resets ~Aug 22) — surfaced honestly at run time. |
| **Gmail**  | ● configured (via `local_secrets.py`, gitignored) — IMAP read + search/send all work. |
| **Freebuff** | ● app running (UI:51819, API:51820). ⚠ API needs a bearer token shown in the Freebuff app — paste into `.env` `FREEBUFF_API_TOKEN` to unlock real API reads. |
| Slack      | ○ tokens absent — add SLACK_BOT_TOKEN/SLACK_APP_TOKEN to .env |
| Alpaca     | ○ keys absent — add APCA_* keys to .env for paper trading |
| ATLAS      | ○ ATLAS_REPO_PATH/ATLAS_VENV_PATH absent in .env |

**Honest caveat on "connect to my Freebuff account":** Freebuff Desktop does
not expose a public auth flow for third-party local apps; its local API
(port 51820) requires a per-app token that the Freebuff app itself shows you.
The integration hooks are in place (`FREEBUFF_API_URL` / `FREEBUFF_API_TOKEN`
in `.env.example`); the moment the token is pasted, the tool flips from
"app running · token missing" to "API ready". Everything else on this table
is genuinely connected and live-tested.

---

## 2. New in v5.3 (executed this session)

1. **`dourmouse/connections.py`** — deterministic, honest per-account status
   (ollama/nvidia/claude/codex/gmail/freebuff/slack/alpaca/atlas). Never
   leaks secrets; never crashes on a dead probe. Tests: `test_connections.py`.
2. **`codex_code` tool on `dev_coding`** — real Codex CLI delegation
   (`codex exec` headless), mirroring the proven `claude_code` tool, using
   the ChatGPT login already on the machine. Tests: `test_codex_code.py`.
3. **`check_connections` tool on the `system` subagent** — one directive
   ("check your connections") returns the whole account matrix.
4. **`GET /api/connections`** + two new SETUP-panel rows (`codex_cli`,
   `freebuff`) — the HUD now shows connection truth (●/○ per service).
5. **Model-brain fix (live-measured):** per-agent `qwen2.5-coder:14b` for
   dev_coding was removed — measured live, that model does NOT call tools
   when acting as the dispatch brain, while `qwen2.5:7b` / `qwen3:8b` do.
   Documented the trap in `.env.example`.

## 3. Coding-capability test (live, real model + real tools)

Directive: *"write fib.py in the workspace with an iterative fib(n), then
run it with run_python to compute fib(10) and report the result."*

```
TOOLS CALLED: ['write_file', 'run_python', 'write_file', 'run_python']
ARTIFACT EXISTS: /tmp/dm_coding_ws/fib.py  (correct iterative fib)
FINAL: fib(10) = 55
```

Backend live checks: `claude → CLAUDE_LIVE_OK (4s)`,
`ollama → OLLAMA_LIVE_OK (54s)`, `deepseek → DEEPSEEK_LIVE_OK (5.4s, via
NVIDIA NIM)`. Codex verified wired but rate-limited by OpenAI until ~Aug 22.

---

## 4. Recommendations — prioritized

### Now (high value, low risk)
1. **Freebuff token** — paste the app's API token into `.env`; the API
   integration is already wired and flips on. *(5 min, manual)*
2. **Scheduled daily operations** — the prompt in `docs/daily_agent_prompt.md`
   exists; wire a second `DailyReporter` fire at market close so the
   open+close routine runs unattended. *(report.py, ~1h)*
3. **`gmail_delete` / `gmail_flag` tools** — the mail triage phase of the
   daily ops order needs them (currently missing). IMAP `STORE +FLAGS`,
   confirmation-gated like `delete_path`. *(~2h)*
4. **Calendar + Google Drive** — `google_services` has Gmail only; add
   `calendar_events` (read-only) + Drive read so "what's my week" works.
   *(~3h)*
5. **Slack/Telegram relay** — a channel front-end so Dourmouse can message
   you (and take directives) outside the HUD. Env hooks already exist.

### Research value (medium)
6. **Roster self-audit tool** — an agent that diffs its own tool list
   against `.env` capabilities daily and proposes registrations (the
   "build gmail_delete when missing" loop, automated).
7. **Vector memory upgrade** — semantic recall exists via
   `memory_search_semantic`; wire `nomic-embed-text` (already pulled) into
   the daily digest so morning briefs rank yesterday's sessions by meaning.
8. **Strategy pipeline** — the ATLAS integration (`ATLAS_REPO_PATH` +
   `ATLAS_VENV_PATH`) is the last unconfigured entry; once set, the atlas
   ops agent can run backtests/sweeps on directive.

### Bigger bets (later)
9. **Multi-device + token auth** — `DOURMOUSE_HOST=0.0.0.0` +
   `DOURMOUSE_ACCESS_TOKEN` is ready; pair with the phone PWA for on-the-go
   directives.
10. ~~**Browser agent**~~ — **DONE (v5.25)**: the `browser` subagent drives a
    real headless Chrome (Playwright + system Google Chrome, no browser
    download) for signup/login/form-filling: open/snapshot/fill/click/
    submit/screenshot + a 0600 credential vault. Submitting, logging in and
    storing credentials are confirmation-gated. The ATLAS `.mcp.json`
    Playwright MCP reference remains for the ATLAS sidecar; the core app no
    longer needs it.
11. **Trading guardrail integration** — Dourmouse already carries
    JARVIS_* risk numbers; wire the ATLAS engine's daily-loss kill-switch
    into the live markets agent so the monitor becomes an action system.

---

## 5. Housekeeping & validation

- Full test suite: **881 passed** (was 853 → +28: connections + codex_code +
  setup/endpoint coverage). New code ruff-clean and house-style.
- New files: `dourmouse/connections.py`, `tests/test_connections.py`,
  `tests/test_codex_code.py`; edited `general_roster.py`, `system_access.py`,
  `webui.py`, `.env.example`, `.env` (model-brain fix only).
- Server restarted with all changes; `/api/connections`, `/api/setup`,
  `/api/roster` (codex_code present) verified live in the Preview tab.
