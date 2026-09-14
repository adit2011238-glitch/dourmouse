# Dourmouse, commercial-grade: problem, solution, plan

Status: proposal, not started. This document lives on its own branch
(`commercial-v1`) in its own checkout (`~/dourmouse-commercial`), created
specifically so the current `~/dourmouse-recon` checkout and the pinned
desktop app reading from it are never touched by this work. Nothing
below is built yet. Real implementation only starts after this document
is reviewed and approved.

## 1. Problem statement

### 1.1 What Dourmouse already is

A genuinely capable personal AI orchestrator, not a chatbot: it drives
a real browser, reads and sends real email, manages real calendar and
tasks, codes through real Claude Code/Codex CLI sessions, monitors
markets and news continuously, and routes work across several LLM
backends (Claude, Ollama, NVIDIA NIM, Gemini) with real fallback and
retry. It runs as a real desktop app the owner launches like any other
program, not a web page they visit.

### 1.2 What it structurally is not, today

Every one of these is a real, current fact about this codebase, not a
guess:

- **One user, one machine, one set of secrets.** API keys and settings
  live in a single local `.env` file
  (`~/Library/Application Support/Dourmouse/.env` on macOS). There is no
  concept of "which user" anywhere in the code — one Dourmouse process
  serves exactly one person, the one who installed it.
- **No accounts, no auth.** The local HTTP server
  (`dourmouse/webui.py`) binds to `127.0.0.1` and trusts whatever hits
  it. That is the correct, safe design for "software the owner runs on
  their own machine" and the wrong one for "software a stranger signs
  up for."
- **No multi-tenant isolation.** State (chat history, projects, memory,
  the credential vault) is one shared set of files per machine. Two
  different paying customers cannot share a running Dourmouse process
  today without seeing each other's data.
- **The owner's own API keys pay for everything.** The real multi-account
  fallback system already built (`model_router.AccountPool`,
  generalized this session to Ollama Cloud alongside NVIDIA) rotates
  between *the owner's own* keys on rate-limit — it has no concept of
  metering cost per paying end user.
- **OS-level power, single-operator trust model.** Tools like
  `run_command`, `run_privileged_command`, and real browser automation
  assume the person who can reach the server is the same person who
  configured it. That assumption is reasonable for a local app and
  becomes a real security question the moment a second, untrusted party
  is involved.

### 1.3 The actual market gap

Consumer "AI assistant" products today are mostly one of two things:
narrow single-purpose bots (a browser extension that summarizes pages,
a scheduling bot), or general chat interfaces with no real, ongoing
tool-calling power over the user's own accounts and machine. Nothing
polished sits where Dourmouse already technically sits — an assistant
that actually orchestrates a person's real email, calendar, browser,
code, and market/news feeds, continuously, with multiple LLM backends
underneath for reliability. That gap is real. Closing it commercially
requires solving the five bullets above; it does not require
reinventing the orchestration engine, which already works.

## 2. Proposed solution

Two genuinely different packaging paths exist. They are not the same
size of project, and picking the wrong one first would waste real time.

### Path A — self-hosted "Pro" download (recommended starting point)

Keep today's core architecture: one user, one machine, their own keys.
Sell polish, safety, and support, not a re-architecture:

- A real first-run setup wizard: guided API key entry per provider, a
  live test call against each key before it's saved (fail fast and
  honestly, the same Rule 2.1/2.2 discipline the codebase already
  holds every tool to), sensible defaults so a non-technical buyer
  doesn't need to hand-edit a `.env` file.
- A real update mechanism, so a purchased copy can receive fixes
  without the buyer reinstalling by hand.
- A real "what this app can do" onboarding screen, shown once, listing
  every real capability category (reads your email, can delete files,
  can run shell commands, can browse as you) *before* first use — this
  matters far more for a buyer who is not the developer.
- Auto-approve (the "skip confirmations" toggle shipped this session)
  defaults OFF for every new install, no exception, and its own copy
  says plainly what turning it on means.
- License-key gating if sold as paid software: a real, honest check
  (a signed license file validated locally), not always-online DRM that
  breaks the app when a license server is unreachable.
- Packaging: a signed, notarized macOS build and a real Windows build,
  reusing this session's own Electron packaging work as the shell
  rather than building a second one.

This path never needs multi-tenant auth, hosted compute, or a security
review of exposing OS-level tools to strangers over a network, because
nothing changes about *who* can reach the running app. It is a real,
shippable v1.

### Path B — hosted, multi-tenant SaaS (a real, later, bigger project)

Only worth starting once Path A has shown real demand. Genuinely larger
scope, listed honestly rather than hand-waved:

- Real user accounts via an established auth provider (not homegrown
  password handling — this repo's own standing rule against handling
  credentials in plain text applies doubly to a product that stores
  OTHER people's).
- Per-user encrypted secret storage, replacing the single shared `.env`
  file entirely.
- Per-tenant compute isolation — a hosted Dourmouse cannot be one long
  -lived shared Python process the way the desktop app is; each
  tenant's session needs a real sandbox boundary.
- Usage-based billing wired into the existing `AccountPool` rotation
  design, repurposed from "fall back between the owner's own keys" to
  "meter and bill per paying tenant."
- A real security audit specifically covering what changes when
  `run_command`/browser automation/email-send are reachable by a
  paying stranger instead of the machine's owner — this is a different
  threat model, not a bigger version of the same one, and deserves a
  dedicated review before any hosted OS-level tool ships.

## 3. Phased implementation plan

**Phase 0 — decision, not code.** Confirm Path A vs Path B before
anything below starts. This document recommends A first.

**Phase 1 (Path A).** First-run wizard with live key validation, update
mechanism, packaging (signed macOS + Windows builds via the existing
Electron shell), license-key gating if sold as paid software.

**Phase 2 (Path A).** Safety and support polish for a non-developer
owner: the capability-disclosure onboarding screen, auto-approve
defaulting off with clear copy, an opt-in crash/error reporting path so
the vendor can support a real customer without them manually pasting
logs, real end-user documentation.

**Phase 3 (Path B, only if Phase 0 chooses it, or is revisited later).**
Real accounts/auth, per-user secret vault, per-tenant isolation, metered
billing on top of the existing multi-key rotation design, and the
dedicated security audit named above — completed and signed off before
any hosted OS-level tool is exposed to a paying tenant.

**Phase 4 (either path).** Monetization mechanics: pricing model
(one-time license vs. subscription vs. usage-metered) and exactly what
infrastructure each requires, decided once Phase 1/2 (or 3) results are
in hand rather than guessed up front.

## 4. Working arrangement for this plan

- This branch (`commercial-v1`) and this checkout
  (`~/dourmouse-commercial`) are where any approved work happens.
  `~/dourmouse-recon` and the pinned desktop app are not touched by it.
- Work is committed here as it progresses ("save as you go") but is
  never pushed to a remote and never merged back into
  `recon-2026-09-11` without being asked for separately.
- Nothing past this document starts until it is reviewed and a
  direction (Path A, Path B, or a modified version of either) is
  confirmed.
