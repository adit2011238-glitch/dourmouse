# PL-10 T3: change plan for agents, jobs and the Agent Room (2026-11-13)

Status: **plan only. No code has been changed.** Nothing below is built until the owner says yes in the T3 session.
Branch: `claude/sweet-almeida-e10eec` (worktree), based on `recon-2026-09-11` at `cd777c1`.

## Owner decisions already made (T3 session, 2026-11-13)

1. Roster: approved as shown. 44 agents become 13 plus Agent Smith. Nothing is deleted; every old name keeps working.

| New agent | Old agents it takes over (they become its skills) | Tools today |
|---|---|---|
| Orchestrator | orchestrator, companion (its friendly voice), messenger (bus tools go to every agent) | 7 |
| Researcher | research_info, evidence_pipeline, rnd, study, research_mesh | 29 |
| Librarian | memory, device_wiki, admin_ops | 17 |
| Mail and Calendar | mail, comms, scheduling; google_workspace's Gmail and Calendar half | 17 |
| Docs | docs; google_workspace's Drive, Docs, Sheets and Slides half | 11 |
| Browser | browser | 21 |
| Mac Operator | system, apps, app_driver, compute, panel_control | 47 |
| Coder | dev_coding, reviewer (read-only mode), code_claude, code_codex, code_deepseek, code_nvidia, code_ollama (backend choice), freebuff | 27 |
| Security | security | 28 |
| Markets | markets, forex, t212, mt5 | 17 |
| News and World | news, worldmonitor, globe | 10 |
| Media | media, music, design_3d | 15 |
| Planner | goals, tasks | 11 |
| Agent Smith | agent_smith (toolsmith, owner-gated, unchanged) | 2 |

Tool counts exclude the two shared memory lookups every agent has. Checked against the live registry: the mapping covers all 44 names exactly once, and all 258 tools stay reachable. google_workspace's 8 Drive, Docs, Sheets and Slides tools are already Docs tools today (same objects), so that half of the split costs nothing.

2. Agent Room: its own tab.
3. Room screen: a plain working screen now, on today's shell styles. T6 restyles it once its design system is approved (the coordinating session confirmed the owner's choice stands). T6's seven sidebar places put the Room inside Agents; T6 decides where it sits in the new layout.
4. Live capability tests may touch real accounts read-only: read the inbox, list the calendar, now playing, broker account summary. Never send, delete, buy or sell.
5. Pancakes (owner, 2026-11-14: "make it so we can integrate pancakes later"): **design to join later.** The Room stays local and uses Pancakes' message shape and loop guard, so a bridge can be added later. The same reply is taken as the yes to this plan; building starts with step 1.

## Facts this plan rests on (checked in today's code)

- The registry (`dispatch.DispatchRegistry`, dispatch.py:1265) holds 44 agents. Tool names are unique; one tool object can belong to several agents (`extend_subagent`, dispatch.py:1291).
- Old names are everywhere. In tests: `mail` in 48 files, `system` 35, `research_info` 31, `browser` 31, `news` 30. About 20 places in the source list every agent (`all_subagents`, `subagent_names`). A rename would break all of them, so old names must keep resolving.
- Routing scores agents by keywords (`planner.find_agents_for_query`). planner.py:444-470 records a live bug: an agent that unions other agents' tools always out-scores the narrow ones on generic verbs ("create a file called hello.txt" went to google_workspace because `drive_create_doc` contains "create"). google_workspace was taken out of automatic routing for that reason. A merged agent with five agents' tools would bring the same bug back.
- Tool scoping already accepts a set of agent names (`_scoped_tool_specs`, dispatch.py:1443).
- Per-agent models come from `DOURMOUSE_MODEL_<AGENT>` and built-in defaults keyed by old names (`config.model_for_agent`, config.py:387).
- Talking today: `ask_agent` and `list_agents` (peer_tools.py) call the `delegate_task` handler; the message bus is in memory (500 messages); `office_logger.py` already stores bus messages, delegate_parallel branch events, per-call agent events and a cursor-read event log in SQLite.
- Jobs: `goals.py` already stores a goal with status, `resource_limits`, tasks with `assigned_agent`, an event log, and pause, resume and cancel.
- T4's library plan (PL-10-T4) gives `add(source, text, meta)` with `meta.namespace` and `search(query, filters)` with a namespace filter, plus agent tools `library_search`, `library_add`. Agent memory will go through it.
- `scripts/e2e_peer_sweep.py:79` auto-approves owner prompts. The new test runner will never approve anything.

## The model: an agent is an identity; its old names are its skills

Each of the 14 is one identity: profile, persona, memory namespace, workspace folder, permissions, budget, tests and a seat in the Room. Each old agent becomes a **skill** of exactly one new agent. A skill keeps its name, its tools and its prompt unchanged.

