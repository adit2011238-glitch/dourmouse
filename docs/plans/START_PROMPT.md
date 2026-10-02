# One prompt to start the work (paste into a fresh session, model Sonnet 5.5, effort medium)

Continue the Dourmouse build. Read in this order, no more: ~/Documents/DOURMOUSE/HARD_RULES.md,
EXECUTION_PLAN.md, PRODUCT_VISION_AND_STATUS.md, then the memory notes dourmouse_final_app and
dourmouse_browser_next_phase. Then:

1. Call get_usage. Report both windows. If weekly is above 85 percent, stop and tell me; do not launch anything.
2. Tell me, in one table, for the next wave in EXECUTION_PLAN.md: each phase, the model and effort you will use, the files it owns, and its expected weekly cost in points. Wait for my "go" or "go parallel".
3. On "go" run the phases of Wave 1 ONE AT A TIME with the Agent tool (safest for budget); on "go parallel" use the Workflow tool, one agent per phase, each with its own model and effort exactly as in the table, strict file ownership, each told: only its own tests, never run the full suite, never touch ports 8765/9333/9334, never pkill, end with a structured report (fixed, not fixed, files, tests, risks, finding text).
4. After the wave: one read-only Sonnet-high reviewer, then the full suite (when no agent writes), fix what fails, write the numbered finding and roadmap line, commit, push (credentials note in EXECUTION_PLAN section 6), run ~/Documents/DOURMOUSE-CODE/refresh_backup.sh, update CURRENT_STATUS.md, REMAINING_WORK.md and memory.
5. Plain-English status to me after each wave: what changed, what you saw live, what is still open, weekly usage now. Never claim something works that you did not see work.

Budget rule: never more than 4 agents running at once (the 5-hour window hit 96 percent with about 8). Estimated weekly cost: Wave 1 about 15 points, Wave 2 about 25, Wave 3 about 20, Wave 4 about 10, plus reviewers and main-thread steps about 25. Total about 95, so plan two weeks: Waves 1 and 2 this week, Waves 3 and 4 after the 10-09 reset. Re-measure after Wave 1 and update these numbers.
