# G benchmark: how to measure the model's tool skill for real

Written 2026-10-05 by the phase G builder. Nothing in this file has been run in `--real` mode: a real run sends 40 real model turns and tools that need no confirmation really execute, so the owner (or the main thread, with the owner's say) decides when. What HAS been run: `--validate` and the default stub run, both on the phase G tree, 40 of 40 (stub only proves the harness, not a model).

The target in EXECUTION_PLAN.md is 85 percent of the 40 tasks (34 of 40), measured from a real baseline.

## What the runner does

`scripts/bench/run.py --real` posts each prompt in `scripts/bench/tasks.json` to `POST /api/chat` on a running server, records every `tool_use` event, and scores it (all_of, any_of, none_of plus argument checks). It never answers a confirmation: when a gated tool is chosen it records the call as made and drops the connection, so nothing confirmation-gated is ever approved. It refuses ports 8765, 9333 and 9334.

The request body is only `prompt` and `tab_id`, so which model answers is whatever the server resolves for a free chat turn (the runner does not send `force_backend`). Keys come from the user config file (`~/.config/dourmouse/.env`, `OLLAMA_API_KEY` and `GEMINI_API_KEY`), the same file the live app reads; do not paste keys into a chat or a command.

## 1. Check the tasks still match the registry (free, no network)

```bash
cd /Users/aditagrawal/dourmouse-recon
.venv/bin/python scripts/bench/run.py --validate
.venv/bin/python scripts/bench/run.py          # stub, scripted, expect 40/40
rm -rf scripts/bench/results                   # the stub run writes a results file; it is untracked
```

## 2. Make the fixtures the prompts name (all under /tmp/dm_bench)

```bash
mkdir -p /tmp/dm_bench/project
printf 'Quarterly notes.\nThe invoice for March is overdue.\nCall the vendor.\n' > /tmp/dm_bench/notes.txt
printf 'invoice 4471 from Acme\n' > /tmp/dm_bench/project/billing.txt
printf 'def foo_old():\n    return 1\n' > /tmp/dm_bench/app.py
printf 'def helper():\n    return 1\n\ndef helper_again():\n    return 1\n' > /tmp/dm_bench/project/util.py
printf '%%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%%%EOF\n' > /tmp/dm_bench/report.pdf
```

The PDF is only a file that exists with the right extension. The task checks that `open_file_preview` was called with the path, not that the pane rendered it.

## 3. Start an isolated server (never port 8765)

In a separate terminal tab (stop it later with Ctrl-C there; do not use pkill):

```bash
cd /Users/aditagrawal/dourmouse-recon
DOURMOUSE_UI_PORT=18791 scripts/dev_preview_webui.sh
```

This is the owner's existing recipe: a throwaway workspace (`.dev-preview-workspace`) on port 18791. It carries no Electron pane, so the `browser_*` tools will answer honestly that no pane is reachable; the calls still count, because the tasks score the tool the model chose, not the page. With no `DOURMOUSE_ACCESS_TOKEN` and no launch secret, loopback needs no cookie. If the server was started with a token, pass `--cookie "dourmouse_session=..."`.

Honest limit: the isolated server shares the real Mac. `read_path`, `write_path`, `run_command`, `open_path` and the other system tools act on the real filesystem, not the throwaway workspace. The tasks only name `/tmp/dm_bench`, and the four refusal tasks rely on the confirmation gates and the command guard, which the runner never approves.

## 4. The real run (this spends model calls)

```bash
cd /Users/aditagrawal/dourmouse-recon
.venv/bin/python scripts/bench/run.py --real --port 18791 --timeout 180 \
  --out scripts/bench/results/real-after-G.json
```

A cheaper first look at one category: add `--only browser` (categories are files, browser, media, mail, calendar, docs, system, research, code, refusal) or a comma list of task ids such as `--only browser-04,media-02`. Add `--min-pass 0.85` to make the exit code say whether the target was met.

Expect up to 40 times 180 seconds in the worst case; a healthy run is a few minutes to about half an hour depending on the model.

## 5. The baseline before phase G (needed to claim an improvement)

Phase G is uncommitted. To measure the tree as it was before G without a second checkout, stash only the files G changed, restart the isolated server (Ctrl-C the tab, run step 3 again), run the same command with a different `--out`, then restore:

```bash
cd /Users/aditagrawal/dourmouse-recon
git stash push -m g-baseline -- dourmouse/general_roster.py dourmouse/agent_prompts.py \
  dourmouse/system_access.py dourmouse/app_driver/tools.py dourmouse/dispatch.py \
  dourmouse/tests/test_browser_agent.py
# restart the isolated server (step 3), then:
.venv/bin/python scripts/bench/run.py --real --port 18791 --out scripts/bench/results/real-before-G.json
git stash pop
```

Do this only while no other builder is editing those files (the stash takes their uncommitted changes with it). If G has been committed by then, use the parent commit instead: `git stash` is not needed, run the baseline from a commit before G using a normal `git checkout <sha> -- <those files>` and `git checkout HEAD -- <those files>` to put them back.

Compare the two JSON files: the summary has per-category pass rates and each failing task lists its reasons ("missing browser_fill", "called forbidden ...", argument check failures).

## 6. Known problems with the tasks themselves (decide before reading the number)

These were found while checking each task against the real handlers. None was changed, because a pass rule must not be edited to make a number better; each is the owner's call.

- files-04 expects `search_files`, but `search_files` is workspace-sandboxed and refuses an absolute path like `/tmp/dm_bench`. A correct model answers with `run_command` and grep, and fails this task. The description now says so; the rule probably should accept `run_command`.
- browser-04 ("type hello world into the search box") expects `browser_fill` with a `value` argument. `browser_type` now exists and its description says it is for editors where fill does not reach, but a model that picks it is arguably right and fails the rule.
- cal-02 expects `propose_time_slots`, which does not read the calendar (it lists weekday 30-minute steps). A careful model may call `list_calendar_events` first and then propose; that still passes (all_of only needs the expected tool), but it is a sign the task measures less than its name says.
- media-02, media-03 test `player_now_playing` and `player_seek`, which report NOT CONFIRMED and last-opened state only. The task passes on the call, not on the player actually moving.
- The tasks score which tool was called and how. They do not score whether the final answer was true. A high pass rate says the model reached for the right tool, nothing more.
