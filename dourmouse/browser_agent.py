"""Browser agent (v5.25) — real headless-browser automation via Playwright.

Drives the LOCALLY INSTALLED Google Chrome (``channel='chrome'``) — no browser
binary download, no CDN, nothing leaves the machine. Every tool returns REAL
page state; nothing is fabricated (Rule 2.2).

Safety model (Rule 2.9 permission tiers are enforced by the ENGINE; tools only
declare their tier):

- Only ``http(s)://`` URLs are ever opened — ``file://``, ``chrome://``,
  ``javascript:`` and every other scheme is REFUSED deterministically.
- Credentials live in a 0600 JSON vault under ``<project>/data/``; passwords
  are NEVER returned by any tool — only masked hints.
- ``browser_submit`` / ``browser_signin`` / ``browser_creds_store`` /
  ``browser_creds_forget`` are REQUIRES_CONFIRMATION: a human approves the
  exact site + action before anything submits.

Threading: Playwright's sync API cannot share a context across threads, and
dispatch tools can run on different server threads. So ALL Playwright work
runs on ONE dedicated asyncio event-loop thread; every tool call submits a
coroutine to that loop and blocks on the future. Deterministic and safe.

Activity is kept in a bounded ring buffer surfaced via ``/api/browser/*`` in
the UI and through the dispatch SSE tool events (the task deck).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_VAULT_PATH = _DATA_DIR / "browser_creds.json"
_SHOTS_DIR = _DATA_DIR / "browser" / "shots"

_ACTIVITY: list[dict[str, str]] = []
_ACTIVITY_LOCK = threading.Lock()
_ACTIVITY_CAP = 300

_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None
_CONTEXT: Any = None
_PAGE: Any = None
_LAUNCH_ERROR: str | None = None
_GLOBAL_LOCK = threading.Lock()

# Phase C1 state, all owned by the browser thread (the one event loop every tool runs on).
_PW: Any = None  # the one Playwright driver, shared by the pane connection and a headless launch
_BROWSER: Any = None  # the CDP connection to the Electron shell (pane mode only)
_PAGE_MODE: str | None = None  # "pane" (the owner's tab) or "headless" (a separate Chrome)
_PREPARED_CONTEXTS: set[int] = set()  # contexts that already carry the request filter and the page listener
_TARGET_IDS: dict[Any, str] = {}  # Playwright Page -> CDP target id (matched against the pane bridge)
_NEW_PAGES: list[Any] = []  # pages that appeared in the context, newest last (headless popup following)
_PANE_SEEN: dict[str, Any] = {"active": None, "ids": frozenset()}  # what the last sync saw of the pane's tabs
_NOTES: list[str] = []  # one-line notices appended to the next tool result (tab followed, popup opened)
_NOTES_LOCK = threading.Lock()
_NEXT_ID = 1  # element ids are unique across tabs and snapshots: the next number to hand out
_ID_OWNER: dict[int, Any] = {}  # element id number -> the Page it was issued on
_ID_OWNER_CAP = 6000
_WORLDS: dict[Any, dict[str, Any]] = {}  # Page -> {"session", "ctx", "href", "gen"}: the isolated world
_SCRIPT_PATH = Path(__file__).resolve().parent / "browser_scripts" / "element_ids.js"
_MEDIA_SCRIPT_PATH = Path(__file__).resolve().parent / "browser_scripts" / "media_control.js"
# "e12", "id:e12", "@e12" and "[e12]" name an element id. Anything else is a label, CSS or text target.
_ELEMENT_ID_RE = re.compile(r"^(?:id:|@)?\[?e(\d{1,7})\]?$", re.IGNORECASE)
_POPUP_POLL_SECONDS = (0.0, 0.1)  # an immediate check caught every popup in a live test (12 of 12); the second is slack

_UA_NOTE = (
    "Automation is the whole point of the browser agent — this is a "
    "headless Chrome driven by Dourmouse, never hidden."
)

# --------------------------------------------------------------------------- #
# Electron embedded pane (Stage D of the desktop-shell migration — see
# ~/.claude/plans/sorted-wiggling-pearl.md). electron/main.js sets these TWO
# env vars only when IT spawned this server process; absent entirely under
# the older pywebview shell (dourmouse/desktop.py) or a plain headless
# server with no shell at all — _electron_pane_configured() treats that as
# "no pane available" and _ensure_browser() falls straight through to its
# existing launch()-its-own-Chrome behavior below, unchanged.
#
# The load-bearing fact this whole path depends on was proven LIVE in the
# migration spike, not assumed: Playwright's chromium.connect_over_cdp()
# against Electron's own --remote-debugging-port enumerated every real open
# page (the app's own windows AND the embedded pane) and successfully drove
# a real page.goto() navigation on the pane's page — the exact same API
# every tool below already calls. So NONE of those tools change for this
# path; only _ensure_browser() gains a second way to obtain the one _PAGE/
# _CONTEXT pair they all already share.
# --------------------------------------------------------------------------- #

_ELECTRON_CDP_PORT_ENV = "DOURMOUSE_ELECTRON_CDP_PORT"
_ELECTRON_PANE_PORT_ENV = "DOURMOUSE_ELECTRON_PANE_PORT"
_PANE_BRIDGE_TIMEOUT = 5.0
_PANE_DISCOVERY_TIMEOUT = 5.0


def _electron_pane_configured() -> tuple[int, int] | None:
    """(cdp_port, pane_bridge_port) if this process was spawned by the
    Electron shell, else None. Both-or-nothing: a partially-set pair (one
    var present, not the other) is treated as absent rather than guessed
    at — this only ever happens if something hand-sets one env var without
    the other, which is not a real, supported configuration."""
    cdp_raw = os.environ.get(_ELECTRON_CDP_PORT_ENV, "").strip()
    pane_raw = os.environ.get(_ELECTRON_PANE_PORT_ENV, "").strip()
    if not cdp_raw or not pane_raw:
        return None
    try:
        return int(cdp_raw), int(pane_raw)
    except ValueError:
        return None


def _pane_bridge_request(pane_port: int, method: str, path: str) -> dict[str, Any]:
    """One real HTTP call to electron/main.js's tiny local pane-bridge
    server — the same "a tiny second local server for cross-process
    signaling" pattern dourmouse/vision_bridge.py's own docstring already
    establishes and justifies in this codebase, just the Python-calling-
    Node-instead-of-Node-calling-Python direction of it."""
    import urllib.request

    url = f"http://127.0.0.1:{pane_port}{path}"
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=_PANE_BRIDGE_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


class _PaneUnreachable(RuntimeError):
    """The pane bridge does not answer at all: the Electron shell is not there (any more).
    Distinct from "the pane is there but no page was found", which must NOT fall back to a
    separate Chrome."""


def _pane_bridge_json(pane_port: int, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST a small JSON body to the pane bridge and return (status, answer). A 4xx answer is a
    real answer (the bridge says why), not an error; only a bridge that does not answer raises."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{pane_port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_PANE_BRIDGE_TIMEOUT) as resp:  # noqa: S310 - always http://127.0.0.1
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            answer = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            answer = {}
        return exc.code, answer if isinstance(answer, dict) else {}


# --------------------------------------------------------------------------- #
# Shared control (phase C2): the owner's real input in the pane always wins.
#
# Every tool that changes the page first claims the tab it acts on from the Electron main
# process (POST /control/begin). The claim is refused while the owner pressed a key, clicked,
# scrolled or touched in that tab in the last 2.5 s; the agent waits up to 3 s for the owner to
# pause, then gives up with a plain message. While the agent acts, the owner's own input on that
# tab interrupts the claim at once, and the next step is refused by the main process itself, so
# the agent stops between steps and says what it did and did not do. Text goes in through
# /control/type (main-process insertText), so the owner's keystrokes and the agent's chunks pass
# through one event loop and never interleave inside a field. The owner's Stop and Take control
# buttons live in the BROWSER screen only. Design: ~/Documents/DOURMOUSE/C2_SHARED_CONTROL_DESIGN.md.
# A separate headless Chrome has no owner, so it has no lock.
# --------------------------------------------------------------------------- #

_CONTROL_WAIT_SECONDS = 3.0
_TYPE_CHUNK = 24  # characters per /control/type step: the owner can cut in between any two
_FILL_CHUNK = 512  # the bridge's own cap per call
_CONTROL_NOTED_OLD_SHELL = False


class _OwnerControl(RuntimeError):
    """The owner has the tab (or stopped the agent). ``code`` says how; the message says what
    was and was not done."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def with_progress(self, progress: str) -> "_OwnerControl":
        if not progress:
            return self
        return _OwnerControl(self.code, f"{self} {progress.strip()}")


_STOP_TEXT = {
    "owner-input": (
        "STOPPED: the owner started using this tab ({kind}) while the agent was acting, and the owner "
        "always goes first. The agent stopped so the two never write into the same place."
    ),
    "stopped": "STOPPED BY THE OWNER: the owner pressed Stop in the BROWSER screen. Do not retry this unless the owner asks.",
    "owner-control": (
        "OWNER HAS CONTROL: the owner pressed Take control in the BROWSER screen, so the agent may not act in "
        "the browser until the owner presses Let the model act. Ask the owner."
    ),
    "no-tab": "STOPPED: the tab the agent was acting on was closed.",
    "expired": "STOPPED: the agent's claim on the tab lapsed (it was not heard from for a minute).",
}


def _stop_error(reason: str, kind: str = "") -> _OwnerControl:
    text = _STOP_TEXT.get(reason) or f"STOPPED: the browser refused the next step ({reason})."
    return _OwnerControl(reason, text.format(kind=kind or "input"))


class _NoClaim:
    """No owner to share with (a separate headless Chrome, or an older shell without /control)."""

    pane = False

    def __init__(self, page: Any) -> None:
        self.page = page

    async def check(self) -> None:
        return None

    async def verdict(self) -> _OwnerControl | None:
        return None

    async def insert(self, text: str) -> None:
        await self.page.keyboard.insert_text(text)

    @contextlib.asynccontextmanager
    async def pointing(self, x: float | None = None, y: float | None = None):
        yield

    async def end(self, outcome: str) -> None:
        return None


