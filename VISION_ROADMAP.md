# DOURMOUSE VISION — Roadmap to "Incredible" (v13.2)

## Status

- **Phase 1 (gesture -> globe): SHIPPED and live-verified.** GLOBE MODE
  toggle in the VISION screen; PINCH/PEACE/THREE/THUMBS UP/THUMBS
  DOWN/ROCK/OK/FIST repoint to real `gevActions` calls
  (`adjust_camera_zoom`, `select_nearest_aircraft`, `stop_tracking`,
  `zoom_to_globe`, `frame_overhead`) through the real
  `POST /api/dourmouse/action` bridge — no new server code, the existing
  voice-command action runner and dev-server proxy do all of it. OPEN
  PALM/BOTH PALMS/WAVE are untouched. Continuous zoom is throttled to one
  real call per ~400ms. `select_nearest_aircraft`/`track_entity` need real
  args the API actually requires (a place/lat-lon, a query string) — no
  gesture has a typed location, so `sendGevSelectNearestAircraft()` reads
  the camera's own current position via `get_current_view_state` first and
  uses that as "nearest to what I'm looking at." Live-verified against the
  real running globe: `get_current_view_state`, `zoom_to_globe`, and
  `stop_tracking` all round-tripped with real data;
  `select_nearest_aircraft`'s full compound call (enable layer -> fly ->
  refresh -> query -> track) can exceed the vendored bridge's fixed 15s
  wait under real feed latency — a pre-existing constraint of
  `dourmouseActionBridgeProxy` (the same thing would happen via voice),
  not a bug in this integration. 13 tests, `dourmouse/tests/test_index_globe_mode.py`.
- **Phase 4, corrected scope: partially shipped.** The 21-point hand
  skeleton this doc originally proposed adding already existed
  (`drawLandmarks()`, pre-existing, verified before writing that part of
  this plan) — genuinely missing was a live hold-progress meter, now
  shipped: the hysteresis engine's own `GS.pending`/`GS.count`/`GS.NEED`
  counters are now painted as a real fill bar + label
  ("HOLD PINCH — 2/4") the instant a gesture is recognized, not just after
  it fires. The interactive first-run trainer is not built.
- **Phases 2, 3, 5, 6: not started.** Real, substantial pieces of new
  design/engineering each — see their own sections below, unchanged from
  the original plan.
- Also fixed in passing: `dourmouse/tests/test_ui_local.py`'s
  no-external-resources check only ever stripped text after the LAST
  `<script>` tag (`html.split("<script>")[-1]`) before scanning for
  `https?://` references — correct for a page with one script block, a
  real gap for `index.html`'s three. Phase 1's own `fetch()` call (in an
  earlier block) was getting scanned as if it were static markup. Fixed
  to strip every `<script>...</script>` block.

## Where this actually starts from

Before proposing anything new: Vision is not a stub. Real, shipped, tested
pieces already exist:

- **`ui/index.html`** (5,910 lines) — self-hosted MediaPipe `HandLandmarker`
  (GPU delegate, 2-hand tracking) and face presence detection, feeding a
  real hysteresis-gated gesture engine (`VISION` object, v5.29): a gesture
  must hold 4 consecutive frames before firing and has a 900ms per-gesture
  cooldown. 14 gestures are mapped to real actions today — pause/halt,
  emergency stop, send-directive, pointer-mode + click, scroll up/down,
  approve/deny a pending confirmation, cycle workspaces, open the command
  palette, dismiss the task deck, wake-on-wave, presence-based idle.
- **`ui/workspace.html`** (1,071 lines) — a floating multi-window desktop:
  real draggable/resizable panels (mail, chat/research, world map),
  **one-hand pinch drags a panel, two-hand pinch rotates it** — the same
  MediaPipe engine driving both mouse and gesture through one code path
  (`panelSeq`/`panels` manager). Voice commands and a companion-agent panel
  are wired in too.
- **`dourmouse/tray.py` / `overlay.py` / `wakeword.py` / `vision_bridge.py`
  / `proactive.py`** — native desktop pieces (system tray + kill switch,
  always-on-top status window, local ONNX wake-word listener, a real
  `ThreadingHTTPServer` bridge that reaches an in-progress browser gesture
  session to force-stop it, proactive alert popups). `console.html`'s
  VISION screen is the honest status dashboard for all of them (real
  `GET /api/vision/status`, never a decorative guess).
