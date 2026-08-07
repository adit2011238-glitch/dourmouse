# DOURMOUSE Upgrade Plan — v3.2.0 → v4.0.0
## "Self-Hosted Personal AI Operating System"

**Date:** 2026-08-06 · **Base:** `dourmouse-4.0.0` · **Baseline:** 607 pass / 1 fail / 2 skip
**Goal:** close every gap in the 13-phase master spec while keeping the project's core
philosophy: zero build step, zero Node, stdlib-only engine, single-file local UI.

---

## 0. Frozen architecture decisions (do not re-litigate)

| Decision | Why |
|---|---|
| **Keep the stdlib HTTP server** (webui.py), not FastAPI | It already delivers API + SSE + RBAC + SQLite; a rewrite breaks 607 tests for zero capability gain |
| **Keep single-file HTML UI** (index/map/agent.html), not React/Three.js | Zero-build is the product's identity; every reference visual is achievable in CSS+SVG+Canvas2D with no CDN (fully local) |
| **Ollama is the primary LLM backend** | Already installed with real models; makes the whole stack free + offline. NVIDIA stays as a configured alternative |
| **Auth before exposure** | No non-loopback bind, no public tunnel, until a token gate exists and is tested |
| **Every new feature**: deterministic data path (Rule 2.8), honest NOT CONFIGURED (Rule 2.2), tests-first, hermetic | The project's constitution |

---

## 1. Gap map — the 13 spec phases vs. what exists

| Spec phase | Current state | Verdict |
|---|---|---|
| 1 Core foundation | stdlib server, RBAC, ChatSession, dispatch router, SSE events, SQLite | ✅ exists |
| 2 Local LLM | NVIDIA NIM only; **Ollama installed but unwired** | 🔴 build |
| 3 Freebuff/ATLAS integration | research bridge exists (`_atlas_runner`, research_agent); no project-status tools | 🟠 extend |
| 4 Manus agents | planner, orchestrator, delegate_task, 17-agent roster, depth/budget bounds | ✅ exists |
| 5 Memory | SQLite FTS5 + learn loop + feedback; no vector layer | 🟡 optional |
| 6 Project intelligence | Obsidian vault ingest + session ingest; no repo index / semantic search | 🟡 optional |
| 7 Premium UI | functional HUD (dark/cyan/scanlines); no radar core, particles, motion states | 🟠 redesign |
| 8 Motion intelligence | CSS pulses only | 🟠 with UI |
| 9 Multi-device | loopback-only, no HTTP auth | 🔴 build |
| 10 Voice | absent | 🟡 stretch |
| 11 Automation | live poll loops exist; no morning report / scheduler | 🟠 build |
| 12 Security | RBAC (operator/readonly/custom), DLP, budget, audit chain, sandbox, confirm gates | ✅ exists |
| 13 Self-improvement | feedback loop; no self-analysis/evolution dashboards | 🟡 build |

**Build order (dependencies first):** P0 fix → P1 local LLM → P2 ATLAS centre → P3 automation
→ P4 multi-device → P5 UI → P6 memory/index → P7 voice → P8 self-improvement → P9 release.

---

## 2. The phases

### P0 — Fix the baseline (0.5 day)
- **Fix** `tests/test_desktop.py` packaging path bug: asserts `scripts/build_app.command`,
  but the file is at project root (`build_app.command`). This is the 1 failing test.
- Confirm `make test` equivalent: `.venv/bin/python -m pytest dourmouse/tests` → 608 green.
- PROGRESS.md entry; no re-roll yet.

### P1 — Fully local LLM: Ollama first-class backend (1–2 days) 🔴 highest value
Objective: run the entire assistant on local open-weight models, zero API spend.

- **`config.py`** — `OllamaConfig` (api_key optional/empty, base_url default
  `http://127.0.0.1:11434/v1`, model default `qwen3:8b`, per-agent `DOURMOUSE_OLLAMA_MODEL_<AGENT>`)
  + `load_ollama_config()` + `load_llm_config()` resolver honoring
  `DOURMOUSE_LLM_BACKEND=ollama|nvidia|auto` (auto = probe `127.0.0.1:11434/api/tags`, fall back to NVIDIA).
  `model_for_agent()` on both configs (share an interface).
