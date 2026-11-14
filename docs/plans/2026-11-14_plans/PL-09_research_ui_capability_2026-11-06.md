# PL-09: research that works, more capability, a redesigned UI (2026-11-06)

Owner asks (2026-11-06): redesign the UI, increase what Dourmouse can actually do, make the research feature work properly, stop hitting the call budget. The budget part is done (finding #192). This plan covers the rest. Nothing below is built yet.

## 1. What today's testing already showed

- **Call budget (fixed, #192).** Three of eight steps were waste, the planner never offered the markets agent for "S&P 500", a rate-limited tool was retried seven times, and the last-chance summary returned empty. Live: 8 to 11 calls with the budget exhausted became 3 calls and a full answer.
- **Claude front was silently broken (fixed, #191).** An inherited ANTHROPIC_* environment sent every Claude turn to the wrong provider, and the owner's own Claude login on this Mac is expired. Until `claude auth login` is run, chats use the cloud model.
- **Command Center shows LIVE lanes for agents that did nothing (CC-1, open).** A fresh workspace lists tasks, mail, rnd and markets as "currently live" with "no call id". A badge that says LIVE must mean a live call.
- **Market data has one source (MKT-1, open).** Yahoo rate-limits this Mac; every quote then fails. Stooq's keyless CSV returned "not found".
- **Research screen, as built.** 13 of 15 pipeline stages have a tool. Not built, by the screen's own text: source validation (nothing judges a source's credibility; only the verbatim passage check exists) and statistics (describe_samples exists in code, no registered tool calls it). The pipeline is plan, discover sources, extract evidence with a verbatim passage check, synthesize, detect contradictions, critique, follow up, experiments.

## 2. Research: make it work properly

R1. **Measure first (half a day).** Run five fixed questions through RESEARCH and HOME and record, per run: time, sources found, claims kept, claims rejected, citations that resolve to a real passage, contradictions found, failures. Questions: a checkable fact, a contested claim, a numeric statistic, a recent news event, a "compare these two sources". Output: a table in EVIDENCE with the failures named. The owner's "doesn't work properly" becomes a list.
R2. **Source validation tool (M).** Deterministic score per source from domain class (primary, publisher, aggregator, social), date, whether another independent source agrees, and whether the passage was found verbatim. Shown beside each claim.
R3. **Statistics stage (S).** Register describe_samples as a tool and let the pipeline call it when claims carry numbers.
R4. **Answer quality badge (S).** Every research answer shows claims, checked sources, failed sources. Makes "researched or not" visible.
R5. **Budget-aware research (M).** Save after every source (done in #188), show progress, and when steps run out return the verified claims so far instead of nothing (the last-chance summary of #192 now does this for chat).
R6. **Export for writing (S).** One button: a document with the answer, the claims, and a reference list in MLA, because the owner writes school reports.

## 3. More capability (from COMPETITOR_RESEARCH_AND_IDEAS_2026-10-27, ranked by value for effort)

1. A morning brief as a screen and as a scheduled question (the code exists, nothing fires it daily).
2. Saved prompts as skills, called with `/name` in any chat.
3. Deep links that carry a target, so alerts, NEWS to RESEARCH and similar buttons open the right item (USE1-37).
4. A memory screen that shows, edits and deletes what Dourmouse remembers.
5. A second market data source and a cached last good quote (MKT-1).
6. Buttons that carry a prepared question to HOME (summarise this mail, ask about this diff).

## 4. UI redesign: three directions, pick one

Problems now: text-heavy screens, one green-on-dark palette, many empty states that explain instead of show, and at least one screen (Command Center) that shows data that is not live.

- **A. Refine the shell (recommended first).** Keep the OS shell and sidebar. New type scale, card components with real hierarchy, empty states that show a sample result, live badges tied to real events, consistent spacing. Screen by screen, each with before and after screenshots. Lowest risk, visible in days.
- **B. Home as a live canvas.** Replace the chat log on HOME with cards: today's brief, agents working now, security posture, research in progress, with the chat as a drawer. Biggest change in feel, needs A's components first.
- **C. Command bar first.** Raycast style: one bar, results as cards, screens secondary. Fast for power use, weaker for the "operating system" story.

Method for any of them: a clickable HTML mockup first (owner picks), then build one screen at a time, test through the real UI, screenshot evidence per screen, no screen ships with sample data presented as live.

## 5. Decisions needed from the owner

1. Which UI direction (A, B or C), or A then B.
2. Research first, or UI first? Recommendation: R1 (measure) this week, then UI direction A in parallel.
3. Should the Claude CLI be signed in again (`claude auth login`) so Claude front mode returns, or stay on the cloud model only?
