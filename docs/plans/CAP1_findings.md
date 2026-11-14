# CAP1 findings: capability and wiring read of the Dourmouse agent layer

Scope: /Users/aditagrawal/dourmouse-recon/dourmouse. Read-only. No server started, no network.
Method note (honest): general_roster.py (7,241 lines) was covered by (a) reading the registry builder
(3537-4000, 6800-7241) and the handlers I cite, and (b) an AST pass that extracted every ToolSpec name and
description and every subagent tool list; agent_prompts.py by a script (tool names mentioned vs real tools,
section headers) plus targeted greps; planner.py, model_context.py, voice_commands.py, mcp_bridge.py,
goal_tools.py, research_agent.py read in full. Routing results below come from running the REAL
planner.find_agents_for_query against a stand-in registry built from the real descriptions (neural net off, local
router off). Agents whose tools are built dynamically (forex, t212, mt5, freebuff, worldmonitor, globe) had
empty or partial tool text in that stand-in, so their scores are understated. The Claude CLI route reaches every
tool through the MCP bridge and is NOT affected by the scoping findings; the cloud and Ollama routes are.

### CAP1-1 [high value] Plain factual questions match no agent, so web_search is never offered and RESEARCH FIRST cannot be obeyed
- where: dispatch.py:5115-5160 (no plan_agents -> `scoped_tools = []`), dispatch.py:4888 `_needs_evidence` (only stops the fast lane), planner.py:290 (URLs stripped before scoring)
- problem or gap: Base rule 5 says check every real-world fact with web_search/fetch_url. But tools are scoped to agents scoring >= 3, and nothing attaches research_info when the question needs evidence. `_needs_evidence` is used in exactly one place (`_is_pure_chat`) and only decides "do not use the fast lane"; the turn then runs the loop with zero tool schemas. Pasting a URL makes it worse: planner.py:290 deletes every `\S*/\S+` token, so "summarize this article https://..." scores nothing and fetch_url is not offered.
- idea: when `plan_agents` is empty after the scoring block and `_needs_evidence(last_user)` is true, or the prompt contains an http(s) URL, set `plan_agents = {"research_info"}` (add "news" when a current-event cue is present). Keep the single-agent bespoke prompt that this unlocks.
- build: ~8 lines in dispatch.py after line 5153, one URL regex; reuses `_needs_evidence`, `_scoped_tool_specs`, AGENT_SYSTEM_PROMPTS["research_info"]. Add one test per example below.
- evidence: stand-in run: "what is the population of Norway?" => []; "is it true that coffee stunts growth?" => []; "how tall is Mount Everest?" => admin_ops 1; "who won the world cup in 2022?" => worldmonitor 1; "summarize this article https://example.com/a" => browser 1. Code: `plan_agents = {m["name"] for m in matches if m["score"] >= 3}`

### CAP1-2 [high value] Substring matching against huge haystacks sends chit-chat to security/system/browser (the 'free' vs 'freebuff' shape, still alive)
- where: planner.py:474 `hay_hits = sum(1 for t in tokens if t in haystack)`; stop words planner.py:28
- problem or gap: The 'free' fix changed only the NAME check to a word match. The description/tool-text check is still raw substring over a haystack that, for browser (21 tools), security (28) and system (23), is thousands of characters. Any 2-3 letter or common word ("was", "we", "be", "by", "so", "go", "way", "best", "far", "many") hits. Stop words are only 30 words. Result: ordinary sentences clear the >= 3 threshold for the biggest agents, which then get their whole tool schema attached (including security_kill_process, lockdown_start, browser_submit), their 10-17k-character bespoke prompt injected (single-agent case), and, for browser, the "heavy workflow" Claude escalation (dispatch.py `_is_heavy_workflow_agent`).
- idea: build `hay_words = set(re.findall(r"[a-z0-9]+", haystack))` once per agent and test `t in hay_words`; drop tokens shorter than 3 characters unless in `_DOMAIN_WORDS`; add be/by/we/was/were/so/go/way/best/far/many/about/from/have/has/had/will/would/could/should/there/their/they/them/been/being/just/also/very/more/most/some/any/all to the stop list.
- build: ~10 lines in planner.py; no new pieces. Re-run test_map and the routing tests; expect a few accidental-match tests (like the 'search' one already documented) to need the same explicit compound rule.
- evidence: stand-in run: "i was thinking we could be ready by monday" => security 6, system 6, browser 5; "what is the best way to go about it" => system 4, browser 3; "can you tell me a bit about my day so far" (browser haystack alone) hits ['so','far'].

### CAP1-3 [high value] No stemming or spelling variants: the right agent scores 1-2 and gets no tools
- where: planner.py:81 `_OUTCOME_RE`, planner.py:112 `_VERB_CAPABILITY`, planner.py:290 tokenizer
- problem or gap: Tokens are matched literally. "organize" never matches "organization" (admin_ops), "download/downloaded/downloads" differ from each other and from security_downloads, "messaging" is not "message" (messenger), "summarise" is not "summarize" (the multi-step detector and `_VERB_CAPABILITY` only know the American spelling), and "to-do" tokenizes to "to"+"do" (both stop words) so the "todo" domain word never fires.
- idea: add a 12-line `_stem()` (strip -ing/-ed/-es/-s/-ize/-ise/-ization) applied to both query tokens and haystack words; map "summarise"->"summarize"; pre-normalize "to-do"/"to do" -> "todo" before tokenizing; add "add", "book", "save", "email", "schedule", "remind" to the outcome verbs so multi-step prompts using them are planned.
- build: ~25 lines in planner.py.
- evidence: stand-in run: "organize my downloads folder" => admin_ops 2, docs 2, security 2 (all below threshold, zero tools); "what are my to-do items for monday" => tasks 1; "who has been messaging me" => no messenger; "what did I download yesterday" => security 2.

