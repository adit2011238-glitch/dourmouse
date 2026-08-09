# DOURMOUSE DESKTOP APPLICATION — DESIGN & ARCHITECTURE PORTFOLIO

Version 1.0 · 2026-08-09 · Companion visual portfolio: `ui/DOURMOUSE_DESKTOP_MOCKUPS.html`
(served live at `http://127.0.0.1:8765/assets/DOURMOUSE_DESKTOP_MOCKUPS.html`)

**Status: DESIGN ONLY. No production code was changed to produce this portfolio.**
Implementation begins only after this portfolio is reviewed and approved (per the brief).

---

## 0. Executive summary

DourMouse today is **already a desktop application** — but a fragile one. It runs
the same stdlib-Python web core (`dourmouse/webui.py`, `ThreadingHTTPServer`) inside
a **PyWebView (WKWebView) native window** (`dourmouse/desktop.py`), with packaging via
an AppleScript `dourmouse.app` that opens **Terminal**, plus a self-contained dist
folder. Everything the brief asks for — ATLAS as a workspace, World Monitor as a
workspace, command center, per-user state, cross-device — **already exists in the
web core and the UI shell**. What is missing is not the application; it is the
**native shell polish**: a real standalone `.app` (no Terminal), menu-bar/tray
presence, native notifications, a deep-link URL scheme, window-state memory,
offline caching, and an update system.

**Recommendation (evidence-based):** do **not** port to Electron or Tauri. Keep and
harden the **PyWebView native shell** (Option A), and layer the missing native
affordances onto the existing in-process server. The full comparison is in §3; the
single decisive fact is that every subsystem — auth, per-user state, ATLAS bridge,
World Monitor bridge, the 26-agent AI dispatch, the SSE fan-out — is Python +
plain HTML/JS that already speaks `http://127.0.0.1:8765`, and the entire test
suite is pytest. Porting the shell would discard working, tested functionality for
zero user-visible gain.

---

## 1. Existing architecture — the audit

### 1.1 Stack inventory (verified against the repo at v5.18)

| Layer | What exists today | Reuse? |
|---|---|---|
| **Backend** | Stdlib `ThreadingHTTPServer` (`webui.py`); `run_server()` factory (testable, ephemeral ports) + `serve_forever()` blocking entry. Binds `127.0.0.1:8765` (`DOURMOUSE_UI_PORT`), `DOURMOUSE_HOST` for LAN. | **Reuse as-is** |
| **Desktop shell** | `desktop.py`: PyWebView native window (WKWebView), main window + Agent Map window + per-agent live windows; `DesktopBridge` JS API; honest browser fallback. | **Reuse + extend** |
| **Frontend** | `ui/index.html` (14 views; routed shell: sidebar ≥1440 / icon rail 900–1439 / bottom tabs <900; ⌘K + ⚡ command palette; SSE state fan-out; per-user DATA SCOPE card). Plus `login.html` (token + Google OAuth), `map.html`, `agent.html`, `mobile.html` (pairing). | **Reuse as-is** |
| **PWA** | `manifest.webmanifest` + icons (v5.13). **No service worker** (no offline). | Gap |
| **State** | SQLite (WAL): per-user `StateStore` (watchlist/alerts/prefs/recent/workspace, v5.17), `AuthStore` (OAuth users+sessions), `MemoryStore` (learn), artifacts, uploads, message bus, `tasks.json`. | **Reuse as-is** |
| **Auth** | Shared `DOURMOUSE_ACCESS_TOKEN` gate (loopback exempt) + per-user Google OAuth (PKCE; gmail/calendar/drive scopes, v5.15–v5.18). | **Reuse as-is** |
| **AI** | Ollama / NVIDIA / OmniRoute backends (`config.py`), `ChatSession` + `DispatchRegistry` (26 agents), human confirmation gate, `live_runtime` polls (news/markets/mail/tasks/rnd). | **Reuse as-is** |
| **ATLAS** | `atlas_cli.py` (panel telemetry), `_atlas_runner.py` (managed single-flight `fx-daily` runs + SYSTEM alerts), `atlas_ops.py`. | **Reuse as-is** |
| **World Monitor** | `worldmonitor.py`: real bridge to worldmonitor.app (keyless health + 59-tool catalog; keyed data tools, allow-list guarded). | **Reuse as-is** |
| **Other bridges** | Freebuff (`freebuff_bridge`, `freebuff_events` SSE watcher), Spotify, voice (whisper/piper), repo index, neural orchestrator. | **Reuse as-is** |
| **Packaging** | `build_app.command` (osacompile → **Terminal applet**), `build_dist.sh` (self-contained dist folder + venv + INSTALL.md), `start.command`/`start.sh`/`stop.command`. | **Replace the applet; keep dist model** |
| **Tests** | pytest, 71 passing in `tests/` (hermetic: ephemeral-port servers, faked transports). | **Reuse + extend** |
| **Git** | Real history through v5.12; v5.13–v5.18 uncommitted. | Checkpoint needed (§16) |

