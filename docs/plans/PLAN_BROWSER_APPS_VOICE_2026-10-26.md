# Plan: a browser you can sign in with, agents that use apps easily, and a voice mode that feels alive (2026-10-26)

Written from the code as it is today (read, not remembered). Nothing in this plan has been built yet.

## 0. The three things, in one paragraph each

**Browser.** The browser already has tabs, a Keychain-backed password vault, import of Chrome bookmarks, history and a passwords file, separate profiles, site permissions, extensions and DRM. What is missing is the part you actually feel: you have never completed a Google sign-in in it, there is no passkey or Touch ID, no "one sign-in everywhere", and the sign-in state is not visible. One thing cannot be done and I will not pretend otherwise: **signing in to Chrome itself (Chrome Sync) is impossible in Dourmouse**, because sync only exists inside Google's own Chrome build. What we can build is a browser where you sign in to Google once, stay signed in, and bring your Chrome data across.

**Apps.** The app-driving engine exists (snapshot an app's controls, click, type, press keys, scroll, with a deny list, an allow list, a kill switch and a confirmation on every action). It has never driven a real app live, because macOS Accessibility has not been granted. And it is not yet "easy": every single click asks you, the model works pixel by pixel, and there are no shortcuts for common apps.

**Voice.** Today there is no voice mode. The VOICE screen is push-to-talk, a command parser and a log: you record, it transcribes, it understands about a dozen fixed commands, and anything else is not sent anywhere. A hands-free loop exists in code but is switched off and has never run. There is no continuous listening, no streaming, no interrupting it while it speaks, and the replies are read out by the macOS `say` voice. Making this "really interactive" is the largest build of the three.

## 1. Facts this plan stands on (checked today)

| Fact | Where it comes from |
|---|---|
| Vault stores logins as ciphertext in the Keychain, per origin, https only | `electron/passwords.js` header |
| Chrome import reads Bookmarks, History and an exported passwords CSV only; never Chrome's own password database, Keychain key or cookies, by design | `electron/importers.js` header |
| Up to 8 profiles, each with its own cookie jar | `electron/profiles.js` |
| No passkey or Touch ID code anywhere in `electron/` | search returned nothing |
| App driver tools: snapshot, click, type, press key, scroll, stop, status; click/type/press/scroll all ask first | `dourmouse/app_driver/tools.py` |
| Accessibility not granted; the Homebrew `Python.app` path needs the grant | earlier APPS screen check |
| Voice flag `DOURMOUSE_VOICE` unset, hands-free off ("DOURMOUSE_HANDS_FREE is off") | live `/api/hands_free/status` |
| Speech-to-text installed: faster-whisper, with `large-v3-turbo` and `tiny` models already downloaded | model cache |
| Wake word package installed (openwakeword), microphone package installed (sounddevice) | package check |
| Text-to-speech: macOS `say` only. `piper` Python package present but no voice files and no `piper` program | `~/.local/share/piper_models`, `which piper` |
| Mac is an M3 with 8 cores and 24 GB | `sysctl` |
| No voice activity detector package (webrtcvad, silero) and no echo handling | package check |
| The old `ui/voice.html` page already does transcribe, then stream the chat reply, then speak | `ui/voice.html` |

## 2. What is already decided by you (so it is not re-asked)

Cloud models only; the privacy gate decides what leaves the Mac; anything that sends, submits, buys or deletes asks first; the model never answers its own approval; no trading without the demo checklist; page, mail and file text is data, not instructions. This plan keeps all of it. Where a feature pushes against one (voice approvals, saved passwords, driving other apps) the design below says how.

## 3. Part A: the browser (build order A1 to A6)

**A1. Sign in to Google once, properly (owner step, 5 minutes, then I verify).** You sign in at accounts.google.com in the pane yourself; I never type a password. I then check Gmail, Drive, Docs and YouTube are signed in, and that it survives a full app restart. This is the single most useful step for everything else in this plan. Acceptance: all four open signed in after Cmd-Q and relaunch.

**A2. A visible account and sign-in state.** A small account chip in the BROWSER bar (which Google account the pane is signed in as, per profile), a one-click "sign out of this site", and a plain message when a site's session has expired. Decision needed (A-D1): when you sign in through SETTINGS (the OAuth that lets the model use Gmail/Calendar), should that also sign the pane in? Recommendation: keep them separate (OAuth for the model's tools, cookies for you), and show both clearly, because mixing them lets a page-level compromise reach the model's account tokens.