class _Claim(_NoClaim):
    """The agent's claim on one pane tab, held for one tool call."""

    pane = True

    def __init__(self, page: Any, pane_port: int, action: str, tab: Any) -> None:
        super().__init__(page)
        self.port = pane_port
        self.action = action
        self.tab = tab

    async def _post(self, name: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            return await asyncio.to_thread(_pane_bridge_json, self.port, f"/control/{name}", {"action": self.action, **body})
        except Exception as exc:  # noqa: BLE001 - the shell went away: fail closed, readable
            raise _OwnerControl("error", f"STOPPED: the browser pane stopped answering ({type(exc).__name__}: {exc}).") from exc

    async def verdict(self) -> _OwnerControl | None:
        status, data = await self._post("check", {})
        if data.get("ok"):
            return None
        return _stop_error(str(data.get("reason") or f"status {status}"), str(data.get("kind") or ""))

    async def check(self) -> None:
        refusal = await self.verdict()
        if refusal is not None:
            raise refusal

    async def insert(self, text: str) -> None:
        status, data = await self._post("type", {"text": text})
        if not data.get("ok"):
            if data.get("reason") == "error" or status >= 500:
                raise RuntimeError(f"BROWSER TYPE FAILED in the pane: {data.get('error') or status}")
            raise _stop_error(str(data.get("reason") or f"status {status}"), str(data.get("kind") or ""))

    @contextlib.asynccontextmanager
    async def pointing(self, x: float | None = None, y: float | None = None):
        """Tell the main process the next mouse events on this tab are the agent's own, so they are
        not taken for the owner's. Opened right before the click is sent, closed right after."""
        body: dict[str, Any] = {"on": True, "ms": 2500}
        if x is not None and y is not None:
            body.update(x=float(x), y=float(y))
        status, data = await self._post("pointer", body)
        if not data.get("ok"):
            raise _stop_error(str(data.get("reason") or f"status {status}"), str(data.get("kind") or ""))
        try:
            yield
        finally:
            await self._post("pointer", {"on": False})

    async def end(self, outcome: str) -> None:
        try:
            await self._post("end", {"outcome": outcome})
        except _OwnerControl as exc:  # the shell went away: there is no claim left to end
            _log("control", f"could not end the claim: {exc}")


async def _claim(page: Any, tool: str) -> _NoClaim:
    """Claim the tab the agent is about to change. In the pane this waits (at most 3 s) for the
    owner to pause and then refuses with a plain message; elsewhere it is a no-op."""
    global _CONTROL_NOTED_OLD_SHELL
    pane = _electron_pane_configured()
    tab = _PANE_SEEN.get("active")
    if _PAGE_MODE != "pane" or pane is None or tab is None:
        return _NoClaim(page)
    started = time.monotonic()
    deadline = started + _CONTROL_WAIT_SECONDS
    while True:
        try:
            status, data = await asyncio.to_thread(_pane_bridge_json, pane[1], "/control/begin", {"tab": tab, "tool": tool})
        except Exception as exc:  # noqa: BLE001 - fail closed, readable
            raise _OwnerControl(
                "error", f"REFUSED: could not check who has the browser tab ({type(exc).__name__}: {exc}). Nothing was done."
            ) from exc
        if data.get("ok") and data.get("action"):
            return _Claim(page, pane[1], str(data["action"]), tab)
        reason = str(data.get("reason") or "")
        if not reason and (status == 404 or data.get("ok")):
            # A shell without /control (an older build answers 404 "not found", a stand-in bridge
            # answers ok with no claim): act as before, and say so once.
            if not _CONTROL_NOTED_OLD_SHELL:
                _CONTROL_NOTED_OLD_SHELL = True
                _note("this app's browser shell is older than the owner/model lock, so the agent cannot tell when the owner is using the tab. Quit and reopen the app.")
            return _NoClaim(page)
        if reason == "owner-active" and time.monotonic() < deadline:
            wait = max(0.05, min(float(data.get("retryInMs") or 300) / 1000.0 + 0.05, deadline - time.monotonic()))
            await asyncio.sleep(wait)
            continue
        waited = time.monotonic() - started
        if reason == "owner-active":
            raise _OwnerControl(
                reason,
                f"OWNER IS USING THIS TAB: the owner typed, clicked or scrolled in tab {tab} in the last few seconds, "
                f"so the agent waited {waited:.1f}s and did not {tool.replace('_', ' ')}. Nothing was done. Try again in a "
                "moment, or ask the owner whether to go ahead.",
            )
        if reason == "owner-control":
            raise _OwnerControl(reason, _STOP_TEXT[reason] + " Nothing was done.")
        if reason == "no-tab":
            raise _OwnerControl(reason, f"REFUSED: tab {tab} is gone (it was closed). Nothing was done. Take a new browser_snapshot.")
        raise _OwnerControl(reason or "error", f"REFUSED: the browser did not let the agent act ({reason or status}). Nothing was done.")


@contextlib.asynccontextmanager
async def _acting(page: Any, tool: str):
    """Hold a claim on the tab for one tool call. A step that failed because the owner took over
    (a navigation the owner's Stop aborted, say) is reported as that, not as a page error."""
    claim = await _claim(page, tool)
    outcome = "error"
    try:
        yield claim
        outcome = "done"
    except _OwnerControl as exc:
        outcome = exc.code
        raise
    except Exception:
        refusal = await claim.verdict() if claim.pane else None
        if refusal is not None:
            outcome = refusal.code
            raise refusal from None
        raise
    finally:
        await claim.end(outcome)


def _note(text: str) -> None:
    """Queue a one-line notice for the model; ``_call`` appends it to the tool result."""
    with _NOTES_LOCK:
        if text not in _NOTES:
            _NOTES.append(text)


def _drain_notes() -> str:
    with _NOTES_LOCK:
        notes = list(_NOTES)
        _NOTES.clear()
    return "".join(f"\nNOTE: {n}" for n in notes)


def _tab_label(tab: dict[str, Any]) -> str:
    title = " ".join(str(tab.get("title") or "").split())[:80]  # page-controlled: one line, bounded
    url = tab.get("url") or "a new tab"
    return f"tab {tab.get('id')} ({title + ', ' if title else ''}{url})"


async def _bridge_tabs(pane_port: int, refresh: bool = False) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            _pane_bridge_request, pane_port, "GET", "/tabs?refresh=1" if refresh else "/tabs"
        )
    except Exception as exc:  # noqa: BLE001 - bridge unreachable, readable
        raise _PaneUnreachable(
            f"could not reach the Electron pane bridge on 127.0.0.1:{pane_port}: {exc}"
        ) from exc


def _blank_url(url: str) -> bool:
    return url in ("", "about:blank")


def _url_matches(page_url: str, tab_url: str) -> bool:
    """The bridge reports "" for a blank tab and the full address otherwise."""
    if _blank_url(tab_url):
        return _blank_url(page_url)
    return page_url == tab_url or page_url.split("#")[0] == tab_url.split("#")[0]


async def _target_id_of(page: Any) -> str | None:
    """The CDP target id of a Playwright Page, read once and cached. This is what the pane
    bridge reports per tab, so it names a tab without guessing from the address."""
    for gone in [p for p in _TARGET_IDS if p.is_closed()]:
        del _TARGET_IDS[gone]
    known = _TARGET_IDS.get(page)
    if known:
        return known
    session = await page.context.new_cdp_session(page)
    try:
        info = await session.send("Target.getTargetInfo")
    finally:
        await session.detach()
    tid = ((info or {}).get("targetInfo") or {}).get("targetId")
    if tid:
        _TARGET_IDS[page] = tid
    return tid


async def _match_pane_page(
    browser: Any, tab: dict[str, Any], tabs: list[dict[str, Any]], refreshed: bool = True
) -> tuple[Any, str]:
    """The Playwright Page that is the pane tab ``tab``: by CDP target id when the bridge
    reports one, else by address. Returns (page or None, how). When the bridge named a target
    that no page carries and the address leaves several candidates, the answer is (None, "stale")
    on the first try so the caller can ask the bridge to re-read its ids before guessing."""
    pages = [p for ctx in browser.contexts for p in ctx.pages if not p.is_closed()]
    wanted = tab.get("targetId") or ""
    if wanted:
        for p in pages:
            if _TARGET_IDS.get(p) == wanted:
                return p, "target id"
    candidates = [p for p in pages if _url_matches(p.url, tab.get("url") or "")]
    if wanted:
        for p in candidates:
            if p in _TARGET_IDS:
                continue  # already known to be some other target
            try:
                got = await _target_id_of(p)
            except Exception as exc:  # noqa: BLE001 - a page that vanished mid-lookup
                _log("engine", f"could not read the target id of {p.url!r}: {type(exc).__name__}")
                continue
            if got == wanted:
                return p, "target id"
        others = {t.get("targetId") for t in tabs if t.get("targetId") and t.get("targetId") != wanted}
        candidates = [p for p in candidates if _TARGET_IDS.get(p) not in others]
        if len(candidates) > 1 and not refreshed:
            return None, "stale"
    if len(candidates) == 1:
        return candidates[0], "address"
    if len(candidates) > 1:
        return (_PAGE if _PAGE in candidates else candidates[0]), "address (ambiguous)"
    return None, "none"


async def _prepare_context(context: Any, filter_requests: bool = True) -> None:
    """Once per context: the listener that notices new pages, and the speed filter.

    Phase C2: the speed filter (abort media and a few trackers) is for the agent's OWN headless
    Chrome only. Installed on the pane's context it intercepted every request of the owner's
    shared browser through this process and aborted the owner's own audio and video (a plain
    <video src> or <audio src> stopped loading the moment the agent attached)."""
    if id(context) in _PREPARED_CONTEXTS:
        return
    _PREPARED_CONTEXTS.add(id(context))

    def _on_page(new_page: Any) -> None:
        _NEW_PAGES.append(new_page)
        del _NEW_PAGES[:-50]

    context.on("page", _on_page)
    if not filter_requests:
        return

    async def _route_handler(route: Any) -> None:
        req = route.request
        if _should_block_request(req.url, req.resource_type):
            await route.abort()
        else:
            await route.continue_()

    try:
        await context.route("**/*", _route_handler)
    except Exception as exc:  # noqa: BLE001 - best-effort, matches the launch() path
        _log("engine", f"ad/media blocking not installed on a browser context: {exc}")


