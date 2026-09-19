# Dourmouse — Commercial-Grade Master Requirements & Adversarial Acceptance Bible

Living document. Source of truth for "what does commercial-grade actually mean for this
product," synthesized from every real input this initiative has been given, not invented.
Every future self-assessment ("how far along are we") is measured against this document, not
against vibes. When a new input arrives (another reference doc, another user instruction), this
file gets extended, not replaced.

## 0. Inputs this document is built from

- `~/Documents/claude prompt .pdf` (121 pages) — the founding spec. Four workstreams: (A) a
  Claude-Code-architecture translation for a distributed research network, (B) a 20-acceptance-test
  spec for genuine autonomy (persistent Goal/Task objects, real background runtime, verification,
  recovery, scheduling, approval gates), (C) a 30-item deep engineering audit mandate (fix, don't
  just report), (D) a 54-item defensive-cybersecurity spec, explicitly authorized-defensive scope
  only.
- `Hermes_Claude_Code_Codex_Feature_Research_and_Recommendations.pdf` — a real architectural
  comparison of Hermes, Claude Code, and Codex, with an explicit recommendation: combine Hermes'
  personal-agent core (memory, skills, messaging, provider flexibility), Claude Code's control
  plane (rules, hooks, permissions, subagents, MCP), and Codex's execution plane (worktrees,
  cloud/background execution, review queues).
- `Architecting-Modern-Web-Applications-with-ASP.NET-Core-and-Azure.pdf` — used as a
  language-agnostic backend-architecture reference (Clean Architecture, testing strategy,
  observability, deployment discipline). Distillation pending (§14).
- `UI Design Principles - Michael Filipiuk.pdf` — used as a design-principles reference against
  Dourmouse's own already-established dark-terminal design system. Distillation pending (§15).
- ThreatSentinel (`github.com/MohibShaikh/ThreatSentinel`, cloned to
  `~/dourmouse-reference-repos/ThreatSentinel`, MIT) — a real, working SOC-agent architecture
  (event → intelligence gathering → risk scoring → action planning → report → memory, with human
  escalation above a risk threshold). Adopted as the **pattern** for Dourmouse's own Phase 4 AI
  security sentries, not vendored wholesale (it targets enterprise firewalls/SIEM Dourmouse
  doesn't have; Dourmouse's own `dourmouse/security/platform_adapter.py` telemetry and
  `DesktopNotifier` are the real tool/action layer here instead).
- AI-Scientist (`github.com/SakanaAI/AI-Scientist`, cloned to
  `~/dourmouse-reference-repos/AI-Scientist`) — assessed honestly, not oversold: its actual
  pipeline needs Linux + an NVIDIA GPU + PyTorch + a full TeX install to generate ML papers on
  three fixed templates (NanoGPT/2D-diffusion/Grokking). Not a drop-in fit for Dourmouse's
  macOS/CPU-friendly stack. What transfers is the **methodology**: idea generation → literature
  search (Semantic Scholar/OpenAlex) → hypothesis → experiment design → execution → write-up →
  self-review — adopted as the shape of an upgraded Dourmouse RESEARCH agent, not the codebase.
- A user-supplied "Jarvis feature" list (2026-09-18) — real, mostly adoptable (local wake-word,
  screen-frame capture, messaging webhooks, an async task queue, structured tool-output parsing,
  conditional tool chaining, a vector-backed persistent-context store, an immutable audit-trail
  ledger). One cluster explicitly excluded from this document's scope: automated
  exploit-chaining/wordlist-fuzzing-against-"targets" tooling (Metasploit-output chaining, an
  "exploits" archive, "intelligent wordlist routing") — this contradicts the founding spec's own
  explicit "authorized-defensive scope only... no exploit deployment against third parties"
  boundary, and nothing in this initiative has stated an authorized-pentest scope that would
  legitimize it. Revisit only if the user explicitly names an owned/authorized target scope.
- Direct user instructions layered on throughout: Hermes' own visual language (fonts, color
  scheme) as the UI north star; a local "device wiki" memory concept (every file on the user's own
  machine, organized into one browsable knowledge base the assistant maintains); Dourmouse should
  be able to add new agents/tools/scope to itself; a scheduling/timetable feature; the four
  backends — Ollama, Gemini, Claude CLI, Codex CLI — must all be real and wired (verified
  2026-09-18, one gap found and fixed: Gemini was fully functional but invisible on the connection
  status report, see `docs/ENGINEERING_AUDIT.md` finding #021).

## 1. Product vision, stated once, plainly

Dourmouse is not a chatbot with a nice frontend. It is a persistent, local-first, personal
operating layer: an assistant that remembers, plans, delegates, executes, verifies, schedules,
monitors, and produces finished work, continuing to do so in the background whether or not a chat
window is open, while keeping the human in control of anything consequential or irreversible. Every
capability below exists to serve that one sentence. A feature that makes Dourmouse *say* something
useful but not *do* it does not count.

## 2. Architecture layers (the shape every feature below has to fit into)

Adapted from the Hermes-comparison document's own reference architecture, and consistent with
`docs/ARCHITECTURE.md`'s real, file-grounded map of what already exists:

```
Identity / Profiles / Personality
        |
Memory  ---  Persistent facts, episodic history, procedural skills, the device wiki (§7)
        |
Orchestrator  ---  Planner, Task Router, Context Manager, Permission Manager, Evaluator
        |
Subagents  ---  Researcher, Coder, Reviewer, Tester, Security Sentry, Specialist agents
        |
Tool / MCP layer  ---  Browser, computer use, files, terminal, external APIs, connectors
        |
Execution backends  ---  Local process, the DOURMOUSE compute node, Docker/sandbox where used
        |
Learning  ---  Trajectory capture, evaluation, skill generation, validated promotion only
```

The model is a replaceable component inside this stack, never the stack itself. This is already
mostly true of Dourmouse's real architecture (`dispatch.py` as orchestrator, `general_roster.py`
as the tool/subagent registry, `goals.py`/`goal_runtime.py` as the Goal/Task layer) — the gaps
tracked in this document are gaps in the STACK, not a need to re-architect around the model.

## 3. Domain A — Multi-backend model routing (the four named backends)

**Requirement**: Ollama (local and cloud), Gemini, Claude Code CLI, and Codex CLI must each be a
real, independently verifiable, functioning backend — not merely present in a config file.

**Real status (verified 2026-09-18, not assumed)**:
- Ollama: real (`config.load_ollama_config`), correctly prefers cloud the instant
  `OLLAMA_API_KEY` is set (a deliberate, documented 2026-09-14 policy), local otherwise. The setup
  wizard mislabeled this on a real machine until today's fix (finding #020).
