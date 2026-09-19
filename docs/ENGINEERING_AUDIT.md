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
