# USE1 product-usefulness findings (Dourmouse OS shell, read-only)

### USE1-1 [high value] COMMS: opened message has no "summarize" / "draft a reply" / "make a task" action
- where: ui/assets/os/screens/comms/index.js:236-250 (paintDetail actions), :384 (sendCompose uses ctx.chat.send with focusAgent 'mail')
- today: an opened message offers only REPLY (empty body), FLAG, ARCHIVE, TRASH, CLOSE. The owner must retype context into the directive box to get help.
- idea: add SUMMARIZE, DRAFT REPLY (model writes the body into the compose form or hands to mail agent) and ADD TO GOALS buttons next to REPLY.
- build: reuse ctx.chat.send(directive, {focusAgent:'mail'}) exactly as sendCompose does; message id and subject are already in st.detail. GOALS add route is already used by goals/index.js. small.
- value: owner, every mail triage session (daily).

### USE1-2 [medium value] COMMS: inbox has no unread/starred filter chips and no paging past 25
- where: comms/index.js:33 (LIMIT = 25), :60-70 (search bar)
- today: only free-text Gmail search; list is fixed at 25 rows with no "load more"; the owner must know Gmail query words (the placeholder mentions them but there is no one-click).
- idea: chips UNREAD / STARRED / TODAY / FROM ME that call runSearch with is:unread, is:starred, newer_than:1d; a MORE button that raises limit by 25.
- build: load() already takes a query and builds the URL with limit; chips only set input.value and call runSearch. small.
- value: daily, every owner session on COMMS.

### USE1-3 [low value] SECURITY: LOCK button is disabled with no reason when the lockdown read fails
- where: security/index.js:238 (`${ld ? '' : 'disabled'}`)
- today: the button is greyed; the reason appears only as a small line (lockErr) and the spec text does not mention it.
- idea: show a RETRY next to "Lockdown state unavailable" which calls load('manual').
- build: load() already exists; small.
- value: rare, only when the lockdown endpoint is down.

### USE1-4 [medium value] SECURITY: findings cannot be dismissed, accepted or snoozed
- where: security/index.js:241-268 (paintFindings: only REMEDIATE -> HAND TO DOURMOUSE)
- today: a known-good device or an expected open port stays in the count and in the red "attention" tag on every scan; the only choice is to hand it to the model.
- idea: ACCEPT / IGNORE button per finding that remembers the fingerprint (new_device findings first), so the Mac tag can reach "clean".
- build: needs a small server route (a per-fingerprint ack in settings); ctx.api.post pattern exists. medium.
- value: owner, daily noise reduction; the tag state drives trust in the page.

### USE1-5 [medium value] SECURITY: SCAN NOW and the analyst are manual only; no "scan then explain" and no routine
- where: security/index.js:75-88, :365-378
- today: EXPLAIN MY FINDINGS asks a confirmation card every time and replaces the analysis; no history of past analyses or reports (report path only shown in a note that disappears).
- idea: show the saved report path as a copyable/openable link (reveal in Finder) and keep the note; offer a scheduled daily scan summary to HOME.
- build: r.saved_to is already returned; a reveal route likely exists in media/projects (open path); small.
- value: owner, weekly.

PROGRESS: done home
PROGRESS: done security
PROGRESS: done comms

### USE1-6 [high value] NEWS TO RESEARCH creates a record that RESEARCH cannot continue (dead end)
- where: news/helpers.js:879-882 (prompt says "you run the model stages from RESEARCH"); research/index.js:66-79 (only NEW QUESTION and EXPORT), :82-127 (newQuestion sends a fresh text)
- today: the card promises the owner will run model stages from RESEARCH, but RESEARCH has no "run the pipeline on this question" action. NEW QUESTION creates a different question; the one from NEWS just sits at 0 claims with only the empty-state "ask the research agent in the thread". The toast action also goes to #/research without selecting the new record.
- idea: a RUN PIPELINE / CONTINUE button on the selected question (sends "Continue research on question <id>" with focusAgent evidence_pipeline after a confirm), and make the NEWS toast deep link select the id (hash query).
- build: reuses ctx.chat.send(..., {focusAgent:'evidence_pipeline'}) and confirmHere already in the file; r from /api/os/news/research should include the question id (check os_api/news.py). small.
- value: owner, every time a headline is worth digging into.

