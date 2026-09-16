# Architecture — real state as of 2026-09-16

Produced by a full-repo read (not assumed) ahead of the Phase 2 (autonomous runtime) work in
`docs/GODSPEED_ROADMAP.md`. All paths are relative to `dourmouse/` unless stated otherwise.
Companion doc: `docs/UI_SOURCE_MAP.md` (frontend). Repo root `/Users/aditagrawal/dourmouse-recon`.

## 0. Line counts

`dispatch.py` 5,168 · `webui.py` 7,276 · `desktop.py` 1,168 · `config.py` 1,727 · `planner.py` 530.

Largest files in `dourmouse/`: `webui.py` (7,276), `general_roster.py` (6,178), `dispatch.py`
(5,168), `agent_prompts.py` (4,730, pure prompt-text constants), `google_services.py` (1,957,
largest non-test).

## 1. Entry points and process model

- `desktop.py` is a native launcher, not a subprocess wrapper. `launch()` (`desktop.py:831`)
  imports `webui` directly and calls `webui.run_server(...)` **in-process**
  (`desktop.py:894-913`), then runs the returned server's `serve_forever` on a **daemon background
  thread** (`desktop.py:934`). PyWebView's window loop (`webview.start()`, `:1112`) blocks the
  **main** thread. One OS process, two roles — never a subprocess spawn of `webui.py`.
- Port/host: `DOURMOUSE_UI_PORT` (default 8765, both `desktop.py:46-55` and `webui.py:95`). Host
  defaults to `127.0.0.1` (`config.bind_host()`); binding non-loopback without
  `DOURMOUSE_ACCESS_TOKEN` is a hard refusal unless `DOURMOUSE_ALLOW_INSECURE_BIND=1`
  (`webui.py:6777-6810`).
- `webui.py` is standalone-runnable (`python -m dourmouse.webui`, `webui.py:7273-7276`).
- **No SIGTERM handler anywhere in the codebase** (grepped) — `serve_forever` only catches
  `KeyboardInterrupt`. A `dourmouse.service` systemd unit exists at repo root for headless Linux
  deployment but isn't wired to anything else. launchd agents under `.freebuff/` belong to a
  separate sibling app, not this one.
- **Conclusion: no OS-level persistent worker exists today.** The only "always-on" behavior is
  daemon threads living inside one already-running server process (§7).

## 2. Dispatch / agent loop (one user turn)

`webui.py:4099 _handle_chat_authed` → SSE stream opened → slash-command/All-Hands short-circuits
checked → `session.ask()` (`chat.py:139`) → `dispatch.run_dispatch_messages` →
`_run_dispatch_loop` (`dispatch.py:4145`):

1. Build/emit a `plan` event (`planner.build_plan`).
2. Resolve `plan_agents` via the local fine-tuned router (opt-in,
   `DOURMOUSE_AGENT_ROUTER_AUTO=1`) or the deterministic `planner.find_agents_for_query` fallback.
3. Scope tool schemas to those agents, run the tool-calling loop: LLM call
   (`_call_with_retry`) → `tool_calls` → `_execute_tool` (`dispatch.py:2506`) → append results →
   repeat until `max_turns` or no more tool calls.
4. Every event goes through `event_sink` → SSE **and** `ActivityTracker`/`AttentionQueue`.
5. Turn ends with a `done` event; connection closes.

**Everything above is strictly request-scoped** — starts and returns within the HTTP handler call.

**State surviving across turns**: one `ChatSession` per server (or per-tab via
`server.sessions_by_tab`), holding the authoritative `self.messages` list. Each turn is
hash-chained to `workspace/sessions/session_<ts>.jsonl` plus a resumable `.messages.json`
snapshot — a process restart can resume the latest conversation. Governance objects
(`BudgetTracker`, `DlpFilter`, `RbacPolicy`) live at session scope, not per-turn. **No separate
memory/goal state machine exists beyond this transcript.**

