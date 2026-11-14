# STRESS1 robustness and stress read (read-only, 2026-10-27)
Scope: dourmouse/webui.py and the other files named in the task. Findings already in FIX_LEDGER_2026-10-06.md are not repeated.

### STRESS1-1 [high] One stalled /api/events client blocks the dispatch thread on every streamed token
- where: dourmouse/webui.py:1161-1169 (_SSEStream.emit), 1236-1242 (_SSEBroadcast.broadcast), dourmouse/dispatch.py:2810-2839 (_broadcast_live_trace)
- trigger: a second window or a phone on Tailscale that stops reading its /api/events socket without closing it (laptop lid closed on the other device, wifi drop, half-open TCP) while an agent run streams thinking_delta/assistant_delta events. The per-socket kernel buffer fills, then every write blocks.
- symptom: broadcast() walks clients serially and each emit() holds that stream's lock across a blocking socket write (handler.timeout = 60). Every delta the run emits stalls the run's own thread up to 60 s, so a turn that normally takes 20 s crawls for many minutes. After the timeout emit() raises TimeoutError (only BrokenPipe/ConnectionReset are caught), so the later clients in the list miss that event, and the dead client stays registered because its own ping() queues on the same lock.
- evidence: `except (BrokenPipeError, ConnectionResetError):` in emit(); `for c in clients: c.emit(payload)` in broadcast()
- fix: give each SSE client a bounded queue (for example 200 events) drained by that client's own handler thread; drop the client when the queue is full; catch OSError (including TimeoutError) in emit and mark client_gone; never write to a socket from the dispatch thread.
- test: open /api/events with a raw socket that never reads, run a fake agent emitting 50k thinking_delta events of 1 KB through _broadcast_live_trace, time the loop (expect 60 s stalls after the buffer fills). Unit version: a fake wfile whose write sleeps 2 s, 20 broadcasts, assert total time stays under 1 s.

### STRESS1-2 [medium] Closing the window (or a sleeping laptop) during an approval card freezes every other chat for up to 300 s, repeated per gated call
- where: dourmouse/webui.py:468-470 (_PendingConfirmation.wait), 5933-5943 (session_lock held across session.ask), 5819-5825 (sink)
- trigger: ask for anything gated (send mail), then close the console window or let the Mac sleep before clicking. The chat thread waits on an Event for _CONFIRM_TIMEOUT_SECONDS = 300 and ignores should_stop/client_gone; it holds session_lock (shared by every untagged chat, hands-free, role switch) the whole time. A model that then tries a second gated tool waits another 300 s.
- symptom: a new window opened after the first one was closed sends a message and sees no output for up to 5 minutes (or 5 min x number of gated calls). The 200 response header has already been sent, so the UI shows a spinner, not an error. Wall clock timers in Event.wait stop counting during sleep, so after wake the 300 s starts again effectively.
- evidence: `self._event.wait(_CONFIRM_TIMEOUT_SECONDS)`
- fix: wait in 1 s slices and also return False when the originating stream has client_gone (pass should_stop into the gate); decline on the first slice after the client leaves; cap total approval wait per turn.
- test: start a turn with a fake gated tool, close the socket without answering, then POST a second /api/chat; measure the second request's time to first byte (expect about 300 s).

### STRESS1-3 [medium] Every chat request is a thread parked on one global lock; double submits and hammering queue full model turns
- where: dourmouse/webui.py:5933 (`with session_lock:`), 5811-5815 (SSE headers sent before the lock is taken)
- trigger: a user (or a retrying client) sends 10 prompts quickly with no tab_id, or two windows send at once. Each request gets a handler thread, writes the 200 SSE headers, then blocks on session_lock with no keepalive and no way to notice the client leaving (client_gone is only set by a failed write).
- symptom: threads are unbounded (ThreadingHTTPServer, no cap); abandoned prompts still run as full model turns, in lock acquisition order (threading.Lock is not FIFO), each adding its user message to the shared history. The user sees stale replies arrive minutes later and cost/budget is burned for prompts they already gave up on.
- evidence: `with session_lock:` after `self.send_response(200)` / `self.end_headers()` with no ping loop
- fix: acquire the lock with a short timeout in a loop that pings the stream and checks the socket for EOF (select on self.connection); answer 409 "a turn is already running" or queue explicitly with a visible position; cap the queue length.
- test: loop 30 POST /api/chat requests with a stub slow session.ask (2 s); close 25 of the sockets immediately; count how many ask() calls still run (expect 30).

