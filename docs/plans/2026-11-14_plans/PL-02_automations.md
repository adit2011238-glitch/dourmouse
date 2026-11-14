# PL-02: Automations (scheduled and triggered tasks, reminders, morning brief)

Status: planned. Sources: Grok Automations (16 July 2026), Claude Cowork scheduled tasks, findings CAP1-6, 17, 23, USE1-10, 42.

## Goal
Say in one sentence what should happen and when ("every weekday at 8 tell me about unread mail and today's calendar", "remind me tomorrow at 5 to call the bank", "when a PDF lands in Downloads, summarise it"), approve it once, and have it run, report, and be removable, with a history of what it did.

## Today (verified in code)
- `dourmouse/schedules.py`: a `Schedules` store (`add`, `list`, `remove`, `set_enabled`, `update_spec`, `mark_run`), `parse_schedule` (clock, weekday, "every N minutes/hours/days"), and `SchedulerRunner` (`_tick_once`, `_due`, `_run_one`) which fires **one tool with fixed arguments** and fails closed on any tool that needs confirmation. Missed runs catch up once.
- Routes: `/api/schedules`, `/toggle`, `/update`, `/remove`. The TIMETABLE screen can create (by sending a sentence to HOME), enable and disable, but **not remove**, and shows no history.
- Gaps: no one-shot schedule ("tomorrow at 5"), no trigger other than the clock, a schedule can only run a tool (not a question), no results delivery, no run history, no notify tool, no templates.

## Design
**An automation** = trigger + action + delivery + state.
- **Triggers (v1):** `at` (one-shot), `every` (the existing recurring forms), `mail_match` (a filter on the newest mail, checked on the existing inbox poll), `file_appears` (a folder, reusing the Downloads watcher's polling and settle logic), `security_finding` (a new finding at or above a severity, from the sentry's listener). v2: calendar event starting, a web page changing.
- **Actions:** (a) a tool call (what exists), or (b) **a prompt run through the normal chat path in a fresh thread** with a fixed tool allow-list stated by the owner at creation. Case (b) is what makes "summarise my unread mail" possible. Unattended prompt runs only get REGULAR-tier tools; anything gated is *prepared and parked*, never executed (the existing rule, extended).
- **Delivery:** a notification (native), an entry in a **Run history** (time, trigger, result text, tools used, duration, cost), and optionally mail to yourself. A run that needs approval parks as a pending item and notifies you (depends on PL-03).
- **Creation:** from a chat sentence (the model proposes the automation as a card: trigger, action, tools it may use, where results go; you approve), from templates, or by hand.
- **Safety rules:** nothing destructive, sending or spending unattended; an automation's tools are listed on its card and frozen at approval; a daily cap on runs and on model spend per automation; auto-disable after 3 failures in a row with a notification; owner can run now, pause, remove.

## Templates shipped (5)
Morning brief (unread mail, today's calendar, goals due, top news, security status). Daily security check. Downloads tidy-up (summarise new files, flag risky ones). Weekly research digest (re-run chosen research questions, report what changed). Bill and deadline reminders (from mail subjects and the calendar).

## Work breakdown
1. **Store v2** (`schedules.py`): add fields `kind` (tool or prompt), `prompt`, `allowed_tools`, `trigger`, `delivery`, `last_result`, `fail_count`, `runs_today`; migrate old entries in place (default kind tool); one-shot support in `parse_schedule` ("tomorrow at 5pm", "in 20 minutes", "on 30 October at 9"). Files: `schedules.py`, new `automation_runs.py` (history store, sqlite, capped at 90 days or 5,000 rows).
2. **Runner v2:** prompt runs through `ChatSession.ask` with `force_plain_dispatch`-style scoping and `RunPolicy(actor="automation")`; parked approvals; budget and failure rules. Files: `schedules.py`, `dispatch.py` (the policy hook only).
3. **Triggers:** `mail_match` on the Gmail poll, `file_appears` reusing `security/downloads.py`'s `DownloadsWatcher` pattern, `security_finding` via `SentryRuntime.add_listener`. Files: new `automation_triggers.py`; one call site each.
4. **Notify tool:** a REGULAR tool `notify_owner(title, body)` that adds an alert (`state_store.add_alert`) so the existing native notifications fire. Files: `general_roster.py` (one small tool).
5. **TIMETABLE screen:** REMOVE, RUN NOW, history drawer per automation, template picker, empty state with the five templates, an "automation card" approval. Files: `ui/assets/os/screens/timetable/*`, `dourmouse/os_api/timetable.py` (new routes: history, run-now, templates).
6. **HOME:** a "what ran overnight" strip (feeds PL-08).
7. **Planner/routing:** routing words for reminders and schedules (CAP1-23), so "remind me" reaches the right agent.

## Tests
Parser table (30 phrases incl. DST edges and "tomorrow"); store migration from an old file; runner with a fake clock: one-shot fires once, catch-up fires once, failure counter disables after 3; a prompt run with a gated tool parks and does not execute; mail trigger with a fake inbox; file trigger with a temp folder; history cap; notify tool creates an alert; UI contract test and a node test for the timetable helpers. A live test: create "in 2 minutes remind me to stand up", watch it fire (silent: check the alert and the history row, not the sound).

## Security and privacy
Prompt automations are the new unattended code path: they run with the lowest tier, the privacy gate applies (a mail-reading automation needs Mail consent standing, shown on its card), results are stored locally only and appear in the egress ledger like any call. Add the prompt-injection corpus to this path: a hostile mail must not make the morning brief send anything.

## Risks
Model spend from a runaway automation (caps above). Mac asleep means missed runs (catch-up once, and say so in history). Wake-time clock jumps (use wall-clock comparisons with the stored last-run, as the runner does).

## Effort and dependencies
About 4 sessions. Depends on PL-03 for parked approvals and notifications (can ship without: parked items just wait in history). Enables PL-08 (Today panel).

## Done when
Create-from-sentence works for the five templates and one-shot reminders; a run appears in history with its cost; TIMETABLE can remove; 3 failures disable with a notification; no gated tool ever executes unattended; tests and live check pass.