**A3. Save and fill that feels like Chrome.** The vault exists; make the loop smooth: a save bar after a successful login, autofill offered in the page's own field, a native Touch ID or password confirmation before filling (never auto-fill silently), a "never for this site" list, a password manager screen with search, edit, delete, and a breach-style check (re-used and weak passwords, no network calls). Addresses and cards: addresses already there; cards deliberately not (payment details are in the "never" tier of the privacy gate). Acceptance: save, reload, fill with a confirmation, on a real login form, no submit without you.

**A4. Bring your Chrome across.** The import exists (bookmarks, history, passwords CSV). Build the guided path: a three-step wizard that tells you exactly how to export your passwords from Chrome, takes the file, imports it, and **shows you to delete the CSV afterwards** (it is plaintext). Cookies are not imported, on purpose: that would mean reading Chrome's Keychain key, which is the thing malware does. You sign in again instead (A1).

**A5. Passkeys and Touch ID (a spike first, because it may not be possible).** Chrome's passkeys use the Mac's built-in authenticator. Electron does not offer that out of the box, and I do not know yet whether this build can. Plan: a one-day spike that tries WebAuthn against a test site and reports honestly. If it works, wire it. If it does not, the fallback is: password plus Touch ID confirmation on fill (A3), and for sites that insist on passkeys, "open in your own Chrome" with one click. I will not promise passkeys until the spike says yes.

**A6. Two-step sign-in and awkward logins.** Google and many sites ask for a phone prompt, a code or a security key. Make those flows work and not get stuck: a clear "waiting for you to approve on your phone" state, a code field that does not lose focus, and a recovery path when a site blocks "unsecure browsers" (the user agent is already Chrome's; test the sites you use: Google, school Classroom, GitHub, your bank's login page only to the point of the form, never past it).

**Owner decisions for Part A:** A-D1 above; and A-D2: which sites do you sign in to daily (I test those first).

## 4. Part B: agents that use apps easily (build order B1 to B6)

The engine is good and deliberately strict. The work is to make it fast to use without making it less safe.

**B1. Grant Accessibility and prove one real drive (owner step, then I verify).** You grant Accessibility to the Python.app path shown on the APPS screen; I do one supervised TextEdit task end to end (open, type a sentence, confirm it landed, stop with the kill switch). Until this is done nothing in Part B has been seen working.

**B2. Session grants instead of a question per click.** Today every click and keystroke asks. Replace with: you approve an app **for a task or for N minutes** ("let Dourmouse drive TextEdit for 10 minutes"). While the grant is live, a visible strip shows which app is being driven, what it just did, and a big STOP; every action is still logged and the deny list still applies. Anything dangerous inside the app (send, delete, pay, quit with unsaved work, a password field) **still asks every time**. Acceptance: a ten-step TextEdit task needs one approval, and a Send button in Mail still stops for you.

**B3. Faster paths before pixel-driving.** The cheapest reliable way to use an app is often not clicking: Shortcuts, AppleScript/JXA and URL schemes ("open a new note", "start a timer", "add a calendar event", "play this track") are faster and fail less. Add a "recipes" layer: for each common app (Notes, Calendar, Reminders, Messages, Mail, Music, Finder, Preview, Terminal-denied), a small set of verified actions that use the fast path first and fall back to driving the interface. Each recipe states exactly what it can do and what always asks.

**B4. See apps that do not expose controls.** Some apps give no usable accessibility tree. Add screen capture with Screen Recording permission (second owner grant) and let the model look at the window, with the same grants and the same privacy gate (a screenshot of a window is personal content; it goes through consent like any file).

**B5. Make the model good at it (the "easily" part).** Tool descriptions with worked examples, a routing rule so "type this in Notes" goes to the app driver and not a shell, and a **benchmark of 25 real tasks** (open and type, rename a file, add a calendar event, set a reminder, copy text between two apps, fill a form in the pane) with a pass rate we track and must beat before calling this done. Target: 85 percent.

**B6. Recovery and honesty.** If an app changes under the model, it re-reads, tells you what changed, and stops rather than guessing. A task log you can read afterwards ("opened Notes, typed 3 lines, saved") and a one-click "undo what you can".

**Security notes for Part B.** The deny list (shells, System Settings, password managers, code runners, Dourmouse itself) stays. The two findings the scan raised here are fixed first as part of B2: approval prompts must show what will happen (SEC2-13, SEC2-14), and a typed "yes" must only approve the question actually pending (SEC4-1).

