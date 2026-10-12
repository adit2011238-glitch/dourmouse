# PRIV design note: privacy under a cloud-only rule (2026-10-12)

What was built for the approved PRIV design (FIX_PLAN_2026-10-06.md, section "PRIV"), and every decision taken where the plan left room. Plain words first, code names in brackets.

## The honest limit, said up front

Every model Dourmouse uses runs in the cloud. Anything the model sees leaves this Mac. The gate below makes Dourmouse send less, swap identifiers for placeholders, ask before the sensitive kinds, refuse the things that must never leave, and keep a record. It cannot make a cloud model private, and it cannot remove names or free text reliably. The SETTINGS screen says this in one sentence.

## Where the gate sits

One module, `dourmouse/privacy.py`, holds the tier map, the consent store, the Private mode state, the placeholder maps and the egress ledger. `dourmouse/governance.py` holds the identifier patterns (`PiiRedactor`, next to the existing `DlpFilter`). Three places call the gate:

1. **The dispatch loop** (`dispatch._run_dispatch_loop`), right where a tool result is appended to the message list, after the existing secret scrub. Every tool result of every backend that uses the loop (Ollama Cloud, Gemini, NVIDIA and other OpenAI-compatible endpoints) passes here, including nested `delegate_task`, `delegate_parallel` and `delegate_to_models` runs, which share the turn's privacy state through a context variable.
2. **The MCP bridge** (`mcp_bridge._handle_tools_call`). With Claude front mode, Claude calls Dourmouse's tools through this separate process and the results go straight to Anthropic, never through the loop. The bridge applies the same gate (tiers, T3 refusal, stored consent, Private mode, placeholders). It cannot show a consent card (no window channel), so an undecided T2 source is refused with the NOT SENT note and the owner decides in SETTINGS or in a normal chat.
3. **The egress recorders** at the point each backend builds its request (see "Egress ledger").

## Tiers

| Tier | Meaning | What happens |
|---|---|---|
| T0 public | web pages, search, news, public market data, world monitor | sent after placeholder swap; never asks |
| T1 personal | calendar entries, tasks, goals, file and window names, status replies of actions, files inside the workspace, files the owner attached | sent after placeholder swap; never asks |
| T2 sensitive | mail, memory vault, documents and Drive content, finance accounts, contacts, files outside the workspace and command output, screen, app and browser page content | needs consent for its source; removed in Private mode; cut when oversized; placeholder swap |
| T3 never | password vault, Keychain items, .env files and keys, SSH keys, browser cookies and tokens, the owner secret, card and bank numbers | never sent; the whole result is replaced by a NOT SENT note, or (cards and IBANs inside other text) the number is replaced by a placeholder that is never restored into a tool call |

### Tier map of tools (decided per tool; an agent default covers anything unlisted)

