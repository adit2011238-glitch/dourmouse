#!/usr/bin/env python3
"""Live check (phase C2): Google Docs typing and YouTube control in an ALREADY RUNNING Dourmouse.

Runs the browser agent's real tools (browser_type, browser_media, ...) against the pane of an app
that is already open, through that app's own DevTools and pane-bridge ports, exactly as the server
does. It never starts or stops an app, never signs in to anything, never types a password, and
talks to nothing but those two local ports (the pages themselves are loaded by the pane, as a
person would load them). See scripts/live_checks/docs_and_youtube.md for the steps around it.

Safety:
* Ports 8765, 9333 and 9334 belong to the owner's own app. They are refused unless --owner-app is
  given on purpose, by the owner.
* The Docs check types one marked line at the END of the document you name (use a scratch doc).
* The YouTube check keeps the video element muted for the whole check (a page-world override in
  that tab, set before the page loads) and never unmutes it.

Usage:
  .venv/bin/python scripts/live_checks/run_docs_youtube_check.py --cdp-port 19333 --pane-port 19334 \
      --youtube-url https://www.youtube.com/watch?v=jNQXAC9IVRw
  .venv/bin/python scripts/live_checks/run_docs_youtube_check.py --owner-app --cdp-port 9333 \
      --pane-port 9334 --doc-url https://docs.google.com/document/d/<id>/edit [--lock-check]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

OWNER_PORTS = {8765, 9333, 9334}
ROOT = Path(__file__).resolve().parents[2]

# Silence for the duration of the check, in this tab only, for documents loaded while it is set.
# It runs in the PAGE's world before the page's own scripts: whatever the player sets, the element
# stays muted (seen live: YouTube switched an element-level mute back on within half a second).
# The agent's tools run in their own isolated world, which has its own copy of the property, so
# they still see and report the element's real state.
QUIET_START = (
    "(() => { const d = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'muted');"
    " Object.defineProperty(HTMLMediaElement.prototype, 'muted', { configurable: true, enumerable: true,"
    " get() { return d.get.call(this); }, set(_v) { d.set.call(this, true); } });"
    " const keep = (e) => { const m = e.target; if (m instanceof HTMLMediaElement) d.set.call(m, true); };"
    " for (const t of ['loadstart', 'play', 'playing', 'volumechange']) document.addEventListener(t, keep, true); })()"
)


def _bridge(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _step(results: list, name: str, ok: bool | None, detail: str) -> None:
    mark = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    results.append({"step": name, "result": mark, "detail": detail})
    print(f"[{mark}] {name}: {detail}", flush=True)


def check_docs(ba, url: str, lock_check: bool, results: list) -> None:
    token = f"DM-C2-{int(time.time())}"
    out = ba.browser_open({"url": url})
    first = out.splitlines()[0] if out else ""
    _step(results, "docs: open", None, first)
    if "accounts.google.com" in out.split("\n", 1)[0] or "ServiceLogin" in out:
        _step(results, "docs: signed in", False, "the pane is not signed in to Google (a sign-in page loaded). Sign in yourself in the pane, then run again. This script never signs in.")
        return

    async def _wait_editor():
        page = await ba._ensure_browser()
        await page.wait_for_selector("iframe.docs-texteventtarget-iframe", state="attached", timeout=20_000)
        return "found"

    try:
        ba._call(_wait_editor, timeout=30)
        _step(results, "docs: editor frame", True, "docs-texteventtarget-iframe is on the page")
    except Exception as exc:  # noqa: BLE001 - report it
        _step(results, "docs: editor frame", False, f"no docs-texteventtarget-iframe within 20 s ({exc})")
        return
    print(ba.browser_click({"target": "css:.kix-appview-editor"}).splitlines()[0], flush=True)
    print(ba.browser_press({"key": "Meta+ArrowDown"}).splitlines()[0], flush=True)  # caret to the end of the document
    line = f"\nDourmouse live check {token}: typed by the agent into the hidden text frame."
    try:
        typed = ba.browser_type({"text": line, "delay_ms": 30})
        _step(results, "docs: type", True, typed)
    except Exception as exc:  # noqa: BLE001
        _step(results, "docs: type", False, str(exc))
        return
    m = re.search(r"/document/d/([A-Za-z0-9_-]+)", url)
    if not m:
        _step(results, "docs: verify", None, "not a docs.google.com/document address: check the page by eye")
        return

    async def _export():
        page = await ba._ensure_browser()
        return await page.evaluate(
            "async (id) => { const r = await fetch('/document/d/' + id + '/export?format=txt', {credentials: 'include'}); return r.ok ? await r.text() : 'HTTP ' + r.status; }",
            m.group(1),
        )

    text = ""
    for _ in range(10):
        time.sleep(2)
        text = ba._call(_export, timeout=30) or ""
        if token in text:
            break
    _step(results, "docs: saved text contains the line", token in text, f"token {token} {'found' if token in text else 'NOT found'} in the document's own text export")
    if lock_check:
        print("\nLOCK CHECK: in the next 5 seconds the agent starts typing a long paragraph. Click into the document and type a few letters yourself while it types.", flush=True)
        time.sleep(5)
        try:
            ba.browser_type({"text": " ".join(["owner-lock-check"] * 120), "delay_ms": 120})
            _step(results, "docs: lock", False, "the agent typed everything: no owner input was seen (did you type in the pane, in that tab?)")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            _step(results, "docs: lock", msg.startswith("STOPPED: the owner started using this tab") or msg.startswith("OWNER IS USING THIS TAB"), msg[:400])


def check_youtube(ba, url: str, results: list) -> None:
    async def _quiet():
        page = await ba._ensure_browser()
        session = await page.context.new_cdp_session(page)
        await session.send("Page.enable")  # without it the script is accepted but never runs (seen live)
        added = await session.send("Page.addScriptToEvaluateOnNewDocument", {"source": QUIET_START})
        return session, added["identifier"]

    session, ident = ba._call(_quiet)
    try:
        out = ba.browser_open({"url": url})
        head = out.splitlines()[:2]
        _step(results, "youtube: open", None, " | ".join(head))
        if "consent." in out.split("\n", 1)[0] or "Before you continue" in out:
            _step(results, "youtube: consent page", False, "YouTube showed a consent page. Not bypassed; answer it yourself in the pane, then run again.")
            return
        if "sorry" in out.split("\n", 1)[0] or "unusual traffic" in out:
            _step(results, "youtube: bot check", False, "YouTube showed a bot check. Not bypassed.")
            return

        async def _wait_video():
            page = await ba._ensure_browser()
            await page.wait_for_selector("video.html5-main-video", state="attached", timeout=20_000)

        ba._call(_wait_video, timeout=30)
        muted = ba.browser_media({"action": "mute"})
        _step(results, "youtube: mute", muted.startswith("MUTED."), muted.replace("\n", " | "))
        async def _yt_label():
            page = await ba._ensure_browser()
            return await page.evaluate(
                "(() => { const b = document.querySelector('.ytp-mute-button button, button.ytp-mute-button');"
                " return b ? (b.getAttribute('data-title-no-tooltip') || b.getAttribute('aria-label') || '') : ''; })()"
            )

        label = ba._call(_yt_label) or ""
        _step(results, "youtube: the player's own mute state says muted", label.lower().startswith("unmute"), f"YouTube's mute button now reads {label!r}")
        status = ba.browser_media({"action": "status"})
        _step(results, "youtube: status", status.startswith("MEDIA (YouTube video)"), status.replace("\n", " | "))
        played = ba.browser_media({"action": "play"})
        _step(results, "youtube: play", played.startswith("PLAYING."), played.replace("\n", " | "))
        time.sleep(3)
        after = ba.browser_media({"action": "status"})
        t = re.search(r"\(([\d.]+)s of", after)
        _step(results, "youtube: time advances", bool(t and float(t.group(1)) > 0.5), after.replace("\n", " | "))
        paused = ba.browser_media({"action": "pause"})
        _step(results, "youtube: pause", paused.startswith("PAUSED."), paused.replace("\n", " | "))
        sought = ba.browser_media({"action": "seek", "to": 10})
        _step(results, "youtube: seek to 0:10", sought.startswith("SOUGHT to 0:1"), sought.replace("\n", " | "))
        vol = ba.browser_media({"action": "volume", "level": 20})
        _step(results, "youtube: volume 20 (still muted)", vol.startswith("VOLUME set to 20%") and "muted" in vol, vol.replace("\n", " | "))
        final = ba.browser_media({"action": "status"})
        _step(results, "youtube: still muted at the end", "muted" in final and "sound on" not in final, final.replace("\n", " | "))
    finally:
        async def _unquiet():
            await session.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": ident})
            await session.detach()

        try:
            ba._call(_unquiet)
        except Exception as exc:  # noqa: BLE001 - the tab may be gone
            print(f"(could not remove the quiet-start script: {exc})", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cdp-port", type=int, required=True)
    ap.add_argument("--pane-port", type=int, required=True)
    ap.add_argument("--ui-port", type=int, default=0, help="only checked against the owner's ports; nothing is sent to it")
    ap.add_argument("--owner-app", action="store_true", help="allow the owner's own app ports (8765, 9333, 9334)")
    ap.add_argument("--doc-url", default="", help="a Google Doc the owner owns and is signed in to in the pane (a scratch doc)")
    ap.add_argument("--youtube-url", default="", help="a public YouTube watch page")
    ap.add_argument("--lock-check", action="store_true", help="also check that typing in the doc yourself stops the agent")
    ap.add_argument("--json", default="", help="write the step results to this file")
    args = ap.parse_args(argv)

    ports = {args.cdp_port, args.pane_port} | ({args.ui_port} if args.ui_port else set())
    if ports & OWNER_PORTS and not args.owner_app:
        print(f"REFUSED: {sorted(ports & OWNER_PORTS)} belong to the owner's own app. Pass --owner-app only if you are the owner and mean it.", file=sys.stderr)
        return 2
    if not args.doc_url and not args.youtube_url:
        print("Nothing to do: give --doc-url and/or --youtube-url.", file=sys.stderr)
        return 2
    try:
        control = _bridge(args.pane_port, "/control")
        _bridge(args.pane_port, "/status")
    except Exception as exc:  # noqa: BLE001
        print(f"The pane bridge on 127.0.0.1:{args.pane_port} did not answer ({exc}). Is the app running with that port?", file=sys.stderr)
        return 1
    if control.get("held"):
        print("The owner holds the browser (Take control). Press Let the model act first.", file=sys.stderr)
        return 1

    os.environ["DOURMOUSE_ELECTRON_CDP_PORT"] = str(args.cdp_port)
    os.environ["DOURMOUSE_ELECTRON_PANE_PORT"] = str(args.pane_port)
    sys.path.insert(0, str(ROOT))
    from dourmouse import browser_agent as ba

    results: list[dict] = []
    # Nothing is closed at the end: the pane and its tabs belong to the app (closing the CDP
    # context of a connected browser could close the owner's tabs). The process just exits.
    if args.doc_url:
        check_docs(ba, args.doc_url, args.lock_check, results)
    if args.youtube_url:
        check_youtube(ba, args.youtube_url, results)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [r for r in results if r["result"] == "FAIL"]
    print(f"\n{len(results) - len(failed)} of {len(results)} steps without a failure.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