**Registry**: `dispatch.DispatchRegistry` — `dict[name]->Subagent` / `dict[name]->ToolSpec`,
global tool-name uniqueness by identity. No process-wide singleton — every entry point builds a
fresh one via `general_roster.build_general_registry()`.

## 3. Tool system

- `dispatch.ToolSpec` (frozen dataclass: name, description, parameters, handler, permission,
  confirm_prompt, output_schema) and `Permission` enum (`REGULAR / REQUIRES_CONFIRMATION /
  PROHIBITED`, `dispatch.py:890-893`).
- Registration mostly in `general_roster.py` (6,178 lines, one big `build_general_registry()`).
- **Gated tool count: 34 call sites across 6 files** (higher than the "~23" this session
  previously tracked — the registry has grown since).
- Execution path `dispatch._execute_tool` (`dispatch.py:2506-2664`): `PROHIBITED` → refuse;
  missing required args → honest error (checked before building `confirm_prompt`);
  `REQUIRES_CONFIRMATION` → build prompt, log a confirmation ledger pair, call
  `confirmation_gate(...)`; run `spec.handler(...)` inside a broad try/except classifying failures
  via `net_errors` and logging via `obs`; optional `output_schema` validation.
- `DOURMOUSE_AUTO_APPROVE` (`config.auto_approve_enabled()`, persisted-file-backed, no restart
  needed) is read at **two separate call sites** since the MCP bridge doesn't share the web gate:
  `webui.WebConfirmationGate.__call__` and `mcp_bridge._handle_tools_call`. A separate per-turn
  `gate.autonomous` UI flag exists too (labels confirmations resumable rather than skipping them).
- `mcp_bridge.py` is a full stdio JSON-RPC MCP server exposing every non-`PROHIBITED` tool except
  `delegate_task`/`delegate_parallel` and the CLI-shim tools — real prior art for exposing this
  registry externally.

## 4. Persistence (no unified data layer — each module owns its own file)

| Store | Path | Shape |
|---|---|---|
| `state_store.StateStore` | `workspace/state/dourmouse.db` | SQLite/WAL, per-owner tables (`watchlist, alerts, prefs, recent, workspace`) — **closest existing template for a new Task table.** |
| `memory_store.MemoryStore` | `workspace/memory/atlas_memory.db` | SQLite + FTS5 `facts`/`facts_fts`, content-hash dedup. |
| `google_auth.AuthStore` | `workspace/auth/dourmouse_auth.db` | `users`, `sessions`. |
| `global_memory.GlobalMemory` | `~/Library/Application Support/Dourmouse/global_memory.sqlite3` | single flat `memory` table, embedding-based. |
| `project_bookkeeper` | `workspace/project_bookkeeper.json` | plain JSON, read-mostly; its own docstring explicitly says "no background thread/daemon here." |
| Sessions | `workspace/sessions/session_<ts>.jsonl` + `.messages.json` | hash-chained turn ledger. |
| Schedules | `workspace/schedules.jsonl` | see below. |

**Scheduler precedent — three independent, non-unified mechanisms, none a general task queue:**

1. `schedules.py` — user-defined recurring **single tool calls**. `Schedules` is a flat JSONL
   store; `SchedulerRunner` is a 15s-tick daemon thread, started only when live polling is
   enabled. Hard-refuses anything but a `REGULAR`-permission tool — no unattended confirmation
   path. Stores `(tool, args, cron-spec, last_run)`, not multi-step plans; cannot resume mid-run.
2. `live_runtime.LiveRuntime` — fixed hardcoded poll table, one thread per `(agent, tool)` pair,
   not user-configurable, not persisted.
3. `atlas/atlas_scheduler.py` — a standalone CLI script, domain-specific, not run inside webui.

No job-queue library anywhere (`requirements.txt` confirmed clean). **No task/job table, no
persisted "pending work" concept, no retry/resume of a partially-completed multi-step run
anywhere.**

## 5. Memory system

Two genuinely separate, non-integrated systems:

