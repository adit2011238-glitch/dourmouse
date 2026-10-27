# Test, stress and usefulness report (2026-10-27)

You asked me to test everything, stress it, read the code in the background, and think about how to make Dourmouse more useful. This is what was done, what was found, what is already fixed, and the ranked list of what to build next.

## 1. What I did

| Work | How |
|---|---|
| Full test suite | Run in two halves on the fresh tree (see section 6 for the final numbers) |
| Stress tests | A harness that starts a **separate** copy of the server (real routes, real agent roster, its own empty data folder, a stub model, its own port). Your app and your data were never touched. Scripts are in `~/Documents/DOURMOUSE/stress/`. |
| Background code reads | Three read-only reviewers: robustness under load (25 findings), product usefulness across every screen (43 findings), and agent capability and routing (27 findings). Files: `scan/STRESS1_findings.md`, `scan/USE1_findings.md`, `scan/CAP1_findings.md`. |
| Brainstorm | Section 4 below, from the findings plus my own ideas. |

## 2. Stress results (measured, on the isolated server)

| Experiment | Result | Verdict |
|---|---|---|
| 1,280 simultaneous requests to three busy routes | 215 to 914 requests a second, no errors, slowest 5 percent under 0.7 s | Good |
| 64 chat turns at once on 16 tabs | all succeeded in 1.1 s | Good |
| 8 turns at once on the same tab | all succeeded | Good |
| 8 MB body, invalid JSON, 50,000-deep nesting, NUL and unicode tricks, 64 KB URL, 1,000 headers | all refused cleanly with 400, 414 or 431 | Good |
| Number with 6,000 digits in a JSON body | **500 error with a traceback** | **Bug, fixed** (SEC1-16, confirmed here) |
| 200 half-open connections ("slowloris") | server kept answering instantly, threads returned to 8 afterwards | Good |
| 100 event-stream connections opened and dropped | no leaked threads, hub back to 0 clients | Good |
| 3,500 requests, memory | 312 MB before, 312 MB after | Good |
| 30 queued chats, 25 abandoned | only the 5 live clients got model calls | Good |
| **One event-stream client that stops reading** | **a broadcast blocked 60 s, then raised, and the client stayed registered so the next event stalled again** | **Serious bug, fixed** |
| Upload cut off at 1 MB of 40 MB | the partial file was kept as if complete | **Bug, fixed** |
| 5,000 streamed tokens into the office log | 1.30 s on the agent's thread and 5,001 database rows | **Slow, fixed** (0.01 s, 15 rows) |

### The serious one, in plain words
One browser window that stopped reading the live event stream (a laptop asleep, a hidden window, a stuck page) could freeze **every agent run** for a minute at a time, repeatedly. It was a latent bug for status updates and I made it much more likely when I built the Command Center (it broadcasts every token). It is fixed properly: each window gets its own queue and writer, so a slow window can only hurt itself.

## 3. Fixed today (finding #188), all with tests that fail without the fix

| ID | Problem | Fix |
|---|---|---|
| STRESS1-1 / ST-1 | One non-reading window stalled every agent | Per-window queue and writer thread; dead windows leave the hub |
| STRESS1-2 | Closing the window with an approval card open froze all chat for up to 5 minutes | The wait checks the connection every second and declines when the window has gone |
| STRESS1-10 | A full disk threw away a finished answer and could corrupt the next history line | Answer returned with a "not saved" warning; a torn last line is repaired |
| STRESS1-12 | One database write per streamed token | Tokens joined per run (130 times faster, 333 times fewer rows) |
| STRESS1-14 | A five-minute research run lost everything if one step failed | Saved after every source; any error skips only that source |
| SEC1-16 | 500 error on a huge number in a request | Treated as an empty body |
| ST-4 | Cut-off uploads kept as whole files | Rejected, nothing written |
| **CAP1-1** | **The research-first rule was inert for ordinary questions** | See below |

