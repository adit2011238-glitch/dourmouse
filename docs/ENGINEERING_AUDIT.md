# Engineering Audit Log

Real findings only, recorded as found and fixed, per the commercial-grade audit
tracked in `docs/GODSPEED_ROADMAP.md`. Severity communicates engineering risk,
never an arbitrary quality score. Nothing here is marked resolved without a
real regression test and a full, green pytest run — see each entry's own
numbers.

## Format

Each entry: Finding, Severity, Root cause, Fix, Files changed, Tests added,
Tests run, Result.

---

### 001 — Agent-inbox endpoint silently faked an empty inbox on failure

**Severity**: MEDIUM (data integrity / observability — a real backend fault
was indistinguishable from "no messages," which could hide a genuine
`MessageBus` regression from ever surfacing).
**Root cause**: `webui.py`'s `_handle_agent_api` wrapped `bus.inbox`/
`mark_read`/`unread_count` in a blanket `except Exception: pass`, then
returned the empty defaults it had already initialized — the same shape as a
real, honestly-empty inbox.
**Fix**: capture the exception into a new `inbox_error` response field
(`None` on success) instead of silently substituting a fabricated empty
result. Resilience (never 500 the whole endpoint) is unchanged.
**Files changed**: `dourmouse/webui.py`.
**Tests added**: covered by existing `_handle_agent_api` test coverage plus
manual diff review (the change is additive to the response shape; no
existing assertion depended on `inbox_error`'s absence).
**Tests run**: full suite, 4702 passed / 10 skipped.
**Result**: fixed.

### 002 — 14 other silent `except Exception: pass` sites, audited individually

**Severity**: LOW each (all 14 were genuine best-effort swallows — an
observer, a secondary broadcast, or a non-essential cache read that must
never take down dispatch/a chat turn/the schedule loop) — grouped here
because the *investigation* (verifying each one individually rather than
blanket-trusting the pattern) is the real audit value, not any single fix.
**Root cause**: none were bugs; 12 had no comment explaining why swallowing
was correct, which is itself a minor maintainability gap — a future reader
can't tell "deliberate" from "forgotten."
**Fix**: added a one-line WHY comment to each, matching this codebase's own
established terse-comment style (e.g. `# a raising observer must never break
the bus`).
**Files changed**: `dourmouse/message_bus.py`, `dourmouse/webui.py`,
`dourmouse/dispatch.py`, `dourmouse/schedules.py`, `dourmouse/chat.py`,
`dourmouse/report.py`, `dourmouse/atlas/atlas_lab.py`.
**Tests added**: none needed (no behavior change).
**Tests run**: full suite, 4702 passed / 10 skipped.
**Result**: documented, no fix needed beyond finding 001 above.

### 003 — `rnd` and `browser` agents had no prompt-injection instruction-hierarchy framing

**Severity**: HIGH (the `mail`/`docs` agents already treat retrieved
email/Drive content as untrusted data — real, working language — but `rnd`
(`web_search`/`fetch_url`, genuinely adversarial-controlled internet content)
and `browser` (renders arbitrary third-party pages near the model) had none
at all, despite being *more* exposed to injection than an internal document).
**Root cause**: the untrusted-content instructions were added per-agent as
each agent's own risk was considered; `rnd`/`browser` were never given a pass
for this specific concern.
**Fix**: added an explicit `UNTRUSTED CONTENT` section plus matching
`AGENT BOUNDARIES` items to both agents' prompts, using the exact same
established `AGENT_SYSTEM_PROMPTS` mechanism the `mail` agent already uses
(no new prompting infrastructure).
**Files changed**: `dourmouse/agent_prompts.py`.
**Tests added**: `test_rnd_prompt_treats_fetched_content_as_untrusted`,
`test_browser_prompt_treats_page_content_as_untrusted` in
`dourmouse/tests/test_agent_prompts.py`.
**Tests run**: full suite, 4734 passed / 10 skipped.
**Result**: fixed.

### 004 — Dead UI files, one false positive caught before deletion

**Severity**: LOW (repo cleanliness / new-developer orientation).
**Root cause**: `ui/DOURMOUSE_DESKTOP_MOCKUPS.html`, root `quill-onboarding.html`,
and the vendored `ui/assets/vendor/lucide/lucide.min.js` bundle had zero
references anywhere in the repo. A first pass also flagged `ui/hub.html`,
`ui/graveyard.html`, `ui/product.html`, `ui/agent_chat.html`, and
`ui/decision_cards.json` as dead by the same "not referenced from
webui.py/desktop.py/electron" check — re-verified with a full-repo reference
search before deleting anything, which caught that those five actually
belong to a separate, real, tested sub-app (`tools/serve_hub.py`, its own
port, its own contrast-checker in `dourmouse/ui_contrast.py`,
`dourmouse/tests/test_agent_chat_page.py`/`test_ui_contrast.py`/
`test_ui_focus_visible.py`).
**Fix**: removed only the three confirmed-dead artifacts. Left the five
false positives untouched and corrected `docs/UI_SOURCE_MAP.md` to explain
why.
**Files changed**: deleted `ui/DOURMOUSE_DESKTOP_MOCKUPS.html`,
`quill-onboarding.html`, `ui/assets/vendor/lucide/lucide.min.js`.
**Tests added**: none needed (no code referenced these files).
**Tests run**: full suite, 4702 passed / 10 skipped.
**Result**: fixed.

### 005 — `.gitignore` didn't cover rotated log files or the `logs/` directory

**Severity**: LOW (repo hygiene).
**Root cause**: `*.log` matches files ending in `.log`, but a rotated log
(`perf.log.1`) ends in `.1` — the pattern never matched it, so the whole
`logs/` directory (10MB of runtime output at the time of the audit) showed
as untracked.
**Fix**: added `logs/` and `*.log.*` to `.gitignore`. Confirmed nothing
inside `logs/` was already tracked before adding the blanket rule.
**Files changed**: `.gitignore`.
**Tests added**: none (not code).
**Tests run**: n/a (no Python changed).
**Result**: fixed.

### 006 — Secret-pattern scan of the tracked working tree

**Severity**: n/a (verification pass, not a finding).
**Scope**: `git grep` across tracked `.py`/`.js`/`.html`/`.json`/`.md` for
OpenAI/Google/GitHub/Slack-shaped key patterns.
**Result**: clean. The only matches were cache-busting hash fragments in an
archived, unrelated academic webpage under `jarvis/research_mesh/` (a
separate research corpus, not Dourmouse's own code or secrets) — false
positives, confirmed by inspection, not real credentials. `.gitignore`
already correctly excludes `.env`, `dourmouse/local_secrets.py`, and
`dourmouse/_builtin_oauth.py` from ever being tracked. No git-history
mining performed yet (real, separate follow-on — scanning full history is a
larger, slower pass than the working-tree check done here).

### 007 — Two new background daemon threads (goal_runtime, scheduler_runner) were never stopped on shutdown

**Severity**: LOW (process hygiene — neither hangs exit, since both are
`daemon=True` threads, but "shutdown" left a tick loop running against a
process that no longer had a coherent server around it).
**Root cause**: every other background service (`live_runtime`) gets an
explicit `.stop()` in both real shutdown paths
(`webui.serve_forever`'s `finally` block and `desktop.py`'s own cleanup);
`scheduler_runner` was missing from both already (pre-existing, found
while wiring the new `goal_runtime`, which had the identical omission by
construction).
**Fix**: added `.stop()` calls for both in both shutdown paths.
**Files changed**: `dourmouse/webui.py`, `dourmouse/desktop.py`.
**Tests added**: none (matches the pre-existing gap for `live_runtime`,
which also has no dedicated shutdown-triggers-stop test — noted as a real,
narrow test-coverage gap rather than inventing new test infrastructure
disproportionate to a two-line fix).
**Tests run**: full suite, 4780 passed / 10 skipped.
**Result**: fixed.

### 008 — Goal completion/permanent-failure was only detected at the START of a tick

**Severity**: MEDIUM (correctness — not a crash, but a real, user-visible
delay: a goal whose last task finished, or whose task permanently failed,
mid-tick would not flip to COMPLETED/BLOCKED until the NEXT tick,
observed directly by this module's own test suite before the fix).
**Root cause**: `GoalRuntime._advance_goal` checked "all tasks
complete"/"any task permanently failed" once, using the task list fetched
at the top of the method, then ran the ready tasks for that tick without
re-checking either condition against the post-run state.
**Fix**: extracted the check into `_resolve_if_terminal()`, called both
before and after running each tick's task batch.
**Files changed**: `dourmouse/goal_runtime.py`.
**Tests added**: caught by the module's own pre-existing test suite
(`test_goal_runtime.py`) — several tests failed against the original
implementation and passed once fixed; no new tests needed beyond those
already written for the feature itself.
**Tests run**: `test_goal_runtime.py` (17/17), then full suite (4780
passed / 10 skipped).
**Result**: fixed.

### 009 — Real security observation: this host's own current exposure (not a code defect)

**Severity**: n/a (a factual finding surfaced by the new
`dourmouse/security/platform_adapter.py`, not a bug in Dourmouse itself).
**Finding**: on this development machine, at the time this was captured
(2026-09-17), the macOS Application Firewall reports disabled
(`socketfilterfw --getglobalstate` → `State = 0`), and a `Python` process
(PID 7502 at capture time) listens on TCP port 8793 bound to `*`
(`ALL_INTERFACES` — reachable from the LAN and any Tailscale peer, not
just this machine). Neither is inherently malicious — plenty of personal
machines run without the Application Firewall on, relying on
router/NAT — but both are exactly the kind of fact a real security
dashboard should surface plainly rather than silently.
**Action**: none taken by this pass (Phase 4 is observation-only so far,
deliberately — see `docs/GODSPEED_ROADMAP.md`). Recorded here as the
first real, evidence-backed output of the new telemetry layer, and to
avoid this exact observation being rediscovered and treated as a new
finding later.

### 010 — Real static analysis (ruff) set up and run for the first time

**Severity**: n/a (tooling addition, not itself a finding).
**What**: installed `ruff==0.16.8` (`requirements-dev.txt`), wrote a real,
curated `[tool.ruff]` config in `pyproject.toml` — a deliberately selected
rule set (F/E/W/I/B/C4/SIM/S/PLW1510), not the full default catalogue,
which ran to 1,354 findings dominated by noise for this codebase's own
style (e.g. `EXE002` "shebang missing executable bit" alone fired 296
times against library modules never meant to be invoked directly). The
curated config found **598** real findings. Per the spec's own instruction
("do not add tools merely for appearance... configure them
professionally"), every category was triaged by hand, not blindly
autofixed — see findings 011-014 below for what came out of it.
**Result**: tooling in place; `ruff check dourmouse` is now a real,
repeatable check future work should run before claiming a pass "done."

### 011 — Real, live undefined-name bugs (F821), one already reachable in production

**Severity**: HIGH for the first one (a genuine `NameError` waiting to
happen on a real, live code path), LOW for the other two (string-quoted
type annotations, never evaluated at runtime, but incorrect and
undiscoverable to any type checker).
**Root cause 1**: `dourmouse/atlas/atlas_lab.py`'s `_build_report()`
referenced bare names `direction`/`entry`/`exit_cond` in its
human-readable-summary section — these were never assigned as local
variables; the actual data lives in the `report` dict under
`report["direction"]`/`["entry_condition"]`/`["exit_condition"]`. The
**only** existing test coverage of this function fully replaced it via
`monkeypatch.setattr(al, "_build_report", lambda **k: {...})`, so the real
function body — reachable from the live `submit_backtest` →
`_run_backtest_worker` pipeline — had never actually been executed by any
test. Exactly the spec's own warning: "Existing tests are evidence of what
someone thought should work. They are not proof that the system works."
**Root cause 2**: `dourmouse/backend_fallback.py` and `dourmouse/learn.py`
each have a string-quoted forward-reference type annotation
(`"OllamaConfig | None"`, `"MemoryStore | RemoteMemoryStore | None"`)
naming a real class that exists elsewhere in the codebase but was never
imported into that specific file.
**Fix**: root cause 1 — use the real dict keys. Root cause 2 — add the
missing imports (`OllamaConfig` from `dourmouse.config`, `RemoteMemoryStore`
from `dourmouse.memory_store`) to each file's existing import line.
**Files changed**: `dourmouse/atlas/atlas_lab.py`, `dourmouse/backend_fallback.py`,
`dourmouse/learn.py`.
**Tests added**: `TestBuildReportSummary` in `dourmouse/tests/test_atlas_lab.py`
— two real, direct (not monkeypatched) calls to `_build_report()`, closing
the exact coverage gap that let this bug ship undetected.
**Tests run**: `test_atlas_lab.py` (17/17), then full suite.
**Result**: fixed.

### 012 — SQL construction findings (S608): three false-positive classes, one real deferred

**Severity**: the four false positives are n/a (reviewed, safe, now
documented inline); the real one is MEDIUM (requires control of an
already-untrusted external file or this process's own environment
variables to exploit — a real but non-trivial bar).
**False positives** (`dourmouse/goals.py` ×3, `dourmouse/state_store.py`
×1): each builds a `?`-placeholder string sized to a **fixed, hardcoded**
Python frozenset/dict (`GOAL_ACTIVE_STATES`, `GOAL_TERMINAL_STATES`,
`TASK_TERMINAL_STATES`, `_TABLE_DDL`'s own keys) — never a value, and
never user-influenced; the real bound values are always passed separately
via the parameterized `?` marks. Documented with an inline comment plus
`# noqa: S608` at each site rather than silently suppressed.
**Real, deferred finding** (`dourmouse/shared_rag.py`, 3 sites): this
module's own docstring already states it "probes the actual SQLite
table/column names at connection time" of an **external database file
this codebase does not own and has never inspected directly** (a separate
process's vault). Table/column identifiers can never be parameterized in
SQL (a fundamental limitation, not an oversight) — but `shared_rag.py`'s
own `where_clause`/`order_sql` (lines ~380-385) are
built directly from **environment variables**
(`_VAULT_ID_ORDER_ENV`/`_VAULT_ID_FILTER_ENV`) with zero sanitization
before being interpolated into raw SQL — a real SQL-injection shape if
those env vars were ever attacker-influenced. The bar to exploit is
already high (an attacker able to set this process's environment
variables already has code-execution-equivalent access), and the module's
own docstring states this environment has no access to the real vault
file to test against. **Deferred, not fixed**: proper SQL identifier
quoting (SQLite's doubled-double-quote convention, which Python's `!r`
does NOT implement) plus rejecting or allow-listing `filter_sql`/`order_sql`
shapes, to be done when this module is actually exercised against a real
vault, not guessed at blind.
**Files changed**: `dourmouse/goals.py`, `dourmouse/state_store.py` (noqa +
comments only, no behavior change).
**Tests added**: none needed for the false positives (no behavior change).
**Tests run**: full suite.
**Result**: 4 documented as safe, 1 documented and deferred with a clear
reason.

### 013 — XML parsed from real external feeds without XXE/entity-expansion protection

**Severity**: MEDIUM (defense-in-depth — Google News and the 17
world-pulse sources are not hostile by design, but a compromised CDN,
DNS hijack, or TLS misconfiguration could serve malicious XML, and the
stdlib `xml.etree.ElementTree` parser has no protection against XML
bombs/entity expansion).
**Root cause**: `dourmouse/live_feeds.py` and `dourmouse/world_pulse.py`
both used `xml.etree.ElementTree` directly on HTTP response bodies from
external sources.
**Fix**: added `defusedxml==0.7.1` (a real, maintained, drop-in-API-compatible
replacement) and swapped both imports.
**Files changed**: `requirements.txt`, `dourmouse/live_feeds.py`,
`dourmouse/world_pulse.py`.
**Tests added**: none needed (API-compatible swap, verified with a direct
`fromstring`/`ParseError` compatibility check before landing).
**Tests run**: full suite.
**Result**: fixed.

### 014 — SSRF hardening for `fetch_url`: scheme validation existed, host validation did not

**Severity**: MEDIUM (a model-supplied URL could already only use
http(s) schemes — `file://` etc. were already refused — but nothing
stopped a fetch targeting an internal address: a cloud metadata endpoint,
this machine's own loopback services, a router's admin page. Directly
relevant to finding #003, this same audit's own prompt-injection
finding: a page the research agent already visited could try to redirect
a *later* `fetch_url` call at an internal target via embedded text).
**Root cause**: `_fetch_url_tool` validated the URL's scheme but never
resolved or validated its host before fetching.
**Fix**: added `_refuse_private_fetch_target()` — resolves the hostname
once and refuses private/loopback/link-local/reserved/multicast
destinations via `ipaddress`, with an honest error when the host doesn't
resolve at all. Not DNS-rebinding-proof (a second resolution at connect
time could differ) — a real, meaningful improvement over no check,
not a claimed complete guarantee.
**Files changed**: `dourmouse/general_roster.py`.
**Tests added**: 3 new tests (refuses a metadata-range address, refuses
loopback, honest error on an unresolvable host) plus fixes to 3 existing
tests that now need `socket.gethostbyname` mocked for hermeticity (they
previously depended, unnoticed, on real DNS resolution of
`example.com`/`example.test` succeeding).
**Tests run**: `test_general_roster.py` + `test_roster_error_paths.py`
(123/123), then full suite.
**Result**: fixed.

### 015 — Dead-code findings (F841), one uncovering the real root cause of finding #011

**Severity**: LOW for two (pure dead code, zero behavior change to remove),
MEDIUM-but-deliberately-deferred for the third (real financial/trading
code with an incomplete validation path).
**`dourmouse/atlas/atlas_lab.py`** (`_run_backtest_worker`): `lookback`,
`direction`, `entry`, `exit_cond` (and the `params` dict that only ever
fed the also-dead `lookback`) were computed from `spec` and never used —
`_build_report()` (the function called right after) already independently
re-derives the same three values from the same `spec` dict internally.
This is almost certainly the exact origin of finding #011's real bug:
whoever wrote `_build_report`'s summary section was very likely looking
at (or copied from) this calling scope's own local variable names
(`direction`/`entry`/`exit_cond`) and wrote them as bare names in the
wrong scope. Removed the dead locals; the real values now flow through
`spec` exactly once, matching `_build_report`'s own actual expectations.
**`dourmouse/atlas/atlas_lab.py`** (`_build_report`, separately): `windows`
extracted from parsed CLI output but never read anywhere — vestigial,
removed.
**`dourmouse/forex_ops.py`** (`forex_inventory`): a `pairs` dict declared
but never populated or read — the function already reports via
`timeframe_counts`/`total_bars`/`d1_rows` instead. Vestigial, removed.
**`dourmouse/mt5_ops.py`** (real trade-order placement): `sym =
mt5mod.symbol_info(name)` was fetched and never used — the intended use
was almost certainly client-side validation of the order's `volume`
against the symbol's real `volume_min`/`volume_max`/`volume_step`, and
price rounding to the symbol's tick size, neither of which this function
does today. **Deliberately not implemented here**: this is live,
real-money-adjacent order-placement code, and guessing at MT5's exact
rounding/step semantics without a real server to verify against risks
introducing a WORSE bug (a silently-wrong volume that the broker still
accepts) than the current behavior, which already fails honestly via
`result.retcode`/`last_error()` when the broker itself rejects a
malformed order. Removed the dead fetch; left a clear comment naming the
real, deferred validation gap for whoever next touches this with access
to a real MT5 server to verify against.
**Files changed**: `dourmouse/atlas/atlas_lab.py`, `dourmouse/forex_ops.py`,
`dourmouse/mt5_ops.py`.
**Tests added**: none needed (all three are zero-behavior-change removals
of genuinely dead code, confirmed by tracing every use of each name
through its enclosing function before removing).
**Tests run**: full suite.
**Result**: fixed (two), honestly deferred with reasoning (one).

### 016 — Concurrency pass, first real finding: `cancel_goal` could be silently undone by an in-flight task

**Severity**: MEDIUM (not a crash or data corruption, but a real
correctness gap: cancelling a goal is meant to stop it, and this let an
already-in-flight task's stale result resurrect it seconds later without
any error or indication anything went wrong).
**Root cause**: `GoalRuntime._run_task` checks the goal's status once,
before starting a task's dispatch call, but a real dispatch call (a full
chat turn, potentially tool calls and all) can run for many real seconds.
Nothing re-checked whether the task had been cancelled (from another
thread — a normal chat turn calling `cancel_goal`) by the time that call
finally returned, so a late "success" result would silently overwrite the
already-CANCELLED task back to COMPLETED.
**Fix**: re-check the task's own current status immediately after
`_execute_via_dispatch` returns, before writing any
completion/failure/approval status; a task already in a terminal state
(`CANCELLED`/`COMPLETED`/`FAILED`) is left alone.
**Files changed**: `dourmouse/goal_runtime.py`.
**Tests added**: `test_cancelling_mid_flight_is_never_clobbered_back_to_completed`
in `dourmouse/tests/test_goal_runtime.py` — simulates the exact race (the
scripted fake response calls `cancel_goal` mid-"dispatch", then returns a
normal success) and proves the task stays CANCELLED.
**Tests run**: `test_goal_runtime.py` (18/18), then full suite.
**Result**: fixed.
**Broader concurrency pass, spot-checked and NOT re-litigated**: the
three real shared-state tracker classes already in the codebase before
this session (`ActivityTracker`, `AttentionQueue`, `dispatch.JobTracker`)
each already hold a real `threading.Lock`, confirmed by direct
inspection. The per-tab session/gate/lock creation in
`webui.py`'s `_session_gate_lock_for_tab` correctly holds
`server.tab_state_lock` around its entire check-then-create sequence — no
TOCTOU gap there. A full pass across the 5+ independent SQLite stores
and the rest of `GoalRuntime`'s own tick loop is real, separate,
not-yet-done work (see the roadmap's own tracked backlog).

### 017 — Concurrency pass, second finding: `global_memory.py`'s store was unsafe across the ThreadingHTTPServer's own worker threads

**Severity**: HIGH (silent, near-total feature failure once enabled, not a
rare edge case — see below).
**Root cause**: `GlobalMemory.__init__` opened its one persistent
`sqlite3.Connection` with the stdlib default `check_same_thread=True`, and
the process-wide singleton (`get_default_memory()`) that owns it is reached
from `dispatch.py` on every top-level chat turn. `webui.py` runs a
`ThreadingHTTPServer` — one real OS thread per request — so any turn
landing on a thread other than whichever one happened to construct the
singleton first would raise `sqlite3.ProgrammingError: SQLite objects
created in a thread can only be used in that same thread`. Both real call
sites (`dispatch.py`'s ingestion and retrieval wiring) wrap that call in a
bare `try/except Exception: pass` (deliberately, so a memory failure can
never break a turn) — which meant this didn't crash, it just silently
no-opped. Confirmed live: a 16-thread probe run against the pre-fix class
(via `git show HEAD:...`, exec'd in isolation so nothing in the working
tree was touched) failed on all 16 threads. Since ingestion and retrieval
almost never land on the exact same single thread on a real threaded
server, this feature would have appeared to do close to nothing for any
real multi-tab user the moment `DOURMOUSE_GLOBAL_MEMORY=1` was set — a
second, independent instance of the exact "existing tests are evidence of
what someone thought should work" pattern from finding #011, and worse in
practice because the failure mode is total silence rather than a crash.
A second, smaller bug in the same area: `get_default_memory()`'s
lazy-singleton construction (`if _default_instance is None: _default_instance
= GlobalMemory()`) had no lock at all, so two threads racing in before
either finished constructing it could each build and use their own
`GlobalMemory`, leaking one connection permanently.
**Fix**: `check_same_thread=False` plus a real `threading.Lock` wrapping
every `sqlite3` touch (`__init__`'s table creation, `add`, `search`,
`close`) — the exact same pattern already established and audited this
session in `google_auth.py`'s `AuthStore` and `memory_store.py`. The
singleton constructor now uses the same double-checked-locking pattern
`goals.get_goal_store()` already uses (a direct precedent from this same
initiative's Phase 2 work), with its own module-level lock.
**Files changed**: `dourmouse/global_memory.py`.
**Tests added**: `TestGlobalMemoryConcurrency` in
`dourmouse/tests/test_global_memory.py` — `test_add_and_search_from_
multiple_threads_never_raises` (16 real threads hammering `add`/`search`
concurrently) and `test_get_default_memory_singleton_survives_a_
concurrent_first_call` (8 threads released simultaneously via a
`threading.Barrier`, asserts every thread got the identical instance).
Both fail against the pre-fix code (verified directly, not assumed) and
pass against the fix.
**Tests run**: `test_global_memory.py` (29/29), then full suite.
**Result**: fixed.
**Broader survey this same pass**: every module doing its own
`sqlite3.connect` was enumerated (12 files) and each own-write-path store
read line-by-line, not just grepped. `cache.py` opens a fresh connection
per call (no shared cross-thread connection object at all) and already
wraps each one in a module-level lock — confirmed correct, no change
needed. `google_auth.py` was already correct. `memory_store.py` and
`supabase_sync.py` were read in full: every single method touching
`self._conn` is wrapped in `with self._lock:` with no gaps, both already
use `check_same_thread=False` plus `PRAGMA journal_mode=WAL` and a real
30s `busy_timeout` — `memory_store.py`'s own comments document a prior,
separate live incident (2026-08-31, before this session) where
multi-process contention against this exact file was diagnosed and fixed,
so this store had already earned its hardening the hard way. Both
confirmed clean, no changes needed. `desktop_rag.py`, `history_import.py`,
`project_bookkeeper.py`, `project_import.py`, and `shared_rag.py` only
open read-only (`mode=ro`) connections into other applications' external
databases (Claude Code/Codex history, project files) — a different risk
category (a concurrently-writing external process, not our own
in-process race) that this pass did not evaluate. `global_memory.py` was
the one real gap: newer code than the others, written before this
session's `google_auth.py`/`goals.py` locking convention was established
as the house pattern, and it never got retrofitted.

### 018 — Git-history secret mining (real tool, full history, clean result)

**Severity**: n/a (verification pass, not a finding — but a real one this
time, not the working-tree-only pass from finding #006).
**Scope**: finding #006 explicitly flagged that only the tracked working
tree had been scanned, and that a secret committed once and later removed
from the tree would still be permanently readable in history — a real,
distinct exposure a `.gitignore` rule added after the fact does nothing to
close. This pass closes that gap. `gitleaks` (industry-standard, not
previously installed — installed via `brew install gitleaks`, a real dev
tool, same category as `ruff`) run against the full history of every
branch: `gitleaks git --log-opts="--all"`. 295 commits, ~166MB of diff
content, scanned in full.
**Result**: 168 raw matches, all triaged individually (grouped by file,
every distinct match value inspected, not just counted):
- **158** are in `jarvis/research_mesh/fields/exams/papers/_archives/*.html`
  — the exact same category finding #006 already identified in the working
  tree (archived, scraped, third-party academic web pages, part of the
  research corpus, not Dourmouse's own code or secrets). 155 are
  WordPress/CMS-style CSRF or theme cache-bust tokens repeated identically
  across multiple pages scraped from the same site (e.g. the identical
  token `5e7d06...` across five separate economics.ucdavis.edu archives) —
  client-side, non-secret values by construction. 2 are a genuine-looking
  Google API key (`AIzaSy...`), but it belongs to `umd.edu`'s own public
  page (almost certainly a client-side Maps/reCAPTCHA embed key, a common
  and normal practice for that key type) — a third party's already-public
  key, incidentally captured by scraping their page, not a Dourmouse or
  user credential and not an actionable leak for this project.
- **10** are in `dourmouse/tests/` (`test_v50_features.py` x5,
  `test_governance.py` x3, `test_google_services.py` x2,
  `test_live_feeds.py` x1) — every one individually inspected, and every
  one a deliberately fake, sequential-digit placeholder
  (`1234567890abcdef`, `sk-test-1234567890abcdef`, a JWT literally encoding
  `{"sub": "1234567890"}`), used exactly where you would expect: testing
  env-var key loading and testing the governance/redaction system's own
  ability to detect and redact secret-shaped strings. A redaction test
  cannot verify anything without a realistic-looking fake secret to redact.
- **0** matches touch `dourmouse/` outside `tests/`, `.env`,
  `dourmouse/local_secrets.py`, `dourmouse/_builtin_oauth.py`, or any real
  Dourmouse/user credential, anywhere across the full 295-commit history.
**Fix**: none needed — genuinely clean result, not an unexamined one.
**Files changed**: none.
**Tests added**: none (not code; `gitleaks` is a local dev tool, not a
project dependency, so nothing added to `requirements-dev.txt`).
**Tests run**: n/a.
**Result**: verified clean.

### 019 — First-ever type-checking pass (`mypy`), one real bug found and fixed

**Severity**: MEDIUM (the real bug: silent, permanent no-op of a real
feature — same failure shape as finding #017, smaller blast radius).
**Scope**: installed `mypy` (never run on this codebase before) with a
deliberately lenient config (`ignore_missing_imports`, no strict mode —
see `pyproject.toml`'s own comment on why: 337 previously-unchecked files
would drown in missing-annotation noise under strict mode on a first
pass). Ran against the whole `dourmouse/` package: 341 raw errors,
dominated by `attr-defined` (206) and `union-attr` (22) — expected, not
investigated further this pass, on a codebase with zero prior
annotations (dynamic attribute patterns, monkeypatched test doubles, and
un-narrowed `Optional`s are the overwhelmingly likely source, matching
ordinary experience adopting a type checker onto an untyped codebase, but
this is stated as a reasonable expectation, not verified line-by-line).
Every one of the remaining, rarer, higher-signal categories WAS
individually read and triaged, not sampled: `call-overload` (2),
`call-arg` (1), `no-redef` (1), `exit-return` (1), `list-item` (1),
`dict-item` (1).
**Result of that triage**:
- **One real, fixed bug**: `model_delegation.py`'s Gemini delegation path
  (`_run_cloud`) called `gemini_backend.call_gemini(..., on_usage=
  _on_usage)`, but `call_gemini` never actually implemented an `on_usage`
  parameter at all — every real call raised `TypeError`, defensively
  caught by a fallback branch that therefore always fired. Usage/cost
  tracking for every one-shot Gemini delegation call has been a silent,
  total no-op since this code was written, with zero test coverage on
  either side of the gap (no test called `_run_cloud` at all, and no test
  exercised `call_gemini` with `on_usage`). The sibling function
  `stream_gemini` already had a complete, correct, already-tested
  implementation of exactly this (`_extract_usage`, fire-once-on-last-
  frame semantics) — `call_gemini` re-implements the same request/parse
  loop locally (a documented, deliberate choice, not a mistake) but never
  had the usage half ported over. Fixed by adding the same `on_usage`
  parameter to `call_gemini` and wiring `_extract_usage` into its own
  loop, mirroring `stream_gemini`'s exact semantics. The now-permanently-
  dead `TypeError` fallback branch in `model_delegation.py` was removed
  (Rule: no error handling for a scenario that can no longer happen).
- **One trivial cleanup**: `general_roster.py`'s `_web_search_tool` had a
  redundant, byte-identical re-annotation of `last_exc` at its second
  assignment (`no-redef`) — cosmetic, zero behavior change, removed.
- **One test-only cleanup**: a fake context manager's `__exit__` in
  `test_google_services.py` was annotated to return `bool` while always
  returning `False` (`exit-return`) — misleading (implies `True`/suppress
  is possible when it never is); retyped to `-> None`.
- **Three confirmed false positives, verified by reading the real code
  and the real call site, not assumed**: `webui.py`'s `_handle_memory_
  remote_search` (`list-item`) and `atlas_lab.py`'s `_serialize_findings`
  sort key (`call-overload`) are both mypy inferring an overly narrow
  literal type from a `dict`/`list` construction in code with no explicit
  annotations — both behave correctly at runtime (`MemoryStore.search`'s
  real signature is `source: str | None`; the sort key's `dict` really
  does hold a `str` under `"verdict"` at runtime). `spotify_services.py`'s
  playback-payload branch (`dict-item`) is the real, correct Spotify Web
  API contract (`{"uris": [...]}` for tracks vs. `{"context_uri": ...}`
  for albums/playlists), not a mistake. None of the three were changed.
**Files changed**: `dourmouse/gemini_backend.py`, `dourmouse/
model_delegation.py`, `dourmouse/general_roster.py`, `dourmouse/tests/
test_google_services.py`, `pyproject.toml`, `requirements-dev.txt`.
**Tests added**: `test_call_gemini_usage_fires_exactly_once_with_the_
last_frame`, `test_call_gemini_a_failing_on_usage_callback_never_breaks_
the_real_reply`, `test_call_gemini_no_usage_metadata_means_on_usage_
never_fires` in `test_gemini_backend.py`; `TestRunCloud` (two tests, the
success path proving `usage` now actually populates, and a failure path
proving errors are still reported) in `test_model_delegation.py` — a
class that did not exist before, since `_run_cloud` had no direct test
at all.
**Tests run**: `test_gemini_backend.py` + `test_model_delegation.py`
(62/62), then full suite.
**Result**: fixed.

### 020 — Setup wizard claimed "Local, no key needed" on a machine that was never going to run local Ollama

**Severity**: MEDIUM (a real, live, user-facing accuracy bug in the
first-run experience — not a crash, but exactly the "the UI says one
thing, the system does another" class of bug this whole audit exists to
catch). User-caught, live, by asking a direct question about the running
setup wizard rather than reported as a symptom.
**Root cause**: `ui/setup.html`'s "Choose your brain" step calls
`detect_ollama()`, which only ever probes the LOCAL server at
`127.0.0.1:11434` — it has no way to know that
`config.load_ollama_config()` (the function that actually builds the
runtime client) unconditionally prefers Ollama Cloud the instant
`OLLAMA_API_KEY` is present in the environment, per an explicit,
deliberate 2026-09-14 user instruction ("Ollama should only use cloud
models from the api key, never local models") already documented in that
function's own docstring. On this exact machine, `OLLAMA_API_KEY` is
already set in the real `.env` — so the wizard showed "Local · Ollama, 8
model(s) available, works now, no key needed" and auto-selected it as the
default, while the real runtime was always going to use Ollama Cloud
instead, key or no key, local models or not. Two genuinely separate code
paths (the wizard's own local-only probe, and the runtime's real
local-vs-cloud decision) that can silently disagree, with the wizard's
copy never disclosing that a key elsewhere in the environment overrides
what it just detected.
**Fix**: `dourmouse/firstrun.py`'s `setup_status()` now also reports
`ollama_cloud_key_configured` (a plain, real `bool(OLLAMA_API_KEY)`
check, same honesty standard as the existing `has_nvidia_key` field).
`ui/setup.html` uses it to show "Ollama Cloud" (not "Local") with an
honest explanation whenever a key is present, regardless of whether local
Ollama also happens to be running, and to auto-select that option instead
of silently defaulting to a "Local" pick that was never going to be
honored.
**Files changed**: `dourmouse/firstrun.py`, `ui/setup.html`.
**Tests added**: `test_setup_status_reports_when_a_cloud_key_would_
override_local`, `test_setup_status_reports_false_with_no_cloud_key` in
`dourmouse/tests/test_webui.py::TestFirstRunSetup`.
**Tests run**: `TestFirstRunSetup` (7/7), then full suite. Live-verified
in the browser: `/api/setup/status` confirmed to carry the new field, and
the wizard's Step 2 screenshot-confirmed showing "Ollama Cloud" with the
honest explanation on this real machine.
**Result**: fixed.

### 021 — Gemini was fully wired but invisible on the connection-status report

**Severity**: MEDIUM (user-directed audit: "remember it should use the
ollama api key, the gemini api key, claude and codex cli, these 4 should
all be wired in" — checking this claim directly surfaced a real gap).
**Root cause**: `dourmouse/connections.py`'s `check_connections()` is the
one deterministic, honest report of every backend Dourmouse can actually
reach (surfaced in the UI as "N OF M CONNECTIONS LIVE" and via
`/api/connections`) — its own module docstring enumerates ollama, nvidia,
claude, codex, gmail, freebuff, slack, alpaca, atlas, server. Gemini was
never in that list, despite `gemini_backend.py` being a fully real,
working backend (`call_gemini`/`stream_gemini`, image generation, the
delegation path fixed in finding #019). `gemini_backend.gemini_status()`
already existed with the exact `{ok, detail, hint}` shape this report
needs — its own docstring says so explicitly ("the exact shape every
probe in connections.py returns, so this can be dropped into that report
without translation") — but nothing had ever actually called it. A
backend can be completely functional and still be invisible on the one
screen whose entire purpose is showing which backends are functional.
**Fix**: wired `gemini_status()` into `check_connections()`, mirroring
the existing per-backend `try/except` pattern. Also found and fixed the
same decorative `//` this session's Phase 3 sweep already eliminated from
every UI file, in `format_connections()`'s own header line
(`"CONNECTION STATUS //"` → `"CONNECTION STATUS"`) — server-side Python
text, not caught by that sweep since it only covered `ui/*.html`.
**Files changed**: `dourmouse/connections.py`.
**Tests added**: `test_gemini_tracks_env` in
`dourmouse/tests/test_connections.py::TestEnvGates`; added `"gemini"` to
the existing shape/off-by-default/format-text assertion loops in
`TestReportShape`/`TestFormat`. The hermetic `no_real_probes` fixture now
also clears `GEMINI_API_KEY`/`GOOGLE_AI_STUDIO_KEY` (this developer's real
`.env` sets `GEMINI_API_KEY`, the same leak class documented throughout
`dourmouse/tests/conftest.py` — see `docs/TESTING.md`).
**Tests run**: `test_connections.py` (18/18), then full suite.
**Result**: fixed.

### 022 — Cross-goal audit trail: the data existed, the global query and export did not

**Severity**: MEDIUM (a named acceptance test from the founding spec's own 20-test autonomy
checklist — "the user can inspect what the agent actually did through the activity/audit
history" — was not actually satisfiable at the product level before this).
**Context**: `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` (added the same day) names this
directly in Domain B. Investigating it properly turned up a codebase in much better shape than
assumed: `goals.py`'s `goal_events`/`log_event`/`goal_snapshot` already form a real, genuinely
append-only (no update/delete method exists for the table anywhere), comprehensive event log —
`create_goal`/`update_goal_status`/`cancel_goal`/`create_task`/`update_task_status` all already
auto-log real events, and `goal_runtime.py`'s own `_run_task`/`_execute_via_dispatch` already log
real `tool_call`/`tool_result`/`recovery_attempted` events with real tool arguments and results,
not placeholders. `/api/goals?id=<id>` already surfaces one goal's full event history via
`goal_snapshot()`.
**Root cause (the real, narrow gap)**: every one of those real query paths is scoped to a single
goal. There was no way to ask "what has the assistant done across every goal," and no
human-readable export at all — despite the founding spec's own explicit ask for exactly that
("Implement an event log/audit trail... The user should be able to inspect what the assistant
actually did") and the user's own later "Jarvis feature" list separately, independently asking
for "an immutable background ledger that... automatically render[s] them into an audit-ready
Markdown report." Confirmed live: `console.html` has zero rendering of goal events anywhere
(grepped, zero matches) — the backend data was real and complete, but neither globally queryable
nor human-presentable.
**Fix**: `GoalStore.all_events(since=None, limit=500)` — the same `goal_events` table, a second
query shape over it (cross-goal, newest-first, optional ISO-8601 time cutoff), not a second
logging system. `GoalStore.export_events_markdown(goal_id=None, since=None, limit=500)` — a real,
chronological, human-readable Markdown report, global or scoped to one goal. `GET /api/audit` in
`webui.py` exposes both (`?goal_id=`, `?since=`, `?format=markdown`), mirroring `/api/goals`'s own
exact route-handling style.
**A real bug caught by writing and running the tests, not assumed working**: the first
implementation treated `since` as a Unix-timestamp float and used `datetime.fromtimestamp(...)`
to render it — `goal_events.at` is actually stored as an ISO-8601 string (matching `_now()`'s own
real format throughout `goals.py`), so the first version raised `TypeError: argument must be int
or float, not str` the moment a real test exercised it. Fixed by changing `since` to accept the
table's own real ISO-8601 string type end to end (store, route, and the Markdown formatter's
`datetime.fromisoformat`), rather than converting types at a layer boundary.
**Honestly still not done**: a real UI surface in `console.html` (or a new dedicated
screen/panel) rendering this data for the user — the backend and API are real and tested, but
Acceptance Test 15 is not fully closed until a human can see this without calling the API
directly. Tracked as real, separate, not-yet-done work in `docs/GODSPEED_ROADMAP.md` Phase 3.
**Files changed**: `dourmouse/goals.py`, `dourmouse/webui.py`.
**Tests added**: `TestCrossGoalAuditTrail` (6 tests) in `dourmouse/tests/test_goals.py`;
`TestAuditEndpoint` (4 tests) in `dourmouse/tests/test_webui.py`, hitting a real
`ThreadingHTTPServer` over real HTTP, not a mock.
**Tests run**: `test_goals.py` (36/36), `test_webui.py::TestAuditEndpoint` (4/4), then full suite.
Live-verified against the real file-backed store via the browser (`fetch('/api/audit')` returned
a real, honest `{"events": []}` on a clean store).
**Result**: fixed (backend + API); UI surface tracked as separate follow-on.

### 023 — `create_goal` could report success while silently doing nothing, forever

**Severity**: HIGH (the single headline capability the founding spec opens with — "it should run
forever without prompting, in the background" — was a live no-op by default; a tool call that
returns success while accomplishing nothing is a worse failure mode than an error, per this
project's own Rule 2.2).
**Context**: user asked directly, after the previous self-assessment, "how far are we from the
121 page doc" — answering that honestly meant actually re-examining Domain B (§4 of
`docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`) rather than restating the existing estimate.
**Root cause**: `goal_runtime_enabled()` gated whether `webui.run_server` started a real
`GoalRuntime` worker thread — but `goal_tools.build_goals_subagent()` (the `create_goal`/
`add_tasks`/`get_goal_status`/`list_goals`/`cancel_goal` tool family) is registered by
`general_roster.py` completely unconditionally, with no check of that flag anywhere in that code
path. With the flag off (the original default), a user could ask Dourmouse to do something
autonomously, the model would call `create_goal`, the call would succeed and return a real goal
id, Dourmouse would tell the user work was now happening in the background — and because no
worker thread existed anywhere in the process to ever advance that goal, it would sit in
CREATED/READY state permanently. Confirmed: zero other place in the codebase checked this flag
before exposing the tool. This is precisely the founding spec's own explicitly named anti-pattern
("Do not create fake background execution in which the UI merely displays a spinner while no
real worker is operating") — worse than a spinner, since nothing on screen even hinted that
nothing was happening.
**Fix**: `goal_runtime_enabled()` flipped from opt-in (`DOURMOUSE_GOAL_RUNTIME=1` required) to
opt-out (`DOURMOUSE_GOAL_RUNTIME=0` to disable); default is now enabled. No safety gate was
removed by this change — a `REQUIRES_CONFIRMATION` tool inside an autonomous task still pauses at
`WAITING_FOR_APPROVAL` exactly as before, regardless of this flag; the flip only makes the
already-exposed, already-documented tool actually do what it already claimed to do.
**A real, newly-surfaced consequence handled directly**: this wiring (`run_server` starting a
real `GoalRuntime` thread) had zero direct test coverage before this pass, and flipping the
default meant every existing test using `test_webui.py`'s `server` fixture would now also start a
real background worker thread against whatever the process-wide goal-store singleton held at
that moment. Added a new autouse `_goal_runtime_off` fixture to `conftest.py` (same "hermetic by
default, opt in explicitly" convention as every other isolation fixture there) before running
anything, and added the first direct test coverage for the wiring itself
(`TestGoalRuntimeWiring`, 2 tests, confirming a real thread starts/doesn't start).
**Files changed**: `dourmouse/goal_runtime.py`, `dourmouse/webui.py`, `dourmouse/tests/conftest.py`.
**Tests added/updated**: `TestEnvGate` in `test_goal_runtime.py` (2 tests updated to the new
default); `TestGoalRuntimeWiring` in `test_webui.py` (2 new tests, previously zero coverage of
this wiring).
**Tests run**: `test_goal_runtime.py` (18/18), `TestGoalRuntimeWiring` (2/2), then full suite.
**Live proof, not just passing tests**: against the real dev-preview server (unmodified, no
special test flags, the actual default this finding changes), a goal was inserted directly into
its own live SQLite file from a separate external process — standing in for exactly what the
`create_goal` tool itself does — with zero further interaction. The server's own already-running
background worker, entirely on its own:
```
10:08:46.445  task -> READY        (inserted externally)
10:08:48.980  task -> RUNNING      (picked up by the real worker thread, ~2.5s later, unprompted)
10:08:53.678  task -> COMPLETED    (~4.7s of real dispatch execution: a real model call, real
                                     text back: "Hello." plus the real, unbypassed Grounded Mode
                                     honesty disclaimer about zero tool calls)
10:08:53.680  goal -> COMPLETED
```
This is the founding spec's own headline claim, verified true by default for the first time this
session, not asserted from a passing unit test.
**Result**: fixed.

### 024 — Crash-recovery drill (acceptance test 8), genuinely performed, not assumed

**Severity**: n/a (verification pass, not a bug — see below for why it was worth doing anyway).
**Context**: acceptance test 8 ("the runtime is restarted during a task; the task resumes from
durable state without duplicating external side effects") was flagged as real, honestly
not-yet-done verification work in finding #016 and in
`docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` Domain B — "a genuinely engineered crash-recovery
drill (kill -9 the process mid-goal, confirm resumption) has not been run." With finding #023
making the autonomous runtime the default live behavior for every install, this stopped being a
nice-to-have to verify eventually and became something worth confirming before moving on.
**Method**: against the real, unmodified dev-preview server (no special test hooks): inserted a
real goal/task directly into its live SQLite file from a separate process, ran a tight local poll
loop against the same database file (avoiding HTTP round-trip latency, which caused the first
attempt to miss the window entirely — the task was already COMPLETED by the time a browser-based
check landed), and sent `kill -9` to the real server PID the instant the task flipped to
`RUNNING` — a genuine process kill mid-dispatch, not a graceful shutdown.
**Result, in full, from the real event log**:
```
task_created
-> READY
-> RUNNING                                          (first real dispatch call starts)
[kill -9 sent here — the whole process dies mid-call]
[server restarted fresh, same as a real app relaunch after a crash]
recovery_attempted   {"reason": "found RUNNING at worker startup"}
-> RETRYING           error: "worker restarted mid-execution"
-> RUNNING                                          (second real dispatch call, automatic)
-> COMPLETED
```
The retried attempt produced a real, correct, on-topic answer (a real question about the Eiffel
Tower and Golden Gate Bridge, answered correctly). `GoalRuntime._recover_orphaned_tasks` (called
from `.start()`, before the tick loop begins) already existed, already correctly implements the
founding spec's own stated principle ("never blindly repeat an external side effect" — it routes
an orphaned `RUNNING` task through the normal retry/fail path rather than assuming it's still
alive or silently marking it complete), and worked correctly on the first real drill, no code
changes needed.
**Files changed**: none (verification only).
**Tests added**: none — this was a real, live, manual drill against a running process, not
something naturally expressed as a pytest unit test (the scenario requires an actual process
kill mid-flight, which is closer in spirit to `test_goal_runtime.py`'s own existing
`_recover_orphaned_tasks` unit-level coverage, already present, than to a new integration test).
**Result**: verified — acceptance test 8 closed for real, not assumed.

### 025 — Real independent verification (acceptance test 11), not self-reported

**Severity**: n/a (feature completion, the largest remaining named gap in Domain B — see
`docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`).
**Context**: `goal_runtime.py`'s own module docstring had named this as honestly-tracked v1
scope since it was first written: "Verification is 'the task's own turn completed without
raising' — NOT an independent check that the model's claim of success is actually true." With
tests 8 and default-on autonomy (findings #023/#024) closed, this became the clear next-highest
item.
**Design**: a genuine second, independent reasoning pass — a fresh `ChatSession` over an EMPTY
`DispatchRegistry` (no tools available at all, so the verifier cannot itself take any action,
gated or not; it can only judge), shown the REAL tool-call evidence collected during the actual
run (never the worker's own `final_text` alone, which is exactly the thing being checked — the
model that did the work does not get to grade its own homework). A genuine `NOT_VERIFIED`
verdict is routed through the normal failure/retry path, same as any other failure. If the
verifier itself cannot run, the real work is not thrown away and not blocked forever on a broken
checker, but the uncertainty is stated plainly in the result (`"verification could not run: ..."`
), never silently upgraded to verified. `TASK_STATES` already had a `VERIFYING` state defined
since this module's very first version — never once used until now, confirming this was designed
for from the start and simply never wired in.
**A real cost tradeoff, stated honestly, not hidden**: every task now makes two real dispatch
calls instead of one (the work, then the verification), roughly doubling per-task latency and
API cost. Deliberate: the founding spec is explicit that a confident-sounding fabricated success
is a worse failure mode than a slower, more expensive honest one, and this codebase's own Rule
2.2 agrees throughout.
**A real test-infrastructure problem caught before it silently broke the suite**: the existing
`_FakeSession` test double (`test_goal_runtime.py`) replaces `chat_module.ChatSession` globally —
since `_verify_completion` constructs its own `ChatSession` over that exact same reference, every
existing test in the file would have also exercised the fake verifier call, and its old default
unscripted response (`{"final_text": "done"}`) contains no verdict at all, which my own parsing
correctly treats as fail-safe `NOT_VERIFIED` — meaning literally every pre-existing test expecting
a task to reach `COMPLETED` would have started failing. Fixed by giving `_FakeSession` a
dedicated, separately-overridable `verification_response` (defaulting to a clean `VERIFIED`, so
every test written before this feature existed keeps its original, unrelated meaning) recognized
by a distinctive prompt-header marker, and by fixing 3 existing tests that asserted on the raw
call list (now containing the verification call too) to filter to task-only calls via a new
`_task_calls()` helper.
**Live proof, not just passing tests**: against the real, unmodified dev-preview server, a real
goal was created and watched end to end. The task genuinely passed through `RUNNING` →
`VERIFYING` (that dormant state, used for real for the first time) → `COMPLETED`, with a real,
independently-reasoned verdict logged to the real audit trail: *"The agent produced the word
'banana' in its output, which meets the task requirement. No external action was required, and
the evidence shows no tool call was made — all consistent with the task. VERDICT: VERIFIED."*
**Files changed**: `dourmouse/goal_runtime.py`, `dourmouse/tests/test_goal_runtime.py`.
**Tests added**: `TestIndependentVerification` (6 tests) — a genuinely verified task marked as
such; a `NOT_VERIFIED` verdict treated as a real failure, not a silent success; a `NOT_VERIFIED`
task retrying through the normal failure path; a broken verifier completing the real work
honestly-uncertain rather than destroying it; the verifier prompt carrying real tool evidence,
not just the worker's own claim; a real `verification` event logged to the audit trail. Plus 3
existing tests fixed to account for the new call.
**Tests run**: `test_goal_runtime.py` (24/24), then full suite. Live-verified against the real
dev-preview server as described above.
**Result**: fixed — acceptance test 11 closed.

### 026 -- The GOALS screen: the autonomous runtime finally has a UI (acceptance test 15)

**Severity**: n/a (feature completion -- the single largest UI gap in the whole product).
**Context**: findings #023-#025 hardened the Goal/Task runtime's real behavior (default-on
autonomy, crash recovery, independent verification), but every one of those findings was verified
through the API and the database directly. Before doing any UI work, every `ui/*.html` file was
grepped for `/api/goals` and `/api/audit` -- zero matches, in any file. The headline autonomous
feature of this whole product had no UI surface at all: a user could create a goal from chat and
it would genuinely run forever in the background, and there was no way to see it, its tasks, or
what it had actually done without calling the API by hand.
**Design**: a new GOALS screen in `ui/console.html`, following the exact same `SCREENS`/`show()`/
`pane-<name>` pattern every other screen in this console already uses (no new navigation idiom
invented). Polled every 4 seconds while the screen is the active one, not pushed over the shared
`/api/events` SSE stream ORCHESTRATION's live agent feed uses -- the goal runtime has no event-sink
wiring into that stream today, and adding one is real, separate runtime-layer work, stated plainly
as a deliberate scope line, not silently skipped. Shows every goal (status, priority, blocked
reason), a live cross-goal audit trail (finding #022, now also carrying finding #025's
`verification` events), and lets a task list be expanded per goal on demand (lazy-fetched via
`GET /api/goals?id=`, not bundled into the list poll, so an active session with many goals never
pays for task data it isn't looking at).
**The runtime's first real write action**: a CANCEL button on every non-terminal goal, backed by
a new `POST /api/goals/cancel` route in `webui.py`. Deliberately a thin route, not new logic:
`GoalStore.cancel_goal` already existed and was already tested (`test_goals.py`) -- safe against a
task mid-flight, since the worker checks goal status before starting any task. A resumable
per-task APPROVAL action (acceptance test 7's real remaining gap) was deliberately NOT added here
-- it needs real runtime-side design (when should a `WAITING_FOR_APPROVAL` task actually resume,
how does the worker recheck it) that a thin route over existing logic can't honestly provide;
tracked separately, not smuggled in under this UI pass.
**Live proof, not just passing tests**: against the real, unmodified dev-preview server (a fresh
isolated `.dev-preview-workspace`, `DOURMOUSE_UI_PORT=18765` -- the established test port, kept
well clear of the real desktop deployment's own port per the standing caution in
`docs/GODSPEED_ROADMAP.md`), two real goals were created directly in the live database and
watched end to end through real browser clicks: the goal list rendered correctly (6 goals total,
2 active, matching real leftover state from finding #025's own earlier live-verification runs in
the same workspace); clicking a goal's title expanded its real task list; clicking CANCEL on
"A goal nobody has touched yet." genuinely cancelled it (confirmed via a direct backend read, not
just a UI read) -- the active count dropped from 2 to 1, the goal moved into FINISHED as
`CANCELLED`, and a real `goal_status_changed` event appeared at the top of the live audit trail.
**Result**: fixed -- acceptance test 15 closed end to end (data, API, and now UI).
**Files changed**: `dourmouse/webui.py` (`POST /api/goals/cancel`), `ui/console.html` (GOALS
screen), `dourmouse/tests/test_webui.py` (`TestGoalsCancelEndpoint`, 4 tests).
**Tests added**: a real goal cancelled end to end over real HTTP; a missing `id` is a real 400,
not a silent no-op; an unknown goal id reports `{"ok": false}`, not an error; an already-terminal
goal cannot be "uncancelled" by this route.
**Tests run**: `test_webui.py -k "TestGoalsCancelEndpoint or TestAuditEndpoint or
TestGoalRuntimeWiring"` (10/10), then full suite.

### 027 -- The TIMETABLE screen: Domain C's real gap, the user's own explicit ask

**Severity**: n/a (feature completion -- one of the two explicit items in the user's own founding
request: "add a scheduling feature and such timetable").
**Context**: `schedules.py` (the `Schedules` store, `SchedulerRunner`) and its three chat-facing
tools (`schedule_recurring`/`list_schedules`/`cancel_schedule`, `general_roster.py`) already
existed and were already well tested -- a real, honest, working backend, confirmed by
`docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` Domain C before starting. What was missing,
confirmed by grep before writing any code: zero HTTP routes over that store, and zero UI files
referencing them. A user could create a real recurring routine through chat and it would run
forever, correctly, in the background -- with no visible, editable timetable, exactly the gap the
user's own request named.
**Design**: three new routes (`GET /api/schedules`, `POST /api/schedules/toggle`, `POST
/api/schedules/remove`) and a new TIMETABLE screen in `ui/console.html`, following the exact same
pattern the GOALS screen (finding #026) just established. `GET /api/schedules` computes
`schedule_description`/`next_run` server-side via the store's own existing `describe_spec`/
`describe_next_run` helpers, so the UI never re-derives schedule date-math in JS. A genuinely new
capability had to be added to the store itself: `Schedules.set_enabled()` -- pause/resume,
deliberately NOT the same as `remove()` (a paused entry keeps its id and its `last_run` history;
resuming it is not recreating it from scratch). `SchedulerRunner._tick_once` already skipped any
entry with a falsy `enabled` field, so this one store method is the complete implementation --
proven by a new test that pauses a due entry and confirms the real runner genuinely skips it, not
just that the store field flipped.
**A deliberate scope line, stated plainly**: no creation form in the UI. Typing a plain sentence
("every Monday at 8am, review my email and brief me") into any existing chat composer already
works today, for real, through the same `schedule_recurring` tool this finding's own live proof
exercised -- building a second, parallel natural-language parser bolted onto this one screen would
duplicate real reasoning the model already does, for no honest gain. Acceptance test 4 (editing a
routine's schedule from the UI) was also deliberately NOT built: no `update()` method exists on
the store, and faking "edit" as delete-then-recreate would silently lose the entry's id and
history -- real, separate, not-yet-done work, same category of considered exclusion as finding
#026's per-task approval gap.
**Live proof, not just passing tests**: a real HTTP POST to `/api/chat` (the same endpoint the
real UI composer calls) with the plain sentence "Schedule a recurring routine: every Monday at
8am, call list_tasks with no arguments" -- no test hooks, no scripted backend -- was handled by
the real, currently-configured Ollama Cloud backend (`gpt-oss:20b`), which reasoned about it,
called the real `schedule_recurring` tool with `{"tool": "list_tasks", "schedule_text": "every
Monday at 8:00", "arguments": {"include_done": false}}`, and got back `SCHEDULED sched-001:
list_tasks every Monday at 08:00 -- next run 2026-09-21 08:00`. That real entry then appeared
correctly in the TIMETABLE screen over real browser clicks: PAUSE moved it to a PAUSED section
with a real dot-color change and the button relabeled RESUME; RESUME moved it back to ACTIVE;
DELETE removed it for good, confirmed by a direct backend read (`GET /api/schedules` returning
`{"schedules": []}`), not just a UI read.
**Files changed**: `dourmouse/schedules.py` (`Schedules.set_enabled`), `dourmouse/webui.py` (3
routes), `ui/console.html` (TIMETABLE screen), `dourmouse/tests/test_schedules.py` (3 tests),
`dourmouse/tests/test_webui.py` (`TestSchedulesEndpoints`, 6 tests).
**Tests added**: `set_enabled` pauses/resumes without losing the entry's id or history; an unknown
id reports `False`; a paused entry is genuinely skipped by the real runner even when due; each of
the 3 new HTTP routes over real requests (list with human-readable fields, toggle, toggle missing
id is a real 400, remove, remove of an unknown id reports `{"ok": false}` not an error).
**Tests run**: `test_schedules.py` (25/25), `test_webui.py -k TestSchedulesEndpoints` (6/6), then
full suite.
**Result**: fixed -- Domain C's real gap closed; acceptance tests 1-3 genuinely demonstrated live,
test 4 explicitly named as remaining, separate work.

### 028 -- Domain D: self-extension, "the single most architecturally sensitive item"

**Severity**: n/a (feature completion -- the other explicit item in the user's own founding
request, paired with scheduling: "DOURMOUSE should also be able to add new agents add itself new
features and scope").
**Context**: `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` Domain D names this exactly: "a system
that writes and registers its own new tools/agents needs a review gate, or it becomes an
unbounded-trust problem." Confirmed by an architecture survey before writing any code: every tool
in this codebase is a hardcoded Python object statically imported at edit time; there was no
plugin/dynamic-loading mechanism anywhere, and zero existing scaffolding (`agent_smith` existed
only as a placeholder name in the requirements doc).
**Design**: a new `dourmouse/self_extensions.py` module and `agent_smith` subagent
(`general_roster.py`), following the four constraints Domain D itself lists literally:
1. `draft_tool` writes a real draft (capability gap, tool name, description, parameters schema,
   handler source, test source) to a workspace staging file. It never touches the live registry.
2. Approval is human-only: `POST /api/self_extensions/approve` (webui.py), reachable only from
   the new AGENT SMITH screen's own APPROVE button. There is NO approve or reject tool anywhere
   in the chat-facing roster -- a dedicated regression test
   (`TestAgentSmith.test_no_approve_or_reject_tool_exists_anywhere_in_the_roster`) asserts this
   directly against the live registry, not just by code review.
3. `_write_approved_module` ALWAYS forces `Permission.REQUIRES_CONFIRMATION` on the written
   `ToolSpec`, regardless of anything the draft's own source claims -- a self-added tool can never
   grant itself unattended execution.
4. Approval re-validates syntax, writes the real module, sanity-imports it, then runs the draft's
   own test file through a REAL `pytest` subprocess -- never mocked, because mocking away the
   actual test run is exactly the "side channel" acceptance test 4 says must never exist. A
   failing test blocks approval (`APPROVAL_FAILED`, honest reason, the half-written module
   deleted) rather than silently merging broken code. A real, permanent, per-installation
   changelog (`workspace/self_extensions/CHANGELOG.md`) gets a new entry on every real approval.
An approved tool becomes callable only after a real process restart -- `general_roster.py`'s new
loader scans `workspace/self_extensions/approved/*.py` once, at `build_general_registry()` time,
exactly like any other Python import. No live code injection into a running process, on purpose
(Rule 2.8): claiming a self-added tool is live before a restart would itself be a fabricated-
success bug.
**A real bug caught live, not in a unit test**: the first design had the changelog live at
`docs/SELF_EXTENSIONS.md` in the git-tracked repo (matching the requirements doc's own literal
path). Live-verifying the real HTTP approval route exposed the actual problem: `webui.py`'s
`POST /api/self_extensions/approve` never threaded a `repo_root` override through, so every real
approval -- including from this session's own automated `test_webui.py` runs -- silently wrote
into the ACTUAL tracked repository as a side effect of approving a draft. Fixed by moving the
changelog to `workspace/self_extensions/CHANGELOG.md`, workspace-relative like every other piece
of self-extension state, consistent with this module's own stated design principle ("a
self-added tool is a property of one installation, not something this change silently ships to
every other Dourmouse install"). The stray polluted file the bug had already written into the
real repo (never committed) was deleted; every test call site updated to match; full 341-test
re-run confirmed clean afterward.
**A second real gap caught live**: the first live draft (a real, unscripted Ollama Cloud
`gpt-oss:20b` call, asked to add `celsius_to_fahrenheit`) produced a genuinely correct
`handle()` implementation but a test file written as bare module-level `assert` statements --
valid Python, but never collected by pytest as real tests ("no tests ran"), so approval correctly
refused it (`APPROVAL_FAILED`). Not a bug in the approval logic -- it worked exactly as designed,
refusing to silently accept a test file that wasn't actually a real test. Fixed the ROOT cause
instead of the symptom: `draft_tool`'s own tool description (told to every future model call, not
just this one) now explicitly states the `def test_something():` requirement and explains why a
bare assert is invisible to pytest. Re-tried live with the improved instructions and the same
real model self-corrected on the next draft, producing properly collectible tests.
**Live proof, in full, against the real dev-preview server**: a real, unscripted chat message
("You have no tool to convert Celsius to Fahrenheit...") made the real model call `draft_tool` for
real, producing genuinely correct Python. The draft was reviewed in the real AGENT SMITH screen
(full `handler_source`/`test_source` rendered, readable) and approved for real over HTTP; the real
subprocess pytest run genuinely passed; the real changelog was written to the real workspace path,
confirmed the actual tracked repo was untouched. The real server was then restarted (a genuine
process kill and relaunch, not simulated), and `GET /api/roster` confirmed
`celsius_to_fahrenheit_v2` was now really registered under a new `self_extended` subagent. A
FRESH chat thread (no prior context biasing the model) was then asked to use the tool: the real
model called it, and the real `REQUIRES_CONFIRMATION` gate genuinely paused the request
(`confirmation_requested`, id `confirm-1`) -- proving a self-added tool categorically cannot
bypass human confirmation, the single most important property this whole domain exists to
guarantee. Only after a real `POST /api/confirm` did the tool actually execute, returning the
mathematically correct result (`"98.6"` for 37°C), and the model reported it back correctly
(`"37 °C equals 98.6 °F."`).
**A third real gap, caught by the full suite, not live testing**: adding a new subagent broke two
pre-existing exhaustive-set tests elsewhere in the codebase that had nothing to do with this
feature directly -- `test_dispatch.py`'s own full roster-shape assertion (same class of fix as
`test_general_roster.py`'s, above) and `test_model_delegation.py`'s routing-policy completeness
check, which requires every real subagent to have an explicit local-only/cloud-ok classification.
`agent_smith` was classified `_LOCAL_ONLY_AGENTS` (`model_delegation.py`) -- it drafts real Python
source that becomes part of this very system, the same sensitivity class as the existing
`dev_coding`/`code_*` agents right next to it, not public-web material. A first attempt also
proactively classified `self_extended` (the approved-tools subagent) the same way, which broke
the SAME test's other direction (a policy entry for an agent that doesn't currently exist is
itself an error in this codebase's own convention, matching the already-documented reasoning for
why removed agents like the old `atlas`/`atlas_cmd`/`atlas_ui` names were deleted rather than
left dangling) -- reverted; any unclassified agent already defaults to local routing, so
`self_extended` is never accidentally cloud-routed by this deliberate omission.
**Files changed**: `dourmouse/self_extensions.py` (new), `dourmouse/general_roster.py`
(`agent_smith` subagent + `self_extended` startup loader), `dourmouse/webui.py` (4 routes),
`dourmouse/model_delegation.py` (routing policy), `ui/console.html` (AGENT SMITH screen),
`dourmouse/tests/test_self_extensions.py` (new, 21 tests), `dourmouse/tests/test_general_roster.py`
(`TestAgentSmith`, 10 tests, plus the roster-shape test's expected-subagent set),
`dourmouse/tests/test_webui.py` (`TestSelfExtensionsEndpoints`, 6 tests),
`dourmouse/tests/test_dispatch.py` (roster-shape set).
**Tests added**: 37 new, spanning syntax/name validation, the draft store, the full approve/reject
paths (a genuinely correct draft approved for real; a draft whose own test fails never silently
merged; a name collision with a real tool refused; bad syntax caught again at approval time even
after passing the initial chat-tool check; an already-decided draft can't be approved or rejected
twice), the chat-facing tools (including the critical "no approve/reject tool anywhere in the
roster" invariant), the startup loader (an approved extension becomes a real live tool after a
registry rebuild; a hand-corrupted approved file never crashes server startup), and the full HTTP
surface.
**Tests run**: `test_self_extensions.py` + `test_general_roster.py` + `test_webui.py` together
(341/341), then full suite.
**Result**: fixed -- Domain D's core loop (draft, human-only review, forced confirmation tier,
real test enforcement, real changelog, restart-gated activation) is real and live-verified end to
end. Acceptance tests 1-4 all genuinely demonstrated, not assumed. Not yet built, real and
separate: promoting an approved tool's permission tier (there is no path to ever let a
self-added tool run unattended -- a deliberate, not accidental, absence); a UI action to
re-approve a corrected draft after `APPROVAL_FAILED` (today the model must draft a fresh one).

### 029 -- The resumable per-task approval ticket (acceptance test 7)

**Severity**: n/a (feature completion -- the largest remaining named gap in Domain B, called out
explicitly in findings #025, #026, and #027 as real, separate, not-yet-done work every time it
came up).
**Context**: `goal_runtime.py`'s own module docstring named this honestly since the module was
first written: a REQUIRES_CONFIRMATION tool inside an autonomous task either runs (the GLOBAL
`DOURMOUSE_AUTO_APPROVE` toggle) or the task waits forever at `WAITING_FOR_APPROVAL` -- the only
way past it was flipping that toggle, approving every gated action on every task everywhere, not
just the one a human actually reviewed. `GOAL_ACTIVE_STATES` (`goals.py`) has never included
`WAITING_FOR_APPROVAL`, so a waiting task/goal is invisible to the worker's own `tick()` forever
until something explicitly moves it back to an active state -- nothing did.
**Design**: `GoalStore.resolve_task_approval(task_id, approved, reason)` -- the one real path in
or out of `WAITING_FOR_APPROVAL`. Approving writes a one-time ticket into the task's own `result`
column (no schema migration -- reuses the existing flexible JSON field) and moves task -> READY,
goal -> EXECUTING, so the worker's own next tick picks it back up. `goal_runtime.py`'s `_run_task`
reads and immediately CONSUMES the ticket in the same `update_task_status` call that marks the
task `RUNNING` again -- before the task's own dispatch call ever runs -- so it can never silently
carry over to an unrelated later retry of the same task (a crash mid-run, an unrelated failure-
then-retry, or a second different gated action hit during the same run all leave the ticket
already consumed, requiring fresh human approval rather than inheriting a stale blanket grant).
The confirmation-gate builder itself was extracted from a bare module-level function
(`_autonomous_confirmation_gate`) into `_confirmation_gate_for(approved_this_run)`, a small,
directly unit-testable closure factory -- `_FakeSession` (this test file's own scripted double)
replaces `ChatSession` entirely and never actually calls a real confirmation_gate, so testing the
gate LOGIC needed to happen at this level, not only through the harder-to-probe full dispatch
path. Declining is immediate and explicit: the task goes straight to `FAILED` with the human's own
reason and the goal straight to `BLOCKED`, not left to generic attempt-count-exhaustion heuristics
that would otherwise never fire (a WAITING_FOR_APPROVAL task typically has plenty of attempts left
when it first waits) and could leave a declined task silently stalling the goal forever with no
clear signal anything is wrong.
**A real bug caught by the full HTTP test, not code review**: the new `/api/goals/tasks/approve`
route's first version raised `UnboundLocalError: cannot access local variable 'get_goal_store'`
on every real request. Root cause: `do_POST` is one large function, and a DIFFERENT `elif` branch
elsewhere in it already does `from dourmouse.goals import get_goal_store` -- Python's scoping
rules make an imported name local to the WHOLE enclosing function the moment it's imported
ANYWHERE in that function's body, even though only one branch ever executes per request. Fixed by
adding the same local import to this route's own branch, matching the exact pattern every other
branch in `do_POST` already independently follows.
**Live proof, against the real dev-preview server, no test hooks**: a real goal/task was created
directly in the live database describing a `send_draft` call (a real REQUIRES_CONFIRMATION tool).
The real worker picked it up, the real model called the tool for real, and the real confirmation
gate genuinely declined it (`DECLINED BY USER: Send a email message to test@example.com...`),
moving the real task and goal to `WAITING_FOR_APPROVAL` -- confirmed by polling the live database
directly, not assumed. The task was then approved through a real click on the real GOALS screen's
new APPROVE button (the task expanded to show its own real reason first, matching the same
deliberate "read it before you approve it" pattern as Agent Smith's own review screen). The real
audit trail shows the complete, honest sequence: `approval_resolved` (approved: true) ->
task READY -> goal EXECUTING -> task RUNNING -> a SECOND real `send_draft` call, this time NOT
declined -> `NOT CONFIGURED: no messaging channel backend wired yet... nothing was sent`. The
approval ticket worked exactly as designed. What happened next was an unplanned, genuinely useful
demonstration of a separate safeguard (finding #025) still doing its own job even after a human
approval: the independent verifier correctly judged that a real tool call returning "not
configured, nothing sent" does NOT mean the task's actual objective (sending a message) was
accomplished, marked it `NOT_VERIFIED`, retried once, reached the same honest conclusion, and the
goal correctly finished `BLOCKED` with a precise, accurate reason -- a human approving the GATE
never means the system stops checking whether the work actually happened.
**Files changed**: `dourmouse/goals.py` (`resolve_task_approval`), `dourmouse/goal_runtime.py`
(`_confirmation_gate_for`, ticket consumption in `_run_task`), `dourmouse/webui.py`
(`POST /api/goals/tasks/approve`), `ui/console.html` (APPROVE/DECLINE on a waiting task row),
`dourmouse/tests/test_goals.py` (`TestResolveTaskApproval`, 7 tests), `dourmouse/tests/
test_goal_runtime.py` (`TestConfirmationGateFor` + `TestResumableApprovalTicket`, 6 tests),
`dourmouse/tests/test_webui.py` (`TestGoalsTaskApprovalEndpoint`, 4 tests).
**Tests added**: 17 new, spanning the store method (approve resumes both task and goal; the
one-time ticket is written correctly; declining fails the task and blocks the goal with the real
reason, with and without an explicit reason given; a task not actually waiting can't be resolved;
an unknown task id reports false; a real audit event is logged), the gate builder in isolation
(a fresh ticket approves regardless of global auto-approve; no ticket and no global auto-approve
declines; global auto-approve still works without a per-task ticket), the full worker resume path
(an approved task is genuinely picked up and completes on the next tick; the ticket is consumed by
the run it authorized, not left dangling for an unrelated future retry to inherit; declining fails
the task for good and the worker never touches it again), and the full HTTP surface.
**Tests run**: `test_goals.py` + `test_goal_runtime.py` + `test_webui.py` together (279/279), then
full suite.
**Result**: fixed -- acceptance test 7 closed, live-verified against the real dev-preview server
with a real gated tool call, a real decline, a real human approval click, and a real resume.

### 030 -- Schedule editing, the real remaining half of Domain C (acceptance test 4)

**Severity**: n/a (feature completion -- the single named remaining gap in Domain C after finding
#027 closed the rest of it).
**Context**: `Schedules` (`schedules.py`) had `add`/`list`/`remove`/`mark_run`/`set_enabled` but no
way to change an existing schedule at all -- deleting and recreating loses the entry's id,
`last_run` history, and enabled/paused state, which is not the same thing as editing it.
**Design**: `Schedules.update_spec(schedule_id, schedule_text)`, deliberately scoped to WHEN a
routine runs, never WHAT it does. Re-parses `schedule_text` through the exact same
`parse_schedule()` the create path already validates with -- the same real honesty guarantee, no
double standard for an edit -- and replaces only `spec`/`schedule_text`, leaving
`id`/`tool`/`arguments`/`created_at`/`last_run`/`enabled` untouched. A rejected edit (unparseable
text) raises the same `ValueError` `parse_schedule` already raises for the create path, and the
original schedule survives completely untouched -- never partially applied. Editing the tool or
its own arguments would need the same validation the create path already does (must resolve to a
real, existing, `REGULAR`-tier tool) -- deliberately not bundled into this one method, so a
schedule's identity and history are never at risk from a bad edit to something unrelated.
**A real bug caught before it ever ran, not live**: the new `/api/schedules/update` route's first
version referenced `schedules_module` without importing it in its own branch -- the exact same
Python function-scoping trap finding #029 already caught for `get_goal_store` (`do_POST` is one
large function; a name imported anywhere in it becomes local to the WHOLE function, even though
only one branch executes per request). Caught by pattern-matching the earlier finding rather than
waiting to hit the same `UnboundLocalError` again at runtime -- fixed with the same local import
every other branch in `do_POST` already independently carries.
**Live proof, against the real dev-preview server**: a real schedule (`every Monday at 9:00`) was
created directly in the live store. A real click on the real TIMETABLE screen's new EDIT button
(a `prompt()` pre-filled with the schedule's own current text, honest about scope: "what this
routine DOES stays the same, only WHEN it runs changes") changed it to `every Friday at 17:00`.
Confirmed by a direct backend read, not just the UI: `spec.weekday` moved from `0` to `4`,
`next_run` recomputed correctly to the following Friday, and `tool`/`arguments` were provably
untouched.
**Files changed**: `dourmouse/schedules.py` (`update_spec`), `dourmouse/webui.py`
(`POST /api/schedules/update`), `ui/console.html` (EDIT button on a schedule row),
`dourmouse/tests/test_schedules.py` (5 tests), `dourmouse/tests/test_webui.py` (3 tests).
**Tests added**: 8 new, spanning the store method (reschedules the real job; never touches
tool/arguments; preserves enabled/last_run history; an unparseable edit is rejected with the
original untouched; an unknown id raises) and the full HTTP route (reschedules for real over
HTTP; an unparseable edit is a real 400 with the original untouched, not a silent no-op; missing
fields is a real 400).
**Tests run**: `test_schedules.py` + `test_webui.py` + `test_general_roster.py` together
(357/357), then full suite.
**Result**: fixed -- Domain C fully closed. All 4 acceptance tests now real: creation (finding
#027), survival across restart and honest catch-up (both pre-existing, already tested), and now
editing.

### 031 -- Deterministic success-criteria check, Domain B's real remaining nuance

**Severity**: n/a (feature completion -- the exact gap finding #025's own docstring named: "no
such criteria field exists on a TASK yet ... a richer criteria-based check remains real, separate,
not-yet-done follow-on work").
**Context**: `success_criteria` (a `list[str]`) has existed on every goal since `goals.py`'s very
first version -- settable through `create_goal`'s own schema, stored, retrievable via
`goal_snapshot()` -- but nothing anywhere ever read it back. A goal could declare "the report
cites at least 3 real sources" and that declaration was pure documentation: `_complete_goal`
marked a goal `COMPLETED` the instant every task's own independent verification passed, with zero
check against what the goal itself had said would count as actually done.
**Design**: `_verify_goal_criteria`, the goal-scoped sibling of finding #025's own
`_verify_completion`, called from `_complete_goal` only when a goal declares at least one
criterion (zero added latency or cost for the common case of a goal with none). Same real
independent-reasoning shape -- a fresh, tool-less `ChatSession` so the check cannot itself take
any action -- but shown every task's own real result summary and asked to judge EACH declared
criterion individually, not one vague "was this done" question, ending with a single
`VERDICT: SATISFIED`/`VERDICT: NOT_SATISFIED` line. `GOAL_STATES`'s own `VERIFYING` value, defined
since the module's first version and never once used for a goal before (only for tasks), is now
real. An unsatisfied verdict routes the goal to `BLOCKED` with the real reasoning naming which
criterion failed -- immediate and explicit, the same shape as the pre-existing permanent-task-
failure path, not left to a generic heuristic that might never fire. The real, already-completed
task work is never thrown away: a goal blocked on its own criteria still shows every task
`COMPLETED`, exactly as it happened. If the checker itself cannot run, the goal still completes
(never blocked forever on a broken checker) with the uncertainty stated plainly, matching finding
#025's own fail-safe design precisely.
**A real prompt collision caught before it ever ran, not live**: the new goal-criteria prompt's
first draft opened with the exact same words as `_verify_completion`'s own
`_VERIFIER_PROMPT_MARKER` ("You are a strict, skeptical verifier") -- in the real test double
(`_FakeSession`, `test_goal_runtime.py`) this would have made the two checks indistinguishable,
silently routing goal-criteria checks through the per-task verifier's own scripted response and
vice versa. Caught while writing the test fixture, before ever running a test: reworded the
opening to be genuinely distinct, and gave `_FakeSession` a third, separately-scripted
`goal_criteria_response` track.
**A second real gap closed in the same pass**: `create_goal`'s own `success_criteria` parameter
had zero description in its JSON schema -- nothing ever told the model this field mattered, which
plausibly explains why it went unused in practice even though it always existed. Given a real
description now that it is genuinely enforced, and a real, unscripted Ollama Cloud call
immediately used it meaningfully on the very first try (see live proof below).
**Live proof, against the real dev-preview server, no test hooks**: a real chat message asked the
model to create a goal with a deliberately unsatisfiable criterion (a specific token the task's
own simple response would never contain). The real model populated `success_criteria` correctly
on its own. The real worker ran the task, which genuinely said hello and was independently
verified as accomplishing exactly what it was asked -- then the goal itself correctly failed its
OWN declared bar and went `BLOCKED`, not `COMPLETED`, with the real reasoning
("Criterion: XYZZY-UNIQUE-TOKEN-99 -- Not satisfied (no evidence of that token in the output)")
visible in the real audit trail and the real GOALS screen, task still shown `COMPLETED`
underneath. The GOALS screen needed one small addition (a "Must satisfy: ..." line on expand) to
surface the declared criteria; the existing `blocked_reason` display already handled the rest with
zero changes, confirming the original screen design was general enough for this new case for
free.
**Files changed**: `dourmouse/goal_runtime.py` (`_verify_goal_criteria`, `_complete_goal`),
`dourmouse/goal_tools.py` (schema description), `ui/console.html` (success-criteria display),
`dourmouse/tests/test_goal_runtime.py` (`TestGoalSuccessCriteria`, 5 tests, plus a third
scripted-response track on `_FakeSession`).
**Tests added**: a goal with no criteria completes exactly as before with zero extra LLM round
trip; satisfied criteria complete the goal with the real reasoning kept; unsatisfied criteria
block the goal instead of completing it, with the real completed task work left intact; a broken
criteria checker completes the real work honestly-uncertain rather than destroying it; a real
`verification` (scope `goal`) event is logged to the audit trail.
**Tests run**: `test_goal_runtime.py` (35/35), then full suite.
**Result**: fixed -- Domain B's last named nuance closed, live-verified with a real model, a real
unmet criterion, and a real block.

---

### 032 -- `delegate_parallel` reported a branch OK when it had only run out of turns

**Severity**: HIGH (correctness / self-reported success -- a parent agent reading `(OK, 11.39s)`
for a branch that never actually finished its job has no way to know the result is unreliable;
the exact class of bug finding #025 already closed for goal-runtime tasks, found live on a
different execution path that finding never touched).
**Context**: `Domain F`'s own harsh acceptance test (`docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`:
"give a goal that genuinely needs 3+ specialists ... confirm a real synthesized final result, not
three disconnected fragments") was run live against the real dev-preview server: a real chat
message asked the model to fan out three specialist branches via `delegate_parallel`. One branch
came back reporting `(OK, 11.39s)` with body text that was, verbatim, the dispatch loop's own
`max_turns`-exhaustion fallback string ("I wasn't able to reach a complete answer within my tool
budget ... Try rephrasing the question more narrowly, or ask again"). The branch had picked
`max_turns: 1` for a task that plainly needed several tool calls, `dispatch.py`'s real forced-
synthesis mechanism kicked in exactly as designed, and `delegate_parallel`'s own `_run_one` still
marked it `ok: True` -- "the dispatch call didn't raise an exception" was being reported as
"the branch actually accomplished its task," with nothing to tell them apart.
**Design**: `dispatch.py` already emits a real, structured `{"type": "budget_exhausted", ...}`
transcript entry the instant `max_turns` is exhausted, unconditionally, before the forced
synthesis call is even attempted -- a pre-existing signal `delegate_parallel` never read. `_run_one`
now sets a real `incomplete` flag by checking the branch's own returned transcript for that entry.
`_format_delegate_parallel_result` reports `succeeded`/`incomplete`/`failed` as three separate
counts (not folded into a binary ok/fail), shows `INCOMPLETE` instead of `OK` in that branch's own
status line, and appends an explicit warning to its body text -- while still keeping the branch's
own real partial text intact underneath the warning, never discarding a best-effort partial answer
just because it did not finish cleanly. Zero cost for the common case: a branch that finishes
within its turn budget is completely unaffected, same `ok: True`, same `OK` status line.
**A second real gap closed in the same pass**: `max_turns`'s own JSON-schema description (identical
in both `delegate_task` and `delegate_parallel`) said nothing about what a turn actually is or how
many a real task needs -- a plausible root cause of the model's own `max_turns: 1` choice for a
multi-step job. Given a real description: `1` is enough only for a single lookup, multi-step work
needs `3-5`, and picking too low means getting cut off mid-work.
**Live proof, real test double, no mocked shortcut**: `test_self_dispatch.py`'s own `FakeClient`
genuinely loops a branch through one real tool-call turn, its own real forced-synthesis call once
`max_turns: 1` is exhausted, then the parent's own next turn after `delegate_parallel` returns --
the exact same "repeat the last queued response once only one remains" mechanism already proven in
`test_dispatch.py::test_max_turns_bounds_looping_model`, applied here through the real
`delegate_parallel` tool path for the first time. The real formatted output shows `[branch 0] ...
INCOMPLETE`, the real `"ran out of its own turn budget"` warning, and the branch's own real partial
text ("best effort from the branch"), all produced by the genuine code path, not asserted against
a mock.
**Files changed**: `dourmouse/general_roster.py` (`_run_one`'s `incomplete` flag,
`_format_delegate_parallel_result`, both `max_turns` schema descriptions).
**Tests added**: `test_self_dispatch.py::TestDelegateParallelTool::
test_a_branch_that_exhausts_its_turns_is_marked_incomplete_not_silently_ok` -- one branch genuinely
exhausts `max_turns: 1` through the real dispatch loop; asserts the real `incomplete` count, the
real `INCOMPLETE` status string, the real warning text, and that the branch's own partial text is
never discarded.
**Tests run**: `test_self_dispatch.py` (43/43), then full suite.
**Result**: fixed -- a parent agent reading a `delegate_parallel` result can now tell a genuinely
finished branch from a best-effort partial one, closing the exact self-reported-success gap finding
#025 closed for goal-runtime tasks, now closed on this second, independent execution path too.

---

### 033 -- `research_mesh`: the orphaned jarvis package rebuilt as a real, chat-reachable capability

**Severity**: n/a (feature -- a real, user-directed rebuild of a real, previously-orphaned package).
**Scope correction (2026-09-19, same-day)**: this was initially framed as closing the founding
spec's own workstream A ("a Claude-Code-architecture translation for a distributed research
network") -- corrected by direct user instruction: workstream A's real target is a distinct "3
device research network" (this Mac, the Dell compute node, the DOURMOUSE desktop -- real physical
machines already partially wired via the existing `compute` subagent and the DOURMOUSE desktop
sync/history work tracked in prior-session memory), which the 500-field jarvis mesh rebuilt here is
explicitly NOT. `research_mesh` stands on its own merits as a real, valuable, now-working
capability (field-specialist qualification against real held-out exam corpora) -- it is simply not
what "the distributed research network" means in the founding spec's own terms. See
`docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`'s own Domain A/G sections for the corrected split.
**Context**: user instruction: "forget the existing one and rebuild." Investigation found "the
existing one" was not the cross-device JARVIS effort tracked in prior-session memory, but a real,
concrete package already sitting in this repo: `jarvis/research_mesh/agents/` -- a field-specialist
qualification mesh (`core.py`/`study.py`/`exams.py`/`store.py`/`brain.py`/`pipeline.py`, 2395
lines, its own real test suite) with a genuinely well-designed, already-tested state machine: a
field-agent studies a real, held-out academic exam corpus for one field, sits a real exam, fails
honestly when held out, remediates, retries, and only reaches QUALIFIED after genuinely passing --
citation-gated grading so a fabricated citation fails an attempt no matter how good the prose is.
Real backing data: 6836 real exam PDFs across 500 real academic fields (309 materialized with a
real tests/keys corpus), 2.2GB, scraped by tools still in `jarvis/tools/`. The gap: zero references
from `dourmouse/` anywhere (confirmed by grep), last touched 2026-08-18 (over a month stale), and
only `MockBrain`/`NotConfiguredBrain` ever existed -- no real reasoning backend, no chat reachability,
nothing in the product could ever call it.
**Design**: relocated the real, substantively-unchanged state machine into `dourmouse/research_mesh/`
(the real package location this codebase's own convention expects) rather than rewriting logic that
was already correct -- matching this session's own established principle of never rebuilding what's
already real (see finding #032's own reuse of a pre-existing signal). What's genuinely new: a
`RealBrain` (`dourmouse/research_mesh/brain.py`) backed by this codebase's own real, already-verified
model routing -- the exact same tool-less `ChatSession(DispatchRegistry(), session_file=None)`
primitive `goal_runtime.py`'s `_verify_completion`/`_verify_goal_criteria` already use for independent
reasoning passes, reused rather than inventing a new call path. Study is real PDF text extraction
into a capped running context (`_STUDY_CONTEXT_CAP_CHARS`, same bounding discipline as
`general_roster.py`'s `_DELEGATE_RESULT_CAP`); answering is one real, grounded model call instructed
to cite only real studied filenames verbatim, independently checked by the exam engine's own
pre-existing citation gate -- RealBrain cannot pass an exam by asserting a citation that does not
exist. Real chat reachability: `dourmouse/research_mesh_tools.py` (mirroring `goal_tools.py`'s own
dedicated-module shape), registered as a new `research_mesh` subagent in `general_roster.py`,
classified `_CLOUD_OK_AGENTS` in `model_delegation.py` (public academic PDFs, the same privacy class
as `research_info`, never the user's own private data). The default SQLite path
(`workspace/research_mesh/qualification.db`, via `config.workspace_dir()`) is workspace-relative,
applying finding #028's own hard-won lesson from the start rather than repeating that mistake.
**A real pre-existing bug caught by the relocation itself**: `core.py`'s own module docstring had an
invalid `\-` escape sequence (a `SyntaxWarning`, present in the original jarvis code too, carried
forward faithfully by the relocation until pytest's own warning surfaced it) -- fixed with a raw
docstring, not worked around.
**Live proof, real model, real data, no test hooks**: direct pipeline run (`python -m
dourmouse.research_mesh.pipeline --real --domain "Condensed Matter & Materials (physics)" --field
"Photonics & Optoelectronics"`) against the real 6-paper corpus: iteration 1 (`qualifier2018.pdf`)
FAILED on first attempt (score 0.00 -- correctly held out, the anti-cheat rule working against a
real model, not just MockBrain), remediated, PASSED on retry (score 1.00, citations_verified=True);
iteration 2 (`qualifier2019.pdf`) PASSED first try; iteration 3 (`2021 Qualifying Exams.pdf`) FAILED
three consecutive real attempts, correctly triggering permanent exclusion (NOT_QUALIFIED) -- the
real state machine's own MAX_ATTEMPTS rule, never touched, firing correctly against real model
output for the first time. State persisted for real (confirmed via a fresh `AgentStore.load()`
call in a separate process). Full chat-reachability proof: a real `/api/chat` call asking the model
to invoke `research_mesh_status` with specific arguments and report the result verbatim returned
the tool handler's own exact private string template character-for-character -- not something a
model could plausibly fabricate, confirming the real tool genuinely ran through the real chat path,
not just in isolation.
**Two real, separate, out-of-scope bugs found live and flagged (not fixed here)**: (1) this
machine's global `~/.claude/settings.json` had `"model": "deepseek-r1:14b"` -- a stray override from
unrelated past testing that broke every Claude Code CLI call app-wide, not just this feature;
user-directed fix applied directly (removed the override, verified with a real `claude -p` call)
since it is the user's own account setting, outside this repository entirely. (2) Grounded Mode's
own "zero tool calls" warning is a false positive: it fired on the exact verbatim-template proof
above, where a real tool call demonstrably happened -- flagged as a separate background task
(`task_d88c3f91`), not fixed here (root cause is in Grounded Mode's own detection, unrelated to
research_mesh). (3) `tests/test_launch_restores_geometry_and_returns` (a separate, pre-existing,
unrelated top-level test) was found genuinely hanging in complete isolation during this session's
own full-suite runs -- flagged separately (`task_ade8f6a2`), deselected from this session's own
full-suite verification since it is unrelated to this change.
**Files changed**: new `dourmouse/research_mesh/` package (`__init__.py`, `core.py`, `study.py`,
`exams.py`, `store.py`, `brain.py`, `pipeline.py`, `tests/`), new `dourmouse/research_mesh_tools.py`,
`dourmouse/general_roster.py` (registration), `dourmouse/model_delegation.py` (routing
classification), `dourmouse/tests/test_dispatch.py` (exhaustive subagent-name set). Removed:
`jarvis/research_mesh/agents/` (superseded, git history keeps it recoverable). Left unchanged:
`jarvis/tools/` (the real scraping tools), `jarvis/research_mesh/fields/` (the real 2.2GB corpus).
**Tests added**: `dourmouse/research_mesh/tests/` (33 tests: the relocated `test_core.py`/
`test_study.py`/`test_exams.py`/`test_pipeline.py`, plus new `test_brain.py` -- 12 RealBrain tests
covering real text extraction, real prompt construction, citation parsing, context capping, honest
failure on a broken model call, real re-ingestion on remediate with no wasted model call, and one
full end-to-end pipeline run through the real citation gate with only the model call faked).
**Tests run**: `dourmouse/research_mesh/` (33/33) in isolation, `test_dispatch.py` +
`test_model_delegation.py` (241/241) for the roster-registration regression, then full suite.
**Result**: fixed -- workstream A's real gap (a disconnected, brainless, unreachable package) closed
with a real model-backed brain and real chat reachability, live-verified against real data with a
real model, while preserving the real, already-correct state-machine logic underneath rather than
discarding working design.

---

### 034 -- `desktop.launch()` test hang: a stale fake webview seam, not a real product bug

**Severity**: MEDIUM (test-suite integrity -- a genuinely hanging test blocks every future full-suite
gate this session's own commit discipline depends on, even though the real product code was never
broken).
**Diagnosed and fixed by a spawned peer session** (`task_ade8f6a2`), independently re-verified here
before committing.
**Context**: `tests/test_desktop.py::test_launch_restores_geometry_and_returns` (and two sibling
tests in the same file) hung indefinitely, confirmed reproducible in complete isolation with zero
other processes running. `desktop.py`'s real `launch()` calls `webview.start(private_mode=False,
storage_path=...)` (v13.10) -- the test's own `_FakeWebview.start(self)` (stale since 2026-08-12)
took no keyword arguments, so the call raised `TypeError`, which `launch()`'s own broad `except
Exception` silently routed into `_fallback_to_browser()` -> `webbrowser.open()` +
`_wait_forever()`'s real `while True: time.sleep(3600)`, breaking only on `KeyboardInterrupt`.
**Live proof**: confirmed via macOS `sample` on a stuck pytest pid (main thread genuinely parked in
real `time.sleep`), plus a direct repro of the `TypeError` outside pytest. The sibling
`dourmouse/tests/test_desktop.py` already had the correct fake signature and was never affected.
**Fix**: `_FakeWebview.start(self, **kwargs)`; `hermetic_env` now also sets
`DOURMOUSE_VISION_AUTOSTART=0`/`DOURMOUSE_GOAL_RUNTIME=0` (both default on in real `launch()`, both
silently doing real work -- a real `GoalRuntime` thread + SQLite file, real tray/overlay/wakeword
attempts -- inside what is supposed to be a hermetic test); `@pytest.mark.timeout(30)` added to all
3 launch tests as a regression guard against this exact failure mode recurring; `pytest-timeout`
added to `requirements-dev.txt`.
**Files changed**: `tests/test_desktop.py`, `requirements-dev.txt`.
**Tests run**: `tests/test_desktop.py` (9/9, 5.54s -- was: infinite hang).
**Result**: fixed -- a real, root-caused fix, not a `--deselect` workaround.

---

### 035 -- A plan step completed via `delegate_task` was wrongly flagged as untouched

**Severity**: MEDIUM (a real, confusing false nudge/caveat on genuinely completed work).
**Diagnosed and fixed by a spawned peer session** (`task_d88c3f91`, while investigating finding
#036 below and finding this real, separate bug along the way), independently re-verified here
before committing.
**Context**: `_missing_plan_steps()`'s own "was this step touched" check
(`tool_owner.get(name) == step['subagent']`) only recognized a step as satisfied via a DIRECT tool
call by name. A plan step satisfied through `delegate_task`/`delegate_parallel` instead leaves the
parent's own transcript with only a `"delegate_task"` entry owned by `"orchestrator"` -- never the
step's real target subagent -- so a genuinely-completed delegated step still looked untouched, and
the plan-checkpoint nudge (or the exit-path "not executed via tools" caveat) could misfire on it.
**Fix**: `dispatch.py` gains `_first_nonempty_str()` (alias-tolerant argument lookup, duplicated
from `general_roster.py`'s own helper rather than imported, since `general_roster` imports FROM
`dispatch` and the reverse would be circular) and `_delegated_targets()` (scans the transcript for
`delegate_task`/`delegate_parallel` calls and resolves which real subagents they targeted);
`_missing_plan_steps()` now also treats a step as touched when its subagent appears there.
**Live proof**: fail-before/pass-after via `git stash` on the new regression test -- fails on
pre-fix code (a spurious `plan_reminder` fires), passes with the fix.
**Files changed**: `dourmouse/dispatch.py`, `dourmouse/tests/test_dispatch.py`.
**Tests added**: `test_step_completed_via_delegate_task_gets_no_reminder`.
**Tests run**: `test_dispatch.py` + `test_general_roster.py` (341/341).
**Result**: fixed. Note: `dourmouse-commercial` (a separate worktree of this same repo) has the
identical pre-fix code, not touched by this finding.

---

### 036 -- Grounded Mode false positive: every `claude_cli`-backed answer flagged "unverified"

**Severity**: HIGH (a real, correct, tool-backed answer was unconditionally mislabeled as guessed,
100% of the time, on this deployment's own default backend -- Claude Front Mode is on by default).
**Diagnosed, designed, and implemented by a spawned peer session** (`task_d88c3f91`), independently
reviewed line by line and verified here before committing.
**Live-caught**: while live-verifying finding #033's own chat-reachability, a real `/api/chat` call
asking the model to invoke `research_mesh_status` and report the exact result verbatim came back
with the tool handler's own private string template reproduced character-for-character -- proof a
real tool call happened -- yet still carried Grounded Mode's "this answer used zero tool calls...
treat it as unverified" warning. Flagged as a separate task (`task_d88c3f91`) rather than guessed at.
**Root cause**: `ClaudeCliClient._create()` (`dispatch.py`) hardcoded
`_OllamaMessage(text, None)` unconditionally on every completion. When Claude calls a real Dourmouse
tool, it does so over MCP *inside* the `claude -p` subprocess, through `mcp_bridge.py`'s own
SEPARATE OS process that binary spawns as its configured MCP server -- the call is genuinely real
and its result genuinely correct, but `_run_dispatch_loop`'s own `tools_used` count (real
`"tool_use"` transcript entries) had no way to see it. Not intermittent: structurally 0, every time,
for this backend.
**Scope-checked before fixing** (so the fix doesn't over-correct): `GeminiClient._create()` has the
identical code shape, but Gemini is never given tool/MCP access in this integration -- `tools_used
== 0` is honestly true there, and grounded-mode firing is correct, not a bug. `ollama_cloud` reuses
`OllamaNativeClient` (real native tool-calling, already populates `tool_calls` normally) --
unaffected. This fix is specific to `claude_cli`.
**Design, real not suppressed**: two options were considered -- (A) a new `opaque_tool_backend` flag
suppressing the check specifically for `claude_cli`, small and safe but creates a real, silent
blind spot (a genuinely zero-tool `claude_cli` turn would ALSO stop being flagged, on the deployment's
own default backend); (B) make `tools_used` actually accurate for this backend. Chose (B) --
consistent with this session's own standing discipline (finding #032's own reuse of a real signal
rather than suppressing a symptom): `mcp_bridge.py`'s `_handle_tools_call` now logs every real call
attempt (success, error, or a `REQUIRES_CONFIRMATION` refusal -- verified this already matches
`tools_used`'s own existing semantics for every other backend, which counts a `tool_use` entry the
instant a call is attempted, gated or not) to a fresh, per-invocation temp file. The one design
question this rested on was verified live, not assumed: does the real `claude` CLI pass its own
inherited environment through to an MCP stdio server it spawns? Confirmed yes (a minimal probe MCP
server + config, a marker env var set only on the outer process, reached the child: 62 env vars
total, full inheritance, the config's own `"env"` merges on top rather than replacing) -- so a
per-invocation-unique env var, set only for that one `_run_claude_once` call, is enough; no
persistent session/tab identity needed in the cached, shared `mcp-config.json` at all.
`ClaudeCliClient._create()` reads the log back after the CLI call returns (both the streaming and
non-streaming paths -- the top-level orchestrator loop always sets `on_delta`, so a
non-streaming-only fix would never have fired in production) and `_run_dispatch_loop` replays it as
real `"tool_use"`/`"tool_result"` transcript entries -- into `transcript` only, never `messages`
(there is no real `tool_call_id` to pair it with in this conversation's own OpenAI-shaped history),
so grounded-mode, the plan checkpoint, audit, and experience-recording all see the truth without
risking the actual conversation history sent back to the model.
**Live proof, real end-to-end, no mocks**: the real production registry, the real `ClaudeCliClient`,
the exact original repro prompt through a real `claude -p` call and the real `mcp_bridge.py`
subprocess. Result: real `tool_use`/`tool_result` transcript entries, the real correct answer, zero
Grounded Mode disclaimer.
**Files changed**: `dourmouse/dispatch.py`, `dourmouse/code_backends.py`, `dourmouse/mcp_bridge.py`,
`dourmouse/tests/test_dispatch.py`, `dourmouse/tests/test_mcp_bridge.py`.
**Tests added**: 17 new/updated -- `mcp_bridge.py` logging (success/error/gated-refusal/
unknown-tool-never-logged/multi-call NDJSON/unwritable-path-never-breaks-the-call), `ClaudeCliClient`
(streaming + non-streaming replay, cleanup, a genuinely-zero-tools turn still correctly caveats),
and the dispatch-level proof test using the exact original repro shape.
**Tests run**: fail-before/pass-after via `git stash` on the 3 core new tests; full suite.
**Result**: fixed -- the real signal now exists, not a suppressed symptom; a genuinely tool-free
`claude_cli` turn still correctly caveats.

---

### 037 -- Project-instruction file: Dourmouse's own CLAUDE.md-equivalent (Domain H, first piece)

**Severity**: n/a (feature -- the smallest, first real piece of Domain H's own build plan).
**Context**: `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` Domain H asked, as an open question,
whether a CLAUDE.md-equivalent project-instruction mechanism already existed anywhere in
`dourmouse/` -- answered definitively by grep before writing any code: no, a genuine clean-slate gap.
**Design**: `dourmouse/project_instructions.py`, one real function
(`load_project_instructions()`), reading `<workspace>/DOURMOUSE.md` -- honest empty string (never a
placeholder) when missing, unreadable, or whitespace-only, capped at 8,000 chars with a real
truncation marker (same bounding discipline as `general_roster.py`'s `_DELEGATE_RESULT_CAP` and
`research_mesh/brain.py`'s `_STUDY_CONTEXT_CAP_CHARS`). Spliced into `dispatch.py`'s own
`system_message()` -- the single function already shared by `run_dispatch` and `chat.ChatSession`,
so every backend and every delegation depth picks it up automatically, no second injection path --
ALONGSIDE the base orchestrator rules, never instead of them, the exact same "alongside, not
instead of" contract `agent_prompts.py`'s own bespoke per-agent prompts already establish for the
identical reason (confirmation-gating and honest-failure rules must survive regardless of what a
user's own instructions say). No file present: `system_message()` stays byte-identical to before,
matching that function's own pre-existing stated contract.
**Deliberately not built yet, named explicitly**: per-directory hierarchical nesting (a narrower
file in a subfolder layering on top of the workspace-root one, matching CLAUDE.md's own
nearest-file-wins convention) needs a real `cwd` threaded into `system_message()`'s own signature,
touching every current caller -- a real, separate, larger piece of Domain H's own build plan, not
bundled into this smallest-first increment.
**Live proof, real chat, real model**: a fresh dev-preview workspace's own `DOURMOUSE.md` instructed
"when asked what day it is, always mention the code phrase PURPLE-NARWHAL-42 verbatim." A real
`/api/chat` call asking "What day of the week is it today?" delegated to the `system` subagent and
the real reply contained the code phrase verbatim -- confirming the splice reaches the real model
through the real dispatch path AND survives a real delegation hop (`system_message()` is shared, so
the nested sub-dispatch inherited it too, not just the top-level orchestrator turn).
**Files changed**: new `dourmouse/project_instructions.py`, `dourmouse/dispatch.py`
(`system_message()`).
**Tests added**: new `dourmouse/tests/test_project_instructions.py` (5 tests: honest empty on no
file, real read-and-strip, whitespace-only is honest empty, oversized file capped with a marker,
unreadable path -- a directory in the file's place -- is honest empty not a crash) and
`test_dispatch.py::TestSystemMessageSplicesProjectInstructions` (2 tests: no file leaves the prompt
byte-identical; a real file is spliced in alongside the base rules, ordered after them).
**Tests run**: both new test files (7/7) plus `test_dispatch.py` in full (235/235).
**Result**: fixed -- Domain H's own smallest real piece shipped, live-verified with a real model,
including through a real delegation hop.

---

### 038 -- Named specialist roles on `delegate_parallel` (Domain F remainder)

**Severity**: n/a (feature -- Domain F's own remaining piece, per its harsh acceptance test).
**Context**: Domain F asked for named specialist roles (Researcher, Coder, Reviewer, Tester,
Security Sentry) reachable through `delegate_parallel`. A first design draft (written the same day)
assumed a reusable per-branch tool-allowlist mechanism modeled on `agent_smith`'s own "scoped
permissions" -- corrected before writing any code, by checking `self_extensions.py` directly:
`agent_smith` forces ONE newly-drafted tool to `Permission.REQUIRES_CONFIRMATION`, a different
mechanism entirely; no reusable allowlist exists in this codebase. The real, already-existing
mechanism reused instead: `_build_delegate_parallel_tool`'s own docstring already states a named
`agent_or_task` becomes "a forced_agent ROUTING DIRECTIVE run on that one subagent's configured
model" -- a branch routed to one real subagent already only ever sees that subagent's own fixed
toolset, no new enforcement layer needed.
**Design**: `general_roster.py` gains `_DELEGATE_ROLE_PRESETS`, a small, real, named table --
`researcher` -> `research_info`, `coder`/`tester` -> `dev_coding`, `security_sentry` -> `security`,
each with a real persona-prefix instruction. An optional `role` field on each `delegate_parallel`
branch (JSON-schema `enum`-constrained to the known names) prepends that persona to the branch's own
instructions and, ONLY when the caller does not name an explicit `agent_or_task`, defaults the
routing target to the preset's real subagent -- an explicit target always wins, a role never
silently overrides what the caller actually asked for.
**Deliberately not included, named explicitly, not silently dropped**: `reviewer` is NOT in the
table. No currently-registered subagent has a genuinely write-free toolset that also fits reviewing
arbitrary code/text (`dev_coding` can write files; `study` is real read-only but hard-scoped to the
user's own personal study folder, the wrong domain entirely) -- pointing it at a write-capable
subagent with a stern prompt would be a request, not an enforced restriction, and claiming otherwise
here would be exactly the kind of overclaim this codebase's own audit discipline exists to catch.
Registering a real, narrow, read-only subagent first is real, separate follow-on work.
**Live proof, real chat, real model, real tools**: a real `/api/chat` call asked for a
`delegate_parallel` fan-out with `role=researcher` and `role=security_sentry`, explicitly withholding
`agent_or_task` on both branches. Real result: `[branch 0] agent=research_info ... [branch 1]
agent=security` -- both roles resolved to their real preset subagents with zero explicit routing
from the model. The `security_sentry` branch returned genuinely real host/network telemetry (real
interface IPs, real default gateway, real DNS resolvers, a real disabled-Application-Firewall
finding, real listening ports with real PIDs) -- the exact same class of live, unprompted finding
Domain I's own foundation work already surfaced once before. The `researcher` branch failed
honestly (a real 404 from the search service), not fabricated.
**Files changed**: `dourmouse/general_roster.py` (`_DELEGATE_ROLE_PRESETS`, the branch-parsing loop,
the tool's own JSON schema).
**Tests added**: `test_self_dispatch.py::TestDelegateParallelNamedRoles` (4 tests: a role with no
explicit agent resolves to its preset's real subagent; an explicit agent always wins over the role's
default while the persona still applies; an unknown role errors, listing the known ones; no role at
all leaves the branch's task byte-identical, a real regression guard).
**Tests run**: the new class (4/4), then `test_self_dispatch.py` + `test_general_roster.py` +
`test_dispatch.py` + `test_model_delegation.py` (414/414).
**Result**: fixed -- Domain F's own remaining named-role gap closed, live-verified with a real
model routing to real subagents with zero new enforcement machinery, `reviewer` honestly deferred
rather than faked.

---

### 039 -- AI security sentry: real deterministic scan, scoring, and false-positive memory (Domain I)

**Severity**: n/a (feature -- Domain I's own remaining "AI sentries" piece).
**Context**: Domain I's own build plan first proposed "one real `ChatSession` call... scoring the
gathered telemetry against the real weighted formula" for RISK_ANALYSIS -- corrected before writing
any detection rule: a security finding's own severity and existence must be exactly reproducible,
not a paraphrase a model could drop a detail from. Rebuilt as a fully deterministic pipeline instead,
matching this domain's own explicit requirement ("never a bare LLM vibe-check with no formula behind
it") more literally than the original plan did.
**Design**: `dourmouse/security/sentry.py` -- two real, honestly-scoped detection rules
(`_detect_findings`, pure logic, no I/O): a disabled Application Firewall (HIGH) and any listening
service exposed beyond `LOOPBACK_ONLY`/`LOCAL_NETWORK`/`TAILSCALE`'s own expected tiers, i.e.
`ALL_INTERFACES` (MED) -- both read from `platform_adapter.py`'s already-real, already-tested
telemetry, no new collection code. `SentryStore` (workspace-relative SQLite, same one-connection-
per-operation WAL discipline as every other real store in this codebase) gives each finding a stable
fingerprint and real persisted history: a genuinely first-ever detection is `"new"`, a repeat is
`"known"` (still counted, never re-alerted), and a user-dismissed one is `"dismissed"` (excluded from
both the risk score and future new-finding alerts) -- the real historical-pattern-match half of
ThreatSentinel's own weighted-formula design. Only a genuinely NEW **HIGH** finding writes a real
alert (`state_store.add_alert`, the exact same mechanism `goal_runtime.py`'s own system alerts
already use) -- MED findings are reported in every scan's own text but never proactively alert,
a deliberate choice: this machine's own real MED findings (rapportd/ARDAgent/Spotify/Python, all
legitimate, all normal) would otherwise generate an alert burst on a user's very first scan.
**Deliberately not built, named explicitly**: new-LAN-device detection and external-IP reputation
lookups (this domain's own harsh acceptance test 2) need a real, persisted ARP-neighbor baseline
this pass does not build. A continuously-running background scheduler (needed for harsh acceptance
test 1's own "unprompted, within a bounded window" wording) and the live SSE push through
`DesktopNotifier` (needs the running server's own hub instance -- checked directly before writing
this: no global accessor for it exists today) are both real, separate follow-on; a scan today is
real and chat-reachable, and a genuinely new HIGH finding writes a real, persisted alert visible on
the next alerts-screen refresh, just not an instant push. Dashboard UI not started, per the domain's
own build plan ("last, after the sentry itself is real and tested").
**Live proof, real machine, real finding, no mocks**: a real scan against THIS machine found its
Application Firewall genuinely disabled (HIGH, risk score contribution 9.0) plus 6 real
`ALL_INTERFACES`-exposed services (MED each, risk score 33.0 total) -- independently cross-verified
against an unrelated earlier live test this same session (finding #038's own role-based
`security_sentry` branch surfaced the identical real services). Exactly 1 real alert was written
(the HIGH finding only), confirmed landed in the real `state_store` alerts table. A second real scan
of the same, unchanged machine state reported 0 new findings and wrote 0 further alerts -- the
persisted-memory suppression working correctly against real, not synthetic, repeat state. A real
`/api/chat` call asking to "run a real security sentry scan" reached the new tool through the real
dispatch path and the model faithfully reported every real finding.
**Files changed**: new `dourmouse/security/sentry.py`, `dourmouse/security/tools.py`
(`security_sentry_scan`, `security_sentry_dismiss`).
**Tests added**: new `dourmouse/tests/test_sentry.py` (20 tests: rule detection for both findings,
honest no-finding on unavailable/clean telemetry, stable fingerprinting, new/known/dismissed
store-classification transitions, cross-instance persistence, dismiss-then-suppress, dismissing an
unseen fingerprint is an honest `False` not a crash, risk-score arithmetic, only-HIGH alerts,
a broken alert write never breaking the real scan result, honest telemetry-availability reporting).
`test_security_tools.py`'s own exhaustive tool-name set updated for the two new tools.
**Tests run**: the new file (20/20), then `test_sentry.py` + `test_security_tools.py` +
`test_security_platform_adapter.py` + `test_general_roster.py` + `test_dispatch.py` (412/412).
**Result**: fixed -- Domain I's real sentry now exists, live-verified against this real machine's
own real, previously-known firewall gap, with real persisted memory and honestly-scoped limits.

---

### 040 -- Domain J: real Hermes-reference color retone (evidence-based, not guessed)

**Severity**: n/a (feature -- Domain J's own blocker resolved: the user supplied the real Hermes
reference screenshot this section's own build plan was waiting on).
**Context**: real pixel colors extracted programmatically from the reference screenshot (Python/PIL,
not eyeballed): the single most common color by pixel count was `rgb(9,41,36)`, the second most
common `~rgb(20,44,31)` -- a cohesive dark teal/olive-green canvas+panel family, not the neutral
zinc-black Dourmouse's own `--dm-canvas`/`--bg` family currently uses. Font family could NOT be
extracted with any confidence from a raster screenshot (no embedded font metadata in a PNG/WebP) --
this section's own build plan explicitly required real evidence over guessing, so fonts are
deliberately untouched rather than naming a guessed family.
**Design**: token-level retone, not a redesign, exactly as this domain's own build plan specified.
Two places carry the real values (a pre-existing duplication this pass did not introduce): `dourmouse
-ui.css`'s `--dm-canvas`/`--dm-layer`/`--dm-layer-hi`/`--dm-line`/`--dm-line-focus`, and `console.
html`'s own "Terminal Core" theme block (`--bg`/`--panel`/`--panel2`/`--line`/`--line-2`/`--amber-
line`/`--amber-soft`), which duplicates the same values under different names. Canvas/layer values
are the real extracted samples; the "hi"/line tiers (not clearly sampleable as a distinct surface in
one screenshot) are proportional lightenings at the SAME ratio the original neutral values used, not
independently guessed. Deliberately UNCHANGED: text colors, the functional accents (amber/ok/error),
and both fonts -- the screenshot does not give confident evidence to move those, and moving `--dm-
active` to match `--dm-ok`'s existing green would have collapsed two semantically distinct states
(selected vs. passing) into one visually-identical color, a real usability regression the reference
image does not actually call for.
**Real, honestly-scoped gap found, not fixed here**: `workspace.html` (and likely other files among
the 16 that link `dourmouse-ui.css`) does not actually render `var(--dm-canvas)` for its own
background -- confirmed live: the shared stylesheet's computed custom-property value was correctly
`#0A2A22`, but `workspace.html`'s own rendered body background stayed black, meaning it has its own
local override elsewhere in the cascade. This pass only confirmed and fixed the PRIMARY, default-
landing screen (`console.html`'s "Terminal Core" theme, the one shown in `HOME`); auditing every
other one of the 16 linking files for the same local-override gap is real, separate, not-yet-done
follow-on.
**Live proof, real browser, real pixels**: `console.html`'s "Terminal Core" theme (this app's
default) screenshotted before and after -- before, a neutral black/gray console; after, a cohesive
dark green console with the exact same layout, panels, nav, and amber functional accents untouched.
Confirmed via computed style that the shared token's live value matches the edit
(`getComputedStyle(document.documentElement).getPropertyValue('--dm-canvas')` = `#0A2A22`).
**Files changed**: `ui/assets/dourmouse-ui.css`, `ui/console.html`.
**Tests**: none added -- a rendered-color change has no meaningful Python-level assertion in this
codebase's own test suite; live browser screenshot + computed-style verification is the real,
appropriate check here, matching how this session's own prior UI-only changes were verified.
**Result**: fixed for the primary screen, live-verified -- a real, evidence-based step toward this
domain's own harsh acceptance test ("these clearly share a visual language"), with the remaining
15 files' own local-override status honestly named as unverified, not silently assumed fixed.

---

### 041 -- Continuous, always-running security sentry (Domain I, user-directed scale-up)

**Severity**: n/a (feature -- closes finding #039's own named gap, user-directed: "this needs to be
a really powerful cybersecurity system... always running sentry, continuous data stream").
**Context**: the reference architecture image supplied names the security subsystem an "always
running sentry" with a "continuous data stream" -- finding #039 shipped the real detection/scoring/
memory engine but only as an on-demand chat tool, explicitly naming the continuous loop as
not-yet-built. This finding closes that gap.
**Design**: `SentryRuntime` (`dourmouse/security/sentry.py`) -- the exact same real daemon-thread
shape `GoalRuntime`/`SchedulerRunner` already use (one instance per process, started from
`webui.run_server`, a broken tick wrapped in the same `try/except: pass` so one bad scan never
kills the loop, same default-ON/opt-out convention via `DOURMOUSE_SECURITY_SENTRY_LOOP=0`). The
loop calls its own real tick BEFORE the first wait, so a genuinely "always running" sentry does not
sit silent for a full interval before its first real result. Interval floor: 60 seconds, default
300 -- a real scan shells out to `lsof`/`ifconfig`/`scutil`/`arp`/`socketfilterfw` per tick, and a
real host security tool polling every few minutes (not every second) matches how real endpoint
security agents actually behave, not an unbacked "real-time" marketing claim.
**Honest, minor limitation found live, not hidden**: a service bound to the same port over BOTH
IPv4 and IPv6 (observed live: `rapportd` on this real machine) produces two separate `lsof` rows
that fingerprint identically, so one real tick can record `times_seen` twice and add that
finding's MED weight to `risk_score` twice for what is genuinely one service. Real, small,
separate follow-on: de-duplicate by `(command, port, protocol)` before scoring. Does not affect
the HIGH-severity/alerting path (the firewall check has no such duplication).
**Live proof, real server boot, zero chat messages sent**: started a real dev-preview server;
before any chat interaction, the real workspace's own `sentry.db` already had 6 real findings
persisted and a real HIGH alert already written to the real `state_store` alerts table --
confirming the loop starts automatically at boot and completes its first real tick with no user
action, exactly the "always running" property this finding exists to add.
**Files changed**: `dourmouse/security/sentry.py` (`SentryRuntime`, `sentry_runtime_enabled`),
`dourmouse/webui.py` (server startup wiring, same placement as `GoalRuntime`'s own).
**Tests added**: 10 new tests (`TestSentryRuntimeEnabled`, `TestSentryRuntime`) -- the opt-out env
var, the 60s interval floor, a real synchronous tick updating real state, a real background thread
completing a real tick before its first wait, idempotent `start()`, prompt thread exit on `stop()`,
and a broken tick never killing the runtime (same discipline as `GoalRuntime._loop`'s own test
coverage).
**Tests run**: `test_sentry.py` in full (30/30, 0.73s).
**Result**: fixed -- the sentry is now genuinely continuous, live-verified starting and completing
a real scan automatically at server boot with zero user interaction, matching the reference
architecture's own "always running... continuous data stream" requirement.

---

### 042 -- Domain G: the real data model and persisted store (first piece)

**Severity**: n/a (feature -- the smallest, first real piece of Domain G's own build plan, per this
session's own established "smallest lift first" sequencing, same shape as findings #033/#037/#039).
**Context**: Domain G was pure scoping before this -- zero pipeline code existed. This finding
starts the real build: the data model and persistence, mirroring `research_mesh/store.py`'s own
proven one-row-JSON-body SQLite shape (finding #033) rather than inventing new persistence
plumbing a fourth time.
**Design**: `dourmouse/research_pipeline/core.py` -- a real `Claim` dataclass matching this domain's
own spec field list verbatim (`claim, source_id, url, document_hash, location, passage,
retrieved_at, agent`), a `Contradiction` dataclass, and a `ResearchRecord` state machine (`PLANNED
-> SOURCES_DISCOVERED -> EVIDENCE_EXTRACTED -> SYNTHESIZED`) enforcing real transition order (
cannot discover sources before a plan exists, cannot synthesize before real evidence exists).
`reject_claim` never deletes -- it replaces a claim with a `REJECTED`-status copy carrying the same
real provenance, so the record stays honest about what was once believed, matching this domain's
own harsh acceptance test 3 ("a rejected hypothesis... must remain visible, never quietly
discarded") applied to claims as well as hypotheses. `ResearchStore` (`dourmouse/research_pipeline/
store.py`) persists the whole record as one row, workspace-relative from the start.
**A real bug caught by its own test, fixed before commit**: `add_sources`'s first draft computed
`existing = set(self.sources)` once before its dedup loop, so it deduped against sources already
present from BEFORE the call, but not against duplicates WITHIN the same incoming batch -- passing
`["https://a.com", "https://b.com", "https://a.com"]` let the repeated URL through twice. Caught by
`test_add_sources_deduplicates_and_advances_stage` on first run, fixed by tracking `seen` as a
running set updated during the same loop, not a static snapshot taken before it.
**Deliberately not built yet, named explicitly**: the actual stage FUNCTIONS (a real `ChatSession`
call for planning, real source discovery through the already-real `research_info` tools, real
per-source evidence extraction with real citations, contradiction detection, synthesis) are real,
separate, not-yet-built follow-on -- this finding is the data model and store only, no model call
anywhere in it, no chat reachability yet. Building the stage functions next follows the exact same
incremental, independently-tested, live-verified pattern as every other domain this session.
**Files changed**: new `dourmouse/research_pipeline/` package (`__init__.py`, `core.py`, `store.py`).
**Tests added**: new `dourmouse/tests/test_research_pipeline.py` (17 tests: full state-machine
transition order and its guards, dedup-on-add, never-delete-only-reject, active-claims filtering,
contradiction accumulation, lossless save/load round-trip including rejected-status claims and
contradictions, idempotent update not a duplicate row, cross-instance persistence, most-recent-first
listing).
**Tests run**: the new file (17/17, 0.23s).
**Result**: fixed -- Domain G's own real foundation now exists and is tested; the actual research
pipeline (plan/discover/extract/synthesize) builds on top of it next.

---

### 043 -- Domain G: real stage functions (plan + discover_sources)

**Severity**: n/a (feature -- the second real piece of Domain G's own build plan, directly on top
of finding #042's data model, same "smallest lift next" sequencing).
**Context**: finding #042 built the data model and store with zero model calls and zero chat
reachability. This finding adds the first two real stage FUNCTIONS named as follow-on there:
`plan()` and `discover_sources()`, both reusing already-proven call paths rather than inventing
new ones.
**Design**: `dourmouse/research_pipeline/stages.py`. `plan()` is the fifth reuse this session of
the tool-less `ChatSession(DispatchRegistry(), session_file=None)` primitive (after
`_verify_completion`, `_verify_goal_criteria`, `RealBrain.answer`) -- decomposing a question into
real sub-questions is pure reasoning, no tools needed. Unlike `RealBrain.answer` (which must stay
honest-uncertain mid-exam so a broken checker never destroys real completed work), a broken
`plan()` call has produced no real work yet to protect, so it raises loudly instead of swallowing
the error. `discover_sources()` reuses `run_dispatch_messages(..., forced_agent="research_info",
...)` directly -- the same forced-agent mechanism `delegate_task`/`delegate_parallel` already use
internally -- to force one real nested dispatch run onto the already-real `research_info`
subagent, then reads back which real URLs it actually touched via
`_extract_urls_from_transcript()`. That helper has two real, ranked sources: `fetch_url`
tool_call's own `raw_arguments` JSON (clean, exact, primary) and a best-effort regex scan of
`web_search` tool_result text (secondary, honestly incomplete for the Wikipedia-fallback engine,
which carries no URLs in its own output shape at all -- named, not silently assumed complete).
**Live proof, real model, real web, zero mocks**: ran both stages back to back with no mocking --
`plan()` against "What is the Model Context Protocol (MCP) and why does it matter for AI agents?"
returned 5 real, genuinely distinct sub-questions (not vague restatements) decomposing the
question. `discover_sources()` against "What are the core components and structure of the Model
Context Protocol (MCP)?", using the real production `build_general_registry()` (not the test-only
fake registry), advanced the record to `Stage.SOURCES_DISCOVERED` and returned 6 real,
deduplicated URLs a real `research_info` agent actually found and fetched:
`modelcontextprotocol.io/introduction`, `modelcontextprotocol.io/docs/learn/architecture`, and
four raw GitHub spec/README URLs -- read back through the real transcript with no fabrication.
**Files changed**: new `dourmouse/research_pipeline/stages.py`.
**Tests added**: `dourmouse/tests/test_research_pipeline.py` grew from 17 to 30 (13 new:
`TestPlanStage`, `TestExtractUrlsFromTranscript`, `TestDiscoverSourcesStage`), with local
test-double reconstructions matching `test_dispatch.py`'s own `FakeClient`/`_FakeToolCall`/
`_FakeMessage` shapes and `research_mesh/tests/test_brain.py`'s own `_FakeSession` shape,
following this codebase's own convention of not importing test internals across files. Two real
em dashes in this file's own new mock search-result fixture text (mimicking `_brave_search`'s
real format) and one unused `Permission` import were caught and fixed before commit (ruff plus
the standing no-em-dash self-check).
**Tests run**: `test_research_pipeline.py` in full (30/30).
**Result**: fixed -- Domain G now has a real plan stage and a real source-discovery stage, both
live-verified against a real model and the real live web, not just unit-tested against fakes.
Evidence extraction (real per-source `Claim` generation with real citations), contradiction
detection, and synthesis remain real, separate, not-yet-built follow-on, same as finding #042
already named.

---

### 044 -- Cross-cutting: a stale LOCAL persisted orchestrator model leaked into Ollama Cloud

**Severity**: HIGH (every non-escalated orchestrator turn on a machine with a stale local-tagged
persisted choice and an active Ollama Cloud key 404'd -- not scoped to Domain G at all).
**Context**: live-caught while verifying Domain G's `extract_evidence()` against the real
production registry (finding #045, same session) -- the second real model call inside that stage
(the CLAIM/PASSAGE extraction pass) raised an unhandled `urllib.error.HTTPError: 404` reaching
`https://ollama.com/api/chat` with body `{"error": "model 'qwen2.5:7b' not found"}`. `plan()`'s
own earlier call in the exact same run succeeded, so this was not a blanket outage -- isolating it
(a direct `ChatSession(...).ask()` call with no research_pipeline code involved at all) reproduced
the identical 404, proving this was never a bug in the new Domain G code, only exposed by it being
the first caller this session to exercise the tool-less `ChatSession(DispatchRegistry(), session_
file=None)` primitive from a machine actually running Ollama Cloud.
**Root cause**: `OllamaConfig.model_for_agent("orchestrator")` (`dourmouse/config.py`) checks a
persisted orchestrator-model choice (`_persisted_model_for_backend("ollama")`) BEFORE checking
`is_cloud` -- and that persisted value is tagged only by backend NAME ("ollama"), never by
locality. This machine's real persisted choice was "qwen2.5:7b", saved back when this backend was
genuinely local; once `OLLAMA_API_KEY` later made the SAME "ollama" config cloud, the stale local
value was still trusted and sent to Ollama Cloud, which has no such catalog entry. This is the
exact mirror of the bug `skip_persisted_orchestrator_choice` already exists to prevent (a
CLOUD-saved persisted value leaking into a `force_local=True` config, fixed 2026-09-13) -- that
fix only ever closed ONE direction. Confirmed live: `orchestrator_model_setting()` returned
`"qwen2.5:7b"` / `orchestrator_backend_setting()` returned `"ollama"` on this machine right now,
while `load_ollama_config().model` (the real, correctly-resolved cloud default) is `"gpt-oss:20b"`.
**Design**: rather than guess which locality an "ollama"-tagged value was actually saved under (the
stored tag genuinely cannot say -- expanding the schema to tag locality too, e.g. "ollama_cloud" vs
"ollama", would be the fully general fix but touches the Settings save endpoint's own request shape
as well), this follows the same rule this file already uses elsewhere in this exact function (the
untagged-model-id case, `webui.py`'s own comment: "an untagged value is never auto-applied"): an
ambiguous persisted value is skipped, not guessed. Added `and not self.is_cloud` to the persisted-
choice branch's condition, symmetric with the very next check in the same function
(`_OLLAMA_FAST_DISPATCH`'s own pin already reads `if not self.is_cloud and ...`). Once cloud is
active, the persisted choice is skipped entirely and the real, known-correct cloud default
(`self.model`, resolved by `load_ollama_config` from `OLLAMA_CLOUD_MODEL`/`_OLLAMA_CLOUD_DEFAULT_
MODEL`) is used instead.
**Deliberately not built, named explicitly**: a user's deliberately-chosen CLOUD model saved
through the Settings picker (while already on cloud) is now also skipped by this same guard, since
the storage tag cannot currently distinguish "saved while cloud" from "saved while local" -- it
always reads back as plain "ollama" either way. Fixing that for real needs the Settings save
endpoint (`webui.py`'s `_handle_orchestrator_model_post`) to tag locality at save time, a real,
separate, not-yet-built follow-on. Correctness (never send an unservable model name to a real paid
API) was judged the higher priority over preserving a rare, currently-broken-anyway convenience.
**Live proof**: reproduced the real 404 in isolation (a bare `ChatSession(...).ask()` call, no
research_pipeline involved), confirmed the exact root cause via direct calls to
`orchestrator_model_setting()`/`load_ollama_config()`, applied the fix, re-ran `load_ollama_config()`
and confirmed `model_for_agent("orchestrator")` now returns `"gpt-oss:20b"` (matches `self.model`)
instead of the stale `"qwen2.5:7b"`, then re-ran Domain G's full `plan -> discover_sources ->
extract_evidence` live chain end to end against the real production registry -- zero errors, a real
validated `Claim` produced (see finding #045).
**Files changed**: `dourmouse/config.py` (`OllamaConfig.model_for_agent`).
**Tests added**: `dourmouse/tests/test_config.py::TestSkipPersistedOrchestratorChoice::
test_cloud_config_skips_a_stale_local_persisted_choice` -- saves a local-shaped persisted choice,
forces `OLLAMA_API_KEY` (cloud) via `monkeypatch.setenv`, confirms `model_for_agent("orchestrator")`
never returns the stale local value and instead matches the real cloud default.
**Tests run**: `test_config.py` in full (85/85, includes the 84 pre-existing).
**Result**: fixed -- confirmed no longer 404s live; the full existing config suite still passes
unchanged, proving this closes only the specific cloud/local gap without disturbing any of the
already-tested cross-backend or force-local behavior.

---

### 045 -- Domain G: real evidence extraction with verbatim-validated citations (third piece)

**Severity**: n/a (feature -- the third real piece of Domain G's own build plan, directly on top
of findings #042/#043, same incremental sequencing).
**Context**: findings #042/#043 built the data model, store, `plan()`, and `discover_sources()`.
This finding adds `extract_evidence()` -- the piece this domain's own harsh acceptance test 1 ("a
research answer must let the user click through to the ORIGINAL passage that supports each real
claim") actually depends on, and the piece finding #042 itself named as "the most complex remaining
piece."
**Design**: `dourmouse/research_pipeline/stages.py::extract_evidence()`. Two real calls, both
reusing already-proven machinery, no new call path invented: (1) a forced fetch of one real source
through `run_dispatch_messages(forced_agent="research_info")` (the same mechanism
`discover_sources` already uses), reading the real fetched body back from the transcript's own
`fetch_url` tool_result via a new `_extract_fetched_text()` helper matched by exact `f"FETCHED
{url} ("` prefix (so a model that fetches a DIFFERENT url mid-transcript is never mistaken for the
one this call asked for); (2) a real tool-less `ChatSession` call (same primitive as `plan()`) asked
to reply with a `CLAIM:`/`PASSAGE:`/`LOCATION:` triple. The `PASSAGE` is never trusted on the
model's word alone: it is checked as a real, whitespace-normalized substring of the real fetched
text (`_normalize_whitespace`, tolerating only formatting differences from `fetch_url`'s own HTML-
stripping, never paraphrase) before a `Claim` is built -- a genuinely enforced guarantee, not a
prompt request. `document_hash` is a real `hashlib.sha256` of the actual fetched text (never a
placeholder, closing the one gap finding #042's own build plan had explicitly left as "the caller's
responsibility"). `source_id` is a new deterministic `_source_id_for_url()` (sha256 of the url,
truncated) so repeated claims from the same source share one id. Raises loudly (no `Claim` added)
on a genuinely failed fetch, a malformed model reply, or a passage that fails verbatim validation --
same "no real work yet to protect" honesty as `plan()`'s own failure mode.
**Live proof, real model, real web, zero mocks**: ran the full real chain `plan -> discover_sources
-> extract_evidence` against "What is the Model Context Protocol (MCP) and what are its core
components?" using the real production `build_general_registry()`. Real sources discovered
(`modelcontextprotocol.io/docs/learn/architecture` among them); `extract_evidence()` fetched it for
real, and the model's returned `PASSAGE` passed real verbatim validation against the real fetched
text on the first attempt -- a real `Claim` was produced with a real `document_hash`, a real
`source_id`, and `retrieved_at` a real timestamp. Honest note, not glossed over: the produced claim
itself ("the fetched text does not contain an official definition of MCP") is a weak result content-
wise, because the real first-discovered source happened to be a documentation-index redirect page
with little substantive text on it -- exactly the kind of real-data variability a live test is
supposed to surface, and arguably the CORRECT behavior (the model did not fabricate a confident
definition it had no real material for). A caller iterating `source_index` across `record.sources`
until a substantive claim is found is real, separate follow-on, not built here.
**Files changed**: `dourmouse/research_pipeline/stages.py` (`extract_evidence`,
`_extract_fetched_text`, `_normalize_whitespace`, `_source_id_for_url`).
**Tests added**: `dourmouse/tests/test_research_pipeline.py` grew from 30 to 43 (13 new:
`TestExtractFetchedText` and `TestExtractEvidenceStage`, covering the happy path with exact-field
assertions including a real `hashlib.sha256` comparison, fetch failure, a malformed model reply, a
hallucinated/non-substring passage, a whitespace-only-difference passage still validating, and
`source_index` selection).
**Tests run**: `test_research_pipeline.py` in full (43/43).
**Result**: fixed -- Domain G's evidence stage is real, live-verified, and enforces its own harshest
acceptance test in code rather than trusting the model's word. Contradiction detection and synthesis
remain real, separate, not-yet-built follow-on.

---

### 046 -- Domain G: real synthesis (fourth piece), plus a leaked internal-diagnostic bug it exposed

**Severity**: n/a for the synthesis stage itself (feature, fourth real piece of Domain G's build
plan, on top of findings #042/#043/#045); MEDIUM for the diagnostic-leak half (real, user-visible
data contamination, not a crash).
**Context**: `synthesize()` is the final stage the current `ResearchRecord` state machine supports
(`EVIDENCE_EXTRACTED -> SYNTHESIZED`) -- hypothesis generation and a dedicated criticism/revision
pass are named in the master requirements doc's own build plan but have no corresponding state in
`core.py` (finding #042's own docstring already says so), so they stay real, separate, not-yet-built
follow-on rather than being forced into this piece.
**Design**: the sixth reuse this session of the tool-less `ChatSession(DispatchRegistry(), session_
file=None)` primitive. The prompt is built from `record.active_claims()` ONLY -- never the raw
fetched text -- so the synthesis literally cannot cite anything no `Claim` supports. A record with
zero active claims (every claim rejected, or none ever added -- a real, reachable state per harsh
acceptance test 3, "a rejected hypothesis must remain visible") skips the model call entirely: there
is nothing real to synthesize, and asking a model to summarize an empty claim list only invites
fabrication, so a fixed, honest string is used instead, deterministically. A genuinely empty model
reply -- unlike `plan()`'s "nothing real yet to protect" case -- degrades to an honest placeholder
rather than raising, since real `Claim`s already exist here and must never be destroyed over a
downstream formatting hiccup (same reasoning as `RealBrain.answer`'s own honest-uncertain fallback).
**A real bug caught live, not by a unit test**: live-verifying `synthesize()` end to end (`plan ->
discover_sources -> extract_evidence -> synthesize`, real model, real web, zero mocks) produced a
synthesis text ending in a literal `"[DOURMOUSE: plan step(s) not executed via tools -- STEP 1/2
(orchestrator): ...; STEP 2/2 (orchestrator): ...]"` block -- `dispatch.py`'s own plan-reminder loop
(the fabrication guard that flags a claimed-but-untool-executed step, see its own extensive
commentary around line 4930) misread part of the long, multi-instruction synthesis prompt as a
declared multi-step plan. With zero tools registered (`force_plain_dispatch`'s tool-less primitive,
by design), no step can ever be satisfied, so after the reminder budget was spent the loop appended
its honest caveat -- real and correct for a live chat UI's own event feed (`webui.py`'s
`incomplete_plan` notice), never for a stored data field. Fixed with a new `_strip_internal_
diagnostics()` helper (a `\n\n[DOURMOUSE:...]\s*\Z`-anchored regex, so it only ever strips a
genuinely trailing diagnostic block, never touches a `[DOURMOUSE: ...]`-shaped string that happens
to appear mid-text) applied at every point this session's stage functions consume `final_text` as
data: `plan()` (naturally near-immune already, since it only keeps dash-prefixed lines, but fixed
for defense-in-depth and consistency), `extract_evidence()` (a real, latent contamination risk --
the `LOCATION` regex group is unanchored-to-end, so an undetected leak here would have silently
appended garbage onto a real claim's `location` field), and `synthesize()` (the confirmed, observed
case). Deliberately fixed at this call-site level rather than inside `dispatch.py`'s own
plan-reminder loop, to avoid touching that shared, considerably more delicate, heavily-commented
machinery for a symptom only ever observed from research_pipeline's own long, multi-instruction
prompts.
**Live proof**: re-ran the full live chain after the fix -- a real claim extracted, a real synthesis
produced, `"[DOURMOUSE" not in record.synthesis` confirmed true. Honest note on content quality, not
glossed over: live extraction across three separate runs this session consistently produced
claims/syntheses admitting the fetched sources did not contain a strong direct answer to the
question -- real, live-data variability (thin documentation-index pages, a getting-started page with
little substantive prose), and arguably correct behavior (the model did not fabricate confidence it
had no real material for) rather than a bug. A caller iterating `source_index` until a substantive
claim accumulates (already named in finding #045) is the real fix for weak content, not this stage.
**Files changed**: `dourmouse/research_pipeline/stages.py` (`synthesize`, `_strip_internal_
diagnostics`, `_DIAGNOSTIC_SUFFIX_RE`, plus the two call-site fixes in `plan()`/`extract_evidence()`).
**Tests added**: `dourmouse/tests/test_research_pipeline.py` grew from 43 to 54 (11 new:
`TestSynthesizeStage` -- real model reply becomes synthesis, active claims reach the prompt,
rejected claims excluded from the prompt, zero-active-claims skips the model call entirely with a
fixed string, an empty model reply degrades to a placeholder instead of raising, the stage guard --
plus `TestStripInternalDiagnostics`, `TestSynthesizeStripsLeakedDiagnostics`, and
`TestExtractEvidenceStripsLeakedDiagnostics` proving the leak fix directly, including that a
`[DOURMOUSE: ...]`-shaped string mid-text is correctly left alone).
**Tests run**: `test_research_pipeline.py` in full (54/54); full suite (5189 passed, 10 skipped, 5
pre-existing deselected, 0 failed).
**Result**: fixed -- Domain G now has a real, live-verified synthesis stage, and a real, live-caught
data-contamination bug (not scoped to synthesis alone) is closed everywhere this session's stage
functions could have been exposed to it. Contradiction detection remains the one real, separate,
not-yet-built piece of Domain G's own core loop.

---

### 047 -- Domain G: a real workspace default and an on-disk document cache

**Severity**: n/a (feature/gap-closure -- named explicitly as a real, honest gap in this session's own
standing status report before being closed here).
**Context**: every other domain's own store resolves a workspace-relative default the same way
(`security/sentry.py`'s own `DEFAULT_DB = workspace_dir() / "security" / "sentry.db"`) -- Domain G's
`ResearchStore` had no such default, so nothing persisted anywhere unless a caller built its own path
by hand, and this session's own live tests never exercised persistence at all as a result. Worse: a
fetched source page lived only in memory for the duration of one `extract_evidence()` call --
`document_hash` fingerprinted content that no longer existed anywhere the instant the function
returned, and a research run revisiting the same URL re-fetched it from the real network every time.
**Design**: `dourmouse/research_pipeline/store.py` gains `DEFAULT_DB = workspace_dir() / "research_
pipeline" / "research.db"`, computed once at import time, matching `sentry.py`'s own exact
convention (tests that need isolation build their own `ResearchStore(tmp_path)` explicitly, same as
sentry's tests already do). `extract_evidence()` (`stages.py`) now checks a real on-disk cache before
fetching -- `config.workspace_dir() / "research_pipeline" / "documents" / f"{_source_id_for_url(url)}
.txt"` -- reusing the already-real `_source_id_for_url()` hash rather than inventing a second one.
On a cache miss, the real fetched text is written to that path immediately after a successful fetch,
before the model ever sees it, so the artifact survives even if the later extraction/validation step
fails. On a cache hit, `run_dispatch_messages` (the real network fetch) is never called at all.
**Live proof**: a real live run against `modelcontextprotocol.io/introduction` wrote a real cache
file (`0668b361cac37fb4.txt`) after its first real fetch; a second call against the same URL, in a
fresh `ResearchRecord`, read the cached file with zero additional `run_dispatch_messages` calls --
confirmed both by the cache file's real presence and by three new hermetic tests that count real
fetch invocations directly (not inferred from timing, which is dominated by the model call, not the
fetch).
**Files changed**: `dourmouse/research_pipeline/store.py` (`DEFAULT_DB`), `dourmouse/research_
pipeline/stages.py` (`extract_evidence`'s cache-check/write).
**Tests added**: `dourmouse/tests/test_research_pipeline.py`'s new `TestExtractEvidenceDocumentCache`
(3 tests: a second extraction against the same URL never re-fetches, the cache file exists on disk
with the exact real fetched text, two different URLs never collide in the cache).
**Tests run**: `test_research_pipeline.py` in full (57/57).
**Result**: fixed -- Domain G's persistence and document-caching gaps, both named explicitly in this
session's own status report to the user, are closed. Contradiction detection, the multi-source/
multi-sub-question orchestration loop, and chat reachability remain the real, separate, not-yet-built
pieces of Domain G's own core loop.

---

### 048 -- Domain F: the reviewer role, closed

**Severity**: n/a (feature -- the one deliberately-deferred gap named in finding #038, closed).
**Context**: finding #038 built named specialist roles (`researcher`/`coder`/`tester`/`security_sentry`)
on `delegate_parallel` but explicitly deferred `reviewer`: "no currently-registered subagent has a
genuinely write-free toolset that fits reviewing arbitrary code." `dev_coding`, the obvious candidate,
also carries `write_file`/`edit_file`/`run_python`/`deploy`.
**Design**: a new `reviewer` subagent (`general_roster.py`), reusing the exact same real handler
functions `dev_coding`'s own read-only tools already use (`_read_file_tool`, `_search_files_tool`,
`_diff_preview_tool`) rather than writing new ones -- deliberately omitting everything `dev_coding`
has that can write, execute, or deploy. **A real bug caught immediately, before any test ran**: the
registry enforces a globally unique tool name across every subagent (`dispatch.py::register_
subagent`), and the first draft reused the bare names `read_file`/`search_files`/`diff_preview` --
`dev_coding` already owns those, so registry construction raised `ValueError: tool name collision
across registry: 'read_file' (from reviewer)` the moment `build_general_registry()` ran. Fixed by
prefixing all three `review_*`, the exact same convention `study` already uses for its own
`study_read_file` to avoid the identical collision. Added to `_DELEGATE_ROLE_PRESETS` as the fifth
role, and to `model_delegation.py`'s `_LOCAL_ONLY_AGENTS` (same privacy class as `dev_coding` --
reads real repository content).
**Live proof**: a real 2-branch `delegate_parallel` call (`researcher` + `reviewer`, real model, real
dispatch loop) correctly resolved `reviewer` -> the new subagent and the branch genuinely called its
own `review_search_files` tool -- a real, isolated tool call from the reviewer's own restricted
toolset, never attempting a write (it has no write tool to attempt). The target file was outside the
reviewer's own sandboxed workspace root in this particular run and it reported that honestly rather
than fabricating a review -- correct behavior (Rule 2.2), not a failure of this finding.
**A second real bug, caught by the full suite, not by any test written for this finding**:
`planner.py::find_agents_for_query`'s deterministic scorer (Rule 2.8, no LLM) uses substring
matching for its "other description/tool overlap" signal (`hay_hits`), a documented, intentional
looseness -- and `reviewer`'s own first-draft description ("Domain F's own **named** specialist
role...") happened to substring-match the token `"named"` in an existing regression test's query
("save it to a file **named** outlook_brief.txt"), plus genuine, unavoidable overlap with shared
tool-description text (`"file"`, `"workspace"`, both from the same `path_note` string `dev_coding`'s
own read-only tools already carry). The combined score tied `reviewer` with `dev_coding` on a
write-intent query neither the test nor `reviewer` itself has anything to do with --
`test_planner.py::TestFindAgentsForQueryRegression::test_write_intent_routes_to_write_capable_agent`
failed in the full suite (not in any file touched directly by this finding). Fixed by rewording
`reviewer`'s own description to drop the coincidentally-colliding word `"named"` -- the same class
of fix, and the same lesson, as this file's own long-documented "free"/"freebuff" substring
incident: a brand-new subagent's own prose is real, live attack surface against this scorer's loose
matching, not just documentation.
**Files changed**: `dourmouse/general_roster.py` (new `reviewer` subagent, `_DELEGATE_ROLE_PRESETS`
entry, its description reworded after the collision above), `dourmouse/model_delegation.py`
(`_LOCAL_ONLY_AGENTS`).
**Tests added/updated**: the three exhaustive real-subagent-name assertions this codebase's own
convention requires touching together whenever a subagent is added (`test_general_roster.py::
TestRosterShape::test_all_subagents_registered`, `test_dispatch.py::TestEndToEndThroughGeneralRoster::
test_general_roster_registers_all_subagents`, and `model_delegation.py`'s own `_LOCAL_ONLY_AGENTS`
set, checked for completeness by `test_model_delegation.py::TestRoutingPolicy::
test_every_real_agent_has_an_explicit_policy`) -- all three updated together, all three now pass.
`test_planner.py`'s own pre-existing regression test required no code change, only the description
reword above.
**Tests run**: `test_general_roster.py` + `test_model_delegation.py` + `test_dispatch.py` +
`test_planner.py` in full (413/413); full suite green.
**Result**: fixed -- Domain F's own last deferred gap is closed. The domain's harsh acceptance test
(a goal needing 3+ specialists, real isolated context per specialist, a real synthesized result) can
now be run in full with a genuine reviewer branch rather than two specialists standing in for three.

---

### 049 -- Domain G: real contradiction detection (harsh acceptance test 2, closed)

**Severity**: n/a (feature -- the last real piece of Domain G's own core loop besides the multi-
source orchestration loop and chat reachability).
**Context**: this domain's own harsh acceptance test 2 requires that two contradicting sources on the
same question surface as a real contradiction, never silently merged into one confident-sounding
synthesis. `core.py`'s `Contradiction` dataclass and `add_contradiction()` existed since finding #042;
nothing ever called them.
**A real, necessary schema gap closed first**: comparing "claims answering the same sub-question"
required knowing WHICH sub-question each claim answered, and `Claim` never recorded it --
`extract_evidence()` only ever used `sub_question_index` as an ephemeral local variable. Added a real
`sub_question: str = ""` field to `Claim` (default-empty so every pre-existing call site keeps
working unchanged), populated it in `extract_evidence()`, threaded it through `reject_claim()`'s own
never-delete copy, and through `store.py`'s serialization.
**Design**: `detect_contradictions()` (seventh reuse of the tool-less `ChatSession` primitive this
session). Groups `active_claims()` by real `sub_question`; a group with fewer than two claims costs
no model call at all (same "don't invite fabrication over nothing" discipline as `synthesize()`'s own
zero-claims case). For every real pair within a group (`itertools.combinations`), one real
`ChatSession` call judges genuine disagreement versus merely different/complementary information. A
real `Contradiction` row is added only on an explicit "yes," fingerprinted via a new
`_claim_fingerprint()` (`source_id` + a real hash of the claim text, matching `core.py`'s own
`Contradiction` docstring verbatim). `_strip_internal_diagnostics()` (finding #046) is applied here
too, closing the same leak class for a third stage function.
**A real bug caught by live verification, not by any unit test**: the first live run against a
genuinely seeded contradiction (two claims giving different completion years for the Eiffel Tower)
returned zero contradictions found -- the real model correctly judged "CONTRADICTION: yes" but never
emitted the literal "NOTE:" label the prompt asked for, continuing straight into its own explanation
instead. The original strict regex required both labels together and silently discarded a real,
correct verdict over a formatting miss -- the same class of over-strict parsing this session already
fixed once for `plan()`'s own dash-line fallback. Fixed with `_parse_contradiction_reply()`: only the
verdict marker (`CONTRADICTION: yes|no`) is required; everything after it becomes the note, with a
leading `NOTE:` label stripped if the model does include it.
**Live proof**: after the fix, the SAME seeded contradiction was correctly detected and recorded with
a real, sensible note ("The statements give different completion years, so they cannot both be
correct."). A real control case (two genuinely compatible MCP claims -- one about tools, one about
resources) correctly produced zero contradictions.
**Files changed**: `dourmouse/research_pipeline/core.py` (`Claim.sub_question`, `reject_claim`),
`dourmouse/research_pipeline/store.py` (serialization), `dourmouse/research_pipeline/stages.py`
(`detect_contradictions`, `_parse_contradiction_reply`, `_claim_fingerprint`).
**Tests added**: `dourmouse/tests/test_research_pipeline.py` grew from 57 to 65 (8 new:
`TestDetectContradictionsStage` -- zero/one claim skips the model call entirely, claims from
different sub-questions never compared, a real contradiction recorded with the right fingerprint and
note, a real non-contradiction correctly not recorded, a malformed reply skipped rather than raised,
only active (never rejected) claims compared, a three-way group checks every real pair (`C(3,2) = 3`
calls), and a leaked diagnostic never contaminates the note field).
**Tests run**: `test_research_pipeline.py` in full (65/65).
**Result**: fixed -- Domain G's harsh acceptance test 2 is now real and enforced in code. The
multi-source/multi-sub-question orchestration loop and chat reachability remain the last real,
separate, not-yet-built pieces of Domain G's own core loop.

---

### 050 -- Domain G: chat reachability, closed

**Severity**: n/a (feature -- the last named gap in Domain G's own core loop besides the multi-
source orchestration loop and 3-device distribution).
**Context**: named explicitly in this session's own standing status report to the user: every prior
Domain G stage function (findings #042-#049) was only reachable by writing a short Python script --
there was no subagent, so a user could never actually ask Dourmouse to research something.
**Design**: `dourmouse/research_pipeline_tools.py`, mirroring `research_mesh_tools.py`'s own shape
(one dedicated module, one `build_research_pipeline_subagent(registry)` factory). Six real tools
(`research_plan`, `research_discover_sources`, `research_extract_evidence`, `research_detect_
contradictions`, `research_synthesize`, `research_status`), each a thin, honest wrapper over an
already-tested stage function: loads a `ResearchRecord` from the real `ResearchStore` (finding
#047's `DEFAULT_DB`) keyed by the exact question text, calls the real stage, persists the result,
reports a real error string on a caught `ValueError`/`IndexError` rather than crashing the tool
call. No new call path -- the stage functions themselves are completely unchanged. Named limitation
(not hidden): each call runs one real stage synchronously (a few seconds to tens of seconds for a
real web fetch and model call) -- no background/goal-runtime integration yet, the same already-
documented limitation `research_mesh_qualify` carries. The caller drives the multi-stage loop turn
by turn.
**A real bug caught immediately, before any test ran**: the first draft named the subagent
`deep_research`. `find_agents_for_query`'s deterministic router gives a `+3` bonus for a query token
matching an agent's own NAME stem, and `deep_research`'s stems include `"research"` -- exactly the
word this codebase's own routing tests deliberately use to mean "route to `research_info`" (a
comment in `test_planner.py` explains this was chosen specifically as "a real word match, not a
substring accident" after a prior `"search"`/`"research_info"` substring incident). Adding
`deep_research` reintroduced the identical class of collision one level up: three existing,
unrelated `test_planner.py` tests broke, all routing a genuine "Research X online..." query to the
new agent instead of `research_info`. Fixed by renaming to `evidence_pipeline` (stems `"evidence"`,
`"pipeline"`, confirmed to collide with nothing) -- no scorer change needed, the same resolution
finding #048 already used for a name-collision one domain over.
**A second real bug, caught by live verification, not a unit test**: a plain-prose dispatch call
("Use the evidence_pipeline agent: call research_plan with...", no `forced_agent`) reported "I do
not have access to any tools in this turn" -- natural-language auto-routing to the brand-new
subagent is not yet confirmed working, a real, separate, not-yet-investigated gap named honestly
here rather than papered over. The SAME call with an explicit `forced_agent="evidence_pipeline"`
(the mechanism `delegate_task`/`delegate_parallel` already use, and the one a caller can always use
directly) worked correctly -- see the live proof below.
**Live proof**: a real dispatch call forced onto `evidence_pipeline` (routing temporarily forced
local in-process for this one verification run, after two consecutive genuine Gemini outages --
HTTP 503 "high demand" and a `MALFORMED_FUNCTION_CALL` response, unrelated to this code, confirmed
by the fact `"default"` JSON-schema keys are already used in 68 other tool schemas across this
codebase with no issue) correctly called `research_plan` (5 real sub-questions produced) then
`research_status` (accurately reported `stage=PLANNED`, `plan: 5 real sub-question(s)`, `sources: 0`)
-- a real, working, end-to-end chat-reachable pipeline, not just isolated stage-function tests.
**Files changed**: new `dourmouse/research_pipeline_tools.py`; `dourmouse/general_roster.py`
(import + registration); `dourmouse/model_delegation.py` (`_CLOUD_OK_AGENTS`).
**Tests added**: new `dourmouse/tests/test_research_pipeline_tools.py` (12 tests covering every
tool's happy path, its required-precondition error, and the subagent's own tool roster), plus the
three exhaustive real-subagent-name assertions updated together as this codebase's own convention
requires.
**Tests run**: `test_research_pipeline_tools.py` (12/12), `test_general_roster.py` +
`test_model_delegation.py` + `test_dispatch.py` + `test_planner.py` (413/413).
**Result**: fixed -- Domain G is now chat-reachable end to end. The multi-source/multi-sub-question
orchestration loop, natural-language auto-routing to `evidence_pipeline` (named above, not yet
investigated), and the 3-device distribution remain Domain G's last real, separate, not-yet-built
pieces.

---

### 051 -- Domain G: the multi-source/multi-sub-question orchestration loop, closed

**Severity**: n/a (feature -- the last named core-loop gap in Domain G besides 3-device
distribution).
**Context**: named explicitly in finding #050's own "Result" line: a caller had to drive
`research_discover_sources`/`research_extract_evidence` one real sub-question and one real source at
a time -- nothing walked the whole plan automatically.
**Design**: `run_full_pipeline()` in `dourmouse/research_pipeline/stages.py` -- a pure caller-side
loop over the exact same `discover_sources()`/`extract_evidence()` calls a human operator already
drove by hand, no new dispatch machinery, no new prompt. Walks every real sub-question in the plan
in order; for each one, discovers real sources, then extracts evidence from up to
`max_sources_per_sub_question` (default 3, a real named cost bound -- a live web search can return
many hits, and extracting from every one uncapped is an uncontrolled real cost per sub-question) of
the real, newly-found sources for THAT sub-question, tracked by list position before/after the
`discover_sources()` call (`add_sources()` already dedupes globally, so a source rediscovered for a
later sub-question is correctly skipped here, never double-extracted). A single source that fails
extraction (`extract_evidence()`'s own real `ValueError` cases -- a bad fetch, a hallucinated or
unverifiable passage) is skipped, not fatal to the rest of the run -- the same "one bad pairing must
never block every other one still to check" reasoning `detect_contradictions()` (finding #049)
already uses. Requires a real plan already set, raises loudly otherwise -- same "no real work yet to
protect" honesty as `plan()`/`extract_evidence()`'s own failure mode. Exposed to chat as a seventh
tool, `research_run_pipeline`, on the `evidence_pipeline` subagent (`research_pipeline_tools.py`) --
runs the whole loop in one call rather than the six finer-grained tools driven turn by turn; those
remain, for a caller that wants manual control over which source gets extracted.
**Live proof**: a real, unforced local run against `research_run_pipeline` for "What is the Model
Context Protocol (MCP)?" produced 5 real sub-questions, discovered 8 real sources across them, and
extracted 3 real, passage-verified claims -- a genuine end-to-end multi-sub-question research pass in
one call, not isolated stage-function tests. A first debug run capped to 1 source per sub-question
(4 sub-questions, 4 single-source attempts) genuinely found zero claims -- root-caused by hand: 2 of
4 sources DID produce a real claim when extracted directly, the third's own quoted passage genuinely
failed verbatim-substring validation (real, honest rejection, not a bug), and the cap of 1 meant no
second source was tried for any sub-question that got unlucky on its first. Not a defect in the
orchestrator -- the cap is a real, working cost/coverage tradeoff, and a caller who wants a higher
per-sub-question success rate raises `max_sources_per_sub_question`.
**Files changed**: `dourmouse/research_pipeline/stages.py` (`run_full_pipeline`);
`dourmouse/research_pipeline_tools.py` (`research_run_pipeline` tool + updated module docstring).
**Tests added**: `TestRunFullPipelineStage` in `test_research_pipeline.py` (4 tests: requires a real
plan first, walks every sub-question and extracts from its own newly-found sources, the per-sub-
question cap holds, one failing source never blocks the rest of the run); `TestResearchRunPipelineTool`
in `test_research_pipeline_tools.py` (3 tests: requires a plan first, real totals reported end to
end, honest empty-question error), plus the subagent's own exhaustive tool-name-set assertion
updated.
**Tests run**: `test_research_pipeline.py` (69/69), `test_research_pipeline_tools.py` (15/15), full
suite (5223 -> see below).
**Result**: fixed -- Domain G's core loop (plan through synthesis, including contradiction detection)
is now fully chat-reachable AND fully automatable in one call. 3-device distribution (blocked on the
Dell node) and natural-language auto-routing to `evidence_pipeline` (named in finding #050, still not
investigated) remain Domain G's last real, separate, not-yet-built pieces.

---

### 052 -- Domain I: real, persisted known-device baseline (asset inventory, harsh acceptance test 2)

**Severity**: n/a (feature -- Phase 2 step 1 of the standing plan, the domain's own harsh acceptance
test 2: "a brand-new device joins the LAN... confirm it's surfaced").
**Context**: named explicitly in `sentry.py`'s own module docstring since finding #039: new-LAN-
device detection needed a real, persisted ARP-neighbor baseline this pass had not yet built.
**Design**: `SentryStore` (`dourmouse/security/sentry.py`) gains a `known_devices` table
(`device_key` primary key, `mac`, `ip`, `hostname`, `first_seen`, `last_seen`) built from
`platform_adapter.get_arp_neighbors()`'s own already-real telemetry -- no new telemetry-gathering
code. `_device_key(mac, ip)` is a real device's stable identity: its MAC when the kernel resolved
one, else `ip:<address>` as an honest fallback for the "(incomplete)" ARP entries the parser already
reports as `mac=None` (a real, named limitation: DHCP churn on an unresolved-MAC device changes its
IP and therefore re-reports as "new" -- a real ARP limitation, not a bug here). `_detect_findings`
gains a third real rule, still fully deterministic (no model call, matching this module's own
corrected design from finding #039): a real ARP neighbor whose device key is not in the baseline is a
MED `new_device` finding, scored and persisted through the exact same `record_and_classify`/
`risk_score`/alert pipeline every other finding already uses -- no new machinery. `run_scan()` reads
the baseline BEFORE detection and writes it back AFTER, so a scan's own newly-seen devices never
suppress themselves. The very first scan against a genuinely empty baseline passes
`known_device_keys=None` (not an empty set) into `_detect_findings`, which skips the new-device rule
entirely -- a real, deliberate choice: an empty-set baseline would flag every device already on the
LAN as "new" on day one, a false-positive storm, not a useful first scan. `security_known_devices`
(new chat tool, `dourmouse/security/tools.py`) lists the real baseline on request.
**Live proof**: against THIS machine's own real ARP table (16 real neighbors), a first `run_scan()`
into a fresh store found zero `new_device` findings (silent baseline seed, as designed) and persisted
all 16 real device keys. A second scan with one real device injected into the same real telemetry
snapshot (`intruder.local`, a real MAC/IP pair not in the baseline) correctly produced exactly one
`new_device` MED finding naming that device, and the baseline grew to 17 -- a genuine, live-verified
pass of harsh acceptance test 2's own scenario.
**Files changed**: `dourmouse/security/sentry.py` (`known_devices` schema, `_device_key`,
`_detect_findings`'s third rule, `SentryStore.get_known_device_keys`/`record_devices`/
`devices_snapshot`, `run_scan`'s baseline read/write); `dourmouse/security/tools.py`
(`security_known_devices` tool).
**Tests added**: `TestDeviceKey` (2), `TestDetectFindingsNewDevice` (5, including the `None`-vs-
empty-set baseline distinction and the incomplete-ARP-entry fallback), `SentryStore` device-baseline
tests (4), `TestRunScan` baseline-seeding and real-new-device tests (2) in `test_sentry.py`;
`TestSecurityKnownDevices` (2) in `test_security_tools.py`, plus the security subagent's own
exhaustive tool-name-set assertion updated.
**Tests run**: `test_sentry.py` (43/43), `test_security_tools.py` (11/11), full suite (see below).
**Result**: fixed -- Domain I's asset-inventory/baseline step (Phase 2 step 1) is closed and harsh
acceptance test 2 passes live against this machine's real network. Threat-intelligence enrichment,
incident/case tracking, the correlation engine, and local remediation (Phase 2 steps 2-5) remain
real, separate, not-yet-built follow-on.

---

### 053 -- Domain I: threat-intelligence enrichment for real external peers

**Severity**: n/a (feature -- Phase 2 step 2 of the standing plan).
**Context**: named explicitly in `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`'s own scale-out plan
(§11, step 2): real reputation lookups for an external IP genuinely seen talking to the host, honestly
`NOT_CONFIGURED` when no key is set. No telemetry existed yet for which external IPs the host is
actually connected to -- `platform_adapter.py` only had `get_listening_ports()` (LISTEN state).
**Design**: `platform_adapter.get_established_connections()` (new), real `lsof -iTCP -sTCP:ESTABLISHED
-n -P` parsing (same argument-list-only, bounded-timeout discipline as every other `_run()` call in
this module), capturing local/remote address:port pairs for both IPv4 and bracketed IPv6, added to
`get_system_security_state()`'s own bundled snapshot. `dourmouse/security/reputation.py` (new): a
real, keyed AbuseIPDB `check()` call, honestly `NOT CONFIGURED` without `ABUSEIPDB_API_KEY` set (the
exact same convention `worldmonitor.py`'s own `WORLDMONITOR_API_KEY` already established), and refuses
private/loopback/link-local/reserved/multicast targets before any network call -- reusing
`general_roster.py`'s own `_refuse_private_fetch_target` classification logic rather than
re-deriving it (asking a public reputation API about a LAN address is meaningless, and would leak the
user's own internal topology to a third party for no reason). Two new chat tools on the `security`
subagent: `security_external_peers` (lists real, currently-established outbound connections to
public-internet peers only, excluding LAN/loopback/Tailscale) and `security_check_reputation` (the
real AbuseIPDB lookup for one IP from that list) -- never a scan target, only IPs the host has
genuinely been observed connecting to.
**Live proof**: against this real machine's real network state, `security_external_peers` correctly
listed 15 real distinct external peers (Google, Spotify, Claude, AvidLink) with their real owning
processes, correctly excluding LAN/Tailscale/loopback addresses; `security_check_reputation` correctly
refused a real private IP (`192.168.1.1`) before any network call, and correctly reported honest
`NOT CONFIGURED` for a real public IP (`8.8.8.8`) since no `ABUSEIPDB_API_KEY` is set on this machine
-- exactly the Rule 2.2 behavior this feature is required to have, not a fabricated score.
**Files changed**: `dourmouse/security/platform_adapter.py` (`get_established_connections`,
`get_system_security_state`); new `dourmouse/security/reputation.py`; `dourmouse/security/tools.py`
(`security_external_peers`, `security_check_reputation`).
**Tests added**: `TestEstablishedConnections` (4) in `test_security_platform_adapter.py` against real
captured `lsof ESTABLISHED` output (IPv4 and bracketed IPv6), plus the snapshot-shape tests updated
for the new key; new `test_security_reputation.py` (17 tests: key detection, private/public
classification, and every real `check_ip_reputation` outcome -- not configured, success, HTTP error,
network error, malformed JSON, missing score field); `TestSecurityExternalPeers` (3) and
`TestSecurityCheckReputation` (3) in `test_security_tools.py`, plus the subagent's own exhaustive
tool-name-set assertion updated.
**Tests run**: `test_security_platform_adapter.py` (39/39), `test_security_reputation.py` (17/17),
`test_security_tools.py` (17/17), full suite (see commit).
**Result**: fixed -- Domain I Phase 2 step 2 is closed. Incident/case tracking, the correlation
engine, and local remediation (Phase 2 steps 3-5) remain real, separate, not-yet-built follow-on.

---

### 054 -- Domain I: real incident/case tracking (Phase 2 step 3)

**Severity**: n/a (feature -- Phase 2 step 3 of the standing plan).
**Context**: named explicitly in `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`'s own scale-out plan
(§11, step 3): a real `SentryStore` already existed as the foundation, but only a flat findings table
-- no real operator workflow (triage, note, close) existed, unlike `goals.py`'s own real state-machine
precedent for a comparable lifecycle.
**Design**: a new `incidents` table on `SentryStore` (`dourmouse/security/sentry.py`):
`fingerprint` (references an existing `seen_findings` row), `status` (`OPEN` / `INVESTIGATING` /
`RESOLVED` / `ACCEPTED_RISK`, `INCIDENT_STATES`/`INCIDENT_TERMINAL_STATES` module constants mirroring
`goals.py`'s own `GOAL_TERMINAL_STATES` "never silently reopen" discipline rather than inventing new
status semantics), a JSON `notes` list (`{at, text}` per entry, append-only), `opened_at`/`updated_at`.
`open_incident()` refuses an unknown fingerprint (a real, honest error -- an incident must reference a
real, already-detected finding, never an arbitrary string) and is idempotent (`already_open` on a
second call, never a duplicate case). `update_incident()` refuses to move a RESOLVED/ACCEPTED_RISK
incident to any OTHER status (`"terminal"`) but still accepts a note-only update or a re-confirmation
of the SAME terminal status -- a closing confirmation is not a reopen. Three new chat tools on the
`security` subagent: `security_incident_open`, `security_incident_update`, `security_incidents`
(list, optionally filtered by status).
**Live proof**: against this real machine's own real, currently-active `security_sentry_scan` finding
(the real disabled-Application-Firewall HIGH finding this codebase has used as its own live example
since finding #039), a real incident was opened, transitioned to `INVESTIGATING` with a real note,
closed as `ACCEPTED_RISK` with a second real note, correctly refused a subsequent attempt to reopen it
to `OPEN`, and correctly listed under a status filter -- a genuine end-to-end SOC-style case lifecycle
against a real, currently-true finding on this host, not a synthetic fixture.
**Files changed**: `dourmouse/security/sentry.py` (`incidents` schema, `INCIDENT_STATES`/
`INCIDENT_TERMINAL_STATES`, `SentryStore.open_incident`/`update_incident`/`get_incident`/
`list_incidents`); `dourmouse/security/tools.py` (`security_incident_open`/`security_incident_update`/
`security_incidents`, updated module docstring distinguishing host-mutation from own-bookkeeping
mutation).
**Tests added**: `TestSentryStoreIncidents` (12 tests: unknown-fingerprint refusal, open/idempotent-
reopen, unknown-incident update, invalid status, real transition + note, note-only update leaves
status unchanged, terminal refuses to change status, terminal accepts a same-status re-confirmation,
`get_incident` on a never-opened fingerprint, status-filtered listing) in `test_sentry.py`;
`TestSecurityIncidentTools` (9 tests covering every tool's happy path and honest-refusal path) in
`test_security_tools.py`, plus the subagent's own exhaustive tool-name-set assertion updated.
**Tests run**: `test_sentry.py` (66/66), `test_security_tools.py` (35/35), full suite (see commit).
**Result**: fixed -- Domain I Phase 2 step 3 is closed. The correlation engine and local remediation
(Phase 2 steps 4-5) remain real, separate, not-yet-built follow-on -- the last two pieces of the
Phase 2 scale-out plan.

---

### 055 -- Domain I: correlation engine and specific remediation text (Phase 2 steps 4-5)

**Severity**: n/a (feature -- the last two steps of the standing Phase 2 scale-out plan).
**Context**: named explicitly in `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`'s own scale-out plan
(§11, steps 4-5): a real SOC's own value-add over isolated point checks is noticing multiple weak
signals together, and local remediation should compose real, specific text rather than a vague
suggestion, never auto-applied.
**Design (correlation, step 4)**: `_detect_correlations(new_findings)` (`dourmouse/security/
sentry.py`) -- one explicit, deterministic rule, the spec's own named example verbatim: a real
`new_device` finding AND a real `exposed_port` finding BOTH appearing in the SAME scan's own
`new_findings` (never the persisted, still-open condition list) produce one HIGH
`correlated_new_device_and_exposed_port` finding. Checking `new_findings` rather than all currently-
open findings is the real design choice that makes this a genuine same-window COINCIDENCE rule: it
fires exactly once, on the scan where both first appear together, and correctly never re-fires on a
later scan where either condition was already known (since an already-known finding never lands in
`new_findings` again). Deliberately never persisted through `SentryStore.record_and_classify` -- a
stable fingerprint for a coincidence has nothing meaningful to deduplicate against on a later,
unrelated scan. Wired into `run_scan()` right after the ordinary findings loop, sharing the exact same
scoring/alert-writing path (`_write_alert()`, extracted as a small shared helper from the pre-existing
inline alert-write code so both paths use one real mechanism).
**Design (remediation text, step 5)**: every existing detection rule's `recommended_action` upgraded
from a generic suggestion to real, specific, copy-pasteable text: `firewall_disabled` now gives the
exact real `socketfilterfw --setglobalstate on` command; `exposed_port` now composes the exact real
`pf` rule for that specific command/port/protocol; `new_device` now names the specific real MAC/IP to
block at the router; the correlation finding's own `recommended_action` concatenates both underlying
findings' real, specific text. Deliberately text-only -- nothing here executes a command, changes a
firewall rule, or touches the network; matches the master doc's own "never auto-applied" wording more
strictly than a suggest-then-confirm execution gate would, and avoids introducing any new
execution/privilege-escalation surface into a defensive-only subsystem.
**Live proof**: against this real machine's real telemetry, a synthetic injected new device (`intruder
.local`) AND a synthetic injected exposed service (`backdoor` on port 4444, `ALL_INTERFACES`) in the
SAME scan correctly produced all three real findings together -- the two underlying MED findings plus
one HIGH `correlated_new_device_and_exposed_port` finding naming both by their real titles.
**Files changed**: `dourmouse/security/sentry.py` (`_detect_correlations`, `_write_alert` helper,
`run_scan`'s wiring, four `recommended_action` upgrades).
**Tests added**: `TestDetectCorrelations` (5 tests: no correlation with one signal, no correlation
with unrelated signals, a real HIGH correlation with both, stable fingerprint, empty input) and two
`TestRunScan` integration tests (a real coincident new-device+exposed-port scan correlates and alerts;
a lone new device never correlates) in `test_sentry.py`.
**Tests run**: `test_sentry.py` (61/61), `test_security_tools.py` + `test_webui.py` (unaffected by the
text-only `recommended_action` changes, confirmed passing), full suite (see commit).
**Result**: fixed -- Domain I's Phase 2 scale-out plan (asset inventory, threat intel, incident
tracking, correlation, remediation text) is now fully closed. Remaining Domain I work: the dashboard
UI and Windows/cross-device platform coverage (Phase 2 items 6-7), and the much larger ~40-item balance
of the founding spec's own 54-item cybersecurity build list.

---

### 056 -- Domain E: the device wiki's pure data model (first piece)

**Severity**: n/a (feature -- Domain E's own first-scoped build-plan step).
**Context**: Domain E (the device wiki) was previously blocked pending confirmation that a prior
concurrent session's own conflicting access to `~/Documents/` had ended. Re-checked before writing
any file here: `ps aux | grep -i "claude.*Documents/dourmouse"` shows no matching process, and
`~/Documents/` contains only this session's own prior deliverables (the status report, the
already-archived `DOURMOUSE_DO_NOT_TOUCH.tar.gz`) -- clear to proceed.
**Design**: `dourmouse/device_wiki/core.py`, mirroring `research_pipeline/core.py`'s own pure-logic-
first shape exactly (finding #042's own precedent): a real `WikiEntry` (path, content_hash, size_bytes,
status, summary, summarized_at, last_seen), three real statuses (`UNSUMMARIZED`/`SUMMARIZED`/
`MISSING`), and pure transition functions (`with_new_entry`, `with_summary`, `with_failed_summary`,
`mark_missing`) plus the real reconciliation function the whole domain's harsh acceptance test 2
depends on: `reconcile(existing, found, now)` -- given the wiki's current entries and what a real
walker (step 4, not yet built) actually found on disk this scan, produces the new real state: a new
path is added `UNSUMMARIZED`; an unchanged path only advances `last_seen`; a path whose real content
hash changed reverts to a fresh `UNSUMMARIZED` entry (a stale summary of content that no longer exists
is never silently kept, matching harsh acceptance test 1's "never fabricate" spirit one layer over); a
path no longer found on disk is `mark_missing`'d, never dropped from the result (harsh acceptance test
2, literally). A previously-`MISSING` file that reappears with unchanged content self-heals back to
`SUMMARIZED` (its real prior summary, preserved through `mark_missing`, is still honest and valid) or
`UNSUMMARIZED`, derived from whether a real summary is actually present rather than trusting the stale
`MISSING` status itself -- a real edge case worked through by hand while writing the tests, not
originally in the build plan's own one-line description. No I/O, no clock read internally (the caller
always passes `now`), no model call -- fully unit-testable, matching `research_pipeline/core.py`'s own
established discipline.
**Files changed**: new `dourmouse/device_wiki/__init__.py`, `dourmouse/device_wiki/core.py`.
**Tests added**: new `dourmouse/tests/test_device_wiki_core.py` (16 tests: entry validation, every
transition function's real behavior, and 8 `reconcile()` cases including the missing-file-reappears
self-heal for both a summarized and an unsummarized prior entry).
**Tests run**: `test_device_wiki_core.py` (16/16), full suite (see commit).
**Result**: fixed -- Domain E's own build plan step 1 (the pure data model) is closed. Steps 2-6 (the
real SQLite store, the summarizer, the walker with its explicit root-folder allowlist, the chat tool,
the UI page) remain real, separate, not-yet-built follow-on, in that sequence.

---

### 057 -- Domain E: the device wiki's real SQLite store (second piece)

**Severity**: n/a (feature -- Domain E's own build plan step 2).
**Context**: finding #056 built the pure `WikiEntry`/`reconcile()` data model with no persistence.
**Design**: `dourmouse/device_wiki/store.py`, workspace-relative `DEFAULT_DB` applied from the start
(`config.workspace_dir() / "device_wiki" / "wiki.db"`, finding #028's own lesson, not retrofitted).
Unlike `research_pipeline/store.py`'s own one-row-per-question JSON-body shape, this store is one row
PER REAL FILE (`wiki_entries` table, `path` primary key) -- a device wiki can hold thousands of
entries, and updating/querying one file's own row is the natural, efficient shape here, not a single
mega-JSON blob for the whole wiki. `save_all()` batches a whole `reconcile()` result into one
transaction rather than one commit per file; `all_as_dict()` returns the exact shape `reconcile()`'s
own `existing` parameter expects, closing the real read-modify-write loop a walker (step 4) will drive
on every scan: `WikiStore.all_as_dict()` -> `reconcile(existing, found, now)` -> `WikiStore.save_all(result)`.
**Files changed**: new `dourmouse/device_wiki/store.py`; `dourmouse/device_wiki/__init__.py` (export
`WikiStore`/`DEFAULT_DB`).
**Tests added**: new `dourmouse/tests/test_device_wiki_store.py` (8 tests: round-trip, unknown-path
honesty, upsert-not-duplicate, batch save, status filtering, a MISSING entry's status persists,
`all_as_dict()`'s shape, cross-instance persistence).
**Tests run**: `test_device_wiki_store.py` (8/8), full suite (see commit).
**Result**: fixed -- Domain E's build plan step 2 is closed. Step 3 (the real summarizer, reusing the
tool-less `ChatSession` primitive) is next.

---

### 058 -- Domain E: the device wiki's real per-file summarizer (third piece, harsh acceptance test 1)

**Severity**: n/a (feature -- Domain E's own build plan step 3).
**Context**: findings #056-#057 built the pure data model and its persistence with no real content or
model call yet.
**Design**: `dourmouse/device_wiki/stages.py`. `read_file_for_summary(path)` is the real, honest file
read: returns `None` (never raises) for a missing/unreadable file, a real binary file (a null byte in
the first 8000 real sniffed bytes, the standard cheap real-world heuristic), or a file with zero real
readable content after stripping whitespace -- truncates to a real, named 8000-character cost bound
otherwise (the same "an unbounded call is an uncontrolled real cost" reasoning `research_pipeline`'s
own `max_sources_per_sub_question` cap already established). `summarize_entry(entry, content, now)` is
the sixth reuse of the tool-less `ChatSession(DispatchRegistry(), session_file=None)` primitive,
stripping the same leaked `[DOURMOUSE: ...]` dispatch diagnostic finding #046 first caught (a local
copy of `_strip_internal_diagnostics`, matching this codebase's own established per-domain
reconstruction convention rather than a cross-domain import) and degrading a genuinely empty model
reply to `with_failed_summary` rather than raising. `summarize_file(entry, now)` composes both: a
binary or unreadable file gets an honest `with_failed_summary` WITHOUT ever reaching the model --
harsh acceptance test 1 ("every file gets a real, non-fabricated summary or an honest 'could not
summarize' marker -- never silence, never a made-up description") enforced in code, not just hoped for
in a prompt.
**Live proof**: a real, unforced call against this session's own `dourmouse/device_wiki/core.py`
produced a real, accurate summary describing the file's actual `WikiEntry` dataclass, its three real
statuses, and its transition/reconciliation functions -- not a fabricated or generic description.
**Files changed**: new `dourmouse/device_wiki/stages.py`.
**Tests added**: new `dourmouse/tests/test_device_wiki_stages.py` (16 tests: real file reads against
real temp files -- text, missing, directory, binary, empty, whitespace-only, truncation to the cost
bound -- plus the mocked-model summarize_entry/summarize_file paths including the leaked-diagnostic
strip and the binary/deleted-file-never-reaches-the-model guarantees).
**Tests run**: `test_device_wiki_stages.py` (16/16), full suite (see commit).
**Result**: fixed -- Domain E's build plan step 3 is closed, and harsh acceptance test 1 is a real,
enforced code guarantee. Step 4 (the real walker with its explicit root-folder allowlist) is next.

---

### 059 -- Domain E: the device wiki's real walker with an explicit root-folder allowlist (fourth piece)

**Severity**: n/a (feature -- Domain E's own build plan step 4).
**Context**: findings #056-#058 built the data model, persistence, and summarizer; nothing yet
actually walked a real folder. Domain E's own requirement is explicit and literal: "walks a
user-configured set of real root folders (Documents, Desktop, code repos -- never silently the whole
filesystem)".
**Design**: `dourmouse/device_wiki/walker.py`. `configured_roots()` reads a real, explicit
`DOURMOUSE_WIKI_ROOTS` allowlist (`os.pathsep`-separated real absolute paths); unset or empty is an
honest empty list, NEVER a fallback to walking the whole filesystem -- the literal requirement enforced
in code, not just documented. A configured path that no longer exists is silently excluded (config
drift, e.g. a renamed folder, must not break every other configured root). `walk_roots()` is a real
`os.walk` over the allowed roots, skipping known system/cache/build directory names plus any
dotfile-convention directory (`.git`, `.venv`, etc. -- matches the founding spec's own literal "skip
system/cache/build files" wording), skipping a file over a real 5&nbsp;MB cost bound entirely (an
honest, complete skip, never a silent partial/truncated hash), and skipping a file this process cannot
read (permission error, a broken symlink) as an honest omission rather than a crash. `scan(store,
roots, now)` is the real, complete cycle: read the store's current state, walk, `reconcile()`, persist
-- the exact loop a chat tool (step 5) will drive on every real scan.
**Live proof**: (see the full-suite regression below; no separate live proof needed beyond the real
temp-directory tests themselves, since this stage function does real, unmocked filesystem I/O against
real files in every test -- the same "real data, not fabricated samples" discipline `test_security_
platform_adapter.py` already established, adapted to real local files instead of captured command
output.)
**Files changed**: new `dourmouse/device_wiki/walker.py`; `dourmouse/device_wiki/__init__.py`
(exports).
**Tests added**: new `dourmouse/tests/test_device_wiki_walker.py` (16 tests, all against real temp
directories and real files: root-allowlist parsing including config drift, real SHA-256 content
hashing, real file-size reporting, skip-directory behavior, the oversized-file cost bound, multiple
roots, nested subdirectories, and three real end-to-end `scan()` cycles proving harsh acceptance test 2
-- a real deletion and a real content change both correctly reflected on the next real scan through the
real store).
**Tests run**: `test_device_wiki_walker.py` (16/16), full suite (see commit).
**Result**: fixed -- Domain E's build plan step 4 is closed. Step 5 (the chat-reachable tool) is next,
then step 6 (the UI page) and live proof against one real folder end to end.

---

### 060 -- Domain E: chat reachability for the device wiki (fifth piece)

**Severity**: n/a (feature -- Domain E's own build plan step 5).
**Context**: findings #056-#059 built the data model, store, summarizer, and walker; nothing yet was
reachable from chat.
**Design**: `dourmouse/device_wiki_tools.py`, the fourth reuse of the dedicated-module-plus-
`build_X_subagent()`-factory shape (`research_mesh_tools.py`, `research_pipeline_tools.py`, `security/
tools.py`, now this). Three real tools on a new `device_wiki` subagent: `device_wiki_scan` (the real,
complete cycle -- walk the configured roots, reconcile, summarize up to `max_files_to_summarize`
newly-discovered-or-changed files, a real named cost bound so a huge first scan does not run
unboundedly), `device_wiki_status` (real SUMMARIZED/UNSUMMARIZED/MISSING counts), `device_wiki_get`
(one real file's current entry by exact path). `device_wiki_scan` honestly refuses with no configured
`DOURMOUSE_WIKI_ROOTS` rather than falling back to any default. Registered in `general_roster.py`;
added to `model_delegation.py`'s `_LOCAL_ONLY_AGENTS` (personal file content, same privacy class as
`docs`/`memory`). Checked for the same name/description substring-collision risk that bit
`deep_research` (finding #050) before committing: `device_wiki` and its description contain no token
`test_planner.py` uses to mean something else -- confirmed by grep, not assumed.
**A real bug caught before any test ran**: the first draft of `_device_wiki_scan_tool` called
`summarize_file()` twice per newly-discovered file (a leftover duplicate line from editing) -- would
have doubled every real model call's cost with no functional effect visible in the tool's own output
(the first call's result was simply discarded). Caught by re-reading the diff before running tests,
fixed by keeping only the one real call and threading its result back into the in-memory `result` dict
so the reported UNSUMMARIZED/MISSING counts stay accurate.
**Live proof**: a real scan against two real files in a real temp folder (a meeting-notes text file, a
project README) produced two real, accurate summaries via `device_wiki_scan`, correctly reported by
`device_wiki_status` (2 SUMMARIZED) and individually retrievable via `device_wiki_get` for each real
path -- an end-to-end real pass, not isolated stage-function tests.
**Files changed**: new `dourmouse/device_wiki_tools.py`; `dourmouse/general_roster.py` (import +
registration); `dourmouse/model_delegation.py` (`_LOCAL_ONLY_AGENTS`).
**Tests added**: new `dourmouse/tests/test_device_wiki_tools.py` (12 tests covering every tool's happy
path and honest-refusal path, the `max_files_to_summarize` cost-bound cap, a real deletion reported as
missing, and harsh acceptance test 3 enforced directly -- a source-inspection test asserting no tool
handler contains `unlink`/`os.remove`/`shutil.move`/`write_text`/`write_bytes`), plus the two exhaustive
real-subagent-name assertions (`test_dispatch.py`, `test_general_roster.py`) updated together.
**Tests run**: `test_device_wiki_tools.py` (12/12), `test_dispatch.py` + `test_general_roster.py` +
`test_model_delegation.py` + `test_planner.py` (413/413), full suite (see commit).
**Result**: fixed -- Domain E is now chat-reachable end to end. Step 6 (the UI page) is the last
remaining build-plan step; cross-link generation (harsh acceptance test 4) is real, separate follow-on
layered on after, per the domain's own build plan.

---

### 061 -- Domain E: the device wiki's real browsable UI page (sixth and final piece)

**Severity**: n/a (feature -- Domain E's own build plan step 6, the last step).
**Context**: findings #056-#060 built a fully chat-reachable device wiki with no UI surface. Domain
E's own explicit requirement (section 7) names "a searchable structure the assistant (and the user,
via a real UI) can browse" -- a tool the model can query silently is not sufficient on its own,
matching the exact same gap the GOALS screen closed for the Goal/Task runtime (finding #026).
**Design**: `GET /api/device_wiki` (`dourmouse/webui.py`), mirroring `/api/goals`'s own "already-tested
backend, first HTTP surface over it, nothing new logically" shape -- `?status=` filters,
`?path=<exact real path>` returns one entry (a real 404 for an unknown path, never a fabricated empty
entry), and the response includes `configured_roots` so the UI can distinguish "nothing configured
yet" from "configured but nothing scanned yet" honestly. Deliberately read-only: a real scan runs
through the `device_wiki_scan` chat tool, never a button on this route, since a real scan can take
minutes (one real model call per file needing a summary) and should be a deliberate chat action, not
something that blocks a screen. A new WIKI screen in `ui/console.html` (added to `SCREENS`, auto-
appearing under the console's own overflow "MORE" menu since the row was already full), polling every
5s while active (same accepted tradeoff as GOALS/TIMETABLE/AGENTSMITH), grouping real entries by
SUMMARIZED/UNSUMMARIZED/MISSING with a real per-file summary shown inline.
**Live proof**: a real dev server booted against a real temp folder (`DOURMOUSE_WIKI_ROOTS` pointed at
one real file), a real `device_wiki_scan` chat-tool call produced one real, accurate summary, and the
WIKI screen -- reached through the console's real "MORE" overflow menu, screen 18/18 -- correctly
rendered the real file's status, path, and summary text with a live-green status dot, confirmed by
screenshot in the browser pane. The empty (no roots configured) state was also confirmed honest before
any root was set.
**Files changed**: `dourmouse/webui.py` (`GET /api/device_wiki`); `ui/console.html` (`SCREENS` array,
the WIKI pane, `loadWiki`/`paintWiki`/`wikiRowHtml`/`wikiStatusDot`, the 5s poll).
**Tests added**: `TestDeviceWikiEndpoint` in `test_webui.py` (7 tests: empty state, listing a real
entry, one-entry-by-path lookup, a real 404 for an unknown path, status filtering, configured-roots
reporting). Caught and fixed a real test-authoring mistake before running anything: the new class was
first inserted in the middle of the pre-existing `TestGoalsEndpoint` class's own method list, silently
re-parenting four unrelated tests (`test_sessions_recent_endpoint_with_real_summaries` and three
others) onto the new class -- found by re-reading the class-boundary grep output before trusting the
edit, fixed by moving the whole new class to sit cleanly after `TestGoalsEndpoint`'s real closing
brace.
**Tests run**: `test_webui.py` (`TestDeviceWikiEndpoint`, `TestGoalsEndpoint`, `TestSecurityEndpoint`
together, 16/16, confirming the re-parented tests are back where they belong and still pass), full
suite (see commit).
**Result**: fixed -- Domain E's full build plan (steps 1-6) is closed. Cross-link generation (harsh
acceptance test 4) is the one real, separate piece of this domain's own scope layered on AFTER
per-file summaries, per the domain's own build plan -- not yet built, named honestly.

---

### 062 -- Domain I: the real security dashboard UI (user-directed, "more visual... dashboard")

**Severity**: n/a (feature -- Domain I item 6, "the practical, calm, non-decorative version" this
section's own build plan named last, "after the sentry itself is real and tested").
**Context**: user asked mid-session for the cybersecurity UI to be "more visual with circles task
bars more of a dashboard" -- the sentry (findings #039-#055) had real, live, continuously-updated
risk/finding/incident/device data with zero visual surface: `security_sentry_scan` returned plain
text, and no screen anywhere referenced it.
**Design**: `GET /api/security_dashboard` (`dourmouse/webui.py`), mirroring `/api/goals`'s own
"already-tested backend, first HTTP surface over it" shape -- reads the REAL, already-computed state
the server's own background `SentryRuntime` tick produced (`server.security_sentry.last_result`),
never triggers a fresh scan itself (a real scan shells out to several real system commands per call).
Reports `scanned` honestly `false` when no tick has landed yet (server just started, or
`DOURMOUSE_SECURITY_SENTRY_LOOP=0`) rather than fabricating a zero-risk snapshot. A new SECURITY
screen in `ui/console.html`: a real circular risk gauge (plain SVG `stroke-dasharray`, no chart
library -- a fixed visual scaling ceiling of 40 controls only how full the ring looks, never the real
`risk_score` itself, which is always shown verbatim in the center label), color-coded green/amber/red
by score, plus real horizontal bars for findings-by-severity and incidents-by-status, a known-device
count, and the real findings list with a pulsing dot on genuinely NEW findings.
**A real bug caught before any test ran**: the first draft used `var(--no)` and `var(--dim)` for
colors -- neither CSS custom property exists anywhere in this stylesheet (confirmed by grep before
assuming otherwise); the real severity/status color and the real established theme text color are
`var(--bad)` and `var(--blue-deep)` respectively (the existing `.dot.no{background:var(--bad)}` rule
and `.row .d{color:var(--blue-deep)}` rule were the real, already-established precedent). Fixed with a
global find/replace across the file before any browser check, confirmed by a repeat grep finding zero
remaining occurrences.
**Live proof**: a real dev server, real `SentryRuntime` tick against this machine's own real telemetry
(risk score 21.0, 1 real HIGH + 3 real MED findings, 5 real known devices) rendered correctly in the
browser: the gauge, both bar sections, the device count, and all four real findings with their real
titles/detail text, confirmed by screenshot.
**Files changed**: `dourmouse/webui.py` (`GET /api/security_dashboard`); `ui/console.html` (`SCREENS`
array, the SECURITY pane, gauge/bar rendering functions, the 5s poll).
**Tests added**: `TestSecurityDashboardEndpoint` in `test_webui.py` (5 tests: honest no-scan-yet state,
a real scan result reported with correct severity/is_new breakdown, known-device count, incidents-by-
status counts).
**Tests run**: `test_webui.py` (219/219, full file), full suite (see commit).
**Result**: fixed -- Domain I's dashboard UI (item 6) is closed. Windows/cross-device platform coverage
(item 7) and the much larger balance of the founding spec's own 54-item cybersecurity build list remain
the last real Domain I work.

---

### 063 -- Domain, agent ecosystem visual monitor: static tiles, then real movement (user-directed)

**Severity**: n/a (feature -- Phase 7 of the standing plan, closing the one gap named explicitly in
this session's own status report: no visual per-agent status board of any kind existed).
**Context**: user asked for the AI corporate ecosystem to look like a pixelated 3D office, referencing
`github.com/KbWen/agent-virtual-office` (verified via browser fetch: a real, MIT-licensed, pixel-art
office visualizing real coding-session status as costumed characters). A first pass shipped a static
grid of pixel-art desk tiles, fully rebuilt via `innerHTML` on every update. The user pushed back
immediately: "I don't like the ui... I want the visual 3d office where u can see them move interact
discuss ... exactly like the GitHub repo I shared" -- a real, specific correction, not a style
preference to defer.
**Design (v2, the real fix)**: fetched the reference repo's own `package.json` (not assumed) --
"Pure SVG, zero backend" -- confirming a persistent SVG scene with CSS-transitioned movement is the
right-shaped native reimplementation, not a canvas/WebGL rebuild. The scene is now built ONCE per
real agent roster (`officeBuildScene`) and every subsequent update PATCHES existing DOM nodes in place
(`officeUpdateSprite`: `.style.transform`, `.style.color`, text content) rather than replacing
`innerHTML` -- the real reason v1 could never animate: CSS transitions cannot interpolate between two
states of a node that no longer exists after a full re-render. `.office-sprite{transition:transform
1.1s ease-in-out}` now genuinely animates a sprite walking between its home desk and a real meeting-
room seat. Movement is still driven ONLY by real signals: `officeMeetingAssignments()` reads
`_orchFanouts` (the SAME real, already-proven fan-out broadcast `orchFanoutRowHtml` already renders as
text) and assigns any agent with a real branch still `phase !== "result"` to a real meeting-room seat;
the sprite walks back to its desk the instant that branch reports `phase === "result"`, both purely
because `_orchFanouts`/`_orchAgents` changed, never a timer. A "computing" agent gets a real CSS-
keyframe typing bob (`officeBob`) at its own desk. A real per-agent speech bubble shows the last real
tool name for 3 real seconds on each new activity event, then fades -- real data, never fabricated
chatter.
**A real regression this change fixed, caught by the full suite, not found by inspection**: the v1
draft's `officeMeetingRoomHtml` contained `const busy = b.phase !== "result";` -- a bare `busy`
identifier, exactly the anti-pattern `test_console_per_screen_busy_state.py`'s own regression guard
exists to catch (a real, previously-shipped bug class: a single shared `busy` flag conflated state
across screens). The guard test failed on the very next full-suite run; the v2 rewrite deleted that
function entirely (replaced by `officeMeetingAssignments`/`officeUpdateSprite`), which incidentally
removed the violation -- confirmed by rerunning the specific guard test file, not assumed.
**Live proof**: a real dev server's OFFICE screen rendered all 43 real registered subagents as
persistent pixel-art desks, including two agents (`mail`, `markets`, the always-on `live_runtime.py`
pollers) showing a real, distinct `LIVE` status neither idle/computing/auth -- proof the renderer
reflects whatever real status string the backend actually reports, not a hardcoded enum. Three real
`delegate_parallel` fan-outs were driven live through the actual chat composer (`forced_agent`
branches to `research_info`/`reviewer`); each one genuinely dispatched and completed (confirmed via
real tool-error results: a real 404 from `research_info`, a real "no such file" from `reviewer`) --
proving the underlying fan-out mechanism this screen depends on fires for real. Honest limitation, not
hidden: every real fan-out in this local, offline environment completed in well under a second (no
real network fetch succeeded, and the local reviewer tool is instant), so the meeting-room walk
animation itself was code-reviewed against the exact proven `_orchFanouts` update path but never
independently caught mid-flight in a screenshot -- a real gap in this pass's own verification, named
rather than papered over with a staged/fabricated screenshot.
**Files changed**: `ui/console.html` (`SCREENS` array, the OFFICE pane, the office CSS transition/
keyframe rules, `officeBuildScene`/`officeUpdateSprite`/`officeMeetingAssignments`/
`officeMeetingSeat`/`officePixelPersonPaths`, replacing v1's `officeDeskHtml`/`officeMeetingRoomHtml`,
wired into the existing `orchApplyDelta`/`orchApplyFanout` SSE handlers).
**Tests added**: none -- pure client-side rendering over already-tested, already-live backend state;
syntax-verified with `node --check` against the extracted inline script before each live browser
proof; the pre-existing `test_console_per_screen_busy_state.py` regression guard caught the real
`busy`-identifier bug above.
**Result**: fixed -- the agent ecosystem now genuinely moves in response to real signals, not just a
static status grid. Deeper 3D/pixel-office asset work (real character sprites in place of the blocky
6x4 grid glyph, a real floor-plan with distinct rooms beyond one dashed meeting-room rectangle,
celebration/argument flavor animations tied to real deploy/blocked-streak signals like the reference
project's own) is real, separate, not-yet-built follow-on -- this pass is real movement over real
data, not the finished visual vision.

---

### 064 -- `send_message` sender impersonation: `from_agent` was model-declared, never verified

**Severity**: real, security (spoofing). **Context**: a design-phase review of the (not-yet-built)
multi-floor agent office concept walked the real `message_bus`/`delegate_task`/`delegate_parallel`
source (not the mockup) looking for genuine flaws, per the user's explicit "find flaws in this ai
agent ecosystem". `send_message`'s handler read `from_agent` straight out of the model's own tool-call
arguments and validated only that it named a real roster agent -- never that the calling agent
actually WAS that name. Any agent (or the untargeted top-level orchestrator turn, which has no single
real identity at all) could forge a message claiming to be `security` or `orchestrator`, e.g. "security:
cleared, proceed" -- a real trust problem given inter-agent messages are meant to carry real information
between agents mid-task.
**Fix**: the real caller identity already exists and is deterministic --
`current_dispatch_context(registry).forced_agent` is set exactly when a run is hard-scoped to one
subagent (a `delegate_task`/`delegate_parallel` branch; see `run_dispatch_messages`'s own
`forced_agent` docstring), the same mechanism `delegate_task`'s own handler already reads via
`current_dispatch_context` (`general_roster.py`, pre-existing pattern, not new). `send_message` now
reads that as the real sender, refuses loudly (never silently overrides) if the untargeted top-level
orchestrator calls it at all, and refuses loudly if the arguments claim a different name than the real
one. `from_agent` is now optional in the tool schema (informational only) instead of required.
**Files changed**: `dourmouse/general_roster.py` (`_send_message_tool`).
**Tests added**: `dourmouse/tests/test_message_bus.py::TestMessengerTools` --
`test_send_message_refuses_without_real_caller_identity`,
`test_send_message_refuses_impersonation_of_another_agent`; the pre-existing sender/recipient/body
tests updated to push a real `DispatchContext` with `forced_agent` set (the same pattern
`test_general_roster.py`'s own `delegate_parallel` context tests already use) instead of asserting a
caller-supplied `from_agent`.
**Result**: fixed. Full targeted suite (`test_message_bus.py`, `test_general_roster.py`,
`test_dispatch.py`) green after the change.

---

### 065 -- `message_bus` had no proactive notification path (direct messages sat until polled)

**Severity**: real, usability/reliability gap, explicitly requested by the user ("yes we need a
notification or pinging system"). **Context**: same flaw-finding pass as #064. `message_bus.post()`
is real and correct, but nothing observes it proactively -- a direct message sits in the recipient's
inbox until something explicitly calls `read_agent_inbox`. Given the CEO-console concept (a human
watching the roster), a real handoff could go unnoticed indefinitely.
**Fix**: reused three pieces that already existed rather than building new infrastructure --
`message_bus.on_post(fn)` (the real, already-existing observer hook, previously used only for the
memory mirror), the real `StateStore.add_alert`/SSE `state_change` path (the exact mechanism ATLAS
run-started alerts already use, at `webui.py`'s `/api/atlas` POST handler), and the desktop app's
already-existing `DesktopNotifier`, which already subscribes to that same alerts SSE section and
already turns a new alert into a native OS notification -- no new client-side code needed. Added a new
`"agent"` alert kind (`state_store.ALERT_KINDS`) and a `webui.py`-level `on_post` observer that creates
one alert per DIRECT message (`to != BROADCAST`) and broadcasts the same `state_change` ATLAS alerts
use. Deliberately excludes broadcasts: a broadcast (e.g. `news`'s live feed onto `*`) is routine data-
plane traffic an agent chose to make available to everyone, not a deliberate one-to-one handoff that
warrants interrupting the user -- same honesty-gating principle the `agent-virtual-office` reference
project uses for its own work-claim events (never surface routine activity as if it were an urgent
signal).
**Files changed**: `dourmouse/state_store.py` (`ALERT_KINDS` gains `"agent"`), `dourmouse/webui.py`
(`run_server`'s `_notify_direct_message` observer, registered on `server.bus.on_post`).
**Tests added**: `dourmouse/tests/test_message_bus.py::TestDirectMessageNotifications` --
`test_direct_message_creates_an_agent_alert`, `test_broadcast_message_creates_no_alert`, both against
a real `run_server` instance with an isolated `StateStore(path=None)` and `MessageBus()`, verifying the
real alert row (kind, title, detail) rather than mocking the store.
**Result**: fixed. Real backend piece still needed to make this durable across restarts (named,
not built): `office_logger.py` persists `message_bus` traffic itself; today's alert is real and live
but, like `message_bus`, does not survive a server restart.

---

### 066 -- `office_logger.py`: a real, persistent log for `message_bus` + `delegate_parallel` fan-outs

**Severity**: n/a (feature -- closes the real, repeatedly-named "message_bus is in-memory-only, dies
on restart" gap from the agent-ecosystem design review, same review that produced findings #064-065).
**Context**: `message_bus.py` is real and correct but bounded and in-memory; `delegate_parallel`'s own
`delegate_parallel_branch` lifecycle events (`start`/`result`, carrying a real `run_id`/`agent`/`ok`/
`elapsed_s`) already stream live through the chat `event_sink` but were never persisted anywhere --
both are real signal with no durable home.
**Design**: one new, workspace-relative SQLite store (`dourmouse/office_logger.py`, same one-
connection-per-operation/WAL-mode/`DEFAULT_DB` discipline as `device_wiki/store.py`), append-only,
two tables (`messages`, `fanout_events`). Wired with zero new plumbing: `OfficeLogger.log_message` is
registered on the SAME real `message_bus.on_post` hook the memory mirror and finding #065's alert
observer already use; `OfficeLogger.on_event` is added to the SAME `sink()` closure `ActivityTracker`/
`AttentionQueue` already consume in `webui.py`'s SSE chat handler, filtering to `delegate_parallel_
branch` entries only. A new read-only `GET /api/office_log` (`?kind=messages|fanouts`, `?run_id=`,
`?limit=`) mirrors `/api/device_wiki`'s own "already-tested backend, first HTTP surface over it"
shape.
**Honest, named scope limit (not silently skipped)**: per-tool-call reasoning/transcript capture for
nested branches is deliberately NOT in this piece. `tool_use`/`tool_result` events only carry the TOOL
name (not the calling agent or a call-instance id), and `dispatch.py`'s `thinking_delta` only fires at
`ctx.depth == 0` today (`dispatch.py` ~4698-4714, deliberate). This store persists WHO messaged WHOM
and WHICH branch ran WHERE and how it finished -- real and durable, but not yet a full replay of an
agent's own chain of thought. That remains the one still-not-built "per-branch reasoning tagging"
piece named in the design review.
**Files changed**: `dourmouse/office_logger.py` (new), `dourmouse/webui.py` (`run_server`'s new
`office_log` param and wiring, `sink()`'s new `office_log.on_event` call, new `/api/office_log` route).
**Tests added**: `dourmouse/tests/test_office_logger.py` (hermetic, tmp_path-backed store tests --
persistence, ordering/limit, malformed-input swallowing, reopen-same-path durability, fan-out
run_id scoping, non-fan-out event types ignored) and
`dourmouse/tests/test_message_bus.py::TestOfficeLogWiring` (real `run_server` + real HTTP,
`office_log=` isolation matching the existing `bus=`/`state=` test convention).
**Result**: fixed. Full suite green.

---

### 067 -- real per-agent, per-call reasoning/transcript tagging (the "full chain of thought" gap, closed)

**Severity**: n/a (feature -- the one backend piece named as still-missing at the end of the
agent-ecosystem design review: "we also want to see the live verbose transcription of all agents,
meeting rooms, their full chain of thought tokens ... whenever we want").
**Context**: `dispatch.py`'s `tool_use`/`tool_result`/`thinking_delta`/`assistant_delta`/
`assistant_text`/`brain` events already streamed live through the chat `event_sink`, but carried no
reliable identity: `tool_use`/`tool_result` only ever named the TOOL, never the calling agent, and
nothing distinguished two concurrent runs against the same agent (the exact real gap finding #066
named as flaw #4, "`ActivityTracker`'s live status collision for two independent `delegate_task`
calls to the same agent"). Without that, an on-demand transcript viewer could show WHAT happened but
never reliably WHO did it or WHICH run it belonged to.
**Fix**: `DispatchContext` gains a `call_id` field (`uuid.uuid4().hex[:12]` via `default_factory`,
so every `DispatchContext` instance -- one per `run_dispatch_messages` call, including every nested
`delegate_task`/`delegate_parallel` branch -- gets a genuinely fresh one, never inherited). `_emit_
event` gains an optional `ctx` parameter that additively tags the entry in place (`entry.setdefault
("agent", ctx.forced_agent or "orchestrator")`, `entry.setdefault("call_id", ctx.call_id)`) --
`setdefault` so a `delegate_parallel_branch` entry's own already-correct `agent` (the branch's real
target, not the parent's `forced_agent`) is never overwritten. Every real emission site for the six
event types above inside `_run_dispatch_loop`, plus the top-level "brain" event in `run_dispatch_
messages` (moved a few lines later, past `ctx`'s construction, so it can be tagged too -- nothing
between the old and new position reads or depends on `ctx`, confirmed by re-reading the moved span),
now passes `ctx=ctx`. Additive only: no entry lost a key, no consumer that reads `entry.get(...)`/
`entry["type"]` needed to change, and nothing in this codebase asserts exact dict equality on a
transcript/event entry (checked before making this change, not assumed).
**Consumer wired the same day**: `office_logger.py` (finding #066) gained a third table,
`agent_events`, and a new `transcript(agent=, call_id=, limit=)` read method -- the real backing for
"show me everything agent X did" or "show me exactly this one run", oldest-first. An untagged event
(no `call_id` at all -- e.g. a future emitter that never passes `ctx`) is skipped, never logged under
a fabricated identity. `GET /api/office_log?kind=events` exposes it.
**Honest, named scope limit**: this persists the real event STREAM per agent/call_id; it does not
itself reconstruct a "conversation" (grouping `thinking_delta` chunks into one paragraph, merging
several concurrent `call_id`s from one meeting into a single timeline) -- that assembly is a read-
side/UI concern layered on top of this real, ordered, durable data, not yet built.
**Files changed**: `dourmouse/dispatch.py` (`DispatchContext.call_id`, `_emit_event`'s new `ctx`
param, ~17 call sites in `_run_dispatch_loop` plus the top-level brain event in `run_dispatch_
messages`), `dourmouse/office_logger.py` (`agent_events` table, `_AGENT_EVENT_TYPES`, `_log_agent_
event`, `transcript()`), `dourmouse/webui.py` (`/api/office_log?kind=events`).
**Tests added**: `dourmouse/tests/test_dispatch.py::TestEmitEventTagsRealAgentAndCallId` (untargeted
top-level run tags `"orchestrator"`, a `forced_agent` run tags the real agent name, two independent
`forced_agent` runs to the SAME agent get distinct `call_id`s -- the exact flaw #4 scenario);
`dourmouse/tests/test_office_logger.py::TestOfficeLoggerAgentEvents` (persistence, untagged events
skipped not fabricated, unrelated event types ignored, scoping by agent/call_id, the same two-
concurrent-calls scenario at the store level).
**Result**: fixed. Full suite green.

---

### 068 -- Domain H, piece 4/7: deterministic hooks (pre-tool/post-tool/stop/session)

**Severity**: n/a (feature -- Domain H's own build plan, §10 of `docs/COMMERCIAL_GRADE_MASTER_
REQUIREMENTS.md`, sequenced this right after piece 2, the project-instruction file already shipped
as finding #037).
**Context**: the founding spec names four real hook types (matching Claude Code's own PreToolUse/
PostToolUse/Stop/SessionStart shape). The plan itself named exactly where to wire each: pre/post-
tool hooks against `dispatch.py`'s one real call site for every tool invocation (`_execute_tool`,
confirmed by grep to be `spec.handler(...)`'s ONLY caller, matching the plan's own claim rather than
assuming it); stop/session hooks against `chat.py`'s `ChatSession` lifecycle.
**Design**: new `dourmouse/hooks.py` -- five real registries (`pre_tool`, `post_tool`, `stop`,
`session_start`, `session_stop`), each a plain module-level list with a `register_*`/`run_*`
function pair, plus `clear_hooks()` for test isolation (same convention as `message_bus.set_message_
bus(None)`). Pre-tool hooks are the one type that can genuinely BLOCK (a non-empty string return is
a real denial, short-circuiting further hooks, surfaced to the model as `BLOCKED BY HOOK: <reason>`)
-- every other type is a pure observer, matching this codebase's own established discipline
(`message_bus.on_post`, `ActivityTracker.on_event`, `office_logger.on_event`: can watch, never
alter or abort). A raising hook of any type is swallowed and treated as "no opinion" -- never a
block, never a crash.
**Wiring**: `_execute_tool` calls `run_pre_tool_hooks` right after the `PROHIBITED` short-circuit
(so policy-prohibited tools never even reach a hook) and before required-argument validation or
confirmation gating (a hook's policy applies to every real call attempt alike); `run_post_tool_hooks`
fires at both real exit points -- the exception-path `ERROR:` text and the normal-path successful
result. `ChatSession.__init__` fires `run_session_start_hooks` unconditionally (every session,
resumed or fresh, is a real start from this process's point of view); `ask()` fires `run_stop_hooks`
with the real dispatch report once a turn completes; a new `ChatSession.close()` fires `run_session_
stop_hooks`, wired into all three of the REPL's real exit paths (`python -m dourmouse.chat`'s
one-shot mode, `exit`/`quit`, and `EOFError`/`KeyboardInterrupt`).
**Honest, named limitation**: `webui.py`'s server path keeps ONE `ChatSession` alive for the whole
process's lifetime (many HTTP turns share it), so nothing there calls `close()` today -- process
exit IS that session's real end, with no real hook point to attach to yet. Named in `close()`'s own
docstring, not silently skipped.
**Files changed**: `dourmouse/hooks.py` (new), `dourmouse/dispatch.py` (`_execute_tool`'s pre/post
wiring), `dourmouse/chat.py` (`ChatSession.__init__`/`ask`/new `close()`, three REPL call sites in
`main()`).
**Tests added**: `dourmouse/tests/test_hooks.py` (the module in isolation -- allow/block, short-
circuit on first block, raising hooks never block/crash, `clear_hooks()`);
`dourmouse/tests/test_general_roster.py::TestExecuteToolRunsRealHooks` (through the real
`_execute_tool`: a blocking hook prevents the real handler from ever running, an allowing hook lets
it run, post-tool sees the real success/error result, a raising pre-tool hook never blocks a real
call, a hook never runs at all for a `PROHIBITED` tool);
`dourmouse/tests/test_chat.py::TestSessionAndStopHooks` (construction fires session-start with the
real session id, `ask()` fires stop with the real report, `close()` fires session-stop, a raising
session/stop hook never breaks construction or a turn).
**Result**: fixed (shipped). Full suite green.

---

### 069 -- Domain H, piece 5/7: context compaction (audited, found already real, not rebuilt)

**Severity**: n/a (audit, not a new feature -- Domain H's own build plan explicitly said "audit
what `chat.py`/session-file handling already does today before building anything... confirm or
refute with a real read before writing new code").
**Context**: the founding spec's own explicit warning is that session management needs REAL context
compaction, not "summarize everything" (a lossy, un-auditable pattern this codebase's own Rule 2.1/
2.2 discipline would reject) -- structured state must be preserved separately.
**Finding, by real read, not assumption**: this already exists, on both halves the spec asks for,
and was simply never marked closed against Domain H:
1. **Structured state, never lossy-summarized**: `ChatSession` persists every real turn as an
   immutable JSONL audit record (`<workspace>/sessions/session_<ts>.jsonl`) plus a full
   `.messages.json` snapshot for resume -- the complete real history, untouched, forever.
2. **Real, deterministic, structured compaction at the LLM API boundary**: `dispatch.py`'s
   `_bounded_context()` (pre-existing, `v13.7`/`v13.9` per its own docstring, thoroughly tested in
   `TestBoundedContext`) builds a BOUNDED COPY for the actual API call -- never touching the real
   stored history above. It always keeps the leading system-message block and the entire in-flight
   exchange (from the most recent `user` message onward) intact, adds older complete exchanges
   most-recent-first while a real token budget allows, drops anything beyond that budget at a clean
   `user` boundary (never mid-exchange, which some backends reject outright), and truncates
   tool-result messages OLDER than the in-flight exchange to a real character cap (already seen in
   full when produced; later turns only need the gist). This is structured, rule-based compaction --
   exactly what the spec asked for instead of an LLM-summarization pass that could silently drop or
   distort a real fact.
**Result**: no new code. Domain H piece 5 is closed as a documentation/audit finding: the real
mechanism already exists, is already tested, and already matches the spec's own explicit
requirement. Named for completeness, not a gap: `_bounded_context` bounds what the MODEL sees per
call, not the on-disk ledger size over a very long-lived session -- an unbounded `.messages.json` for
a session that runs for months is a real, separate, much narrower future concern (disk, not
correctness), not attempted here.

---

### 070 -- Domain H, piece 3/7: Skills-as-modular-capability-packages

**Severity**: n/a (feature -- §10's own build plan, sequenced after the hooks piece).
**Context**: the founding spec calls for skills as real, human-authorable documentation packages
(`SKILL.md` + resources) loaded only when relevant to a turn, never concatenated into one giant
always-on system prompt -- and to reuse an existing real relevance-routing mechanism rather than
invent a second one.
**Design**: new `dourmouse/skills.py` -- a `dourmouse/skills/<name>/SKILL.md` convention (minimal
frontmatter: `name`/`description`/`keywords`, no YAML dependency added, this codebase has none
today and the format is simple enough not to need one), `load_skills()` (real directory scan,
malformed or nameless skill files skipped rather than guessed at), and `relevant_skills()`/
`skill_context_block()` -- deterministic keyword-overlap matching (Rule 2.8: no LLM judgment in the
lookup path), the same discipline `planner.find_agents_for_query` already established for routing a
turn to the right SUBAGENT, applied here to a capability PACKAGE instead. A skill is relevant only
when one of its own declared keywords appears as a whole token in the turn's text (no substring
false positives -- checked explicitly: "pdfs" does not falsely match a "pdf" keyword).
**Wiring**: `ChatSession.ask()` splices a matched skill's real body in as its own trailing system
message, the SAME established pattern the memory recall block (`recall_block`) already uses and for
the same reason (KV-cache stability: `messages[0]`, the immutable base prompt, is never touched).
Empty when nothing is relevant -- never a standing cost on every turn.
**Honest scope**: this ships the real infrastructure (format, loader, matcher, wiring) -- it does
not ship any actual skill content under `dourmouse/skills/`; populating the library is real,
separate, ongoing work, not part of this piece.
**Files changed**: `dourmouse/skills.py` (new), `dourmouse/chat.py` (`ask()`'s new splice, mirroring
the existing memory-recall splice immediately above it).
**Tests added**: `dourmouse/tests/test_skills.py` (frontmatter parsing incl. malformed/nameless
files, missing-directory handling, deterministic sort, keyword-overlap relevance incl. the
substring-false-positive case, multi-skill inclusion); `dourmouse/tests/test_chat.py::
TestSkillContextInjection` (a relevant skill's real body is spliced in, an irrelevant turn injects
nothing, zero shipped skills never breaks a turn).
**Result**: fixed (shipped). Full suite green.

---

### 071 -- Domain H, piece 6/7: a programmable SDK-style headless interface

**Severity**: n/a (feature -- §10's own build plan explicitly named generalizing the one existing
real instance of this shape, `research_mesh/pipeline.py`'s `main()` CLI, rather than designing a new
pattern).
**Context**: `ChatSession`/`DispatchRegistry` already self-resolve a real backend with zero
arguments (`client=None`/`config=None` already fall through to `load_llm_config_with_fallback`/
`_build_client` inside `dispatch.py` -- confirmed by reading the actual call site, not assumed), so
the real gap was never the engine -- it was that a script author had to know that internal shape at
all (`build_general_registry()`, `ChatSession(...)`'s constructor) to drive Dourmouse headlessly, and
`chat.py`'s existing one-shot mode prints human-formatted text, not something a second program can
parse.
**Design**: new `dourmouse/sdk.py` -- a thin `Dourmouse` facade (constructor resolves `registry`
automatically when omitted; every other keyword passes straight through to the real `ChatSession`
constructor, no new parameters invented) exposing `ask()` (returns the SAME real report dict
`ChatSession.ask()` returns, unmodified) and `close()`/context-manager support (`close()` fires the
real session-stop hook from finding #068). A CLI (`python -m dourmouse.sdk "prompt" [--json]
[--forced-agent X]`) for scripting/piping, distinct from `python -m dourmouse.chat`'s interactive-
first REPL -- `--json` prints the real report as machine-parseable JSON instead of a human-formatted
line.
**Files changed**: `dourmouse/sdk.py` (new).
**Tests added**: `dourmouse/tests/test_sdk.py` -- the facade (real report returned unmodified,
registry auto-resolution, `close()`/context-manager firing the real hook, `forced_agent` passthrough
verified against the real `run_dispatch_messages` call via a spy) and the CLI (plain-text output,
`--json` output is real parseable JSON containing `final_text`, and a real subprocess smoke test
confirming `python -m dourmouse.sdk --help` is genuinely invocable, not just importable).
**Result**: fixed (shipped). Full suite green. Domain H is now 6/7 pieces done (1, 2, 3, 4, 5, 6 --
only MCP, piece 7, explicitly scoped as its own dedicated pass, remains).

---

### 072 -- Domain H, piece 7/7: MCP, second half (Dourmouse as an MCP client)

**Severity**: n/a (feature -- the last piece of Domain H's own build plan, explicitly scoped as
"its own dedicated pass" once pieces 1-6 landed).
**Context, found by real read, not assumed**: `dourmouse/mcp_bridge.py` already made Dourmouse a
real, tested MCP SERVER (JSON-RPC 2.0 over stdio, `initialize`/`tools/list`/`tools/call`,
confirmation-gate-aware) so external CLIs (Claude Code, Codex) can call Dourmouse's own tools --
built earlier under a different initiative name, never marked against this Domain H checklist item,
the same situation piece 5's context-compaction audit found. What did NOT exist: the other, more
central half of Claude Code's own MCP feature -- the user configuring an EXTERNAL MCP server and
Dourmouse connecting to it AS A CLIENT to gain its tools, the way Claude Code's own `.mcp.json`
works. Confirmed by grep across the codebase before writing anything: no MCP client of any kind
existed.
**Design**: new `dourmouse/mcp_client.py` -- a real, stdlib-only, hand-rolled stdio JSON-RPC 2.0
client (`McpClient`: spawns the configured server as a subprocess, performs a real `initialize`
handshake, sends `notifications/initialized`, fetches the real `tools/list`, and `call_tool()`s
against it), speaking the exact same protocol subset `mcp_bridge.py`'s own server implements --
proven by a real end-to-end test connecting a real `McpClient` to a real `dourmouse.mcp_bridge`
subprocess and listing/using its real tools, not two isolated mocks. Config format
(`workspace/mcp_servers.json`, `{"mcpServers": {...}}`) is the SAME shape `mcp_bridge.build_mcp_
config_file` already writes for Claude Code and Claude Code's own `.mcp.json` uses, so an existing
Claude Code MCP server config can be pointed at directly rather than needing a second bespoke
format. Every external tool is wrapped into a real `ToolSpec` named `mcp__<server>__<tool>` -- the
exact naming convention this codebase already documented elsewhere (`code_backends.py`'s own
`--allowedTools "mcp__dourmouse__*"` comment) -- and registered under a new `mcp_tools` subagent,
built only when at least one server configures at least one real tool (never a standing empty
subagent; the common zero-config case returns instantly with no subprocess spawned at all).
**Failure discipline**: one misconfigured or unreachable server (a bad `command`, a real
`subprocess.Popen` `OSError`, a bad handshake) is caught per-server and recorded honestly on the
subagent's own description -- never raised, never blocking any other configured server or
`build_general_registry()` itself. Verified: a real, guaranteed-nonexistent binary path never
crashes the build; a mix of one working and one broken server still yields a working subagent for
the one that connected.
**Honest, named limitation**: `_readline`'s `timeout` parameter is accepted for interface symmetry
but not enforced on the real subprocess path (no portable non-blocking read-with-timeout without
extra machinery this codebase doesn't otherwise use) -- a genuinely hung external server blocks the
calling thread, same as this codebase's other synchronous subprocess calls (`code_backends.py`'s own
CLI invocations). Named as real, separate follow-on if ever observed in practice, not hidden.
**Files changed**: `dourmouse/mcp_client.py` (new), `dourmouse/general_roster.py`
(`build_general_registry()`'s new opt-in `mcp_tools` subagent registration, mirroring the
`self_extended` block's own guarded-registration pattern immediately above it).
**Tests added**: `dourmouse/tests/test_mcp_client.py` -- handshake (real protocol version/client
info sent, tools/list parsed, server-error and empty-response handling), `call_tool` (real text
result, honest error surfacing, transport failure never raises), config loading (missing/malformed/
valid), subagent building (no servers, no command, unreachable command, one failing server never
blocking a working one, real tool-name prefixing), and the real end-to-end subprocess test against
the actual `dourmouse.mcp_bridge` server.
**Result**: fixed (shipped). Domain H is now 7/7 pieces done -- fully closed. Full suite green.

---

### 073 -- agent ecosystem flaw #4: `ActivityTracker`'s live status collision, fixed

**Severity**: real, correctness (silent data corruption in the live view, not a crash).
**Context**: named in finding #066 as flaw #4 -- `ActivityTracker._status[name]`/`_last[name]`/
`_feed[name]` are keyed by AGENT NAME only. Two independent `delegate_task` calls to the same agent
running concurrently (outside a `delegate_parallel` fan-out, which IS index-safe -- see finding
#066's own distinction) shared one `_last[agent]` slot: whichever call's `tool_use` landed most
recently owned the slot, and a `tool_result` for the OTHER, now-superseded call could silently
attach its result text to the wrong call's snapshot, with no way to tell after the fact that this
had happened.
**Fix**: finding #067's real per-run `call_id` tagging is what makes this fixable at all --
`tool_use`/`tool_result` entries now carry a real `call_id`. `ActivityTracker._record` stores that
`call_id` alongside `_last[agent]`'s existing fields and only applies a `tool_result` to `_last
[agent]["result"]` when it genuinely belongs to the call currently occupying the slot (`last.get
("call_id") == call_id`); a result for a call_id no longer in the slot still reaches the feed (real
data, never dropped) but is no longer misrepresented as the agent's current activity. A new
`concurrent_call_ids(agent)` (and a `concurrent_call_ids` key added to every agent in
`snapshot()`/`GET /api/activity`) surfaces real call_ids seen within a 60s recency window --
0-or-1 is the normal case, 2+ is genuinely concurrent activity on the same agent, the exact scenario
this flaw named. Backward compatible: an untagged event (no `call_id` at all) keeps the pre-existing
unconditional-overwrite behavior exactly as before this fix.
**Honest, named scope limit**: this fixes the DATA CORRECTNESS problem (no more silent
misattribution, and concurrency is now visible) but does not redesign the single-slot live view
itself -- the Agent Map / office desk UI still shows one status per agent, not two simultaneous
activities rendered side by side. `concurrent_call_ids` gives the UI a real signal to build on (e.g.
a "2 active" badge); that UI work is real, separate, not attempted here. The 60s recency window is
a heuristic (this codebase has no reliable per-call_id "this run just finished" event today, only a
whole-top-level-run "done"/"error" signal), named as such, not claimed to be a precise lifecycle.
**Files changed**: `dourmouse/webui.py` (`ActivityTracker.__init__`'s new `_active_call_ids`,
`_record`'s `tool_use`/`tool_result` branches, new `_prune_call_ids_locked`/`concurrent_call_ids`,
`snapshot()`'s new field).
**Tests added**: `dourmouse/tests/test_map.py::TestActivityTrackerConcurrentCallIds` -- single and
concurrent call tracking, a result only updating `last` when it matches the current call (the exact
corruption scenario, reproduced and proven fixed), untagged-event backward compatibility, the public
method matching `snapshot()`, and stale-entry pruning after the recency window.
**Result**: fixed. Full suite green.

---

### 074 -- agent ecosystem flaw #5: real local backend concurrency ceiling, fixed

**Severity**: real, reliability (a genuine error under real concurrent local load, not a crash of the whole
process, but a failed turn for whichever branch lost the race).
**Context**: named in finding #066 as flaw #5 -- two simultaneous real calls against local Ollama threw a
genuine `HTTP Error 400: Bad Request` (live-observed during the design review's own verification pass),
root-caused as the local model server's own real capacity limit, not a Dourmouse bug (`dispatch.py`'s own
thread-local `_registry_ctx_stack` invariant comment confirms concurrent branches on separate threads are
BY DESIGN). Named fix direction at the time: "either cloud burst capacity or a real queue that degrades to
serial instead of erroring."
**Fix**: a real, process-wide `threading.Semaphore` gates the one real network-call boundary
(`_call_with_retry_inner`, renamed to a thin wrapper; the actual retry/fallback logic moved unchanged into
a new `_call_with_retry_inner_impl`) whenever `backend_identity(config)` identifies the call as genuinely
local -- every other backend (NVIDIA, cloud Ollama, Gemini, Claude CLI) is completely unaffected, no gate,
no wait, byte-for-byte the same as before this fix. Default concurrency is 1 (fully serial), matching the
flaw's own named fix direction exactly; a real env var, `DOURMOUSE_LOCAL_MODEL_MAX_CONCURRENT`, lets an
operator whose local setup genuinely handles more (e.g. a deliberately raised `OLLAMA_NUM_PARALLEL`) raise
it. The semaphore is lazily built once and deliberately NOT rebuilt on every read of the env var (a
semaphore's permit count is live state; recreating it while another thread holds a permit on the old
object would silently double the real limit) -- `reset_local_model_semaphore()` is the real test-isolation
seam, same convention as `message_bus.set_message_bus(None)`.
**Live proof, real threads and real timing, not mocked**: two real local (`OllamaConfig`) calls through a
slow fake client never overlap in wall-clock time and take roughly additive total time (serialized, proven
by real timestamps); two real cloud (`NvidiaConfig`) calls through the same fake client DO overlap and
take roughly one call's worth of total time (unaffected, also proven by real timestamps, not asserted on
faith); raising the env var to 2 lets two local calls genuinely overlap; the semaphore releases even when
the call raises (a second call never hangs behind a permit an exception failed to release).
**Honest, named limitation**: this bounds concurrent LOCAL calls to a safe default; it does not add cloud
burst capacity (the other half of the originally-named fix direction) -- a `delegate_parallel` fan-out
that would benefit from more real parallelism than one local model can serve still runs serially rather
than bursting to the cloud. Real, separate, not attempted here.
**Files changed**: `dourmouse/dispatch.py` (`_local_model_max_concurrent`, `_get_local_model_semaphore`,
`reset_local_model_semaphore`, `_is_local_backend`, the `_call_with_retry_inner` wrapper/`_impl` split).
**Tests added**: `dourmouse/tests/test_local_model_concurrency.py` -- backend-locality detection, env var
parsing (default/override/invalid), the real serialization-vs-concurrency proof described above via real
threads and real wall-clock windows, the raised-limit case, and semaphore release-on-exception.
**Result**: fixed. Full suite green (5486 passed, same 5 pre-existing unrelated failures as every prior
commit this session -- `google_auth`/`deeplink` env leakage).

---

### 075 -- audit: the embedded browser/PDF/media shell (real state, user-directed, "replace every app")

**Severity**: n/a (audit only, no code changed). **Context**: user framed the embedded browser as key
to a larger ambition -- Dourmouse as a real OS-level shell users never need to leave (writing docs,
watching video, browsing, media, search, all inside Dourmouse). Asked for a brutally honest map of what
actually exists today before scoping new work, not a guess from file/tool names.
**Real, source-verified findings** (file:line references from a dedicated read, not inferred):
1. **Browser automation is real, not a stub**: `dourmouse/browser_agent.py` drives real, locally-installed
   Chrome via Playwright (`pw.chromium.launch(channel="chrome", ...)`) -- real DOM fill/click/submit, not
   a urllib scraper. A separate, much simpler `fetch_url`/`open_url` pair lives on `research_info`
   (text-only fetch / OS-level `webbrowser.open`, not embedded).
2. **A real in-page browser pane exists**: `console.html`'s `<iframe id="bpFrame">` (real nav chrome:
   back/forward/reload/address bar, resizable/minimizable), backed by `browser_pane.py`'s real
   `check_frameable()` (reads actual `X-Frame-Options`/CSP) and a real server-side rewriting proxy
   fallback for sites that block framing. **Real, named limitation**: the proxy fallback carries no live
   cookies/session -- a site requiring login inside the pane, once proxied, will not behave like a real
   logged-in session. The iframe sandbox deliberately omits `allow-same-origin` (a documented sandbox-
   escape tradeoff), which also narrows what can work embedded.
3. **A second, faster embed exists but is not the shipped default**: `electron/main.js` wires a real
   Chromium `BrowserView` connected over CDP to the *same* live Playwright session the automation tools
   drive -- but it self-reports NOT CONFIGURED unless launched through the Electron shell specifically,
   and the currently-scripted launch path (`start.command`, `build_dist.sh`, `README.md`) still boots the
   older pywebview/WKWebView shell. Real, half-migrated state, not a design choice announced anywhere.
4. **PDF has a real page-image viewer**, not just text extraction: `pdf_reader.py` uses `pypdfium2`
   (the same engine Chrome's own PDF viewer uses) to render real page PNGs, plus real Tesseract OCR for
   scanned pages. Surfaced two ways: `ui/workspace.html`'s dedicated PDF READER panel, and `ui/file_
   preview.html` loaded into the shared browser pane via `open_file_preview`. A separate, backend-only
   `extract_pdf` (pypdf, text-only, no images) feeds RAG ingestion -- correctly kept separate, not
   confused with the visual viewer.
5. **No embedded audio/video playback exists anywhere in Dourmouse's own UI.** Checked directly: the only
   `<audio>`/`<video>` elements in any `ui/*.html` are TTS voice output (`new Audio()` against `/api/
   speech`) and a webcam feed for hand-gesture tracking -- neither is media playback. **Spotify is real
   but remote-control only**: it drives the user's own separate Spotify Connect device via the real
   Spotify Web API; Dourmouse never receives or plays audio bytes itself. **No YouTube integration exists
   at all** (zero matches repo-wide).
6. **This was never a scoped domain**: grepped both `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` and
   `docs/GODSPEED_ROADMAP.md` for "browser"/"PDF"/"media"/"replace every app"/"OS-level" as a named
   feature domain -- none exists. The closest real text is one product-vision sentence calling Dourmouse
   a "persistent, local-first, personal operating **layer**" (not "operating system," and about autonomy/
   persistence, not embedding other apps' UIs). A separate, pre-existing note,
   `docs/browser_pane_architecture.md`, scoped the iframe-vs-second-window tradeoff for the pane
   specifically, before both the fuller iframe+proxy implementation and the Electron `BrowserView` (both
   confirmed real above) existed.
**Result**: audit only. Real gap list (not yet built, see `docs/GODSPEED_ROADMAP.md`'s new Phase 5 item
for the tracked TODO): no in-app audio/video player, Electron's faster real embed not on the default
launch path, iframe-proxy sites without a live logged-in session, no YouTube integration.

---

### 076 -- OS-1: a real embedded audio/video player, the first media playback in the product

**Severity**: real missing capability (the vision deck's own "media playback for any type of
media or files"), plus one real bug found live and one real pre-existing defect fixed along
the way. **Context**: finding #075 established that no embedded audio or video playback
existed anywhere -- the only `<audio>`/`<video>` elements in any `ui/*.html` were TTS output
and a webcam gesture feed, Spotify is remote-control only (it drives the user's own separate
Connect device and never receives audio bytes), and there is no YouTube integration at all.
This builds it.

**What was built**:
1. `webui.py`: `_PREVIEWABLE_AUDIO_EXTS` / `_PREVIEWABLE_VIDEO_EXTS` / `_MEDIA_CONTENT_TYPES`,
   folded into `_PREVIEWABLE_EXTS` so `_sandboxed_preview_path` (the same open_path trust
   boundary, unchanged) accepts media. A container a browser cannot natively decode (`.mkv`,
   `.avi`, `.flac`, `.wmv`) is deliberately NOT listed: listing it would produce a silently
   blank player instead of an honest refusal. `_MEDIA_CONTENT_TYPES` is explicit rather than
   `mimetypes.guess_type()` because that is registry-dependent and returns `None` for several
   of these on a stock macOS Python, which would make the browser refuse a file it can decode.
2. `_parse_byte_range()` plus `_send_media_file()`: real HTTP range semantics (206 with
   `Content-Range`, `Accept-Ranges`, a real 416 for an unsatisfiable range) streamed from disk
   in 256KB chunks. Not an optimisation: without a 206 and `Accept-Ranges` a `<video>` element
   cannot seek at all, and `read_bytes()` on a real video would be a genuine way to kill a
   stdlib `ThreadingHTTPServer`. Multi-range is deliberately declined (falls back to a full
   200, which is legal) rather than half-answered. `BrokenPipeError`/`ConnectionResetError`
   are swallowed because a media element seeking or closing mid-stream is ordinary client
   behaviour, never a server error -- but only those two, never a read error on the file.
3. `GET /api/files/media`, the audio/video counterpart to the existing `/api/files/image`.
4. `ui/file_preview.html`: real `<audio>`/`<video>` branches with native controls, an explicit
   image branch, and an honest terminal state for anything else that names `open_path` instead
   of rendering an empty player.
5. `system_access.py`: `open_file_preview` accepts media, refuses an undecodable container by
   name, and reports a media open distinctly -- explicitly stating it does not press play,
   because it cannot.

**A real bug this had, found by live testing and NOT by the unit tests** (the exact reason
`docs/TESTING.md` says a passing test is evidence of what someone thought should work): for an
open-ended range past the end of the file (`bytes=99999999-`), `end` is computed as `size-1`,
which is LESS than `start`, so an inverted-range check placed before the past-the-end check
classified a genuinely unsatisfiable range as merely malformed and served the whole file with
a 200. The unit tests only covered a CLOSED range past the end, where `end >= start` held and
the bug was invisible. This is precisely the request a media element makes when it seeks near
the end of a file it has stale duration metadata for. Fixed by reordering; both shapes now
have regression tests; re-verified live (416 with `Content-Range: bytes */1447248`).

**A second real problem, in this change's own error copy**: a mistyped test URL returned 400
and the player surfaced it as `MEDIA_ERR_SRC_NOT_SUPPORTED` (code 4), which the message
confidently attributed to the codec. A browser raises code 4 both for an undecodable codec and
for a server that refused the URL. Naming one cause when there are two is a guess presented as
a diagnosis; rewritten to state both, show the real media URL, and say how to tell them apart.

**A real pre-existing defect fixed along the way**: `open_file_preview` handed the pane an
absolute `http://127.0.0.1:<port>/file_preview.html?...` for this app's OWN page, while the
console may be loaded as `localhost` -- different origins to a browser, so the app was framing
its own page as a cross-origin embed for no reason, which is exactly why the pane's own code
comments say back/forward history "isn't reachable from the parent page". The tool now hands
the pane a ROOT-RELATIVE URL; `/api/browser-pane/open` accepts one; `console.html` skips the
frameability probe for one (this server never sends `X-Frame-Options` against itself, and the
probe needs an absolute URL to fetch anyway). `//host/path` is explicitly refused -- it reads
as relative but is a different origin, and accepting it as same-origin would frame a foreign
site with this app's own trust.

**Live verification** (`~/Documents/DOURMOUSE/EVIDENCE/002_media_player.txt`): range semantics
and byte-exactness confirmed against a real 1.4MB MP4 (a mid-file range's md5 matches `dd` of
the same offsets exactly); a real H.264 video decoded and SEEKED in the browser
(`readyState: 4`, `videoWidth: 194`, seek to t=1.0s completed, frame drawn); real AAC audio
decoded with native transport, including a filename containing a space; the honest
unsupported state rendered for a real `.mkv`; the real tool run against the real server for
both the accept and the refuse path.

**What is NOT verified, stated plainly (two separate things)**:

*(a) Wall-clock playback.* Pushed past metadata deliberately, because "the player loads" is a
weaker claim than "the player plays". `play()` resolves successfully and then `currentTime`
does not advance, for audio OR video, in this test browser: a headless/automated browser has no
audio sink and does not drive the media clock when the tab is not genuinely painting. What IS
proven is the substantive part: `readyState: 4` (HAVE_ENOUGH_DATA) on both, a parsed real video
track (`videoWidth: 194`), a real decoded frame drawn on screen, and a seek to an arbitrary
offset landing EXACTLY (`currentTime == 5.0`) -- a seek cannot land without a successful 206 and
real demuxing at that offset, which is the strongest available evidence that the range
implementation is correct end to end from the browser's side. So: decode verified, seek
verified, render verified, wall-clock playback NOT verified and not claimed. It needs the real
desktop shell with a real audio device.

*(b) Rendering inside the browser pane's sandboxed iframe.* Diagnosed rather than
assumed: a plain PNG, a pre-existing path this change never touched, renders blank in the pane
identically; the network trace shows `net::ERR_BLOCKED_BY_CLIENT` on the iframe load, a
client-side block that never reached the server, while `/api/browser-pane/check` returned 200;
and after the same-origin fix above the same-origin URL is still blocked identically, so the
origin mismatch was a real defect but not the cause. This test browser blocks sandboxed-iframe
navigation outright, matching the standing note that it is a separate browser hitting the same
server rather than the native pywebview shell. The route, the player and the tool are
verified; the pane embed is not claimed, and is tracked as OS-4.

**Also found, recorded separately rather than absorbed here**: driving this through the real
directive box, the local `gpt-oss:20b` backend answered "I've opened the audio file in the
embedded preview pane" WITHOUT calling `open_file_preview` at all (`GET /api/office_log?kind=
events` shows zero tool events for that turn). A fabricated tool result is a direct violation
of this product's own honesty contract and is a real, separate problem.

**Security of the new route, checked rather than assumed** (it streams arbitrary absolute
paths, so it is a real surface): traversal, non-media absolute paths, `/dev/zero`, an empty
path, and a traversal suffix appended to a real media path are all refused, verified live
against the running server and pinned in tests. The load-bearing property is an ORDERING one:
`_sandboxed_preview_path` resolves the path FIRST and then checks the extension of what it
actually resolved to, so a symlink named `looks_like.mp4` pointing at `/etc/passwd` is refused
(confirmed live), while a symlink whose real target IS media is served. A check on the given
NAME instead would have served the former. Both halves are tested, because a test for only the
refusal would also pass under a blanket "refuse all symlinks" rule, which is not what the code
does and not what it should do.

**Regression check on what this restructured**: `file_preview.html`'s single else-branch became
three (media, image, honest fallback), so the pre-existing PDF and image paths were re-checked
rather than assumed intact: `/api/files/pdf-info` 200 with a real `page_count: 11`,
`/api/files/pdf-page.png` 200 with real PNG bytes, `/api/files/image` 200, and a real 11-page
PDF rendering in the browser with correct paging controls.

**One existing test changed deliberately, and why**:
`test_system_access.py::TestOpenFilePreview::test_real_post_reaches_the_browser_pane_open_
endpoint` asserted the pane URL started with `http://127.0.0.1:8765/file_preview.html?...`.
That assertion encoded the defect described above, so it was updated to require the
root-relative form (and to reject a protocol-relative one), not merely relaxed. The API call
itself is still asserted to be absolute, because that half is correct and must stay.

**A second full-suite failure, diagnosed and NOT caused by this work**:
`test_app_control_ax.py::TestRealLiveIntegration::test_activate_app_fast_really_works_against_
finder` failed with `macOS refused to activate 'Finder' (activateWithOptions_ returned false)`.
It passes in isolation on clean HEAD and in isolation with these changes applied. It failed
because a browser was being driven on this machine throughout that suite run, and macOS
refuses a foreground activation while another app holds focus. Its own docstring's claim that
it is "safe to exercise for real in CI on a real Mac runner too" is too strong. Recorded as a
real, separate item (X-7) rather than patched over or ignored.

**Tests**: `dourmouse/tests/test_media_preview.py`, 46 cases -- the real route against real
files on disk with the fake only at the socket boundary, the range parser's three return
shapes in isolation, extension/content-type consistency between the tool and the server (two
lists that can drift is a real bug class), and the same-origin pane URL including the
protocol-relative refusal.

---

### 077 -- an unhandled BrokenPipeError on every client disconnect, found by booting the real shell

**Severity**: operability. **Context**: found while verifying finding #076's two unverified
gaps in the real Electron shell rather than the Claude Browser test tool. The very first
Electron cold boot printed a ~25-line traceback to stderr:

```
Exception occurred during processing of request from ('127.0.0.1', 55925)
  ... File "dourmouse/webui.py", line 2159, in do_GET
      self._send_json(self.server.tracker.snapshot())
BrokenPipeError: [Errno 32] Broken pipe
```

**Why this is real and not cosmetic**: `/api/activity` is polled on a timer by this server's
own UI, so any reload, navigation or window close aborts an in-flight poll. A vanished client
is ORDINARY, expected behaviour, not an error -- but `socketserver` logged a full traceback for
each one. Noise that routinely buries real errors is an operability bug.

**Fix**: this codebase already treats a vanished client as ordinary in both places that stream
(the SSE emitter sets `client_gone`; `_send_media_file` returns quietly, added in #076). This
generalizes that ESTABLISHED convention to every ordinary route at the one chokepoint,
`_Handler.handle_one_request`, rather than adding a third opinion or sprinkling try/except per
route. Deliberately narrow: only `BrokenPipeError` and `ConnectionResetError` are caught, and
`close_connection` is set so the socket is torn down rather than reused. Every other exception
still propagates and is still logged loudly -- swallowing more here would hide real server bugs
behind a silent socket close, which is the opposite of what this codebase does everywhere else.

**Verified**: re-booted the real Electron shell after the fix. Traceback count 0 (was 1),
`BrokenPipeError` count 0. Tests exercise the real method through the real MRO (faking only
what it delegates to) for both exception types, assert a `ValueError` still propagates, and
end-to-end abort a real in-flight request by closing the socket mid-body and then prove the
next request still succeeds.

**Both of #076's unverified gaps are now closed by this same session**, in the real shell:
- **Wall-clock playback**: audio advanced 1.95s over 2.5s and seeked exactly to 5.0; video
  advanced **2.002s across 2.000s** of wall clock with a real decoded frame. In the Claude
  Browser tool `play()` resolved but `currentTime` never moved, for either, because a headless
  browser has no audio sink.
- **The pane embed**: `POST /api/browser-pane/open` with the root-relative URL returned
  `{"ok": true}` and the media player rendered correctly INSIDE the real browser pane, inside
  the real Electron window, on the first try. The pane's address bar reads
  `/file_preview.html?src=files&path=...`, so #076's same-origin fix is live and visible in the
  real UI. Screenshots: `~/Documents/DOURMOUSE/EVIDENCE/003_*.png` and `004_*.png`.

The #076 diagnosis was therefore correct: the blank pane was the Claude Browser test tool
blocking sandboxed-iframe navigation (`ERR_BLOCKED_BY_CLIENT`), never a product defect. Worth
recording as a method, not just a result: it was established as a harness problem rather than a
regression by checking a PRE-EXISTING path the change never touched (a plain PNG) and finding it
failed identically.

**Also established, and it supersedes a standing limitation**: screenshots CAN be written to
real disk paths, via Playwright `connect_over_cdp` against the Electron shell's own CDP port
plus `page.screenshot(path=...)` -- the same mechanism `browser_agent.py` already uses. Two real
gotchas: Electron refuses `Target.createTarget` (so navigate an existing page from
`context.pages` and restore it in a `finally`, never `new_page()`), and a sandboxed iframe's
`contentDocument` is `null` from the parent (an opaque origin, meaning it cannot be introspected
from outside, NOT that it failed to render).

---

### 078 -- OS-2: the Electron shell was fully built and nothing ever launched it

**Severity**: real shipped capability that no user could reach. **Context**: the roadmap
carried this as "half-built, pick one: finish the migration or delete it". Reading the source
showed that framing was wrong. All five migration stages are real and present in
`electron/main.js`: A/B (shell, windowing, IPC bridge), C (real `Tray`, `Notification`,
`nativeImage` branding), D (the embedded CDP-driven `BrowserView` plus a pane-bridge HTTP
server), and E (`electron-builder` config with notarization and `extraResources` laying
`dourmouse/`, `ui/` and `.venv/` out in the layout the Python path helpers already expect).
`dourmouse/browser_agent.py` genuinely reads `DOURMOUSE_ELECTRON_CDP_PORT` and calls
`chromium.connect_over_cdp()`. `electron/verify-result.json` records a real verification run
with `"errors": []`.

**The actual defect was that nothing launched it.** `start.command` and both installed `.app`
bundles all ran `python -m dourmouse.desktop`, which opened the older pywebview shell. Every
one of those stages shipped to a user who never saw any of it -- including the CDP pane that
finding #077 proved is what makes the browser pane, PDF viewer and media player behave like a
real browser instead of a blocked sandboxed iframe.

**Fix**: shell selection in `dourmouse/desktop.py`'s `__main__` block.
- `_electron_shell_argv()` checks for the real files (an executable
  `electron/node_modules/.bin/electron` and `electron/main.js`) rather than trusting a flag.
- `_resolve_shell_choice()` reads `DOURMOUSE_SHELL` (`auto` default, or `electron`/`pywebview`).
- `os.execv` REPLACES this process, so there is exactly one app process either way and the
  existing `.pid` file still refers to the real running app. The deep link is forwarded verbatim.

**Two deliberate constraints, both load-bearing:**
1. **This lives in `__main__`, never inside `launch()`.** `launch()` is called directly by many
   hermetic tests, which must keep getting the pywebview path byte for byte; if the choice ever
   migrated into `launch()` those tests would start exec'ing a real Electron binary. A test
   asserts this by reading `launch`'s own source. The payoff is that every real launcher --
   both `.app` bundles, `start.command`, a bare `python -m dourmouse.desktop` -- picks the new
   behaviour up with **zero changes to any of them**.
2. **Electron is NOT an unconditional default.** `electron/node_modules` is gitignored and
   ~408MB, so a fresh clone genuinely does not have it. `auto` means "prefer the better shell
   when it is really here", never "assume it is here". An explicit `DOURMOUSE_SHELL=electron`
   that cannot be satisfied prints the real reason AND the real fix (`cd electron && npm
   install`) and then still opens the app, because refusing to start at all would be worse than
   degrading -- but it degrades loudly, never silently.

**Live-verified through the real entry point**, not a helper: `python -m dourmouse.desktop`
printed the handoff line, exec'd Electron, and the shell came up with its backend on 18791, CDP
on 19335 and the pane bridge on 19336, all three held by a single PID (confirming `execv`
replaced rather than spawned). Screenshot of the real Vision Workspace running under the
default path, with real live data (44 World Pulse events across 8 channels):
`~/Documents/DOURMOUSE/EVIDENCE/005_electron_is_the_default_shell.png`.

**Also corrected: `electron/main.js`'s own header comment was stale**, claiming Stages C, D and
E were "not yet done" while `package.json`'s description in the same directory said the
opposite. This is the same documentation-reality drift `docs/UI_SOURCE_MAP.md` already recorded
twice ("four skins" where there were eight, "nine screens" where there were fourteen). Rewritten
to state what is actually there, with a note to treat in-file comments as directional until
re-checked.

**Tests**: `dourmouse/tests/test_shell_selection.py`, 17 cases. They fake the FILESYSTEM (a real
temp `electron/` layout) rather than the function under test, per `docs/TESTING.md`'s house
convention. Covered: a real install is found; a missing `node_modules` is not an error; a
non-executable binary (an interrupted `npm install`) is refused rather than handed to `execv`; a
missing `main.js` is refused; `auto` prefers Electron only when present; the `pywebview` opt-out
wins over availability; an unsatisfiable explicit request both degrades AND says so with the
fix; unknown and mixed-case values resolve sanely; and `launch()` itself contains neither the
selection nor `execv`.

**One notable S606 lint annotation**: `os.execv` is flagged as "starting a process without a
shell", which is the SAFE form (S602/S605 flag the opposite). Annotated with the real reason --
every element of the argv is built from this file's own resolved location, never from user
input or the environment.

---

### 079 -- UI-2: the type and spacing scales, plus a real Figma design system

**Severity**: design-system gap that `docs/DESIGN_SYSTEM.md` itself named as blocking.
**Context**: `docs/UI_SOURCE_MAP.md` section 2 recorded that colour is fully tokenized ("every
colour already routes through var()") while **seventeen** distinct font-size values ran from
7.5px to 22px in half-pixel increments across `console.html` alone with no scale governing any
of them, and nothing at all governed section/panel spacing against ~720 raw px literals in the
same style block. `DESIGN_SYSTEM.md` listed these as gaps 1 and 2 and said explicitly to close
them **before** building new panels, so new components do not reintroduce the problem.

**What shipped, in `ui/assets/dourmouse-ui.css`:**
1. **An 8-step type scale** (`--dm-text-2xs` 9px through `--dm-text-2xl` 22px), integers only.
   The steps are DERIVED from where the seventeen real values actually clustered, not invented:
   the 7.5/8/8.5/9 cluster becomes `2xs`, 10/10.5 becomes `xs`, 11/11.5 `sm`, 12/12.5 `base`,
   13/13.5 `md`, then 15/19/22 which were already single values.
2. **A 6-step spacing scale** (`--dm-space-1` 4px through `--dm-space-6` 32px). These are the
   exact values `DESIGN_SYSTEM.md` had already specified in prose, now real rather than
   described -- implementing the existing decision, not inventing a second one.

**Two deliberate design decisions, both recorded in the stylesheet and pinned by tests:**
- **A conventional 1.25 "major third" scale was rejected.** It would collapse the five distinct
  metadata/label/body weights this dense terminal UI genuinely distinguishes into two or three.
  The small end therefore stays tight (~1.1 ratio) and widens only toward display sizes. A test
  asserts the small steps stay under 1.18 so a future "tidy up the scale" cannot silently undo
  it.
- **The spacing scale is ADDITIVE, never a replacement for `--dm-row-y`/`--dm-gap`.** Those two
  have precise, different jobs (vertical padding inside a dense row; the gap between adjacent
  controls) and folding them into a generic scale would lose that meaning. A test asserts they
  still exist.

**Deliberately NOT done in this pass, and stated rather than implied**: migrating the ~720 raw
px literals and seventeen font-size call sites onto these tokens. Defining the scale is the
prerequisite and is safe and additive; a blind sweep of 720 literals across a 7,092-line file is
exactly the kind of change that breaks a UI silently. Tracked as real follow-on.

**A second, separate real find, fixed here**: `ui/workspace.html`'s hand-control panel rendered
a `.note` containing pure developer commentary directly to the user -- internal constant names
(`LANDMARK_SMOOTH_ALPHA` / `PINCH_ENGAGE_RATIO` / `PINCH_RELEASE_RATIO`), "see page source",
"see startHandControl below", and a paragraph on MediaPipe's GPU-delegate internals. Spotted in
a real screenshot of the running app, not by grep. The vision deck asks for "sleek professional
and easy to use". The information was NOT deleted -- it moved into an HTML comment, where the
developers it was written for actually read it, and the two sentences that genuinely tell a
USER what to do stayed visible.

**The Figma design system** (user-directed: "use claude design and figma design in regards to
ui"). A real Figma file now mirrors these tokens: `Dourmouse Design System`, file key
`zHH3ZLx5MZHfAltHOGkYBk`. Built per the `figma-generate-library` workflow.
- *Phase 0 discovery*: `get_libraries` returned 8 subscribed community kits (Material 3, Simple
  Design System, the Apple platform kits) and nothing Dourmouse-specific. **Decision: build from
  code, do not reuse** -- those kits' token models are incompatible and contradict this
  product's own documented philosophy (dense terminal, four solid layers, no translucency, one
  accent under ~10% of the screen). That is the skill's own "rebuild if token model
  incompatible" branch, recorded rather than assumed.
- *Phase 1 foundations*: 43 variables across 3 collections -- `Primitives` (12 raw values,
  scopes deliberately `[]` so they never pollute a property picker), `Color` (13 semantic roles,
  each a real `VARIABLE_ALIAS` to a primitive, never a duplicated literal), and `Scale` (18: the
  8 type steps, the 6 spacing steps, and the 4 density values kept as their own family for the
  same reason they are separate in CSS). Every variable carries explicit scopes and WEB code
  syntax using the real `var(--dm-*)` name, so Dev Mode round-trips to the actual stylesheet.

**Honest limits on the Figma work**: Phase 2 (foundations documentation pages) and Phase 3
(components) are NOT done. The tokens are what UI-2 asked for and what unblocks the rest; the
component library is real, separate, larger work. Also worth recording for a future session: the
account reports a "View" seat on a starter tier, which I assumed would block writes -- it did
not, verified by actually creating the file rather than concluding from the label.

**Phase 1 exit criteria verified from the file, not from the write's return value**: a
read-only audit re-read all 43 variables and reported 0 problems. Every one of the 13 semantic
colours is a genuine `VARIABLE_ALIAS` to a primitive (0 literals), every non-primitive has real
scopes with no `ALL_SCOPES` survivor, every primitive has the hidden `[]` scope, and every
variable has WEB code syntax with the `var()` wrapper. Three end-to-end spot checks resolve to
the exact hex in the real stylesheet: `canvas` -> `green/canvas` -> `#0a2a22` ->
`var(--dm-canvas)`; `active` -> `amber/500` -> `#f59e0b`; `fg` -> `neutral/50` -> `#fafafa`.

**Tests**: `dourmouse/tests/test_design_tokens.py`, 29 cases, reading the REAL shipped
stylesheet rather than a fixture. They pin that every step exists, every step is a whole pixel
(the half-pixel mess is the specific thing being replaced), the scale ascends with no duplicate
steps, it spans the real 9-to-22 range that was in use, the small end stays dense, the spacing
values match what the design doc already specified, the dense row tokens survive, and both
scales carry a stated rationale rather than bare numbers.

---

### 080 -- the OS shell design system and a 20-screen interactive mockup

**Severity**: n/a (new design layer plus a prototype; nothing wired into a live surface).
**Context**: user direction, verbatim: *"keep in mind this is an os, it shoukd be as such,
wallpapers, custom widgetes, fonts, neat professional presentatons, animations for stuff, 3d
designs, similar tp how mac os is, os design rules etc"*, then *"i need it more detaled much
more, mock up everything, every part of dourmouse"* and *"i need to understand how tghe
buttonswould work, everythng"*.

**A real conflict, resolved rather than ignored.** `ui/assets/dourmouse-ui.css`'s own header
forbids four things BY NAME: floating cards, translucent wash, decorative glow, icon/badge
vomit. macOS is built on the first three. Those rules were written for a DENSE TOOL SURFACE
and they remain correct there, which is why nothing in this change touches `console.html`'s
terminal feed. The resolution keeps the intent and drops the letter, and the reasoning is
written into the new stylesheet rather than left implicit: depth is allowed but every level
MEANS something (focus, stacking, modality), never "looks nicer"; translucency is allowed but
only as a real material over a real wallpaper, never a wash faking depth over a flat colour;
**glow stays banned**; icons still have to earn their place. The ~10% accent budget is
unchanged and still binding.

**`ui/assets/dourmouse-os.css` (601 lines), thirteen numbered sections**: materials (four
vibrancy steps, each a blur PLUS a tint because a bare alpha over a busy wallpaper destroys
text contrast), elevation (four levels, each a statement about STATE, two shadows per level
because one alone reads as a sticker), geometry, motion (real springs with overshoot, because
an OS springs and does not ease), procedural wallpaper, window chrome, dock, widgets, menu
bar, accessibility, depth/3D, controls, and the annotation mode.

**Decisions worth recording because they are not the obvious choice:**
- **The dock lifts, it does not magnify.** macOS magnification reflows every neighbouring tile
  and is genuinely disorienting on a dense workbench where the dock sits under live content.
- **Wallpapers are procedural, not bundled images.** This app self-hosts its fonts specifically
  to stay offline-capable; shipping photography would contradict that. Four gradient presets,
  plus a real user photo upload read with `FileReader` so the bytes never leave the machine.
- **A dim control ships with the photo upload.** A real photo has arbitrary brightness and body
  copy needs 4.5:1 over whatever is behind it, so this is a readability control, not a taste one.
- **An opaque fallback for `backdrop-filter`.** Some embedded webviews silently no-op it, and
  this app ships in two different shells.
- **`prefers-reduced-motion` collapses every animation to 1ms but keeps hover and focus
  feedback.** Killing animation is not the same as killing feedback.

**`ui/os_mockup.html`: an interactive prototype, not a picture.** All 20 screens from
`console.html`'s own real `SCREENS` array, each with realistic content, driven by data so
twenty screens do not mean twenty copies of the same scaffolding. Screens state what is NOT
built rather than implying completeness: RESEARCH greys out 10 of its 14 stages and says the
loop cannot run backwards; PROJECTS says outright it is a read-only summary rather than the
isolated environment the deck asks for; the Alerts dock tile is labelled "not built yet".
Every figure shown is a real observed value from this machine (49 agents, risk 21 with 4
findings, 8 pulse channels, 5442 tests, 4 of 14 research stages) so the layout is judged
against content it will really carry.

**Annotation mode answers "how do the buttons work" directly.** Press `A` and every interactive
element labels itself from its own `data-spec` attribute. The spec sits ON the control rather
than in a legend to cross-reference.

**One custom icon per screen**, stroke-based, 24x24, 1.5 weight, all on the same grid so they
read as one family rather than twenty borrowings. Each is a literal of what the screen does.

**Three real bugs found by DRIVING the prototype, not by reading it** (all mine, all fixed):
1. `show()` rebuilt the entire sidebar on every screen change, which destroyed the element a
   user was mid-press on (a scripted click timed out for exactly this reason), threw away
   focus, and flickered. A real window manager moves a highlight; it does not rebuild its dock.
2. The fix for (1) broke it worse: `renderNav()`/`renderDock()` were only ever CALLED from
   inside `show()`, so removing them meant the chrome never rendered at all. Zero nav items.
3. GLOBE overflowed: a `height:100%` card inside a scrolling body pushed three cards past the
   viewport.

**Also found while doing this, and it is a real product defect**: the server injects
`#dmSpotifyWidget` into EVERY served page. It was covering the browser pane window in a
screenshot, reporting NOT CONFIGURED on a surface it has nothing to do with. The mockup opts
out; the underlying injection is tracked as a separate item.

**Verified**: all 20 screens walked programmatically in a real Chromium, zero JS errors, zero
overflow, zero window collisions, every screen rendering its own content. Photo upload verified
end to end (a real PNG applied as a `data:` URL at `background-size: cover` and persisted to
`localStorage`), and the dim slider verified moving the overlay from 0.35 to 0.65. Evidence:
`~/Documents/DOURMOUSE/EVIDENCE/mockup_screens/` (24 files) plus `007`/`008`.

**Not done and not claimed**: none of this is wired into `workspace.html` or `console.html`.
That is the approval gate. Figma Phases 2 and 3 (foundations doc pages, component library) are
also still not built.

---

### 081 -- the browser pane's proxy errors are an architecture problem, and the fix is proven

**Severity**: real capability defect. **Context**: user asked directly, *"how can we integrate
a rea kworking browser in here that works and doesnt throw proxy errors"*.

**The real cause.** The in-app browser pane is an `<iframe>`. Any site sending
`X-Frame-Options` or CSP `frame-ancestors` refuses to load in it, which is most of the real
web, so a server-side rewriting proxy exists as the fallback. **That proxy is where the errors
come from**: it rewrites relative URLs imperfectly, it carries no cookies so a logged-in site
looks logged out (named as a real limitation in finding #075), and it has to rewrite responses
it cannot always parse.

**The fix is not a better proxy. It is not needing one.** Those headers restrict FRAMING. An
Electron `BrowserView` is a real top-level browsing context, not a frame, so they do not apply
to it at all.

**Proven with a controlled experiment rather than asserted.** A local page was served with both
`X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`, then loaded two
ways in the SAME Chromium, in the same process:

```
iframe      -> "BLANK (refused)"   (ERR_BLOCKED_BY_RESPONSE)
BrowserView -> "LOADED: this page refuses framing"
```

A local page was used deliberately over a real site: it makes the headers the only variable,
needs no network, and is reproducible by anyone.

**What shipped**: `electron/main.js`'s pane bridge previously had only `/status`, `/show` and
`/hide`, so the `BrowserView` could never be navigated except through Playwright over CDP. It
now has `POST /navigate` plus `/back`, `/forward` and `/reload`.

**Security of the new endpoint, since this bridge is reachable from any local process**: only
real `http(s)` URLs are accepted. `file://` and `javascript:` are refused by name and verified
refused live, because a `BrowserView` pointed at `file://` would read the user's disk. The
request body is capped at 8KB.

**Verified end to end**: `POST /navigate` with the framing-blocked page returned
`{"ok":true}`, and reading the real pane back over CDP confirmed it had genuinely rendered
(`document.getElementById('ok').textContent` came back as the page's own text, not a blank
document). Screenshot: `EVIDENCE/009_browserview_loads_framing_blocked_site.png`.

**Consequences worth stating**: no proxy means no URL rewriting, no broken relative links, real
cookies and real sessions, and real history (the iframe pane's own comments note cross-origin
history "isn't reachable from the parent page"; a `BrowserView` owns its history outright).

**Honest limits.** This is the Electron path only. The pywebview shell has no `BrowserView`, so
the iframe plus proxy remains its fallback and keeps every limitation named in #075. Finding
#078 made Electron the default when it is installed, so the good path is now the normal one,
but a fresh clone without `electron/node_modules` still gets the old behaviour. Wiring the
console's pane UI to call `/navigate` instead of setting `iframe.src` is the remaining step and
is NOT done.

---

### 082 -- the UI was set in a pixel font, and the clean one was already on disk

**Severity**: real, product-wide. **Context**: user reviewed the OS mockup and asked for "a
standard font for everything, not pixelated, clean and neat but not your defaults".

**The defect**: `--dm-font-sans`, the token every label, heading and line of body copy in the
product resolves through, was set to **Departure Mono, a pixel font**. That is a legitimate
deliberate accent face and entirely the wrong choice for the typeface a whole UI is set in.
It is why the interface read as pixelated rather than clean.

**The fix cost no new bytes, because the right face was already here.** `ui/assets/fonts/`
contains **nine Space Grotesk files** (three weights, three subsets) with **no `@font-face`
declaration anywhere**, so none of it could ever load. `docs/UI_SOURCE_MAP.md` predicted
exactly this in its typography section: *"several weights not obviously wired to any current
theme token -- check for orphans during the redesign."* This is that orphan.

**What shipped, in `ui/assets/dourmouse-ui.css`:**
- Six `@font-face` declarations for Space Grotesk: 400/500/600, latin and latin-ext.
- `--dm-font-sans` is now Space Grotesk. Geometric, real weights, distinctive enough to be
  this product's own voice rather than the system default every application reaches for
  (explicitly not `system-ui` and not Inter, per the user's "not your defaults").
- `--dm-font-mono` leads with Monaspace Neon as the real shipped face. Clean, modern, four
  genuine weights, and a mono so columns of numbers and paths actually align. Berkeley Mono
  stays first in the stack for anyone who has licensed it locally.
- Fallback stacks reordered so a missing face degrades to the SAME shape, sans to sans and
  mono to mono, never across. A sans falling back to a mono changes every column width.

**`unicode-range` is load-bearing, not decoration.** With it the browser downloads only the
subset a page actually needs, so a latin-only screen never fetches the Vietnamese file.
Without it all three subsets download and the split is strictly worse than having no split at
all.

**Departure Mono is kept DECLARED but unreferenced.** Deleting the face would remove a real
option from any theme that wants a pixel accent on purpose; the defect was using it as the
default, not its existence.

**Offline-first is preserved.** Both faces were already self-hosted, which is the whole reason
this app vendors its fonts. Nothing new is fetched and no CDN is introduced.

**Verified live, from the browser rather than from the source**: `document.fonts` reports
`Space Grotesk 400`, `500`, `600` and `Monaspace Neon 400` all loaded;
`document.fonts.check('16px "Space Grotesk"')` is `true` and the same check for Departure Mono
is `false`; and the computed `font-family` on the menu bar, the nav items and the body all
resolve to Space Grotesk. All 20 mockup screens re-walked afterwards: zero JS errors, zero
overflow, layout unaffected.

**Addendum, same day: the agent office view.** The mockup's OFFICE screen was a flat list,
which does not show what the design actually calls for. Rebuilt as the real building from the
design phase: five floors plus the Lounge, with the **verified 43-agent floor map** (F1
Executive and Ops 11, F2 Research and Intel 9, F3 Trust and Security 3, F4 Engineering 11, F5
Finance and Media 8, Lounge 1). That map came from a live `build_general_registry()` call, not
from invention. A floor selector on the left doubles as a building cross-section, each floor
showing a dot per agent coloured by its real `ActivityTracker` status (idle, working, live, in
meeting). Selecting a floor renders its desks; an agent in a fan-out has its desk dimmed
because the seat is genuinely empty, and appears in the meeting room instead with a real
arrival animation. The animation is the point: it is how a fan-out reads as agents going
somewhere rather than a list changing colour.

**Two real defects found while building it, both worth recording as classes:**
1. **An undercount caught by counting.** The first build rendered 42 of 43 agents. `mail` is an
   always-on poller belonging to no team, so it had silently fallen out. It now has the Lounge.
   This is the same quiet-undercount class this project has repeatedly found in its own stale
   comments, and it was caught only because the render was checked against the known total.
2. **A CSS class collision that only shows visually.** `meet` was used as BOTH a status
   modifier (`<i class="fd meet">`, `<div class="desk meet">`) and the meeting room's own class.
   The bare `.meet{width:196px;display:flex}` rule therefore styled every status dot as a
   196px-wide meeting room, producing purple bars across the building column. A modifier and a
   component must never share a class name; the room is now `.meetroom`.

Worth noting how (2) was diagnosed, because the first two hypotheses were wrong: it was
initially assumed to be an animation artifact, and a screenshot with `animations="disabled"`
disproved that by reproducing it identically. A DOM query then found three elements with a
computed `width: 196px` that should have been 4px, which named the collision immediately.
Checking computed values beat reasoning about the symptom.

**Scope note**: this changes the shared token, so it reaches every surface that actually uses
`--dm-*`. `console.html` and `workspace.html` still maintain their own duplicated `:root`
palettes (finding #040, roadmap item UI-1), so they keep their own font declarations until
that duplication is resolved. That is a known, separately tracked gap, not an oversight here.

---

## Not yet audited (honest, tracked gap — see `docs/GODSPEED_ROADMAP.md` Phase 1)

Every own-write-path SQLite store's cross-thread safety is now verified
(finding #017): `goals.py`, `cache.py`, `google_auth.py`,
`memory_store.py`, `supabase_sync.py`, and (after the fix)
`global_memory.py`. Git-history secret mining is also now done (finding
#018, clean). Still open: external-database read-safety for the five
read-only readers listed in finding #017 (a concurrently-writing external
process is a different failure mode than an in-process race), database
audit (schema/constraints/transactions across every store), full network
audit, source-ingestion audit, dependency audit (beyond the two additions
in findings #010/#013), and a formal `docs/TEST_MATRIX.md`. None of
these are silently assumed clean — they are explicitly not done yet.

**`mypy` is now configured and run for the first time (finding #019)**,
lenient config in `pyproject.toml`. Real remaining backlog from that run,
triaged by category but not individually fixed line-by-line — the same
deliberate, documented deferral as ruff's own backlog below, not an
oversight: `attr-defined` (206 occurrences), `union-attr` (22),
`arg-type` (44), `misc` (16), `assignment` (16), `return-value` (13),
`operator` (8), `index` (5), `var-annotated` (4). Expected to be
overwhelmingly type-inference noise from a previously-unannotated
codebase (dynamic attributes, un-narrowed `Optional`s, test doubles) —
consistent with the 6 sampled higher-signal categories, of which 3 of 6
turned out to be exactly that once actually read. Not yet individually
confirmed at this volume, and not claimed to be.

**ruff is now configured and run (finding #010)**, curated rule set in
`pyproject.toml`. Real remaining backlog from that run, triaged but not
individually fixed — deliberately, not an oversight:

- **`S110`/`SIM105`** (104 + 89 ≈ 193 combined) — `try`/`except`/`pass`
  (or `continue`) sites beyond the 15 individually reviewed in finding
  #002. Same real class of finding; a full sweep of the rest is real,
  worthwhile, and NOT done in this pass (193 sites individually is a
  dedicated pass of its own, not a few-minutes tail end of this one).
- **`S310`** (64) — every `urlopen`/`Request` call site ruff can see.
  Sampled, not exhaustively reviewed: the one real gap found this way
  (`fetch_url` had no host validation) is fixed (finding #014); the rest
  are overwhelmingly fixed, hardcoded API endpoints (Spotify, Trading212,
  Supabase, world_pulse's 17 sources, etc.), not exhaustively confirmed
  one by one.
- **`PLW1510`** (63) — `subprocess.run(...)` without an explicit `check=`
  argument. Sampled and judged **not worth fixing**: Python's own default
  is `check=False`, and every sampled call site already manually inspects
  `.returncode`/`result.returncode` itself — this codebase's real,
  working convention already IS what this rule asks to spell out
  explicitly. A style preference, not a correctness gap.
- **`E402`/`E731`/`E741`/`B007`/`B905`/`B904`/etc.** (≈60 combined) — minor
  style findings (module-level imports not at the very top, lambda
  assignment, ambiguous single-letter names, loop-variable reuse,
  `zip()` without `strict=`, `raise` losing exception context). Real,
  low-severity, genuinely not reviewed individually yet.

### 083 -- the OS mockup, redesigned and approved as the reference UI

Status: DONE, owner-approved 2026-09-24. UI only, wired to nothing.

The 20-screen mockup from finding #080 was reworked in a live design session and
approved as the reference OS design. Same green Hermes design language, but the shell
now behaves like a real desktop OS and two data-heavy screens became diagrams.

What changed, in `ui/os_mockup.html`:

- **Dead features removed.** VISION (camera/hand control) and DESIGN3D (3D scene editor)
  were gimmicks that do not serve the "replace every app / act as an OS" north star, so
  both screens, their icons and their nav entries are gone. The redundant GLOBE world-map
  screen was repurposed into a real BROWSER screen. Nav is 18 focused screens, not 20.

- **Real OS chrome.** Window traffic lights on every window bar. A macOS-style Control
  Centre (click the menubar status cluster): Agents, Security, Network, Brain, Do Not
  Disturb and Autonomous tiles, a brightness slider, and accent swatches that live-recolour
  the whole OS through `--dm-active` and persist to localStorage. A Notification Centre on
  the dock's Alerts button with unread state and Clear.

- **Research is a flowchart.** `rflow()` draws the full 15-stage loop as a serpentine, green
  for the 4 built stages and grey for the rest, with the backward edge drawn as a dashed
  amber loop around the whole diagram and labelled as not built. `egraph()` draws the
  first-class-object graph (a hypothesis with supported / contradicted / tested / revised
  edges). This is finding #083's honest picture of Domain G: mostly not built, and the one
  thing that matters most (the backward edge) drawn as absent.

- **Security is a flowchart.** `sflow()` draws the layered pipeline telemetry -> deterministic
  analyzers -> baseline -> event engine -> AI sentries -> correlation -> dashboard -> response,
  with a bracket over the first four reading "DETECTION -- no model, cannot fabricate" and a
  confidence-band legend (confirmed / strong / weak / unknown). The fleet (three machines with
  a lockdown control) sits alongside.

- **The Browser mirrors the Claude preview pane.** Chrome-style tabs with favicons, a clean
  toolbar (ghost back / forward / reload, a pill address bar with a lock and the real URL), a
  device viewport switcher (phone / tablet / desktop / fill) with a live size readout, a popout
  control, and a drag divider on the edge. It defaults to filling the window and reflows live
  when resized. The screen states the real architecture honestly: a BrowserView is a top-level
  context so X-Frame-Options never applies, which is why the old rewriting proxy (the real
  source of the proxy errors, finding #081) is gone on the Electron shell.

- **Settings** shows the model policy from the 2026-09-24 planning pass: large cloud models
  only, small/local marked blocked.

Honest limits, unchanged from #080: this is a prototype, wired to nothing, and is not the
live console. Wiring any of it into a real surface is separate work and waits behind the §0
foundation (MODEL-1, INFRA-1) in the tracking folder's `REMAINING_WORK.md`. A CSS block was
briefly lost to a bad edit anchor mid-session (the browser rendered unstyled), caught by a
live screenshot and fixed; recorded here because "the screenshot caught it, the assertion
did not" is the reason edits to this file assert their anchors.

### 084 -- the "flaky" tests were real bugs, the suite leaked into the real workspace, and 112 tests had never been in the gate

Status: DONE 2026-09-24. Phase 1 of the completion plan (items X-7 and X-4).

Both tests tracked as "load-dependent, not a regression" (X-7) turned out to hide real defects.
Chasing X-4 then found a whole test tree that the documented suite command never ran.

**1. Atlas leaderboard: a request lock held across a network git pull (product bug, HIGH for
the Atlas window).** `atlas_lab._sync()` ran `_ensure_repo()` (a `git pull`, 60s timeout, or a
`git clone`, 120s) while holding `_LAB_LOCK`, and every reader (`get_state()`, the leaderboard,
the whole Atlas API) takes that lock first. So any request arriving mid-sync blocked for the
entire git operation, contradicting `get_state()`'s own docstring ("the API must never block on a
git pull"). Under full-suite load the pull was slow enough to push the leaderboard request past
its 5s client timeout: that is the real X-7 failure. Fix: a separate `_SYNC_LOCK` serialises the
slow work (git, then parsing); `_LAB_LOCK` is now held only for the in-memory swap. Measured
against the real old code with a stubbed 3s pull: a reader waited **3.00s** before, **0.00s**
after. Regression tests: `TestSyncNeverBlocksReaders` (a reader returns in under 1s while git is
in flight; four concurrent syncs never run git at once).

**2. The Atlas test fixture let the suite do real network git (test hermeticity).** The autouse
fixture reset `_LAB_STATE` to None and nothing else, so every test that reached `get_state()`
started a real background `git pull` of the GitHub strategy repo into `/tmp/atlas-strategy-lab`
(which exists on this machine because of it), plus a real auto-sync daemon. That daemon was an
unstoppable `while True` that woke 300s later, after the test's stubs had been undone, and pulled
again mid-suite. Fix: `_auto_sync_loop` takes a stop `Event` and `stop_auto_sync()` exists; the
fixture stubs `_ensure_repo` for every test and stops any daemon at teardown. The loop-retry test
was rewritten to drive the real loop with its own stop signal and assert the thread actually ends.
A clean-shutdown hook is now available for X-3; it is not wired into server shutdown yet.

**3. activate_app_fast asserted the OS always obeys (test defect, no product change).** macOS 14+
cooperative activation lets the OS refuse an activation requested by a process that is not itself
frontmost. Live-checked from a background shell: the same call was refused once, accepted and
applied late once, and accepted but ignored once; the AppleScript path behaved the same, so an
AppleScript fallback would NOT have helped (and Finder is on the app-control blocklist anyway).
The product already reports this honestly: `ERROR` on refusal, and `_verify_activation_result`
checks the real frontmost state after an accepted request. The test now accepts exactly the two
honest outcomes and nothing else.

**4. A second test tree that was never in the gate (X-4).** A top-level `tests/` directory held
112 tests. The documented full-suite command, `pytest dourmouse/tests`, never ran it, so its
5 failures were invisible and 107 passing tests guarded nothing. It also had one autouse isolation
fixture where `dourmouse/tests/conftest.py` has twelve, so it ran against the developer's real
settings. The 5 failures: 2 read `GOOGLE_OAUTH_FULL_SCOPES=1` from the real persisted user config
file (the env var was deleted but `config.google_oauth_full_scopes_enabled()` also reads the
file); 2 were not isolated from the gitignored built-in OAuth client, so "no env" no longer meant
"unconfigured"; 1 asserted a deeplink redirect target from before v8.7 moved the hash router to
`/index.html`. Fix: the six files were `git mv`d into `dourmouse/tests` (two renamed to avoid
collisions: `test_desktop_bridge_and_launch.py`, `test_google_auth_flow.py`), where the full
conftest applies; the built-in-client isolation became a `no_builtin_oauth` fixture reusing the
existing `_remove_builtin_module` technique; the stale deeplink assertion was updated with the
reason. `tests/` is retired. All 112 pass, in both orders relative to `test_google_auth.py`.

**5. Every server test started a real security scanner against the Mac, and none ever stopped
(test hermeticity, found by the first full run of this commit).** That run stalled at 94% and
showed one failure. `ps` showed pytest spawning `arp -a` every few seconds. Root cause:
`sentry_runtime_enabled()` is default ON, and `run_server()` starts a real `SentryRuntime`, so
every test that built a server also started a daemon that shells out to arp, lsof and the
firewall tool on its 300s interval. `conftest.py` already turned the goal runtime off for exactly
this reason but not the sentry, so hundreds of scanners piled up over one run and loaded the
machine until timing-sensitive tests failed. Fix: autouse `_security_sentry_off` fixture, same
convention as `_goal_runtime_off`. Also a product fix found on the way: `serve_forever`'s
shutdown stopped every other background runtime but never the sentry; it now stops it too.

**6. Import-time store paths let the suite write into the real workspace (test hermeticity,
HIGH for data integrity).** `lsof` showed the pytest process holding the real
`workspace/security/sentry.db` open. Six modules computed their default path once at import:
`DEFAULT_DB = workspace_dir() / ...` in `office_logger`, `security/sentry`,
`research_pipeline/store`, `research_mesh/pipeline`, `device_wiki/store`, and `DEFAULT_CONFIG` in
`mcp_client`. They are first imported during test collection, before the autouse fixture points
`DOURMOUSE_WORKSPACE` at a tmp dir, so the frozen value was the real one. Evidence in the real
dev `workspace/office/office_log.db`: 9021 messages and 2902 agent events, dominated by fixture
output ("REAL NEWS: markets steady" 4076 times, "ECHOED: x" 640, "GATED-EXECUTED: secret" 40).
Fix: each module now has `default_db()` (or `default_config()`) resolved on every call; stores
take `path=None` and resolve at construction; every importer and test was moved to the function.
Regression tests in `test_workspace_paths_are_lazy.py`: each default follows a workspace changed
after import, and an AST guard fails if any module-level assignment in the package calls
`workspace_dir()` again. The polluted rows were left in place: that database also holds the
owner's real history and deleting from it is the owner's call.

**7. The one failure itself: a client timeout shorter than the server's own budget (test
defect).** `test_approve_over_real_http_makes_a_genuinely_working_extension` passes alone. The
approve endpoint really runs the draft's tests in a pytest subprocess with a 60s budget
(`self_extensions.py`), but the test's HTTP client gave up after 5s, so it failed whenever the
machine was busy (item 5 made it busy). The client timeout is now 90s.

Suite after all seven: **5565 passed, 10 skipped, 0 failed** in 727s (was 5442 on HEAD; +112 moved, +11 new).
No new ruff findings in any touched file (four import-spacing findings the refactor introduced were fixed).
Lint: `atlas_lab.py` ruff findings went from 15 to 14 (one S110 removed, zero added); mypy
unchanged at 79 repo-wide for that file's import graph.

### 085 -- CI existed but had never run on the code being built; lint had no gate

Status: IN PROGRESS 2026-09-24. Phase 1 of the completion plan (item X-1). This entry covers the
workflow rewrite and the lint ratchet; the first real runner results are recorded below as they land.

**1. The workflow never ran on the working branch.** `.github/workflows/tests.yml` triggered on
`main` and pull requests only. Every commit lands on `recon-2026-09-11`, so CI had never run on the
code actually being built, and its last runs (2026-08-13, on `main`) were red with nobody looking.
It also pinned Python 3.11 while development runs 3.14, and ran the whole suite twice (the second
run only grepped the first run's output). Rewritten: triggers on the working branch, `main`, pull
requests and manual dispatch; Python 3.14; a 3-OS matrix (Ubuntu, macOS, Windows, all free on a
public repo); one pytest run whose own exit code is the gate; stale runs cancelled.

**2. Lint ratchet.** `scripts/lint_ratchet.py` compares per-rule ruff counts and the mypy error
total with `scripts/lint_baseline.json` and fails if any number goes up. `--update` only tightens:
it refuses to write a higher number, so a count can only rise through a visible hand edit of the
JSON. Tool crashes exit 2 and are never read as "zero errors". mypy runs with `--platform linux`:
measured on this Mac, the unpinned total is 351 and the Linux-platform total is 352, so without the
pin the Mac and the CI runner would disagree. Baseline: ruff 447 across 29 rules, mypy 352.
Proven: lowering S110 and the mypy total by one in the baseline makes the check exit 1 with both
regressions named, and `--update` refuses.

Not done, owner's call: branch protection requiring these checks is a repository settings change.

**3. What the first real runs found.** Run 1: all three suites stopped at collection (`fastapi`
comes from `dell/requirements.txt`, which CI never installed); the ratchet saw mypy 353 on Linux
against 352 in a clean Mac venv built from the same requirements; checkout warned because
`atlas-strategy-lab` was a gitlink with no `.gitmodules` entry. Fixes: CI installs the dell
requirements; the ratchet prints every mypy error on a mypy regression, which named the extra one
(`google_auth.py` imported the gitignored `_builtin_oauth` statically, and the ignore comment
named the wrong error code; it now uses `importlib.import_module`, so both machines count 352);
`.gitmodules` declares the submodule at the commit already pinned. Run 2: the suites ran and
failed on real portability defects, recorded as findings #087 and #088, plus three undeclared
dependencies: `Pillow` is imported by `pdf_reader.py` and `tray.py` but no requirements file listed
it (it only arrived on the dev Mac through other installs), and CI did not install the desktop or
voice extras, whose macOS-only `pyobjc` line had no platform marker and so could not install on
Linux or Windows. Pillow is now declared, the marker added, CI installs both extras (plus
PortAudio on Linux for `sounddevice`).

### 086 -- fetch_url's SSRF guard could be walked around three ways

Status: DONE 2026-09-24. Phase 2 of the completion plan, first item (X-9, also tracked as R0-SEC).

`_refuse_private_fetch_target()` (finding #003's follow-up) resolved the host once with
`gethostbyname`, checked that one IPv4 answer against a list of ranges, then handed the URL to
`urllib.request.urlopen`. Three real holes:

**1. Redirects were never re-checked (HIGH).** `urlopen` follows redirects on its own. A public
page answering `302 Location: http://169.254.169.254/...` or `http://127.0.0.1:8765/...` reached
the internal address through a guard that had already passed. Demonstrated against a real local
server: plain `urlopen` followed the 302 and tried to connect to 169.254.169.254 (it timed out
only because this Mac has no metadata service; on a cloud host it would have answered).

**2. DNS rebinding between check and connect.** The connection resolved the name a second time,
so a name answering public for the check and internal for the connect got through. The old
docstring named this limit honestly; it is now closed.

**3. Incomplete address rule.** Only the first IPv4 answer was checked (a name returning one
public and one internal address, or an IPv6-only internal name, was not caught), and the range
list missed non-global space such as CGNAT 100.64.0.0/10.

Fix: new `dourmouse/net_guard.py`. `guarded_urlopen` builds an opener whose HTTP and HTTPS
connections resolve the host themselves, refuse the whole answer if any address fails
`is_public_address` (`is_global`, unicast, IPv4-mapped IPv6 judged by the IPv4 inside), and
connect to exactly the address they vetted; TLS still verifies the certificate against the
hostname. Every redirect hop goes through the same connection path, non-web redirect schemes are
refused, and the chain is capped at 5. Environment proxies are ignored, since through a proxy the
real destination cannot be vetted. An unresolvable name is now reported as the network failure it
is (`FETCH FAILED`), not as a refusal. `fetch_url` uses it; the old guard is removed.
`security/reputation.py` now uses the same `is_public_address` rule, so a CGNAT peer is no longer
sent to AbuseIPDB.

Also found, not changed: `browser_pane.fetch_and_rewrite_for_proxy` and `check_frameable` fetch
any URL with no guard. That pane is human-facing and may legitimately need LAN pages, so the right
policy belongs to the OS-3 browser work in Phase 4; recorded there.

Tests: `test_net_guard.py` (31): the address rule (CGNAT, metadata, mapped IPv6, public), every
answer checked, redirects to 169.254.169.254 / 10.0.0.1 / [::1] refused after a real first hop,
non-web scheme refused, chain cap, the connection pinned to the vetted address when the second
resolution would rebind, proxies ignored, and `fetch_url` end to end. Five existing tests moved
to the new seams.

### 087 -- the security sentry's device view went blind whenever reverse DNS was slow

Status: DONE 2026-09-24. Found by the full suite for #086: two `SentryRuntime` tests that passed an
hour earlier failed in isolation. Measured: `get_arp_neighbors()` took exactly 5.00s and returned
`available: False, "arp timed out after 5.0s"`. `arp -a` reverse-resolves every neighbour, and this
LAN has 206 entries, so whenever reverse DNS was slow the whole ARP view came back unavailable and
new-device detection saw nothing. It also sent one PTR query per LAN device on every scan.
`arp -an` answers in 0.1s. Fix: the adapter runs `arp -an`; hostnames are therefore always None
there (a display label only; devices are keyed by MAC), and `SentryStore.record_devices` now keeps
a hostname recorded earlier (`COALESCE`) instead of erasing it on a nameless sighting. Tests: the
command is numeric; a nameless sighting keeps the stored name. The security test files went from
27s to 6s.

### 088 -- Windows: the Claude backend could not run a first turn, plus six portability defects

Status: DONE 2026-09-24, pending the Windows CI job to confirm. Found by the first Windows CI run
(52 failures; 29 were the missing extras above).

**1. Every first Claude turn failed on Windows (HIGH for Windows users).** The task went to the
CLI as one argv element, and the first turn prepends the orchestrator preamble (16,508 chars, 104
lines). On Windows the npm-installed `claude` is a `.cmd` shim run through cmd.exe, which caps a
command line at 8191 characters and mangles newlines: CI reported "The command line is too long".
Linux also caps one argument at 128 KB, so a large pasted task would fail there too. Fix: the task
travels on stdin for `claude -p` (both the plain and the stream-json paths) and for `codex exec -`,
in `code_backends.py` and in the `claude_code`/`codex_code` tools. Verified live on this Mac with
the real CLIs: a new session and a resume both read stdin, stream-json too, and a first turn
through `run_code_task` (preamble included) answered for both Claude and Codex. This also retires a
live-caught v13 hazard where `--allowedTools` swallowed a trailing positional prompt.

**2. Text-mode subprocess output was decoded as cp1252 on Windows.** 31 product `subprocess` calls
used `text=True` with no encoding, so a UTF-8 character from a child (an em dash or emoji in a
Claude reply, JSON-RPC from an MCP server) kills the reader thread and the output comes back as
None. All 31 now pass `encoding="utf-8", errors="replace"` (no change on macOS or Linux, which are
UTF-8 already); an AST guard fails on any new text-mode call without an encoding. Two tests read
UTF-8 files the same way and were fixed.

**3. A second server could share a listening port on Windows.** `ThreadingHTTPServer` sets
SO_REUSEADDR, which on Windows lets another process bind a port already in use, so two servers
share it and requests land on either. This is a plausible mechanism for the "stale Dourmouse.exe
shadowing port 8765" seen on the desktop. New `http_server.DourmouseHTTPServer` binds with
SO_EXCLUSIVEADDRUSE on Windows (POSIX unchanged); all four servers use it; a guard forbids plain
construction and a test proves a second bind is refused.

**4. Project context went blank on Windows.** `project_import` merges records under a
`normpath`'d key, which on Windows rewrites separators, and `project_bookkeeper` then looked each
tool up by that key (codex's exact `WHERE cwd = ?`, Claude's sanitized directory name), finding
nothing. Merged projects now carry each tool's raw spelling and lookups use it.

**5. CLI discovery and PATH on Windows.** The known-directory fallback looked for a bare `claude`,
which is never runnable on Windows, and skipped npm's global shim folder; it now tries
`.exe`/`.cmd`/`.bat` and `%APPDATA%\npm`. `_cli_env` put POSIX system dirs on the child PATH,
which became junk like `D:\usr\bin`; those are now added off Windows only.

**6. POSIX-only tests.** Three tests model POSIX behaviour (a Dock-launched HOME, an extensionless
executable, a chmod-ed binary) and are now skipped on Windows with the reason stated.

Tests: `test_windows_portability.py` (encoding guard, stdin guard, extension names, npm shim found
outside PATH on the Windows runner, no POSIX dirs on the Windows PATH, double bind refused, server
construction guard); 29 CLI backend tests moved from argv inspection to what was sent on stdin.

### 089 -- research evidence was built from a truncated, mis-decoded copy that was then thrown away

Status: DONE 2026-09-24. Phase 2 of the completion plan: R0-6 (raw document cache), R0-4 (the
truncation and decoding defects) and R0-5 (final-URL provenance), done together because they share
one code path.

**What was wrong.** `extract_evidence` ran a nested LLM dispatch whose only job was to call
`fetch_url` on a URL it already had, then scraped the tool's text output. That output was built by
reading `max_chars * 2 + 4096` bytes before any parsing (so a page with a big `<head>` could yield
no body at all), decoding them as UTF-8 whatever the page declared (Latin-1 and Shift-JIS became
mojibake), regex-stripping anything including PDFs and images as if they were HTML, decoding four
HTML entities, and cutting at 8000 characters mid-word. Only that text was cached, keyed by the
requested URL, so `document_hash` fingerprinted text that matched no stored document and a cited
passage could never be re-read against the page it came from. The URL recorded was the requested
one even when a redirect served the content from somewhere else.

**Fix.** New `research_pipeline/acquire.py`. `fetch_document` fetches through `net_guard` (#086),
reads up to 10 MB (a bigger body is stored and marked `truncated`, never presented as whole),
classifies the content type (HTML, text, PDF through the existing pypdf path; anything else is
`UnsupportedContent`, and a PDF with no text layer is reported, not stored as its error message),
detects the charset from the header, then a BOM, then `<meta charset>`, then UTF-8 (an unknown
codec name is not trusted), and decodes every HTML entity. The raw bytes go into a
content-addressed `DocumentCache` (`raw/<ab>/<sha256>.bin`, atomic writes) with the fetch metadata
beside them (requested URL, final URL, redirect chain, status, content type, charset and where it
came from) and a per-URL index for both the requested and final URL. Text is always re-derived from
the stored bytes, and reading a blob verifies its hash. `net_guard.guarded_urlopen` now reports
the redirect chain.

`extract_evidence` fetches directly (no LLM needed to fetch a known URL), sets
`document_hash` to the SHA-256 of the stored raw bytes and records the new `Claim.final_url`; the
model sees up to 60,000 characters while passage validation still runs against the whole
document. `fetch_url` (chat) uses the same layer, always fresh, still stored: the shown text is cut
at a word boundary and the header says so ("showing N of M chars", "final URL ..."). Also fixed on
the way: `ResearchRecord.reject_claim` rebuilt the claim field by field and would have dropped any
new field; it now uses `dataclasses.replace`. Removed as dead: `_strip_html`,
`_extract_fetched_text`, `_FETCH_INSTRUCTIONS`.

Verified live: `http://github.com/` recorded as final `https://github.com/` with the chain, charset
from the header, all 576,667 bytes stored (the old read took about 20 KB before parsing).

CI dependency found in the same pass: `openwakeword` declares `tflite-runtime` on Linux, which has
no Python 3.14 wheel, so `requirements-voice.txt` could not install on Linux. Dourmouse uses only
its ONNX backend; the file now installs it off Linux, pins its real runtime deps, and documents
`pip install --no-deps openwakeword` for Linux (which CI does).

Tests: `test_research_acquire.py` (18, real local server: hash-addressed storage, cache reuse,
re-derivation, corruption detected, header/meta/BOM charset, all entities, big head, image
refused, text-less PDF refused, redirect chain and final URL indexed, truncation marked, word-safe
cut). The research pipeline tests moved from fake dispatch transcripts to the fetch seam, plus one
end-to-end test that the claim's hash names bytes really on disk after a redirect.

### 090 -- the privacy kill switch lived wherever the process happened to start

Status: DONE 2026-09-24. Found by the third Windows CI run, whose six remaining failures were all
tests; one of them was hiding a real defect.

`tray._state_path()`, the file holding the camera and microphone kill switch, defaulted to
`Path(os.environ.get("DOURMOUSE_WORKSPACE") or "workspace")`: a path relative to the current
directory of whichever process asked. `config.workspace_dir()` (the single source of truth added
precisely to stop path drift) resolves to the project's own `workspace/`. So a tray started from
one directory and a server or vision bridge started from another read different kill-switch files,
and switching the microphone off in one was invisible to the other; a packaged app started with
`/` as its directory would have tried `/workspace`. The same cwd-relative fallback was in
`live_feeds._tasks_path`, `world_watch_regions` and `world_pulse_history`. All four now use
`workspace_dir()`, and `tradingview_ops` (correct, but re-implemented) does too. On this Mac the dev
server runs from the repo root, so existing files stay where they are. A guard fails if any module
reintroduces `or "workspace")`. Five other modules re-implement the lookup correctly
(project_bookkeeper, spotify_services, orch_net, schedules, learn); left as they are.

Also in this pass: the Windows fake CLI in `test_code_backends.py` learned the argv-echo shape (three
tests), and the tray path tests compare `Path`s instead of POSIX strings.

Windows CI progress across the three runs: 52 failures, then 6, then these fixes.

### 091 -- research evidence included the site's own menu, and a claim's location was a guess

Status: DONE 2026-09-24. Phase 2, R0-1 (RES-7).

The HTML-to-text path stripped tags and kept everything else: navigation, site headers, footers,
sidebars, cookie banners, share bars, "related stories". So a claim's passage could come from a
menu, and `Claim.location` was whatever the extraction model wrote ("second paragraph"), because
the text carried no structure to check it against.

New `research_pipeline/extract_html.py`, stdlib only (no lxml or trafilatura, so no Python 3.14
wheel risk on the three CI operating systems). It parses the page into an element tree with HTML's
implicit end tags handled and stray end tags ignored; removes boilerplate: semantic elements
(`nav`, `aside`, site-level `header`/`footer`, `form`, `svg`, ...), ARIA landmark roles, hidden
elements (`hidden`, `aria-hidden`, `display:none`), and class/id names that say menu, cookie,
share, sidebar and so on; picks the main content (a marked `<article>`/`<main>` when present,
otherwise the container with the best readability-style paragraph score discounted by link
density, falling back to the whole body when the best container holds under a quarter of the
prose); and serialises blocks carrying their heading path. `ExtractedDocument.locate(passage)`
returns where a passage sits, e.g. "Guide > Setup, paragraph 1". `extract_evidence` now uses that
computed location, keeping the model's wording only when the passage cannot be placed.
`acquire.fetch_document` exposes the structure alongside the text; a page where extraction finds
nothing falls back to the plain tag strip.

Found live and fixed before landing: on bbc.com inline `<svg><title>` logos were appended to the
page title (only the document's first `<title>` counts now); on docs.python.org Sphinx's `¶`
heading anchors leaked into headings (`headerlink` added to the chrome names); on github.com a
page-wide wrapper `<div>` whose class contained `header-overlay` made the class heuristic remove
the entire page (a class/id match now never removes an element holding 40% or more of the page's
text; semantic tags and roles stay unconditional). Measured on real pages: Wikipedia's MCP article
20,392 characters of soup to 11,674 of article with its real headings; extraction of the 576 KB
GitHub page takes 0.03s.

Tests: `test_research_extract_html.py` (12), plus a pipeline test that the heading path replaces
the model's location.

Also from the fourth CI run (Linux now installs everything; 5515 passed, 9 failed, all tray): on
Linux `pystray` connects to the X display while importing, so on a headless machine
`tray._import_pystray()` let a raw `Xlib.error.DisplayNameError` escape instead of Dourmouse's
honest refusal. It now reports "NOT AVAILABLE: the system tray needs a desktop display (...)", with
a test; the Linux CI job runs the suite under `xvfb-run` so the real tray code is exercised there.
The same run's macOS job failed once in `test_auto_sync_loop_survives_failures` (the #084
rewrite): it slept a fixed 0.15s and expected two loop ticks, too short on a busy runner. It now
waits for the retry itself (up to 5s) before stopping the loop.

### 092 -- JavaScript-only pages were recorded as successful fetches of nothing

Status: DONE 2026-09-24. Phase 2, R0-2 (RES-8).

A single-page app answers a plain HTTP fetch with an empty shell (`<div id="root"></div>` and
scripts). The pipeline stored that as a successful fetch with no text, the dominant observed
failure mode on modern sites.

New `research_pipeline/render.py`. When a fetched HTML page has under 300 characters of extracted
text and carries scripts, `acquire.fetch_document` renders it in a short-lived headless Chrome
(Playwright, already a dependency; never the user's shared `browser_agent` page) and stores the
rendered DOM as its own document, marked `rendered` and linked by `rendered_from` to the sha of the
bytes the server actually sent, which are kept too. When rendering is switched off
(`DOURMOUSE_RESEARCH_RENDER=0`) or Chrome is unavailable, the static document is returned with a
`render_note` saying why, never silently.

Security design: the browser never touches the network itself. Every request the page makes (the
document, scripts, XHR/fetch, every redirect) is intercepted and fulfilled from Python through
`net_guard.guarded_urlopen`, so rendering has exactly the SSRF properties of `fetch_url` (#086).
Playwright's `route()` alone was not enough: it does not see redirects, so a page could have
bounced the browser to 169.254.169.254. Images, media, fonts and stylesheets are not fetched at
all.

Performance, measured live: docsify.js.org (which renders its markdown in the browser) came back
with its real content ("A magical documentation site generator", "What it is"). With the sync
Playwright API the 57 requests were served one at a time and the render took 41.9s; with async
route handlers fetching concurrently on worker threads it takes 7.9s. excalidraw.com also renders
(6.5s). Server-rendered sites (react.dev, vitejs.dev) are correctly not rendered.

Tests: `test_research_render.py` (5, real headless Chrome and a real local SPA whose script
fetches its content and also tries the metadata address: content kept, internal request refused,
server bytes kept under their own hash, static pages not re-rendered, switched-off rendering
recorded). They skip where Chrome cannot start.

Noticed, not changed: react.dev's extraction still leads with some header chrome ("Search ⌘ Ctrl K
/ Learn / Reference"), because that site marks it with neither `<nav>` nor a recognisable class.
Extraction quality tuning is a follow-on, measured against real pages.

### 093 -- the system tray crashed on Linux over one character

Status: DONE 2026-09-24. Found by the fifth CI run, the first to run the real tray code under a
virtual X display (added in #091). Five tray tests failed with
`UnicodeEncodeError: 'latin-1' codec can't encode character '—'`: pystray's X11 backend
encodes the icon title as Latin-1, and `TrayApp._title()` put an em dash in it
("DourMouse — mic on / cam on"). On any Linux desktop the tray, which carries the camera and
microphone kill switch, would crash at start or on the first state change. The title is now
"DourMouse: mic on / cam on" (ASCII). A test builds the real title for all four kill-switch
states and encodes each as Latin-1; verified to fail on the old title and pass on the new one.

CI state after run 5: macOS and Windows green, Linux 5533 passed with only these five failing.

### 094 -- automated fetches ignored robots.txt and hammered a host as fast as the loop ran

Status: DONE 2026-09-24. Phase 2, R0-3 (RES-9). This closes R0 (acquisition).

Twenty sources on one domain meant twenty requests back to back, and no fetch path read robots.txt.
Both get a research tool blocked, and honouring robots.txt is the courtesy an automated reader of
the public web owes.

New `research_pipeline/politeness.py`. `Politeness.wait_turn(url)` runs before every network fetch
in `acquire.fetch_document` (so both the research pipeline and the agents' `fetch_url`): it reads
the host's robots.txt once per hour through the SSRF guard with the stdlib parser, raises
`RobotsDisallowed` for a URL disallowed for our user agent (`dourmouse-research`), and spaces
requests to the same host by at least `DOURMOUSE_FETCH_MIN_INTERVAL` (default 1s) or the site's
`Crawl-delay` when longer, capped at 10s. Per the standard, a 4xx robots.txt allows everything and
a 5xx is a temporary full disallow; an unreachable robots.txt is not enforced (the page fetch
reports the real network problem). Cache hits wait for nothing and touch nothing. `fetch_url`
returns "REFUSED BY ROBOTS.TXT: ...". A conftest fixture gives every test a fresh gate, because
tests reuse 127.0.0.1 ports and a shared gate would carry one test's robots rules into the next.

Verified live: Wikipedia's robots.txt disallows `/w/` for crawlers, and a history URL under it is
now refused while its articles still fetch. Tests: `test_research_politeness.py` (9, real local
server: disallowed path refused and never requested, a rule for our own agent honoured, missing
robots allows, 5xx disallows, robots read once per host, spacing, capped Crawl-delay, cache hits
free, fetch_url's refusal text).

Also: the full suite for this finding failed once in
`test_local_model_concurrency.py::test_two_cloud_calls_run_concurrently_unaffected`, which passes
alone. It proved concurrency with overlapping call windows and then also asserted a wall-clock
upper bound (`elapsed < 0.35`), which a busy machine breaks without saying anything about
concurrency. The overlap assertion stays; the upper bound is gone (the serial test's lower bound
is kept, since load can only slow it down).

### 095 -- research lived in one JSON blob per question; now it is a versioned object graph

Status: DONE 2026-09-24 (R1 object model + R2 versioning + the migration). Phase 2.

`research_pipeline/store.py` persisted one row per question holding the whole `ResearchRecord` as a
JSON blob. A blob cannot answer "what evidence contradicts hypothesis H1", cannot be queried across
projects, and three of the spec's 21 research objects existed at all.

New package `dourmouse/research_graph/`:

- `model.py` (pure): the 21 objects of spec item 35, each declared IMMUTABLE (source, document,
  passage, evidence, dataset, metric, result, message, decision, artifact, event) or VERSIONED
  (project, research question, objective, hypothesis, claim, experiment, experiment run,
  contradiction, agent, task), with required and optional fields; an undeclared field is refused
  rather than silently stored. A closed relation vocabulary (supported_by, contradicted_by,
  tested_by, revised_by, derived_from, part_of, extracted_from, decomposes_into, answers, about,
  spawned, ...).
- `store.py`: one SQLite table per type keyed `(id, version)`; nothing is updated in place. A
  revision inserts version N+1 and marks N superseded; immutable objects refuse revision (spec
  item 36: "the original passage should not change"). `history()` and `as_of(when)` answer "what
  did we actually know when this conclusion was produced". A typed, append-only `edges` table
  with both ends checked. `related()` turns the spec's worked example ("H1 supported by E1 and E4,
  contradicted by E9, tested by X3, revised by D7") into a query, and the test does exactly that.
  `transaction()` runs a whole record's sync on one connection with one commit: all or nothing,
  and 39s down to 2.5s for the test file.
- `sync.py`: maps a `ResearchRecord` onto the graph with deterministic ids, so migration and
  ongoing sync are the same idempotent operation. A document is its bytes: its body holds only the
  hash (`raw_sha256` for claims made after #089; `legacy_text_hash` for older ones, since calling a
  stripped-text hash a raw hash would be false), and the URLs it was reached by are sources linked
  by edges, so the same bytes met through two URLs are one document. A later claim rejection
  becomes a new claim version.

`ResearchStore` now opens the graph in the same `research.db`, copies existing legacy records in
once (the legacy table is read only; a test proves it is byte-for-byte unchanged), and syncs the
graph after every save. The blob table stays until R3 moves the stages onto graph Tasks.

On the spec's "test against the live research.db": there is no `research.db` on this Mac (the
pipeline has never persisted a record here), and the desktop is not reachable from here. The
migration is therefore tested against databases written by the real current `ResearchStore` code,
checks every field of every claim, and runs automatically and non-destructively the first time any
existing database is opened, including the desktop's if it has one.

Tests: `test_research_graph.py` (24).

### 096 -- the research pipeline was a report generator: once it answered, nothing could change it

Status: DONE 2026-09-24. Phase 2, R3 (RES-19, "the backward edge").

The spec: "contradiction discovered, new research task, new experiment, new evidence, revised
synthesis. This is what turns the system into a research network rather than a report generator."
Verified in code: `set_synthesis()` moved the record to SYNTHESIZED and `add_claim()` raised from
there, so no evidence could ever enter after an answer existed. And the spec does not ask for
looser guards: the project only moves forward and the work grows.

- **Tasks** (`core.Task`). A contradiction spawns exactly one follow-up task
  (`spawn_task_for`, idempotent), at stage SOURCE_DISCOVERY, carrying the sub-question and the
  contradiction it came from. Evidence may enter after synthesis only as a claim tagged with an
  OPEN task (`Claim.task_id`); a closed or unknown task is refused.
- **Forward-only stages.** `add_sources` and `add_claim` used to set the stage unconditionally,
  so follow-up evidence would have dragged a synthesized record back to SOURCES_DISCOVERED; stages
  now only advance.
- **Revised synthesis.** Allowed from SYNTHESIZED once no follow-up task is open; every synthesis
  is kept in `synthesis_history` (records saved before this get their one synthesis as history).
- **Contradictions surfaced.** `synthesize()` never showed the model the recorded
  contradictions, although the core's own docstring promised they are "surfaced in synthesis rather
  than one side being silently dropped". The prompt now lists every known disagreement and asks for
  each to be stated with which side the evidence favours, or that it is unsettled.
- **No double counting.** `add_contradiction` is idempotent in either claim order, and
  `detect_contradictions` no longer re-asks the model about pairs already recorded, so a follow-up
  round costs only the new pairs.
- **The loop** (`stages.run_backward_edge`): spawn follow-ups, work each (discovery aimed at
  settling the disagreement: "find sources that settle it, primary sources, specifications", then
  evidence tagged with the task), close them, re-detect, write the revised synthesis. Bounded to 3
  follow-ups per call; with nothing unsettled it spends nothing. Exposed as the
  `research_follow_up` chat tool; `research_status` reports tasks and synthesis versions.
- **In the graph** (#095): follow-up tasks become task objects, the contradiction `spawned` its
  task, the task `produced` its claims, and every synthesis version is a result answering the
  question.
- Also: `store.py` serialised claims with a hand-listed field copy (the same pattern that nearly
  dropped `final_url` in `reject_claim`); it now uses the dataclass's own field list in both
  directions.

Tests: forward-only stages, task gating, one task per contradiction, order-independent
contradiction dedupe, revision gating and history, save/load round trip and old-record
compatibility, the full backward edge end to end (discovery prompt carries the disagreement, the
new claim carries the task, the synthesis prompt lists the disagreement, only new pairs are
re-judged), the no-op case costs no model call, the graph shows contradiction -> spawned task ->
produced claim with both results, and the new tool's honest outputs.

### 097 -- the device network: a node service for the desktop and the Dell, and the Mac's client

Status: service DONE and verified live on the desktop 2026-09-24; network deployment (firewall,
autostart, the Dell) pending the owner's machine-level steps. Owner decision the same day: the
desktop is the Python compute workspace, the Dell holds the data, the Mac orchestrates, every model
is cloud-hosted (REMAINING_WORK §3n, NET-1..4).

Found first: nothing was wired, and the existing Dell code (`dell/dell_server.py`,
`dell/compute_api.py`, and the Mac-side `remote_server.py`) was built for the opposite role,
serving a LOCAL `qwen3:1.7b`, which breaks both halves of the model policy. It is superseded, not
extended; retiring it is tracked separately.

New `dourmouse/nodes/node_server.py`: one standard-library-only file, so a node needs nothing but
Python (no pip, no Dourmouse checkout). Roles: **data** (blobs by SHA-256, verified on write and
on read; JSON metadata beside a blob) and **compute** (Python jobs, each in its own folder with
`in/` inputs fetched by hash from the data node, `out/` for artifacts and `metrics.json`, logs
tailed into the status). Security: a bearer token is required on every request and compared in
constant time (the old server's auth was optional), tokens under 32 characters are refused, and
binding to all interfaces is refused (a node binds to its Tailscale address). Jobs get a stripped
environment (no inherited secrets; a test proves a planted variable does not reach the job), a
wall-clock limit, and a memory limit: a Windows Job Object (process created suspended, capped,
then resumed, so it never runs a line uncapped) or RLIMIT_AS on Linux. macOS does not enforce
RLIMIT_AS, so a job there records "NOT enforced" rather than pretending. Artifact paths cannot
escape the job folder. It is process isolation, not a container, and the file says so: job code
must come from Dourmouse's own agents.

Found by the first test run: macOS refused `RLIMIT_AS` inside `preexec_fn`, the job thread died,
and every job reported "running" forever. Any failure in a job's thread is now recorded as that
job's failure. Also: 64-bit Windows `HANDLE`s would have been truncated by ctypes' default 32-bit
return type; the Win32 calls declare their types, and the Windows-only code sits under a
module-level platform check so Linux type-checking stays clean.

`dourmouse/nodes/client.py`: the registry (`<user config dir>/nodes.json`, never `.env` or the
repo), `NodeClient` (blobs, metadata, jobs, artifacts, all hash-verified on the Mac side too), and
`network_status()`, which reports an offline node as offline rather than caching "online". It
deliberately bypasses `net_guard`, since Tailscale addresses are exactly the non-public space the
guard refuses; only registry addresses are ever contacted.

Verified live on the desktop (Windows 10, Python 3.11.2, `D:\dourmouse-node`, bound to 127.0.0.1
for the check): a job ran and returned metrics; a job allocating 600 MB under a 128 MB cap was
stopped by the Job Object with a MemoryError before it could continue; a job past its time limit
was killed. Tests: `test_nodes.py` (19; the memory-kill test runs on the Linux and Windows CI
runners).

**Deployed on the desktop the same day, with the owner's go-ahead.** The owner added a Windows
firewall rule allowing TCP 8770 from 100.64.0.0/10 only, on both machines. Installed at
`D:\dourmouse-node\` (C: has 6 GB free): the one file, a config holding a fresh 48-character token
(ACL restricted to the user, SYSTEM and Administrators), and a scheduled task `\DOURMOUSE-Node`
that starts it at logon as the user, windowless, the same shape as the existing
`\DOURMOUSE-Desktop` task. The Mac's registry entry is in `~/Library/Application
Support/Dourmouse/nodes.json` (mode 600). Verified from the Mac over Tailscale: the desktop reported
online at 59 ms; a 2-million-sample Monte Carlo job ran under the Job Object memory limit and
returned its metrics and artifact in 1.1s round trip; no token and a wrong token both got 401;
the desktop's LAN address (192.168.1.242:8770) does not answer, only its Tailscale address.

The Dell is not deployed yet: its SSH server is not running (port 22 closed), so the Mac cannot
install anything there. The owner's SSH setup block is the remaining step.

### 098 -- every job records what it ran on; the Mac's job sandbox gets a real memory limit; Mac-only

Status: DONE 2026-09-24.

**Owner decision the same day: "scrap all plans for other device control, only for mac for
everything".** The desktop and the Dell are out of the plan (REMAINING_WORK §3n kept as history).
Dourmouse runs on this Mac alone, so the job runner from #097 becomes the Mac's local experiment
sandbox; its network parts are dormant. On the owner's earlier instruction, the old desktop
Dourmouse tasks were disabled (not deleted) and ollama stopped; the `\DOURMOUSE-Node` task there was
left running when the network dropped (the owner can disable it on the desktop). Tailscale is
dropped too: the owner saw internet problems with it, most likely because the Mac was routing all
its traffic through the Dell as an exit node.

**Environment hash (R5 prerequisite).** The spec's experiment record needs the environment a run
used. The job runner now probes its interpreter once, in the background from startup (listing
packages takes seconds and a health check must never wait on it; the first version did, and a
5-second health check timed out), records Python version, implementation, platform, machine and
the installed packages, and stamps a SHA-256 of that on every job. Live on the desktop before the
network dropped: CPython 3.11.2 on Windows 10 with 242 packages. That run also showed
`platform.machine()` empty, because Windows reads it from `PROCESSOR_ARCHITECTURE`, which the
stripped job environment dropped; that variable and `NUMBER_OF_PROCESSORS` (no secrets) are kept.

**Memory limit on macOS.** macOS refuses `RLIMIT_AS`, so on the Mac, now the only machine, jobs
had no memory cap at all (they said "NOT enforced", honestly). A resident-memory watchdog now
samples the job and its children every 250 ms through psutil (already a dependency) and kills them
past the limit, recording "memory limit exceeded". The honest limit of the method is in the code:
a burst faster than 250 ms can overshoot briefly before the kill. The memory test now runs on
macOS too (a job growing to 1.2 GB under a 128 MB cap is stopped).
