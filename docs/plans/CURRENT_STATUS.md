# DOURMOUSE — CURRENT STATUS

**Last updated: 2026-09-27.** Against the latest commit (findings up to #160; see `git log`) on branch
`recon-2026-09-11`, pushed.

This document records what is **actually built and verified**. Every claim here is backed by
a specific file, a specific test, or a specifically observed live behavior, and is traceable
to a numbered finding in `dourmouse-recon/docs/ENGINEERING_AUDIT.md`. Where something is
partial, it says partial. Where something is not built, it lives in `REMAINING_WORK.md`
instead of being rounded up to done here.

---

*For a plain-English view of every capability, see `WHAT_IT_WILL_DO.md`. This document is the engineering-accurate status; that one is the reader's overview.*

**The app (2026-09-27, #159):** `~/Applications/Dourmouse.app` is the one pinned Dock app (a re-branded Electron clone that runs the live checkout; `scripts/install_app.sh` rebuilds it); old copies are in the Trash; see `APP_CLEANUP_2026-09-27.md`.

**Since the redesign shipped (2026-09-27, #155 to #158):** #155 a QA pass over all 18 screens; #156 the Command K launcher; #157 security round 4, batch 1 (env-file injection guard, setup routes need login, handler crash wrapper, SVG sandbox, Seatbelt deny for app code and start-up files, run_command cwd checks, Drive download confined, MCP env allow-list, claude CLI tool deny flag; ten items still open, listed in the finding); #158 the UX critique round (contrast, RESEARCH NEW QUESTION, Command shortcuts and help panel, dock, sidebar groups and icon rail, thread markdown with COPY, type scale, COMMS two-pane, TIMETABLE and VOICE honesty). Open UX items: `UX_ISSUES_2026-09-27.md` (56 of 71 still open, S3 setup and login restyle the biggest). Lint ratchet ruff 436 / mypy 346.

## 1. The numbers

| Measure | Value | How it was measured |
|---|---|---|
| Python modules (non-test) | 259 | `find dourmouse -name "*.py" -not -path "*/tests/*"` |
| Lines of Python (non-test) | ~94,500 | same set, `wc -l` |
| Test files | 298 | `ls dourmouse/tests/test_*.py` |
| Tests passing | **6,944 passed, 12 skipped, 0 failed** | full run 2026-09-27, 728s, findings #084-#158 |
| Registered subagents | 49 | `register_subagent` call sites in `general_roster.py` |
| Numbered engineering findings | 160 (#001 to #160) | `docs/ENGINEERING_AUDIT.md` |
| Roadmap items closed / open | 76 done, 22 open | `docs/GODSPEED_ROADMAP.md` checkboxes |
| Largest single files | `webui.py` 7,276 · `general_roster.py` 6,178 · `dispatch.py` 5,168 · `console.html` 7,092 | `docs/ARCHITECTURE.md`, `docs/UI_SOURCE_MAP.md` |

**On the test count, stated precisely.** The 2026-09-23 full run was clean: 5,346 passed,
10 skipped, zero failures. Prior sessions recorded 5 pre-existing failures in
`tests/test_google_auth.py` and `tests/test_deeplink.py`, attributed to real `.env` leakage on
this machine and confirmed pre-existing via `git stash`. Those tests are environment-sensitive
by their own documentation (`docs/TESTING.md` describes exactly this bug class). The honest
reading of a clean run is that the leak was not present in that shell, **not** that the
underlying isolation bug is fixed. Item X-4 in `REMAINING_WORK.md` stays open.

The 10 skips are all real, checked environmental preconditions, never silenced flaky tests:
Node not on PATH, Ollama not reachable, `sandbox-exec` unavailable, wakeword model files absent.

**One test is focus-dependent and will fail if you use the Mac during a run.**
`test_app_control_ax.py::TestRealLiveIntegration::test_activate_app_fast_really_works_against_
finder` genuinely brings Finder to the foreground, and macOS refuses that while another app
holds focus (`activateWithOptions_ returned false`). Observed for real on 2026-09-23 during a
run where a browser was being driven on the same machine; it passes in isolation both with and
without that day's changes. Tracked as item X-7.

## 2. Domain-by-domain status

The ten capability domains are defined in `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`.

### Domain A — Multi-backend model routing — **DONE**

All four backends (local/cloud Ollama, Gemini, Claude Code CLI, Codex CLI) are real and
independently verifiable. Killing every other backend's credentials leaves the remaining one
working; an invalid key surfaces the provider's real error rather than a fabricated success.
`/api/connections` reports all four honestly. A real cross-cutting routing bug was found and
fixed in the process: a stale, locality-untagged persisted model choice was sending a
local-only Ollama model name to Ollama Cloud and 404ing on every non-escalated turn
(finding #044).

### Domain B — Autonomous Goal/Task runtime — **DONE**

All 20 of the founding spec's own acceptance tests for genuine autonomy are closed:
persistent goal/task graphs, background execution that survives closing the chat, concurrent
independent tasks, failure recovery without the user saying "continue," pause/resume for
approval, crash recovery from durable state (live-verified with a real `kill -9`), scheduled
routines, delegation, independent verification of claimed completion (a second reasoning pass
checks the model's own claims rather than accepting them), per-goal audit history in the UI,
and resource limits. This is the single most load-bearing subsystem in the product and it is
genuinely finished.

### Domain C — Scheduling / timetable — **DONE**

All 4 acceptance tests closed: natural-language routine creation, persistence across
restarts, honest handling of a missed run, and live editing of an existing schedule
(verified: a real edit moved a routine from Monday to Friday without disturbing its history).

### Domain D — Self-extension — **DONE**

Dourmouse can draft new tools for itself and cannot register them without a human clicking
approve. Live-verified end to end including the hard case: after approval and a real server
restart, a fresh chat's call to the new tool still correctly paused for confirmation. A
self-drafted tool is permanently forced to the most restrictive permission tier and has no
path to ever grant itself more.

### Domain E — Memory and the device wiki — **DONE** (build plan steps 1-6, shipped 2026-09-21)

Two pre-existing memory systems (SQLite+FTS5 `memory_store`, embedding-based `global_memory`)
plus the new device wiki, built to completion this month:

- `device_wiki/core.py` — `WikiEntry` state machine (UNSUMMARIZED / SUMMARIZED / MISSING) and
  `reconcile()`. A real deletion is marked MISSING, never dropped. A real content change
  reverts to UNSUMMARIZED rather than keeping a stale summary. A reappearing MISSING file
  self-heals from its preserved summary. (Finding #056.)
- `device_wiki/store.py` — real SQLite store, one row per real file, workspace-relative DB path.
- `device_wiki/stages.py` — the summarizer, reusing the tool-less `ChatSession` primitive.
  A binary or unreadable file never reaches the model, enforced in code.
- `device_wiki/walker.py` — explicit `DOURMOUSE_WIKI_ROOTS` allowlist, never a
  filesystem-wide fallback. Proven end to end through three real temp-directory scans.
- `device_wiki_tools.py` — the `device_wiki` subagent: scan / status / get.
- `GET /api/device_wiki` plus a real WIKI screen in `ui/console.html`, polled every 5s.

Live-verified against two real local files with two real accurate summaries. A real
duplicate-summarize-call bug was caught before any test ran and fixed. (Findings #057-#061.)
Cross-link generation between related documents is real, separate follow-on, not built.

### Domain F — Subagents and delegation — **DONE** except one deliberate deferral

Named specialist roles (`researcher`, `coder`, `tester`, `security_sentry`) route correctly
to real subagents with zero explicit routing from the model, live-verified. A real
reliability gap in parallel fan-out (a branch that ran out of turns was silently reporting
"OK") was found and fixed. `delegate_parallel` runs up to 6 branches genuinely concurrently
with real per-thread isolation.

**The one open gap:** a `reviewer` role was deliberately deferred because no currently
registered subagent has a genuinely write-free toolset, and a stern prompt on a
write-capable one is not an enforced restriction. Registering a real narrow read-only
subagent first is the remaining work. (Finding #038.)

### Domain G — The research network — **UPDATED 2026-09-26: the 20-25% figure below is from 2026-09-23 and is out of date**

Since that date the research network gained: the acquisition layer (R0, findings #086-#094: SSRF
guard, real main-content extraction, headless-render fallback, per-domain rate limits and robots,
final URL, raw document cache), the versioned 21-object graph with an immutable evidence chain
(R1+R2, #095), the backward edge so a contradiction spawns follow-up work and a revised answer is
written (R3, #096), hypotheses from real claims with a critic, and experiments designed and run on
this Mac with the code, run, numbers and environment recorded and re-runnable (R4, R5, R8, RES-18,
#127-#132), an append-only research event log and the console RESEARCH view (R6, #128-#130), a
runtime that records every proposed action and bounds a lead agent's authority (R7, #133), and the
answer critic (R9, #141, lexical, never run against a live model). What is NOT done: the new OS
shell's RESEARCH screen (redesign builder E, not started); the answer critic on a live model; the
3-device network (retired: Mac only). The 2026-09-23 text below is kept for the record; the DONE
entries in `REMAINING_WORK.md` are the current truth.

*Original 2026-09-23 text:* **ROUGHLY 20-25% BUILT**

**Two items corrected 2026-09-23 by reading the code, both previously mis-recorded as open.**
RES-1 (workspace-relative default store path) is done: `DEFAULT_DB` exists in
`research_pipeline/store.py`. RES-3 (contradiction detection) is done and well built:
`detect_contradictions()` groups active claims by sub-question and judges each pair with one
tool-less model call, costing nothing when a group has fewer than two claims.

**Owner decision 2026-09-23: build the FULL version**, not the load-bearing subset. All 14
stages, all 21 objects, experiments as reproducible jobs, event sourcing, bounded lead
authority, the research view. Sequenced in `REMAINING_WORK.md` §3g, with the acquisition layer
it depends on in §3h. Honest sizing: this is now the largest single body of work remaining,
larger than Domain I.

**The blocking architectural fact:** `store.py` persists one row per question with the whole
record as a single JSON blob, which cannot express a queryable 21-object graph. The full
version replaces that persistence layer with a migration rather than extending it.

**Corrected 2026-09-23.** This was previously recorded here as "CORE DONE", which was accurate
about the module and misleading about the domain. The founding spec devotes items 34 to 48 to
the research network, the largest single architectural section in it. Measured against that:
**4 of 14 stages, 3 of 21 first-class objects, 0 of 4 architectural demands.** Full breakdown
in `REMAINING_WORK.md` §3.

**What is genuinely real and works**, and it is good work: a real data model
(`Claim` / `Contradiction` / `ResearchRecord` with a stage state machine), persisted storage,
and four real stage functions. `plan` decomposes a question into real sub-questions.
`discover_sources` does real web search plus fetch. `extract_evidence` fetches a real source
and extracts a claim **whose supporting quote is verified as an exact substring of the real
page text**, never trusted on the model's word. `synthesize` writes a final answer built only
from surviving real claims. Two real cross-cutting bugs were found and fixed building it.

`research_pipeline/core.py`'s own `Stage` docstring is honest about the gap and explicitly
refuses to fold hypothesis generation, experimental design or the criticism pass into
`SYNTHESIZED`. The module never overclaimed; the tracking document did.

**What is missing is structural, not cosmetic:**
- **The backward edge.** The spec's loop must run backwards (synthesis finds a contradiction,
  spawning a new task, experiment, evidence, revised synthesis) and says so explicitly: *"This
  is what turns the system into a research network rather than a report generator."* Current
  guards enforce strictly forward transitions, so the state machine cannot express it at all.
- **The object model.** 3 of 21 objects, and no relational graph linking hypotheses to the
  evidence supporting or contradicting them, the experiments testing them, or the decisions
  revising them. The spec: *"much more powerful than a vector database alone."*
- **Immutable evidence.** `SOURCE -> DOCUMENT -> PASSAGE -> EVIDENCE RECORD` does not exist,
  and no raw scraped page content is written to disk anywhere, so a cited passage can never be
  re-read. That single gap blocks the whole chain.
- **"The LLM proposes, the platform executes."** No bounded lead authority, no event sourcing,
  no research view over an event stream, no critic agents.
- **The 3-device network** (Mac orchestrates, desktop is the lab, Dell fetches) is blocked on
  the Dell node being unreachable.

**Three different things share this name**: `research_pipeline/` (808 lines, the above),
`research_mesh/` (1,337 lines, a separate study/exam subsystem), and `jarvis/` (2.2GB, the
500-agent PhD network mirrored from the desktop).

### Domain H — Claude Code platform feature parity — **DONE, 7 of 7** (closed 2026-09-20)

1. Isolated subagents with tool/permission scope — already real via Domain F.
2. `DOURMOUSE.md` project-instruction file — shipped, finding #037.
3. **Skills** — `dourmouse/skills.py`. A `dourmouse/skills/<name>/SKILL.md` convention, loaded
   only when a turn's text overlaps a skill's declared keywords (deterministic whole-token
   matching, reusing `planner.find_agents_for_query`'s own discipline). Spliced into
   `ChatSession.ask()` as a trailing system message. Infrastructure only — no actual skill
   content ships yet under `dourmouse/skills/`. (Finding #070.)
4. **Hooks** — `dourmouse/hooks.py`. Five registries: pre-tool, post-tool, stop, session-start,
   session-stop. Pre-tool hooks can genuinely block (a non-empty return string is a real
   denial and short-circuits); every other kind is a pure observer that swallows exceptions
   and can never break dispatch. Wired at `_execute_tool` and `ChatSession`'s lifecycle.
   Named limitation: `webui.py`'s long-lived server session never calls `close()`, so process
   exit is its real end today. (Finding #068.)
5. **Session compaction** — audited, no new code needed. `dispatch.py`'s pre-existing
   `_bounded_context()` already does real structured compaction (drop, never lossy
   summarization), alongside already-real JSONL and `.messages.json` persistence. (Finding #069.)
6. **SDK** — `dourmouse/sdk.py`. A `Dourmouse` facade (`ask()` / `close()` / context manager)
   plus `python -m dourmouse.sdk "prompt" --json` for scripting, separate from the interactive
   REPL. (Finding #071.)
7. **MCP** — both halves now real. The server half (`mcp_bridge.py`, stdio JSON-RPC 2.0,
   exposing every non-PROHIBITED tool except the delegate primitives) pre-existed. The client
   half is new: `dourmouse/mcp_client.py`, the same `mcpServers` config shape Claude Code's own
   `.mcp.json` uses, external tools wrapped as `mcp__<server>__<tool>` under an opt-in
   `mcp_tools` subagent. A real end-to-end test connects a real `McpClient` to a real
   `dourmouse.mcp_bridge` subprocess — proven interop, not two isolated mocks. Named
   limitation: `_readline`'s `timeout` is not enforced on the real subprocess path.
   (Finding #072.)

### Domain I — Defensive cybersecurity — **FOUNDATION ONLY. Largest remaining domain.**

What exists is real and correctly engineered: a fully deterministic scan with no model call
anywhere in the detection path (a deliberate design correction made before any detection rule
was written), persisted findings with false-positive memory, and a continuous runtime that
starts automatically at boot and completes a real scan before any chat interaction. It
correctly detects a real condition on this machine (Application Firewall disabled) end to
end, unprompted.

A real SECURITY dashboard screen also shipped 2026-09-21 (`GET /api/security_dashboard`): an
SVG circular risk gauge, severity and incident bars, known-device count, and a real findings
list, reading the server's already-computed `SentryRuntime.last_result` rather than
triggering a fresh scan. Live-verified against this machine's own real risk score of 21.0
and 4 real findings. (Finding #062.)

Also real, and previously MIS-RECORDED as not built (corrected 2026-09-23 by reading the
code, commit `e4b8584`): a LAN device inventory (`known_devices`), incident/case tracking with
a real lifecycle and a terminal-state guard (`incidents`), one deterministic correlation rule
(new device plus newly exposed service in the same scan), AbuseIPDB reputation enrichment that
is honestly inert without a key, and recommended-action remediation text.

**Recounted honestly 2026-09-23: 3 of the 54 spec items are genuinely real (16, 26, plus
reputation enrichment), 7 are partial (4, 9, 17, 18, 28, 30, 31), and 44 are untouched.** The
earlier "8 of 54" counted partials as whole. The layer ABOVE the detectors is real; the
telemetry UNDERNEATH it is thin. See `REMAINING_WORK.md` §2 (not §3, an old mis-reference).

**A second XL body of work now sits alongside it:** `REMAINING_WORK.md` §2c, the owner's
fleet endpoint monitoring and remote lockdown spec (2026-09-23). Mostly new rather than a
restatement of §2b, and it does not deliver the baseline engine or any network telemetry.

### Domain J — UI/UX, Hermes visual language — **PARTIAL**

**Reference design approved 2026-09-24 (finding #083).** `ui/os_mockup.html` (mirrored to the
tracking folder's `MOCKUPS/`) is the owner-approved OS design: a real OS shell (window chrome,
Control Centre, Notification Centre, live accent theming, wallpapers), Research and Security as
flowcharts, and a Claude-preview-style resizable Browser. It is a prototype wired to nothing;
building the live shell to match it waits behind the §0 foundation.

Real pixel colors were extracted programmatically from the Hermes reference screenshot (not
eyeballed) and applied as a token-level retone to the primary screen, live-verified. A real
architectural issue was found in the process: the shared design-token file and the primary
screen's own theme block were two separate duplicated systems carrying the same colors under
different names. Both are retoned; **the other 15 UI files that link the shared stylesheet have
not been individually checked** for the same local-override problem (UI-1).

**The type and spacing scales now exist** (2026-09-23, UI-2, finding #079). Before this, colour
was fully tokenized while seventeen distinct font sizes ran 7.5px to 22px in half-pixel
increments with no scale, and nothing governed section spacing against ~720 raw px literals.
`ui/assets/dourmouse-ui.css` now carries an 8-step type scale and a 6-step spacing scale, both
derived from real usage rather than invented, with the rejected alternative (a 1.25 major-third
scale, too coarse for this dense UI) recorded and pinned by tests.

**A real Figma design system mirrors them**: `Dourmouse Design System`, file key
`zHH3ZLx5MZHfAltHOGkYBk`. 43 variables across Primitives / Color / Scale, semantic colours
aliased to primitives rather than duplicated, explicit scopes on every variable, and
`var(--dm-*)` code syntax so Dev Mode round-trips to the real stylesheet. Discovery found 8
subscribed community kits (Material 3, Simple Design System, Apple platform kits) and none were
reused: their token models are incompatible and contradict this product's documented philosophy.
Figma Phases 2 and 3 (documentation pages, component library) are **not** done.

**One instance of developer commentary rendered to users was found and fixed**: the
hand-control panel showed internal constant names, "see page source", and MediaPipe GPU
internals. Spotted in a screenshot of the running app, not by grep. The other screens have not
been swept the same way (UI-9).

**The UI typeface is now a clean sans, not a pixel font** (2026-09-23, finding #082).
`--dm-font-sans` was Departure Mono, a pixel face, for every label and every line of body copy
in the product. Space Grotesk was already self-hosted (nine files, three weights) with no
`@font-face` declaration at all, so it could never load; `UI_SOURCE_MAP.md` had predicted that
exact orphan. Space Grotesk now carries UI text and Monaspace Neon leads the mono stack for
data, both already on disk so offline-first is untouched. Verified from the browser rather than
the source. `console.html` and `workspace.html` keep their own duplicated font declarations
until UI-1 resolves that duplication.

**An OS shell design layer now exists** (2026-09-23, finding #080). `ui/assets/dourmouse-os.css`,
601 lines across thirteen sections: materials, elevation, geometry, motion, wallpaper, window
chrome, dock, widgets, menu bar, accessibility, 3D, controls, annotation mode. It resolves a real
conflict rather than ignoring it: `dourmouse-ui.css` forbids floating cards, translucent wash and
decorative glow by name, and macOS is built on the first three. Those rules stay correct for the
dense tool surface, so nothing here touches the terminal feed; the OS layer keeps their intent
(depth must mean something, translucency only as a real material over a real wallpaper, glow
still banned, the ~10% accent budget unchanged) and drops only the letter.

`ui/os_mockup.html` is an interactive prototype of all 20 real screens with one custom icon each,
a wallpaper picker including real photo upload, and an annotation mode where every control labels
itself with what it does. **None of it is wired into a live surface** — that is the approval gate.

**Still open**: migrating the ~720 px literals and seventeen font-size call sites onto the new
scales (UI-8, deliberately not done as a blind sweep), the 15-file retone audit, the contrast
pass, the missing components (`DiffWidget` highest value), the icon system, and the keyboard
accessibility pass.

## 3. The agent ecosystem (the vision deck's "corporate workspace")

This was hardened across five rounds on 2026-09-20 after a design review found five real
flaws. **All five are now fixed.**

1. **Message impersonation — FIXED.** `send_message`'s `from_agent` used to come straight
   from the model's tool-call arguments, checked only against "is this a real roster name."
   Any agent, or the untargeted top-level orchestrator turn, could forge a message as
   `security` or `orchestrator`. Now the real caller identity is read off
   `current_dispatch_context(registry).forced_agent` (the same hard scoping the delegate
   primitives already use) and a mismatch is refused loudly, never silently overridden.
   (Finding #064.)
2. **No notification path — FIXED.** A direct message used to sit until something explicitly
   called `read_agent_inbox`. `message_bus.on_post` is now wired to a new `"agent"` alert kind
   on the real existing `StateStore.add_alert` / SSE `state_change` path that the desktop
   app's `DesktopNotifier` already watches. Filtered to direct messages only — a broadcast is
   routine data-plane traffic and is never surfaced as urgent. (Finding #065.)
3. **Bus was in-memory only — FIXED.** New `office_logger.py`: an append-only,
   workspace-relative SQLite store with `messages`, `fanout_events` and `agent_events` tables,
   wired onto the same `message_bus.on_post` hook and the same chat `event_sink` that
   `ActivityTracker` already consumes, plus a read-only `GET /api/office_log`. (Findings #066, #067.)
4. **ActivityTracker status collision — FIXED.** Two concurrent `delegate_task` calls to the
   same agent used to overwrite each other's live status. `DispatchContext` now carries a
   real fresh-per-run `call_id`; `_emit_event` additively tags every reasoning and tool event
   with the real calling agent and that `call_id` (never overwriting a fan-out branch entry's
   own correct agent). `ActivityTracker._record` only applies a `tool_result` to a slot when
   it genuinely belongs to the call occupying it — a superseded call's result still reaches
   the feed, it just no longer corrupts the snapshot. New `concurrent_call_ids(agent)` and a
   matching `GET /api/activity` field. (Findings #067, #073.)
5. **Local backend concurrency ceiling — FIXED.** A real, live-observed HTTP 400 from two
   simultaneous local Ollama calls. A process-wide semaphore now gates the single real
   network-call boundary whenever `backend_identity(config)` says the call is local — fully
   serial by default, every other backend unaffected, raisable via
   `DOURMOUSE_LOCAL_MODEL_MAX_CONCURRENT`. Live-proved with real threads and real wall-clock
   timing: local calls never overlap and take additive time, cloud calls do overlap and take
   roughly one call's time. Named limitation: this bounds local concurrency, it does not add
   cloud burst capacity. (Finding #074.)

**Also shipped:** the agent ecosystem visual monitor (finding #063). A persistent SVG scene
patched in place — transform, color and text set on existing nodes, never rebuilt via
innerHTML — so CSS transitions genuinely animate a sprite walking between its desk and a real
meeting-room seat, driven only by real fan-out and agent-state changes, never a timer. All 43
real subagents render as persistent desks. Three real `delegate_parallel` fan-outs driven
through the actual chat composer all dispatched and completed. Honest gap recorded: every
real fan-out in this environment finished in under a second, so the walk animation itself was
code-reviewed but never caught mid-flight in a screenshot.

**Still open from that review:** the read-side transcript-assembly UI — merging several
concurrent `call_id`s from one "meeting" into a single conversation view. See
`REMAINING_WORK.md` §5.

## 4. Browser, PDF and media — the "replace every app" surface (audited 2026-09-22, finding #075)

Audited honestly, no code changed. Real state:

- **Browser automation is real, not a stub.** `browser_agent.py` drives real locally-installed
  Chrome through Playwright (`channel="chrome"`) — real DOM fill, click, submit. One shared
  browser context per process, reused across tool calls.
- **A real in-page browser pane exists.** `console.html`'s `<iframe id="bpFrame">` with real
  nav chrome (back, forward, reload, address bar, resizable, minimizable), backed by
  `browser_pane.py`'s real `check_frameable()` reading actual `X-Frame-Options` and CSP, plus
  a real server-side rewriting proxy fallback for sites that block framing.
  **Named limitation:** the proxy fallback carries no live cookies or session, so a site
  requiring login will not behave like a logged-in session inside the pane. The iframe sandbox
  deliberately omits `allow-same-origin`, which narrows what can work embedded.
- **A second, faster embed exists but is not the shipped default.** `electron/main.js` wires a
  real Chromium `BrowserView` connected over CDP to the same live Playwright session the
  automation tools drive. It self-reports NOT CONFIGURED unless launched through the Electron
  shell, and every scripted launch path still boots the older pywebview/WKWebView shell. A
  real half-migrated state, not a decision announced anywhere.
- **PDF has a real page-image viewer.** `pdf_reader.py` renders real page PNGs through
  `pypdfium2` (the same engine Chrome's own PDF viewer uses) plus real Tesseract OCR for
  scanned pages. Surfaced two ways: the dedicated PDF READER panel in `ui/workspace.html`, and
  `ui/file_preview.html` loaded into the shared browser pane. A separate backend-only
  `extract_pdf` (pypdf, text-only) feeds RAG ingestion, correctly kept distinct.
- **The browser pane's proxy errors have a proven cause and a shipped fix** (2026-09-23,
  finding #081). The pane is an `<iframe>`, so any site sending `X-Frame-Options` or CSP
  `frame-ancestors` refuses to load, which is most of the real web. The rewriting proxy that
  exists as the fallback is where the errors come from: imperfect URL rewriting, no cookies (a
  logged-in site looks logged out), and responses it cannot always parse. **The fix is not a
  better proxy, it is not needing one**: those headers restrict FRAMING, and an Electron
  `BrowserView` is a top-level browsing context, not a frame. Proven with a controlled
  experiment rather than asserted: the same local page serving `X-Frame-Options: DENY` plus
  `frame-ancestors 'none'` came back BLANK in an iframe (`ERR_BLOCKED_BY_RESPONSE`) and LOADED
  in a BrowserView, same Chromium, same process. The pane bridge gained `POST /navigate` plus
  `/back`, `/forward`, `/reload`, with `file://` and `javascript:` refused by name and verified
  refused. **Remaining and not claimed:** `console.html`'s pane still sets `iframe.src` rather
  than calling `/navigate`, so the proxy path is what a user hits today. The pywebview shell has
  no BrowserView and keeps every limitation named in finding #075.

- **Embedded audio and video playback is now real** (2026-09-23, OS-1, finding #076). Before
  this, the only `<audio>`/`<video>` elements in any UI file were TTS output and a webcam
  gesture feed. `GET /api/files/media` streams media from disk in 256KB chunks with real HTTP
  byte-range support (206 with `Content-Range`, `Accept-Ranges`, a real 416 for an
  unsatisfiable range) — a requirement rather than a refinement, since without it a `<video>`
  element cannot seek at all. `ui/file_preview.html` renders real `<audio>`/`<video>` with
  native controls, plus an honest terminal state naming `open_path` for anything a browser
  cannot decode. `.mkv`, `.avi`, `.flac` and `.wmv` are deliberately absent from the allowlist
  on both the server and tool sides, so they refuse rather than showing a blank player.
  Live-verified: byte-exact ranges against a real 1.4MB MP4 (a mid-file range's md5 matches
  `dd` of the same offsets), a real H.264 file decoded with a parsed video track and a real
  frame drawn, a seek landing exactly on target (which cannot happen without a working 206),
  and real AAC audio with native transport. Path handling was security-checked directly:
  traversal, non-media absolute paths and `/dev/zero` are refused, and the sandbox resolves
  symlinks before checking the extension, so a `.mp4`-named symlink to `/etc/passwd` is refused
  while one pointing at real media is served. **Two things are NOT verified and are not
  claimed:** wall-clock playback (`play()` resolves but `currentTime` does not advance in this
  headless browser, which has no audio sink) and rendering inside the pane's sandboxed iframe
  (blocked outright with `ERR_BLOCKED_BY_CLIENT`, reproduced identically on a pre-existing image
  path). Both need the real desktop shell. Spotify remains **remote-control only** (it drives the user's own separate
  Connect device and never sends audio bytes here), and **there is still no YouTube
  integration at all**.

## 4b. Models and backends - **corrected against the live config 2026-09-24**

**Owner model policy (hard rule):** large cloud models only, never local, never under 14B.

**What actually runs** (read from the real `.env`, names only): the only keys set are
`OLLAMA_API_KEY`, `OLLAMA_API_KEY_2` and `GEMINI_API_KEY`. NVIDIA is absent, so `config.py`'s
`_NVIDIA_AGENT_DEFAULTS` table is dead. The fleet routes to **Ollama Cloud** (`ollama.com/v1`,
default `gpt-oss:20b`). A second real large-cloud route exists and is proven for coding agents:
the **`claude_cli` backend** (`claude-sonnet-5 (CLI)`), which shells to the `claude` CLI the
owner is signed into.

**FIXED 2026-09-24 (MODEL-1 applied):** the orchestrator was pinned to `qwen2.5:7b`, a sub-14B model that could not reliably call tools (the X-6 root cause). Repointed to `gpt-oss:120b` (Ollama Cloud), verified through the app's config and a real authenticated call; on the exact X-6 scenario the new brain emitted the correct tool call where qwen 404'd on the cloud.md`.

**Are the agents real?** Yes: each of the 49 runs a real separate model call with its own
toolset and can delegate. But today they are differentiated by toolset and prompt, essentially
never by model - they all ride one Ollama Cloud model. Per-agent large-cloud assignment is
MODEL-2.

## 5. Infrastructure and operations

- **Process model.** One OS process, two roles. `desktop.py` imports `webui` directly and runs
  `serve_forever` on a daemon background thread while PyWebView's window loop blocks the main
  thread. Never a subprocess spawn.
- **Persistence.** No unified data layer by design — each module owns its own store.
  `StateStore` (`workspace/state/dourmouse.db`, SQLite/WAL, per-owner tables), `MemoryStore`
  (SQLite + FTS5, content-hash dedup), `AuthStore`, `GlobalMemory`, session JSONL ledgers,
  `schedules.jsonl`, the device wiki store, the security sentry store, `office_log.db`.
- **Governance.** `BudgetTracker` (40 calls / $1.00 / 600s wall-clock per top-level request
  tree, shared across the whole delegate tree), `DlpFilter` (regex-redacts keys, PEM blocks,
  JWTs and secrets before anything reaches the model or the transcript), `RbacPolicy`.
- **Error handling.** A shared network-error taxonomy in `net_errors.py`. LLM-call retry with
  exponential backoff, one fallback-model attempt, and a hard wall-clock deadline.
- **Remote access.** Real and documented: Tailscale plus `DOURMOUSE_ACCESS_TOKEN`. Binding
  non-loopback without a token is a hard refusal unless explicitly overridden.
- **Static analysis.** `ruff` and `mypy` both configured and both have already found and fixed
  live bugs. Neither is wired into a CI gate — there is no CI pipeline for this repo at all,
  which is a tracked honest gap.
- **Secret scanning.** `gitleaks` run periodically and manually over all branches.

## 6. The confirmed central architectural gap (still true)

From `docs/ARCHITECTURE.md`, re-verified: **there is no checkpoint or resume of an in-flight
dispatch loop.** Real daemon threads outlive individual requests, but every one is
`daemon=True` inside one process. Nothing survives a crash except what is already durably on
disk. `JobTracker` is a 500-entry in-memory ring buffer. `schedules.jsonl` holds only single
tool calls on a cron spec. A killed process loses any in-flight dispatch entirely; an SSE
disconnect can cancel early but never resume.

Domain B's goal/task runtime addresses this for goals specifically — a goal does survive a
`kill -9`. It does not address an arbitrary mid-flight dispatch turn.

---

*Update this document at every checkpoint, per `HARD_RULES.md` Rule 5.*

## 2026-10-02 Wave 1 (finding #161, commit cf1665b)
Shared desk for every chat (browser, file preview incl. txt/md/csv/json/code, player tools), MEDIA queue and PDF reader with highlights, 40-task benchmark harness (stub mode only run), app-driving core wired as subagent app_driver (fake backend only; Accessibility not granted). Suite 7106 passed, 12 skipped. Not seen live in Electron. F1 design: F1_APP_DRIVING_DESIGN.md.

## 2026-10-02 Wave 2a (finding #162)
Owner secret live (A5), desk and login hardening, multi-tab browser pane with downloads, find, zoom, print, history, bookmarks. Suite 7299 passed, 12 skipped. Seen live in isolated copies: tabs, downloads, find, zoom, history, owner gate, stale-server replacement. Not seen: print dialog, Finder reveal.

## 2026-10-03 Wave 2b (finding #163)
Per-site permission prompts, encrypted password and address manager with click-to-fill (native confirm), password fields redacted from model snapshots. Suite 7421 passed, 12 skipped. Seen live in an isolated copy except the native dialogs.

## 2026-10-03 Wave 2c (finding #164): Wave 2 complete
Extensions, profiles, Chrome import, DRM readiness. Suite 7605 passed, 12 skipped. Browser now has tabs, downloads, find, zoom, print, history, bookmarks, permissions, passwords, extensions, profiles, import. Not yet: Widevine DRM build (owner go needed), Phase C shared control, 60-site parity run.

## 2026-10-05 Wave 3 C1 and C2 (findings #165 to #167)
Stable element ids, agent follows the owner's active tab, N3 git hardening, user/model lock with Stop and Take control, editor typing, YouTube control. Seen live on isolated copies including real YouTube. Suite 7837 passed. Evidence EVIDENCE/167_c2_*.

## 2026-10-05 Wave 3 complete (finding #168)
Tool skill: new browser tools registered, look-alike descriptions, tool-use guide, Enter gates, calendar-aware slots, routing fixes. Suite 7934 passed. Not measured with a real model yet.

## 2026-10-05 Wave 4 F2 (finding #169)
Driving strip, APPS screen, permissions guide. Suite 7989 passed. Evidence EVIDENCE/169_f2_*.

## 2026-10-05 Wave 4 I1 (finding #170)
Setup and login restyle, Text size, UX round: 11 fixed, 28 partly. Suite 8007 passed. Evidence EVIDENCE/170_i1_*.

## 2026-10-05 Wave 4 complete (findings #169 to #171): EXECUTION_PLAN.md fully built
All four waves built, reviewed and pushed (commit e74001a). Suite 8071 passed, 12 skipped, 0 failed. Crash recovery and the map-window CPU fix seen live (HOME 8 to 14 s, was over 60 s). Numbers in PERF_BUDGET.md; build plan in SHIP_PLAN.md.