async def _connect_pane(cdp_port: int, pane_port: int) -> Any:
    """The CDP connection to the Electron shell, made once and re-made if it dropped. The
    pane is shown first (POST /show is idempotent): attaching CDP to a pane that was never
    shown can crash its renderer (the B1 trap)."""
    global _PW, _BROWSER
    if _BROWSER is not None and _BROWSER.is_connected():
        return _BROWSER
    try:
        await asyncio.to_thread(_pane_bridge_request, pane_port, "POST", "/show")
    except Exception as exc:  # noqa: BLE001 - bridge unreachable, readable
        raise _PaneUnreachable(
            f"could not reach the Electron pane bridge on 127.0.0.1:{pane_port}: {exc}"
        ) from exc
    from playwright.async_api import async_playwright

    if _PW is None:
        _PW = await async_playwright().start()
    _TARGET_IDS.clear()
    _WORLDS.clear()
    _PREPARED_CONTEXTS.clear()
    try:
        _BROWSER = await _PW.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    except Exception as exc:  # noqa: BLE001 - the DevTools port refused, readable
        raise RuntimeError(
            f"BROWSER PANE: the pane bridge answers but the DevTools port {cdp_port} does not "
            f"accept a connection ({type(exc).__name__}: {exc}). The agent did not fall back to "
            "a separate Chrome, because the pane exists."
        ) from exc
    return _BROWSER


async def _ensure_browser_via_electron_pane(cdp_port: int, pane_port: int, quiet: bool = False) -> Any:
    """The Playwright Page of the tab the OWNER IS LOOKING AT, in the same Chromium session the
    Electron shell's embedded pane renders (see electron/main.js).

    Phase C1 replaces the old "attach to the first about:blank tab and hold it" rule, which
    followed tab 1 forever: a tab the owner switched to, a popup the agent opened and a closed
    tab 1 were all invisible. Now every call asks the pane bridge which tab is active and
    maps it to a Page by CDP target id (the bridge's ``targetId``), falling back to the
    address only when the bridge cannot name the target. A change of tab is told to the model
    in a NOTE line on the tool result. If the pane is there but no page can be found for its
    active tab this raises, and never falls back to a separate headless Chrome.
    """
    global _PAGE, _CONTEXT, _PAGE_MODE, _PANE_SEEN
    browser = await _connect_pane(cdp_port, pane_port)
    deadline = time.monotonic() + _PANE_DISCOVERY_TIMEOUT
    refreshed = False
    while True:
        data = await _bridge_tabs(pane_port, refresh=refreshed)
        tabs = [t for t in (data.get("tabs") or []) if isinstance(t, dict)]
        active = next((t for t in tabs if t.get("id") == data.get("active")), None) or next(
            (t for t in tabs if t.get("active")), None
        )
        if active is None:
            raise RuntimeError(
                "BROWSER PANE: the pane reports no active tab. Call browser_pane_show, then retry. "
                "The agent did not fall back to a separate Chrome, because the pane exists."
            )
        page, how = await _match_pane_page(browser, active, tabs, refreshed)
        if page is not None:
            break
        if not refreshed and active.get("targetId"):
            refreshed = True  # the bridge's cached target id may be stale after a navigation
            continue
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"BROWSER PANE: could not find the browser page for the active {_tab_label(active)} "
                f"within {_PANE_DISCOVERY_TIMEOUT:.0f}s. The pane is open and reachable, so the agent "
                "did NOT fall back to a separate Chrome. Ask the owner to click the tab once, call "
                "browser_pane_show, or use browser_open to load an address into it."
            )
        await asyncio.sleep(0.15)
    await _prepare_context(page.context, filter_requests=False)
    previous = _PANE_SEEN.get("active")
    _PANE_SEEN = {"active": active.get("id"), "ids": frozenset(t.get("id") for t in tabs), "label": _tab_label(active)}
    if not quiet and previous is not None and previous != active.get("id") and (_PAGE is None or _PAGE is not page):
        _note(
            f"following the tab the owner is viewing: now on {_tab_label(active)} (was tab {previous}). "
            "Element ids from the other tab do not apply here; take a new browser_snapshot."
        )
    if how.startswith("address (ambiguous"):
        _note("several tabs show the same address, so the agent could not tell them apart by id; it picked one.")
    _PAGE, _CONTEXT, _PAGE_MODE = page, page.context, "pane"
    return page

#: backlog #8, Phase 4 of the user's own spec ("Ad & Media Blocking...
#: speeds up page loads by up to 5x"). Conservative on purpose: only
#: video/audio streams (a real, large, page-load-blocking cost with zero
#: value for text/structure-based tool use) and a short real list of
#: well-known tracker domains — NOT images/CSS/scripts wholesale, which
#: would risk breaking the very page structure browser_snapshot needs to
#: read. Overridable via DOURMOUSE_BROWSER_BLOCK_MEDIA=0 for a page where
#: media genuinely matters.
_BLOCKED_RESOURCE_TYPES = frozenset({"media"})
_BLOCKED_HOST_SUBSTRINGS = (
    "doubleclick.net",
    "google-analytics.com",
    "googletagmanager.com",
    "googlesyndication.com",
    "facebook.com/tr",
    "connect.facebook.net",
    "scorecardresearch.com",
    "adservice.google.com",
)


def _should_block_request(url: str, resource_type: str) -> bool:
    """Pure predicate (independently testable — no real network/Playwright
    involved) deciding whether a request should be aborted for speed."""
    if os.environ.get("DOURMOUSE_BROWSER_BLOCK_MEDIA", "1").strip() == "0":
        return False
    if resource_type in _BLOCKED_RESOURCE_TYPES:
        return True
    lowered = url.lower()
    return any(host in lowered for host in _BLOCKED_HOST_SUBSTRINGS)


# --------------------------------------------------------------------------- #
# Event-loop plumbing — one thread owns the browser; tools submit coroutines.
# --------------------------------------------------------------------------- #


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _LOOP_THREAD
    with _GLOBAL_LOCK:
        if _LOOP is not None and not _LOOP.is_closed():
            return _LOOP
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, daemon=True, name="dourmouse-browser")
        t.start()
        _LOOP = loop
        _LOOP_THREAD = t
        return loop


def _call(factory: Any, timeout: float = 60.0) -> Any:
    """Run an async coroutine on the browser thread and block for the result."""
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(factory(), loop)
    try:
        result = fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise RuntimeError(
            f"BROWSER TIMEOUT after {timeout:.0f}s — the page may be stuck. "
            "Use browser_wait or retry."
        ) from None
    # A notice raised while the call ran (the tab changed, a popup opened) rides on the
    # result so the model is told. After an error it stays queued for the next call.
    notes = _drain_notes() if isinstance(result, str) else ""
    return result + notes if notes else result


def _log(kind: str, text: str) -> None:
    with _ACTIVITY_LOCK:
        _ACTIVITY.append(
            {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "text": text[:300]}
        )
        if len(_ACTIVITY) > _ACTIVITY_CAP:
            del _ACTIVITY[: len(_ACTIVITY) - _ACTIVITY_CAP]


# --------------------------------------------------------------------------- #
# Browser lifecycle (async — runs on the browser thread only)
# --------------------------------------------------------------------------- #


async def _ensure_browser(quiet: bool = False) -> Any:
    """The page the tools act on: the owner's active pane tab when the Electron shell is there,
    otherwise a Chrome launched here (once per process). ``quiet`` skips the "following the
    owner" notice for a caller that tells the model something more specific itself."""
    global _CONTEXT, _PAGE, _LAUNCH_ERROR, _PAGE_MODE, _PW
    electron_pane = _electron_pane_configured()
    if electron_pane is None and _PAGE is not None and not _PAGE.is_closed():
        return _PAGE
    # Real bug (2026-09-11): _LAUNCH_ERROR used to short-circuit every call
    # for the rest of the process's life once ANY launch failed once —
    # including a transient failure (Chrome mid-update, a momentary
    # resource hiccup) with nothing actually permanent about it. The only
    # way back was restarting the whole app. Retrying costs one failed
    # launch attempt (Playwright fails fast when Chrome is genuinely
    # missing), which is far cheaper than a browser capability staying
    # dead for a session after one bad moment. _LAUNCH_ERROR itself is
    # still recorded below (real, current-value error reporting, e.g. via
    # /api/browser/status), just no longer a permanent latch.
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - env-dependent
        _LAUNCH_ERROR = (
            "NOT CONFIGURED: the browser engine (playwright) is not installed "
            "in this venv. Install it with: .venv/bin/python -m pip install "
            "playwright  (the agent drives the system Google Chrome — no "
            "browser download). Nothing was opened."
        )
        raise RuntimeError(_LAUNCH_ERROR) from exc

    if electron_pane is not None:
        try:
            page = await _ensure_browser_via_electron_pane(*electron_pane, quiet=quiet)
        except _PaneUnreachable as exc:
            # The shell is gone (the bridge does not answer at all): there is no pane to be
            # loyal to. A page already open in the separate Chrome keeps working; otherwise
            # one is launched below, and the model is told so, never silently.
            if _PAGE is not None and _PAGE_MODE == "headless" and not _PAGE.is_closed():
                return _PAGE
            _log("engine", f"Electron pane not reachable, using a separate Chrome: {exc}")
            _note(
                "the embedded browser pane is not reachable, so this is a separate headless Chrome "
                "the owner cannot see."
            )
        else:
            _LAUNCH_ERROR = None
            _log("engine", f"Chrome ready (Electron embedded pane, CDP port {electron_pane[0]})")
            return page

    if _PAGE is not None and _PAGE_MODE == "headless" and _CONTEXT is not None:
        # The page we held was closed (a popup that closed itself, or the owner closed it). The
        # context may still hold others: continue on the most recent one instead of relaunching.
        live = [p for p in _CONTEXT.pages if not p.is_closed()]
        if live:
            _PAGE = live[-1]
            _note(f"the previous tab was closed; now on the most recent open tab ({_PAGE.url or 'blank'}).")
            return _PAGE

    headless = os.environ.get("DOURMOUSE_BROWSER_HEADLESS", "1").strip() != "0"
    try:
        if _PW is None:
            _PW = await async_playwright().start()
        browser = await _PW.chromium.launch(channel="chrome", headless=headless)
    except Exception as exc:  # noqa: BLE001 - launch failures, readable
        _LAUNCH_ERROR = (
            f"BROWSER LAUNCH FAILED: {type(exc).__name__}: {exc} — the agent "
            "drives the system Google Chrome (channel='chrome'); install "
            "Google Chrome if it is missing. Nothing was opened."
        )
        raise RuntimeError(_LAUNCH_ERROR) from exc
    try:
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36 Dourmouse"
            ),
        )
        page = await context.new_page()
        await _prepare_context(context)
    except Exception as exc:  # noqa: BLE001 - context failures, readable
        _LAUNCH_ERROR = f"BROWSER CONTEXT FAILED: {type(exc).__name__}: {exc}"
        raise RuntimeError(_LAUNCH_ERROR) from exc
    _CONTEXT = context
    _PAGE = page
    _PAGE_MODE = "headless"
    _LAUNCH_ERROR = None  # a fresh launch just succeeded — don't keep reporting the old one
    _log("engine", "Chrome ready (headless)" if headless else "Chrome ready (visible)")
    return page


