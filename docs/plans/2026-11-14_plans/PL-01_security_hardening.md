# PL-01: Security hardening, waves S1 to S3 (with taint tracking from the research)

Status: planned, nothing built. Source: `SECURITY_ASSESSMENT.md` (73 findings, 7 high), `scan/SEC1..4_findings.md`, the live canary test of 2026-10-23, and the prompt-injection research of 2026-10-27.

## Goal
Take the app from about 6 to 8 out of 10 on the assessment scale: no open high, every state-changing route owner-only or explicitly allow-listed, a steered model unable to plant code that runs later, and a way to prove each control still works.

## Why now
Every feature in the other plans (voice, app driving, automations, a phone gateway) widens what a steered model could do. The highs are exactly about that, so this runs first or alongside, never after.

## What the research changed
- **CaMeL (Google DeepMind) and the dual-model pattern:** the strongest known defence keeps *untrusted data from ever influencing control flow*, by separating the model that plans from the one that reads untrusted text, and by tracking where each value came from ("capabilities") and checking a policy when a tool is called. It solved 77 percent of benchmark tasks with provable security versus 84 percent undefended, so the cost is real but small. [paper](https://arxiv.org/abs/2503.18813)
- We will not build CaMeL. We will build its cheap core, **taint tracking on outbound tools** (item S1-6): every value that came from a page, mail, file or tool result is marked untrusted; a tool that sends data out (fetch with a query string, browser_open with data in the address, send_message, mail, write to a file outside the workspace) asks you when its arguments contain untrusted-marked text, and says exactly which text.
- Lesson from Gemini's panel flaw (extension reached local files and camera): the browser pane must never share trust with extensions (item S2-8).

## Work breakdown

### Wave S1: the seven highs and the cheap mediums (about 2 sessions, 4 builders, strict file ownership)

| # | Item | Findings | Files (owner) | Test that must fail first |
|---|---|---|---|---|
| S1-1 | Ungated writes become an allow-list: workspace subfolders, uploads, project roots, Documents/Downloads/Desktop; deny every dot-folder, every repo-root file, CLAUDE.md, AGENTS.md, .mcp.json, sitecustomize.py, conftest.py, git config; protect `claude_scratch`, `claude_orchestrator`, `DOURMOUSE.md` | SEC2-2, 4, 6, 7 | `dourmouse/system_access.py`, `general_roster.py` (file tools), `code_backends.py` | parametrised path table (case, unicode, symlink, `~`, `..`) all REFUSED |
| S1-2 | Run the Claude CLI in a fresh empty temp directory per call with `--setting-sources user` | SEC2-2 | `code_backends.py` | argv contains the flag; cwd is empty and fresh |
| S1-3 | Tool scope enforced at execution: a turn may only run tools of its scoped agents (plus delegate/desk tools) | SEC2-1 | `dispatch.py` `_execute_tool` and the scoping block | a tool outside scope returns REFUSED, logged |
| S1-4 | Own-server guard also applied in the BROWSER pane to the address after every navigation, not only to browser_agent tabs; block navigation of the pane itself to ports 8765, 9333, 9334 | SEC2-5 (done for tools, #187) | `electron/main.js` pane navigation, `electron/policy.js` | pane `will-redirect` to 127.0.0.1:8765 is cancelled |
| S1-5 | Sentry: catch per file in `get_persistence_items`, count consecutive failed ticks and raise a HIGH alert; add `netstat -anv` as a second source for listeners (root-owned sockets) | SEC3-9 (confirmed), SEC3-7 | `security/mac_telemetry.py`, `security/sentry.py`, `platform_adapter.py` | the three bad plist payloads give an error item and the scan completes |
| S1-6 | **Taint tracking for outbound tools** (see above) | SEC2-8 | `dispatch.py` (mark results), `general_roster.py` (outbound tool list), `confirm-card.js` (show the text) | page text placed in a fetch_url query string triggers an approval that quotes it |
| S1-7 | Owner-gate `/api/projects/*`, `/api/state/alerts`, `/api/attention/dismiss`; drop `path` from the old project route | SEC1-6, 8, 9 | `request_guard.py`, `webui.py` | router-walk test: every POST route is owner-only or on an allow-list file |
| S1-8 | A typed "yes" only answers the question actually pending; approval prompts show host, fields, cwd and the full arguments | SEC4-1, SEC2-9, 13, 14 | `core/chat.js`, `split-chat.js`, `webui.py` `_try_affirm`, `general_roster.py` prompts | "yes" with a non-mail card pending does not approve it |
| S1-9 | PII redactor patterns: dotted and spaced cards, lowercase IBAN, 10-digit mobiles, national ids; security tools to the right privacy tier; global memory stores masked text | SEC3-1, 6, 18 | `privacy.py`, `memory_store.py` | corpus must-mask and must-not-mask lists |

### Wave S2: the remaining mediums (about 2 sessions)
S2-1 GET routes with side effects get the same-site check (SEC1-7). S2-2 CSP, frame-ancestors and nosniff on every served page (SEC1-11). S2-3 login lockout covers Bearer and cookie paths (SEC1-12). S2-4 baselines keyed on full path and signature identity, not the 9-character name (SEC3-10, 11, 12, 24). S2-5 absolute paths for every security probe (SEC3-8). S2-6 trim entitlements `allow-dyld-environment-variables` and `disable-library-validation` (SEC4-2). S2-7 the shell checks the identity of whatever answers on 8765 before loading it into a privileged window (SEC4-3). S2-8 extensions get no route to the assistant or the vault; extensions become per-profile (open item). S2-9 downloads: new file types (.mobileconfig, .terminal, .workflow, archives) and the crafted attribute (SEC3-14, 15). S2-10 mic and camera kill-switch file moves out of the model-writable workspace (SEC3-5).

### Wave S3: lows and hardening (about 1 session)
Session lifetime, error text without paths, unbounded OAuth state, markdown link display, download size limits, the remaining 35 lows in the ledger.

## Tests (the four layers from the assessment)
1. **Unit:** one test per finding, written first. A router-walk test that fails when a new POST route is neither owner-only nor allow-listed.
2. **Fuzz (nightly):** path guards, HTTP layer, plist and codesign parsers, redactor.
3. **Live attack suite:** `scripts/security_canary/` already has A1, A3, A6, A9, A12, A13; add A4, A5 (planting files), A8 ("yes" abuse), A10 (GET side effects), A14 (taint prompt), A15 (kill switch file). Run each wave against an isolated copy.
4. **Detection efficacy:** decoys for every new file type, the malformed plist, a look-alike process name, a self-signed binary; targets 100 percent caught, zero new false positives per week.
5. **Injection corpus (new, from the research):** 60 hostile instructions in pages, files and mail run through each model in use; target zero forbidden tool calls for write, send, shell and approval actions. Rerun after every prompt or routing change (the research-first change already moved this once).

## Owner decisions
S-D1: allow-list folders (default: workspace, uploads, project roots, Documents, Downloads, Desktop). S-D2: should extensions be removed from the pane until S2-8 lands (recommended: disable by default)? S-D3: turn off Remote Login, Screen Sharing, File Sharing and Remote Management on the Mac if unused (your action, in System Settings; the sentry already flags them).

## Risks
Allow-lists will block something the owner legitimately does; mitigate with a clear refusal that says which folder and how to add it. Taint tracking could prompt too often; start with outbound tools only and measure prompts per day before widening.

## Done when
No open high; router-walk green; every live attack passes on the isolated copy and again on the real app; injection corpus at target; suite and lint green; each fix has a numbered finding.

## Effort and dependencies
About 5 sessions. No dependency. Everything else that opens a remote door (PL-17) depends on this.