One function resolves any name:
- a new agent id (`researcher`): that agent, with the skills that match the task (scored by the same planner), or all of them when none matches;
- an old name (`research_info`): its new agent, scoped to that one skill. This is how old routing, pinned chat screens, saved sessions and tests keep working;
- where a new id equals an old name (orchestrator, docs, browser, security, markets, media, agent_smith), the new agent wins. Example: `markets` now reaches quotes plus forex, Trading 212 and MT5. A superset, so nothing is lost.

Routing keeps scoring skills exactly as today, so the roll-up bug cannot return; the run then takes the identity of the skill's agent. What the owner sees everywhere (roster, `list_agents`, the orchestrator's roster text, Office, Command Center, Room) is the 14. Old names show only as skills on a profile.

Each run carries `agent_id` (the identity) next to the skill it was scoped to. Events, bus posts, Room messages, memory and the workspace use `agent_id`.

google_workspace keeps its old 22-tool scope when someone names it (old behaviour), running as Mail and Calendar. When Mail and Calendar runs under its own id it gets mail, comms and scheduling only; documents go to Docs.

## Step 1. The agent profile, stored and served

- **Fields:** id, display name, job (one line), contract (what it promises, 3 to 5 lines), skills, tools (derived from skills), model, memory namespace (`agent/<id>`), workspace folder (`<workspace>/agents/<id>/`, created on first use), permissions, step budget, capability test ids, and availability (computed from test results).
- **Model:** `DOURMOUSE_MODEL_<ID>` first, then the old skill's override, then today's default. Cloud models only (model policy).
- **Permissions summary:** which tools ask the owner first, whether the agent reads text written by other people (today's `UNTRUSTED_CONTENT_AGENTS`), whether it can act on the Mac, whether its data leaves the Mac, and who it may ask without the owner (today's `PEER_ASK_OPEN_TARGETS`). Enforcement stays where it is today: per tool and per skill. The profile shows it in one place.
- **Memory:** no new store (shared foundation 2). Agent memory goes through T4's library with `namespace = agent/<id>`, filled in from the run, never from the model's arguments. If T4's stage 1 has not merged when I reach this, the field ships and the wiring waits for T4. I will not build a third store.
- **Storage:** profiles live in code (`dourmouse/agent_profiles.py`, one table, the way `os_api/office.py` keeps floors today), reviewed and tested. Test results live in SQLite (step 5).
- **API:** `GET /api/agents` (the 14 with pass rates and availability) and `GET /api/agents/<id>` (full profile), as a new `os_api` route module.
- **Tests:** every old name maps to exactly one agent; no tool is lost (the union of skills equals today's registry); the resolve table for all 44 old names, the 14 ids and the 7 collisions; the API shape.

## Step 2. Merge into the 13 (plus Agent Smith)

- Name resolution goes into the places that check names today: `delegate_task` (general_roster.py:2867 and :3237), `ask_agent` (peer_tools.py:121), `send_message` and `read_agent_inbox` (general_roster.py:1974-2049), pinned chats (webui.py:2896 and :5896), planner output, live poller bus posts, `model_for_agent`.
- Public views switch to the 14: `build_roster_payload` (webui.py:1698) gains `agents` while keeping `subagents` for old callers; `list_agents` prints the 14 with their skills; `describe_roster` lists the 14; Office floors (`os_api/office.py`) regroup by agent; the link topology follows.
- Persona: each agent gets a short identity paragraph in its run's system prompt (name, job, contract, workspace, namespace). The companion stays the Orchestrator's friendly voice.
- Coder: the five backends stay as tools; the profile shows which ones work right now (the existing per-backend status). The Claude CLI is signed out on this Mac (`claude auth login` needed), so that backend will test as unavailable until the owner signs in.
- **Tests:** the existing suite passes unchanged except tests that assert the public roster has 44 entries (Office floors, roster payload, `list_agents`). Those are re-specified to the 14, never relaxed, and each one is named in the finding.

## Step 3. Jobs (shared foundation 1)

- A job is a goal in `goals.db` with `kind = 'job'`. Added: the owner's question, a budget (steps and seconds; tokens counted when the backend reports them), spend so far, a board, and its Room thread.
- **Board:** a `job_board` table (note, file, link, finding; who posted it; when) and files in `<workspace>/jobs/<id>/`. Agents inside a job get `board_post` and `board_read`; the job id travels with the run, never from model arguments. The browser posts what it found; the researcher reads it from the board instead of receiving long text through prompts.
- **Log:** `goal_events`, already there.
- **Budget:** checked by the runner before every step. When spent, the job stops with status "budget spent" and an alert in its thread. It never just keeps going.
- **API:** `GET` and `POST /api/jobs`, `GET /api/jobs/<id>` (board, log, budget). Pause, resume and cancel reuse the goal calls.
- **Who creates jobs:** the orchestrator, through a `create_job` tool, when a request needs more than one agent or more than a few steps. The other threads (research runs, security scans, browser tasks) through the same API.

## Step 4. The Agent Room

- **Storage:** two new tables in the existing `office.db` (`office_logger.py`), not a new database. `room_threads` holds id, job, chat run, title, status, paused, taken over and the agent streak. `room_messages` holds thread, seq, kind, from, to, text, reply_to, call id, tokens, cost, board links, content hash and time. Each thread has its own gap-free `seq`, so a reader asks for "everything after 41".
- **Message kinds:** ask, answer, hand-off, note, alert, plus owner.
- **Writers** (no new engine; each hooks a path that already exists):
  - `ask_agent` posts the ask and the answer;
  - `delegate_task` and `delegate_parallel` post the hand-off and the answer;
  - `send_message` posts a note;
  - budget, loop and security alerts post an alert;
  - the owner posts as owner.
- **Threads:** one per job. A chat turn where agents talk to each other gets a thread too, marked "chat", so no conversation between agents stays invisible.
- **Live:** each new message goes out on the existing `/api/events` stream as `room_message`.
- **Owner controls:**
  - Post: every agent in that thread sees unread owner messages before its next step.
  - Pause: the job, and any chat turn in the thread, stops before its next model call.
  - Take over: pause, and agents may only answer the owner until resumed.
  - Resume.
- **Safety (borrowed from Pancakes):**
  - Loop guard: 12 agent messages in a row with no owner message pauses the thread with an alert.
  - Duplicates are refused.
  - Text that looks like a key or token is refused.
  - A peer's message is always handed to the next agent as untrusted data.
- **API:** `GET /api/room/threads`, `GET /api/room/threads/<id>?after_seq=`, `POST /api/room/threads/<id>/messages`, `POST /api/room/threads/<id>/pause`, `/resume`, `/takeover`.
- **Screen:** a new ROOM screen in the Agents sidebar group (`ui/assets/os/core/registry.js`). Plain: threads on the left; messages on the right with sender, kind, cost and board links; a post box; Pause and Take over buttons. No gradients, no glow, sentence case. The hardcoded screen count test (`test_os_shell_route.py`, see ledger P-3) is updated with a comment.
- **Acceptance (PL-10):** a three-agent job (News and World, Markets, Mac Operator) shown end to end in the Room, with screenshots from an isolated instance.

## Pancakes (open question)

Pancakes (~/Documents/Claude Projects/pancakes) plans a hosted Supabase MCP server (Mumbai region) where agents from any vendor join rooms by invite code. Phase 0 (schema) is done; the agent server is not built yet.

- **Build on it now:** the Room would wait for Pancakes Phase 1, and every agent message would go to Supabase. Those messages carry the owner's mail text, file contents and security findings, so Dourmouse's privacy gate would have to cover them first.
- **Stay separate:** simplest, but two room designs drift apart.
- **Design to join later (my recommendation):** the Room stays local in SQLite and uses the same message shape as Pancakes (per-room seq, reply_to, mentions, client message id, agent streak, content hash) and the same loop guard. A later bridge can mirror a chosen thread into a Pancakes room, through the privacy gate and only with the owner's consent per thread.

## Step 5. Capability tests (5 to 10 real tasks per agent, run live)

- **Definitions:** `dourmouse/agent_tests/` per agent. Each test has a prompt, the agent or skill it pins, and deterministic checks: the tool that must be called, the facts the answer must contain, tools that must not be called, and whether an owner prompt must appear. No model grades another model.
- **Runner:** `scripts/agent_capability_run.py` drives an isolated server through `POST /api/chat`, like the chat box, and reads the event stream. It never approves anything. When an owner prompt appears, the runner records it and answers no.
- **Real accounts are read-only:** inbox, calendar, now playing, broker summary. A task that would send or buy only checks that the owner prompt appears, then denies it.
- **Results:** a `capability_runs` table in `office.db`, plus a copy in `docs/agent_capability_results.json` with date, model and per-test outcome and error. A fresh install then shows the last recorded rate honestly, with its date.
- **Shown:** each profile shows its pass rate. An agent with no passing test is shown as unavailable, and automatic routing skips it. The owner can still name it directly.
- **Cost:** about 100 runs on the fleet model (Ollama Cloud `gpt-oss:120b`). A failure is recorded with its error, never skipped. Some agents may honestly show unavailable on day one: Media's image generation (Gemini image quota is 0 on this account), Markets when Yahoo rate-limits (MKT-1), and Coder's Claude backend while signed out.
- **First drafts** (three per agent; the full 5 to 10 come with step 5):
  - Orchestrator: "What is on my calendar today and what are the top 3 headlines?" (two hand-offs, both answers used); "Compare Apple and Microsoft's moves today and any news that explains them" (creates a job); "Who here can check my firewall?" (answers from `list_agents`, no hand-off).
  - Researcher: "When did the Peace of Westphalia end the Thirty Years' War? Cite the page." (1648 plus a source URL); "Boiling point of water at 2,000 m, two sources"; "List my study files about biology" (study skill).
  - Librarian: remember a fact, then recall it; find the PDF files in Downloads (read-only); device wiki status.
  - Mail and Calendar: unread count and newest sender; tomorrow's events; draft a reply without sending (`gmail_send` must not run).
  - Docs: read cell B2 of Google's public sample sheet; search Drive for "report"; read a public Doc.
  - Browser: open example.com and report its heading ("Example Domain"); find the RFC number on IANA's example-domains page; screenshot a page.
  - Mac Operator: free disk space (checked against `df`); running apps; the 30th Fibonacci number in the compute sandbox (832040).
  - Coder: write and run a string-reverse function in the isolated workspace; review `peer_tools.py` and name one risk (reviewer mode, no write tool called); report which backends work.
  - Security: is the firewall on (checked against `socketfilterfw --getglobalstate`); listening services; incidents list.
  - Markets: Apple's price now; top 5 gainers; Trading 212 account summary (read-only). A paper order must raise the owner prompt, which is denied.
  - News and World: top 3 headlines; news search on a named topic; world pulse summary.
  - Media: now playing on Spotify; a UI component spec for a toggle; generate an image (expected to fail honestly while the Gemini quota is 0).
  - Planner: add a task and list tasks (isolated workspace); create a goal; list goals.
  - Agent Smith: draft a Celsius to Fahrenheit tool (nothing gets registered); list drafts.

## Step 6. CC-1: Command Center lanes only from real events

- **Root cause:**
  - `live_runtime.py:41-57` schedules model-free polls for news, markets, rnd, mail and tasks.
  - Each poll sends a `live` event to the ActivityTracker (live_runtime.py:167), which sets that agent's status to `live` for good (webui.py:933-946).
  - Command Center's `seedLanes` (`ui/assets/os/screens/command_center/index.js`, around line 141) opens a lane for every agent whose status is not idle.
  - So the poll loops appear as running agents with "no call id". The polls are real (the ledger's "none of those tools ran" is wrong and gets corrected), but they are not agent runs: no model, no reasoning.
- **Fix:**
  - Lanes come only from `agent_trace` events that carry a call id, and from the snapshot only for agents in a computing or auth state with a real call id (`concurrent_call_ids`, finding #073).
  - Feed polls get one plain line: "Feeds: news polled 2 min ago, no model".
- **Tests and evidence:** a node screen test, a Python test, and a screenshot of a fresh isolated workspace showing no lanes until a real run starts.

## How each step lands

- One commit per step. Before each: the full suite green (`.venv/bin/python -m pytest dourmouse/tests -q`, only when `pgrep -f "pytest dourmouse/tests"` shows no other thread running it) and `scripts/lint_ratchet.py`, with counts in the message.
- Each step gets a numbered finding in `docs/ENGINEERING_AUDIT.md` and `docs/GODSPEED_ROADMAP.md` (next free number at the time; #192 is the highest today), a ledger row in `FIX_LEDGER_2026-10-06.md`, and evidence in `EVIDENCE/`.
- UI checks run on an isolated instance (own ports and workspace, headless Chrome screenshots), never the owner's app. Drivers never approve anything.
- Merge into `recon-2026-09-11` only after rebasing onto its latest commit and a full suite. T2 also edits `webui.py`, so conflicts there are expected and are resolved by hand.
- No push without the owner's yes.

## Risks and limits, stated plainly

- Asking a merged agent by its new id can hand it a large tool list (Mac Operator has 47). The run is focused on the skills that match the task, and the capability tests measure whether that works.
- Live tests depend on outside services. A failure is shown as a failure, so some agents may be unavailable until a key, a quota or a sign-in is fixed.
- "Own memory" depends on T4's library landing. Until then the profile names the namespace, and nothing claims the memory is separate.
- Running Browser and Mac Operator in their own process is a later step (PL-10 T3 item 4) and is not in this plan.

## What I need from the owner

1. Yes to this plan, starting with step 1. Or say what to change.
2. Pancakes: build on it, stay separate, or design to join later.