### STRESS1-4 [medium] Truncated or failed uploads are saved as complete files and overwrite the previous file of the same name
- where: dourmouse/webui.py:5177-5192 (_handle_upload), 5237-5249 (_handle_rag_upload)
- trigger: upload 40 MB from a phone and lose wifi at 30 MB, or fill the disk during the write. `self.rfile.read(length)` returns short data at EOF with no error; the handler never compares len(data) with length. open(target, "wb") truncates the existing file first and writes in place.
- symptom: the response is `{"ok": true, "size": 31457280}` for a partial file; the RAG path then indexes half a PDF. On ENOSPC the handler returns 500 but the earlier upload with that name is already destroyed and a zero or partial file remains.
- evidence: `data = self.rfile.read(length) if length else b""` followed by `with open(target, "wb") as fh:`
- fix: reject when len(data) != length (400, nothing written); write to a temp file in the same directory, fsync, os.replace; remove the temp on any OSError; stream in chunks instead of holding up to 50 MB per request in RAM.
- test: send a request with Content-Length 1000 and 400 bytes of body then close; expect 400 and no file. Fill a tiny tmpfs and upload over an existing name; expect the old file intact.

### STRESS1-5 [medium] /api/setup recomputes under every concurrent poll and its cache is already stale when stored
- where: dourmouse/webui.py:1704-1715 (build_setup_status), 1881-1889 (per-call memory-count thread)
- trigger: two windows (or a window plus a status widget) polling /api/setup. The uncached build runs `claude --version` (10 s timeout), a keychain subprocess, and connection probes; the cache timestamp `now` is taken before the build.
- symptom: with a build that takes 15 s and a 20 s TTL the entry is only 5 s fresh when stored, and there is no single flight, so every poll in that window starts its own full build (extra subprocesses and a new daemon thread calling memory.count() against a slow remote store each time). Offline, a steady trickle of subprocess and socket work and the SETUP panel never feels fast.
- evidence: `now = time.monotonic()` before `result = _build_setup_status_uncached(server)` and `_setup_status_cache.update(server=server, at=now, result=result)`
- fix: stamp the cache with time.monotonic() after the build, hold a per-cache "building" flag so concurrent callers wait for or reuse the in-flight build (return the last result while refreshing).
- test: monkeypatch _build_setup_status_uncached to sleep 3 s, fire 10 threads calling build_setup_status, count builds (expect 10, want 1).

### STRESS1-6 [medium] Resuming or reloading a long session re-reads and returns the whole ledger; one torn line breaks reload forever
- where: dourmouse/webui.py:8429-8446 (_list_recent_sessions reads each of 10 files twice), 8496-8527 (_get_session_transcript)
- trigger: a session ledger that has grown to tens of MB over days (each record carries the full tool transcript), then a page reload or the dashboard polling /api/sessions/recent. Or a crash/kill/ENOSPC leaves a half-written last JSONL line.
- symptom: /api/session/current builds one JSON response holding every turn with every transcript event (hundreds of MB possible) and the reload hangs or the renderer runs out of memory. /api/sessions/recent does 20 whole-file reads per call on the request thread. A single bad line raises JSONDecodeError, so /api/session/current answers `could not read session` for the rest of that session's life (the UI loses all history) and /api/sessions/recent silently drops the session from the list.
- evidence: `for line in target.read_text(encoding="utf-8", errors="replace").splitlines():` ... `except (json.JSONDecodeError, OSError) as exc:`
- fix: parse line by line and skip a bad line; return only the last N turns (with a `before` cursor) and cap transcript size per turn; compute turn count and summary from a small sidecar index or by tail-reading.
- test: write a 200 MB ledger of fake turns and time GET /api/session/current; append `{"turn": ` with no closing and confirm the error.

### STRESS1-7 [low] Gmail inbox warmer logs in every 90 s forever with no backoff, even when offline or the password is wrong
- where: dourmouse/webui.py:1554-1560 (_loop), 1497-1508 (_gmail_inbox_cache_refresh)
- trigger: laptop offline for a day, or an expired app password.
- symptom: a new IMAP SSL connection attempt (30 s timeout) every 90 s for the life of the process; a failed attempt is cached as a fresh `{"ok": false}` payload for the TTL, and repeated bad-password logins are what mail providers lock accounts for. The same pattern in the world pulse warmer (1403-1411): force=True refresh every 60 s with each source timing out, no backoff.
- evidence: `_gmail_warmer_stop.wait(max(_gmail_inbox_ttl() / 2.0, 1.0))` after a failure path that does not lengthen the wait
- fix: exponential backoff (cap 30 min) on consecutive failures, stop retrying after an auth failure until settings change, pause while the network is down.
- test: point gmail_search at a stub that raises; run the loop with a fake clock and assert the interval grows.

