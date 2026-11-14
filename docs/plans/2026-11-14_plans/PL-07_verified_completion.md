# PL-07: Verified completion ("done" means checked)

Status: planned. Source: a hands-on test of Edge Copilot Mode in which the agent reported it had deleted an email when it had not [report](https://radar.firstaimovers.com/edge-copilot-vs-chatgpt-atlas-2025-ai-browser-battle.md); Dourmouse's own rule (never claim something works that you did not see work); the T-7 finding (the model answers browser questions with zero tools); the Grounded Mode warning that already flags a zero-tool answer.

## Goal
After any action that changes something (send, save, create, edit, delete, type into an app, click in a page, schedule), Dourmouse checks the result and tells you one of three things in plain words: **Verified** (and how), **Not checked** (and why it could not), or **Failed** (and what it saw). It never says "done" on the strength of the model's own sentence.

## Today (verified)
- Tools return honest strings (`REFUSED`, `NOT CONFIGURED`, `CONFIRMATION REQUIRED`), and the base prompt says to report them. Goals double-check each task with a second reasoning pass and mark "unverified" when the check could not run.
- The action ledger (`execution_policy.record`) stores tool name, argument *names* and a hash, not outcomes.
- Grounded Mode (`config.grounded_mode_enabled`) nudges once when a turn with tools available used none.
- No read-back after a write. A tool's own success string is believed.

## Design
**A verification contract per tool class.** Each state-changing tool declares a small `verify` hook next to its handler, run by the one choke point (`_execute_tool`) after the handler returns success:

| Tool class | What "verified" means (examples) |
|---|---|
| File write, edit, delete | read the path back: exists and size or hash matches; deleted path is gone |
| Mail send, draft | the draft or sent item is found by id in the mailbox listing |
| Calendar / Sheets / Docs write | read the object back and see the change |
| Browser click, fill, submit | a new snapshot shows the expected change (URL, element state, success text) |
| App driving type/click | re-read the accessibility tree and compare the target |
| Schedule / automation create | the entry is in the store with the intended spec |
| Memory remember / forget | the row exists or is gone |
| Shell command | exit code and, if the owner named an outcome, a check command |
| Anything with no hook | **Not checked**, stated |

**The result is a first-class field**, not prose: `outcome: verified | not_checked | failed`, `evidence: "<short>"`, shown as a small badge on the tool step in the thread view and in the final answer line ("Saved. Verified: file exists, 1,204 bytes."). The model is told, in the tool result, not to say "done" unless the outcome is verified, and the answer line is **appended by the app**, not written by the model, so the model cannot overstate it.
**Final-answer audit:** before a reply is shown, if it claims an action ("I sent", "I saved", "I deleted", "I booked") and no tool step in the turn changed state with a verified outcome, the app appends "Not confirmed: no action was recorded this turn" and, in Grounded Mode, asks the model once to retry with tools. Deterministic keyword check, no extra model call.
**Failure handling:** a failed verify turns the step red and gives the model the evidence to retry once; a second failure stops and reports.

## Work breakdown
1. `ToolSpec` gains an optional `verify(arguments, result) -> Verification` (in `dispatch.py`); `_execute_tool` runs it under a short timeout, catches errors (an error in verify is "not checked", never a failure of the action), records the outcome on the transcript entry and in the action ledger (outcome and evidence, still no secrets).
2. Hooks for the first eight classes in `general_roster.py` and `browser_agent.py` (files, mail drafts, calendar, memory, schedules, browser click/fill via a lightweight snapshot diff, shell exit code).
3. Final-answer audit in `dispatch.py` next to the Grounded Mode check; the phrase list lives in one table with tests.
4. UI: outcome badge component in `kit/thread-view.js`; `confirm-card.js` shows the outcome after approval.
5. App driving (PL-11) and automations (PL-02) use the same field.
6. Report: SECURITY or COMMAND CENTER shows a verified/not-checked/failed count for the day.

## Tests
Per hook: a success that really happened is verified; a handler that returns success without doing it is *failed* (the Edge failure, reproduced with a fake that lies); a missing hook is not_checked; verify timeout is not_checked. Audit: the phrase table (30 claims) with and without a state-changing step; no extra model call is made. Thread-view node tests. A live run on the isolated server: ask for a file write with a broken write function; the screen shows Failed.

## Security and privacy
Evidence strings pass the redactor (a read-back of a file must not copy its contents into the log; use size, hash prefix, counts). Verify hooks are read-only by rule and tested to be (they may call list/get functions only).

## Risks
Verifying costs time (a mailbox read after every draft): make hooks cheap and skippable for batch work. False "failed" from eventual consistency (mail lists lag): retry with a short backoff before failing.

## Effort and dependencies
About 3 sessions. Feeds PL-11 and PL-02. Independent of the others.

## Done when
A tool that lies is caught in a test; the badge appears on real steps; the "I sent it" claim without a verified step gets the appended note; no extra model calls; suite green.
