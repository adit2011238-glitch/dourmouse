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
import json
import os
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

_UA_NOTE = (
    "Automation is the whole point of the browser agent — this is a "
    "headless Chrome driven by Dourmouse, never hidden."
)


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
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise RuntimeError(
            f"BROWSER TIMEOUT after {timeout:.0f}s — the page may be stuck. "
            "Use browser_wait or retry."
        ) from None


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


async def _ensure_browser() -> Any:
    """Return the live page, launching Chrome once per process."""
    global _CONTEXT, _PAGE, _LAUNCH_ERROR
    if _PAGE is not None and not _PAGE.is_closed():
        return _PAGE
    if _LAUNCH_ERROR:
        raise RuntimeError(_LAUNCH_ERROR)
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
    headless = os.environ.get("DOURMOUSE_BROWSER_HEADLESS", "1").strip() != "0"
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(channel="chrome", headless=headless)
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
    except Exception as exc:  # noqa: BLE001 - context failures, readable
        _LAUNCH_ERROR = f"BROWSER CONTEXT FAILED: {type(exc).__name__}: {exc}"
        raise RuntimeError(_LAUNCH_ERROR) from exc
    _CONTEXT = context
    _PAGE = page
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


async def _page_summary(page: Any, max_elems: int = 60) -> str:
    """Readable state of the current page: URL, title, interactive elements."""
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = "(no title)"
    url = page.url or "(none)"
    elems = await page.locator(
        "input, textarea, select, button, a[href], [role='button'], [role='link'], [role='textbox']"
    ).evaluate_all(
        """(els) => els.slice(0, 60).map(el => {
            const t = el.tagName.toLowerCase();
            const aria = el.getAttribute('aria-label') || '';
            const ph = el.getAttribute('placeholder') || '';
            const val = (el.value !== undefined && el.value !== null) ? String(el.value) : '';
            const txt = (el.innerText || el.textContent || '').trim().slice(0, 60);
            const href = el.getAttribute('href') || '';
            let name = aria || ph || txt || href;
            if (!name && (t === 'input' || t === 'textarea')) name = '<unlabeled ' + t + '>';
            if (!name && t === 'button') name = '<unlabeled button>';
            if (name) {
              return { t, name: name.slice(0, 60), val: val.slice(0, 40) };
            }
            return null;
          }).filter(Boolean)"""
    )
    lines = [f"URL: {url}", f"TITLE: {title}"]
    if elems:
        lines.append("ELEMENTS:")
        for e in elems:
            v = f"  value={e['val']!r}" if e["val"] else ""
            lines.append(f"- [{e['t']}] {e['name']!r}{v}")
    else:
        lines.append("ELEMENTS: none found (static page?)")
    try:
        body = await page.locator("body").inner_text(timeout=2000)
        sample = " ".join(body.split())[:900]
        if sample:
            lines.append(f"PAGE TEXT: {sample}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


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
        loc, how = await _find(page, target)
        try:
            await loc.fill(str(value), timeout=8_000)
        except Exception as exc:  # noqa: BLE001 - element may be read-only etc
            raise RuntimeError(
                f"BROWSER FILL FAILED: {type(exc).__name__}: {exc} (target={target!r})"
            ) from exc
        return f"FILLED {target!r} (via {how})."

    _log("fill", f"{target!r} <- {str(value)[:40]}")
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
        for label, value in fields.items():
            loc, how = await _find(page, str(label))
            try:
                await loc.fill(str(value), timeout=8_000)
                done.append(f"{label} (via {how})")
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"BROWSER FILL FORM FAILED on {label!r}: {type(exc).__name__}: {exc}"
                ) from exc
        return f"FILLED {len(done)} fields: " + ", ".join(done) + "."

    _log("fill_form", f"{len(fields)} fields")
    return _call(_fill)


def browser_click(arguments: dict[str, Any]) -> str:
    target = (arguments.get("target") or "").strip()

    async def _click():
        page = await _ensure_browser()
        loc, how = await _find(page, target)
        try:
            await loc.click(timeout=8_000)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"BROWSER CLICK FAILED: {type(exc).__name__}: {exc} (target={target!r})"
            ) from exc
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:  # noqa: BLE001 - a click need not navigate
            pass
        return f"CLICKED {target!r} (via {how}).\n" + await _page_summary(page)

    _log("click", target)
    return _call(_click)


def browser_select(arguments: dict[str, Any]) -> str:
    target = (arguments.get("target") or "").strip()
    value = (arguments.get("value") or "").strip()
    if not value:
        return "ERROR: browser_select requires a value."

    async def _select():
        page = await _ensure_browser()
        loc, how = await _find(page, target)
        try:
            await loc.select_option(value, timeout=8_000)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"BROWSER SELECT FAILED: {type(exc).__name__}: {exc} (target={target!r})"
            ) from exc
        return f"SELECTED {value!r} on {target!r} (via {how})."

    _log("select", f"{target!r} <- {value}")
    return _call(_select)


def browser_press(arguments: dict[str, Any]) -> str:
    key = (arguments.get("key") or "").strip()
    if not key:
        return "ERROR: browser_press requires a key (Enter, Tab, Escape...)."

    async def _press():
        page = await _ensure_browser()
        try:
            await page.keyboard.press(key, timeout=8_000)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"BROWSER PRESS FAILED: {type(exc).__name__}: {exc} (key={key!r})"
            ) from exc
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
        url_before = page.url
        # Prefer Enter on the focused field, else click the submit control.
        try:
            focused = await page.evaluate("document.activeElement && document.activeElement.tagName")
        except Exception:  # noqa: BLE001
            focused = None
        if focused in ("INPUT", "TEXTAREA", "SELECT"):
            await page.keyboard.press("Enter", timeout=8_000)
        else:
            sub = page.locator(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Sign in'), button:has-text('Log in'), "
                "button:has-text('Create account'), button:has-text('Continue')"
            )
            if await sub.count() > 0:
                await sub.first.click(timeout=8_000)
            else:
                raise RuntimeError(
                    "ERROR: no submit control found — nothing was submitted. "
                    "Use browser_snapshot to inspect the form."
                )
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


def browser_screenshot(arguments: dict[str, Any]) -> str:
    name = (arguments.get("name") or "latest").strip()
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "latest"

    async def _shot():
        page = await _ensure_browser()
        _SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _SHOTS_DIR / f"{safe}.png"
        await page.screenshot(path=str(path), full_page=False)
        return (
            f"SCREENSHOT saved: {path} — view it in the app at "
            f"/api/browser/screenshot?name={safe}"
        )

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
        await user_loc.first.fill(creds["username"], timeout=8_000)
        await pw_loc.first.fill(creds["password"], timeout=8_000)
        url_before = page.url
        sub = page.locator(
            "button[type='submit'], input[type='submit'], button:has-text('Sign in'), "
            "button:has-text('Log in'), button:has-text('Continue')"
        )
        if await sub.count() > 0:
            await sub.first.click(timeout=8_000)
        else:
            await page.keyboard.press("Enter", timeout=8_000)
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
