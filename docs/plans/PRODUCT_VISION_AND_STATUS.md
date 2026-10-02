# Dourmouse: completed, remaining, and the finished product (2026-10-02)

Companion to `PLAN_REPLACE_EVERYTHING.md` (the phased plan) and `BROWSER_NEXT_PHASE_PLAN.md`.

## 1. Completed (all committed and pushed unless marked)

- **The app:** one pinned `~/Applications/Dourmouse.app` (a re-branded Electron clone that runs the
  live checkout; `scripts/install_app.sh` rebuilds it). Old copies are in the Trash.
- **The OS shell:** 18 screens (HOME, COMMS, RESEARCH, BROWSER, MEDIA, CODE, PROJECTS, WIKI,
  GOALS, TIMETABLE, ORCHESTRATION, AGENTSMITH, VOICE, ATLAS, NEWS, SECURITY, SETTINGS, OFFICE),
  Command K launcher, keyboard shortcuts and help panel, grouped sidebar, icon rail, contrast and
  type scale fixes, chat with tables, code copy, retry.
- **Model and tools:** about 129 tools across 49 agents; Google Gmail, Calendar, Drive, Docs,
  Sheets, Slides tools; Spotify control; open apps; run commands in a sandbox; real shared browser
  (Playwright over CDP into the embedded Chromium); file preview for PDF, image, audio, video.
- **Safety:** approval gate for risky tools, Seatbelt sandbox, secret scrubbing, request guard,
  confirmation prompts that say when they show only an excerpt, read and write deny-lists for
  secrets and app code, Google sign-in locked to the install owner, socket timeouts.
- **Quality:** about 6,950 tests, lint ratchet held, 160 numbered findings, every change recorded.
- **Browser (this session, #160):** the pane now identifies as plain Chromium (no "Electron"
  token) with a persistent profile `persist:dourmouse-browser`. Verified live in an isolated
  copy: Google's sign-in page loads instead of "content blocked". Committed with this document.
- **Guardrail (#160):** the model is told never to try signing the owner into Google itself.

## 2. Remaining (in the order planned)

J. Shared desk: every chat box can open the browser and the media player, and open mp3, mp4,
   PDF, txt, Markdown, CSV, JSON, code (today RESEARCH, COMMS, AGENTSMITH are pinned to one
   agent, CODE has no Dourmouse tools, and txt is refused).
A. You sign in to Google once in the pane; verify logins survive restart; permissions like Chrome.
B. Chrome parity: tabs, bookmarks, history, downloads, find, passwords, autofill, extensions,
   DevTools, print, PiP, profiles, DRM (castLabs build), WebRTC, passkeys, import from Chrome.
   Test on 60 real sites, target 95 percent.
C. Model and you share the browser: stable element ids, a lock so you and the model do not fight
   over one field, live tests typing in Google Docs and driving YouTube.
D. Player: one surface for YouTube, Spotify Web, local files; queue, now playing, media keys,
   model controls (play, pause, seek, what is playing). PDF reader with annotations.
F. Drive other Mac apps: Accessibility and Screen Recording, read any app's controls, click and
   type by element, per-app approvals, visible "model is driving" indicator, kill switch.
G. Tool skill: a 40-task benchmark, better tool descriptions, fix the top failure causes.
H. Security closeout: A5, R2B-08, R2B-11, A9, parts of R2B-07, R2B-09, N3.
I. Ship: self-contained signed build, auto-update, first-run permission walkthrough, crash
   recovery, speed budget, setup and login restyle, 56 open UX items.
Owner-only pending: allow the DRM build, first-launch macOS prompts, Chrome lockdown extension,
sudo helper install, AGENT SMITH re-approve, one real HOME message, Apple Developer account choice.

## 3. What the finished product looks like

One app in the Dock called Dourmouse. Opening it shows a calm dark desktop with a sidebar of
your work (Home, Mail, Research, Browser, Media, Code, Docs and goals, Security and more), a
Command K launcher, and a chat box at the bottom of every working screen.

- **Browser:** a full browser in the window. Tabs, bookmarks, history, downloads, passwords,
  extensions, your Google account and every other site, the way Chrome behaves. You stop opening
  Chrome. The model can use the same tab you are on, and you can see and take over at any time.
- **Ask from anywhere:** in any chat box you can say "open this PDF", "play this album", "open
  google.com and find flights", "summarise this tab", "put this in a Doc"; it opens the browser
  tab or the player right there and does it.
- **Media:** video, music, PDFs and text files open in the built-in player and reader; YouTube
  and Spotify play with sound and DRM; queue, now playing, media keys.
- **Docs:** Google Docs, Sheets and Slides in the browser; the model types and edits with you,
  paragraph by paragraph, while you watch.
- **Other apps:** it can open and operate Mac apps (with permission, one app at a time, with an
  indicator and a stop button), and replaces the simple ones with its own screens.
- **Safe by default:** nothing risky happens without your approval; secrets stay out of the
  model's reach; everything it does is logged and visible in Security and Office.
- **Always on:** agents work in the background (research, news, security scans), notify you
  quietly, and everything runs on your Mac with cloud models only.