### CAP1-4 [high value] Domain words that hijack unrelated requests: quote, price, drive, schedule, sheet, stock
- where: planner.py:140 `"quote": "markets"` (also "price", "prices", "quotes"), planner.py:176 `"drive": "docs"`, planner.py:152 `"schedule": "scheduling"`, "sheet"/"sheets" -> docs, "stock"/"stocks" -> markets
- problem or gap: Each is a flat +4 to one agent regardless of context. "quote the part about X" (documents) and "get a quote for a plumber" go to markets; "price of a flight" goes to markets; "hard drive / external drive space" goes to docs (Google Drive) and the system agent that owns system_info is not even in the top 3 (the 'disk' fix covers only the word disk); "schedule this to run every morning" goes to the Google Calendar agent although schedule_recurring lives on tasks; "sheet music" goes to docs.
- idea: make these compound like compound_free_when: "quote/price" -> markets only with a market cue (stock, share, ticker, crypto, coin, etf, symbol, etc. or an upper-case 1-5 letter symbol); "drive" -> docs unless hard/external/usb/disk/space/gb/storage/free/ssd present (then system); "schedule/schedule this" with every/daily/weekly/hourly/recurring/repeat/morning/nightly -> tasks (and a new "recurring"/"repeat" domain word -> tasks); "sheet" -> docs unless music/cheat/bed/answer.
- build: ~20 lines in planner.py following the existing `compound_*` pattern.
- evidence: stand-in run: "how do I get a quote for a plumber" => markets 5; "what is the price of a flight to tokyo" => markets 5; "how much space is left on my hard drive" => docs 5, app_driver 3, apps 3 (no system); "schedule this to run every morning at 8" => scheduling 5, dev_coding 4, system 4 (no tasks); "delete all the sheet music" => docs 6, music 5.

### CAP1-5 [high value] 'Find the PDF I downloaded last week and quote the part about X' is broken at four separate points
- where: librarian.py:242 `find` (has mtime, no date arg) and librarian.py:498 `librarian_find` (on admin_ops only); system_access.py:1148 `extract_pdf` (on system only, no page or search arguments); extract.py:33 `extract_pdf_text` (returns every page); planner.py routing
- problem or gap: (1) Routing: no word "pdf" or "download" routes anywhere useful ("quote" goes to markets, see CAP1-4). (2) The file index knows the modified time of every file but `librarian_find` accepts only query, kind, limit, so "last week" cannot be expressed. (3) Finding (admin_ops) and reading (system) are on different agents, and the planner only attaches an agent whose words matched. (4) `extract_pdf` returns the whole document; a 200-page PDF blows past the tool-result cap and the model never sees page 140. There is no "find passage" step.
- idea: add `since_days` and `folder` args to librarian_find; add `query` and `pages` args to extract_pdf (return up to N matching passages with page numbers, reuse the same extractor); `extend_subagent("system", librarian_find)` and `extend_subagent("admin_ops", extract_pdf)` (the same pattern the file already uses for drive_search); domain words "pdf" -> system, "download/downloaded/downloads" -> admin_ops.
- build: ~60 lines total: SQL `AND mtime > ?` in librarian.find, a pypdf page loop plus case-insensitive window match in extract.py, two extend_subagent lines, two domain words.
- evidence: stand-in run: "find that PDF I downloaded last week and quote the part about tariffs" => markets 6, system 6, mail 3 (no admin_ops, no librarian). "read the pdf in my downloads and summarize it" => security 4, admin_ops 3, apps 3 (no system, so extract_pdf is never offered).

### CAP1-6 [high value] 'Every morning tell me what changed in my projects' has no path at all
- where: general_roster.py:2653 `_schedule_recurring_tool`; schedules.py:455 `_run_one`; planner routing
- problem or gap: schedule_recurring can only repeat ONE regular-permission tool with fixed arguments, the runner calls the handler with no model, and the raw tool text goes nowhere a person reads. A prompt ("tell me what changed") cannot be scheduled, and the tool description never says that create_goal (a regular tool that does start agent work) can be the scheduled tool. Routing is also empty: no word in the sentence points at tasks, goals or freebuff; browser wins by substring noise.
- idea: a `schedule_prompt(prompt, schedule_text, agent?)` tool: stores `{tool: "create_goal", arguments: {objective: prompt, tasks:[{description: prompt, assigned_agent: agent}]}}` through the existing store, and have the goal completion post an alert (state_store alert kinds already include "agent"). Short term, add one sentence plus an example to schedule_recurring's description: scheduling tool="create_goal" gives a recurring agent run.
- build: ~80 lines; reuses schedules.Schedules, goal_tools._create_goal, goal_runtime, state_store.add_alert, run_command (git log --since), freebuff_* read tools for "projects".
- evidence: stand-in run: "every morning tell me what changed in my projects" => browser 3, app_driver 2, worldmonitor 2. Code: `text = _execute_tool(spec, args, lambda _prompt: False, ...)` (no agent run).

### CAP1-7 [medium value] The goals agent is nearly unreachable and its main tool needs a hand-built JSON graph with no example
- where: goal_tools.py:215 create_goal description and `_TASK_ITEM_SCHEMA`; planner.py `_DOMAIN_ROUTE` (no goal word); goals.py:288/305 (pause_goal, resume_goal) used only by os_api/goals.py:121/132
- problem or gap: "goals" has no domain word, no bespoke prompt, and the name stem is "goals" so the singular "goal" does not even earn the +3 name hit. A weak model must produce `tasks:[{description, depends_on:["#0"], assigned_agent}]` from a description that names the shape but gives no example. The store can pause and resume goals but there is no chat tool, so the owner can only do it from the OS screen.
- idea: domain words goal, goals, "in the background", "keep working", "monitor" -> goals; stem the name; put one worked example in create_goal's description; add `pause_goal` and `resume_goal` ToolSpecs (3-line wrappers).
- build: ~40 lines in goal_tools.py plus 4 domain words.
- evidence: stand-in run: "set a goal to monitor competitor pricing in the background" => goals 3 (exactly at threshold, music 2); "check on my background goal" => goals 3 tied with research_mesh 3; "create a recurring reminder every friday" => tasks 4, goals 3.
PROGRESS: done planner.py