def _is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# --------------------------------------------------------------------------- #
# Element location (async)
# --------------------------------------------------------------------------- #


async def _find(page: Any, target: str) -> tuple[Any, str]:
    """Find ONE element by label / placeholder / button name / CSS / text.

    Returns (locator, how-found) — the first strategy with a match wins.
    """
    target = (target or "").strip()
    if not target:
        raise RuntimeError("ERROR: a target (label/name/CSS/text) is required.")
    if target.lower().startswith("css:"):
        sel = target[4:].strip()
        loc = page.locator(sel)
        if await loc.count() > 0:
            return loc.first, f"css:{sel}"
        raise RuntimeError(f"ERROR: no element matches css:{sel!r}.")
    strategies = [
        ("label", lambda: page.get_by_label(target, exact=False)),
        ("placeholder", lambda: page.get_by_placeholder(target)),
        ("name", lambda: page.get_by_role("button", name=target, exact=False)),
        ("link", lambda: page.get_by_role("link", name=target, exact=False)),
        ("css", lambda: page.locator(target)),
        ("text", lambda: page.get_by_text(target, exact=False).first),
    ]
    for how, make in strategies:
        try:
            loc = make()
            if await loc.count() > 0:
                return loc.first, how
        except Exception:  # noqa: BLE001 - a strategy may throw; try the next
            continue
    raise RuntimeError(
        f"ERROR: no element found for {target!r} — use browser_snapshot to see "
        "the exact labels/names on the page."
    )


# --------------------------------------------------------------------------- #
# Stable element ids (phase C1).
#
# browser_snapshot gives every interactive element a short id (e12). The ids are
# kept by a script that runs in an ISOLATED WORLD of the page (CDP
# Page.createIsolatedWorld), so no page script can read, forge or reassign one, and
# nothing is written to the page's DOM. A click, fill, type, select or extract by id
# re-resolves the element at that moment and refuses, with the reason, when it is gone,
# hidden, disabled, covered, or when the page navigated since the snapshot. The plain
# label, CSS ("css:...") and text targets keep working exactly as before.
# --------------------------------------------------------------------------- #


class _IdRefused(RuntimeError):
    """An element id that cannot be used right now; ``code`` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _element_id(target: str) -> int | None:
    """The number in an element-id target (``e12``, ``id:e12``, ``@e12``, ``[e12]``), else None."""
    m = _ELEMENT_ID_RE.match((target or "").strip())
    return int(m.group(1)) if m else None


@functools.lru_cache(maxsize=1)
def _script_source() -> str:
    try:
        return _SCRIPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"ELEMENT IDS UNAVAILABLE: the injected script {_SCRIPT_PATH} could not be read ({exc})."
        ) from exc


@functools.lru_cache(maxsize=1)
def _media_script_source() -> str:
    try:
        return _MEDIA_SCRIPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"MEDIA CONTROL UNAVAILABLE: the injected script {_MEDIA_SCRIPT_PATH} could not be read ({exc}).") from exc


def _is_gone_context(exc: Exception) -> bool:
    return "Cannot find context with specified id" in str(exc) or "Execution context was destroyed" in str(exc)


async def _make_world(page: Any, session: Any = None) -> dict[str, Any]:
    """A fresh isolated world on the page's main frame with the id script loaded into it."""
    for stale in [p for p in _WORLDS if p.is_closed()]:
        del _WORLDS[stale]
    if session is None:
        session = await page.context.new_cdp_session(page)
    last: Exception | None = None
    for attempt in range(4):
        try:
            tree = await session.send("Page.getFrameTree")
            created = await session.send(
                "Page.createIsolatedWorld",
                {"frameId": tree["frameTree"]["frame"]["id"], "worldName": "dourmouse-agent"},
            )
            ctx = created["executionContextId"]
            for source in (_script_source(), _media_script_source()):
                injected = await session.send(
                    "Runtime.evaluate", {"expression": source, "contextId": ctx, "returnByValue": True}
                )
                if injected.get("exceptionDetails"):
                    raise RuntimeError(str(injected["exceptionDetails"].get("text") or "script error"))
            # "base": every id this world will ever issue is >= it, so a smaller id that was issued
            # on this same page belongs to an earlier document: the page navigated since.
            world = {"session": session, "ctx": ctx, "href": "", "gen": 0, "base": _NEXT_ID}
            _WORLDS[page] = world
            return world
        except Exception as exc:  # noqa: BLE001 - a frame mid-navigation has no context yet: retry
            last = exc
            await asyncio.sleep(0.15 * (attempt + 1))
    raise RuntimeError(
        f"ELEMENT IDS UNAVAILABLE on this page ({type(last).__name__}: {last}). Target elements by "
        "label, text or css: instead."
    )


async def _world_call(page: Any, op: str, params: dict[str, Any], *, fresh_ok: bool, api: str = "__dmAgent") -> Any:
    """Run one op of the id script (or, with ``api="__dmMedia"``, the media script) in the page's
    isolated world. ``fresh_ok`` lets a snapshot build a new world; an id action must find the one
    the snapshot built, because a missing or destroyed world means the page navigated and every id
    from before is dead. An op that returns a promise is awaited."""
    world = _WORLDS.get(page)
    if world is None or world.get("ctx") is None:
        if not fresh_ok:
            raise _IdRefused("unknown" if world is None else "navigated", "")
        world = await _make_world(page, world["session"] if world else None)
    for second_try in (False, True):
        try:
            res = await world["session"].send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": f"function(op, p) {{ return globalThis.{api}.run(op, p); }}",
                    "executionContextId": world["ctx"],
                    "arguments": [{"value": op}, {"value": params}],
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 - the context is gone after a navigation
            if not _is_gone_context(exc):
                raise
            world["ctx"] = None
            if not fresh_ok or second_try:
                raise _IdRefused("navigated", "") from exc
            world = await _make_world(page, world["session"])
            continue
        if res.get("exceptionDetails"):
            detail = (res["exceptionDetails"].get("exception") or {}).get("description") or res["exceptionDetails"].get("text")
            raise RuntimeError(f"ELEMENT IDS script error: {detail}")
        return (res.get("result") or {}).get("value")
    raise RuntimeError("ELEMENT IDS UNAVAILABLE: the page kept replacing its document.")  # pragma: no cover


async def _element_snapshot(page: Any, max_elems: int) -> dict[str, Any]:
    global _NEXT_ID
    data = await _world_call(page, "snapshot", {"start": _NEXT_ID, "max": max_elems}, fresh_ok=True)
    _NEXT_ID = max(_NEXT_ID, int(data["nextId"]))
    for issued in data.get("fresh") or []:
        _ID_OWNER[int(issued)] = page
    if len(_ID_OWNER) > _ID_OWNER_CAP:
        for old in sorted(_ID_OWNER)[: len(_ID_OWNER) - _ID_OWNER_CAP]:
            del _ID_OWNER[old]
    world = _WORLDS.get(page)
    if world is not None:
        world["href"], world["gen"] = data.get("href", ""), data.get("gen", 0)
    return data


def _refusal(code: str, num: int, res: dict[str, Any], page: Any) -> _IdRefused:
    label = f"e{num}" + (f" ({res['name']!r})" if res.get("name") else "")
    gen = _WORLDS.get(page, {}).get("gen") or res.get("gen") or 0
    snap = f"snapshot #{gen}" if gen else "the last snapshot"
    again = " Call browser_snapshot for fresh ids."
    if code == "navigated":
        was = res.get("was") or _WORLDS.get(page, {}).get("href") or ""
        now = res.get("now") or ""
        where = f" (it was {was}, it is {now})" if was and now else ""
        return _IdRefused(code, f"REFUSED: element {label} is stale: the page navigated since {snap}{where}. Ids die with the page.{again}")
    if code == "unknown":
        owner = _ID_OWNER.get(num)
        base = _WORLDS.get(page, {}).get("base", 0)
        if owner is page and num < base:
            return _refusal("navigated", num, res, page)
        if owner is not None and owner is not page:
            return _IdRefused(code, f"REFUSED: element {label} was issued on a different tab than the one the agent is on now (the agent follows the tab the owner is viewing).{again}")
        if num >= _NEXT_ID or owner is None:
            return _IdRefused(code, f"REFUSED: element id {label} was never issued on this tab. Ids come from browser_snapshot and from the page report of a click or open.{again}")
        return _IdRefused(code, f"REFUSED: element {label} is not known on this tab any more.{again}")
    if code == "gone":
        return _IdRefused(code, f"REFUSED: element {label} is no longer on the page: it was removed or the page re-rendered since {snap}.{again}")
    if code == "hidden":
        return _IdRefused(code, f"REFUSED: element {label} is hidden now (not displayed or zero size). Nothing was done.{again}")
    if code == "disabled":
        return _IdRefused(code, f"REFUSED: element {label} is disabled. Nothing was done.")
    if code == "readonly":
        return _IdRefused(code, f"REFUSED: element {label} is read-only. Nothing was done.")
    if code == "obscured":
        return _IdRefused(code, f"REFUSED: element {label} is covered by <{res.get('by', 'another element')}> at its centre, so a click would hit that instead. Close the overlay or scroll, then retry.")
    if code == "wrongtype":
        return _IdRefused(code, f"REFUSED: element {label}: {res.get('detail', 'the element does not take that action')}.")
    if code == "nooption":
        return _IdRefused(code, f"REFUSED: element {label} has no such option. Options: {', '.join(map(repr, res.get('options') or []))}.")
    return _IdRefused(code, f"REFUSED: element {label} could not be used ({code}).{again}")