- **`dispatch.py`** — `run_dispatch_messages` default: `load_llm_config()` instead of
  `load_nvidia_config()`; `_build_client` works for either (OpenAI-compatible).
- **`orchestrator.py`** — same resolver swap.
- **`webui.py`** — `_resolve_server_config` + `_effective_model` via the unified config;
  new `GET /api/backend` → `{backend, model, per_agent}`; roster UI shows the active backend.
- **`code_backends.py`** — add `ollama` backend (base_url + model, keyless).
- **Tests:** env matrix (ollama/nvidia/auto/unknown), auto-probe with mocked reachability,
  dispatch default resolution, webui `_effective_model` + `/api/backend`, code_backends ollama,
  keyless honesty.
- **Acceptance:** `DOURMOUSE_LLM_BACKEND=ollama` + real chat against local `qwen3:8b`, offline-capable.

### P2 — ATLAS command-centre agent (1–2 days) 🔴 connects to the real repo
Objective: Dourmouse answers "check ATLAS" with real telemetry.

- **`atlas_ops.py`** (new, mirrors research_agent conventions): `atlas_status()` (repo
  exists? branch, last commit, dirty count, test-file count), `atlas_bootstrap_status()`
  (read `data/fx-backfill.log` tail + raw pair-days + `.done` marker — deterministic, no subprocess),
  `atlas_deliverables()` (newest `deliverables/` files with sizes/dates).
- **`general_roster.py`** — new `atlas` subagent (domain "Projects") with the three tools;
  honest `NOT CONFIGURED` when `ATLAS_REPO_PATH` unset.
- **Tests:** tmp-dir fake repo, monkeypatched env, roster wiring, honest degradation.
- **Acceptance:** "Show running projects" → real ATLAS status text in the dashboard.

### P3 — Automation engine: the morning report (1 day) 🟠
Objective: Dourmouse is proactive (spec Phase 11).

- **`report.py`** (new): `build_morning_report(registry, fetcher=None)` — deterministic
  assembly using the SAME registered tool handlers: news_headlines, market_movers,
  list_tasks, atlas_status, system health (system_access `_system_info_tool`). Every
  section fails honestly; no LLM in the data path.
- **`DailyReporter`** — daemon thread, configurable `DOURMOUSE_REPORT_TIME` (default 08:30),
  posts the report to the message bus (`dourmouse -> *`) + tracker; injectable clock/fetcher.
- **`webui.py`** — `run_server(reporting=False)` (hermetic default off), `serve_forever` enables.
- **Tests:** fetcher injection, honest section failures, bus post, clock-driven run, stop cleanly.
- **Acceptance:** scheduled report appears on the dashboard COMMS feed + in memory.

### P4 — Multi-device: auth gate + bind + Tailscale (1.5–2 days) 🔴 security-critical
Objective: phone/laptop access, free + local + encrypted.

- **Auth:** `DOURMOUSE_ACCESS_TOKEN` — bearer token required on every route (`/api/*`, `/`,
  `/map`, `/agent/*`) when set; loopback `127.0.0.1` stays token-free so the pywebview
  desktop app and local chat never change. Login screen in the UI when the token is set
  and not provided.
- **Bind:** `DOURMOUSE_HOST` env (default `127.0.0.1`); loud warning when set to `0.0.0.0`
  without a token.
- **Tailscale:** onboarding doc + `tailscale.command` launcher (install, tailnet join,
  print the tailnet IP).
- **Responsive pass:** single-column layout, touch targets, tap-to-open agent panel on all
  three pages (CSS media queries only).
- **Tests:** 401 vs 200 matrix, loopback exemption, bind config, warning path, responsive CSS presence.
- **Acceptance:** phone on the tailnet opens the dashboard with the token.

### P5 — Premium HUD redesign (2–3 days) 🟠 the reference look
Objective: the Dourmouse concept-art aesthetic, still zero-build and fully local.