### 1.2 What each "workspace" is today

- **ATLAS** — the `#/atlas` view renders *real* telemetry (repo path, version, branch,
  FX bootstrap depth, last report, managed-run launch button). It is an ATLAS-in-
  DourMouse workspace already; it is not a separate window, tab, or app.
- **World Monitor** — the `#/world` view renders the honest bridge status
  (health/signal counts/catalog). The full "intelligence digest" is scoped to Phase R1.
- **Portfolio / Markets / Intelligence / Alerts / Settings** — routed views backed by
  the per-user StateStore and the bridges.
- **AI** — the HUD chat (`/api/chat` SSE) drives the full 26-agent dispatch with a
  human confirmation gate.

### 1.3 Architecture diagram (current)

```
                ┌─────────────────────────────────────────────┐
                │              DOURMOUSE CORE (Python)        │
                │  webui.py ThreadingHTTPServer :8765 (std)  │
                │  ┌───────────┬───────────┬───────────────┐  │
                │  │ StateStore│ AuthStore │ MemoryStore   │  │  SQLite (WAL)
                │  │ (per-user)│ (OAuth)   │ (learn)       │  │
                │  ├───────────┴───────────┴───────────────┤  │
                │  │ DispatchRegistry · 26 agents · gate    │  │  Ollama/NVIDIA
                │  │ ChatSession · live_runtime polls       │  │  /OmniRoute
                │  ├───────────┬───────────┬───────────────┤  │
                │  │ ATLAS     │ WorldMon  │ Freebuff      │  │  external
                │  │ bridge    │ bridge    │ bridge        │  │  APIs
                │  └───────────┴───────────┴───────────────┘  │
                └───────────────┬─────────────────────────────┘
                                │ SSE (/api/events) + fetch
        ┌───────────┬───────────┴───────────┬───────────┐
        │           │                       │           │
   WKWebView   Browser (fallback)      Phone / Tablet   Browser (LAN/Tailscale)
  desktop.py  (same UI)               (mobile.html)     (DOURMOUSE_ACCESS_TOKEN)
```

**Components reused directly:** everything above the shell line. **Modified:** none
required. **Replaced:** the Terminal-applet `.app` (and, later, the dist-folder
installer on Windows). **Gaps this portfolio closes:** tray/menu-bar, native
notifications, deep links, window-state memory, offline mode, auto-update, real app
bundle, dockable multi-panel layouts, CI.

---

## 2. Target platforms

- **macOS** — primary (the current dev environment; WKWebView via PyWebView is native).
- **Windows** — fully supported by PyWebView (Edge WebView2) and by the dist model;
  the launcher scripts need a `.bat`/PowerShell twin. No OS-specific code in the core.
- **Linux** — supported via PyWebView (GTK/WebKitGTK) and `start.sh`; practical on
  desktop distros. Packaging per-distro is deferred (§15) — "support if practical".