### STRESS1-8 [low] Artifact sink is a single global slot shared by concurrent chats
- where: dourmouse/webui.py:5842-5844, 5856-5857, 6028-6029 (artifacts_store.set_sink)
- trigger: two tabs with different tab_ids (separate locks) run turns at once, or a code_claude passthrough overlaps a normal turn.
- symptom: the second request overwrites the first one's sink, and when either finishes its `set_sink(None)` removes the other's, so artifact cards for the still-running turn stop appearing, or appear in the other window.
- evidence: `artifacts_store.set_sink(stream.emit)` ... `artifacts_store.set_sink(None)`
- fix: key sinks by request or tab (a dict), or pass the sink in the tool context instead of a process global.
- test: start two fake turns on different tab_ids, finish the first, publish an artifact in the second, assert it is delivered.

### STRESS1-9 [low] Per-tab sessions, gates and locks are never evicted and tab_id is client chosen
- where: dourmouse/webui.py:5587-5661 (_session_gate_lock_for_tab), 8746-8749
- trigger: opening many projects over days, or any client posting varying tab_id values.
- symptom: each new tab_id builds a full ChatSession (message history, client handles, a ledger path per P4-21) held in three dicts for the life of the process. Memory grows with every distinct id; none is released after the tab closes.
- evidence: `self.server.sessions_by_tab[tab_id] = session`
- fix: LRU cap (for example 32 sessions), evict idle ones after a few hours and persist-then-drop; reject tab_ids that are not the app's own pattern.
- test: loop 2000 POSTs to /api/chat with distinct tab_id and a stub client; watch RSS and len(server.sessions_by_tab).

PROGRESS: done dourmouse/webui.py (partial: lines 1-2780, 3250-3370, 3940-4060, 5130-5270, 5560-6520, 8140-9408; route bodies elsewhere are read in the next pass)

NOTE on STRESS1-1: while this read was running a sibling change (test file dourmouse/tests/test_fix_ST1_sse_slow_client.py, "ST-1") started rewriting _SSEStream/_SSEBroadcast in the working tree (per-client queue and writer thread). Treat STRESS1-1 as the same defect; verify that fix also covers dispatch._broadcast_live_trace and ActivityTracker, and the 'done' path where emit() raised TimeoutError.

### STRESS1-10 [high] A full disk turns a finished answer into an error and leaves a torn ledger line that corrupts the next record
- where: dourmouse/chat.py:419-424 (ask() finally calls _persist), 631-642 (_persist: append, then tmp write and os.replace of the whole snapshot)
- trigger: the volume holding workspace/sessions fills up (or quota, or a read-only remount) between the model answering and the ledger append.
- symptom: `_persist` runs inside `finally`, so its OSError replaces the return value: the complete answer the model produced is discarded and webui shows "ERROR: [Errno 28] No space left on device". The single `fh.write(json.dumps(record) + "\n")` can land partially, so the file ends in a half line with no newline; the next successful turn appends its record on the same line, producing one unparseable line. From then on `_last_record_hash`, `verify_session_audit`, `/api/session/current` (STRESS1-6) and ingest_session_file all choke on or skip it, the hash chain reads as tampered, and `<name>.messages.json.tmp` is left behind.
- evidence: `self._persist(prompt, report, elapsed_ms, display_text=display_text, screen=screen)` inside `finally:`
- fix: wrap `_persist` in try/except OSError inside the finally so the report still returns (and the UI says "answer shown but not saved"); before each append check the file ends in "\n" and repair or start a new line; write the record to a temp, fsync, and only then append; show a disk-full notice via the alerts store.
- test: monkeypatch Path.open / os.replace to raise OSError(ENOSPC) on the second call, call ask() with a stub client, assert the report is returned; then truncate the last line mid-record and call ask() again and assert verify_session_audit isolates the bad line.

### STRESS1-11 [medium] Every turn re-reads and re-parses the whole ledger and rewrites the whole message snapshot, and the in-memory history never shrinks
- where: dourmouse/chat.py:430-437 and dourmouse/memory_store.py:403-440 (ingest_session_file reads every line of the ledger each turn), chat.py:640-642 (snapshot of all of self.messages each turn), chat.py:185,334 (self.messages only ever grows)
- trigger: one long-lived session over days (the server keeps ONE ChatSession for the process and resumes the newest ledger on restart), with tool-heavy turns (each ledger record carries the full transcript).
- symptom: turn N costs O(total ledger size) of file read plus json.loads of every record plus a SELECT of all stored titles, and rewrites the entire messages.json, all under session_lock. After a few hundred tool-heavy turns (ledger tens of MB) each reply is delayed by seconds after the answer is already shown, the next request waits on the lock, RSS grows without bound, and a restart has to json-load the whole snapshot before serving.
- evidence: `self.memory.ingest_session_file(self.session_file)` (called after every completed turn)
- fix: ingest only the record just written (the autosave path already does this per turn via remember()); keep a byte offset for incremental reads; cap or summarise self.messages beyond a window (the LLM already sees a bounded copy) and snapshot only the retained window; rotate ledgers by size (new file per N turns with a pointer record).
- test: stub client, loop 500 ask() calls each with a 50 KB fake tool result, time ask() at turns 10, 100, 500 (expect growth), watch RSS and the sizes of the .jsonl and .messages.json.