async def _id_prepare(page: Any, num: int, op: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Re-resolve element ``num`` right now and get it ready for ``op`` (scrolled into view,
    focused, hit-tested for a click). Raises _IdRefused with a readable reason, doing nothing."""
    try:
        res = await _world_call(page, "prepare", {"id": num, "op": op, **(extra or {})}, fresh_ok=False)
    except _IdRefused as exc:
        raise _refusal(exc.code, num, {}, page) from None
    if not res.get("ok"):
        raise _refusal(str(res.get("code")), num, res, page)
    return res


async def _click_by_id(page: Any, num: int, claim: _NoClaim | None = None) -> dict[str, Any]:
    claim = claim or _NoClaim(page)
    try:
        res = await _id_prepare(page, num, "click")
    except _IdRefused as exc:
        if exc.code != "obscured":
            raise
        await asyncio.sleep(0.25)  # an animation or a closing overlay: look once more
        res = await _id_prepare(page, num, "click")
    await claim.check()
    async with claim.pointing(res["x"], res["y"]):
        await page.mouse.click(res["x"], res["y"])
    return res


async def _insert_chunks(claim: _NoClaim, text: str, size: int, done_box: list[int]) -> None:
    """Insert ``text`` in chunks through the claim, counting into ``done_box[0]``. In the pane
    each chunk is a separate main-process step the owner's input can cut in front of."""
    for start in range(0, len(text), size):
        chunk = text[start : start + size]
        await claim.insert(chunk)
        done_box[0] += len(chunk)


def _progress(done: int, total: int, who: str, text: str) -> str:
    if done >= total:
        return f"All {total} characters had already been typed into {who}."
    left = text[done:]
    shown = left if len(left) <= 60 else left[:57] + "..."
    if done == 0:
        return f"Nothing was typed into {who}."
    return f"Typed {done} of {total} characters into {who}; NOT typed: the last {total - done} ({shown!r})."


async def _fill_by_id(page: Any, num: int, value: str, claim: _NoClaim | None = None) -> dict[str, Any]:
    claim = claim or _NoClaim(page)
    res = await _id_prepare(page, num, "fill", {"value": value})
    if res.get("done"):
        return res
    if res.get("focused") is False:
        raise RuntimeError(f"BROWSER FILL FAILED: element e{num} would not take focus.")
    await claim.check()
    if value == "":
        await page.keyboard.press("Backspace")  # the field's contents are selected: this clears them
        return res
    who = f"e{num} ({res.get('name', '')!r})"
    done = [0]
    try:
        await _insert_chunks(claim, value, _FILL_CHUNK, done)
    except _OwnerControl as exc:
        raise exc.with_progress(_progress(done[0], len(value), who, value)) from None
    return res


_MULTILINE_TAGS = {"textarea"}
_TYPE_MODES = ("text", "keys")


async def _focus_still(page: Any, num: int | None, tag: str, claim: _NoClaim | None = None) -> None:
    """Between two chunks: the text must still be going where it started. When the focus moved,
    the claim is asked first: if the owner's click moved it, that is what the agent reports (seen
    live: the owner's click lands before the next chunk and takes the focus with it); otherwise the
    page moved it, and the typing stops too."""
    try:
        await _focus_unmoved(page, num, tag)
    except _OwnerControl:
        refusal = await claim.verdict() if claim is not None else None
        if refusal is not None:
            raise refusal from None
        raise


async def _focus_unmoved(page: Any, num: int | None, tag: str) -> None:
    if num is not None:
        try:
            res = await _world_call(page, "focusCheck", {"id": num}, fresh_ok=False)
        except _IdRefused:
            raise _OwnerControl("focus-moved", "STOPPED: the page navigated while the agent was typing, so the agent stopped.") from None
        ok = bool(res and res.get("ok"))
    else:
        state = await _world_call(page, "focusState", {}, fresh_ok=True)
        ok = bool(state and state.get("has") and state.get("tag", "") == tag)
    if not ok:
        raise _OwnerControl("focus-moved", "STOPPED: the keyboard focus left the element the agent was typing into (the page moved it), so the agent stopped rather than type somewhere else.")


async def _type_text(page: Any, num: int | None, text: str, clear: bool, delay_ms: int, mode: str = "text", claim: _NoClaim | None = None) -> str:
    """Type into an element (an id from browser_snapshot) or into whatever has focus.

    ``text`` mode (default) inserts the text the way an input method does, in short chunks: it
    reaches editors that take no value and listen for input (a contenteditable, Google Docs'
    hidden text iframe) and never presses Enter, so a line break cannot submit anything. ``keys``
    mode sends one real key event per character, for widgets that only listen to key presses.
    In the shared pane every chunk is a separate step: the owner's own key, click or scroll in the
    tab stops the typing before the next chunk, and the error says how far it got."""
    claim = claim or _NoClaim(page)
    check_num = num  # whose focus is re-checked between chunks; None means "the same kind of focus"
    if num is not None:
        res = await _id_prepare(page, num, "type", {"clear": bool(clear)})
        tag = res.get("tag", "")
        multiline = bool(res.get("multiline"))
        who = f"e{num} ({res.get('name', '')!r})"
        if res.get("focused") is False and res.get("clickAt"):
            # An editor surface that takes focus only from a click (a canvas editor): click it,
            # as a person would, then type into whatever the editor focused (its text frame).
            await claim.check()
            at = res["clickAt"]
            async with claim.pointing(at["x"], at["y"]):
                await page.mouse.click(at["x"], at["y"])
            await asyncio.sleep(0.05)
            state = await _world_call(page, "focusState", {}, fresh_ok=True)
            if not state or not state.get("has") or state.get("editable") is False:
                raise RuntimeError(f"BROWSER TYPE FAILED: clicking e{num} did not put a text cursor anywhere. Nothing was typed.")
            res = {**res, "focused": True, "frame": bool(state.get("frame"))}
            tag, multiline = state.get("tag", ""), bool(state.get("multiline"))
        if res.get("focused") is False:
            raise RuntimeError(f"BROWSER TYPE FAILED: element e{num} would not take focus.")
        if res.get("frame"):
            who = f"{who} through its editor frame"
            check_num, tag = None, "iframe"
    else:
        if clear:
            raise RuntimeError("ERROR: clear needs a target (an element id from browser_snapshot).")
        state = await _world_call(page, "focusState", {}, fresh_ok=True)
        if not state or not state.get("has"):
            raise RuntimeError("ERROR: nothing has focus; pass a target (an element id from browser_snapshot), or click into the editor first.")
        tag, multiline = state.get("tag", ""), bool(state.get("multiline"))
        if mode == "text" and state.get("editable") is False:
            raise RuntimeError(
                f"ERROR: the focused element (<{tag}>{' in a frame' if state.get('frame') else ''}) does not take text, "
                "so nothing would arrive. Click into the field or editor first, or use mode 'keys' for a widget "
                "that listens to key presses. Nothing was typed."
            )
        who = "the focused element"
        if state.get("frame"):
            inner = state.get("inner") or ""
            who = f"the focused element inside a frame{' (' + inner + ')' if inner else ''}"
    if "\n" in text or "\r" in text:
        if mode == "keys" and tag not in _MULTILINE_TAGS:
            raise RuntimeError(
                "REFUSED: a line break was typed into something that is not a <textarea>: in keys mode "
                "Enter would submit or send. Use the default text mode for an editor, or browser_press "
                "Enter (it asks for confirmation)."
            )
        if mode == "text" and not multiline:
            raise RuntimeError(
                f"REFUSED: a line break was asked for in a one-line field (<{tag or 'input'}>): it would be lost "
                "or turned into a space. Line breaks go into a <textarea>, a contenteditable editor or an "
                "editor frame. Nothing was typed."
            )
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    await claim.check()
    done = 0
    try:
        if mode == "keys":
            delay = max(0, min(delay_ms, 500))
            for start in range(0, len(text), _TYPE_CHUNK):
                chunk = text[start : start + _TYPE_CHUNK]
                if start:
                    await claim.check()
                    await _focus_still(page, check_num, tag, claim)
                await page.keyboard.type(chunk, delay=delay)
                done += len(chunk)
        else:
            pause = max(0, min(delay_ms, 500)) / 1000.0
            for start in range(0, len(text), _TYPE_CHUNK):
                chunk = text[start : start + _TYPE_CHUNK]
                if start:
                    await _focus_still(page, check_num, tag, claim)
                await claim.insert(chunk)
                done += len(chunk)
                if pause and done < len(text):
                    await asyncio.sleep(pause)
    except _OwnerControl as exc:
        raise exc.with_progress(_progress(done, len(text), who, text)) from None
    return who


async def _extract_by_id(page: Any, num: int) -> str:
    res = await _id_prepare(page, num, "text")
    return str(res.get("text", ""))


async def _page_summary(page: Any, max_elems: int = 60) -> str:
    """Readable state of the current page: URL, title, tab, interactive elements with ids."""
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = "(no title)"
    url = page.url or "(none)"
    lines = [f"URL: {url}", f"TITLE: {title}"]
    if _PAGE_MODE == "pane" and _PANE_SEEN.get("active") is not None:
        lines.append(
            f"TAB: tab {_PANE_SEEN.get('active')}, one of {len(_PANE_SEEN.get('ids') or ())} open "
            "(the tab the owner is viewing)"
        )
    try:
        snap = await _element_snapshot(page, max(1, min(int(max_elems), 300)))
    except Exception as exc:  # noqa: BLE001 - say so; the label, text and css targets still work
        lines.append(f"ELEMENTS: unavailable ({type(exc).__name__}: {str(exc)[:160]}). Use label, text or css: targets.")
        snap = None
    if snap is not None:
        items = snap.get("items") or []
        lines.append(f"SNAPSHOT: #{snap.get('gen')}. Element ids (e12) are valid until the page navigates or the element disappears.")
        if items:
            lines.append("ELEMENTS:")
            for e in items:
                ident = f"e{e['id']} " if e.get("id") else ""
                kind = f"{e['t']}:{e['type']}" if e.get("type") and e["type"] != "text" else e["t"]
                v = f"  value={e['val']!r}" if e.get("val") else ""
                fl = f"  ({', '.join(e['flags'])})" if e.get("flags") else ""
                opts = f"  options={e['options']!r}" if e.get("options") else ""
                lines.append(f"- {ident}[{kind}] {e['name']!r}{v}{fl}{opts}")
            more = int(snap.get("total", 0)) - len(items)
            if more > 0:
                lines.append(f"... {more} more elements not shown (raise max_elements).")
        else:
            lines.append("ELEMENTS: none found (static page?)")
        if snap.get("hiddenOmitted"):
            lines.append(f"({snap['hiddenOmitted']} hidden elements left out.)")
    frames = max(0, len(page.frames) - 1)
    if frames:
        lines.append(f"IFRAMES: {frames} (their contents are not listed; target them by css: or text).")
    try:
        body = await page.locator("body").inner_text(timeout=2000)
        sample = " ".join(body.split())[:900]
        if sample:
            lines.append(f"PAGE TEXT: {sample}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Following the page the owner is looking at (phase C1)
# --------------------------------------------------------------------------- #


def _action_mark() -> dict[str, Any]:
    """What the world looked like just before an action that may open or close a tab."""
    return {"new": len(_NEW_PAGES), "seen": dict(_PANE_SEEN)}


async def _settle_after_action(page: Any, mark: dict[str, Any]) -> Any:
    """After a click, Enter or a submit: if the action opened a tab (a target=_blank link,
    window.open) or closed the one it was on, move to the page the owner now sees and say so.
    Returns the page the next step should use."""
    global _PAGE
    pane = _electron_pane_configured()
    if _PAGE_MODE == "pane" and pane is not None:
        before = mark["seen"]
        for delay in _POPUP_POLL_SECONDS:
            if delay:
                await asyncio.sleep(delay)
            try:
                data = await _bridge_tabs(pane[1])
            except _PaneUnreachable:
                break
            ids = frozenset(t.get("id") for t in (data.get("tabs") or []) if isinstance(t, dict))
            if data.get("active") != before.get("active") or ids != before.get("ids") or page.is_closed():
                opened = sorted(i for i in ids - before.get("ids", frozenset()) if i is not None)
                tabs_by_id = {t.get("id"): t for t in (data.get("tabs") or []) if isinstance(t, dict)}
                new_page = await _ensure_browser(quiet=True)
                if opened and data.get("active") in opened:
                    _note(f"your action opened {_tab_label(tabs_by_id[data['active']])} and the agent switched to it (the owner sees it too).")
                elif opened:
                    _note(f"your action opened {', '.join(_tab_label(tabs_by_id[i]) for i in opened)} in the background; the agent stays on its tab.")
                elif page.is_closed():
                    _note("your action closed the tab it was on; the agent re-attached to the tab the owner is viewing.")
                else:
                    _note(f"the tab the owner is viewing changed while the action ran: now on {_tab_label(tabs_by_id.get(data.get('active'), {'id': data.get('active')}))}.")
                return new_page
        if page.is_closed():
            return await _ensure_browser()
        return page
    # A separate headless Chrome: a popup is a new page in the context.
    for delay in _POPUP_POLL_SECONDS:
        if delay:
            await asyncio.sleep(delay)
        fresh = [p for p in _NEW_PAGES[mark["new"] :] if not p.is_closed()]
        if fresh:
            newest = fresh[-1]
            try:
                await newest.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception as exc:  # noqa: BLE001 - a slow popup is still the page to work on
                _log("engine", f"popup still loading: {type(exc).__name__}")
            _PAGE = newest
            _note(f"your action opened a new tab ({newest.url or 'blank'}); the agent switched to it.")
            return newest
    if page.is_closed():
        return await _ensure_browser()
    return page


# --------------------------------------------------------------------------- #
# Tool handlers (sync wrappers over the browser thread)
# --------------------------------------------------------------------------- #


def browser_status() -> dict[str, Any]:
    """Engine/state report for /api/browser/status (never opens the browser)."""
    from importlib.util import find_spec

    engine = find_spec("playwright") is not None
    headless = os.environ.get("DOURMOUSE_BROWSER_HEADLESS", "1").strip() != "0"
    with _ACTIVITY_LOCK:
        activity = list(_ACTIVITY[-20:])
    creds = _vault_sites()
    return {
        "engine": "playwright + system Chrome" if engine else "not installed",
        "ready": engine,
        "headless": headless,
        "launch_error": _LAUNCH_ERROR,
        "page": None,
        "sites": creds,
        "shots": sorted(p.name for p in _SHOTS_DIR.glob("*.png"))[-5:] if _SHOTS_DIR.exists() else [],
        "activity": activity,
        "note": _UA_NOTE,
    }


def browser_activity(limit: int = 50) -> list[dict[str, str]]:
    with _ACTIVITY_LOCK:
        return list(_ACTIVITY[-max(1, min(limit, 300)):])


def browser_open(arguments: dict[str, Any]) -> str:
    url = (arguments.get("url") or "").strip()
    if not url:
        return "ERROR: browser_open requires a url."
    if not _is_http_url(url):
        return (
            f"REFUSED: only http(s):// URLs are ever opened — got {url!r}. "
            "Local files, chrome:// and javascript: are never navigated to."
        )

    async def _go():
        page = await _ensure_browser()
        async with _acting(page, "open"):
            try:
                await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001 - navigation failures, readable
                _log("open", f"{url} -> {type(exc).__name__}")
                raise RuntimeError(
                    f"BROWSER OPEN FAILED: {type(exc).__name__}: {exc} (url={url})"
                ) from exc
        return await _page_summary(page)

    _log("open", url)
    return _call(_go)


def browser_snapshot(arguments: dict[str, Any]) -> str:
    max_elems = int(arguments.get("max_elements", 60) or 60)

    async def _snap():
        page = await _ensure_browser()
        return await _page_summary(page, max_elems)

    _log("snapshot", "page state read")
    return _call(_snap)


def browser_fill(arguments: dict[str, Any]) -> str:
    target = (arguments.get("target") or "").strip()
    value = arguments.get("value")
    if value is None:
        return "ERROR: browser_fill requires a value."

    async def _fill():
        page = await _ensure_browser()
        async with _acting(page, "fill") as claim:
            num = _element_id(target)
            if num is not None:
                res = await _fill_by_id(page, num, str(value), claim)
                return f"FILLED e{num} ({res.get('name', '')!r}) (via element id)."
            loc, how = await _find(page, target)
            await claim.check()
            try:
                await loc.fill(str(value), timeout=8_000)
            except Exception as exc:  # noqa: BLE001 - element may be read-only etc
                raise RuntimeError(
                    f"BROWSER FILL FAILED: {type(exc).__name__}: {exc} (target={target!r})"
                ) from exc
            return f"FILLED {target!r} (via {how})."

    _log("fill", f"{target!r} <- ({len(str(value))} characters)")
    return _call(_fill)


def browser_fill_form(arguments: dict[str, Any]) -> str:
    fields = arguments.get("fields")
    if not isinstance(fields, dict) or not fields:
        return "ERROR: browser_fill_form requires a 'fields' object of label->value."
    if len(fields) > 25:
        return "REFUSED: browser_fill_form accepts at most 25 fields per call."
    done: list[str] = []

    async def _fill():
        page = await _ensure_browser()
        async with _acting(page, "fill_form") as claim:
            try:
                for label, value in fields.items():
                    await claim.check()  # the owner may have started on this form: stop between fields
                    num = _element_id(str(label))
                    if num is not None:
                        await _fill_by_id(page, num, str(value), claim)
                        done.append(f"{label} (via element id)")
                        continue
                    loc, how = await _find(page, str(label))
                    try:
                        await loc.fill(str(value), timeout=8_000)
                        done.append(f"{label} (via {how})")
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(
                            f"BROWSER FILL FORM FAILED on {label!r}: {type(exc).__name__}: {exc}"
                        ) from exc
            except _OwnerControl as exc:
                filled = [d.split(" (via ")[0] for d in done]
                rest = [str(k) for k in fields if str(k) not in filled]
                raise exc.with_progress(
                    f"Filled {len(filled)} of {len(fields)} fields"
                    + (f" ({', '.join(filled)})" if filled else "")
                    + f"; NOT filled: {', '.join(rest)}."
                ) from None
        return f"FILLED {len(done)} fields: " + ", ".join(done) + "."

    _log("fill_form", f"{len(fields)} fields")
    return _call(_fill)


def browser_click(arguments: dict[str, Any]) -> str:
    target = (arguments.get("target") or "").strip()

    async def _click():
        page = await _ensure_browser()
        mark = _action_mark()
        async with _acting(page, "click") as claim:
            num = _element_id(target)
            if num is not None:
                res = await _click_by_id(page, num, claim)
                what, how = f"e{num} ({res.get('name', '')!r})", "element id"
            else:
                loc, how = await _find(page, target)
                what = repr(target)
                await claim.check()
                try:
                    async with claim.pointing():
                        await loc.click(timeout=8_000)
                except _OwnerControl:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"BROWSER CLICK FAILED: {type(exc).__name__}: {exc} (target={target!r})"
                    ) from exc
            page = await _settle_after_action(page, mark)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:  # noqa: BLE001 - a click need not navigate
            pass
        return f"CLICKED {what} (via {how}).\n" + await _page_summary(page)

    _log("click", target)
    return _call(_click)


def browser_select(arguments: dict[str, Any]) -> str:
    target = (arguments.get("target") or "").strip()
    value = (arguments.get("value") or "").strip()
    if not value:
        return "ERROR: browser_select requires a value."

    async def _select():
        page = await _ensure_browser()
        async with _acting(page, "select") as claim:
            num = _element_id(target)
            if num is not None:
                res = await _id_prepare(page, num, "select", {"value": value})
                return f"SELECTED {res.get('chose', value)!r} on e{num} ({res.get('name', '')!r}) (via element id)."
            loc, how = await _find(page, target)
            await claim.check()
            try:
                await loc.select_option(value, timeout=8_000)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"BROWSER SELECT FAILED: {type(exc).__name__}: {exc} (target={target!r})"
                ) from exc
            return f"SELECTED {value!r} on {target!r} (via {how})."

    _log("select", f"{target!r} <- {value}")
    return _call(_select)