The core deliberately contains **zero OS-specific code**; all native behavior lives
behind the shell layer (desktop.py + packaging scripts), exactly as it does today.

---

## 3. Desktop technology — evidence-based choice

### 3.1 The candidates

| Criterion | **PyWebView (Option A — recommended)** | Electron | Tauri | PWA-only |
|---|---|---|---|---|
| Reuse of existing stack | 100% — server, UI, tests unchanged | Server reused via child process; UI same | Server must become a Rust **sidecar** (tauri-plugin-shell spawning `.venv` python) | 100% UI; browser chrome |
| Runtime weight | ~60–120 MB (existing venv + WebKit) | ~300–500 MB baseline RAM, ~150 MB app | ~10–30 MB + Python sidecar process | browser |
| Startup | < 2 s (server boots on a thread, window opens) | 2–4 s | 1–2 s + sidecar spawn | browser tab |
| Native tray/menu-bar | **pywebview has native menu + tray support** (macOS/Windows) | Excellent (mature) | Plugin (`tauri-plugin-single-instance`, tray in core v2) | No |
| Native notifications | pywebview macOS notifications; Python `rumps`/`pyobjc` for tray-rich | Excellent | Plugin | Web notifications (browser-bound) |
| Deep links | Register `dourmouse://` in app bundle Info.plist / Windows registry | Excellent | Plugin | No |
| Auto-update | DIY (version JSON + signed zip + dist model) | electron-updater (mature) | tauri-updater (signed) | N/A |
| Security surface | Existing: loopback-only server, token gate, no Node integration, no remote code | Node integration risks (mitigable) | Capability system (good) | Browser sandbox |
| Dev complexity for THIS team | **Zero new toolchains** (Python + pytest only) | New Node toolchain, keep Python server | New Rust toolchain + sidecar lifecycle | None |
| Multi-panel drag-dock | Custom JS (dockable panels in the UI) | Same custom JS | Same custom JS | Same |
| Signed installers | py2app/PyInstaller + codesign (doable) | electron-builder (best-in-class) | tauri-bundler (best-in-class) | N/A |

### 3.2 The recommendation

> **Option A — keep the PyWebView native shell and harden it.**

Reasoning, in order of weight:

1. **The application already exists as a native-windowed app.** `desktop.py` boots
   the real core in-process and opens a WKWebView window. The user-visible gaps are
   *affordances*, not architecture: the `.app` is a Terminal-opening applet, and
   tray/notifications/deep-links/window-state don't exist yet. Those are all
   achievable with pywebview + ~1,500 lines of Python + one rewritten `build_app`.
2. **Porting to Electron/Tauri rewrites the shell for zero user-visible gain.** The
   UI is plain HTML/JS talking HTTP+SSE; the backend is stdlib Python. Tauri would
   wrap the *same* localhost server in a Rust sidecar (more moving parts, a new
   toolchain, and a process-lifecycle to babysit). Electron would add a Node
   toolchain and a 300 MB baseline for identical rendering.
3. **The test story stays intact.** 71 hermetic pytest tests keep passing; the
   native layer gets its own small test seam (desktop.py already takes a
   `webview_loader`).
4. **Electron's real advantages — mature auto-update and packaging — are not worth
   the port.** Our update model (§12) is a signed dist artifact + version JSON +
   downloader; PyInstaller/py2app handle signed `.app`/`.dmg`. If, after Phase 6,
   auto-update or installer quality becomes the top blocker, **Electron remains the
   documented fallback (Option B)** — the UI and server are 100% portable to it, so
   the migration cost is bounded to the shell.

**Trade-offs acknowledged:** Electron and Tauri have richer ecosystems for
tray/notifications/updates; PyWebView's are thinner and macOS-centric. We mitigate by
keeping every native feature behind the thin shell layer (§5) and by using macOS
first-party bridges (pyobjc for the menu-bar extra and notifications) with
PyWebView APIs where they suffice.

---

## 4. The desktop application shell