### STRESS1-12 [medium] The office log writes one fsync'd row per streamed token on the dispatch thread, never prunes, and transcript() returns the oldest rows
- where: dourmouse/office_logger.py:221-237 (_log_agent_event), 143-144 (new connection per write), 372-399 (transcript ASC LIMIT), dourmouse/webui.py:5823-5825 (sink calls office_log.on_event inline)
- trigger: any streaming turn (assistant_delta and thinking_delta are in _AGENT_EVENT_TYPES), or several agents streaming in parallel (all share one lock).
- symptom: each delta opens a SQLite connection, takes the log-wide lock, INSERTs and commits (WAL, synchronous FULL means a sync per commit) before the next token can be handed to the browser, so streaming speed is bounded by disk latency and gets visibly choppy on a busy disk; with delegate_parallel the branches serialize on `self._lock`. The table has no retention, so a heavy week produces millions of rows and a multi-GB agent_events table (idx on agent, call_id and ts add write cost). transcript(agent=...) is `ORDER BY id ASC LIMIT n`, so asking for an agent's activity returns its oldest 200 rows and never what it is doing now. append_event (events table) has no try/except, so a locked or full DB raises into the security-scan listener (`_log_scan`) and the research-graph writers.
- evidence: `INSERT INTO agent_events (agent, call_id, depth, type, name, text, ts)` executed per event
- fix: buffer deltas per call_id and write one row per flushed chunk (for example every 500 ms or on tool_use/done); use one long-lived connection with synchronous=NORMAL; add a retention job (keep 14 days or 200 MB); make transcript() return the newest N then reverse; wrap append_event like the other writers.
- test: emit 20k assistant_delta events through OfficeLogger.on_event and time it (expect tens of seconds on a spinning or busy disk); then check row count and file size after a simulated week.

### STRESS1-13 [medium] No cap on a tool result: it is kept whole in history, ledger, snapshot, three SSE copies and the live trace
- where: dourmouse/dispatch.py:5939-5946 (result_entry and tool_message appended uncapped), 598-606 (_bounded_context only truncates results OLDER than the in-flight exchange), 2842-2870 (_emit_event fan-out)
- trigger: a tool that returns a large body: read_file on a big log, run_command output, fetch_url of a large page, a mail search with big bodies, an agent_smith CLI result.
- symptom: the whole string is (1) sent to the model on every remaining loop turn of this exchange (provider 413/400 or a very slow turn, then an error that leaves the huge message in history), (2) appended to the transcript and written into the JSONL ledger and messages.json, (3) streamed as a tool_result SSE event to the browser, again inside the final `done.transcript`, and to every /api/events client as agent_trace, and (4) run through DlpFilter.redact and the privacy gate regexes over the whole text on the dispatch thread. A 30 MB result makes the browser tab and the ledger heavy for the rest of the session.
- evidence: `tool_message = {"role": "tool", "tool_call_id": tool_call.id, "content": result_text}`
- fix: cap result_text centrally (for example 60k chars for the model copy and 20k for events and ledger) with a "[truncated: N chars, saved to <file>]" marker and the full text written once to a workspace file; keep per-tool caps as a second layer.
- test: register a tool returning "x" * 20_000_000, run one ask() with a stub client, and measure ledger size, SSE bytes and the request body size handed to the client.