### CAP1-8 [high value] Bespoke agent prompts only apply when exactly ONE agent is selected, and 22 agents have none
- where: dispatch.py:5296 `if len(plan_agents) == 1:` ... `AGENT_SYSTEM_PROMPTS.get(next(iter(plan_agents)))`; agent_prompts.py:137
- problem or gap: The hard-won per-agent rules (mail confirmation steps, browser snapshot-before-act, music "never invent a URI") are dropped whenever two agents score >= 3, which is common (mail+scheduling, system+admin_ops, markets+news, and every false second match from CAP1-2). Agents with no bespoke prompt at all: agent_smith, app_driver, apps, code_claude, code_deepseek, device_wiki, evidence_pipeline, forex, freebuff, globe, goals, google_workspace, media, memory, mt5, panel_control, research_mesh, reviewer, security, study, t212, tasks. Those include the ones with the most dangerous tools (security: 28 tools incl. kill/quarantine/block; t212 and mt5: orders) and the most complicated ones (evidence_pipeline: a 6-step sequence; goals).
- idea: (a) when two agents match, splice the TOP agent's prompt plus a 6-line extract (the "TOOL USAGE" list) of the second; (b) write short prompts (15-25 lines each, the compute prompt at agent_prompts.py:4090 is the right size) for security, evidence_pipeline (the exact call order plan -> run_pipeline -> detect_contradictions -> synthesize -> critique_answer), goals, tasks, memory (remember vs recall vs search_vault vs write_note), study, t212, mt5.
- build: dispatch.py ~12 lines; prompts are text only. Existing test_agent_prompts.py already enforces that keys are real agents.
- evidence: `if len(plan_agents) == 1:` and the comment "No bespoke prompt for the agent, or more than one agent in play ... fall back to the existing generic roster prompt".

### CAP1-9 [medium value] Base prompt rule 10 sends the model to a deleted agent and omits half the roster
- where: dispatch.py:1518 (`ATLAS quant repo → atlas`), the rule-10 routing list dispatch.py:1480-1520
- problem or gap: atlas was unplugged from the roster (general_roster.py:4890 comment) but rule 10 still routes "ATLAS quant repo" to it, so a delegate_task(agent="atlas") hits "no such subagent". The rule-10 list also never names goals (background work), compute (Python jobs), media (generate_image), security, evidence_pipeline, study, device_wiki, panel_control or app_driver, so a weak model that does not scan the long roster below it has no cue for them. The comment says the roster lists everything, but the explicit list is what small models follow.
- idea: delete the atlas clause; add one line per missing agent in the same arrow style ("long-running background work -> goals", "simulations, number crunching -> compute", "make an image -> media", "is my Mac safe, downloads, lockdown -> security", "check a claim, hypotheses, experiments, citations -> evidence_pipeline", "my textbooks and notes -> study", "tidy / find my files -> admin_ops (librarian_find)").
- build: text only, ~10 lines.
- evidence: "ATLAS quant repo → atlas; Freebuff → freebuff; global intelligence → worldmonitor"