```
┌────────────────────────────────────────────────────────────────┐
│ DOURMOUSE  ◈  ● ONLINE · signed in as a@x.com      —  □  ✕   │
├──────────────┬─────────────────────────────────────────────────┤
│ ◈ HOME       │                                                 │
│ ◈ ATLAS      │                                                 │
│ ◎ WORLD      │            ACTIVE WORKSPACE                     │
│ ▣ PORTFOLIO  │      (ATLAS · WORLD · PORTFOLIO · MARKETS ·     │
│ ▥ MARKETS    │       INTEL · ALERTS · SETTINGS · AI CHAT)      │
│ ✦ INTEL      │                                                 │
│ ◉ ALERTS     │                                                 │
│ ⚙ SETTINGS   │                                                 │
│              │                                                 │
│ ───────────  │                                                 │
│ ⚡ ⌘K        │                                                 │
└──────────────┴─────────────────────────────────────────────────┘
```

- **One window, one workspace.** The existing routed shell becomes the app frame:
  a single native window whose webview is the shell, exactly like today's
  `desktop.py` main window. ATLAS, World Monitor, Portfolio, Markets, Intelligence,
  Alerts, Settings, and the AI chat are **routes**, never new windows/tabs/apps.
- **Window chrome:** standard traffic lights (macOS), `-`/`□`/`✕`, native title bar
  (or hidden-titlebar with draggable regions once the webview paints it — decided in
  Phase 2 by feel, not by decree).
- **Status strip:** top-right shows online/offline, signed-in account (v5.17 `me`),
  alert count. Honest: STALE markers when offline (§11).
- **Secondary windows stay optional:** Agent Map and per-agent LIVE windows remain
  the deliberate exception — they are *windows the user opens on purpose*, matching
  the existing v2.7/v2.8 behavior.

---

## 5. Native behavior — only what earns its place

| Feature | UX justification | Implementation |
|---|---|---|
| Window resize/max/min/fullscreen | Free, native | WebView handles it; CSS breakpoints already adapt |
| **Window state memory** | §11 core ask: close → reopen in the same place | `win.events.closed` → persist bounds+maximized to StateStore prefs; restore on launch (capped: only if the saved display still exists) |
| **⌘K / ⌘1–⌘4 / ⌘, / Esc** | §22 shortcuts | `window.events.closing`… no — a `keyPress` bridge: `DesktopBridge.keyboard(name)`; JS already owns ⌘K; native shortcuts map to hash routes via a tiny bridge call |
| Context menus | Right-click in views | WebView `context_menu` events; text fields keep native edit menu; app menus (File/Edit/View/Window/Help) via pywebview menu API |
| **Native notifications** | §13 | `webview` macOS notifications + pyobjc `UserNotifications`; deep-link payload `dourmouse://world?event=…` |
| **System tray / menu-bar** | §12 | `rumps` (menu-bar, macOS) or pywebview tray; menu: Open, Workspaces, Quick ⌘K, Quit |
| Deep links (`dourmouse://`) | §14 | App bundle `CFBundleURLTypes` (macOS) / registry (Windows); handler routes to `#/…` via a `/api/deeplink?to=` endpoint with strict allow-list (see §10) |
| Drag-and-drop | Files onto chat (uploads already exist), symbols onto watchlist | WebView drag events → existing `/api/upload` |
| Clipboard | Copy/paste already works; "copy result" buttons in chat | Web APIs, no native code |
| App lifecycle | Close = window close (server shuts down); tray mode = close-to-tray option (off by default — no unnecessary background process, per §12) | `desktop.py` already manages shutdown cleanly |

Deliberately **not** implemented: file-system access beyond uploads (no shell tools),
global hotkeys (system conflicts), background auto-start (user opt-in later).

---

## 6. The command center (⌘K / Ctrl+K)

Already shipped in the web shell (`/api/palette` powers ⌘K on desktop and ⚡ on
mobile — 8 destinations + 26 agents + fx commands, filterable). Desktop additions:

