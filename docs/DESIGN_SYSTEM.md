# Design System — Phase 3

Canonical source: `ui/assets/dourmouse-ui.css` (425 lines). It already states a
real, considered design philosophy — this doc extends it and tracks what's
missing; it does not replace it. See `docs/UI_SOURCE_MAP.md` for the audit
that found this system exists but is used by almost nothing (`console.html`
and `workspace.html`, the two surfaces that matter, each define their own
separate, independently-drifting `:root` palette instead).

## Principles (already established, keep exactly as-is)

From the stylesheet's own header, four anti-patterns it exists to prevent:

1. **No floating cards.** Space divides by 1px structural lines and solid
   panes — reads as a workbench, not widgets scattered on a desk.
2. **No translucent wash.** A surface is exactly one of four solid layer
   colors, never `rgba()`/opacity layered for fake depth.
3. **No decorative glow.** No gradient borders, radial glows, or sparkle
   iconography. Status is a 6px solid dot.
4. **No icon/badge vomit.** Typography carries hierarchy. Icons only where
   space is genuinely scarce (a collapsed rail, an icon-only button).

Motion: transform/opacity only, 180–260ms, ease-out. Never animate
width/height/top/left/margin/padding.

## Tokens (real, current)

```
--dm-canvas      #09090B   root/streams/output, ~60% of the screen
--dm-layer       #18181B   rails, inactive tabs, headers, ~30%
--dm-layer-hi    #27272A   active tab, code-block header, hover fill

--dm-line        #27272A   every pane/tab/window division
--dm-line-focus  #3F3F46   focus rings and input borders only

--dm-fg          #FAFAFA   headers, active titles, the user's own text
--dm-fg-body     #A1A1AA   body copy, logs, commentary
--dm-fg-label    #71717A   small uppercase metadata labels
--dm-fg-dim      #52525B   timestamps, line numbers, paths, hints
--dm-fg-prose    #D4D4D8   assistant prose in the terminal feed

--dm-active      #F59E0B   selection / active state (the one real accent)
--dm-ok          #10B981   online, passing, settled
--dm-error       #EF4444   breaking log, failed run

--dm-font-sans   'Departure Mono', 'Commit Mono', 'Intel One Mono', ui-monospace, monospace
--dm-font-mono   'Berkeley Mono', 'Monaspace Neon', 'JetBrains Mono NL', 'JetBrains Mono', ui-monospace, monospace

--dm-row-y 6px · --dm-row-y-lg 8px · --dm-gap 8px · --dm-gap-lg 12px
--dm-dur 220ms · --dm-ease cubic-bezier(0.16, 1, 0.3, 1)
```

Accent budget: all accent color combined stays under ~10% of the screen —
enforce this in every new component, not just the ones that happen to
remember.

**Note on the Security/Research concept mockups** shared earlier this
initiative (a separate design-canvas artifact): those used their own
standalone teal accent for that exploration, which was appropriate for a
throwaway concept page but is **not** what Phase 3 implements — the real
build adopts `--dm-active` (amber) as the one accent, per the existing,
already-deliberate token above. The mockups' layout ideas (topology view,
device list, tool-activity panel, composer bar) carry over; their color
choices do not.

## Existing components (`.dm-*`, already built, already correct)

`dm-label`, `dm-prose`, `dm-mono`, `dm-dim`, `dm-meta` (typography) ·
`dm-split`/`dm-split-col`/`dm-pane`/`dm-pane-fill` (layout) ·
`dm-rail-left`/`dm-rail-right`/`dm-center` (three-pane shell) ·
`dm-pane-head`/`dm-pane-body` · `dm-tabs`/`dm-tab` · `dm-btn`/`dm-btn-primary` ·
`dm-input`/`dm-select`/`dm-textarea` · `dm-pill` · `dm-dot`/`dm-dot-ok`/
`dm-dot-active`/`dm-dot-error` · `dm-row`.

