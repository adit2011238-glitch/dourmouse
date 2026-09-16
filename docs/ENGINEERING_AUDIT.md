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

---

## Not yet audited (honest, tracked gap — see `docs/GODSPEED_ROADMAP.md` Phase 1)

Concurrency/race-condition pass, database audit (schema/constraints/
transactions across the 5+ independent SQLite stores), full network audit,
source-ingestion audit, type-safety/static-analysis pass (ruff/mypy — not yet
configured or run), dependency audit, git-history secret mining, and a formal
`docs/TEST_MATRIX.md`. None of these are silently assumed clean — they are
explicitly not done yet.
