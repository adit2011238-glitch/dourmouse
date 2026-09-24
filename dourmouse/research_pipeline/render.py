"""Headless render for pages that are empty until JavaScript runs (R0-2,
finding #092).

A single-page app answers a plain fetch with an empty shell, and the
pipeline used to record that as a successful fetch of nothing, the dominant
failure mode for modern sites. This renders such a page in a short-lived
headless Chrome (Playwright, already a dependency for browser_agent; never
the user's shared browser page) and returns the rendered DOM.

Security: the browser never touches the network itself. Every request the
page makes, including the main document and every redirect hop, is fulfilled
from Python through ``net_guard.guarded_urlopen``, so rendering is exactly as
SSRF-safe as ``fetch_url``: a script cannot reach 169.254.169.254 or a LAN
address, and Playwright's own route() (which does not see redirects) is not
relied on for that. Images, media, fonts and stylesheets are not fetched at
all (no text in them).
"""

from __future__ import annotations

import asyncio
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from dourmouse import net_guard

RENDER_TIMEOUT_MS = 20_000
_MAX_RESOURCE_BYTES = 5 * 1024 * 1024
_SKIPPED_TYPES = {"image", "media", "font", "stylesheet"}
_HOP_BY_HOP = {"host", "connection", "content-length", "accept-encoding", "transfer-encoding"}


class RenderUnavailable(RuntimeError):
    """Rendering is switched off, or Playwright/Chrome is not usable here."""


@dataclass(frozen=True)
class RenderResult:
    html: str
    final_url: str
    requests_served: int
    requests_refused: tuple[str, ...]


def render_enabled() -> bool:
    return os.environ.get("DOURMOUSE_RESEARCH_RENDER", "1").strip() != "0"


def looks_like_an_empty_shell(markup: bytes, extracted_text: str) -> bool:
    """A page whose static HTML carries almost no readable text but does
    carry scripts: the client-side app has not run yet."""
    if len(extracted_text.strip()) >= 300:
        return False
    head = markup.lower()
    return b"<script" in head


def _guarded_fetch(url: str, method: str, headers: dict[str, str], body: bytes | None) -> tuple[str, int, dict[str, str], bytes]:
    """One request, through the SSRF guard. Returns ("ok"|"refused"|"failed",
    status, headers, body). Runs on a worker thread (blocking I/O)."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 -- scheme enforced by net_guard
    try:
        with net_guard.guarded_urlopen(req, timeout=10) as resp:
            data = resp.read(_MAX_RESOURCE_BYTES)
            status = int(getattr(resp, "status", 200) or 200)
            out = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
            return "ok", status, out, data
    except net_guard.FetchRefused:
        return "refused", 0, {}, b""
    except urllib.error.HTTPError as exc:
        data = exc.read()[:_MAX_RESOURCE_BYTES] if exc.fp else b""
        out = {k: v for k, v in (exc.headers or {}).items() if k.lower() not in _HOP_BY_HOP}
        return "ok", exc.code, out, data
    except (urllib.error.URLError, OSError):
        return "failed", 0, {}, b""


async def _render_async(url: str, timeout_ms: int) -> RenderResult:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RenderUnavailable(f"playwright is not installed: {exc}") from exc

    refused: list[str] = []
    served: list[int] = []

    async def serve(route: Any, request: Any) -> None:
        # Async handlers run concurrently, so a page's dozens of requests are
        # fetched in parallel (the sync API served them one by one: 42s on
        # docsify.js.org).
        if request.resource_type in _SKIPPED_TYPES:
            await route.abort("blockedbyclient")
            return
        target = request.url
        if target.startswith(("data:", "blob:")):
            await route.continue_()
            return
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        outcome, status, out, data = await asyncio.to_thread(
            _guarded_fetch, target, request.method, headers, request.post_data_buffer,
        )
        if outcome == "refused":
            refused.append(target)
            await route.abort("blockedbyclient")
        elif outcome == "failed":
            await route.abort("failed")
        else:
            served.append(1)
            await route.fulfill(status=status, headers=out, body=data)

    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(channel="chrome", headless=True)
            except PlaywrightError as exc:
                raise RenderUnavailable(f"could not start headless Chrome: {exc}") from exc
            try:
                context = await browser.new_context(java_script_enabled=True, service_workers="block")
                page = await context.new_page()
                await page.route("**/*", serve)
                try:
                    await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                except PlaywrightError:
                    # A page that never goes quiet still has a DOM worth
                    # reading; a page that never loaded at all has none.
                    if page.url in ("about:blank", ""):
                        raise
                return RenderResult(
                    html=await page.content(), final_url=page.url,
                    requests_served=len(served), requests_refused=tuple(refused),
                )
            finally:
                await browser.close()
    except RenderUnavailable:
        raise
    except PlaywrightError as exc:
        raise RenderUnavailable(f"render failed: {exc}") from exc


def render_page(url: str, timeout_ms: int = RENDER_TIMEOUT_MS) -> RenderResult:
    """Render ``url`` and return the resulting DOM. Raises RenderUnavailable
    when switched off or when Playwright/Chrome cannot run. Runs its own
    event loop on its own thread, so it works whether or not the caller is
    already inside a running asyncio loop."""
    if not render_enabled():
        raise RenderUnavailable("rendering is switched off (DOURMOUSE_RESEARCH_RENDER=0)")
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = asyncio.run(_render_async(url, timeout_ms))
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread
            box["error"] = exc

    t = threading.Thread(target=run, name="dourmouse-research-render", daemon=True)
    t.start()
    t.join(timeout_ms / 1000 + 30)
    if t.is_alive():
        raise RenderUnavailable("render did not finish in time")
    if "error" in box:
        raise box["error"]
    result: RenderResult = box["result"]
    return result