1. `memory_store.MemoryStore` — SQLite+FTS5, the "Store & Learn" loop, callable via the `memory`
   subagent and `/api/memory*`. `RemoteMemoryStore` can proxy to another machine, with
   `LocalFallbackMemoryStore` degrading to a local copy (tagged `[PENDING SYNC]`) when unreachable.
2. `global_memory.GlobalMemory` — embedding-based (Ollama embeddings + brute-force cosine, no
   FAISS), auto-injected into the dispatch loop at depth 0 only. Off by default
   (`DOURMOUSE_GLOBAL_MEMORY=0`).

This app was literally renamed from a prior system called JARVIS (`dourmouse/__init__.py:1`).
`GlobalMemory`'s own docstring explicitly rejects JARVIS's per-agent-silo design in favor of one
shared table — a real, documented architectural decision, not an oversight.

**No task/episodic/semantic separation exists as deliberate architecture** — both stores are flat
single-table designs; the only "categorization" is a free-text `source` convention.

## 6. Multi-agent / subagent delegation

- `delegate_task` (`general_roster.py:2404-2556`) — reads the active `DispatchContext`
  (thread-local stack), enforces `max_depth` (default 3) and a shared tree-wide delegate budget
  (`max_delegates=25`), runs a synchronous nested `run_dispatch_messages` that can itself delegate
  again (same guards).
- `delegate_parallel` (`general_roster.py:2590-2870`, v8.31) — the general parallel-fan-out
  primitive: a `branches` list, budget consumed up-front sequentially (avoids a cross-thread
  race), up to 6 branches run genuinely concurrently via `ThreadPoolExecutor`. Per-thread isolation
  is real (`_registry_ctx_stack` moved to `threading.local()` specifically for this). Both tools
  are the same `ToolSpec` objects shared by identity across the `orchestrator`/`companion`
  subagents — a **general primitive**, not a one-off.
- `all_hands.py` fans the same goal across different LLM CLI backends (Claude Code/Codex/ChatGPT)
  — a different axis of "parallel" than roster fan-out.
- `mcp_bridge.py` deliberately excludes `delegate_task`/`delegate_parallel` from external exposure
  — an external CLI has no budget/depth tracker of its own, judged unsafe.
- `JobTracker` (`dispatch.py:3040-3138`) — the audit trail for every delegate spawn. **In-memory
  only**, bounded ring buffer (500), never persisted, dies with the process.

## 7. Background/async execution — the central question

Real daemon threads already outlive individual requests, but all live inside the one running
server process as narrow, fixed-purpose pollers, not a general task executor: `LiveRuntime`,
`SchedulerRunner` (15s tick), cache warmers (world pulse, Gmail inbox, GDELT graph, remote
health), `DailyReporter`, `FreebuffEventWatcher`, `NewsStreamWatcher`, ATLAS auto-sync/idea
generator, `HandsFreeController`, the neuro bootstrap thread, and `_call_with_retry`'s own
heartbeat + deadline enforcement (scoped to one in-flight LLM call).

**What does not exist**: every one of the above is `daemon=True` inside one process — nothing
survives a crash/restart except what's already durably written to disk. **There is no persistence
of "a multi-step run is currently at step 3 of 7" anywhere.** `JobTracker` is in-memory,
`Schedules` only stores single tool-calls on a cron spec, and `_run_dispatch_loop` has no
checkpoint/resume mechanism — a killed process loses any in-flight dispatch entirely; SSE
disconnect can only cancel early, never resume.