def browser_type(arguments: dict[str, Any]) -> str:
    """Type text into an element, appended at the caret (or replacing everything when ``clear``
    is true). ``target`` is an element id from browser_snapshot; without one the text goes to
    whatever has focus, which is how a Google Docs style editor is reached (it types through a
    hidden frame). Use this where browser_fill does not reach.

    ``mode`` "text" (default) inserts the text as an input method does, in short chunks: it works
    in editors that take no value and it never presses Enter, so a line break cannot submit.
    ``mode`` "keys" sends a real key event per character, for widgets that only listen to key
    presses; a line break then goes only into a textarea. In the shared pane the owner's own
    input in the tab stops the typing between chunks, and the result says how far it got."""
    target = (arguments.get("target") or "").strip()
    text = arguments.get("text", arguments.get("value"))
    if text is None:
        return "ERROR: browser_type requires text."
    text = str(text)
    if len(text) > 5000:
        return "REFUSED: browser_type accepts at most 5000 characters per call."
    clear = bool(arguments.get("clear", False))
    mode = str(arguments.get("mode") or "text").strip().lower()
    if mode not in _TYPE_MODES:
        return "ERROR: browser_type mode must be 'text' (default) or 'keys'."
    try:
        delay_ms = int(arguments.get("delay_ms", 20))
    except (TypeError, ValueError):
        delay_ms = 20
    num = _element_id(target) if target else None
    if target and num is None:
        return (
            "ERROR: browser_type takes an element id from browser_snapshot (for example e12) as its "
            "target, or no target to type into whatever has focus. Use browser_fill for a label."
        )

    async def _type():
        page = await _ensure_browser()
        async with _acting(page, "type") as claim:
            who = await _type_text(page, num, text, clear, delay_ms, mode, claim)
        return f"TYPED {len(text)} characters into {who}."

    _log("type", f"{target or 'focus'} <- ({len(text)} characters, {mode} mode)")
    return _call(_type, timeout=max(60.0, 30.0 + len(text) * max(0, min(delay_ms, 500)) / 1000.0 * (1 if mode == "keys" else 1 / _TYPE_CHUNK)))


