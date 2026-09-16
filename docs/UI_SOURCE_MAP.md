# UI Source Map — real state as of 2026-09-16

Produced by a full read-through audit ahead of the Phase 3 (UI/UX redesign) work in
`docs/GODSPEED_ROADMAP.md`. Every claim below is file:line grounded, not assumed. Two prior
in-code comments were already found to be stale during this audit (see §6) — treat other
in-file comments as directional, not authoritative, until re-verified.

## 0. Entry-point reality check

- `dourmouse/webui.py:1804-1825` — `GET /`, `/console`, `/console.html` → serves `ui/console.html`
  (redirects to `/setup` first if unconfigured).
- `dourmouse/webui.py:1826-1832` — `GET /dispatch`, `/index.html` → `ui/index.html`, the legacy
  "HUD," kept alive only for deep-link compatibility.
- `dourmouse/webui.py:1856-1864` — `GET /workspace` → `ui/workspace.html` ("Vision" floating-panel UI).
- **The desktop launchers do not open `/`.** `dourmouse/desktop.py:919-927` sets
  `initial_href = "/workspace"` explicitly. `electron/main.js:664,712` both
  `loadURL(`${BASE_URL}/workspace`)`. So **workspace.html, not console.html, is the actual boot
  screen** in both the pywebview app and the Electron shell — console.html is one click away via
  workspace.html's own `← CONSOLE` link (`ui/workspace.html:524`). Redesign scope must cover both,
  workspace.html slightly more so.
- Three parallel native-shell efforts exist: pywebview (`dourmouse/desktop.py`, shipped), Electron
  (`electron/main.js`, self-described "not yet a distributable app" in its own header comment),
  and a barely-started Tauri+React scaffold (`dourmouse-native/app/`, still mostly default Vite
  template files). Electron carries no frontend of its own — it points at the same Python-served
  HTML (`electron/main.js:44-45`), so auditing `ui/` covers all three shells at once.

## 1. Framework / architecture

- **No framework.** Vanilla HTML/CSS/JS. The only ES module imports anywhere are vendored
  Three.js (`ui/assets/vendor/three/`, used by `console.html` and `workspace.html`).
- **One giant inline script per page.** `console.html`: one `<script>` IIFE, lines 1469-6827
  (~5,358 lines, 161 top-level functions), plus a module script 6840-7090 for a Three.js editor.
  `workspace.html`: main script 581-2621 (~2,040 lines) plus a module script. No build step, no
  bundler, no shared `.js` included by multiple pages.
- **Routing**: no client router, no History API on the two live surfaces. `console.html` uses
  `show(name)` (`:1723`) toggling `display` across a 14-entry `SCREENS` array (`:1476`) — no URL
  changes, no back button, nothing deep-linkable. Only `index.html` (legacy) has real hash routing.
  Cross-page navigation is full page loads / new native windows.
- **State**: module-scope globals inside the IIFE (`console.html:1501`, per-screen maps at
  `:1523-1542` added specifically to fix cross-screen state bleed). `localStorage` for durable
  prefs (theme, code-tool choice), `sessionStorage` for per-tab identity (`TAB_ID_KEY`,
  `ACTIVE_PROJECT_KEY`, `:1860,1873`) so browser tabs don't collide. No state library.
- Backend is also framework-free: `dourmouse/webui.py` is a stdlib `http.server.ThreadingHTTPServer`
  subclass, no Flask/Django.

## 2. Styling system

- A real shared design system exists: `ui/assets/dourmouse-ui.css` (425 lines), linked by 17 of
  the `ui/*.html` files, with a `--dm-*` token set and `.dm-*` utility classes, and an explicit
  header stating what it exists to prevent (floating cards, translucent washes, decorative glow,
  icon/badge vomit).