**This is the confirmed central gap**: a genuinely persistent worker (survives process restart,
can resume/retry a plan step, isn't tied to holding an HTTP connection open) does not exist yet.
This is exactly what the new Goal/Task runtime must add.

## 8. Error handling and retry conventions

- No single exception hierarchy — per-module `RuntimeError`/`ValueError` subclasses named
  `<Feature>NotConfiguredError` / `<Feature>Error` / `<Feature>Unavailable`.
  `dispatch.ModelCallDeadlineExceeded(TimeoutError)` is the one exception deliberately designed to
  flow through the retry path for free (subclasses `TimeoutError`).
- Shared network-error taxonomy: `net_errors.py` — `ErrorKind` enum, `classify()`, `friendly()`
  (user-facing sentence, strips URLs/status codes), `report()` (logs raw, returns friendly text),
  `is_retryable()`. Used throughout the roster and inside `_execute_tool`'s catch-all.
- LLM-call retry: `dispatch._call_with_retry` — bounded retry + exponential backoff, transience
  via `_is_transient_error`, one fallback-model attempt, account-rotation hook. A hard wall-clock
  deadline (`ModelCallDeadlineExceeded`, default 240s) enforced via
  `ThreadPoolExecutor(max_workers=1)` + `future.result(timeout=...)` — the call is abandoned, not
  killed (documented trade-off).
- Governance guardrails (`governance.py`): `BudgetTracker` (40 calls / $1.00 / 600s wall-clock per
  top-level request tree, shared across the whole delegate tree), `DlpFilter` (regex-redacts
  keys/PEM/JWTs/secrets before anything reaches the model or transcript), `RbacPolicy`
  (`operator`=all tools, `readonly`=~14-tool allowlist), `validate_tool_arguments`/
  `validate_against_schema`.

## 9. Config/env conventions

`config.py` has no single Config class — a flat module of `X_enabled()`/`X_setting()`/
`save_X_setting()` accessor triples, each reading fresh on every call (no restart required).

- `.env` loading: `user_config_dir()` (macOS: `~/Library/Application Support/Dourmouse`) →
  `load_dotenv(user_env_path(), override=False)` then repo-root `.env` — installed-app config wins
  over a dev checkout's. `workspace_dir()` (default `<repo>/workspace`) is a **separate** directory
  from config/secrets, on purpose.
- Confirmed opt-in-gate idiom (matches `DOURMOUSE_AGENT_ROUTER_AUTO=1`): `os.environ.get(NAME,
  "").strip() == "1"`. Opt-out idiom: `.strip().lower() not in ("0","false","no","off")`. A new
  runtime flag should follow one of these two literally, not invent a third shape.
- Persisted (not just env) settings: read `_read_user_config_file()`, set one key, rewrite the
  whole file sorted, `chmod 0o600` — same shape as `save_auto_approve_setting`.

## 10. Notification system

Real, shipped, native: `desktop.DesktopNotifier` — subscribes to the server's own SSE broadcast
hub (the same one `/api/events` uses), diffs new alert events against a seen-id set (late
subscribers never get spammed with pre-existing alerts), fires macOS `osascript` notifications
with an honest console-print fallback if unavailable. A second surface,
`proactive.ProactiveSurfacer`, renders the same feed as an in-app dismissible popup window. Both
are real prior art for surfacing autonomous-runtime completion/failure to the user.

## What's missing for the new Goal/Task runtime (drives Phase 2 design directly)

- No persistent worker/daemon that survives process exit.
- No task/goal/plan persistence — `build_plan` output is transcript-only; `JobTracker` is
  in-memory; `schedules.jsonl` can't hold or resume a multi-step plan.
- No resume/checkpoint of an in-flight dispatch loop.
- No generic retry/recovery policy for *tasks* (only for LLM calls).
- No unified data layer for a new persistent object like `Task`/`Goal`.

**What already exists and must be reused, not rebuilt**: `DispatchRegistry`/`ToolSpec`/
`Permission` + `_execute_tool` for tool execution and gating; `governance.py`'s Budget/DLP/RBAC
for cost/safety caps on a long-running agent; `delegate_task`/`delegate_parallel` +
`DispatchContext` for spawning/parallelizing sub-work with depth/budget guards already in place;
`net_errors.py` for failure classification; `DesktopNotifier`/`ProactiveSurfacer` for surfacing
completion/failure; `state_store.StateStore`'s SQLite/WAL/per-owner pattern as the persistence
template for a new Task table; `SchedulerRunner`'s daemon-thread-with-tick-and-JSONL-store shape
as the closest existing precedent to extend into a real task queue.