- **`gods-eye-view/`** — a vendored, actively-integrated third-party
  project: a photorealistic Cesium 3D globe with live aircraft/ships/
  satellites/earthquakes/CCTV and its own **voice-command action runner**
  (`gevActions.js`, `createGevActionRunner`) already exposing a rich,
  real, tested action vocabulary — `zoom_to_globe`, `adjust_camera_zoom`,
  `select_nearest_aircraft`, `fly_to_location`, `track_entity`,
  `stop_tracking`, `frame_overhead`, `control_cockpit`, `control_cctv`,
  `control_radio`, `annotate_map`, `move_camera`, `fly_route`, and more.
  `dourmouseBridge.js` already proves the pattern of driving this runner
  from OUTSIDE the globe's own voice loop, over a simple poll/result queue
  (`/api/dourmouse/pending` + `/api/dourmouse/result`).

The honest gap: three genuinely impressive systems — hand tracking,
2.5D panel control, and a photorealistic live-Earth console — exist
**side by side**, not fused. Gestures move windows; they don't touch the
globe. Voice drives the globe; gestures don't. That fusion, not a new
subsystem from scratch, is where "incredible" actually is: it's mostly
wiring, on top of three things already proven to work.

---

## Phase 1 — Gesture takes the globe (highest leverage, lowest risk)

**The core idea:** every gesture already resolves to a named action in
`ui/index.html`'s `gestureAct()`. The globe already accepts named actions
through exactly one door: `window.__godsEyeView.voiceCommands.runner(name,
args)`. Phase 1 is a dispatch table between the two — no new globe-side
plumbing, no new tracking model, reusing two already-tested surfaces.

- Two-hand pinch-and-spread → `adjust_camera_zoom` (the same panel-rotate
  gesture in `workspace.html` already proves two-hand geometry is
  reliable; zoom is a more natural mapping for it on a globe than rotate).
- Open-palm pan/swipe → `move_camera`.
- POINT (already POINTER MODE in the existing engine) raycast against the
  globe's own picked-entity API → `select_nearest_aircraft` /
  `track_entity` on whatever the fingertip is over, instead of a generic
  screen cursor.
- THUMBS UP / THUMBS DOWN on a tracked entity → same approve/deny gesture
  already wired to confirmations, repointed at `track_entity` /
  `stop_tracking` when the globe has focus.
- FIST (already "dismiss the task deck") → `frame_overhead` as a "put the
  view back" gesture when the globe has focus — same gesture, context-
  dependent action, exactly like PINCH already means "send" in the
  directive box and "click" in pointer mode.
- OPEN PALM / BOTH PALMS (already pause / emergency-stop) stay identical —
  the kill-switch and halt gestures must mean the same thing everywhere,
  never be reinterpreted per screen.