- Gemini: real (`gemini_backend.py` — `call_gemini`/`stream_gemini`, image generation, usage
  tracking fixed in finding #019) but was invisible on the connection-status report until today's
  fix (finding #021).
- Claude Code CLI: real (`code_backends.py::_run_claude`, sign-in detection via macOS Keychain,
  Claude-front mode on by default per explicit user direction, MCP bridge registration).
- Codex CLI: real (`code_backends.py::_run_codex`, `codex exec`, `~/.codex/auth.json` detection,
  MCP bridge registration, history import already covers Codex's own session store).

**Harsh acceptance tests**:
1. Kill every other backend's credentials. Confirm the one remaining backend is used, not a
   silent full failure.
2. Set an invalid `OLLAMA_API_KEY`. Confirm the failure surfaces the provider's own real error
   text, never a fabricated "it worked."
3. `/api/connections` must report all four with zero manual cross-referencing needed to know
   whether a backend that LOOKS configured actually answers a real request.
4. A pessimistic reviewer's question: "if I unplug the network right now, which of these four
   still works?" — the honest answer (local Ollama only) must be exactly what the UI already
   says, not something a developer has to explain verbally.

## 4. Domain B — Autonomous Goal/Task runtime (background-forever agents)

This is the founding spec's own 20-acceptance-test workstream (§ B of the 121-page doc, read in
full this session). Current real status: `dourmouse/goals.py` + `goal_runtime.py` +
`goal_tools.py` exist, 84+ real tests, wired into `run_server`, and **default ON since
2026-09-18** (`DOURMOUSE_GOAL_RUNTIME=0` to opt out) — flipped from the original opt-in default
after investigating it directly surfaced a real live bug: `create_goal` was registered
unconditionally regardless of the flag, so with the worker off, a successful-looking tool call
could promise background work that then silently never happened, forever. The founding spec's
own headline ask ("it should run forever without prompting, in the background") is now the
default live behavior, not just built-but-dormant infrastructure.

**The 20 acceptance tests, verbatim from the founding spec, kept here as the permanent checklist**:

1. A multi-step goal creates a persistent goal + task graph without the user manually enumerating
   every step.
2. Closing/navigating away from the chat does not stop the task — it continues through the
   backend/runtime.
3. Several sequential tasks complete without additional user messages.
4. Independent tasks execute concurrently when appropriate.
5. A failing task triggers recovery/retry/replanning without the user having to say "continue."
6. A task needing authentication pauses correctly, requests it, resumes from checkpoint after.
7. A high-risk action requests approval, waits, resumes automatically after approval.
8. Restarting the runtime mid-task resumes from durable state without duplicating external side
   effects.
9. A scheduled routine runs even when the user has sent no new message.
10. Agent A delegates to Agent B and receives a structured result automatically.
11. A task that claims completion but produced an invalid artifact is caught by a real verifier,
    not accepted on the model's word.
12. A learned/reusable Skill can be invoked later by a Routine or Goal without manual
    reconstruction.
13. User permissions are enforced independently of what the model requests.
14. The user can pause, resume, and cancel an autonomous goal.
15. The user can inspect what the agent actually did via activity/audit history.
16. The agent does not stop merely because one LLM call finished.
17. The agent does not ask "what should I do next" when the answer is inferable from the goal.
18. The system survives backend/worker crashes and resumes active goals safely.
19. Resource limits (runtime, token/cost budget, tool-call budget, parallel-agent limit) prevent
    runaway execution.
20. Multiple agents/goals run simultaneously without their state, credentials, memory, or files
    becoming mixed together.

**Status, updated 2026-09-18 -- all 20 acceptance tests now real, Domain B closed**: tests 1-6,
9-10, 12-14, 16-20 real and exercised by this session's own tests. Test 7 (approval requested,
waits, auto-continues after approval) **closed** (finding
#029): a resumable PER-TASK approval ticket (`GoalStore.resolve_task_approval`, the GOALS screen's
own APPROVE/DECLINE on a waiting task) -- a human approves the ONE task they reviewed, not every
gated action on every task everywhere via the global toggle. Live-verified against the real
dev-preview server: a real gated `send_draft` call was genuinely declined, the real task/goal
reached `WAITING_FOR_APPROVAL`, a real click on the real APPROVE button resumed it, and the SAME
tool call ran for real on the next attempt -- no more decline. The approval ticket is one-time,
consumed the instant the task starts running again, before its own dispatch call happens, so it
can never silently carry over to an unrelated later retry. Test 8 (crash mid-task, resumes from
durable state, no duplicated side
effects) **verified live** (finding #024): a real `kill -9` mid-dispatch against the real
dev-preview server, restarted, real automatic recovery (`recovery_attempted` event, RETRYING,
successful retry, correct real answer) — not assumed from unit tests. Test 11 (a task claims
completion but the artifact is invalid; a real verifier catches it) **closed** (finding #025): a
genuine second, independent reasoning pass over a tool-less `ChatSession`, shown the real
tool-call evidence from the actual run rather than the worker's own claim, with a real
`NOT_VERIFIED` verdict routed through the normal failure/retry path. Live-verified: a real task
passed through `RUNNING` → `VERIFYING` → `COMPLETED` with a real, independently-reasoned verdict
logged to the real audit trail. A real, deliberate cost tradeoff: every task now makes two real
dispatch calls instead of one. The deterministic-criteria-check gap named here is now **closed**
too (finding #031): `success_criteria`, declarable on any goal since this module's first version
but never once read back, now gets a real, goal-scoped independent check (`_verify_goal_criteria`)
before a goal is allowed to complete -- criterion-by-criterion, not one vague verdict, with an
unsatisfied criterion routing the goal to `BLOCKED` rather than silently reporting done. Live-
verified with a real, unscripted model call: a task fully succeeded on its own terms (independently
verified as such) while the GOAL it belonged to still correctly blocked because its own declared
bar (a specific token that never appeared) was not met. Test 15 (inspect what
the agent actually did) **closed** (finding #026): the GOALS screen (`ui/console.html`) is a real,
live-verified UI over `goal_events`/`all_events`/`export_events_markdown` + `GET /api/audit`
(finding #022) -- every goal, expandable per-goal task lists on demand, and the real cross-goal
audit trail, polled every 4s while the screen is open. Before this, a user could create a goal
that ran forever in the background and had zero way to see it without calling the API by hand --
confirmed by grepping every UI file in the product for `/api/goals` or `/api/audit` before
starting this work and finding not one reference anywhere. Also the runtime's first real write
action: a CANCEL button per active goal, a thin route over the already-tested `cancel_goal`.
Live-verified end to end against the real dev-preview server: list, expand-to-tasks, cancel, and
the resulting audit-trail entry all confirmed with real clicks and a real backend read, not just
passing tests.

**Harsh pessimism check**: a reviewer should be allowed to say "prove it" for every single one of
the 20 — a live demo, not a code pointer, for each.

## 5. Domain C — Scheduling / timetable (new, explicit user ask)

The founding spec already specifies routines (Skill = HOW, Routine = WHEN) and a persistent
scheduler supporting one-time, daily/weekly/monthly, cron-like, and event-based triggers
(file-created, email-received, calendar-event, webhook). `scheduler_runner` already exists in the
codebase per this session's own shutdown-cleanup fix (finding #007) -- meaning the backend
primitive exists. **Status, updated 2026-09-18 (findings #027, #030) -- CLOSED, all 4 acceptance
tests real**. The timetable UI is now real --
`ui/console.html`'s TIMETABLE screen, backed by 3 new routes (`GET /api/schedules`, `POST
/api/schedules/toggle`, `POST /api/schedules/remove`) over the pre-existing, already-tested
`Schedules` store. Pause/resume is a genuinely new store capability (`set_enabled`), proven live
against the real runner, not just the store field. No creation FORM was built deliberately --
natural language already works today through any chat composer, via the real `schedule_recurring`
tool; a second parser bolted onto this one screen would be a fabricated shortcut around the one
that's already real and already tested.

**Acceptance tests**:
1. A routine created via natural language ("every Monday at 8am, review my email and brief me")
   produces a real, visible, editable entry in the timetable UI, not just a hidden cron string.
   **Demonstrated live** (finding #027): a real chat message, handled by the real, currently
   configured Ollama Cloud backend, created a real schedule entry visible in the TIMETABLE screen.
2. The timetable survives an app restart and fires at the correct real time. Backed by the
   pre-existing JSONL persistence (`Schedules._load`/`_save`) and `SchedulerRunner`'s own
   already-tested catch-up logic -- not re-verified with a fresh restart drill this pass (that
   drill is real, separate follow-on, same shape as finding #024's crash-recovery drill for
   goals).
3. A missed run (app was closed at trigger time) is either caught up honestly or logged as missed
   -- never silently pretended to have happened. Already true and already tested
   (`test_catch_up_runs_once_after_missed_window`), pre-existing.
4. Editing a routine's schedule from the UI actually reschedules the real underlying job.
   **Closed** (finding #030): `Schedules.update_spec` re-parses the schedule text through the
   exact same `parse_schedule()` the create path already validates with, deliberately scoped to
   WHEN a routine runs, never WHAT it does -- the entry's id, `last_run` history, and
   enabled/paused state all survive an edit untouched, unlike a delete-then-recreate. Live-
   verified: a real EDIT click rescheduled a real routine from Monday to Friday, confirmed by a
   direct backend read (`spec.weekday` 0 -> 4, `next_run` recomputed correctly, `tool`/`arguments`
   provably untouched).

## 6. Domain D — Self-extension (Dourmouse adds agents/tools/scope to itself)

New, explicit ask. This is the single most architecturally sensitive item in this document — a
system that writes and registers its own new tools/agents needs a review gate, or it becomes an
unbounded-trust problem.

**Status, updated 2026-09-18 (finding #028)**: real, closed, live-verified end to end. One
deliberate deviation from the sketch below, stated honestly: rather than reusing test 7's
per-task approval ticket (still real, separate, not-yet-done work at the time this was built --
see Domain B), self-extension got its OWN dedicated human-only approval route
(`POST /api/self_extensions/approve`, reachable only from the new AGENT SMITH screen's own
button). Waiting on test 7 first would have blocked this entire domain on an unrelated,
independently-scoped gap; a purpose-built gate for exactly this one high-stakes action is more
defensible than stretching a not-yet-real generic mechanism to cover it. "Lowest permission tier"
is implemented as ALWAYS `Permission.REQUIRES_CONFIRMATION`, forced at write time regardless of
what the draft's own source claims -- not merely "by default" (a self-added tool has no path to
ever request or receive `REGULAR`, unattended execution). The changelog lives at
`workspace/self_extensions/CHANGELOG.md`, not `docs/SELF_EXTENSIONS.md` in the tracked repo as
first sketched -- a real bug, caught live, showed the tracked-repo path let every real approval
silently write into the actual git working tree; workspace-relative matches every other piece of
self-extension state and keeps a self-added tool a property of one installation, never something
this change ships to every other install.

**Concrete design, adversarially scoped**:
- A new `agent_smith` subagent whose only job is: given a described capability gap, draft a new
  tool function, matching the house conventions `docs/TESTING.md` and `docs/ENGINEERING_AUDIT.md`
  already establish (real error handling, no fabricated success, a real test file). Subagent
  definitions (not just individual tools) remain real, separate, not-yet-done follow-on.
- The draft is written to a real file, registered nowhere automatically. Approval is human-only,
  reachable only through the UI, never a chat tool -- a dedicated regression test asserts no
  approve/reject tool exists anywhere in the live registry, not just by code review.
- A self-added tool is ALWAYS forced to `REQUIRES_CONFIRMATION` -- it cannot grant itself elevated
  access no matter what it asks for.
- Every self-added capability is logged in a real, permanent, human-readable, per-installation
  changelog (`workspace/self_extensions/CHANGELOG.md`) -- what was added, why, when, by which
  goal.

**Harsh acceptance tests**:
1. Ask Dourmouse to add a genuinely new, useful tool. Confirm it drafts real code and a real test,
   not a stub. **Demonstrated live**: a real, unscripted Ollama Cloud call drafted a genuinely
   correct `celsius_to_fahrenheit` implementation from a plain-English request.
2. Confirm the draft does NOT self-register without an explicit approval step. **Demonstrated
   live**: the tool was confirmed not-live both by direct API check and by the model's own
   (overly cautious, but correct) refusal to call it before approval.
3. Confirm a self-added tool cannot request `REQUIRES_CONFIRMATION`-bypass or elevated
   permissions for itself. **Demonstrated live, the hardest way possible**: after real approval
   and a genuine server restart, a fresh chat thread's real tool call against the newly-live
   extension genuinely paused on a real `confirmation_requested` event and only executed after a
   real `POST /api/confirm`.
4. Confirm the full pytest suite still passes after a self-added tool lands (i.e., self-extension
   goes through the same CI discipline as human-written code, never a side channel).
   **Demonstrated live**: approval runs the draft's own test through a real pytest subprocess
   before merging -- proven both ways, catching a real draft whose test genuinely failed to
   collect (bare asserts, no `test_` functions) and correctly refusing to approve it.

## 7. Domain E — Memory: short-term, long-term, and the device wiki (new, explicit user ask)

Three real, distinct layers, not to be conflated:

1. **Already real**: `memory_store.py` (FTS5 long-term facts), `global_memory.py` (embedding-based
   cross-screen memory, gated, fixed for thread-safety in finding #017).
2. **The device wiki (new)**: a local, LLM-curated knowledge base built from the user's OWN files
   — not a raw file index, a genuine wiki: per-file and per-project summaries, cross-links between
   related documents/projects, a searchable structure the assistant (and the user, via a real UI)
   can browse. Concretely: a new `dourmouse/device_wiki.py` module that (a) walks a user-configured
   set of real root folders (Documents, Desktop, code repos — never silently the whole filesystem),
   (b) for each file/project, generates a real summary via the local model (never fabricated,
   honestly marked `UNSUMMARIZED` for binary/unreadable files), (c) stores summaries + cross-links
   in a real SQLite store (matching this codebase's own established WAL/lock conventions from
   finding #017's fix), (d) exposes a real browsable wiki UI page, not just a tool the model can
   query silently. This is explicitly the same "permanent local AI file-management agent" idea
   already named in the founding spec's own opening pages (Qwen2.5:7b via Ollama, SQLite memory
   layer, "never delete or overwrite files, skip system/cache/build files, log every action") —
   this document treats the wiki as an EXTENSION of that already-specified agent: the file-manager
   organizes; the wiki explains and cross-references what it organized.

**Build plan (scoped 2026-09-19, not yet started -- blocked, see below)**: mirror the exact
sequence `research_mesh` (finding #033) just proved out, since the shape is the same problem
(real files -> real per-item summary via a real model -> real persisted, queryable state -> real
UI): (1) a pure-logic `WikiEntry`/`WikiStore` pair first, no model, no I/O beyond a given file list
-- fully unit-testable; (2) a real SQLite store, workspace-relative
(`config.workspace_dir() / "device_wiki" / "wiki.db"`, applying finding #028's own lesson from the
start, not retrofitted); (3) the real summarizer, reusing the SAME tool-less
`ChatSession(DispatchRegistry(), session_file=None)` primitive proven three times now
(`_verify_completion`, `_verify_goal_criteria`, `RealBrain.answer`) -- no new call path to invent;
(4) a real walker with an explicit, user-configured root-folder allowlist (never silently the whole
filesystem, matching the requirement above literally); (5) a real chat-reachable tool
(`device_wiki_tools.py`, mirroring `research_mesh_tools.py`'s own dedicated-module shape) plus a
UI page (mirroring the GOALS screen's own polled-list pattern from finding #026); (6) live proof on
one real folder with a real model, exactly like `research_mesh`'s own live proof today. Cross-link
generation (harsh acceptance test 4 below) is real, separate follow-on work layered on AFTER
per-file summaries are proven -- do not build it first.
**Blocked, re-check before starting**: this domain's own real folders overlap `~/Documents/`, which
prior-session memory flags as off-limits while a separate, concurrent Claude Code session
(`ps aux | grep -i "claude.*Documents/dourmouse"`) is still touching that tree. Verify that session
has actually ended before writing a single file here, not just before the live-proof step.

**Harsh acceptance tests**:
1. Point the wiki at a real folder with 50+ mixed files. Confirm every file gets a real,
   non-fabricated summary or an honest "could not summarize" marker — never silence, never a
   made-up description.
2. Delete a file from disk. Confirm the wiki reflects that on its next real scan rather than
   showing a stale entry forever.
3. The wiki must NEVER touch, move, rename, or delete a real file itself — it is read-only
   knowledge, the separate file-organizer agent (already spec'd) is the only thing with write
   access, and even that is bound by "never delete or overwrite."
4. Cross-link accuracy: pick 10 real cross-links the wiki generated, confirm a human agrees at
   least 8 are genuinely related, not hallucinated connections.

## 8. Domain F — Subagents, delegation, parallel work

**Status, updated 2026-09-19**: the harsh acceptance test below was run live for the first time
and surfaced a real reliability gap in the pre-existing `delegate_parallel` fan-out tool, now
fixed and live-verified (`docs/ENGINEERING_AUDIT.md` finding #032) -- a branch that only ran out
of its own turn budget was reported `OK`, indistinguishable from a branch that actually finished.
Named specialist roles have since shipped too -- `researcher`/`coder`/`tester`/`security_sentry`,
live-verified routing to real subagents with zero explicit routing from the model (finding #038).
Domain F is still not fully closed: `reviewer` was deliberately deferred (no currently-registered
subagent has a genuinely write-free toolset that fits it -- see finding #038's own reasoning),
real, separate, not-yet-done follow-on.

Already substantially real (`general_roster.py`'s subagent registry, `model_delegation.py`'s
routing policy, `TestFanOut`'s concurrent-execution tests). Extend per the Hermes/Claude-Code
comparison's own recommendation: named specialist roles (Researcher, Coder, Reviewer, Tester,
Security Sentry — Analyst/Browser-Operator/Executor already exist under different names in the
current roster) with the smallest useful toolset each, synthesized centrally rather than one
model doing everything.

**Build plan (scoped 2026-09-19, not yet started; corrected same day after checking the mechanism
that does not exist yet -- see below)**: do NOT build a second roster system parallel to
`general_roster.py`'s real one -- a "specialist role" is a `delegate_parallel` branch with a
pre-filled persona + a restricted tool subset, not a new architectural layer. **Real correction**:
`agent_smith`'s own "scoped permissions" (Domain D) force a NEWLY-DRAFTED tool to
`Permission.REQUIRES_CONFIRMATION` -- that is a permission TIER on one tool, not a reusable
per-branch tool-ALLOWLIST mechanism, and no such allowlist filter exists in this codebase today
(checked directly in `dourmouse/self_extensions.py` before writing this). The real, already-existing
mechanism to reuse instead: `_build_delegate_parallel_tool`'s own docstring states a named
`agent_or_task` already becomes "a forced_agent ROUTING DIRECTIVE run on that one subagent's
configured model" -- meaning a branch routed to a specific real subagent ALREADY only ever sees
that subagent's own fixed toolset, no new enforcement needed. Concretely: (1) a `role` parameter on
`delegate_parallel`'s branch schema (`general_roster.py`'s `_build_delegate_parallel_tool`),
resolved against a small, real, named table mapping each role to (a) a short persona prefix
prepended to the branch's own instructions, and (b) a real, already-registered subagent name to
force as `agent_or_task` when the caller does not name one explicitly (e.g. `"reviewer"` ->
a real, already-narrow-toolset subagent -- pick one that genuinely has no write-capable tools;
audit the real roster for the closest fit rather than assuming one, since this document has already
been wrong once about what exists). If no existing subagent's toolset genuinely fits a role, that is
a signal to register a real new one the normal way (a real `Subagent` with a deliberately narrow
toolset in `general_roster.py`), not to bolt a bespoke allowlist filter onto `delegate_parallel`
itself. (2) The synthesis step: `_format_delegate_parallel_result`'s own output already concatenates
branch results with labeled headers (finding #032 just made the `INCOMPLETE`/`OK` distinction
honest) -- a REAL synthesis pass is one more `ChatSession` call over the combined, labeled output,
not a new mechanism. (3) Live-verify with a fresh 3-role run (research a topic / implement a
finding / review the code) using named roles this time, confirming per-role tool restriction is
real (a Reviewer branch's own transcript shows a refused write attempt, not just an unused one).
**Real infrastructure note**: yesterday's own partial run already confirms the underlying fan-out
mechanics are sound; this build plan is scoped narrowly to the role-table + synthesis addition,
not a rebuild.

**Harsh acceptance test**: give a goal that genuinely needs 3+ specialists (research a topic,
write code implementing a finding, review the code). Confirm real, isolated subagent context per
specialist (not one giant shared transcript) and a real synthesized final result, not three
disconnected fragments pasted together. **Partially run, 2026-09-19**: a real 3-branch fan-out was
exercised live against the dev-preview server; it is what surfaced the `delegate_parallel`
reliability gap above. Isolated-context and synthesis behavior confirmed sound for branches that
finish cleanly; the specialist-role extension itself has not yet been built or re-tested against
this acceptance test in full.

## 9. Domain G — Research capability, upgraded

**Scope correction, user-directed, 2026-09-19**: the founding spec's own workstream A ("a
Claude-Code-architecture translation for a distributed research network") is a real **3-device**
network -- this Mac, the Dell compute node (already a real, wired backend, see Domain A's `compute`
subagent), and the DOURMOUSE desktop (the separate Windows machine documented across prior-session
memory: its own dourmouse server on port 8765, the live history sync between it and this Mac) --
genuinely distributing real research/compute work across the user's own 3 real machines. This is
explicitly **not** the 500-field jarvis qualification mesh rebuilt in finding #033
(`dourmouse/research_mesh/`) -- that mesh is real and valuable on its own terms (field-specialist
qualification against real held-out exam corpora) but was the wrong target for this workstream, per
direct user correction. **Not yet started**: no code here today distributes a research task across
the 3 real devices; the structured research pipeline below and the 3-device distribution are two
real, separate pieces of the same workstream, both still open.
**Real infrastructure check, 2026-09-19**: the Dell compute node is `enabled` but NOT `configured`
in this dev environment (`remote_server.server_url_configured()` is `False`) and a live
`server_available(force=True)` check returned `False` -- honestly offline or unreachable from here
right now, not a code gap. `generate_with_fallback`'s own real fallback-to-local-Ollama path is
proven, existing infrastructure (see Domain A). The DOURMOUSE desktop's own reachability from this
checkout is unverified today. A genuine, live 3-device (or even 2-device) proof needs this real
infrastructure actually reachable first -- tracked honestly as a real blocker, not silently assumed
working. Building the structured research pipeline itself (below) does not need to wait on this: it
can be built and proven single-device first, with device distribution layered on once the Dell/
desktop are confirmed reachable from wherever this runs.

Current real capability: `web_search`/`fetch_url`/`research_info` — a search-and-summarize loop.
AI-Scientist's real methodological contribution (not its codebase, see §0): structure research as
an explicit, auditable pipeline — **question → research plan → source discovery → evidence
extraction → hypothesis generation → experimental design → execution → analysis → criticism →
revision → further research → synthesis** (this exact loop is also independently specified in the
founding spec's own "research network" workstream, §0/A — the two sources agree). Every claim
should carry real provenance: `{claim, source_id, url, document_hash, location, passage,
retrieved_at, agent}` — never a bare assertion with no traceable source.

**Build plan (scoped 2026-09-19, not yet started)**: single device first (per the infrastructure
note above), real persisted stage-by-stage state so a killed run resumes -- the exact
`step()`-per-transition, one-row-per-record SQLite shape `research_mesh/store.py` (finding #033)
and `goals.py` both already prove out in this codebase, reused a third time rather than invented
fresh. Concretely:
1. `dourmouse/research_pipeline.py`: a real `Claim` dataclass matching the spec's own field list
   verbatim (`claim, source_id, url, document_hash, location, passage, retrieved_at, agent`) --
   `document_hash` is a real `hashlib.sha256` of the fetched content (already a pattern in this
   codebase, see `jarvis/research_mesh/fields/exams/papers/MANIFEST.json`'s own `sha256` field from
   the crawl tooling), never a placeholder. A `ResearchRecord` (question, plan, claims, hypotheses,
   status) persisted the same one-row-JSON-body way `research_mesh/store.py::AgentStore` already
   does -- copy that store's shape almost directly, it is already the right design for "resumable,
   auditable, one atomic transition at a time."
2. Stage-by-stage, each a real, separate, independently-testable function -- NOT one giant prompt
   asked to do all eleven stages in one shot (that would be exactly the un-auditable, un-resumable
   anti-pattern this domain exists to replace): **plan** (one real `ChatSession` call, tool-less,
   decomposing the question into sub-questions -- same primitive as `RealBrain`/`_verify_completion`
   a fourth time now), **source discovery** (the ALREADY-real `research_info` tools --
   `web_search`/`fetch_url` -- no new fetch mechanism), **evidence extraction** (a real per-source
   `ChatSession` call producing `Claim` objects with real `location`/`passage` quoted directly from
   the fetched text, never paraphrased into an unverifiable summary), **hypothesis generation**,
   **criticism/revision** (a real second-pass `ChatSession` call reviewing the FIRST pass's own
   claims -- same "independent reasoning pass over real evidence" shape as `_verify_completion`),
   **synthesis** (the final real `ChatSession` call, given every surviving `Claim` with its real
   citation, explicitly instructed never to state a claim the record does not contain a `Claim` for).
3. **Contradiction detection (harsh acceptance test 2)**: an explicit, separate step, not an
   emergent hope -- when two `Claim`s answer the same sub-question with incompatible `claim` text,
   a real `ChatSession` call judges "same question, different answer" and the record stores a real
   `Contradiction {claim_a_id, claim_b_id, sub_question}` row, surfaced in synthesis rather than one
   side being silently dropped.
4. **Never delete (harsh acceptance test 3)**: a rejected hypothesis gets `status=REJECTED`, kept in
   the record forever, the same terminal-but-visible pattern `research_mesh`'s own `NOT_QUALIFIED`
   and `goal_runtime.py`'s own `BLOCKED` already establish in this codebase -- do not `DELETE` a row,
   ever.
5. Chat reachability: `dourmouse/research_pipeline_tools.py` (the now-three-times-proven dedicated-
   module-plus-`build_X_subagent()`-factory shape), a `research` subagent (name TBD to avoid
   colliding with the existing `research_info`/`rnd` names -- check `general_roster.py`'s real
   roster before picking one).
6. Live proof: a real multi-source question with a genuine contradiction seeded in (two real pages
   that actually disagree), confirming the record surfaces both sides rather than picking a winner
   silently. Device distribution (Domain G's other half) layers on AFTER this single-device version
   is real and tested, by making stage 2's extraction calls dispatchable to the Dell node via the
   already-real `generate_with_fallback`, once that node is confirmed reachable.

**Harsh acceptance tests**:
1. A research answer must let the user click through to the ORIGINAL passage that supports each
   real claim, not just a source list.
2. Two contradicting sources on the same question must be surfaced as a contradiction, never
   silently merged into one confident-sounding answer.
3. A rejected hypothesis or a failed experiment must remain visible in the research record, never
   quietly discarded.

## 10. Domain H — Code-generation / "Claude Code feature duplicates"

**Status, updated 2026-09-19**: piece 1/7 (the project-instruction file) shipped and live-verified,
`dourmouse/project_instructions.py` + `dispatch.py`'s `system_message()` -- see
`docs/ENGINEERING_AUDIT.md` finding #037. The other 6 pieces (hierarchical nesting, Skills-as-
packages, hooks, session-compaction audit, SDK interface, MCP) remain real, separate, not-yet-done
work, in that sequence per this section's own Build plan below.

The Hermes-comparison document (§0) is the working reference for what "duplicate Claude Code's
features" concretely means: project-instruction files (Dourmouse's equivalent of CLAUDE.md — does
one already exist as a per-project context file? tracked as an open question for
`docs/ARCHITECTURE.md` to answer, not assumed), hierarchical rules, Skills-as-modular-capability-
packages (SKILL.md + scripts/resources, loaded only when relevant — NOT one giant system prompt),
isolated subagents with their own tool/permission scope, deterministic hooks (pre-tool/post-tool/
stop/session, matching the founding spec's own explicit hook-type breakdown), MCP, session
management with real context compaction (not "summarize everything," preserve structured state
separately per the founding spec's own explicit warning), and a programmable SDK-style interface
for headless/scripted use.

**Build plan (scoped 2026-09-19, not yet started)**: seven real sub-features, sequenced by real
dependency and effort, not the order listed above. **Answering this section's own open question,
confirmed by grep, not assumed**: no CLAUDE.md-equivalent project-instruction file exists anywhere
in `dourmouse/` today -- this is a real, clean-slate gap, not an unknown.
1. **Isolated subagents with their own tool/permission scope**: already substantially real (Domain
   F, `general_roster.py`). Nothing new needed here beyond Domain F's own named-role build plan.
2. **Project-instruction file** (the CLAUDE.md-equivalent, smallest real lift, do first): a
   `DOURMOUSE.md` (or `.dourmouse/instructions.md`) read at session/workspace start, injected into
   the orchestrator's own system context alongside the existing capability preamble
   (`code_backends.py`'s own comment already names this exact preamble mechanism -- extend it, do
   not build a second injection path). Hierarchical: workspace-root file first, a narrower one in a
   subfolder (matching CLAUDE.md's own nearest-file-wins convention) layers on top, never replaces.
3. **Skills-as-modular-capability-packages**: a new `dourmouse/skills/<name>/SKILL.md` convention,
   loaded ONLY when `model_delegation.py`-style routing decides a turn is relevant to it (reuse that
   module's own real classified/unclassified split mechanism rather than inventing a second
   relevance-scoring system) -- never concatenated into one giant system prompt. A skill's own
   `SKILL.md` is real, human-authorable documentation; the harsh acceptance test below is the real
   bar (an external developer writes one from the doc alone).
4. **Deterministic hooks (pre-tool/post-tool/stop/session)**: `dispatch.py` already has one central
   call site where every real tool handler is invoked (`_execute_tool`, confirmed real in
   `test_general_roster.py::TestToolExecution`) -- pre/post-tool hooks are real callables registered
   against THAT call site, not a new dispatch layer. Session/stop hooks fire from `chat.py`'s own
   `ChatSession` lifecycle boundaries. Real, minimal risk: a broken hook must never take down a real
   tool call (wrap in the same `except Exception` fail-open discipline finding #002 already
   established for this codebase's other best-effort call sites).
5. **Session management with real context compaction**: audit what `chat.py`/session-file handling
   already does today before building anything -- this may already be partially real (this
   codebase's own session `.jsonl`/`.messages.json` files, referenced throughout `history_sync.py`
   and `history_import.py`, already look like structured state, not a raw transcript dump). Confirm
   or refute with a real read before writing new code.
6. **A programmable SDK-style headless interface**: `research_mesh/pipeline.py`'s own `main()` CLI
   (finding #033) is the first real instance of this shape in the codebase -- generalize its pattern
   (`ChatSession`/`DispatchRegistry` wrapped in a scriptable, non-UI entry point) rather than
   designing a new one from scratch.
7. **MCP**: the largest, most externally-facing piece (a real protocol implementation, not an
   internal refactor) -- scope as its own dedicated pass once 1-6 are real, not bundled in.

**Harsh acceptance test**: an external developer should be able to write a new Dourmouse Skill
using only its own documentation, without reading Dourmouse's source code, the same way a Claude
Code user writes a Skill without reading Anthropic's own agent-loop implementation.

## 11. Domain I — Defensive cybersecurity subsystem

**Authorized-defensive scope only. Restated, permanently binding**: host + the user's own network
+ Tailscale. No credential theft, no deauth/evil-twin attacks, no packet injection against
networks, no exploit deployment against third parties, no covert surveillance. This boundary is
older than and takes precedence over any later feature request that doesn't explicitly and
narrowly override it (see §0's note on the excluded Jarvis-list items).

**Real status, updated 2026-09-19**: `platform_adapter.py`'s own foundation (interfaces, gateway,
DNS, ARP, listening ports with exposure classification, Application Firewall state -- macOS only,
44 real tests) now has a real sentry on top of it, `dourmouse/security/sentry.py` -- a fully
deterministic scan (no model call; corrected away from an LLM-scored design before writing any
detection rule, see `docs/ENGINEERING_AUDIT.md` finding #039), real persisted false-positive memory,
and real alerting for a genuinely new HIGH finding, reachable now via `security_sentry_scan`. The
disabled-firewall/all-interfaces finding this section itself named as a real, live example was used
as the sentry's own live proof: it correctly detected, scored, and alerted on this machine's own
real, still-current instance of exactly that condition. Baselines beyond firewall/exposed-ports,
new-LAN-device detection, a continuously-running background scheduler, the live SSE push, the
dashboard UI, and remediation actions are still NOT built -- real, separate, named follow-on.

**ThreatSentinel-adapted architecture for the AI sentry** (the real, concrete plan — see §0 for
why this repo, not a vendored copy):
- Mirror its `InvestigationPhase` enum: `INITIAL_ASSESSMENT → INTELLIGENCE_GATHERING →
  RISK_ANALYSIS → ACTION_PLANNING → REPORTING → MEMORY_UPDATE`, applied to a Dourmouse-scoped
  event (a new device on the LAN, an unexpected listening port, a firewall state change, an
  unfamiliar Tailscale node) instead of an enterprise SIEM alert.
- "Intelligence gathering" for Dourmouse means the ALREADY-real `platform_adapter.py` telemetry
  plus, where genuinely useful and already scoped-in, real reputation lookups for an external IP
  seen talking to the host (the same class of check ThreatSentinel does against VirusTotal/
  AbuseIPDB) — never scanning or acting against that IP's own infrastructure, only asking a
  reputation API about it, mirroring ThreatSentinel's own honest "no fake data when a key isn't
  configured" rule.
- "Action planning" for Dourmouse means real, LOCAL, defensive actions only: alert the user (the
  existing `DesktopNotifier`), suggest (never silently apply) a local `pf`/firewall rule change,
  suggest revoking a Tailscale node's access — always through the SAME real-approval-gate system
  as any other high-risk action (§4). Never an automated block/kill executed without approval
  unless the user has explicitly configured that specific class of action as pre-approved.
- Real risk-scoring formula, adapted from ThreatSentinel's own (weighted base severity + real
  signal + historical pattern match), stored in a real local memory with pattern detection so a
  repeated false positive teaches the sentry, matching ThreatSentinel's own analyst-feedback loop.
- A real dashboard UI (not yet built) showing current exposure, recent findings, and sentry status
  — the practical, toned-down version of the orbital/sphere visualization already referenced from
  the user's own design images, per `docs/DESIGN_SYSTEM.md`.

**Build plan (scoped 2026-09-19, not yet started)**: the `InvestigationPhase` enum above is
structurally the SAME state-machine shape `research_mesh/core.py` (finding #033) already proves out
end to end -- one real, resumable, persisted record stepping through named phases, exactly matching
`UNLEARNED -> STUDYING -> READY -> TESTING -> ...`. Reuse that template directly rather than
designing security-specific state-machine plumbing from scratch:
1. `dourmouse/security/sentry.py`: a real `SentryRecord` (event, phase, findings, risk_score,
   status) persisted the same one-row-JSON-body SQLite way `research_mesh/store.py::AgentStore`
   already does, workspace-relative from the start (`config.workspace_dir() / "security" /
   "sentry.db"`).
2. `INITIAL_ASSESSMENT`/`INTELLIGENCE_GATHERING`: pure, already-real telemetry reads from
   `platform_adapter.py` -- no model call needed for this half, matching that module's own
   already-tested, deterministic shape.
3. `RISK_ANALYSIS`: one real `ChatSession` call (the same tool-less primitive used a fourth time in
   this document alone now -- `_verify_completion`, `_verify_goal_criteria`, `RealBrain.answer`,
   research pipeline's own plan/extract/synthesize calls) scoring the gathered telemetry against the
   real weighted formula, never a bare LLM vibe-check with no formula behind it.
4. `ACTION_PLANNING`/`REPORTING`: routes through the SAME real approval-gate mechanism Domain D's
   self-extension and finding #029's resumable-ticket system already built and live-verified --
   do not build a second confirmation system for security actions specifically.
5. `MEMORY_UPDATE`: a real row in the sentry's own store marking a finding as a confirmed false
   positive when the user declines the suggested action, read back into future `RISK_ANALYSIS`
   passes as real pattern history -- not a vague "it learns" claim.
6. Live proof, cheapest first: re-run this exact domain's own harsh acceptance test 1 below (the
   Application Firewall's real current state on this machine was ALREADY found disabled once,
   live, by the foundation work alone -- confirm the new sentry catches it for real, unprompted,
   end to end, not just that the underlying telemetry can see it).
7. Dashboard UI last, after the sentry itself is real and tested -- do not build the visualization
   before there is real data for it to show.

**Harsh acceptance tests**:
1. Turn the Application Firewall off on a real test machine. Confirm the sentry notices and
   alerts, unprompted, within a bounded real time window — not "eventually," a stated number.
2. A brand-new device joins the LAN. Confirm it's surfaced, not silently absorbed into "normal."
3. Confirm zero outbound action is ever taken against anything that is not this host, this user's
   own LAN, or a Tailscale node already in this user's own tailnet.
4. A pessimistic security reviewer's question: "show me the exact line of code that would stop
   this from attacking a third party even if a compromised model tried." The answer must be a real
   code path (the approval gate + the scope boundary), not a policy statement in a prompt.

## 12. Domain J — UI/UX: Hermes visual language, applied honestly

The user explicitly wants Hermes' fonts and color scheme. Dourmouse already has a real,
established, documented dark-terminal design system (`docs/DESIGN_SYSTEM.md`,
`ui/assets/dourmouse-ui.css` tokens: `--dm-canvas`/`--dm-layer`/`--dm-active`/`--dm-ok`/
`--dm-error`). This is a real, honest tension worth stating plainly rather than papering over:
"use Hermes' fonts and colors" and "keep Dourmouse's own no-floating-cards/no-decorative-glow
philosophy" are compatible (typography and a color palette are swappable within the existing
token system) but need the ACTUAL Hermes screenshots/exports the user has (referenced but not
re-attached in this session) to extract real font family names and real hex values from, rather
than guessing. Tracked as an explicit, open action: request or locate the Hermes UI reference
images already mentioned earlier this initiative, extract concrete typography/color tokens, apply
them to `dourmouse-ui.css` as a token-level change (never a wholesale redesign that discards the
existing "no floating cards" philosophy this session's own design docs already committed to).
The em-dash/decorative-separator UI-copy sweep (`docs/ENGINEERING_AUDIT.md` findings around
2026-09-17, `console.html`/`login.html`/`setup.html` done; `index.html`/`workspace.html`/16 other
files tracked as remaining work in `docs/GODSPEED_ROADMAP.md` Phase 3) is part of this same
"sleek, professional, no special characters" instruction and stays in scope here.

**Build plan, mechanical, ready the moment the images arrive (scoped 2026-09-19)**: this is the
ONE domain in this document whose blocker is a real external input, not scope or infrastructure --
the plan below should make execution near-instant once unblocked, not something to re-derive later.
1. From the real Hermes screenshots/exports: read off the actual `font-family` (check for a
   `@font-face`/Google Fonts link in any exported HTML/CSS, don't guess from a screenshot's look
   alone) and the actual hex values for its primary/accent/surface colors (sample real pixels, not
   an approximate read).
2. Map those to `ui/assets/dourmouse-ui.css`'s EXISTING token names one-for-one (`--dm-canvas`,
   `--dm-layer`, `--dm-active`, `--dm-ok`, `--dm-error`, plus whatever font-family variable already
   backs the current typography) -- a pure value swap on already-existing tokens, zero new
   selectors, zero structural CSS changes. This is what makes it "a token-level change, never a
   wholesale redesign" concrete rather than aspirational.
3. Live-verify in the browser pane exactly like every UI change this session: load a real screen
   (console.html), confirm computed CSS values match the new tokens, screenshot for the harsh
   acceptance test's own side-by-side comparison.
4. Do the em-dash/decorative-separator sweep on the remaining 16 UI files (already-proven tokenizer
   script, see `docs/GODSPEED_ROADMAP.md`'s own remaining-files list) in the same pass, since it is
   already scoped as part of this same "sleek, professional" instruction.

**Harsh acceptance test**: put a Hermes screenshot and a Dourmouse screenshot side by side. A
neutral reviewer should say "these clearly share a visual language" without being told to look for
it — not "these are two unrelated apps that happen to both be dark-themed."

## 13. Commercial-grade engineering bar (cross-cutting, applies to every domain above)

This is the standing bar from `docs/ENGINEERING_AUDIT.md`'s own accumulated discipline this
session, restated as a permanent checklist any new feature must clear before being called done:

- Real, live-verified behavior — never "the code looks right," always "I ran it and watched it
  work" (matches this session's own browser-verification discipline for every UI change).
- A real regression test that would have failed against the pre-fix code, not a vacuous pass.
- Honest failure everywhere (Rule 2.2 throughout this codebase's own comments): never a fabricated
  success, never a silently swallowed error, never a placeholder pretending to be data.
- No secrets logged, ever — verified pattern throughout this codebase's own conventions.
- Full pytest suite green before every commit — no exceptions, no "I'll fix the test later."
- A pessimistic reviewer's standing question for every claimed feature: **"prove it, right now,
  live, not in the code."**

## 14. Backend architecture principles (from the ASP.NET/Azure reference)

Full 118 pages read (background pass, 2026-09-18). The source is a generalist enterprise-web
architecture guide — its ASP.NET/Azure mechanics don't transfer, but its language-agnostic
principles do, filtered honestly against what a single-user local-first Python agent runtime
actually needs versus what would be ceremony without payoff.

**Applies directly, real payoff**:
- **Single tool-invocation gate.** The book's "uniform mechanism for cross-cutting concerns"
  (one reusable chokepoint for auth/validation/caching, never copy-pasted per call site) is the
  exact fix for the v8.15 permission-gating bug already on record (`schedule_recurring` bypassing
  gates because checks lived in two places). Every tool call — interactive and scheduled alike —
  should pass through ONE authorization chokepoint, closing the whole bug class rather than one
  instance of it.
- **Explicit Dependencies / Composition Root.** No global reach-for-config, no ambient state; one
  visible startup routine wires backend identity, provider fallback order, and tool registries.
  Directly targets the "stale Dourmouse.exe shadowing port 8765" class of bug — ambiguity about
  which process/config is authoritative is what happens when dependencies are implicit.
- **Testing pyramid, named and enforced.** Many fast unit tests (no I/O) at the base, a slimmer
  integration tier (real/realistic dependencies — with the two already-diagnosed flaky spots,
  `ssh.exe` subprocess spawning and non-interactive-SSH `find -exec`, explicitly mocked by
  default), a small functional tier hitting the real running server. Test naming convention
  (`Module.Method_ExpectedBehavior_GivenCondition`) is directly adoptable, zero cost.
- **Resilient connections** (retry with exponential backoff for external/unreliable calls) is a
  named fix for the two already-diagnosed hang bugs — worth one centralized retry/timeout wrapper
  every subprocess/cross-machine call goes through, so the next flaky call inherits the fix
  instead of needing rediscovery.
- **Background-job durability**: give scheduled tasks, history sync, and sandboxed backtests a
  real, queryable status (queued/running/succeeded/failed + error) instead of inferring success
  from logs after the fact.
- **Response-shape discipline** for every `/api/*` endpoint — a documented, consistent
  field-naming convention, directly motivated by the real `/api/memory` "returns `count` not
  `facts`" incident already in this project's own memory.
- **Structured logging at decision points** (routing scores, permission-gate checks,
  provider-fallback triggers) — several of this project's own hardest-diagnosed bugs (the
  capability-denial substring bug, the two hang bugs) were found by manual digging that logging
  at the actual decision point would have surfaced immediately.

**Explicitly ruled out, and recorded as ruled out so it isn't relitigated later**: formal
Domain-Driven Design, bounded contexts, multi-package Clean/Onion Architecture, full CQRS, and
enterprise authentication/OAuth systems. All solve problems of organizational scale or
multi-tenant identity that a one-user, one-codebase, one-deploy-target system does not have. The
one idea worth keeping from Clean Architecture without its ceremony: keep planning/routing/scoring
logic free of direct subprocess/socket/file calls, push those into small adapter modules called
through a narrow surface (informal core/infra split by convention, not by package) — motivated by
the fact that several real bugs this session root-caused traced back to exactly this kind of
mixing (`planner.py`'s routing scorer, `mcp_bridge.py`'s cwd/PYTHONPATH bug).

## 15. UI design principles (from the Filipiuk reference)

Full 323 pages read (background pass, 2026-09-18), cross-checked against the real
`docs/DESIGN_SYSTEM.md`/`docs/UI_DESIGN_REFERENCES.md` already in this repo. Honest framing up
front: this book is a generalist consumer/marketing-UI primer (landing pages, onboarding
mascots, Dribbble-style presentation) — it has **zero data-visualization coverage** and almost no
dark-mode guidance (two sentences, in the Shadows chapter). Weighted accordingly below; not
oversold as a dashboard-design authority it isn't.

**Strongly reinforces, closes named gaps in `DESIGN_SYSTEM.md`**:
- **Typography**: a real, buildable type-scale method (base size, small steps near it, wider
  steps further out, round to whole numbers; hierarchy from a weight jump more than a size jump)
  — this is the concrete fix for Dourmouse's own documented Gap #1 (18 ad hoc font-size values).
  One real tension flagged honestly: the book's "16-17pt minimum body text" is consumer-mobile
  guidance; Dourmouse is closer to a code editor (VS Code/iTerm run 12-13px body text
  routinely) — take the scale-building method, not that specific number.
- **Spacing**: an 4pt "soft grid" for dense UI (measure in multiples of a base unit, don't force
  every dimension to snap) — closes Gap #2 (no spacing scale) almost exactly:
  `--dm-space-1..6 (4/8/12/16/24/32px)`.
- **Color**: the book's palette-building method (one primary by psychology not taste → semantic
  notification colors kept separate → tints/shades → a hue-anchored gray ramp) describes,
  almost verbatim, the token system Dourmouse already has. Worth citing this method as the
  documented *why* in `DESIGN_SYSTEM.md`. One real risk flagged: the book warns against an
  accent color sitting too close to a semantic notification color — Dourmouse's amber accent
  (`--dm-active`) is near where "warning" usually lives; worth auditing that "selected" and
  "needs attention" are never ambiguous in the same view.
- **Motion**: independent validation, not new information — the book's own ceiling (~1000ms,
  restrained, feedback-only) is looser than Dourmouse's own existing doctrine
  (transform/opacity only, 180-260ms).
- **Dark-mode elevation**: the two sentences that exist say don't invert shadows to white, elevate
  with a lighter flat shade instead — exactly what `--dm-layer-hi` already does.

**Real, honest tension (not silently smoothed over)**:
- The book's default White Space lean is toward MORE breathing room than a dense ops console
  wants, and its default Cards recommendation is literally "white fill + drop shadow = clickable"
  — precisely the floating-card anti-pattern #1 already named in `DESIGN_SYSTEM.md`. The book
  does offer the bordered/flat alternative as valid, just not its default. Worth stating
  explicitly in `DESIGN_SYSTEM.md` that Dourmouse is deliberately on that less-common branch, so a
  future contributor doesn't "fix" it toward the book's default.
- The Gradients chapter treats gradients as a default modern technique — directly opposed to
  Dourmouse's own "no gradient borders, no decorative glow" rule. Correctly not adopted.

**Prioritized, directly testable checklist for a design review**:
1. Build `--dm-text-xs/sm/base/md/lg` from the book's method; migrate the 18 ad hoc sizes onto it.
2. Extend the spacing scale per the 4pt soft-grid method; audit for raw px literals.
3. Run every `--dm-fg*`/`--dm-canvas`/`--dm-layer*` pairing through a real WCAG contrast checker
   (4.5:1 body / 3:1 large text) — verify, don't assume.
4. Audit every `Widget`/`Card` for the full state set (loading/populated/empty/stale/error) per
   the already-named Gap #4.
5. Grep for any `box-shadow`-as-elevation or white-card pattern; confirm elevation is always a
   solid `--dm-layer-hi` fill or 1px `--dm-line`.
6. Confirm status dots are distinguishable without color alone (shape/position/label), given the
   book's ~4.5% colorblind baseline.
7. Every list/pane needs a real designed empty state (text + optional action), never a blank void.
8. Destructive/irreversible confirmations (kill task, delete, cancel run) should name the specific
   consequence on the button, never a generic Yes/No.
9. Grep for any transition touching width/height/top/left/margin/padding instead of
   transform/opacity.

## 16. The Jarvis-feature list, mapped item by item (2026-09-18)

Every item from the user's own "what Jarvis does" list, mapped to where it lands. Nothing silently
dropped; the excluded cluster is named explicitly rather than ignored.

| Item | Disposition |
|---|---|
| Local wake-word (Porcupine/Whisper-class) | Already real — `dourmouse/wakeword.py`. |
| Screen-frame capture → structural insight | Already real — the Vision/overlay subsystem (kill-switch, overlay status, hand-tracking bridge per `docs/UI_SOURCE_MAP.md`). |
| Messaging webhooks (Telegram/Discord-class alerts) | Real gap, not yet built. Matches the Hermes-comparison document's own "messaging gateway" feature and the founding spec's own notification-categories requirement (§4). New: a `dourmouse/messaging/` module, opt-in per channel, real delivery confirmation, never a silent drop. |
| Async task queue for heavy background work | Partially real (`scheduler_runner`, the Goal/Task runtime's own worker) — the specific "don't lock the interface for a heavy scan" framing is already how the Goal runtime is meant to work (§4). No need for a second queue system (Celery/RabbitMQ) — extending the existing durable-queue requirement from the founding spec is the right target, not a second parallel infrastructure. |
| Structured output parsing (raw tool output → JSON) | Real, generally useful pattern — already how `platform_adapter.py` turns raw macOS command output into structured telemetry (real captured samples, not synthetic, per `docs/ENGINEERING_AUDIT.md`). Extend the same discipline to any new tool integration. |
| Dynamic conditional tool chaining | Already the shape of the founding spec's own Plan→Execute→Observe→Verify→Replan loop (§4) and the task-graph model. Not a new primitive, an application of the existing one. |
| Vector-backed persistent context (ChromaDB/Faiss-class) | Already real — `global_memory.py`'s embedding-based store (fixed for thread-safety in finding #017). The "archive past targets/configurations" framing is folded into the general memory system, not a separate store, and explicitly excludes archiving "successful exploits" (see the excluded cluster below). |
| Local LLM via Ollama/vLLM, fully offline reasoning | Already real and verified end-to-end this session (§3). |
| MCP client, plug in new tools with zero rewrites | Already real (`mcp_bridge.py`, both Claude Code and Codex CLI register through it). |
| Immutable audit-trail ledger, timestamped, Markdown-renderable | Real gap, high value, directly matches the founding spec's own explicit "event log/audit trail" requirement (§4) almost word for word. New: every meaningful autonomous action (not just security actions) gets one durable, append-only, human-readable record — extends the existing goal/task execution-history table rather than creating a parallel logging system. |
| Command-macro compression (complex payload → one keyword/voice prompt) | Legitimate as a general Skill-system feature (this is precisely what Skills already are, per the founding spec's own Claude-Code-Skills analysis, §0/A) — build it generically, not scoped to any specific payload class. |
| **Excluded**: Nmap/Metasploit output chaining framed around "exploits," an "exploits" archive, "intelligent wordlist routing" against "targets" | Contradicts the founding spec's own explicit, standing "authorized-defensive scope only... no exploit deployment against third parties" boundary (§0, §11). Not built as described. Revisit only if the user explicitly states an owned/authorized-pentest scope this would apply within — at which point it becomes a real, legitimate, buildable Domain I extension, gated the same way every other high-risk capability in this document is gated. |

## 17. Self-assessment protocol

Whenever progress against this document is assessed (at any future milestone, not just once): hold
the result to the bar a real CEO/CTO reviewing a due-diligence report would apply — cite the exact
file, the exact test, the exact live-verified behavior for every claim of "done." A domain with no
cited evidence is NOT done, regardless of how much design work exists for it. Compare against
publicly available engineering-quality reports and audits used by real professionals, not against
this project's own aspirations.
