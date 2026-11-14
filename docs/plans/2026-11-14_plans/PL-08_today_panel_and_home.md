# PL-08: A "Today" start screen (the assistant that tells you things)

Status: planned. Sources: Grok's Morning Brief automation (unread mail, calendar conflicts, deadlines), ChatGPT Atlas's home page that remembers what you explored and suggests next steps, Edge Copilot's resumable topic projects, USE1-42, USE1-2, 8, 9.

## Goal
Open Dourmouse in the morning and, before typing anything, see what matters today in one place, each item one click from the action. HOME becomes "Today" above the chat thread; nothing on it is invented, every line comes from a route that exists.

## Today (verified)
HOME is a chat thread plus a project scope bar and an attention list (`screens/home/index.js`, 224 lines). Data that already has a route but no place on HOME: unread mail (`/api/gmail...`, COMMS), calendar events (Calendar tools), goals and their states (`/api/goals`), top news (`/api/news`), security posture (`/api/security_dashboard`), overnight automation runs (PL-02), usage and budget (`/api/usage`, `/api/budget` have no caller), the sessions list (`/api/sessions/recent`), pending approvals (PL-03).

## Design
**Today strip (collapsible, remembered):** six compact cards, each with a headline number, one line of text, and a primary action. Cards load independently and fail independently (a card that cannot load says why and offers retry).

| Card | Shows | Action |
|---|---|---|
| Mail | unread count, 3 newest senders and subjects (no bodies) | OPEN COMMS; "summarise" sends a skill run to HOME |
| Calendar | next 3 events, conflicts flagged | ASK "prepare me for the 2pm" |
| Goals | running, waiting on you, due today | OPEN GOALS |
| Overnight | what automations ran and any that need you | OPEN history (PL-02) |
| Security | posture word and top finding (honest: "3 high") | OPEN SECURITY |
| Usage | weekly usage and reset time | OPEN SETTINGS usage |

**Continue where you left off:** up to three items from recent sessions, research records in progress, and open browser tabs grouped by topic ("visa research, 4 tabs, Tuesday"), with RESUME. Built from `/api/sessions/recent`, the research store and the tab list; the topic grouping is a cheap deterministic grouping by host and shared words first, a model call only on demand.
**Suggested next steps (v2, opt-in):** three one-line suggestions the app can justify from the cards ("2 mails mention Friday deadlines: add to calendar?"). Each suggestion shows its evidence and is generated only when you open the strip, not in the background.
**Privacy line:** "Last 24 hours: N cloud requests, M sources shared, K redactions" from the egress ledger, linking to the SECURITY ledger (this is the thing no competitor shows; also PL-12-style trust).

## Work breakdown
1. Aggregation route `GET /api/os/today` returning all cards in one response with per-card status, so the screen paints from one call (and one cache, 30 s) instead of seven; each card is computed by an existing function, no new data sources. Files: new `dourmouse/os_api/today.py`.
2. `screens/home/today.js` (new) with a pure helper module for the card models (node-testable), integrated above the thread; collapse state in prefs; refresh on screen focus and on matching SSE events (`security_scan`, `delegate_fanout`, attention events).
3. Resume model: `today_resume.py` grouping tabs and sessions; RESUME opens the right screen with `ctx.params` (PL-03).
4. Notes view (small follow-up): a read-only list of notes saved by `write_note` and PL-04's panel, so notes have a home (WIKI today does not hold them).
5. Suggestions (v2): a single bounded model call over the card data, scoped to REGULAR tools, never run in the background.

## Tests
`/api/os/today` with every upstream unavailable (each card reports its reason); with one slow upstream (the others still arrive: per-card timeouts of 3 s); card model helpers; privacy: the Mail card contains no body text; no card triggers a cloud call by itself (spy on the client); screen contract test; a visual check with screenshots at two widths.

## Security and privacy
The Today route is owner-only (it aggregates mail and calendar). Mail subjects are personal data: shown locally only; the Mail card's "summarise" is a normal gated chat turn. The route never fetches mail bodies.

## Risks
Slow first paint if Gmail or Calendar are slow: cache with a visible age ("5 min ago") and refresh in the background. Overcrowding: six cards maximum, user can hide any.

## Effort and dependencies
About 3 sessions. Gains most from PL-02 (overnight) and PL-03 (needs-you); works without them with those cards hidden.

## Done when
HOME shows Today with real data, each card degrades alone, no card makes a cloud call on its own, the privacy line matches the ledger, and an owner day-start takes one glance instead of five screens.
