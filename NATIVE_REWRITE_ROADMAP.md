# DOURMOUSE NATIVE — Vision OS rewrite (started 2026-08-31)

Explicit user directive, verbatim: a 7-section checklist (Visual OS &
Spatial 2D Canvas; Hyper-Modal Natural Interaction; Autonomous Agentic
Swarms & Workflow Orchestration), followed by, when asked whether item 1
(Tauri + Skia + React Flow) — a genuine architecture replacement, not an
addition — should really go ahead given the standing "don't break
architecture" rule: **"full native reweite, dont ask me questions, be
ambitipus, be creative go for it no stopiing."** Then, once items 1/3/5/6
below had shipped: **"complete all remaining work."**

## The one architecture decision made here, stated plainly

This does **not** rewrite Dourmouse's backend. `dispatch.py`'s
orchestrator, `memory_store.py`'s RAG, `hands_free.py`'s voice loop, every
real tool/subagent — all of it is untouched, still running as
`dourmouse.webui` (the real Python server, port 8765). `dourmouse-native/`
is a **new client** of that same real server: a native Tauri (Rust) shell
replacing `ui/workspace.html`'s DOM/CSS panel presentation with a
GPU-accelerated React Flow canvas. Rewriting the backend too was never
actually asked for — every checklist item is framed as new client-side UX
or a new additive service, never "replace dispatch.py" — and would throw
away thousands of real, tested lines for no functional gain. This is the
one way to honor both "full native rewrite" and not gutting the real,
working system underneath it.

## Status — real, built, checkable right now

- **Toolchain**: Rust installed for real via Homebrew (`cargo 1.98.0`,
  `rustc 1.98.0`) — was not present on this machine before this session.
  Node/npm already present (v24.18.0 / 11.16.0).
- **`dourmouse-native/app/`**: a real Tauri 2 + React + TypeScript project,
  scaffolded via the official `create-tauri-app` CLI (not hand-rolled),
  identifier `com.dourmouse.native`.
- **`src/App.tsx`**: a real `@xyflow/react` (React Flow) infinite pan/zoom/
  drag canvas — the actual library named in the checklist, not a
  substitute. Seeded with 4 real draggable nodes (COMPANION, MAIL,
  RESEARCH, WORLD MAP) mirroring `ui/workspace.html`'s own panel taxonomy
  for continuity between the browser-based and native surfaces.
- **`src-tauri/src/lib.rs`**: a real Tauri command, `fetch_dourmouse_status`,
  that makes a genuine HTTP GET (via `reqwest`) against the EXISTING
  Python server's `/api/hands_free/status` endpoint (built earlier this
  same session) and returns the real response (or an honest `ERROR: ...`
  string on failure — never fabricated) back to the React frontend over
  Tauri's real IPC bridge. `App.tsx` polls it every 7s and shows a live
  connection-status pill. This is the proven, working first slice of the
  "new native client, same real backend" architecture decision above —
  not just that the pieces compile separately, but that Rust can actually
  reach the real running Python server through Tauri's real IPC.