**Owner decisions for Part B:** B-D1: which apps may NEVER be driven (suggest Terminal, System Settings, Keychain, 1Password/Passwords, Dourmouse, your banking tabs); B-D2: default grant length (suggest 10 minutes).

## 5. Part C: voice-only mode that feels alive (build order C1 to C8)

This is the biggest build, so it gets the most detail. The aim is a conversation, not a command box: you talk, it answers in a natural voice within about a second of you stopping, you can interrupt it, and it does things while talking.

**What "really interactive" means (the targets we will measure):**

| Feel | Target |
|---|---|
| You stop talking, it starts answering | under 1.5 s for a simple reply; for a research question it says something within 1.5 s ("Let me check that") and the answer when it has it |
| You interrupt it | it stops speaking within 0.3 s of you talking |
| It hears you | 95 percent word accuracy on your own voice in a normal room, tested with recorded samples |
| It does not talk to itself | zero false triggers from its own voice over a 10-minute test |
| It does not feel dead while thinking | a visible and audible state at all times: listening, thinking, speaking |

**C1. A proper VOICE MODE screen.** A full-window mode (not the old command log): a large orb that shows listening, thinking and speaking, a live transcript of what it heard and what it is saying, a mute button, a stop button, and one key to enter and leave (Alt+V). Typed and voice can be mixed.

**C2. Continuous listening with real turn-taking.** Replace "press, record, release" with always-on listening while the mode is open: a voice activity detector (a proper one, not just loudness) to tell speech from noise, a short pre-roll so the first word is never cut, and end-of-turn detection that waits a little longer after a trailing "and" or a pause mid-sentence so it does not cut you off. A push-to-talk key stays as the fallback. Microphone use stays under the existing consent: it only listens while the mode is open, the indicator is always visible, and the mic kill switch works.

**C3. Faster speech-to-text.** The big `large-v3-turbo` model is accurate but slow on this Mac's processor for live use. Plan: a small fast model for live partial transcripts (the `tiny` model is already downloaded; test `base.en` and `small.en`) and the larger model only to re-check the final sentence if confidence is low. Run on the Apple GPU if a Metal build proves faster in a test. Everything stays local: your voice never leaves the Mac. We measure real latency on recorded samples before choosing.

**C4. A natural voice.** The macOS `say` voice is the weakest part of how it will feel. Options, in order of recommendation: (1) Piper neural voices, local and free: download one or two good voices (about 60 MB each; needs your OK to download) and install the program; (2) the better macOS voices if you download them in System Settings (Siri-quality "Enhanced" voices); (3) a cloud voice, which is the best sound but sends the text of every reply out, so it would have to pass the privacy gate and is **not recommended** given your rules. Decision C-D1: pick the voice after hearing 3 samples (I will render them to files and you listen; I will not play anything).

**C5. Speak while it is still thinking: streaming.** Do not wait for the whole reply. Stream the model's text, cut it into sentences as they arrive, speak sentence one while sentence two is still being written. Voice replies are already shortened by a system rule (`_VOICE_MARKER`); extend it to speak in short natural sentences, no lists, numbers in words, and never read out a web address.