- **It's essentially unused where it matters.** `dm-` token/class occurrence counts: `console.html`
  **0**, `workspace.html` **0**, `hud.html`/`setup.html`/`login.html`/`study.html`/
  `atlas_lab.html`/`all_hands.html`/`agent.html`/`mobile.html`/`voice.html` **0** each; vs.
  `index.html` **235**, `design-system.html` **125** (its own reference demo), `os.html` **7**,
  `app.html` **4**, `map.html` **1**. The two surfaces that actually matter today link the shared
  sheet only for its `@font-face` rules, then each defines its **own separate `:root` token set**
  from scratch — `console.html:60-114` and `workspace.html:75+` maintain near-identical but
  independently-authored palettes under the same token names (`workspace.html:63-64` admits "225
  call sites" depend on keeping those names). A palette change today means editing at least two
  places by hand.
- Color is tokenized (console.html's own comment at `:1804`: "every colour already routes through
  var()"); **spacing is not** — zero `--space-*`/`--gap-*` tokens against ~720 raw `px` literals in
  the same ~1,156-line style block. Typography is equally ungoverned: 18 distinct font-size values
  in half-pixel increments, no type scale.
- Theming: `[data-theme="x"]` on `<html>`, set by `themeSet()` (`console.html:1938-1951`),
  persisted to `localStorage`. **8 real themes** (`arc`/`aether`/`protocol`/`keep`/`studio-void`/
  `studio-ink`/`studio-paper`/`studio-frost`, `:1804-1854`) — two in-code comments still say "four
  skins" (`:118,1799`), stale relative to the object below them. `workspace.html` has **zero**
  `data-theme` selectors — one fixed dark look, no picker, unlike console.html.
- A routing comment (`webui.py:1820-1821`) says console.html is "nine screens" — it's actually
  **14** (`console.html:1476`). Same drift pattern as the theme count.
- No CSS build step; hand-written `<style>` blocks plus one plain `.css` file.

## 3. Icon system

- **No icon library is actually wired up.** `ui/assets/vendor/lucide/lucide.min.js` (358 KB) is
  vendored but referenced by zero `.html` files — dead weight.
- "Icons" today are Unicode glyphs styled via CSS: `◆` (33 occurrences, decorative bullets),
  `▸`/`▾` (8, disclosure carets), `✕` (3, close buttons), `●` (2, status dots) — plus text labels
  for everything else (tabs, nav, buttons are all words). No consistent icon system exists.

## 4. Typography

- Self-hosted fonts (34 files under `ui/assets/fonts/`) — deliberately offline-capable per
  `dourmouse-ui.css:36-39`. A lot of typefaces for one app (Departure Mono, Intel One Mono,
  Monaspace Neon, JetBrains Mono, Fira Sans, Space Grotesk, Share Tech Mono, Sekuya, Orbitron,
  Rajdhani); several weights not obviously wired to any current theme token — check for orphans
  during the redesign.
- Three type-role tokens exist and are mostly correctly assigned: `--mono` (dense/machine text),
  `--display` (structural headers), `--font-prose` (chat bodies, `console.html:671`) — the right
  instinct.
- **But it breaks under theming**: the `aether` theme reassigns `--mono` to `'Space Grotesk'`
  (`console.html:141`), a proportional sans, so every call site expecting monospace column
  alignment silently loses it in that one theme. Token name and semantic contract diverge.
- No type scale — sizes are ad hoc per component.

## 5. Component inventory (file:line, or "doesn't exist")

- **Conversation rendering**: `addYou()` (`console.html:2550-2554`), `addReply()` (`:2555-2604`),
  hand-built `.turn` divs. Per-screen threads via `threadFor()` (`:2530-2533`).
- **Markdown**: `md(src)` (`console.html:2287-2322`), ~35 lines, regex-based. Supports fenced code
  (language token captured then discarded), inline code, bold, flat lists, scoped image syntax,
  plain links. **No headings, blockquotes, tables, nested lists, italics, or strikethrough.**
  Duplicated verbatim in `ui/os.html` — not shared as a module.
- **Code blocks**: same `md()` path → plain `<pre><code>`, hardcoded theme-independent background
  (`console.html:685-688`, a deliberate 2026-09-13 dark-on-dark contrast fix). **No syntax
  highlighting anywhere** — no hljs/Prism/Shiki in the repo; the language identifier is parsed and
  discarded. A copy button is injected via `wireCopies()` (`:2635-2643`). **No diff rendering
  exists** despite real `diff_preview`/`edit_file`/`write_file` tools already registered.
- **Tool-call/activity rendering**: `act()` (`console.html:2605-2634`) — a collapsible chip
  expanding to raw `<pre>`. Tool args shown as pretty-printed **raw JSON**
  (`JSON.stringify(...,null,2)`, `:2993,3183`); tool results dumped as raw text (`:3001`) — no
  per-tool-type formatting (a shell command's stdout and a file's contents render identically). A
  static `WORK` lookup (`:2330-2446`) maps ~110 tool names to friendly labels; unmapped tools fall
  back to `"Using <name>"`.
- **Multi-agent rendering**: `delegate_parallel_branch` events get labeled chips per branch
  (`:3005-3021`). A dedicated ORCHESTRATION screen exists (`paintOrchestration()` `:6538` and
  related, a live fan-out table) — real, not fake.
- **File operations**: no dedicated component — funnels through the generic tool chip. A separate
  page-level `ui/file_preview.html` handles PDF/image preview via iframe, unrelated to inline chat.
- **Terminal output**: no distinct styling versus any other tool result.
- **Research/evidence/sources**: no dedicated component. Web search results render as plain
  markdown text through `md()` — no source cards, no domain/favicon chips.
- **Security center / network topology / device monitor**: **does not exist.** Closest analog is
  `ui/map.html`, which is an *agent orchestration* graph, unrelated to network/device security.
- **Command palette (⌘K)**: exists **only in `ui/index.html`** (legacy). `console.html` — the
  actual primary surface — has **zero global keyboard-shortcut handling** (confirmed by grepping
  every keydown/metaKey use; all are local Enter-to-submit handlers).
- **Right-side context/inspector panel**: does not exist.
- **Global status bar**: doesn't exist as a literal bar. Closest equivalent is the persistent
  `<aside class="live">` LIVE ACTIVITY rail (`:1411-1413`), capped at 60 DOM rows.
- **Streaming**: SSE only, no WebSocket anywhere in the repo. Chat turns use manual
  `fetch` + `getReader()`/`TextDecoder` + hand-parsed `data:` lines — **duplicated independently in
  at least 7 files** (`console.html` 3 call sites, `app.html`, `os.html`, `index.html`,
  `agent.html`, `study.html`, `voice.html`). The global `/api/events` bus uses a real
  `EventSource`. A client-side "typewriter" pacer (`makeRevealer()`, `:2723-2784`) fakes
  incremental reveal even when the backend bursts a full response at once.

## 6. Concrete UI debt

- **Five overlapping "chat-first home" surfaces still live and routed**: `console.html` (current
  content default), `workspace.html` (current *launch* default), `index.html` (legacy, hash
  routing), `app.html` ("consumer, chat-first"), `os.html` ("conversation-first"). Each maintains
  its own copy of the SSE parser, a `:root` palette, and in ≥2 cases the markdown renderer.
- **Genuinely dead, re-verified 2026-09-16 (zero references anywhere in the repo, not just from
  webui.py/desktop.py/electron)**: `ui/DOURMOUSE_DESKTOP_MOCKUPS.html` (a static design portfolio,
  never live UI), repo-root `quill-onboarding.html`, and the unused Lucide bundle (§3). Removed.
- **Correction to an earlier pass in this audit**: `ui/hub.html`, `ui/graveyard.html`,
  `ui/product.html`, `ui/agent_chat.html`, and `ui/decision_cards.json` are **not dead** — they
  belong to a separate, real, tested sub-app: `tools/serve_hub.py` serves them as "the dourmouse UI
  shell" on its own port (8791), embedding ATLAS (8790) and a chat-feed relay (8788); `ui_contrast.py`
  (a real, documented contrast-checking utility from an earlier interface-audit pass) reads their
  CSS tokens directly; `dourmouse/tests/test_agent_chat_page.py`,
  `dourmouse/tests/test_ui_contrast.py`, and `dourmouse/tests/test_ui_focus_visible.py` all
  exercise them. They're simply outside the `webui.py`/`desktop.py`/`electron` routing this audit
  started from, not unused. Left untouched — evaluate as part of the separate ATLAS-hub surface if
  it's ever in scope, not as Dourmouse-console dead weight.
- **Oversized components**: `console.html` is 7,092 lines / 571,862 bytes, one file for 14
  unrelated screens (chat, email client, project manager, an orthographic world-map/globe
  renderer, a 2D/3D design tool with its own Three.js scene graph, voice control, orchestration),
  one script, one style block, no splitting. `workspace.html` similarly bundles a floating-window
  manager, a full MediaPipe hand-gesture pipeline, and voice parsing in one file. `webui.py` itself
  is 7,276 lines in one `BaseHTTPRequestHandler` subclass.
- **Raw JSON dumped to the user**: confirmed (tool-call args, §5).
- **Ugly/unstyled code blocks**: confirmed, no highlighting anywhere.
- **Inconsistent spacing/typography/icons**: quantified in §2/§3.
- **Loading states**: minimal. One CSS spinner in the whole file, used only for in-flight tool
  chips. Plain `"Loading…"` text in a handful of spots. No skeleton pattern; several `paint*()`
  functions swap `innerHTML` synchronously with no interim state.
- **Card-wrapping**: not actually a `console.html` problem (zero `class="card"` there) — worth
  spot-checking the legacy files (`app.html`/`os.html`/`index.html`) instead. 77 scattered
  `border:1px solid` declarations, functional but not centralized into a pane utility.
- **Non-keyboard-accessible controls**: concrete example, the tool-call disclosure chip
  (`console.html:2615-2629`) is a plain `<div>` with `.onclick`, no `tabindex`/`role`/keydown
  handling. Inconsistent practice, not universal — other controls (queue-remove button, mail rows)
  are done correctly as real `<button>`s with `aria-label`s.
- **Documentation/reality drift** (meta-finding): at least two stale in-code comments undercount
  reality — "four skins" (8 real) and "nine screens" (14 real). Other in-file comments should be
  re-verified before being trusted during the redesign, not assumed accurate.

## 7. Performance concerns

- **Unbounded in-memory array, confirmed**: `liveEvents` (`console.html:2447`) is pushed to on
  every activity event and only read for `.length` or wiped manually — nothing trims it
  automatically. The DOM list is capped at 60 rows, but the backing array grows for the session's
  full lifetime in an app explicitly designed to run long-lived.
- **No virtualization anywhere.** Only the live-activity rail self-limits (hard 60-item DOM cap);
  chat transcript, mail list, and project list all grow the DOM unbounded per session.
- **Large conversation histories**: no pagination, no lazy-mount. Individual tool-output blobs are
  truncated at 8,000 characters, but total history length is uncapped.
- `workspace/world_history.jsonl` is already 846,622 bytes, append-only — worth checking whether
  any endpoint reads it whole into memory.

## 8. Line counts

| File | Lines | Bytes | Role |
|---|---|---|---|
| `ui/console.html` | 7,092 | 571,862 | Primary content surface ("/"), 14-screen SPA-in-one-file |
| `ui/index.html` | 5,719 | 312,131 | Legacy "HUD," hash-routed, only real command palette |
| `ui/workspace.html` | 2,873 | 140,213 | **Actual default launch surface** |
| `ui/os.html` | 1,523 | 75,109 | Alternate "conversation-first" home (duplicate) |
| `ui/map.html` | 1,203 | 60,242 | Agent orchestration map window |
| `ui/atlas_lab.html` | 997 | 46,458 | ATLAS strategy-lab window |
| `ui/app.html` | 819 | 33,699 | Alternate "consumer" home (duplicate) |
| `ui/hud.html` | 767 | 37,877 | Another tactical HUD variant |
| `ui/voice.html` | 580 | 30,513 | Voice-only interface |
| `ui/setup.html` | 576 | 27,028 | First-run setup |
| `ui/DOURMOUSE_DESKTOP_MOCKUPS.html` | 616 | 46,060 | Dead — static portfolio |
| `ui/login.html` | 472 | 25,148 | Token login |
| `ui/agent.html` | 435 | 23,073 | Per-agent live window |
| `ui/all_hands.html` | 381 | 18,888 | Multi-source parallel run window |
| `ui/study.html` | 298 | 14,115 | Study-scoped chat |
| `ui/product.html` | 275 | 15,957 | Dead |
| `ui/hub.html` | 247 | 12,225 | Dead |
| `ui/mobile.html` | 222 | 10,879 | Phone-pairing page |
| `ui/graveyard.html` | 141 | 23,586 | Dead, self-named |
| `ui/design-system.html` | 140 | 7,492 | Live reference for `dourmouse-ui.css` |
| `ui/agent_chat.html` | 117 | 5,068 | Dead |
| `ui/file_preview.html` | 118 | 4,748 | Sandboxed PDF/image preview target |
| `ui/assets/dourmouse-ui.css` | 425 | 16,793 | Shared design system — adopted almost nowhere |
| `dourmouse/webui.py` | 7,276 | — | Backend server + router + SSE, stdlib-only |
| `electron/main.js` | 856 | — | Electron shell (windowing/IPC only, no own UI assets) |

Total `ui/*.html` + css/js: ~26,281 lines, ~60% concentrated in console.html + index.html + workspace.html.

## Implications for Phase 3

- Redesign must treat **console.html and workspace.html as co-primary** (workspace is the literal
  boot screen; console is where daily use has actually happened). Retire or consciously fold in
  `app.html`/`os.html`/`index.html` rather than maintaining five parallel homes.
- A real shared design system already exists in spirit (`dourmouse-ui.css`) but is unused by the
  surfaces that matter — the redesign should extend/replace it and then actually adopt it
  everywhere, not create a second parallel system.
- Highest-value net-new components, in priority order: syntax-highlighted code + real diff
  rendering (tools already exist server-side with nothing to show them), a real command palette on
  console.html, source/evidence cards for research, a security/network view (net-new, ties to
  Phase 4), and virtualization/capping for the unbounded live-event array before it becomes a real
  memory issue.
- Dead files (`hub.html`, `graveyard.html`, `product.html`, `agent_chat.html`,
  `DOURMOUSE_DESKTOP_MOCKUPS.html`, `decision_cards.json`, root `quill-onboarding.html`, the unused
  Lucide bundle) are safe removal candidates for the Phase 1 audit pass — confirm zero references
  once more immediately before deleting, since this map is a point-in-time snapshot.
