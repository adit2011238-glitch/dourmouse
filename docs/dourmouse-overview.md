# DOURMOUSE — What It Is

**DOURMOUSE is a self-hosted neural agent operating system.** It is not a
website, a SaaS product, or a single chatbot — it is a local-first
orchestration layer that runs on the owner's own hardware, drives a fleet of
specialized sub-agents through one directive box, and keeps its memory,
tools, and data on the machine it runs on.

This document is the honest overview: what the system actually does, what it
can and cannot do, and how it is put together.

---

## 1. One line

DOURMOUSE turns a single natural-language directive into a coordinated run
across dozens of purpose-built agents — mail, web research, markets, code,
browser automation, calendar, memory, music, and more — with every action
visible in a live task deck and every risky action gated behind human
confirmation.

## 2. The core loop

1. You type a directive (or speak one, or send one from the mobile link).
2. A **planner** deterministically ranks the roster's sub-agents by
   capability match and emits a numbered plan (STEP 1/N → agent).
3. The **dispatch orchestrator** runs an OpenAI-shaped tool loop: the model
   calls real tools, gets real results, and keeps going until the plan's
   steps are done or a step is honestly impossible.
4. Everything streams live: plan steps, tool runs, results with done-ticks,
   and the reply with first-token timing.
5. Confirmation-gated tools (sending mail, creating Drive files, submitting
   forms, placing orders) pause for a human approval card before anything
   irreversible happens.

## 3. The roster (sub-agents)

| Agent | What it does |
|---|---|
| `mail` | Inbox + Gmail search/read/send + Drive read (signed-in Google account; sending is gated) |
| `docs` | Link-shared Sheets reads, Drive downloads, and Drive/Slides **write** (Docs + Slides creation, gated) |
| `research_info` | Keyless Wikipedia search, fetch/open URL |
| `news` / `rnd` | Live Google News headlines (keyless RSS) |
| `markets` | Yahoo Finance quotes + day movers |
| `worldmonitor` | Global intel catalog + the self-hosted keyless World Pulse (disasters, cyber, conflict, macro, news, markets) |
| `code_*` / `dev_coding` | Coding via local Ollama, Claude Code CLI, Codex API, NVIDIA NIM, DeepSeek |
| `browser` | Real headless Chrome (Playwright): open/snapshot/fill/submit/sign-in, local credential vault (gated) |
| `compute` | LAN inference offload to the Dell node (Qwen3 1.7B) with automatic local fallback |
| `memory` | Obsidian vault + SQLite FTS5 long-term store, honest daily self-review |
| `tasks` / `scheduling` | Local task list + calendar read / time-slot proposals |
| `music` | Spotify link, now-playing, search, playback control |
| `atlas*` | Quant research repo telemetry, FX pipeline, paper broker (MT5/T212) |
| `orchestrator` | Delegates nested agent runs via `delegate_task` |

Every agent reports its state (IDLE / COMPUTING / HOLD_AUTH / LIVE) in the
Agent Map and its activity in the dispatch feed. Nothing is hidden.

## 4. Honesty contract (the part that matters)

DOURMOUSE never fabricates a result:

- Missing config → `NOT CONFIGURED` with the exact fix, never a fake success.
- Offline backend → the real error (e.g. Yahoo 429), and the local AI takes
  over with transparent failover.
- A tool the agent can't run → it says so explicitly and finishes, instead of
  inventing an output.
- Every write (mail, Drive, forms, orders) is confirmation-gated; every
  read-only capability states its scope.

## 5. Architecture

```
              USER
                |
                v
         DOURMOUSE (this machine)
        orchestrator + roster + UI
                |
    +-----------+-----------+
    |                       |
    v                       v
 LOCAL RESOURCES      COMPUTE NODE (the Dell, optional)
 Ollama / tools       Qwen3 1.7B over LAN, auto-failover
```

- **Main machine = brain + interface + integrations.** Memory, the UI, the
  orchestrator, and the tools stay here.
- **Dell = infrastructure only.** It serves `/v1/status`, `/v1/generate`,
  `/v1/chat`; when it's offline the main machine keeps working with local AI.
- The whole UI is a single self-contained `index.html` (no CDN, no external
  fonts) served on the local network; the mobile link pairs any phone to the
  same session.

## 6. What it cannot honestly do (yet)

- **Google Drive/Slides write** — the code path is real and tested, but it
  needs you to sign in at `/login` with `GOOGLE_OAUTH_FULL_SCOPES=1` so the
  session grants Drive write scope. Until then it reports NOT CONFIGURED.
- **CAPTCHA / phone verification** — no tool can truthfully solve those.
- **Brand-new external accounts** — it can fill and submit forms, but
  creating accounts that require human verification stays a human step.

## 7. Quick start

```bash
# run the server (from the project root)
python -m dourmouse.webui          # serves ui/index.html on 127.0.0.1:8765

# or with the launchd service (macOS)
launchctl kickstart -k gui/$(id -u)/dourmouse-ui-once
```

Open http://127.0.0.1:8765, type a directive, watch the task deck.

---

*Generated by the full systems check, 2026-08-13. Every claim above was
verified live in the running app or by the test suite at commit time.*