### STRESS1-14 [medium] A five-minute research pipeline saves nothing until it finishes and any non-ValueError discards all of it
- where: dourmouse/research_pipeline/stages.py:498-520 (run_full_pipeline), dourmouse/research_pipeline_tools.py:200-203 (_save only after the run)
- trigger: research_run_pipeline on a 4 to 6 sub-question plan (T-5 measured 346 s) when the app is closed or killed, a laptop sleeps past the model deadline, or discover_sources/extract_evidence raises anything other than ValueError (ModelCallDeadlineExceeded, the RATE LIMITED RuntimeError, URLError, OSError).
- symptom: sources and claims gathered for the first sub-questions exist only on the in-memory record; the exception propagates (only extract's ValueError is caught per source), `_save(record)` is never reached, and the tool returns an error; the next call starts from the stored plan with zero claims and repeats all the fetching and model calls. The store's own docstring promises "a killed run resumes exactly where it stopped".
- evidence: `run_full_pipeline(record, registry, max_sources_per_sub_question=max_sources, skipped=skipped)` followed by `_save(record)` only after it returns
- fix: pass a checkpoint callback so the record is saved after every discover/extract; catch Exception per source and per sub-question (record the reason in `skipped`); skip sub-questions that already have claims on a re-run.
- test: stub discover/extract so sub-question 3 raises RuntimeError, call the tool, then load the stored record and assert claims from sub-questions 1 and 2 are present (currently they are not).

### STRESS1-15 [medium] Research document cache never expires, one bad file makes a URL permanently unfetchable, and a full disk loses a fetched page
- where: dourmouse/research_pipeline/acquire.py:240-249 and 265-275 (lookup, load_cached), 292-296 (cache hit returns), 231-238 (read_raw raises on hash mismatch), 338 (cache.put after the download)
- trigger: (a) asking about a live page again days later; (b) a blob or by_url index file damaged (partial restore, disk error); (c) disk full while fetching.
- symptom: (a) `fetched_at` is stored but never compared, so any URL fetched once is served from the raw cache forever and "latest" questions get days-old evidence with a fresh-looking retrieved_at on the claim; (b) `json.loads` of the index or `read_raw`'s ValueError escapes fetch_document, which never falls through to the network, so that URL fails every time with "cached document ... is corrupt" until someone deletes files by hand; (c) cache.put raises ENOSPC after the page was already downloaded and decoded, so the fetch fails and the work is lost. The cache also has no size limit.
- evidence: `cached = load_cached(url, cache)` / `if cached is not None: return cached`
- fix: treat entries older than a TTL (for example 6 h, shorter for news hosts) as misses; wrap the cache read in try/except and fall back to a network fetch (and rename the bad file aside); wrap put() so a write failure still returns the document; add an LRU or size cap for the raw directory.
- test: write garbage into by_url/<hash>.json and call fetch_document with a stub opener (expect network fetch, currently JSONDecodeError); set fetched_at to 7 days ago and assert a refetch; point the cache at a full tmpfs.

### STRESS1-16 [medium] With a full disk the scheduler re-runs the same due schedule every 15 seconds
- where: dourmouse/schedules.py:302-309 (_tick_once: _run_one, then mark_run), 223-236 (_save)
- trigger: disk full, or workspace/schedules.jsonl on a volume that went read-only, with any due schedule (for example a daily briefing or web check).
- symptom: the tool runs, then `mark_run` calls `_save`, which raises ENOSPC; the exception leaves _tick_once before the tracker event and bus post and before the remaining entries, `last_run` is never updated, and the loop (P5-47 keeps it alive) tries again 15 s later: the same tool executes every tick for as long as the disk stays full, spending network and model quota and repeating any side effect, while the other schedules after it in the file are starved. The same happens whenever the file is momentarily unwritable.
- evidence: `result = self._run_one(entry)` then `self._store.mark_run(entry["id"], at=now)` with no guard
- fix: record the attempt in memory first (a dict of schedule id to last attempted time) and skip entries attempted within their interval even when the save fails; catch OSError around mark_run and raise one alert; mark_run before running for at-most-once, or after with the in-memory guard for at-least-once.
- test: SchedulerRunner with a fake store whose mark_run raises OSError and an injected clock; call _tick_once five times and count tool invocations (currently 5).

### STRESS1-17 [low] The scheduler, the goal runtime and the sentry each run user work inline on one thread with no watchdog, so one hung tool stops every schedule or goal
- where: dourmouse/schedules.py:273-277 (_run_one inline), dourmouse/goal_runtime.py:226-230 and 163-169 (tasks run serially in tick), dourmouse/security/sentry.py:884-890
- trigger: a scheduled or goal task whose tool blocks without a timeout (a CLI call, a network call without a deadline, a browser action waiting on an owner who never answers, an approval wait).
- symptom: the model-call deadline (240 s) does not cover tool handlers, so the single worker thread never returns; every other schedule/goal silently stops running until the app is restarted, and nothing in the UI says the runner is wedged (running is still True).
- evidence: `report = self._execute_via_dispatch(goal, task, approved_prompts)` called directly on the runtime thread
- fix: run each task on a worker with a deadline (future.result(timeout)) and mark it failed/retrying on expiry; expose last_tick_at and "stuck for N s" in /api/standing or /api/goals so the UI can show a wedged runner.
- test: schedule a stub tool that sleeps forever plus a second quick schedule; assert the second still fires within two ticks.

### STRESS1-18 [low] A corrupt line in schedules.jsonl is dropped for good the next time anything is saved
- where: dourmouse/schedules.py:209-221 (_load skips bad lines), 223-236 (_save rewrites only what was loaded)
- trigger: a torn write or hand edit leaves one bad line; then the runner's mark_run, a toggle, or a new schedule saves.
- symptom: the bad line (which may be a real schedule) vanishes from the file with no message, no set-aside copy, and the user's routine silently stops existing. `_load` also lets UnicodeDecodeError/OSError escape.
- evidence: `continue  # a corrupt line never breaks the store (Rule 2.2)`
- fix: use state_file.set_aside-style behaviour: keep unreadable lines in `schedules.jsonl.corrupt-<stamp>` and raise one alert.
- test: write one valid and one garbage line, call mark_run, and diff the file.

### STRESS1-19 [medium] Speech-to-text holds a global lock across a multi-GB model download and the whole transcription; failure is never cached; TTS reloads the voice on every call
- where: dourmouse/voice.py (_stt_lock around _load_whisper and model.transcribe in speech_to_text), (_piper_speak: download_voice and PiperVoice.load on every call)
- trigger: first use of voice (large-v3-turbo downloads from Hugging Face), voice used offline, the voice screen and hands-free used at once, or a long recording (the upload cap is 50 MB of audio).
- symptom: every other STT request, including hands-free, waits on the RLock for the entire download or transcription with no deadline (the request thread's 60 s socket timeout does not apply while it is computing), so the mic button appears dead; offline, `_load_whisper` fails and is retried (another download attempt) on every request instead of being remembered for a while; each spoken reply reloads the ONNX voice from disk (and calls download_voice, which may touch the network) before synthesising, adding latency and an offline failure mode for a "fully local" feature.
- evidence: `with _stt_lock:\n        model = _load_whisper()`
- fix: load the model in a background thread at startup or first status call and report "loading" via voice_status; cache a load failure for a few minutes; keep the lock only around model.transcribe and bound audio length; cache the PiperVoice object and only call download_voice when the .onnx file is missing.
- test: patch faster_whisper.WhisperModel with a constructor that sleeps 30 s, fire two POST /api/speech calls, and time the second; with no network call speech_to_text five times and count download attempts.

### STRESS1-20 [medium] Hands-free fails silently, and a spoken turn can wait behind the chat lock and then speak a stale answer
- where: dourmouse/hands_free.py:_handle_turn (bare `return` on STT failure, empty text, TTS failure), dourmouse/webui.py:9047-9070 (_hands_free_dispatch holds server.session_lock)
- trigger: offline or model not downloaded (STT raises), TTS engine missing, or a typed turn / approval wait (STRESS1-2) holding session_lock when the wake word fires.
- symptom: the user says the wake word and a request and gets silence: no chime, no spoken "I could not hear that", no UI line, and /api/hands_free/status still says running. If the lock is held, the recorded turn blocks on `with server.session_lock` for minutes (self._busy also rejects every later wake word during that time) and then speaks an answer to a question the user has long since moved on from. A raw exception string is spoken on dispatch errors ("Something went wrong on that one: ...").
- evidence: `except Exception:  # noqa: BLE001 - a real STT failure ends this turn, never crashes the listener\n            return`
- fix: record the last failure reason in status (last_error, last_turn_at) and play a short error tone or spoken "I couldn't do that"; acquire session_lock with a timeout and say "I'm busy with another request" instead of queueing; speak a fixed phrase on error, never the exception text.
- test: HandsFreeController with a transcribe_fn that raises and a spy on play_fn (expect no call); hold server.session_lock in another thread and time _hands_free_dispatch.

### STRESS1-21 [medium] After a restart the main session may resume a project tab's conversation, and every tab session starts blank
- where: dourmouse/chat.py:131-153 (most_recent_session_file), 208-217 (session_file=None with a tool-bearing registry writes to the top-level sessions folder), dourmouse/webui.py:5590-5598 (per-tab ChatSession(session_file=None)), 9274 (serve_forever resumes most_recent_session_file())
- trigger: use two or more tabs/projects, restart the app (or crash).
- symptom: each tab_id gets a brand-new `session_<ts>_<rand>.jsonl` every process start, so a project's earlier conversation is never reloaded (its context is gone: "restarts from zero"), and the ledger with the newest mtime, which may belong to a project tab, is the one `serve_forever` hands to the shared HOME session, so HOME opens showing, and answering with, a project conversation.
- evidence: `return max(candidates, key=lambda p: p.stat().st_mtime)`
- fix: name per-tab ledgers by tab_id (`tab_<id>.jsonl`) so they resume deterministically, and make most_recent_session_file ignore tab ledgers (or keep a pointer file naming the main session).
- test: create sessions for tab "A" then HOME, touch A's file last, call most_recent_session_file; restart a server twice with the same tab_id and compare session_file.

### STRESS1-22 [low] A flapping network (wake from sleep) queues one full security scan per change
- where: dourmouse/webui.py:8936-8940 (a thread per on_change calling run_one_tick_now), dourmouse/security/sentry.py:run_one_tick_now (serialised by _scan_lock, no coalescing)
- trigger: lid open after sleep, roaming between access points, a VPN reconnect loop: NetworkWatcher reports several identity changes in a minute.
- symptom: each change starts a thread that waits on `_scan_lock` and then runs a complete scan (about 10 s of lsof, spctl, networksetup subprocesses, plus listeners: office-log row, SSE broadcast, analyst thread). A dozen changes mean minutes of back-to-back scanning at wake-up, a hot fan, and a stale-looking dashboard while the queue drains.
- evidence: `threading.Thread(target=sentry_rt.run_one_tick_now, args=("network_change",), daemon=True,`
- fix: coalesce: set a "scan requested" flag and let one worker run at most one extra scan after the current one; debounce changes by 5 to 10 s after wake.
- test: call the on_change callback 20 times with a stub scan taking 0.5 s and count scans (currently 20, want 2).

### STRESS1-23 [low] A fan-out that never reports every branch stays in ActivityTracker forever
- where: dourmouse/webui.py:773-783 (_broadcast_fanout deletes a run only when every branch phase is "result"), 1002-1005 (snapshot lists all remaining runs)
- trigger: STOP mid fan-out, a branch whose run raised before its result event, or the client leaving.
- symptom: the run_id stays in `_fanouts` for the life of the process; the ORCHESTRATION screen shows a ghost fan-out "in flight" on every load and the dict grows with each aborted run.
- evidence: `finished = bool(total) and len(branches) >= total and all(`
- fix: expire runs that have had no event for a few minutes in snapshot(), and clear a run on the parent's terminal done/error.
- test: feed ActivityTracker._record_fanout three events of a four-branch run, wait past the expiry, and check snapshot().

### STRESS1-24 [low] Every turn pays a synchronous backend probe (up to 3 s offline) and rereads the config file several times
- where: dourmouse/backend_fallback.py:47-92 (load_llm_config_with_fallback), called from dourmouse/dispatch.py:4292-4293 for every top-level turn; dourmouse/config.py:1094-1155 (_read_user_config_file parses .env on each call of every setting getter, including auto_approve_enabled on each gated call)
- trigger: no network (plane, cafe wifi that blocks), a dead cloud endpoint, or a long agentic turn with many gated tools.
- symptom: with the network down each message waits for the probe timeout before the real error appears ("thinking" for several seconds, then the error); the probe adds a request to the provider on every message when it is up; a turn with 30 tool calls reparses the dotenv file dozens of times.
- evidence: `if _probe_backend(cfg.base_url):` run per turn
- fix: cache the probe result for 30 to 60 s (failure cached shorter) and share across turns; cache parsed settings keyed by file mtime.
- test: patch _probe_backend to sleep 3 s, time 3 consecutive ask() calls with a stub client.

### STRESS1-25 [low] Two browser-tool users share one page with no model-to-model lock
- where: dourmouse/browser_agent.py:305-348 (_claim only arbitrates owner vs model), 644-662 (_call)
- trigger: delegate_parallel branches or a goal task plus a chat turn both using browser_open/browser_click.
- symptom: both drive the single `_PAGE`: one navigates while the other is mid-form, so clicks land on the wrong page and the model reports a confusing failure. The 60 s `_call` timeout then reports the stuck page to the wrong caller.
- evidence: `if _PAGE_MODE != "pane" or pane is None or tab is None:\n        return _NoClaim(page)`
- fix: an asyncio.Lock per page (or per agent id) held for the duration of `_acting`, with a clear "browser busy with <agent>" refusal after a short wait.
- test: submit two coroutines that goto different URLs via _call from two threads and assert they serialize.

PROGRESS: done dourmouse/chat.py, dourmouse/office_logger.py, dourmouse/state_file.py (no defect: atomic write and set-aside are sound; stray .tmp files after kill -9 are never cleaned, low), dourmouse/config.py, dourmouse/schedules.py, dourmouse/hands_free.py, dourmouse/voice.py, dourmouse/research_pipeline/*.py, dourmouse/goals.py and goal_runtime.py (skimmed for lifecycle and recovery; crash recovery exists), dourmouse/browser_agent.py (threading and _call only), dourmouse/security/sentry.py (SentryRuntime), dourmouse/dispatch.py (loop, budgets, timeouts, retries, streaming)

PROGRESS: done dourmouse/webui.py (read in full except: route bodies for /api/world/*, /api/atlas-lab/*, /api/spotify/*, /api/projects/*, the Google OAuth callback, the mobile pairing page, the orchestrator catalog and vision status were skimmed for threads, locks, caches, timeouts and large reads, not line by line)
NOTE: webui.py was being edited by another process during this read (line numbers moved by about 61 after line 4100 between two reads, and the ST-1 SSE change appeared). Line numbers above are from the first read of the file; function names are the stable anchors.

SUMMARY:
- findings: 25 (high 2, medium 14, low 9)
- high: STRESS1-1 (stalled /api/events client blocks agent threads; overlaps the in-progress ST-1 change), STRESS1-10 (disk full discards the answer and tears the ledger)
- medium: STRESS1-2, 3, 4, 5, 6, 11, 12, 13, 14, 15, 16, 19, 20, 21
- low: STRESS1-7, 8, 9, 17, 18, 22, 23, 24, 25
- areas checked and found sound: state_file.py (atomic write plus set-aside), JobTracker/AttentionQueue/ActivityTracker feed caps, _CONFIRM_DETAILS cleanup, model-call deadline and bounded retry with backoff, goal_runtime crash recovery, RunMarker, helper-session pruning, request body caps (64 MB) and upload name sandbox.
- not examined: wakeword.py, net_guard.py, mac_telemetry.py subprocess timeouts, memory_store internals beyond ingest_session_file, tray.py.

STRESS_PLAN:
1. Silent SSE subscriber | open /api/events with a raw socket that never reads (plus a second normal one), then run a scripted agent turn emitting 50k thinking_delta events of 1 KB | time to finish the turn, time between deltas seen by the healthy client, thread count, whether the dead client is ever unregistered (STRESS1-1)
2. Approval card abandoned | start a turn that calls a gated tool, close the SSE socket without answering, immediately POST a second /api/chat from another connection | time to first byte of the second request (expect about 300 s), lock owner thread stack via faulthandler dump (STRESS1-2)
3. Queue of abandoned prompts | POST 30 /api/chat requests with no tab_id against a stub model that takes 2 s, drop 25 sockets at once | number of model calls actually made, handler thread count, RSS, order of replies in the ledger (STRESS1-3)
4. Disk full during a turn | run the isolated server with DOURMOUSE_WORKSPACE on a small tmpfs or disk image (hdiutil 8 MB), fill it while a turn runs, then send two more turns | HTTP response text of the first turn, whether the answer was kept, state of the .jsonl tail, /api/session/current result afterwards, schedules re-fire count (STRESS1-10, 6, 16)
5. Long session soak | loop 1,000 /api/chat turns with a stub model returning 20 KB tool results, sample every 50 turns | per-turn latency, RSS, sizes of the .jsonl, .messages.json and office_log.db, time of /api/session/current and /api/sessions/recent (STRESS1-6, 11, 12, 13)
6. Streaming throughput with the office log on | one turn streaming 20k deltas through the real sink, with the DB on a slow volume and again with 4 parallel delegate branches | tokens per second seen by the browser, SQLite lock waits, agent_events row count, WAL size (STRESS1-12)
7. Upload and STT abuse | POST /api/upload with Content-Length 40 MB but send 10 MB and close; 20 parallel 45 MB uploads; two parallel POST /api/speech with a model constructor stubbed to sleep 30 s | resulting file size and response, RSS peak, thread count, STT second-request latency (STRESS1-4, 19)
8. Sleep and wake simulation | run the isolated server, SIGSTOP it for 10 minutes (or freeze the clock with libfaketime) then SIGCONT, with a schedule due and a network-change event fired 20 times | number of schedule runs, scans started, SSE reconnects, warmer fetch counts, sentry listener queue (STRESS1-7, 16, 22)
9. Offline first run | start with an empty workspace, no .env, no keys, network blocked (pfctl on a throwaway interface or HTTP(S)_PROXY to a black hole), open every screen and send three chats and one voice request | what each screen displays, per-request latency (3 s probe), error text, repeated retry rate of the Gmail/world warmers in the log, hands-free silence (STRESS1-7, 19, 20, 24)
10. Research pipeline kill test | run research_run_pipeline against a local stub web server and stub model, kill the process at 60% and at an injected RuntimeError in sub-question 3, restart and rerun; also corrupt one by_url index file and age another by 7 days | claims preserved after the kill, fetches repeated, behaviour on the corrupt and stale cache entries (STRESS1-14, 15)