- **`ui/index.html`** — central radar/reticle core bound to real `/api/*` state (roster
  count, live agents, memory facts, budget); Canvas2D particle field; panel corner
  brackets + layered glow; motion states Idle→Thinking→Planning→Executing→Complete
  driven by the existing SSE events.
- **`ui/map.html`** — SVG neural graph: glow filters, ring-pulse on comms, node reticles.
- **`ui/agent.html`** — HUD chrome + small live status ring.
- **Tests:** UI wiring strings updated to match new markup (existing tests assert on
  real strings); a "no external references" test (no http(s) fetch, no CDN).
- **Acceptance:** dashboard visually matches the reference; `grep -E "https?://" ui/*.html`
  returns nothing.

### P6 — Memory & project intelligence deepening (2–3 days, optional) 🟡
- **Semantic recall (optional, gated `DOURMOUSE_EMBED=1`):** Ollama embeddings
  (`nomic-embed-text`) for similarity recall layered OVER the FTS5 store; FTS5 stays the
  zero-dep primary; honest NOT CONFIGURED when embed model absent.
- **Repo index:** `atlas_index` tool ingests the ATLAS repo (README, CHANGELOG, reports,
  key source) into the memory store so "why did we change X" surfaces past decisions.
- **Tests:** fallback honesty, idempotent ingestion, recall still works without embeddings.
- **Acceptance:** "why did we change ATLAS risk parameters" returns real stored facts.

### P7 — Voice assistant (2–3 days, stretch) 🟡
- Whisper (`faster-whisper` or `openai-whisper`, local) STT; Piper TTS (local).
- `POST /api/speech` (STT) + `GET /api/speech/<text>` (TTS, WAV); mic button in UI;
  `DOURMOUSE_VOICE=0` gate; honest NOT CONFIGURED without models.
- **Acceptance:** offline voice round-trip with local models only.

### P8 — Self-improvement & hardening (1–2 days) 🟡
- **Self-analysis:** daily stats digest (turns, tool failures, budget spend, interventions,
  feedback counts) written to memory + appended to the morning report.
- **Agent evolution:** per-agent success/failure tracking surfaced in the roster UI and fed
  to recall (already have feedback facts; add per-agent aggregation).
- **Tests:** digest correctness with synthetic tracker, no LLM in the path.

### P9 — Packaging & release (0.5 day)
- Re-run `bash scripts/build_dist.sh 4.0.0`; verify dourmouse.app; PROGRESS.md full entry;
  README updates (Ollama, ATLAS agent, report, auth, Tailscale); `.env.example` additions
  (`DOURMOUSE_LLM_BACKEND`, `OLLAMA_*`, `DOURMOUSE_REPORT_TIME`, `DOURMOUSE_ACCESS_TOKEN`,
  `DOURMOUSE_HOST`, `DOURMOUSE_VOICE`).
- Full suite green (target ~680–720 tests).

---

## 3. Sequencing & time estimate

| Track | Phases | Focused time |
|---|---|---|
| **Critical path** (free+local, ATLAS link, automation, devices, UI) | P0→P5, P9 | **~7–10 days** |
| **Stretch** (memory/index, voice, self-improvement) | P6, P7, P8 | +6–8 days |
| **Total to v4.0** | all | **~2–3 weeks** focused, done in the evaluate→plan→execute→validate loop |

First slice worth doing immediately (one session): **P0 + P1** — fixes the suite and makes
the whole system free and local. The UI (P5) and devices (P4) both depend on nothing else
and can run in parallel with P2/P3.

## 4. Risks & decisions to confirm

- **Ollama model defaults:** `qwen3:8b` (chat) + `qwen2.5-coder:14b` (coding) — confirm,
  or specify others (glm-4.7-flash, gpt-oss:120b-cloud also installed).
- **ATLAS research on-demand from chat:** the existing `run_atlas_research` spawns a real
  ATLAS run (slow, minutes). Keep it on-demand only; never in a poll loop.
- **Voice scope:** STT+TTS both local adds heavy wheels (torch). Confirm before P7.
- **Cloudflare Tunnel:** not recommended — exposes to the public internet; Tailscale is the
  local-first answer. Only revisit after P4 auth exists, for non-tailnet devices.