def browser_press(arguments: dict[str, Any]) -> str:
    key = (arguments.get("key") or "").strip()
    if not key:
        return "ERROR: browser_press requires a key (Enter, Tab, Escape...)."

    async def _press():
        page = await _ensure_browser()
        mark = _action_mark()
        async with _acting(page, "press"):
            try:
                await page.keyboard.press(key, timeout=8_000)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"BROWSER PRESS FAILED: {type(exc).__name__}: {exc} (key={key!r})"
                ) from exc
            page = await _settle_after_action(page, mark)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        return f"PRESSED {key}.\n" + await _page_summary(page)

    _log("press", key)
    return _call(_press)


def browser_submit(arguments: dict[str, Any]) -> str:
    """Submit the active form (login / signup / search). CONFIRMATION-GATED."""
    note = (arguments.get("note") or "").strip()

    async def _submit():
        page = await _ensure_browser()
        mark = _action_mark()
        url_before = page.url
        async with _acting(page, "submit") as claim:
            # Prefer Enter on the focused field, else click the submit control.
            try:
                focused = await page.evaluate("document.activeElement && document.activeElement.tagName")
            except Exception:  # noqa: BLE001
                focused = None
            await claim.check()
            if focused in ("INPUT", "TEXTAREA", "SELECT"):
                await page.keyboard.press("Enter", timeout=8_000)
            else:
                sub = page.locator(
                    "button[type='submit'], input[type='submit'], "
                    "button:has-text('Sign in'), button:has-text('Log in'), "
                    "button:has-text('Create account'), button:has-text('Continue')"
                )
                if await sub.count() > 0:
                    async with claim.pointing():
                        await sub.first.click(timeout=8_000)
                else:
                    raise RuntimeError(
                        "ERROR: no submit control found, so nothing was submitted. "
                        "Use browser_snapshot to inspect the form."
                    )
            page = await _settle_after_action(page, mark)
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001 - networkidle is best-effort
            pass
        url_after = page.url
        moved = "PAGE CHANGED" if url_after != url_before else "SAME PAGE"
        _log("submit", f"{url_before} -> {url_after} ({moved})")
        return f"SUBMITTED the active form ({moved}).\n" + await _page_summary(page)

    _log("submit", f"note={note or 'form submit'}")
    return _call(_submit)


def browser_wait(arguments: dict[str, Any]) -> str:
    try:
        ms = int(arguments.get("ms", 1000))
    except (TypeError, ValueError):
        return "ERROR: browser_wait requires an integer ms."
    ms = max(0, min(ms, 60_000))

    async def _wait():
        page = await _ensure_browser()
        await page.wait_for_timeout(ms)
        return f"WAITED {ms}ms — page still live at {page.url}."

    _log("wait", f"{ms}ms")
    return _call(_wait)


def browser_back(arguments: dict[str, Any]) -> str:
    async def _back():
        page = await _ensure_browser()
        async with _acting(page, "back"):
            try:
                await page.go_back(timeout=15_000)
            except Exception:  # noqa: BLE001
                pass
        return await _page_summary(page)

    _log("back", "go back")
    return _call(_back)


def browser_extract(arguments: dict[str, Any]) -> str:
    target = (arguments.get("target") or "").strip()

    async def _extract():
        page = await _ensure_browser()
        num = _element_id(target)
        if num is not None:
            return f"EXTRACTED e{num} (via element id):\n{(await _extract_by_id(page, num))[:4000]}"
        loc, how = await _find(page, target)
        try:
            text = await loc.inner_text(timeout=8_000)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"BROWSER EXTRACT FAILED: {type(exc).__name__}: {exc} (target={target!r})"
            ) from exc
        return f"EXTRACTED {target!r} (via {how}):\n{text[:4000]}"

    _log("extract", target)
    return _call(_extract)


# --------------------------------------------------------------------------- #
# Media control (phase C2): YouTube and any page with a <video> or <audio>
# --------------------------------------------------------------------------- #

_MEDIA_ACTIONS = ("status", "play", "pause", "seek", "mute", "unmute", "volume")
_TIME_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$")


def _seconds(value: Any) -> float | None:
    """A time as seconds: a number, "83.5", "1:23" or "1:02:03"."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    m = _TIME_RE.match(text)
    if m:
        h, mi, s = m.group(1), m.group(2), m.group(3)
        return int(h or 0) * 3600 + int(mi) * 60 + float(s)
    try:
        return float(text)
    except ValueError:
        return None


def _clock(sec: Any) -> str:
    if not isinstance(sec, (int, float)):
        return "unknown"
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _media_report(st: dict[str, Any], did: str) -> str:
    if not st.get("found"):
        return (
            f"NO MEDIA: there is no <video> or <audio> on this page ({st.get('url') or 'no page'}). "
            "Open a video page first (for YouTube, a watch page: https://www.youtube.com/watch?v=...)."
        )
    where = "YouTube video" if st.get("youtube") else st.get("kind", "media")
    dur = st.get("duration")
    lines = [
        f"MEDIA ({where}): {st.get('title') or '(no title)'!r}",
        f"TIME: {_clock(st.get('currentTime'))} of {_clock(dur) if dur is not None else 'a live stream or unknown length'}"
        f" ({st.get('currentTime')}s of {dur if dur is not None else '?'}s)",
        "STATE: " + ", ".join(
            [
                "ended" if st.get("ended") else ("paused" if st.get("paused") else "playing"),
                "muted" if st.get("muted") else "sound on",
                f"volume {round(float(st.get('volume') or 0) * 100)}%",
            ]
            + ([f"speed {st.get('rate')}x"] if st.get("rate") not in (1, 1.0, None) else [])
        ),
    ]
    if st.get("ad"):
        lines.append("NOTE: an advert is playing in the YouTube player; the time and length are the advert's.")
    if st.get("ytMuted") is not None and bool(st.get("ytMuted")) != bool(st.get("muted")):
        lines.append(f"NOTE: YouTube's own mute button says {'muted' if st.get('ytMuted') else 'sound on'}, the video element says {'muted' if st.get('muted') else 'sound on'}.")
    if (st.get("count") or 0) > 1:
        lines.append(f"({st['count']} media elements on the page; this is the main one.)")
    if did:
        lines.insert(0, did)
    if st.get("error"):
        lines.append(f"PROBLEM: {st['error']}")
    lines.append(f"URL: {st.get('url', '')}")
    return "\n".join(lines)


def browser_media(arguments: dict[str, Any]) -> str:
    """Control the main video or audio on the current page (a YouTube watch page included):
    ``action`` is status, play, pause, seek, mute, unmute or volume. ``seek`` takes ``to`` (seconds
    or "m:ss") or ``by`` (seconds, negative goes back); ``volume`` takes ``level`` 0 to 100. Works
    through the page's own HTML5 media element: no YouTube API, no key. Status only reads; every
    other action is a change to the owner's tab and waits for the owner like any browser action."""
    action = str(arguments.get("action") or "status").strip().lower()
    if action not in _MEDIA_ACTIONS:
        return f"ERROR: browser_media action must be one of {', '.join(_MEDIA_ACTIONS)}."
    params: dict[str, Any] = {}
    if action == "seek":
        if arguments.get("by") is not None:
            by = _seconds(arguments.get("by"))
            if by is None:
                return "ERROR: browser_media seek 'by' must be a number of seconds (negative goes back)."
            params["by"] = by
        else:
            to = _seconds(arguments.get("to", arguments.get("seconds")))
            if to is None or to < 0:
                return "ERROR: browser_media seek needs 'to' (seconds or m:ss) or 'by' (seconds)."
            params["to"] = to
    if action == "volume":
        try:
            level = float(str(arguments.get("level")))
        except (TypeError, ValueError):
            return "ERROR: browser_media volume needs 'level' from 0 to 100."
        if not 0 <= level <= 100:
            return "ERROR: browser_media volume 'level' must be from 0 to 100."
        params["level"] = level

    async def _media():
        page = await _ensure_browser()
        if action == "status":
            st = await _world_call(page, "status", {}, fresh_ok=True, api="__dmMedia")
            return _media_report(st or {}, "")
        async with _acting(page, f"media_{action}"):
            st = await _world_call(page, action, params, fresh_ok=True, api="__dmMedia")
        st = st or {}
        if not st.get("found"):
            return _media_report(st, "")
        did = {
            "play": "PLAYING." if not st.get("paused") else "ASKED TO PLAY, but it is still paused (see PROBLEM).",
            "pause": "PAUSED." if st.get("paused") else "ASKED TO PAUSE, but it is still playing.",
            "seek": (
                f"SOUGHT to {_clock(st.get('currentTime'))}."
                if st.get("landed")
                else f"ASKED TO SEEK to {_clock(st.get('seekedTo'))}, but it is at {_clock(st.get('currentTime'))} (see PROBLEM)."
            ),
            "mute": "MUTED." if st.get("muted") and st.get("held", True) else "ASKED TO MUTE, but it is not muted (see PROBLEM).",
            "unmute": "UNMUTED." if not st.get("muted") and st.get("held", True) else "ASKED TO UNMUTE, but it is still muted (see PROBLEM).",
            "volume": (
                f"VOLUME set to {round(float(st.get('volume') or 0) * 100)}%."
                if st.get("held", True)
                else f"ASKED FOR VOLUME {round(params.get('level', 0))}%, but it is {round(float(st.get('volume') or 0) * 100)}% (see PROBLEM)."
            ),
        }[action]
        return _media_report(st, did)

    _log("media", action)
    return _call(_media)


