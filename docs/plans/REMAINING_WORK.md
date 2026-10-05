# DOURMOUSE — REMAINING WORK (THE TO-DO LIST)

**This is the work queue. Go down it in order unless the owner redirects.**

**Last updated: 2026-09-23.** Against commit `1729231`, pushed. Findings #076-#082. Full
suite at that commit: 5442 passed, 10 skipped, 0 failed.

## How this document works (read before touching it)

- Every item has an **ID** (`OS-1`, `SEC-14`, ...), a **size** (S/M/L/XL), a **status**, and
  enough detail that a session with no memory of this project can start on it.
- **When an item is finished** it does not get a checkbox tick. It gets rewritten in place as
  a real paragraph: what was actually built, which files, what the honest remaining limitation
  is, the numbered finding, and the filename of its screenshot in `EVIDENCE/`. Then it moves
  to the DONE section at the bottom and `CURRENT_STATUS.md` gains the matching entry.
- **Nothing is marked done without** a green full test suite, a commit, a push, a numbered
  finding, a real UI verification, and a screenshot. See `HARD_RULES.md`.
- Sizes are relative effort, not time. S reuses a proven pattern. XL is a multi-pass initiative.

## Ordering

The owner's stated priority is **"turn Dourmouse into a real OS system to replace every
app"**. **§0 (models and the standing-agent runtime) leads everything**, because three domains depend on it and MODEL-1 fixes a live defect (X-6). After §0, §1 leads. §2 is the second-largest and most specified body of work. §3 onward are
the tracked remainders of the existing domains.

**§2 now has two halves and they are not interchangeable.** §2b is the founding spec's own
network-posture build list. §2c is the owner's fleet endpoint monitoring and remote lockdown
spec, given 2026-09-23. They overlap on only five spec items, so shipping §2c does NOT reduce
§2b: the item 18 baseline engine and the network telemetry of items 5 to 14 stay open either
way. Pick one deliberately rather than assuming progress in one is progress in the other.

**One standing deferral, set by the owner 2026-09-20:** the Phase 0-4 backlog (§7) waits
until the weekly usage limit resets. Do not start it ahead of §1 to §6.

---

# §0 - FOUNDATION: MODELS AND THE STANDING-AGENT RUNTIME - **owner decisions 2026-09-24**

**This section is first on purpose.** Three domains (research, cybersecurity, the file
librarian) converge on the two items below. They are foundational: build them wrong and every
autonomous feature above inherits the flaw. Established with the owner 2026-09-24.

## The model policy (hard rule, owner-set)

**Large cloud models only. Never local. Never any model under 14B.** Local inference is too
slow to be practical on the owner's hardware. This is now a standing constraint on every
agent-model decision, not a preference.

**Verified live config 2026-09-24** (read from `~/Library/Application Support/Dourmouse/.env`,
names only): keys present are `OLLAMA_API_KEY`, `OLLAMA_API_KEY_2`, `GEMINI_API_KEY`. NVIDIA is
absent, so the entire `_NVIDIA_AGENT_DEFAULTS` table in `config.py` is DEAD - none of
`nemotron`, `codellama-70b`, `llama-253b` runs. The general fleet routes to **Ollama Cloud**
(`ollama.com/v1`, default `gpt-oss:20b`). Gemini is the image path and is quota-blocked.

**The two real large-cloud routes on this machine:**
1. **Ollama Cloud** via the two keys: `gpt-oss:120b` (strong tool calling) and `gpt-oss:20b`.
2. **The Claude CLI.** The owner signs in with the `claude` CLI, and `dispatch.py` already has
   a real `claude_cli` backend (`claude-sonnet-5 (CLI)`, ~lines 3829/4652) that shells to the
   authenticated CLI. Currently wired for CODING agents only. It is the strongest tool-calling
   route available and the natural choice to extend to the orchestrator. Note: `claude_cli` is
   NOT one of `DOURMOUSE_LLM_BACKEND`'s options (ollama|omniroute|nvidia|auto); it lives in the
   dispatch code path, so extending it to the orchestrator is a code change, not an env switch.

### MODEL-1 - Repoint the orchestrator off qwen2.5:7b - **DONE 2026-09-24**

The orchestrator is pinned to `qwen2.5:7b` by a saved `DOURMOUSE_ORCHESTRATOR_MODEL` setting.
That is a local-class model name (config.py's own comment: not a real Ollama Cloud catalog
entry) and a sub-14B model, so it violates the model policy AND is the direct mechanism behind
X-6: a 7B brain does not reliably emit tool calls, so it answers "I opened the media player"
instead of calling the tool.

**Fix (config, then verify):**
```
OLLAMA_CLOUD_MODEL=gpt-oss:120b            # fleet default, strong tool calling
DOURMOUSE_ORCHESTRATOR_MODEL=gpt-oss:120b  # was qwen2.5:7b
DOURMOUSE_ORCHESTRATOR_BACKEND=ollama      # already cloud via the key
```
**Applied and verified 2026-09-24.** Set `DOURMOUSE_ORCHESTRATOR_MODEL=gpt-oss:120b` and
`OLLAMA_CLOUD_MODEL=gpt-oss:120b` in the real `.env` (backed up first, mode 600, captured in the
offline backup). Verified through the app's own config: `orchestrator_model_setting()` and
`model_for_agent('orchestrator')` both resolve to `gpt-oss:120b`, backend cloud. A real
authenticated `/api/chat` call returned in 1.4s with no 401. The decisive test: on the exact
X-6 scenario ('preview the file ...m4a'), gpt-oss:120b emitted the correct
`open_file_preview` tool call, while qwen2.5:7b 404s on Ollama Cloud (it was never a valid cloud
model). The `claude_cli` route (claude-sonnet-5) remains available as an even stronger option
and a code change if wanted later.

### MODEL-2 - Per-agent large-cloud assignment - S
Once MODEL-1 lands, set each agent to a large cloud model via `DOURMOUSE_OLLAMA_MODEL_<AGENT>`
(or the `claude_cli` route). No agent, including pollers, may sit on a sub-14B or local model.
Coding agents already use `claude_cli`; leave them.

## INFRA-1 - The standing-agent runtime - M - **shared substrate, three consumers**

A general runtime for agents that run WITHOUT prompting, distinct from on-demand agents that
only wake on dispatch. The deck's autonomous agents, the security AI sentries, and an
autonomous research loop all need it. Build once (HARD_RULE 9), do not write three loops.

**The loop:**
```
standing agent, forever:
  1. drain my inbox    - did another agent ask me something on the bus? answer it.
  2. check my mission  - is there outstanding work under my standing goal?
  3. if yes: do ONE bounded unit, record it in my progress memory
  4. sleep on a timer, OR block until the bus wakes me
```

**Design rules, all forced by real constraints:**

- **Standing vs on-demand is a per-agent MODE, not a global switch.** Most of the 49 agents
  stay on-demand (the coder must never write code unprompted). An agent opts in; only opted-in
  agents get a loop. Some agents are both (research_info is on-demand when asked, standing when
  the research network runs autonomously).
- **Cost-tiered loop, now that everything is large cloud.** A standing agent CANNOT sit a large
  cloud model in a tight always-on loop - that burns the Ollama Cloud keys continuously. So the
  always-hot tier is **deterministic** (like the existing `SentryRuntime`, no model) or a slow
  interval poll (like `LiveRuntime`, mail every 300s); it escalates to a **large cloud model
  only when the cheap tier finds something worth reasoning about.** This is exactly the security
  shape: deterministic detection always on, the AI sentry (cloud model) wakes on a finding.
- **Inbox-woken, not only timed.** "Handles data requests from other agents" means the loop
  wakes when the message bus delivers, not only on a clock. The bus is already durable
  (`office_log.db`); nothing currently loops on an inbox. That loop is the new piece.
- **The tightest leash in the product.** An always-on agent that can touch files, the network
  or the host needs a hard capability allowlist (READ allowed, MODIFY/DESTRUCTIVE gated -
  security spec item 20), confirmation on every destructive step, and a visible permanent log
  of everything it did unprompted (`office_log.db`). X-6 bites hardest here: an autonomous agent
  that CLAIMS it acted without having acted is worse than one that asks. MODEL-1 is therefore a
  prerequisite - do not run a standing agent on a brain that fabricates tool calls.

**Reuse map:** loop shape from `LiveRuntime`; progress memory from the device-wiki store's
one-row-per-file shape; wake events from R6 event sourcing (§3g); durable log from
`office_logger.py`; deterministic always-on precedent from `SentryRuntime`.

**Three consumers, cross-referenced below:** OS-5 (file librarian), §2b item 19 (security AI
sentries) plus item 30 (responses), and §3g autonomous research (R6/R7).

---

# §1 — BECOMING AN OS: the app-replacement features

This is the north star. The vision deck's final slide: *"DOURMOUSE replaces all apps on the
user's system, it acts as an OS... THE END RESULT MUST BE COMMERCIAL GRADE, ABLE TO REPLACE
ALL OPERATING SYSTEMS."* Everything in this section is a concrete sub-feature the deck itself
names, checked against the real codebase.

### OS-3 — Real browser, no proxy errors — **ANSWERED AND HALF-SHIPPED** — S remaining