**C6. Interrupt it (barge-in) without it hearing itself.** While it speaks, keep listening. If you start talking, stop the speech at once and take your new sentence. The hard part is the Mac's own speaker feeding back into the microphone: use the system's echo cancellation (Chromium's `echoCancellation`) plus a rule that ignores the microphone for the first 300 ms of its own speech unless the sound is loud and clearly a person. Test with and without headphones; headphones will always be best.

**C7. Do real work out loud, and ask by voice safely.** It should be able to say "On it", run tools (browser, apps, mail), and report: "I found it, the Python 3.15 release was published on 9 October, from python.org." Because of the research-first change (finding #184), factual questions now search first, so the "Let me check" line matters. For actions that need approval: **low-risk** ones (open an app, play a track, read mail aloud) can be approved by saying "yes" or "go ahead"; **high-risk** ones (send, delete, buy, trade, anything with money or a recipient) still need you to press the button on screen, because a spoken "yes" can be forged by anyone or anything that can make sound in the room, and by a video playing on your own computer. This is the same rule as typed "yes" (finding SEC4-1) and I recommend it stay strict. Decision C-D2: confirm that split.

**C8. Wake word and hands-free (optional, last).** The wake-word and hands-free loop exist (openwakeword is installed) but are off and never ran. Enable them only after C1 to C7 feel good, behind a switch in SETTINGS, with the microphone indicator on and a clear "listening for the wake word" state. Wake-word false triggers are the most annoying thing a voice assistant can do, so this ships last and off by default.

**Testing voice without making a sound.** You asked for no audio during tests. Everything here can be tested silently: recorded and synthesized audio fed in as files (speech-to-text accuracy and latency), replies rendered to audio files and measured for time to first word, the turn-taking logic run against scripted audio with known pauses and interruptions, and echo tests done on recorded loops. The final "does it feel good" test is yours, with the volume where you want it, using a checklist I will give you.

## 6. Order, effort, and what runs in parallel

Files do not overlap between the three parts, so they can be built at the same time by separate builders (browser: `electron/` and the BROWSER screen; apps: `dourmouse/app_driver/`, the APPS screen; voice: `dourmouse/voice*.py`, the VOICE screen and `ui/voice.html`).

| Step | What | Effort | Needs you |
|---|---|---|---|
| 1 | A1 Google sign-in, B1 Accessibility grant | 15 minutes of yours | yes, both |
| 2 | A2, A3, A4 (browser accounts, save and fill, Chrome import wizard) | about 2 sessions | A-D1, A-D2 |
| 3 | B2, B3 (session grants, app recipes) | about 2 sessions | B-D1, B-D2 |
| 4 | C1 to C3 (voice screen, listening, fast speech-to-text) | about 2 sessions | none |
| 5 | C4 to C6 (voice, streaming, interrupting) | about 2 sessions | C-D1 (listen to samples), OK to download voices |
| 6 | C7, B4, B5, A5 spike (voice actions, screen reading, app benchmark, passkey spike) | about 2 sessions | C-D2 |
| 7 | A6, B6, C8 and the owner test pass | about 1 session | your listening test |

Recommended start: step 1 today (it unlocks real testing of everything), then steps 2, 3 and 4 in parallel.

## 7. How it is tested and what "done" means

| Part | Done when |
|---|---|
| Browser | You use only Dourmouse for a normal day of browsing and signing in with no need to open Chrome; Gmail, Drive, Docs, YouTube stay signed in after a restart; save and fill works on a real login with a confirmation; Chrome import wizard completes; passkey spike reported honestly |
| Apps | One approval covers a ten-step task; the 25-task benchmark passes at 85 percent or better; every danger case (send, delete, password field, deny-listed app) still stops; kill switch stops within a second; unit tests for every grant rule |
| Voice | The five feel targets above are met on recorded tests; a 10-minute silent soak test has no false triggers; your own listening checklist passes |
| All | Full test suite green, lint ratchet OK, every finding numbered in the audit, security tests added for each new capability (voice approval rule, grant limits, vault fill confirmation), committed and pushed |

## 8. Risks, said plainly

1. **Passkeys may not be possible in Electron.** The spike will tell us; there is a fallback.
2. **Voice latency is bounded by the cloud model.** Each model call takes about 1 to 3 seconds and the research-first rule adds searches. Streaming plus a spoken "let me check" hides some of it, but a research answer will never be instant.
3. **Echo.** Speaker to microphone feedback is the classic voice-assistant failure. Headphones are the real fix; software only reduces it.
4. **Local speech models cost battery and fan noise on this Mac.** The small-model-first design keeps this low.
5. **Driving apps is inherently fragile** (apps change their interface). The recipes layer and the benchmark exist to find that early, but some apps will always be unreliable. The plan lists which ones we support, and the model says plainly when it cannot.
6. **Every new capability widens what a steered model could abuse.** Each part ships with its own attack tests (a page that tries to read saved passwords; a hostile page that talks the model into driving an app; a video that says "yes, send it" aloud). The security plan from 2026-10-19 (waves S1 to S3) should run before or alongside Part B, because its highs are about exactly this (file-write limits, tool scope, own-server redirects).

## 9. What I need from you to start

1. OK to begin with step 1: you sign in to Google in the pane (I will not type anything) and grant Accessibility (I will tell you the exact path).
2. A-D1: keep model sign-in and pane sign-in separate? (my recommendation: yes)
3. B-D1: the apps that must never be driven (my suggestion above).
4. C-D1 and the download OK: can I download two Piper voices (about 120 MB) so you can listen to samples later?
5. C-D2: the voice approval split (low-risk by voice, high-risk by button).