- **Native entry points:** ⌘K in the webview, tray → "Quick Command", and a
  `dourmouse://command?q=…` deep link.
- **Structured first, natural later:** the palette index is already JSON; a future
  NL layer turns a typed phrase into the same index query (`open atlas` →
  destination match). The architecture (server-side palette + client render) is
  where an AI resolver plugs in without UI changes.

---

## 7. Workspaces: ATLAS, World Monitor, Portfolio — inside the shell

- **ATLAS workspace** (`#/atlas`, `#/atlas/research`, `#/atlas/risk`): real
  telemetry today; gains a **research workspace** (§23) and managed-run history with
  per-user attribution (alerts tied to the user who launched the run). No new
  windows. The `_atlas_runner` single-flight stays.
- **World Monitor workspace** (`#/world`, `#/world/events`): bridge status today;
  Phase R1 adds the digest, events feed, and map — all rendered inside the view.
  "Avoid opening external browser windows" — a deliberate "open in browser" is the
  only escape hatch, for links the user explicitly clicks.
- **Portfolio** (`#/portfolio`): watchlist stars (per-user) already cross-device;
  gains panel layouts (§8) and cached snapshots (§11).
- **AI** — the chat pane is a first-class workspace (`#/chat`): full dispatch,
  confirmation gate, artifacts renderer, per-account identity.

The rule: **DourMouse → Workspace**, never DourMouse → other-window, for any of these.

---

## 8. Multi-panel desktop experience

A lightweight, honest panel system (not a trading-terminal monster):

- **Preset layouts** per workspace: e.g. ATLAS = `[Portfolio | Chart | Risk]`,
  Research = `[ATLAS signals | Chart | World feed | Notes]`.
- **Resizable splitters** (CSS + pointer events), **collapsible panels**, **layout
  saved** to per-user StateStore prefs (`layout.atlas = [...]`).
- **Dockable**: panels drag between a left/center/right gutter within one workspace;
  floating windows are *not* built (they add tray-like complexity for little value —
  documented decision).
- Layouts are **named** (`DEFAULT`, `RESEARCH`, `TRADING`) and restorable; a layout
  reset is one click. Default layouts ship; customization is opt-in.

---

## 9. Workspace memory (state that follows you)

Persisted in the per-user StateStore (already cross-device, v5.17):

- Last workspace + route (desktop and per-device).
- Window size/position (native, §5).
- Panel configuration per workspace (§8).
- Watchlists, alert filters/mutes, prefs, recent activity — already stored.
- **Never restored automatically:** anything that could confuse — e.g. a layout
  from a display that no longer exists, or a "resume" banner on a fresh device
  without context. The existing resume banner (dismissible, 24 h) is the pattern.

---

## 10. Security architecture

| Surface | Control |
|---|---|
| **Core** | Loopback-only by default; `DOURMOUSE_HOST` + token gate for LAN; per-user Google sessions; no Node integration; no remote code. |
| **IPC** | The webview talks only to the loopback HTTP server (no `file://` privileges). The `DesktopBridge` exposes a **tiny, typed API** (`keyboard`, `deeplink`, `set_window_state`) — no shell, no file paths, no arbitrary commands. |
| **Deep links** | `dourmouse://` → a strict parser: only `atlas`, `world`, `portfolio`, `markets`, `alerts`, `settings`, `command` (+ one optional `/id` segment, `[A-Za-z0-9_-]+`). Anything else is dropped. No query-string eval, no exec. |
| **Notifications** | Payload deep-links go through the same allow-list parser. |
| **File access** | Uploads stay sandboxed under `workspace/uploads` (existing name whitelist + size cap); the app never grants blanket FS access. |
| **Secrets** | Stay in `.env` (600) / AuthStore; never in the frontend. The dist never ships `.env` or `local_secrets.py` (already enforced). |
| **Updates** | Signed artifacts; HTTPS-only download; version pinning; hash verification (§12). |
| **Webview** | `localhost` origin only; no remote content is ever loaded into the shell window. |

