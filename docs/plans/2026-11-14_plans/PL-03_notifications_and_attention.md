# PL-03: Notifications and attention (approvals, finished runs, alerts that open the right place)

Status: planned. Sources: Claude Cowork (you hear about a task only when it finishes or needs a go-ahead), findings USE1-34, 37, 38, 12, 3, 11, and the approval-wait fix of 2026-10-27 (#188).

## Goal
Nothing the model is waiting on, and nothing that finished, is ever invisible. A model asking for approval while you are on another screen, or with the window hidden, reaches you within a second; clicking it takes you to the exact card.

## Today (verified)
- Native macOS notifications exist for **alerts** only: `electron/main.js` polls `/api/state` and shows each new alert (`new Notification`).
- `approvals.openCount()` exists in the shell's core and **nothing calls it** (USE1-34). An approval card appears only in the thread of the screen that asked.
- Deep links parse a sub-path in `core/router.js` and then drop it, so a click cannot land on a specific item (USE1-37).
- Alerts and the HOME attention list do not open their source screen (USE1-38).
- Closing the window with a card open now declines within a second (#188), but the owner is not told it was declined.

## Design
Three kinds of attention item, one feed: **needs you** (approval, consent card, sign-in), **finished** (a goal, automation, long research, download assessed), **heads up** (security finding, trigger fired). Each has a title, a one-line reason, a screen and an anchor, and a state (new, seen, done).
- **Sidebar and dock badge:** the count of *needs you* items, red; *finished* count, quiet.
- **Toast with GO:** appears on any screen; GO navigates to `#/<screen>/<anchor>` and focuses the card.
- **Native notification:** for *needs you* and *finished* when the window is hidden or the app is in the background; the notification click opens the app at the anchor. Quiet hours setting (default 22:00 to 07:00: only *needs you* with a flag of urgent).
- **A declined-because-you-left note:** when a card is auto-declined, an item says "declined because the window closed: send again?" with a re-run button.
- **Phone push (later, PL-17):** the same items, same rules.

## Work breakdown
1. **Deep-link arguments** (`core/router.js`, `core/ctx.js`): pass the sub-path and query to the screen's `mount(ctx)` as `ctx.params`; screens read it. One change, four consumers (USE1-6, 36, 38, 39).
2. **Attention feed** (`AttentionQueue` is a class in `dourmouse/webui.py`, line about 1035, fed by `attention.on_event` in the chat sink; move it to its own module `dourmouse/attention.py` as the first step, keeping the import in webui.py): extend items with `screen`, `anchor`, `kind`; add `needs_you` entries from `WebConfirmationGate` (on request: add; on resolve: mark done) and `finished` entries from goal and automation completion; route `GET /api/attention` and an SSE event `attention_changed`.
3. **Badges and toasts** (`chrome/sidebar.js`, `chrome/toasts.js`, `core/approvals.js`): call `openCount()`, show toast with GO; remember dismissed items.
4. **Native notifications** (`electron/main.js`): fire for the new kinds when `!win.isFocused()`; click handler sends `host.navigate('#/screen/anchor')`; quiet hours read from prefs.
5. **Alerts and HOME attention items open their screen** (USE1-38): use the anchors.
6. **SETTINGS card "Notifications":** which kinds, quiet hours, test button.
7. **Declined-card follow-up item** (webui.py confirmation gate).

## Tests
Router: sub-path and query reach `ctx.params`; hostile hashes still safe (existing router tests). Feed: a gate request creates a needs_you item and resolving removes it; a goal completion creates a finished item; quiet hours table. Node tests for badge and toast helpers. Live check with an isolated server: trigger a gated tool, assert the item, the SSE event, and that GO anchors to the card (screenshot).

## Security and privacy
Notification text never includes message bodies, file names from mail, or secrets (title and a short generic reason only; the full text only inside the app). A notification click only navigates, never approves. Approvals from native notification buttons are **not** offered (a banner button is too easy to hit by accident and cannot show the full action).

## Risks
Notification fatigue; start with *needs you* and *finished goals* only and widen after a week of use.

## Effort and dependencies
About 2 sessions. No dependency. PL-02 and PL-17 build on it.

## Done when
Run a gated action from HOME while on SECURITY: badge appears, toast with GO appears, GO focuses the card; hidden-window case produces a native notification whose click opens the card; counts correct after resolve.
