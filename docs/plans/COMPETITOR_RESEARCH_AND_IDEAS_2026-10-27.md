# What similar apps do, and what Dourmouse should borrow (2026-10-27)

You asked me to research existing apps like the Grok app for inspiration. I searched twelve topics and read four pages in full. **A warning on the evidence:** almost everything online about these products is press, blogs and vendor marketing, and the vendors' own numbers (for example voice speed) are claims, not measurements. I mark that below. Where two sources disagreed, I say so instead of picking one.

## 1. What each product does that is worth noticing

### Grok (xAI)
- **Automations** (launched 16 July 2026): describe a task once, then run it on a schedule or when an email matches a filter. Examples: a "Morning Brief" of unread email, calendar conflicts and deadlines; daily reports; watching an inbox filter. Results go to email, an app notification, or a run history. Built by hand or **from a chat prompt**; a page of templates helps new users. Schedules are free; email triggers need the paid plan. [source](https://blockchain.news/news/grok-automations-scheduled-tasks)
- **Voice**: xAI's own claim is about 0.7 seconds to answer; one comparison page gives a different benchmark where Gemini Live is fastest. I would not trust either number. Selectable voice personalities. Companions are 3D animated characters that react to your voice (the sources disagree on price, and this part is not relevant to Dourmouse). [source](https://tech-insider.org/ie/?p=941)
- Also launched: a coding tool on every plan, and a car integration (place calls, play music).

### Claude Cowork and computer use (Anthropic)
- **Dispatch**: message the assistant from your phone, and it works on your desktop with your files, connectors and apps. You get a **push notification when a task finishes or needs your go-ahead**, not a stream of steps.
- **Two modes**: "ask before acting" (approval at each step) or "act without asking" (runs the whole plan).
- **Scheduled tasks** and **memory you can view, edit and delete**. Computer use clicks and types in desktop apps directly.
- Their own page admits limits: the computer must stay awake and the app open, and there is **one continuous thread**, no way to run several. They also warn that a phishing link met during a task can cascade into hard-to-undo actions. [source](https://support.claude.com/en/articles/13947068)

### ChatGPT Atlas and agent mode (OpenAI)
- Agent mode **asks before many important actions**, can be paused or taken over at any time, pauses on sensitive sites such as banks, and cannot run code, install extensions or read local files. A home page remembers what you explored and suggests next steps; "browser memories" are user-controlled. Voice: sources say the new voice mode is full-duplex (it listens while speaking, so interruptions feel natural). The searches did **not** confirm a feature called "Pulse". [source](https://aiagentindex.mit.edu/2025/chatgpt-atlas)

### Gemini in Chrome (Google)
- **Skills**: save a prompt you like, give it a name and an emoji, call it later by typing `/`. A library of 50+ ready ones; custom ones sync across your devices. Runs on the current tab or up to **ten chosen tabs** at once.
- **Auto-browse**: carries out a goal, fills details from the password manager, and **stops before the final click** so you confirm. Paid plans, US only, preview.
- **A warning for us**: a March 2026 report described a high-severity flaw in the Gemini side panel where a low-privilege browser extension could reach local files, screenshots and the camera. Fix status was not confirmed in the results. [source](https://pasqualepillitteri.it/en/news/3256/gemini-in-chrome-skills-how-they-work-countries-availability), [report](https://www.malwarebytes.com/ru/blog/news/2026/03/chrome-flaw-let-extensions-hijack-geminis-camera-mic-and-file-access)

### Perplexity Comet, Edge Copilot Mode, Dia
- Comet: a sidebar that summarises pages and manages tabs, an agent toggle, Gmail and Calendar integration, cited research reports, Chrome extensions. Reviewers flag prompt-injection risk. [source](https://techcrunch.com/2025/07/09/perplexity-launches-comet-an-ai-powered-web-browser/)
- Edge Copilot Mode: groups your browsing into **topic projects you can resume**. One hands-on test found it said it had deleted an email when it had not. That is the most important line in this whole section for us: **an agent that reports success without checking is worse than one that fails**. [source](https://radar.firstaimovers.com/edge-copilot-vs-chatgpt-atlas-2025-ai-browser-battle.md)
- Dia (from the makers of Arc): the same "Skills" idea. [source](https://efficient.app/compare/dia-vs-edge)

### OpenClaw (open source, self-hosted)
- One always-on gateway connects your own agent to **WhatsApp, Telegram, Slack, Discord, Signal and iMessage**, so you talk to it from any chat app. 100+ ready-made skills, a public skills registry (ClawHub), persistent memory, hosted or local models, your data on your machine. Sources disagree on its star count and governance. The public skills registry is also a supply-chain risk by design. [source](https://www.digitalocean.com/resources/article/what-is-openclaw)

### Raycast
- Keyboard-first launcher with a model choice (Claude, GPT and others), extensions and custom commands; it wins with people who live on shortcuts. [source](https://www.arahi.ai/blog/best-ai-assistant-for-mac)

## 2. Where Dourmouse stands against them

| Idea | Dourmouse today | Gap |
|---|---|---|
| Scheduled tasks | TIMETABLE routines exist (a runner, a store) | Can only run a **tool** on a clock; no email/file/event triggers, no templates, no run history, no results delivery |
| Morning brief | A daily briefing exists in code | Fires only about 3 percent of days (an earlier finding, fixed), has no screen, and cannot be a scheduled *question* |
| Phone to desktop | A pairing script and a phone-usable web page exist (`mobile_link.py`) | No push notification, no approvals from the phone, no chat-app gateway |
| Notification when done or needs approval | Native macOS notifications exist | Nothing tells you an approval is waiting when you are on another screen (USE1-34) |
| Ask-before-acting vs act-without-asking | Every gated action asks; one global "auto-approve" switch | No per-task or per-app dial (planned as session grants) |
| Memory you can see and edit | A memory store and search routes exist | No screen shows or edits what it remembers |
| Saved prompts as skills (`/name`) | AGENTSMITH builds tools; Command K launches screens | No saved-prompt skills, no `/` menu in chat |
| Many tabs as context | BROWSER chat reads the active tab | Cannot say "compare these three tabs" |
| Pause on sensitive sites | Click gate on submit-like buttons; privacy tiers | No rule that a banking or health page is read-only for the agent |
| Honest completion | Goals double-check; tools report REFUSED/NOT CONFIGURED | No "verified: yes / not checked" on every action (the Edge failure above) |
| Voice that interrupts and streams | Push-to-talk and a command parser | The voice plan (2026-10-26) covers it |
| System-wide hotkey | Command K inside the window | **No global shortcut anywhere** (none in `electron/`) |
| Multiple threads at once | **Yes**: separate threads per screen, many agents, Command Center | This is a real advantage over Cowork |
| Show what left the machine | **Yes**: an egress ledger with no content, a privacy gate | Unique; none of the products above offer it |
| Honest about being wrong | Grounded-mode warnings, "NO VERIFIED CLAIMS" | Keep and extend |

## 3. Ideas, ranked by value for effort

Effort: S under a day, M one to three days, L a week or more.

1. **Automations with triggers, templates and run history (M).** Copy Grok's shape: create from a chat sentence ("every weekday at 8 tell me about unread mail and today's calendar"), choose a trigger (clock, new mail matching a filter, a file appears in Downloads, a security finding, a calendar event), results go to a run history and a notification. Start with five templates (morning brief, daily security check, downloads tidy-up, weekly research digest, bill reminder). *Builds on TIMETABLE; fixes USE1-10 and CAP1-6, 17, 23 together.*
2. **Approvals and "done" as notifications (S).** Copy Cowork's rule: you hear about a task when it **finishes or needs a go-ahead**, nothing in between. A toast with a GO button, a badge on the sidebar, and a macOS notification when the window is hidden. (USE1-34.)
3. **A global hotkey that opens a small Ask panel from any app (S to M).** The Raycast habit. Type or speak a question without switching windows; the answer streams; a button sends it to HOME. Needs a global shortcut in Electron plus a small always-on-top window. *The cheapest way to make Dourmouse feel like part of the Mac.*
4. **Skills: saved prompts called with `/` (S to M).** Copy Gemini and Dia. "Save as skill" on any good prompt, a name and emoji, a `/` menu in every chat, and run it on the current tab, the current file, or the clipboard. Ship with ten useful ones (summarise this page, explain this error, draft a reply, extract dates into my calendar). *Keep them local files you can read; do not build a public registry (see section 4).*
5. **A Memory screen (M).** What it remembers about you, where each fact came from, edit and delete, and "why did you say that" linking a reply to the memories it used. The routes exist and nothing shows them. This is the answer to "do I trust it".
6. **Verified completion on every action (M).** After any action that changes something (send, save, create, delete, type into an app), read the result back and say **verified** or **not checked**. This is the direct answer to the Edge failure and it fits your rule against claiming success.
7. **Tab sets as context (S).** In BROWSER chat, pick up to ten tabs and ask across them ("compare these three", "which has the cheapest delivery"). Reuses the tab list and the page reader.
8. **Sensitive-site rule (S).** For sites in a list (banks, health, government, payments), the agent may read but never click or type unless you take over. Matches Atlas's behaviour and your own "ask first" principle, but automatic.
9. **Phone access with real push and approvals (M to L).** The pairing page exists. Add push notifications (a free service such as ntfy, or Telegram) so a finished task or an approval request reaches your phone, with a one-tap approve that needs your phone's own authentication. A chat-app gateway like OpenClaw's is the bigger step; do it only after the security fixes (it opens a remote door).
10. **Voice plus screen: "look at this and help" (L).** Gemini Live's best trick is talking while it sees your screen. After the voice build and screen capture (plan parts B4 and C), a button that captures the window and speaks the answer. Strongest daily use: error messages and forms.
11. **Resume where I left off (M).** Group browsing and chats into topic projects (Edge does this) and show "continue: the visa research you started Tuesday" on HOME. Ties into PROJECTS and the Today panel.
12. **Show your privacy as a feature (S).** The egress ledger is something no competitor has. Surface a small "last 24 hours: 31 requests, 0 secrets, 4 placeholders" line on HOME and a one-click view. It is your strongest answer to "why use this instead of ChatGPT".

## 5. Things I would deliberately not copy

- **Companions and avatars.** Not your use case, and the press coverage is mostly about discomfort.
- **A public skills registry.** OpenClaw's community registry and Gemini's silent updates are exactly the supply-chain problem the security review found elsewhere. Keep skills as local, readable files with a review step like AGENTSMITH already has.
- **A chat-app gateway before the security fixes.** It opens a remote door to an app that can act on your Mac. The seven high-severity findings from 19 October come first.
- **"Act without asking" as a global switch.** Cowork offers it; a per-task, time-limited version (session grants) is safer and almost as convenient.
- **Browser extensions that can reach the assistant.** Gemini's March flaw is the pattern to avoid; Dourmouse's extensions are global across profiles today, a known open item.

## 6. Where to start

The three that pay off fastest together, in about a week: **(1) automations with templates, (2) approval and done notifications, (3) the global hotkey Ask panel.** They turn Dourmouse from an app you open into one that tells you things and is always one keystroke away, and none of them needs a new capability, only wiring what already exists.

## 7. How to check my sources
All figures above come from the pages linked inline; searches I ran and could not confirm are marked as such. The vendor numbers for voice speed conflict with each other and should not be used for decisions. If you want, I can re-run any of these against official release notes.