### USE1-7 [high value] RESEARCH claims cannot be sent anywhere (WIKI, GOALS, HOME)
- where: research/index.js:251-265 (claim rows only expand a source), research/source-widget.js:550-577
- today: an opened claim shows passage and an OPEN link; no copy, no "save to WIKI", no "ask HOME about this", no "make a goal from this".
- idea: three small buttons in SourceWidget: COPY QUOTE WITH URL, SAVE TO WIKI, ASK ABOUT THIS (ctx.chat.send with the claim text, then #/home).
- build: copy via navigator.clipboard; ask via ctx.chat.send; wiki save via the existing wiki write route (see wiki/index.js). small each.
- value: owner, each research session; turns the pipeline output into durable notes.

### USE1-8 [medium value] NEWS has no filter, no search, no topic watch and no "ask about this"
- where: news/index.js:706-733 (rowFor), :735-764 (paintFeed), :811-821 (only TOP HEADLINES action)
- today: one flat 60-item list mixing news, disaster and conflict feeds; the only per-row actions are open and TO RESEARCH.
- idea: channel chips (news/disaster/conflict/important), a text filter over the already held ring, and an ASK button per row that sends "Summarize and say why it matters: <title> <link>" to the thread below.
- build: all client side over feed.newestFirst(); ASK reuses the thread view's ctx.chat.send. small.
- value: owner, daily.

### USE1-9 [low value] RESEARCH export is markdown only and only one question at a time
- where: research/index.js:130-148 (format=markdown hard coded)
- today: one file per question; no copy-to-clipboard, no "export all".
- idea: COPY MARKDOWN next to EXPORT using the same r.content.
- build: same route; small.
- value: owner, weekly.

PROGRESS: done research
PROGRESS: done news

### USE1-10 [high value] TIMETABLE: routines cannot be deleted, run on demand, or started from a template
- where: timetable/index.js:55-59 (only NEW ROUTINE), :170-181 (row has only EDIT and DISABLE/ENABLE), :137-144 (empty state)
- today: the server has POST /api/schedules/remove (request_guard.py:158, webui.py:5042) but no screen calls it, so a stale routine can only be disabled forever. There is no RUN NOW to test a routine, and a new routine is a blank textbox handed to HOME. The empty state says only "Describe what should happen".
- idea: (a) REMOVE after a confirm card; (b) empty-state quick starts such as "Every weekday 07:30 check mail for anything urgent", "Daily 08:00 scan this Mac", "Every morning top headlines" that pre-fill the hand-off textarea; (c) RUN NOW via the same hand-off.
- build: reuse confirmHere and ctx.api.post('/api/schedules/remove',{id}) (route exists); templates are strings feeding openHand() (st.hand). small.
- value: owner, setup once then maintain; templates turn a blank page into 3 clicks.

### USE1-11 [medium value] GOALS: finished goals show no outcome, and a failed goal or task has no retry or "run again"
- where: goals/index.js:486-493 (finishedRow shows objective, counts, AUDIT only), :475-484 (task row actions only for WAITING_FOR_APPROVAL)
- today: to learn what a completed goal produced the owner must open AUDIT and read the event log; FAILED/BLOCKED goals offer only CANCEL/AUDIT. Creating a similar goal means retyping the form.
- idea: show the last note/result line on the finished row; add RUN AGAIN (prefills the NEW GOAL form with the objective, steps and criteria) and "ASK HOME ABOUT THIS" (sends the objective plus blocked_reason).
- build: the form state st.form is plain data (objective, steps, criteria, priority) and the board already carries g.tasks[].description; RUN AGAIN is openForm() with st.form filled. small. Ask-HOME uses ctx.chat.send. small.
- value: owner, after every goal; repeat goals are the norm for an autonomous runtime.

### USE1-12 [medium value] GOALS: no way to edit or add a step to a running goal; no notification when a task waits for approval
- where: goals/index.js:451-473 (goalCard buttons: PAUSE/RESUME, AUDIT, CANCEL only)
- today: a task that needs approval is only visible when the owner has the GOALS screen open (poll 5s, goals/index.js:643); the sidebar and HOME attention list are separate. A wrong step means cancel and recreate.
- idea: raise the waiting task into the HOME attention list / a toast via the notif centre, with a button that opens #/goals.
- build: board is already read; ctx.notify exists; HOME attention reads /api/attention (server side would add the kind). medium.
- value: owner, whenever a goal is parked; otherwise the goal silently stalls.

### USE1-13 [medium value] WIKI: dead end when no folder is configured; the wiki cannot be searched by meaning or asked
- where: wiki/index.js:127-133 (unavailable state), :48-58 (SCAN disabled), :98 (OPEN shows stored summary only)
- today: with no roots the screen says set DOURMOUSE_WIKI_ROOTS and restart the server. That is a shell edit with no way forward inside the product. With files present, the only filter is by path substring; no content search, no ASK, no reveal-in-Finder.
- idea: a "choose folder" button (Electron has a folder picker used by PROJECTS/CODE?) storing the roots in settings; a SEARCH box that sends the query to HOME as "search the wiki for ...", and per file ASK HOME ABOUT THIS FILE and COPY PATH.
- build: ctx.chat.send exists; settings store routes in os_api/settings.py; desktop_rag.py / device_wiki tools exist server side. medium (picker) and small (ask/copy).
- value: owner, first run and daily lookup.

### USE1-14 [medium value] PROJECTS: a project card cannot open its folder, copy its path, or jump to CODE
- where: projects/index.js:578-597 (card actions OPEN-as-scope and STOP TRACKING only; path is plain text)
- today: OPEN sets chat scope but does not take the owner to HOME or to CODE; the card shows the folder path as unclickable text.
- idea: after OPEN, offer GO TO HOME (already shown in scope card at :575 but only for the active one); add COPY PATH and REVEAL IN FINDER and OPEN IN CODE.
- build: ctx.host.openExternal exists; code/projects route (os_api/code.py) lists the same folders; hash navigation. small.
- value: owner, each time a project is started (daily).

PROGRESS: done wiki
PROGRESS: done goals
PROGRESS: done timetable
PROGRESS: done projects

### USE1-15 [high value] ORCHESTRATION: an all-hands run can only be started by typing /all in HOME; the screen has no input
- where: orchestration/index.js:484-487 (empty state: "In HOME, type /all and a goal"), :438-442 (only REFRESH action); home/index.js:193 (hint)
- today: the screen that shows the runs has no way to start one; the owner must switch to HOME and know the /all syntax. The merged answer (run.synthesis, :508) has no COPY, no SAVE, no "make this a goal", no follow-up.
- idea: a NEW ALL HANDS action with a goal box (confirm card because it fans out to every model and spends credit) that calls ctx.chat.send('/all <goal>'); on each merged answer add COPY, ASK HOME TO ACT ON THIS, and ADD AS GOAL.
- build: ctx.chat.send is used the same way by wiki/timetable/security; confirmHere in kit; the GOAL create route /api/os/goals/create takes {objective, steps}. small to medium.
- value: owner, whenever a hard question needs several models; removes a context switch each time.

### USE1-16 [medium value] AGENTSMITH: an approved tool is "not live" until a server restart, and the screen offers no restart
- where: agentsmith/index.js:205-209 (warns "cannot be called until the server restarts"), helpers restartLine
- today: after APPROVE the owner is told to restart the server but must do it outside the product; the board keeps showing a warning banner (restartEl) until then.
- idea: a RESTART SERVER button (confirm card) beside the banner.
- build: /api/setup/restart exists in webui.py but no OS screen calls it (verified by grep of ui/assets/os). Needs a check that the Electron host supervises and relaunches the Python server; otherwise offer COPY RESTART COMMAND. small to medium.
- value: owner, once per approved tool; without it a finished feature feels broken.

### USE1-17 [medium value] AGENTSMITH: the draft box is a blank textarea and the reject reason is fixed
- where: agentsmith/index.js:83-111 (draft form), :354 (reason is the literal 'rejected from the AGENTSMITH screen')
- today: no examples of what a good capability gap sounds like; a rejected draft carries no reason that Agent Smith could learn from.
- idea: 3 example chips ("Read the title of the front Safari tab", ...) and an optional reason field in the reject card.
- build: the reject route already accepts a reason; chips only fill df.text. small.
- value: owner, occasional; improves draft quality.

### USE1-18 [medium value] COMMAND CENTER: lanes cannot be stopped, searched, copied or jumped to their transcript
- where: command_center/index.js:78-85 (lane markup is read only), :179-190 (only PAUSE), helpers.js:216 (LANE_CAP 8, older finished lanes dropped)
- today: the screen shows every agent thinking live but nothing can be done from it: no STOP on a runaway lane, no copy of the trace, no filter by agent or kind, and a finished lane vanishes after eight others.
- idea: per lane STOP (the stop route used by the composer), COPY TRACE, and OPEN TRANSCRIPT (the orchestration screen already reads /api/office_log?meeting=<id>); a text filter box.
- build: lane.call_id is stored; stop path is in core/chat.js; office_log read in orchestration/index.js:591. small each.
- value: owner, when something is slow or wrong; this is the one screen where the problem is visible.

### USE1-19 [medium value] APPS: an app can only be allowed while it is running, and the model's drive tools are not exposed
- where: apps/index.js:519-533 (allowApp takes rows from st.list.running only), :460 (empty state "until you allow one below")
- today: an app not currently open cannot be pre-allowed; the owner must launch it first. The server also has GET /api/os/apps/snapshot and POST /api/os/apps/act (no screen calls them) so there is no way to test that driving an allowed app really works.
- idea: an ADD BY NAME box; a TEST button per allowed app that calls snapshot (read only) and shows the first lines, with a one-line "ready" or the error.
- build: os_api/apps.py already has the routes; reuse states.populated. small to medium.
- value: owner, at setup and when an app misbehaves.

### USE1-20 [medium value] ATLAS: the server's overnight brief and world map exist but the screen shows only counts and 12 items
- where: atlas/index.js:808-953; unused routes GET /api/world/brief (webui.py:3100) and GET /api/worldmap, /api/world/correlations, /api/world/history
- today: ATLAS says what each feed returned and the pulse label; the deterministic "what happened" brief from world_brief.generate_brief is not shown anywhere in the OS shell. Items open in the system browser (atlas/index.js:943), unlike NEWS which opens the browser pane.
- idea: a BRIEF card at the top (text from /api/world/brief, no model) with COPY and ASK HOME TO EXPLAIN; per item TO RESEARCH and ASK buttons like NEWS; open links in the browser pane.
- build: ctx.api.get('/api/world/brief') plus the news toResearch flow (news/index.js:658) lifted into kit; pane open route used at news/index.js:647. small to medium.
- value: owner, daily morning check.

PROGRESS: done agentsmith
PROGRESS: done orchestration
PROGRESS: done command_center
PROGRESS: done apps
PROGRESS: done atlas

### USE1-21 [high value] CODE: the diff is read only with no "explain", "write the commit message", "undo this file" or copy
- where: code/index.js:136-195 (fileCard: only SHOW DIFF, EXPAND), :212-222 (gate note says undoing "is a git action you take yourself"), :359-364 (thread under the diffs)
- today: the owner sees what the model changed, then must leave the screen or type a long request into the thread to act on it. There is no copy of a diff, no commit helper, no revert.
- idea: per file ASK ABOUT THIS DIFF (sends "Explain the change to <path> in project <name>" into the CODE thread), COPY DIFF, and per tree WRITE COMMIT MESSAGE and UNDO THIS FILE as thread directives (the model's edit tools are already gated by edit_gate).
- build: ctx.chat.send into the thread this screen already mounts (mountThreadView at :359); diff text is in st.open. small each.
- value: owner, every coding session; closes the review loop on the one screen that shows the change.

### USE1-22 [medium value] VOICE: an unrecognised phrase is never sent on, so a spoken question needs a second click
- where: voice/helpers.js:992-1000 (unknown -> "Nothing was sent"), voice/index.js:737-743 (SEND TO HOME per row)
- today: the parser knows a handful of commands (open panel, search, email). Anything else, which is most natural speech, stops at the log and waits for a manual SEND TO HOME. A hands-free assistant that needs a mouse click per sentence is a dead end for the use it is named after.
- idea: a remembered toggle "Send everything I say to HOME" (default off, keeps the credit-safe behaviour of F7); when on, unknown phrases call sendToHome automatically.
- build: sendToHome(r) already exists (:750); the toggle uses ctx.prefs.set/get (core/prefs.js, used by CODE for 'project'). small.
- value: owner, every spoken interaction.

### USE1-23 [medium value] VOICE: the assistant never speaks back, although the server has text to speech
- where: voice/index.js:681 (shows "Server speech out" status only); unused route GET /api/speech (webui.py ~4331, returns audio/wav from local TTS) and /api/player/now
- today: replies appear as text on HOME; the VOICE screen reports tts status but nothing plays.
- idea: a READ ALOUD button on the latest HOME answer and a "speak replies to voice commands" toggle; STOP button.
- build: GET /api/speech?text= returns wav; new Audio(url). thread-view already renders turns, so one button per answer. small to medium.
- value: owner, hands-busy use (cooking, driving strip exists in chrome/driving-strip.js).

### USE1-24 [low value] VOICE: Whisper setup is a dead end inside the product
- where: voice/helpers.js:1034 ("set DOURMOUSE_VOICE=1 and install faster-whisper")
- today: when no engine works the screen tells the owner to set an env var and pip install; no copyable command, no check button after they do.
- idea: COPY COMMAND and RE-CHECK buttons; the load() function already re-reads status.
- build: navigator.clipboard plus load(). small.
- value: owner, once.

PROGRESS: done code
PROGRESS: done voice

### USE1-25 [high value] MEDIA PDF reader: the page is an image, so the owner cannot ask about it, search it, or export highlights
- where: media/pdf-view.js:1-12 (comment: "text cannot be selected"), :170-200 (highlights stored as rectangles with an optional typed note), media/index.js:193-199 (a thread is mounted under the player)
- today: the owner can mark rectangles but nothing can be done with the content: no "summarize this page / document", no find-in-document, no copy, no export of the highlight notes. The thread under the player has no link to the open file. The server already has text routes: /api/pdf/text, /api/pdf/info, /api/files/text-view (webui.py, none called from ui/assets/os).
- idea: ASK ABOUT THIS PAGE and SUMMARIZE DOCUMENT buttons in the PDF bar that send a prepared directive naming the open file (the model has file tools), plus COPY PAGE TEXT (via /api/pdf/text) and EXPORT HIGHLIGHTS (markdown of page numbers and notes).
- build: ctx.chat.send in the MEDIA thread (same view as media/index.js:193); /api/pdf/text for copy; highlights list already in st.hl. small to medium.
- value: owner, each time a PDF is read (papers, contracts, manuals); the biggest gap in the reader.

### USE1-26 [medium value] MEDIA: opening a file means typing a full path; no picker, no drag-and-drop, no folder-as-queue, no resume
- where: media/index.js:57-77 (path field), :343-346 (roots listed, "Subfolders are not searched" at :397), :792-807
- today: library lists only files directly inside Movies/Music/Downloads/etc., newest first, max 100. The player reports position to the server every 4s (REPORT_MS) but never restores it; a long video restarts at 0:00 after leaving the screen. A folder cannot be queued in one click.
- idea: (a) remember last position per path in ctx.prefs and seek on reopen (st.pendingSeek exists); (b) PLAY ALL / QUEUE ALL on a folder listing; (c) accept a file dropped on the stage.
- build: openPath already supports opts.seek; queueOp({op:'add'}) exists; prefs.js stores keyed values. small each.
- value: owner, daily for anyone who watches or listens in the app.

### USE1-27 [low value] MEDIA: YouTube and Spotify open in the browser pane but the pane is a different screen with no return
- where: media/index.js:710-729
- today: the note says "Open BROWSER to see it" and the owner navigates manually.
- idea: auto-navigate to #/browser after a successful pane open, or make the note a link.
- build: set location.hash as voice/index.js does. small.
- value: owner, each use.

PROGRESS: done media

### USE1-28 [high value] SETTINGS: "key not set" and "Orchestrator brain" are read only; there is no way to enter a key or pick the model
- where: settings/index.js:581-611 (paintModels shows keyTag "not set" / "none chosen" with no control), :493-508 (loadOrch is GET only), settings/helpers.js:883-888 (keyTag)
- today: when OLLAMA_API_KEY or GEMINI_API_KEY is "not set" the only message is the tag. Nothing says where to put it. The model policy says only large cloud models are used, so a missing key silently breaks every chat, and the owner cannot fix it from the shell that reports it. The orchestrator model cannot be changed at all.
- idea: an ADD KEY / REPLACE KEY form per provider (password field, confirm card, value never echoed) and a model dropdown filled from st.orch.backends (the GET already returns a catalog), then TEST ALL.
- build: the server already has POST /api/settings/api-keys (webui.py:4475, config.save_api_key_setting) and POST /api/settings/orchestrator-model (webui.py:4446) behind the normal auth gate; neither is called from ui/assets/os. Reuse ask() at settings/index.js:687 and testAll(). medium.
- value: owner, setup and key rotation; today a broken key is a dead end.

### USE1-29 [medium value] SETTINGS: background switches say "applies at the next launch" but offer no relaunch
- where: settings/index.js:646, :662, :710, :720, :727 (all say "applies at the next launch")
- today: five different controls queue a change that needs a restart; the owner must quit and reopen the app by hand and nothing says which changes are pending.
- idea: a pending-changes banner "N changes wait for a restart" with a RESTART NOW button (confirm card).
- build: POST /api/setup/restart (firstrun.restart_app, webui.py:6971) exists and is unused by the OS; pending count from items whose saved value differs from the running value if the summary reports both (otherwise count the session's own writes in a local array). small to medium.
- value: owner, each time a switch is changed.

### USE1-30 [medium value] Model spend and usage are not shown anywhere in the shell
- where: server routes GET /api/usage (webui.py:4321, persisted Claude and Ollama totals), GET /api/budget (webui.py:2902, cost budget snapshot) have no caller in ui/assets/os (grep found none); settings/index.js has no usage card; security egress ledger (security/index.js:128) shows size not cost.
- today: EXPLAIN MY FINDINGS, NEW QUESTION, /all and goals all say "uses credits" but the owner cannot see how much has been used or what the budget is.
- idea: a USAGE card in SETTINGS (and a small figure in the status chrome) from /api/usage and /api/budget.
- build: two GETs, states.populated, no writes. small.
- value: owner, weekly; every credit warning on every screen would finally have a number behind it.

### USE1-31 [medium value] OFFICE: an agent desk shows facts but offers no "talk to this agent"
- where: office/index.js:147-160 (agentCard: model, tools, last activity, OPEN AGENT WINDOW only when not a browser)
- today: after choosing an agent the only action is a separate live window. The owner who wants that specialist (mail, research, news) has to leave OFFICE and rely on routing in HOME.
- idea: an ASK THIS AGENT button that opens HOME with focus on the agent (ctx.chat.send(..., {focusAgent: name}) is already how COMMS, RESEARCH and AGENTSMITH address one agent) with a prefilled prompt box, and a last-activity link to the transcript.
- build: ctx.chat focusAgent plus hash navigation; the agent name is data-agent. small.
- value: owner, daily for power use; turns OFFICE from a status board into a launcher.

PROGRESS: done office
PROGRESS: done settings

### USE1-32 [high value] BROWSER: the open page cannot be saved as a research source, a goal, or a wiki note in one click
- where: browser/split-model.js:~170-190 (STARTERS: only "Summarise this page", "Find the price", "Fill this form"), browser/index.js:1143 (downloads shelf: OPEN and SHOW IN FOLDER only)
- today: the split chat is the right place to act on a page but the three starters are all read-only questions. The server already accepts an arbitrary url and title for a research record (POST /api/os/news/research takes {url, title, channel}; os_api/news.py:55-81 creates the record, adds the link as a source and returns the question text). BROWSER never calls it.
- idea: a SAVE TO RESEARCH button next to the star (bookmark) that posts the active tab's url and title with channel 'browser' (confirm card like NEWS), then toasts "Go to Research"; starters "Extract the main facts as bullet points", "Compare this with my other open tabs", "Add this to my goals as a to-do".
- build: reuse the NEWS toResearch flow (news/index.js:658-689) and cur().url / title (index.js:323); starters are plain data. small.
- value: owner, daily; the browser is where most research starts and today every page must be copy-pasted into RESEARCH by hand.

### USE1-33 [medium value] BROWSER downloads: a finished download cannot be opened in MEDIA or checked for risk from the shelf
- where: browser/index.js:1135-1148 (completed: OPEN, SHOW IN FOLDER, REMOVE)
- today: a PDF, video or audio file lands in Downloads and the shelf offers the system OPEN. MEDIA can read the Downloads folder (media/index.js:67) and has a PDF reader and highlighter, but there is no hop. A security download assessment exists (security_download events, security/helpers.js:124) but the shelf does not show the risk for the file the owner just fetched.
- idea: OPEN IN MEDIA for pdf/audio/video/image (POST /api/os/media/open then #/media), and show the sentry's risk word on the row when security_download carried that file name.
- build: media open route takes a path; the shelf row has the path (d.path assumed from the shell state, verify the field); security events are on ctx.events. small to medium.
- value: owner, several times a week.

PROGRESS: done browser (index.js 1-520 read fully, remainder read by targeted sections; split-chat.js and split-model.js read fully; helpers/privacy/manage files skimmed for user-visible states only)

### USE1-34 [high value] A model approval waiting on another screen is invisible; the shell never says "something needs you"
- where: core/approvals.js:122 (openCount() is defined, nothing calls it: grep over ui/assets/os finds no other use), core/chat.js:147-152 (confirmation_requested sets turn.status 'waiting'), boot.js:153 (chat.onBusy only lights the sidebar dot)
- today: if the owner sends a mail from COMMS, then switches to SECURITY, the mail agent stops at the approval card and waits (the server times it out after about 300 s, approvals.js comment on declineAll). The only hint is a "running" dot on COMMS in the sidebar. The dock Alerts badge counts server alerts only. HOME's attention list reads /api/attention, not pending approvals. Likewise a finished run in another thread gives no notice.
- idea: a toast "Approval needed on COMMS" with a GO action when a confirmation_requested arrives for a thread that is not on screen, a number badge (approvals.openCount()) on the dock or menu bar, and a "Run finished on <screen>" toast with GO when a background thread ends.
- build: approvals.onChange and chat.onBusy listeners are already available in boot.js; toasts.show supports an action. small.
- value: owner, many times a day; today a timeout silently kills the action the owner asked for.

### USE1-35 [high value] No way to open an earlier conversation; NEW THREAD buries it
- where: kit/thread-view.js:110-115 and :353-362 (NEW THREAD posts /api/os/session/new and clears; spec text says "The old session file stays on disk"), server routes GET /api/sessions and GET /api/sessions/recent (webui.py:2913-2915) with no caller in ui/assets/os
- today: after NEW THREAD or Cmd+N the old chat is unreachable from the shell. There is no history list, no search over past chats and no export.
- idea: a HISTORY button on every thread screen (and a launcher entry "Past conversations") listing recent sessions with first line and time; open one read only, with CONTINUE THIS to restore it as the live thread and EXPORT AS MARKDOWN.
- build: list from /api/sessions/recent; restore through the same ledger read the thread view already uses (GET /api/os/session?tab_id=, thread-view.js:368) if the server accepts a session id (needs a small server param), render with buildTurn. medium.
- value: owner, weekly; chat history is the memory of the product.

### USE1-36 [high value] A reply can only be copied or regenerated; it cannot be sent to another screen
- where: kit/thread-view.js:173-180 (acts: COPY, REGENERATE only), :259-265
- today: every thread screen shares one turn view, so adding actions once helps all six. After an answer the owner copies it and pastes it into GOALS, TIMETABLE, WIKI, COMMS or RESEARCH by hand.
- idea: a SEND TO menu on each finished reply: MAKE A GOAL (opens GOALS NEW GOAL prefilled with the question as objective), SCHEDULE THIS (TIMETABLE hand-off text), DRAFT AN EMAIL (COMPOSE prefilled), SAVE TO RESEARCH (news/research route), plus EDIT AND RESEND on the user line.
- build: needs the deep link arguments of USE1-37 (or a pane-inbox style one-slot handoff, core/pane-inbox.js shows the pattern); each target form already has an open-with-values function (openForm in goals, openHand in timetable, openCompose in comms). medium.
- value: owner, daily; this is the missing glue between "ask" and "do".

### USE1-37 [high value] Deep links carry no arguments, so no screen can send the owner to a specific item
- where: core/router.js:14-35 (parseHash returns `rest` but resolveRoute ignores it), news/index.js:677 (toast action only sets '#/research'), voice/index.js:755, security/index.js:407
- today: every cross-screen link lands on the top of the screen. NEWS TO RESEARCH cannot select the record it just made (USE1-6), SECURITY cannot jump to a finding, ALERTS cannot open what they are about, GOALS cannot be opened on a goal id.
- idea: pass `rest` (and a ?query) to the mounted screen as ctx.route.args, or add a generic one-slot inbox like pane-inbox.js (putPaneRequest/takePaneRequest) keyed by screen; then #/research/<question id>, #/goals/new?objective=..., #/comms/compose?to=..., #/security/<fingerprint>.
- build: router.js already has the parsed pieces; ctxFactory can add `route` to ctx; each screen reads it once at mount. About 15 lines in the shell plus 3 to 8 per screen. small to medium. This unlocks USE1-6, 36, 38, 40.
- value: owner, indirect but large: it makes every other connection one click.

### USE1-38 [medium value] Notification Centre rows cannot be opened, and HOME attention items show their screen name as plain text
- where: chrome/notif-centre.js:38 (row has icon, title, detail, time and a dismiss X, no click target), home/index.js:84-108 (it.screen is printed as muted text next to DISMISS)
- today: the owner reads "Security finding..." and then has to find the screen. The attention item even carries a screen field and still offers no OPEN button.
- idea: make the row a button that navigates to alert.screen / kind (security -> #/security, agent -> #/office) and add OPEN beside DISMISS in the attention card.
- build: both already have kind or screen; go(slug) exists. small.
- value: owner, daily.

### USE1-39 [medium value] The launcher searches screens and a few actions only; no content search and no "do" actions
- where: chrome/palette.js:86-119 (buildItems), :121-127 (typed text becomes "Ask Dourmouse")
- today: Cmd+K cannot find a goal, a routine, a research question, a mail, a past chat or a wiki file, and has no entries such as "New goal", "New routine", "Compose mail", "New research question", "Top headlines", "Open file in MEDIA", "Lock this Mac".
- idea: a second group of Actions that deep-link into each screen's form (needs USE1-37) and a debounced content group: goals (/api/os/goals/board), routines (/api/schedules), questions (/api/os/research/questions), wiki entries (/api/device_wiki), mail search (/api/os/comms/inbox?q=) - each fetched only when the query is 3+ characters, cached for the open session.
- build: palette already ranks pure item arrays (palette-search.js); add async providers returning the same {label, help, run} shape. medium.
- value: owner, daily for a power user; Cmd+K becomes the single entry for everything.

### USE1-40 [medium value] Cmd+1..9 reaches only the first nine screens; the daily ones after that (GOALS is 9, TIMETABLE is not) have no shortcut
- where: core/shortcuts.js:307-309 (screens.slice(0, 9)), core/registry.js:215-240 (order)
- today: SECURITY, NEWS, TIMETABLE, COMMAND CENTER, OFFICE need the mouse or the launcher; the order is a fixed mockup order, not use order.
- idea: let the owner pin their own 9 (a remembered list in prefs) or add Cmd+Shift+letter mnemonics (S security, T timetable, N news); show them in the shortcut sheet.
- build: shortcutList and bindable already generate bindings from data. small.
- value: owner, daily keyboard use.

PROGRESS: done core (router, registry, shortcuts, keymap, pane-inbox, ctx, approvals, chat head and send path, host; api.js, events.js, prefs.js, scope.js, ring.js skimmed for routes and stale handling only)
PROGRESS: done chrome (palette, palette-search, shortcuts-panel, sidebar, dock, menubar, composer, control-centre, notif-centre, status, startup-check, driving-strip, panels, stage, toasts, wallpaper, spec-overlay)
PROGRESS: done kit (thread-view, thread-helpers, approval-card, confirm-card read; states, md, html, icons, format, actions, flow-svg skimmed)

### USE1-41 [medium value] "Not available" states hide the one remedy behind a "For developers" fold, and several have no button
- where: kit/states.js:13-26 (splitDeveloper moves any sentence with an ALL_CAPS_NAME, ".env", "restart the server" or "/api/" into a disclosure), wiki/index.js:129-132, voice/helpers.js:1034, news/index.js:745-748, atlas/index.js:833
- today: the WIKI unavailable state says only "No folders are configured" in the main line; the variable to set (DOURMOUSE_WIKI_ROOTS) is folded away. VOICE, NEWS ("watcher is not running") and ATLAS offer no action button at all, only text or RETRY.
- idea: give each unavailable call an `action` (states.unavailable already supports {label, onClick|href}): WIKI "Choose folders" (SETTINGS librarian folders box at settings/index.js:664 already saves a folder list), VOICE "Open SETTINGS", NEWS "Open SETTINGS" (background switches), COMMS already does this (comms/index.js:197).
- build: one-line `action` per call site plus a deep link to the settings group (USE1-37). small.
- value: owner, first run and whenever a feature is off.

### USE1-42 [high value] No daily brief: the morning check is five screens and a dozen clicks
- where: home/index.js:195-200 (empty-state starters are four static prompts), plus data that already exists: GET /api/attention (home/index.js:62), GET /api/world/brief (webui.py:3100), GET /api/os/goals/board, GET /api/schedules (timetable up-next), GET /api/os/comms/inbox, GET /api/security_dashboard
- today: to see overnight events the owner visits COMMS, SECURITY, NEWS, GOALS, TIMETABLE and ATLAS in turn; the only combined view is the model prompt "What needs my attention?" which spends credit and may miss things.
- idea: a BRIEF card on HOME (or the first thing in the empty state) with one line each: unread mail count, security findings and new devices, goals waiting for approval, next routine, world pulse, plus a button "Explain this" that hands the same lines to the model. A routine template "Every morning 07:30 give me the brief" in TIMETABLE (USE1-10).
- build: all reads exist and are cheap, no model; states.populated plus six small fetches with Promise.allSettled as security/index.js:92 does. medium.
- value: owner, every day; turns the shell into a start-of-day dashboard.

### USE1-43 [medium value] New mail never notifies; the inbox is read only while COMMS is open
- where: comms/index.js:445-447 (ctx.every(POLL_MS...) runs only while the screen is mounted; the comment says "there is no mail event"), chrome/dock.js (no mail badge)
- today: an urgent message arrives and nothing says so unless the owner opens COMMS. The sidebar shows no unread count.
- idea: a light shell-level poll (180 s, read-only inbox count with is:unread, only when the Google account is signed in) feeding a dock badge and one toast "3 new messages" with GO TO COMMS.
- build: reuse /api/os/comms/inbox?limit=1&q=is:unread (cached by the server) from chrome/status.js's pattern (timers.every, status.onChange). Respect Private mode. small to medium.
- value: owner, continuous.

TOP10:
1. USE1-34 Surface pending approvals and finished background runs with a toast, GO action and badge (small; approvals.openCount() already exists, unused)
2. USE1-37 Pass deep-link arguments to screens (small to medium; unlocks USE1-6, 36, 38, 39)
3. USE1-28 SETTINGS: enter API keys and choose the orchestrator model (medium; POST routes already exist and are unused by the shell)
4. USE1-10 TIMETABLE: REMOVE (route exists, unused), empty-state templates, RUN NOW (small)
5. USE1-38 Make alerts and HOME attention items open their screen (small)
6. USE1-6 NEWS TO RESEARCH leaves a record RESEARCH cannot continue: add RUN PIPELINE and select the new question (small)
7. USE1-32 BROWSER: SAVE TO RESEARCH for the open tab via the existing news/research route, plus better starters (small)
8. USE1-15 ORCHESTRATION: start an all-hands run from the screen and act on the merged answer (small to medium)
9. USE1-1 COMMS: SUMMARIZE, DRAFT REPLY and ADD TO GOALS on an opened message (small)
10. USE1-21 CODE: ASK ABOUT THIS DIFF, WRITE COMMIT MESSAGE, COPY DIFF per file (small)

SUMMARY:
- findings: 43 (14 high, 25 medium, 4 low)
- by category: dead ends 9 (USE1-6, 13, 16, 19, 24, 28, 29, 35, 41), empty states 3 (USE1-10, 17, 41), missing connections 12 (USE1-1, 6, 7, 14, 21, 25, 31, 32, 33, 36, 37, 38), repeated manual steps 5 (USE1-10, 15, 22, 40, 42), feedback gaps 5 (USE1-3, 11, 12, 34, 43), quick wins 6 (USE1-2, 8, 9, 26, 39, 40), unexposed server capability 8 (USE1-23, 28, 29, 30, 35, 20, 33, 19)
- folders fully read: home, security, comms, research, news, wiki, goals, timetable, projects, agentsmith, orchestration, command_center, apps, atlas, code, voice, media, office, settings, core (main files), chrome, kit (main files)
- folders only partly read: browser (index.js lines 1-520 fully, 520-1562 by targeted sections; privacy-ui.js, privacy-model.js, manage-ui.js, manage-model.js, control-ui.js, split.js, helpers.js skimmed for user-visible states only); core/api.js, events.js, prefs.js, scope.js and kit/md.js, html.js, icons.js, format.js, flow-svg.js skimmed
- routes with no caller in ui/assets/os that carry product value: /api/usage, /api/budget, /api/world/brief, /api/worldmap, /api/schedules/remove, /api/sessions, /api/sessions/recent, /api/memory, /api/memory/search, /api/tasks, /api/settings/api-keys, /api/settings/orchestrator-model (POST), /api/setup/restart, /api/pdf/text, /api/speech, /api/security/quarantine, /api/standing, /api/librarian, /api/os/apps/snapshot, /api/os/apps/act