### CAP1-1 matters most, and it was my own change
When I changed the prompts so answers must be researched (finding #184), I assumed the model would be given web tools. It was not. Tools are only attached when an agent scores 3 or more on keywords, and a plain question scores nothing: "Who is the current UK prime minister?" scored 2.8 for the *browser* agent and got **no tools at all**; "Is it true that Python 3.15 removes the GIL?" went to the *coding* agent. So the model could not follow the rule I had just written. Now any question that needs evidence, or any message with a link in it, always gets web search and page fetch. Operational requests ("how much free disk space do I have", "check my inbox") deliberately do not. Checked on the real roster for eight kinds of question.

## 4. What to build next: ranked by value for the effort

From the three reviews plus my own ideas. "Small" means under about 50 lines.

### A. Make the agents more capable (from the capability review)
1. **Fix the routing words (CAP1-2, 3, 4).** Chit-chat like "I was thinking we could be ready by Monday" scores security 6 because words match inside other words; "quote", "price", "drive", "schedule" hijack unrelated requests; "organize" and "downloaded" miss their agent for want of simple word endings. One cleanup of the router. *Value: every message gets better tools.* Small to medium.
2. **Reminders (CAP1-23).** "Remind me tomorrow at 5" cannot work today: no routing word, no one-shot schedule, no way to notify you. All the pieces exist. About 90 lines. *The most-asked-for assistant feature.*
3. **"Find the PDF I downloaded last week and quote X" (CAP1-5).** Breaks in four places (routing, no date filter, finder and reader on different agents, no passage search in a PDF). About 60 lines.
4. **Scheduled prompts and a daily briefing (CAP1-6, 17, USE1-42).** "Every morning tell me what changed" needs a way to schedule a *question*, not only a tool. About 120 lines.
5. **Bespoke prompts for the 22 agents that have none (CAP1-8)**, including security, trading and the research pipeline, and the 15 worst tool descriptions rewritten (CAP1-20, rewrites are in the file).

### B. Make the screens easier to use (from the usefulness review)
1. **Approvals and finished runs are invisible when you are on another screen (USE1-34).** A model waiting for your approval does nothing visible unless you are on HOME. A toast with a GO button and a badge: small. *This alone will stop "it just sat there".*
2. **Links that carry a target (USE1-37).** Deep links drop their arguments, so no screen can send you to a specific item. One small change unlocks four more ideas (alerts that open the right screen, NEWS to RESEARCH, and so on).
3. **SETTINGS cannot set an API key or pick the model (USE1-28).** The routes exist and no screen calls them. Medium.
4. **TIMETABLE routines can only be switched off, never removed (USE1-10)**, with an empty box where ready-made routines (daily scan, morning mail check) would help. Small.
5. **NEWS "to research" leaves a record RESEARCH cannot continue (USE1-6).** Add a RUN button and select the new question. Small.
6. **Buttons that carry a prepared question to HOME**: summarise or draft a reply from COMMS (USE1-1), ask about this diff from CODE (USE1-21), save this page to RESEARCH from BROWSER (USE1-32), act on the all-agents answer (USE1-15). Each small.

### C. New ideas of my own
1. **Today panel on HOME.** One screen at the start of the day: unread mail, today's calendar, goals due, top news, security status, and what your overnight routines found. Built from routes that already exist. *Turns Dourmouse from something you ask into something that tells you.*
2. **Quick capture.** One key, say or type a thought, it lands in the right place (a goal, a note, a reminder). Pairs with the voice plan.
3. **Usage and budget meter.** The routes (`/api/usage`, `/api/budget`) exist and nothing shows them; you care about weekly usage.
4. **Search across your own past.** "What did I ask about X last month?" The session and memory search routes exist, unused.
5. **A health page for Dourmouse itself.** Backends up or down, model speed over time, error counts, disk used. The logs already hold it; it would also have caught today's stalls.
6. **Answer quality badge on every research answer**: how many claims, how many checked sources, how many failed. Makes "researched or not" visible, which is your original complaint.
7. **System prompt diet.** The prompt sent on every turn measures about 38,000 characters (roughly 9,600 tokens). Cutting the roster text for agents the question cannot need would make every turn faster and cheaper. Measure first, then trim.
8. **Offline and first-run experience.** What every screen shows with no network, no key, no Google sign-in. The stress plan has an experiment for it (STRESS_PLAN 9); not yet run.

## 5. Not yet done (honest list)

- The stress plan has 10 experiments; I ran 6 equivalents. Not run: disk-full with a real small disk image, a sleep-and-wake simulation, offline first run, and a research-pipeline kill test against a stub web server.
- 19 stress findings remain open (for example: the whole history file is re-read every turn, and tool results have no central size cap). They are in the ledger as `STRESS1-n`.
- Of the 43 usefulness ideas and 27 capability findings, none beyond CAP1-1 are built yet. They are ranked above.
- The security fixes from the 2026-10-19 assessment (seven highs) are still unbuilt.

## 6. Verification

See the final numbers in the commit message and audit entry #188 (the full suite was run in two halves after all edits; any failure is named there).