---

## 11. Performance + offline/degraded mode

- **Startup:** server on a thread + window open; lazy module imports (the codebase
  already imports bridges on demand). Target: window visible < 1.5 s.
- **Navigation:** hash routes + cached panel data; virtualized watchlist/alerts lists
  (the lists are small today — virtualization lands when they grow).
- **Memory/CPU:** only the loaded workspace renders; the live polls are the only
  background CPU (already env-gated); tray mode stays off by default.
- **Offline:** a **service worker** (the missing PWA piece) caches the app shell +
  last-known snapshots; every cached datum renders with an explicit **STALE**
  marker; market/quote data is never presented as live when stale. Local surfaces
  that work offline: shell, settings, watchlist (read), alerts (read), cached
  research.

---

## 12. Auto-updates (secure, honest)

- **Model:** version file `https://…/dourmouse/latest.json` (`{version, url, sha256,
  notes}`) + signed `.zip`/`.dmg` artifact; the app checks on launch + a daily
  timer, shows **current / latest / UPDATE AVAILABLE** in Settings, downloads with
  hash verification, applies on restart, and keeps the previous dist folder as a
  **rollback**.
- **Channels:** `stable` and `beta` (env-chosen).
- **Signed releases:** codesign (macOS) / Authenticode (Windows) from Phase 9;
  unsigned builds clearly show "UNSIGNED BUILD" (Rule 2.2 — never pretend).
- **No insecure paths:** no auto-apply without verification, no remote script
  execution, no update-over-HTTP.

---

## 13. Design system (desktop)