**Security note added 2026-09-24 (finding #086):** `browser_pane.fetch_and_rewrite_for_proxy` and
`check_frameable` fetch any URL server-side with no address guard, so the model can point the pane
at `169.254.169.254` or a LAN admin page and the proxy fetches it. Left unchanged on purpose: a
human-facing browser may legitimately need LAN pages. Decide the policy here (for example: the
guarded opener for LLM-initiated navigation, direct for user-typed URLs), and it largely goes away
once the pane is a BrowserView instead of a server-side proxy.

**SCOPED UP 2026-09-24 to full commercial-grade, owner decision.** Target: a real embedded
browser that behaves like Chrome inside the app, that BOTH the user and the LLM drive, on ONE
shared surface, with the LLM able to watch and scrape it continuously. Decision: **Electron is
the shipped browser shell.** pywebview drops to a minimal degraded fallback and is explicitly
NOT the commercial target, because it has no BrowserView and no CDP and therefore cannot be the
Chrome-like, LLM-observable surface the owner asked for.

**Why the proxy errors happen and the real fix:** the live `console.html` pane still calls the
PROXY (`/api/browser-pane/proxy` -> `fetch_and_rewrite_for_proxy`), the iframe + server-side
URL-rewriting path. THAT proxy is the source of the errors (imperfect rewriting, no cookies so
logins look logged out, unparseable responses). The BrowserView that solves it is already built
(finding #081) but the UI does not call it yet. The fix is not a better proxy, it is not needing
one: a BrowserView is a top-level context so `X-Frame-Options`/`frame-ancestors` do not apply.

**Build:**
1. Wire the console pane to the bridge `/navigate` (BrowserView) under Electron; keep the proxy
   only as the pywebview fallback, labelled degraded.
2. Full browser chrome: tabs, address bar with real history, back/forward, reload, bookmarks,
   downloads, find-in-page.
3. One shared surface: the user drives via the UI; the LLM drives the SAME BrowserView via the
   bridge and reads it via CDP (the scraping path R0-2 in §3h also reuses this). Not two
   browsers.
4. Honest platform note in the UI on the pywebview fallback.



**The cause was architectural, and it is now proven.** The pane is an `<iframe>`, so any site
sending `X-Frame-Options` or CSP `frame-ancestors` refuses to load, which is most of the real
web. A server-side rewriting proxy existed as the fallback, and **that proxy is the source of
the errors**: imperfect URL rewriting, no cookies (so a logged-in site looks logged out), and
responses it cannot always parse.

The fix is not a better proxy, it is not needing one. Those headers restrict FRAMING. An
Electron `BrowserView` is a top-level browsing context, not a frame, so they do not apply.

Proven with a controlled experiment, not asserted. Same Chromium, same process, a local page
serving both `X-Frame-Options: DENY` and `frame-ancestors 'none'`:

```
iframe      -> BLANK (refused)   ERR_BLOCKED_BY_RESPONSE
BrowserView -> LOADED
```

**Shipped (finding #081):** `electron/main.js`'s pane bridge gained `POST /navigate` plus
`/back`, `/forward`, `/reload`. It previously had only status/show/hide, so the BrowserView
could never be navigated except through Playwright. Only real `http(s)` is accepted;
`file://` and `javascript:` are refused by name and verified refused, because this bridge is
reachable from any local process and a BrowserView on `file://` would read the user's disk.

**What remains, and it is small:** wire `console.html`'s pane UI to call `/navigate` instead of
setting `iframe.src` when running under Electron, keeping the iframe plus proxy as the
pywebview fallback. Also decide what the address bar does about history now that real history
exists.

**Honest limit:** this is the Electron path only. pywebview has no BrowserView, so it keeps
every limitation named in finding #075.


### OS-4 — Live-verify the whole preview surface against real content — **MOSTLY DONE** — S

The audit (finding #075) read the source. The media player was then live-verified against real
files (OS-1, finding #076), and **the two gaps that could not be closed in the Claude Browser
test tool were closed in the real Electron shell** the same day (finding #077):

- **Wall-clock playback: VERIFIED.** Video advanced 2.002s across 2.000s of wall clock with a
  real decoded frame; audio advanced 1.95s over 2.5s and seeked exactly to 5.0. Screenshot:
  `EVIDENCE/003_media_player_in_electron_shell.png`.
- **The pane embed: VERIFIED.** The media player renders correctly inside the real browser
  pane, inside the real Electron window, first try. The pane's address bar shows the
  root-relative URL, so OS-1's same-origin fix is live and visible. Screenshot:
  `EVIDENCE/004_browser_pane_in_electron_shell.png`.

The earlier blank pane was the Claude Browser test tool blocking sandboxed-iframe navigation
(`ERR_BLOCKED_BY_CLIENT`), never a product defect.

**What still remains here** is the rest of the content spread, none of which has been driven
yet: a framing-friendly site, a framing-blocked site (the proxy path), a login-walled site, a
JS-heavy SPA, a long multi-column PDF, a scanned/OCR PDF, a large PDF (paging), and a plain
image. Record every failure honestly as a new item. Do this in the real Electron shell, not the
Claude Browser tool, for the reasons above.


### OS-10 - Media playback: play EVERY format, like Windows - **SCOPED 2026-09-24** - M

Owner target: play every media type a desktop OS plays - mp3 and every audio format, video of
any container/codec, PDFs, images, everything - with no silent blank player.

**What already works (finding #076, byte-range streaming proven):** images (png/jpg/jpeg/gif/
webp/svg/bmp), audio (mp3/m4a/aac/wav/ogg/opus/weba), video (mp4/m4v/webm/ogv/mov), PDF.

**The exact gap:** mkv, avi, flac, wmv, and files whose codec the browser cannot decode (HEVC/
H.265 video, AC3/DTS audio) are DELIBERATELY excluded today, because the HTML `<video>`/`<audio>`
element cannot decode them and listing them would give a silently blank player - the fabrication
this codebase refuses (webui.py's own comment). `ffmpeg` is NOT installed. That is the crux.

**Decision: ffmpeg, remux-first (owner: "whichever is easier and more versatile").** Easier to
ship (one binary, works on both shells) and the most versatile on formats (ffmpeg decodes
essentially everything). Runtime strategy:
- **Remux when the codec is already browser-playable** but the container is not (most mkv/avi are
  H.264+AAC in a wrapper): stream-copy into mp4, no re-encode - instant, seekable, no CPU.
- **Transcode only when the codec is genuinely unsupported** (HEVC->H.264, AC3->AAC): the CPU-heavy
  path, used as a last resort.
- Feed the result into the EXISTING byte-range player. Seeking during a live transcode is the one
  hard part; remux-first avoids it for the common case.
- Anything ffmpeg itself cannot handle still reports an honest "cannot decode", never a blank player.

**Full-parity scope beyond formats:** subtitle tracks (srt/vtt sidecar and embedded), audio-track
selection, a playlist/queue, remember-position, chosen so it genuinely replaces a desktop media
player rather than just previewing files.

**Honest limit:** the heavier transcode path is Electron-shell territory for the same reasons as
OS-3; a remux stream works anywhere the byte-range player already does.

### OS-5 — The always-on local file librarian — **NOT BUILT AS SPECIFIED** — L

**Depends on §0 INFRA-1 (the standing-agent runtime) and MODEL-1 (a large-cloud brain). The librarian is INFRA-1's first tenant, not its own bespoke loop.**

This is the **first capability the founding spec names**, and the least built. The deck: an
agent that "perpetually organizes the users files and is responsible of being a librarian for
the users data and files and handles data requests from other agents... Works all the time
without any prompting required."

What exists is an on-demand, confirmation-gated file-organization **tool** inside the
`admin_ops` subagent — it runs when asked, once, and stops. There is no scheduled job, no
incremental memory of what it has already organized, and no continuous operation.

**Build:** a real scheduled/always-on job (reuse `SchedulerRunner`'s daemon-thread-with-tick
shape, the closest existing precedent), a real incremental memory of what has been organized
(reuse the device wiki store's one-row-per-file shape — the device wiki already walks and
tracks files, so this is an extension of a proven system, not a new one), and a real
inter-agent request path so other agents can ask the librarian where something is (the
message bus and `office_logger` now make this durable). Must respect the same explicit
`DOURMOUSE_WIKI_ROOTS`-style allowlist — never the whole filesystem.

### OS-6 — Isolated per-project workspaces — **NOT BUILT AS SPECIFIED** — L

The deck: "Replica of how claude desktops project ecosystems work, each project is basically
its own isolated virtual environment which has access to all dourmouse features i.e research,
coding, cybersecurity testing everything simply isolated and saves etc, it should automatically
make a folder for each project in the users document."

What exists (`project_bookkeeper.py`, `project_import.py`) is a passive read-only dashboard
summarizing Claude Code / Codex session history per project directory. Genuinely useful, but
it is not an isolated environment and it does not create anything or grant a project scoped
access to Dourmouse's other subsystems.

**Build:** real project creation that makes a folder in `~/Documents`, a real per-project
scope carried through dispatch (the `DispatchContext` thread-local stack is the existing
mechanism to extend — do not invent a second one), per-project memory and session isolation,
and real scoped access to research, coding and security features from inside a project.

### OS-7 — Google Workspace: Docs, Sheets, and write access — **PARTIAL** — M

Real OAuth (Authorization Code + PKCE) gives each signed-in user their own Gmail (read plus
confirmation-gated send), Calendar (read-only) and Drive (read-only), honestly reporting
NOT_CONFIGURED without credentials. Missing: Docs, Sheets, and write access to Calendar and
Drive. The Drive/Slides write code path reportedly exists and is tested but needs a sign-in at
`/login` with `GOOGLE_OAUTH_FULL_SCOPES=1` to grant the scope — **verify that claim against
the real code before treating it as nearly-done.**

### OS-8 — The three unevaluated OS questions — **NOT EVALUATED** — M

Named honestly in the status report as open rather than silently assumed covered. Each needs
the same rigor as everything else before anyone claims it:

1. **A real notification / alert center.** `DesktopNotifier` is a real building block (it
   subscribes to the server's own SSE hub, diffs against a seen-id set so late subscribers are
   not spammed, and fires real macOS notifications with an honest fallback). There is no
   center — no history, no per-source muting, no read state.
2. **An application-launcher / dock equivalent** for jumping between the file previewer,
   browser pane, research and security surfaces as if they were separate apps. Today they are
   screens inside one 7,092-line HTML file reached through an overflow menu.
3. **A real settings / preferences surface** complete enough that a user never needs to drop
   to a terminal or edit a config file to change how Dourmouse behaves. Today: `config.py` is
   a flat module of accessor triples, some persisted to a 0600 file, some env-only, surfaced
   inconsistently across `setup.html` and scattered console toggles.

### OS-9 — UI consolidation: stop maintaining five home screens — **NOT DONE** — L

Five overlapping "chat-first home" surfaces are live and routed: `console.html` (content
default), `workspace.html` (the actual launch default), `index.html` (legacy, the only one
with a command palette), `app.html`, `os.html`. Each maintains its own copy of the SSE parser,
its own `:root` palette, and in at least two cases its own markdown renderer.

The deck is explicit about this: *"i don't want tens of different tabs either since that is
unprofessional, make it practical and easy to use, something like hermes or claude desktop is
preferred, but the listening tab is nice keep that."*

**Build:** consciously fold `app.html`, `os.html` and `index.html` into one, keeping the
listening surface. Treat `console.html` and `workspace.html` as co-primary. This is a
prerequisite for OS-8.2 (a dock over five inconsistent homes is worse than none).

---

# §2M — MAC-ONLY CYBERSECURITY BUILD PLAN (owner decision 2026-09-24) — XL

**Owner: "for cybersecurity, we make it work just for this mac device, scrap all plans for other
device control, only for mac for everything, full liberty."** §2c (fleet EDR across three machines,
remote lockdown) is scrapped as specified; its Mac-relevant parts are folded in below. Detection
stays deterministic and pure (the existing `_detect_findings` convention); models reason above it,
never detect. Every action that changes the machine goes through the approval gate, ask-first.

| ID | Work | Spec items |
|---|---|---|
| MS-1 | Telemetry: Wi-Fi profile, process details (path, parent, signature), code-signing and Gatekeeper assessment, connection diagnostics (latency, loss, DNS), network identity | 5, 7, 8, 15 |
| MS-2 | Baseline engine: per-network, time-aware, learning period, "differs from before" | 9, 18 |
| MS-3 | Detectors: gateway MAC change and ARP duplicates, DNS resolver change, new listening port, unsigned or ad-hoc-signed process with network activity, processes running from Downloads or temp, new persistence (LaunchAgents/LaunchDaemons/login items), open or weak Wi-Fi | 8, 10, 11, 13, 14, 15 |
| MS-4 | Downloads watch and file assessment: quarantine flag, signature, notarization, Gatekeeper verdict, type, hash; ClamAV only if installed, never implied | E2 |
| MS-5 | "Am I being monitored?" indicators: proxies, configuration profiles and MDM enrollment, extra trusted root certificates, remote login and screen sharing, VPN and network extensions; honest confidence per indicator | 24 |
| MS-6 | Connection failure taxonomy (BLOCKED / UNREACHABLE / TIMEOUT / DNS / TLS / ROUTING / SERVER / UNKNOWN) | 6 |
| MS-7 | Multi-dimensional trust posture (never one magic number) and the one-button evidence report with an explicit Unknowns section | 25, 49, 50 |
| MS-8 | Response actions through the approval gate: kill process, quarantine a file, disable a persistence item, block a domain (root-only steps given as exact commands, never faked) | 30 |
| MS-9 | AI security analyst: wakes on a new finding, reasons over the evidence with a large cloud model, writes an assessment and suggested response; never detects, never acts on its own | 19, 40 |
| MS-10 | Live monitor and automatic check on network change; events on the SSE hub | 27, 29 |
| MS-11 | Browser history and typed searches (Chrome, Safari with Full Disk Access), local only | E1 |
| MS-12 | Threat model document, privacy mode, security self-audit | 37, 42, 48 |

| MS-13 | **Lockdown (owner spec 2026-09-24):** "a list of urls and apps and websites that can't be opened when initiated". Owner-managed blocklist of apps and websites/URLs; "initiate lockdown" (approval-gated) makes them unopenable until ended. Apps: a user-level enforcer that closes a blocklisted app the instant it launches, relaunches included (no admin needed). Websites: system-wide via `/etc/hosts` plus a DNS cache flush, through a small root helper the owner installs once with one sudo command (Dourmouse never enters a password); after that lockdown toggles without prompts. Honest limit: hosts blocks whole domains, not individual URL paths; path-level blocking needs a browser extension (follow-on). | E6 |

Build order: MS-1, MS-2, MS-3, MS-4, MS-5, MS-6, MS-7, MS-8, MS-13, MS-9, MS-10, MS-11, MS-12.

# §2 — DOMAIN I: the defensive cybersecurity subsystem — XL

**The single largest remaining body of work in the product.** The founding spec
(`REFERENCE/FOUNDING_SPEC.pdf`) contains a 54-item build list. **Recounted 2026-09-23 against
the real code:** 3 items are genuinely real (16 firewall monitor, 26 dashboard, plus the
reputation enrichment of §2a), 7 are partial (4 platform adapter at 8 of 14 operations and
module-level rather than the specified class abstraction, 9, 17, 18, 28, 30, 31), and 44 are
untouched. The earlier "8 of 54" figure counted partials as whole.

The foundation is correctly designed and the pattern for extending it is proven. This is not
a criticism of what exists; it is a statement of how much of the owner's own ambition for this
domain is still ahead.

## §2a - The sequenced first five - **ALL FIVE BUILT. Corrected 2026-09-23.**

**This section previously marked all five NOT BUILT. That was wrong**, and the error was in
this document, not in the code. Corrected by reading `dourmouse/security/sentry.py` and
`dourmouse/security/reputation.py` directly. Commit `e4b8584` "Domain I: close the Phase 2
scale-out plan (threat intel, incidents, correlation, remediation text)" shipped a real first
version of every one of the five.

### SEC-1 - Asset inventory - **BUILT** (spec items 9, 18) - remaining: the baseline engine
`known_devices` table, `record_devices()` upsert that establishes the baseline on the very
first call and refreshes `ip`/`hostname`/`last_seen` on every call after,
`get_known_device_keys()`, `devices_snapshot()`. A device never seen before becomes a
`new_device` finding through the same pipeline as everything else.

Named limitation, recorded in its own docstring: a device whose MAC the kernel never resolved
is keyed `ip:<address>`, so DHCP churn changes its key and it can re-announce as new.

**Remaining:** this is a LAN device inventory, not spec item 18's baseline engine. There is no
time-aware, per-network baseline, so home wifi and school wifi are not yet distinguished.

### SEC-2 - Incident / case tracking - **BUILT** (spec item 28, the store half)
`incidents` table with `open_incident()`, `update_incident()`, `get_incident()` and
`list_incidents(status=...)`. Real lifecycle through `INCIDENT_STATES` with a terminal-state
guard: a resolved incident refuses to transition to any other status, because a real analyst
opens a new case rather than reviving a closed one. `open_incident` is idempotent and returns
`unknown_fingerprint` rather than accepting an arbitrary string, so a case can only ever
reference a finding that was really detected.

**Remaining:** spec item 28's one-button "Investigate Network" bounded workflow and its
structured report (Executive Summary, Observations, Baseline Deviations, Unknowns,
Recommended Next Tests). The case store exists. The investigation does not.

### SEC-3 - Correlation engine - **BUILT, exactly one rule** (spec item 17, deeper half)
`_detect_correlations()` fires on the spec's own named example: a new LAN device AND a newly
exposed service both first detected in the SAME scan. Deliberately reads this scan's new
findings rather than the persisted open-condition list, so it fires once on the coincidence
and never re-fires on a later scan where one half was already known. Not persisted, because a
stable fingerprint for a coincidence has nothing meaningful to deduplicate against.

**Remaining:** one rule is a correlation engine in shape only. Breadth is blocked on there
being something to correlate: build §2b items 5 to 15 first.

### SEC-4 - Threat-intelligence enrichment - **BUILT**
`dourmouse/security/reputation.py`, AbuseIPDB-backed, exposed as the `security_check_reputation`
tool. Honestly inert with a stated reason when no key is configured. Refuses private, loopback
and reserved addresses by name rather than asking a public API about a LAN address.

### SEC-5 - Local remediation - **BUILT, text only** (spec item 30)
Every finding carries `recommended_action` text routed to the human. `security/tools.py`'s own
module docstring is explicit that remediation tools which actually act on the host are separate
and not built.

**Remaining:** the acting half, through the existing human-approval gate. Never auto-remediates,
and "ask before changing anything" stays the default policy per the spec.

**What this correction changes about the plan.** The §2a on-ramp is spent. The layer ABOVE the
detectors (cases, correlation, enrichment, recommended actions, dashboard) is real. What is thin
is the telemetry UNDERNEATH it. Build downward into §2b items 5 to 15 and the item 18 baseline
engine, not sideways into more case management.

## §2b - The remaining ~46 spec items, by the spec's own numbering

Each is specified in detail in `REFERENCE/FOUNDING_SPEC.pdf`. Read the spec section before
building; do not work from these one-line summaries alone.

| # | Item | Status | Size |
|---|---|---|---|
| 4 | Cross-platform security engine | macOS only | L per platform |
| 5 | Network connection profile (SSID/BSSID/security mode/DHCP/DNS) | NOT BUILT | M |
| 6 | Connection failure taxonomy (BLOCKED / UNREACHABLE / TIMEOUT / DNS / TLS / ROUTING / SERVER / UNKNOWN) | NOT BUILT | S |
| 7 | Internet connection diagnostics (latency, jitter, packet loss, DNS reachability) | NOT BUILT | M |
| 8 | WiFi security assessment | NOT BUILT | M |
| 9 | Local network baseline | NOT BUILT | M (= SEC-1) |
| 10 | ARP / neighbor anomaly detection | NOT BUILT | M |
| 11 | DNS security monitor | NOT BUILT | M |
| 12 | Tailscale security monitor | NOT BUILT | S |
| 13 | Host exposure scanner (loopback vs LAN vs Tailscale vs all-interfaces) | NOT BUILT | M |
| 14 | Outbound connection monitor | NOT BUILT | M |
| 15 | Process security monitor | NOT BUILT | M |
| 16 | Firewall monitor | **REAL** | done |
| 17 | Security event engine | partial | M |
| 18 | Baseline engine | partial (firewall/exposed-ports only) | M |
| 19 | Local AI security sentries (a §0 INFRA-1 tenant; detection stays deterministic, the sentry reasons ABOVE it) | NOT BUILT | L |
| 20 | Capability-based AI tool permissions | NOT BUILT | M |
| 21 | Packet capture, with strict limits | NOT BUILT | L |
| 22 | White-hat security tool integration | NOT BUILT | M |
| 23 | Parrot OS / security Linux integration | NOT BUILT | L |
| 24 | "Am I being monitored?" indicator analyzer, technically honest | NOT BUILT | M |
| 25 | Network trust score, multi-dimensional, explicitly **not** a single magic number | NOT BUILT | M |
| 26 | Security dashboard | **REAL** (finding #062) | done |
| 27 | Live security monitor | NOT BUILT | M |
| 28 | Security investigation mode | NOT BUILT | M (= SEC-2 extended) |
| 29 | Automatic WiFi check | NOT BUILT | S |
| 30 | Automatic defensive responses | NOT BUILT | M (= SEC-5) |
| 31 | Security database | partial | S |
| 32 | Security provenance | NOT BUILT | S |
| 33 | Security logging | partial | S |
| 34 | Prompt-injection defense (security-specific) | partial, general only | M |
| 35 | Testing | partial | M |
| 36 | Security test lab (synthetic) | NOT BUILT | L |
| 37 | Threat model document | NOT BUILT | M |
| 38 | Security node architecture | NOT BUILT | L |
| 39 | Local model architecture for sentries | NOT BUILT | M |
| 40 | AI failure policy | NOT BUILT | S |
| 41 | Resource management | partial | S |
| 42 | Privacy mode | NOT BUILT | M |
| 43 | Source organization | partial | S |
| 44 | Security API | partial | S |
| 45 | Security UI data model | partial | S |
| 46 | Installation | NOT BUILT | S |
| 47 | Permissions | partial | S |
| 48 | Security self-audit | NOT BUILT | M |
| 49 | Security score must not hide evidence | design rule, enforce | S |
| 50 | No security theater | design rule, enforce | S |
| 51 | Deep security review | NOT DONE | L |
| 52 | Deep failure testing | NOT DONE | L |
| 53 | Final security acceptance criteria | NOT DONE | M |
| 54 | Final engineering task and report | NOT DONE | M |

**Standing design rules from the spec that apply to every item above:** the detection path
contains no model call (already correctly enforced); the scope is authorized-defensive only;
a security score never hides the evidence behind it; no security theater - if a check cannot
be done honestly, it reports that it cannot, it does not report a green tick.

## §2c - FLEET ENDPOINT MONITORING AND REMOTE LOCKDOWN - XL - **owner spec, 2026-09-23**

**Owner-defined scope, given in chat 2026-09-23 and recorded here verbatim in substance.** This
is a personal-fleet EDR plus MDM across the owner's three machines (Mac, desktop, Dell): see
what runs on them, scan what lands on them, and lock one down remotely from the dashboard.

**This is mostly NEW work, not a restatement of §2b.** The founding spec's 54 items are
network-posture focused; this is endpoint focused. Real overlap is only items 15, 22, 30, 31
and 33. It therefore does NOT deliver the item 18 baseline engine, does NOT deliver network
telemetry (items 5 to 14), and does NOT deliver the confidence banding and multi-dimensional
posture of items 24 and 25. Those stay open in §2b regardless of whether §2c ships.

**Standing constraint, from the founding spec's own authorized-defensive scope.** The spec
forbids surveillance tooling, covert monitoring and stealth persistence. Every endpoint is
therefore registered as owner-operated in the SEC-1 asset inventory, and anyone else who uses
one is aware it is monitored. This is a field on an existing table, not additional work. It is
what keeps a legitimate personal-fleet EDR from being the other thing.

### Verified platform facts (measured on this Mac, 2026-09-23, not assumed)

```
macOS 26.6.2
/etc/security/audit_control      No such file or directory   -> auditd is dead on macOS
/usr/sbin/auditd                 present but unconfigurable  -> a vestige, do not ship it
System Integrity Protection      enabled
/System/Applications/Utilities/Activity Monitor.app          SIP-protected, chmod impossible
~/Library/Application Support/Google/Chrome/Default/History  FOUND
~/Library/Safari/History.db                                  FOUND
osquery / clamav / wazuh                                     none installed
```

### SEC-E1 - Browser history and typed search queries - M - **do first**
All URLs across all browsers, plus the exact search strings typed, on every endpoint.

Chrome: `urls`, `visits` and `keyword_search_terms`. That last table holds typed search terms
directly, so this needs no network interception and no TLS handling at all. Chrome holds a lock
on the file while running, so copy before reading. Safari: `History.db`, requires Full Disk
Access (a one-time TCC approval). Firefox: `places.sqlite` when present, absent on this Mac.

**Why first:** works today on all three platforms, needs no Apple entitlement, both databases
verified present, and it extends the existing LLM-wiki and `history_sync` modules rather than
inventing a second one (HARD_RULE 9).

### SEC-E2 - Download interception and malware scanning - M
Flag the second a file lands in Downloads, scan it, feed a verdict to the dashboard.

Watcher: FSEvents (macOS), inotify (Linux), `ReadDirectoryChangesW` (Windows). Python
`watchdog` covers all three in one dependency. Scanner: resident `clamd` plus `clamdscan`.

**Do NOT use `clamscan`:** it reloads the full signature database on every invocation, roughly
15 to 30 seconds per file, which defeats "the millisecond a download appears". Cost of the
daemon is about 1 GB resident for the signature set. State that cost, do not hide it.

**Honesty requirement (spec items 25, 50):** ClamAV's detection rate on live malware is well
below commercial engines. Report a confidence band, never a bare green tick.

### SEC-E3 - Command logging - M on Windows/Linux, **BLOCKED as specified on macOS**
Every command typed into a terminal, system-wide, on every device.

| Platform | Mechanism | Real? |
|---|---|---|
| Windows | Sysmon event 1 plus PowerShell ScriptBlock logging | yes |
| Linux | auditd `execve` rules | yes |
| macOS | auditd dead; Endpoint Security Framework needs an Apple entitlement | **no** |

**macOS path that works now:** a `preexec` hook in zsh and a `DEBUG` trap in bash, appending
each command to a log. Captures every command typed into Terminal, which is the requirement as
stated. Does NOT capture programs spawned by other programs. Record that limit in the UI.

**The full-fidelity macOS path is a real separate project:** `com.apple.developer.endpoint-
security.client` requires a paid Apple developer account, an approval request to Apple, a
signed system extension, and a user approval prompt. Decide deliberately; do not start it by
accident.

### SEC-E4 - Application launch tracking - M
Windows and Linux: the same Sysmon and auditd sources as SEC-E3. macOS without the entitlement:
`log stream` filtered on NSWorkspace launch notifications, plus periodic osquery `processes`
snapshots. **Honest gap:** polling misses short-lived processes. Say so rather than implying
complete coverage.

### SEC-E5 - osquery as the shared host telemetry layer - M - **highest leverage**
One dependency, one SQL surface, all three platforms. Delivers spec items 13 (host exposure),
14 (outbound connections) and 15 (processes) together, and partially answers BACK-6 because the
queries are identical across platforms even though the collectors underneath are not.

```sql
SELECT p.name, p.pid, l.address, l.port FROM listening_ports l
  JOIN processes p ON l.pid = p.pid WHERE l.address NOT LIKE '127.%';
```

Needs Full Disk Access on macOS, no Apple entitlement. Feed its rows into the EXISTING
`_detect_findings` pipeline, never a parallel one. **Caveat:** osquery's `process_events` table
depends on audit/ESF and is therefore degraded on modern macOS; use `processes` snapshots there
and accept the gap named in SEC-E4.

### SEC-E6 - Remote lockdown - M per platform - **partial on macOS, SIP is the reason**
Transport is already solved and live: Tailscale plus `~/.ssh/dourmouse_desktop`.

| Action | Windows | Linux | macOS |
|---|---|---|---|
| Website block | `hosts` | `hosts` | `hosts` or `pf` rules (root) |
| Kill app, block reopen | AppLocker | `chmod -x` | kill plus a `launchd` re-kill watcher |
| Disable Task Manager / terminal | registry `DisableTaskMgr` | `chmod` | **impossible, SIP** |

**The macOS limit is hard, verified, and not worth fighting.** SIP is enabled and Activity
Monitor and Terminal live under `/System/Applications/`, which root cannot `chmod`. The only
supported mechanism for real app restriction on macOS is an MDM configuration profile, which
means enrolling the device or installing a signed profile. Report the limitation in the UI
rather than shipping a lockdown that silently does less on one of the three machines.

**Two things to know before building the Windows path.** Registry-locking Task Manager is a
documented malware technique, so Defender and a Wazuh ruleset will both alert on your own
lockdown. And it is bypassable via safe mode, a second admin account, or an offline registry
edit. Treat it as friction, not a wall, and say so.

**Every lockdown action routes through the existing human-approval gate** (spec item 30: "ask
before changing anything" is the default policy).

### SEC-E7 - Cross-device dashboard - S - **cheapest part**
Rows come from the existing `known_devices` table; the feed comes from the existing SSE hub;
the surface is the existing security dashboard (finding #062). One unified activity stream
across commands, browsing and file drops, plus a per-device lockdown trigger.

### The Wazuh question - **an architecture decision, not a shortcut**
A single Wazuh agent per endpoint would manage Sysmon, command tracking, malware scanning and
lockdown, with the app pulling clean JSON from the Wazuh REST API. That is a legitimate trade
and it would make SEC-E2 to SEC-E6 mostly configuration rather than code.

**But it is not a shortcut, it is a different architecture.** Wazuh is a server stack: indexer
(an OpenSearch fork) plus server plus dashboard, wanting multiple GB of heap. Dourmouse is
local-first and runs in one process today. Decide this consciously and record the decision;
do not let it arrive as a footnote.

### Suggested §2c order
SEC-E1 (works today, no entitlement, extends an existing module) -> SEC-E2 (self-contained,
immediate value) -> SEC-E7 (close the loop, see it working) -> SEC-E5 (the shared telemetry
layer) -> SEC-E3 and SEC-E4 (per platform, macOS honestly degraded) -> SEC-E6 last (most
platform-specific, most caveats).

---

# §3n — THE THREE-DEVICE NETWORK (owner decision 2026-09-24) — L

**SUPERSEDED, 2026-09-24 (owner): "scrap all plans for other device control, only for mac for
everything".** No desktop, no Dell. Dourmouse runs on this Mac alone: research, compute
(experiments run in a local sandbox on the Mac), data, and cybersecurity. The node service code
stays in the repo as the Mac's local job runner (its JobRunner is platform-neutral); the network
parts are dormant. The `\DOURMOUSE-Node` task on the desktop was left running when the network
dropped; the owner can disable it there (`schtasks /Change /TN "\DOURMOUSE-Node" /Disable`).
Everything below in this section is kept as history only.

**UPDATE, same day: the Dell is SCRAPPED (owner: "if dell not working, scrap dell from the plan and
move its function to the desktop as well").** The Dell's SSH server never came up, so the desktop
now runs BOTH roles, compute and data, from the one node service. Where this section says "Dell",
read "desktop". Also on the owner's instruction, the old desktop Dourmouse tasks were disabled (not
deleted): DOURMOUSE-Desktop, -DailyDigest, -HistorySync, -InboxWatcher, DourmouseAutoSync,
DourmousePushWatcher; ollama was stopped. Left untouched pending the owner: the Forex* tasks
(E:\forex-data), ATLAS Seasonal Scheduler, the JARVIS-* tasks.

**Owner's words:** "the research workspace... like the python workspace in which the model can run
sims, research and numbers will be on the desktop, the models themselves will be cloud hosted
models, and the data etc papers and all will be on the dell."

| Device | Role | Tailscale |
|---|---|---|
| Mac | orchestrator: Dourmouse server, dispatch, UI | adits-macbook-air 100.84.156.49 |
| Desktop (Windows) | **compute workspace**: the Python sandbox where agents run sims, experiments, statistics | desktop-4u4t12k 100.98.97.23 |
| Dell (Windows) | **data node**: papers, fetched documents (the raw document cache), datasets, downloads | dourmouseserver 100.64.102.59 |
| Models | **cloud only**, on every device; never local, never under 14B (MODEL-1) | n/a |

**State found 2026-09-24:** none of this is wired. Both Windows machines were offline on Tailscale
(desktop last seen 2 days ago, Dell 20 hours ago); the old LAN address 192.168.1.108 did not answer.
The existing `dell/dell_server.py` was built for the opposite role: it serves a LOCAL `qwen3:1.7b`
model, which breaks both halves of the model policy. It is replaced, not extended.

- **NET-1 Dell data node.** Replace the model server with an authenticated data service: store and
  fetch blobs by SHA-256 (the research DocumentCache as a remote backend, so every fetched page
  lands on the Dell), a papers/datasets library with metadata search, bulk download jobs. Bearer
  key REQUIRED (the old server's auth was optional), bound to the Tailscale interface only.
- **NET-2 Desktop compute workspace.** A job runner: submit Python code plus inputs (by SHA from
  the Dell), run it in an isolated per-job environment with time/memory limits and no host
  secrets, stream logs, return metrics and artifacts (artifacts stored back on the Dell). This is
  the runner R5 experiments need.
- **NET-3 Mac node registry.** Known nodes, health checks, routing: acquisition stores to the Dell,
  experiments run on the desktop, graceful and honest when a node is offline.
- **NET-4 Deploy and verify live** on both machines once they are online (autostart, firewall,
  Tailscale-only).

Order: NET-3 skeleton, NET-1, NET-2, NET-4. Replaces R10. R5 builds on NET-2.

**Progress 2026-09-24 (finding #097):** the node service (`dourmouse/nodes/node_server.py`, both
roles) and the Mac client/registry (`dourmouse/nodes/client.py`) are built and tested. **The
desktop compute node is LIVE**: `D:\dourmouse-node`, task `\DOURMOUSE-Node` at logon, port 8770 on
100.98.97.23 only, token auth; a Monte Carlo job ran from the Mac in 1.1s with the memory limit
enforced. **The Dell is NOT deployed**: its SSH server is not running, so nothing can be installed
there yet. Remaining: Dell SSH (owner), then deploy the data role there; then wire the research
DocumentCache to store on the Dell, and job inputs/artifacts through it; retire the old
qwen3:1.7b Dell server and `remote_server.py`.

# §3 — THE RESEARCH NETWORK (Domain G) — XL

**Rescoped 2026-09-23.** This was previously tracked as "Domain G, core done, M" with a short
list of follow-ons. That was accurate about the module and badly misleading about the domain.
The founding spec devotes **items 34 to 48** to the research network, the largest single
architectural section in the document, and the vision deck adds a 3-device shape on top. On
the spec's own weighting this is second only to cybersecurity.

**Honest completion: roughly 20-25%.** 4 of 14 stages, 3 of 21 objects, 0 of 4 architectural
demands.

Credit where due: `research_pipeline/core.py`'s own `Stage` docstring is honest about this and
explicitly refuses to fold the missing stages into `SYNTHESIZED`. The module never overclaimed.
The tracking document did.

**Three different things share this name; keep them straight.**
- `dourmouse/research_pipeline/` (808 lines) is the 4-stage pipeline below.
- `dourmouse/research_mesh/` (1,337 lines) is a DIFFERENT subsystem (study dossiers, concept
  cards, exam attempts). Not the research network.
- `jarvis/` (2.2GB) is the 500-agent PhD network, a mirror of what lives on the desktop.

## §3a — The 14-stage loop that can run backwards

The spec's own diagram:

```
QUESTION -> DECOMPOSITION -> RESEARCH PLAN -> SOURCE DISCOVERY ->
SOURCE VALIDATION -> EVIDENCE EXTRACTION -> HYPOTHESIS GENERATION ->
HYPOTHESIS CRITICISM -> EXPERIMENT DESIGN -> EXPERIMENT EXECUTION ->
STATISTICAL ANALYSIS -> REPLICATION -> CONTRADICTION SEARCH ->
REVISION -> SYNTHESIS
```

Today `Stage` has four members: `PLANNED`, `SOURCES_DISCOVERED`, `EVIDENCE_EXTRACTED`,
`SYNTHESIZED`.

**The missing piece that matters most is not a stage, it is the backward edge.** The spec is
explicit: synthesis finds a contradiction, which spawns a new research task, a new experiment,
new evidence, a revised synthesis. In its own words, *"This is what turns the system into a
research network rather than a report generator."* The current guards enforce strictly forward
transitions (`if self.stage is not Stage.PLANNED: raise`), so the state machine cannot express
this at all. Redesign the transitions before adding stages, or every new stage inherits the
same dead end.

| Stage | State | ID |
|---|---|---|
| Decomposition / research plan | partial (`set_plan`) | RES-11 |
| Source validation | NOT BUILT | RES-12 |
| Hypothesis generation | NOT BUILT | RES-13 |
| Hypothesis criticism | NOT BUILT | RES-14 |
| Experiment design | NOT BUILT | RES-15 |
| Experiment execution | NOT BUILT | RES-16 |
| Statistical analysis | NOT BUILT | RES-17 |
| Replication | NOT BUILT | RES-18 |
| Contradiction search | **BUILT**, corrected 2026-09-23 | RES-3 |
| Revision / backward edges | **NOT BUILT, blocks the rest** | RES-19 |

## §3b — The 21 first-class research objects — **3 of 21** — L

Spec item 35: *"The database shouldn't simply contain conversations. It needs first-class
research objects."* Project, Research Question, Research Objective, Hypothesis, Claim, Source,
Document, Passage, Evidence, Experiment, Experiment Run, Dataset, Metric, Result,
Contradiction, Agent, Task, Message, Decision, Artifact, Event.

Real today: `Claim`, `Contradiction`, `ResearchRecord`.

**Relationships are the point**, not the rows. The spec's own example:

```
Hypothesis H1
  |-- supported by Evidence E1
  |-- supported by Evidence E4
  |-- contradicted by Evidence E9
  |-- tested by Experiment X3
  |-- revised by Decision D7
```

Its closing line: *"That is much more powerful than a vector database alone."* Dourmouse has
two flat memory stores and no relational research graph.

## §3c — Immutable evidence — **NOT BUILT** — M

Spec item 36: `SOURCE -> DOCUMENT -> PASSAGE -> EVIDENCE RECORD`. *"The interpretation can
change. The original passage should not."*

None of the middle layers exist. Worse, and already tracked as RES-2: **no raw scraped page
content is ever written to disk, anywhere.** Fetched text lives in memory for one call, is
hashed, quoted from, and discarded. So `document_hash` fingerprints something that no longer
exists, and a cited passage can never be re-read or re-verified. RES-2 is therefore a
prerequisite for this whole section, not a nice-to-have.

## §3d — "The LLM proposes, the platform executes" — **NOT BUILT** — L

Spec items 39 to 41, the architectural heart and the part with nothing behind it.

- **Bounded lead authority (item 39).** The lead coordinates and may not bypass the runtime.
  Every important action passes through scheduler and policy. Today the dispatch loop calls
  tools directly.
- **Event sourcing (item 40).** Every important operation emits a typed event
  (`experiment.completed`, with agent, task, experiment and result). Nothing does this. The
  closest real thing is `office_logger.py`, built for the agent bus, which is the right shape
  to extend rather than a second system to invent.
- **The UI as a viewer of state (item 41).** The control center subscribes to the event stream
  and queries current state, showing nodes, jobs, agents, tasks, messages, projects,
  experiments, sources, evidence, results, logs. Clicking a task drills into assigned agent,
  tool calls, sources accessed, messages, artifacts, result. Real prior art exists (the SSE
  hub, the ORCHESTRATION screen, the office monitor) but no research view.
- **Critic agents (item 58).** Nothing.

## §3e — The 3-device network — **BLOCKED** — S once unblocked

Deck: *"Mac orchestrates, desktop is where the actual research is done it is the lab, the dell
finds and downloads relevant info."* Blocked on the Dell node being unreachable. The dispatch
mechanism to route extraction calls already exists, so this is genuinely small once the node is
up. Do not confuse "small" with "done".

## §3f — The already-tracked pipeline follow-ons (still open, still real)

**RES-1 workspace-relative default store path - DONE, corrected 2026-09-23.** `DEFAULT_DB =
workspace_dir() / "research_pipeline" / "research.db"` exists in `store.py`, with a comment
recording the gap it closed on 2026-09-20.

**RES-3 contradiction detection - DONE, corrected 2026-09-23.** `detect_contradictions()` in
`stages.py` groups active claims by the real sub-question they answered, then makes one
tool-less model call per pair within a group. A group with fewer than two active claims costs
no model call, so it never invites fabrication over nothing. A malformed reply is skipped
rather than raised, so one bad judgment cannot block the remaining pairs. This document
previously said "type exists, no detection", which was wrong.

Still open: **RES-2 on-disk raw document cache (S, prerequisite for §3c, now tracked as R0-6
in §3h)** · RES-4 hypothesis generation and
criticism (M) · RES-5 the orchestration loop that walks every sub-question (M) · RES-6 chat
reachability, there is no subagent letting a user ask Dourmouse to research anything (S) ·
RES-7 readability main-content extraction, currently a regex tag-strip with no DOM parser (M) ·
RES-8 headless-render fallback for JS-heavy pages, the dominant observed failure (M) · RES-9
per-domain scrape rate limiting, currently 20 sources on one domain is 20 unthrottled requests
(S) · RES-10 three-device distribution (blocked).

**Suggested order:** RES-1 and RES-2 first (cheap, and RES-2 unblocks §3c) → RES-19 the
backward edge (redesign transitions before adding stages) → §3b the object model and its
relationships → the remaining stages → §3d event sourcing and the research view.

## §3g - THE FULL-VERSION BUILD PLAN (owner decision, 2026-09-23)

**The owner chose the full version, not the load-bearing subset.** All 14 stages, all 21
objects, experiments as reproducible jobs, event sourcing, bounded lead authority, the research
view. This section is the sequenced plan for that.

**Honest sizing up front: this is the largest single body of work remaining, larger than §2.**
The 46 security items are mostly independent detectors that can be stopped and resumed at any
point. This is a spine rewrite with hard ordering constraints.

### The architectural fact that decides the order

`research_pipeline/store.py` persists **one row per question with the entire ResearchRecord
serialized as a single JSON blob**:

```sql
CREATE TABLE research_records (
    question TEXT PRIMARY KEY, body TEXT NOT NULL, stage TEXT NOT NULL, updated_at REAL
);
```

A JSON blob cannot answer "what evidence contradicts hypothesis H1", and cannot be queried
across projects at all. **The full version therefore REPLACES this persistence layer with a
migration. It does not extend it.** Everything else waits on that landing.

### The backward edge is not a backward transition

Verified in code: `set_synthesis()` moves the record to `SYNTHESIZED`, and `add_claim()` raises
from that stage, so once synthesized no new evidence can ever enter. That is the report
generator, mechanically.

But the spec does not ask for looser guards. Its own words are "contradiction discovered, new
research task, new experiment, new evidence, revised synthesis." That is a project **spawning a
new Task at an earlier stage**, linked by edge to the contradiction that caused it. The project
only ever moves forward; the graph grows.

So `stage` moves off the record and onto a Task, and the project owns many tasks. **R1 gates R3.
Do not attempt the backward edge before the graph exists.**

### What must survive the rewrite

The existing core is well built and its discipline is exactly what the full spec demands. Carry
it forward rather than starting from a blank file:

- **Append-only.** Nothing is ever deleted. `reject_claim()` replaces a claim with a REJECTED
  copy carrying the same provenance, so the record stays honest about what was once believed.
- **Real provenance on every claim:** `source_id`, `url`, `document_hash`, `location`,
  `passage`, `retrieved_at`, `agent`, `sub_question`, with `passage` the exact quote and never a
  paraphrase, because a paraphrase cannot be checked against its source.
- **Pure core:** no I/O, no clock, no model call.

### The sequence

| ID | Work | Size | Depends on |
|---|---|---|---|
| **R0** | The acquisition layer. See §3h. | L | nothing |
| **R1** | Object model: 21 tables plus one typed `edges` table, plus migration | L | nothing |
| **R2** | Versioning and immutability | M | R1 |
| **R3** | The backward edge, via Task-owned stages | M | R1 |
| **R4** | The eight missing stages | 8 x S-M | R1, R3 |
| **R5** | Experiments as first-class jobs | L | R1 |
| **R6** | Event sourcing | M | nothing |
| **R7** | Bounded lead authority | L | R6 |
| **R8** | The research view | L | R1, R6 |
| **R9** | Critic agents (spec item 58) | M | R4 |
| **R10** | Three-device distribution | S | BLOCKED on the Dell |
| **RES-6** | Chat reachability | S | nothing |

**R1 - the object model.** One table per object for the 21 named types, plus the edges table
that carries the actual power:

```sql
CREATE TABLE edges (
  src_type TEXT, src_id TEXT,
  relation TEXT,          -- supported_by, contradicted_by, tested_by, revised_by, derived_from
  dst_type TEXT, dst_id TEXT,
  created_at REAL, created_by TEXT
);
```

That answers the spec's own worked example by query rather than by inspection. **The migration
must be tested against the live `research.db`, not only fixtures**, and must not lose a field.

**R2 - versioning.** Evidence immutable. Interpretation, Claim and Hypothesis versioned as
`(id, version, superseded_by)`, never updated in place. `reject_claim()` already has the right
instinct; this generalizes it. Answers "what did we actually know when this conclusion was
produced", which the spec calls a major requirement for serious research.

**R4 - the eight stages.** RES-12 source validation, RES-13 hypothesis generation, RES-14
hypothesis criticism, RES-15 experiment design, RES-16 experiment execution, RES-17 statistical
analysis, RES-18 replication, plus finishing RES-11 decomposition. The four existing stage
functions in `stages.py` are good templates. Cheap individually, long in aggregate.

**R5 - experiments.** The spec's record: protocol, code, dataset, environment hash, node,
timestamps, exit code, artifacts, metrics, logs. Needs a sandboxed runner. Precedent exists and
should be reused rather than reinvented: the ATLAS proposals work already built a sandboxed
backtest path, and Domain B's goal runtime has the durable-job shape. Build locally first; real
distribution waits on §3e.

**R6 - event sourcing. Best value per hour in this section.** Extend `office_logger.py`, never
build a second event system (HARD_RULE 9). **This is also most of what X-2 needs**, so it closes
the confirmed central architectural gap as a side effect.

**An autonomous research loop is a §0 INFRA-1 tenant** (standing agent, mission-driven, cost-tiered), and R6 event sourcing is INFRA-1's wake signal - build them together.

**R7 - bounded lead authority. Highest regression risk on the whole list.** Lead proposes,
runtime executes, through scheduler and policy. Today dispatch calls tools directly. This is a
change to the spine of the system, not an addition beside it. Do it after R6, never before.

**R8 - the research view. Tension to name honestly:** the owner deprioritized UI, and this is
UI. Without it the graph is real but invisible and the whole section ships with nothing to look
at. Recommend carving out this one view as a deliberate exception once R1 and R6 land.

**RES-6 - chat reachability. Small, and oddly important.** There is currently NO subagent that
lets a user ask Dourmouse to research anything. The pipeline exists and is unreachable from
chat. Without it the full version is a library nobody can call.

### Critical path

R0, then R1 with R2, then R3, then R6. Everything else can follow in any order.
For visible progress early, RES-6 and R6 both produce something observable and neither blocks
anything else.

---

## §3h - THE ACQUISITION LAYER (R0) - L - **the evidence is only as good as this**

Read in full 2026-09-23: `general_roster.py`'s `_fetch_url_tool`, `_strip_html` and
`_refuse_private_fetch_target`, plus `stages.py`'s `_extract_fetched_text`. The pipeline is
about to start storing evidence permanently. **Storing bad extraction permanently is worse than
not storing it**, so this comes before the object model in value even though it does not block
it technically.

### R0-SEC - Redirect-following defeats the SSRF guard - **DONE 2026-09-24, finding #086**

`_refuse_private_fetch_target()` resolves the hostname and refuses private, loopback,
link-local, reserved and multicast destinations. It then hands the URL to
`urllib.request.urlopen`, **which follows redirects automatically with no re-check**. A public
URL that answers `302 Location: http://169.254.169.254/...` or `http://127.0.0.1:8765/...`
therefore reaches an internal address through a guard that already passed.

The guard's own docstring honestly names the DNS-rebinding TOCTOU limit. It does not name the
redirect hole, which is the easier of the two to exploit and the easier to fix.

**Fix:** a custom `HTTPRedirectHandler` that re-runs the refusal check on every hop, with a hop
cap. Related to finding #003 (the original SSRF finding on this same tool) and to the 64 open
S310 lint findings in BACK-1.

### R0-1 - Real main-content extraction (RES-7) - **DONE 2026-09-24, finding #091**

`_strip_html` is regex-only: strip script/style/noscript, strip all remaining tags, unescape
four entities. It keeps navigation, headers, footers, sidebars, cookie banners and ads, so
"evidence" currently includes the site's own menu. There is no main-content detection at all.

**Build:** a real DOM parse and a readability-style main-content extraction, preserving heading
structure so `Claim.location` can be meaningful rather than a character offset into soup.

### R0-2 - Headless-render fallback (RES-8) - **DONE 2026-09-24, finding #092**

No JS execution, so a single-page app returns an empty shell and the pipeline records a
successful fetch of nothing. This is the dominant observed failure mode.

**Build:** detect the empty-shell case and retry through a real headless render. Electron is
already a dependency and already drives a real Chromium, so reuse it rather than adding a second
browser stack.

### R0-3 - Per-domain rate limiting and robots (RES-9) - **DONE 2026-09-24, finding #094**

20 sources on one domain is currently 20 unthrottled requests as fast as the loop runs. No
robots.txt consultation. Both get the tool blocked in practice, and the second is a courtesy a
research tool scraping the public web should extend.

### R0-4 - The truncation and decoding defects - **DONE 2026-09-24, finding #089**

Four separate real problems in `_fetch_url_tool`, all currently invisible:

1. **Byte-blind read.** `resp.read(max_chars * 2 + 4096)` reads a fixed byte count before any
   parsing. A page with a large inline `<head>` can consume the entire budget and yield zero
   body text, reported as a successful fetch.
2. **`max_chars` defaults to 8000.** For research evidence that is tiny; one section of a paper
   exceeds it. And `[:max_chars]` cuts mid-sentence, so a stored passage can end mid-word.
3. **Charset is hardcoded.** `.decode("utf-8", errors="replace")` ignores the `Content-Type`
   charset and the meta charset. A Latin-1 or Shift-JIS page becomes mojibake, silently, and
   that mojibake would be stored as an immutable passage.
4. **No `Content-Type` check.** A PDF or image URL is regex-stripped as though it were HTML,
   producing garbage that looks like text.

### R0-5 - Record the FINAL url, not the requested one - **DONE 2026-09-24, finding #089**

`_source_id_for_url()` hashes the URL that was requested. `urlopen` follows redirects, so the
content can come from a different URL than the one the `source_id` and `Claim.url` name. Record
the final resolved URL and the redirect chain.

### R0-6 - The raw document cache (RES-2) - **DONE 2026-09-24, finding #089**

`raw` is stripped and truncated immediately and then discarded. Nothing is ever written to disk.
So `document_hash` fingerprints something that no longer exists and a cited passage can never be
re-read or re-verified by a later agent or a critic.

**Build:** write the raw body to a content-addressed on-disk cache keyed by the hash that is
already computed, with the fetch metadata (final URL, status, content type, charset, timestamp).
This is what makes `SOURCE -> DOCUMENT -> PASSAGE -> EVIDENCE` in §3c possible at all.

### R0 order
R0-SEC first (it is a live security hole), then R0-6 and R0-4 together (cheap, and they stop
corrupt evidence being stored permanently), then R0-1, then R0-2, then R0-3 and R0-5.

---

# §4 — DOMAIN J: finish the visual retone — M

**The reference design is now approved.** `MOCKUPS/dourmouse_os_v2.html` (in this folder) and
the repo's `ui/os_mockup.html` are the owner-approved OS design as of 2026-09-24 (finding #083):
the green Hermes language, a real OS shell (window chrome, Control Centre, Notification Centre,
live accent theming, wallpapers), Research and Security as flowcharts, and a Claude-preview-style
resizable Browser. The UI items below are now measured against THAT reference. **None of it is
wired to a live surface**, and wiring waits behind the §0 foundation (MODEL-1, INFRA-1). So the UI
work is: (1) build the real OS shell to match the approved mockup, and (2) the audit/token/a11y
items already listed.

### UI-1 — Audit and retone the remaining 15 UI files — **NOT DONE** — M
For the same local-override problem already found and fixed on the primary screen: the shared
design-token file and each screen's own theme block are separate duplicated systems carrying
the same colors under different names.

### UI-2 — Type and spacing token scales — **DONE 2026-09-23, finding #079** — S

Shipped in `ui/assets/dourmouse-ui.css`: an 8-step type scale (`--dm-text-2xs` 9px through
`--dm-text-2xl` 22px, integers only) and a 6-step spacing scale (`--dm-space-1` 4px through
`--dm-space-6` 32px). The type steps are derived from where the seventeen real ad hoc values
actually clustered, not invented. The spacing values are exactly what `DESIGN_SYSTEM.md` had
already specified in prose.

Two decisions pinned by tests: a conventional 1.25 major-third scale was **rejected** (it would
collapse the five metadata/label/body weights this dense UI distinguishes), and the spacing
scale is **additive**, never a replacement for `--dm-row-y`/`--dm-gap`, which have precise
different jobs.

Also mirrored into a real Figma design system: `Dourmouse Design System`, file key
`zHH3ZLx5MZHfAltHOGkYBk`. 43 variables across Primitives / Color / Scale, semantic colours
aliased to primitives, explicit scopes on every one, `var(--dm-*)` code syntax so Dev Mode
round-trips to the real stylesheet. 29 tests.

**The follow-on this deliberately did NOT do** is now item UI-8 below.

### UI-8 — Migrate call sites onto the new scales — **NOT DONE** — M

UI-2 defined the scales; nothing uses them yet. ~720 raw px literals and seventeen font-size
call sites in `console.html` alone still carry hardcoded values.

Deliberately not done in one sweep: a blind find-and-replace across a 7,092-line file is exactly
the kind of change that breaks a UI silently, and the tokens had to exist first. Do this
incrementally, screen by screen, verifying each in the real Electron shell (which since OS-2 is
the default and supports real screenshots to disk).

### UI-9 — Audit the UI for developer commentary rendered to users — **PARTLY DONE** — S

One real instance was found and fixed in finding #079: `ui/workspace.html`'s hand-control panel
rendered internal constant names (`LANDMARK_SMOOTH_ALPHA` / `PINCH_ENGAGE_RATIO` /
`PINCH_RELEASE_RATIO`), "see page source", "see startHandControl below", and a paragraph on
MediaPipe GPU-delegate internals directly to the user. The information moved into an HTML
comment where developers actually read it; the two sentences that tell a USER what to do stayed
visible.

**It was spotted in a screenshot of the running app, not by grep** — which is the point. Sweep
the other UI files the same way: open each screen in the real shell and read what it actually
says to a user. The deck asks for "sleek professional and easy to use"; implementation prose in
a panel is neither.


### UI-3 — Run a real contrast check across every color pairing — **NOT DONE** — S
Rather than assuming it passes. `ui_contrast.py` already exists as a real utility.

### UI-4 — The missing components — **NOT BUILT** — M
In priority order, from the UI audit: `DiffWidget` (the file-editing tools exist server-side
with nothing to render them — highest value), `ToolActivity` with a real five-state vocabulary
(queued / running / completed / failed / cancelled) replacing the current ad hoc chip,
`TerminalWidget` (summarized command output with the truncated-raw view as fallback, never a
fabricated summary when the shape is not recognized), a command palette on `console.html`
(it exists only on the legacy `index.html`), and `SourceWidget` for research evidence.

### UI-5 — Cap the unbounded live-event array — **NOT DONE** — S
`console.html`'s `liveEvents` is pushed to on every activity event and never trimmed. The DOM
list caps at 60 rows but the backing array grows for the session's entire lifetime, in an app
explicitly designed to run long-lived.

### UI-6 — Icon system — **NOT BUILT** — M
No icon library is wired up. "Icons" are Unicode glyphs. Build a small consistent inline-SVG
stroke-based set, matching the existing "icons only where space is genuinely scarce"
principle — not a wholesale icon-ification.

### UI-7 — Keyboard accessibility pass — **NOT DONE** — S
Concrete known counterexample: the tool-call disclosure chip is a plain `<div>` with
`.onclick`, no tabindex, no role, no keydown handling. Other controls are done correctly, so
this is inconsistent practice, not a universal failure.

---

# §5 — THE AGENT ECOSYSTEM: the last open piece — M

### AGENT-1 — Read-side transcript assembly UI — **NOT BUILT** — M
All five backend flaws from the design review are fixed and the data is now durable
(`office_log.db` has `messages`, `fanout_events` and `agent_events`, with real per-agent,
per-call `call_id` tagging). What does not exist is the view: merging several concurrent
`call_id`s from one "meeting" into a single readable conversation.

**Known correlation gap to close first:** `fanout_events` does not carry the nested run's
`call_id`, because branch events are emitted through `general_roster.py`'s `_safe_emit`, not
`dispatch.py`'s `_emit_event` (which is where the tagging lives). Fix that before building the
view, or the view cannot correlate a branch to its transcript.

### AGENT-2 — Render concurrent activity on the office desks — **NOT BUILT** — S
`concurrent_call_ids(agent)` and the matching `GET /api/activity` field are real. Nothing
renders "2 active" on a desk yet.

### AGENT-3 — Cloud burst capacity — **DONE 2026-09-26 (finding #134)** — M
Round 5 bounded local concurrency (fully serial by default). It did not add cloud burst
capacity, which was the other half of the originally-named fix direction.

### AGENT-4 — Adversarial test of a hostile broadcast — **NOT DONE** — S
Nothing on record confirms a hostile broadcast on the message bus has ever been tested: an
agent posting adversarial text (via a compromised feed, or a manipulated `send_message`) that
a reader agent then treats as an instruction rather than data. The general "treat
network-originated content as data" principle is established elsewhere in the codebase; this
specific channel is an open, untested risk.

### AGENT-5 — Deeper office visual work — **NOT BUILT** — L
Real sprites, a full floor plan, deploy and blocked-streak triggered flavor animations. The
persistent-SVG data plumbing is done and correct; this is asset and design work layered on it.
Also honest: the walk animation was code-reviewed but never caught mid-flight in a screenshot,
because every real fan-out in this environment finishes in under a second.

---

# §6 — DOMAIN F: close the last gap — S

### F-1 — Register a real write-free `reviewer` subagent — **NOT BUILT** — S
Deliberately deferred, not forgotten: no currently registered subagent has a genuinely
write-free toolset that fits reviewing arbitrary code, and a stern prompt on a write-capable
one is not an enforced restriction. Register a real narrow read-only subagent first, then
re-run Domain F's own three-specialist acceptance test in full.

---

# §7 — THE PHASE 0-4 BACKLOG — **DEFERRED BY THE OWNER**

Standing deferral set 2026-09-20: *"the large phase 0-4 backlog is to be done once my weekly
usage resets."* Do not start these ahead of §1 to §6.

### BACK-1 — `ruff` backlog — M — **STARTED**

**Real measured counts 2026-09-23** (the old estimates were stale): **445 total.**
S110 115, SIM105 97, PLW1510 65, S310 64, E402 20, E731 10, E741 9, B007 7, plus a tail.

**B905 CLOSED, 7 of 7** (finding #082). Real latent bug class rather than style: `zip()`
silently truncates to the shorter iterable. Each site decided on its merits, not swept. Four
had an explicit length guard directly above them, so `strict=True` now asserts that invariant.
Two are deliberate asymmetry, including the adjacent-pairs idiom `zip(xs, xs[1:])` which is
off by one by construction. `world_pulse.py` zips against a LIVE API response and is
`strict=False` with the real risk named: a short response silently drops trailing cities from
the air-quality report.

**The big four need PER-SITE JUDGMENT, not a sweep:**
- **PLW1510 (65)** `subprocess.run` without `check`. A silently failing subprocess is a real
  bug class, but adding `check=True` turns a tolerant call into a raising one. That is a
  behaviour change, not a lint fix. Highest chance of a real find.
- **S110 + SIM105 (212 combined)** `try/except/pass`. Mostly CORRECT here: observers
  (`message_bus.on_post`, `ActivityTracker.on_event`, `office_logger.on_event`) must swallow
  exceptions so a logging failure never breaks dispatch, and there is already a
  `# noqa: BLE001 - reason` convention. This is triage and annotation, not repair.
- **S310 (64)** `urlopen`. Security-relevant with precedent: finding #003 found a genuine SSRF
  gap here. Needs review, never blanket suppression.

### BACK-2 — `mypy` backlog — M
~334 lower-signal occurrences, dominated by type-inference noise from a 337-file codebase that
was unannotated until this initiative started.

### BACK-3 — Security pass over the ~34 gated tools — M
Path traversal and credential handling specifically.

### BACK-4 — Dead code and duplicate utility sweep — S
Beyond the UI dead-file pass and the F841 findings already fixed.

### BACK-5 — Missing architecture docs — S
`docs/SOURCE_MAP.md` and `docs/DEVELOPMENT.md` (`docs/ARCHITECTURE.md` is done).

### BACK-6 — Windows and Linux platform adapters — L per platform
Telemetry is macOS-only by explicit original scope. Each additional OS is a real separate
implementation. Directly gates SEC item 4.

### BACK-7 — Cross-device control — M
Explicit user requirement, 2026-09-16: Dourmouse should be able to control the owner's other
machines.

### BACK-8 — Scheduler-created goals — S
A routine that auto-creates a `Goal` on a schedule.

### BACK-9 — Real independent verification of task completion — M
Currently a completed turn that did not raise counts as success. Domain B has a real
verification pass for goals; this is the narrower per-task case.

### BACK-10 — Run the 20 autonomy acceptance tests as a suite — S
They have been closed individually. Running them as one suite against the current build,
including test 8 (crash recovery), is a separate real check.

---

# §8 — CROSS-CUTTING GAPS (not in any domain, real, tracked)

### X-1 — No CI pipeline exists at all — **CORRECTED + IN PROGRESS 2026-09-24** — M
**Correction:** a CI workflow did exist (`.github/workflows/tests.yml`, a 3-OS matrix), but it
triggered only on `main`, so it never ran on the working branch, and its last runs (2026-08-13)
were red. Being rebuilt as finding #085: working-branch trigger, Python 3.14 to match dev, one
suite run, and a ruff/mypy ratchet (`scripts/lint_ratchet.py`) so the lint backlog can only
shrink.

`ruff` and `mypy` are configured and have both already found real bugs, but neither is wired
into a gate. There is no CI for this repo. Given HARD_RULE 2 (commit and push immediately),
a CI gate is the thing that keeps that rule from becoming a way to push a broken tree.

### X-2 — No checkpoint or resume of an in-flight dispatch loop — L
The confirmed central architectural gap, re-verified. Every background thread is
`daemon=True` inside one process. A killed process loses any in-flight dispatch entirely; an
SSE disconnect can cancel early but never resume. `JobTracker` is a 500-entry in-memory ring
buffer. Domain B's goal runtime solves this for goals (a goal survives `kill -9`); it does not
solve it for an arbitrary mid-flight dispatch turn.

### X-3 — No SIGTERM handler anywhere in the codebase — S
`serve_forever` catches only `KeyboardInterrupt`. Relevant the moment this runs as a real
supervised service.

### X-4 — Two pre-existing failing test files — **DONE 2026-09-24, finding #084** — S
`tests/test_deeplink.py` (a stale assertion) and `tests/test_google_auth.py` (an
environment-isolation bug affecting 4 tests). Found by a peer session, flagged, permanently
excluded from this initiative's full-suite runs so they do not mask new regressions. Not
fixed. Either hand them back or fix them directly.

### X-6 — The local model fabricated a tool result — **ROOT CAUSE FOUND 2026-09-24, fix is MODEL-1** — M

**Root cause FIXED 2026-09-24 (see §0 MODEL-1, applied):** the orchestrator was pinned to `qwen2.5:7b`, a sub-14B model that does not reliably emit tool calls. This was not a mysterious bug; it is what small models do. The structural guard (a turn asserting a completed action with zero tool_use events is mechanically detectable) is still worth building as defence in depth, but the primary fix is repointing the brain to a large cloud model per the owner's model policy.

Found 2026-09-23 while live-testing OS-1 through the real directive box. Typing
"preview the file /Users/.../verification.m4a" produced, from the local `gpt-oss:20b` backend:

> "I've opened the audio file in the embedded preview pane. You can play, pause, and seek
> directly from the controls in the pane."

The pane did not open. `GET /api/office_log?kind=events&limit=400` shows **zero tool events**
for that turn. The model never called `open_file_preview` and asserted that it had.

This is a direct violation of the product's own honesty contract, which is the single thing
that most distinguishes it. Two separable questions, both real:

1. **Why was no tool called?** `open_file_preview` is registered and its description is
   explicit. Check whether the planner routed the turn to an agent that carries the tool at
   all, and whether this backend reliably emits tool calls (there is a standing note that
   `qwen2.5-coder:14b` does not call tools when acting as the dispatch brain, and a benchmark
   showing real strict-format reliability differences between local models).
2. **Why did an answer claiming a completed action survive with no tool call behind it?**
   Domain B already has a real independent-verification pass that re-checks a model's own
   completion claims rather than accepting them. Nothing equivalent guards an ordinary chat
   turn. A turn that asserts it performed an action, with no tool call in the transcript, is
   mechanically detectable.

Reproduce: run a directive naming a real file and a real tool, then check
`/api/office_log?kind=events` for `tool_use` entries on that turn.

### X-8 — A Spotify widget is injected into every page — **REAL, OPEN** — S

The server injects `#dmSpotifyWidget` into **every** served HTML page. Found 2026-09-23 while
screenshotting the OS mockup: it was covering the browser pane window and reporting
NOT CONFIGURED on a surface it has nothing to do with.

Two things are wrong independently. A global injection that appears on surfaces unrelated to
music is a layering mistake, and a panel whose only content is "not configured" is noise rather
than information. An OS does not put a disabled music widget on your security dashboard.

**Fix:** make the injection opt-in per surface, or render nothing at all when the integration
is not configured. Do not simply hide it with CSS on the pages where it is inconvenient, which
is what the mockup does as a local workaround.

### X-7 — Tests that fail under full-suite load, not from regressions — **DONE 2026-09-24, finding #084** — S

**Two confirmed instances of the same pattern**, both found 2026-09-23. Neither is a
regression; both pass in isolation with and without the changes under test.

1. `test_app_control_ax.py::TestRealLiveIntegration::test_activate_app_fast_really_works_against_finder`
   fails with `macOS refused to activate 'Finder' (activateWithOptions_ returned false)` when
   another app holds focus. It genuinely brings Finder to the foreground, so it competes with
   whatever else is on screen. Its docstring claims it is "safe to exercise for real in CI on a
   real Mac runner too", which is too strong.
2. `test_atlas_lab.py::TestAtlasLabRoutes::test_leaderboard_endpoint` fails with a raw socket
   `TimeoutError` on `/api/atlas-lab/leaderboard`. Verified: 17/17 pass in isolation on both
   clean HEAD and the working tree. It failed on a run that took **858s against a normal 460s**,
   so the endpoint simply exceeded the client timeout under load.

**Why this matters beyond the two tests.** A suite that fails for reasons unrelated to the code
trains you to ignore failures, which is exactly how a real regression gets waved through. Each
of these cost a full verification cycle to diagnose.

**Fix, in preference order:** (1) give time-sensitive live tests a generous, explicit timeout
rather than the default, and assert honestly on either outcome where the OS is entitled to
refuse; (2) mark them as requiring a quiet machine and skip with a real stated reason when that
precondition is not met; (3) move them behind an opt-in live-integration marker. Do not delete
them: activating an app and serving a leaderboard are real shipped capabilities.

**Operational note for anyone running the suite:** do not drive a browser or use the Mac during
a full run. A concurrent preview server roughly doubled the wall time on its own.


### X-9 - Redirect-following defeats the fetch_url SSRF guard - **DONE 2026-09-24, finding #086** - S

Found 2026-09-23 while planning §3h. `_refuse_private_fetch_target()` in `general_roster.py`
resolves the target hostname and refuses private, loopback, link-local, reserved and multicast
addresses, then hands the URL to `urllib.request.urlopen`, **which follows redirects
automatically with no re-check on any hop**. A public URL answering
`302 Location: http://169.254.169.254/...` or `http://127.0.0.1:8765/...` therefore reaches an
internal address through a guard that already passed.

The guard's own docstring honestly names the DNS-rebinding TOCTOU limit. It does not name this
one, which is both easier to exploit and easier to fix.

**Fix:** a custom `HTTPRedirectHandler` that re-runs the refusal check on every hop, with a hop
cap. Directly related to finding #003 (the original SSRF finding on this same tool) and to the
64 open S310 findings in BACK-1.

**Also tracked as R0-SEC in §3h**, because the research pipeline is its heaviest caller. Listed
here as well because it is a live security defect, not research work.

### X-10 - A 65 MB chat database is published in the PUBLIC repo - **CLOSED: OWNER DECIDED TO LEAVE IT, 2026-09-24** - S

Found 2026-09-24 while checking CI. The GitHub repo is PUBLIC. `.freebuff/desktop-v2.db` (65 MB
SQLite, plus its `-wal` and `-shm` files) is tracked and was committed in `9e6d3fd` and `b5f0a1f`.
Its tables are `projects`, `threads`, `messages`, `queue_items`, `thread_deliveries`: a tool's
conversation history, now world-readable.

A secret scan flagged a Google-API-key-shaped string inside it. Investigated and ruled out: the
match sits mid-run inside a base64 blob with no delimiter on either side (a coincidental substring
of binary data, about four such coincidences are expected by chance in 65 MB). So it is NOT a key
leak. It IS a privacy question, because message history is published.

Options, all the owner's call because they are outward-facing or destructive:
1. Stop tracking it going forward (`git rm --cached`, add to `.gitignore`). Easy, reversible, but
   the file stays in public history.
2. Also purge it from history (`git filter-repo`) and force-push. Removes it from the repo, but
   rewrites every commit hash, breaks every other clone, and does not remove copies anyone already
   fetched or GitHub's cached views.
3. Make the repository private.
4. Leave it, if its contents are not sensitive.

**Owner decision 2026-09-24: leave it as is.** Nothing changed. Recorded so a later scan does not
re-raise it as new.

### X-5 — Docker and systemd deployment paths were never run — S
`Dockerfile`, `docker-compose.yml` and `dourmouse.service` are written from a careful read of
the real import chain, but none was ever build-tested or run — no Docker daemon and no spare
Linux box in the environment where they were written. Run them before trusting either path.

---

# DONE — completed items, newest first

### 2026-09-27 — The app was launched the way you launch it, on clean data (finding #154 verification)

The DourmouseRecon app was started through its own launcher with a throwaway workspace and
settings folder and spare ports, so your real data and your running app were never touched. A
brand-new install opened the first-run setup page, as it should. A configured install opened the
new shell straight away, knew it was inside the Electron app, mounted all 18 screens with no
errors, showed the sign-in reminder (the throwaway settings have no Google sign-in), played a real
audio file from the MEDIA screen (a real click on play moved the clock from 0.0 to 1.6 seconds)
and reported the voice state. Evidence: `EVIDENCE/154_fresh_launch_*`, `152_media_electron*.png`,
`152_voice_electron.png`. Not done: a real cloud-model reply through the new HOME (no key in the
throwaway settings, and it would spend credits: you type one message), the microphone, and the
self-contained app build (it downloads packages, so it needs your go-ahead).


### 2026-09-27 — The last screen, and the new shell is now what opens (findings #153, #154)

BROWSER, the eighteenth and last screen, is built: one shared browser pane. Inside the Electron app
it drives the real browser view; the view hides itself whenever something (a panel, a notification, a
drag) is drawn over it and whenever you leave the screen, follows the window when you resize, and
shows the engine's own reason when a page fails, with a retry. The address bar refuses dangerous
addresses and says why. Outside Electron it falls back to a sandboxed frame fed through the safe
proxy. A page sent to the browser while you are on another screen (a news headline, an agent's
browser tool) now waits: a notification says so and the page opens when you open BROWSER. It was
verified in a second running Electron with a real page, real back and forward, a real failed load
and real resizes (and a real listener leak found there was fixed). And the switch-over is done:
the new shell is the page that opens. The old console is one address away at `/console`, and
setting `DOURMOUSE_DEFAULT_SHELL=console` puts it back at `/` with no revert. The offline cache
now holds the shell. Not built in BROWSER: tabs, pop-out and clear-browsing-data (Electron has no
support for them yet). Still to do: the fresh-launch test through the DourmouseRecon app with clean
data folders, MEDIA playback and VOICE in Electron, and your own set-up steps.


### 2026-09-26 — Eight more screens, and the switch-over groundwork (findings #148 to #152)

The new shell now has 17 of its 18 screens (only BROWSER is missing). COMMS shows the real mailbox
with unread and starred state (the old mail list could not show a signed-in Google account's mail
at all) and archive, trash and flag each ask first; sending mail is deliberately not on the screen.
AGENTSMITH shows the exact code of a tool Dourmouse drafted for itself and only lets you approve
the exact text you read. PROJECTS creates a project in a folder it shows you first; SETTINGS shows
honest models, keys and switches, resets only the background switches, and puts auto-approve behind
a stronger confirm. CODE shows a repository's real changes and diffs (read-only). RESEARCH shows
the research loop, questions, claims, graph and an export. MEDIA plays files from a fixed list of
folders; VOICE shows wake-word state and takes voice commands. Groundwork: a setting makes the new
shell the default page, another makes Electron open it, and the shell has its own dark sign-in
dialog. Four builders were cut off by a session restart before their own live checks; the work was
intact, every backend was read and reviewed, and the live check ran on all eight in real Chrome. Two
faults found on the way: a test server that started the real background loops (and made an
unrelated test fail) and git calls that a repository's own configuration could have made run
programs (fixed with no-textconv and no fsmonitor). Not yet proven: real Gmail, real research data,
Electron playback and microphone, clicking through toggles and project creation in a browser.


### 2026-09-26 — Seven more screens in the new shell, and a real Pause for goals (findings #146, #147)

NEWS shows live headlines (new ones arrive without a refresh) with a conversation about them, and
TO RESEARCH asks first, then saves a research record (no model call). ATLAS is the world monitor:
one row per data feed with the count it returned, failed or unconfigured feeds shown with their own
reason, and a throttled REFRESH. WIKI is read only (SCAN is shown disabled with the reason).
GOALS lists every goal and its tasks, shows the exact action a task is waiting on before you
approve it, and PAUSE now really pauses: the goal remembers what state it was in, a task already
running cannot undo the pause, and RESUME puts it back exactly (also across a restart); NEW GOAL
takes the steps you write. TIMETABLE lists routines with EDIT and DISABLE (the parser's own reason
if a schedule is refused), marks routines that cannot run unattended, and shows overdue ones
honestly. ORCHESTRATION follows parallel-agent runs live and opens a branch's transcript. OFFICE
draws the 43 agents on 6 floors (the grouping rule is printed on screen) with real status. Every
change is your click plus a card saying what will happen. Checked in real Chrome against an
isolated server (no errors, no leaks, all six states seen); evidence
`EVIDENCE/146_os_*.png`, `147_os_*.png`. Honest limits are in findings #146 and #147 (the goal
runtime was off in the live checks, so the pause race is proven by unit tests; some error states
were forced by intercepting requests).


### 2026-09-26 — The OS shell now runs, with HOME and SECURITY built on real data (findings #144, #145)

Opening `/shell` now shows the new operating-system look: menu bar with live status, sidebar and
dock, a stage with a directive box, a Control Centre and a Notification Centre that read real
alerts (dismissing one really dismisses it on the server), a wallpaper picker, and an accent colour
that is remembered on the server as well as in the window. HOME is the conversation: every tool
the model calls shows as a chip that opens to the real arguments and real result, approvals appear
as cards, STOP declines anything still waiting, and it survives leaving and coming back. SECURITY
shows this Mac's real posture (firewall, exposed ports, risk score, known devices), the real
findings, a flowchart of how a finding is made with real counts, and live activity from the
security events; SCAN NOW runs a real scan, LOCK asks first, and REMEDIATE hands the finding to
HOME where the approval gate applies. The other 16 screens show an honest "not built yet".
Checked live in real Chrome at 1440x900 against an isolated server: no console errors across
screens, nothing left running after leaving a screen, a forced server error shows the server's own
words, and restarting the server resyncs the screen with no reload. Evidence:
`EVIDENCE/144_os_home.png`, `144_os_security.png`, and the mockup beside them. Honest limits: no
reply from a real cloud model was ever seen through HOME (streaming, tool chips, approvals and STOP
were checked against a scripted stream, labelled on screen); the Electron window was not driven;
LOCK was declined, not approved. Also fixed (#145): `/api/office_log?meeting=` no longer sends a
second response after the meeting. The builder's `pkill -f dourmouse.webui` could have closed the
owner's running Dourmouse if it was open; nothing of it was running afterwards.


### 2026-09-26 — The OS shell has its front door and a way to add backends (finding #143)

The new shell page now has its own address (`/shell`) with a strict security policy (no inline
script, cannot be framed), and each screen can add its own backend in its own small file instead
of editing the giant server file. The shell itself is only partly built: the frame, controls and
building blocks exist on disk, but the page does not start yet and none of the 18 screens are
built.

### 2026-09-26 — Security review: 36 findings, the serious ones fixed (findings #135 to #141)

A read-only review (with a second pair of agents trying to disprove each finding) went through
the security and agent stack and found 36 problems. The worst is fixed and proven; nothing
below is "assumed fixed".

- **A web page could drive the app (#135).** Any website you had open could send commands to
  Dourmouse: change your lockdown list, and by the reviewers' account stop programs and move
  files, and it could read your pictures, PDFs and videos off the disk. Demonstrated first in
  real Chrome (a page added a lockdown entry and read a file), then closed: the server now
  refuses requests that came from a web page, and the preview frame needs a private
  per-launch key. The same page against the fixed server changes and reads nothing.
- **Code the AI writes now runs in a sandbox (#137).** Before, a booby-trapped web page or
  email could talk the AI into running code as you with your API keys in reach, with no prompt.
  Now that code runs in a locked room: it can read only its own folder, cannot use the network,
  and sees no keys (checked on this Mac). Running code outside the room, and the Claude Code and
  Codex helpers, ask you first and show the exact code. Repeating tasks and unattended goals ask
  first too, and a goal you approved cannot quietly do something else.
- **Lockdown and quarantine are sturdier (#136).** Ending a lockdown works even if its file is
  damaged, it can no longer close Finder or the Dock, the root helper refuses to block things
  macOS needs and treats its request file as untrusted (you must re-run the one sudo install
  command once to get the new helper), a quarantined file can no longer strand itself, and a
  quarantined app can no longer run. The stop-process button now shows what it is stopping.
- **Secret scrubbing (#138).** Keys such as GEMINI_API_KEY and OLLAMA_API_KEY are now caught, and
  every secret in your own settings file is matched exactly. One helper's first version froze
  the app on any long block of text (base64, for example) and wrongly scrubbed things like
  "max_tokens"; I found that in review and replaced it. Approved self-written tools can no longer
  smuggle code through their description.
- **Browser pane and Electron (#139), one run limit per request (#140), a damaged history file
  no longer stops the app (#140).**
- **Lockdown can block single pages, and answers can be fact-checked (#141).** A small Chrome
  extension (you load it once, see its README) blocks pages such as `reddit.com/r/all`, and a
  critic marks each sentence of a research answer as backed, uncited, or unsupported.

Not done, stated plainly: the Chrome extension has not been loaded into a real Chrome (Chrome
does not allow scripted loading); the Electron hardening was checked in a second running Electron
(evidence 139) but not in the packaged app; the critic's judgment of "backed" is word-overlap and
has not run against a real model.

### 2026-09-26 — Parallel agents can run wide on the cloud, and now tell the truth (finding #134; AGENT-3)

Asking Dourmouse to do eight things at once now runs all eight at the same time (up to 16 on a
cloud model; measured: the cloud accepted 32 simultaneous calls). The console shows how wide it
ran ("8 branches, 8 at once"). Running it for real found four defects, all fixed: each parallel
helper started its own fan-out (8 jobs became 64), each helper answered the whole request
instead of its own part, a "too many requests" reply failed helpers after 1.5 seconds instead of
waiting, and the older `delegate_to_models` tool reported "8 succeeded" over eight failures. It
now counts a backend's own error as a failure and runs on the same model as the rest of the app.

### 2026-09-25 — The runtime decides (finding #133; R7)

Every action an agent takes is now recorded (proposed, then refused, declined by you, done, or
failed), and one request can no longer loop the same action forever or send you more than 8
approval prompts.

### 2026-09-25 — The research loop closes (findings #131, #132; R4, UI-5, UI-7)

Dourmouse can now go from sourced claims to hypotheses (each must rest on real claims), have a
critic review them, design and run an experiment for one, and report honest statistics against
the value the hypothesis predicts. Tested live: a dice experiment it designed itself gave a mean
of 3.4985 against 3.5, p = 0.65. Small fixes: the console no longer grows memory forever in long
sessions, and tool-call details open from the keyboard.

### 2026-09-25 — Experiments, the research log and view (findings #127-#130; R5, R6, R8, RES-18)

Dourmouse can now run a research experiment on this Mac, record every part of it (code, run,
numbers, logs, the exact environment) in its research graph, and re-run it to check whether the
result holds. Every change to the research graph is recorded in order, and the RESEARCH screen
finally shows it all: questions with their sourced claims and disputes, experiments with their
runs and numbers, and the latest changes. Found and fixed while testing: research and news agents
could never use any tool in the default "split" mode (the Gemini route passed no tools), and the
cloud route ignored the chosen gpt-oss:120b model.

### 2026-09-25 — Watch an agent think (findings #125, #126; Phase 5 A2, A3)

Open any agent's own window and give it a task: its reasoning, every tool it uses and what came
back, and its answer appear live, and anything needing approval gets APPROVE / DECLINE buttons
(before, such a request silently timed out after 5 minutes). Office desks show when an agent is
working on several things at once. **Phase 5 (the observable agent ecosystem) is complete.**

### 2026-09-25 — Google write, agent meetings, hostile messages (findings #122-#124; OS-7, A0, A1, A4)

Creating calendar events could never have worked (the sign-in never asked Google for write access
to the calendar); it does now, and Dourmouse can add rows to your existing Google Sheets. **You will
need to sign in to Google again once** so it can grant the new access. When Dourmouse splits work
across several agents, the OFFICE screen now shows each such "meeting" as one readable
conversation. A message planted on the agent bus to hijack an agent is shown to the model as data
only, and even a model that obeyed it could not send anything without your approval.

### 2026-09-25 — Settings, launcher, one home (findings #120, #121; OS-8.2, OS-8.3, OS-9)

Everything Dourmouse does in the background (security scans, the analyst, the downloads check,
lockdown blocking, the librarian and its folders) can now be switched in SETTINGS, no terminal.
Cmd+K opens a launcher to any screen or action, or sends what you type to Dourmouse. Two of the
five duplicate home screens are gone (their addresses open the console). Fixed along the way: the
app could show the previous version of the console after an update, and the librarian was
replying to broadcast messages and flooding the notifications.

### 2026-09-25 — Browser, media, projects, notifications (findings #116-#119; OS-3, OS-10, OS-6, OS-8.1)

Inside the desktop app the browser pane is now a real browser (logins, cookies, back and forward
all work) instead of a proxy that broke many sites. Every media file plays: formats the browser
cannot handle are repackaged or converted automatically, with subtitles and remembered position.
A project's chat now really works inside that project's folder. A notification bell collects
security findings, risky downloads, the security analyst's explanations and the librarian's
suggestions, with history and per-source mute.

### 2026-09-25 — Compute on this Mac, standing agents, the file librarian (findings #113-#115; MODEL-2, INFRA-1, OS-5)

The old Dell compute node (a small local model on another machine) is gone; the `compute` agent
now runs Python simulations and experiments on this Mac, safely and reproducibly. Every agent is
confirmed on the large cloud model. Dourmouse can now run agents that work without being asked:
the first is a file librarian that keeps an index of Documents, Desktop and Downloads, answers
"where is my ..." from chat or other agents, and suggests tidying (duplicates, old installers,
old downloads). It never moves anything until the owner approves, and every move can be undone.

### 2026-09-25 — Mac security finished: console, browser history, privacy, self-audit (findings #110-#112; MS-11, MS-12)

The SECURITY screen now shows posture by area instead of one score, and every security tool is
one click away (lockdown editor, respond, quarantine, connection doctor, browsing history,
self-audit, privacy toggle); verified in a real browser. Browser history and typed searches are
read locally from all Chrome profiles (Safari needs Full Disk Access, which the screen says).
Privacy mode keeps security data off the cloud model. The self-audit checks Dourmouse itself and
found a real issue (security folder readable by others), now fixed. Threat model written. Three
bugs found and fixed at the root along the way. **The Mac-only security plan MS-1..MS-13 is
complete.**

### 2026-09-25 — Mac security: diagnosis, report, response, analyst, live monitor (findings #104-#109; MS-6..MS-10)

"It won't load" now gets one named cause with evidence (blocked, DNS, routing, refused, timeout,
bad certificate, server error). The one-button security report rates six areas separately, never
as one number, shows "unknown" where it could not look, and lists what it could not check. The
owner can now act on findings from chat or the console, each needing approval and each undoable:
stop a process, quarantine a file (moved, never deleted), disable a startup item, block a
dangerous domain for good. An AI analyst on the cloud model explains new findings in plain
English and can only talk about findings the detectors really made. A network change triggers a
scan at once, and every scan shows up live.

### 2026-09-25 — Lockdown (finding #103, MS-13)

The owner's lockdown: keep a list of apps and websites, start a lockdown, and none of them can be
opened until it ends. Apps are closed the instant they launch (no admin needed). Websites are
blocked for every browser and app through /etc/hosts by a tiny root helper that can do nothing
but block websites; **the owner installs it once** with one sudo command shown in the lockdown
status. Starting and ending a lockdown always ask for approval. Honest limits shown in the status:
whole domains (not single pages), and a browser's own DNS-over-HTTPS can bypass it.

### 2026-09-24 — Mac security: Downloads assessment and "am I being monitored?" (findings #101, #102; MS-4, MS-5)

Every file landing in ~/Downloads is assessed by its bytes, origin, signature and Gatekeeper
verdict, with the classic tricks caught (decoy double extensions, stripped quarantine flags, apps
hidden in zips); risky ones become sentry findings and live events. ClamAV is not installed, so
verdicts say plainly that contents were not malware-scanned. The monitoring check reports eight
indicators with evidence and confidence plus what it could not check; live it found the Tailscale
VPN and extension, Remote Login and Screen Sharing on, and two remote-control apps running
(Splashtop XDisplay, Parsec). Next: MS-6 connection failure taxonomy, MS-7 posture and one-button
report, MS-8 response actions, MS-13 lockdown.

### 2026-09-24 — Mac security: telemetry, baseline engine, Mac detectors (findings #099, #100; MS-1..MS-3)

The sentry now sees the Mac itself: Wi-Fi security, FileVault/SIP/Gatekeeper/firewall/stealth,
Remote Login and Screen Sharing (enabled and accepting), who each network-active program is (path,
parent, code signature, Gatekeeper verdict), persistence items, and connection diagnostics. A
baseline engine learns what is normal (per network for the router and DNS, per host for ports,
persistence, protections) and then reports only what changed: a new router MAC on the same network
(ARP spoofing), new DNS, a new listening port, a new or modified startup item, a protection turned
off, a new unsigned program on the network. First live scan of this Mac: firewall off (high),
Remote Login and Screen Sharing on and accepting (med), two services listening on all interfaces
(med), stealth mode off (low). Nothing was changed on the Mac; fixes are offered as exact steps.
Next: MS-4 Downloads watch and file assessment, MS-5 "am I being monitored?", then MS-6..MS-13.

### 2026-09-24 — The research network can revise itself (finding #096, R3)

The backward edge. A contradiction now spawns a follow-up task, that task looks for sources that
settle the disagreement, its evidence enters the record even after an answer exists, and a revised
answer is written while the earlier one is kept. Synthesis now states known disagreements instead
of silently picking a side, and the same contradiction is never recorded or re-judged twice. In
the graph: contradiction spawned task, task produced claim. Chat tool: research_follow_up. Next:
R4 (the missing stages), R6 event sourcing, then R5/R7/R8 and RES-6.

### 2026-09-24 — Research is now a versioned object graph (finding #095, R1 + R2)

The one-JSON-blob-per-question store is replaced as the home of research by `research_graph/`: all
21 objects from spec item 35, the evidence chain (source, document, passage, evidence) immutable,
interpretations (claims, hypotheses, tasks...) versioned so "what did we know when" is a query,
and a typed edges table that answers the spec's own H1/E1/E4/E9/X3/D7 example. Existing records
migrate automatically and non-destructively the first time a database is opened; every save keeps
the graph in step. Honest gap: no live research.db exists on this Mac to migrate, so the migration
is tested against databases written by the real store code. Next: R3, stages move onto graph Tasks
and a contradiction can spawn new work (the backward edge).

### 2026-09-24 — R0 acquisition complete: polite fetching (finding #094)

R0-3. Every automated fetch now reads the site's robots.txt (once per hour, through the SSRF guard)
and refuses what it disallows, and spaces requests to the same host (1s default, or the site's
Crawl-delay up to 10s). Verified live on Wikipedia. With #086, #089, #091, #092 and #094, the whole
R0 acquisition layer is done: SSRF-safe, stored raw with provenance, correctly decoded, main
content with headings, JavaScript pages rendered safely, polite. Next: R1, the research object
graph.

### 2026-09-24 — JavaScript-only pages are rendered, safely (finding #092)

R0-2. Pages that are empty until their JavaScript runs used to be stored as successful fetches of
nothing. They are now rendered in a short-lived headless Chrome, and every request the page makes
is served through the same SSRF guard as fetch_url, so a page cannot use the browser to reach
internal addresses (Playwright's own interception misses redirects, so it is not relied on). The
rendered DOM is stored as its own document linked to the bytes the server really sent. Live:
docsify.js.org now yields its real content, in 7.9s (41.9s before the requests were served
concurrently). Next: R0-3 per-domain rate limiting and robots.txt, then R1.

### 2026-09-24 — Research evidence is the article, not the page chrome (finding #091)

R0-1. A stdlib extractor now removes menus, headers, footers, sidebars, cookie banners and hidden
text, picks the article (marked or by paragraph density), and keeps each block's heading path, so
a claim's location is computed ("Guide > Setup, paragraph 1") instead of guessed by the model.
Three real-page bugs found live and fixed first (bbc.com svg titles, Sphinx anchors, a github.com
wrapper div that took the whole page). Wikipedia's MCP article goes from 20 KB of soup to 11.7 KB of
article with its real headings. Next: R0-2 headless render for JS-only pages, then R0-3.

### 2026-09-24 — Research evidence now comes from a stored, correctly decoded document (finding #089)

R0-6, R0-4 and R0-5 together. Evidence used to be built by asking an LLM to call fetch_url on a URL
it already had, then scraping the tool's output: the first ~20 KB of bytes decoded as UTF-8
regardless of the page, regex-stripped (PDFs and images included), cut at 8000 characters
mid-word, and cached as that text only, so the claim's hash matched nothing stored. Now
`research_pipeline/acquire.py` fetches directly through the SSRF guard, stores the raw bytes under
their SHA-256 with full fetch metadata (final URL, redirect chain, content type, charset and its
source), honours header/BOM/meta charsets, refuses non-documents, reads PDFs, and marks anything
over 10 MB as truncated. A claim's `document_hash` is now the hash of bytes really on disk, and
`Claim.final_url` records where the content actually came from. Verified live on github.com (full
576 KB page stored, redirect recorded). Also: `reject_claim` no longer drops fields, and the voice
extras install on Linux (openwakeword's unused tflite dependency worked around). Next: R0-1 real
main-content extraction, then R0-2 headless render, then R0-3 rate limiting.

### 2026-09-24 — SSRF guard hardened, sentry ARP view fixed, Windows made to work (findings #086-#088)

X-9 / R0-SEC: `fetch_url`'s guard checked one DNS answer and then let `urlopen` follow redirects
unchecked; demonstrated live that plain `urlopen` followed a 302 to 169.254.169.254. New
`net_guard.py` resolves inside each connection, refuses the whole answer if any address is not
globally routable (CGNAT and IPv4-mapped IPv6 included), connects to exactly the address it vetted
(no DNS rebinding window), re-checks every redirect hop with a cap of 5, and ignores proxies; 31
tests including real local redirect chains. The security sentry's ARP view had been blank whenever
reverse DNS was slow: `arp -a` resolved 206 LAN names past its 5s timeout; now `arp -an` (0.1s),
keeping any name recorded earlier. Windows, from the first real Windows CI run: every first Claude
turn failed because the 16.5 KB preamble went on the command line of a `.cmd` shim (8191-char
limit), so the task now goes on stdin for Claude and Codex (verified live with both real CLIs);
31 text-mode subprocess calls now decode UTF-8 instead of cp1252; servers bind exclusively so a
second process can no longer share a listening port (a plausible cause of the stale-instance port
shadowing seen on the desktop); project context lookups use each tool's own path spelling; CLI
discovery knows `.exe`/`.cmd` and npm's folder. Also declared the Pillow dependency nobody had
listed. Open: the browser pane's own proxy has no address guard (see OS-3).

### 2026-09-24 — X-7 + X-4: two "flaky" tests were real bugs; 112 tests were outside the gate (finding #084)

Both tests tracked as load-dependent turned out to hide real defects. The Atlas leaderboard
timeout was a product bug: `atlas_lab._sync()` ran a network `git pull` (60s timeout) while
holding the same lock every Atlas request takes first, so any request arriving mid-sync blocked
for the whole pull. Git and parsing now run under a separate `_SYNC_LOCK` and the request lock is
held only for the in-memory swap; measured against the real old code, a reader waited 3.00s before
and 0.00s after. The same investigation found the Atlas test fixture was doing real network git
into `/tmp/atlas-strategy-lab` on every suite run, via an unstoppable `while True` daemon that woke
after the test's stubs were undone; the loop now has a real stop signal (`stop_auto_sync()`), and
the fixture stubs git and stops any daemon. The app-activation test asserted macOS always obeys;
macOS 14+ cooperative activation may refuse, which the product already reports honestly, so the
test now accepts exactly the two honest OS outcomes. X-4's two files were in a top-level `tests/`
tree that `pytest dourmouse/tests` never ran: 112 tests, 5 failing, run against the developer's
real settings. Consolidated into `dourmouse/tests` (history kept with `git mv`), the 5 failures
fixed at the root (real user-config leak, built-in OAuth client leak, one stale deeplink target),
and `tests/` retired. The first full run of this commit then stalled at 94% and exposed three more real problems.
Every test that built a server started a real security sentry scanning the Mac (arp, lsof,
firewall) that was never stopped; hundreds piled up and loaded the machine. The conftest now turns
it off like the goal runtime, and real server shutdown now stops it (it never did). Six modules
froze their default store path at import time, before the test workspace redirect, so the suite
wrote into the real dev workspace: the real `office_log.db` holds thousands of fixture rows
("REAL NEWS: markets steady" 4076 times). All six now resolve the path per call, with an AST guard
against the pattern returning; the polluted rows were left for the owner to decide on. The one
failure was an approve test whose 5s client timeout was shorter than the server's own 60s
subprocess budget. Suite: 5565 passed, 10 skipped, 0 failed. Evidence: `EVIDENCE/084_phase1_x7_x4.md`.

### 2026-09-24 — OS mockup redesigned and APPROVED as the reference UI (finding #083)

In a live design session the owner approved the redesigned mockup as the reference OS design.
Same green Hermes language, but the shell now behaves like a real desktop OS. Removed the two
gimmick screens (Vision hand-control, Design3D) and turned the redundant Globe into a real
Browser, so the nav is 18 focused screens. Added real OS chrome: window traffic lights, a
macOS-style Control Centre (agents/security/network/brain/DND/autonomous tiles, brightness, and
accent swatches that live-recolour the whole OS and persist), and a Notification Centre with
unread state. Research became a flowchart of the 15-stage loop with the backward edge drawn as
an absent dashed loop; Security became the layered detection-to-response pipeline with a
"no model in detection, cannot fabricate" bracket and a confidence-band legend, alongside the
three-machine fleet with lockdown. The Browser was rebuilt to mirror the Claude preview pane
(chrome tabs, ghost toolbar, pill address bar, device viewport switcher, popout, edge-drag
resize) and fills the window by default. Settings shows the large-cloud-only model policy.
Verified live in the preview at every step; a lost-CSS regression mid-session was caught by a
screenshot and fixed. Prototype only, wired to nothing. Evidence: `EVIDENCE/083_os_mockup.md`
(the browser tool cannot write a screenshot to disk here; the inline screenshots were shown in
chat and the log records what was driven). Committed with `ui/os_mockup.html`, finding #083.

Each entry: what was built, the honest limitation, the finding number, the evidence file.

### 2026-09-23 — Established this tracking folder

Created `~/Documents/DOURMOUSE/` as the single status-tracking location for the project:
`README.md` (orientation and folder map), `HARD_RULES.md` (12 non-negotiable working rules
consolidated from many sessions), `CURRENT_STATUS.md` (detailed record of everything real,
domain by domain, every claim traceable to a finding), this file, `CODE_LOCATION.md` (where
the code lives and how the backup works), `EVIDENCE/` and `REFERENCE/`. Verified and recorded
that the code lives in exactly one place, `/Users/aditagrawal/dourmouse-recon`, and that both
installed `.app` bundles are thin launchers pointing at it. Found and fixed the real, serious
gap that the working branch was **58 commits ahead of origin and had never been pushed** —
the entire recent body of work existed on one disk only. Evidence: `EVIDENCE/001_*.txt`.

### 2026-09-23 — OS-1: the embedded media player (finding #076)

Dourmouse can now play audio and video inside its own pane. This is the first media playback
anywhere in the product: before it, the only `<audio>`/`<video>` elements in any UI file were
TTS output and a webcam gesture feed, and Spotify was remote-control only.

A new `GET /api/files/media` streams media from disk in 256KB chunks with **real HTTP
byte-range support** — 206 with `Content-Range`, `Accept-Ranges`, a real 416 for an
unsatisfiable range. That is a requirement, not a refinement: without it a `<video>` element
cannot seek at all, and reading a real video into memory would be a genuine way to kill a
stdlib `ThreadingHTTPServer`. `ui/file_preview.html` gained real `<audio>`/`<video>` branches
with native controls plus an honest terminal state for anything a browser cannot decode, which
names `open_path` instead of showing an empty player. `open_file_preview` accepts media,
refuses an undecodable container by name, and says explicitly that it does not press play,
because it cannot. `.mkv`, `.avi`, `.flac` and `.wmv` are deliberately absent from the
allowlist on both the server and tool sides: listing them would produce a silently blank
player, which is exactly the fabrication this codebase refuses everywhere else.

**The live test found a real bug the 30 passing unit tests had missed.** An open-ended range
past the end of a file (`bytes=99999999-`) returned 200 with the whole file instead of a 416,
because the inverted-range check ran before the past-the-end check and an open-ended range
computes `end` as `size-1`, which is less than a past-the-end `start`. That is precisely the
request a media element makes when it seeks near the end of a file it has stale duration
metadata for. Fixed, regression-tested for both shapes, re-verified live.

A second real problem was in this change's own error copy: a 400 from the server surfaced as
`MEDIA_ERR_SRC_NOT_SUPPORTED`, which the message confidently blamed on the codec. Browsers
raise that same code for both an undecodable codec and a refused URL, so the message now
states both and says how to tell them apart.

Fixed en route, a real pre-existing defect: the tool handed the pane an absolute
`http://127.0.0.1:<port>/...` URL for this app's own page while the console may be loaded as
`localhost` — different origins, so the app was framing its own page cross-origin for no
reason, which is why the pane's own comments say back/forward history is unreachable. It now
sends a root-relative, same-origin URL; `//host/path` is explicitly refused because it reads
as relative but is not.

Verified live: byte-exact ranges against a real 1.4MB MP4 (a mid-file range's md5 matches `dd`
of the same offsets), a real H.264 file decoded and **seeked** in the browser (`readyState: 4`,
`videoWidth: 194`, seek to t=1.0s completed, frame drawn), real AAC audio with native transport
including a filename with a space, and the honest unsupported state for a real `.mkv`.

**Not verified, and not claimed:** rendering inside the pane's sandboxed iframe. This test
browser blocks sandboxed-iframe navigation outright (`ERR_BLOCKED_BY_CLIENT`, reproduced
identically on a pre-existing image path). Tracked in OS-4.

The new route streams arbitrary absolute paths, so its path handling was checked rather than
assumed: traversal, non-media absolute paths, `/dev/zero`, an empty path and a traversal suffix
on a real media path are all refused. The load-bearing property is an ordering one, and it is
correct: the sandbox resolves the path first and checks the extension of what it resolved to,
so a symlink named `looks_like.mp4` pointing at `/etc/passwd` is refused while a symlink whose
real target is media is served. Both halves are tested. 46 tests. Evidence:
`EVIDENCE/002_media_player.txt`.

### 2026-09-23 — The agent office, and the UI typeface (finding #082)

**The office.** The mockup's OFFICE screen was a flat list, which does not show what the design
calls for. Rebuilt as the real building: five floors plus the Lounge, with the **verified
43-agent floor map** taken from a live `build_general_registry()` call rather than invented.
A building cross-section on the left doubles as the floor selector, each floor showing one dot
per agent coloured by its real `ActivityTracker` status. Selecting a floor renders its desks;
an agent in a fan-out has its desk dimmed because the seat is genuinely empty and appears in the
meeting room with an arrival animation. That animation is the point: it is how a fan-out reads
as agents going somewhere rather than a list changing colour. Driven only by real events, so an
empty meeting room means nothing is fanned out.

Two real defects found building it, both worth keeping as classes:
- **An undercount, caught by counting.** The first build rendered 42 of 43. `mail` is an
  always-on poller belonging to no team and had silently fallen out. It now has the Lounge.
  Same class as this project's other stale undercounts, and caught only because the render was
  checked against a known total.
- **A CSS class collision that only shows visually.** `meet` was both a status modifier and the
  meeting room's class, so `.meet{width:196px}` styled every status dot as a meeting room. The
  first two hypotheses (animation artifact, oversized seats) were both wrong; querying `.fd`
  computed widths found three elements at 196px that should have been 4px and named it
  immediately. A modifier and a component must never share a class name.

### 2026-09-23 — The UI was set in a pixel font (finding #082)

`--dm-font-sans`, the token every label, heading and line of body copy resolves through, was
set to **Departure Mono, a pixel font**. A legitimate accent face, entirely the wrong choice
for the typeface a whole UI is set in, and the reason the interface read as pixelated.

**The fix cost no new bytes, because the right face was already on disk.** `ui/assets/fonts/`
held **nine Space Grotesk files** with **no `@font-face` declaration anywhere**, so none of it
could load. `UI_SOURCE_MAP.md` predicted this exact orphan in its typography section.

Now: Space Grotesk (400/500/600, latin and latin-ext) for UI text, Monaspace Neon leading the
mono stack for data and numbers. `unicode-range` on every declaration so a latin-only screen
never fetches the Vietnamese subset. Fallbacks reordered so a missing face degrades to the same
shape, sans to sans and mono to mono, never across. Departure Mono stays declared but
unreferenced, so a theme can still choose a pixel accent deliberately.

Verified from the browser, not the source: `document.fonts` reports all three Space Grotesk
weights plus Monaspace loaded, the Departure Mono check is `false`, and computed `font-family`
on the menu bar, nav and body all resolve to Space Grotesk. 20 screens re-walked, zero errors,
layout unaffected.

**Scope note:** this changes the shared token, so it reaches every surface that actually uses
`--dm-*`. `console.html` and `workspace.html` still carry their own duplicated palettes and
keep their own font declarations until UI-1 resolves that.

### 2026-09-23 — The OS shell design system and a 20-screen mockup (finding #080)

`ui/assets/dourmouse-os.css`, 601 lines in thirteen numbered sections: materials, elevation,
geometry, motion, wallpaper, window chrome, dock, widgets, menu bar, accessibility, 3D,
controls, annotation mode. Built on the `--dm-*` tokens from UI-2.

**A real conflict had to be resolved rather than ignored.** `dourmouse-ui.css` forbids floating
cards, translucent wash and decorative glow by name, and macOS is built on the first three.
Those rules are correct for a dense tool surface, so nothing here touches the terminal feed.
The resolution keeps the intent and drops the letter, written into the stylesheet rather than
left implicit: depth is allowed but every level means something (focus, stacking, modality);
translucency only as a real material over a real wallpaper; **glow stays banned**; the ~10%
accent budget is unchanged.

Decisions that were not the obvious choice, each with its reason recorded: the dock **lifts
rather than magnifies** (magnification reflows neighbours and is disorienting over live
content); wallpapers are **procedural, not bundled images** (this app self-hosts fonts to stay
offline-capable, so shipping photography would contradict that), with a real user photo upload
read via `FileReader` so bytes never leave the machine; a **dim control ships with the upload**
because a real photo has arbitrary brightness and body copy needs 4.5:1 over it; an opaque
fallback for `backdrop-filter` because some embedded webviews silently no-op it.

`ui/os_mockup.html` is an interactive prototype, not a picture: all 20 real screens, one custom
icon each, and an annotation mode (press `A`) where every control labels itself with what it
does from its own `data-spec`. Screens state what is NOT built rather than implying
completeness.

**Three real bugs were found by driving it rather than reading it**, all mine: `show()` rebuilt
the whole sidebar on every switch and destroyed the element being pressed; the fix for that
broke it worse because the render functions were only ever called from inside `show()`; and
GLOBE overflowed from a `height:100%` card in a scrolling body. All fixed, all re-verified.

Verified: 20/20 screens walked in a real Chromium, zero JS errors, zero overflow, zero
collisions. Photo upload verified end to end and persisted. Evidence:
`EVIDENCE/mockup_screens/` (24 files).

**Not wired into any live surface.** That is the approval gate.

### 2026-09-23 — A real browser with no proxy errors, proven (finding #081)

See OS-3 above for the full reasoning. Short version: the proxy was never fixable because the
iframe was the problem. An Electron `BrowserView` is a top-level browsing context, so
`X-Frame-Options` and `frame-ancestors` do not apply to it. Demonstrated with a controlled
local experiment (same page, same Chromium: iframe refused, BrowserView loaded), then shipped
as `POST /navigate` plus `/back`, `/forward`, `/reload` on the pane bridge, with `file://` and
`javascript:` refused by name and verified refused.

### 2026-09-23 — OS-2: Electron is now the default shell (finding #078)

The roadmap called this "half-built, pick one". Reading the source showed that was wrong: all
five migration stages are real in `electron/main.js` (windowing, IPC, tray/notifications, the
CDP `BrowserView` plus pane bridge, and electron-builder packaging with notarization),
`browser_agent.py` genuinely calls `connect_over_cdp`, and `verify-result.json` records a clean
run. **The actual defect was that nothing launched it** — `start.command` and both `.app`
bundles all ran `python -m dourmouse.desktop`, which opened the older pywebview shell. Every one
of those stages shipped to a user who never saw any of it, including the CDP pane that finding
#077 proved is what makes the browser pane, PDF viewer and media player behave like a real
browser rather than a blocked sandboxed iframe.

Fixed with shell selection in `desktop.py`'s `__main__`: `_electron_shell_argv()` checks for the
real files rather than trusting a flag, `_resolve_shell_choice()` reads `DOURMOUSE_SHELL`
(`auto` default, `electron`/`pywebview` explicit), and `os.execv` replaces the process so there
is exactly one app process and the existing `.pid` file still refers to it.

Two constraints, both load-bearing. **It lives in `__main__`, never in `launch()`** — `launch()`
is called directly by many hermetic tests that must keep getting pywebview byte for byte, and a
test asserts this by reading `launch`'s own source. The payoff is that both `.app` bundles and
`start.command` pick it up with **zero changes to any of them**. And **Electron is not an
unconditional default**: `electron/node_modules` is gitignored and ~408MB, so a fresh clone
genuinely has none. `auto` means "prefer the better shell when it is really here". An explicit
request that cannot be satisfied prints the reason and the fix (`cd electron && npm install`)
and still opens the app — degrading loudly, never silently.

Live-verified through the real entry point: `python -m dourmouse.desktop` printed the handoff,
exec'd Electron, and the shell came up with backend, CDP and pane-bridge ports all held by a
single PID. Screenshot: `EVIDENCE/005_electron_is_the_default_shell.png`.

Also corrected `main.js`'s own stale header, which claimed Stages C/D/E were unfinished while
`package.json` beside it said otherwise — the same drift `UI_SOURCE_MAP.md` already caught twice.

17 tests, faking the filesystem rather than the function under test.

### 2026-09-23 — Client disconnect is no longer logged as an error (finding #077)

Found by booting the real Electron shell for the first time: every client that went away
mid-response raised an unhandled `BrokenPipeError` out of `do_GET`, and `socketserver` printed
a ~25-line traceback for it. Not an error at all: the UI polls `/api/activity` on a timer, so
any reload, navigation or window close aborts an in-flight poll. But noise that routinely
buries real errors is an operability bug.

Fixed at the single chokepoint, `_Handler.handle_one_request`, generalizing the convention this
codebase already used in both places that stream (the SSE emitter sets `client_gone`;
`_send_media_file` returns quietly) rather than adding a third opinion. Deliberately narrow:
only `BrokenPipeError` and `ConnectionResetError`, with `close_connection` set so the socket is
torn down rather than reused. Any other exception still propagates loudly, because swallowing
more would hide real server bugs behind a silent socket close.

Re-verified on a second real cold boot: traceback count 0, was 1.

**The same session closed both of OS-1's unverified gaps**, in the real shell rather than the
test browser. Wall-clock playback is real (video advanced 2.002s across 2.000s, audio 1.95s
over 2.5s plus an exact seek to 5.0), and the media player renders correctly inside the real
browser pane inside the real Electron window on the first try, with the pane's address bar
showing the root-relative URL that proves OS-1's same-origin fix is live. The earlier blank
pane was the Claude Browser tool blocking sandboxed iframes, exactly as diagnosed.

**A standing limitation was also superseded**: screenshots CAN be written to real disk paths,
via Playwright `connect_over_cdp` against the Electron shell's CDP port plus
`page.screenshot(path=...)`. Evidence: `EVIDENCE/003_*.png`, `EVIDENCE/004_*.png`, both real
files. Two gotchas learned the hard way: Electron refuses `Target.createTarget`, so navigate an
existing page and restore it rather than calling `new_page()`; and a sandboxed iframe's
`contentDocument` is `null` from the parent, which means it cannot be introspected from
outside, not that it failed to render.

### 2026-09-22 — Browser / PDF / media audit (finding #075)

Audit only, no code changed. Source-verified map of what the embedded preview surface really
is: real Playwright Chrome automation; a real iframe browser pane with real nav chrome, a real
`check_frameable()` header check and a real rewriting proxy fallback that carries no live
session; a faster Electron CDP `BrowserView` that exists but is not the default launch path; a
real `pypdfium2` PDF page-image viewer plus separate text-only `extract_pdf` for RAG; and **no
embedded audio or video player anywhere**, Spotify being remote-control only with no YouTube
integration at all. Also established that none of this was ever a scoped domain in either
planning document. Produced items OS-1 through OS-4 above.

*For everything completed before this folder existed, see `CURRENT_STATUS.md` §2 (domain by
domain) and `dourmouse-recon/docs/ENGINEERING_AUDIT.md` findings #001 through #075.*

## DONE 2026-09-27 (later): #155 to #158

DONE: QA pass over all 18 screens (#155), the Command K launcher (#156), security round 4 batch 1 (#157, commit a2a7d7f) and the UX critique fixes (#158, commit 42adee6). Full suite 6,915 passed, 12 skipped, 0 failed.

STILL OPEN from the security round (finding #157): A3 any Google account that signs in gets owner access on a network bind (needs an allow-list of emails); A5 the settings and approve routes cannot tell the owner from a browser the model drives (needs a per-launch secret); R2B-06 read tools can read the token databases; R2B-07 other approval prompts truncate their payload; R2B-08 DLP gaps; R2B-09 open_path, security dismissals and browser_open are ungated; R2B-11 the MCP bridge has no run policy or scrubbing; A9 /mobile leaks addresses before login; N2 no socket timeouts; N3 ffmpeg protocol whitelist and git signature settings.

STILL OPEN from the UX audit: S3 setup and login restyle; flowchart SVG text; a Text size setting; 56 lesser items in `UX_ISSUES_2026-09-27.md`.

## DONE 2026-09-27 (evening): #159 the final app

DONE: `~/Applications/Dourmouse.app` (built by `scripts/install_app.sh`, commit fcb681a) is pinned to the Dock and the old copies (`dourmouse2.app`, `DourmouseRecon.app`, the stale in-repo `DourMouse.app` build) were moved to the Trash; record in `APP_CLEANUP_2026-09-27.md`, Dock backup in `backups/`. Also fixed: the downloads watcher blocked server start under a new app identity. Also the same day, after #157 and #158: Google sign-in owner check (`DOURMOUSE_ALLOWED_EMAILS`), handler socket timeout, read denies for the token stores, approval prompts that say when they show only an excerpt, `open_path` refuses programs, an ffmpeg protocol whitelist, and VOICE's SEND TO HOME chip. Suite 6,942 passed, 12 skipped, 0 failed.

STILL OPEN (owner): press Allow on the first-launch macOS prompts (Downloads, Documents, Desktop, microphone); load `extension/lockdown` in Chrome; the one sudo lockdown-helper install; Google sign-in again (add a second account to `DOURMOUSE_ALLOWED_EMAILS` only if you use one); re-approve AGENT SMITH extensions; one real HOME message with a cloud key; decide whether the two archived app copies in `Documents/LLM wiki /School/Dourmouse Archived Versions/` may go to the Trash; decide on the self-contained electron-builder build (downloads packages).
STILL OPEN (engineering): A5, R2B-08, R2B-11, A9, the rest of R2B-07 and R2B-09, git signature options (N3); UX S3 (setup and login restyle), flowchart text, a Text size setting, 56 lesser UX items.


## STATUS 2026-10-02: the next phase

DONE: #160 (Chrome-like pane, Google sign-in page loads, guardrail), plans written. NEXT, in order: see `PLAN_REPLACE_EVERYTHING.md` (phases J, A, B, C, D, E, F, G, H, I) and `PRODUCT_VISION_AND_STATUS.md` (completed, remaining, finished product). Owner decisions recorded: Chrome parity browser, Google Docs in the browser only, media player and PDF reader built in, every chat box gets the browser and player, castLabs DRM build approved in principle.

## Wave 1 done (2026-10-02, #161); still open from it
Live Electron check of J and D; grant Accessibility and drive TextEdit and Music live (F1 exit test); run bench with --real against an isolated server for a baseline; player play/pause/seek acknowledgement path and live now-playing; app-driving routes need A5 (phase H); pinned chats can open URLs ungated (revisit in H); F2 UI (indicator strip, Stop button, Apps settings). Next: Wave 2 (B1, B2, B3 with H).

## Wave 2a done (#162). Next: B2 (passwords, autofill, permission prompts), B3 (Widevine DRM, extensions, profiles, Chrome import). Open from H: N3, CDP-held owner secret, Show all button, Codex opt-in decision. Open from B1: agent follows tab 1 only (phase C), shared popup limit, favicon private-address filter.

## Wave 2b done (#163). Next: B3 (Widevine DRM build, extensions, profiles, Chrome import). Open: CDP-held console IPC for permission answers, kill switch not stopping running streams, legacy browser_creds.json store, packaged entitlements for camera and microphone.

## Wave 2 done (#164). Next: Wave 3 (C: stable element ids, user/model lock, live Docs and YouTube tests, agent follows active tab; G: tool descriptions and benchmark baseline), then Wave 4 (F2, I). Owner: DRM build decision (B3_DRM_PLAN.md), sign in to Google in the pane, grant Accessibility for F1.