### CAP1-10 [high value] Mail prompt promises attachments that no tool can read or send
- where: agent_prompts.py:4109 mail prompt ("3. Read Gmail messages and relevant attachments", "6. Identify attachments if requested"); general_roster.py:6448 gmail_send schema (to, subject, body only); google_services.py:1761 `gmail_send`; gmail_read (general_roster.py:6386)
- problem or gap: Neither gmail_read nor gmail_send touch attachments (google_services has no attachment/filename handling beyond search syntax). The model is told it can, so it either claims it did or improvises. Also missing for real mail work: cc/bcc, reply-in-thread (gmail_send always starts a new thread, so "reply to this email" produces a detached message), and name-to-address lookup ("email Sam" from the voice command has no way to find Sam's address).
- idea: (1) `gmail_get_attachment(message_id, attachment_id|filename)` saving into the uploads folder, so it chains into extract_pdf / read_path; gmail_read lists attachment names; (2) `attachments:[paths]`, `cc`, and `reply_to_message_id` args on gmail_send (threadId and In-Reply-To are in the message the API already returns); (3) `find_contact(name)` = gmail_search `from:name OR to:name` reduced to distinct addresses.
- build: ~120 lines in google_services.py (Gmail API users.messages.attachments.get is a single GET) plus 3 ToolSpec edits.
- evidence: mail prompt: "6. Identify attachments if requested." vs `"properties": {"to", "subject", "body"}` in gmail_send.

### CAP1-11 [medium value] publish_artifact is attached to only 3 agents although its own docstring says "every agent that produces reports"
- where: general_roster.py:6906 `for _name in ("research_info", "dev_coding", "rnd"):`; dispatch.py:1245 docstring
- problem or gap: compute (whose out/metrics.json and series are exactly what the "series" artifact renders), markets, worldmonitor, evidence_pipeline, news, mail, t212, forex and mt5 cannot show a table or chart beside the chat, so the model dumps text. The compute prompt even tells the model to put results in out/ but has no way to chart them.
- idea: extend the tuple with compute, markets, worldmonitor, evidence_pipeline, news, forex, t212, mt5 (and mention publish_artifact in the compute prompt).
- build: 1 line plus 2 prompt lines.
- evidence: `for _name in ("research_info", "dev_coding", "rnd"):`

### CAP1-12 [medium value] google_workspace roll-up is missing sheets_append and is reachable only if the weak model names it
- where: general_roster.py:7176 `_google_workspace_tool_names`; planner.py:50 `_ROLLUP_AGENT_NAMES`; dispatch.py:1498-1506 rule 10
- problem or gap: The list has sheets_read, sheets_create, but not sheets_append (it lives on docs), so "add a row to my budget sheet" through the agent that claims to be "everything in Google Workspace" fails. The roll-up is excluded from automatic scoring and rule 10 says the model must pick `delegate_task(agent="google_workspace")` itself; weak models do not. The usual cross-surface sentence still works because mail and scheduling both score (stand-in: "summarise my unread mail and add the deadlines to my calendar" => mail 9, scheduling 6), but then no bespoke prompt is used (CAP1-8).
- idea: add "sheets_append" to the tuple (the build already raises if a name is missing, so it is safe); when the top two agents are both in the Google set, treat the pair as one group for prompt purposes.
- build: 1 line.
- evidence: `"sheets_read", "sheets_create", "slides_create",` (no sheets_append)

### CAP1-13 [medium value] Study agent: reachable only by the word 'study', searches file names only, and its read tool tells the model to use its own knowledge
- where: general_roster.py:3710 study_search_files ("(not contents)"), general_roster.py:3735 study_read_file ("PRIORITIZE YOUR OWN KNOWLEDGE"), planner.py (no study domain words)
- problem or gap: A folder of textbooks and past papers cannot be searched by what is inside; the model must guess file names. The agent has no web_search, so base rule 5 (research first) cannot be satisfied there, and its tool description tells the model the opposite of rule 5. No domain word routes "my notes", "textbook", "past paper", "homework", "revision", "MYP" to it.
- idea: `study_search_content(query)` that greps text files and runs the existing PDF extractor over PDFs with a small on-disk cache keyed by mtime (the librarian index already stores path and mtime); domain words for textbook/past paper/assessment/homework/revision/quiz; reword study_read_file: "Read the owner's own material. For general facts use web_search".
- build: ~70 lines; reuses extract.extract_pdf_text, librarian.configured_roots, _study_search_tool's walker.
- evidence: stand-in run: "quiz me on my economics notes" => study 2 (below threshold, zero tools); "what do my notes say about inflation" => memory 2, study 2.

### CAP1-14 [high value] Several agents have almost no routing words, so natural requests score 1-2 and never reach them
- where: planner.py:120-200 `_DOMAIN_ROUTE` (no entries for media, security beyond 4 words, compute, globe, panel_control, study, goals, app_driver, device_wiki, freebuff, t212, mt5)
- problem or gap: `_DOMAIN_ROUTE` covers about 12 agents. The rest depend on description overlap, which fails when the user's verb or noun is not in the description text. Measured: image generation by any word except "generate image", the security agent for "who is on my wifi", "which downloads are risky", compute for "monte carlo", panel_control for "arrange my windows". The neural router and the local router model are the only other chance, and the local router is opt-in (DOURMOUSE_AGENT_ROUTER_AUTO=1).
- idea: add domain words (all single-agent-unambiguous): media <- draw, picture, logo, illustration, image, photo, wallpaper; security <- wifi, malware, virus, firewall(already compound), router, devices, risky, suspicious, downloaded, quarantine, vpn, monitored; compute <- simulation, monte, carlo, numpy, pandas, regression, statistics; globe <- flight, vessel, ship, satellite; panel_control <- windows+arrange/tidy/organize, panels; t212/mt5/forex <- their literal brand words ("trading 212", "mt5", "metatrader", "ibkr"). Stem first (CAP1-3) so each entry covers its family.
- build: ~40 dictionary lines in planner.py; verify each with a one-line test using the existing routing test style.
- evidence: stand-in run: "draw a picture of a cat in a spacesuit" => admin_ops 1, apps 1, design_3d 1; "make a logo for my bakery" => docs 2; "who is on my wifi network" => security 1; "which of my downloads are risky" => security 1, admin_ops 1; "run a monte carlo with 5000 samples" => compute 2; "arrange my windows neatly" => panel_control 2.

### CAP1-15 [medium value] The "answer from memory" fast lane still exists for imperative questions (contradicts rule 5)
- where: dispatch.py:4647-4660 `_KNOWLEDGE_CUES` comment ("answer directly from its weights ... takes them EVEN when a research/info agent matches"), dispatch.py:4875-4905 `_QUESTION_OPENERS`/`_needs_evidence`, dispatch.py:4949-4953
- problem or gap: `_needs_evidence` only fires for text ending in "?" or starting with a question opener. "explain the new EU AI act", "describe how X works", "summarize the history of Y", "give me an overview of Z", "compare A and B" start with none of the openers, so they are classed pure chat and answered tool-less from memory by the compact fast-lane prompt (which says "Answer directly"). "explain" is even in `_KNOWLEDGE_CUES`, which is the opposite of rule 5.
- idea: add explain, describe, summarize, summarise, outline, overview, compare, list, give me, show me, walk me through, what do you know about to `_QUESTION_OPENERS`, and delete "explain" and "difference between" from `_KNOWLEDGE_CUES` or rely on `_STABLE_CHAT_CUES` only. Combined with CAP1-1 this gives these prompts research_info's tools.
- build: ~6 lines.
- evidence: `_KNOWLEDGE_CUES = ("what is", ... "meaning of", "definition", "difference between", "explain",`

### CAP1-16 [medium value] Claude/Codex route: the MCP bridge sends no instructions, and the session briefing hides the most important tool names
- where: mcp_bridge.py `_handle_initialize` (returns only protocolVersion, capabilities, serverInfo); model_context.py:55 `shown = ", ".join(tools[:10])` over `sorted(tools)`
- problem or gap: The Claude CLI gets a flat list of about 240 tools and the bespoke per-agent rules never reach it. The one-time preamble lists each agent's tools alphabetically and cuts at 10, which hides exactly the working tools: system hides read_path, run_command, system_info, write_path (13 hidden); browser hides browser_snapshot, browser_type, browser_submit, browser_press, browser_screenshot, open_browser_pane; security hides security_report, security_downloads, security_status, security_kill_process (18 hidden); mail hides gmail_untrash and read_inbox. The preamble then says "you do not need to search for your own tools again".
- idea: (1) order the preamble by importance: the first sentence of each description is already used for the agent; list ALL tool names if the agent has <= 30 (names are cheap, about 8 tokens each; the full list is roughly 2,000 tokens, which the preamble comment says is acceptable), or list one line of verbs per agent; (2) return an `instructions` string in the MCP initialize result built from the same preamble sections (clients that read it get it on every session without prepending to the prompt); (3) add `annotations: {readOnlyHint: true}` for tools that never write so clients can auto-allow them.
- build: model_context.py ~10 lines; mcp_bridge.py ~12 lines (reuse `claude_orchestrator_preamble()`); no new pieces.
- evidence: `shown = ", ".join(tools[:10])` ... `shown += f", +{len(tools) - 10} more"`

### CAP1-17 [medium value] No real daily briefing; rule 11 steers "summarize the day" to a tool that only counts bus messages
- where: general_roster.py:2283 `daily_digest` (description: "INTER-AGENT BUS ONLY ... a low count here means a quiet BUS"), dispatch.py rule 11 (`_SYSTEM_PROMPT` "run the honest daily self-review via the memory agent's daily_digest tool")
- problem or gap: A person asking for "my morning briefing", "what's on today", "what did I miss" gets either the bus review (not what they meant) or a fabricated recap. All the parts exist as separate tools on five agents and no recipe ties them together, and no single agent owns more than two of them.
- idea: a `daily_briefing` tool (regular permission, read-only) that calls the existing handlers directly: calendar_events(5), gmail_search("is:unread newer_than:1d", 5), list_tasks, news_headlines, market_movers or stock_quote for a watchlist, world_brief, list_goals(status active), security_status one-liner, and formats a sectioned text with "not configured" lines kept honest. Put it on memory (where rule 11 already sends "summarize the day") and on tasks, so a scheduled `schedule_recurring(tool="daily_briefing", schedule_text="daily at 8:00")` works with no model in the loop.
- build: ~70 lines, handlers imported from general_roster.py module scope (they are plain functions taking an arguments dict) and goal_tools.
- evidence: `name="daily_digest"` description: "This does NOT cover general single-agent tool use (most real work never touches the bus)"

### CAP1-18 [medium value] The calendar can be read only as "next N events" and never edited, though range support already exists in the Google layer
- where: general_roster.py:1330 `_list_calendar_events_tool` -> google_services.calendar_events(max_results) (no range); google_services.py:1814-1815 already builds `timeMin`/`timeMax` for free/busy; create_calendar_event (google_services.py:528) has no attendees, update or delete
- problem or gap: "what is on tomorrow" / "what is between Monday and Friday" requires pulling up to 25 upcoming events and counting dates; a person with a busy calendar loses anything past event 25. A wrongly created event (the model got the time wrong) cannot be fixed or removed by any tool, so the model creates a second one.
- idea: add `from_iso`, `to_iso` to list_calendar_events (reuse the params dict of the free/busy call); add `update_calendar_event(event_id, ...)` and `delete_calendar_event(event_id)` both REQUIRES_CONFIRMATION, and return the event id from list and create so they can be referenced; allow `attendees` with a separate confirmation line since it sends invitations.
- build: ~90 lines in google_services.py (three REST calls), three ToolSpec edits, add the new names to `_google_workspace_tool_names`.
- evidence: tool text: "There is no date filter: read the dates in the result yourself to answer 'what is on this week'"

### CAP1-19 [medium value] gmail_search teaches one operator; the unread and date operators the API supports are never mentioned
- where: general_roster.py:6356 gmail_search description; google_services.py:1369 (docstring: `from:x`, `newer_than:3d`, `label:y`, `has:attachment`)
- problem or gap: The description says only `gmail_search(query='from:alice budget')`. A weak model asked for "unread mail" has no `unread` argument and does not know `is:unread` works, so it lists the latest inbox and filters in its head, or fabricates. The max_results limit (20) is also not shown.
- idea: rewrite (see CAP1-20 item 15). Also add `unread_only` boolean that appends `is:unread`.
- build: description text; 3 lines of handler.
- evidence: `e.g. gmail_search(query='from:alice budget')`

### CAP1-20 [high value] The 15 worst tool descriptions, with one-line rewrites
- where: all in dourmouse/general_roster.py unless noted
- problem or gap: Terse, no argument example, no "use this instead of X" pointer where a look-alike exists, or stale wording.
- idea (current -> rewrite):
  1. activate_app (line ~3789) "Bring a running app to the foreground." -> "Bring a running Mac app to the front, e.g. activate_app(app_name='Safari'); take the exact name from list_running_apps. To click or type inside it use app_driver_snapshot; to show a web page use open_browser_pane."
  2. quit_app (~3801) "Quit a running app." -> "Quit a running Mac app, e.g. quit_app(app_name='Spotify'), exact name from list_running_apps, asks first. To stop a stuck process by pid use security_kill_process."
  3. add_task (~6643) "Add one task to the local task list." -> "Add one item to the owner's local to-do list, e.g. add_task(title='Call the bank'). Title only: no due date. For a timed repeat use schedule_recurring, for a calendar entry create_calendar_event, for long background work create_goal."
  4. complete_task (~6655) "Mark one task done by its id." -> "Mark a to-do item done, complete_task(task_id='...'); get the id from list_tasks. Not for goals (cancel_goal) or schedules (cancel_schedule)."
  5. search_vault (2231) "Search the Obsidian vault for notes containing a query (filesystem-backed until Phase 2 MCP fix)." -> "Search the owner's Obsidian note vault for a word, e.g. search_vault(query='tax'); then read_note on a hit. Facts the owner asked Dourmouse to remember are in recall, files anywhere on the Mac are in librarian_find."
  6. write_note (2260) "Write (create/overwrite) one note in the Obsidian vault..." -> "Create or overwrite one Markdown note in the Obsidian vault, write_note(path='Projects/x.md', content='...'); asks before overwriting. For a short fact to recall later use remember, not this."
  7. research_quote (5513) "Real quote for one symbol (Yahoo Finance, keyless)." -> "R&D-loop copy of stock_quote; for a user's request about a price use stock_quote on the markets agent. (Same treatment for research_news, research_movers, research_web_search, research_fetch_url, which are one-line near-duplicates of news_headlines, market_movers, web_search, fetch_url.)"
  8. research_web_search (5537) "Search the live web (keyless) for research — the roster's own R&D loop uses this to study new capabilities." -> "R&D-loop web search; for answering the user use web_search."
  9. browser_wait (6055) "Wait a fixed number of milliseconds for the page (animations, redirects, CAPTCHA-adjacent delays)." -> "Pause for ms milliseconds, e.g. browser_wait(ms=1500), while a page loads or redirects; then call browser_snapshot. It cannot wait for the owner to do something."
  10. compute_run_python (6288) -> append "Use it for jobs longer than a few seconds or needing numpy/pandas; for a quick snippet or a test use run_python, for a hypothesis test with recorded statistics use research_experiment."
  11. spotify_playback_state (5362) "Current Spotify playback state: device, shuffle, repeat, volume, and the selected track." -> "Device, shuffle, repeat and volume of the Spotify session. For just 'what song is this' use spotify_now_playing."
  12. security_status (615) "A real, current summary of this host's network and security posture..." -> "Quick network snapshot (interfaces, DNS, firewall, listening services). For 'is my Mac OK' use security_report; to run the detectors and record findings use security_sentry_scan; for 'am I being monitored' use security_monitoring_check."
  13. device_wiki_get (device_wiki_tools.py:149) "...by its exact real path." -> "Look up ONE file's saved summary by exact absolute path. There is no search: find the path first with librarian_find."
  14. daily_digest (2283) -> "Message-bus activity per agent only (who messaged whom). NOT a recap of the owner's day or of work done; for 'what happened in the world' use world_brief, for 'what is on today' use list_calendar_events and list_tasks."
  15. create_goal (goal_tools.py:215) -> append an example: "create_goal(objective='Weekly competitor price check', tasks=[{'description':'search prices for X','assigned_agent':'research_info'},{'description':'summarise and email','depends_on':['#0'],'assigned_agent':'mail'}])". Plus gmail_search (6356): "...operators work: is:unread, newer_than:7d, has:attachment, label:work, in:sent; max_results up to 20."
- build: text only, about 30 minutes; tool name lists in tests that pin description text may need updating.
- evidence: `description="Bring a running app to the foreground."` / `description="Add one task to the local task list."`
PROGRESS: done mcp_bridge.py model_context.py voice_commands.py goal_tools.py

### CAP1-21 [high value] The 12 most useful missing chains (task a person says in one sentence -> tools in order -> what is missing)
- where: no recipe exists in agent_prompts.py, the dispatch.py rules, or any tool description for any of these
- problem or gap / idea / build, one line each:
  1. "Find the PDF I downloaded last week and quote the part about X": librarian_find(kind=document, since_days=7) -> extract_pdf(path, query=X) -> answer with the page number. Missing: date arg, passage arg, cross-agent attach (CAP1-5).
  2. "Summarise my unread mail and add the deadlines to my calendar": gmail_search("is:unread newer_than:7d") -> gmail_read x N -> list_calendar_events -> create_calendar_event (gated) x M. Routing already selects mail+scheduling (mail 9, scheduling 6). Missing: a 6-line recipe in the mail and scheduling prompts (list proposed events first, skip ones already on the calendar, ISO times in the owner's timezone).
  3. "Summarise the PDF Priya emailed me": gmail_search(from:priya has:attachment) -> gmail_read -> gmail_get_attachment (new, CAP1-10) -> extract_pdf. Today it stops after gmail_read.
  4. "Every morning tell me what changed in my projects" or "my morning briefing": schedule_prompt (CAP1-6) or schedule_recurring(tool="daily_briefing") (CAP1-17), using run_command(git log --since=yesterday), the freebuff_* project reads and list_goals.
  5. "Email Sam that I'm late": gmail_search(from:sam) -> pick the address -> draft_message -> gmail_send (gated). The voice command "email <person> saying ..." forwards the NAME as chat text (ui/workspace.html:2015); nothing resolves it. Add find_contact (CAP1-10).
  6. "Research X properly with sources": research_plan -> research_run_pipeline -> research_detect_contradictions -> research_synthesize -> research_critique_answer -> publish_artifact. Six calls in a fixed order, no bespoke prompt for evidence_pipeline (CAP1-8), no one-call macro, and publish_artifact is not on that agent (CAP1-11).
  7. "Compare A and B and chart it": web_search x2 -> fetch_url -> run_python (build the series) -> publish_artifact(kind="series"). web_search is on research_info only, run_python on dev_coding only, publish_artifact on three agents; no single agent has all four and the planner attaches at most two agents. Fix by extending the artifact tuple (CAP1-11) and giving research_info run_python (it is sandboxed).
  8. "Tidy my Downloads safely": security_downloads (risk per file) -> librarian_proposals -> show the moves -> librarian_apply (gated), and security_quarantine_file for the risky ones. security and admin_ops never co-select (stand-in "which of my downloads are risky" => 1 point each). Add a 5-line recipe to the admin_ops prompt plus domain words (CAP1-14).
  9. "Prep me for my 3pm meeting": list_calendar_events -> gmail_search(attendee name) -> drive_search(title) -> drive_read -> drive_create_doc (gated). Three agents (scheduling, mail, docs) with no group; google_workspace is the right owner but is reached only by name (CAP1-12).
  10. "Tell me when BTC drops under 60k" or "watch this page for changes": schedule_recurring(tool="stock_quote", args) only stores a text result each time. There is no threshold, no "changed since last run", no notification. Add watch(tool, args, condition, schedule_text) calling state_store.add_alert (ALERT_KINDS already has "market" and "agent").
  11. "Add a row to my budget sheet": sheets_read -> sheets_append (gated). sheets_append is on docs only and missing from the google_workspace roll-up (CAP1-12); sheets_read has no range argument, so the model reads the whole tab to find the last row.
  12. "Remember this page": fetch_url -> summarise -> remember(title, body). Nothing teaches it; rule 12 forbids persisting unless asked, and without a recipe the model stores 8,000 characters of HTML-stripped text.
- build: chains 2, 8, 9, 11, 12 are prompt text only (~10 lines each in agent_prompts.py); 1, 3, 4, 5, 6, 10 need the small tools named in CAP1-5/6/10/17.
- evidence: stand-in run: `summarise my unread mail and add the deadlines to my calendar => [('mail', 9), ('scheduling', 6), ('docs', 2)]` (routing works, no recipe exists).

### CAP1-22 [medium value] Cheap capabilities whose building blocks already exist
- where: listed per item
- problem or gap / idea / build:
  1. daily_briefing: calendar_events, gmail_search, list_tasks, news_headlines, world_brief handlers in general_roster.py, ~70 lines (CAP1-17).
  2. watch/alert: schedules.Schedules + SchedulerRunner (schedules.py:338) + state_store.add_alert (ALERT_KINDS at state_store.py:69), ~80 lines (chain 10).
  3. extract_pdf(query, pages): extract.py:33 already loops pages, add a filter, ~25 lines.
  4. device_wiki_search(query): WikiStore.list_all (device_wiki/store.py:91) returns summaries; a case-insensitive filter, ~20 lines. The wiki is write-only for chat today: device_wiki_get needs the exact path.
  5. study_search_content: extract.extract_pdf_text plus the study walker plus an mtime cache, ~70 lines (CAP1-13).
  6. calendar range, update, delete: google_services.py:1814 already builds timeMin/timeMax, ~90 lines (CAP1-18).
  7. gmail reply-in-thread, attachments, cc: threadId and Message-ID are in the message gmail_read already fetches, ~120 lines (CAP1-10).
  8. research_answer macro over the six evidence_pipeline handlers (research_pipeline_tools.py), ~40 lines (chain 6).
  9. schedule_prompt: schedules + goal_tools._create_goal, ~50 lines (CAP1-6).
  10. pause_goal and resume_goal tools: goals.py:288/305, 3-line wrappers (CAP1-7).
  11. add_task due date and priority: `_add_task_tool` at general_roster.py:2625 writes tasks.json; ~15 lines. "Add the deadlines" currently has nowhere to put a date.
  12. Voice media commands: voice_commands.py grammar plus tools that need no model (spotify_playback_control, player_*, organize_panels). Add "pause", "next song", "volume 30", "organize panels" patterns and have webui.py /api/voice/command call the tool directly instead of a chat turn (instant, deterministic). The 4-command grammar covers only email, open, close, search, so "pause the music" costs a full model round trip.
  13. Raise the no-plan fallback from `limit=2` to 3 (dispatch.py:5149) for cross-surface requests, once CAP1-2 removes the noise matches.
- build: sizes above; each under about 120 lines.
- evidence: `def list_all(self, status: str | None = None)` (device_wiki/store.py:91) and no search method anywhere in device_wiki_tools.py.

### CAP1-23 [high value] Reminders cannot be done: no word routes them, no one-shot schedule, and no notify tool exists for a schedule to call
- where: schedules.py:81 `parse_schedule` (accepts only every N minutes/hours/days, every <weekday>, weekly, daily, "at HH:MM", all repeating); general_roster.py:2653 `_schedule_recurring_tool` (must name an existing REGULAR tool); tool list (no notify/alert tool)
- problem or gap: "remind me to call mom tomorrow at 5" is among the most common assistant requests. There is no "remind", "reminder" or "alarm" word in any routing table (stand-in: => app_driver 2, browser 2, companion 2; "remind me every monday to take out the trash" => browser 3, mail 3, security 3, a three-way tie of unrelated agents). Even with the right agent, parse_schedule has no one-off form ("tomorrow at 5", "in 20 minutes", "on Friday"), "at 5pm" repeats daily, and schedule_recurring needs a tool to run, but no tool shows the owner anything (state_store.add_alert is not exposed as a ToolSpec). add_task stores a title with no time. The model therefore says it will remind, which is the fabricated-success pattern the rules forbid.
- idea: (1) `notify_me(title, detail)` regular tool that calls state_store.add_alert(kind="agent"); (2) a `once` kind in parse_schedule ("in 20 minutes", "tomorrow at 17:00", "on 2026-11-03 at 9:00") that the runner disables after the first run (Schedules already has set_enabled); (3) `remind_me(text, when)` wrapping the two; (4) domain words remind, reminder, reminders, alarm, wake -> tasks; (5) confirmation text already exists on schedule_recurring, reuse it.
- build: ~90 lines (schedules.py parser + runner one-shot, one ToolSpec, three domain words); reuses Schedules, SchedulerRunner, state_store.add_alert.
- evidence: `raise ValueError("schedule not understood. Accepted shapes: 'every Monday at 9:00', 'daily at 8:30', 'every 30 minutes', 'weekly'.`

### CAP1-24 [medium value] Nothing can launch an app, and 'open Safari' never reaches the apps agent
- where: general_roster.py:3758 apps agent (activate_app needs an already-running app), open_path description ("It will not open a program, which would run it"), app_driver allow-list
- problem or gap: Every app tool begins "a running app". `list_running_apps` then `activate_app` fails when the app is closed, and open_path refuses programs. "Open Safari/Spotify/Notes" is a daily request. Routing also fails: stand-in "open Safari" => security 3, browser 2, docs 2 (no apps), and `quit Spotify` goes to music 5.
- idea: `launch_app(app_name)` running `open -a <name>` (REQUIRES_CONFIRMATION like activate_app, name checked against /Applications and /System/Applications so it cannot run an arbitrary path); domain words "launch", "open"+app-ish word is too broad, so use: launch, quit, "force quit", "switch to" -> apps.
- build: ~35 lines next to `_apps_activate_tool` (general_roster.py:1073), reuses `_app_control_dry_run`, `_DRY_RUN_PROP` and the confirm pattern.
- evidence: stand-in: `open Safari => [('security', 3), ('browser', 2), ('docs', 2)]`

### CAP1-25 [medium value] The read-only reviewer agent is never chosen; "review my code" goes to three LLM coding backends that ship the code off the Mac
- where: planner.py (no "review" domain word; agent name "reviewer" does not word-match "review", see CAP1-3); general_roster.py:4690 reviewer; code_* agents general_roster.py:4828-4866
- problem or gap: The reviewer exists so review is read-only and local-tooled, but no route reaches it. "review my code for bugs" ties code_claude, code_codex and code_deepseek at 4 each (alphabetical order picks code_claude first), each with one tool that hands the whole task to an external model. That is the opposite of the least-privilege design the reviewer was built for.
- idea: domain words review, reviewing, "code review", audit, "look over" -> reviewer (after stemming "review" matches the name); in the reviewer prompt add: "to fix what you find, delegate to dev_coding".
- build: 3 dictionary lines, 1 prompt line, plus a bespoke prompt for reviewer (it has none).
- evidence: stand-in: `review my code for bugs => [('code_claude', 4), ('code_codex', 4), ('code_deepseek', 4)]`

### CAP1-26 [low value] Dead or superseded code that hides a missing feature
- where: research_agent.py:126 `run_atlas_research` and RESEARCH_TOOL_SPEC; orchestrator.py:31-45 `_TOOLS`; general_roster.py:4897-4915, 5194-5215 (atlas, atlas_ui, atlas_cmd commented out); atlas/atlas_ui_ops.py `atlas_terminal_status`; _atlas_runner.py and setup_atlas_venv.py (no importer anywhere, including ui/ and scripts)
- problem or gap: `run_atlas_research` (genetic strategy research over symbols) is registered only in the legacy orchestrator.py tool table; orchestrator.dispatch() has no caller (eval_harness.py:138 and bench.py:185 import only `_build_client`), and the real roster has no atlas agent since the user-directed v14 unplug. So the capability exists but nothing can reach it. This is intentional, but three residues remain: dispatch.py rule 10 still routes to "atlas" (CAP1-9), `_HEAVY_WORKFLOW_AGENT_MARKERS` still lists "atlas_", and `atlas_terminal_status` is a defined ToolSpec attached nowhere. No tool is defined twice (checked every ToolSpec name across the repo: no duplicate); rnd's five `research_*` tools are deliberate near-duplicates of news/markets/research_info tools (CAP1-20 items 7-8).
- idea: decide: either delete orchestrator.py's loop and keep `_build_client`, or re-add one `atlas` agent that wraps only run_atlas_research behind a confirmation (it launches a long subprocess); remove the rule-10 clause either way.
- build: deletion ~0 lines of new code; re-adding ~15 lines (one `_subagent("atlas", ..., [ToolSpec(**RESEARCH_TOOL_SPEC...)])`).
- evidence: `_TOOLS: list[dict[str, Any]] = [RESEARCH_TOOL_SPEC]` in orchestrator.py with no caller of `dispatch(`.

### CAP1-27 [low value] Voice comment says email goes straight to the mail agent, but it goes to a chat panel; panel aliases omit real panels
- where: ui/workspace.html:2015-2019 (comment "Routed straight at the real mail agent (not the companion)" then `openPanel("chat")` + sendChatMessage); voice_commands.py:56-66 `_PANEL_ALIASES`
- problem or gap: The email voice command sends `email sam: <message>` as ordinary chat, so mail-routing is up to the planner (it works: "email" is a domain word) but the person is only a name (CAP1-10). The panel alias table has six panel ids while workspace_panel_ops has organize_panels and move/resize tools the voice grammar cannot reach.
- idea: see CAP1-22 item 12 and CAP1-10 find_contact; fix the comment or pass a forced agent.
- build: ~10 lines.
- evidence: `// Routed straight at the real mail agent (not the companion) ... const p = openPanel("chat");`
PROGRESS: done general_roster.py (registry, wiring, descriptions via AST), dispatch.py (routing/scoping/plan parts), research_agent.py, research_pipeline_tools.py, goals.py

TOP10:
1. CAP1-1 Factual questions and pasted URLs get zero tools, so RESEARCH FIRST cannot be followed (attach research_info when `_needs_evidence` or a URL is present; ~8 lines).
2. CAP1-2 planner.py:474 substring match on huge haystacks sends chit-chat to security/system/browser (word-boundary match plus longer stop list; ~10 lines).
3. CAP1-23 Reminders are impossible: no routing word, no one-shot schedule, no notify tool (~90 lines, all pieces exist).
4. CAP1-5 "Find the downloaded PDF and quote X" broken at routing, date filter, agent split and PDF passage search (~60 lines).
5. CAP1-4 Hijacking domain words: quote and price go to markets, drive to Google Docs, schedule to the calendar (compound gates, ~20 lines).
6. CAP1-3 No stemming or spelling variants: organize, downloaded, messaging, summarise, to-do all miss their agent (~25 lines).
7. CAP1-6 Scheduled prompts and morning briefings have no path (schedule_prompt plus daily_briefing, CAP1-17; ~120 lines).
8. CAP1-8 Bespoke agent prompts only apply to a single selected agent, and 22 agents including security, t212 and evidence_pipeline have none.
9. CAP1-14 / CAP1-25 Agents with almost no routing words (media, security, compute, globe, panel_control, reviewer) are effectively unreachable by natural phrasing; "review my code" picks three outbound LLM backends instead of the read-only reviewer.
10. CAP1-10 Mail prompt promises attachment reading and sending that no tool supports; no reply-in-thread, cc, or contact lookup.

SUMMARY:
The tools themselves are in good shape: no tool is defined twice, none is orphaned except the deliberately unplugged ATLAS one, every tool named in an agent prompt exists, and most descriptions already carry examples and "use X instead" hints. The weak layer is the deterministic router in planner.py plus the single-agent gate in dispatch.py. Running the real scorer against the real descriptions (neural off) shows that plain factual questions and pasted URLs receive no tools at all, that chit-chat lights up the biggest agents through substring matching, and that about a dozen everyday requests (reminders, open an app, review code, draw a picture, who is on my wifi, organise downloads, quote from a PDF) either miss their agent or are hijacked by an over-broad domain word. Fixing planner.py and one dispatch block (CAP1-1 to CAP1-4, about 80 lines) should lift capability more than any new tool. After that, the best value is a handful of small tools built from existing pieces: one-shot reminders with a notify tool, PDF passage search, calendar range and edit, mail attachments and reply, a daily briefing and scheduled agent prompts. Caveats: measurements used the deterministic scorer only (neural router and the opt-in local router model could rescue some cases, and the Claude CLI route sees every tool through MCP); tools built dynamically (forex, t212, mt5, freebuff, worldmonitor, globe) were scored with partial text; general_roster.py was covered by structured extraction plus reading its registry and wiring sections, not every handler line.