Kept from the existing HUD language (already the product's identity): dark,
bracket-chrome, mono labels, cyan/amber/red semantic colors, gold accents,
`prefers-reduced-motion` honored. Desktop adaptations: tighter spacing grid
(4/8/12), denser data tables, quieter card shadows, no excessive glass/gradients,
no neon. One design system across desktop/tablet/mobile — layouts adapt, language
does not (§19–20 of the original cross-device brief, already implemented).

---

## 14. Technical implementation plan (phases)

| Phase | Scope | Exit criteria |
|---|---|---|
| **0. Checkpoint** | Commit v5.13–v5.18 as a baseline branch (`desktop-portfolio`); tag `v5.18`. | Clean baseline; nothing lost |
| **1. Native shell v2** | Rewrite `build_app` to a real standalone `.app` (py2app/PyInstaller, Info.plist with URL scheme, signed when possible); window-state memory; menu bar | Launch without Terminal; state restored |
| **2. Tray + notifications** | menu-bar extra (rumps/pyobjc), native notifications wired to StateStore alerts, tray menu actions | Tray opens app; alerts notify; click deep-links |
| **3. Deep links + shortcuts** | `dourmouse://` allow-list parser + `/api/deeplink`; native ⌘1–⌘4/⌘,/Esc bridge | Links navigate; shortcuts work in-app |
| **4. Workspaces + panels** | Research workspace, panel layouts + splitters, layout persistence | Preset layouts usable; persisted per-user |
| **5. Offline** | Service worker + STALE markers; cached snapshots | App usable offline, honestly marked |
| **6. Updates** | latest.json + downloader + rollback; Settings UI | Version check end-to-end; rollback works |
| **7. Performance** | Startup timing, lazy loads, list virtualization where needed | Startup < 1.5 s; no jank on nav |
| **8. Security audit** | Deep-link fuzz, IPC review, webview posture, signed-build check | Audit report; fixes landed |
| **9. Cross-platform packaging** | Windows (WebView2 + installer/portable), Linux (AppImage/deb), codesigning | Builds on all three; docs updated |
| **10. Testing** | Desktop test seam (fake webview), deep-link parser unit tests, panel-layout tests, update-simulator tests; cross-device suite stays green | All suites green; matrix run |
| **11. Release infra** | CI (GitHub Actions): lint, pytest, build, sign, publish latest.json; release channels | One-command release |

---

## 15. Build & distribution

- **App id:** `app.dourmouse.desktop` · **Versioning:** `MAJOR.MINOR.PATCH` from git
  tags (v5.18 → 0.5.18 desktop-series mapping documented).
- **Icons:** reuse `ui/icon-*.png` / `icon.svg`; generate `.icns` (macOS) and `.ico`
  (Windows) in Phase 9.
- **macOS:** standalone `.app` (no Terminal) + `.dmg`; codesign + notarize when a
  Developer ID exists; unsigned dev builds are labeled.
- **Windows:** installer (Inno/NSIS) + optional portable zip; WebView2 runtime note.
- **Linux:** AppImage (practical); deb/rpm deferred to demand.
- **Channels:** stable/beta via `latest.json`.

---

## 16. Do-not-destroy contract

1. **Checkpoint first (Phase 0):** commit the current working tree (v5.13–v5.18:
   cross-device shell, Google OAuth, per-user state, Drive, tests) to a baseline
   branch *before* any Phase 1 code. Nothing is discarded; nothing is force-overwritten.
2. Every phase is **additive and reversible**: new native modules behind the shell
   layer; the web/browser mode and every existing API remain functional and tested
   (the browser mode is a supported platform, not a fallback).
3. Existing working functionality is never deleted without a replacement and a
   green test. ATLAS, World Monitor, auth, state, and the 71 tests stay green
   throughout.
4. The dist-folder model remains the cross-platform fallback; the new installer is
   layered on top, not a replacement fork.

---

## 17. Testing strategy

- **Functional:** every workspace (existing 71 hermetic tests + per-phase additions:
  deep-link parser, panel-layout persistence, window-state store, update-simulator).
- **Desktop:** a fake `webview_loader` test seam (already present) + a scripted
  smoke test on macOS; Windows/Linux smoke when built (Phase 9).
- **Responsive:** existing viewport tests (three chrome modes) + tablet/mobile.
- **Performance:** startup timer in CI; memory/CPU spot checks on the two OSes.
- **Security:** deep-link fuzz (allow-list rejection), IPC surface review, webview
  posture check, secret-presence check in dist builds (already `rm`'d).
- **Cross-device:** desktop→mobile→desktop flow through the one server (the existing
  SSE + per-user state tests are the skeleton; a scripted two-client test fills it).

---

## 18. Risks & trade-offs

| Risk | Mitigation |
|---|---|
| PyWebView ecosystem thinner than Electron's | Every native feature behind the shell layer; Electron is the documented, bounded fallback |
| macOS-centric notifications/tray | pyobjc-first with Windows WebView2 equivalents in Phase 9 |
| Auto-update is DIY | Signed artifacts + hash verification + rollback from day one; electron-updater as the fallback if quality demands |
| Panel layouts could add clutter | Preset layouts only; customization opt-in; one-click reset |
| Offline data staleness | STALE markers mandatory; never present cached as live |
| Scope creep into a trading terminal | Explicit non-goals (§5, §8); the "calm professional tool" language from §21 is a review gate |

---

## 19. Final success criteria (from the brief)

1. Launch from the computer (standalone `.app`, no Terminal). ✅ target Phase 1
2. Command center visible and native (⌘K). ✅ already in shell; tray entry Phase 2
3–4. ATLAS and World Monitor navigated to **inside the window**, instantly. ✅ already routes; polish Phase 4
5–6. Portfolio + system search. ✅ per-user portfolio; palette search Phase 3
7. Native notifications deep-linking into workspaces. Phase 2
8. Keyboard shortcuts. Phase 3
9. Efficient background presence (tray off by default). Phase 2
10. Close/reopen without losing state. Phase 1 (window state) + Phase 9 (workspace)
11–12. Same account/data on mobile and web. ✅ already (one server, per-user state)
13. Move between platforms without feeling like different products. ✅ by construction

**One DourMouse** — the app is one window over one server; the device changes, the
data and identity follow.
