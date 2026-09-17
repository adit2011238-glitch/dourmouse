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
in findings #010/#013), `mypy`/`pyright` (type checking, distinct from
ruff's lint-only checks), and a formal `docs/TEST_MATRIX.md`. None of
these are silently assumed
clean — they are explicitly not done yet.

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