`ui/index.html` (235 usages) and `ui/design-system.html` (the live reference
demo, 125 usages) are the two real examples to study before building new
components — everything else uses 0-7.

## Gaps Phase 3 must close (real, not cosmetic)

1. **No type scale.** `console.html`'s own style block has 18 distinct
   font-size values in half-pixel increments (`docs/UI_SOURCE_MAP.md` §2).
   Define one scale as tokens (`--dm-text-xs/sm/base/md/lg`, mapped to the
   Display/Heading/Body/Secondary/Caption/Code/Metadata roles the spec asks
   for) and migrate call sites onto it — do not invent a second, competing
   scale.
2. **No spacing scale beyond row/gap.** `--dm-row-y`/`--dm-gap` cover dense
   rows; nothing governs section/panel-level spacing. Add
   `--dm-space-1..6` (4/8/12/16/24/32px) before building new panels, so
   Phase 3 components don't reintroduce the 720 raw-px-literal problem the
   audit found.
3. **No icon system.** Currently: an unused vendored Lucide bundle (removed
   in the Phase 1 audit pass), a handful of Unicode glyphs (`◆`/`▸`/`▾`/`✕`/`●`),
   and text labels everywhere else. Phase 3 needs a real, small, consistent
   icon set — inline SVG, stroke-based, matching the "icons only where space
   is scarce" principle (not a wholesale icon-ification of the UI).
4. **No component states as a first-class concept.** Every new
   `Widget`/`Card` component must define loading / populated / empty / stale
   / unavailable / error explicitly (spec requirement, and directly serves
   "real data only, no fake widgets" — an explicit empty/unavailable state is
   what replaces a fabricated placeholder value).
5. **Token drift across surfaces.** `console.html` and `workspace.html` each
   define their own separate `:root` (same token *names*, independently
   maintained — a real change means editing both). Phase 3's first concrete
   step is migrating both onto `dourmouse-ui.css`'s real `--dm-*` tokens
   directly, deleting the duplicated local palettes, not adding a third one.

## New components Phase 3 builds (priority order, from `docs/UI_SOURCE_MAP.md`'s findings)

1. `DiffWidget` — compact `+N −N` summary, expandable real syntax-highlighted
   hunks. Highest value: the tools already exist, nothing renders them.
2. Real syntax highlighting for code blocks generally (language already
   parsed and discarded today — wire it to an actual highlighter).
3. `ToolActivity` — the five-state (queued/running/completed/failed/
   cancelled) component described in `docs/UI_DESIGN_REFERENCES.md`,
   replacing the current ad hoc `.act` chip.
4. `TerminalWidget` — summarized command output (exit code, pass/fail counts
   when detectable) with the existing truncated-raw view as fallback.
5. Command palette (⌘K) on `console.html` — exists today only on the legacy
   `index.html`; the actual daily-use surface has none.
6. `SourceWidget` — compact source/evidence cards for research (domain, type,
   relevance; expand for passage/citation), replacing plain-text search
   results.
7. Security/Network center (net-new, ties to Phase 4) and a Goals/Tasks view
   (net-new, ties to Phase 2's `GET /api/goals`) — both build on the
   concept-mockup layouts, with real `--dm-*` styling, real data, real empty
   states, no fabricated numbers.
8. Live-activity list virtualization / a real cap on the backing `liveEvents`
   array (currently unbounded — `docs/UI_SOURCE_MAP.md` §7), before any new
   long-lived list widget makes the same mistake.

## Accessibility (spec requirement, not optional)

Real `<button>`/`<a href>`/`<input>`+`<label>` for every interactive element —
the audit found one concrete counterexample (the tool-call disclosure chip,
a plain `<div>` with `.onclick`, no keyboard path) alongside several correct
examples (queue-remove button, mail rows) to match, not invent from scratch.
`aria-label` on every icon-only control. 4.5:1 text contrast (3:1 at 24px+).