def browser_pane_show(arguments: dict[str, Any]) -> str:
    """Show the real, embedded browser pane (Electron shell only) — the
    SAME Chromium session browser_open/browser_click/browser_fill/... are
    already driving, now visible to the human, not a second/mirrored
    browser. Honest NOT CONFIGURED under the older pywebview shell or a
    plain headless server, neither of which has a pane to show."""
    configured = _electron_pane_configured()
    if configured is None:
        return (
            "NOT CONFIGURED: the embedded browser pane needs the Electron "
            "shell (electron/main.js), which sets DOURMOUSE_ELECTRON_CDP_PORT "
            "and DOURMOUSE_ELECTRON_PANE_PORT when it starts this server — "
            "not available under the older pywebview shell or a plain "
            "headless server."
        )
    _cdp_port, pane_port = configured
    try:
        result = _pane_bridge_request(pane_port, "POST", "/show")
    except Exception as exc:  # noqa: BLE001 - bridge failures, readable
        raise RuntimeError(
            f"BROWSER PANE SHOW FAILED: {type(exc).__name__}: {exc}"
        ) from exc
    if not result.get("ok"):
        raise RuntimeError(f"BROWSER PANE SHOW FAILED: {result}")
    _log("pane", "shown")
    return (
        "BROWSER PANE now visible — this is the agent's real, live browsing "
        "session, not a copy. Use browser_open next to navigate it."
    )


def browser_pane_hide(arguments: dict[str, Any]) -> str:
    """Hide the embedded browser pane. The underlying browsing session and
    its page state are unaffected — browser_open/click/fill/... keep
    working exactly as before; only the human-visible pane is hidden."""
    configured = _electron_pane_configured()
    if configured is None:
        return (
            "NOT CONFIGURED: the embedded browser pane needs the Electron "
            "shell — not available under the older pywebview shell or a "
            "plain headless server."
        )
    _cdp_port, pane_port = configured
    try:
        result = _pane_bridge_request(pane_port, "POST", "/hide")
    except Exception as exc:  # noqa: BLE001 - bridge failures, readable
        raise RuntimeError(
            f"BROWSER PANE HIDE FAILED: {type(exc).__name__}: {exc}"
        ) from exc
    if not result.get("ok"):
        raise RuntimeError(f"BROWSER PANE HIDE FAILED: {result}")
    _log("pane", "hidden")
    return "BROWSER PANE hidden."


def browser_screenshot(arguments: dict[str, Any]) -> str:
    name = (arguments.get("name") or "latest").strip()
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "latest"

    async def _shot():
        page = await _ensure_browser()
        _SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _SHOTS_DIR / f"{safe}.png"
        await page.screenshot(path=str(path), full_page=False)
        # 2026-09-14 (user-directed: "ability to display... screenshots
        # and images"): the endpoint below already worked (this session
        # already fixed a real crash in it); the real gap was that
        # nothing in this tool's own return text ever told the chat
        # renderer to show it. console.html's md() now understands real
        # markdown image syntax scoped to this app's own /api/... paths,
        # so this one line is the whole fix on this side.
        url = f"/api/browser/screenshot?name={safe}"
        return f"SCREENSHOT saved: {path}\n\n![screenshot]({url})"

    _log("screenshot", name)
    return _call(_shot)


# --------------------------------------------------------------------------- #
# Credential vault — 0600 JSON; passwords never leave it.
# --------------------------------------------------------------------------- #


def _vault_sites() -> list[str]:
    if not _VAULT_PATH.exists():
        return []
    try:
        data = json.loads(_VAULT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt vault is an honest empty
        return []
    return sorted(data.keys())


def browser_creds_store(arguments: dict[str, Any]) -> str:
    """Store credentials for a site. CONFIRMATION-GATED."""
    site = (arguments.get("site") or "").strip().lower()
    username = (arguments.get("username") or "").strip()
    password = arguments.get("password")
    if not site or not username or not password:
        return "ERROR: browser_creds_store requires site, username and password."
    if not _is_http_url(site):
        site = "https://" + site.lstrip("/")
    if not _is_http_url(site):
        return "REFUSED: the site must be a real domain (e.g. example.com or https://example.com)."
    netloc = urllib.parse.urlparse(site).netloc
    if not netloc or "." not in netloc or any(c.isspace() for c in netloc):
        return (
            "REFUSED: the site must be a real domain (e.g. example.com or "
            "https://example.com) — got a malformed host."
        )
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    if _VAULT_PATH.exists():
        try:
            data = json.loads(_VAULT_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    data[urllib.parse.urlparse(site).netloc] = {
        "username": username,
        "password": str(password),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _VAULT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(_VAULT_PATH, 0o600)
    except OSError:  # pragma: no cover - best-effort on exotic filesystems
        pass
    _log("creds", f"stored credentials for {urllib.parse.urlparse(site).netloc}")
    return (
        f"CREDENTIALS STORED for {urllib.parse.urlparse(site).netloc} "
        f"(user {username!r}). Password is kept in the 0600 vault and is "
        "never shown again."
    )


def browser_creds_list(arguments: dict[str, Any]) -> str:
    sites = _vault_sites()
    if not sites:
        return "VAULT: empty — no credentials stored yet (browser_creds_store)."
    lines = ["VAULT (usernames only, passwords never shown):"]
    try:
        data = json.loads(_VAULT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    for site in sites:
        u = (data.get(site) or {}).get("username", "?")
        lines.append(f"- {site}  (user {u!r})")
    return "\n".join(lines)


def browser_creds_forget(arguments: dict[str, Any]) -> str:
    """Remove stored credentials for a site. CONFIRMATION-GATED."""
    site = (arguments.get("site") or "").strip().lower()
    if not site:
        return "ERROR: browser_creds_forget requires a site."
    if not _VAULT_PATH.exists():
        return "VAULT: empty — nothing to forget."
    try:
        data = json.loads(_VAULT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "VAULT: unreadable — nothing removed (file may be corrupt)."
    netloc = urllib.parse.urlparse(site if _is_http_url(site) else "https://" + site).netloc
    if netloc not in data:
        return f"VAULT: no credentials stored for {netloc!r}."
    del data[netloc]
    _VAULT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _log("creds", f"removed credentials for {netloc}")
    return f"CREDENTIALS REMOVED for {netloc}."


def browser_signin(arguments: dict[str, Any]) -> str:
    """Log in to a site using its stored credentials. CONFIRMATION-GATED.

    Real flow: open the site, find the username + password fields, fill them
    from the vault, submit, and report where the page landed. Only ever runs
    after a human approves the site.
    """
    site = (arguments.get("site") or "").strip().lower()
    if not site:
        return "ERROR: browser_signin requires a site."
    if not _is_http_url(site):
        site = "https://" + site.lstrip("/")
    if not _is_http_url(site):
        return "REFUSED: the site must be a real domain."
    netloc = urllib.parse.urlparse(site).netloc
    if not _VAULT_PATH.exists():
        return f"NO CREDENTIALS for {netloc}: store them with browser_creds_store first."
    try:
        data = json.loads(_VAULT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "VAULT: unreadable — cannot sign in (file may be corrupt)."
    creds = data.get(netloc)
    if not creds:
        return f"NO CREDENTIALS for {netloc}: store them with browser_creds_store first."

    async def _signin():
        page = await _ensure_browser()
        async with _acting(page, "signin") as claim:
            return await _signin_steps(page, claim)

    async def _signin_steps(page: Any, claim: _NoClaim) -> str:
        await page.goto(site, timeout=30_000, wait_until="domcontentloaded")
        # Username field: email/username/text inputs, labeled or placeholder.
        user_sel = (
            "input[type='email'], input[type='text'][name*='user' i], "
            "input[type='text'][name*='email' i], input[type='text'][name*='login' i], "
            "input[name*='user' i], input[name*='email' i], input[autocomplete='username']"
        )
        pw_sel = "input[type='password'], input[autocomplete='current-password']"
        user_loc = page.locator(user_sel)
        pw_loc = page.locator(pw_sel)
        if await user_loc.count() == 0 or await pw_loc.count() == 0:
            return (
                f"SIGNIN BLOCKED on {netloc}: no username/password fields found "
                "— the site may use a multi-step or non-standard login. "
                "Use browser_snapshot to see the real form, then drive it with "
                "browser_fill / browser_click / browser_submit."
            )
        await claim.check()
        await user_loc.first.fill(creds["username"], timeout=8_000)
        await claim.check()
        await pw_loc.first.fill(creds["password"], timeout=8_000)
        url_before = page.url
        mark = _action_mark()
        sub = page.locator(
            "button[type='submit'], input[type='submit'], button:has-text('Sign in'), "
            "button:has-text('Log in'), button:has-text('Continue')"
        )
        await claim.check()
        if await sub.count() > 0:
            async with claim.pointing():
                await sub.first.click(timeout=8_000)
        else:
            await page.keyboard.press("Enter", timeout=8_000)
        page = await _settle_after_action(page, mark)
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass
        moved = "PAGE CHANGED" if page.url != url_before else "SAME PAGE"
        _log("signin", f"{netloc} -> {page.url} ({moved})")
        return (
            f"SIGNIN ATTEMPTED on {netloc} ({moved}). The result is on the "
            "page:\n" + await _page_summary(page)
        )

    _log("signin", netloc)
    return _call(_signin)


# --------------------------------------------------------------------------- #
# Latest screenshot — served by the webui at /api/browser/screenshot
# --------------------------------------------------------------------------- #


def latest_screenshot(name: str = "latest") -> Path | None:
    safe = "".join(c for c in (name or "latest") if c.isalnum() or c in "-_") or "latest"
    path = _SHOTS_DIR / f"{safe}.png"
    if path.exists():
        return path
    if name != "latest":
        latest = _SHOTS_DIR / "latest.png"
        if latest.exists():
            return latest
    return None


def close_browser() -> None:
    """Best-effort shutdown (called from server teardown paths)."""
    global _CONTEXT, _PAGE
    if _LOOP is None or _LOOP.is_closed():
        return

    async def _close():
        global _CONTEXT, _PAGE
        try:
            if _CONTEXT is not None:
                await _CONTEXT.close()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
        _CONTEXT = None
        _PAGE = None

    try:
        fut = asyncio.run_coroutine_threadsafe(_close(), _LOOP)
        fut.result(timeout=10)
    except Exception:  # noqa: BLE001 - teardown must never raise
        pass