Sensitive agents (the work list's set: mail, comms, messenger, docs, memory, companion, admin_ops, system, tasks, scheduling, compute, browser, panel_control, freebuff, design_3d, apps, app_driver, markets, forex, mt5, t212) default to T2 with the source named below. Tools those agents own that only return a status or a list of names are T1, because the plan names calendar entries, file names, goals and the owner's own notes as T1. Every other agent defaults to T1.

| Source (consent switch) | T2 tools |
|---|---|
| Mail | read_inbox, gmail_search, gmail_read |
| Memory | recall, memory_search_semantic, search_vault, read_note, daily_digest, query_shared_memory |
| Documents and Drive | drive_read, drive_download, sheets_read, study_read_file, device_wiki_get, extract_receipt |
| Finance | t212_account, t212_positions, t212_portfolio, forex_ibkr, forex_paper, forex_report, forex_inventory, mt5_status |
| Contacts | none today (no tool reads contacts yet; the switch is kept so a future contacts tool is gated from day one, and the screen says so) |
| Files outside the workspace | read_path, extract_pdf (when the path is outside the workspace), run_command, run_privileged_command, run_python_host, compute_run_python, freebuff_read_thread, freebuff_notes, freebuff_dispatch |
| Apps, screen and browser pages (added: the plan's five sources plus the work list's sixth do not cover window contents and logged-in pages) | browser_snapshot, browser_extract, browser_screenshot, browser_click, browser_type, browser_fill, browser_fill_form, browser_select, browser_press, browser_submit, browser_wait, browser_back, browser_media, app_driver_snapshot and the other app_driver actions that return a snapshot, clipboard_get, security_browser_history |

T0 by name: web_search, fetch_url, open_url, research_* (rnd and evidence pipeline), research_mesh_*, news_*, worldmonitor_*, world_pulse, world_pulse_details, world_brief, world_correlations, globe_control, stock_quote, market_movers, mt5_quote, mt5_universe, forex_events, forex_strategy, generate_image, spotify_search.

T1 by name (inside sensitive agents): list_calendar_events, propose_time_slots, create_calendar_event, list_tasks, add_task, complete_task, schedule_recurring, list_schedules, cancel_schedule, list_files, list_path, librarian_*, delete_file, system_info, check_connections, repo_map, write_path and the apply/undo/delete tools, open_path, open_file_preview, player_*, clipboard_set, read_upload (a file the owner attached on purpose), list_running_apps, list_app_windows, activate_app, quit_app, send_app_keystrokes, press_app_key, click_app_menu_item, app_driver_status, app_driver_stop, every panel_control and design_3d tool, freebuff_status, freebuff_projects, freebuff_threads, freebuff_skills, freebuff_changes, compute_job_status, compute_jobs, compute_environment, open_browser_pane, browser_open, browser_pane_show, browser_pane_hide, browser_signin, browser_creds_list (site and user names only, never passwords), browser_creds_store, browser_creds_forget, remember, write_note, draft_message, send_draft, send_message, read_agent_inbox, drive_search (names), every write and send action of mail and docs (their result is a status line), email_identity_status, t212_status, t212_order, mt5_order, delegate_task, delegate_parallel, delegate_to_models (a nested run's own results already passed the gate), query_desktop_vault (a general-knowledge reference corpus).

Path rule: read_path, extract_pdf, extract_receipt, study_read_file are T1 when the path is inside the workspace folder and T2 "Files outside the workspace" (or their named source) when it is outside.

### T3 detection (never sent)

* **Paths.** Any path-like argument (path, file, filename, src, dest, target, cwd, and every token of a command line) that `system_access._is_sensitive` already treats as a credential (.ssh, .aws, .gnupg, Keychains, .env*, *.pem, *.key, id_rsa and friends, .netrc, .git-credentials, browser Cookies and Login Data, local storage folders, dourmouse_auth.db, browser_creds.json, gcloud and gh config). The existing deny list is reused, not copied, and extended with Keychain and cookie names in the gate's own check (`.keychain-db`, `Cookies.binarycookies`, `cookies.sqlite`, `key4.db`, `logins.json`).
* **Commands.** `security find-generic-password`, `find-internet-password`, `dump-keychain`, `export` (the macOS Keychain tool) in a command argument.
* **Content.** A private key block (the existing DLP pattern only replaced the header line, so the key body still went out: fixed here by withholding the whole result), a Netscape cookie file header, Keychain dump output, the owner secret value (registered by the server at start, matched exactly).

## Placeholders (reversible redaction, PRIV-2)

Format: `[EMAIL_1]`, `[PHONE_1]`, `[CARD_1]`, `[IBAN_1]`, `[ID_1]` (government or tax number), `[ADDRESS_1]`. Numbering is per kind, per chat. The same value always gets the same placeholder in one chat.

Patterns: card numbers of 13 to 19 digits (spaces or dashes allowed) that pass the Luhn check; IBANs that pass the mod-97 check; US SSN, UK National Insurance, India PAN and Aadhaar shapes, and any value after a label such as passport, tax ID, national ID, driving licence; phone numbers in international form (+ and 8 to 15 digits) and in the common national shapes (123-456-7890, (123) 456-7890, 020 7946 0958); street addresses (a number, one to four capitalised words, a street word such as Street, Road, Avenue, Lane); email addresses except the owner's own (the Gmail account Dourmouse uses, its +dourmouse alias, DOURMOUSE_EMAIL_ADDRESS and DOURMOUSE_OWNER_EMAILS).

What is swapped: every tool result (all tiers) before it reaches the model. The owner's own typed message is not swapped: the owner chose to send it, and swapping it would make Claude's own file tools write `[EMAIL_1]` into a file the owner asked it to edit.

Where values come back: the reply the owner reads (including the live stream, with a short hold-back so a placeholder split across two chunks is still restored), and the arguments of a tool call before it runs (so "reply to [EMAIL_1]" reaches the real address). The message history the model sees keeps the placeholders. Card and IBAN placeholders are restored only in the owner's reply text, never into a tool call: a card number never leaves through the model's actions either.

The map lives in memory only, one per chat (the tab id; a CLI or scheduled run uses its session stem), at most 64 chats and 2,000 values each, and is gone when the server stops. For the Claude CLI front mode the bridge process needs the same map: it is handed over in a temp file readable only by the owner (0600), created for one call and deleted after it, the same mechanism the bridge already uses for its tool-call log. That file is on this Mac for the length of one call; the plan's "in memory" is otherwise kept.

## Consent and Private mode data model

File: `privacy.json` in the settings folder (`config.user_config_dir()`), written through `state_file.atomic_write_text`, read through `state_file.load_json_or_aside` (a corrupt file is kept aside and every source counts as "ask", never as "allow").

```json
{
  "version": 1,
  "consent": {"mail": "allow", "memory": "deny"},
  "private_mode": false,
  "private_chats": {"<tab id>": "2026-10-12T10:00:00Z"},
  "providers": {"ollama_cloud": {"verified": true, "at": "2026-10-12T10:00:00Z"}}
}
```

* `consent[source]` is `allow`, `deny`, or absent (ask). Ask once: the first T2 result from an undecided source raises one consent card through the chat's own confirmation channel (`WebConfirmationGate.ask_owner`). The card is one sentence: what is sent and to which provider. The answer is stored for that source. Auto approve never answers a consent card (it is a privacy choice, not an action), and a card nobody answered (no window, hands-free voice, timeout) stores nothing.
* A plain confirmation callable (the terminal CLI, a background goal) can approve a source for the rest of that one request; that answer is not stored.
* `deny` and an unanswerable ask both turn the result into: `NOT SENT: the owner has not allowed <source> to be shared with a cloud model; ask them to turn it on in SETTINGS, PRIVACY.`
* Private mode is on for a chat when `private_mode` (the global switch) is on or the chat's tab id is in `private_chats` (at most 500 kept, oldest dropped). In Private mode every T2 tool is removed from the tool list of that chat's turns and any T2 result that still arrives (a delegated run, the bridge) is refused.
* Only the owner changes any of it: the routes are `POST /api/os/settings/privacy/consent`, `/private_mode`, `/chat`, `/provider`, all owner-only in `request_guard.OWNER_ONLY_POST` (and under the existing owner-only `/api/os/settings/` prefix). No tool writes this file.

## Minimise

A T2 result over 12,000 characters is cut to the paragraphs that share the most words with the owner's request, in their original order, with a note saying how much was cut and how to ask for a specific part. Mail tools already return subjects and snippets first; bodies are one message per `gmail_read`. A generic "bulk read asks" prompt was not added: the size cut covers it without a second card, which the owner asked to avoid.

## Provider table

SETTINGS lists every backend Dourmouse can call, whether it is configured, which company receives the data, and a checkbox the owner ticks after checking that account's retention and training settings: Ollama Cloud (Ollama), Gemini (Google AI Studio), Claude CLI sign-in (Anthropic), Codex CLI (OpenAI), NVIDIA NIM (NVIDIA), FreeLLMAPI or OmniRoute (a local proxy that forwards to whatever providers it is set up with). The code cannot verify retention; the checkbox records the owner's own check with a date.

## Egress ledger (PRIV-4)

`<workspace>/privacy/egress.jsonl`, one JSON line per request to a model, capped at 1 MB and rotated to `.1`, `.2`, `.3`. A row holds: time, provider, backend, model, an opaque short id of the chat, tier counts of this turn's tool results (T0, T1, T2, T3 withheld), source names, the request size in bytes and message count, redactions by kind, whether Private mode was on, and a SHA-256 of the request. Never content.

Recorded where each backend builds its request, so a call made outside a chat turn (a summary, the personality profile, the security analyst) is recorded too: `OllamaNativeClient` (Ollama Cloud), `gemini_backend.call_gemini` and `stream_gemini` (Gemini), `code_backends.run_code_task` and `stream_claude` (Claude CLI, Codex CLI, OpenAI-compatible coding backends; what Dourmouse hands the CLI is recorded), and the dispatch loop's model call when the client is a plain OpenAI-compatible client (NVIDIA, FreeLLMAPI, OmniRoute). Read with `GET /api/os/privacy/egress`, shown in SECURITY.

## Removed false promises (PRIV-3, A-13, P5-31, P2-21)

`force_local`, `skip_persisted_orchestrator_choice`, `_LOCAL_ONLY_AGENTS` and `_config_for_agent_model` are gone. `model_delegation` now routes a sensitive agent's task to the gated tool loop on the configured cloud backend (ROSTER) and a public agent's task to Gemini; a model-supplied `model: "gemini"` is honoured only for an agent that is not sensitive, and no task hops to Gemini once the request has read a T2 source (P5-31); the Gemini prompt is swapped for placeholders and recorded. The split mode's internal "local" verdict is now called "default" (the configured backend). Delegated runs share the turn's gate, so a nested `mail` run is gated the same way (P2-21). Prompts, tool descriptions and UI lines that said private content stays local for model use are reworded; storage claims (the vault file never leaves this Mac) stay and are tested.

## Not done, said plainly

* Names and free text are not removed.
* Claude CLI and Codex CLI have their own tools (Bash, file read, web fetch). What they read with those tools goes to Anthropic or OpenAI without passing this gate; only what Dourmouse hands them and what they read through the bridge is gated.
* A session rebuilt from the ledger after a corrupt state snapshot carries the owner-facing replies, which have real values, back into the model's history.

## Added during the build (2026-10-12)

* **The model call worker lost the caller's context** (H-PRIV-1): `_call_with_retry` ran each model call in a worker thread started without `contextvars.copy_context()`, so the privacy turn (and the project scope, for Claude front mode) was empty inside the client. Fixed; the ledger rows and the bridge handoff depend on it.
* **A private key's body went out** (H-PRIV-2): the secret scrubber replaced only the header line of a key block. The gate now withholds the whole result.
* **Ollama Cloud with no key still sent the request** (H-PRIV-3): found live in an isolated server. The security analyst's findings went to ollama.com with no key, only to come back 401. A cloud request with no key is now refused before anything is sent.
* **Recalled past turns carried real identifiers** (H-PRIV-4): replies show the owner real values and the learn store ingests them; its recall block is now swapped for placeholders like any T1 result. Past turns stay T1 (no consent needed); global memory and shared memory added without a tool call need Memory consent and are skipped in Private mode, without asking.
* Memory added to a Claude or Codex task (`_inject_shared_context`) follows the same rule.
