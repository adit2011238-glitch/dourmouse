# Fix ledger (2026-10-09 update): one row per finding. Status values: open, fixed (commit), needs-decision (question), needs-backend (what), accepted risk (owner), cannot-reproduce (what was tried; stays open until the owner agrees)

Category is a heuristic label from the title, used to group work; the agent column decides who fixes it.

| ID | Sev | Category | Agent | File | Title | Status |
|---|---|---|---|---|---|---|
| P4-8 | high | Reliability (crash, hang, leak, race) | FB | dourmouse/browser_agent.py | browser_press, browser_submit (focused field) and browser_signin (no submit button) always fail: Keyboard.press has no `timeout` argument | open |
| P3-37 | high | Security and privacy | FD2 | dourmouse/mobile_link.py | mobile_link write_env never writes a changed host or token, so --rotate silently does nothing while the CLI claims it did | open |
| P2-13 | high | Security and privacy | FR | dourmouse/general_roster.py | Protected-path checks in the workspace file tools are case-sensitive on a case-insensitive filesystem | open |
| P5-40 | high | Broken behaviour or false promise | FR | dourmouse/report.py | The scheduled 08:30 daily briefing almost never fires (about 3% of days) | open |
| P3-8 | high | Security and privacy | FS1 | dourmouse/code_backends.py | Claude CLI runs with bypassPermissions and the native-tool deny list is silently dropped if the MCP config step raises | open |
| P5-1 | high | Security and privacy | FS1 | dourmouse/app_control_ax.py | AX app-control fast path bypasses the app blocklist entirely | open |
| P3-67 | high | Logic and correctness | FS2 | dourmouse/voice.py | _say_speak passes user text to `say` as an option-capable argument, so text like "-f/path" makes TTS read any local file and return it as au | open |
| P5-65 | high | Security and privacy | FS2 | dourmouse/system_access.py | The ungated-write protection list is case-sensitive, so ~/.ZSHRC, ~/Library/LaunchAgents spelled in another case, <repo>/DOURMOUSE/, .GIT/ho | open |
| A-1 | medium | Logic and correctness | FB | electron/main.js | Dock "activate" never recreates the console window (hidden windows count) and the recreated one lacks wiring | open |
| A-2 | medium | Logic and correctness | FB | electron/preload.js | Console STUDY and PROJECT buttons feature-detect bridge methods the Electron preload never exposes, and the fallback is denied | open |
| P4-10 | medium | Data loss and integrity | FB | dourmouse/browser_agent.py | browser_creds_store wipes the whole vault when it reads a corrupt or half-written file, and creates it world-readable before chmod | open |
| P4-9 | medium | Security and privacy | FB | dourmouse/browser_agent.py | browser_signin fills the stored password into whatever page the navigation lands on, and accepts plain http | open |
| P5-11 | medium | Logic and correctness | FB | dourmouse/browser_pane.py | check_frameable treats frame-ancestors 'self' as embeddable | open |
| P3-17 | medium | Broken behaviour or false promise | FD1 | dourmouse/eval_harness.py | The eval grader defaults to the same backend and model that answered, contradicting the module's own rule | open |
| P3-36 | medium | Reliability (crash, hang, leak, race) | FD1 | dourmouse/memory_embed.py | Semantic recall can crash with a remote memory store, never fills its embedding cache after one failure, and mixes vectors from different mo | open |
| P4-26 | medium | Logic and correctness | FD1 | dourmouse/history_sync.py | The remote reference file is set with `touch -t` from a UTC timestamp, but touch interprets -t in the remote's local time zone | open |
| P4-27 | medium | Logic and correctness | FD1 | dourmouse/history_sync.py | sync_and_import advances the sync marker even when some scp pulls failed | open |
| P4-29 | medium | Data loss and integrity | FD1 | dourmouse/librarian.py | The librarian's own archive folder is under a default scan root, so archived duplicates are re-indexed and re-proposed as duplicates of thei | open |
| P4-32 | medium | Data loss and integrity | FD1 | dourmouse/live_feeds.py | The task list treats an unreadable tasks.json as empty and the next add_task overwrites it, destroying every task; writes are non-atomic and | open |
| P4-37 | medium | Logic and correctness | FD1 | dourmouse/memory_store.py | Every chat turn re-ingests the whole session ledger, rewriting every old fact and deleting all of their cached embeddings | open |
| P4-39 | medium | Reliability (crash, hang, leak, race) | FD1 | dourmouse/memory_store.py | RemoteMemoryStore does not convert read timeouts or connection resets into RemoteMemoryStoreUnavailable, so LocalFallbackMemoryStore never f | open |
| P4-4 | medium | Logic and correctness | FD1 | dourmouse/backend_fallback.py | probe_ollama_fallback and the "try default local Ollama" branch always fail: probe hits http://127.0.0.1:11434/models, which does not exist | open |
| P4-40 | medium | Logic and correctness | FD1 | dourmouse/memory_store.py | LocalFallbackMemoryStore lacks ingest_session_file, ingest_vault, delete, save_embedding and get_embeddings, so with DOURMOUSE_MEMORY_REMOTE | open |
| P4-6 | medium | Logic and correctness | FD1 | dourmouse/bulk_ingest.py | Drive ingest records every failed download as "skipped_no_text" and marks it done permanently | open |
| P5-13 | medium | Security and privacy | FD1 | dourmouse/device_wiki/walker.py | Device wiki walker follows symlinks and indexes secret files, then sends them to the model | open |
| P5-19 | medium | Reliability (crash, hang, leak, race) | FD1 | dourmouse/global_memory.py | ingest_corpus_file crashes on a non-object entry after validation already flagged it | open |
| P5-23 | medium | Logic and correctness | FD1 | dourmouse/hands_free.py | record_utterance never re-checks the mic kill switch while recording | open |
| P5-24 | medium | Logic and correctness | FD1 | dourmouse/hands_free.py | The whole hands-free turn runs inside the wake-word PortAudio callback, so stop()/kill switch block until the turn ends | open |
| P3-47 | medium | Logic and correctness | FD2 | dourmouse/project_bookkeeper.py | project_bookkeeper has no lock and non-atomic whole-file writes, so a refresh can erase a project created or hidden while it ran | open |
| P3-68 | medium | Data loss and integrity | FD2 | dourmouse/tray.py | The tray kill switch is fail-open on a bad state file, and its in-memory copy overwrites changes made by other processes | open |
| P4-43 | medium | Logic and correctness | FD2 | dourmouse/patch_apply.py | apply_unified_diff inserts pure-addition hunks one line too early | open |
| P4-44 | medium | Reliability (crash, hang, leak, race) | FD2 | dourmouse/patch_apply.py | Both patch appliers read with errors="replace" and rewrite the whole file, so untouched bytes change (invalid UTF-8 becomes U+FFFD, CRLF bec | open |
| P4-46 | medium | Logic and correctness | FD2 | dourmouse/repo_map.py | iter_source_files skips every file when the repo root itself sits under a directory named build, dist, target, node_modules, venv and so on | open |
| P4-48 | medium | Security and privacy | FD2 | dourmouse/repo_index.py | atlas_repo_scan takes any directory from the model, ungated, and stores json/yaml/toml/ini/cfg/txt/html/csv contents (credentials included)  | open |
| P5-32 | medium | Logic and correctness | FD2 | dourmouse/net_errors.py | classify() reports most non-listed HTTP error statuses as "offline" | open |
| P5-38 | medium | Logic and correctness | FD2 | dourmouse/personality_profile.py | personality_profile stores unparseable raw model output as the once-only profile | open |
| P5-39 | medium | Logic and correctness | FD2 | dourmouse/proactive.py | ProactiveSurfacer swallows the first alert after launch (priming happens on the triggering event) | open |
| P5-55 | medium | Data loss and integrity | FD2 | dourmouse/self_extensions.py | Approving a second draft of an already-approved tool overwrites its module, and a failing test then deletes it | open |
| P5-58 | medium | Security and privacy | FD2 | dourmouse/shared_rag.py | merged_search promises "NEVER raises" but non-ExternalCorpusError failures escape; uuid/text vault ids crash the id map | open |
| P5-61 | medium | Data loss and integrity | FD2 | dourmouse/trading212_ops.py | t212_order can place an order and still report an error (read timeout is not caught); duplicate orders on retry | open |
| P3-2 | medium | Reliability (crash, hang, leak, race) | FI1 | dourmouse/atlas/atlas_proposals.py | Harness embeds params as JSON text in Python source, so any true/false/null param crashes every run | open |
| P3-22 | medium | Logic and correctness | FI1 | dourmouse/gdelt_graph.py | poll_once marks a GDELT file as processed even when the download failed, so that 15-minute window is never retried | open |
| P3-3 | medium | Security and privacy | FI1 | dourmouse/atlas/atlas_proposals.py | A non-numeric metric from sandboxed code makes _verdict_from_metrics raise and leaves the async run "running" forever | open |
| P3-30 | medium | Security and privacy | FI1 | dourmouse/google_services.py | gmail_send skips recipient and subject validation on the OAuth path | open |
| P3-34 | medium | Logic and correctness | FI1 | dourmouse/google_services.py | drive_download reads the entire response into memory before enforcing its 50 MB limit | open |
| P3-40 | medium | Reliability (crash, hang, leak, race) | FI1 | dourmouse/mt5_probe.py | mt5_probe panel command crashes whenever the account has any listed symbol, so the HUD MT5 panel never shows a universe | open |
| P3-41 | medium | Logic and correctness | FI1 | dourmouse/mt5_ops.py | MT5 demo/live detection is a server-name substring check, and order size has no cap | open |
| P3-69 | medium | Reliability (crash, hang, leak, race) | FI1 | dourmouse/atlas/atlas_proposals.py | The "local" backtest target can never load data: its loader always raises and claims ATLAS_DATA_PATH is unset | open |
| P4-23 | medium | Reliability (crash, hang, leak, race) | FI1 | dourmouse/freebuff_events.py | FreebuffEventWatcher blocks on resp.read(4096), so events are held back until 4 KB arrives, and an idle stream is treated as offline after 3 | open |
| P4-66 | medium | Logic and correctness | FI1 | dourmouse/atlas/atlas_proposals.py | atlas_proposals resolves its default workspace to dourmouse/workspace (inside the package) instead of the real <repo>/workspace, which is wh | open |
| P5-33 | medium | Data loss and integrity | FI1 | dourmouse/nodes/node_server.py | Node HTTP handlers only catch ValueError, so the documented "refusing to run a job" error (and any other OSError/RuntimeError) drops the con | open |
| P5-5 | medium | Reliability (crash, hang, leak, race) | FI1 | dourmouse/atlas/atlas_lab.py | POST /api/atlas-lab/sync crashes with AssertionError before the lab state exists | open |
| P5-6 | medium | Logic and correctness | FI1 | dourmouse/atlas/atlas_lab.py | Catalog JSON parse errors abort the whole sync without recording sync_error | open |
| P3-49 | medium | Logic and correctness | FI2 | dourmouse/research_mesh/study.py | Practice scores are graded against the raw bytes of a PDF decoded as text, so they are meaningless | open |
| P3-54 | medium | Data loss and integrity | FI2 | dourmouse/research_pipeline/stages.py | run_follow_up closes a task as DONE even when no evidence was gathered, so the contradiction is never retried but the synthesis is revised a | open |
| P3-63 | medium | Broken behaviour or false promise | FI2 | dourmouse/spotify_services.py | The Spotify login callback port defaults to the same port as the TV webhook server, and a failed callback bind is swallowed after the tool c | open |
| P4-51 | medium | Logic and correctness | FI2 | dourmouse/research_mesh_tools.py | research_mesh_qualify resumes a half-finished agent with a brand-new RealBrain that has studied nothing, so later exams fail and can permane | open |
| P4-52 | medium | Logic and correctness | FI2 | dourmouse/research_pipeline/answer_critic.py | The answer critic is blind to negation: "not" and "no" are stopwords, so a sentence that states the opposite of a stored claim scores as SUP | open |
| P4-60 | medium | Reliability (crash, hang, leak, race) | FI2 | dourmouse/tradingview_ops.py | The paper-log rewrite is non-atomic, treats a failed read as an empty log, and crashes on a short or malformed row | open |
| P4-62 | medium | Reliability (crash, hang, leak, race) | FI2 | dourmouse/worldmonitor.py | worldmonitor_call_tool uses the 3 s probe timeout for real data calls, and the catalog check adds a second network round trip to every call | open |
| P5-43 | medium | Logic and correctness | FI2 | dourmouse/research_mesh/exams.py | Citation gate accepts any short substring, so fabricated citations pass | open |
| P5-44 | medium | Reliability (crash, hang, leak, race) | FI2 | dourmouse/research_pipeline/hypotheses.py | Re-running hypothesis generation raises GraphError when the model repeats a statement with a different rationale | open |
| P5-45 | medium | Logic and correctness | FI2 | dourmouse/research_pipeline/hypotheses.py | describe_samples reports p=0 ("significant") for zero-variance samples equal to the null, and for a NaN null | open |
| P5-63 | medium | Data loss and integrity | FI2 | dourmouse/world_watch_regions.py | Watch-region ids collide after a deletion, and the file is written non-atomically | open |
| P5-66 | medium | Security and privacy | FI2 | dourmouse/world_pulse.py | FIRMS map key and ENTSO-E token are embedded in request URLs that appear verbatim in error text served to the UI | open |
| P5-67 | medium | Reliability (crash, hang, leak, race) | FI2 | dourmouse/world_pulse.py | The per-source timeout does not bound the snapshot (executor with-block waits for all workers) and concurrent callers each start a full fan- | open |
| P5-68 | medium | Data loss and integrity | FI2 | dourmouse/world_pulse.py | Partial market failures are never reported: the "EQUITIES UNAVAILABLE" item is truncated away | open |
| P2-14 | medium | Data loss and integrity | FR | dourmouse/general_roster.py | Every build_general_registry() call starts the configured external MCP servers again and never closes them | open |
| P2-2 | medium | Data loss and integrity | FR | dourmouse/dispatch.py | Live Harmony filter drops the rest of a normal reply after any literal "</" | open |
| P2-22 | medium | Logic and correctness | FR | dourmouse/planner.py | Planner routes "message ... agent" requests to messenger, but send_message refuses every call outside a delegated single-agent run | open |
| P2-23 | medium | Broken behaviour or false promise | FR | dourmouse/agent_prompts.py | mail prompt tells the model to use email_own_send as an "is this my address" checker; it sends mail | open |
| P2-3 | medium | Logic and correctness | FR | dourmouse/dispatch.py | A transient error mid-stream is retried and the already-shown text is streamed again | open |
| P2-4 | medium | Reliability (crash, hang, leak, race) | FR | dourmouse/dispatch.py | Tool-call exceptions write the raw arguments to logs/errors.log | open |
| P2-5 | medium | Reliability (crash, hang, leak, race) | FR | dourmouse/dispatch.py | A model tool call whose arguments parse to a non-object crashes the whole turn | open |
| P2-6 | medium | Logic and correctness | FR | dourmouse/dispatch.py | Account rotation never marks or skips the account that actually failed first | open |
| P2-7 | medium | Broken behaviour or false promise | FR | dourmouse/general_roster.py | open_url has no scheme check and the argument gate skips every non-http form, contradicting "the tool itself refuses" | open |
| P3-24 | medium | Reliability (crash, hang, leak, race) | FR | dourmouse/goal_runtime.py | A task left in VERIFYING by a crash is never recovered and can wedge or mislabel its goal | open |
| P3-43 | medium | Logic and correctness | FR | dourmouse/orch_net.py | orch_net builds a fresh NeuroStore on every call, so each prediction re-reads the whole experience log and the store's lock protects nothing | open |
| P4-21 | medium | Logic and correctness | FR | dourmouse/chat.py | ChatSession(session_file=None) used as a throwaway helper writes real ledgers into workspace/sessions, which the app then resumes as "the mo | open |
| P5-30 | medium | Reliability (crash, hang, leak, race) | FR | dourmouse/model_delegation.py | delegate()'s timeout is not enforced: the executor's with-block waits for every worker | open |
| P5-31 | medium | Security and privacy | FR | dourmouse/model_delegation.py | The task `model` field lets any agent's work bypass the "private agents stay local" policy | decision made 2026-10-09: cloud models only; fix = privacy gate (see PRIV in FIX_PLAN), no local routing |
| P5-46 | medium | Data loss and integrity | FR | dourmouse/schedules.py | Schedule ids collide after any removal | open |
| P5-47 | medium | Reliability (crash, hang, leak, race) | FR | dourmouse/schedules.py | SchedulerRunner thread dies on any exception outside _run_one | open |
| P5-48 | medium | Logic and correctness | FR | dourmouse/schedules.py | schedules.jsonl is read-modify-written by several threads without a lock and non-atomically | open |
| A-3 | medium | Security and privacy | FS1 | dourmouse/google_auth.py | Google token refresh wipes the signed-in user's name, picture and sub | open |
| P3-11 | medium | Logic and correctness | FS1 | dourmouse/code_backends.py | MCP-connection retry in _run_claude re-runs the whole task and reuses a --session-id that now exists | open |
| P3-20 | medium | Logic and correctness | FS1 | dourmouse/freebuff_bridge.py | freebuff_dispatch confirmation shows only 160 chars of a prompt that is up to 8000 chars | open |
| P3-9 | medium | Reliability (crash, hang, leak, race) | FS1 | dourmouse/code_backends.py | stream_claude reports a timeout kill as "NOT SIGNED IN" | open |
| P4-16 | medium | Security and privacy | FS1 | dourmouse/design_3d_ops.py | read_manifest_entry / list_manifest read any JSON object file the model names, ungated, which exposes other stores such as the browser crede | open |
| P4-17 | medium | Logic and correctness | FS1 | dourmouse/desktop.py | DesktopBridge.split_with_app interpolates an unvalidated bundle_id into AppleScript, allowing `do shell script` from page JavaScript | open |
| P4-24 | medium | Logic and correctness | FS1 | dourmouse/guardrails.py | guardrails.py is described as "the SAFETY BOUNDARY" but nothing in production calls evaluate_trade or KillSwitch.update, and evaluate_trade  | open |
| P4-33 | medium | Logic and correctness | FS1 | dourmouse/mcp_bridge.py | The Codex MCP registration omits PYTHONPATH, so the bridge cannot import dourmouse unless Codex happens to run from the repo root (the bug a | open |
| P5-15 | medium | Logic and correctness | FS1 | dourmouse/extract.py | Receipt total picks the Subtotal line | open |
| P5-18 | medium | Logic and correctness | FS1 | dourmouse/git_safety.py | auto_commit sweeps everything already staged into the "[dourmouse-auto]" commit, and undo_last then reverts it | open |
| P5-2 | medium | Reliability (crash, hang, leak, race) | FS1 | dourmouse/app_control_ax.py | find_menu_item_ax raises IndexError on a path through a leaf item | open |
| P5-3 | medium | Data loss and integrity | FS1 | dourmouse/app_control_ax.py | send_keystrokes_ax types into whatever is frontmost, and silently truncates long text | open |
| P3-51 | medium | Reliability (crash, hang, leak, race) | FS2 | dourmouse/research_pipeline/extract_html.py | extract_html.extract_main raises RecursionError on deeply nested pages, and the fetch path does not catch it | open |
| P3-60 | medium | Logic and correctness | FS2 | dourmouse/security/platform_adapter.py | Listening-port exposure classification misreads bracketed IPv6 addresses, so IPv6 loopback and all-interface listeners come out as UNKNOWN | open |
| P4-35 | medium | Logic and correctness | FS2 | dourmouse/mcp_client.py | McpClient never matches a response to its request id, so any server notification or stray line desyncs every later reply | open |
| P4-36 | medium | Reliability (crash, hang, leak, race) | FS2 | dourmouse/mcp_client.py | External MCP servers are started with no enforced timeout and no shutdown, and every build_general_registry() call relaunches them all | open |
| P4-53 | medium | Logic and correctness | FS2 | dourmouse/security/browser_history.py | Browser history reader copies every Chromium "Default/History" to the same temp name, so another browser's leftover -wal file can be applied | open |
| P4-54 | medium | Logic and correctness | FS2 | dourmouse/security/downloads.py | DownloadsWatcher never forgets a file name, so a re-downloaded or replaced file with a name seen before is never assessed, and a download pa | open |
| P4-55 | medium | Logic and correctness | FS2 | dourmouse/security/monitoring.py | Several "monitoring" indicators discard the command's success flag and report ABSENT (high confidence) when the check could not run | open |
| P4-57 | medium | Logic and correctness | FS2 | dourmouse/security/sentry.py | A finding is "new" only the first time it has ever been seen, so a recurrence (firewall switched off again, a device or exposed port that re | open |
| P5-26 | medium | Reliability (crash, hang, leak, race) | FS2 | dourmouse/media_convert.py | media_convert._run can deadlock on a full stderr pipe | open |
| P5-27 | medium | Reliability (crash, hang, leak, race) | FS2 | dourmouse/media_convert.py | Failed conversion/probe jobs are cached forever, and probe() runs under the global lock without catching its timeout | open |
| P5-28 | medium | Logic and correctness | FS2 | dourmouse/media_convert.py | sidecar_subtitles glob uses the raw stem as a pattern and a bare prefix match | open |
| P5-51 | medium | Data loss and integrity | FS2 | dourmouse/security/mac_detectors.py | new_unsigned_network_process never fires for processes with long or spaced names (lsof truncation vs psutil name) | open |
| P5-52 | medium | Security and privacy | FS2 | dourmouse/security/privacy.py | Privacy mode fails open: an unreadable or corrupt security.json turns it OFF | open |
| P5-53 | medium | Logic and correctness | FS2 | dourmouse/security/tools.py | security_sentry_dismiss and security_incident_update are ungated tools that can permanently silence real findings | open |
| U1-1 | medium | UI behaviour | FU | ui/assets/os/screens/browser/index.js | BROWSER fallback reload loads the real site, not the proxy URL | open |
| U1-11 | medium | Security and privacy | FU | ui/hub.html | HUB: engine token is baked into a page that serve_hub.py serves with Access-Control-Allow-Origin: * | open |
| U1-15 | medium | UI behaviour | FU | ui/assets/os/screens/home/index.js | HOME and ORCHESTRATION read all hands run.started / run.finished as numeric seconds, but the server sends ISO strings | open |
| U1-16 | medium | Broken behaviour or false promise | FU | ui/all_hands.html | all_hands.html never polls a run it started itself, and its comment-promised SSE retry is disabled | open |
| U1-17 | medium | UI behaviour | FU | ui/hud.html | HUD shows hard-coded numbers as live suit and model readings | open |
| U1-19 | medium | UI behaviour | FU | ui/atlas_lab.html | ATLAS LAB proposals view re-renders every 5 s, resetting the run-target select and collapsing the code under review | open |
| U1-20 | medium | UI behaviour | FU | ui/console.html | (legacy page) console.html approval box moves focus onto APPROVE | open |
| U1-21 | medium | UI behaviour | FU | ui/console.html | (legacy page) console.html SETTINGS save failures are wiped by an immediate paintSettings() | open |
| U1-3 | medium | Broken behaviour or false promise | FU | ui/assets/os/screens/orchestration/index.js | ORCHESTRATION "OPEN PAGE" can never open: host.openExternal refuses a relative path | open |
| U1-6 | medium | Security and privacy | FU | ui/assets/os/screens/timetable/index.js | TIMETABLE schedule editor loses what the owner is typing every 10 seconds | open |
| U1-7 | medium | Reliability (crash, hang, leak, race) | FU | ui/assets/os/screens/voice/index.js | VOICE: pressing LISTEN twice while the microphone prompt is pending leaks a live microphone stream | open |
| U1-9 | medium | UI behaviour | FU | ui/login.html | Sign-in "system browser" bridge reads the Google URL with a cross-origin fetch, which the browser rejects | open |
| A-13 | medium | Logic and correctness | FW | dourmouse/config.py | load_ollama_config(force_local=True) no longer forces local, so "local-only" agents (mail, docs, study) send private content to Ollama Cloud | decision made 2026-10-09: cloud models only; fix = privacy gate (see PRIV in FIX_PLAN), no local routing |
| A-14 | medium | Data loss and integrity | FW | dourmouse/config.py | Settings writers cannot round-trip the config file: an `export KEY=...` line makes every save fail, and values containing " #" are silently  | open |
| A-16 | medium | Logic and correctness | FW | dourmouse/webui.py | force_backend=freellmapi swaps the shared session's client and config outside the lock, and early returns never restore them | open |
| A-17 | medium | Logic and correctness | FW | dourmouse/webui.py | The /api/events stream is closed after 60 idle seconds, so Electron alert notifications stop for the rest of the session | open |
| A-20 | medium | Security and privacy | FW | dourmouse/webui.py | Google sign-in "claim" bridge: caller-chosen claim code plus a pre-auth redeem route lets another party collect the owner's session | open |
| A-10 | low | Logic and correctness | FB | electron/main.js | open_agent builds the window URL from an unencoded renderer-supplied name | open |
| A-11 | low | Broken behaviour or false promise | FB | electron/policy.js | Downloads shelf will open HTML, SVG and webarchive files even though it claims to refuse anything that runs code | open |
| A-12 | low | Data loss and integrity | FB | electron/main.js | Stores keep a corrupt-file rename outside any try/catch, so one failed rename throws out of a store read | open |
| A-4 | low | Logic and correctness | FB | scripts/install_drm_electron.sh | install_drm_electron.sh exits silently when the tag lookup fails, so its own error message never prints | open |
| A-5 | low | Logic and correctness | FB | dourmouse/browser_scripts/media_control.js | Media seek reports a false failure when the target equals the current position | open |
| A-6 | low | Logic and correctness | FB | electron/main.js | Adding or enabling an extension loads it only into the active profile's session; other already-started profiles never get it | open |
| A-7 | low | Reliability (crash, hang, leak, race) | FB | electron/main.js | Stale-server cleanup SIGTERMs whatever pid is in server.pid, which is never removed or verified | open |
| A-8 | low | Logic and correctness | FB | electron/main.js | Tray "Kill camera + mic NOW" and the toggles fail silently when the server call fails | open |
| A-9 | low | Logic and correctness | FB | electron/main.js | Import from Chrome merges against one profile's lists, then writes into whichever profile is active after the confirmation | open |
| P4-11 | low | Logic and correctness | FB | dourmouse/browser_agent.py | Headless popup following stops working after 50 pages because the _NEW_PAGES window is trimmed to a fixed length | open |
| P4-12 | low | Logic and correctness | FB | dourmouse/browser_agent.py | _call() abandons a timed-out coroutine without cancelling it, so a "timed out" browser action can still happen later | open |
| P4-13 | low | Data loss and integrity | FB | dourmouse/browser_agent.py | A relaunched headless Chrome leaks the previous browser process; close_browser never closes the browser or stops Playwright | open |
| P5-12 | low | Logic and correctness | FB | dourmouse/browser_pane.py | Frame-bust regexes have no word boundaries and rewrite the whole HTML | open |
| P3-1 | low | Logic and correctness | FD1 | dourmouse/artifacts.py | publish_artifact tool does not lowercase kind before deciding to JSON-parse content | open |
| P3-16 | low | Security and privacy | FD1 | dourmouse/email_identity.py | email_send_via_smtp lets header, port and socket errors escape although it promises "Nothing was sent" strings | open |
| P3-18 | low | Logic and correctness | FD1 | dourmouse/firstrun.py | save_config re-parses the .env by hand and fails permanently on valid dotenv syntax | open |
| P3-28 | low | Logic and correctness | FD1 | dourmouse/gods_eye.py | run_globe_action reports every HTTP error from the bridge as "not reachable", discarding the bridge's own message | open |
| P3-35 | low | Logic and correctness | FD1 | dourmouse/history_import.py | Codex history import hides schema drift and mishandles the DB path in its read-only URI | open |
| P3-7 | low | Logic and correctness | FD1 | dourmouse/bench.py | bench.percentile is not nearest-rank: it overshoots by one rank for exact products | open |
| P4-22 | low | Logic and correctness | FD1 | dourmouse/device_wiki/stages.py | read_file_for_summary reads the entire file after sniffing 8000 bytes, so a huge text-like file is loaded fully into memory | open |
| P4-28 | low | Reliability (crash, hang, leak, race) | FD1 | dourmouse/history_sync.py | list_remote_changed_files strips leading dots from file names with lstrip("./") | open |
| P4-30 | low | Data loss and integrity | FD1 | dourmouse/librarian.py | apply() can overwrite an existing destination and moves "duplicates" without re-checking that the keeper still exists or that the contents s | open |
| P4-31 | low | Logic and correctness | FD1 | dourmouse/librarian.py | undo() performs real moves inside one SQLite transaction and does not catch move errors, so a failure leaves moved files marked as not undon | open |
| P4-38 | low | Logic and correctness | FD1 | dourmouse/memory_store.py | _fts_query keeps only ASCII letters and digits, so accented and non-Latin queries lose characters or return nothing | open |
| P4-5 | low | Logic and correctness | FD1 | dourmouse/bulk_ingest.py | bulk_ingest checkpoint and status are written only on files that reach the bottom of the loop | open |
| P4-7 | low | Logic and correctness | FD1 | dourmouse/bulk_ingest.py | Local ingest reads whole files into memory before applying the 200k cap, and read/store errors are checkpointed as done | open |
| P5-14 | low | Logic and correctness | FD1 | dourmouse/device_wiki_tools.py | device_wiki_scan accepts a negative max_files_to_summarize | open |
| P5-17 | low | Security and privacy | FD1 | dourmouse/gemini_backend.py | Gemini transport only wraps connection-phase errors; mid-stream failures escape raw and the "wall-clock" timeout is a per-read idle timeout | open |
| P5-20 | low | Logic and correctness | FD1 | dourmouse/global_memory.py | One wrong-dimension vector makes every GlobalMemory.search raise, silently disabling memory | open |
| P5-21 | low | Logic and correctness | FD1 | dourmouse/goals.py | goal_events/export return the OLDEST N events, so a long goal's snapshot never shows what it is doing now | open |
| P5-22 | low | Logic and correctness | FD1 | dourmouse/goals.py | update_goal_status has no terminal-state guard; a stale-snapshot writer can resurrect a cancelled goal | open |
| P5-25 | low | Data loss and integrity | FD1 | dourmouse/learn.py | _last_session_record gives up on the whole session if any single line is corrupt | open |
| P5-4 | low | Logic and correctness | FD1 | dourmouse/all_hands.py | _run_backend_slash only catches RuntimeError | open |
| P3-38 | low | Logic and correctness | FD2 | dourmouse/model_check.py | model_check._matches treats a tagless model name as present if any tag is installed | open |
| P3-39 | low | Logic and correctness | FD2 | dourmouse/model_context.py | The Claude orchestrator briefing hardcodes a desktop-vault claim and probes the retired desktop over SSH | open |
| P3-46 | low | Logic and correctness | FD2 | dourmouse/project_bookkeeper.py | project_bookkeeper cannot find the Claude Code project directory for any path containing a space, dot, underscore or similar character | open |
| P3-62 | low | Security and privacy | FD2 | dourmouse/tv_webhook_server.py | tv_webhook_server accepts every request when TV_WEBHOOK_SECRET is empty, and reads a quoted secret differently from the main server | open |
| P4-45 | low | Logic and correctness | FD2 | dourmouse/patch_apply.py | apply_unified_diff silently applies a hunk to the first matching context when the stated line number does not match, unlike the SEARCH/REPLA | open |
| P4-47 | low | Logic and correctness | FD2 | dourmouse/repo_map.py | iter_source_files enumerates the entire tree (including node_modules and .git) before filtering and applying max_files | open |
| P4-49 | low | Data loss and integrity | FD2 | dourmouse/repo_index.py | Changelog sections with the same "## heading" overwrite each other under one title, and the later one is skipped as "unchanged" | open |
| P4-50 | low | Logic and correctness | FD2 | dourmouse/repo_index.py | scan_repo's prune step calls store.all_facts() and store.delete() on LocalFallbackMemoryStore, which reads a different store than the one wr | open |
| P4-61 | low | Security and privacy | FD2 | dourmouse/ui_contrast.py | audit_tokens silently skips text tokens that are missing or unparseable, the failure mode its own extract_tokens docstring calls dangerous | open |
| P4-64 | low | Data loss and integrity | FD2 | dourmouse/updates.py | Update feed: DOURMOUSE_UPDATE_CHANNEL does not filter anything, and the staged file name is built from the unvalidated feed version with a ` | open |
| P4-65 | low | Data loss and integrity | FD2 | dourmouse/usage_tracker.py | usage_tracker claims atomic writes but rewrites usage.json in place, and one null field in a usage report drops the whole record | open |
| P5-56 | low | Reliability (crash, hang, leak, race) | FD2 | dourmouse/semantic_graph.py | build_semantic_graph can raise despite its "never raises" contract | open |
| P5-57 | low | Security and privacy | FD2 | dourmouse/settings_registry.py | Settings writes create the credentials file with default permissions before chmod 0600 | open |
| P5-59 | low | Logic and correctness | FD2 | dourmouse/shared_rag.py | Vault scores are min-max normalised per query, so a lone hit scores 0.0 (or 1.0) and the top vault hit always outranks local hits | open |
| P5-60 | low | Logic and correctness | FD2 | dourmouse/supabase_sync.py | supabase_sync is never constructed outside tests, its default outbox path is the current directory, and push marks server-skipped rows as sy | open |
| P3-19 | low | Logic and correctness | FI1 | dourmouse/forex_ops.py | forex_paper reports $0.00 realised P&L if any single row has a non-numeric pnl_usd | open |
| P3-23 | low | Logic and correctness | FI1 | dourmouse/gdelt_graph.py | The "bounded" kinetic graph is bounded only by age, not by size | open |
| P3-29 | low | Logic and correctness | FI1 | dourmouse/google_services.py | gmail_search (IMAP path) only swaps double quotes, so backslash and CR/LF in the query reach the IMAP command | open |
| P3-31 | low | Security and privacy | FI1 | dourmouse/google_services.py | Drive search escapes apostrophes the wrong way, so any query containing one fails | open |
| P3-32 | low | Logic and correctness | FI1 | dourmouse/google_services.py | Model-supplied Google ids are interpolated into API URLs without validation | open |
| P3-33 | low | Logic and correctness | FI1 | dourmouse/google_services.py | Partially created Drive docs and Slides decks are not reported when the second call fails for a reason other than 403 | open |
| P3-4 | low | Reliability (crash, hang, leak, race) | FI1 | dourmouse/atlas/atlas_proposals.py | spec["code"] of a non-string type raises TypeError that the callers do not catch | open |
| P3-42 | low | Logic and correctness | FI1 | dourmouse/mt5_ops.py | mt5_panel_snapshot starts a new probe subprocess on every poll while the cache is stale | open |
| P3-5 | low | Logic and correctness | FI1 | dourmouse/atlas/atlas_proposals.py | Every approved local run leaves its work directory behind | open |
| P3-6 | low | Security and privacy | FI1 | dourmouse/atlas/atlas_proposals.py | Static safety pre-filter denylist is bypassed by `__builtins__` as a Name | open |
| P4-1 | low | Logic and correctness | FI1 | dourmouse/atlas/atlas_cli.py | atlas_version() never caches a failed probe, so every /api/atlas poll can block up to 60 s | open |
| P4-2 | low | Reliability (crash, hang, leak, race) | FI1 | dourmouse/atlas/atlas_command.py | atlas_command standard formatter crashes on partial standard files and mis-grades permutation p == 0 | open |
| P4-3 | low | Logic and correctness | FI1 | dourmouse/atlas/atlas_ui_ops.py | atlas_ui_ops puts the wrong directory on sys.path (module moved into dourmouse/atlas/) | open |
| P4-41 | low | Security and privacy | FI1 | dourmouse/nodes/client.py | NodeClient and network_status let malformed node responses escape as raw ValueError/KeyError instead of NodeUnavailable | open |
| P5-10 | low | Logic and correctness | FI1 | dourmouse/atlas/atlas_scheduler.py | Memorial Day computed as the 5th Monday of May, which lands in June in many years | open |
| P5-34 | low | Logic and correctness | FI1 | dourmouse/nodes/node_server.py | Job code is launched with preexec_fn from a multi-threaded server | open |
| P5-35 | low | Logic and correctness | FI1 | dourmouse/nodes/node_server.py | Job-controlled metrics.json is read into memory with no size cap | open |
| P5-7 | low | Broken behaviour or false promise | FI1 | dourmouse/atlas/atlas_lab.py | User-supplied pair is dead; LLM pair always wins, and LLM strategy_type/pair go unvalidated into CLI argv | open |
| P5-8 | low | Broken behaviour or false promise | FI1 | dourmouse/atlas/atlas_lab.py | Merge-order comment contradicts code: strict battery overrides catalog rows | open |
| P5-9 | low | Logic and correctness | FI1 | dourmouse/atlas/atlas_lab.py | _build_report JSON extraction stops at the first "{" line and swallows the failure | open |
| P3-48 | low | Reliability (crash, hang, leak, race) | FI2 | dourmouse/research_mesh/pipeline.py | QualificationPipeline.step raises a bare StopIteration, and the record stays stuck, when the current exam paper is no longer pending | open |
| P3-50 | low | Logic and correctness | FI2 | dourmouse/research_pipeline/core.py | ResearchRecord.reject_claim accepts a reason and throws it away | open |
| P3-52 | low | Reliability (crash, hang, leak, race) | FI2 | dourmouse/research_pipeline/render.py | render_page leaks a headless Chrome and a thread when the render overruns, and reports the requested URL as final_url | open |
| P3-53 | low | Logic and correctness | FI2 | dourmouse/research_pipeline/stages.py | plan() keeps a stray "- " prefix on indented bullet lines | open |
| P3-55 | low | Logic and correctness | FI2 | dourmouse/research_pipeline/stages.py | The verbatim-passage check accepts any substring, however short or unrelated | open |
| P3-64 | low | Security and privacy | FI2 | dourmouse/spotify_services.py | Spotify 401 handling can recurse without bound, and refresh failures escape as raw urllib errors | open |
| P3-65 | low | Security and privacy | FI2 | dourmouse/spotify_services.py | Spotify refresh token is written world-readable, and the login callback accepts any request | open |
| P3-66 | low | Logic and correctness | FI2 | dourmouse/spotify_services.py | Spotify read helpers hide or mis-state real conditions | open |
| P3-70 | low | Broken behaviour or false promise | FI2 | dourmouse/world_pulse_history.py | world_pulse_history rewrites the whole file on every snapshot, writes it non-atomically, and never applies the retention prune its docstring | open |
| P4-59 | low | Logic and correctness | FI2 | dourmouse/tradingview_ops.py | TradingView signals are posted to the bus with to_agent="BROADCAST", but the bus's broadcast address is "*", so no agent ever receives them | open |
| P4-63 | low | Reliability (crash, hang, leak, race) | FI2 | dourmouse/worldmonitor.py | worldmonitor_catalog raises IndexError for a tool whose description is only whitespace | open |
| P4-67 | low | Reliability (crash, hang, leak, race) | FI2 | dourmouse/workspace/atlas_lab/tmp | atlas_lab run directories (generated harness.py and strategy_module.py) are never removed, and each harness hardcodes the absolute path of i | partly done 2026-10-09: 20 of 24 run folders moved to Trash, 4 newest kept (owner OK); root cause (never cleaned up, absolute path in harness) still open |
| P5-42 | low | Reliability (crash, hang, leak, race) | FI2 | dourmouse/research_agent.py | call_research_tool does not handle the subprocess timeout or missing arguments | open |
| P5-62 | low | Broken behaviour or false promise | FI2 | dourmouse/world_brief.py | World brief says "No <channel> items this cycle" when the source reports items but none were passed | open |
| P5-64 | low | Logic and correctness | FI2 | dourmouse/workspace/atlas_lab/tmp | ATLAS lab test runs leave per-run folders (with a hard-coded home path) in the source tree | partly done 2026-10-09: 20 of 24 run folders moved to Trash, 4 newest kept (owner OK); root cause (never cleaned up, absolute path in harness) still open |
| P5-69 | low | Logic and correctness | FI2 | dourmouse/world_pulse.py | One bad news edition aborts the whole news channel | open |
| P5-70 | low | Logic and correctness | FI2 | dourmouse/world_pulse.py | GDELT export is fetched over plain HTTP and unzipped without a size limit | open |
| P2-10 | low | Logic and correctness | FR | dourmouse/dispatch.py | Concurrent delegate budget is a non-atomic check-then-increment | open |
| P2-11 | low | Broken behaviour or false promise | FR | dourmouse/dispatch.py | Model-call deadline counts time spent queued on the local-model semaphore, then the abandoned call runs anyway | open |
| P2-12 | low | Logic and correctness | FR | dourmouse/dispatch.py | The CLI "complete answer" shortcut returns declined or confirmation text as the final answer | open |
| P2-15 | low | Security and privacy | FR | dourmouse/general_roster.py | claude_code's description says it runs with default permissions, the handler bypasses all permissions | open |
| P2-16 | low | Logic and correctness | FR | dourmouse/general_roster.py | claude_code and codex_code spawn the CLI with the bare server environment | open |
| P2-17 | low | Reliability (crash, hang, leak, race) | FR | dourmouse/general_roster.py | CLI timeout message claims the task is still running although subprocess.run killed it | open |
| P2-18 | low | Logic and correctness | FR | dourmouse/general_roster.py | read_agent_inbox lets any agent read (and mark read) any other agent's inbox | open |
| P2-19 | low | Logic and correctness | FR | dourmouse/general_roster.py | Confirmation for docs_insert_image never shows the image URL | open |
| P2-20 | low | Logic and correctness | FR | dourmouse/general_roster.py | File tools raise after writing when the workspace path contains a symlink | open |
| P2-21 | low | Security and privacy | FR | dourmouse/general_roster.py | Delegated runs ignore the privacy pin that _build_client enforces | open |
| P2-24 | low | Logic and correctness | FR | dourmouse/agent_prompts.py | Bespoke prompts name tools that do not exist or have a different name | open |
| P2-25 | low | Broken behaviour or false promise | FR | dourmouse/agent_prompts.py | research_info prompt tells the agent to use open_url, which the tool description forbids for research | open |
| P2-26 | low | Security and privacy | FR | dourmouse/agent_prompts.py | music prompt points "give me a Spotify link" at spotify_link, which starts account OAuth linking; it also promises seek | open |
| P2-27 | low | Broken behaviour or false promise | FR | dourmouse/agent_prompts.py | comms and docs prompts promise actions the tools cannot perform | open |
| P2-28 | low | Broken behaviour or false promise | FR | dourmouse/agent_prompts.py | Bespoke prompts contradict the gates or the base prompt on confirmation | open |
| P2-29 | low | Broken behaviour or false promise | FR | dourmouse/agent_prompts.py | Stale or false capability claims in prompts | open |
| P2-30 | low | Logic and correctness | FR | dourmouse/model_router.py | "429" substring makes any error text containing those digits a rate-limit signal | open |
| P2-31 | low | Logic and correctness | FR | dourmouse/execution_policy.py | The identical-call cap is shared by the whole request tree, so fan-outs with zero-argument tools are refused after three calls | open |
| P2-8 | low | Logic and correctness | FR | dourmouse/dispatch.py | Stopping a run between tool calls leaves an assistant tool_calls message with no tool results in history | open |
| P2-9 | low | Broken behaviour or false promise | FR | dourmouse/dispatch.py | DlpFilter's promise is not met for tool_use arguments and live deltas | open |
| P3-25 | low | Reliability (crash, hang, leak, race) | FR | dourmouse/goal_runtime.py | Orphan recovery spends two attempts for one interrupted run | open |
| P3-26 | low | Broken behaviour or false promise | FR | dourmouse/goal_tools.py | create_goal's tool description promises concurrent execution of independent tasks, but the runtime runs them one at a time | open |
| P3-27 | low | Logic and correctness | FR | dourmouse/goal_tools.py | Goal tools accept unvalidated depends_on ids and non-list success_criteria | open |
| P3-44 | low | Logic and correctness | FR | dourmouse/orch_net.py | The cached routing model can pair new weights with the previous agent vocabulary | open |
| P4-14 | low | Reliability (crash, hang, leak, race) | FR | dourmouse/chat.py | Per-turn recall and skill blocks are inserted into self.messages and never removed in a live session, so stale ones keep being re-sent | open |
| P4-15 | low | Data loss and integrity | FR | dourmouse/chat.py | Session state snapshot is written non-atomically and a truncated file then makes every later resume raise | open |
| P5-36 | low | Reliability (crash, hang, leak, race) | FR | dourmouse/orchestrator.py | orchestrator.dispatch lets a malformed tool call crash the loop instead of returning an error tool result | open |
| P5-41 | low | Logic and correctness | FR | dourmouse/report.py | The first _report_time() call in the reporter thread is unguarded | open |
| P5-49 | low | Broken behaviour or false promise | FR | dourmouse/schedules.py | "every <weekday>s" is documented but rejected | open |
| P2-1 | low | Security and privacy | FS1 | dourmouse/governance.py | DLP spaced-secret pattern is malformed, so multi-word secrets split by newline or spaces are not redacted | open |
| P3-10 | low | Logic and correctness | FS1 | dourmouse/code_backends.py | The shared-desk awareness hint is never sent on the CODE chat path | open |
| P3-12 | low | Logic and correctness | FS1 | dourmouse/code_backends.py | The once-per-session preamble gate is keyed on a session id recorded before the first run succeeds | open |
| P3-13 | low | Logic and correctness | FS1 | dourmouse/code_backends.py | stream_claude reads stderr only after stdout hits EOF | open |
| P3-14 | low | Logic and correctness | FS1 | dourmouse/connections.py | format_connections always reports the Freebuff API as not ready | open |
| P3-15 | low | Reliability (crash, hang, leak, race) | FS1 | dourmouse/connections.py | A malformed DOURMOUSE_MEMORY_REMOTE_URL port crashes the whole connections report | open |
| P3-21 | low | Logic and correctness | FS1 | dourmouse/freebuff_bridge.py | freebuff_dispatch accepts any absolute path, although its docs say it must be a path Freebuff already knows | open |
| P4-18 | low | Logic and correctness | FS1 | dourmouse/desktop.py | _open_in_chrome always reports success, so the default-browser fallback never runs when the Chrome launch fails | open |
| P4-19 | low | Logic and correctness | FS1 | dourmouse/desktop.py | DesktopNotifier builds AppleScript strings with json.dumps, so non-ASCII alert text is shown as literal \uXXXX | open |
| P4-20 | low | Reliability (crash, hang, leak, race) | FS1 | dourmouse/desktop_rag.py | The one automatic retry doubles the worst-case TIMEOUT wait to about five minutes, contradicting the "hard timeout, never wedges a chat turn | open |
| P4-25 | low | Logic and correctness | FS1 | dourmouse/guardrails.py | Sector concentration check ignores a trade whose sector label differs from the held position's sector | open |
| P4-34 | low | Reliability (crash, hang, leak, race) | FS1 | dourmouse/mcp_bridge.py | A non-object JSON line (for example an array) crashes McpBridgeServer.serve_forever | open |
| P5-16 | low | Logic and correctness | FS1 | dourmouse/extract.py | Encrypted PDFs raise out of extract_pdf_text instead of returning "PDF READ FAILED" | open |
| P3-56 | low | Reliability (crash, hang, leak, race) | FS2 | dourmouse/security/connectivity.py | connectivity.diagnose tries only the first resolved address and files handshake and HTTP timeouts under the wrong categories | open |
| P3-57 | low | Logic and correctness | FS2 | dourmouse/security/lockdown.py | lockdown.site_is_blocked reports a site as blocked whenever DNS fails, so status shows "blocked_now" while offline or with the helper missin | open |
| P3-58 | low | Logic and correctness | FS2 | dourmouse/security/lockdown_helper.py | More than 500 blocked domains makes the root helper clear the whole block silently | open |
| P3-59 | low | Logic and correctness | FS2 | dourmouse/security/mac_telemetry.py | Host firewall check reads "block all incoming" mode as firewall off | open |
| P3-61 | low | Logic and correctness | FS2 | dourmouse/security/report.py | Security report rates the Downloads area "good" without having checked anything, and hardcodes a ClamAV statement | open |
| P4-56 | low | Reliability (crash, hang, leak, race) | FS2 | dourmouse/security/reputation.py | check_ip_reputation and run_self_audit can raise despite "never raises" and have no timeout on git | open |
| P4-58 | low | Broken behaviour or false promise | FS2 | dourmouse/security/sentry.py | open_incident refuses to open a case for a fingerprint that has a closed one, contradicting the documented "open a NEW incident for a recurr | open |
| P5-29 | low | Logic and correctness | FS2 | dourmouse/media_convert.py | Audio files with embedded cover art are planned as video and transcoded with libx264 | open |
| P5-50 | low | Logic and correctness | FS2 | dourmouse/security/baseline.py | Baseline "gone" persistence anomalies are produced but never reported | open |
| P5-54 | low | Security and privacy | FS2 | dourmouse/security/tools.py | Privacy mode does not withhold security_self_audit or lockdown_status output from the cloud chat model | open |
| U1-10 | low | UI behaviour | FU | ui/setup.html | SETUP continues with an NVIDIA key that was edited after it was validated | open |
| U1-12 | low | UI behaviour | FU | ui/hub.html | HUB health dots show green for any HTTP answer, including 401 and 500 | open |
| U1-13 | low | Security and privacy | FU | ui/product.html | PRODUCT page escapes only & < > but puts values into attributes, inline JS and unescaped template slots | open |
| U1-14 | low | Broken behaviour or false promise | FU | ui/product.html | PRODUCT page REJECT claims "Logged to the audit trail" but nothing is recorded | open |
| U1-18 | low | Broken behaviour or false promise | FU | ui/hud.html | HUD "DRIVE" button says it opens the ATLAS dashboard but navigates to a raw JSON endpoint | open |
| U1-2 | low | UI behaviour | FU | ui/assets/os/screens/browser/index.js | BROWSER mount consumes and acts on a pending pane request after the screen was disposed | open |
| U1-22 | low | Reliability (crash, hang, leak, race) | FU | ui/console.html | (legacy page) console.html copies code blocks with a trailing "COPY" line | open |
| U1-23 | low | UI behaviour | FU | ui/console.html | (legacy page) console.html queued directives run on whichever screen is open when they drain | open |
| U1-24 | low | UI behaviour | FU | ui/console.html | (legacy page) console.html SKIP CONFIRMATIONS (auto-approve) turns on with one click | open |
| U1-25 | low | UI behaviour | FU | ui/console.html | (legacy page) console.html puts feed-supplied links into href and window.open without checking the scheme | open |
| U1-26 | low | Reliability (crash, hang, leak, race) | FU | ui/console.html | (legacy page) console.html 3D editor keeps rendering, and can poll forever, while its screen is hidden or Three.js failed | open |
| U1-27 | low | Security and privacy | FU | ui/console.html | (legacy page) console.html boot readout writes node and model strings with innerHTML unescaped | open |
| U1-4 | low | UI behaviour | FU | ui/assets/os/screens/goals/index.js | GOALS keeps the "Could not refresh" banner after the next poll succeeds | open |
| U1-5 | low | UI behaviour | FU | ui/assets/os/screens/research/index.js | RESEARCH "Live updates paused" banner is never cleared by a successful poll | open |
| U1-8 | low | Broken behaviour or false promise | FU | ui/assets/os/screens/voice/index.js | VOICE header comment says unrecognised text is sent to the companion, the code deliberately never does | open |
| A-15 | low | Security and privacy | FW | dourmouse/config.py | Credential config file is created with the default umask and only chmod'ed afterwards | open |
| A-18 | low | Reliability (crash, hang, leak, race) | FW | dourmouse/webui.py | Login cookie check raises ValueError on a malformed expiry instead of returning False | open |
| A-19 | low | Logic and correctness | FW | dourmouse/webui.py | GET /api/security/report?fresh=1 builds and writes a report, and GET requests are not cross-site checked | open |
| A-21 | low | Security and privacy | FW | dourmouse/webui.py | /api/auth/claim writes two status lines, so the response is malformed HTTP | open |
| A-22 | low | Logic and correctness | FW | dourmouse/webui.py | GET /api/settings/orchestrator-model reports the NVIDIA model as "current" when the active backend is another one | open |
| A-23 | low | Logic and correctness | FW | dourmouse/webui.py | Hands-free turns that need a confirmation wait invisibly for 300 s while holding the global session lock | open |
| P3-45 | low | Logic and correctness | FW | dourmouse/os_api/__init__.py | os_api route table is marked loaded before the backend modules finish importing | open |
| P4-42 | low | Security and privacy | FW | dourmouse/os_api/agentsmith.py | AGENTSMITH approve binds the owner's hash to the module text only, although the draft's test source is executed too and is shown to the owne | open |
| P5-37 | low | Logic and correctness | FW | dourmouse/os_api/apps.py | POST /api/os/apps/allow with only a bundle id can adopt the name of an unrelated running app | open |
| H-1 | high | Security and privacy | FB | dourmouse/browser_agent.py | Clicks on submit-like controls (Send, Submit, Buy, Pay, Place order, Delete) are not confirmation-gated | open (owner decision 2026-10-09: ask first) |
| PRIV-1 | high | Security and privacy | PRIV | dourmouse/governance.py | No data-classification tier or consent gate before sensitive content (mail, memory, files, Drive) is sent to a cloud model | open (new, from owner decision: cloud only) |
| PRIV-2 | medium | Security and privacy | PRIV | dourmouse/governance.py | Redaction covers secrets only; personal identifiers (card, IBAN, ID numbers, phone, third-party emails) go to the cloud unchanged | open (new) |
| PRIV-3 | medium | Broken behaviour or false promise | PRIV | dourmouse/model_delegation.py | _LOCAL_ONLY_AGENTS and force_local promise local handling that cloud-only policy and A-13 make impossible; every comment, prompt and UI line that says 'stays local' for model use is false | open (new) |
| PRIV-4 | medium | Security and privacy | PRIV | dourmouse/dispatch.py | No egress ledger: nothing records which provider received which class of data, how much, and what was redacted | open (new) |
| PRIV-5 | medium | Security and privacy | PRIV | ui/assets/os/screens/settings | No visible per-source consent and a Private mode toggle; no provider table with retention and training terms for the owner to verify | open (new) |
