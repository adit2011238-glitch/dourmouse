# Live check: Google Docs typing, the owner/model lock, and YouTube control (phase C2)

These are the checks an isolated test copy cannot do, because they need the owner's own Google
sign-in in the pane. The owner (or the main thread, with the owner present) runs them in the
owner's signed-in app. Nothing here signs in, types a password, or sends a message. The helper
talks only to the app's own DevTools and pane-bridge ports on this Mac.

## Before you start

1. Open Dourmouse, go to the BROWSER screen, and make sure you are signed in to Google in the pane
   (sign in yourself, by hand, if not; the agent never does).
2. Create a scratch Google Doc you do not mind a line being added to. Copy its address
   (`https://docs.google.com/document/d/<id>/edit`).
3. Pick a public YouTube watch page (for example `https://www.youtube.com/watch?v=jNQXAC9IVRw`).
4. Note the bar under the browser toolbar: it should say **You have control.**

The owner's app uses DevTools port 9333 and pane port 9334. The helper refuses them unless
`--owner-app` is passed, so it cannot be pointed at your app by accident.

## Check 1: the agent types into a real Google Doc

```bash
cd ~/dourmouse-recon
.venv/bin/python scripts/live_checks/run_docs_youtube_check.py --owner-app \
  --cdp-port 9333 --pane-port 9334 --doc-url 'https://docs.google.com/document/d/<id>/edit'
```

What it does: opens the doc in the active pane tab, waits for Docs' hidden text frame
(`iframe.docs-texteventtarget-iframe`), clicks into the page, presses Cmd+Down (end of document),
and types one line, `Dourmouse live check DM-C2-<time>: typed by the agent into the hidden text
frame.`, with `browser_type` in text mode (no target: it types into whatever Docs focused). Then it
reads the doc's own text export (a GET of your doc, inside the pane) until the line shows up.

Expected:
- `[PASS] docs: editor frame`
- `[PASS] docs: type: TYPED ... characters into the focused element inside a frame ...`
- `[PASS] docs: saved text contains the line`
- While it types, the bar says **Model is acting in this tab: typing.**

Also look at the document: the line is at the end, on its own paragraph. If the line is there
but glued to the previous paragraph, Docs ignored the line break: report that (it decides how
`browser_type` should send line breaks to Docs).

## Check 2: your typing stops the agent (the lock)

```bash
.venv/bin/python scripts/live_checks/run_docs_youtube_check.py --owner-app \
  --cdp-port 9333 --pane-port 9334 --doc-url 'https://docs.google.com/document/d/<id>/edit' --lock-check
```

After check 1 it prints `LOCK CHECK`, waits 5 seconds, then types a long paragraph slowly. While
it types, click into the document and type a few letters yourself.

Expected:
- `[PASS] docs: lock: STOPPED: the owner started using this tab (key) ... Typed N of M characters ...`
- Your letters and the agent's text are not mixed inside each other: the agent's text stops, your
  letters come after it.
- The bar changes from **Model is acting** to **You have control**, and a note says the model
  stopped.

## Check 3: Stop and Take control

1. Start check 2 again, and this time press **Stop** in the bar instead of typing.
   Expected: `STOPPED BY THE OWNER: the owner pressed Stop ...`, the bar returns to
   **You have control**.
2. Press **Take control**. Ask the model in any chat to type something in the browser.
   Expected: it answers that the owner has control and does nothing. The bar says
   **You have control. The model is paused until you let it act.**
3. Press **Let the model act**. The model may act again.

## Check 4: YouTube control

```bash
.venv/bin/python scripts/live_checks/run_docs_youtube_check.py --owner-app \
  --cdp-port 9333 --pane-port 9334 --youtube-url 'https://www.youtube.com/watch?v=jNQXAC9IVRw'
```

It keeps the video element muted for the whole check: before the page loads it sets a small
script in that tab's page world that keeps every video and audio muted whatever the player sets
(seen live: YouTube switched an element-level mute back on within half a second, so a plain mute
is not enough). It never unmutes. Steps: open, mute (through YouTube's own mute button, which then
reads "Unmute"), status (title, time, length), play, wait 3 s, status (time moved), pause, seek
to 0:10, volume 20 percent (still muted), final status. The script stays on that tab's documents
until the helper exits; reload the tab afterwards if you want to be sure it is gone.

Expected: every step `[PASS]`. If YouTube shows a consent page or a bot check, the helper stops
and says so; answer the consent page yourself, never let a script do it. If an advert plays, the
status says so and the times are the advert's: run it again after the advert.

Unmute and volume with sound were checked on a local silent clip only (so the Mac stayed quiet);
try `browser_media` unmute yourself from a chat if you want to hear it.

## What to send back

The printed `[PASS]`/`[FAIL]` lines (or `--json results.json`), and a screenshot of the BROWSER
screen with the bar while the agent was typing.