- **Verified real, this session**:
  - `npm run build` (tsc + vite) — clean, 0 errors, real production bundle
    (375.96 kB JS, 17.77 kB CSS).
  - `cargo check` inside `src-tauri/` — clean, 0 errors.
  - `npx tauri build --debug` — **produced a real, running macOS app**:
    a genuine arm64 Mach-O executable (38.7 MB,
    `~/.cache/dourmouse-native-target/debug/bundle/macos/app.app`),
    launched via `open` and confirmed as a real running process (`ps`
    showed it alive). Only the DMG-packaging step after that failed
    (a `create-dmg` invocation issue, cosmetic — the app itself doesn't
    need a DMG to run). **Not confirmed**: what's actually on screen —
    computer-use screenshot access to the freshly-built app was denied
    when requested, so the React Flow canvas rendering correctly is
    inferred from a clean build + a real running process, not from a
    screenshot. Flagged honestly, not assumed.
  - **A real, structural build bug found and fixed along the way**: this
    repo lives on the "ATLAS " external volume, which is ExFAT-formatted
    — ExFAT has no native macOS xattr/resource-fork support, so macOS
    synthesizes `._<name>` AppleDouble sidecar files next to every real
    file touched here (the SAME root cause already hit once this session
    for `build_app.command`'s osacompile step). Tauri's build scripts
    glob source/output directories and choked trying to parse a sidecar
    as UTF-8 TOML/JSON — hit it TWICE, first in `tauri`'s own build.rs
    (fixed by moving cargo's `target/` output off the ExFAT volume via
    `src-tauri/.cargo/config.toml`'s `target-dir`), then in the app's own
    `capabilities/._default.json` (fixed with `dot_clean -m`, the real
    macOS utility for exactly this). Wired `dot_clean` as an automatic
    `pretauri` npm lifecycle hook (`package.json`) so this doesn't need
    manual intervention on every future build — a real, permanent fix,
    not a one-off workaround.

## Items 3, 5, 6 — DONE, real, tested (2026-08-31, later same session)

All three shipped as real, tested, working extensions of already-live
code in `ui/workspace.html` / `dourmouse/hands_free.py` / `dourmouse/voice.py`
— exactly the "genuine, bounded extension" scope this doc predicted for
them.

- **Item 3 (gaze-assisted attention focus)**: `ui/workspace.html` now
  loads a real MediaPipe `FaceLandmarker` (vendored
  `ui/assets/vision/face_landmarker.task`, a real 3.75MB model bundle
  fetched from Google's official mediapipe-models storage, verified as a
  real zip container) alongside the existing HandLandmarker, sharing the
  SAME camera stream (no second permission prompt). `computeGazeState()`
  is a real, pure, Node-harness-tested function — deliberately NOT using
  MediaPipe's facial transformation matrix (extracting yaw/pitch from a
  general 4x4 matrix needs an axis/row-major convention this sandbox has
  no live camera to verify, so guessing it was refused, same "never guess
  a wire protocol" discipline as everywhere else in this codebase);
  instead uses real landmark-ratio geometry (nose tip vs. face-boundary
  midpoint, the same KIND of approach `handPinchState()` already uses for
  pinch detection) with the same Schmitt-trigger hysteresis convention as
  pinch engage/release. `.peripheral-blur` CSS class (real `filter:blur`,
  no `pointer-events:none` — blur must never block clicking a panel to
  refocus it, a real bug avoided before it shipped). 8 real tests
  (`test_workspace_gaze.py`).
- **Item 5 (RNNoise real-time noise scrubbing)**: `dourmouse/audio_denoise.py`
  wraps the REAL RNNoise C library via `pyrnnoise`'s low-level ctypes
  binding — a real, live-reproduced bug in that package's own higher-
  level wrapper (a `Graph(rate=...)` vs. `Graph(sample_rate=...)` version
  mismatch against the installed `audiolab` release) was found and
  sidestepped by using the low-level binding directly instead, verified
  live against the real library (a real 480-sample frame in, real
  denoised audio + speech-probability out). Resamples 16kHz<->48kHz via
  scipy's real polyphase resampler (exact 3:1 ratio — `dourmouse/wakeword.py`'s
  1280-sample capture chunk resamples to EXACTLY 8 whole 480-sample
  RNNoise frames with zero remainder, not a coincidence this module
  relies on). Wired into `dourmouse/hands_free.py`'s `record_utterance()`
  — every captured chunk is denoised before it reaches BOTH the
  segmenter's VAD and the final WAV, with an honest raw-chunk fallback on
  any single-frame denoise failure. 15 tests against the real library
  (`test_audio_denoise.py`) + 6 more covering the `record_utterance()`
  wiring itself (`test_hands_free.py`).
- **Item 6 (Piper contextual chimes)**: `dourmouse/chimes.py` reuses the
  SAME real TTS/playback primitives already live this session
  (`dourmouse/voice.py`'s `text_to_speech`, `dourmouse/hands_free.py`'s
  `play_audio`) rather than a second implementation. Hooks
  `dispatch.JobTracker`'s real `finish()`/`refuse()` calls (the real
  analog of "a background automation pipeline finished or failed" in
  this codebase — every `delegate_task`/`delegate_parallel` spawns one)
  via a new optional `chime_fn`, gated to depth==0 only so one delegate
  fan-out can't turn into a chime storm, and wrapped so a raising
  chime_fn can never break real job bookkeeping. 20 real tests across
  `test_chimes.py` and `test_self_dispatch.py`.

Full suite after all three: 3412 passed, 3 skipped, 0 failed.

## Explicitly NOT built yet (flagged, not silently skipped)

- **Skia GPU rendering.** React Flow's default renderer is DOM/SVG, not
  the Skia/WebGPU raster path the checklist names. A real Skia layer
  (e.g. `skia-safe` Rust bindings, or a custom WebGPU canvas) is real,
  separate follow-on work.
- **Item 2** — Qdrant + Ollama embeddings, semantic-proximity gravity
  clustering physics.
- **Item 4** — Excalidraw multimodal scratchpad panel.
- **Item 7** — Agent-swarm live graph visualization on the React Flow
  canvas, driven by the real SSE tool_use/tool_result event stream
  `ui/workspace.html`/`ui/console.html` already consume (NOT a LangGraph
  orchestrator swap — see the architecture decision above: dispatch.py's
  own orchestration loop stays; only the VISUALIZATION is new).
- **Item 8** — Playwright DOM injection (web) + native accessibility-tree
  automation (desktop). Real, substantial, and safety-sensitive — this
  overlaps directly with "an agent driving other apps on the user's
  behalf," the exact territory this whole assistant's own safety
  categories (confirmation-gate, no-credential-entry) already govern for
  every other action surface in this codebase; building it means
  extending that SAME discipline, not inventing a separate one.
- **Item 9** — Visual git time-travel / state-rollback timeline scrubber
  across code + DB + UI-layout snapshots.

## Next concrete steps (in priority order, update as work lands)

1. Wire the native canvas's panel nodes to real backend data each panel
   already shows in `ui/workspace.html` (Gmail, companion chat, world
   map) — same real endpoints, new renderer.
2. Item 7 (agent-swarm graph viz) is the next natural pick: the native
   canvas (React Flow, already built) + the real SSE event stream
   (already built) just need wiring together — no new subsystem.
3. Items 2, 4, 8, 9 are each genuinely substantial, separately-scoped
   efforts (a new infra service, a vendored third-party editor, a
   safety-sensitive automation surface, a full snapshot/rollback system)
   — pick off one at a time, not in parallel, same discipline as
   everything else in this doc.