**Build shape:** a small new bridge (`gods-eye-view/src/gestureBridge.js`,
mirroring `dourmouseBridge.js`'s own poll/result pattern) or, more
directly, teaching `ui/index.html`'s existing `onHands()` to call the
globe's runner straight through `window.__godsEyeView` when the globe is
the focused surface. Either way: no new tracking, no new gesture
vocabulary yet — just a second consumer for the one already built.

**Why this order:** it's the smallest real step that makes the system
*feel* different — "point at a plane, pinch to track it" on a
photorealistic Earth is the single most demo-able moment available today,
built entirely from parts that already work independently.

---

## Phase 2 — Multimodal deixis (point + speak)

**Revised after building Phase 1 — the original plan below has a real
architectural gap, documented rather than papered over.** Gesture alone
is a blunt instrument for anything with a name (a specific city, a
specific flight). Voice alone has no sense of "this one, right here."
Fused, they cover both — but fusing them needs the fingertip's on-screen
position to mean something in the GLOBE's own coordinate space for a
raycast, and Phase 1 confirmed hand-tracking and the globe are two
separate browser pages/origins (`ui/index.html`'s camera loop vs.
`gods-eye-view`'s Cesium scene at `localhost:4173`) with no shared pixel
space between them — `POST /api/dourmouse/action` can carry a NAMED
action across that gap (Phase 1's whole trick), but a raw (x, y)
fingertip coordinate from one page's video frame has no meaning in the
other page's 3D viewport at all.

Two real ways to actually get deixis, neither of them Phase 1's
"send a named action" trick:

1. **Move the camera+gesture engine INTO the globe page itself** — port
   `onHands()`/the hysteresis engine into `gods-eye-view` (or embed the
   globe inside `ui/index.html` via an iframe with a real message-passing
   channel to reach `window.__godsEyeView`'s live Cesium `viewer` for an
   actual `viewer.scene.pickPosition()` raycast). This is genuine new
   engineering on top of a vendored third-party app, not wiring.
2. **Skip raycasting, keep it coarse**: point gestures already resolve to
   discrete DIRECTIONS (up/down/left/right screen quadrant) cheaply,
   without needing pixel-perfect scene coordinates — "point + say 'what's
   here'" could pan/query in that coarse direction relative to the
   CURRENT camera center (`get_current_view_state`, already used by Phase
   1's `select_nearest_aircraft` chaining) rather than a literal pick.
   Much less precise, but buildable with the SAME bridge Phase 1 already
   proved, no new cross-origin plumbing.

Not started. Option 2 is the tractable next step within the current
architecture; option 1 is the "actually incredible" version and a
real, separate undertaking.

---

## Phase 3 — The companion actually looks back

Face-presence detection already exists (`FACE PRESENT` gesture row: idle
when the face is gone). The companion agent (`general_roster.py`'s
`companion` subagent — "friendly-persona counterpart to orchestrator, for
the Vision workspace chat panel") already exists as a text-only presence
in `workspace.html`.

Fusing them: a small rendered avatar (even a simple animated glyph/HUD
element, not a photorealistic face) that visibly reacts to real detected
state — turns to "attentive" on face-present, shows a distinct state on a
recognized gesture firing (a real confidence pulse, not decorative), goes
"idle" on presence-loss exactly when the existing wake-on-wave gesture
already fires. This is a rendering layer over signals the system already
computes every frame (landmark confidence, presence boolean, last fired
gesture) — no new inference, no new model, just finally showing the user
what the system has been sensing about them the whole time.

---

## Phase 4 — Gesture confidence overlay + trainer (adoption, not features)

14 real gestures is a rich vocabulary most users will never discover
without being shown. Two additive, low-risk pieces:

- ~~**Debug/confidence overlay**: render the actual hand skeleton~~
  **SHIPPED, partially pre-existing.** The 21-point skeleton
  (`drawLandmarks()`) already existed before this plan was written — the
  genuinely missing half, a live hold-progress meter over the camera feed
  showing the hysteresis engine's own real per-frame count toward the
  4-frame threshold, is now built (`paintHoldMeter()`). "Why didn't that
  gesture fire" now has a visible answer in real time instead of being a
  mystery.
- **Interactive first-run trainer**: walk through the 14-row gesture
  table live, waiting for each gesture to actually fire once (reusing the
  existing hysteresis gate as the "you did it" signal) before advancing —
  turns the static table in the VISION screen into something the user
  actually learns by doing, once, and remembers. Not built.

---

## Phase 5 — Personal calibration (Store & Learn, pointed at the body)

The existing long-term memory system (96 facts and growing, "Store &
Learn") already persists what the system has learned across sessions.
Nothing about hand geometry is personalized today — thresholds like the
pinch distance check (`_dist(...) < scale * 0.42`) are one-size-fits-all
constants.

A calibration pass (once, opt-in, during the Phase 4 trainer) that
measures this user's actual hand scale and pinch/spread range, and
persists the derived per-user thresholds through the same memory store
already used for everything else — reduces false-fires/misses for hands
outside whatever range the current constants were tuned against, without
inventing a second persistence mechanism.

---

## Phase 6 (moonshot) — Custom gesture macros

The most ambitious, most novel piece, saved for last on purpose because
it is genuinely new engineering, not fusion of existing parts: let the
user record a gesture sequence, name it, and bind it to ANY directive or
tool call — "this shape means 'read me the news.'" Needs real work: a
gesture-recording mode, a similarity/matching model distinct from the
current fixed-geometry classifiers, and a UI for reviewing/deleting
learned macros. Correctly last: everything in Phases 1-5 is real,
bounded, and buildable from parts that already exist; this alone would
need genuine new ML surface area and should not block shipping the rest.

---

## Sequencing note

Phases 1-4 touch existing, already-tested surfaces additively — none of
them require touching the hysteresis engine, the kill-switch, or the
gesture-to-action table's EXISTING entries, only adding new consumers and
new context-dependent branches (matching the pattern gesture engine
already uses: PINCH already means two different things in two different
modes today). Each phase should land with its own live-verification pass
against the real running desktop app + real camera, the same standard
this session held every other fix to — a gesture roadmap is exactly the
kind of feature where "looks right in code" and "actually fires reliably
in front of a real camera" can disagree.
